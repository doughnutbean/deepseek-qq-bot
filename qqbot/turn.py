"""这一轮对话的「上下文」，工具只认它。

为什么要有（2026-11-05 定的三条之一）：
以前 `llm.reply` 把整个业务状态塞进一个大字典，用 `provider.run(name, args, **ctx)`
一次性铺给所有工具；工具想拿什么就 `ctx.get("xxx")`。这样「工具循环」和「业务状态」
就焊在一起了 —— 将来想换一个 agent runtime（比如 DSH 那套），得把所有工具重写。

`Turn` 把这件事收窄成"一轮对话是谁在哪儿说的"：
- 会话、说话人、是不是主人、现在几点、原始问题、是不是上线补收……
- 外加一个 `services`：过渡期放跨服务的引用（live_tools、memory…），
  以后应逐步挪进各 provider 的构造函数。

用法（新代码）：
    async def run(self, name, arguments, turn: Turn | None = None, **legacy):
        ctx = turn.as_ctx() if turn is not None else legacy
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Turn"]


@dataclass
class Turn:
    """一轮对话的上下文。字段都是"这一轮"的事实，不放业务服务。"""

    session: str = ""            # group:123 / private:456
    user_id: Any = None          # 说话人（或请求发起人）
    speaker_uid: Any = None
    speaker_nick: str = ""
    is_owner: bool = False       # 私聊里的主人（改资料类工具只认它）
    owner_live: bool = False     # 主人本人（群里也算），直播工具认它
    now: float = field(default_factory=time.time)
    ts_start: float = field(default_factory=time.time)
    ori_question: str = ""       # 原始问句，搜索/评估要用
    catchup: bool = False        # 上线补收的旧消息：写操作一律不给
    task: str = "chat"
    turn_id: str = ""
    text: str = ""               # 本轮正文（去标记后）
    model: str = ""
    # 过渡期：跨服务引用和零碎状态（逐步搬进构造函数）
    services: dict[str, Any] = field(default_factory=dict)
    # 造这个 Turn 时的原始字典（迁移期用：工具里 `ctx.get("xxx")` 还能work）
    _ctx: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    # ---------- 和旧字典互转 ----------
    # 迁移期工具内部还在用 ctx.get("xxx")，这两个方法让两边能共存，
    # 等某个 provider 迁完了就删掉它对应的键。

    def as_ctx(self) -> dict[str, Any]:
        """摊平成老接口用的字典（含 services 和原始键）。"""
        ctx: dict[str, Any] = dict(self._ctx or {})
        ctx.update(self.services)
        ctx.update(
            {
                "session": self.session,
                "user_id": self.user_id,
                "speaker_uid": self.speaker_uid,
                "speaker_nick": self.speaker_nick,
                "is_owner": self.is_owner,
                "owner_live": self.owner_live,
                "now": self.now,
                "ts_start": self.ts_start,
                "ori_question": self.ori_question,
                "catchup": self.catchup,
                "task": self.task,
                "turn_id": self.turn_id,
                "text": self.text,
                "model": self.model,
            }
        )
        ctx["turn"] = self
        return ctx

    @classmethod
    def from_ctx(cls, ctx: dict[str, Any]) -> "Turn":
        """从老的大字典造一个 Turn（`llm.reply` 每轮调一次）。"""
        source = ctx if isinstance(ctx, dict) else {}
        data = dict(source)
        turn = cls(
            session=str(data.get("session") or ""),
            user_id=data.get("user_id") or data.get("speaker_uid"),
            speaker_uid=data.get("speaker_uid"),
            speaker_nick=str(data.get("speaker_nick") or ""),
            is_owner=bool(data.get("is_owner")),
            owner_live=bool(data.get("owner_live") or data.get("is_owner")),
            now=float(data.get("now") or time.time()),
            ts_start=float(data.get("ts_start") or time.time()),
            ori_question=str(data.get("ori_question") or ""),
            catchup=bool(data.get("catchup")),
            task=str(data.get("task") or "chat"),
            turn_id=str(data.get("turn_id") or ""),
            text=str(data.get("text") or ""),
            model=str(data.get("model") or ""),
        )
        for key, value in data.items():
            if key not in cls.__dataclass_fields__:
                turn.services[key] = value
        # 留住原始字典：老工具读的新键（比如后面才 setdefault 的 tools_used）
        # 也能通过 as_ctx() 看到
        turn._ctx = source
        return turn
