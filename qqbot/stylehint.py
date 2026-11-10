"""真实对话片段：给她看群里人是怎么一来一回说话的。

2026-11-05 的教训：一开始想配「谁接了谁」，用「下一条消息」当回应——
但群里是交叉聊天，挨着不代表在接，隔几句反而可能还在说同一件事。
配错一次，教的就是错的。

所以不判「谁接谁」了：检索到相似话题后，把那一段真实对话（前后几条）整段给她。
她学的是**节奏和口气**，不是某一句的配对关系。
"""

from __future__ import annotations

MAX_CHARS = 260
MAX_LINES = 5

_HEAD = "（群里聊到类似话题时，真实的一段对话："
_TAIL = "看他们怎么接话、说多长、什么口气；别照抄内容，也别当成现在有人在说。）"


def render_thread(lines: list[tuple[str, str]]) -> str:
    """一段真实对话：[(谁, 说了什么)]，按时间顺序，压成一小段。"""
    kept: list[str] = []
    used = len(_HEAD) + len(_TAIL)
    for nick, text in lines[:MAX_LINES]:
        body = " ".join(str(text or "").split())[:24]
        if not body:
            continue
        who = " ".join(str(nick or "").split())[:8]
        line = f"{who}：{body}" if who else body
        if used + len(line) > MAX_CHARS:
            break
        kept.append(line)
        used += len(line)
    if len(kept) < 2:
        return ""
    return _HEAD + "\n" + "\n".join(kept) + "\n" + _TAIL
