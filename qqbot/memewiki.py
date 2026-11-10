"""通用梗百科：只读本地词库，回答「X 是什么梗」，也帮她按用法接。

和 `memes.py` 的分工：

- `memes.py`：**这个群怎么用**——复读、固定接法、谐音代称，按群隔离，会自己长；
- 本模块：**网上这个梗是什么意思、通常怎么用**——只读，不学习、不改群梗表。

两种用法：

1. **明确问梗**（`X 是什么梗 / 什么意思 / 出自哪`）：给一句意思 + 用法，
   并明确要求「要接就按这个用，别背定义、别主动科普」；
2. **平时提到梗**：给一句意思 + 通常怎么用/例子，不展开出处/典故，让她直接接一句；
   同一个群同一个词默认 10 分钟内只提示一次，避免每句都塞小抄。

带骂人/黄腔/刷屏性质的词条另走 `memetone`：只让她「懂」，不教她「用」，
不复述原句、不给粗口模板；场景里写着「知道意思就行 / 不用接」的也按这条。

群梗/热梗已经命中时不会再用它盖过去。词库格式（上游 `dsh-memewiki` /
`astrbot_plugin_memewiki`，MIT）：`memes(title, slug, tags, usage, origin, other, summary)`。
词条内容来自 cnmeme.wiki；`tools/meme_wiki.py download` 可以更新。
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import memetone, memevariant

MAX_TERM = 16
DEFAULT_MAX_CHARS = 220
DEFAULT_COOLDOWN_MINUTES = 10

# 「X 是什么梗 / 什么意思 / 出自哪」：只认这种明确问法。
# 第一条故意把 X 前面的整段也吞进来（最多 40 字），再由 clean_term 去掉
# 「你知道 / 请问 / 听说」这类前缀；去不干净也没关系，note() 会从长到短
# 试 X 的后缀，最后只认词库里完全同名的条目，不会硬猜。
_PATTERNS = (
    re.compile(
        r"(?P<term>[^\s，。！？,.!?]{1,40}?)\s*"
        r"(?:是什么梗|是啥梗|什么梗|啥梗|是什么意思|是啥意思|什么意思|啥意思|"
        r"出自哪里|出自哪|的出处(?:是什么)?|的梗是什么|的梗是啥|"
        r"这个梗是什么|那个梗是什么|这个梗是啥|那个梗是啥|"
        r"梗是什么意思|梗是啥意思)"
    ),
    re.compile(r"(?:什么是|啥是|什么叫)\s*(?P<term>[^\s，。！？,.!?]{1,30})"),
)

_PREFIXES = (
    "你们知不知道",
    "你知不知道",
    "你们知道",
    "你知道",
    "知不知道",
    "请问一下",
    "想问一下",
    "问一下",
    "请问",
    "大家说的",
    "他们说的",
    "你们说的",
    "你说的",
    "他说的",
    "有人说",
    "有人问",
    "所谓",
    "听说",
    "最近",
    "网上",
    "群里",
    "话说",
    "所以",
)

_TRAIL_FILLERS = (
    "这个说法",
    "那个说法",
    "这种说法",
    "那种说法",
    "这个梗",
    "那个梗",
    "这种梗",
    "那种梗",
    "这句话",
    "那句话",
    "这梗",
    "那梗",
    "的梗",
)

_STOP_TERMS = {
    "这", "那", "这个", "那个", "这种", "那种", "这些", "那些",
    "你", "我", "他", "她", "它", "你们", "我们", "他们", "她们",
    "什么", "啥", "谁", "哪", "哪个", "哪里", "怎么", "为什么",
    "这个梗", "那个梗", "这句话", "那句话", "这个意思", "那个意思",
    "梗", "意思", "出处", "来源", "东西", "玩意", "问题", "事情",
    "本鱼", "主人", "群友", "楼上", "楼下", "刚才", "刚刚",
    # 单字泛词：从「这个梗」这类整段里切出来时会剩下它们，不能当词条
    "个", "些", "点", "样", "么", "吧", "呢", "啊", "呀", "嘛", "哦", "嗯",
    "是", "的", "了", "着", "过", "和", "与", "及", "或", "就", "都", "也",
    "还", "又", "很", "太", "更", "最", "不", "没", "无", "有",
}

# 词条标签里出现这些，才值得在「平时提到」时提示用法。
# 不上「文字梗」这种太宽的：它会把很多普通词条也带进来。
_MEME_TAG_WORDS = (
    "梗", "网络用语", "迷因", "黑话", "流行语", "谐音", "热梗", "网络梗",
    "梗文化", "玩梗", "网络迷因", "空耳", "缩写",
)
# 标签里带这些，说明语气有攻击性/玩笑性，提示她别对当事人认真用。
_TONE_TAG_WORDS = ("嘲讽", "反讽", "贬义", "调侃", "自嘲", "阴阳", "玩笑")

# 平时提到时，如果是明显的疑问/追问，不要当“在玩梗”去接。
_QUESTION_CUE = re.compile(r"(是谁|为什么|怎么|哪里|哪个|多少|吗|呢|[?？])")
_EXAMPLE_RE = re.compile(r"(?:例如|比如|示例|常见句式|可以接)[：:，,\s]*[“\"]([^”\"]{2,40})[”\"]")

_LEAD_MARKS = " \t\r\n「」『』“”\"'‘’（）()[]【】"
_TRAIL_MARKS = _LEAD_MARKS + "，。！？,.!?~～;；:：、"
_LIST_MARK = re.compile(r"^[\-\*·\d]+[\.、)]?\s*")


def _strip_marks(text: str) -> str:
    body = str(text or "").strip()
    # 两边可能叠着引号和标点，多剥两轮
    for _ in range(3):
        before = body
        body = body.strip(_LEAD_MARKS).strip(_TRAIL_MARKS).strip()
        if body == before:
            break
    return body


def clean_term(raw: str) -> str:
    """把问句里抠出来的 X 清成词条名；不合格返回空串。"""
    body = _strip_marks(raw)
    for _ in range(3):
        before = body
        for prefix in _PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix) :].strip()
                body = _strip_marks(body)
                break
        if body == before:
            break
    for _ in range(3):
        before = body
        for suffix in _TRAIL_FILLERS:
            if body.endswith(suffix) and len(body) > len(suffix):
                body = _strip_marks(body[: -len(suffix)])
                break
        if body == before:
            break
    if body.startswith("的"):
        body = _strip_marks(body[1:])
    if body.endswith("梗") and len(body) > 1:
        body = _strip_marks(body[:-1])
    if not body or body in _STOP_TERMS:
        return ""
    if len(body) > MAX_TERM:
        return ""
    if not re.search(r"[\w\u4e00-\u9fff]", body):
        return ""
    return body


def question_terms(text: str) -> list[str]:
    """从一句「X 是什么梗」里列出候选 X（先整段，再从长到短的后缀）。

    返回多个候选是为了兜住「我昨天听他们说尊嘟假嘟是什么意思」这种
    前面带了一串话的问法；调用方只查完全同名的词条，不会因为候选多就乱注入。
    """
    body = str(text or "")
    out: list[str] = []

    def add(term: str) -> None:
        if term and term not in out and len(out) < 24:
            out.append(term)

    for pattern in _PATTERNS:
        for match in pattern.finditer(body):
            raw = str(match.group("term") or "")
            add(clean_term(raw))
            cleaned_raw = _strip_marks(raw)
            for n in range(min(MAX_TERM, len(cleaned_raw)), 1, -1):
                add(clean_term(cleaned_raw[-n:]))
    return out


def question_term(text: str) -> str:
    """只要第一个候选；测试/命令行用。"""
    terms = question_terms(text)
    return terms[0] if terms else ""


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    return con


def _clip(text: str, limit: int) -> str:
    """压成一行，优先在句号/问号处收尾，别把半句话硬塞给模型。"""
    body = " ".join(str(text or "").split())
    body = _LIST_MARK.sub("", body).strip()
    if len(body) <= limit:
        return body
    cut = 0
    for i, ch in enumerate(body[: max(1, limit)]):
        if ch in "。！？!?" and i >= 24:
            cut = i + 1
            break
    if cut:
        return body[:cut].rstrip()
    return body[: max(1, limit - 1)].rstrip("，。；、 ") + "…"


def _fit_note(head: str, body: str, tail: str, max_chars: int) -> str:
    note = head + body + tail
    if len(note) <= max_chars:
        return note
    room = max(20, max_chars - len(head) - len(tail))
    body = _clip(body, room).rstrip("，。；、 ")
    note = head + body + tail
    if len(note) <= max_chars:
        return note
    return note[: max_chars - 1].rstrip("，。；、 ") + "…"


class MemeWiki:
    """只读梗百科；每次查询开一个只读连接，数据库可随时替换。"""

    SOURCE = "cnmeme.wiki"

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        max_chars: int = DEFAULT_MAX_CHARS,
        mention: bool = True,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        spell: memevariant.SpellBook | None = None,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.max_chars = max(120, int(max_chars))
        # 平时提到也提示用法；关掉就只回答「X 是什么梗」
        self.mention = bool(mention)
        self.cooldown = max(0, int(cooldown_minutes)) * 60
        # 写法 → 正词 的显式链接（data/meme_aliases.json）；和梗表共用同一个
        self.spell = spell
        # 标题索引：第一次用到时加载；数据库 mtime 变了会自动重载
        self._titles: list[str] = []
        # 拼音首字母骨架 → 词条名：`f8fq` / `fbfq` 走这张表认成「飞八分钱」
        self._skeletons: dict[str, tuple[str, ...]] = {}
        self._titles_mtime = -1.0
        # (群号, 词) -> 上次提示时间；只在“平时提到”模式里用
        self._recent: dict[tuple[str, str], float] = {}

    def __bool__(self) -> bool:
        return self.enabled

    # ---------- 状态 ----------

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def stats(self) -> dict[str, Any]:
        if not self.exists:
            return {"enabled": self.enabled, "exists": False, "path": str(self.path)}
        try:
            con = _open_ro(self.path)
            try:
                total = int(con.execute("SELECT COUNT(*) FROM memes").fetchone()[0])
                meta = {
                    str(r["key"]): str(r["value"])
                    for r in con.execute("SELECT key, value FROM meta")
                }
            finally:
                con.close()
        except sqlite3.Error as exc:
            return {
                "enabled": self.enabled,
                "exists": True,
                "path": str(self.path),
                "error": str(exc),
            }
        return {
            "enabled": self.enabled,
            "exists": True,
            "path": str(self.path),
            "count": total,
            "built_at": meta.get("built_at", ""),
            "source": self.SOURCE,
            "size_kb": self.path.stat().st_size // 1024,
        }

    # ---------- 查询 ----------

    @staticmethod
    def _lookup_with(con: sqlite3.Connection, term: str) -> dict[str, Any] | None:
        row = con.execute(
            "SELECT title, slug, tags, usage, origin, summary FROM memes "
            "WHERE title = ? COLLATE NOCASE OR slug = ? COLLATE NOCASE LIMIT 1",
            (term, term),
        ).fetchone()
        return dict(row) if row else None

    def lookup(self, term: str) -> dict[str, Any] | None:
        """按词条名精确查；大小写不敏感，不做模糊匹配。"""
        term = _strip_marks(term)
        if not self.enabled or not term or not self.exists:
            return None
        try:
            con = _open_ro(self.path)
            try:
                return self._lookup_with(con, term)
            finally:
                con.close()
        except sqlite3.Error:
            return None

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """标题包含 query 的条目；命令行排查用，不参与自动注入。"""
        query = _strip_marks(query)
        if not self.enabled or not query or not self.exists:
            return []
        try:
            con = _open_ro(self.path)
            try:
                rows = con.execute(
                    "SELECT title, slug, tags, usage, origin, summary FROM memes "
                    "WHERE title LIKE ? ORDER BY title LIMIT ?",
                    (f"%{query}%", max(1, min(50, int(limit)))),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    # ---------- 标题索引：平时提到时用 ----------

    @staticmethod
    def _has_meme_tag(tags: Any) -> bool:
        blob = str(tags or "")
        return any(word in blob for word in _MEME_TAG_WORDS)

    def _ensure_titles(self) -> None:
        if not self.enabled or not self.exists:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._titles_mtime:
            return
        try:
            con = _open_ro(self.path)
            try:
                rows = con.execute("SELECT title, tags FROM memes").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return
        titles: list[str] = []
        skeletons: dict[str, list[str]] = {}
        for row in rows:
            title = _strip_marks(str(row["title"] or ""))
            if len(title) < 2 or title in _STOP_TERMS:
                continue
            if self._has_meme_tag(row["tags"]):
                titles.append(title)
            # 纯数字/符号的标题（「11-1」「67」）不参与缩写归一：
            # 它们的骨架是 lll / yy 这种，会把随手的字母串认成梗。
            if not re.search(r"[a-z\u4e00-\u9fff]", title.lower()):
                continue
            key = memevariant.skeleton(title)
            if not (memevariant.MIN_CODE <= len(key) <= memevariant.MAX_CODE):
                continue
            bucket = skeletons.setdefault(key, [])
            if len(bucket) < 8:
                bucket.append(title)
        # 长的优先：「尊嘟假嘟」不能被「嘟」之类的短词先抢走
        titles = sorted(set(titles), key=len, reverse=True)
        self._titles = titles
        self._skeletons = {k: tuple(v) for k, v in skeletons.items()}
        self._titles_mtime = mtime

    def warm(self) -> int:
        """提前建标题索引；启动时调一次，别让第一条消息等。"""
        self._ensure_titles()
        return len(self._titles)

    def mention_term(self, text: str) -> str:
        """正常聊天里提到了哪个梗（最长匹配）；没提到返回空串。"""
        body = str(text or "").lower()
        if not body:
            return ""
        # 主人标过的写法（「鱼八分钱 = 飞八分钱」）：换字了，骨架认不出
        term = self.spell.resolve(text) if self.spell is not None else ""
        if term:
            return term
        self._ensure_titles()
        for title in self._titles:
            if title.lower() in body:
                return title
        # 缩写/数字谐音：`f8fq` 也认成「飞八分钱」
        for code in memevariant.codes_in(text):
            title = self.resolve_code(code)
            if title:
                return title
        return ""

    def _skeleton_titles(self, code: str) -> tuple[str, ...]:
        """缩写的骨架能对上哪些词条（可能不止一个）。"""
        code = memevariant.clean(code)
        if not code or not memevariant.is_code(code):
            return ()
        self._ensure_titles()
        return self._skeletons.get(memevariant.skeleton(code), ())

    def resolve_code(self, text: str) -> str:
        """把一个写法认成词库里的正词；认不出、或撞得太散，返回空串。

        顺序：主人标的显式链接（「鱼八分钱 = 飞八分钱」）→ 拼音骨架。
        骨架撞车时优先「纯汉字、更短」的那条（词库里 `飞8分钱` 和 `飞八分钱`
        是两条，但它们本来就是一个梗）；撞到 3 条以上就当认不出 ——
        把 `sb` 认成「随便」还是「傻逼」是要命的事。
        """
        if not self.enabled or not self.exists:
            return ""
        term = self.spell.resolve(text) if self.spell is not None else ""
        if term and self.lookup(term):
            return term
        hits = self._skeleton_titles(text)
        if len(hits) == 1:
            return hits[0]
        if not hits or len(hits) > 3:
            return ""
        return min(hits, key=self._canonical_rank)

    @staticmethod
    def _canonical_rank(title: str) -> tuple[int, int]:
        """越像「正词」排越前：纯汉字优先，然后短的优先。"""
        return (1 if re.search(r"[a-z0-9]", title.lower()) else 0, len(title))

    # ---------- 文案 ----------

    @staticmethod
    def _definition(row: dict[str, Any]) -> str:
        for key in ("usage", "summary", "origin"):
            body = " ".join(str(row.get(key) or "").split()).strip()
            if body:
                if key == "summary":
                    body = re.sub(r"^来源\s*", "", body).strip()
                # 词库里的列表/编号压成一句，别让模型把 Markdown 抄进回复
                body = _LIST_MARK.sub("", body).strip()
                body = re.sub(r"\s+[-*·]\s+", "；", body)
                body = re.sub(r"\s+\d+[.、)]\s+", "；", body)
                body = re.sub(r"[；;]{2,}", "；", body)
                body = re.sub(r"[：:][；;]", "：", body)
                body = re.sub(r"[；;][：:]", "；", body).strip("； ")
                return body
        return ""

    @staticmethod
    def _example_of(body: str) -> str:
        match = _EXAMPLE_RE.search(body)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _tone(row: dict[str, Any]) -> str:
        tags = str(row.get("tags") or "")
        for word in _TONE_TAG_WORDS:
            if word in tags:
                return word
        return ""

    @staticmethod
    def _risk(row: dict[str, Any]) -> str:
        """这条词库资料能不能让她跟着用；能返回空串。"""
        return memetone.risk(
            row.get("title"),
            row.get("tags"),
            row.get("usage"),
            row.get("origin"),
            row.get("summary"),
        )

    @staticmethod
    def _first_sentence(body: str, limit: int) -> str:
        body = " ".join(body.split()).strip()
        if not body:
            return ""
        for i, ch in enumerate(body[: max(1, limit)]):
            if ch in "。！？!?" and i >= 10:
                return body[: i + 1].strip()
        return _clip(body, limit)

    @staticmethod
    def _common_phrases(body: str) -> str:
        match = re.search(
            r"(?:常见说法包括|常见句式|可以接|常用句式|常见表达)[：:]\s*([^。]{2,120})",
            body,
        )
        if not match:
            return ""
        raw = _strip_marks(match.group(1))
        parts = [p.strip("；; ") for p in re.split(r"[；;]", raw) if p.strip()]
        # 只留前两句，别把一整串例句都塞进去
        return _clip("；".join(parts[:2]), 60)

    @staticmethod
    def _short_meaning(row: dict[str, Any]) -> str:
        """从 summary/origin 里找一句更像“意思”的话（尤其是谐音梗）。"""
        for key in ("summary", "origin"):
            body = " ".join(str(row.get(key) or "").split()).strip()
            if not body:
                continue
            match = re.search(r"是[“\"]([^”\"]{1,20})[”\"][^。]{0,15}谐音", body)
            if match:
                return f"是“{match.group(1)}”的谐音"
            match = re.search(r"(?:意思是|指的是|是指)[：:]?\s*([^。；]{2,40})", body)
            if match:
                return match.group(1).strip()
        return ""

    def _usage_parts(self, row: dict[str, Any]) -> tuple[str, str]:
        """拆成（一句意思，能直接接的用法/例子）。"""
        body = self._definition(row).rstrip("，。；、 ")
        if not body:
            return "", ""
        meaning = self._first_sentence(body, 70)
        short = self._short_meaning(row)
        if short and short not in meaning:
            meaning = f"{short}；{meaning}"
            meaning = _clip(meaning, 90)
        example = self._example_of(body)
        if example:
            meaning = meaning.replace(f"“{example}”", "").strip("；。 ")
            usage = f"例：“{example}”"
        else:
            usage = self._common_phrases(body)
            if usage:
                usage = f"常见说法：{usage}"
                if "常见说法包括" in meaning:
                    meaning = meaning.split("常见说法包括", 1)[0].rstrip("；。 ")
        if not meaning:
            meaning = _clip(body, 70)
        return meaning, usage

    @staticmethod
    def _strip_title(text: str, title: str) -> str:
        """去掉释义开头重复的词条名，读起来别像「X 是 X」。"""
        cleaned = text.replace(f"“{title}”", "", 1).lstrip("；，。:： ")
        return cleaned or text

    def _question_note(self, row: dict[str, Any], term: str) -> str:
        title = _strip_marks(str(row.get("title") or term)) or term
        title = title[:24]
        # 先判风险：骂人/黄腔/刷屏的词条只给「懂但不用」，不能把原句里的
        # 「刷屏使用」这类模板和粗口再喂回去。
        risk = self._risk(row)
        if risk:
            return memetone.question_note(title, risk)
        meaning, usage = self._usage_parts(row)
        if not meaning and not usage:
            return ""
        meaning = self._strip_title(meaning, title)
        body = meaning.rstrip("，。；、 ") + (f"。用法：{usage}" if usage else "")
        head = f"（网上的说法：「{title}」："
        tail = "。要接就按这个用，别背定义、别主动科普。）"
        return _fit_note(head, body, tail, self.max_chars)

    def _usage_note(self, row: dict[str, Any], term: str) -> str:
        title = _strip_marks(str(row.get("title") or term)) or term
        title = title[:24]
        risk = self._risk(row)
        if risk:
            return memetone.mention_note(title, risk)
        meaning, usage = self._usage_parts(row)
        if not meaning and not usage:
            return ""
        meaning = self._strip_title(meaning, title)
        body = meaning.rstrip("，。；、 ") + (f"。可以这么接：{usage}" if usage else "")
        tone = self._tone(row)
        tone_note = f"语气偏{tone}。" if tone else ""
        head = f"（「{title}」："
        tail = (
            f"。{tone_note}懂了就按这个接一句，别解释、别背定义；"
            "不搭就别硬塞。）"
        )
        return _fit_note(head, body, tail, self.max_chars)

    # ---------- 对外入口 ----------

    def _lookup_note(self, terms: list[str], *, mode: str) -> str:
        try:
            con = _open_ro(self.path)
            try:
                for term in terms:
                    row = self._lookup_with(con, term)
                    if not row:
                        # 「f8fq 是什么意思」：按写法认成正词再查
                        title = self.resolve_code(term)
                        row = self._lookup_with(con, title) if title else None
                    if not row:
                        continue
                    if mode == "question":
                        return self._question_note(row, term)
                    return self._usage_note(row, term)
            finally:
                con.close()
        except sqlite3.Error:
            return ""
        return ""

    def _cooling(self, group_id: str, term: str, now: float | None) -> bool:
        if self.cooldown <= 0:
            return False
        now = time.time() if now is None else float(now)
        last = self._recent.get((str(group_id), term), 0.0)
        return bool(last and now - last < self.cooldown)

    def _mark(self, group_id: str, term: str, now: float | None) -> None:
        if self.cooldown <= 0:
            return
        now = time.time() if now is None else float(now)
        self._recent[(str(group_id), term)] = now
        # 只留最近用得上的，别让长期运行攒成一张大表
        if len(self._recent) > 4000:
            cutoff = now - self.cooldown * 2
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}

    def risk_of(self, text: str) -> str:
        """这句里提到的词条有没有高风险（骂人/黄腔/刷屏）。

        给「跟着复读」这类不经过模型的路径做本地硬门：知道是骂人梗，
        也不能原样发出去。只查词条，不产生提示、不改冷却。
        缩写也要认：群里刷 `f8fq` 的时候，不能因为词库里只写了「飞八分钱」
        就放过去 —— 那正是要拦的那一类。
        """
        if not self.enabled or not self.exists or not str(text or "").strip():
            return ""
        term = self.mention_term(text)
        row = self.lookup(text) or (self.lookup(term) if term else None)
        if row:
            risk = self._risk(row)
            if risk:
                return risk
        for code in memevariant.codes_in(text):
            for title in self._skeleton_titles(code):
                cand = self.lookup(title)
                risk = self._risk(cand) if cand else ""
                if risk:
                    return risk
        return ""

    def note(self, text: str, *, group_id: str = "", now: float | None = None) -> str:
        """问梗/提到梗时返回一段 ≤max_chars 的提示；不该提示返回空串。"""
        if not self.enabled or not self.exists or not str(text or "").strip():
            return ""
        terms = question_terms(text)
        if terms:
            # 明确问梗：给意思 + 用法；不走冷却，问几次答几次
            return self._lookup_note(terms, mode="question")
        if not self.mention:
            return ""
        if _QUESTION_CUE.search(str(text)):
            # 「尊嘟假嘟是谁」不是在使用这个梗，别硬塞用法
            return ""
        term = self.mention_term(text)
        if not term:
            return ""
        if self._cooling(group_id, term, now):
            return ""
        note = self._lookup_note([term], mode="usage")
        if note:
            self._mark(group_id, term, now)
        return note
