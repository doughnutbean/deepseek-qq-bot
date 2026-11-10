"""主人的自然语言调参：说人话就行，不用记指令。

只走本地规则（0 token）：同义词、语序、问句口吻、前后缀都尽量认。
认不出就当普通聊天 —— 不再调用模型判意图，省掉每次指令判定的小额花费。

**改这里的规则之前先读 `指令规范.md`**（成对、整句、方向不许做反、管线顺序）。
改完必须跑：

    python tools/check_command_spec.py      # 文档里的说法 + 规范里的成对词表
    python tools/test_command_symmetry.py   # 认不认、误不误判、走不走 app
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .bili_live import extract_room_id
from .live_history import parse_day
from .spendtool import is_spend_question
from .stickermanual import split_command as _split_manual_command

# 潜水不写时长时默认多久（分钟）
DIVE_DEFAULT_MINUTES = 60
# 一次最多潜 24 小时，防止写错数字变成永久静音
DIVE_MAX_MINUTES = 24 * 60

# 情绪 / 场景标签，用来认「少发委屈的」
TAGS = (
    "开心", "得意", "害羞", "撒娇", "委屈", "生气", "无语",
    "困惑", "疲惫", "慌张", "涨价", "版本", "掉线", "干饭", "摸鱼", "干活", "别的模型",
)

# ---------- 方向词 ----------
#
# 「多」和「少」的说法都收进来，别让人记措辞。
# 长词放前面，避免「多来点」被「多来」先截断。
_UP = (
    "多来点", "多发点", "多贴点", "多放点", "多给点", "多整点", "多弄点", "多搞点",
    "再加点", "加一点", "多一点", "多一些",
    "多发", "多贴", "多来", "多给", "多点", "多整", "多弄", "多放", "多搞", "多些",
    "增加", "提高", "调高", "加大", "偏爱", "喜欢", "爱发", "爱看", "常用",
    "太少",
)
_DOWN = (
    "不喜欢", "别老是发", "别一直发", "别老发", "别再用",
    "少来点", "少发点", "少贴点", "少放点", "少给点", "少整点", "少弄点", "少搞点",
    "少一点", "少一些",
    "少发", "少贴", "少来", "少给", "少点", "少整", "少弄", "少放", "少搞", "少些",
    "别发", "别再发", "别贴", "别来", "别放", "别搞", "别用", "别拿", "别选",
    "不要发", "不要再发", "不要再", "不要",
    "降低", "调低", "减少", "去掉", "撤掉", "不爱", "讨厌",
    "太多", "太频繁", "太勤",
)
# 正则里长词优先，避免「多发点」被「多发」截断
_UP_RE = "|".join(sorted(_UP, key=len, reverse=True))
_DOWN_RE = "|".join(sorted(_DOWN, key=len, reverse=True))
_DIR_RE = f"({_UP_RE}|{_DOWN_RE})"

# 表情包目标的说法
_STICKER_TARGET = r"(?:表情包|表情图|表情|贴纸|图片|图)"
# 指「刚才那一张」的说法
_THIS_TARGET = (
    r"(?:这个表情包|这个表情|这张图|这张|这个|这图|这只|"
    r"刚才那张|刚才那个|刚发的那张|上面那张|最后那张|上一条|那张)"
)
# 目标在前、方向在后时，不用光秃秃的「这个」——「这个价格降低点」不是表情包指令
_THIS_ORDER_TARGET = (
    r"(?:这个表情包|这个表情|这张图|这张|这图|这只|"
    r"刚才那张|刚才那个|刚发的那张|上面那张|最后那张|上一条|那张)"
)
_TAG_RE = "|".join(TAGS)
# 句尾数量（「表情包能不能少放两张」）——数量也是这句话的一部分，不算多余内容
_STICKER_QTY = r"(?:[一两二三四五六七八九十几\d]+\s*(?:张|个|条|次|下))?"

# ---------- 「潜水」的时长 ----------
#
# 主人说的是人话：「这个群潜水半小时」「本群潜水 2 小时」「潜水到 18:30」。
# 这里只负责把时长挖出来，单位统一成分钟；认不出就返回 None。

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_NUM_TOKEN = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+)"


def _number(token: str) -> float | None:
    """「2」「2.5」「两」「二十四」→ 数字。"""
    token = (token or "").strip()
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        pass
    if "十" in token:
        head, _, tail = token.partition("十")
        if (head and head not in _CN_DIGITS) or (tail and tail not in _CN_DIGITS):
            return None
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return float(tens * 10 + ones)
    if len(token) == 1:
        value = _CN_DIGITS.get(token)
        return float(value) if value is not None else None
    values = [_CN_DIGITS.get(ch) for ch in token]
    if any(v is None for v in values):
        return None
    return float(sum(values))


def _clamp_minutes(value: float) -> int:
    return max(1, min(DIVE_MAX_MINUTES, int(round(value))))


def parse_duration_minutes(text: str) -> int | None:
    """从一句话里挖出「潜水多久」。

    认：2小时 / 两小时 / 半小时 / 一个半小时 / 一小时半 / 90分钟 / 1.5h /
        1小时30分 / 一刻钟 / 到 18:30 / 到 18 点半
    不认「一会儿」这种模糊说法 —— 宁可让主人说清楚。
    """
    body = text or ""

    # 到点：到 18:30 / 到18点 / 到 18 点半
    match = re.search(r"到\s*(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|点\s*(半|\d{1,2})?)", body)
    if match:
        hour = int(match.group(1))
        minute = 0
        if match.group(2):
            minute = int(match.group(2))
        elif match.group(3) == "半":
            minute = 30
        elif match.group(3):
            minute = int(match.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)  # 晚上说「到 8 点」就是明早 8 点
            seconds = (target - now).total_seconds()
            return max(1, min(DIVE_MAX_MINUTES, int(seconds // 60) or 1))

    if "一刻钟" in body:
        return 15

    # 半小时 / 一个半小时 / 半个钟头
    match = re.search(
        rf"(?:(?P<num>{_NUM_TOKEN})\s*个?\s*)?半\s*个?\s*(?:小时|钟头)", body
    )
    if match:
        if match.group("num"):
            num = _number(match.group("num")) or 0
            return _clamp_minutes(num * 60 + 30)
        return 30

    # 1小时30分 / 两小时十五分
    match = re.search(
        rf"(?P<h>{_NUM_TOKEN})\s*个?\s*(?:小时|钟头)\s*(?P<m>{_NUM_TOKEN})\s*分", body
    )
    if match:
        hours = _number(match.group("h")) or 0
        minutes = _number(match.group("m")) or 0
        return _clamp_minutes(hours * 60 + minutes)

    # 一小时半 / 两小时半
    match = re.search(rf"(?P<num>{_NUM_TOKEN})\s*个?\s*(?:小时|钟头)\s*半", body)
    if match:
        return _clamp_minutes((_number(match.group("num")) or 0) * 60 + 30)

    # 2小时 / 两小时 / 2h
    match = re.search(rf"(?P<num>{_NUM_TOKEN})\s*个?\s*(?:小时|钟头|h|H)", body)
    if match:
        return _clamp_minutes((_number(match.group("num")) or 0) * 60)

    # 90分钟 / 30分 / 45m
    match = re.search(rf"(?P<num>{_NUM_TOKEN})\s*(?:分钟|分|min|m)", body)
    if match:
        return _clamp_minutes(_number(match.group("num")) or 0)

    return None


# 潜水口令。「潜水」是强口令：带群前缀时不写时长也能用；「安静/别插话」
# 只有带了时长才算潜水，否则保持原来的「长期不插话」语义。
_DIVE_STRONG = ("潜水", "潜一会", "潜一会儿", "潜一下", "闭麦")
_DIVE_TIMED = ("别插话", "别说话", "别接话", "安静", "闭嘴", "噤声", "静音")
_GROUP_PREFIX = re.compile(r"^(这个群|本群|这群|群里|这里|群聊|本群聊)")

_DIVE_OFF = re.compile(
    r"^(?:"
    r"(?:结束|取消|解除|终止)(?:一下)?(?:潜水|闭麦)"
    r"|(?:潜水|闭麦)(?:结束|取消|解除)"
    r"|(?:不|别)潜水了"
    r"|浮上来|浮出水面|冒个泡|可以冒泡"
    r")(?:吧|了|啦|啊)?$"
)


def _dive_command(body: str) -> tuple[str, dict] | None:
    """认出「潜水 + 时长」和「结束潜水」。"""
    if _DIVE_OFF.match(body):
        return "group_dive_off", {}
    minutes = parse_duration_minutes(body)
    strong = any(word in body for word in _DIVE_STRONG)
    timed = bool(minutes) and any(word in body for word in _DIVE_TIMED)
    if not (strong or timed):
        return None
    at_group = bool(_GROUP_PREFIX.match(body)) or any(
        token in body for token in ("这个群", "本群", "这群")
    )
    # 「我去潜水了」是聊天，不是指令：既没群前缀、也没时长就不认
    if minutes is None and not at_group:
        return None
    # 没带时长时只认「这个群潜水」这种短整句；
    # 「这个群潜水的人好多」是聊天，不能把群静音一小时
    if minutes is None:
        if not re.fullmatch(
            r".{0,8}(?:潜水|潜一会|潜一下|闭麦)(?:吧|了|啊|呀|哦|呗)?", body
        ):
            return None
    return "group_dive", {"minutes": minutes or DIVE_DEFAULT_MINUTES}


# ---------- 群白名单 / 黑名单 ----------
#
# .env 的 GROUP_WHITELIST 是硬门：不在名单里的群，她一句话都不回（连 @ 都不回）。
# 可主人刚把她拉进一个新群、想让她回话时，改 .env 再重启太笨；更糟的是
# **那个群里她根本读不到你的指令**（硬门在指令判定之前）。所以这里给一组
# 聊天里就能改的名单，app.py 的硬门会特意放行这几条。
#
# 三张名单，优先级从高到低（见 app._allowed_group）：
#   黑名单 > 聊天里「移出白名单」 > 聊天里「加白名单」 > .env > 没设就全放行
#
# 只认整句：句子必须几乎全是这些词（`_GA_TOKEN`）+ 一张名单 + 一个方向，
# 「白名单是什么意思」「本群黑名单怎么加」都是聊天，不动名单。
# 否定式按 R6 处理：「别把本群加白名单」= 不加（移出），不是加。
_GA_TOKEN = re.compile(
    r"(?:本群|这个群|这群|这里|群聊|白名单|黑名单|列表|清单|"
    r"看看|查看|列出|列一下|一下|群里|群号|群|"
    r"不要再加|不要加|不要进|不要|不用|不许|不准|禁止|别加|别进|别|"
    r"移出去|移出|移走|移除|踢出去|踢出|退出|删掉|删除|去掉|取消|撤掉|收回|"
    r"解除|解禁|不加|不进|"
    r"加入|添加|加到|加进|放进|拉进|拉入|设成|设为|归到|算进|加|进|放|设|"
    r"把|将|给|从|的|里|中|里面|到|入|号|"
    r"\d{5,12}|了|吧|啊|呀|嘛|呗|啦|哦|呢)"
)
_GA_LIST_RE = re.compile(
    r"^(?:(?:看看|查看|列出|列一下|查一下))?群(白名单|黑名单)(?:列表|清单|名单)?$"
)
# 整句 = 一串上面那些词；混进别的字（「白名单是什么意思」）就当聊天
_GA_SENTENCE = re.compile(rf"(?:{_GA_TOKEN.pattern})+")
_GA_DEL_WORDS = (
    "移出去", "移出", "移走", "移除", "踢出去", "踢出", "退出", "删掉", "删除",
    "去掉", "取消", "撤掉", "收回", "解除", "解禁", "不加", "不进", "别加",
    "别进", "不要加", "不要进", "不要再加",
)
_GA_ADD_WORDS = (
    "加入", "添加", "加到", "加进", "放进", "拉进", "拉入", "设成", "设为",
    "归到", "算进", "加", "进", "放", "设",
)
_GA_NEG = re.compile(r"^(?:别|不要|不用|不许|不准|禁止)")
_GA_NEG_KEEP = ("不加", "不进", "别加", "别进", "不要加", "不要进", "不要再加")

# 不在白名单的群里，主人还能用这几条（app.py 的硬门照着它放行）
GROUP_ACCESS_KINDS = frozenset(
    {
        "group_whitelist_add",
        "group_whitelist_remove",
        "group_blacklist_add",
        "group_blacklist_remove",
        "group_access_list",
    }
)


def _group_access_command(head: str) -> tuple[str, dict] | None:
    """认「把本群加白名单」这一族。认不出返回 None。

    返回的 arg 里 ``gid`` 是明确的群号；空串表示「本群」，由 app 补。
    """
    text = re.sub(r"\s+", "", head or "").strip("。！？!?~～…，,.；;、")
    if not text or len(text) > 24:
        return None
    listed = _GA_LIST_RE.match(text)
    if listed:
        return "group_access_list", {
            "which": "white" if listed.group(1) == "白名单" else "black"
        }
    white, black = "白名单" in text, "黑名单" in text
    if white == black:  # 都没提，或者同时提了两张名单 —— 不猜
        return None
    # 整句必须是「名单 + 方向 + 群」这几种字的组合，混进别的词就是聊天
    if not _GA_SENTENCE.fullmatch(text):
        return None
    voice = "white" if white else "black"
    if any(word in text for word in _GA_DEL_WORDS):
        direction = "del"
    elif any(word in text for word in _GA_ADD_WORDS):
        direction = "add"
    else:
        return None
    # R6：否定词必须处理。「别把本群加白名单」是不要加，「不要移出」是别动
    if _GA_NEG.match(text) and not text.startswith(_GA_NEG_KEEP):
        direction = "add" if direction == "del" else "del"
    scope = re.search(r"本群|这个群|这群|这里|群聊", text)
    found = re.search(r"\d{5,12}", text)
    # 带号就按号；没写号（也没写「本群」）默认就是本群 ——
    # 私聊里没有「本群」，由 app 要求补群号。
    gid = found.group(0) if (found and not scope) else ""
    kind = {
        ("white", "add"): "group_whitelist_add",
        ("white", "del"): "group_whitelist_remove",
        ("black", "add"): "group_blacklist_add",
        ("black", "del"): "group_blacklist_remove",
    }[(voice, direction)]
    return kind, {"gid": gid}


# ---------- 退群 ----------
#
# 「退群」是**不可逆**的主人指令：只认整句、只认主人（app 里验 QQ 号），
# 问句不认（「退群？」「能不能退群？」是商量，不是命令）。
# 群里不带群号 = 退当前群；私聊必须带号，由 app 反过来问，绝不猜。
# 和「把本群移出白名单」区分开：那个只是不回话，人还在群里。
_GL_ACTION = r"(?:退出去|退出|退掉|退了|退|离开)"
_GL_TARGET = r"(?:这个群|本群|这群|群聊|群|这里)"
_GL_TAIL = r"(?:吧|了|啊|呀|呗|啦|哦|咯|嘛|就行|就好)*"
_GL_LEAD = r"(?:(?:请|麻烦|你|本鱼|大肥鱼|现在|马上|就|先|赶紧|把|将|给)\s*){0,3}"
_GL_NUM = r"(?P<gid>\d{5,12})"

# 「退群」「退出这个群」「把本群退掉」「退群 123456789」
_GL_ACTION_FIRST = re.compile(
    rf"^{_GL_LEAD}{_GL_ACTION}(?:出|掉)?\s*(?:{_GL_TARGET})?\s*"
    rf"(?:群号)?\s*(?:{_GL_NUM})?\s*{_GL_TAIL}$"
)
# 「这个群退了」「本群退掉」「群 123456789 退了」
_GL_TARGET_FIRST = re.compile(
    rf"^{_GL_LEAD}{_GL_TARGET}\s*(?:群号)?\s*(?:{_GL_NUM})?\s*"
    rf"{_GL_ACTION}(?:出|掉|了)?\s*{_GL_TAIL}$"
)
# 「把 123456789 这个群退了」
_GL_NUMBER_FIRST = re.compile(
    rf"^{_GL_LEAD}{_GL_NUM}\s*(?:这个)?(?:群)?\s*"
    rf"{_GL_ACTION}(?:出|掉|了)?\s*{_GL_TAIL}$"
)


def _group_leave_command(
    head: str, *, question: bool = False
) -> tuple[str, dict] | None:
    """认「退群」这一族。认不出返回 None。

    返回 ``("group_leave", {"gid": ...})``；``gid`` 空串表示「本群」，由 app 补。
    问句一律不认：退群不可逆，宁可让主人再说一句明确的。
    """
    if question:
        return None
    text = re.sub(r"\s+", "", head or "").strip("。！？!?~～…，,.；;、")
    if not text or len(text) > 24:
        return None
    # 整句里必须真的提到「群 / 这里」或带群号，别让光秃秃的「退出」也命中
    if not re.search(r"群|这里|\d{5,12}", text):
        return None
    explicit = bool(re.search(r"这个群|本群|这群|群聊|这里", text))
    for index, pattern in enumerate(
        (_GL_ACTION_FIRST, _GL_TARGET_FIRST, _GL_NUMBER_FIRST)
    ):
        match = pattern.fullmatch(text)
        if not match:
            continue
        gid = match.groupdict().get("gid") or ""
        # 「群退了」这种只说了个「群」、又没群号的，不知道退哪个，别猜；
        # 同理「123456789 退了」不算 —— 得说清是群。
        if index == 0 and "群" not in text and not explicit:
            return None
        if index == 1 and not explicit and not gid:
            return None
        if index == 2 and "群" not in text and not explicit:
            return None
        return "group_leave", {"gid": gid}
    return None


# ---------- 私聊黑名单 ----------
#
# 主人烦了某个号：私聊里说一句「别理他了 / 别跟他聊天了 / 拉黑 123456789」，
# 这个人以后私聊一律不回（连模型都不调）。反过来「可以理他了 / 解除拉黑 123456789」
# 放出来，「私聊黑名单」看现在禁了谁。
#
# 只认整句：这句必须几乎整句都在说这件事 —— 「别理他，他喝多了」是聊天，
# 不能把对方关进黑名单。带 QQ 号就禁那个号；不带号时由 app 决定禁谁
# （私聊里就是"最近一个来私聊她的非主人"），commands 只负责认出来。
_HIM = r"(?:他|她|这个人|那个人|这人|那人|对方|它)"
_TONE = r"(?:了|吧|啊|啦|哦|呀|呗)?"
_NOT = r"(?:不要|不用|别|不许|不准|禁止|别再|不要再)"

_PB_BAN_HIM = re.compile(
    rf"^(?:以后|都|就|请|先)?\s*(?:"
    # 不要和他聊天了 / 别理他 / 别回他了 / 别管他
    rf"{_NOT}\s*(?:再)?\s*(?:和|跟|给|理|回|搭理|管)\s*{_HIM}"
    rf"(?:聊天|聊|说话|回消息|回话|私聊|发消息)?"
    # 别让他私聊了 / 不许他私聊
    rf"|{_NOT}\s*(?:再)?\s*(?:让|允许)?\s*{_HIM}\s*(?:私聊|聊天|发消息|烦你)"
    rf"){_TONE}$"
)

_PB_BAN_QQ = re.compile(
    rf"^(?:拉黑|屏蔽|封禁|禁言|禁止(?:私聊)?|别理|不用理|不要理)\s*(?:QQ|qq)?\s*[:：]?\s*"
    rf"(?P<qq>\d{{5,12}})\s*(?:私聊|聊天|发消息)?{_TONE}$"
)

_PB_UNBAN_HIM = re.compile(
    rf"^(?:可以|能|允许|准许|恢复|放开)\s*(?:再)?\s*(?:和|跟|给|理|回|搭理|管)\s*{_HIM}"
    rf"(?:聊天|聊|说话|回消息|回话|私聊|发消息)?{_TONE}$"
    rf"|^(?:别|不要)\s*(?:再)?禁\s*{_HIM}{_TONE}$"
    rf"|^(?:把\s*{_HIM}\s*放|放\s*{_HIM})\s*出来{_TONE}$"
)

_PB_UNBAN_QQ = re.compile(
    rf"^(?:解除|取消|撤销|解禁|解封|放过)\s*(?:对)?\s*"
    rf"(?P<qq>\d{{5,12}})\s*(?:的)?\s*(?:私聊)?\s*"
    rf"(?:禁言|拉黑|屏蔽|封禁|黑名单)?{_TONE}$"
    rf"|^(?:解除|取消)\s*(?:拉黑|屏蔽|封禁|禁言)\s*(?P<qq2>\d{{5,12}})?{_TONE}$"
)

_PB_LIST = re.compile(
    r"^(?:看看|查看|列一下|列出)?\s*(?:私聊)?\s*"
    r"(?:黑名单|禁言名单|封禁名单|被禁名单)(?:里)?(?:有谁|都有谁)?$"
    r"|^(?:都)?(?:禁了谁|谁被禁了|谁在黑名单里)$"
)


def _private_ban_command(body: str) -> tuple[str, dict] | None:
    """认「私聊黑名单」这一族。认不出返回 None。

    只认整句（`^...$`）：「别理他，他喝多了」不能被当成禁言指令。
    """
    match = _PB_BAN_QQ.match(body)
    if match:
        return "private_ban", {"uid": match.group("qq")}
    match = _PB_UNBAN_QQ.match(body)
    if match:
        # 「解除拉黑」不带号：arg 就是空的，由 app 放最近禁的那个
        uid = match.group("qq") or match.group("qq2") or ""
        return "private_unban", ({"uid": uid} if uid else {})
    if _PB_LIST.match(body):
        return "private_bans", {}
    if _PB_BAN_HIM.match(body):
        return "private_ban", {}
    if _PB_UNBAN_HIM.match(body):
        return "private_unban", {}
    return None


# ---------- 「多说点 / 少说点」 ----------
#
# 主人的说法远不止「多说点」一种：多说话 / 多说两句 / 多说一点 / 少说话 /
# 少说一句 / 少回我几句 / 多发一点 …… 都该算。以前是一条条硬编码的，
# 结果「多」这边能调、「少」这边漏了一大片。这里按「方向 + 说/回/发 + 量词」一起认，
# 并且只认整句（^...$）——「少说话多做事」是谚语，不是让本鱼降档。
# 「多发一点」是 14:31 实测漏掉的说法：整句才认，所以「多发这个表情包」
# 「多发点表情」不会被这里截走（它们仍走表情权重）。
_SPEECH_VERB = r"(?:说|回|发)"
_SPEECH_AMOUNT = (
    r"(?:(?:(?:点|些|一点|一些)(?:话)?)|一句|两句|几句|一声|几回|几次|"
    r"我(?:点|些|一?两句|几?句)?|话)?"
)
_SPEECH_TAIL = r"(?:吧|啊|哈|嘛|呗|啦|了|哦|呀|捏|吗|么|呢)?"
_SPEECH_MORE_RE = rf"^(?:多|增加|提高){_SPEECH_VERB}{_SPEECH_AMOUNT}{_SPEECH_TAIL}$"
_SPEECH_LESS_RE = rf"^(?:少|减少|降低){_SPEECH_VERB}{_SPEECH_AMOUNT}{_SPEECH_TAIL}$"

# ---------- 「多聊点 / 少聊点」 ----------
#
# 同样是「多 / 少」，落到「聊 / 讲」上判的是**说话长短**（verbosity），
# 不是回复频率：多聊点 = 聊开一点，少聊点 = 说短一点。
# 上一轮只补了「说/回/发」，漏了「聊/讲」——「多聊点」一直躺在 chatty 里，
# 「少聊点」却没人认，主人一试就掉回聊天。这里两边一起补。
_CHAT_VERB = r"(?:聊|讲|扯|唠)"
_CHAT_AMOUNT = (
    r"(?:(?:(?:点|些|一点|一些)(?:话)?)|一句|两句|几句|一?会(?:儿)?|聊|话)?"
)
_CHAT_MORE_RE = rf"^(?:多|增加|提高){_CHAT_VERB}{_CHAT_AMOUNT}{_SPEECH_TAIL}$"
_CHAT_LESS_RE = rf"^(?:少|减少|降低){_CHAT_VERB}{_CHAT_AMOUNT}{_SPEECH_TAIL}$"

# 称呼的值：「什么来着」是在问，「起床 / 起来」是动词，都不算起名
#
# 「干嘛 / 干什么 / 干啥」也是在问「叫我有什么事」，不是起名 —— 以前只挡了
# 「什么 / 啥」，偏偏落下「干嘛」，主人随口一句「叫我干嘛」就把称呼设成了
# 「干嘛」。凡是「叫 + 动词 / 疑问词 / 量词」的说法，一律不当名字。
_TITLE_NOT_NAME = (
    r"(?:"
    r"(?:干|做|弄)(?:嘛|吗|啥|什么)"
    r"|一声|一下|一遍"
    r"|什么|啥|谁|哪|几|怎么"
    r"|起床|起来|醒来|过去|进来|出去|吃饭|睡觉|加班|干活|帮忙"
    r")"
)
_TITLE_WORD = rf"(?!{_TITLE_NOT_NAME})"
_TITLE_VALUE = rf"({_TITLE_WORD}[^\s，。！？,.!?]{{1,8}})"
# 「叫我一声就行 / 叫我一声吧」：剥完只剩请求尾巴或语气词，那不是名字
_TITLE_FILLER = re.compile(
    r"(?:就行|就好|即可|好了|好吗|行吗|可以吗|可以么|好不好|行不行|成吗|好么"
    r"|吧|啊|哦|呀|嘛|呢|了|哈|呗|啦|的|就|才|也|再"
    r"|喔|咯|诶|捏)+"
)

# ---------- 本群上下线推送 ----------
#
# 单个群不想被这类公告打扰，给一个**按群**开关。
# 只认带「本群 / 这个群 / 这群」的说法；裸说「关闭上下线推送」是整体开关。
_PUSH_GROUP = r"(?:本群|这个群|这群)"
_PUSH_WORD = r"上下线\s*(?:的)?\s*(?:推送|通知|公告|提醒)"
_PUSH_OFF = r"(?:关闭|关掉|停用|取消|停止|别再|不要|不用|不许|禁用|关|别)"
_PUSH_ON = r"(?:打开|开启|启用|恢复|继续|需要|可以|要)"
_PUSH_TAIL = r"(?:了|吧|啊|呀|嘛|呗|啦|哦)?"
# 「别再发」「不要再发」里的「再 / 发」可有可无
_PUSH_EXTRA = r"(?:再)?\s*(?:发|发送)?"
_PUSH_OFF_RE = re.compile(
    rf"^(?:"
    rf"(?:{_PUSH_OFF})\s*(?:{_PUSH_GROUP})\s*(?:的)?\s*(?:{_PUSH_EXTRA})?\s*"
    rf"{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_GROUP})\s*(?:的)?\s*(?:{_PUSH_OFF})\s*(?:{_PUSH_EXTRA})?\s*"
    rf"{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_GROUP})\s*(?:的)?\s*(?:把)?\s*{_PUSH_WORD}\s*"
    rf"(?:{_PUSH_OFF})\s*(?:发|发送)?{_PUSH_TAIL}"
    rf")$",
    re.IGNORECASE,
)
_PUSH_ON_RE = re.compile(
    rf"^(?:"
    rf"(?:{_PUSH_ON})\s*(?:{_PUSH_GROUP})\s*(?:的)?\s*{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_GROUP})\s*(?:的)?\s*(?:{_PUSH_ON})\s*{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_GROUP})\s*(?:的)?\s*(?:把)?\s*{_PUSH_WORD}\s*"
    rf"(?:{_PUSH_ON}){_PUSH_TAIL}"
    rf")$",
    re.IGNORECASE,
)

# ---------- 群聊上下线推送（整体） ----------
#
# 总开关：上线问候 + 下线公告，默认开。关掉后所有群都不发，也不再调模型。
# 按群开关在上面单独认（必须带「本群 / 这个群 / 这群」）。
# 总开关允许不带群前缀：「关闭上下线推送」「关闭所有群上下线推送」；
# 私聊里也能说，因为它不依赖当前群。
_PUSH_ALL_SCOPE = r"(?:群聊|所有群|全部群|全群|全局)?"
_PUSH_ALL_OFF_RE = re.compile(
    rf"^(?:"
    rf"(?:{_PUSH_OFF})\s*(?:{_PUSH_ALL_SCOPE})?\s*(?:{_PUSH_EXTRA})?\s*"
    rf"{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_ALL_SCOPE})\s*(?:{_PUSH_OFF})\s*(?:{_PUSH_EXTRA})?\s*"
    rf"{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_ALL_SCOPE})\s*{_PUSH_WORD}\s*(?:{_PUSH_OFF})"
    rf"\s*(?:发|发送)?{_PUSH_TAIL}"
    rf")$",
    re.IGNORECASE,
)
_PUSH_ALL_ON_RE = re.compile(
    rf"^(?:"
    rf"(?:{_PUSH_ON})\s*(?:{_PUSH_ALL_SCOPE})?\s*{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_ALL_SCOPE})\s*(?:{_PUSH_ON})\s*{_PUSH_WORD}{_PUSH_TAIL}"
    rf"|(?:{_PUSH_ALL_SCOPE})\s*{_PUSH_WORD}\s*(?:{_PUSH_ON}){_PUSH_TAIL}"
    rf")$",
    re.IGNORECASE,
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 风控规避（放在功能开关最前面）：一开就把群聊慢回、限速和主动功能全停。
    # 只认整句，且必须带动作词；「别打开风控模式」「不要关闭风控模式」
    # 这种否定式不会被 fullmatch 命中（开头是「别/不」）。
    ("risk_mode_on", re.compile(
        r"(?:打开|开启|启用|进入|切到|切换到|开)(?:一下)?"
        r"(?:风控规避|风控|风险控制)(?:模式)?(?:一下|吧|了|啦|啊|呀|呗)?"
    )),
    ("risk_mode_off", re.compile(
        r"(?:关闭|关掉|关|停用|退出|解除|取消|撤掉)(?:一下)?"
        r"(?:风控规避|风控|风险控制)(?:模式)?(?:一下|吧|了|啦|啊|呀|呗)?"
    )),
    # 语序倒过来：「把风控模式打开 / 风控模式关掉」。只认完整动词，
    # 不认裸「开/关」——「风控开了」是聊天里在说风控状态，不是指令。
    ("risk_mode_on", re.compile(
        r"(?:把|将)?(?:风控规避|风控|风险控制)(?:模式)?"
        r"(?:打开|开启|启用|进入|切到|切换到)(?:一下吧|吧|了|啦|啊|呀|呗)?"
    )),
    ("risk_mode_off", re.compile(
        r"(?:把|将)?(?:风控规避|风控|风险控制)(?:模式)?"
        r"(?:关闭|关掉|停用|退出|解除|取消|撤掉)(?:一下吧|吧|了|啦|啊|呀|呗)?"
    )),
    # 说人话检查：发之前本地再复查一遍（作文腔 / 跟刚才那句太像）。
    # 必须带动词：「说人话」单独一句是让人讲明白，不是开关（R5 整句 + R3 宁漏勿错）。
    ("saycheck_on", re.compile(
        r"(?:打开|开启|启用|恢复|开)(?:一下)?"
        r"(?:说人话|人话)(?:检查|模式|检查模式)?"
        r"(?:一下|吧|了|啦|啊|呀|呗)?"
    )),
    ("saycheck_off", re.compile(
        r"(?:关闭|关掉|关|停用|禁用|取消)(?:一下)?"
        r"(?:说人话|人话)(?:检查|模式|检查模式)?"
        r"(?:一下|吧|了|啦|啊|呀|呗)?"
    )),
    # 日记开关：默认开。关掉只是不再自动写，旧的日记还能翻。
    # 打开必须带「功能/写日记」，因为「打开日记」是翻日记（读），不是开开关。
    ("diary_off", re.compile(
        r"(?:(?:关闭|关掉|关|停用|禁用|取消)(?:一下)?(?:你的)?"
        r"(?:日记功能|写日记|日记)"
        r"|(?:别|不要|别再|不用)(?:再)?(?:自动)?写(?:日记|日记了))"
        r"(?:吧|了|啦|啊|呀|呗)?"
    )),
    ("diary_on", re.compile(
        r"(?:打开|开启|启用|恢复|继续)(?:一下)?(?:你的)?"
        r"(?:日记功能|写日记|自动写日记|继续写日记)"
        r"(?:吧|了|啦|啊|呀|呗)?"
    )),
    # 功能开关（放在最前面：避免「关闭群风格」被别的规则抢走）
    ("forward_off", re.compile(
        r"(?:关闭|关掉|关|停用|别读|不要读|别再读)(?:合并转发|转发记录|聊天记录转发|转发)"
    )),
    ("forward_on", re.compile(
        r"(?:打开|开启|启用|恢复|可以读|读一下)(?:合并转发|转发记录|聊天记录转发|转发)"
    )),
    # 找同款肥鱼（主动搭话）：默认开。只认带动词的整句，
    # 免得群里一句「别找肥鱼了」在聊别人也被当成关开关。
    ("fishchat_off", re.compile(
        r"(?:(?:关闭|关掉|关|停用|禁用)(?:这个)?(?:主动)?(?:去)?(?:找|跟)?"
        r"(?:同款)?(?:肥鱼|同类)(?:聊天|搭话|玩)?"
        r"|(?:别|不要|不许)(?:再|总|老|老是)?(?:主动)?(?:去)?(?:找|跟)?"
        r"(?:同款)?(?:肥鱼|同类)(?:聊天|搭话|玩)?)"
    )),
    ("fishchat_on", re.compile(
        r"(?:(?:打开|开启|启用|恢复)(?:这个)?(?:主动)?(?:去)?(?:找|跟)?"
        r"(?:同款)?(?:肥鱼|同类)(?:聊天|搭话|玩)?"
        r"|(?:可以|允许)(?:主动)?(?:去)?(?:找|跟)?"
        r"(?:同款)?(?:肥鱼|同类)(?:聊天|搭话|玩)?)"
    )),
    # 好友申请：默认关。带动词才认，免得把「你们别加好友啊」这种闲聊当成开关。
    # 否定式必须排在肯定式前面：不然「不要通过好友申请」会被「通过……」抢走
    ("friend_off", re.compile(
        r"(?:关闭|关掉|关|停用|别|不要|不许)(?:通过|加|收)?(?:你的)?(?:好友申请|加好友|好友)"
    )),
    ("friend_on", re.compile(
        r"(?:可以|允许|打开|开启|启用|通过|接受)(?:加|收)?(?:你的)?(?:好友申请|加好友|好友)"
    )),
    ("trends_off", re.compile(
        r"(?:关闭|关掉|关|停用|别管|不要管|别再看)(?:热梗|热搜|热点|热梗库|热点库)"
    )),
    ("trends_on", re.compile(
        r"(?:打开|开启|启用|恢复|看看|关注)(?:热梗|热搜|热点|热梗库|热点库)"
    )),
    # 群梗表（群日志里统计出来的复读梗）。必须带「群」或「玩梗」，
    # 否则「关闭梗」会和上面的热梗开关抢；热梗那条排在前面，先认它。
    ("memes_off", re.compile(
        r"(?:关闭|关掉|关|停用|别用|别学)(?:群梗库|群梗|群里的梗|玩梗|梗库)"
    )),
    ("memes_on", re.compile(
        r"(?:打开|开启|启用|恢复)(?:群梗库|群梗|群里的梗|玩梗|梗库)"
    )),
    # 跟着复读：硬动词可以带「复读」，软拒绝（别/不要）必须带「跟着」，
    # 否则群里一句「别复读」会被当成关开关。
    ("repeat_off", re.compile(
        r"(?:(?:关闭|关掉|关|停用|禁用)(?:这个)?(?:跟着复读|跟读|复读)"
        r"|(?:别|不要|不许)(?:再)?(?:跟着复读|跟读)"
        r"|(?:跟着复读|跟读|复读)(?:关掉|关闭|关|停用|禁用))"
        r"(?:这个)?(?:功能|开关)?(?:了|啦|吧|啊|呀|嘛|呗)?"
    )),
    ("repeat_on", re.compile(
        r"(?:(?:打开|开启|启用|恢复)(?:这个)?(?:跟着复读|跟读|复读)"
        r"|(?:可以|允许)(?:再)?(?:跟着复读|跟读)"
        r"|(?:跟着复读|跟读|复读)(?:打开|开启|启用|恢复))"
        r"(?:这个)?(?:功能|开关)?(?:了|啦|吧|啊|呀|嘛|呗)?"
    )),
    ("style_off", re.compile(
        r"(?:关闭|关掉|关|停用|别学|不要学|别再学)"
        r"(?:群风格|群里说话|群友说话|学群友|说话风格)(?:统计)?"
    )),
    ("style_on", re.compile(
        # 「开」必须和上面的裸「关」成对：2026-10-26 实测「关群风格」认、
        # 「开群风格统计」不认，模型就口头答应了一句「开好啦」，开关没动。
        # 前面的否定词要挡住：「别开群风格」不是打开（旧行为是当聊天，保持）。
        r"(?:学学|学习|打开|开启|启用|恢复|"
        r"(?<![别不没])(?<!不要)(?<!不用)(?<!不想)开)"
        r"(?:群风格|群里说话|群友说话|学群友|说话风格)(?:统计)?"
    )),
    # 生气的设定。目标词必须写全（「设定/机制/功能」），
    # 否则「别生气」会被当成关开关 —— 那句是在哄她，走的是对话。
    ("mood_off", re.compile(
        r"(?:关闭|关掉|关|停用|禁用|不要|别用)(?:这个)?"
        r"(?:生气的?(?:设定|机制|功能|开关))"
    )),
    ("mood_on", re.compile(
        r"(?:打开|开启|启用|恢复|开着)(?:这个)?"
        r"(?:生气的?(?:设定|机制|功能|开关))"
    )),
    # 目标在前、动作在后（「生气的设定关掉」）
    ("mood_off_order2", re.compile(
        r"(?:生气的?(?:设定|机制|功能|开关))[^，。！？,\s]{0,4}(?:关掉|关闭|关了吧|关|停用)"
    )),
    ("mood_on_order2", re.compile(
        r"(?:生气的?(?:设定|机制|功能|开关))[^，。！？,\s]{0,4}(?:打开|开启|启用|恢复)"
    )),
    # 她自己拉黑人（私聊）：纯本地判断广告/骚扰，先警告三次再拉黑。
    # 关掉之后她只认主人的「别理他」，不自己动手。
    ("autoban_off", re.compile(
        r"(?:关闭|关掉|关|停用|别|不要|不许|别再)(?:再)?(?:自动|自己)"
        r"(?:拉黑|封禁|禁言)"
    )),
    ("autoban_on", re.compile(
        r"(?:打开|开启|启用|恢复|可以|允许)(?:再)?(?:自动|自己)"
        r"(?:拉黑|封禁|禁言)"
    )),
    # @ 人：默认关，必须成对（可开可关）。排在群静默前面 ——
    # 「别@人了」不是「这个群别插话」，别被下面带「群」的规则抢走。
    # 目标词必须写全（人/别人/大家/群友/点名），「别@我」是群友在拒绝，
    # 不是主人的开关指令，不能认。
    # **带 ^ 和 $**：短句允许 search，不锚住的话「他@人了」会被当成开关（R5）。
    ("at_off", re.compile(
        r"^(?:别|不要|不许|不准|不用|关闭|关掉|关|停用|禁用)(?:再)?\s*"
        r"(?:@|at|艾特|圈)\s*(?:人|别人|大家|群友)(?:了|吧|啊|呀|嘛|呗)?$"
        r"|^(?:关闭|关掉|关|停用|禁用|别|不要)(?:再)?\s*点名(?:了|吧|啊|呀|嘛|呗)?$"
    )),
    ("at_on", re.compile(
        r"^(?:可以|能|允许|打开|开启|启用|恢复|开始)?\s*"
        r"(?:@|at|艾特|圈)\s*(?:人|别人|大家|群友)(?:了|吧|啊|呀|嘛|呗)?$"
        r"|^(?:可以|允许|打开|开启|启用|恢复|开始)\s*点名(?:了|吧|啊|呀|嘛|呗)?$"
    )),
    # 「@ 谁由谁定」：和上面的总开关是两层 —— 总开关管「能不能 @」，
    # 这个管「@ 谁由模型挑，还是只按规则回@」。必须成对（R1）。
    # 「别让她自己@」不能被 at_off 抢走：at_off 要求「别」后面直接是 @/艾特。
    ("at_pick_on", re.compile(
        r"^(?:让)?(?:她|你)?自己(?:决定|选|挑|定)?(?:要)?\s*"
        r"(?:@|at|艾特|圈|点名)(?:谁)?(?:了|吧|啊|呀|嘛|呗)?$"
        r"|^(?:@|艾特|点名)(?:谁|人)?(?:让|由)(?:她|你)?自己"
        r"(?:决定|选|挑|定)?(?:了|吧|啊|呀|嘛|呗)?$"
    )),
    ("at_pick_off", re.compile(
        r"^(?:别|不要|不许|不用|不让)(?:再)?(?:让)?(?:她|你)?自己(?:决定|选|挑|定)?"
        r"(?:要)?\s*(?:@|at|艾特|圈|点名)(?:谁)?(?:了|吧|啊|呀|嘛|呗)?$"
        r"|^(?:@|艾特|点名)(?:谁|人)?(?:按|照)规则(?:来|吧|了|点)?$"
    )),
    # 群聊上下线推送：整体开关优先于单群开关（「关闭上下线推送」是全局，
    # 「关闭本群上下线推送」才是单群）。
    ("presence_push_all_off", _PUSH_ALL_OFF_RE),
    ("presence_push_all_on", _PUSH_ALL_ON_RE),
    # 本群上下线推送：上线问候 + 下线公告，按群开关，必须成对。
    # 放在群静默前面：句子里带「本群」，但说的不是插话/闭嘴。
    ("group_push_off", _PUSH_OFF_RE),
    ("group_push_on", _PUSH_ON_RE),
    # 群要先判：不然「这个群别插话」会被下面的降档规则抢走。
    # 完全静默分两档，说法上靠「这个群 / 本群」区分：
    #   - 带「这个群 / 本群 / 这群」→ 只静默这个群（group_silence）
    #   - 「所有群 / 全部群 / 群里 / 群聊」→ 所有群一起（group_silence_all）
    # 比「别插话」更硬：一句话都不说、连 @ 都不回。必须排在 group_mute 前面。
    # 只认「说话 / 发言 / 讲话 / 出声」这一族：冒泡 / 插话仍是弱静默。
    ("group_silence", re.compile(
        r"^(?:你)?\s*(?:以后|从今以后)?\s*"
        r"(?:不要|别|不许|不准|不用)(?:再)?\s*在"
        r"(?:这个群|本群|这群)\s*"
        r"[^，。！？,\s]{0,2}"
        r"(?:说话|发言|讲话|出声|吭声|吱声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
        r"|^(?:这个群|本群|这群)(?:就)?(?:不要|别|不许|不准)(?:再)?"
        r"(?:说话|发言|讲话|出声|吭声|吱声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
        r"|^在(?:这个群|本群|这群)\s*(?:就)?"
        r"(?:不要|别|不许|不准)(?:再)?\s*"
        r"(?:说话|发言|讲话|出声|吭声|吱声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
    )),
    ("group_silence_all", re.compile(
        r"^(?:你)?\s*(?:以后|从今以后)?\s*"
        r"(?:不要|别|不许|不准|不用)(?:再)?\s*(?:在)?"
        r"(?:所有群聊?|全部群聊?|每个群聊?|各个群聊?|群聊|群里头?)(?:里|里头)?\s*"
        r"[^，。！？,\s]{0,2}"
        r"(?:说话|发言|讲话|出声|吭声|吱声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
        r"|^(?:所有群聊?|全部群聊?|每个群聊?|各个群聊?|群聊|群里头?)(?:里|里头)?"
        r"(?:都|就)?(?:不要|别|不许|不准)(?:再)?"
        r"(?:说话|发言|讲话|出声|吭声|吱声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
        r"|^在(?:所有群聊?|全部群聊?|每个群聊?|各个群聊?|群聊|群里头?)(?:里|里头)?\s*"
        r"(?:都|就)?(?:不要|别|不许|不准)(?:再)?\s*"
        r"(?:说话|发言|讲话|出声|吭声|吱声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
    )),
    ("group_silence_off", re.compile(
        r"^(?:可以|能|允许|恢复)?\s*在"
        r"(?:这个群|本群|这群)\s*"
        r"(?:说话|发言|讲话|出声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
        r"|^(?:这个群|本群|这群)(?:可以|能)?"
        r"(?:说话|发言|讲话|出声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
    )),
    ("group_silence_all_off", re.compile(
        r"^(?:可以|能|允许|恢复)?\s*(?:在)?"
        r"(?:所有群聊?|全部群聊?|每个群聊?|各个群聊?|群聊|群里头?)(?:里|里头)?\s*"
        r"(?:说话|发言|讲话|出声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
        r"|^(?:所有群聊?|全部群聊?|每个群聊?|各个群聊?|群聊|群里头?)(?:里|里头)?"
        r"(?:(?:都|就|可以|能)\s*)*"
        r"(?:说话|发言|讲话|出声)(?:了|啦|吧|啊|呀|嘛|呗)?$"
    )),
    # 「安静」后面允许带「点 / 一点 / 些」，这些是同一个意思，不是多余内容。
    ("group_mute", re.compile(
        r"(这个群|本群|这群|这里|群里|群聊)"
        r"[^，。！？,\s]{0,6}"
        r"(别插话|不要插话|不插话|别说话|不要说话|少说话|别接话|别出声|"
        r"别冒泡|别冒头|别吭声|禁言|闭嘴|安静点|安静一点|安静些|安静)"
    )),
    ("group_unmute", re.compile(
        r"(?:(?:这个群|本群|这群|这里|群里|群聊)\s*)?"
        r"(可以插话|可以说话了|可以说话|开口吧|解除静音|恢复插话|恢复说话|"
        r"可以冒泡|出来说话|继续说话|别憋着|正常说话|不用安静了)"
    )),
    # 主人点名收一张：偷这个表情包 / 把这个表情包收了 / 收下这张图。
    # 必须带「这个/这张」或明确的「表情包/图」，别把「收下」这种日常词抢走。
    ("sticker_steal", re.compile(
        r"^(?:偷|收下|收了)(?:这个|这张|那个|那张|刚才那张)"
        r"(?:表情包|表情|图)?(?:吧|呗|啊|呀)?$"
        r"|^(?:偷|收下)(?:表情包|表情|图)(?:吧|呗|啊|呀)?$"
        r"|^(?:把)?(?:这个|这张|那个|那张|刚才那张)?(?:表情包|表情|图)"
        r"(?:收了|收下|偷了|偷走)(?:吧|呗|啊|呀)?$"
    )),
    # 这一张：把「这个/这张/刚才那张」泛指都认了
    ("sticker_this", re.compile(
        rf"{_DIR_RE}[^，。！？,\s]{{0,8}}{_THIS_TARGET}"
    )),
    # 「刚才那张少发点」「这个表情别发了」：目标在前、方向在后
    ("sticker_this_order2", re.compile(
        rf"({_THIS_ORDER_TARGET})[^，。！？,\s]{{0,6}}{_DIR_RE}{_STICKER_QTY}"
    )),
    # 「那个表情少发点」「表情发太多了」这种目标在前、方向在后的语序
    ("sticker_order2", re.compile(
        rf"({_STICKER_TARGET})[^，。！？,\s]{{0,8}}{_DIR_RE}{_STICKER_QTY}"
    )),
    # 「别发图」「多发表情」「少贴两张」这种动词直接接目标的
    ("sticker_split", re.compile(
        r"(别老|别再|不要再|别|不要|少|多)"
        r"[^，。！？,\s]{0,4}"
        r"(发表情包|发表情|发图|贴表情|发这些|发那么多|发这么多)"
    )),
    # 某个标签，两种语序都认；「少发委屈的（表情）」里「的 / 表情」可省
    ("sticker_tag", re.compile(
        rf"{_DIR_RE}[^，。！？,\s]{{0,8}}({_TAG_RE})(?:的)?{_STICKER_TARGET}?"
    )),
    ("sticker_tag_order2", re.compile(
        rf"({_TAG_RE})(?:的)?{_STICKER_TARGET}?[^，。！？,\s]{{0,6}}{_DIR_RE}"
    )),
    # 全局表情包
    ("sticker_global", re.compile(
        rf"{_DIR_RE}[^，。！？,\s]{{0,10}}{_STICKER_TARGET}"
    )),
    # 主动程度（「少」的要先判，否则「少主动点」会被「主动点」抢走）
    ("dm_less", re.compile(
        r"(少主动(?:点|一点|些)?|别老找我|别总找我|少来找我|别主动|别烦我|别老私聊|别总私聊|"
        r"少私聊|别老发消息|少发消息|别老打扰|少打扰)"
    )),
    ("dm_more", re.compile(
        r"(多主动(?:点|一点|些)?|主动(?:一点|点)(?:来找我|找我)?|多来找我|多关心我|多找我说话|别总等我说|"
        r"多私聊|多给我发消息|多陪陪我|多找我聊)"
    )),
    # 思考档位（off / low / high）
    ("think_off", re.compile(
        r"(别想那么多|不用想了|不用想|别想了|别琢磨了|快点回|想少点|"
        r"不用思考|不要思考|别推理|直接答|别想太久|省点思考)"
    )),
    ("think_low", re.compile(
        r"(正常想|想一点就行|普通模式|默认思考|正常思考|普通思考)"
    )),
    ("think_high", re.compile(
        r"(想深一点|想透一点|认真想想|多想想|仔细想|想清楚点|好好想|"
        r"想仔细点|深入思考|多想一会)"
    )),
    # 「多说点 / 少说点」这一族：方向 + 说/回 + 量词，两边一起认。
    # 以前只硬编码了「多说点 / 多说话」（多能调档），少的这边只列了「少说点」，
    # 于是「少说话」「少说一点」「多说一句」全掉回聊天 —— 一多一少不对等。
    # 用整句卡住（^...$）：只认几乎整句都在说这句话的，
    # 免得「少说话多做事」「多说无益」这种谚语被当成调档。
    ("tier_up", re.compile(_SPEECH_MORE_RE)),
    ("tier_down", re.compile(_SPEECH_LESS_RE)),
    # 「多聊点 / 少聊点」：落点是说话**长短**，不是档位
    ("chatty", re.compile(_CHAT_MORE_RE)),
    ("short", re.compile(_CHAT_LESS_RE)),
    # 「安静 / 安静点 / 小声点」：也只认整句。
    # 以前 tier_down 里是一个光秃秃的「安静」，配上短句的 search 兜底，
    # 「我今天安静地看了一天书」也会被降一档。
    ("tier_down", re.compile(
        r"^(?:你|先|快|给我|稍微|能不能|可不可以)?"
        r"(?:安静|安静点|安静一点|安静些|安静下来|安静一下|小声点|小点声|轻点声)"
        r"(?:吧|啊|了|哦|呀|呗|嘛|一下|一会)?$"
    )),
    # 「话太少 / 话太多了」：整句才算。
    # 不能写成裸子串 —— 「少说话多做事」里也有「话多」，search 一捞就误降档。
    ("tier_up", re.compile(
        r"^(?:你|我)?话(?:太|很|真|有点)?少(?:了|啦|啊|吧|呢)?$"
    )),
    ("tier_down", re.compile(
        r"^(?:你|我)?话(?:太|很|真|有点)?多(?:了|啦|啊|吧|呢)?$"
    )),
    # 档位（升档要先判：「太安静了」不能被下面的「安静」抢走）
    ("tier_up", re.compile(
        r"(太安静|太冷清|冷清|太闷|多说点|多回点|多回我|多说话|活跃点|"
        r"活跃一点|积极点|积极一点|别装死|理理我|怎么不理我|热闹点|多冒泡|"
        r"多出现|存在感高点|回得(?:太|很|真)?少)"
    )),
    ("tier_down", re.compile(
        r"(回得?(?:太|有点|过于)?(?:勤|多|频繁)|太勤|太频繁|太活跃|别太活跃|"
        r"别那么活跃|太吵|太闹|吵死了|少说点|少说两句|少回点|别老说话|"
        r"少出现|别老出现|"
        r"别老发|少冒泡|存在感低|刷屏|太高频|别这么活跃)"
    )),
    # 长度
    ("short", re.compile(
        r"(话太长|说短点|短一点|别啰嗦|简短点|简洁点|字少点|少写点|压缩点|"
        r"话(?:少|短)(?:点|些|一点|一些))"
    )),
    ("chatty", re.compile(
        r"(多聊点|话多点|聊开点|放开点|说多点|详细点|说详细点|"
        r"多讲点|别惜字如金|话(?:多|长)(?:点|些|一点|一些))"
    )),
    # 取消称呼：「以后别叫我老板了」是让她改回默认，不是把称呼改成「老板」。
    # 以前只有下面的 title 一条，search 兜底会在「别叫我老板」里找到「叫我老板」，
    # 把意思做反 —— 说「别叫」反而叫上了。
    ("title_clear", re.compile(
        r"^(?:以后|都|就|请|你)?(?:别|不要|不用|不许|别再|不要再|别老|别总是)"
        r"(?:再)?(?:叫我|喊我|称我|称呼我|管我叫)(?:一声|一下)?"
        rf"{_TITLE_WORD}[^\s，。！？,.!?]{{0,8}}$"
    )),
    # 称呼（长的说法放前面：「叫我一声老王」不能被「叫我」先截走）
    # 「叫我什么来着」是在问，不是在起名 —— 值里不许以疑问词开头；
    # 「叫我起床 / 别叫我起来」是动词，不是称呼。
    ("title", re.compile(
        r"(?:以后|都|就|请)?"
        r"(?:叫我一声|叫我一下|叫我做|称呼我|管我叫|叫我|喊我|称我)"
        rf"\s*{_TITLE_VALUE}"
    )),
    # 重置
    ("reset", re.compile(
        r"(恢复正常|恢复默认|重置设置|清空设置|都回到默认|恢复原样|重置一下|"
        r"全部重置|全部恢复|回到默认|恢复初始)"
    )),
]

# 出现请求口吻时，问句也可以当指令（「能不能少发点表情？」）
_REQUEST_FRAME = re.compile(
    r"(能不能|可不可以|可以不可以|能否|好不好|行不行|可以吗|可以么|"
    r"行吗|成吗|好吗|好么|行么|帮我|帮忙|麻烦|请你|请|可以)"
)

# 「别生气了 / 气消了」：主人手动把她哄好。只认这种**明确的指令**，
# 「对不起」「我错了」不算 —— 那是真的在道歉，得让情绪状态自己听着消气。
_SOOTHE_RE = re.compile(
    r"^(?:你|快|先|赶紧|别)?(?:"
    r"别生气了?|不要生气了?|不生气了?|气消了|消消气|消消气吧|哄好了?|哄好了没|"
    r"不气不气|别气了|原谅你|原谅你了"
    r")(?:啦|了|嘛|呗|好不好|行不行|吗|吧)?$"
)

# 可以在句首省掉的客气话 / 主语。「以后叫我老板」里的「以后」也算。
_PREFIXES = (
    "从现在开始", "从今天起", "可以不可以", "可不可以", "能不能",
    "麻烦你", "麻烦", "请你", "请", "帮我", "帮忙", "我想让你", "我要你", "希望你",
    "本鱼", "大肥鱼", "机器人", "bot", "以后", "给我", "记得", "都", "就", "先", "你",
)

# 句尾的请求尾巴，去掉后再匹配，免得被算进称呼里
_REQUEST_TAIL = re.compile(
    r"(好吗|行吗|可以吗|可以么|好不好|行不行|成吗|好么|行么)$"
)

# 严格整句匹配时允许句尾多出来的语气词（「少说话吧」「太吵了」「少发委屈的」）。
# 只从**句尾**剥，句中的一字不动 —— 「别理他了，他喝多了」还是聊天，不会变指令。
_TAIL_PARTICLES = re.compile(r"(?:了|吧|啊|呀|嘛|呗|啦|哦|呢|吗|么|哈|捏|喔|咯|诶|的)+$")


def _direction(text: str) -> float:
    for word in _DOWN:
        if word in text:
            return -1.0
    for word in _UP:
        if word in text:
            return 1.0
    # 单字方向词只在整段就是它时才认，避免「多少」被当成「多」
    if text in ("别", "不要", "少", "别再", "别老"):
        return -1.0
    if text == "多":
        return 1.0
    return 0.0


def _strip_prefixes(body: str) -> str:
    head = body
    for _ in range(4):
        for prefix in _PREFIXES:
            if head.startswith(prefix) and len(head) > len(prefix):
                head = head[len(prefix):].lstrip("，, 、")
                break
        else:
            break
    return head


def _is_question(body: str) -> bool:
    return body.endswith(("吗", "呢", "么")) or "？" in body or "?" in body


# ---------- 机器人 / AI 名单（人工维护，QQ 上直接改） ----------
#
# 这类号自动认不准，所以不猜：主人说一句就记一个，写进 speakers.json。
# 认的写法尽量宽：
#   加机器人 123456789 / 加AI 123456789 / 标记机器人 285...
#   把 123456789 标记成机器人 / 123456789 是机器人
#   取消机器人 285... / 把 285... 从机器人名单里删掉
#   机器人名单 / AI名单
_SPEAKER_WORD = r"(?P<kind>机器人|人工智能|AI)"
_QQ = r"@?(?P<qq>\d{5,12})"

_SPEAKER_ADD = (
    # 加机器人 123 / 标记 AI 123
    re.compile(
        rf"(?:添加|加|标记|标|登记|设置|设为|设成)\s*(?:一下|一个)?\s*"
        rf"{_SPEAKER_WORD}\s*(?:[:：=]|是)?\s*{_QQ}",
        re.IGNORECASE,
    ),
    # 把 123 标记成机器人 / 将 123 加到 AI 名单
    re.compile(
        rf"(?:把|将)\s*{_QQ}\s*(?:标记|标|设|加进|放进|加到|加|登记|算|当)?"
        rf"(?:为|成|作|到)?\s*{_SPEAKER_WORD}",
        re.IGNORECASE,
    ),
    # 123 是机器人 / 123 标记为 AI
    re.compile(
        rf"{_QQ}\s*(?:是|算|标记为|标为|设为|当成|当作)\s*{_SPEAKER_WORD}",
        re.IGNORECASE,
    ),
    # 机器人 123 / AI 123（省掉动词）
    re.compile(
        rf"^{_SPEAKER_WORD}\s*[:：=]?\s*{_QQ}$",
        re.IGNORECASE,
    ),
)
_SPEAKER_DEL = (
    # 删除机器人 123 / 取消 AI 123
    re.compile(
        rf"(?:删除|删掉|删|移除|移出|去掉|取消|解除)\s*(?:一下)?\s*"
        rf"{_SPEAKER_WORD}\s*(?:[:：=])?\s*{_QQ}",
        re.IGNORECASE,
    ),
    # 把 123 从机器人名单里删掉
    re.compile(
        rf"(?:把|将)\s*{_QQ}\s*(?:从)?\s*{_SPEAKER_WORD}\s*(?:名单)?\s*"
        rf"(?:里|中)?\s*(?:删除|删掉|删|移除|移出|去掉|取消)",
        re.IGNORECASE,
    ),
    # 123 不是机器人
    re.compile(rf"{_QQ}\s*(?:不是|别当|不算)\s*{_SPEAKER_WORD}", re.IGNORECASE),
)


def _speaker_kind(word: str) -> str:
    return "robot" if word == "机器人" else "ai"


def _speaker_command(body: str) -> tuple[str, dict] | None:
    """认「把某个号标成机器人/AI」这类指令。认不出返回 None。"""
    if not re.search(r"机器人|人工智能|AI", body, re.IGNORECASE):
        return None
    # 名单：不带 QQ 号的才是「查名单」，带号的多半是增删
    if not re.search(r"\d{5,12}", body):
        if re.fullmatch(r"(?:看看|查看)?\s*(?:机器人|AI|人工智能)", body, re.IGNORECASE) or re.fullmatch(
            r"(?:看看|查看|列一下|列出)?\s*(?:机器人|AI|人工智能|标记)?\s*(?:的)?\s*(?:名单|列表|清单)",
            body,
            re.IGNORECASE,
        ):
            return "speaker_list", {}
    for pattern in _SPEAKER_DEL:
        match = pattern.search(body)
        if match:
            return "speaker_del", {
                "kind": _speaker_kind(match.group("kind")),
                "uid": match.group("qq"),
            }
    for pattern in _SPEAKER_ADD:
        match = pattern.search(body)
        if match:
            return "speaker_add", {
                "kind": _speaker_kind(match.group("kind")),
                "uid": match.group("qq"),
            }
    return None


# ---------- 查账 ----------
#
# 「今天花了多少」只算今天 00:00 到现在（自然日），不是"最近 24 小时"。
# 这条必须排在问句闸门**前面**：「今天花了多少钱？」带问号，按普通问句会被直接拒掉。
# 判定规则和群聊里的 `today_spend` 工具共用 `spendtool.is_spend_question`，
# 免得主人的指令和群友的问法各认一套。

def _spend_command(body: str) -> tuple[str, dict] | None:
    """认「今天花了多少」这类查账指令。认不出返回 None。"""
    if is_spend_question(body):
        return "spend", {"period": "today"}
    return None


# ---------- 余额 / 对账 ----------
#
# 和「今天花了多少」不是一回事：那个只算本机 usage.db；这个读 DeepSeek
# 账户余额，是**整个账户**的实扣（DSH、别的脚本也在花）。只认整句，
# 免得把聊天里的「余额宝」当成查余额。
_BALANCE_WORDS = frozenset(
    {
        "余额",
        "查余额",
        "看看余额",
        "看一下余额",
        "账户余额",
        "查账户余额",
        "余额多少",
        "余额多少钱",
        "余额还剩多少",
        "余额还剩多少钱",
        "余额还有多少",
        "余额还有多少钱",
        "余额剩多少",
        "余额剩多少钱",
        "余额有多少钱",
        "余额是多少",
        "还有多少钱",
        "还有多少余额",
        "还剩多少余额",
        "账户还有多少钱",
        "账户还剩多少钱",
        "账户还剩多少",
        "账户还有多少",
        "账户里还剩多少钱",
        "账户里还有多少钱",
        "账户里剩多少钱",
        "账户里有多少钱",
        "对账",
        "账单对账",
        "账户对账",
        "对一下账",
        "查账对账",
    }
)

# 句首的垫话：「我 / 帮我 / 查一下 / 看看」只说明怎么问，不改变问的是账户。
# 只脱一层，长句里的「我」不动 —— 「我今天余额不多了」仍当聊天，不当指令。
_BALANCE_LEAD = re.compile(
    r"^(?:我的|我们的|咱们的|本鱼的|我|我们|咱们|本鱼|咱|你)?"
    r"(?:帮我|给我|麻烦你?|请你?)?"
    r"(?:查查|看看|瞅瞅|查|看|瞅|瞄)?"
    r"(?:一下|下|一眼)?"
    r"(?:我的|咱的|咱们的|本鱼的)?"
)


def _balance_command(body: str) -> tuple[str, dict] | None:
    """认「余额 / 对账」这类账户口径查询。认不出返回 None。

    2026-10-29 实锤：主人在群里问「账号还剩多少钱」，她回「本鱼看不到」——
    整句白名单里只有「账户还剩多少钱」，「账号」这个写法、以及「账号余额」
    这类常见说法全漏了。这里先把「账号 / 帐号」并回「账户」，再脱掉
    「我 / 帮我查一下」这类垫话，最后仍按**整句**比对；不改成子串匹配，
    免得「支付宝余额」「我今天余额不多了」被当成查账。
    """
    text = re.sub(r"\s+", "", body or "").strip("。！？!?~～…，,.；;、")
    if text in _BALANCE_WORDS:
        return "balance", {"live": True}
    text = text.replace("账号", "账户").replace("帐号", "账户")
    text = _BALANCE_LEAD.sub("", text)
    if text in _BALANCE_WORDS:
        return "balance", {"live": True}
    return None


# ---------- 状态（只读） ----------
#
# 她报自己的状态：在线 / 记忆 / 情绪 / 花费 / 功能 / 群 / 额度。
# **只读**：不写 prefs、不改 mood、不调模型；回执由 app.py 读现有状态拼。
# 和「查账」一样，问句也认（「状态怎么样？」），所以排在问句闸门前面。

_STATUS_SECTIONS = {
    "状态": "all",
    "自身状态": "all",
    "你的状态": "all",
    "本鱼状态": "all",
    "大肥鱼状态": "all",
    "运行状态": "all",
    "自检": "all",
    "自检一下": "all",
    "看看状态": "all",
    "看看你的状态": "all",
    "查状态": "all",
    "查一下状态": "all",
    "状态怎么样": "all",
    "状态怎么样啊": "all",
    "状态如何": "all",
    "你现在什么状态": "all",
    "你现在的状态": "all",
    "在线状态": "online",
    "连接状态": "online",
    "模型状态": "online",
    "记忆状态": "memory",
    "心情状态": "mood",
    "情绪状态": "mood",
    "功能状态": "features",
    "额度状态": "limits",
    "群状态": "groups",
    "本群状态": "groups",
}


def _status_command(body: str) -> tuple[str, dict] | None:
    """认「状态」这类只读自查指令。认不出返回 None。"""
    text = re.sub(r"\s+", "", body or "").strip("。！？!?~～…，,.；;、")
    section = _STATUS_SECTIONS.get(text)
    if section is None:
        return None
    return "status", {"section": section}


# 「更新」：看她这一版会什么。只认整句，避免把聊天里的「更新了没」截走。
# 清单来自 qqbot/capabilities.py，和 system prompt 里那段是同一份事实源。
_CAPABILITY_WORDS = ("更新", "更新清单", "更新日志", "能力清单")


# ---------- 直播陪看 ----------
#
# 只读 B 站直播间的标题 + 弹幕，0 token。房间号可以写真号，也可以贴直播间链接。
# 没写房间号时也认（app 会告诉主人补一个），这样「看直播」不会掉回聊天被模型瞎答。

# 「发个弹幕」「发条弹幕 说 XXX」：往正在陪看的房间真发一条（写操作，主人限定）。
_LIVE_DANMAKU_SEND = re.compile(
    r"^(?:给直播(?:间)?)?(?:发|扔|丢|刷)(?:一条|一个|一句|个|条|句)?(?:弹幕|弹屏)"
    r"(?:[，,]?\s*(?:就)?\s*(?:说|写)?\s*[:：]?\s*(?P<text>.+?))?"
    r"\s*$"
)
# 「在弹幕里发一句 XXX」「弹幕上发一个 XXX」：主语在前，另一种说法。
# 2026-11-04 实锤：主人在群里这么说，没被认出来，模型就只在群里讲了个笑话、
# 嘴上说「发啦」，直播间一条都没有。
_LIVE_DANMAKU_WHERE = re.compile(
    r"^(?:在)?(?:直播间)?(?:的)?(?:弹幕(?:里|上|区)?|弹屏(?:里|上)?)\s*"
    r"(?:里|上)?\s*(?:发|扔|丢|刷)(?:一条|一个|一句|个|条|句)?\s*(?:弹幕)?\s*"
    r"(?:[，,]?\s*(?:就)?\s*(?:说|写)?\s*[:：]?\s*(?P<text>.+?))?"
    r"\s*$"
)
_LIVE_WATCH = re.compile(
    r"^(?:陪(?:我|主人)?)?(?:看|盯|关注|追|守着)(?:一下|一会|一会儿|着)?"
    r"(?:B站|b站|哔哩哔哩|bilibili)?(?:的)?(?:直播|直播间|直播频道)"
    r"(?:房号|房间号|房间|room|id)?\s*[:：]?\s*"
    r"(?P<room>\d{1,12}|(?:https?://)?[^\s]*live\.bilibili\.com[^\s]*)?$"
)
_LIVE_STOP = re.compile(
    r"^(?:不看了|不看直播了?|别看了|关掉直播|关闭直播|停止看直播|结束看直播|"
    r"关掉直播陪看|关闭直播陪看|停止直播陪看|"
    r"别盯(?:直播)?了|退出直播间)(?:吧|啦|了|嘛|啊|呀)?$"
)
# 主人可能先说「打开直播陪看」再说看谁；这句本身没有房间，只回一句要房号，
# 别让模型假装已经打开。
_LIVE_OPEN = re.compile(
    r"^(?:打开|开启|开始|启动|可以)(?:一下)?(?:直播陪看|陪看)(?:吧|了|呀|嘛)?$"
)
# 「开启直播陪看Chris」「打开陪看 123456789」：带目标的版本。
# 2026-11-04 实锤：主人群里说「开启直播陪看Chris1i」没被认出来，
# 落到模型那边又因为群里没有直播工具，只能回一句「开不起来」。
_LIVE_OPEN_TARGET = re.compile(
    r"^(?:打开|开启|开始|启动)\s*(?:一下)?\s*(?:直播陪看|陪看)\s*"
    r"(?P<name>[^\s，。！？]{1,20}?)\s*(?:吧|了|呀|嘛)?$"
)
# 「进入asaki的直播陪看」「换到克里斯的直播间」「切换直播间」：
# 切换房间走和「看某人的直播」同一条名字解析；没给名字就只问看哪个。
_LIVE_ENTER = re.compile(
    r"^(?:进入|切换(?:到|成)?|换(?:到|成)?|转到|跳到|改成)\s*"
    r"(?:(?P<name>[^\s，。！？]{1,20}?)\s*(?:的)?)?\s*"
    r"(?:直播陪看|直播间|直播|陪看)"
    r"(?:[，,]\s*(?:就)?\s*这个直播间)?"
    r"(?:吧|了|啦|呀|嘛|就行|就好)?$"
)
# 「退出克里斯的直播陪看」「结束直播陪看」：带不带名字都停当前这个。
_LIVE_EXIT = re.compile(
    r"^(?:退出|关掉|关闭|停止|结束)\s*"
    r"(?:(?P<name>[^\s，。！？]{1,20}?)\s*(?:的)?)?\s*"
    r"(?:直播陪看|直播间|直播|陪看)"
    r"(?:吧|啦|了|嘛|啊|呀)?$"
)
# 「看看弹幕 / 弹幕都在聊什么」：只读当前缓冲；没在盯就先问看谁的，
# 不能拿「区」之类的群梗当弹幕回答。
_LIVE_DANMAKU = re.compile(
    r"^(?:看看|瞅瞅|查看|现在|这会儿)?\s*(?:直播)?弹幕\s*"
    r"(?:都|还|又)?\s*(?:在)?\s*"
    r"(?:聊些什么|聊了些什么|聊什么|聊了啥|聊啥|说了些什么|说些什么|说了些啥|说什么|说了啥|说啥|什么|咋样|情况)?"
    r"\s*(?:呢|吧|啊|呀|嘛)?$"
)
_LIVE_STATUS = re.compile(
    r"^(?:直播状态|直播间状态|看的什么直播|在看什么直播|在看哪个直播|"
    r"在看直播吗|你在看直播吗)$"
)
# 「今天开播了吗 / 今天播了多久 / 昨天开播多久」：查某一天的开播记录。
# 这条必须排在 _LIVE_ASK 前面，否则「昨天开播了吗」会被当成查一个叫「昨天」
# 的主播（2026-11-08 加开播记录时一并修的坑）。
_LIVE_DAY_WORDS = r"(?P<day>今天|今日|今晚|昨天|昨日|昨晚|前天)"
_LIVE_HISTORY_DAY = re.compile(
    rf"^{_LIVE_DAY_WORDS}\s*(?:开播|直播|播)(?:了)?\s*"
    r"(?:状态|记录|情况|吗|没|没有|多久|多长时间|多长|时长|"
    r"几个小时|几小时)?\s*"
    r"(?:呢|吗|啊|呀|嘛|了)?$"
)
# 「克里斯今天开播了吗 / 克里斯昨天播了多久」：名字在前、日期在后。
# 必须排在 _LIVE_ASK 前面，否则会被当成查「克里斯现在开没开播」。
_LIVE_HISTORY_NAMED = re.compile(
    rf"^(?P<name>[^\s，。！？]{{1,20}}?)\s*{_LIVE_DAY_WORDS}\s*"
    r"(?:开播|直播|播)(?:了)?\s*"
    r"(?:状态|记录|情况|吗|没|没有|多久|多长时间|多长|时长|"
    r"几个小时|几小时)?\s*"
    r"(?:呢|吗|啊|呀|嘛|了)?$"
)
# 「今天克里斯开播了吗 / 昨天克里斯播了多久」：日期在前、名字在后。
# 放在日期-only 后面：「今天开播了吗」不能被拆成名字「开」。
_LIVE_HISTORY_DAY_NAME = re.compile(
    rf"^{_LIVE_DAY_WORDS}\s*(?P<name>[^\s，。！？]{{1,20}}?)\s*"
    r"(?:开播|直播|播)(?:了)?\s*"
    r"(?:状态|记录|情况|吗|没|没有|多久|多长时间|多长|时长|"
    r"几个小时|几小时)?\s*"
    r"(?:呢|吗|啊|呀|嘛|了)?$"
)
# 「我的意思是今天开播了吗 / 所以克里斯昨天播了多久」：先摘掉话头。
_LIVE_HISTORY_PREFIX = re.compile(
    r"^(?:我的?意思是|我的?问题是|我是说|我说的是|我问的是|我问一下|"
    r"我想问|想问一下|想问下|想问|就是问问|所以|那么|那)\s*"
)
# 「她今天播了吗」里的「她」指当前陪看的房间，不要当主播名去搜。
_LIVE_HISTORY_PRONOUNS = {"她", "他", "它", "主播", "这个主播", "那个主播", "这主播", "那主播"}
# 「今天没开播吗 / 昨天还没播吗」：同样归到当天记录，别掉回聊天。
_LIVE_HISTORY_NEG = re.compile(
    rf"^{_LIVE_DAY_WORDS}\s*(?:还)?\s*没\s*(?:开播|直播|播)"
    r"(?:吗|呢|吧|啊|呀)?$"
)
# 「开播记录 / 直播记录 / 查开播记录 昨天 / 开播记录 克里斯」。
_LIVE_HISTORY_RECENT = re.compile(
    r"^(?:查|看看|看一下|查看|翻一下)?\s*(?:开播|直播)(?:记录|历史|时间)"
    r"(?:\s*(?P<arg>.+?))?\s*$"
)
# 「最近开播 / 最近直播记录」。
_LIVE_HISTORY_NEAR = re.compile(
    r"^(?:最近|这几天|最近几天|近几天)\s*(?:开播|直播)(?:记录|历史)?$"
)
# 「克里斯现在在播吗」「Chris1i 开播了吗」：查某个主播开没开播，只读、
# 不换当前盯的房间。名字必须像个人，别把「你在看直播吗」当成主播名。
# 时间词放最前面也要先摘掉：「现在asaki大人在播吗」不能被认成
# 主播叫「现在asaki大人」（2026-10-30 主人 todo 实锤）。
_LIVE_ASK_TIME = r"(?:现在|今天|今晚|此刻|这会儿|目前|刚才)"
_LIVE_ASK = re.compile(
    rf"^{_LIVE_ASK_TIME}?\s*"
    r"(?P<name>[^\s，。！？]{1,20}?)\s*"
    rf"{_LIVE_ASK_TIME}?\s*(?:还)?\s*"
    r"(?:在播吗|在直播吗|在看直播吗|开播了吗|开播没|在播没|直播了吗|播了吗)$"
)


def _live_history_command(text: str) -> tuple[str, dict] | None:
    """「今天开播了吗 / 开播记录」：只读本地记录，0 token。"""
    text = _LIVE_HISTORY_PREFIX.sub("", text).strip()
    if not text:
        return None
    # 「今天没开播吗」先按否定式收掉，别被下面的「日期+名字」拆成名字「没」。
    match = _LIVE_HISTORY_NEG.fullmatch(text)
    if match:
        return "bili_live_history", {"day": match.group("day")}
    # 名字+日期 / 日期+名字：都按「某一天的开播记录」查，不要退成当前状态。
    for pattern in (_LIVE_HISTORY_NAMED, _LIVE_HISTORY_DAY, _LIVE_HISTORY_DAY_NAME):
        match = pattern.fullmatch(text)
        if not match:
            continue
        name = str(match.groupdict().get("name") or "").strip()
        # 「今晚Chris1i在播吗」里的「在」是状态词，不是名字的一部分；
        # 这种问法要留给 _LIVE_ASK 查当前开没开播。
        if name.endswith(("在", "还", "正")):
            continue
        arg: dict[str, str] = {"day": match.group("day")}
        if name and name not in _LIVE_HISTORY_PRONOUNS:
            arg["target"] = name
        return "bili_live_history", arg
    if _LIVE_HISTORY_NEAR.fullmatch(text):
        return "bili_live_history", {"day": "最近"}
    match = _LIVE_HISTORY_RECENT.fullmatch(text)
    if not match:
        return None
    arg_text = (match.group("arg") or "").strip()
    if not arg_text:
        return "bili_live_history", {"day": "最近"}
    day = parse_day(arg_text)
    if day:
        return "bili_live_history", {"day": day}
    if arg_text in {"最近", "记录", "历史"}:
        return "bili_live_history", {"day": "最近"}
    # 「开播记录 克里斯」：名字交给 live_tools 解析，后面列最近的场次。
    return "bili_live_history", {"target": arg_text, "day": "最近"}


def _live_command(body: str) -> tuple[str, dict] | None:
    text = re.sub(r"\s+", " ", (body or "").strip())
    if not text:
        return None
    if _LIVE_STATUS.fullmatch(text):
        return "bili_live_status", {}
    history = _live_history_command(text)
    if history is not None:
        return history
    if _LIVE_STOP.fullmatch(text) or _LIVE_EXIT.fullmatch(text):
        return "bili_live_stop", {}
    if _LIVE_DANMAKU.fullmatch(text):
        return "bili_live_danmaku", {}
    match = _LIVE_DANMAKU_SEND.fullmatch(text) or _LIVE_DANMAKU_WHERE.fullmatch(text)
    if match:
        body = (match.group("text") or "").strip()
        body = re.sub(r"^[，,]?\s*(?:说|写)?\s*[:：]?\s*", "", body).strip()
        # 「发个弹幕吧」里的「吧」是语气词，不算内容；但「克神快撤吧」里的「吧」
        # 是内容的一部分，不能一律砍掉（2026-11-04 实锤被砍成「克神快撤」）。
        if body in {"吧", "呗", "呀", "嘛", "了", "啊"}:
            body = ""
        return "live_danmaku_send", {"text": body}
    if _LIVE_OPEN.fullmatch(text):
        return "bili_live_open", {}
    match = _LIVE_OPEN_TARGET.fullmatch(text)
    if match:
        name = (match.group("name") or "").strip()
        # 「开启直播陪看吧」里那个「吧」不算名字，按"没给目标"处理
        if len(name) >= 2 and name not in _LIVE_NAME_STOP:
            return "bili_live_watch_name", {"name": name}
        return "bili_live_open", {}
    match = _LIVE_ENTER.fullmatch(text)
    if match:
        name = (match.group("name") or "").strip()
        if (
            name
            and name not in _LIVE_NAME_STOP
            and not name.endswith(("在", "看", "还"))
        ):
            return "bili_live_watch_name", {"name": name}
        return "bili_live_open", {}
    match = _LIVE_ASK.fullmatch(text)
    if match:
        name = match.group("name").strip()
        if (
            name
            and name not in _LIVE_NAME_STOP
            and not name.endswith(("在", "看", "还"))
        ):
            return "bili_live_status", {"target": name}
    match = _LIVE_WATCH.fullmatch(text)
    if not match:
        return None
    raw = (match.group("room") or "").strip()
    return "bili_live_watch", {"room": extract_room_id(raw), "raw": raw}


# ---------- 直播弹幕梗 ----------
#
# 弹幕库只做候选；真正进群梗表要主人说「直播梗入库 N」。这三个入口都只认
# 主人（app 的 _handle_owner_command 统一把关），0 token。
_LIVE_MEMES_LIST = re.compile(
    r"^(?:看看|查看|列一下|列出来|有什么)?(?:这个|当前)?(?:直播|直播间)(?:的)?"
    r"(?:梗|弹幕梗)(?:列表|清单)?$"
)
_LIVE_MEME_APPROVE = (
    re.compile(
        r"^(?:直播梗|直播弹幕梗)\s*(?:入库|收下|收了|记下|记进)\s*"
        r"(?P<target>\d{1,3}|[^\s，。！？]{1,20})$"
    ),
    re.compile(
        r"^(?:入库|收下|收了|记下|记进)\s*(?:直播梗|直播弹幕梗)\s*"
        r"(?P<target>\d{1,3}|[^\s，。！？]{1,20})$"
    ),
)
_LIVE_MEME_REJECT = re.compile(
    r"^(?:直播梗|直播弹幕梗)\s*(?:不入库|不要|跳过|不记|删掉)\s*"
    r"(?P<target>\d{1,3}|[^\s，。！？]{1,20})$"
)


# 「看克里斯的直播」「测试一下看 Chris1i 直播」：名字不是房号，不能按
# `_LIVE_WATCH` 的空房号处理，也不能掉回聊天让模型猜。只认主人（app 统一把关），
# 由 app 走 live_tools 的「知识库链接 / B 站同名 UP」解析。
_LIVE_NAME_WATCH = (
    re.compile(
        r"^(?:测试一下|试试|帮我|我想|我要|就|那就|那)?\s*"
        r"(?:看|去|盯|陪我看?)\s*"
        r"(?P<name>[^\s，。！？]{1,20}?)\s*(?:的)?(?:直播|直播间)"
        r"(?:就行|就好|可以|吧|了|啦|呀|嘛)?$"
    ),
    # 「就克里斯的直播间就行」：没有「看/去/盯」，但明显是在指定房间
    re.compile(
        r"^(?:就|那就|那)?\s*"
        r"(?P<name>[^\s，。！？]{1,20}?)\s*(?:的)?直播间"
        r"(?:就行|就好|可以|吧|了|啦|呀|嘛)?$"
    ),
)
_LIVE_NAME_STOP = {
    "这个",
    "那个",
    "这里",
    "那里",
    "我在看",
    "我看",
    "直播间",
    "直播",
    "个",
    "一个",
    # 时间词不是名字：「看现在直播」不能去盯一个叫「现在」的主播
    "现在",
    "今天",
    "今晚",
    "昨天",
    "昨日",
    "昨晚",
    "前天",
    "此刻",
    "这会儿",
    "目前",
    "刚才",
}


def _live_name_watch_command(body: str) -> tuple[str, dict] | None:
    text = re.sub(r"\s+", " ", (body or "").strip())
    for pattern in _LIVE_NAME_WATCH:
        match = pattern.fullmatch(text)
        if not match:
            continue
        name = match.group("name").strip()
        if name and name not in _LIVE_NAME_STOP:
            return "bili_live_watch_name", {"name": name}
    return None


def _live_meme_command(body: str) -> tuple[str, dict] | None:
    text = re.sub(r"\s+", " ", (body or "").strip())
    if not text:
        return None
    if _LIVE_MEMES_LIST.fullmatch(text):
        return "bili_live_memes", {}
    for pattern in _LIVE_MEME_APPROVE:
        match = pattern.fullmatch(text)
        if match:
            return "bili_live_meme_approve", {"target": match.group("target")}
    match = _LIVE_MEME_REJECT.fullmatch(text)
    if match:
        return "bili_live_meme_reject", {"target": match.group("target")}
    return None


def _capability_command(body: str) -> tuple[str, dict] | None:
    if re.sub(r"\s+", "", body or "") in _CAPABILITY_WORDS:
        return "capabilities", {}
    return None


# 「看看群梗」：只读清单，0 token。2026-10-27 主人问「收集的梗在哪可以看见」——
# 表在 data/memes.json 里，但没人愿意翻 JSON。整句匹配，别把聊天里的
# 「这个梗好老」当成查询（那句没有「有什么/收了哪些」这种问法）。
_MEMES_LIST_RE = re.compile(
    r"(?:"
    r"(?:看看|瞧瞧|查看|列出|列一下|显示)"
    r"(?:(?:这个群|本群|咱群)的|群里的?)?"
    r"(?:群梗|梗|梗库)(?:列表|清单|表)?"
    r"|(?:这个群|本群|咱群|群里)(?:都)?"
    r"(?:有什么|有哪些|收了哪些|收了什么|都有什么|都有哪些)(?:群)?梗"
    r"|(?:你|她)?(?:都)?收了(?:哪些|什么)(?:群)?梗"
    r"|(?:群梗|梗库)(?:列表|清单)"
    r")"
)


def _memes_list_command(body: str) -> tuple[str, dict] | None:
    """认「看看群梗」这一族。认不出返回 None。"""
    text = re.sub(r"\s+", "", body or "").strip("。！？!?~～…，,.；;、")
    if text and _MEMES_LIST_RE.fullmatch(text):
        return "memes_list", {}
    return None


# 「看看谐音」：只读，只看谐音代称，不把整张梗表倒出来。
# 必须带「谐音/代称」或「看/列/收了哪些」结构；单独的「谐音」不当指令。
_HOMOPHONE_LIST_RE = re.compile(
    r"(?:"
    r"(?:看看|瞧瞧|查看|列出|列一下|显示)"
    r"(?:(?:这个群|本群|咱群)的|群里的?)?"
    r"(?:谐音|谐音代称|代称)(?:列表|清单|表)?"
    r"|(?:这个群|本群|咱群|群里)(?:都)?"
    r"(?:有什么|有哪些|收了哪些|收了什么|都有什么|都有哪些)"
    r"(?:谐音|谐音代称|代称)"
    r"|(?:你|她)?(?:都)?收了(?:哪些|什么)(?:谐音|谐音代称|代称)"
    r"|(?:谐音|谐音代称|代称)(?:列表|清单)"
    r")"
)


def _homophone_list_command(body: str) -> tuple[str, dict] | None:
    text = re.sub(r"\s+", "", body or "").strip("。！？!?~～…，,.；;、")
    if text and _HOMOPHONE_LIST_RE.fullmatch(text):
        return "homophone_list", {}
    return None


# 「指令 全部」：默认「指令」只给精简版，这个才展开完整清单。
_FULL_HELP_RE = re.compile(r"^指令\s*(?:全部|完整|详细|全量|所有)$")


def is_full_help_command(text: str) -> bool:
    return bool(
        _FULL_HELP_RE.fullmatch(
            (text or "").strip().strip("。！？!?~～…，,.；;、")
        )
    )


# 「梗：词 = 什么意思」/「谐音：词 = 原词」/「写法：缩写 = 正词」：主人给她补场景。
# 必须带冒号前缀，别把聊天里的「这个梗：xxx」当成指令 —— 只认句首的「梗/记梗/谐音/代称」。
# 0 token，写进 data/memes.json（群里标本群，私聊标成所有会话通用）；
# 「写法」写进 data/meme_aliases.json（缩写/换字 → 正词，梗表和梗百科共用）。
_MEME_NOTE_RE = re.compile(
    r"^(?P<kind>记梗|改梗|这条梗|梗|谐音梗|改谐音|谐音|代称|写法|变体|缩写)\s*[:：]\s*"
    r"(?P<text>[^=＝:：\n]{1,12}?)\s*"
    r"(?:=|＝|->|→|是)\s*"
    r"(?P<meaning>[^=＝\n]{1,60})$"
)


def _meme_note_command(body: str) -> tuple[str, dict] | None:
    """认「梗：沉默 = 不想接话时丢的一个字」「写法：f8fq = 飞八分钱」。认不出返回 None。"""
    text = (body or "").strip()
    if not text or not re.match(
        r"^(?:记梗|改梗|这条梗|梗|谐音梗|改谐音|谐音|代称|写法|变体|缩写)", text
    ):
        return None
    match = _MEME_NOTE_RE.match(text)
    if not match:
        return None
    word = match.group("text").strip()
    meaning = match.group("meaning").strip()
    if not word or not meaning:
        return None
    raw_kind = match.group("kind")
    if raw_kind in ("写法", "变体", "缩写"):
        kind = "spell"
    elif raw_kind in ("谐音梗", "改谐音", "谐音", "代称"):
        kind = "alias"
    else:
        kind = "meme"
    return "meme_note", {
        "text": word,
        "meaning": meaning,
        "kind": kind,
        "edit": raw_kind in ("改梗", "改谐音"),
    }


# 「组成：鱼八分钱 = 飞八分钱 + 大肥鱼」：主人标这个梗是哪几个梗拼出来的。
# 原料用 + ＋ 、 ， & 和 隔开；至少一个、最多五个。只认句首这几个词 + 冒号。
_MEME_BLEND_RE = re.compile(
    r"^(?P<kind>组成|拼梗|融合|延伸|缝合)\s*[:：]\s*"
    r"(?P<text>[^=＝:：\n+＋、,，&]{1,12}?)\s*"
    r"(?:=|＝|->|→)\s*"
    r"(?P<parts>[^=＝\n]{1,80})$"
)
_MEME_BLEND_SPLIT = re.compile(r"\s*(?:\+|＋|、|,|，|&|和)\s*")


def _meme_blend_command(body: str) -> tuple[str, dict] | None:
    """认「组成：鱼八分钱 = 飞八分钱 + 大肥鱼」。认不出返回 None。"""
    text = (body or "").strip()
    if not text or not re.match(r"^(?:组成|拼梗|融合|延伸|缝合)", text):
        return None
    match = _MEME_BLEND_RE.match(text)
    if not match:
        return None
    word = match.group("text").strip()
    parts = [
        p.strip() for p in _MEME_BLEND_SPLIT.split(match.group("parts")) if p.strip()
    ]
    if not word or not parts or len(parts) > 5:
        return None
    return "meme_blend", {"text": word, "parts": parts[:5]}


# 「记梗 X」：只查不入库；查完等主人回「入库 / 不入库」。
_MEME_INTAKE_RE = re.compile(
    r"^(?:记梗|查梗入库|梗入库)\s*[:：]?\s*(?P<term>[^\s=＝:：\n]{1,20})\s*$"
)
_MEME_INTAKE_YES_RE = re.compile(
    r"^(?:入库|记进梗库|记进梗表|确认入库|确认记梗|收进梗库|加进梗库)"
    r"(?:吧|了|啦|啊|呀|嘛|呗)?$"
)
_MEME_INTAKE_NO_RE = re.compile(
    r"^(?:不入库|不记了|不记这个|不入|算了|取消|放弃|别入库)"
    r"(?:吧|了|啦|啊|呀|嘛|呗)?$"
)
_MEME_DELETE_RE = re.compile(
    r"^(?:删梗|删掉梗|忘掉梗|删候选|不要这条梗)\s*[:：]?\s*(?P<text>[^\s=＝]{1,20})\s*$"
)
_MEME_DETAIL_RE = re.compile(
    r"^(?:查梗|梗详情|看看这条梗)\s*[:：]?\s*(?P<text>[^\s=＝]{1,20})\s*$"
)
_MEME_CANDIDATES_RE = re.compile(
    r"^(?:看看候选|候选梗|候选列表|有哪些候选梗|查候选)$"
)
_MEME_CONFIRM_RE = re.compile(
    r"^(?:确认梗|确认候选|入库候选)\s*[:：]?\s*(?P<text>[^\s=＝]{1,20})\s*$"
)


def _meme_intake_command(body: str) -> tuple[str, dict] | None:
    text = (body or "").strip()
    match = _MEME_INTAKE_RE.match(text)
    if not match:
        return None
    term = match.group("term").strip()
    if not term:
        return None
    return "meme_intake", {"term": term}


def _meme_intake_confirm_command(body: str) -> tuple[str, dict] | None:
    text = (body or "").strip().strip("。！？!?~～…，,.；;、")
    if _MEME_INTAKE_YES_RE.fullmatch(text):
        return "meme_intake_confirm", {}
    if _MEME_INTAKE_NO_RE.fullmatch(text):
        return "meme_intake_cancel", {}
    return None


def _meme_delete_command(body: str) -> tuple[str, dict] | None:
    match = _MEME_DELETE_RE.match((body or "").strip())
    if not match:
        return None
    return "meme_delete", {"text": match.group("text").strip()}


def _meme_detail_command(body: str) -> tuple[str, dict] | None:
    match = _MEME_DETAIL_RE.match((body or "").strip())
    if not match:
        return None
    return "meme_detail", {"text": match.group("text").strip()}


# ---------- 待办（只存内存） ----------
#
# 主人的临时清单：「todo：内容」记一条，「todo」看，「todo 删 N」删，
# 「todo 清空」全清。**故意不落盘**：重启就清空，不进 prefs、不进记忆库。
# 加一条必须带冒号（中文/英文都认），这样「todo 删 1」「todo 清空」不会
# 被当成一条内容叫「删 1」的待办；「todo list 怎么写」也不会被当成清单查询。
_TODO_ADD = re.compile(
    r"^todo\s*[:：]\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_TODO_DELETE = re.compile(
    r"^todo\s*(?:删掉|删除|删|移除|去掉)\s*(?:第)?\s*(?P<index>\d{1,3})\s*(?:条|个)?$",
    re.IGNORECASE,
)
_TODO_CLEAR = re.compile(
    r"^(?:todo\s*(?:全部)?\s*(?:清空|清掉|清零)|(?:清空|清掉)\s*todo)(?:吧|了|啦)?$",
    re.IGNORECASE,
)
_TODO_LIST = re.compile(r"^todo(?:\s*(?:清单|列表))?\s*[:：]?$", re.IGNORECASE)


def _todo_command(body: str) -> tuple[str, dict] | None:
    """认「todo」这一族。认不出返回 None。"""
    match = _TODO_ADD.match(body)
    if match:
        text = match.group("text").strip()
        if text:
            return "todo", {"action": "add", "text": text}
    match = _TODO_DELETE.match(body)
    if match:
        return "todo", {"action": "delete", "index": int(match.group("index"))}
    if _TODO_CLEAR.match(body):
        return "todo", {"action": "clear"}
    if _TODO_LIST.match(body):
        return "todo", {"action": "list"}
    return None


def _manual_sticker_command(body: str) -> tuple[str, dict] | None:
    """「人工收图 …」：前缀后面原样交给 app 解析字段。"""
    rest = _split_manual_command(body)
    if rest is None:
        return None
    return "sticker_manual", {"body": rest}


_REPLACE_RE = re.compile(
    r"^(?:替换表情包|换掉表情包|替换这个表情包|换掉这个表情包|换图|修图)"
    r"\s*[:：]?\s*(.*)$",
    re.S,
)
_GIVE_REPLACE_RE = re.compile(
    r"^给\s*#?([A-Za-z0-9_]{1,16})\s*换一张\s*$"
)


def _replace_sticker_command(body: str) -> tuple[str, dict] | None:
    """「替换表情包 #084」/「给 #084 换一张」：群友也能用。"""
    match = _REPLACE_RE.match(body)
    if match is not None:
        rest = match.group(1).strip()
        sid = ""
        found = re.search(r"[#＃]([A-Za-z0-9_]{1,16})", rest)
        if found:
            sid = found.group(1)
        elif re.fullmatch(r"[A-Za-z0-9_]{1,16}", rest):
            sid = rest
        return "sticker_replace", {"sid": sid}
    match = _GIVE_REPLACE_RE.match(body)
    if match is not None:
        return "sticker_replace", {"sid": match.group(1)}
    return None


