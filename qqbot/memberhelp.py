"""群友发「指令」时看到的精简清单。

主人发「指令」走 `Bot._commands_text()`（`commands.HELP_SECTIONS` 全量，含功能
开关状态）；群友只该看到他们真能用的几条，不能把主人的调参指令抄过去。

本地拼，0 token。清单里每一条都必须真的能用：
- 「闭嘴 / 安静一下」：`_handle_quiet_command`，只认冲她说的；
- 「可以说话了」：同一条路径，群正在安静时生效；
- 「别@我 / 别点我」：`AtGate` 本地记 30 天，不调模型；
- 「好感度」：从 `familiar.py` 的句数/天数现算，只认四个整句；
- 「状态」：公开版在线/模型/记忆/心情/群状态，不报花费和黑名单；
- 「指令」：就是这份清单本身。

主人的清单只有一份（`commands.py::HELP_SECTIONS`）；群友清单也放这里一份，
别再在 `app.py` 里手写第二份。
"""

from __future__ import annotations

import time
from typing import Any

from .familiar import Person

# 触发词：只认这两个字，前后标点/空白忽略
HELP_WORD = "指令"
_TRAIL = "。！？!?~～…，,.；;、 "

# 群友查询：**只认整句**，错一个字就当普通聊天。
# 别用 search / 同义词 —— 这是群友天天会拿来试的词，宽松一点就会误伤聊天。
# (kind, section)：favorability 只读熟人数据；status 只给公开版。
MEMBER_QUERIES: dict[str, tuple[str, str]] = {
    "状态": ("status", "all"),
    "本鱼状态": ("status", "all"),
    "大肥鱼状态": ("status", "all"),
    "自检": ("status", "all"),
    "你现在的状态": ("status", "all"),
    "在线状态": ("status", "online"),
    "连接状态": ("status", "online"),
    "模型状态": ("status", "online"),
    "记忆状态": ("status", "memory"),
    "心情状态": ("status", "mood"),
    "情绪状态": ("status", "mood"),
    "本群状态": ("status", "groups"),
    "群状态": ("status", "groups"),
    "好感度": ("favorability", ""),
    "查好感度": ("favorability", ""),
    "我的好感度": ("favorability", ""),
    "好感度多少": ("favorability", ""),
}


def is_help_command(text: str) -> bool:
    """这句话是不是单独发了「指令」两个字。"""
    return _query_body(text) == HELP_WORD


def _query_body(text: str) -> str:
    return (text or "").strip().strip(_TRAIL)


def parse_member_query(text: str) -> tuple[str, str] | None:
    """群友查询只认整句；认不出返回 None（当普通聊天）。"""
    return MEMBER_QUERIES.get(_query_body(text))


def is_favorability_command(text: str) -> bool:
    """是不是单独发了好感度查询。只认四个整句，前后标点忽略。"""
    got = parse_member_query(text)
    return got is not None and got[0] == "favorability"


def _signed(value: int) -> str:
    return f"{value:+d}"


def _last_seen(ts: float, now: float) -> str:
    if not ts:
        return "还没记到"
    days = int((float(now) - float(ts)) // 86400)
    if days <= 0:
        return "今天 " + time.strftime("%H:%M", time.localtime(ts))
    if days == 1:
        return "昨天 " + time.strftime("%H:%M", time.localtime(ts))
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def favorability_text(
    person: Person | None,
    affection: dict[str, Any] | None = None,
    *,
    now: float | None = None,
    stale_days: int = 30,
) -> str:
    """群友看到的回执：熟悉度 + 好感度两条轴。只读，不写任何状态。

    熟悉度只涨不跌（认识多久）；好感度会升会降（最近怎么相处）。
    以前把熟悉度叫「好感度」，群友会以为刷屏就能涨，所以分开写。
    """
    moment = time.time() if now is None else float(now)
    score = person.familiarity if person is not None else 0
    aff = affection or {}
    try:
        aff_score = int(aff.get("score") or 0)
    except (TypeError, ValueError):
        aff_score = 0
    aff_name = str(aff.get("name") or "平常")
    head = (
        f"💗 熟悉 {score}/100（{person.tier.name if person is not None else '还没聊过'}）"
        f"｜好感 {_signed(aff_score)}（{aff_name}）"
    )
    if person is None:
        return (
            head + "\n"
            "本鱼还没记到你在群里说过话；只算群聊，私聊的不算。先聊几句再来问。"
        )
    lines = [
        head,
        f"在群里一起聊过 {person.msgs} 句、{person.days} 天；"
        f"最近一次：{_last_seen(person.prior_ts or person.last_ts, moment)}。",
    ]
    if person.is_stale(moment, stale_days, prior=True):
        lines.append("（一个多月没见了，熟悉度还在，但别装太熟。）")
    nxt = person.next_tier
    if nxt is None:
        lines.append("→ 已经是最高档，慢慢处。")
    else:
        need_msgs = max(0, nxt.min_msgs - person.msgs)
        need_days = max(0, nxt.min_days - person.days)
        bits = []
        if need_msgs:
            bits.append(f"再聊 {need_msgs} 句")
        if need_days:
            bits.append(f"满 {need_days} 天")
        lines.append(
            "→ 升「" + nxt.name + "」还差：" + ("、".join(bits) or "马上就到") + "。"
        )
    lines.append("（熟悉度看聊了多久；好感看你最近怎么对她）")
    return "\n".join(lines)


def member_help_text(*, quiet_minutes: int = 5) -> str:
    """群友版清单：只列他们真能用的，压成几行，别抄主人的全量。"""
    lines = ["📋 群友能用（精简版；发「指令」看这份）"]
    if quiet_minutes > 0:
        lines.append(f"【安静】闭嘴 / 安静一下（{quiet_minutes} 分钟）｜可以说话了")
    lines += [
        "【免打扰】别接我的话 30 分钟 / 可以接我了（@ 仍回）",
        "【记忆】你记得我什么 / 忘掉第 2 条 / 别记我（私聊）",
        "【称呼】叫我老王 / 还是叫昵称（够熟悉才解锁）",
        "【权限/状态】我的权限｜状态｜好感度",
        "【推送/点名】关闭本群上下线推送｜别@我 / 别点我",
        "【上线通知】好友私聊「打开上线通知」",
        "主人的调参指令只对主人生效哦。",
    ]
    return "\n".join(lines)
