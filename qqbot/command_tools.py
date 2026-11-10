"""本地整句指令的模型工具：双保险的第二层。

第一层（0 token，永远先跑）
---------------------------
主人发一句话，`commands.parse()` 先按**整句**认；认出来就直接执行，
根本走不到模型——这条路一点 token 都不花。

第二层（这个模块）
------------------
本地没认出来的那一轮，才把 `run_local_command` 递给模型：

- 模型看到全部可用指令和**权限说明**，自己判断「主人这句话是不是在调我」；
- 判断是，就从清单里挑一条**完整指令**传回来；
- 工具把指令交回 `Bot._handle_owner_command()`，**仍旧由本地解析器执行**，
  模型只负责「判断要不要调、该调哪一条」，不能自己改参数、不能绕过解析；
- 执行完回执已经发出，模型这一轮的正文会被丢掉，不会重复说一遍。

权限（写在这里，也写进 schema，人能看见、模型能看见、执行时还会再验）
------------------------------------------------------------------
- 工具只给主人；群友那一轮连 schema 都拿不到。
- 只读、0 token：状态 / 今天花了多少 / 余额 / 能力清单 / 群梗 / 谐音 / 候选梗 /
  直播状态 / 待办清单。
- 改设置、删东西：只有主人能调；群友调会被 `_handle_owner_command` 直接拒。
- 群专用（本群静默/潜水/推送/群名单）在私聊里调，本地会回「这条只能在群里说」。
- 私聊专用（私聊黑名单、todo 清单）在群里调同理。
- 一轮最多执行一条，防止模型连着改两个设置。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from .commands import HELP_SECTIONS

log = logging.getLogger(__name__)

# 清单之外的常用指令：HELP_SECTIONS 只放最常用的，这几类没进去但确实能认。
_EXTRA_CATALOG: tuple[tuple[str, str], ...] = (
    (
        "功能开关",
        "打开风控模式 / 关闭风控模式 / 打开合并转发 / 关闭合并转发"
        " / 打开热梗 / 关闭热梗 / 打开群风格 / 关闭群风格"
        " / 打开群梗 / 关闭群梗"
        " / 打开说人话 / 关闭说人话 / 打开生气的设定 / 关闭生气的设定"
        " / 打开好友申请 / 关闭好友申请 / 打开自动拉黑 / 别自动拉黑"
        " / 打开跟读 / 关闭跟读 / 可以@人了 / 别@人了"
        " / 让她自己@ / 别让她自己@ / 打开上下线推送 / 关闭上下线推送"
        " / 打开找肥鱼 / 关闭找肥鱼",
    ),
    (
        "群名单（带群号）",
        "把群 123456789 加白名单 / 把群 123456789 移出白名单"
        " / 把群 123456789 加黑名单 / 把群 123456789 移出黑名单",
    ),
    ("表情单图/标签", "多发这个 / 少发委屈的"),
    ("直播陪看", "打开直播陪看 / 看直播 12345 / 看克里斯直播 / 看看弹幕 / 不看了 / 直播状态"),
    ("全部指令", "指令 / 指令 全部"),
)


def catalog_text() -> str:
    """给她看的指令清单：一行一类，说法照抄就能被本地 parser 认出来。"""
    rows = [(label, examples) for label, examples, _note in HELP_SECTIONS]
    rows.extend(_EXTRA_CATALOG)
    return "\n".join(f"【{label}】{examples}" for label, examples in rows)


# 权限写在工具描述里：这段是**契约**，改权限先改这里，再改测试。
PERMISSIONS = (
    "权限（执行前还有两道路闸，越权直接拒）：\n"
    "- 只给主人；群友拿不到 schema，也调不了。\n"
    "- 只读、0 token：状态/自检、今天花了多少、余额/对账、"
    "能力、群梗/谐音/候选梗、私聊黑名单、机器人名单、"
    "直播状态/弹幕、待办清单。\n"
    "- 会改设置或删东西，主人明确要改时才调：回复档位、说话长度、称呼、"
    "表情权重、功能开关、思考模式、主动程度、群白/黑名单、本群静默/潜水/"
    "上下线推送、私聊黑名单、待办增删、梗增删、恢复默认。\n"
    "- 退群是不可逆操作：只有主人明确说「退群」时才调；私聊里没给群号就"
    "先问主人，别猜群号。\n"
    "- 只在群里有效：本群静默/解除、本群潜水/结束、本群上下线推送、"
    "带「本群」的群名单；在私聊里调只回「这条只能在群里说」。\n"
    "- 只在私聊有效：私聊黑名单、todo 清单；群里同理。\n"
    "- 余额是账户级信息：在群里调只回一句，正文由她私聊发给主人。\n"
    "- 缺目标（QQ 号/群号/昵称/梗）就先问主人，别猜、别编。"
)


class CommandTools:
    """一个很薄的提供者：schema + run，和 SearchTools/LiveTools 同一套接口。"""

    NAMES = {"run_local_command"}
    # 本地指令大全确实长（约 2.3k 字）；这是唯一一个显式申请超长描述的工具，
    # 别的工具不许学（见 docs/工具规范.md「例外」）。
    description_limit = 2400

    def __init__(
        self,
        run_command: Callable[[str, bool, str], Awaitable[bool]],
    ) -> None:
        # run_command(text, is_group, group_id) -> 是否真的执行并回了执
        self._run_command = run_command

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "run_local_command",
                    "description": (
                        "把主人这句话换成一条**完整的本地整句指令**，交回本地指令"
                        "系统执行。这是第二层保险：本地整句没认出来的那一轮才会"
                        "有这个工具；能认出来的轮次根本走不到这里。\n\n"
                        + PERMISSIONS
                        + "\n\n调用规则：\n"
                        "- 只有主人明确在调她（改设置、开/关功能、查状态/余额/账本、"
                        "记/删梗、群管理）才调；普通聊天、吐槽、问天气一律不调。\n"
                        "- command 只传一条完整指令，用下面清单里的说法；"
                        "不要带解释、不要拆成多条、不要自己发明说法。\n"
                        "- 恢复默认、删梗、清空 todo、私聊黑名单、退群这类不可逆操作，"
                        "主人没说清就别调；宁可先问一句。\n"
                        "- 一轮最多执行一条；执行后回执已经发出，不用再重复。\n\n"
                        "可调用指令（说法照抄；数字/名字/梗换成真实的）：\n"
                        + catalog_text()
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": (
                                    "一条完整的本地指令，例如「少说话」"
                                    "「以后叫我老板」「把本群加白名单」。"
                                    "不要传解释、不要传半句。"
                                ),
                            }
                        },
                        "required": ["command"],
                    },
                },
            }
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
        # 第二道路闸：schema 只给主人，这里再挡一次
        if not is_owner:
            return "（这条工具只有主人能用；群友的只读查询不走这里。）"
        state = command_state if isinstance(command_state, dict) else {}
        if state.get("done"):
            return "（这一轮已经执行过一条指令了，不要连着改第二个设置；等主人下一句。）"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "（参数不是合法 JSON。）"
        if not isinstance(args, dict):
            return "（参数格式不对。）"
        text = str(args.get("command") or "").strip()
        if not text:
            return "（没有给出指令内容。）"
        if "\n" in text or "\r" in text or len(text) > 60:
            return "（只传一条完整指令，别带解释或换行。）"

        is_group = str(session or "").startswith("group:")
        group_id = str(session or "").split(":", 1)[1] if is_group else ""
        ok = await self._run_command(text, is_group, group_id)
        if not ok:
            return (
                "（本地指令系统没认出来这条。不要自己改参数硬凑；"
                "换一种说法，或者直接告诉主人这条没认出来。）"
            )
        state["done"] = True
        state["text"] = text
        log.info("模型判断是本地指令并已执行：%s", text[:40])
        return "（已执行，回执已经发出去了；这一轮不要再重复回执。）"