# ---------- 日记 ----------
#
# 日记是自由字段（「11月5号的日记」里带日期），单独写入口，别塞进 _PATTERNS。
# 只认整句；「我今天写日记了」「你会写日记吗」都不是指令（开头不是动词/日记）。
# 读和写分开：read 只查、0 token；write 会真的花一次模型调用。
_DIARY_DAY = (
    r"(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?"
    r"|\d{1,2}[-/月]\d{1,2}[日号]?"
    r"|今天|今日|昨天|昨日|前天|大前天|明天|后天)"
)
_DIARY_WORD = r"(?:日记本子|日记本|日记)"
_DIARY_TAIL = r"(?:呢|吗|啊|呀|吧|了|啦)?"
_DIARY_READ_RE = re.compile(
    rf"^(?:"
    rf"(?:看看|看|读|念|念念|念一念|翻翻|翻开|打开|翻)(?:一下)?"
    rf"(?:你|她|本鱼|大肥鱼)?(?:写的|记的)?(?:的)?"
    rf"(?:(?P<day1>{_DIARY_DAY})(?:的)?)?{_DIARY_WORD}"
    rf"|(?:你|她|本鱼|大肥鱼)?(?:写的|记的)?(?:的)?"
    rf"(?:(?P<day2>{_DIARY_DAY})(?:的)?)?{_DIARY_WORD}"
    rf"){_DIARY_TAIL}$"
)
_DIARY_WRITE_RE = re.compile(
    rf"^(?:"
    rf"(?:重新写|重写|补写|补一下|补|写|记)"
    rf"(?:(?:一下|一篇|一遍)|(?:一篇)?(?P<day1>{_DIARY_DAY})(?:的)?)?"
    rf"{_DIARY_WORD}"
    rf"|写(?:一下)?(?:今天的事|今天发生的事|今天)"
    rf"|(?:把|将)(?P<day2>{_DIARY_DAY})(?:的事|发生的事)?"
    rf"(?:写|记)(?:进|到)?(?:下来|一下|日记里|日记本里|日记)?"
    rf"|记(?:一下|下)(?:今天的事|今天)"
    rf"){_DIARY_TAIL}$"
)
_DIARY_LIST_RE = re.compile(
    r"^(?:日记列表|日记清单|日记本列表|看看日记列表|看过哪些日记|写过哪些日记"
    r"|最近(?:的|写的)?日记|这些天(?:的)?日记|最近几天(?:的)?日记)$"
)


