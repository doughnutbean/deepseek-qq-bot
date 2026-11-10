"""跟着群友复读：同一个短串被多人短时间复读时，她也接一次。

只做**一次**，不是每句都跟：

- 同一群、`window_seconds` 内；
- 至少 `min_people` 个不同的人、`min_repeats` 次；
- 同一个串 `cooldown_seconds` 内只跟一次；
- 每群每小时最多 `per_hour` 次；
- 只认短文字：命令、占位、链接、@、超长、脏话/广告都不跟。

纯逻辑，不碰 OneBot；发不发由 app 决定。
"""

from __future__ import annotations

import re
import time
from collections import deque

from . import moderation
from .memes import is_command

_PLACEHOLDERS = (
    "[图片]", "[表情]", "[动画表情]", "[语音]", "[视频]", "[文件]",
    "[卡片消息]", "[合并转发]", "[CQ:",
)
_NORM_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def _key(text: str) -> str:
    """复读判定用的归一化：去掉标点/空格/大小写，保留汉字和字母数字。"""
    return _NORM_RE.sub("", text or "").lower()


class RepeatAlong:
    def __init__(
        self,
        *,
        enabled: bool = True,
        min_people: int = 3,
        min_repeats: int = 3,
        window_seconds: float = 30.0,
        cooldown_seconds: float = 300.0,
        max_chars: int = 24,
        per_hour: int = 6,
    ) -> None:
        self.enabled = bool(enabled)
        self.min_people = max(2, int(min_people))
        self.min_repeats = max(2, int(min_repeats))
        self.window = max(1.0, float(window_seconds))
        self.cooldown = max(0.0, float(cooldown_seconds))
        self.max_chars = max(1, int(max_chars))
        self.per_hour = max(0, int(per_hour))
        # group -> deque[(uid, key, raw, ts)]
        self._recent: dict[str, deque[tuple[str, str, str, float]]] = {}
        # (group, key) -> 上次跟读时间
        self._last_join: dict[tuple[str, str], float] = {}
        # group -> deque[join_ts]
        self._joins: dict[str, deque[float]] = {}

    def usable(self, text: str) -> str:
        """能跟的原文；不能跟返回空串。"""
        raw = " ".join(str(text or "").split()).strip()
        if not raw or len(raw) > self.max_chars:
            return ""
        if raw[0] in "/#":
            return ""
        if any(mark in raw for mark in _PLACEHOLDERS):
            return ""
        if is_command(raw):
            return ""
        if "@" in raw or "http://" in raw or "https://" in raw or "www." in raw:
            return ""
        if "本鱼" in raw or "主人" in raw:
            return ""
        if not _key(raw):
            return ""
        try:
            if moderation.classify(raw):
                return ""
        except Exception:  # noqa: BLE001 - 过滤失败不能挡住复读
            pass
        return raw

    def observe(
        self,
        group_id: str,
        uid: object,
        text: str,
        *,
        now: float | None = None,
    ) -> bool:
        """记一条群消息；够格返回 True（由调用方发这一句）。"""
        if not self.enabled:
            return False
        raw = self.usable(text)
        if not raw:
            return False
        gid = str(group_id or "")
        if not gid:
            return False
        moment = time.time() if now is None else float(now)
        key = _key(raw)
        q = self._recent.setdefault(gid, deque())
        q.append((str(uid or ""), key, raw, moment))
        self._prune(q, moment)
        same = [(u, t) for u, k, _r, t in q if k == key]
        people = {u for u, _t in same}
        if len(same) < self.min_repeats or len(people) < self.min_people:
            return False

        pair = (gid, key)
        last = self._last_join.get(pair, 0.0)
        if self.cooldown and last and moment - last < self.cooldown:
            return False
        joins = self._joins.setdefault(gid, deque())
        while joins and moment - joins[0] > 3600:
            joins.popleft()
        if self.per_hour and len(joins) >= self.per_hour:
            return False

        self._last_join[pair] = moment
        joins.append(moment)
        # 只留最近用得上的，别让长期运行攒成一张大表
        if len(self._last_join) > 4000:
            cutoff = moment - max(self.cooldown, 3600) * 2
            self._last_join = {k: v for k, v in self._last_join.items() if v >= cutoff}
        return True

    def _prune(self, q: deque[tuple[str, str, str, float]], now: float) -> None:
        while q and (now - q[0][3] > self.window or len(q) > 50):
            q.popleft()

    def reset(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._recent.clear()
            self._last_join.clear()
            self._joins.clear()
            return
        gid = str(group_id)
        self._recent.pop(gid, None)
        self._joins.pop(gid, None)
        for key in [k for k in self._last_join if k[0] == gid]:
            self._last_join.pop(key, None)

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "min_people": self.min_people,
            "min_repeats": self.min_repeats,
            "window_seconds": self.window,
            "cooldown_seconds": self.cooldown,
            "max_chars": self.max_chars,
            "per_hour": self.per_hour,
            "groups": len(self._recent),
        }
