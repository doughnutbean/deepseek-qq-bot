"""模型自己调用的日记工具：翻日记 / 写日记 / 列日记。

日记在本地 `data/diary.db`，只有主人私聊能看能写。工具只做三件事：

- `read_diary(day)`：读某天或最近一篇；
- `write_diary(day, force)`：真的写一篇（会调一次模型、会花钱、会落库）；
- `list_diary()`：最近写过哪些天。

权限双重把关：`llm.reply` 只把 provider 挂给主人私聊那一轮；`run()` 里再验
`is_owner` + `private:` 会话。群友/群里问起来，连内容都不给。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from .tool_spec import make_tool

log = logging.getLogger(__name__)

ReadFn = Callable[[str], Awaitable[str]]
WriteFn = Callable[..., Awaitable[str]]
ListFn = Callable[[], Awaitable[str]]


class DiaryTools:
    def __init__(self, *, read: ReadFn, write: WriteFn, list_recent: ListFn) -> None:
        self._read = read
        self._write = write
        self._list = list_recent

    def schemas(self) -> list[dict[str, Any]]:
        return [
            make_tool(
                "read_diary",
                "翻本鱼自己的日记。主人说「看看日记」「今天的日记」「昨天写了什么」"
                "「你最近写了什么」时用；只读，不花钱、不改任何东西。day 不传就返回"
                "最近一篇；认不出日期会返回没找到，不要自己编。只有主人私聊能用，"
                "群聊和群友一律不给看。",
                {
                    "day": {
                        "type": "string",
                        "description": (
                            "要翻哪天：today / yesterday / YYYY-MM-DD，"
                            "也可以说「前天」。不传 = 最近一篇。"
                        ),
                    }
                },
            ),
            make_tool(
                "write_diary",
                "真的写一篇日记并保存（会调用一次模型、会产生费用）。主人说「写日记」"
                "「把今天记下来」「补一下昨天的日记」「重写今天的」时用；同一天已经写过"
                "就返回原文，不重复花钱，除非主人明确要重写、传 force=true。素材太少时"
                "会照实说没写成，不要假装写好了。只有主人私聊能用。",
                {
                    "day": {
                        "type": "string",
                        "description": "写哪天：today / yesterday / YYYY-MM-DD；不传 = 今天。",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "true = 覆盖那天已有的日记（仅在主人明确说「重写」时传）。",
                    },
                },
            ),
            make_tool(
                "list_diary",
                "列一下本鱼最近写过哪些日记（日期、标题、字数），只读、不花钱。"
                "主人问「写过哪些日记」「日记列表」「最近都记了什么」时用；"
                "只给主人私聊，群里不列。",
            ),
        ]

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        *,
        turn: Any = None,
        **ctx: Any,
    ) -> str | None:
        if name not in {"read_diary", "write_diary", "list_diary"}:
            return None
        if turn is not None:
            ctx = turn.as_ctx()
        # 双保险：schema 只挂给主人私聊，这里再挡一次
        if not ctx.get("is_owner") or not str(ctx.get("session") or "").startswith(
            "private:"
        ):
            return "日记只在本鱼和主人的私聊里看，群里不给。"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name == "list_diary":
            return await self._list()
        if name == "read_diary":
            return await self._read(str(args.get("day") or ""))
        return await self._write(
            str(args.get("day") or ""),
            bool(args.get("force")),
        )