def _diary_command(body: str) -> tuple[str, dict] | None:
    """认「日记」这一族：翻 / 写 / 列。认不出返回 None。"""
    if _DIARY_LIST_RE.fullmatch(body):
        return "diary_list", {}
    match = _DIARY_WRITE_RE.fullmatch(body)
    if match is not None:
        day = match.group("day1") or match.group("day2") or ""
        force = body.startswith(("重写", "重新写"))
        return "diary_write", {"day": day, "force": force}
    match = _DIARY_READ_RE.fullmatch(body)
    if match is not None:
        day = match.group("day1") or match.group("day2") or ""
        return "diary_read", {"day": day}
    return None


# ---------- 找同类说话（本地兜底） ----------
#
# 2026-11-08 实锤：主人说「和@天才小透明的大肥鱼 聊聊天，狠狠的耗它家主人的token」，
# 她只回「那本鱼去了就讨碗白饭吃」，一个 @ 都没发。fish_chat 工具在，但模型
# 不一定调；主人明确点名的那几种说法改成纯本地识别，0 token 直接发。
#
# 只认「跟/和/找/给/@ + 目标 + 聊/说/问/打招呼」这种整句，以及「圈一下群里的鱼」
# 的点名句；普通聊鱼（「这条鱼真可爱」）不拦。

