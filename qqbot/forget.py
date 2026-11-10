"""记忆的遗忘曲线。

人不会记住所有事，但会记住**反复想起的事**。这里用两条规则模拟：

1. **短期记忆**：强度随时间指数衰减，低于阈值就不再被召回
2. **反复想起会加固**：同一个人隔一阵子又提起，才算"又想起一次"；
   次数够了才升级成长期记忆

"又想起一次"是有门槛的（见 `Memory._touch`）：必须是**用户自己说的**、
不是寒暄/问句、隔了足够久、而且是因为**同一个词**被命中。
本鱼自己的回复永远不进长期记忆 —— 那是聊天，不是关于这个人的事实。

长期记忆不衰减，永久保留。

    kind, strength = classify("我最喜欢喝咖啡")     # 事实类 → 长期
    kind, strength = classify("今天好热")           # 闲聊   → 短期
    effective(strength, kind, last_access, now)     # 现在的强度
"""

from __future__ import annotations

import math
import re

# 短期记忆的衰减速率（每天）。0.08 ≈ 半衰期 8.7 天
DEFAULT_RATE = 0.08
# 低于这个强度就算忘了
DEFAULT_THRESHOLD = 0.15
# 长期记忆的强度（不衰减）
LONG_STRENGTH = 10.0
# 想起一次涨多少
RECALL_BOOST = 0.5
# 强度上限（短期记忆撑不到长期记忆那么高，避免永远不过期）
SHORT_MAX = 2.0
# 长期记忆（以及"值得转长期"的候选）至少要有这么长。
# 聊天记录在库里是「【群友】昵称：正文」，比长度前先剥掉这层壳。
MIN_LONG_CHARS = 12

_TIME_HINT = (
    r"(?:明天|后天|今晚|明晚|下周|下个月|周末|周[一二三四五六日]"
    r"|[一二三四五六七八九十\d]+[号点日]|回头|到时候)"
)

# 一看就是"值得长期记住"的信号。
# 只认**第一人称 + 具体对象**：宁可漏记，也别把"我喜欢这个皮肤""我是真的服了"
# 这种当下情绪当成一辈子的偏好 —— 后者过两天就没人提了。
_LONG_PATTERNS = [
    # 偏好：后面必须跟具体东西，不能是"这个/那种/你/他"这类指代
    re.compile(
        r"我(?:很|最|超|挺|特别|真的)?(?:喜欢|爱|讨厌|不喜欢|受不了|怕)"
        r"(?![这那它你他她谁啥啊呀吧呢了])[^\s，。！？,.!?]{1,}"
    ),
    # 称呼上的雷点
    re.compile(r"别(?:再)?叫我[^\s，。！？,.!?]{1,12}"),
    # 约定：得带时间/地点，才像"约好了"；"记得想我""记得说我是好女孩"不算
    re.compile(r"(?:记得|别忘了|说好了|约好了)" + _TIME_HINT),
    # 身份 / 住处 / 工作 / 宠物：只认第一人称。"叫我爸爸"不是在自我介绍
    re.compile(r"我(?:叫(?!我|你|他|她|它|什么)|住在|养了|家有)"),
    re.compile(r"我在[^\s，。！？,.!?]{1,10}(?:上班|上学|工作|读书)"),
    # 个人资料：必须带"我"，光提到"生日""电话"多半是在聊别人的事
    re.compile(r"我(?:的)?(?:生日|电话|手机号|地址|密码|工资|身份证)"),
    re.compile(r"我(?:今年)?\d{1,2}岁"),
    # 家里的人：必须带"我"；"叫我爸爸""当你妈"不算自我陈述
    re.compile(
        r"(?<![叫喊当认做])我(?:的)?"
        r"(?:女朋友|男朋友|老婆|老公|对象|孩子|儿子|女儿|爸爸|妈妈|老爸|老妈|哥哥|姐姐|弟弟|妹妹)"
    ),
]

# 寒暄、天气、情绪这类，本来就不该记太久
_SHORT_PATTERNS = [
    re.compile(r"^(?:在吗|在不在|你好|您好|早安|晚安|早|好|嗯|哦|哈+|草|6|？|\?)"),
    re.compile(r"(?:天气|好热|好冷|下雨|下雪|吃了吗|睡了吗)"),
]

# 每句话都带的说话人前缀：先剥掉，长度和内容才看得准
_TAGS = re.compile(r"^(?:【[^】]{1,16}】\s*)+")
_NAME = re.compile(r"^[^：:\n]{1,24}[：:]\s*")
_MERGE_NOTE = re.compile(r"（这是连着发的几条，当成一段话一起看）\s*$")
_QUESTION_TAIL = re.compile(r"(?:[吗嘛么呢][？?]?|[？?])$")
# 不算"问号结尾"的问句：克里斯是不是区 / 这枪是哪一把 / 为什么……
_QUESTIONISH = re.compile(r"(?:是不是|有没有|能不能|要不要|会不会|什么|为什么|怎么|哪[个把里些儿]?|谁|多少)")


def body_of(text: str) -> str:
    """去掉「【群友】昵称：」这层壳，只留这个人真正说的话。

    只看**第一行**：转发/引用进来的聊天记录是别人的话，不该拿来判长期。
    """
    body = (text or "").strip()
    body = _TAGS.sub("", body)
    body = _NAME.sub("", body, count=1)
    body = _MERGE_NOTE.sub("", body)
    return body.split("\n", 1)[0].strip()


def worth_promoting(text: str, min_chars: int = MIN_LONG_CHARS) -> bool:
    """这条够不够格升成长期记忆。

    过短、寒暄、一句问话 —— 都不算"值得记一辈子的事"。
    """
    body = body_of(text)
    if len(body) < max(1, min_chars):
        return False
    if any(pattern.search(body) for pattern in _SHORT_PATTERNS):
        return False
    if _QUESTION_TAIL.search(body) or _QUESTIONISH.search(body):
        return False
    return True


def classify(text: str, role: str = "user") -> tuple[str, float]:
    """判断这条记忆该记多久。返回 (kind, 初始强度)。

    role 是 assistant 时一律短期：她自己的俏皮话再可爱，也不是"关于你的事实"。
    """
    if role != "user":
        return "short", 1.0
    body = body_of(text)
    if len(body) < 3:
        # 「嗯」「在吗」「6」——再短就只是语气，起始强度给低一点
        return "short", 0.6
    # 问句里的"我生日""别叫我"是在问，不是在陈述
    if not _QUESTION_TAIL.search(body):
        for pattern in _LONG_PATTERNS:
            if pattern.search(body):
                return "long", LONG_STRENGTH
    for pattern in _SHORT_PATTERNS:
        if pattern.search(body):
            return "short", 0.6
    # 长消息里信息量一般更大，给一点余量
    return "short", 1.0 if len(body) < 40 else 1.3


def effective(
    strength: float,
    kind: str,
    last_access: float,
    now: float,
    rate: float = DEFAULT_RATE,
) -> float:
    """现在还剩多少"记得住"。长期记忆永远满格。"""
    if kind == "long":
        return LONG_STRENGTH
    days = max(0.0, (now - float(last_access or 0)) / 86400.0)
    return max(0.0, float(strength or 0.0)) * math.exp(-max(0.0, rate) * days)


def bump(strength: float, kind: str) -> float:
    """被想起一次：加一点强度。"""
    if kind == "long":
        return LONG_STRENGTH
    return min(SHORT_MAX, float(strength or 0.0) + RECALL_BOOST)
