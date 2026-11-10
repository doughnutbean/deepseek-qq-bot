"""直播域：发弹幕、写操作回执守卫。

从 app.py 里拆出来的第一批（2026-11-05）。混进 Bot 之后 `self` 还是同一个对象，
所以方法体一行没改；拆出来的意义是：这块最近改得最多，单独一个文件好读、好测。

后续要拆的领域：qzone（说说）、memory（记忆自助）、group（群管理）、admin（调参）。
规矩：只搬代码、不改行为；每搬一块就跑全量测试。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..live_danmaku import DanmakuError
from ..textfmt import ack as _ack

log = logging.getLogger(__name__)

__all__ = ["LiveDomain"]


class LiveDomain:
    """直播间相关的那几个动作（发弹幕 + 回执校验）。"""

    # 「发个弹幕 克里斯笑话」这类：要的是"挑一句"，不是把这四个字发出去
    _DANMAKU_META = ("克里斯笑话", "笑话", "段子", "口头禅", "来一句", "随便来", "随便说")
    # 声称"发过了"的说法（写操作回执不该由模型自己编）
    _DANMAKU_CLAIM = re.compile(
        r"(?:本鱼|我)[^。！？\n]{0,8}(?:发了|发啦|发出去了|发好了|已经发)"
    )

    def _guard_write_claim(self, answer: str) -> str:
        """她说「发啦」但其实没发时，补一句实话。

        2026-11-04 实锤：群里说「在弹幕里发一句克里斯笑话」，她在本地讲了个笑话
        就回「本鱼发啦」，直播间一条都没有。提示词里已经写了"回执必须来自工具
        结果"，这条是最后一道网：**两分钟内没有成功发送记录，就不许声称发了**。
        """
        text = str(answer or "")
        if "弹幕" not in text or not self._DANMAKU_CLAIM.search(text):
            return text
        if time.time() - float(getattr(self, "_danmaku_ok_at", 0.0) or 0.0) <= 120:
            return text  # 真发过，随她说
        log.warning(
            "写操作回执不实：回复声称发了弹幕，但最近没有成功发送记录：%s", text[:80]
        )
        return text + "\n（等等，本鱼刚才是嘴上说说，弹幕没真发出去。）"

    def _danmaku_text(self, text: str) -> str:
        """把主人给的内容归一：元指令（"克里斯笑话"）→ 空串（改从语料挑）。"""
        text = str(text or "").strip()
        if not text:
            return ""
        if len(text) <= 10 and any(word in text for word in self._DANMAKU_META):
            return ""
        return text

    async def _live_danmaku_once(self, text: str) -> dict[str, Any]:
        """发一条：有内容按内容发，没内容从语料挑。返回 send 的结果 dict。"""
        room = self.bili_live.room_id
        target = self._danmaku_text(text)
        try:
            if target:
                result = await self.live_danmaku.send(room, target)
            else:
                result = await self.live_danmaku.send_picked(room)
        except DanmakuError as exc:  # 太长 / 空内容
            result = {"ok": False, "code": None, "message": str(exc)}
        # 记账：写操作回执校验靠它（_guard_write_claim）
        if result.get("ok"):
            self._danmaku_ok_at = time.time()
        return result

    async def _live_danmaku_send(self, arg: dict | None = None) -> str:
        """往正在陪看的直播间发一条弹幕（主人限定）。内容从克里斯语料挑或按主人说的发。"""
        room = self.bili_live.room_id
        if not room:
            return _ack("发弹幕", "现在没在陪看直播，先告诉本鱼看哪个直播间。")
        result = await self._live_danmaku_once(str((arg or {}).get("text") or ""))
        if result.get("ok"):
            return _ack("发弹幕", f"发到直播间 {room} 了：「{result.get('text')}」")
        return _ack("发弹幕", f"没发出去：{result.get('message')}")

    async def _live_danmaku_from_tool(self, text: str = "") -> str:
        """模型调 live_send_danmaku 时走这里：返回给模型看的短结果。"""
        room = self.bili_live.room_id
        if not room:
            return "现在没在陪看直播，先让主人说看哪个直播间。"
        result = await self._live_danmaku_once(text)
        if result.get("ok"):
            return f"发了：「{result.get('text')}」（直播间 {room}）"
        return f"没发出去：{result.get('message')}"