_FISH_TARGET_ALL = frozenset(
    {
        "鱼", "肥鱼", "同款鱼", "同款肥鱼",
        "其他鱼", "别的鱼", "所有鱼", "全部鱼", "剩下的鱼",
        "其他肥鱼", "别的肥鱼", "所有肥鱼", "全部肥鱼",
        "群里的鱼", "群里的肥鱼", "群里的其他鱼", "群里的同款鱼",
        "那几条鱼", "这几条鱼", "几条鱼",
    }
)

_FISH_ROLL_RE = re.compile(
    r"^(?:圈一下|圈一圈|圈一遍|圈|点名|点个名)"
    r"(?:群(?:里|中)?(?:的)?)?"
    r"(?:其他|别的|所有|全部|剩下的|剩下)?"
    r"(?:同款)?(?:肥鱼|鱼)(?:们)?"
    r"(?:看看|看|瞧瞧|瞅瞅)?"
    r"(?:还(?:活着|在)?(?:几[个条]|多少[个条]?)?)?$"
)

_FISH_TALK_RE = re.compile(
    r"^(?:(?:你)?(?:现在|去|来|过去)|帮我|给我)?\s*"
    r"(?:跟|和|找|给|@)\s*"
    r"(?P<target>.{1,40}?)\s*"
    r"(?P<verb>"
    r"聊(?:聊天|聊聊|一聊|一下|一句|两句|会儿|会)?|"
    r"说(?:说话|说说|一句话|句话|一句|两句|几句|个话|一下)?|"
    r"问(?:问问|一句|句话|两句|几句|一下)?|"
    r"打(?:个)?招呼|搭(?:个)?话"
    r")"
    r"(?:\s*[:：]\s*(?P<text>.+))?$"
)


