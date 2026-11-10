"""直播画面工具：让模型主动「再看一眼」正在陪看的直播间。

为什么要有
----------
直播画面原来只有关键词一条路：这句里出现「画面 / 在干嘛 / 看看」才抓。
换个说法（「你看错了吧，再看看」「现在呢」）就抓不到，她手里没图，就会
说「直播抓不了帧、根本看不到画面」——可她其实一直在后台抓最近 2 分钟的
帧（`BILI_LIVE_BUFFER_*`），只是这一轮没递给她。

所以把「看直播画面」做成工具：模型自己判断要不要重新看，抽一帧拼图、
先描述画面，再回答。它只读内存缓冲，不重新下载整场直播；有 8 秒冷却，
同一轮只做一次视觉。

只对**正在陪看**的会话开放（`llm.reply` 里按会话挂），群友也能问；
普通视频画面走 `bili_frames`。没有声音、也不是连续视频。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .tool_spec import make_tool

log = logging.getLogger(__name__)

DESCRIPTION = (
    "看**正在陪看的 B 站直播间**的画面：把后台最近抓到的几帧拼成一张图"
    "（需要读屏幕上的字时再附一张原帧），先看清楚再告诉你画面里在发生什么"
    "（谁、在做什么、游戏画面、屏幕上的字）。"
    "有人说「再看看」「你看错了吧」「现在呢」「画面里是什么」时用；不用带参数。"
    "你后台一直在抓最近的帧，调它不会重新下载整场直播。会花一次视觉调用，"
    "返回画面描述；抓不到会如实说。只对正在陪看的直播间有效，普通视频用 "
    "bili_frames。"
)


class LiveFrameTools:
    """一个很薄的 provider：schema + run，和其他工具同一套接口。"""

    def __init__(
        self,
        look: Callable[[str, bool], Awaitable[tuple[list[dict[str, Any]], str]]],
        describe: Callable[[list[dict[str, Any]], str, str], Awaitable[str]],
        watching: Callable[[str, bool], bool],
        title: Callable[[], str] | None = None,
        cooldown: float = 8.0,
    ) -> None:
        # look(session, owner) -> (图片块, 给模型看的说明)
        self._look = look
        # describe(图片块, 直播间标题, 用户在问什么) -> 画面描述
        self._describe = describe
        # watching(session, owner)：这个会话现在是不是在看直播
        self._watching = watching
        self._title = title or (lambda: "")
        self.cooldown = max(0.0, float(cooldown))
        self._last: dict[str, float] = {}

    def schemas(self) -> list[dict[str, Any]]:
        return [make_tool("bili_live_frames", DESCRIPTION)]

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        *,
        turn: Any = None,
        **kwargs: Any,
    ) -> str | None:
        if name != "bili_live_frames":
            return None
        ctx: dict[str, Any] = turn.as_ctx() if turn is not None else dict(kwargs)
        if ctx.get("catchup"):
            return "这是上线补收的旧消息，不补看直播画面。"
        if ctx.get("live_frames_seen"):
            return (
                "这一轮已经给你看过直播画面了（就在上面），直接按画面回答，"
                "不用再抓。"
            )
        session = str(ctx.get("session") or "")
        owner = bool(ctx.get("owner_live") or ctx.get("is_owner"))
        if not self._watching(session, owner):
            return (
                "现在没在陪看可以看画面的直播间；先「看直播 房间号」，"
                "再看画面。普通视频用 bili_frames。"
            )
        now = time.time()
        last = self._last.get(session, 0.0)
        if self.cooldown > 0 and now - last < self.cooldown:
            return "刚看过一眼，过几秒再抓；先按刚才那张画面说。"
        try:
            blocks, note = await self._look(session, owner)
        except Exception:  # noqa: BLE001 - 抓画面失败不能把这一轮打挂
            log.info("直播画面工具抓图失败", exc_info=True)
            blocks, note = [], ""
        if not blocks:
            return (
                "这次没抓到直播画面（可能刚开播、流断了，或后台还没攒够帧）；"
                "过几秒再试。别说自己没有画面功能，你只是这一下没抓到。"
            )
        self._last[session] = now
        if turn is not None:
            try:
                turn.services["live_frames_seen"] = True
            except (AttributeError, TypeError):
                pass
        title = ""
        try:
            title = str(self._title() or "")
        except Exception:  # noqa: BLE001 - 标题拿不到就空着
            title = ""
        question = str(ctx.get("ori_question") or ctx.get("text") or "").strip()
        try:
            desc = await self._describe(blocks, title, question)
        except Exception:  # noqa: BLE001 - 视觉失败不能把这一轮打挂
            log.info("直播画面描述失败", exc_info=True)
            desc = ""
        desc = str(desc or "").strip()
        if not desc:
            return "画面抓到了，但这次没看清；先按弹幕和标题说，别编画面。"
        head = f"《{title[:40]}》的直播画面" if title else "直播画面"
        return f"（{head}：{desc[:600]}）\n{note}"
