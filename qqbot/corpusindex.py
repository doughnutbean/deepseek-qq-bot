"""群语料理解层：把「群里以前说过什么」做成可检索的本地索引。

和另外两套的分工：

- `stylelearn.py`：抽「A 说一句、B 怎么接」，学的是**口音**；本项目不接它。
- `groupstyle.py`：统计句长/标点/节奏，学的是**说法**；默认「观察为主」。
- 本模块：只做**理解**——给定当前这句话，从同一个群的历史里捞相关的旧消息，
  当背景递给模型。不改 persona、不学群友的句式。

索引存在 `data/corpus_index.db`，和正在写的 `group_log.db` 分开：

- `build` 先写临时库，成功后原子替换；机器人读的是只读连接，不会被写坏。
- 建索引时只收真人文字，机器人/AI/她自己、命令、纯占位、原样重复都剔掉。
- 没有 embedding 模型也能用：退回关键词重合；有模型时关键词 + 余弦混合排序。
- 默认不启用（`ENABLE_CORPUS_RECALL=false`）；先在命令行 `search` 看召回，
  再决定要不要开。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .embed import Embedder
from .forget import body_of
from .stopwords import STOPWORDS

_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|动画表情|语音|视频|文件|卡片消息|合并转发)[^\]]*\]"
)
_CMD_RE = re.compile(r"^[/#]")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_AT_RE = re.compile(r"@(\d{5,12})")
_DEF_VERBS = ("就是", "意思是", "叫做", "是", "指", "==", "=")

DEFAULT_DAYS = 30
DEFAULT_LIMIT = 3
DEFAULT_MIN_COS = 0.36
DEFAULT_MIN_KW = 0.50
DEFAULT_SEMANTIC_ONLY = 0.68
# 和 memory_note 去重的时间窗：同一句话前后 10 分钟算同一条。
# 隔很久又说了同样一句，仍然可以进语料背景。
DEDUP_SECONDS = 600

# 这些是「句子零件」，不是群里的说法：统计上很高频，但做成词表只会污染
_COMMON_TERMS = {
    "还有", "不能", "看看", "不会", "直接", "不要", "是不是", "两个", "好像", "这种",
    "确实", "群里", "这是", "几个", "真是", "一样", "只能", "不如", "我要", "没有",
    "可以", "什么", "怎么", "为什么", "因为", "所以", "但是", "而且", "然后", "现在",
    "时候", "知道", "觉得", "感觉", "东西", "事情", "问题", "地方", "一起", "一下",
    "一点", "已经", "开始", "最后", "真的", "其实", "应该", "可能", "需要", "喜欢",
    "不是", "就是", "这个", "那个", "我们", "你们", "他们", "自己", "一个", "这么",
    "那么", "还是", "或者", "如果", "虽然", "不过", "只是", "这样", "那样", "怎样",
    "看到", "听到", "说说", "告诉", "觉得", "认为", "出来", "进去", "回来", "过去",
    "今天", "明天", "昨天", "现在", "最近", "以后", "以前", "时候", "东西", "事情",
    "有点", "很多", "不少", "一些", "这些", "那些", "哪个", "哪里", "这里", "那里",
    "上面", "下面", "里面", "外面", "前面", "后面", "中间", "左右", "大概", "差不多",
    "非常", "特别", "十分", "比较", "更加", "最好", "一定", "必须", "肯定", "当然",
    "居然", "竟然", "果然", "终于", "刚刚", "马上", "立刻", "一直", "总是", "经常",
    "偶尔", "从来", "永远", "根本", "简直", "几乎", "完全", "全部", "所有", "每个",
    "大家", "别人", "人家", "咱们", "咱", "谁", "啥", "咋", "哪", "那", "这",
    "直播", "主播", "笑话", "评价", "意思", "限制", "小时", "分钟", "游戏", "队友",
    "数据", "版本", "更新", "视频", "图片", "表情", "文字", "语音", "消息", "聊天",
}

_BAD_DEFS = {
    "谁", "什么", "多少", "这样", "那样", "这样的", "那样的", "哪", "为什么",
    "怎么", "什么鬼", "啥", "哪样", "哪个", "这里", "那里", "自己", "别人",
}

# 查询里的“句子零件”：只剩这些词就不召回，否则「今天天气怎么样」会把
# 所有带「怎么样」的旧消息都捞出来。它们仍然可以出现在索引/词表里，
# 只是不作为检索依据。
_QUERY_NOISE = {
    "怎么样", "怎么", "什么", "为什么", "哪个", "哪里", "多少", "是不是",
    "可以", "帮我", "一下", "一段", "还有", "今天", "明天", "昨天", "现在",
    "最近", "觉得", "知道", "告诉", "这个", "那个", "我们", "你们", "他们",
    "自己", "一个", "这么", "那么", "意思", "东西", "事情", "问题", "时候",
}


def clean_text(text: str) -> str:
    body = _PLACEHOLDER_RE.sub("", text or "")
    body = _URL_RE.sub("", body)
    return " ".join(body.split()).strip()


def _keywords(text: str) -> list[str]:
    try:
        from .memory import keywords as memory_keywords

        return list(memory_keywords(text))
    except Exception:  # noqa: BLE001 - 分词失败时退回空，不影响检索
        return []


def _norm(text: str) -> str:
    return "".join(clean_text(text).split()).lower()


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    return con


def _same_model(a: str, b: str) -> bool:
    """Windows 路径大小写不敏感；只比目录名，避免相对/绝对路径误判。"""
    return bool(a) and bool(b) and Path(str(a)).name.lower() == Path(str(b)).name.lower()


def _meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return str(row["value"]) if row else default


class CorpusIndex:
    """只读的群语料索引；每次查询开一个只读连接，方便 build 原子替换。"""

    def __init__(
        self,
        path: Path,
        embedder: Embedder | None = None,
        *,
        model: str = "",
        enabled: bool = True,
        limit: int = DEFAULT_LIMIT,
        days: float = DEFAULT_DAYS,
        max_chars: int = 260,
        min_cos: float = DEFAULT_MIN_COS,
        min_kw: float = DEFAULT_MIN_KW,
        semantic_only_min: float = DEFAULT_SEMANTIC_ONLY,
        exclude_recent: int = 600,
    ) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.expected_model = str(model or "")
        self._last_error = ""
        # 索引是用 A 模型建的，查询却用 B 模型：向量空间对不上，宁可按关键词降级
        actual = getattr(embedder, "model_dir", None) if embedder is not None else None
        if (
            embedder is not None
            and self.expected_model
            and actual is not None
            and not _same_model(str(actual), self.expected_model)
        ):
            self.embedder = None
            self._last_error = (
                f"embedding 不匹配：索引 {Path(self.expected_model).name} / "
                f"当前 {Path(str(actual)).name}；已降级为关键词检索"
            )
        self.enabled = bool(enabled)
        self.limit = max(1, int(limit))
        self.days = max(0.1, float(days))
        self.max_chars = max(80, int(max_chars))
        self.min_cos = float(min_cos)
        self.min_kw = float(min_kw)
        self.semantic_only_min = float(semantic_only_min)
        self.exclude_recent = max(0, int(exclude_recent))
        # 索引里记的是建库模型；和当前查询模型对不上就只用关键词
        if self.exists and self.expected_model and self.embedder is not None:
            try:
                con = _open_ro(self.path)
                try:
                    built_model = _meta(con, "model")
                finally:
                    con.close()
                if built_model and not _same_model(built_model, self.expected_model):
                    self.embedder = None
                    self._last_error = (
                        f"索引模型 {Path(built_model).name} / 当前 "
                        f"{Path(self.expected_model).name} 不一致；已降级为关键词检索"
                    )
            except sqlite3.Error:
                pass

    # ---------- 状态 ----------

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def stats(self) -> dict[str, Any]:
        if not self.exists:
            return {"enabled": self.enabled, "exists": False}
        try:
            con = _open_ro(self.path)
            try:
                total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                groups = con.execute(
                    "SELECT group_id, COUNT(*) n FROM messages GROUP BY group_id ORDER BY n DESC"
                ).fetchall()
                terms = con.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
                with_vec = con.execute(
                    "SELECT COUNT(*) FROM messages WHERE vec IS NOT NULL"
                ).fetchone()[0]
                built = _meta(con, "built_at")
                synced = _meta(con, "synced_at")
                terms_stale = _meta(con, "terms_stale")
                terms_at = _meta(con, "terms_at")
                model = _meta(con, "model")
            finally:
                con.close()
            return {
                "enabled": self.enabled,
                "exists": True,
                "total": int(total),
                "groups": {str(r["group_id"]): int(r["n"]) for r in groups},
                "terms": int(terms),
                "with_vec": int(with_vec),
                "built_at": int(built) if built.isdigit() else 0,
                "synced_at": int(synced) if synced.isdigit() else 0,
                "terms_stale": terms_stale == "1",
                "terms_at": int(terms_at) if terms_at.isdigit() else 0,
                "model": model,
                "expected_model": self.expected_model,
                "degraded": self.embedder is None and bool(self.expected_model),
                "last_error": self._last_error,
                "size_kb": self.path.stat().st_size // 1024,
            }
        except sqlite3.Error as exc:
            return {"enabled": self.enabled, "exists": True, "error": str(exc)}

    # ---------- 增量同步 ----------

    def sync(
        self,
        source_db: Path,
        *,
        robots: set[str] | None = None,
        ais: set[str] | None = None,
        bot_qq: str = "",
        group_ids: set[str] | None = None,
        batch_size: int = 64,
    ) -> dict[str, Any]:
        """把 `group_log.db` 里比索引新的消息补进来，不重建全量。

        - 以索引里的 `MAX(id)` 为水位，只读源库更大的 id；
        - 统一过滤机器人/命令/占位/太短/超长/同群重复；
        - 有可用 embedding 时补向量，没有就只补关键词；
        - 词表 `terms` 不在增量里重算（全量 build 时刷新），新消息仍可检索。
        """
        started = time.time()
        if not self.enabled or not self.exists:
            return {"added": 0, "reason": "索引不存在或未启用"}
        source_db = Path(source_db)
        if not source_db.exists():
            return {"added": 0, "reason": "源库不存在"}

        blocked = set(robots or set()) | set(ais or set())
        if bot_qq:
            blocked.add(str(bot_qq))
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=5000")
            row = con.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
            last_id = int(row[0] if row else 0)
            seen: set[tuple[str, str]] = set()
            for r in con.execute("SELECT group_id, text FROM messages"):
                seen.add((str(r["group_id"]), _norm(str(r["text"]))))

            src = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
            try:
                rows = src.execute(
                    "SELECT id, group_id, uid, nick, text, ts FROM messages "
                    "WHERE id > ? ORDER BY id",
                    (last_id,),
                ).fetchall()
            finally:
                src.close()

            pending: list[tuple] = []
            texts: list[str] = []
            max_seen = last_id
            skipped = 0
            for r in rows:
                max_seen = max(max_seen, int(r["id"]))
                gid = str(r["group_id"] or "")
                uid = str(r["uid"] or "")
                if not gid or not uid or uid in blocked:
                    skipped += 1
                    continue
                if group_ids and gid not in group_ids:
                    skipped += 1
                    continue
                text = clean_text(str(r["text"] or ""))
                if not text or _CMD_RE.match(text) or len(text) < 2 or len(text) > 200:
                    skipped += 1
                    continue
                key = (gid, _norm(text))
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                pending.append(
                    (
                        int(r["id"]),
                        gid,
                        uid,
                        str(r["nick"] or "")[:32],
                        text,
                        int(r["ts"]),
                        " ".join(_keywords(text)),
                    )
                )
                texts.append(text)

            added = 0
            before = int(con.total_changes)
            for i in range(0, len(pending), max(8, batch_size)):
                chunk = pending[i : i + max(8, batch_size)]
                chunk_texts = texts[i : i + max(8, batch_size)]
                vecs: list[bytes | None] = [None] * len(chunk)
                if self.embedder is not None and chunk_texts:
                    blobs = self.embedder.encode(chunk_texts)
                    for j, blob in enumerate(blobs[: len(vecs)]):
                        vecs[j] = blob
                con.executemany(
                    "INSERT OR IGNORE INTO messages(id, group_id, uid, nick, text, ts, kw, vec) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (row[0], row[1], row[2], row[3], row[4], row[5], row[6], vecs[j])
                        for j, row in enumerate(chunk)
                    ],
                )
                con.commit()
            added = int(con.total_changes) - before
            count = int(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            with_vec = int(
                con.execute("SELECT COUNT(*) FROM messages WHERE vec IS NOT NULL").fetchone()[0]
            )
            meta = {
                "last_id": str(max_seen),
                "synced_at": str(int(time.time())),
                "count": str(count),
                "with_vec": str(with_vec),
            }
            # 只有真的加了新消息才把词表标脏；空转的 15 分钟同步不该让
            # 下次启动白刷一遍词表。
            if added:
                meta["terms_stale"] = "1"
            con.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", meta.items()
            )
            con.commit()
            return {
                "added": added,
                "skipped": skipped,
                "last_id": max_seen,
                "count": count,
                "with_vec": with_vec,
                "elapsed": round(time.time() - started, 1),
            }
        except sqlite3.Error as exc:
            return {"added": 0, "reason": str(exc), "elapsed": round(time.time() - started, 1)}
        finally:
            con.close()

    def refresh_terms(
        self,
        *,
        min_term_df: int = 5,
        min_term_speakers: int = 3,
    ) -> dict[str, Any]:
        """只重算词表，不重新编码消息；启动时补完增量后跑这个。

        读索引里已有的 `messages.kw/text`，聚合 df/人数/样例/定义，
        然后整体替换 `terms`。不加载 embedding，通常比全量 build 快一个量级。
        """
        started = time.time()
        if not self.exists:
            return {"terms": 0, "reason": "索引不存在", "elapsed": 0.0}
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=5000")
            rows = [
                dict(r)
                for r in con.execute(
                    "SELECT group_id, uid, text, ts, kw FROM messages"
                )
            ]
            term_rows = _term_rows_from_rows(
                rows,
                min_term_df=min_term_df,
                min_term_speakers=min_term_speakers,
            )
            con.execute("DELETE FROM terms")
            con.executemany(
                "INSERT OR REPLACE INTO terms(group_id, term, df, speakers, definition, examples, first_ts, last_ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                term_rows,
            )
            meta = {
                "terms": str(len(term_rows)),
                "terms_stale": "0",
                "terms_at": str(int(time.time())),
            }
            con.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", meta.items()
            )
            con.commit()
            return {
                "terms": len(term_rows),
                "elapsed": round(time.time() - started, 1),
            }
        except sqlite3.Error as exc:
            return {
                "terms": 0,
                "reason": str(exc),
                "elapsed": round(time.time() - started, 1),
            }
        finally:
            con.close()

    # ---------- 检索 ----------

    def _load_rows(self, group_id: str, days: float) -> list[dict[str, Any]]:
        cutoff = int(time.time() - days * 86400)
        con = _open_ro(self.path)
        try:
            rows = con.execute(
                "SELECT id, group_id, uid, nick, text, ts, kw, vec FROM ("
                "  SELECT id, group_id, uid, nick, text, ts, kw, vec FROM messages "
                "  WHERE group_id=? AND ts>=? ORDER BY ts DESC LIMIT 20000"
                ") AS recent ORDER BY ts DESC",
                (str(group_id), cutoff),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    def _rank(
        self,
        rows: list[dict[str, Any]],
        query: str,
        *,
        limit: int,
        min_cos: float,
        min_kw: float,
        exclude_text: str = "",
        exclude_items: list[dict[str, Any]] | None = None,
    ) -> list[tuple[float, dict[str, Any]]]:
        qkw = {w for w in _keywords(query) if w not in _QUERY_NOISE}
        if not qkw:
            return []
        # memory_note 已经用过的同一句话，不再从群语料里重复拿一遍。
        # 只按「正文 + 10 分钟内」去重：同一句话隔很久又说，是新的语境。
        exclude: list[tuple[str, int]] = []
        for item in exclude_items or []:
            body = body_of(str(item.get("content") or item.get("text") or ""))
            ts = int(item.get("ts") or 0)
            if body and ts:
                exclude.append((_norm(body), ts))
        qvec = None
        if self.embedder is not None:
            # encode 会懒加载模型；第一次调用可能几百毫秒。调用方在子线程里跑。
            blobs = self.embedder.encode([query])
            if blobs:
                qvec = Embedder.to_vector(blobs[0])

        # 关键词命中的先算分；没有关键词也不是完全没戏：短 query 可能全靠语义。
        scored: list[tuple[float, dict[str, Any]]] = []
        now = time.time()
        exclude_norm = _norm(exclude_text) if exclude_text else ""
        for row in rows:
            text = str(row.get("text") or "")
            row_ts = int(row.get("ts") or 0)
            row_norm = _norm(text)
            if exclude_norm and row_norm == exclude_norm:
                continue
            if exclude and any(
                row_norm == norm and abs(row_ts - ts) <= DEDUP_SECONDS
                for norm, ts in exclude
            ):
                continue
            if self.exclude_recent and now - row_ts < self.exclude_recent:
                continue
            rowkw = set(str(row.get("kw") or "").split())
            overlap = len(qkw & rowkw)
            kw_score = overlap / max(1, len(qkw)) if qkw else 0.0
            cos = 0.0
            if qvec is not None and row.get("vec"):
                vec = Embedder.to_vector(row["vec"])
                if vec is not None and vec.shape == qvec.shape:
                    cos = float(qvec @ vec)
            # 没有关键词证据时，语义门槛要高一些：bge 对短句的余弦本来就偏高，
            # 0.36 会把「今天天气」和「昨天状态差」硬凑成一对。
            if overlap <= 0:
                if cos < self.semantic_only_min:
                    continue
            elif cos < min_cos and kw_score < min_kw:
                continue
            # 关键词是「字面确定」的证据，权重给高一点；语义补同义/换说法
            score = 0.55 * max(0.0, cos) + 0.45 * kw_score
            if overlap >= 2:
                score += 0.05
            # 太久远的略降，但不要压过语义
            age_days = max(0.0, now - row_ts) / 86400
            score *= 1.0 + 0.08 * math.exp(-age_days / 7)
            # 问「X 是谁」时，「X 是/就是/指…」这种解释句比另一堆问句更有用
            if overlap >= 1 and not text.rstrip().endswith(("？", "?", "吗", "呢")):
                if re.search(r"(?:就是|叫做|意思是|是|指)", text):
                    score += 0.04
            scored.append((round(score, 4), row))

        scored.sort(key=lambda x: (-x[0], -int(x[1].get("ts") or 0)))
        # 同一句话只留一条，避免复读刷屏
        out: list[tuple[float, dict[str, Any]]] = []
        seen: set[str] = set()
        for score, row in scored:
            key = _norm(str(row.get("text") or ""))
            if key in seen:
                continue
            seen.add(key)
            out.append((score, row))
            if len(out) >= limit:
                break
        return out

    def search(
        self,
        group_id: str,
        query: str,
        *,
        days: float | None = None,
        limit: int | None = None,
        exclude_text: str = "",
        exclude_items: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """检索同群历史；返回带 score 的行，失败返回空。"""
        if not self.enabled or not self.exists or not str(group_id).strip() or not query:
            return []
        limit = max(1, int(limit or self.limit))
        days = self.days if days is None else max(0.1, float(days))
        try:
            rows = self._load_rows(str(group_id), days)
            if not rows:
                return []
            # 只拿「这个群的群聊回合」去重；私聊/别的群的同文同时间不算
            items: list[dict[str, Any]] = []
            for item in exclude_items or []:
                session = str(item.get("session") or "")
                if session.startswith("private:"):
                    continue
                if session.startswith("group:"):
                    if session.split(":", 1)[1] != str(group_id):
                        continue
                items.append(item)
            ranked = self._rank(
                rows,
                query,
                limit=limit,
                min_cos=self.min_cos,
                min_kw=self.min_kw,
                exclude_text=exclude_text,
                exclude_items=items,
            )
            out = []
            for score, row in ranked:
                item = dict(row)
                item.pop("vec", None)
                item["score"] = score
                out.append(item)
            return out
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._last_error = str(exc)
            return []

    # ---------- 给提示词 ----------

    def _term_note(self, group_id: str, text: str) -> str:
        if not self.exists or not text:
            return ""
        try:
            con = _open_ro(self.path)
            try:
                rows = con.execute(
                    "SELECT term, definition, speakers, df FROM terms "
                    "WHERE group_id=? AND definition<>'' ORDER BY LENGTH(term) DESC",
                    (str(group_id),),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return ""
        body = clean_text(text)
        for row in rows:
            term = str(row["term"] or "")
            if term and term in body:
                return (
                    f"（群里的说法：「{term}」= {str(row['definition'])[:40]}；"
                    f"{int(row['df'])} 次 / {int(row['speakers'])} 人这么用。"
                    "当背景，别向群友解释这个词。）"
                )
        return ""

    def brief(
        self,
        group_id: str,
        query: str,
        *,
        include_terms: bool = True,
        exclude_items: list[dict[str, Any]] | None = None,
    ) -> str:
        """把检索结果压成一段 ≤ max_chars 的动态提示；没命中返回空串。

        `exclude_items` 是这一轮 memory_note 已经用过的记忆条目：同一句话
        不再从群语料里重复拿一遍（按正文 + 10 分钟内）。
        """
        if not self.enabled or not self.exists or not query:
            return ""
        parts: list[str] = []
        if include_terms:
            term_note = self._term_note(str(group_id), query)
            if term_note:
                parts.append(term_note)
        rows = self.search(str(group_id), query, exclude_items=exclude_items)
        if rows:
            lines = ["（群里以前聊到过（不是这一轮，是更早的）："]
            for row in rows:
                when = time.strftime(
                    "%m-%d %H:%M", time.localtime(int(row.get("ts") or 0))
                )
                who = str(row.get("nick") or "群友")[:12]
                body = str(row.get("text") or "").replace("\n", " ")[:80]
                lines.append(f"- {when} {who}：{body}")
            lines.append("只当背景，别当成当前这个人刚说的；和这一轮无关就别硬提。）")
            parts.append("\n".join(lines))
        if not parts:
            return ""
        text = "\n".join(parts)
        if len(text) <= self.max_chars:
            return text
        # 截断时保住最后那句边界，别让模型只看到半截材料
        boundary = "只当背景，别当成当前这个人刚说的；和这一轮无关就别硬提。）"
        keep = max(40, self.max_chars - len(boundary) - 2)
        return text[:keep].rstrip() + "…\n" + boundary


# ---------- 建索引 ----------


def _term_rows_from_rows(
    rows: list[dict[str, Any]],
    *,
    min_term_df: int = 5,
    min_term_speakers: int = 3,
) -> list[tuple]:
    """从索引行（group_id/uid/text/ts/kw）重算群内词表。

    全量 build 和启动时 `refresh_terms` 共用这一份逻辑，保证两边一致。
    只做统计/定义抽取，不加载 embedding。
    """
    terms: dict[str, Counter[str]] = defaultdict(Counter)
    speakers: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    examples: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    times: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    texts_by_group: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        gid = str(row.get("group_id") or "")
        uid = str(row.get("uid") or "")
        text = str(row.get("text") or "")
        ts = int(row.get("ts") or 0)
        if not gid or not text:
            continue
        texts_by_group[gid].append(text)
        for term in set(str(row.get("kw") or "").split()):
            terms[gid][term] += 1
            if uid:
                speakers[gid][term].add(uid)
            if len(examples[gid][term]) < 2:
                examples[gid][term].append(text[:80])
            times[gid][term].append(ts)

    out: list[tuple] = []
    for gid, counter in terms.items():
        group_texts = texts_by_group[gid]
        for term, df in counter.items():
            if df < min_term_df or len(speakers[gid][term]) < min_term_speakers:
                continue
            if not (1 <= len(term) <= 8) or term.isdigit():
                continue
            if term in _COMMON_TERMS or term in STOPWORDS:
                continue
            # 只拿包含这个词的句子去抽定义，别每词扫全群
            containing = [t for t in group_texts if term in t]
            definition = _definition_of(term, containing)
            if len(term) == 1 and not definition:
                continue
            span = times[gid][term]
            out.append(
                (
                    gid,
                    term,
                    int(df),
                    len(speakers[gid][term]),
                    definition,
                    json.dumps(examples[gid][term], ensure_ascii=False),
                    min(span) if span else 0,
                    max(span) if span else 0,
                )
            )
    return out


def _definition_of(term: str, texts: list[str]) -> str:
    """从「X 是/就是/指…」里抽一句最常出现的解释；抽不到就留空。"""
    verbs = "|".join(re.escape(v) for v in _DEF_VERBS)
    pat = re.compile(
        re.escape(term)
        + r"\s*(?:" + verbs + r")\s*"
        + r"([^，。！？,.!?\n]{1,28})"
    )
    counts: Counter[str] = Counter()
    for text in texts:
        body_text = clean_text(text)
        for m in pat.finditer(body_text):
            body = m.group(1).strip(" \t，。！？,.!?")
            # 问句尾巴不是定义：「克里斯是区吗」要留「区」，不能留「区吗」
            body = re.sub(r"[吗呢吧啊嘛]+$", "", body).strip()
            if not body or body.startswith(("不", "没", "什么", "怎么", "哪")):
                continue
            if body in {"我", "你", "他", "她", "它", "的", "了", "是"} | _BAD_DEFS:
                continue
            if "是" in body or "吗" in body or "？" in body:
                continue
            if body == term:
                continue
            counts[body] += 1
    return counts.most_common(1)[0][0] if counts else ""


def build_index(
    source_db: Path,
    out_db: Path,
    *,
    model_dir: Path | None = None,
    robots: set[str] | None = None,
    ais: set[str] | None = None,
    bot_qq: str = "",
    days: float = 0,
    batch_size: int = 64,
    min_term_df: int = 5,
    min_term_speakers: int = 3,
    progress: Any = None,
) -> dict[str, Any]:
    """从 group_log 建 `data/corpus_index.db`。

    只读源库，写临时文件，成功后 `os.replace`。返回统计。
    """
    source_db = Path(source_db)
    out_db = Path(out_db)
    if not source_db.exists():
        raise FileNotFoundError(source_db)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_db.with_name(f"{out_db.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()

    blocked = set(robots or set()) | set(ais or set())
    if bot_qq:
        blocked.add(str(bot_qq))

    src = _open_ro(source_db)
    dst = sqlite3.connect(tmp)
    dst.row_factory = sqlite3.Row
    dst.executescript(
        """
        CREATE TABLE messages (
            id       INTEGER PRIMARY KEY,
            group_id TEXT NOT NULL,
            uid      TEXT NOT NULL,
            nick     TEXT,
            text     TEXT NOT NULL,
            ts       INTEGER NOT NULL,
            kw       TEXT NOT NULL DEFAULT '',
            vec      BLOB
        );
        CREATE INDEX idx_ci_group_ts ON messages(group_id, ts);
        CREATE TABLE terms (
            group_id   TEXT NOT NULL,
            term       TEXT NOT NULL,
            df         INTEGER NOT NULL,
            speakers   INTEGER NOT NULL,
            definition TEXT DEFAULT '',
            examples   TEXT DEFAULT '',
            first_ts   INTEGER,
            last_ts    INTEGER,
            PRIMARY KEY (group_id, term)
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )

    cutoff = int(time.time() - days * 86400) if days and days > 0 else 0
    rows = [
        dict(r)
        for r in src.execute(
            "SELECT id, group_id, uid, nick, text, ts FROM messages "
            "WHERE ts>=? ORDER BY ts, id",
            (cutoff,),
        )
    ]
    src.close()

    embedder = Embedder(model_dir, enabled=bool(model_dir)) if model_dir else None
    use_vec = bool(embedder and getattr(embedder, "available", False))
    inserted = 0
    seen: set[tuple[str, str]] = set()

    pending: list[tuple] = []
    pending_texts: list[str] = []

    def flush() -> None:
        nonlocal inserted
        if not pending:
            return
        vecs: list[bytes | None] = [None] * len(pending)
        if use_vec and pending_texts:
            blobs = embedder.encode(pending_texts)  # type: ignore[union-attr]
            for i, blob in enumerate(blobs):
                if i < len(vecs):
                    vecs[i] = blob
        dst.executemany(
            "INSERT OR REPLACE INTO messages(id, group_id, uid, nick, text, ts, kw, vec) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (row[0], row[1], row[2], row[3], row[4], row[5], row[6], vecs[i])
                for i, row in enumerate(pending)
            ],
        )
        inserted += len(pending)
        pending.clear()
        pending_texts.clear()

    for row in rows:
        uid = str(row.get("uid") or "")
        gid = str(row.get("group_id") or "")
        text = clean_text(str(row.get("text") or ""))
        if not gid or not uid or uid in blocked:
            continue
        if not text or _CMD_RE.match(text) or len(text) < 2 or len(text) > 200:
            continue
        key = (gid, _norm(text))
        if key in seen:
            continue
        seen.add(key)
        kws = _keywords(text)
        kw = " ".join(kws)
        pending.append(
            (int(row["id"]), gid, uid, str(row.get("nick") or "")[:32], text, int(row["ts"]), kw)
        )
        pending_texts.append(text)
        if len(pending) >= max(8, batch_size):
            flush()
            if progress:
                progress(inserted)
    flush()

    term_rows = _term_rows_from_rows(
        [dict(r) for r in dst.execute("SELECT group_id, uid, text, ts, kw FROM messages")],
        min_term_df=min_term_df,
        min_term_speakers=min_term_speakers,
    )
    dst.executemany(
        "INSERT OR REPLACE INTO terms(group_id, term, df, speakers, definition, examples, first_ts, last_ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        term_rows,
    )
    meta = {
        "built_at": str(int(time.time())),
        "source": str(source_db),
        "model": str(model_dir or ""),
        "dim": "512",
        "count": str(inserted),
        "with_vec": str(
            dst.execute("SELECT COUNT(*) FROM messages WHERE vec IS NOT NULL").fetchone()[0]
        ),
        "terms": str(len(term_rows)),
        "terms_stale": "0",
        "terms_at": str(int(time.time())),
        "version": "1",
    }
    dst.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", meta.items())
    dst.commit()
    dst.close()
    try:
        os.replace(tmp, out_db)
    except OSError as exc:
        raise OSError(
            f"替换索引失败（机器人可能正在读 {out_db}）：{exc}；"
            "先停机器人，或换一个 --out"
        ) from exc
    return {
        "inserted": inserted,
        "terms": len(term_rows),
        "with_vec": int(meta["with_vec"]),
        "path": str(out_db),
        "model": str(model_dir or ""),
    }
