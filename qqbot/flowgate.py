"""群聊发言频率门：慢回、限速、防刷屏。

只在「风控规避模式」开着时生效（`ENABLE_RISK_MODE` / 聊天里「打开风控模式」）。
关掉后完全回到原来的回复路径，不改任何别的开关；所有判断都在本地，0 token。

四层门：

1. **慢回**：一条群消息进来后，至少隔 `reply_delay` 秒才发第一条回复。
   这一秒顺便就是合批窗：同一个人接着发的下一句能并进同一轮。
2. **节奏**：同一个群两条消息之间至少隔 `min_gap` 秒，拆条也不会连着机关枪。
3. **配额**：60 秒最多 `per_minute` 条；10 秒最多 `burst` 条；1 小时最多
   `per_hour` 条。超了就这一轮不发，不排队等一分钟。
4. **防重复 / 单人刷屏**：同一个群 `duplicate_seconds` 内同样的正文不重发；
   同一个人 60 秒最多 `per_user_minute` 条（主人由调用方传 `uid=None` 豁免）。

时间一律用调用方传进来的 `now`（默认 `time.monotonic`），测试不用真睡。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class FlowStats:
    """一眼看这个群现在用掉多少额度。"""

    minute: int
    burst: int
    hour: int
    gap: float
    reply_wait: float

    def summary(self) -> str:
        return (
            f"一分钟 {self.minute} 条、10 秒 {self.burst} 条；"
            f"两条之间还要等 {self.gap:.1f} 秒"
        )


class GroupFlowGate:
    """一个群一套滑动窗口；判断和记账都只在这个对象里，不碰网络。"""

    def __init__(
        self,
        *,
        per_minute: int = 10,
        burst: int = 4,
        per_hour: int = 120,
        min_gap: float = 1.2,
        reply_delay: float = 1.0,
        per_user_minute: int = 3,
        duplicate_seconds: float = 90.0,
        minute_window: float = 60.0,
        burst_window: float = 10.0,
        hour_window: float = 3600.0,
    ) -> None:
        self.per_minute = max(0, int(per_minute))
        self.burst = max(0, int(burst))
        self.per_hour = max(0, int(per_hour))
        self.min_gap = max(0.0, float(min_gap))
        self.reply_delay_seconds = max(0.0, float(reply_delay))
        self.per_user_minute = max(0, int(per_user_minute))
        self.duplicate_seconds = max(0.0, float(duplicate_seconds))
        self.minute_window = max(1.0, float(minute_window))
        self.burst_window = max(1.0, float(burst_window))
        self.hour_window = max(1.0, float(hour_window))

        self._minute: dict[str, deque[float]] = {}
        self._burst: dict[str, deque[float]] = {}
        self._hour: dict[str, deque[float]] = {}
        self._user: dict[tuple[str, str], deque[float]] = {}
        self._last_send: dict[str, float] = {}
        self._last_inbound: dict[str, float] = {}
        self._texts: dict[str, deque[tuple[float, str]]] = {}

    # ---------- 入站 / 出站时间 ----------

    def note_inbound(self, group_id: str, now: float | None = None) -> None:
        """记下这个群最近一条消息什么时候到的（慢回门用）。"""
        gid = str(group_id or "")
        if gid:
            self._last_inbound[gid] = self._now(now)

    def reply_delay(self, group_id: str, now: float | None = None) -> float:
        """离「收到消息后满 reply_delay 秒」还差多久。"""
        gid = str(group_id or "")
        last = self._last_inbound.get(gid)
        if not last or not self.reply_delay_seconds:
            return 0.0
        wait = self.reply_delay_seconds - (self._now(now) - last)
        return wait if wait > 1e-6 else 0.0

    def gap_delay(self, group_id: str, now: float | None = None) -> float:
        """离「上一条发出后满 min_gap 秒」还差多久。"""
        gid = str(group_id or "")
        last = self._last_send.get(gid)
        if not last or not self.min_gap:
            return 0.0
        wait = self.min_gap - (self._now(now) - last)
        return wait if wait > 1e-6 else 0.0

    # ---------- 配额 ----------

    def room(
        self,
        group_id: str,
        units: int = 1,
        uid: object = None,
        now: float | None = None,
    ) -> tuple[int, str]:
        """还能发几条。返回 (最多能发几条, 被挡的原因)。

        只读，不记账；真正发之前用 `reserve()` 一次性把这几条记进去。
        """
        gid = str(group_id or "")
        now = self._now(now)
        units = max(0, int(units))
        if not gid or units <= 0:
            return 0, "没有群号 / 不存在要发的消息"

        minute = self._window(self._minute, gid, now, self.minute_window)
        burst = self._window(self._burst, gid, now, self.burst_window)
        hour = self._window(self._hour, gid, now, self.hour_window)

        room = units
        reason = ""
        if self.per_minute and len(minute) >= self.per_minute:
            room, reason = 0, f"一分钟 {self.per_minute} 条已经用完"
        elif self.burst and len(burst) >= self.burst:
            room, reason = 0, f"{self.burst_window:g} 秒内 {self.burst} 条已经用完"
        elif self.per_hour and len(hour) >= self.per_hour:
            room, reason = 0, f"一小时 {self.per_hour} 条已经用完"
        else:
            if self.per_minute:
                room = min(room, self.per_minute - len(minute))
            if self.burst:
                room = min(room, self.burst - len(burst))
            if self.per_hour:
                room = min(room, self.per_hour - len(hour))
            if uid is not None and self.per_user_minute:
                user = self._window(
                    self._user, (gid, str(uid)), now, self.minute_window
                )
                room = min(room, self.per_user_minute - len(user))
                if room <= 0:
                    reason = f"同一人一分钟 {self.per_user_minute} 条已经用完"
        return max(0, room), reason

    def reserve(
        self,
        group_id: str,
        units: int = 1,
        uid: object = None,
        now: float | None = None,
    ) -> tuple[int, str]:
        """占住 `units` 条的额度；返回实际占到了几条。"""
        gid = str(group_id or "")
        now = self._now(now)
        allowed, reason = self.room(gid, units, uid=uid, now=now)
        if allowed <= 0:
            return 0, reason
        minute = self._minute.setdefault(gid, deque())
        burst = self._burst.setdefault(gid, deque())
        hour = self._hour.setdefault(gid, deque())
        for _ in range(allowed):
            minute.append(now)
            burst.append(now)
            hour.append(now)
        if uid is not None and self.per_user_minute:
            user = self._user.setdefault((gid, str(uid)), deque())
            for _ in range(allowed):
                user.append(now)
        return allowed, ""

    def note_sent(
        self, group_id: str, text: str = "", now: float | None = None
    ) -> None:
        """一条真的发出去了：记下间隔和正文（正文用来防重复）。"""
        gid = str(group_id or "")
        if not gid:
            return
        now = self._now(now)
        self._last_send[gid] = now
        body = _normalize(text)
        if body and self.duplicate_seconds:
            recent = self._texts.setdefault(gid, deque())
            recent.append((now, body))
            cutoff = now - self.duplicate_seconds
            while recent and recent[0][0] < cutoff:
                recent.popleft()

    def duplicate(self, group_id: str, text: str, now: float | None = None) -> bool:
        """`duplicate_seconds` 内是不是刚说过一模一样的一句。"""
        gid = str(group_id or "")
        body = _normalize(text)
        if not gid or not body or not self.duplicate_seconds:
            return False
        now = self._now(now)
        recent = self._texts.get(gid)
        if not recent:
            return False
        cutoff = now - self.duplicate_seconds
        return any(ts >= cutoff and old == body for ts, old in recent)

    # ---------- 查询 / 重置 ----------

    def stats(self, group_id: str, now: float | None = None) -> FlowStats:
        gid = str(group_id or "")
        now = self._now(now)
        return FlowStats(
            minute=len(self._window(self._minute, gid, now, self.minute_window)),
            burst=len(self._window(self._burst, gid, now, self.burst_window)),
            hour=len(self._window(self._hour, gid, now, self.hour_window)),
            gap=self.gap_delay(gid, now),
            reply_wait=self.reply_delay(gid, now),
        )

    def reset(self) -> None:
        """清空所有窗口（开/关风控模式时用，避免旧账压着新的一轮）。"""
        self._minute.clear()
        self._burst.clear()
        self._hour.clear()
        self._user.clear()
        self._last_send.clear()
        self._last_inbound.clear()
        self._texts.clear()

    # ---------- 内部 ----------

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else float(now)

    @staticmethod
    def _window(
        table: dict, key: object, now: float, seconds: float
    ) -> deque[float]:
        """取出这个 key 的窗口并顺手丢掉过期的。"""
        dq = table.get(key)
        if dq is None:
            dq = deque()
            table[key] = dq
        cutoff = now - seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return dq


_WS_RE = re.compile(r"\s+")
_PUNCT = "。！？!?~～…，,.；;、 "


def _normalize(text: str) -> str:
    """比较重复时不管空白和句尾标点：「好呀」和「好呀！」算同一句。"""
    body = (text or "").strip(_PUNCT)
    return _WS_RE.sub("", body)
