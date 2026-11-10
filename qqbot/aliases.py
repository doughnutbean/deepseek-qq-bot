"""群友外号对照。

群里同一个人可能有七八个叫法：群名片、昵称、外号、昵称缩写。
模型只看到当下那条消息的显示名，就会把「明哥」当成陌生人。

这个模块维护一份 QQ 号 → {昵称, 外号[]} 的表，聊天时注入两句话：

- 你在回谁，他还叫什么
- 别人提到的「明哥」指的是谁

文件是纯 JSON，改完不用重启（按修改时间热加载）：

    {
      "10001": {"nick": "帕鲁", "aliases": ["主人", "帕鲁"]},
      "123456789":  {"nick": "小明", "aliases": ["明哥", "小明明"]}
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class AliasBook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._mtime = 0.0
        self.reload()

    # ---------- 读盘 ----------

    def reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._data, self._mtime = {}, 0.0
            return
        if mtime == self._mtime:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("外号表读不出来（%s），这次先不用", exc)
            return
        if not isinstance(raw, dict):
            return
        clean: dict[str, dict[str, Any]] = {}
        for uid, item in raw.items():
            if not isinstance(item, dict):
                continue
            aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()]
            nick = str(item.get("nick") or "").strip()
            if aliases or nick:
                clean[str(uid)] = {"nick": nick, "aliases": aliases}
        self._data, self._mtime = clean, mtime
        log.info("外号表已加载：%d 人", len(clean))

    def _fresh(self) -> dict[str, dict[str, Any]]:
        self.reload()
        return self._data

    # ---------- 查询 ----------

    def aliases_of(self, uid: Any) -> list[str]:
        item = self._fresh().get(str(uid))
        return list(item["aliases"]) if item else []

    def nick_of(self, uid: Any) -> str:
        item = self._fresh().get(str(uid))
        return str(item.get("nick") or "") if item else ""

    def owner_of_alias(self, alias: str) -> str:
        """这个外号是谁的？返回他的昵称（认不出返回空串）。"""
        alias = (alias or "").strip()
        if not alias:
            return ""
        for item in self._fresh().values():
            if alias in item["aliases"]:
                return str(item.get("nick") or "")
        return ""

    def all_aliases(self) -> dict[str, str]:
        """外号 → 昵称。"""
        out: dict[str, str] = {}
        for item in self._fresh().values():
            for alias in item["aliases"]:
                out[alias] = str(item.get("nick") or "")
        return out

    # ---------- 拼提示 ----------

    def prompt_lines(
        self, speaker_uid: Any, speaker_nick: str, text: str, limit: int = 4
    ) -> list[str]:
        """给这一轮要注入的几行说明（没命中就返回空列表，0 开销）。"""
        lines: list[str] = []
        mine = self.aliases_of(speaker_uid)
        if mine:
            lines.append(f"（说话的是「{speaker_nick}」，他也叫：{'、'.join(mine[:6])}）")

        body = text or ""
        seen = 0
        for alias, nick in self.all_aliases().items():
            if seen >= limit:
                break
            if alias == speaker_nick or alias not in body:
                continue
            # 是他自己的外号就跳过（上面已经说过）
            if nick == speaker_nick:
                continue
            lines.append(f"（「{alias}」指的是「{nick}」）")
            seen += 1
        return lines
