"""主动私信：让她偶尔先开口。

照 silence-ping 的思路 —— 判断先走本地规则，只有真有可能开口才问模型一次：

1. **免打扰时段**（默认 00:00-07:00）不发
2. **冷场门槛**：对方至少多少分钟没说话才值得找他
3. **个人冷却**：上次主动找过他之后，隔多久才会再考虑
4. **绝不连发**：上次主动发的他没回，就不再发第二句，直到他先开口
5. **每日上限**

状态落在 `dm_state.json`，重启不清零。
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .qzone_guard import parse_quiet

log = logging.getLogger(__name__)


class ProactiveDM:
    def __init__(
        self,
        path: Path,
        enabled: bool = False,
        wake_min: int = 15,
        wake_max: int = 30,
        idle_minutes: int = 30,
        cooldown_minutes: int = 120,
        quiet_hours: str = "00:00-07:00",
        daily_cap: int = 3,
        dry_run: bool = False,
        followup_hours: float = 0.0,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.wake_min = max(1, wake_min)
        self.wake_max = max(self.wake_min, wake_max)
        self.idle_seconds = max(0, idle_minutes) * 60
        self.cooldown = max(0, cooldown_minutes) * 60
        self.quiet = parse_quiet(quiet_hours)
        self.daily_cap = max(0, daily_cap)
        self.dry_run = dry_run
        # 问了没回，隔这么久可以再提一次（0 = 永不追问）
        self.followup_seconds = max(0.0, followup_hours) * 3600
        self._state: dict[str, Any] = self._load()

    # ---------- 存档 ----------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "last_sent": float(raw.get("last_sent") or 0),
            "last_text": str(raw.get("last_text") or ""),
            "answered": bool(raw.get("answered", True)),
            "day": str(raw.get("day") or ""),
            "day_count": int(raw.get("day_count") or 0),
            "followups": int(raw.get("followups") or 0),
        }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("主动私信状态写不进去：%s", exc)

    # ---------- 时间 ----------

    def next_wake(self) -> float:
        """下一次醒来的间隔（秒），随机，免得像打卡。"""
        return random.uniform(self.wake_min * 60, self.wake_max * 60)

    def in_quiet(self, now: float | None = None) -> bool:
        if not self.quiet:
            return False
        moment = datetime.fromtimestamp(now if now is not None else time.time())
        minute = moment.hour * 60 + moment.minute
        for start, end in self.quiet:
            if start <= end:
                if start <= minute < end:
                    return True
            elif minute >= start or minute < end:
                return True
        return False

    def _today(self, now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    # ---------- 判断 ----------

    def can_consider(
        self, last_user_ts: float, now: float | None = None, pace: float = 1.0
    ) -> tuple[bool, str]:
        """本地规则：值不值得为这件事问一次模型。

        规则 2、3 会被 pace 放大缩小 —— 主人说「多主动点」就直接生效。
        规则 4（不追第二句）留了一个例外：如果上次问的是"要求"且过了
        followup_seconds，允许再提一次（只一次）。
        """
        now = now if now is not None else time.time()
        pace = max(0.2, min(5.0, pace))
        if not self.enabled:
            return False, "没开"
        if self.in_quiet(now):
            return False, "免打扰时段"

        if not self._state["answered"]:
            last = self._state["last_sent"]
            can_follow = (
                self.followup_seconds > 0
                and self._state.get("followups", 0) < 1
                and last
                and now - last >= self.followup_seconds
            )
            if not can_follow:
                return False, "上次主动发的还没回，不追第二句"

        idle_need = self.idle_seconds / pace if self.idle_seconds else 0
        if idle_need and last_user_ts:
            idle = now - last_user_ts
            if idle < idle_need:
                return False, f"才冷场 {int(idle // 60)} 分钟"

        cool = self.cooldown / pace if self.cooldown else 0
        last = self._state["last_sent"]
        if cool and last and now - last < cool:
            return False, f"离上次主动还不到 {int(cool // 60)} 分钟"

        if self.daily_cap:
            same_day = self._state["day"] == self._today(now)
            used = self._state["day_count"] if same_day else 0
            if used >= self.daily_cap:
                return False, f"今天已经主动 {used} 次了"

        return True, ""

    # ---------- 记录 ----------

    def note_sent(self, text: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        day = self._today(now)
        same_day = self._state["day"] == day
        # 上一次发出去还没回，这次才算"追问"
        was_unanswered = not self._state["answered"]
        self._state.update(
            {
                "last_sent": now,
                "last_text": text.strip()[:200],
                "answered": False,
                "day": day,
                "day_count": (self._state["day_count"] if same_day else 0) + 1,
                # 只有"上一次也没回"才算追问；正常发一条时清零
                "followups": (self._state.get("followups", 0) + 1) if was_unanswered else 0,
            }
        )
        self._save()

    def mark_answered(self) -> None:
        """对方回话了：解除"不追第二句"。

        注意**不清冷却计时** —— 插件的原意是"回了话就不算打扰"，
        但两次主动之间还是该有个最小间隔，否则他回一句、聊两句、冷场半小时，
        她又能来找他，一天下来还是会很黏。
        """
        if not self._state["answered"]:
            self._state["answered"] = True
            self._save()
            log.info("对方回话了，可以再次主动（仍受冷却约束）")

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "last_sent": self._state["last_sent"],
            "last_text": self._state["last_text"],
            "answered": self._state["answered"],
            "today": self._state["day_count"] if self._state["day"] == self._today(time.time()) else 0,
            "daily_cap": self.daily_cap,
        }
