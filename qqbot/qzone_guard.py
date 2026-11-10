"""发说说前的频率守卫。

借鉴 qzone-sweeper 的限流思路 —— 刷别人空间会被人看见，自己发说说也一样：
一天连发五条、半夜三点发一条，都很显眼。

三道闸：

1. **免打扰时段**（默认 00:00-08:00，支持跨零点）—— 这段时间不发
2. **每天上限**（默认 2 条）
3. **两条之间的最小间隔**（默认 60 分钟）

状态落在 `qzone_limits.json` 里，重启不会把计数清零。

    guard = QzoneGuard(Path("data/qzone_limits.json"), daily_limit=2, cooldown_minutes=60)
    ok, why = guard.check()
    if ok: guard.note()
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 7


def parse_quiet(spec: str) -> list[tuple[int, int]]:
    """把 "00:00-08:00,13:00-14:00" 解析成 [(0, 480), (780, 840)]。空串 = 不限制。"""
    spans: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        try:
            start_s, end_s = part.split("-", 1)
            sh, sm = (int(x) for x in start_s.strip().split(":"))
            eh, em = (int(x) for x in end_s.strip().split(":"))
            spans.append((sh * 60 + sm, eh * 60 + em))
        except (ValueError, TypeError):
            log.warning("免打扰时段看不懂：%r", part)
    return spans


class QzoneGuard:
    def __init__(
        self,
        path: Path,
        daily_limit: int = 2,
        cooldown_minutes: int = 60,
        quiet_hours: str = "",
    ) -> None:
        self.path = path
        self.daily_limit = max(0, daily_limit)
        self.cooldown = max(0, cooldown_minutes) * 60
        self.quiet = parse_quiet(quiet_hours)
        self._times: list[float] = self._load()

    # ---------- 存档 ----------

    def _load(self) -> list[float]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        cutoff = time.time() - KEEP_DAYS * 86400
        return [float(t) for t in raw if isinstance(t, (int, float)) and t >= cutoff]

    def _save(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        self._times = [t for t in self._times if t >= cutoff]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._times), encoding="utf-8")
        except OSError as exc:
            log.warning("发说说记录写不进去：%s", exc)

    # ---------- 判断 ----------

    def in_quiet(self, now: float | None = None) -> bool:
        if not self.quiet:
            return False
        moment = datetime.fromtimestamp(now if now is not None else time.time())
        minute = moment.hour * 60 + moment.minute
        for start, end in self.quiet:
            if start <= end:
                if start <= minute < end:
                    return True
            elif minute >= start or minute < end:  # 跨零点
                return True
        return False

    def count_today(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        today = datetime.fromtimestamp(now).date()
        return sum(1 for t in self._times if datetime.fromtimestamp(t).date() == today)

    def check(self, now: float | None = None, *, manual: bool = False) -> tuple[bool, str]:
        """能不能发？不能就返回原因。

        `manual=True` 是主人自己点了"发"：免打扰时段和发帖间隔都放行 —— 那是给
        自动说说定的规矩，主人半夜想发一条不该被自己的规则拦住。
        每天条数上限仍然生效（那是给账号的硬保险）。
        """
        now = now if now is not None else time.time()

        if not manual and self.in_quiet(now):
            spans = "、".join(
                f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in self.quiet
            )
            return False, f"现在是免打扰时段（{spans}），这时间段不发"

        used = self.count_today(now)
        if self.daily_limit and used >= self.daily_limit:
            return False, f"今天已经发了 {used} 条了（上限 {self.daily_limit} 条）"

        if not manual and self.cooldown and self._times:
            last = max(self._times)
            remain = self.cooldown - (now - last)
            if remain > 0:
                return False, f"离上一条说说还不到 {self.cooldown // 60} 分钟（还要等 {int(remain // 60) + 1} 分钟）"

        return True, ""

    def note(self, now: float | None = None) -> None:
        self._times.append(now if now is not None else time.time())
        self._save()

    def stats(self, now: float | None = None) -> dict:
        return {
            "today": self.count_today(now),
            "daily_limit": self.daily_limit,
            "cooldown_minutes": self.cooldown // 60,
            "quiet": self.quiet,
            "total_kept": len(self._times),
        }
