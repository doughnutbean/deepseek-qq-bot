"""B 站直播开播/下播记录：她陪看过的每一场直播都留一条。

为什么单独记
------------
B 站公开接口只给「现在开没开」和「这一场几点开播」，没有历史；直播陪看本来
就在轮询房间状态，顺手把开播/下播的边界记下来，不额外发请求。

口径（很重要，别把「停止陪看」当成「主播下播」）：

- **一场直播** = 同一个房间 + 同一个开播时间（B 站 `live_start_time`；拿不到就
  用第一次看到在播的时间）；
- 看到 `live=False` → 记 `offline`，`ended_at` 是发现下播的时间（有轮询误差）；
- 主人说「不看了」但直播间还在播 → 记 `stopped`，表示**跟踪结束，未确认下播**；
- 换到别的房间时旧房间还开着 → 记 `switched`，同上；
- 重新看回同一个开播时间 → 复用原来那条记录，不会把一场拆成两场；
- 重启后第一次拿到房间信息时，如果还在播就接着用旧记录；如果已经下播就补上
  `ended_at`。

数据落在 `data/live_history.db`（SQLite），重启不丢；查询只读本地库，0 token。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 「今天 / 昨天 / 前天」和「最近记录」是两套查询：前者看某一天，后者列最近几场。
_DAY_WORDS = {
    "今天": 0,
    "今日": 0,
    "今晚": 0,
    "昨天": 1,
    "昨日": 1,
    "昨晚": 1,
    "前天": 2,
}
_RECENT_WORDS = {"最近", "记录", "历史", "最近记录", "最近开播", "全部"}
_FULL_DATE_RE = re.compile(
    r"^(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$"
)
_SHORT_DATE_RE = re.compile(r"^(\d{1,2})[-/.月](\d{1,2})日?$")
# 一场直播的「同一个开播时间」容差：B 站接口偶尔会差几秒。
_SAME_START_TOLERANCE = 300.0
# 开播时间不能太离谱：未来 5 分钟以后、或 30 天以前，都当接口没给。
_MAX_START_AGE = 30 * 86400.0
_MAX_START_FUTURE = 300.0


def parse_day(text: Any, now: float | None = None) -> str:
    """把「今天 / 昨天 / 前天 / 11-08 / 2026-11-08」转成 YYYY-MM-DD。

    认不出返回空串。只用于本地查询，不联网。
    """
    raw = re.sub(r"\s+", "", str(text or ""))
    if not raw:
        return ""
    current = datetime.fromtimestamp(float(now or time.time()))
    today = current.date()
    if raw in _DAY_WORDS:
        return (today - timedelta(days=_DAY_WORDS[raw])).isoformat()
    match = _FULL_DATE_RE.fullmatch(raw)
    if match:
        year, month, day = (int(item) for item in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""
    match = _SHORT_DATE_RE.fullmatch(raw)
    if match:
        month, day = (int(item) for item in match.groups())
        year = today.year
        try:
            parsed = date(year, month, day)
        except ValueError:
            return ""
        # 「12-31」在 1 月问，多半是去年的记录，不是明年。
        if parsed > today + timedelta(days=1):
            try:
                parsed = date(year - 1, month, day)
            except ValueError:
                return ""
        return parsed.isoformat()
    return ""


def is_recent_day(text: Any) -> bool:
    """这句是不是要「最近的记录列表」而不是某一天。"""
    return str(text or "").strip() in _RECENT_WORDS


def day_bounds(day: str) -> tuple[float, float]:
    """某一天在本机时区的 [00:00, 次日 00:00) 时间戳。"""
    try:
        start_dt = datetime.strptime(str(day or ""), "%Y-%m-%d")
    except ValueError:
        now = datetime.now()
        start_dt = datetime(now.year, now.month, now.day)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.timestamp(), end_dt.timestamp()


def day_label(day: str, now: float | None = None) -> str:
    """「今天 / 昨天 / 前天 / 11-08」。"""
    try:
        parsed = date.fromisoformat(str(day or ""))
    except ValueError:
        return str(day or "")
    today = datetime.fromtimestamp(float(now or time.time())).date()
    delta = (today - parsed).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta == 2:
        return "前天"
    return parsed.strftime("%m-%d")


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(float(ts or 0)))


def date_clock(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))


def duration_text(seconds: float) -> str:
    """人话时长：不到 1 分钟 / X 分钟 / X 小时 Y 分 / X 天 Y 小时。"""
    total = max(0, int(float(seconds or 0)))
    if total < 60:
        return "不到 1 分钟"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {rest} 分" if rest else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


class LiveHistory:
    """一个 SQLite 文件，记每个房间的每一场直播。"""

    def __init__(
        self,
        path: Path | str,
        *,
        enabled: bool = True,
        keep_days: int = 180,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.keep_days = max(30, int(keep_days or 180))
        self._db: sqlite3.Connection | None = None
        self._last_prune = 0.0

    # ---------- 连接 / 建表 ----------

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), timeout=5.0)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS live_session (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    anchor TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    start_source TEXT NOT NULL DEFAULT 'observed',
                    last_seen_at REAL NOT NULL,
                    ended_at REAL,
                    end_reason TEXT NOT NULL DEFAULT '',
                    peak_online INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_live_session_room_start
                    ON live_session(room_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_live_session_open
                    ON live_session(room_id, ended_at);
                """
            )
        return self._db

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    # ---------- 写入 ----------

    def open(
        self,
        room_id: str,
        started_at: float = 0.0,
        *,
        source: str = "observed",
        title: str = "",
        anchor: str = "",
        online: int = 0,
        now: float | None = None,
    ) -> int:
        """记一场直播开始；已经有开着的同一场就更新，返回记录 id。"""
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return 0
        now = float(now or time.time())
        started = float(started_at or 0)
        if not _plausible_start(started, now):
            started, source = now, "observed"
        if source not in {"api", "observed"}:
            source = "observed"

        existing = self.open_session(room)
        if existing is not None:
            if (
                source == "api"
                and existing.get("start_source") != "api"
                and started < float(existing.get("started_at") or 0) - 60
            ):
                # 先看到「在播」、后拿到真实开播时间：把开始时间改准，不拆场。
                self._set_start(int(existing["id"]), started, "api")
            self.touch(room, now=now, title=title, anchor=anchor, online=online)
            return int(existing["id"])

        if source == "api":
            same = self._find_same_start(room, started)
            if same is not None:
                # 「不看了」之后又看回同一场：复用原记录，不记成两场。
                with self._conn():
                    self._conn().execute(
                        """
                        UPDATE live_session SET
                            ended_at = NULL,
                            end_reason = '',
                            last_seen_at = MAX(last_seen_at, ?),
                            title = CASE WHEN ? != '' THEN ? ELSE title END,
                            anchor = CASE WHEN ? != '' THEN ? ELSE anchor END,
                            peak_online = MAX(peak_online, ?)
                        WHERE id = ?
                        """,
                        (now, title, title, anchor, anchor, int(online or 0), int(same["id"])),
                    )
                return int(same["id"])

        db = self._conn()
        with db:
            cur = db.execute(
                """
                INSERT INTO live_session
                    (room_id, anchor, title, started_at, start_source,
                     last_seen_at, peak_online)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (room, anchor, title, started, source, now, int(online or 0)),
            )
        self._maybe_prune(now)
        return int(cur.lastrowid or 0)

    def touch(
        self,
        room_id: str,
        *,
        now: float | None = None,
        title: str = "",
        anchor: str = "",
        online: int = 0,
    ) -> None:
        """更新开着的这场：最后看到在播的时间、标题、人气峰值。"""
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return
        now = float(now or time.time())
        try:
            with self._conn():
                self._conn().execute(
                    """
                    UPDATE live_session SET
                        last_seen_at = MAX(last_seen_at, ?),
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        anchor = CASE WHEN ? != '' THEN ? ELSE anchor END,
                        peak_online = MAX(peak_online, ?)
                    WHERE room_id = ? AND ended_at IS NULL
                    """,
                    (now, title, title, anchor, anchor, int(online or 0), room),
                )
        except sqlite3.Error:
            log.warning("直播开播记录更新时间失败", exc_info=True)

    def close_session(
        self,
        room_id: str,
        ended_at: float | None = None,
        reason: str = "offline",
    ) -> bool:
        """结束开着的这场；返回是否真的关掉了一条。"""
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return False
        end = float(ended_at or time.time())
        try:
            with self._conn():
                cur = self._conn().execute(
                    """
                    UPDATE live_session SET
                        ended_at = MAX(started_at, ?),
                        end_reason = ?,
                        last_seen_at = MAX(last_seen_at, MAX(started_at, ?))
                    WHERE room_id = ? AND ended_at IS NULL
                    """,
                    (end, str(reason or ""), end, room),
                )
        except sqlite3.Error:
            log.warning("直播下播记录写入失败", exc_info=True)
            return False
        return bool(cur.rowcount)

    def observe_live(
        self,
        room_id: str,
        *,
        started_at: float = 0.0,
        title: str = "",
        anchor: str = "",
        online: int = 0,
        now: float | None = None,
    ) -> int:
        """看到「在播」时调用：开新场 / 续写同一场 / 识别出换了一场。"""
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return 0
        now = float(now or time.time())
        api_start = float(started_at or 0)
        if not _plausible_start(api_start, now):
            api_start = 0.0

        current = self.open_session(room)
        if current is not None:
            current_start = float(current.get("started_at") or 0)
            if api_start and current.get("start_source") != "api" and api_start < current_start - 60:
                # 先按「第一次看到」开的，B 站后来给了真实开播时间：改准，还是同一场。
                self._set_start(int(current["id"]), api_start, "api")
            elif api_start and abs(api_start - current_start) > _SAME_START_TOLERANCE:
                # 同一个房间、开播时间变了：这是新的一场，上一场在最后看到时结束。
                last_seen = float(current.get("last_seen_at") or now)
                self.close_session(room, ended_at=min(now, last_seen), reason="offline")
                return self.open(
                    room,
                    api_start,
                    source="api",
                    title=title,
                    anchor=anchor,
                    online=online,
                    now=now,
                )
            self.touch(room, now=now, title=title, anchor=anchor, online=online)
            return int(current["id"])

        return self.open(
            room,
            api_start,
            source="api" if api_start else "observed",
            title=title,
            anchor=anchor,
            online=online,
            now=now,
        )

    def observe_offline(self, room_id: str, *, now: float | None = None) -> bool:
        """看到「下播」时调用：把开着的这场收尾。"""
        if self.open_session(room_id) is None:
            return False
        return self.close_session(room_id, ended_at=now, reason="offline")

    # ---------- 查询 ----------

    def open_session(self, room_id: str) -> dict[str, Any] | None:
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return None
        try:
            row = self._conn().execute(
                "SELECT * FROM live_session "
                "WHERE room_id = ? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (room,),
            ).fetchone()
        except sqlite3.Error:
            log.warning("直播开播记录读取失败", exc_info=True)
            return None
        return dict(row) if row else None

    def last_session(self, room_id: str) -> dict[str, Any] | None:
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return None
        try:
            row = self._conn().execute(
                "SELECT * FROM live_session WHERE room_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (room,),
            ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row else None

    def last_session_before(self, room_id: str, before: float) -> dict[str, Any] | None:
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return None
        try:
            row = self._conn().execute(
                "SELECT * FROM live_session WHERE room_id = ? AND started_at < ? "
                "ORDER BY started_at DESC LIMIT 1",
                (room, float(before)),
            ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row else None

    def recent(self, room_id: str, limit: int = 8) -> list[dict[str, Any]]:
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return []
        try:
            rows = self._conn().execute(
                "SELECT * FROM live_session WHERE room_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (room, max(1, min(50, int(limit or 8)))),
            ).fetchall()
        except sqlite3.Error:
            log.warning("直播开播记录读取失败", exc_info=True)
            return []
        return [dict(row) for row in rows]

    def latest_room(self) -> str:
        """最近有记录的直播间；停了直播也能查历史。"""
        if not self.enabled:
            return ""
        try:
            row = self._conn().execute(
                "SELECT room_id, MAX(last_seen_at) AS last FROM live_session "
                "GROUP BY room_id ORDER BY last DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row["room_id"]) if row else ""

    def has_record(self, room_id: str) -> bool:
        room = str(room_id or "").strip()
        if not self.enabled or not room:
            return False
        try:
            row = self._conn().execute(
                "SELECT 1 FROM live_session WHERE room_id = ? LIMIT 1",
                (room,),
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def day_stats(
        self,
        room_id: str,
        day: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """某一天的统计：场次、当天累计时长、第一场开始、当前是否还在播。"""
        room = str(room_id or "").strip()
        start, end = day_bounds(day)
        now = float(now or time.time())
        empty = {
            "room_id": room,
            "day": str(day or ""),
            "start": start,
            "end": end,
            "sessions": [],
            "count": 0,
            "total": 0.0,
            "first_start": 0.0,
            "last_end": 0.0,
            "open": False,
            "title": "",
            "anchor": "",
        }
        if not self.enabled or not room:
            return empty
        try:
            rows = self._conn().execute(
                "SELECT * FROM live_session WHERE room_id = ? "
                "AND started_at < ? AND (ended_at IS NULL OR ended_at >= ?) "
                "ORDER BY started_at",
                (room, end, start),
            ).fetchall()
        except sqlite3.Error:
            log.warning("直播开播记录统计失败", exc_info=True)
            return empty
        sessions: list[dict[str, Any]] = []
        total = 0.0
        for row in rows:
            raw_end = row["ended_at"]
            seg_start = max(float(row["started_at"]), start)
            seg_end = min(float(raw_end) if raw_end is not None else now, end)
            if seg_end < seg_start:
                continue
            duration = seg_end - seg_start
            total += duration
            sessions.append(
                {
                    **dict(row),
                    "seg_start": seg_start,
                    "seg_end": seg_end,
                    "duration": duration,
                    "open": raw_end is None,
                }
            )
        if not sessions:
            return empty
        latest = sessions[-1]
        return {
            **empty,
            "sessions": sessions,
            "count": len(sessions),
            "total": total,
            "first_start": min(float(item["started_at"]) for item in sessions),
            "last_end": max(float(item["seg_end"]) for item in sessions),
            "open": any(item["open"] for item in sessions),
            "title": str(latest.get("title") or ""),
            "anchor": str(latest.get("anchor") or ""),
        }

    # ---------- 给回执 / 提示词 ----------

    def day_report(self, room_id: str, day: str, *, now: float | None = None) -> str:
        """「今天开播了吗 / 今天播了多久」的回执。"""
        now = float(now or time.time())
        stats = self.day_stats(room_id, day, now=now)
        label = day_label(day, now)
        title = f"《{stats['title']}》" if stats.get("title") else ""
        head = f"📺 直播间 {stats['room_id'] or room_id}{title}"
        if stats["count"] == 0:
            last = self.last_session_before(room_id, stats["start"])
            if last is not None:
                ended = last.get("ended_at")
                if ended is None:
                    ended = float(last.get("last_seen_at") or now)
                spent = max(0.0, float(ended) - float(last["started_at"]))
                return (
                    f"{head}：{label}（{str(day)[5:]}）没有开播记录。"
                    f"上次开播：{date_clock(float(last['started_at']))}–"
                    f"{clock(float(ended))}，播了 {duration_text(spent)}。"
                )
            return f"{head}：{label}没有开播记录（开始陪看之后才会记）。"

        if stats["open"]:
            current = next(item for item in reversed(stats["sessions"]) if item["open"])
            if float(current["started_at"]) < stats["start"]:
                lines = [
                    f"{head}：{label}（{str(day)[5:]}）现在还在播"
                    f"（{date_clock(float(current['started_at']))} 开播），"
                    f"当天累计 {duration_text(stats['total'])}。"
                ]
            else:
                lines = [
                    f"{head}：{label}（{str(day)[5:]}）现在正在播，"
                    f"{clock(float(current['started_at']))} 开播，"
                    f"当天累计 {duration_text(stats['total'])}。"
                ]
        else:
            lines = [
                f"{head}：{label}（{str(day)[5:]}）播了 {stats['count']} 场，"
                f"当天累计 {duration_text(stats['total'])}。"
            ]

        for item in stats["sessions"][:6]:
            lines.append("  " + self._range_text(item, stats["start"]))
        if stats["count"] > 6:
            lines.append(f"  ……还有 {stats['count'] - 6} 场")
        return "\n".join(lines)

    def recent_report(self, room_id: str, *, limit: int = 8, now: float | None = None) -> str:
        """「开播记录 / 最近开播」的回执。"""
        now = float(now or time.time())
        rows = self.recent(room_id, limit=limit)
        room = str(room_id or "").strip()
        if not rows:
            return f"📺 直播间 {room} 还没有开播记录（开始陪看之后才会记）。"
        lines = [f"📺 直播间 {room} 最近开播记录："]
        for row in rows:
            started = float(row["started_at"])
            ended = row["ended_at"]
            left = date_clock(started)
            start_day = time.strftime("%Y-%m-%d", time.localtime(started))
            if ended is None:
                right = "现在"
                note = "，还在播"
                spent = max(0.0, now - started)
            else:
                end_day = time.strftime("%Y-%m-%d", time.localtime(float(ended)))
                right = clock(float(ended)) if end_day == start_day else date_clock(float(ended))
                note = ""
                if str(row.get("end_reason") or "") in {"stopped", "switched"}:
                    note = "，跟踪结束时还在播"
                spent = max(0.0, float(ended) - started)
            lines.append(f"  {left}–{right}（{duration_text(spent)}{note}）")
        return "\n".join(lines)

    def day_line(self, room_id: str, *, now: float | None = None) -> str:
        """状态回执/提示词里的一行「今天」。没有记录返回空串。"""
        now = float(now or time.time())
        today = datetime.fromtimestamp(now).date().isoformat()
        stats = self.day_stats(room_id, today, now=now)
        if stats["count"] == 0:
            return ""
        if stats["open"]:
            current = next(item for item in reversed(stats["sessions"]) if item["open"])
            started = float(current["started_at"])
            if started < stats["start"]:
                return (
                    f"今天：还在播（{date_clock(started)} 开播），"
                    f"今天累计 {duration_text(stats['total'])}。"
                )
            return (
                f"今天：正在播（{clock(started)} 开播，"
                f"已播 {duration_text(now - started)}）。"
            )
        if stats["count"] == 1:
            item = stats["sessions"][0]
            return (
                f"今天：播了 {duration_text(item['duration'])}"
                f"（{clock(float(item['started_at']))}–"
                f"{clock(float(item['ended_at']))}）。"
            )
        return f"今天：播了 {stats['count']} 场，共 {duration_text(stats['total'])}。"

    # ---------- 内部 ----------

    def _find_same_start(self, room: str, started: float) -> dict[str, Any] | None:
        try:
            row = self._conn().execute(
                "SELECT * FROM live_session WHERE room_id = ? "
                "AND started_at BETWEEN ? AND ? "
                "ORDER BY started_at DESC LIMIT 1",
                (
                    room,
                    started - _SAME_START_TOLERANCE,
                    started + _SAME_START_TOLERANCE,
                ),
            ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row else None

    def _set_start(self, session_id: int, started: float, source: str) -> None:
        try:
            with self._conn():
                self._conn().execute(
                    "UPDATE live_session SET started_at = ?, start_source = ? WHERE id = ?",
                    (float(started), str(source), int(session_id)),
                )
        except sqlite3.Error:
            log.warning("直播开播时间修正失败", exc_info=True)

    def _range_text(self, item: dict[str, Any], day_start: float) -> str:
        started = float(item["started_at"])
        left = clock(started) if started >= day_start else date_clock(started)
        if item["open"]:
            right = "现在"
            spent = float(item["duration"])
            note = "，还在播"
        else:
            ended = float(item["ended_at"])
            start_day = time.strftime("%Y-%m-%d", time.localtime(started))
            end_day = time.strftime("%Y-%m-%d", time.localtime(ended))
            right = clock(ended) if start_day == end_day else date_clock(ended)
            spent = float(item["duration"])
            note = ""
            if str(item.get("end_reason") or "") in {"stopped", "switched"}:
                note = "，跟踪结束时还在播"
        return f"{left}–{right}（{duration_text(spent)}{note}）"

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = now - self.keep_days * 86400
        try:
            with self._conn():
                self._conn().execute(
                    "DELETE FROM live_session WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
        except sqlite3.Error:
            log.warning("直播开播记录清理失败（下次再试）", exc_info=True)


def _plausible_start(started: float, now: float) -> bool:
    return bool(
        started
        and started <= now + _MAX_START_FUTURE
        and started >= now - _MAX_START_AGE
    )
