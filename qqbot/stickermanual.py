"""主人人工收图：字段解析、校验和引导文案。

自动捡图可能因为「不是她 / 注解太虚 / 拒过缓存 / 额度」被拦；人工收图绕过这些，
但字段仍按 `stickers/index.json` 的口径填，方便候选菜单和检索直接用。

格式（回给机器人）：
    人工收图 when=被主人夸但心里不屑时；emotion=无语；text=典型的碳基思维
             never=对方认真倾诉时；tags=被夸,吐槽
只写要改的字段也行；`when` 必填。
"""

from __future__ import annotations

import re

PREFIXES = ("人工收图", "人工收录", "手工收图", "手动收图", "人工收藏")
_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(PREFIXES) + r")\s*[:：]?\s*(.*)$", re.S
)
_KEYS = {
    "when": ("when", "情境", "什么时候", "用法", "场景"),
    "emotion": ("emotion", "情绪", "心情"),
    "text": ("text", "图字", "文字", "图里的字"),
    "visual": ("visual", "画面", "描述"),
    "joke": ("joke", "梗", "笑点"),
    "never": ("never", "禁忌", "别用", "不能"),
    "tags": ("tags", "标签"),
    "folder": ("folder", "分类", "目录"),
    "intensity": ("intensity", "强度"),
    "target": ("target", "指向"),
}
_ALIAS = {alias.lower(): key for key, names in _KEYS.items() for alias in names}
_KEY_LINE = re.compile(r"(?:^|[；;\n])\s*([^=:：]{1,10})\s*[=:：]", re.MULTILINE)


def split_command(text: str) -> str | None:
    """识别「人工收图…」前缀，返回后面的字段串；不是人工收图返回 None。"""
    match = _PREFIX_RE.match((text or "").strip())
    return match.group(1).strip() if match else None


def parse_fields(body: str) -> tuple[dict[str, str], list[str]]:
    """把字段串拆成 dict；返回 (字段, 不认识的键)。

    没有任何 `key=value` 时，整段当 `when`（「人工收图 想卖萌的时候」）。
    """
    body = (body or "").strip().strip("：:;；")
    if not body:
        return {}, []
    if not _KEY_LINE.search(body):
        return {"when": body}, []
    fields: dict[str, str] = {}
    unknown: list[str] = []
    for part in re.split(r"[；;\n]+", body):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^([^=:：]{1,10})\s*[=:：]\s*(.*)$", part)
        if not match:
            unknown.append(part[:16])
            continue
        key = _ALIAS.get(match.group(1).strip().lower())
        if key is None:
            unknown.append(match.group(1).strip()[:16])
            continue
        fields[key] = match.group(2).strip()
    return fields, unknown


def validate(raw: dict[str, str], draft: dict | None = None) -> tuple[dict, str]:
    """合并草稿与人工字段，校验并夹到 index 口径。返回 (字段, 错误)。"""
    draft = draft or {}

    def pick(key: str, default: str = "") -> str:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return str(draft.get(key) or default).strip()

    when = pick("when")
    if len(when) < 2:
        return {}, "when 必填：2~40 字，写「什么情境下贴」，比如「被主人夸但心里不屑时」"
    if len(when) > 40:
        return {}, "when 太长了，压到 40 字以内"
    emotion = pick("emotion", "无语")[:6] or "无语"
    text = pick("text")[:200]
    visual = pick("visual")[:80]
    joke = pick("joke")[:60]
    never = pick("never")[:40]
    if never in ("无", "没有", "不用", "不"):
        never = ""
    folder = pick("folder")[:10]
    try:
        intensity = int(pick("intensity", "2"))
    except ValueError:
        intensity = 2
    intensity = min(3, max(1, intensity))
    target = pick("target", "无")
    if target not in ("对方", "自己", "无"):
        target = "无"

    tags_raw = raw.get("tags")
    if isinstance(tags_raw, str) and tags_raw.strip():
        tags = [t.strip() for t in re.split(r"[,，、/|]", tags_raw) if t.strip()]
    else:
        tags = [str(t).strip() for t in (draft.get("tags") or []) if str(t).strip()]
    tags = list(dict.fromkeys(t[:8] for t in tags))[:4] or [emotion]

    return {
        "text": text,
        "visual": visual,
        "joke": joke,
        "emotion": emotion,
        "intensity": intensity,
        "target": target,
        "when": when,
        "never": never,
        "tags": tags,
        "folder": folder,
    }, ""


def guide_text(draft: dict | None = None, reason: str = "") -> str:
    """没有字段时回给主人的完整引导（带草稿）。"""
    draft = draft or {}
    bits = []
    if draft.get("emotion"):
        bits.append(f"情绪={draft['emotion']}")
    if draft.get("text"):
        bits.append(f"图字={str(draft['text'])[:20]}")
    if draft.get("visual"):
        bits.append(f"画面={str(draft['visual'])[:30]}")
    if draft.get("when"):
        bits.append(f"草稿情境={draft['when']}")
    draft_line = "｜".join(bits) if bits else "（没认出内容，你直接描述）"
    reason_line = f"刚才被拦的原因：{reason}\n" if reason else ""
    return (
        "人工收图：自动判定/额度/拒过缓存都不挡你，人工标注后直接放行。\n"
        f"{reason_line}草稿：{draft_line}\n"
        "回我（只写要改的字段也行）：\n"
        "人工收图 when=想卖萌/装乖时；emotion=撒娇；text=图里的字；"
        "never=对方难过时；tags=卖萌,撒娇\n"
        "规则：\n"
        "1. when 必填，2~40 字，写「什么情境下贴」，不要只写「可爱/好看」\n"
        "2. emotion 从 开心/得意/害羞/撒娇/委屈/生气/无语/困惑/疲惫/慌张/"
        "心虚/嫌弃/求饶/震惊/不行/挑衅 里选，也可自拟 2~4 字\n"
        "3. text 只抄图里真实出现的字，看不清就留空\n"
        "4. never 可选，写「绝对不能贴」的场合；tags 最多 4 个，用逗号隔开\n"
        "例子：人工收图 when=被主人夸但心里不屑时；emotion=无语；"
        "text=典型的碳基思维；tags=被夸,吐槽\n"
        "10 分钟内回我；图可以重新发/引用。"
    )
