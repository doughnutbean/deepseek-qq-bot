"""峰谷时段：跟随 DeepSeek 现行的高峰/空闲定价。

官方规则（2026，以文档为准）：

> 北京时间**周一至周五**（不含中国法定节假日）**9:00-12:00、14:00-18:00** 为高峰时段；
> 其余时段，包括周末及中国法定节假日全天，均为空闲时段。

注意这跟 2025 年那套「00:30-08:30 夜间五折」**不是一回事**，旧资料别再用。

这里只决定一件事：某时刻该给她什么档位。
- **高峰**（贵）：档位 0，只回 @ 和触发词
- **空闲**（便宜）：档位 1，可以关键词触发，但不随机插话

内置 2026 年国务院放假安排（只列工作日放假日），所以默认就能正确避开国庆/中秋；
2027 年及以后用 `holidays` 追加（"2027-10-01" 或 "10-01"），或更新默认表。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# 官方规则写的是北京时间。这里显式钉死 UTC+8，不依赖服务器 TZ；
# 否则部署到 UTC 的 VPS 上，峰谷会整体错 8 小时。
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

# 2026 年国务院办公厅放假安排（2025-11-04 发布）。
# 只列落在工作日的放假日；周末本来就算空闲，调休上班的周末也仍按周末空闲。
# 2027 年及以后请更新，或在 PRICETIME_HOLIDAYS 里追加。
DEFAULT_HOLIDAYS_2026 = (
    "2026-01-01,2026-01-02,"
    "2026-02-16,2026-02-17,2026-02-18,2026-02-19,2026-02-20,2026-02-23,"
    "2026-04-06,"
    "2026-05-01,2026-05-04,2026-05-05,"
    "2026-06-19,"
    "2026-09-25,"
    "2026-10-01,2026-10-02,2026-10-05,2026-10-06,2026-10-07"
)


def _parse_hm(text: str, fallback: int) -> int:
    try:
        hh, mm = (int(x) for x in str(text).strip().split(":"))
        return max(0, min(24 * 60, hh * 60 + mm))
    except (ValueError, TypeError):
        return fallback


def parse_windows(spec: str) -> list[tuple[int, int]]:
    """把 "09:00-12:00,14:00-18:00" 解析成分钟区间。"""
    spans: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if "-" not in part:
            continue
        left, _, right = part.partition("-")
        start = _parse_hm(left, -1)
        end = _parse_hm(right, -1)
        if start < 0 or end < 0 or start == end:
            continue
        spans.append((start, end))
    return spans


class PriceTime:
    def __init__(
        self,
        enabled: bool = True,
        peak_tier: int = 0,
        offpeak_tier: int = 1,
        peak_windows: str = "09:00-12:00,14:00-18:00",
        weekday_only: bool = True,
        holidays: str = "",
    ) -> None:
        self.enabled = enabled
        self.peak_tier = max(0, min(3, peak_tier))
        self.offpeak_tier = max(0, min(3, offpeak_tier))
        self.windows = parse_windows(peak_windows) or [(9 * 60, 12 * 60), (14 * 60, 18 * 60)]
        self.weekday_only = weekday_only
        self.holidays = self._parse_holidays(DEFAULT_HOLIDAYS_2026) | self._parse_holidays(
            holidays
        )

    @staticmethod
    def _parse_holidays(spec: str) -> set[str]:
        """接受 "2026-10-01,2026-10-02" 或 "10-01,10-02"。"""
        out: set[str] = set()
        for part in (spec or "").split(","):
            part = part.strip()
            if part:
                out.add(part)
        return out

    # ---------- 判断 ----------

    @staticmethod
    def _moment(now: float | None = None) -> datetime:
        """把 epoch 转成北京时间；传 None 就取当前时刻。"""
        return datetime.fromtimestamp(
            now if now is not None else time.time(), BEIJING
        )

    def is_holiday(self, now: float | None = None) -> bool:
        moment = self._moment(now)
        return moment.strftime("%Y-%m-%d") in self.holidays or moment.strftime("%m-%d") in self.holidays

    def is_weekend(self, now: float | None = None) -> bool:
        return self._moment(now).weekday() >= 5

    def is_peak(self, now: float | None = None) -> bool:
        """现在是不是高峰时段（贵的时候），按北京时间判断。"""
        # 节假日：官方写明全天按空闲算
        if self.is_holiday(now):
            return False
        # 周末：默认全天空闲；weekday_only=False 时周末也按窗口判
        if self.weekday_only and self.is_weekend(now):
            return False
        moment = self._moment(now)
        minute = moment.hour * 60 + moment.minute
        return any(start <= minute < end for start, end in self.windows)

    def is_offpeak(self, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        return not self.is_peak(now)

    def tier_for(self, now: float | None = None) -> int:
        if not self.enabled:
            return -1
        return self.offpeak_tier if self.is_offpeak(now) else self.peak_tier

    def label(self, now: float | None = None) -> str:
        return "空闲时段" if self.is_offpeak(now) else "高峰时段"

    def describe(self) -> str:
        if not self.enabled:
            return "峰谷档位：关"
        spans = "、".join(
            f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in self.windows
        )
        return (
            f"峰谷档位：高峰（工作日 {spans}）→ 档位 {self.peak_tier}；"
            f"其余空闲 → 档位 {self.offpeak_tier}"
            + ("（周末/节假日全天空闲）" if self.holidays or self.weekday_only else "")
        )
