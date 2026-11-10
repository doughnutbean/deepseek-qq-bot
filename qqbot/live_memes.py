"""B 站直播弹幕库：原始弹幕采样 + 高频词聚合 + 主人审批入库。

为什么单独一个库，不直接写 `group_log.db` / `memes.json`：

- 弹幕是**几万个陌生人**在刷，不是「本群谁说了什么」；混进群日志会让
  群风格、语料召回、复读统计全部失真。
- 弹幕里广告/骂人/黄腔的密度远高于群聊；高风险词只统计、不展示、不入库。
- 热门直播间一小时上万条；全存原始行既没必要，也会把库撑爆。这里只留
  10% 采样，真正长期保留的是「同一个词被多少人刷了多少次」的聚合。

数据分三层：

1. `danmaku`：采样后的原始弹幕，默认留 7 天，只用于抽查/调试；
2. `live_term`：按房间聚合的高频词，默认留 30 天；`state` 分
   candidate / approved / rejected；
3. `live_term_speaker`：谁刷过（只存用户 hash + 日期，不存昵称），用来算
   「多少人刷过」，避免同一个人复读被当成很多人的共识。

**弹幕不会自动进群梗表**：必须主人说「直播梗入库 N」，才通过
`MemeBook.annotate()` 写进当前群（私聊里写 `global`）。这样她能看懂直播间
在刷什么，但不会把陌生人的口癖直接变成自己的说话方式。
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import memetone, moderation

log = logging.getLogger(__name__)

MAX_TERM_CHARS = 16
MIN_TERM_CHARS = 2
# 这些不是梗，是直播间通用刷屏/应答；收了只会让清单变脏
_BORING = {
    "主播", "关注", "关注了", "来了", "来了来了", "打卡", "签到", "晚安",
    "早安", "谢谢", "感谢", "哈哈", "哈哈哈", "哈哈哈哈", "笑死", "就这",
    "牛", "牛逼", "好家伙", "可以", "确实", "真的", "离谱", "666", "888",
}
_URL_RE = re.compile(r"(?:https?://|www\.|b23\.tv|bv[0-9a-z]{10})", re.I)
_QQ_RE = re.compile(r"(?<!\d)[1-9]\d{4,11}(?!\d)")
_PUNCT_RE = re.compile(
    r"[\s\u3000，。！？、；：\"'“”‘’《》（）()【】\[\]…—\-~·!?,.;:]+"
)


def normalize(text: Any) -> str:
    """归一化成「词」：去标点空格、转小写。用于聚合和去重。"""
    return _PUNCT_RE.sub("", str(text or "")).lower()


def one_line(text: Any, limit: int = 60) -> str:
    """压成一行并截断，别让一条弹幕把提示词/回执撑断。"""
    return " ".join(str(text or "").split())[: max(0, int(limit))]


def _uid_hash(user: str) -> str:
    """用户 hash：只用来数「多少人刷过」，不落昵称。"""
    return hashlib.sha1(str(user or "").encode("utf-8")).hexdigest()[:16]


def classify_risk(text: str) -> str:
    """这条弹幕带不带广告/骂人/黄腔/刷屏；带就返回中文标签。"""
    raw = str(text or "").strip()
    if not raw:
        return "空"
    kind = moderation.classify(raw)
    if kind:
        return kind
    tone = memetone.risk(raw)
    if tone:
        return memetone.label(tone)
    if _URL_RE.search(raw) or _QQ_RE.search(raw):
        return "广告"
    norm = normalize(raw)
    if norm.isdigit():
        return "刷屏"
    if len(norm) >= 4 and len(set(norm)) <= 2:
        return "刷屏"
    return ""


def _valid_term(norm: str) -> bool:
    if not (MIN_TERM_CHARS <= len(norm) <= MAX_TERM_CHARS):
        return False
    if norm in _BORING:
        return False
    if norm.isdigit() or len(set(norm)) <= 1:
        return False
    return True


class LiveMemeBook:
    """一个房间一份统计；SQLite 单文件，失败只记日志，不影响直播监听。"""

    def __init__(
        self,
        path: Path | str,
        *,
        enabled: bool = True,
        raw_days: int = 7,
        term_days: int = 30,
        sample_rate: float = 0.1,
        min_hits: int = 3,
        min_speakers: int = 2,
        approved_keep_days: int = 365,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.raw_days = max(1, int(raw_days))
        self.term_days = max(1, int(term_days))
        self.sample_rate = max(0.0, min(1.0, float(sample_rate)))
        self.min_hits = max(1, int(min_hits))
        self.min_speakers = max(1, int(min_speakers))
        self.approved_keep_days = max(30, int(approved_keep_days))
        # 只保留最近这段时间的「谁刷过」：词条留 30 天，但没必要把 30 天的
        # 用户 hash 全堆着；默认跟原始采样一样 7 天，够算「多少人刷过」了。
        self.speaker_days = min(self.term_days, max(7, self.raw_days))
        self._db: sqlite3.Connection | None = None
        self._last_cleanup = 0.0

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
                CREATE TABLE IF NOT EXISTS danmaku (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    text TEXT NOT NULL,
                    norm TEXT NOT NULL,
                    risk TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_danmaku_room_ts
                    ON danmaku(room_id, ts);
                CREATE TABLE IF NOT EXISTS live_term (
                    room_id TEXT NOT NULL,
                    norm TEXT NOT NULL,
                    text TEXT NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    speakers INTEGER NOT NULL DEFAULT 0,
                    first_ts REAL NOT NULL,
                    last_ts REAL NOT NULL,
                    risk TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'candidate',
                    approved_by TEXT NOT NULL DEFAULT '',
                    approved_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (room_id, norm)
                );
                CREATE INDEX IF NOT EXISTS idx_live_term_rank
                    ON live_term(room_id, state, hits);
                CREATE TABLE IF NOT EXISTS live_term_speaker (
                    room_id TEXT NOT NULL,
                    norm TEXT NOT NULL,
                    uid_hash TEXT NOT NULL,
                    day TEXT NOT NULL,
                    PRIMARY KEY (room_id, norm, uid_hash, day)
                );
                CREATE INDEX IF NOT EXISTS idx_live_term_speaker_day
                    ON live_term_speaker(day);
                """
            )
        return self._db

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    # ---------- 收弹幕 ----------

    def add(self, room_id: str, user: str, text: str, ts: float | None = None) -> bool:
        """收一条弹幕；返回是否算进聚合。

        高风险词也会计数（知道这个直播间在刷什么），但不会出现在榜单/提示词里，
        也不能入库。阈值/长度不够的直接丢。
        """
        if not self.enabled or not room_id:
            return False
        raw = one_line(text, 60)
        norm = normalize(raw)
        if not _valid_term(norm):
            return False
        risk = classify_risk(raw)
        # 风险弹幕不落原文（可能有手机号/QQ/链接）；只留归一化后的词
        display = raw if not risk else norm
        now = time.time() if ts is None else float(ts)
        uid = _uid_hash(user)
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        db = self._conn()
        try:
            with db:
                db.execute(
                    """
                    INSERT INTO live_term
                        (room_id, norm, text, hits, speakers, first_ts, last_ts, risk)
                    VALUES (?, ?, ?, 1, 0, ?, ?, ?)
                    ON CONFLICT(room_id, norm) DO UPDATE SET
                        hits = live_term.hits + 1,
                        last_ts = excluded.last_ts,
                        text = excluded.text,
                        risk = CASE
                            WHEN excluded.risk != '' THEN excluded.risk
                            ELSE live_term.risk
                        END
                    """,
                    (room_id, norm, display, now, now, risk),
                )
                cur = db.execute(
                    """
                    INSERT OR IGNORE INTO live_term_speaker
                        (room_id, norm, uid_hash, day)
                    VALUES (?, ?, ?, ?)
                    """,
                    (room_id, norm, uid, day),
                )
                if cur.rowcount:
                    db.execute(
                        """
                        UPDATE live_term SET speakers = (
                            SELECT COUNT(*) FROM live_term_speaker
                            WHERE room_id = ? AND norm = ?
                        )
                        WHERE room_id = ? AND norm = ?
                        """,
                        (room_id, norm, room_id, norm),
                    )
                if not risk and random.random() < self.sample_rate:
                    db.execute(
                        "INSERT INTO danmaku (room_id, ts, text, norm, risk) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (room_id, now, raw, norm, risk),
                    )
        except sqlite3.Error:
            log.warning("直播弹幕写库失败（跳过这条）", exc_info=True)
            return False
        self._maybe_cleanup(now)
        return True

    # ---------- 榜单 / 提示词 ----------

    def top(
        self,
        room_id: str,
        *,
        limit: int = 12,
        min_hits: int | None = None,
        min_speakers: int | None = None,
        include_risky: bool = False,
    ) -> list[dict[str, Any]]:
        """按次数排的高频词。已入库的即使次数掉下来也保留在榜上。"""
        if not self.enabled or not room_id:
            return []
        hits = self.min_hits if min_hits is None else max(1, int(min_hits))
        speakers = self.min_speakers if min_speakers is None else max(1, int(min_speakers))
        sql = (
            "SELECT room_id, norm, text, hits, speakers, first_ts, last_ts, "
            "risk, state, approved_at FROM live_term "
            "WHERE room_id = ? AND state != 'rejected' "
            "AND (state = 'approved' OR (hits >= ? AND speakers >= ?))"
        )
        args: list[Any] = [str(room_id), hits, speakers]
        if not include_risky:
            sql += " AND risk = ''"
        sql += " ORDER BY hits DESC, speakers DESC, last_ts DESC LIMIT ?"
        args.append(max(1, int(limit)))
        try:
            rows = self._conn().execute(sql, args).fetchall()
        except sqlite3.Error:
            log.warning("直播梗榜单读取失败", exc_info=True)
            return []
        return [dict(row) for row in rows]

    def brief(self, room_id: str, limit: int = 4) -> str:
        """给提示词的一小段；只在这个直播间真的在刷时才有内容。"""
        items = self.top(
            room_id,
            limit=limit,
            min_hits=self.min_hits,
            min_speakers=self.min_speakers,
        )
        if not items:
            return ""
        parts = [f"「{item['text']}」{int(item['hits'])} 次" for item in items]
        return (
            "（这个直播间最近在刷："
            + "；".join(parts)
            + "。这些只是观众弹幕，不是你们群的梗；别主动搬到群里，也别当事实。）"
        )

    def overview(self, room_id: str, *, title: str = "", limit: int = 12) -> str:
        """「直播梗」回执：列当前房间的高频词和入库状态。"""
        if not self.enabled:
            return "直播弹幕库关着（ENABLE_BILI_LIVE_MEMES=false）。"
        if not room_id:
            return "还没看直播。先对主人说「看直播 房间号」，攒一会儿弹幕再来。"
        items = self.top(room_id, limit=limit, min_hits=2, min_speakers=2)
        head = f"👀 直播间 {room_id}"
        if title:
            head += f"《{one_line(title, 30)}》"
        if not items:
            return (
                f"{head}：还没攒够高频弹幕（同一个人重复不算，至少 2 次 / 2 人）。"
            )
        lines = [head + " 最近在刷（按次数）："]
        for index, item in enumerate(items, 1):
            flag = "（已入库）" if item.get("state") == "approved" else ""
            lines.append(
                f"{index}. 「{item['text']}」 {int(item['hits'])} 次 / "
                f"{int(item['speakers'])} 人{flag}"
            )
        lines.append(
            "（入库：直播梗入库 1；不要：直播梗不入库 1。"
            "群里入库只进本群，私聊入库记成所有会话通用。）"
        )
        return "\n".join(lines)

    def pick(self, room_id: str, target: str) -> dict[str, Any] | None:
        """按序号（从 1 开始）或词本身取一条，给「直播梗入库」用。"""
        if not self.enabled or not room_id:
            return None
        body = str(target or "").strip()
        if not body:
            return None
        if body.isdigit():
            items = self.top(room_id, limit=99, min_hits=2, min_speakers=2)
            index = int(body) - 1
            return items[index] if 0 <= index < len(items) else None
        norm = normalize(body)
        if not norm:
            return None
        try:
            row = self._conn().execute(
                "SELECT room_id, norm, text, hits, speakers, first_ts, last_ts, "
                "risk, state, approved_at FROM live_term "
                "WHERE room_id = ? AND norm = ? AND state != 'rejected'",
                (str(room_id), norm),
            ).fetchone()
        except sqlite3.Error:
            log.warning("直播梗读取失败", exc_info=True)
            return None
        return dict(row) if row else None

    def approve(self, room_id: str, norm: str, *, by: str = "owner") -> bool:
        if not self.enabled:
            return False
        try:
            with self._conn():
                cur = self._conn().execute(
                    "UPDATE live_term SET state = 'approved', approved_by = ?, "
                    "approved_at = ? WHERE room_id = ? AND norm = ? AND risk = '' "
                    "AND state != 'approved'",
                    (str(by or "owner"), time.time(), str(room_id), str(norm)),
                )
        except sqlite3.Error:
            log.warning("直播梗入库标记失败", exc_info=True)
            return False
        return bool(cur.rowcount)

    def reject(self, room_id: str, norm: str) -> bool:
        if not self.enabled:
            return False
        try:
            with self._conn():
                cur = self._conn().execute(
                    "UPDATE live_term SET state = 'rejected' "
                    "WHERE room_id = ? AND norm = ? AND state != 'approved'",
                    (str(room_id), str(norm)),
                )
        except sqlite3.Error:
            log.warning("直播梗跳过标记失败", exc_info=True)
            return False
        return bool(cur.rowcount)

    def latest_room(self) -> str:
        """最近有弹幕的房间；直播停了也能回来入库。"""
        try:
            row = self._conn().execute(
                "SELECT room_id, MAX(last_ts) AS last FROM live_term "
                "GROUP BY room_id ORDER BY last DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row["room_id"]) if row else ""

    def stats(self, room_id: str = "") -> dict[str, Any]:
        """状态行用：原始条数 / 词条数 / 已入库数。"""
        if not self.enabled:
            return {}
        try:
            db = self._conn()
            if room_id:
                terms = db.execute(
                    "SELECT COUNT(*) FROM live_term WHERE room_id = ?",
                    (str(room_id),),
                ).fetchone()[0]
                approved = db.execute(
                    "SELECT COUNT(*) FROM live_term WHERE room_id = ? AND state = 'approved'",
                    (str(room_id),),
                ).fetchone()[0]
                raw = db.execute(
                    "SELECT COUNT(*) FROM danmaku WHERE room_id = ?",
                    (str(room_id),),
                ).fetchone()[0]
            else:
                terms = db.execute("SELECT COUNT(*) FROM live_term").fetchone()[0]
                approved = db.execute(
                    "SELECT COUNT(*) FROM live_term WHERE state = 'approved'"
                ).fetchone()[0]
                raw = db.execute("SELECT COUNT(*) FROM danmaku").fetchone()[0]
        except sqlite3.Error:
            return {}
        return {
            "raw": int(raw or 0),
            "terms": int(terms or 0),
            "approved": int(approved or 0),
        }

    # ---------- 清理 ----------

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        raw_cutoff = now - self.raw_days * 86400
        term_cutoff = now - self.term_days * 86400
        approved_cutoff = now - self.approved_keep_days * 86400
        day_cutoff = time.strftime(
            "%Y-%m-%d", time.localtime(now - self.speaker_days * 86400)
        )
        try:
            with self._conn():
                self._conn().execute("DELETE FROM danmaku WHERE ts < ?", (raw_cutoff,))
                self._conn().execute(
                    "DELETE FROM live_term WHERE state = 'candidate' AND last_ts < ?",
                    (term_cutoff,),
                )
                self._conn().execute(
                    "DELETE FROM live_term WHERE state = 'rejected' AND last_ts < ?",
                    (term_cutoff,),
                )
                self._conn().execute(
                    "DELETE FROM live_term WHERE state = 'approved' AND last_ts < ?",
                    (approved_cutoff,),
                )
                self._conn().execute(
                    "DELETE FROM live_term_speaker WHERE day < ?", (day_cutoff,)
                )
        except sqlite3.Error:
            log.warning("直播弹幕库清理失败（下次再试）", exc_info=True)