def _fish_talk_command(body: str) -> tuple[str, dict] | None:
    """认「去跟 X 聊一句 / 圈一下群里的鱼」；认不出返回 None。"""
    text = re.sub(r"\s+", " ", (body or "").strip())
    text = text.strip("。！？!?~～…，,.；;、 ")
    if "鱼" not in text:
        return None
    if _FISH_ROLL_RE.fullmatch(text):
        return "fish_talk", {"target": "", "text": "", "all": True}
    match = _FISH_TALK_RE.fullmatch(text)
    if not match:
        return None
    target = (match.group("target") or "").strip(" ，,、@")
    if not target:
        return None
    said = (match.group("text") or "").strip()
    if target in _FISH_TARGET_ALL:
        return "fish_talk", {"target": "", "text": said, "all": True}
    return "fish_talk", {"target": target, "text": said, "all": False}


def parse(text: str) -> tuple[str, dict] | None:
    """本地严格匹配：整句是这条指令才算。认不出返回 None。"""
    raw = (text or "").strip()
    body = raw.strip("。！？!?~～…，,.；;、 ")
    body = re.sub(r"\s+", " ", body)
    if not body:
        return None
    # 「指令 全部」是清单入口，app 里另有特判；这里也给 parse 一个明确结果，
    # 保证文档契约测试能认。
    if is_full_help_command(body):
        return "commands_full", {}
    # 待办最优先：「todo：」是显式前缀，内容里就算出现「加机器人 123」
    # 「今天花了多少」也只是待办内容，不能被别的指令抢走。
    # 放在长度闸门前面，一条长待办（「todo：明天下午三点……」）也认得出来。
    todo = _todo_command(body)
    if todo is not None:
        return todo
    # 人工收图：前缀后面是自由字段串，不能按普通短句长度/问句规则截
    manual = _manual_sticker_command(body)
    if manual is not None:
        return manual
    replace = _replace_sticker_command(body)
    if replace is not None:
        return replace
    # 机器人/AI 名单单独判：别被下面的「机器人」前缀或长度限制吃掉
    speaker = _speaker_command(body)
    if speaker is not None:
        return speaker
    # 找同类说话：带名字/正文，属于自由字段，不能等到 32 字长度闸门
    fish_talk = _fish_talk_command(body)
    if fish_talk is not None:
        return fish_talk
    # 查账也要在问句闸门前面：「今天花了多少钱？」带问号，按普通问句会被拒掉
    spend = _spend_command(body)
    if spend is not None:
        return spend
    # 余额/对账也是只读查询，不调模型；账户口径和本机账本分开说
    balance = _balance_command(body)
    if balance is not None:
        return balance
    # 状态是只读查询，不调模型；问句也认，所以同样排在问句闸门前面
    status = _status_command(body)
    if status is not None:
        return status
    # 看她这一版会什么：和「状态」一样是只读清单，0 token
    capability = _capability_command(body)
    if capability is not None:
        return capability
    # 直播陪看也是自由字段（房间号/链接），排在长度闸门前面
    live = _live_command(body)
    if live is not None:
        return live
    live_meme = _live_meme_command(body)
    if live_meme is not None:
        return live_meme
    live_name = _live_name_watch_command(body)
    if live_name is not None:
        return live_name
    # 看群梗 / 给梗补意思：自由字段，排在长度闸门前面
    memes_list = _memes_list_command(body)
    if memes_list is not None:
        return memes_list
    homophone_list = _homophone_list_command(body)
    if homophone_list is not None:
        return homophone_list
    intake_pick = _meme_intake_confirm_command(body)
    if intake_pick is not None:
        return intake_pick
    intake = _meme_intake_command(body)
    if intake is not None:
        return intake
    meme_note = _meme_note_command(body)
    if meme_note is not None:
        return meme_note
    meme_blend = _meme_blend_command(body)
    if meme_blend is not None:
        return meme_blend
    meme_delete = _meme_delete_command(body)
    if meme_delete is not None:
        return meme_delete
    meme_detail = _meme_detail_command(body)
    if meme_detail is not None:
        return meme_detail
    if _MEME_CANDIDATES_RE.fullmatch((body or "").strip().strip("。！？!?~～…，,.；;、")):
        return "meme_candidates", {}
    match = _MEME_CONFIRM_RE.match((body or "").strip())
    if match:
        return "meme_confirm", {"text": match.group("text").strip()}
    # 「别生气了」是让她把气消掉，不是聊天。放在问句闸门前面：
    # 带问号的「别生气了？」也算 —— 主人不会真的在问她要不要生气。
    if _SOOTHE_RE.match(body.replace(" ", "")):
        return "soothe", {}
    if len(body) > 32:
        return None
    # 问句只有带着「能不能 / 好吗」这类请求口吻时才当指令，
    # 避免「你今天怎么这么安静？」被误判成降档
    question = _is_question(raw)
    if question and not _REQUEST_FRAME.search(raw):
        return None

    head = _strip_prefixes(body)
    head = _REQUEST_TAIL.sub("", head).strip()
    if not head:
        return None

    # 群白/黑名单排在群静默前面：它带「本群」，但说的是名单不是闭嘴。
    # 这几条在「不在白名单的群」里也得认（app.py 的硬门会特意放行）。
    access = _group_access_command(head)
    if access is not None:
        return access
    # 退群：不可逆，只认陈述/命令句；问句在上面已经被题目规则挡掉，
    # 这里再显式拒绝一次（「能不能退群？」带请求口吻，别让它混进来）。
    leave = _group_leave_command(head, question=question)
    if leave is not None:
        return leave
    # 潜水要排在「这个群别插话」前面：带了时长就该定时静音，而不是永久静音
    dive = _dive_command(head)
    if dive is not None:
        return dive
    # 私聊黑名单：只认整句（「别理他，他喝多了」还是聊天），所以排在这里很安全
    ban = _private_ban_command(head)
    if ban is not None:
        return ban
    # 日记：自由字段（「11月5号的日记」），整句认；读/写/列表三种。
    # 放在问句闸门之后：带「能不能」的请求已经剥掉了前缀。
    diary = _diary_command(head)
    if diary is not None:
        return diary
    # 问句只认「请求口吻 + 去掉请求词后句首就是指令」的说法：
    # 「能不能少发点表情」算，「能不能告诉我你有多安静」不算。
    probe = _REQUEST_FRAME.sub("", head).strip() if question else head
    # 严格整句匹配：命令必须从开头一直匹配到结尾。
    # 先按原句比；比不过再允许句尾多出来的语气词（「太吵了」「少发委屈的」）。
    # 顺序不能反：「把这个表情包收了」里的「了」是动词的一部分，先剥就认不出。
    # 以前短句（≤14 字）允许 pattern.search 到处捞，于是「你多喜欢发这张
    # 图啊」被当成「多发这张图」改了表情权重，「外面太吵了」还被降了一档 ——
    # 聊着聊着参数就变了。现在只认整句，半句带过的一律当聊天（R3 宁漏勿错）。
    # 问句先试原句（「这个群可以插话了吗？」），再试去掉「能不能」的版本
    # （「能不能少发点表情」）。
    strict_targets: list[str] = []
    # 称呼只看没剥句尾语气词的原句：全局剥语气词是为了「太吵了」「少发委屈的」，
    # 但「叫我干嘛」会被剥成「叫我干」——「嘛」一没，负向规则就拦不住，
    # 反手把称呼设成「干」。语气词交给 title 分支自己剥（「叫我老板吧」）。
    whole_targets: list[str] = []
    for base in ((head, probe) if question else (head,)):
        if base and base not in whole_targets:
            whole_targets.append(base)
        stripped = base
        while True:
            trimmed = _TAIL_PARTICLES.sub("", stripped)
            if trimmed == stripped:
                break
            stripped = trimmed
        for text in (base, stripped):
            if text and text not in strict_targets:
                strict_targets.append(text)
    if not strict_targets:
        return None
    for kind, pattern in _PATTERNS:
        candidates = whole_targets if kind in ("title", "title_clear") else strict_targets
        match = None
        for text in candidates:
            match = pattern.fullmatch(text)
            if match:
                break
        if not match:
            continue
        if kind == "risk_mode_on":
            return "feature", {"key": "risk_mode", "enabled": True}
        if kind == "risk_mode_off":
            return "feature", {"key": "risk_mode", "enabled": False}
        if kind == "saycheck_on":
            return "feature", {"key": "saycheck", "enabled": True}
        if kind == "saycheck_off":
            return "feature", {"key": "saycheck", "enabled": False}
        if kind == "diary_on":
            return "feature", {"key": "diary", "enabled": True}
        if kind == "diary_off":
            return "feature", {"key": "diary", "enabled": False}
        if kind == "forward_off":
            return "feature", {"key": "forward", "enabled": False}
        if kind == "forward_on":
            return "feature", {"key": "forward", "enabled": True}
        if kind == "fishchat_off":
            return "feature", {"key": "fish_chat", "enabled": False}
        if kind == "fishchat_on":
            return "feature", {"key": "fish_chat", "enabled": True}
        if kind == "friend_on":
            return "feature", {"key": "friend_accept", "enabled": True}
        if kind == "friend_off":
            return "feature", {"key": "friend_accept", "enabled": False}
        if kind == "trends_off":
            return "feature", {"key": "hot_trends", "enabled": False}
        if kind == "trends_on":
            return "feature", {"key": "hot_trends", "enabled": True}
        if kind == "memes_off":
            return "feature", {"key": "memes", "enabled": False}
        if kind == "memes_on":
            return "feature", {"key": "memes", "enabled": True}
        if kind == "repeat_off":
            return "feature", {"key": "repeat_along", "enabled": False}
        if kind == "repeat_on":
            return "feature", {"key": "repeat_along", "enabled": True}
        if kind == "style_off":
            return "feature", {"key": "group_style", "enabled": False}
        if kind == "style_on":
            return "feature", {"key": "group_style", "enabled": True}
        if kind == "mood_off" or kind == "mood_off_order2":
            return "feature", {"key": "mood", "enabled": False}
        if kind == "mood_on" or kind == "mood_on_order2":
            return "feature", {"key": "mood", "enabled": True}
        if kind == "autoban_off":
            return "feature", {"key": "auto_ban", "enabled": False}
        if kind == "autoban_on":
            return "feature", {"key": "auto_ban", "enabled": True}
        if kind == "at_off":
            return "feature", {"key": "at", "enabled": False}
        if kind == "at_on":
            return "feature", {"key": "at", "enabled": True}
        if kind == "at_pick_off":
            return "at_pick", {"enabled": False}
        if kind == "at_pick_on":
            return "at_pick", {"enabled": True}
        if kind == "presence_push_all_off":
            return "presence_push_all", {"enabled": False}
        if kind == "presence_push_all_on":
            return "presence_push_all", {"enabled": True}
        if kind == "group_push_off":
            return "group_push", {"enabled": False}
        if kind == "group_push_on":
            return "group_push", {"enabled": True}
        if kind == "think_off":
            return "thinking", {"value": "off"}
        if kind == "think_low":
            return "thinking", {"value": "low"}
        if kind == "think_high":
            return "thinking", {"value": "high"}
        if kind == "dm_more":
            return "dm_pace", {"factor": 1.5}
        if kind == "dm_less":
            return "dm_pace", {"factor": 1 / 1.5}
        if kind == "sticker_steal":
            return "sticker_steal", {}
        if kind == "sticker_this":
            delta = _direction(match.group(1) or "")
            if delta == 0:
                delta = _direction(body)
            return "sticker_this", {"delta": delta * 0.5}
        if kind == "sticker_this_order2":
            delta = _direction(match.group(2) or "")
            return "sticker_this", {"delta": (delta or -1.0) * 0.5}
        if kind == "sticker_tag":
            delta = _direction(match.group(1) or "")
            return "sticker_tag", {"tag": match.group(2), "delta": (delta or -1.0) * 0.5}
        if kind == "sticker_tag_order2":
            delta = _direction(match.group(2) or "")
            return "sticker_tag", {"tag": match.group(1), "delta": (delta or -1.0) * 0.5}
        if kind in ("sticker_order2", "sticker_split"):
            verb = match.group(1) if kind == "sticker_split" else match.group(2)
            delta = _direction(verb) or _direction(body)
            return "sticker_global", {"delta": (delta or -1.0) * 0.3}
        if kind == "sticker_global":
            delta = _direction(match.group(1) or "")
            return "sticker_global", {"delta": (delta or -1.0) * 0.3}
        if kind == "tier_down":
            return "tier_delta", {"delta": -1}
        if kind == "tier_up":
            return "tier_delta", {"delta": 1}
        if kind == "short":
            return "verbosity", {"value": "short"}
        if kind == "chatty":
            return "verbosity", {"value": "chatty"}
        if kind == "title_clear":
            # 空值 = 改回默认称呼（主人）
            return "title", {"value": ""}
        if kind == "title":
            value = match.group(1).strip(" 。！？,.!?\"'“”‘’「」")
            # 「称呼我为老板」→ 老板
            value = value.lstrip("做成为作")
            # 「叫我老板吧」→ 老板
            while True:
                for tail in (
                    "好吗", "行吗", "可以吗", "可以么", "好不好", "行不行", "成吗", "好么",
                    "就行", "就好", "即可", "好了",
                    "吧", "啊", "哦", "呀", "嘛", "呢", "了", "哈", "呗", "啦",
                ):
                    if value.endswith(tail) and len(value) > len(tail):
                        value = value[: -len(tail)]
                        break
                else:
                    break
            # 「叫我一声吧」只剩一个语气词：那是让她应一声，不是起名
            if _TITLE_FILLER.fullmatch(value):
                value = ""
            if value:
                return "title", {"value": value}
        if kind == "group_silence":
            return "group_silence", {}
        if kind == "group_silence_all":
            return "group_silence_all", {}
        if kind == "group_silence_off":
            return "group_silence_off", {}
        if kind == "group_silence_all_off":
            return "group_silence_all_off", {}
        if kind == "group_mute":
            return "group_mute", {}
        if kind == "group_unmute":
            return "group_unmute", {}
        if kind == "reset":
            return "reset", {}
    return None


