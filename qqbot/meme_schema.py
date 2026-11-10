"""梗的结构化描述：把「一句话解释」拆成 meaning / usage / origin / examples。

老数据只有 `scene` 一段自由文本；这里做兼容：
- 新条目写结构化字段；
- 老条目用 `scene` 回填 meaning，不丢原意；
- `status="candidate"` 表示「聊天里搜到、但还没确认」，不参与注入。
"""

from __future__ import annotations

import re
import time
from typing import Any

_WORD_RE = re.compile(r"^[\w\u4e00-\u9fff·\-—]{1,20}$")
_MEME_HINT = re.compile(
    r"(是什么梗|什么梗|什么意思|啥意思|的梗|梗的?意思|谐音|代称|黑话|出处|来源|怎么用|怎么接)"
)
_URL_RE = re.compile(r"https?://\S+")
_TITLE_RE = re.compile(r"^\[\d+\]\s*")
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")

_MEANING_HINTS = ("意思是", "指的是", "指代", "就是", "本义", "原意", "表示")
_USAGE_HINTS = ("用法", "用来", "通常", "一般", "接法", "可以接", "多用于")
_ORIGIN_HINTS = ("出自", "来源", "来自", "起源", "最早", "源于")


def is_candidate(item: dict[str, Any] | None) -> bool:
    return bool(isinstance(item, dict) and item.get("status") == "candidate")


def term_from_query(query: str) -> str:
    """从「X 是什么梗 / X 什么意思 / X 谐音」里取出 X。"""
    q = " ".join(str(query or "").split()).strip(" \t\"'“”‘’《》<>")
    if not q or not _MEME_HINT.search(q):
        return ""
    q = re.sub(
        r"(?:是什么梗|什么梗|是什么意思|什么意思|啥意思|的出处|出处|来源|怎么用|怎么接|谐音|代称|黑话|的梗)$",
        "",
        q,
    ).strip(" 的，,。！？!?：:；;")
    if not _WORD_RE.fullmatch(q):
        return ""
    return q


def looks_like_meme_query(query: str) -> bool:
    return bool(_MEME_HINT.search(str(query or "")))


def looks_like_meme_material(material: str) -> bool:
    text = str(material or "")
    if not text:
        return False
    return bool(
        re.search(r"(梗|流行语|网络用语|谐音|黑话|代称|出处|意思是|指的是|本义|原意)", text)
    )


def _sentences(material: str) -> list[str]:
    out: list[str] = []
    for line in str(material or "").splitlines():
        line = _TITLE_RE.sub("", line).strip()
        if not line or line.startswith(("搜索「", "（链接", "来源：http")):
            continue
        line = _URL_RE.sub("", line).strip()
        for part in _SENTENCE_SPLIT.split(line):
            part = part.strip(" -—·")
            if len(part) >= 4:
                out.append(part[:160])
    return out


def _first_with(sentences: list[str], hints: tuple[str, ...]) -> str:
    for hint in hints:
        for text in sentences:
            if hint in text:
                return text
    return ""


def build_candidate(term: str, query: str, material: str, *, now: float | None = None) -> dict[str, Any]:
    """从一次搜索结果里抽结构化候选；抽不全就留空，不硬编。"""
    query_text = " ".join(str(query or "").split()).strip()
    sentences = [
        s
        for s in _sentences(material)
        if s.strip() != query_text and not (s.strip().startswith(query_text) and "是什么梗" in s)
    ]
    summary = ""
    for text in sentences:
        if term and term in text:
            summary = text
            break
    if not summary and sentences:
        summary = sentences[0]
    meaning = _first_with(sentences, _MEANING_HINTS) or summary
    usage = _first_with(sentences, _USAGE_HINTS)
    if usage == meaning:
        usage = ""
    origin = _first_with(sentences, _ORIGIN_HINTS)
    tags: list[str] = []
    for word, tag in (
        ("谐音", "谐音"),
        ("缩写", "缩写"),
        ("黑话", "黑话"),
        ("表情包", "表情包"),
        ("主播", "主播"),
        ("游戏", "游戏"),
    ):
        if word in material:
            tags.append(tag)
    urls = _URL_RE.findall(str(material or ""))[:3]
    return {
        "text": term,
        "schema": 2,
        "meaning": meaning[:160],
        "usage": usage[:160],
        "origin": origin[:160],
        "examples": [term] if term else [],
        "tags": tags,
        "status": "candidate",
        "evidence": {
            "source": "chat-search",
            "query": str(query or "")[:128],
            "summary": summary[:300],
            "urls": urls,
            "captured_at": time.time() if now is None else float(now),
        },
    }


def from_legacy(item: dict[str, Any]) -> dict[str, Any]:
    """老条目补齐结构化字段；不覆盖已有的新字段。"""
    out = dict(item)
    scene = str(item.get("meaning") or item.get("scene") or "").strip()
    if scene and not out.get("meaning"):
        out["meaning"] = scene[:160]
    if not out.get("usage"):
        replies = item.get("reply") or []
        if replies:
            texts = [str(r.get("text") or "").strip() for r in replies if isinstance(r, dict)]
            texts = [x for x in texts if x]
            if texts:
                out["usage"] = "通常接「" + "」「".join(texts[:3]) + "」"
    if not out.get("origin"):
        note = str(item.get("verify_note") or "").strip()
        source = str(item.get("source") or "")
        out["origin"] = note or ("网上" if source in ("web", "web-candidate") else "群里")
    if not out.get("examples"):
        example = str(item.get("example") or "").strip()
        out["examples"] = [example] if example else []
    if not out.get("tags"):
        out["tags"] = []
    if not out.get("status"):
        out["status"] = "confirmed"
    if not out.get("evidence"):
        out["evidence"] = {}
    out.setdefault("schema", 2)
    return out
