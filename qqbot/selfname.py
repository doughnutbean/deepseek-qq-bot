"""她自己的名字：运行时直读，不写死。

2026-10-30 的教训：主人的调参指令把昵称改成「帕鲁的大肥鱼二号机」，
`set_nickname` 只调了 QQ 资料，她自己的认知里根本没有这个名字 ——
群里喊「二号机」，她要先说「本鱼怎么不知道自己还有编号呀」；
再后来干脆不理（28 小时后的「二号机你不要给我搞笑…」只回了一句尾巴）。

所以名字不做人设知识、也不做静态清单，而是每轮现读：
QQ 昵称 + 本群群名片。改名之后新名字立刻生效，旧名字自动失效；
旧名只留一小段时间，用来回答「还喊你旧名」的场合，过期就忘。

判定「在喊她」也不建别名表：拿当前名字切出来的片段去匹配。
「二号机」是「帕鲁的大肥鱼二号机」的一段 —— 名字一改，这一段就跟着没了，
不会出现「改没了还在认」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 名字里怎么切：中文名常带「的」，也有 ·/-/_ 分隔
_SPLIT = re.compile(r"[的·・\-—_/|]+|\s+")
# 太短的片段不当名字：两字的「大肥」「号机」误伤太多
MIN_FRAG = 3
# 一个名字最多切这么多片段，防止长名字把提示词撑爆
MAX_FRAG = 12
# 多久重读一次（秒）。改名的当场由 record_change() 直接覆盖，不必等它
DEFAULT_TTL = 1800
# 改名后多久内还认旧名（秒）
OLD_KEEP = 7 * 86400
# 拉失败先晾一会儿，别每条消息都去打接口
FAIL_BACKOFF = 900


def _segments(name: str) -> list[str]:
    return [s for s in _SPLIT.split(str(name or "").strip()) if s]


def fragments_of(name: str) -> list[str]:
    """一个名字能怎么被叫：全名、各段、以及每段的头尾。

    例：「帕鲁的大肥鱼二号机」→ 全名 / 大肥鱼二号机 / 大肥鱼 / 二号机 …
    只收 >= 3 字的片段：两字的「号机」会让「一号机」也算成在喊她。
    """
    name = str(name or "").strip()
    if not name:
        return []
    out: list[str] = []

    def add(word: str) -> None:
        word = word.strip()
        if MIN_FRAG <= len(word) <= 12 and word not in out:
            out.append(word)

    add(name)
    for seg in _segments(name):
        add(seg)
        for size in range(MIN_FRAG, len(seg)):
            add(seg[:size])
            add(seg[-size:])
    return out[:MAX_FRAG]


class SelfName:
    """她当前的名字（QQ 昵称 + 各群名片）与曾用名。"""

    def __init__(
        self,
        bot_qq: str = "",
        fallback: Any = (),
        path: Path | None = None,
        *,
        ttl: float = DEFAULT_TTL,
        old_keep: float = OLD_KEEP,
        clock: Any = time.time,
    ) -> None:
        self.bot_qq = str(bot_qq or "")
        self.fallback = _clean_names(fallback)
        self.path = Path(path) if path is not None else None
        self.ttl = max(60.0, float(ttl))
        self.old_keep = max(0.0, float(old_keep))
        self._clock = clock
        self._nick = ""
        self._cards: dict[str, dict[str, Any]] = {}
        self._old: list[dict[str, Any]] = []
        self._fetched_at = 0.0
        self._fetching: set[str] = set()
        self._fail_until = 0.0
        self._load()

    # ---------- 读 ----------

    def names(self, group_id: str = "") -> list[str]:
        """当前叫什么：QQ 昵称在前，本群名片在后。读不到才退回兜底清单。"""
        out: list[str] = []
        if self._nick:
            out.append(self._nick)
        card = self.card(group_id)
        if card and card not in out:
            out.append(card)
        return out or list(self.fallback)

    def card(self, group_id: str = "") -> str:
        row = self._cards.get(str(group_id or ""))
        return str(row.get("card") or "") if row else ""

    def fragments(self, group_id: str = "") -> list[str]:
        out: list[str] = []
        for name in self.names(group_id):
            for word in fragments_of(name):
                if word not in out:
                    out.append(word)
        # 兜底清单里的叫法（大肥鱼 / 肥鱼 / 鲸鱼）也认，它们是群里叫惯的
        for word in self.fallback:
            if word not in out:
                out.append(word)
        return out

    def is_called(self, text: str, group_id: str = "") -> str | None:
        body = _strip_at(str(text or "")).lower()
        if not body:
            return None
        for word in self.fragments(group_id):
            if word.lower() in body:
                return word
        return None

    def old_names(self, group_id: str = "") -> list[str]:
        """还没过期的曾用名（不含现在的名字）。"""
        now = self._clock()
        out: list[str] = []
        for row in self._old:
            name = str(row.get("name") or "")
            if not name or name in self.names(group_id):
                continue
            if now - float(row.get("ts") or 0) > self.old_keep:
                continue
            if name not in out:
                out.append(name)
        return out

    def note(self, text: str = "", group_id: str = "") -> str:
        """这一轮告诉她「你现在叫什么」：只给事实和推法，不给写死的结论。"""
        names = self.names(group_id)
        if not names:
            return ""
        card = self.card(group_id)
        shown = names[0]
        parts = [f"（你现在的名字是「{shown}」"]
        if card and card != shown:
            parts.append(f"，本群名片「{card}」")
        parts.append("（系统直读，随时会变）。别人从名字里取一段喊你")
        example = _example_fragment(shown)
        if example:
            parts.append(f"（比如「{example}」这样）")
        parts.append("，也是在喊你，不是叫别人；名字以这里为准，别按记忆里的旧名字。）")
        line = "".join(parts)

        low = _strip_at(str(text or "")).lower()
        called_old: list[str] = []
        for name in self.old_names(group_id):
            # 叫旧名时通常只叫一段（「二号机」），不会叫全名
            if any(word.lower() in low for word in fragments_of(name)):
                called_old.append(name)
        if called_old:
            line += (
                f"（「{called_old[0]}」是你以前的名字，现在不叫这个了；"
                f"有人还这么喊，就答一句「本鱼现在叫{shown}啦」，别一直认旧名。）"
            )
        return line

    def status(self) -> dict[str, Any]:
        """调试用：CLI / 日志看现在认哪些名字。"""
        return {
            "bot_qq": self.bot_qq,
            "nick": self._nick,
            "cards": {gid: str(row.get("card") or "") for gid, row in self._cards.items()},
            "fallback": list(self.fallback),
            "old": self.old_names(),
            "fetched_at": self._fetched_at,
        }

    # ---------- 改 ----------

    def record_change(self, nickname: str) -> None:
        """主人刚改了昵称：立刻生效，旧名进曾用名。

        不写「二号机就是在喊你」这种句子 —— 名字本身换了，片段自然跟着换。
        """
        name = str(nickname or "").strip()
        if not name:
            return
        if self._nick and self._nick != name:
            self._old.append({"name": self._nick, "ts": self._clock()})
            self._old = self._old[-8:]
        self._nick = name
        self._fetched_at = self._clock()
        self._save()

    # ---------- 拉取 ----------

    def ensure(self, onebot: Any, group_id: str = "") -> None:
        """过期就在后台读一次；消息路径不等结果（和名册一个套路）。"""
        if onebot is None or not self.bot_qq:
            return
        gid = str(group_id or "")
        if gid in self._fetching or self._fetching == {"*"}:
            return
        if not self._stale(gid):
            return
        if self._clock() < self._fail_until:
            return
        try:
            asyncio.create_task(self.refresh(onebot, gid))
        except RuntimeError:  # 没有事件循环（测试/离线脚本）：跳过
            return

    def _stale(self, group_id: str) -> bool:
        now = self._clock()
        if not self._nick or now - self._fetched_at > self.ttl:
            return True
        if group_id and str(group_id) not in self._cards:
            return True
        row = self._cards.get(str(group_id))
        if row and now - float(row.get("ts") or 0) > self.ttl:
            return True
        return False

    async def refresh(self, onebot: Any, group_id: str = "") -> bool:
        gid = str(group_id or "")
        key = gid or "*"
        if key in self._fetching:
            return False
        self._fetching.add(key)
        ok = False
        try:
            ok = await self._refresh_nick(onebot) or ok
            if gid:
                ok = await self._refresh_card(onebot, gid) or ok
        except Exception as exc:  # noqa: BLE001 - 读不到名字不能拖垮聊天
            log.info("读自己的名字失败（%s）：%s", key, exc)
        finally:
            self._fetching.discard(key)
        if ok:
            self._fail_until = 0.0
            self._save()
        else:
            self._fail_until = self._clock() + FAIL_BACKOFF
        return ok

    async def _refresh_nick(self, onebot: Any) -> bool:
        payload = await onebot.call("get_login_info", {}, timeout=6)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return False
        name = str(data.get("nickname") or "").strip()
        if not name or name == self._nick:
            return False
        if self._nick and name != self._nick:
            self._old.append({"name": self._nick, "ts": self._clock()})
            self._old = self._old[-8:]
        self._nick = name
        self._fetched_at = self._clock()
        log.info("读到自己的昵称：%s", name)
        return True

    async def _refresh_card(self, onebot: Any, group_id: str) -> bool:
        payload = await onebot.call(
            "get_group_member_info",
            {"group_id": int(group_id), "user_id": int(self.bot_qq), "no_cache": False},
            timeout=8,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return False
        card = str(data.get("card") or "").strip()
        nick = str(data.get("nickname") or "").strip()
        name = card or nick
        if not name:
            return False
        self._cards[group_id] = {"card": name, "ts": self._clock()}
        if not self._nick:
            self._nick = nick or name
            self._fetched_at = self._clock()
        return True

    # ---------- 存档 ----------

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("自己的名字存档读不出来，先当空的", exc_info=True)
            return
        if not isinstance(data, dict):
            return
        self._nick = str(data.get("nick") or "")
        self._fetched_at = float(data.get("fetched_at") or 0)
        cards = data.get("cards")
        if isinstance(cards, dict):
            for gid, row in cards.items():
                if isinstance(row, dict) and row.get("card"):
                    self._cards[str(gid)] = {
                        "card": str(row.get("card")),
                        "ts": float(row.get("ts") or 0),
                    }
        old = data.get("old")
        if isinstance(old, list):
            self._old = [r for r in old if isinstance(r, dict) and r.get("name")][-8:]

    def _save(self) -> None:
        if self.path is None:
            return
        payload = {
            "nick": self._nick,
            "fetched_at": self._fetched_at,
            "cards": self._cards,
            "old": self._old,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.warning("自己的名字存档写不进去", exc_info=True)


# @昵称 是别人（QQ 的「回复」会把 @昵称 写成普通文字）：先剥掉再判
_AT_SPAN = re.compile(
    r"[@＠][^\s，,。.！!？?：:；;、~～…（）()\[\]【】「」《》<>\"“”'’]*"
)


def _strip_at(text: str) -> str:
    return _AT_SPAN.sub("", text or "")


def _clean_names(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw or ():
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _example_fragment(name: str) -> str:
    """举一个「从名字里取一段」的例子：优先末尾那截短号（二号机）。

    取 3~4 字的后缀，不是整段：整段只是名字本身，模型学不到「取一段也算」。
    """
    segs = _segments(name)
    if not segs:
        return ""
    tail = segs[-1]
    for size in (3, 4):
        if len(tail) > size:
            return tail[-size:]
    return tail if 3 <= len(tail) <= 12 else ""
