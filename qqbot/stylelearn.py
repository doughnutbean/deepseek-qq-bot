"""从群聊里学「怎么说话」——只学口音，不动人设。

这里只负责**离线抽取**：从真实相邻对话里挖「某人说了 A，另一个人接了 B」的对子。
注入是另一回事，且必须先经过主人过目（`tools/style_learn.py` 里可以预览）。

几条硬规矩（照 MaiBot / 接话对蒸馏的做法）：

1. **绝不用机器人自己的发言**（那是她现在的口音，学了就自我强化）
2. A 和 B 必须是**不同的人**——同一个人连发两行不算接话
3. 去掉昵称前缀、引用尾巴、图片占位、链接
4. 太短太长的都不要（「嗯」「哈哈哈哈」当不了风格样本）
5. 命中黑名单（老铁、家人们、脏话…）的**标记为不可注入**，但仍然统计

学到的只是词汇和句式，**永远不写进 persona.md**。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# 明显不像人设的词：可以学进来，但注入前必须挡掉
BLOCKED_WORDS = (
    "老铁", "家人们", "宝子", "集美", "绝绝子", "栓Q", "栓q",
    "草泥马", "尼玛", "傻逼", "煞笔", "狗东西", "滚",
)

# 她自己说话时才会用的词：带这些的素材说明混进了 bot 的发言
SELF_MARKERS = ("本鱼", "大肥鱼", "鲸鱼娘")

_MIN_LEN = 2
_MAX_LEN = 40
_MAX_PREV = 80

_TAG_RE = re.compile(r"^【[^】]*】")
_PLACEHOLDER_RE = re.compile(r"\[(?:图片|表情|语音|视频|文件|动画表情)[^\]]*\]")
_QUOTE_RE = re.compile(r"（引用了前一条：[^）]*）")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_CMD_RE = re.compile(r"^[/#]")


@dataclass
class Pair:
    session: str
    prev: str
    reply: str
    ts: int
    score: float
    blocked: bool = False
    reason: str = ""


def clean(text: str) -> str:
    """去掉昵称前缀、引用尾巴、占位符、链接。"""
    body = (text or "").strip()
    body = _TAG_RE.sub("", body)          # 【主人】
    body = _QUOTE_RE.sub("", body)
    body = _PLACEHOLDER_RE.sub("", body)
    body = _URL_RE.sub("", body)
    if "：" in body[:24]:                  # 昵称：正文
        head, _, rest = body.partition("：")
        if len(head) <= 16 and rest.strip():
            body = rest
    return body.strip()


def speaker_of(row: dict, raw: str) -> str:
    """谁说的。优先用 owner_id，没有就从前缀里的昵称取。"""
    uid = row.get("owner_id")
    if uid:
        return str(uid)
    match = re.match(r"^【[^】]*】([^：]{1,16})：", (raw or "").strip())
    return match.group(1) if match else ""


def score_pair(prev: str, reply: str, gap_sec: float) -> tuple[float, str]:
    """给一对打分。返回 (分数, 不收的理由)。0 分就是不收。"""
    if len(prev) < _MIN_LEN or len(prev) > _MAX_PREV:
        return 0.0, "上一句长度不合适"
    if len(reply) < _MIN_LEN or len(reply) > _MAX_LEN:
        return 0.0, "接话长度不合适"
    if reply == prev:
        return 0.0, "复读"
    if _CMD_RE.search(prev) or _CMD_RE.search(reply):
        return 0.0, "像是命令"
    if any(mark in reply for mark in SELF_MARKERS):
        return 0.0, "混进了她自己的话"

    score = 1.0
    if gap_sec <= 30:
        score += 0.5           # 秒回，基本是接话
    elif gap_sec <= 120:
        score += 0.2
    # 上一句像在问，接话价值高
    if prev.endswith(("？", "?", "吗", "呢", "吧", "啊")):
        score += 0.4
    # 接话短而完整，最像聊天
    if 3 <= len(reply) <= 20:
        score += 0.3
    return round(score, 2), ""


def extract_pairs(rows: list[dict]) -> tuple[list[Pair], dict[str, int]]:
    """从按 id 排好序的 turn 列表里抽接话对。返回 (对子, 丢弃原因统计)。"""
    pairs: list[Pair] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    by_session: dict[str, list[dict]] = {}
    for row in rows:
        by_session.setdefault(str(row.get("session")), []).append(row)

    for session, items in by_session.items():
        if not session.startswith("group:"):
            continue  # 私聊不学（那是主人和她，不是"群友怎么说话"）
        for i, row in enumerate(items):
            if row.get("role") != "user":
                drop("不是群友发言")
                continue
            nxt = None
            for cand in items[i + 1 :]:
                if cand.get("role") == "user":
                    nxt = cand
                    break
                if cand.get("role") == "assistant":
                    # 中间隔着她自己的话，这对不算干净
                    break
            if nxt is None:
                drop("后面没人接")
                continue

            prev_raw, reply_raw = row.get("content", ""), nxt.get("content", "")
            who_a = speaker_of(row, prev_raw)
            who_b = speaker_of(nxt, reply_raw)
            if not who_a or not who_b:
                drop("分不清是谁说的")
                continue
            if who_a == who_b:
                drop("同一个人连发")
                continue

            prev, reply = clean(prev_raw), clean(reply_raw)
            gap = abs(int(nxt.get("ts") or 0) - int(row.get("ts") or 0))
            score, why = score_pair(prev, reply, gap)
            if score <= 0:
                drop(why or "打分不够")
                continue

            blocked = any(word in prev or word in reply for word in BLOCKED_WORDS)
            pairs.append(
                Pair(
                    session=session,
                    prev=prev,
                    reply=reply,
                    ts=int(nxt.get("ts") or 0),
                    score=score,
                    blocked=blocked,
                    reason="命中黑名单" if blocked else "",
                )
            )
    return pairs, dropped


def load_group_rows(messages: list[dict]) -> list[dict]:
    """把群消息流水转成接话对抽取要的形状（全部是普通发言，没有 bot）。"""
    out = []
    for m in messages:
        out.append(
            {
                "id": m.get("id"),
                "session": f"group:{m.get('group_id')}",
                "role": "user",
                "owner_id": m.get("uid"),
                "content": m.get("text") or "",
                "ts": int(m.get("ts") or 0),
            }
        )
    return out


def load_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, session, role, owner_id, content, ts FROM turns ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


class PairStore:
    """接话对存独立库，不污染 memory.db。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS pairs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL,
                prev    TEXT NOT NULL,
                reply   TEXT NOT NULL,
                ts      INTEGER NOT NULL,
                score   REAL NOT NULL,
                blocked INTEGER DEFAULT 0
            )
            """
        )
        self.db.commit()

    def replace_all(self, pairs: list[Pair]) -> int:
        self.db.execute("DELETE FROM pairs")
        self.db.executemany(
            "INSERT INTO pairs(session, prev, reply, ts, score, blocked) VALUES (?,?,?,?,?,?)",
            [(p.session, p.prev, p.reply, p.ts, p.score, int(p.blocked)) for p in pairs],
        )
        self.db.commit()
        return len(pairs)

    def clear(self) -> int:
        n = self.db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        self.db.execute("DELETE FROM pairs")
        self.db.commit()
        return int(n)

    def stats(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        blocked = self.db.execute("SELECT COUNT(*) FROM pairs WHERE blocked=1").fetchone()[0]
        return {"total": total, "blocked": blocked, "usable": total - blocked}

    def close(self) -> None:
        self.db.close()


def now_str(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))
