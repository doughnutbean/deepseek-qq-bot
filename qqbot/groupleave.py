"""退群：主人说一句，她先告别，再真的离开。

和「把本群移出白名单」不是一回事：那个只是不再回话，人还在群里；
这里会调 OneBot 的 ``set_group_leave``，退出去就回不来了。

所以只留一条最窄的路：

- 只认主人（app 里验 QQ 号）；
- 只认整句命令（``commands._group_leave_command``）或模型工具
  （``group_tools.GroupTools``），聊天里说「退群的人」不算；
- 问句不认（「退群？」「能不能退群？」是商量，不是命令）；
- 群里不带群号 = 退当前群；私聊必须带群号，app 会反过来问，绝不猜。

真正的 OneBot 调用放在这里，0 token；告别话术也是固定的（``LEAVE_TEXT``），
不调模型。退成功后由 app 清队列、清潜水/静默、在 ``group_invites.json``
里记一笔「退过这个群」。
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# 退之前在这个群说的一句；固定文案，0 token，也方便测试。
LEAVE_TEXT = "本鱼退群啦，拜拜～"

# QQ 群号：5~12 位数字（和群白名单指令的口径一致）
GROUP_ID_RE = re.compile(r"^\d{5,12}$")


def normalize_group_id(value: Any) -> str:
    """群号只收 5~12 位数字；别的一律当没填。"""
    text = str(value or "").strip()
    return text if GROUP_ID_RE.fullmatch(text) else ""


async def leave_group(
    onebot: Any, group_id: str, *, timeout: float = 20.0
) -> tuple[bool, str]:
    """真的退出群。返回 ``(成功, 失败原因)``，失败原因已经压成一句人话。"""
    gid = normalize_group_id(group_id)
    if not gid:
        return False, "群号看起来不对（要 5~12 位数字）"
    try:
        await onebot.call(
            "set_group_leave",
            {"group_id": int(gid), "is_dismiss": False},
            timeout=timeout,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001 - 退群失败要把原因收成人话
        log.warning("退群失败 %s: %s", gid, exc)
        return False, _human_error(exc)
    log.info("已退出群 %s", gid)
    return True, ""


def _human_error(exc: Exception) -> str:
    """异常 → 一句能直接发出去的话；不把栈和内部路径甩给模型。"""
    detail = str(exc or "").strip()
    if not detail:
        return "协议端没给原因"
    return detail.splitlines()[0][:120]
