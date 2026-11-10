"""群友问「今天花了多少钱」时，她查自己账本的只读工具。

这个工具只读 `data/usage.db`，本身不花 token/搜索费，但**不能一直挂在模型手上**：

- 平时不给她这个工具，免得她把「今天吃了多少」「这个多少钱」当成查账；
- 只有群里有人问「今天花了多少」这类话时，才把 schema 递过去；
- 执行前再验一次「群聊 + 查账问句」，两道路闸都过了才读账本。

判定是纯本地的（0 token），和主人的查账指令共用一套规则（`is_spend_question`），
避免群友问法认得出、主人指令却认不出（或反过来）。

口径和主人私聊里的「今天花了多少」一样：**今天 00:00 到现在**的自然日，
不是最近 24 小时；只算这个机器人自己的 `usage.db`，不含 DSH / 别的程序。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from .dailyreport import money

# 「今天花了多少」= 今天 00:00 到现在（自然日）。
# 只认句尾的“多少”：「今天花了多少心思」不是查账。
_SPEND_MONEY = re.compile(r"(?:花|花费|花销|消费|支出|花掉|流水|开销)")
_SPEND_ASK_TAIL = re.compile(r"(?:多少(?:钱|米|块)?|几块钱?|几毛|几钱|什么数|啥数)$")
_SPEND_BOOK_TAIL = re.compile(r"(?:账本|账单|流水|开销|花销)$")
_SPEND_TODAY = re.compile(r"(?:今天|今日|今儿)")
# 句尾语气词，先摘掉再判
_SPEND_TAIL_NOISE = re.compile(r"(?:了|呀|啊|呢|吧|哦|嘛|哈|啦|捏|鸭|~|～|\s)+$")

# 查账问句都很短；长句里夹一句「今天花了多少钱买的键盘」不是查账
MAX_QUESTION = 24


def is_spend_question(text: str) -> bool:
    """这句话是不是在问「今天花了多少」。纯本地，认不出就当普通聊天。"""
    body = (text or "").strip("。！？!?~～…，,.；;、 ")
    if not body or len(body) > MAX_QUESTION:
        return False
    if not _SPEND_TODAY.search(body):
        return False
    tail = _SPEND_TAIL_NOISE.sub("", body).strip("。！？!?~～…，,.；;、 ")
    asks_money = bool(_SPEND_MONEY.search(tail)) and bool(_SPEND_ASK_TAIL.search(tail))
    asks_book = bool(_SPEND_BOOK_TAIL.search(tail))
    return asks_money or asks_book


def today_spend_text(usage: Any, now: float | None = None) -> str:
    """今天 00:00 到现在本机花了多少，拼成给她看的一小段话。

    和 `app.Bot._spend_reply` 用同一个 `usage.summary_range`，但只留群友
    需要的那几项：金额、调用次数、搜索/抓取次数。账户余额、DSH 之类
    「整个账户」的口径不往群里带。
    """
    now = time.time() if now is None else float(now)
    moment = datetime.fromtimestamp(now)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    stamp = f"{moment:%m-%d} 00:00 → {moment:%H:%M}"

    if usage is None or not getattr(usage, "enabled", False):
        return "（账本没开，本鱼算不出今天花了多少。）"

    stats = usage.summary_range(start, now + 1)
    calls = int(stats.get("calls") or 0)
    requests = int(stats.get("requests") or 0)
    search_calls = int(stats.get("search_calls") or 0)
    fetch_calls = int(stats.get("fetch_calls") or 0)
    vision_cost = float(stats.get("vision_cost") or 0)
    cost = float(stats.get("cost") or 0)

    if not calls and not search_calls and not fetch_calls and vision_cost <= 0:
        if requests:
            return (
                f"今天（{stamp}）本鱼发了 {requests} 次模型请求，"
                "但没记到 token，账本这边是 ¥0。"
            )
        return f"今天（{stamp}）本鱼一次都没调用，一分钱没花。"

    call_text = f"共 {calls} 次调用"
    if requests and requests != calls:
        call_text += f"（{requests} 次模型请求）"
    lines = [f"今天（{stamp}）本鱼花了 {money(cost)}，{call_text}。"]

    tool_bits: list[str] = []
    if search_calls:
        tool_bits.append(f"搜索 {search_calls} 次")
    if fetch_calls:
        tool_bits.append(f"抓取 {fetch_calls} 次")
    if tool_bits:
        tool_cost = float(stats.get("tool_cost") or 0)
        extra = f"（{money(tool_cost)}）" if tool_cost > 0 else ""
        lines.append("其中" + "、".join(tool_bits) + extra + "，已含在上面。")

    vision_calls = int(stats.get("vision_calls") or 0)
    if vision_cost > 0:
        lines.append(f"图形工具 {vision_calls} 次 {money(vision_cost)}，也含在上面。")

    lines.append("（只算本鱼自己的账本，不含别的程序。）")
    lines.append(
        "本鱼走的是协议端加本地工具这套，token 比同类省一大截，"
        "已经在好几个千人群里跑着；不过本鱼的价格不是部署肥鱼的普遍参考。"
    )
    return "\n".join(lines)


class SpendTools:
    """给模型用的 `today_spend`：只在群聊查账那一轮出现。"""

    NAMES = {"today_spend"}

    def __init__(self, usage: Any) -> None:
        self.usage = usage

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "today_spend",
                    "description": (
                        "查本鱼自己的账本：今天 00:00 到现在一共花了多少钱、调用了几次。"
                        "只有在群里有人问你「今天花了多少钱」「今天花销多少」这类"
                        "查账问题时才调用；普通聊天、别人聊钱、问别的东西都不要调用，"
                        "也不要主动报账。"
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def text(self, now: float | None = None) -> str:
        return today_spend_text(self.usage, now)

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        *,
        ori_question: str = "",
        session: str = "",
        turn: Any = None,
        **_kwargs: Any,
    ) -> str | None:
        if turn is not None:
            _kwargs = turn.as_ctx()
            ori_question = _kwargs.get('ori_question')
            session = _kwargs.get('session')
        if name not in self.NAMES:
            return None
        # 第二道路闸：就算这个工具被挂进了别的回合，也只在「群聊 + 查账问句」时读账。
        if not str(session).startswith("group:") or not is_spend_question(ori_question):
            return "（这个工具只在群里有人问「今天花了多少钱」时才用。）"
        return self.text()
