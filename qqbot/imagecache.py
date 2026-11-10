"""图片记忆：同一张图只让模型看一次。

群里的表情包天天重复出现。每出现一次就把图片再传一遍、再让模型认一遍，
又慢又贵 —— 其实第一次认完就能记住"这张图是什么意思"。

键用**图片字节的 sha1**，不是链接：QQ 下发的图片链接带会过期的 rkey，
同一个表情包每次的链接都不一样，按链接认会每次都认错成新图。

    cache = ImageCache(Path("data/image_cache.db"))
    cache.get(sha1)          # 认过就返回当时的描述
    cache.put(sha1, "蓝色鲸鱼娘举拳…")
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ImageCache:
    def __init__(self, path: Path, enabled: bool = True, limit: int = 2000) -> None:
        self.path = path
        self.enabled = enabled
        self.limit = limit
        self._db: sqlite3.Connection | None = None
        if enabled:
            self._connect()

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS image_cache (
                hash  TEXT PRIMARY KEY,
                desc  TEXT NOT NULL,
                ts    INTEGER NOT NULL,
                hits  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_image_ts ON image_cache(ts);
            """
        )
        db.commit()
        self._db = db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def get(self, image_hash: str) -> str:
        """认得就返回描述并记一次命中；不认得返回空串。"""
        if not self.enabled or self._db is None or not image_hash:
            return ""
        row = self._db.execute(
            "SELECT desc FROM image_cache WHERE hash=?", (image_hash,)
        ).fetchone()
        if not row:
            return ""
        self._db.execute(
            "UPDATE image_cache SET hits = hits + 1, ts = ? WHERE hash=?",
            (int(time.time()), image_hash),
        )
        self._db.commit()
        return str(row["desc"])

    def put(self, image_hash: str, desc: str) -> None:
        if not self.enabled or self._db is None or not image_hash or not desc.strip():
            return
        desc = desc.strip()[:300]
        self._db.execute(
            "INSERT INTO image_cache(hash, desc, ts, hits) VALUES (?,?,?,0) "
            "ON CONFLICT(hash) DO UPDATE SET desc=excluded.desc, ts=excluded.ts",
            (image_hash, desc, int(time.time())),
        )
        self._db.commit()
        self._evict()

    def _evict(self) -> None:
        """超出上限就淘汰最久没用的。"""
        if not self.enabled or self._db is None or self.limit <= 0:
            return
        total = self._db.execute("SELECT COUNT(*) AS n FROM image_cache").fetchone()["n"]
        if total <= self.limit:
            return
        self._db.execute(
            "DELETE FROM image_cache WHERE hash IN ("
            "  SELECT hash FROM image_cache ORDER BY hits ASC, ts ASC LIMIT ?"
            ")",
            (total - self.limit,),
        )
        self._db.commit()
        log.info("图片记忆超出上限，淘汰 %d 条", total - self.limit)

    def stats(self) -> dict[str, Any]:
        if not self.enabled or self._db is None:
            return {"count": 0, "hits": 0}
        row = self._db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS hits FROM image_cache"
        ).fetchone()
        return {"count": row["n"], "hits": row["hits"]}
