"""指代问句：短、没有新主语，问的其实是上一句里的那个东西。

为什么要有这个文件
-----------------
2026-10-28 群里的现场（主人记 todo 的前两分钟）：

    群友：飞八分钱是什么意思
    她  ：这个本鱼知道是骂人的话，就不复述了呀
    群友：是骂谁的
    她  ：这个本鱼不乱指人呀，你问说是谁说出来的嘛
    群友：是谁说出来的
    她  ：（先按事实问题搜了一遍网页，搜的就是「是谁说出来的」）本鱼不说呀……

「是骂谁的」「是谁说出来的」都不是新问题：主语在上一句里，「谁」问的是那个词指向谁。
本地能认出这种句子，就能少做两件错事：

- **不预搜**：拿「是谁说出来的」去查网页，什么有用的都查不到（见 searchgate.py）；
- **把上一句递给她**：明说「这句是追问，指代的是上一句的 X」，她就不会把
  「骂谁」听成「谁说的」，也不会反问「你说的是哪个」。

2026-10-29 又多了一种：**连人称代词都省了**。群里的原话：

    群友：一个人名艺术家是低手 记住了吗
    她  ：行吧，低手就低手，本鱼听你们说的嘛
    群友：低手是谁
    她  ：不就是你自己说的嘛，本鱼跟着喊的呀
    群友：名字叫什么
    她  ：本鱼呀，网上都叫本鱼蓝色大肥鱼

「名字叫什么」没有「他 / 这个」，主语和定语全省了；旧规则只认带代词的说法，
这句就漏过去，她把它理解成在问自己的名字。现在 ``followup_kind()`` 分两种：

- ``pronoun``：句子里有他 / 她 / 它、这个 / 那个，或者以「是……」回指开头；
- ``ellipsis``：一个代词都没有，只剩疑问词 + 属性名词 / 轻动词
  （「名字叫什么」「多少钱」「在哪」「为什么」「骂谁」），靠上一轮补主语。

两种都只给**一句提示**，不复述整段历史 —— 主人 2026-10-29 在群里提醒过
「上下文贵」。``searchgate`` 也不再拿这种半句话去搜网页。

只做判定和提示，不改她的语气 —— 语气归 persona。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

# 句首就是指代/回指的说法：那、这、是……、他/她/它。
_REF_START = (
    "是", "那", "这", "他", "她", "它", "还", "后来", "然后", "结果", "所以",
    "上面", "前面", "刚才", "你刚", "刚说",
)
# 句中带这些词，也算在回指上一句（「你刚才说的那个是什么意思」）。
_REF_TOKENS = (
    "这个", "那个", "这些", "那些", "这句话", "那句话", "上面说", "前面说",
    "刚才说", "你刚说", "说的那个", "指的是",
)
# 得像个问句：有疑问词或问号。
_ASK = ("谁", "什么", "啥", "哪", "怎么", "多少", "吗", "呢", "么")

# 「多大 / 多高 / 多久 / 什么时候」也是疑问词，但不在 _ASK 里；
# 漏了它们，「多大了」「多高」就进不了追问判定。
_WH = (
    "为什么", "为啥", "什么时候", "什么", "干啥", "啥", "谁",
    "哪里", "哪儿", "哪", "怎么", "咋", "多少", "多久", "多大", "多高",
    "多重", "多远", "多贵", "几", "如何", "怎么样", "咋样", "什么样",
    "干什么", "做什么",
)

# 这些不算指代问句：是/不是、有没有 这种自足的验证问句，该查就查。
_VERIFY = (
    "是不是", "有没有", "能不能", "会不会", "可不可以", "行不行", "对不对",
    "真的", "假的", "是吗", "对吧", "好吧", "是不是真的",
)

# 属性名词：它们自己不构成新话题，省掉的是「谁的 / 哪个的」。
# 「名字叫什么」「多大了」去掉这些之后就不剩实词了，才能算追问。
_ATTR = (
    "名字", "姓名", "年龄", "年纪", "岁数", "价格", "价钱", "钱", "地址",
    "电话", "手机号", "号码", "样子", "长相", "身高", "体重", "意思", "原因",
    "理由", "时间", "时候", "地方", "位置", "工作", "职业", "版本", "水平",
    "岁", "名", "家",
)

# 去掉这些之后还剩内容，说明句子里有新主语，不是纯指代。
# 长词放前面先删，避免「出来」被拆成「出」「来」。
_STOP = (
    "他们", "她们", "它们", "这个", "那个", "这些", "那些", "刚才", "上面",
    "前面", "后来", "然后", "什么", "为什么", "怎么样", "怎么", "多少", "哪个",
    "哪些", "哪", "谁", "啥", "是", "的", "了", "着", "过", "吗", "呢", "吧",
    "啊", "呀", "嘛", "么", "哦", "我", "你", "他", "她", "它", "这", "那",
    "个", "些", "位", "条", "句", "还", "又", "就", "也", "都", "在", "有",
    "会", "能", "要", "和", "跟", "给", "说", "讲", "发", "叫", "出来",
    "一下", "到底", "究竟", "意思", "回事", "多大", "多高", "多重", "多远",
    "多贵", "多久", "几岁", "什么时候", "哪里", "哪儿", "咋样", "结果", "所以",
    *_ATTR,
)

# 省略句（一个代词都没有）里允许出现的词：疑问词、属性名词、轻动词、虚词。
# 整句都得由这些拼成，才说明没有新主语 —— 多一个实词就去搜，别乱递上下文。
_ZERO_EXTRA = (
    *_ATTR,
    "人", "种", "款", "部", "首", "一",
    # 轻动词 / 谓语：光有它和疑问词，主语仍然是上一句的
    "出来", "起来", "干什么", "做什么", "干嘛", "叫", "说", "讲", "骂",
    "做", "干", "弄", "搞", "整", "办", "来", "去", "在", "有", "是",
    "会", "能", "要", "想", "给", "发", "当", "成", "算", "用", "玩",
    "买", "卖", "看", "找", "拿", "回", "上", "下", "出", "到", "得",
    "住", "长", "写", "画", "唱", "跳", "打", "死", "活", "混", "回事",
    # 时间问句单独成词，不把「几点」算成省略句的疑问词
    "几点", "几号", "几月", "几年", "星期几",
    # 语气 / 结构词
    "一下", "的", "了", "着", "过", "啊", "呀", "呢", "吗", "嘛", "哦",
    "吧", "么", "哈", "啦", "嘞", "呗", "哟", "喔", "唉", "还", "又",
    "就", "也", "都", "到底", "究竟", "然后", "后来", "结果", "所以",
    "个", "些", "位", "条", "句",
)
_ZERO_VOCAB = tuple(sorted({*_WH, *_ZERO_EXTRA}, key=lambda w: (-len(w), w)))

# 这些是一个人也能说的完整短句，不是在接上一句：
# 「在吗」「吃了吗」是招呼，「几点了」是问时间，别把上一句硬塞给她。
_STANDALONE = (
    "在吗", "在么", "在嘛", "在不在", "吃了吗", "吃了没", "睡了吗", "睡了没",
    "干嘛呢", "干嘛呀", "干啥呢", "干啥呀", "干什么呢", "做什么呢",
    "有事吗", "有事么", "啥事",
    "啥事儿", "几点了", "几点", "几号", "几月", "几年", "星期几",
    "今天几号", "现在几点",
)

# 省略句开头那个不认识的实词，只能是「狗在哪 / 鱼多少钱」里的人或物，
# 不能是「吃什么 / 看什么」里的动词 —— 后者是没主语的新问题，不该硬接上一句。
_FIRST_SKIP_OK = (
    "在", "哪", "去", "到", "是", "叫", "多", "几", "名字", "叫什么",
    "叫啥", "什么名字", "什么地方", "什么时候", "什么价格", "多少钱",
    "在哪", "哪里", "哪儿", "去哪", "到哪", "是哪", "多大", "多高",
    "多久", "多远", "多贵",
)

# 第一/二人称一出现，就是在明确问「你 / 我」，不要按省略句改写成上一轮。
_PERSONAL = ("我", "你", "您", "咱", "他", "她", "它")

# 一句里最多这么长；再长就有自己的主语了。
_MAX_LEN = 24
# 省略句更短：十几个字还没主语，基本就是在接上一句。
_ZERO_MAX_LEN = 14
# 提示里引用上一句时最多带多少字，别把整段转发塞进上下文。
_PREVIEW = 36
# 上一句里最多取出几个字的「话题」（「低手是谁」→「低手」）。
_TOPIC_MAX = 16

# 「这关 / 这图 / 这boss」：明确在指刚才那一轮的对象，不是新话题。
# 2026-11-07 11:03 群 123456789：刚聊完 JT8-3，下一句「这关boss是谁」被
# 跟进窗口判成「话题已转」直接沉默了。这里的词是「接着说」，不是「换话题」。
_TOPIC_REF = (
    "这一关", "这个关卡", "这个关", "这关", "这图", "这个图",
    "这boss", "这个boss", "这阵容", "这个阵容", "这套", "这队", "这波",
    "这角色", "这个角色", "这干员", "这个干员",
)


def refers_to_topic(text: str) -> bool:
    """这句是不是用「这关 / 这图 / 这boss」在指刚才那一轮。0 token。"""
    body = _normalize(text)
    return bool(body) and any(word in body for word in _TOPIC_REF)


# 上一轮在聊直播时，「房间 / 观众 / 人数」说的是直播间，不是字面意思。
# 2026-10-29 22:10 现场：
#
#     群友：直播间标题是什么
#     她  ：标题本鱼刚说过的呀，「暗区之少女前线」嘛
#     群友：房间观众有几个
#     她  ：本鱼没数过呀，你是问豆瓣上多少人评《房间》吗
#
# 「房间」被当成了电影名。窗口历史里明明有上一句「直播间标题是什么」，
# 但没人告诉她「房间」就是那个直播间 —— 这里补一句，0 token。
_LIVE_WORDS = ("直播间", "直播", "开播", "在播", "主播", "弹幕", "房号", "房间号")
_VIEWER_WORDS = ("观众", "人数", "人气", "在线")
_ROOM_PRICE_WORDS = ("钱", "价", "租", "酒店", "宾馆", "民宿", "订")


def asks_room_viewers(text: str) -> bool:
    """这句是不是在问「直播间里有多少人」。纯规则、0 token。

    只认带「房间 / 直播间」+「观众 / 人数 / 人气 / 在线」的问法；
    「房间几个人」这种省了「观众」的也算，但得排除问房价、租房、
    房间号的 —— 「房间号是多少」问的是号，不是人数。
    """
    body = _normalize(text)
    if not body:
        return False
    if "房间" not in body and "直播间" not in body:
        return False
    if not any(word in body for word in _VIEWER_WORDS):
        # 「房间几个人 / 直播间现在多少人」：省了「观众」，但得真的在问人
        if not re.search(r"(?:几个|多少)人", body):
            return False
        if any(word in body for word in _ROOM_PRICE_WORDS):
            return False
    if ("房间号" in body or "房号" in body) and not any(
        word in body for word in _VIEWER_WORDS
    ):
        return False
    return True


def recent_lines(history: Iterable[dict[str, Any]], limit: int = 8) -> list[str]:
    """把结构化历史折成纯文本行（去掉行首说话人标记），给上下文判定用。"""
    out: list[str] = []
    for item in list(history or [])[-limit:]:
        text = _strip_speaker(str(item.get("content") or ""))
        if text:
            out.append(text)
    return out


def live_room_hint(text: str, recent: Iterable[str], limit: int = 8) -> str:
    """上一轮在聊直播、这一句问「房间人数」时，点明「房间」就是那个直播间。

    只在最近几行里真的出现过直播/直播间/开播/主播/弹幕时才提示；没有直播
    上下文就不猜，宁可把「房间」当新话题。提示很短，不复述整段历史。
    """
    if not asks_room_viewers(text):
        return ""
    lines = [str(line or "") for line in recent][-limit:]
    if not any(any(word in line for word in _LIVE_WORDS) for line in lines):
        return ""
    return (
        "（这一句的「房间 / 观众 / 人数」说的是刚才聊的那个直播间，"
        "不是电影《房间》或豆瓣条目；问的是直播间里有多少人在看。"
        "按直播间答，没在盯直播就说看不到人数。）"
    )


def followup_kind(text: str) -> str:
    """这句是什么追问：``pronoun`` / ``ellipsis`` / 空串。纯规则、0 token。

    ``pronoun`` 是 2026-10-28 那种带他 / 这个 / 「是……」的说法；
    ``ellipsis`` 是 2026-10-29 这种连代词都省了的半句话。
    """
    body = _normalize(text)
    if not (2 <= len(body) <= _MAX_LEN):
        return ""
    if body[0] in "/#":
        return ""
    if any(word in body for word in _VERIFY):
        return ""
    if not any(word in body for word in _ASK) and not any(
        word in body for word in _WH
    ):
        return ""
    if body.startswith(_REF_START) or any(word in body for word in _REF_TOKENS):
        if len(_residual(body)) <= 1:
            return "pronoun"
    if _is_ellipsis(body):
        return "ellipsis"
    return ""


def is_followup(text: str) -> bool:
    """这句是不是在追问上一句（句子里没有新主语）。纯规则、0 token。"""
    return bool(followup_kind(text))


def _normalize(text: str) -> str:
    """去空白和句末标点。「狗在哪？」不先把问号去掉会被当成两个多余字。"""
    return "".join(str(text or "").split()).rstrip("?？!！。.,，、~～")


def _residual(body: str) -> str:
    """去掉指代词、疑问词、虚词之后剩下的「实词」。"""
    left = body
    for word in sorted(_STOP, key=len, reverse=True):
        left = left.replace(word, "")
    return re.sub(r"[^\w]", "", left)


def _is_ellipsis(body: str) -> bool:
    """没有代词，靠「疑问词 + 属性名词 / 轻动词」也能看出在接上一句。

    允许整句里有一个不认识的实词（「狗在哪」「啥鱼」），但两个以上就当成
    新话题 —— 「今天多少度」「这游戏多少钱」都有自己的主语，该搜就搜。
    """
    if not (2 <= len(body) <= _ZERO_MAX_LEN):
        return False
    if body in _STANDALONE:
        return False
    if any(word in body for word in _PERSONAL):
        return False
    tokens = _zero_tokens(body)
    if tokens is None:
        return False
    return any(token in _WH for token in tokens)


def _zero_tokens(body: str) -> list[str] | None:
    """把省略句切成允许的词；最多容忍一个不认识的实词。

    返回 None 表示「这不是省略句」（新实词太多）。切出来的词里必须有
    真正的疑问词，光有「吗 / 呢」不算 —— 「在吗」不是追问。
    """
    out: list[str] = []
    skipped = 0
    index = 0
    while index < len(body):
        for word in _ZERO_VOCAB:
            if body.startswith(word, index):
                out.append(word)
                index += len(word)
                break
        else:
            # 开头就遇到不认识的实词：只放过「狗在哪 / 鱼多少钱」这种人/物，
            # 「吃什么 / 看什么」是没主语的新问题，交给搜索闸门。
            if index == 0 and not body[1:].startswith(_FIRST_SKIP_OK):
                return None
            skipped += 1
            if skipped > 1:
                return None
            index += 1
    return out


def _strip_speaker(line: str) -> str:
    """「【群友】克龟的神头：飞八分钱」→「飞八分钱」。

    记忆里存的是带说话人标记的整行；递给模型的提示只要正文。
    只在行首很短的一段里找「：」，免得把正文里的冒号也切了。
    """
    head = line[:40]
    match = re.match(r"^(?:【[^】]*】)?\s*[^：:\n]{1,24}[：:]\s*", head)
    if not match:
        return line.strip()
    return (line[match.end():] or line).strip()


def _recent_user_texts(
    history: Iterable[dict[str, Any]], max_age: float, now: float
) -> list[str]:
    """上下文里最近几条用户消息的正文（从新到旧）；太旧的不要。"""
    out: list[str] = []
    for item in reversed(list(history or [])):
        if str(item.get("role") or "") != "user":
            continue
        ts = item.get("ts")
        if ts is not None:
            try:
                if now - float(ts) > max_age:
                    break
            except (TypeError, ValueError):
                pass
        text = _strip_speaker(str(item.get("content") or ""))
        if text:
            out.append(text)
    return out


def last_user_text(
    history: Iterable[dict[str, Any]], max_age: float = 180.0, now: float | None = None
) -> str:
    """上下文里最近一条「有主语」的用户消息；太旧（换话题了）就返回空串。

    连着追问时（「是骂谁的」→「是谁说出来的」），上一句本身也是追问；
    往回找到第一句带主语的，提示才不会变成「追问上一句的『是骂谁的』」。
    """
    now = time.time() if now is None else float(now)
    recent = _recent_user_texts(history, max_age, now)
    for text in recent:
        if not is_followup(text):
            return text
    return recent[0] if recent else ""


# 「低手是谁」→ 低手；「飞八分钱是什么意思」→ 飞八分钱。
# 省略句问「名字 / 多少钱 / 在哪」时，把这个话题说出来，比「上一句」更准。
_TOPIC_RE = re.compile(
    r"^(?P<topic>[^\s，。！？?!,、]{1,%d}?)"
    r"(?:是谁(?:做|打|发|说|写|画|唱|演|买)的?|是谁|是什么|是啥|叫啥|叫什么"
    r"|怎么样|咋样|在哪里|在哪|多少钱|是什么意思|是啥意思|什么意思|啥意思)"
    r"[\s啊呀呢吧嘛哦哈啦?？。！!，,]*$" % _TOPIC_MAX
)

# 这些话题词本身没有信息量，不能拿来当「上一轮在说谁」。
_PRONOUN_TOPICS = {
    "这个", "那个", "这些", "那些", "这", "那", "他", "她", "它",
    "他们", "她们", "它们", "我", "你", "您", "咱", "我们", "你们",
}


def topic_from(
    history: Iterable[dict[str, Any]], max_age: float = 180.0, now: float | None = None
) -> str:
    """上一轮在问的那个人 / 东西（「低手是谁」→「低手」）；找不到就空串。

    只从「自己带主语」的旧消息里取，且不取省略句 —— 否则连着追问时会
    一路退化成「名字叫什么」自己。
    """
    now = time.time() if now is None else float(now)
    for text in _recent_user_texts(history, max_age, now):
        if is_followup(text):
            continue
        match = _TOPIC_RE.match(text)
        if not match:
            continue
        topic = match.group("topic").strip()
        if len(topic) >= 2 and topic not in _PRONOUN_TOPICS:
            return topic
    return ""


def referent_hint(previous: str) -> str:
    """把上一句递给模型的一句话。上一句为空就不提示。"""
    prev = " ".join(str(previous or "").split())
    if not prev:
        return ""
    snippet = prev[:_PREVIEW] + ("…" if len(prev) > _PREVIEW else "")
    return (
        "（这一句是追问上一句，句子里没有新主语：「谁 / 什么 / 这个 / 那个」指的"
        f"就是上一句的「{snippet}」。按那件事回答，别当成新问题，也别反问"
        "「你说的是哪个」；被问「骂谁的 / 说的谁」时，答的是那个词指向谁，"
        "不是现在说话的人。别用「本鱼不乱指人 / 本鱼不说」这种官腔。）"
    )


def ellipsis_hint(text: str, topic: str = "") -> str:
    """把「省略句问的是谁」讲清楚。只一句，不复述整段上下文。

    典型：上一句「低手是谁」，这一句「名字叫什么」——问的是低手的名字，
    不是她自己的名字（2026-10-29 现场，她答成了「本鱼呀」）。
    """
    body = _normalize(text)
    if not body:
        return ""
    if any(
        word in body
        for word in ("名字", "姓名", "叫啥", "叫什么", "年龄", "岁数", "多大", "几岁", "年纪")
    ):
        target = f"「{topic}」" if topic else "刚才那一轮的人/东西"
        what, negative = f"{target}的名字", "不是你自己的名字"
    elif any(word in body for word in ("多少钱", "价格", "价钱", "多贵")):
        target = f"「{topic}」" if topic else "刚才那一轮的东西"
        what, negative = f"{target}的价格", "不是你自己的事"
    elif any(word in body for word in ("在哪", "哪里", "哪儿", "地址", "地方")):
        target = f"「{topic}」" if topic else "刚才那一轮的人/东西"
        what, negative = f"{target}在哪", "不是你自己在哪"
    elif any(word in body for word in ("为什么", "为啥", "咋了", "怎么了", "怎么回事")):
        what, negative = "刚才那件事的原因/经过", "不是你自己的事"
    else:
        return (
            "（这一句省略了主语/宾语，没有点名：多半是在接上一轮，"
            "问的是刚才那件事/那个人，不是你自己。按上一轮回答；"
            "真是在问新事情就照新事情答，但别把省掉的主语换成自己，"
            "也别反问「你说的是谁」。）"
        )
    return (
        "（这一句省略了主语/宾语，没有点名：问的是"
        f"{what}，{negative}。按上一轮回答，不知道就说不知道；"
        "别把省掉的主语换成自己，也别反问「你说的是谁」。）"
    )
