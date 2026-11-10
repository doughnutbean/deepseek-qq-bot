"""按特征找本地表情包：给模型一个 `find_sticker` 工具。

用户点名要某个表情包（「哦鲸鲸那个」「抱着鲸鱼的那张」「大肥鱼跳舞的」）时，
模型可以先查图库，拿到编号再用 `[表情:#编号]` 发出来；不用靠每轮那 6 张候选碰运气。
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class StickerTools:
    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "find_sticker",
                    "description": (
                        "在自己的本地表情包库里按特征找图：图里的字、画面、梗、情绪、"
                        "适用情境都行。用户点名要某个表情包（比如「哦鲸鲸那个」"
                        "「抱着鲸鱼的那张」「大肥鱼跳舞的」）时先调用这个；"
                        "返回候选编号后，用 [表情:#编号] 发其中一张。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "描述要找的图：图里的字、画面、梗或情绪，1~30 字。",
                            },
                            "count": {
                                "type": "integer",
                                "description": "返回几张候选，1~8，默认 5。",
                            },
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    async def run(
        self, name: str, arguments: str, turn: Any = None, **ctx: Any
    ) -> str | None:
        if turn is not None:
            ctx = turn.as_ctx()
        if name != "find_sticker":
            return None
        book = ctx.get("stickers")
        if book is None or not getattr(book, "available", False):
            return "（本地表情包没开）"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "（参数不是 JSON）"
        query = str(args.get("query") or "").strip()
        if not query:
            return "（要找一个什么样的表情包？）"
        try:
            count = int(args.get("count") or 5)
        except (TypeError, ValueError):
            count = 5
        try:
            items = await book.search(query, count)
        except Exception:  # noqa: BLE001 - 找图失败不能拖垮回复
            log.debug("find_sticker 搜索失败", exc_info=True)
            return "（图库检索失败了，用 [表情:情绪标签] 先顶一下吧）"
        if not items:
            return (
                "（图库里没找到这个特征的表情包；换个说法试试：图里的字、"
                "画面动作、梗，或者直接用 [表情:情绪标签]）"
            )
        lines = ["找到这些，想发哪张就在回复最后写 [表情:#编号]："]
        for item in items:
            desc = item.when or item.joke or item.visual
            bits = [f"#{item.sid}"]
            if item.folder:
                bits.append(f"[{item.folder}]")
            if desc:
                bits.append(desc[:32])
            if item.text:
                bits.append("图字：" + item.text[:24])
            lines.append("｜".join(bits))
        return "\n".join(lines)
