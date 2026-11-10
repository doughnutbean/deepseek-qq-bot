"""私聊里的「好友申请通知」和「拉群邀请」。

这两类东西在 NapCat 里经常被当成一条普通私聊推过来，而不是 OneBot 的
``request`` 事件：

- 好友申请：``raw_message`` 就是「请求添加你为好友」；
- 拉群邀请：可能是「来我的粉丝群」这种文字，也可能是群邀请卡片。

以前它们直接进了模型，模型就会顺着说「加好了哦」「马上去」——实际上
她什么都没做。这里只做**纯本地判断**，命中就交给 ``app`` 走固定流程：
要么真的去批（开关开着、够熟、最近还聊过、好感不为负），要么委婉拒绝。0 token。

好友申请的拒绝**不把责任推给主人**：说清原因（刚认识 / 太久没见 / 好感为负 /
今天批够了 / 本鱼现在不加），不要写成「主人说了」。
拉群邀请不再直接拒绝：先问清人数 / 活跃度 / 聊什么，再报给主人拍板
（见 ``groupinvites.py``）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 兜底拒绝话术：委婉 + 说明原因，不提主人。
DEFAULT_FRIEND_REQUEST_REFUSAL = "我们才刚认识嘛，再多聊两句吧。"

# 不同拒绝原因对应的话术。顺序无所谓，按关键词命中。
_FRIEND_REFUSALS: tuple[tuple[str, str], ...] = (
    ("不够熟", "我们才刚认识嘛，再多聊两句吧。"),
    ("最近", "好久没见啦，先多聊两句再申请吧。"),
    ("好感", "本鱼还在生你的气呢，先聊两句缓缓吧。"),
    ("今天批够", "今天本鱼有点忙不过来啦，明天再来申请吧。"),
    ("开关关着", "本鱼现在不加好友哦，先这样聊着吧。"),
    ("同意失败", "本鱼这边卡了一下，稍后再申请一次吧。"),
    ("没有 flag", "本鱼这会儿没接到申请，你再发一次吧。"),
)


# 「请求添加你为好友」「申请添加好友」「请求加你为好友」都算。
# 必须是整句：普通聊天里提到「他请求添加你为好友」不算，否则会误发拒绝。
# NapCat 有时会在后面带一句「附加消息：…」「验证信息：…」，那还是同一条申请，
# 照样在本地处理，别漏给模型（漏过去模型就会按提示词自由发挥）。
_FRIEND_NOTICE_HEAD_RE = re.compile(
    r"^\s*(?:对方)?(?:请求|申请)(?:添加|加)(?:你|您)?(?:为)?(?:好友|朋友)"
)

# 申请后面只允许跟系统自带的附言：「，附加消息：…」「验证信息：…」。
_FRIEND_NOTICE_TAIL_RE = re.compile(
    r"^(?:[。！!]\s*)?(?:[，,]\s*)?"
    r"(?:(?:附加|验证)(?:消息|信息)|附言|留言|备注)\s*[：:]\s*[^\n]{0,300}$"
)


# 拉群邀请：只认带动作的说法，光看到「群」不算。
_GROUP_INVITE_RES = (
    re.compile(r"邀请(?:你|您)?(?:加入|进入|进|加)(?:我的|这个|一下)?(?:群聊|粉丝群|群组|群)"),
    re.compile(r"(?:拉|邀请)(?:你|您)?(?:进|加入|进入)(?:我的|这个|一下)?(?:群聊|粉丝群|群组|群)"),
    re.compile(
        r"(?:来|进|加|加入)(?:我的|我们|这个|一下)?(?:粉丝群|群聊|群组|群)(?![了过])"
    ),
    re.compile(r"(?:加|进|来)(?:一下|一个)?(?:这个|那个|我的)?群(?:吧|呗|吗|呀|嘛|啊|哦)"),
    re.compile(
        r"(?:来|进|加|加入|邀请|帮).{0,10}粉丝群"
        r"|粉丝群.{0,10}(?:帮忙|活跃|暖场|来|进|加)"
    ),
    re.compile(r"群邀请|邀请你加入群聊|邀请您加入群聊|加入群聊|qun\.qq\.com|加群链接"),
    re.compile(r"(?:帮忙|帮|来|给).{0,8}(?:活跃气氛|暖场|撑场面)"),
)


def is_friend_request_notice(text: str) -> bool:
    """这条私聊是不是 QQ 的好友申请通知，而不是对方真的在说话。"""
    body = (text or "").strip()
    match = _FRIEND_NOTICE_HEAD_RE.match(body)
    if not match:
        return False
    rest = body[match.end():].strip()
    if rest in {"", "。", "！", "!", "…", "..."}:
        return True
    return bool(_FRIEND_NOTICE_TAIL_RE.match(rest))


def is_group_invite(text: str, segments: Any = None) -> bool:
    """这条私聊是不是在拉她进别的群。

    文字和邀请卡片都看；宁可不命中（后面还有提示词兜底），也别把普通
    聊天误判成邀请。
    """
    body = (text or "").strip()
    if body:
        # 「我不加群」「别进群」这种否定句不是邀请。
        if not re.search(r"邀请(?:你|您)", body) and re.search(
            r"(?:不|别|不要|不想|没|懒得)[^，。！？]{0,4}(?:加|进|来)[^，。！？]{0,2}群",
            body,
        ):
            return False
        if any(p.search(body) for p in _GROUP_INVITE_RES):
            return True

    return _segment_is_invite(segments)


def _segment_is_invite(segments: Any) -> bool:
    if not isinstance(segments, list):
        return False
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = str(seg.get("type") or "")
        if kind not in {"json", "xml", "share", "light_app", "group_notice"}:
            continue
        data = seg.get("data")
        raw = (
            json.dumps(data, ensure_ascii=False, default=str)
            if isinstance(data, (dict, list))
            else str(data or "")
        )
        low = raw.lower()
        if "group" in low and (
            "invite" in low or "邀请" in raw or "加群" in raw or "加入群聊" in raw
        ):
            return True
        if "邀请" in raw and "群" in raw:
            return True
    return False


def friend_request_refusal(config: Any, reason: str = "") -> str:
    """好友申请的拒绝话术：优先按原因给委婉说法，再退回配置兜底。

    配置里如果还留着旧的「主人说了……」也不采用——好友申请是她自己的判断，
    不把责任推给主人。
    """
    for keyword, text in _FRIEND_REFUSALS:
        if keyword in (reason or ""):
            return text
    text = str(getattr(config, "friend_request_refusal", "") or "").strip()
    if text and "主人" not in text:
        return text
    return DEFAULT_FRIEND_REQUEST_REFUSAL
