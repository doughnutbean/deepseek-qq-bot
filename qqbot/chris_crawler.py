"""克里斯评论语料的全量采集 + SQLite 存储。

设计口径（见 `docs/克里斯语料.md`）：

- **爬全**：一级评论翻到 `is_end`，每个有楼中楼的根评论都翻完；不是只取热评。
- **完整度可核对**：服务端 `cursor.all_count`（含子评论）对本地
  「一级 + 子评论」算覆盖率，够 99.5%（至少差 1 条）才算 `done`；
  不够的标 `limited`，下次接着补。
- **断点续爬**：一级记 `root_offset`，楼中楼记每个根的 `fetched`；
  所有写入按 `(rpid, aid)` 幂等，重跑不会重复。
- **只在独立 CLI 里跑**：不 import 进 `app.py`，采集失败不影响聊天。

数据最小化：只存评论正文、点赞/回复数、时间和 UID 哈希；不存昵称、头像、
图片 URL，也不建用户画像。
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .bili_comments import (
    all_count as cursor_all_count,
    complete_enough,
    coverage,
    in_window,
    next_offset,
    normalize_space_videos,
    page_cursor,
    page_is_end,
    page_replies,
    root_record,
    sub_record,
)

log = logging.getLogger(__name__)

# 服务端 all_count 会把已删除/折叠/审核中的评论也算进去。根评论翻到 is_end、
# 每个有楼中楼的根都翻到自然结束之后，仍然差一截的，就不该无限重爬：
# 覆盖率 ≥ 这个值才认账（标 has_deleted）。低于它更像接口只回了前几页，
# 继续 limited 补抓。实测删除缺口一般在 10% 出头，取 50% 留足余量。
SHORTFALL_FLOOR = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS video (
    bvid        TEXT PRIMARY KEY,
    aid         INTEGER UNIQUE,
    mid         TEXT,
    title       TEXT,
    pubdate     INTEGER DEFAULT 0,
    all_count   INTEGER DEFAULT 0,
    coverage    REAL DEFAULT 0,
    status      TEXT DEFAULT 'pending',
    has_deleted INTEGER DEFAULT 0,
    root_offset TEXT DEFAULT '',
    first_seen  INTEGER,
    last_crawled INTEGER
);

CREATE TABLE IF NOT EXISTS comment (
    rpid       INTEGER NOT NULL,
    aid        INTEGER NOT NULL,
    root       INTEGER NOT NULL DEFAULT 0,
    parent     INTEGER NOT NULL DEFAULT 0,
    ctime      INTEGER NOT NULL DEFAULT 0,
    message    TEXT,
    like       INTEGER DEFAULT 0,
    rcount     INTEGER DEFAULT 0,
    uid_hash   TEXT,
    first_seen INTEGER,
    last_seen  INTEGER,
    deleted    INTEGER DEFAULT 0,
    PRIMARY KEY (rpid, aid)
);

CREATE TABLE IF NOT EXISTS root_progress (
    aid        INTEGER NOT NULL,
    rpid       INTEGER NOT NULL,
    expected   INTEGER DEFAULT 0,
    fetched    INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'pending',
    checked_at INTEGER,
    PRIMARY KEY (aid, rpid)
);

-- 蒸馏层：从 comment 里提炼的候选，人工 approve 后才写进 memory.db / 群梗表。
CREATE TABLE IF NOT EXISTS candidate (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    meaning     TEXT DEFAULT '',
    evidence    TEXT DEFAULT '',
    bvid        TEXT DEFAULT '',
    rpid        INTEGER DEFAULT 0,
    url         TEXT DEFAULT '',
    score       REAL DEFAULT 0,
    seen_videos INTEGER DEFAULT 0,
    seen_users  INTEGER DEFAULT 0,
    fingerprint TEXT UNIQUE,
    status      TEXT DEFAULT 'candidate',
    pinned      INTEGER DEFAULT 0,
    created_at  INTEGER,
    reviewed_at INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_comment_aid ON comment(aid, parent);
CREATE INDEX IF NOT EXISTS idx_comment_ctime ON comment(ctime);
CREATE INDEX IF NOT EXISTS idx_video_status ON video(status);
CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate(status, kind);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CrawlStopped(RuntimeError):
    """连续失败/风控：本轮停下，进度保留，下次接着爬。"""


class ChrisStore:
    """评论库。WAL + busy_timeout，采集进程和只读查询可以同时开。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "ChrisStore":
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        try:
            self._db.execute(
                "ALTER TABLE candidate ADD COLUMN pinned INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        self._db.commit()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("ChrisStore 没打开：用 with ChrisStore(path) as store:")
        return self._db

    # ---------- meta ----------

    def get_meta(self, key: str) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    # ---------- video ----------

    def upsert_video(
        self, bvid: str, aid: int, mid: str = "", title: str = "", pubdate: int = 0
    ) -> dict[str, Any]:
        now = int(time.time())
        self.db.execute(
            """INSERT INTO video(bvid, aid, mid, title, pubdate, first_seen, last_crawled)
               VALUES (?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(bvid) DO UPDATE SET
                   aid=excluded.aid,
                   mid=CASE WHEN excluded.mid != '' THEN excluded.mid ELSE video.mid END,
                   title=CASE WHEN excluded.title != '' THEN excluded.title ELSE video.title END,
                   pubdate=CASE WHEN excluded.pubdate != 0 THEN excluded.pubdate ELSE video.pubdate END""",
            (bvid, int(aid), str(mid or ""), title or "", int(pubdate or 0), now),
        )
        self.db.commit()
        row = self.get_video(bvid=bvid)
        assert row is not None
        return row

    def get_video(self, bvid: str = "", aid: int = 0) -> dict[str, Any] | None:
        if bvid:
            row = self.db.execute("SELECT * FROM video WHERE bvid=?", (bvid,)).fetchone()
        elif aid:
            row = self.db.execute("SELECT * FROM video WHERE aid=?", (int(aid),)).fetchone()
        else:
            return None
        return dict(row) if row else None

    def update_video_progress(
        self,
        aid: int,
        root_offset: str | None = None,
        all_count: int | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        if root_offset is not None:
            sets.append("root_offset=?")
            vals.append(root_offset)
        if all_count is not None:
            sets.append("all_count=MAX(COALESCE(all_count,0), ?)")
            vals.append(int(all_count))
        if not sets:
            return
        vals.append(int(aid))
        self.db.execute(f"UPDATE video SET {', '.join(sets)} WHERE aid=?", vals)
        self.db.commit()

    def finish_video(
        self,
        aid: int,
        status: str,
        coverage_value: float,
        all_count: int,
        has_deleted: bool,
    ) -> None:
        now = int(time.time())
        self.db.execute(
            """UPDATE video SET status=?, coverage=?, all_count=MAX(COALESCE(all_count,0), ?),
                   has_deleted=?, last_crawled=?, root_offset=''
               WHERE aid=?""",
            (
                status,
                float(coverage_value),
                int(all_count),
                1 if has_deleted else 0,
                now,
                int(aid),
            ),
        )
        self.db.commit()

    def set_video_status(self, aid: int, status: str) -> None:
        self.db.execute(
            "UPDATE video SET status=?, last_crawled=? WHERE aid=?",
            (status, int(time.time()), int(aid)),
        )
        self.db.commit()

    # ---------- comment ----------

    def upsert_comments(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [
            (
                r["rpid"], r["aid"], r["root"], r["parent"], r["ctime"],
                r["message"], r["like"], r["rcount"], r["uid_hash"],
                r["first_seen"], r["last_seen"], r["deleted"],
            )
            for r in rows
        ]
        self.db.executemany(
            """INSERT INTO comment
               (rpid, aid, root, parent, ctime, message, like, rcount, uid_hash,
                first_seen, last_seen, deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rpid, aid) DO UPDATE SET
                   root=excluded.root,
                   parent=excluded.parent,
                   message=excluded.message,
                   like=excluded.like,
                   rcount=excluded.rcount,
                   last_seen=excluded.last_seen,
                   deleted=excluded.deleted""",
            payload,
        )
        self.db.commit()
        return len(payload)

    def root_rpids(self, aid: int) -> set[int]:
        rows = self.db.execute(
            "SELECT rpid FROM comment WHERE aid=? AND parent=0", (int(aid),)
        ).fetchall()
        return {int(r[0]) for r in rows}

    def roots_with_subs(self, aid: int) -> list[dict[str, int]]:
        rows = self.db.execute(
            """SELECT rpid, rcount FROM comment
               WHERE aid=? AND parent=0 AND rcount>0
               ORDER BY rcount DESC""",
            (int(aid),),
        ).fetchall()
        return [{"rpid": int(r["rpid"]), "rcount": int(r["rcount"] or 0)} for r in rows]

    def count_subs(self, aid: int, root: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM comment WHERE aid=? AND root=? AND parent>0",
            (int(aid), int(root)),
        ).fetchone()
        return int(row[0] if row else 0)

    def counts(self, aid: int) -> dict[str, int]:
        row = self.db.execute(
            """SELECT
                   SUM(CASE WHEN parent=0 THEN 1 ELSE 0 END) AS roots,
                   SUM(CASE WHEN parent>0 THEN 1 ELSE 0 END) AS subs,
                   COUNT(*) AS total
               FROM comment WHERE aid=?""",
            (int(aid),),
        ).fetchone()
        return {
            "roots": int(row["roots"] or 0) if row else 0,
            "subs": int(row["subs"] or 0) if row else 0,
            "total": int(row["total"] or 0) if row else 0,
        }

    # ---------- root_progress ----------

    def get_root_progress(self, aid: int, rpid: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM root_progress WHERE aid=? AND rpid=?",
            (int(aid), int(rpid)),
        ).fetchone()
        return dict(row) if row else None

    def set_root_progress(
        self, aid: int, rpid: int, expected: int, fetched: int, status: str
    ) -> None:
        self.db.execute(
            """INSERT INTO root_progress(aid, rpid, expected, fetched, status, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(aid, rpid) DO UPDATE SET
                   expected=excluded.expected,
                   fetched=MAX(COALESCE(root_progress.fetched,0), excluded.fetched),
                   status=excluded.status,
                   checked_at=excluded.checked_at""",
            (int(aid), int(rpid), int(expected), int(fetched), status, int(time.time())),
        )
        self.db.commit()

    # ---------- 统计 ----------

    def stats(self) -> dict[str, Any]:
        by_status = {
            str(r["status"]): int(r["n"])
            for r in self.db.execute(
                "SELECT status, COUNT(*) AS n FROM video GROUP BY status"
            ).fetchall()
        }
        row = self.db.execute(
            """SELECT COUNT(*) AS comments,
                      COUNT(DISTINCT aid) AS videos,
                      COALESCE(SUM(CASE WHEN parent=0 THEN 1 ELSE 0 END), 0) AS roots,
                      COALESCE(SUM(CASE WHEN parent>0 THEN 1 ELSE 0 END), 0) AS subs
               FROM comment"""
        ).fetchone()
        cov = self.db.execute(
            """SELECT COUNT(*) AS n, COALESCE(AVG(coverage), 0) AS avg_cov,
                      COALESCE(SUM(CASE WHEN coverage < 0.99 THEN 1 ELSE 0 END), 0) AS low,
                      COALESCE(SUM(has_deleted), 0) AS deleted
               FROM video WHERE status='done'"""
        ).fetchone()
        return {
            "videos": int(row["videos"] or 0) if row else 0,
            "comments": int(row["comments"] or 0) if row else 0,
            "roots": int(row["roots"] or 0) if row else 0,
            "subs": int(row["subs"] or 0) if row else 0,
            "by_status": by_status,
            "done": int(cov["n"] or 0) if cov else 0,
            "avg_coverage": float(cov["avg_cov"] or 0) if cov else 0.0,
            "low_coverage": int(cov["low"] or 0) if cov else 0,
            "has_deleted": int(cov["deleted"] or 0) if cov else 0,
        }

    def incomplete(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT bvid, title, all_count, coverage, status, has_deleted
               FROM video WHERE status != 'done'
               ORDER BY pubdate DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_videos(
        self, mid: str, since: int = 0, include_done: bool = False, limit: int = 0
    ) -> list[dict[str, Any]]:
        """队列：该 UP 还没爬完的视频，按发布时间倒序。"""
        sql = """SELECT * FROM video
                 WHERE mid = ? AND (? = 0 OR pubdate >= ?)"""
        args: list[Any] = [str(mid), int(since), int(since)]
        if not include_done:
            sql += " AND status != 'done'"
        sql += " ORDER BY pubdate DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---------- candidate ----------

    def upsert_candidates(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = int(time.time())
        for row in rows:
            self.db.execute(
                """INSERT INTO candidate
                   (kind, text, meaning, evidence, bvid, rpid, url, score,
                    seen_videos, seen_users, fingerprint, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       score=MAX(candidate.score, excluded.score),
                       evidence=excluded.evidence,
                       bvid=excluded.bvid,
                       rpid=excluded.rpid,
                       url=excluded.url,
                       seen_videos=MAX(candidate.seen_videos, excluded.seen_videos),
                       seen_users=MAX(candidate.seen_users, excluded.seen_users)""",
                (
                    row["kind"], row["text"], row.get("meaning", ""),
                    row.get("evidence", ""), row.get("bvid", ""),
                    int(row.get("rpid") or 0), row.get("url", ""),
                    float(row.get("score") or 0), int(row.get("seen_videos") or 0),
                    int(row.get("seen_users") or 0), row["fingerprint"], now,
                ),
            )
        self.db.commit()
        return len(rows)

    def list_candidates(
        self, kind: str = "", status: str = "candidate", limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM candidate
               WHERE (? = '' OR kind = ?) AND status = ?
               ORDER BY score DESC, id ASC LIMIT ?""",
            (kind, kind, status, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_candidate(self, cid: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM candidate WHERE id=?", (int(cid),)).fetchone()
        return dict(row) if row else None

    def set_candidate_status(
        self, cid: int, status: str, pinned: bool | None = None
    ) -> None:
        if pinned is None:
            self.db.execute(
                "UPDATE candidate SET status=?, reviewed_at=? WHERE id=?",
                (status, int(time.time()), int(cid)),
            )
        else:
            self.db.execute(
                "UPDATE candidate SET status=?, pinned=?, reviewed_at=? WHERE id=?",
                (status, 1 if pinned else 0, int(time.time()), int(cid)),
            )
        self.db.commit()

    def candidate_counts(self) -> dict[str, int]:
        return {
            str(r["kind"]): int(r["n"])
            for r in self.db.execute(
                "SELECT kind, COUNT(*) AS n FROM candidate WHERE status='candidate' GROUP BY kind"
            ).fetchall()
        }


class ChrisCrawler:
    """全量评论采集器。一轮把单个视频爬到 `done`；失败标 `limited` 保留进度。"""

    def __init__(
        self,
        api: Any,
        store: ChrisStore,
        delay: float = 1.5,
        jitter: float = 1.0,
        stop_after_failures: int = 5,
        salt: str = "",
        retry_wait: float = 20.0,
        space_delay: float = 4.0,
    ) -> None:
        self.api = api
        self.store = store
        self.delay = max(0.0, float(delay))
        self.jitter = max(0.0, float(jitter))
        self.stop_after_failures = max(1, int(stop_after_failures))
        self.salt = salt
        self.retry_wait = max(0.0, float(retry_wait))
        # 投稿列表比评论页脆：实测 1s 间隔翻到第 4 页就拒，放到 4s 稳定。
        self.space_delay = max(0.0, float(space_delay))
        self._fails = 0
        self._short_subs = 0
        self._subs_ok = True
        self.stopped = False

    # ---------- 基础 ----------

    def reset(self) -> None:
        """暂停后重试用：清掉停止标记和连续失败计数。"""
        self._fails = 0
        self.stopped = False
        self._subs_ok = True
        self._short_subs = 0

    async def _pause(self) -> None:
        if self.delay or self.jitter:
            await asyncio.sleep(self.delay + random.uniform(0, self.jitter))

    async def _get(self, call: Callable[[], Any]) -> dict[str, Any] | None:
        """发一次请求，连续失败就停；412 通常只封 1~3 秒，短退避即可恢复。"""
        for attempt in range(4):
            payload = await call()
            if payload is not None:
                self._fails = 0
                return payload
            self._fails += 1
            if self._fails >= self.stop_after_failures:
                raise CrawlStopped(
                    f"连续 {self._fails} 次请求失败（风控/网络），本轮停止"
                )
            wait = 3.0 * (attempt + 1)
            blocked = 0.0
            if hasattr(self.api, "blocked_for"):
                blocked = float(self.api.blocked_for())
            await asyncio.sleep(max(wait, blocked + 1.0))
        return None

    # ---------- 视频信息 ----------

    async def _ensure_video(self, bvid: str = "", aid: int = 0) -> dict[str, Any] | None:
        existing = self.store.get_video(bvid=bvid, aid=aid)
        if existing and existing.get("aid"):
            return existing
        payload = await self._get(
            lambda: self.api.video_view(bvid=bvid, aid=int(aid or 0))
        )
        data = (payload or {}).get("data")
        if not isinstance(data, dict) or not data.get("aid"):
            return None
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        return self.store.upsert_video(
            bvid=str(data.get("bvid") or bvid),
            aid=int(data.get("aid") or aid),
            mid=str(owner.get("mid") or ""),
            title=str(data.get("title") or "").strip(),
            pubdate=_int(data.get("pubdate")),
        )

    # ---------- 一级评论 ----------

    async def _crawl_roots(self, video: dict[str, Any], since: int) -> bool:
        aid = int(video["aid"])
        existing = self.store.root_rpids(aid)
        offset = str(video.get("root_offset") or "")
        seen_offsets: set[str] = set()
        empties = 0
        restarted = False

        while True:
            payload = await self._get(
                lambda: self.api.comments_page(aid, mode=2, offset=offset)
            )
            if payload is None:
                self.store.update_video_progress(aid, root_offset="")
                return False
            code = _int(payload.get("code"))
            if code != 0:
                if code == -404:  # 评论区关闭/不存在
                    return True
                self.store.update_video_progress(aid, root_offset="")
                return False
            replies = page_replies(payload)
            cursor = page_cursor(payload)
            remote = cursor_all_count(cursor)
            if remote:
                self.store.update_video_progress(aid, all_count=remote)

            if not replies:
                if page_is_end(cursor):
                    return True
                if offset and not restarted:
                    # 断点里的 offset 失效：从第一页重扫，靠 rpid 去重。
                    log.info("aid=%s 的 offset 失效，从头重扫", aid)
                    offset = ""
                    restarted = True
                    continue
                empties += 1
                if empties >= 3:
                    self.store.update_video_progress(aid, root_offset="")
                    return False
                await self._pause()
                continue

            empties = 0
            now = int(time.time())
            rows: list[dict[str, Any]] = []
            for reply in replies:
                rpid = _int(reply.get("rpid"))
                if not rpid or rpid in existing:
                    continue
                if not in_window(_int(reply.get("ctime")), since=since):
                    continue
                row = root_record(reply, aid, now, self.salt)
                if row:
                    rows.append(row)
                    existing.add(rpid)
            if rows:
                self.store.upsert_comments(rows)

            nxt = next_offset(cursor)
            end = page_is_end(cursor)
            if end or not nxt:
                # 到页尾（有的响应不给 is_end，只把 next_offset 留空）：按 BiliSpider
                # 的口径当结束；够不够全由 _finish 的覆盖率兜底。
                self.store.update_video_progress(aid, root_offset="")
                return True
            if nxt in seen_offsets:
                self.store.update_video_progress(aid, root_offset="")
                return False
            seen_offsets.add(nxt)
            offset = nxt
            self.store.update_video_progress(aid, root_offset=nxt)
            await self._pause()

    # ---------- 楼中楼 ----------

    async def _crawl_one_sub(
        self, aid: int, root: int, expected: int
    ) -> tuple[int, bool]:
        """抓一个根评论的楼中楼。返回 (实际条数, 是否翻到自然结束)。"""
        page = 1
        total = 0
        natural_end = False
        while True:
            payload = await self._get(
                lambda: self.api.sub_comments(aid, root, pn=page)
            )
            if payload is None:
                return self.store.count_subs(aid, root), natural_end
            replies = page_replies(payload)
            if not replies:
                natural_end = True
                break
            now = int(time.time())
            rows = [
                sub_record(reply, aid, root, now, self.salt)
                for reply in replies
            ]
            self.store.upsert_comments([r for r in rows if r])
            total += len(replies)
            if total >= expected:
                break
            if len(replies) < 20:
                natural_end = True
                break
            page += 1
            await self._pause()
        return self.store.count_subs(aid, root), natural_end

    async def _crawl_subs(self, video: dict[str, Any]) -> bool:
        """抓所有根评论的楼中楼。返回是否全部翻到自然结束。"""
        aid = int(video["aid"])
        all_done = True
        for root in self.store.roots_with_subs(aid):
            expected = int(root["rcount"])
            progress = self.store.get_root_progress(aid, root["rpid"])
            if progress and int(progress.get("fetched") or 0) >= expected:
                continue
            actual, natural_end = await self._crawl_one_sub(aid, root["rpid"], expected)
            if natural_end and actual < expected:
                # 根上写着 N 条，实际翻完少几条：评论被删了，不是我们漏抓。
                self._short_subs += 1
            status = "done" if actual >= expected or natural_end else "limited"
            if status == "limited":
                all_done = False
            self.store.set_root_progress(aid, root["rpid"], expected, actual, status)
            await self._pause()
        return all_done

    # ---------- 收尾 ----------

    async def _finish(
        self, video: dict[str, Any], roots_ok: bool, since: int
    ) -> dict[str, Any]:
        aid = int(video["aid"])
        latest = self.store.get_video(aid=aid) or video
        counts = self.store.counts(aid)
        expected = max(_int(latest.get("all_count")), counts["roots"])
        if expected <= 0:
            expected = counts["total"]
        cov = coverage(counts["roots"], counts["subs"], expected)
        has_deleted = bool(self._short_subs) or counts["total"] > expected + 1
        # 全部可见的部分都翻完了（根到 is_end、楼中楼自然结束），但服务端计数
        # 仍多出一截：那是删除/折叠/审核中的评论，认账并如实标记。
        # 唯一的例外是「游客态只回了 0~3 条根评论」——那是接口截断，不能算完。
        exhausted = roots_ok and self._subs_ok and not since
        suspect_truncated = (
            counts["roots"] <= 3 and expected >= 50 and counts["total"] < expected
        )
        if complete_enough(counts["total"], expected):
            status = "done"
        elif exhausted and not suspect_truncated and cov >= SHORTFALL_FLOOR:
            status = "done"
            has_deleted = True
        elif counts["total"] > expected:
            status = "done"
            has_deleted = True
        else:
            status = "limited"
        self.store.finish_video(aid, status, cov, expected, has_deleted)
        return {
            "aid": aid,
            "bvid": video.get("bvid", ""),
            "status": status,
            "coverage": round(cov, 4),
            "roots": counts["roots"],
            "subs": counts["subs"],
            "expected": expected,
            "has_deleted": has_deleted,
        }

    # ---------- 对外 ----------

    async def crawl_video(
        self, bvid: str = "", aid: int = 0, since_days: int = 0
    ) -> dict[str, Any]:
        video: dict[str, Any] | None = None
        since = int(time.time()) - int(since_days) * 86400 if since_days else 0
        self._short_subs = 0
        self._subs_ok = True
        try:
            video = await self._ensure_video(bvid=bvid, aid=aid)
            if not video:
                return {"status": "error", "bvid": bvid, "aid": aid}
            roots_ok = await self._crawl_roots(video, since)
            if roots_ok or self.store.counts(int(video["aid"]))["roots"]:
                self._subs_ok = await self._crawl_subs(video)
            return await self._finish(video, roots_ok, since)
        except CrawlStopped as exc:
            log.warning("采集暂停：%s", exc)
            self.stopped = True
            if video is not None:
                self.store.set_video_status(int(video["aid"]), "limited")
            return {
                "status": "limited",
                "bvid": (video or {}).get("bvid", bvid),
                "aid": (video or {}).get("aid", aid),
                "error": str(exc),
            }

    async def sync_videos(
        self,
        mid: str,
        max_pages: int = 50,
        force: bool = False,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> int:
        """把 UP 的投稿列表同步进 video 表（只写元数据，不爬评论）。

        6 小时内同步过就跳过（投稿列表翻页容易被 412，重跑没必要再翻一遍）；
        要强制刷新用 force=True。
        """
        key = f"sync:{mid}"
        page_key = f"sync_page:{mid}"
        last = _int(self.store.get_meta(key))
        if not force and last and time.time() - last < 6 * 3600:
            return 0
        added = 0
        complete = False
        # 没有页码记录时，按已同步的视频数估算从第几页接着翻（30 个/页）。
        rows = self.store.db.execute(
            "SELECT COUNT(*) FROM video WHERE mid=?", (str(mid),)
        ).fetchone()
        synced_rows = int(rows[0]) if rows else 0
        guess = max(1, synced_rows // 30 + 1) if synced_rows else 1
        page = 1 if force else max(1, _int(self.store.get_meta(page_key)) or guess)
        while page <= max_pages:
            try:
                payload = await self._get(lambda: self.api.space_videos(mid, pn=page))
            except CrawlStopped as exc:
                # 投稿列表被风控时别把整轮拖死：先爬已经同步到的队列，
                # 页码记下来，下次从这一页接着翻。
                log.warning("投稿列表暂停（第 %d 页）：%s", page, exc)
                self.store.set_meta(page_key, str(page))
                return added
            if payload is None:
                self.store.set_meta(page_key, str(page))
                return added
            if _int(payload.get("code")) != 0:
                log.warning("投稿列表 code=%s，停止同步", payload.get("code"))
                break
            items, total = normalize_space_videos(payload)
            if not items:
                complete = True
                break
            for item in items:
                existing = self.store.get_video(bvid=item["bvid"])
                self.store.upsert_video(
                    item["bvid"],
                    item["aid"],
                    mid=str(mid),
                    title=item["title"],
                    pubdate=item["pubdate"],
                )
                if not existing:
                    added += 1
            if progress:
                progress(page, len(items), total)
            if len(items) < 30:
                complete = True
                break
            self.store.set_meta(page_key, str(page + 1))
            page += 1
            if self.space_delay:
                await asyncio.sleep(self.space_delay)
        if complete:
            self.store.set_meta(key, str(int(time.time())))
            self.store.set_meta(page_key, "0")
        return added

    async def crawl_pending(
        self,
        mid: str,
        limit: int = 0,
        since_days: int = 0,
        refresh: bool = False,
        progress: Callable[[str, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """从库里取未完成的队列，按发布时间倒序爬（先跑 sync_videos）。"""
        since = int(time.time()) - int(since_days) * 86400 if since_days else 0
        rows = self.store.pending_videos(mid, since=since, include_done=refresh)
        done: list[dict[str, Any]] = []
        for row in rows:
            if self.stopped:
                break
            if progress:
                progress(row["bvid"], len(done) + 1)
            result = await self.crawl_video(bvid=row["bvid"])
            done.append(result)
            if limit and len(done) >= limit:
                break
        return done

    async def crawl_mid(
        self,
        mid: str,
        limit: int = 0,
        since_days: int = 0,
        refresh: bool = False,
        resync: bool = False,
        progress: Callable[[str, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """同步投稿列表 → 从库里取未完成的队列，按发布时间倒序爬。"""
        added = await self.sync_videos(mid, force=resync)
        if added:
            log.info("投稿列表新增 %d 个视频", added)
        return await self.crawl_pending(
            mid, limit=limit, since_days=since_days, refresh=refresh, progress=progress
        )
