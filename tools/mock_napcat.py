"""本地假协议端（OneBot 11）：不装 QQ、不花钱，先把整条链路跑通。

用法（两个终端）：
    终端 1: python tools/mock_napcat.py
    终端 2: set DEEPSEEK_DRY_RUN=1 && set BOT_QQ=10001 && python run.py

它会模拟一条群里 @机器人 的消息，然后把你程序发回来的 send_group_msg
原样打印出来。看到打印 = 事件解析、触发判断、LLM 调用、发送 全都通了。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from aiohttp import web

BOT_QQ = 10001
GROUP_ID = 987654321
USER_ID = 123456789
PORT = int(os.environ.get("MOCK_PORT", "3001"))
# 测「加群公告 / 停止机器人」时可以关掉自动推消息：MOCK_PUSH=0
PUSH_EVENT = os.environ.get("MOCK_PUSH", "1") != "0"
# 假的群列表：get_group_list 会原样返回（群名带编号，方便看发给了几个群）
GROUPS = [
    {"group_id": 987654321, "group_name": "测试群一"},
    {"group_id": 987654322, "group_name": "测试群二"},
    {"group_id": 987654323, "group_name": "测试群三"},
]

FAKE_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "sub_type": "normal",
    "time": int(time.time()),
    "self_id": BOT_QQ,
    "user_id": USER_ID,
    "group_id": GROUP_ID,
    "message_id": 4242,
    "message_seq": 4242,
    "raw_message": f"[CQ:at,qq={BOT_QQ}] 你好，介绍一下你自己",
    "sender": {"user_id": USER_ID, "nickname": "小明", "card": "小明", "role": "member"},
    "message": [
        {"type": "at", "data": {"qq": str(BOT_QQ)}},
        {"type": "text", "data": {"text": " 你好，介绍一下你自己"}},
    ],
}


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    print(f"[mock] 客户端连上了: {request.remote}", flush=True)

    async def push_later() -> None:
        await asyncio.sleep(1.0)
        print("[mock] -> 推一条假消息（群里 @ 机器人）", flush=True)
        await ws.send_str(json.dumps(FAKE_EVENT, ensure_ascii=False))

    if PUSH_EVENT:
        asyncio.create_task(push_later())

    async for msg in ws:
        if msg.type != web.WSMsgType.TEXT:
            break
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            continue

        action = payload.get("action")
        echo = payload.get("echo")
        if action == "get_group_list":
            data = GROUPS
            print(f"[mock] <- 机器人要群列表：{len(data)} 个群", flush=True)
        elif action in {"get_login_info", "get_version_info"}:
            data = {"user_id": BOT_QQ, "nickname": "假机器人"} if "login" in action else {"app_name": "mock"}
        else:
            data = {"message_id": int(time.time())}
            params = payload.get("params", {})
            group = params.get("group_id")
            where = f"群 {group}" if group is not None else "私聊"
            print(f"[mock] <- 机器人调用 {action}（{where}）", flush=True)
            for seg in params.get("message", []):
                if seg.get("type") == "text":
                    print(f"[mock]    内容: {seg['data']['text']}", flush=True)

        await ws.send_str(
            json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo}, ensure_ascii=False)
        )

    print("[mock] 客户端断开了", flush=True)
    return ws


async def health(_: web.Request) -> web.Response:
    return web.Response(text="mock onebot running")


def main() -> None:
    app = web.Application()
    app.router.add_get("/", ws_handler)
    app.router.add_get("/onebot/v11/ws", ws_handler)
    app.router.add_get("/health", health)
    print(f"[mock] 监听 ws://127.0.0.1:{PORT} （Ctrl+C 退出）", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
