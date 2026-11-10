"""OneBot 11 正向 WebSocket 客户端。

协议端（SnowLuma）是服务端（websocketServers，默认 3001），本程序是客户端连上去。
一条连接同时承载：协议端推上来的事件 + 我们发下去的动作（按 echo 配对回应）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotError(RuntimeError):
    """动作被协议端明确拒了（status=failed / retcode≠0）。

    和"连接断了"不是一回事：链路还在，只是这一条没被接受 —— 比如 QQ 侧
    网络异常（retcode=1200 + EventChecker Failed）、被风控、参数不对。
    这种回执以前会被当成成功（`call` 只把链路错误变成异常），于是日报
    记了「已发」其实没发出去。现在发送类动作一律抛出来，由调用方决定
    重试还是报错。
    """

    def __init__(self, action: str, payload: dict[str, Any]) -> None:
        self.action = action
        self.payload = payload
        self.retcode = payload.get("retcode")
        self.message = str(payload.get("message") or payload.get("wording") or "").strip()
        detail = self.message.splitlines()[0][:300] if self.message else "没有说明"
        super().__init__(f"{action} 失败：retcode={self.retcode} {detail}")


def _is_failed(payload: dict[str, Any]) -> bool:
    """协议端的回执算不算失败。

    status=failed 一定失败；老实现只给 retcode，所以退一步看 retcode。
    retcode 缺省当成功（部分极简实现不返回它）。
    """
    if not isinstance(payload, dict):
        return True
    if payload.get("status") == "failed":
        return True
    code = payload.get("retcode")
    if code is None:
        return False
    try:
        return int(code) != 0
    except (TypeError, ValueError):
        return True


def ensure_ok(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """回执不是 ok 就抛 OneBotError，否则原样返回。"""
    if _is_failed(payload):
        raise OneBotError(action, payload if isinstance(payload, dict) else {})
    return payload


class OneBot:
    def __init__(self, url: str, token: str = "", on_event: EventHandler | None = None) -> None:
        self.url = url
        self.token = token
        self.on_event = on_event
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self.connected = asyncio.Event()

    async def run(self) -> None:
        """永久重连循环。连接断了会自动退避重试。"""
        backoff = 1.0
        while True:
            try:
                await self._session_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络异常都要重连
                log.warning("与协议端的连接断开: %s", exc)
            finally:
                self.connected.clear()
                self._fail_pending()
            log.info("%.0f 秒后重连…", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                self.url, headers=headers, heartbeat=30, max_msg_size=8 * 1024 * 1024
            ) as ws:
                self._ws = ws
                self.connected.set()
                log.info("已连上 OneBot: %s", self.url)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._dispatch(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break

    def _dispatch(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("收到非 JSON 数据: %.200s", raw)
            return

        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(payload)
            return

        # 心跳等元事件直接忽略
        if payload.get("post_type") == "meta_event":
            return

        # 加好友/加群这类 request 事件也往下送：她在 app 里自己判断批不批
        if self.on_event and payload.get("post_type") in {"message", "message_sent", "request"}:
            asyncio.create_task(self._safe_handle(payload))

    async def _safe_handle(self, event: dict[str, Any]) -> None:
        assert self.on_event is not None
        try:
            await self.on_event(event)
        except Exception:  # noqa: BLE001 - 单个事件出错不能拖垮连接
            log.exception("处理事件出错: %s", event.get("raw_message"))

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20.0,
        *,
        check: bool = False,
    ) -> dict:
        """调用协议端 API，返回 {"status","retcode","data"}。

        check=True 时，协议端回 status=failed / retcode≠0 就抛 OneBotError。
        发送消息一定要开着：**"没抛异常"不等于"发出去了"**。
        """
        if self._ws is None or self._ws.closed:
            raise ConnectionError("还没连上协议端")

        echo = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send_str(json.dumps({"action": action, "params": params or {}, "echo": echo}))
        try:
            payload = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)
        if check:
            ensure_ok(action, payload)
        return payload

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("连接已断开"))
        self._pending.clear()

    # ---- 常用动作的薄封装 ----

    async def send_group(self, group_id: int, segments: list[dict]) -> dict:
        # check=True：被拒就抛 OneBotError，绝不假装发成功
        return await self.call(
            "send_group_msg", {"group_id": group_id, "message": segments}, check=True
        )

    async def send_private(self, user_id: int, segments: list[dict]) -> dict:
        # check=True：被拒就抛 OneBotError，绝不假装发成功
        return await self.call(
            "send_private_msg", {"user_id": user_id, "message": segments}, check=True
        )