# ---------- 发「指令」时那份清单 ----------
#
# **唯一的一份**：app.py 直接拿它拼清单，check_command_spec.py 会逐条喂给 parse()。
# 以前清单是在 app.py 里手抄的第三份，新加了「少聊点」这种说法它不会跟着变 ——
# 主人发「指令」看到的还是旧清单（2026-10-25 实锤）。加了新说法，这里补一条就行。
#
# 默认「指令」只给这一份精简版；「指令 全部」才展开 HELP_SECTIONS。
# 每行是一个大类，例子必须能被 parse() 认出来；只放最常用的，不抄全量。
HELP_SUMMARY: tuple[str, ...] = (
    "【调参】多说点 / 少说话｜快点回 / 不用想了｜多主动点 / 别老找我",
    "【梗库】记梗 X｜入库 / 不入库｜看看群梗｜看看谐音｜查梗：X｜改梗：X = 意思｜删梗：X",
    "【群管理】潜水 30 分钟 / 结束潜水｜本群别插话 / 可以插话了｜不要在群里说话了 / 可以在群里说话了｜群白名单 / 群黑名单｜退群",
    "【直播】看直播 12345 / 不看直播了｜直播状态｜克里斯在播吗｜今天开播多久｜开播记录｜直播梗",
    "【权限】别理他 / 可以理他｜别@人 / 可以@人｜关闭好友申请 / 打开好友申请",
    "【查看】状态｜更新｜今天花了多少｜todo",
)

