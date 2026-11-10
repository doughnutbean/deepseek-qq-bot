"""在正在陪看的 B 站直播间发一条弹幕（写操作，默认关）。

为什么要单独一个模块：发弹幕是**会留痕的写操作**，牵涉
- csrf（`bili_jct`，和 SESSDATA 必须成对）；
- 字数上限（普通用户 20 字，超了接口直接拒）；
- 频率与每日额度（B 站风控 + 自己的保险）；
- 已经在接口这一层做完了，app 只管「要不要发、发哪句」。

设计原则：
- **只发到当前陪看的房间**，不认别的房号；
- 内容优先从本地克里斯语料（`data/chris_comments.db`）里挑 ≤ 上限的短句；
- 同一句 24 小时内不重复；同一房间两条之间至少 `interval` 秒；每天不超过 `daily`；
- 失败只记日志、返回原因，**不重试**——风控时越重试越糟。
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

SEND_URL = "https://api.live.bilibili.com/msg/send"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 语料里能当弹幕的几类
_PICK_KINDS = ("joke", "catchphrase", "meme")
# 明显不适合当弹幕的：带链接、@、换行、QQ 号
_BAD = re.compile(r"https?://|www\.|\n|\r|@\d{5,}|\d{9,}")
# 常见错误码 → 人话
_CODES = {
    -101: "账号没登录",
    -111: "csrf 校验失败（bili_jct 过期或和 SESSDATA 不是一对）",
    10030: "被 B 站限制了（弹幕频率/内容风控），过一会儿再试",
    10031: "这个房间不允许发弹幕",
    1003212: "弹幕被内容过滤拦了，换一句",
}


class DanmakuError(RuntimeError):
    """发不出去，message 是可以直接给主人看的人话。"""


def pick_from_corpus(
    db_path: str | Path,
    max_chars: int,
    avoid: set[str] | None = None,
    limit: int = 80,
) -> str:
    """从克里斯语料里挑一条不超过 `max_chars` 的短句；挑不到返回空串。

    纯本地读 sqlite，0 token；不做裁剪（半句话当弹幕很怪），宁可换一条。
    """
    path = Path(db_path)
    if not path.is_file():
        return ""
    avoid = avoid or set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select text from candidate "
                "where kind in (%s) and length(text) between 4 and ? "
                "order by random() limit ?" % ",".join("?" * len(_PICK_KINDS)),
                (*_PICK_KINDS, int(max_chars), max(1, int(limit))),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        log.debug("读克里斯语料失败", exc_info=True)
        return ""

    pool = [
        str(row[0]).strip()
        for row in rows
        if row and row[0] and str(row[0]).strip() not in avoid
    ]
    pool = [text for text in pool if not _BAD.search(text)]
    return random.choice(pool) if pool else ""


class LiveDanmaku:
    """发弹幕的薄客户端 + 额度账本（落 json，重启不清零）。"""

    def __init__(
        self,
        *,
        sessdata: str,
        jct: str,
        state_path: str | Path,
        corpus_path: str | Path,
        max_chars: int = 20,
        interval: float = 90.0,
        daily: int = 10,
        timeout: float = 15.0,
        enabled: bool = True,
    ) -> None:
        self.sessdata = str(sessdata or "").strip()
        self.jct = str(jct or "").strip()
        self.state_path = Path(state_path)
        self.corpus_path = Path(corpus_path)
        self.max_chars = max(4, int(max_chars))
        self.interval = max(20.0, float(interval))
        self.daily = max(0, int(daily))
        self.timeout = max(5.0, float(timeout))
        self.enabled = bool(enabled)
        self._log: list[dict[str, Any]] = self._load()

    # ---------- 账本 ----------

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        cutoff = time.time() - 48 * 3600
        return [row for row in data if isinstance(row, dict) and float(row.get("ts") or 0) >= cutoff]

    def _save(self) -> None:
        cutoff = time.time() - 48 * 3600
        self._log = [row for row in self._log if float(row.get("ts") or 0) >= cutoff]
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._log, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            log.debug("弹幕账本写不进去", exc_info=True)

    def sent_texts(self, hours: float = 24.0) -> set[str]:
        cutoff = time.time() - hours * 3600
        return {
            str(row.get("text") or "")
            for row in self._log
            if float(row.get("ts") or 0) >= cutoff
        }

    def sent_today(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        return sum(
            1
            for row in self._log
            if time.strftime("%Y-%m-%d", time.localtime(float(row.get("ts") or 0))) == today
        )

    def can_send(self, now: float | None = None) -> tuple[bool, str]:
        """现在能不能发；不能就返回人话原因（0 网络请求）。"""
        now = now if now is not None else time.time()
        if not self.enabled:
            return False, "发弹幕的开关是关着的"
        if not self.sessdata or not self.jct:
            return False, "缺 BILI_SESSDATA 或 BILI_JCT，发不了"
        if self.daily and self.sent_today(now) >= self.daily:
            return False, f"今天已经发了 {self.sent_today(now)} 条，到上限了"
        last = max((float(row.get("ts") or 0) for row in self._log), default=0.0)
        if last and now - last < self.interval:
            wait = int(self.interval - (now - last)) + 1
            return False, f"刚发过，还要等 {wait} 秒"
        return True, ""

    # ---------- 发送 ----------

    def _headers(self, room: str) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": f"https://live.bilibili.com/{room}",
            "Origin": "https://live.bilibili.com",
            "Cookie": f"SESSDATA={self.sessdata}; bili_jct={self.jct}",
        }

    async def send(self, room: str, text: str) -> dict[str, Any]:
        """发一条；返回 {ok, code, message, text}，不抛（除了参数错误）。"""
        room = str(room or "").strip()
        text = str(text or "").strip()
        if not room or not text:
            raise DanmakuError("房间号或内容为空")
        if len(text) > self.max_chars:
            raise DanmakuError(
                f"这条 {len(text)} 字，超过 B 站上限 {self.max_chars} 字"
            )

        data = {
            "color": "16777215",
            "fontsize": "25",
            "mode": "1",
            "msg": text,
            "rnd": str(int(time.time())),
            "roomid": room,
            "csrf": self.jct,
            "csrf_token": self.jct,
            "bubble": "0",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    SEND_URL, data=data, headers=self._headers(room)
                )
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 网络问题不能把聊天打挂
            log.warning("发弹幕失败（网络）：%s", exc)
            return {"ok": False, "code": None, "message": f"网络错误：{type(exc).__name__}"}

        code = payload.get("code")
        ok = code == 0
        if ok:
            self._log.append({"ts": time.time(), "room": room, "text": text})
            self._save()
            log.info("弹幕已发：房间 %s「%s」", room, text)
            return {"ok": True, "code": 0, "message": "ok", "text": text, "room": room}

        hint = _CODES.get(code) or str(payload.get("message") or "未知原因")
        log.info("弹幕没发出去：房间 %s code=%s %s", room, code, hint)
        return {"ok": False, "code": code, "message": hint, "text": text}

    async def send_picked(self, room: str) -> dict[str, Any]:
        """从语料挑一条再发（24 小时内发过的不再挑）。"""
        allow, why = self.can_send()
        if not allow:
            return {"ok": False, "code": None, "message": why}
        text = pick_from_corpus(
            self.corpus_path, self.max_chars, avoid=self.sent_texts(24)
        )
        if not text:
            return {"ok": False, "code": None, "message": "语料里没挑到合适的短句"}
        return await self.send(room, text)


__all__ = ["DanmakuError", "LiveDanmaku", "pick_from_corpus"]
