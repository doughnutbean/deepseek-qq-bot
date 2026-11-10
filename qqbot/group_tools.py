"""模型自己调用的群管理工具：退群。

为什么要有这个工具：本地指令只认「退群 / 退出这个群 / 退群 123456789」
这几种整句；主人换个说法（「把那个群退了」「咱们退群吧」），只靠正则
她就会嘴上答应、实际没退。工具的第二层兜底见 ``docs/工具规范.md``。

安全边界：

- schema 只给主人（``llm.reply`` 里判 ``is_owner``），``run()`` 里再挡一次；
- 群号必须是 5~12 位数字；私聊里没给群号只返回一句提示，不猜；
- 真正的退群、告别、清理都在 ``app._leave_group_and_report`` 里，
  工具只负责「挑动作」，不直接碰 OneBot。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from .groupleave import GROUP_ID_RE
from .tool_spec import make_tool

log = logging.getLogger(__name__)


class GroupTools:
    """一个很薄的工具提供者：schema + run，和 LiveTools/CommandTools 同一套接口。"""

    NAMES = {"group_leave"}

    def __init__(self, leave: Callable[[str, str], Awaitable[str]]) -> None:
        # leave(gid, session) -> 给模型的短结果；用户能看到的回执由 app 发
        self._leave = leave

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            make_tool(
                "group_leave",
                "让本鱼退出一个 QQ 群，只有主人能用。主人明确说「退群」「退出这个群」"
                "「把这个群退了」「把群 123456789 退了」时调用；会真的退出，退完回不来。"
                "如果是在这个群里说的，会先在群里说一句告别；私聊里必须给 group_id，"
                "没给会返回提示，不要猜群号。群友调用会被拒。",
                {
                    "group_id": {
                        "type": "string",
                        "description": (
                            "要退的 QQ 群号；在当前群里说可以省略，"
                            "私聊里必须填，不要猜。"
                        ),
                    }
                },
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
        # 双保险：schema 只给主人，这里再挡一次，防止别的路径把 provider 挂进来
        if not is_owner:
            return "只有主人能让本鱼退群。"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "（参数不是合法 JSON。）"
        if not isinstance(args, dict):
            return "（参数格式不对。）"

        gid = str(args.get("group_id") or "").strip()
        if not gid and str(session or "").startswith("group:"):
            gid = str(session).split(":", 1)[1]
        if not gid:
            return "要退哪个群？告诉本鱼群号（5~12 位数字），别猜。"
        if not GROUP_ID_RE.fullmatch(gid):
            return "群号看起来不对，要 5~12 位数字。"

        result = await self._leave(gid, str(session or ""))
        # 退群/回执已经由 app 发出去了：这一轮模型正文丢掉，别在已退的群里
        # 再补一句「好呀」—— 也避免模型把「退群」复述成一句空话。
        state = command_state if isinstance(command_state, dict) else {}
        state["done"] = True
        state["text"] = f"退群 {gid}"
        return result
