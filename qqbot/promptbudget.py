"""固定前缀的账：人设、规则、能力表、表情标签、工具各占多少 token。

只做测量，**不改** prompt。`tools/prompt_budget.py` 打表、做预算检查、
模拟人设瘦身；`run.py` 启动时也打一行，免得前缀悄悄长胖没人发现。

估算口径：汉字约 0.8 token、其他字符约 0.3 token —— 这是拿本仓库
11,648 的实测缓存前缀反推的，不是 DeepSeek 的官方分词器；绝对值有
5% 上下的误差。所以只要 usage.db 里有实测值，CLI 会把各段按实测前缀
等比缩放，比例是准的。真实 token 以 API 返回的 usage 为准（账本里就有）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

# 汉字 / 其他字符的 token 系数。改这两个数之前先看一遍 CLI 的缩放说明：
# 各段占比只跟系数之比有关，绝对值会被实测值校正。
CJK_TOKEN = 0.8
OTHER_TOKEN = 0.3

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")


def estimate_tokens(text: str) -> float:
    """粗估一段文本的 token 数（汉字 0.8、其余 0.3）。"""
    if not text:
        return 0.0
    cjk = len(_CJK.findall(text))
    return cjk * CJK_TOKEN + (len(text) - cjk) * OTHER_TOKEN


def split_persona(text: str) -> list[tuple[str, str]]:
    """把 persona.md 按 `##` / `###` 切成段，返回 [(标题, 整段原文), ...]。

    标题不含 `#`；第一个标题之前的内容叫「开头」。各段原文按顺序拼起来
    正好等于原文（测试盯着这条），人设分层直接拿它当切分依据。
    """
    out: list[tuple[str, str]] = []
    title = "开头"
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _HEADING.match(line.rstrip("\r\n"))
        if match:
            if out or "".join(buf).strip():
                out.append((title, "".join(buf)))
            title = match.group(2)
            buf = [line]
        else:
            buf.append(line)
    out.append((title, "".join(buf)))
    return [(name, body) for name, body in out if body.strip()]


def persona_sections(text: str) -> list[tuple[str, int]]:
    """按段切，返回 [(标题, 字符数), ...] —— 字符数含标题行，总和等于 len(text)。"""
    return [(title, len(body)) for title, body in split_persona(text)]


def sticker_prompt(book: Any) -> str:
    """system 里的表情标签段。**只此一份**：app.py 也调它，免得两处写出偏差。"""
    if not getattr(book, "available", False):
        return ""
    emotions = book.describe_moods()
    scenes = book.describe_scenes()
    text_parts = [
        f"你可以在回复的最后贴一张表情，写法是 [表情:标签]。\n"
        f"**默认用情绪标签**：{emotions}。"
    ]
    if scenes:
        text_parts.append(
            "只有对话内容**正好对上**下面某个场景时，才用场景标签"
            f"（对不上就别用，别硬套）：{scenes}"
        )
    text_parts.append(
        "规则：最多一张，放在整条回复的最后一个字后面；不要放在句中间；"
        "不要每条都贴，大概三四条里贴一次；情绪要贴对——道谢、被夸贴「害羞」或「开心」，"
        "心疼对方才贴「委屈」，别什么都贴委屈。"
        "**表情是给已经写好的正文配图，不是正文的情绪来源**：先按对话把话说完，"
        "再挑一张不跟正文打架的图；不要因为候选里有「生气/委屈/撒娇」就把自己的语气"
        "改成那样，也不要为了贴图编出原本没有的情绪（本来没生气却写「别以为我气消了」）。"
        "别人发来或引用的表情包也一样：图是图，图库分类不代表谁现在的心情。"
        "如果本轮上下文里给了「可贴的表情包」候选（#编号），想贴其中某一张就写 [表情:#编号]；"
        "没给候选、或候选都对不上，再按上面的情绪/场景标签写。"
        "用户点名要某个表情包（图里的字、画面、梗）时，先调用 find_sticker 找，"
        "再用 [表情:#编号] 发其中一张。"
    )
    return "\n".join(text_parts)


@dataclass(frozen=True)
class Part:
    """前缀里的一段。token 是估算值，实测后会被等比缩放。"""

    key: str
    label: str
    chars: int
    tokens: float

    @property
    def share(self) -> float:
        return self.tokens


def measure(
    *,
    system: str,
    persona: str,
    sticker: str,
    capability: str,
    tools: Iterable[dict[str, Any]] = (),
) -> list[Part]:
    """把一次请求的固定前缀拆成几段。总和约等于 system + tools 的 token。"""
    tool_text = json.dumps(list(tools), ensure_ascii=False) if tools else ""
    total = estimate_tokens(system)
    persona_tok = estimate_tokens(persona)
    sticker_tok = estimate_tokens(sticker)
    cap_tok = estimate_tokens(capability)
    rules_chars = max(0, len(system) - len(persona) - len(sticker) - len(capability))
    rules_tok = max(0.0, total - persona_tok - sticker_tok - cap_tok)
    return [
        Part("persona", "人设", len(persona), persona_tok),
        Part("rules", "规则（身份/记忆/搜索等）", rules_chars, rules_tok),
        Part("capability", "能力表", len(capability), cap_tok),
        Part("sticker", "表情标签", len(sticker), sticker_tok),
        Part("tools", "工具说明", len(tool_text), estimate_tokens(tool_text)),
    ]


def scaled(parts: Iterable[Part], total_tokens: float) -> list[Part]:
    """按实测前缀把各段等比缩放 —— 比例不变，绝对值对上账本。"""
    parts = list(parts)
    estimated = sum(p.tokens for p in parts)
    if estimated <= 0 or total_tokens <= 0:
        return parts
    factor = total_tokens / estimated
    return [Part(p.key, p.label, p.chars, p.tokens * factor) for p in parts]


def total_of(parts: Iterable[Part]) -> float:
    return sum(p.tokens for p in parts)
