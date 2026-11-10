"""群消息全量落库。

只记「她回应过的消息」是不够的 —— 那样学到的永远是"主人跟她说话"，
学不到群友之间怎么接茬。这里把群里每条文字消息都记下来（含没被回应的），
作为风格学习与黑话挖掘的语料。

- 只存文字：图片/表情/语音一律**不**入库；正文只剩占位符的消息整条跳过
- 按 message_id 去重：休眠回连时协议端可能重推，不能记成两条
- 合批后的合成事件（`_merged`）不重复入库：原消息到达时已经记过了
- 总量封顶，超了删最老的
- 隐私：内容只在本机 `group_log.db`，可通过开关关掉
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

# 纯占位正文：图片/表情/语音/视频/文件，以及残留的 CQ 码
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|动画表情|语音|视频|文件|卡片消息|合并转发)[^\]]*\]"
    r"|\[CQ:[^\]]+\]"
)

log = logging.getLogger(__name__)

MAX_ROWS = 200_000


class GroupLog:
    def __init__(self, path: Path, enabled: bool = True, max_rows: int = MAX_ROWS) -> None:
        self.path = path
        self.enabled = enabled
        self.max_rows = max_rows
        self._db: sqlite3.Connection | None = None
        if enabled:
            self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                uid      TEXT NOT NULL,
                nick     TEXT,
                text     TEXT NOT NULL,
                ts       INTEGER NOT NULL,
                msg_id   TEXT
            )
            """
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_gm_group ON messages(group_id, id)")
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_msgid ON messages(msg_id) "
            "WHERE msg_id IS NOT NULL"
        )
        # 老库没有 reply_to（引用回复的目标消息 id）：补一列，老行留空
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}
        if "reply_to" not in cols:
            self._db.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT")
        self._db.commit()

    # ---------- 写 ----------

    def add(
        self,
        group_id: str,
        uid: str,
        nick: str,
        text: str,
        ts: int,
        msg_id: str | None,
        reply_to: str | None = None,
    ) -> bool:
        body = (text or "").strip()
        if not self.enabled or self._db is None or not body or not group_id:
            return False
        if not _PLACEHOLDER_RE.sub("", body).strip():
            return False  # 只有 [图片]/[表情] 之类的占位，学不到说话方式
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO messages(group_id, uid, nick, text, ts, msg_id, reply_to) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(group_id),
                    str(uid),
                    (nick or "")[:32],
                    body[:500],
                    int(ts),
                    msg_id,
                    str(reply_to) if reply_to else None,
                ),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            log.debug("群消息入库失败：%s", exc)
            return False
        self._trim()
        return True

    def _trim(self) -> None:
        if self._db is None:
            return
        row = self._db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        if row and row[0] and row[0] > self.max_rows:
            self._db.execute(
                "DELETE FROM messages WHERE id IN "
                "(SELECT id FROM messages ORDER BY id LIMIT ?)",
                (row[0] - self.max_rows,),
            )
            self._db.commit()

    # ---------- 读 ----------

    def rows(self, group_id: str | None = None, limit: int | None = None) -> list[dict]:
        if self._db is None:
            return []
        sql = "SELECT id, group_id, uid, nick, text, ts FROM messages"
        params: tuple = ()
        if group_id:
            sql += " WHERE group_id=?"
            params = (str(group_id),)
        sql += " ORDER BY id"
        if limit:
            sql = f"SELECT * FROM ({sql} DESC LIMIT {int(limit)}) ORDER BY id"
        self._db.row_factory = sqlite3.Row
        return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def search(
        self,
        group_id: str,
        terms: list[str],
        limit: int = 6,
        days: float = 7,
    ) -> list[dict]:
        """按关键词找群消息（给记忆题的"旁听"用）。

        只读、不建索引：记忆题不是每轮都有，扫几天的 LIKE 足够快。
        关键词来自 `memory.keywords()`，调用方负责去重；这里只做 OR 匹配。
        """
        if self._db is None or not group_id or not terms or limit <= 0:
            return []
        cutoff = int(time.time() - max(0.0, float(days)) * 86400)
        clean = []
        for term in terms:
            t = str(term or "").replace("%", "").replace("_", "").strip()
            if t and t not in clean:
                clean.append(t)
        if not clean:
            return []
        clauses = " OR ".join(["text LIKE ?"] * len(clean))
        params: list = [str(group_id), cutoff, *[f"%{t}%" for t in clean]]
        rows = self._db.execute(
            f"SELECT id, group_id, uid, nick, text, ts FROM messages "
            f"WHERE group_id=? AND ts>=? AND ({clauses}) "
            f"ORDER BY id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def groups(self) -> list[str]:
        """出现过消息的群（给启动预热用，不限于 stats 的前 10）。"""
        if self._db is None:
            return []
        rows = self._db.execute(
            "SELECT group_id, MAX(id) AS last FROM messages GROUP BY group_id ORDER BY last DESC"
        ).fetchall()
        return [str(r["group_id"]) for r in rows]

    def recent(
        self, group_id: str, limit: int = 20, days: float = 1
    ) -> list[dict]:
        """最近几条群消息（按时间正序），给启动后预热上下文用。"""
        if self._db is None or not group_id or limit <= 0:
            return []
        cutoff = int(time.time() - max(0.0, float(days)) * 86400)
        rows = self._db.execute(
            "SELECT id, group_id, uid, nick, text, ts FROM messages "
            "WHERE group_id=? AND ts>=? ORDER BY id DESC LIMIT ?",
            (str(group_id), cutoff, int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def after(
        self, group_id: str, ts: int, *, seconds: float = 90, limit: int = 3
    ) -> list[dict]:
        """某条消息之后几十秒内，群里接着说了什么。

        用来找「这句之后别人怎么接的」——接法样例比单句历史有用。
        """
        if self._db is None or not group_id or limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT id, group_id, uid, nick, text, ts, reply_to FROM messages "
            "WHERE group_id=? AND ts>? AND ts<=? ORDER BY ts LIMIT ?",
            (str(group_id), int(ts), int(ts + max(1.0, float(seconds))), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        if self._db is None:
            return {"enabled": False}
        total = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        rows = self._db.execute(
            "SELECT group_id, COUNT(*) n FROM messages GROUP BY group_id ORDER BY n DESC LIMIT 10"
        ).fetchall()
        people = self._db.execute("SELECT COUNT(DISTINCT uid) FROM messages").fetchone()[0]
        return {
            "enabled": True,
            "total": total,
            "people": people,
            "groups": {r["group_id"]: r["n"] for r in rows},
            "size_kb": self.path.stat().st_size // 1024 if self.path.exists() else 0,
        }

    def purge(self, group_id: str | None = None) -> int:
        """清空群语料（指定群或全部）。"""
        if self._db is None:
            return 0
        if group_id:
            n = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE group_id=?", (str(group_id),)
            ).fetchone()[0]
            self._db.execute("DELETE FROM messages WHERE group_id=?", (str(group_id),))
        else:
            n = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self._db.execute("DELETE FROM messages")
        self._db.commit()
        return int(n)

    def dump_uid(self, uid: str, out_dir: Path) -> tuple[Path | None, int]:
        """把某个号的发言备份成 JSON 再删，标错人时还有机会手工找回。

        返回 (备份文件, 条数)；没有消息返回 (None, 0)。写不进去会抛 OSError，
        调用方据此**放弃删除**——宁可留着脏数据，也不把人的话弄丢。
        """
        if self._db is None or not str(uid or "").strip():
            return None, 0
        rows = self._db.execute(
            "SELECT group_id, uid, nick, text, ts, msg_id FROM messages WHERE uid=?",
            (str(uid).strip(),),
        ).fetchall()
        if not rows:
            return None, 0
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{str(uid).strip()}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        payload = {
            "uid": str(uid).strip(),
            "exported_at": int(time.time()),
            "count": len(rows),
            "messages": [dict(r) for r in rows],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path, len(rows)

    def purge_uid(self, uid: str) -> int:
        """删掉某个号的全部历史发言：标记成机器人/AI 时要连旧语料一起清。

        标记往往晚于它第一次发言，光靠写入端的 `not_a_person` 拦不住那批
        已经落库的话，所以在这里补一刀。返回删掉的条数（幂等：没有就是 0）。
        """
        if self._db is None or not str(uid or "").strip():
            return 0
        cur = self._db.execute("DELETE FROM messages WHERE uid=?", (str(uid).strip(),))
        self._db.commit()
        return int(cur.rowcount or 0)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
