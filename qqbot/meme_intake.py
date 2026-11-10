"""记梗前的待确认状态：主人发「记梗 X」→ 查一次 → 等主人回「入库 / 不入库」。

一个主人同时只挂一个待确认；状态落盘，重启后还能接着确认。
只存查到的结论和出处，不存聊天正文。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TTL_SECONDS = 2 * 3600


class MemeIntake:
    def __init__(self, path: Path, *, enabled: bool = True, ttl: float = TTL_SECONDS) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.ttl = max(60.0, float(ttl))
        self._pending: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        pending = raw.get("pending") or {}
        if not isinstance(pending, dict):
            return
        for owner, item in pending.items():
            if not isinstance(item, dict):
                continue
            if not str(item.get("term") or "").strip():
                continue
            self._pending[str(owner)] = item

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(
                    {"version": 1, "pending": self._pending},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("记梗待确认状态写入失败：%s", exc)

    def _purge(self, now: float) -> None:
        for owner in list(self._pending):
            if now - float(self._pending[owner].get("ts") or 0) > self.ttl:
                self._pending.pop(owner, None)

    def start(
        self,
        owner: Any,
        term: str,
        *,
        verdict: bool | None,
        note: str,
        group_id: str = "",
        is_group: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {}
        owner_s = str(owner or "")
        if not owner_s:
            return {}
        moment = time.time() if now is None else float(now)
        self._purge(moment)
        item = {
            "term": str(term or "").strip(),
            "verdict": verdict,
            "note": " ".join(str(note or "").split())[:100],
            "group_id": str(group_id or ""),
            "is_group": bool(is_group),
            "ts": moment,
        }
        self._pending[owner_s] = item
        self._save()
        return item

    def get(self, owner: Any, *, now: float | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        owner_s = str(owner or "")
        moment = time.time() if now is None else float(now)
        self._purge(moment)
        return self._pending.get(owner_s)

    def clear(self, owner: Any) -> None:
        owner_s = str(owner or "")
        if self._pending.pop(owner_s, None) is not None:
            self._save()

    def status(self) -> dict[str, int | bool]:
        return {"enabled": self.enabled, "pending": len(self._pending)}
