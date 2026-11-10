"""让主人在私聊里问她钱的事时，她心里真的有个数。

「今天花了多少」「余额还剩多少」有整句指令，能现查；但换个说法
（「今天烧了多少」「账户还够用吗」「钱还够吗」）就落到模型，模型手里
没有数字，只能现编一个。这里在**主人私聊**每一轮的动态上下文里塞一行：

- 今天本机花了多少（`data/usage.db`，今天 00:00 到现在，自然日）；
- 账户里还剩多少（最近一次余额快照，整个账户，含 DSH 等别的程序）。

只读、0 调用：花销来自本机账本，余额来自后台每 10 分钟记一次的快照，
这里不联网、不刷新余额。快照超过 `balance.STALE_SECONDS` 就标明
「可能不是最新」，不假装实时。

只进主人私聊：群聊、别人私聊一律不带。账户余额是账户级信息，
群友问「今天花了多少」仍走 `spendtool.today_spend`（只报本机，不带账户）。
也不进 system prompt —— 数字每轮都在变，进去就会让前缀缓存每轮失效。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .balance import STALE_SECONDS, fmt_money
from .dailyreport import money

log = logging.getLogger(__name__)

# 账户余额低于这个数，提醒主人一句（和 app._balance_reply 的阈值一致）
LOW_BALANCE = 5.0


def _spend_part(usage: Any, start: float, now: float) -> str:
    """今天本机花了多少。账本没开就什么都不说，别报一个假 0。"""
    if usage is None or not getattr(usage, "enabled", False):
        return ""
    stats = usage.summary_range(start, now + 1)
    calls = int(stats.get("calls") or 0)
    cost = float(stats.get("cost") or 0)
    tool_calls = int(stats.get("tool_calls") or 0)
    vision_cost = float(stats.get("vision_cost") or 0)
    if not calls and not tool_calls and vision_cost <= 0:
        return "今天本鱼还没花钱"
    text = f"今天本鱼花了 {money(cost)}"
    if calls:
        text += f"（{calls} 次调用）"
    return text


def _balance_part(balance: Any, now: float) -> tuple[str, bool]:
    """账户余额还剩多少。返回 (那句话, 是不是不多了)。"""
    if balance is None or not getattr(balance, "available", False):
        return "", False
    if not getattr(balance, "enabled", True):
        return "账户余额没在后台观测（发「余额」能现查）", False
    last = balance.last_snapshot()
    if not last:
        return "账户余额还没观测到", False
    total = float(last.get("total") or 0)
    try:
        stamp = float(last.get("ts") or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    when = datetime.fromtimestamp(stamp).strftime("%H:%M") if stamp else "刚才"
    tail = f"{when} 看的"
    if stamp and now - stamp > STALE_SECONDS:
        tail += "，可能不是最新"
    low = total < LOW_BALANCE
    return f"账户还剩 {fmt_money(total, 2)}（{tail}，整个账户，含 DSH 等）", low


def _spend_safe(usage: Any, start: float, now: float) -> str:
    """花销拼装失败（账本被锁/被别的工具占着）不能拖垮这一轮回复。"""
    try:
        return _spend_part(usage, start, now)
    except Exception:  # noqa: BLE001 - 少报一句，总比整条消息发不出去好
        log.debug("今天的花销没算出来，这轮不带", exc_info=True)
        return ""


def _balance_safe(balance: Any, now: float) -> tuple[str, bool]:
    try:
        return _balance_part(balance, now)
    except Exception:  # noqa: BLE001
        log.debug("余额没读出来，这轮不带", exc_info=True)
        return "", False


def owner_money_note(
    usage: Any, balance: Any, now: float | None = None
) -> str:
    """主人私聊里的「你自己的账」那一行；没得可说就返回空串。

    `usage` / `balance` 都允许是 None（账本没开、没配 key、演练模式），
    能说多少说多少，不编数字。
    """
    now = time.time() if now is None else float(now)
    moment = datetime.fromtimestamp(now)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    spend = _spend_safe(usage, start, now)
    account, low = _balance_safe(balance, now)
    parts = [part for part in (spend, account) if part]
    if not parts:
        return ""

    if low:
        advice = "余额不多了，聊到钱或充值时提醒主人一句；别的时候不主动报数"
    else:
        advice = "主人问起就照实说、别现编；平时不主动报数"
    if account:
        advice += "；账户余额只对主人说，群里别提"
    return (
        "（你自己心里有数："
        + "；".join(parts)
        + "。"
        + advice
        + "。）"
    )
