"""模型自己调用的「找同款肥鱼」工具。

为什么要有这个工具：群里到底有几条同款肥鱼、分别是谁，光靠系统提示里的
一句「还有 N 条」不够 —— 她说不出名字，也没法真的去搭话。本地指令又只认
「几条鱼」这类整句；主人换个说法（「去跟别的鱼打个招呼」「群里还有谁”）就
只能嘴上答应。工具的第二层兜底见 ``docs/工具规范.md``。

安全边界（都和 ``peerchat`` 共用）：

- 只在群聊里给；私聊没有名册，直接返回一句提示；
- ``list`` 只读，不调模型、不发消息；
- ``say`` 会真的在群里 @ 对方发一句，所以过 ``PeerChat.outreach()``：
  真人不在场、太频繁、风控/潜水/群静默时都会拒绝，并返回人话；
- 发出去之后这一轮不再让模型复述「已经聊上了」，由调用方看
  ``command_state["done"]`` 把正文丢掉。

工具只负责「挑动作」：真正发消息、记账、写上下文在 ``app.Bot`` 里，
这里不直接碰 OneBot。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from .tool_spec import make_tool

log = logging.getLogger(__name__)


class FishTools:
    """一个很薄的工具提供者：schema + run，和 GroupTools/LiveTools 同一套接口。"""

    label = "FishTools"
    NAMES = {"fish_chat"}

    def __init__(
        self,
        lister: Callable[[str], str],
        chatter: Callable[[str, str, str, str], Awaitable[tuple[bool, str]]],
    ) -> None:
        # lister(group_id) -> 给模型看的名单（纯本地、0 调用）
        # chatter(session, group_id, target, text) -> (是否发出去, 给模型的结果)
        self._list = lister
        self._chat = chatter

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            make_tool(
                "fish_chat",
                "本群有别的同款肥鱼时用。action=list 查本群连本鱼一共几条肥鱼、"
                "另外几条分别是谁（昵称+QQ）、谁最近说过话；只读，不发消息。"
                "action=say 主动找其中一条说话：会真的在群里 @ 他并发出 text。"
                "有人说「群里几条鱼」「还有别的鱼吗」「去跟那条鱼聊聊」「找别的鱼玩」，"
                "或主人让你去打个招呼、你自己想找同类搭句话时调用。"
                "say 有频率限制：最近 10 分钟没有真人说话、风控/潜水/群静默时会被拒绝；"
                "本群每小时最多 3 次、两次隔 3 分钟、同一条鱼 10 分钟一次。"
                "失败会返回原因，没发出去就别在正文里说「已经聊上了」；"
                "成功后这一轮不用再复述，正文会被丢掉。",
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "say"],
                        "description": "list=只查有几条、分别是谁；say=真的 @ 对方发一句。",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "say 必填：要 @ 的肥鱼，填 list 里的 QQ 号或昵称；"
                            "别猜，认不准先用 list。"
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "say 必填：你要说的那一句（一两句、口语）。"
                            "会自动 @ 他，别再自己写 [at:QQ]。"
                        ),
                    },
                },
                ["action"],
            )
        ]

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        *,
        is_owner: bool = False,
        session: str = "",
        command_state: dict[str, Any] | None = None,
        turn: Any = None,
        **_ctx: Any,
    ) -> str | None:
        if turn is not None:
            _ctx = turn.as_ctx()
            is_owner = _ctx.get('is_owner')
            session = _ctx.get('session')
            command_state = _ctx.get('command_state')
        if name not in self.NAMES:
            return None
        session = str(session or "")
        if not session.startswith("group:"):
            return "只有群里才有别的同款肥鱼；私聊里看不到本群的名册。"
        group_id = session.split(":", 1)[1].strip()
        if not group_id:
            return "（不知道是哪个群。）"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "（参数不是合法 JSON。）"
        if not isinstance(args, dict):
            return "（参数格式不对。）"

        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            return self._list(group_id)
        if action != "say":
            return "action 只认 list 或 say。"

        target = str(args.get("target") or "").strip()
        text = str(args.get("text") or "").strip()
        if not target:
            return "要 @ 哪条鱼？先调 fish_chat（action=list）看本群有谁。"
        if not text:
            return "想跟他说什么？把要说的话填在 text 里。"

        ok, result = await self._chat(session, group_id, target, text)
        if ok:
            # 消息已经由 app 发出去了：这一轮正文丢掉，别在群里再补一句
            # 「本鱼去打个招呼啦」——那会变成两条消息，而且第二条什么也没做。
            state = command_state if isinstance(command_state, dict) else None
            if state is not None:
                state["done"] = True
                state["text"] = "找肥鱼聊天"
        return result
