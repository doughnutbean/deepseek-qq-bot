"""模型自己调用的好友工具：现在到底加没加上、还能不能加。

为什么要有这个工具：好友申请是**自动批**的（看开关、档位、好感、当天额度），
她手上没有"同意"按钮。以前被问「你加我了吗」「能加你吗」，她只能凭印象回
「本鱼点不了同意」——可实际上申请可能已经被自动通过了（2026-11-01 实锤：
星风酱的申请 16:37 自动通过，她 16:36 还在说"点不了"）。

工具给她一个真数据源，别再瞎猜。

安全边界：
- 只读，不改任何状态；
- 汇总里给"今天通过几个/上限"，不暴露档位、好感这些内部数值；
- 单个查询只回答"是不是好友、什么时候加的"。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .tool_spec import make_tool

# QQ 自带的设备账号，不算"她加的人"
_DEVICE_NICKS = {"我的电脑", "我的手机", "我的Pad", "我的平板"}


class FriendQueryTools:
    """好友状态查询：schema + run，和 GroupTools/LiveTools 同一套接口。"""

    NAMES = {"friend_check"}

    def __init__(self, friends: Any) -> None:
        self.friends = friends

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            make_tool(
                "friend_check",
                "查好友状态（只读）。有人问「你加我了吗」「加上了吗」「还能加好友吗」"
                "「你今天加了几个」时调用，别凭印象答。"
                "不传 qq：返回汇总（好友申请开关、今天已通过几个、好友昵称）。"
                "传 qq：只回答这个号是不是好友、什么时候加的。"
                "结果里没有的号就是还没加上，别编「加好了」。",
                {
                    "qq": {
                        "type": "string",
                        "description": "要查的 QQ 号；不填 = 看汇总。",
                    }
                },
            )
        ]

    async def run(
        self, name: str, arguments: str = "{}", turn: Any = None, **kwargs: Any
    ) -> str:
        if turn is not None:
            kwargs = turn.as_ctx()
        if name not in self.NAMES:
            return json.dumps({"ok": False, "error": f"未知工具 {name}"}, ensure_ascii=False)
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        qq = str(args.get("qq") or "").strip()
        if qq:
            return self._one(qq)
        return self._summary()

    # ---------- 具体查询 ----------

    def _stat(self) -> dict[str, Any]:
        getter = getattr(self.friends, "_stat", None)
        if not callable(getter):
            return {}
        try:
            return getter() or {}
        except Exception:  # noqa: BLE001 - 读额度失败不影响回答
            return {}

    def _used(self) -> int:
        return int(self._stat().get("approved") or 0)

    def _cap(self) -> int:
        return int(getattr(self.friends, "accept_cap", 0) or 0)

    def _on(self) -> bool:
        enabled = getattr(self.friends, "enabled", None)
        return bool(enabled()) if callable(enabled) else False

    def _one(self, qq: str) -> str:
        getter = getattr(self.friends, "get", None)
        item = getter(qq) if callable(getter) else None
        if item:
            since = item.get("since")
            when = time.strftime("%m-%d %H:%M", time.localtime(since)) if since else ""
            nick = str(item.get("nick") or "")
            who = f"{nick}（{qq}）" if nick else qq
            return f"{who} 已经是好友" + (f"，{when} 加的。" if when else "。")

        if self._on():
            cap, used = self._cap(), self._used()
            left = max(0, cap - used) if cap else 0
            tail = f"，今天还能加 {left} 个" if left else "，但今天的额度用完了"
            return (
                f"{qq} 还不是好友。好友申请现在开着{tail}；"
                "他再发一次申请会按规矩判（熟不熟、好感）。"
            )
        return f"{qq} 还不是好友。好友申请现在是关着的，他发申请也不会通过。"

    def _summary(self) -> str:
        head = "好友申请：" + ("开" if self._on() else "关")
        cap, used = self._cap(), self._used()
        if self._on() and cap:
            head += f"（今天已通过 {used}/{cap} 个）"

        allget = getattr(self.friends, "all", None)
        people = list(allget().values()) if callable(allget) else []
        named = [
            str(p.get("nick") or "")
            for p in people
            if p.get("nick") and str(p.get("nick")) not in _DEVICE_NICKS
        ]
        if not named:
            return head + "；还没加过谁"
        tail = f"；好友 {len(people)} 个：" + "、".join(named[:8])
        if len(named) > 8:
            tail += " 等"
        return head + tail


__all__ = ["FriendQueryTools"]