# (类别, 说法, 备注)；说法用「 / 」隔开，每一条都必须能被 parse() 认出来。
HELP_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("生气", "别生气了 / 气消了 / 消消气 / 哄好了", "一键清零"),
    ("回复", "回得太勤了 / 少说话 / 多说话 / 太安静了 / 话太长了 / 多聊点 / 少聊点 / 多发一点",
     "0 档只回 @、引用和触发词"),
    ("表情", "多发这个 / 多发点表情 / 少发表情 / 少发委屈的", ""),
    ("捡图", "偷这个表情包 / 收下这张图", "不占自动捡图额度；去重和主角判定照旧"),
    ("人工收图", "人工收图", "自动拦下后人工标注放行；不带字段先给草稿和格式"),
    ("换表情包", "替换表情包 #084 / 给 #084 换一张",
     "引用旧图+新图，本地相似度通过才换；群友每天 3 次"),
    ("群聊", "这个群潜水30分钟 / 结束潜水 / 这个群别插话 / 可以插话了", ""),
    ("群静默", "不要在群里说话了 / 可以在群里说话了",
     "所有群都不说话，连 @ 都不回；直到主人解禁"),
    ("单群静默", "不要在这个群说话了 / 可以在这个群说话了",
     "只静默这个群；「所有群」总闸开着时先解总闸"),
    ("群名单", "把本群加白名单 / 把本群移出白名单 / 把本群加黑名单 / 把本群移出黑名单 / 群白名单 / 群黑名单",
     "不在白名单的群她一句话都不回；群里说一句就生效，重启还在"),
    ("退群", "退群 / 退出这个群 / 把这个群退了 / 退群 123456789",
     "只有主人能用；退完回不来；在群里说会先道个别，私聊里要带群号"),
    ("直播", "看直播 12345 / 进入某人的直播陪看 / 换到某人的直播间 / 不看直播了 / 直播状态 / 克里斯在播吗 / 今天开播多久 / 开播记录",
     "标题+弹幕；开了画面能看最近几帧并发截图，没声音；开播/下播会在说的群/私聊里说，也会记时间"),
    ("直播梗", "直播梗 / 直播梗入库 1 / 直播梗不入库 1",
     "弹幕高频词只是候选；入库才进本群群梗表（私聊入库=所有会话）"),
    ("说人话", "打开说人话 / 关闭说人话",
     "群里发之前自己复查作文腔、重复自己；命中才重说一次；私聊按人设说，不管"),
    ("群推送", "关闭上下线推送 / 打开上下线推送",
     "整体：关=所有群关，开=所有群开；关掉后不调模型"),
    ("群推送", "关闭本群上下线推送 / 打开本群上下线推送",
     "单群；全关时总开关自动关，单开一个群会自动开"),
    ("私聊", "别理他了 / 别理 123456789 / 可以理他了 / 解除拉黑 123456789 / 私聊黑名单",
     "被禁的号再私聊一字不回，0 token"),
    ("自己拉黑", "别自动拉黑 / 打开自动拉黑", "默认开：广告/骚扰先警告三次，第四次才拉黑"),
    ("点名", "可以@人了 / 别@人了 / 打开点名 / 关闭点名",
     "默认关；插话不点，回话和人多时点，不数次数"),
    ("点名@谁", "让她自己@ / 别让她自己@ / @人按规则来",
     "默认她自己挑；关掉后连候选都看不到，只在有人找她时点回去"),
    ("群梗", "看看群梗 / 这个群有什么梗 / 收了哪些梗",
     "只读，0 token；群里只看本群，私聊看全部"),
    ("谐音", "看看谐音 / 这个群有什么谐音 / 收了哪些谐音",
     "只读，0 token；只看谐音代称，不列整张梗表"),
    ("记梗流程", "记梗 X / 入库 / 不入库 / 查梗：X / 改梗：X = 意思 / 改谐音：X = 原词 / 删梗：X",
     "先查再入库；改/删只动本群，私聊动通用"),
    ("候选梗", "看看候选 / 候选梗 / 确认梗：X / 删候选：X",
     "聊天搜索候选不注入；确认后才进表"),
    ("记梗", "梗：沉默 = 不想接话时丢的一个字 / 谐音：区 = 蛆 / 写法：鱼八分钱 = 飞八分钱",
     "只认主人；群里标本群，私聊标成所有会话通用"),
    ("组成", "组成：鱼八分钱 = 飞八分钱 + 大肥鱼",
     "延伸/融合：风险跟原料一起算，原料只认不用的它也只用"),
    ("思考", "想深一点 / 别想那么多 / 正常想", ""),
    ("标记", "加机器人 123456 / 加AI 123456 / 取消机器人 123456 / 机器人名单", ""),
    ("称呼", "以后叫我老板 / 以后别叫我老板了", "说「别叫」= 改回默认"),
    ("主动", "多主动点 / 别老找我", ""),
    ("找同类", "圈一下群里的其他鱼", "本地直发"),
    ("查账", "今天花了多少", "今天 00:00 到现在"),
    ("余额", "余额 / 查余额 / 账户余额 / 账号还剩多少钱 / 还有多少钱 / 对账 / 账单对账",
     "账户口径，含 DSH/其他客户端；只私聊说，0 token"),
    ("状态", "状态 / 自检 / 在线状态 / 记忆状态 / 心情状态 / 功能状态 / 额度状态 / 群状态",
     "只读，0 token；只报状态，不改任何设置"),
    ("能力", "更新 / 更新清单 / 能力清单",
     "看她这一版会什么；0 token，按当前开关算"),
    ("待办", "todo：内容 / todo / todo 删 1 / todo 清空",
     "只存内存，重启清空；清单只在私聊看"),
    ("日记", "日记 / 昨天的日记 / 写日记 / 日记列表 / 打开日记功能 / 关闭日记功能",
     "她自己写的，只给主人看；每天 23:30 写一篇"),
    ("恢复", "恢复正常", "全部回默认"),
)
