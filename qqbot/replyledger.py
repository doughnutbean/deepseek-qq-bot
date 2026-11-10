"""回复台账：她每说一句，群里接没接住，先只记不算。

2026-10-30 主人的想法：接梗那套「发出去 → 等下一条消息 → 判断」不该只管梗，
每一条回复都该有下落。这条先做**只记录**，不改她的行为：

- 记下每句发出去的时间和群；
- 群里下一条消息来时判一次：接住（有人接话）/ 被纠正 / 冷场（没人理）；
- 只落统计，不喂给模型、不影响语气 —— 先看一两周真实分布再定阈值。

为什么不直接让她"看见评价"：群里没人理一句很常见，不一定是说错；
她又是怯生生的性格，天天看见分数会往讨好和不敢说上靠。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 一条回复等多久算"没人接"
WINDOW = 120.0
# 台账留多久、留多少条
KEEP_DAYS = 14
MAX_ROWS = 5000

_CORRECT = ("不是这意思", "不是这个意思", "理解错", "说错", "答非所问", "没听懂", "你错了")


class ReplyLedger:
    """记录每条回复的下落。只读不写行为，阈值调好了再谈怎么用。"""

    def __init__(self, path: Path | None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path is not None else None
        self.enabled = bool(enabled)
        self._pending: dict[str, dict[str, Any]] = {}
        self._rows: list[dict[str, Any]] = []
        self._load()

    # ---------- 写 ----------

    def record(self, group_id: str, text: str) -> None:
        """她刚发出一条回复：记下等待判决。"""
        if not self.enabled:
            return
        gid = str(group_id or "")
        if not gid:
            return
        self._pending[gid] = {
            "ts": time.time(),
            "len": len(text or ""),
        }

    def settle(self, group_id: str, text: str) -> str | None:
        """群里来了一条消息：给上一条回复判个下落。返回结局（没在等就是 None）。"""
        if not self.enabled:
            return None
        gid = str(group_id or "")
        pending = self._pending.pop(gid, None)
        if not pending:
            return None
        now = time.time()
        gap = now - float(pending.get("ts") or 0)
        if gap > WINDOW:
            return None  # 过了窗口：不记，免得拿闲聊当评价
        body = str(text or "")
        if any(word in body for word in _CORRECT):
            outcome = "corrected"
        elif body.strip():
            outcome = "continued"
        else:
            outcome = "cold"
        self._rows.append(
            {
                "ts": int(now),
                "gid": gid,
                "gap": round(gap),
                "len": int(pending.get("len") or 0),
                "outcome": outcome,
            }
        )
        self._trim()
        self._save()
        return outcome

    def sweep(self) -> int:
        """把超时没动静的做冷场收尾（由定时任务调，别在消息路径上跑）。"""
        if not self.enabled:
            return 0
        now = time.time()
        done = 0
        for gid, pending in list(self._pending.items()):
            gap = now - float(pending.get("ts") or 0)
            if gap <= WINDOW:
                continue
            self._pending.pop(gid, None)
            self._rows.append(
                {
                    "ts": int(now),
                    "gid": gid,
                    "gap": round(gap),
                    "len": int(pending.get("len") or 0),
                    "outcome": "cold",
                }
            )
            done += 1
        if done:
            self._trim()
            self._save()
        return done

    # ---------- 读 ----------

    def stats(self, days: int = 7) -> dict[str, Any]:
        cutoff = int(time.time() - days * 86400)
        rows = [r for r in self._rows if int(r.get("ts") or 0) >= cutoff]
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get("outcome") or "?")
            counts[key] = counts.get(key, 0) + 1
        total = sum(counts.values())
        return {
            "days": days,
            "total": total,
            "counts": counts,
            "rate": {k: round(v / total, 2) for k, v in counts.items()} if total else {},
            "pending": len(self._pending),
        }

    # ---------- 存 ----------

    def _trim(self) -> None:
        cutoff = int(time.time() - KEEP_DAYS * 86400)
        self._rows = [r for r in self._rows if int(r.get("ts") or 0) >= cutoff]
        if len(self._rows) > MAX_ROWS:
            self._rows = self._rows[-MAX_ROWS:]

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("回复台账读不出来，先当空的", exc_info=True)
            return
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            self._rows = [r for r in data["rows"] if isinstance(r, dict)][-MAX_ROWS:]

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"rows": self._rows}, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            log.warning("回复台账写不进去", exc_info=True)
