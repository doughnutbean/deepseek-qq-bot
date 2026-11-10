"""接上下文搜：把「办公室呢 / 这关boss是谁」补成能搜的问题。

2026-11-07 11:01，群 123456789 的现场：

    白羽拾光：大肥鱼我要打黒流树海，推荐一下好用的干员
    她      ：银灰、重岳、凯尔希先抓这几个，稳得很
    入凡啊  ：大肥鱼我要打今天办公室，告诉我用什么干员
    她      ：办公室是哪个呀，本鱼没对上号
    入凡啊  ：昂首！足践烈阳！这个
    入凡啊  ：jt8-3
    她      ：哦——JT8-3呀，那几个本鱼直接派上去
    入凡啊  ：这关boss是谁          （跟进窗口判成「话题已转」，没答）

「办公室」不是新东西，是接着刚才的明日方舟关卡在问；「这关」指的就是 JT8-3。
旧逻辑只拿当前这一句去搜（甚至不搜），所以要么反问「办公室是什么」，要么把
上一关搜到的银灰/重岳/凯尔希直接搬到新关卡上。

这个文件只做三件本地事（0 token、不联网）：

- ``expand_query``：把当前这一句和最近几轮的话题接起来，补出
  「办公室 明日方舟 关卡 干员 攻略」/「JT8-3 boss 明日方舟 攻略」这样的搜索词。
  **上一关的专名（黒流树海）不带进来** —— 那是已经换掉的关卡，带进来只会搜串。
- ``context_hint``：告诉模型「这是接着刚才的游戏关卡说的，别反问是什么；
  上一关的答案不适用这一关」。
- ``needs_context_search``：短追问（「办公室呢」）本来过不了事实闸门，但在游戏
  语境里它问的是可查的关卡信息，值得补一次搜索。

匹配全是本地词表/正则，不判断游戏、不调模型。真正的搜索词仍然交给
``_prefetch_search`` 去查；这里只负责把「这一句」和「刚才在聊什么」接起来。
"""

from __future__ import annotations

import re

from .anaphora import _normalize, refers_to_topic
from .searchgate import query_of

# 游戏/攻略语境里反复出现的「类别词」：它们说明当前在聊一个游戏关卡，
# 而不是字面上的办公室/房间。只收足够具体的词，避免把闲聊也卷进来。
_DOMAIN_TERMS = (
    "关卡", "干员", "boss", "攻略", "阵容", "配队", "打法", "副本",
    "抽卡", "池子", "深渊", "刷图", "推图", "过图", "卡关",
    "源石", "罗德岛",
)

# 一个词基本只在一个游戏里这么叫的，补游戏名比补「游戏」准得多。
# 只收高置信的；不确定的游戏宁可不补，也别把明日方舟的词塞进别的游戏。
_TERM_GAME = (
    ("干员", "明日方舟"),
    ("源石", "明日方舟"),
    ("罗德岛", "明日方舟"),
    ("御主", "FGO"),
    ("从者", "FGO"),
)

# 句子里直接点名游戏时，以这个游戏为准，别把上一轮的游戏名带进来。
_GAME_NAMES = (
    "明日方舟", "原神", "崩坏", "星穹铁道", "FGO", "王者荣耀", "英雄联盟",
    "三角洲", "暗区突围", "和平精英", "鸣潮", "绝区零",
)

# 关卡代号：JT8-3、H4、1-7、jt8-3。不认纯数字（那是价格/年份），
# 也不认 b23/BV 这类链接片段。
_STAGE_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\s?-?\s?\d{1,3}(?:\s?-\s?\d{1,3})?)(?![A-Za-z0-9])"
)
_STAGE_SKIP = {"B23", "BV1", "BV2", "BV3", "AV1", "AV2", "AV3"}

# 句首的称呼/请求词：搜索词里带着「大肥鱼我要打今天」只会稀释关键词。
_LEAD_WORDS = (
    "大肥鱼", "小肥鱼", "肥鱼", "本鱼", "鱼鱼", "请问", "麻烦", "帮我",
    "告诉我", "我想", "我要", "今天", "现在",
)
_LEAD_VERBS = ("打", "刷", "过", "推", "玩", "抽", "练", "看")
# 搜索词里只占位置、不指向具体东西的词。去掉它们，搜索引擎才抓得住
# 「办公室」这种真正要查的名字。
_QUERY_NOISE = (
    "告诉我", "帮我", "请问", "麻烦", "推荐一下", "好用的", "好用",
    "怎么打", "怎么过", "怎么通关", "怎么配", "怎么搭", "怎么获得",
    "怎么解锁", "怎么抽", "用什么", "带什么", "上什么", "是谁", "是什么",
    "是啥", "一下",
)
_TAIL_CHARS = "呢吗啊呀吧嘛哦哈啦"
_ASK_WORDS = ("什么", "怎么", "哪", "谁", "多少", "几", "吗", "呢", "？", "?", "推荐")
# 「这关 / 这boss / 这阵容」基本只在游戏里说；「这图 / 这波 / 这角色」
# 也可能是截图、直播、二次元闲聊，只有后面跟着攻略动作时才算游戏追问。
_STRONG_TOPIC_REF = (
    "这一关", "这个关卡", "这个关", "这关", "这boss", "这个boss",
    "这阵容", "这个阵容", "这队", "这套", "这干员", "这个干员",
)
_WEAK_TOPIC_REF = ("这图", "这个图", "这波", "这角色", "这个角色")
_STRATEGY_WORDS = (
    "怎么打", "怎么过", "怎么通关", "怎么配", "怎么搭", "怎么获得",
    "怎么解锁", "怎么抽", "攻略", "阵容", "配队", "boss", "关卡", "打不过",
    "卡关", "推荐", "用什么", "带什么",
)


def _clean_one(clause: str) -> str:
    """洗一句：去称呼、去请求词、去句末语气词，句读变空格。"""
    query = (clause or "").strip()
    changed = True
    while changed and query:
        changed = False
        for word in _LEAD_WORDS:
            if query.startswith(word):
                query = query[len(word):].lstrip("，,。 .、")
                changed = True
    # 「今天办公室」「今天有什么新闻」里的时间词不参与搜索；具体日期类问题
    # 另有时效性缓存判断，不靠这两个字。
    for word in ("今天", "现在", "刚刚", "这次", "这周"):
        query = query.replace(word, " ")
    for word in _QUERY_NOISE:
        query = query.replace(word, " ")
    # 句首的「打 / 刷 / 过」只是动作，不指向关卡名；「打不过」是固定说法，保留。
    for word in _LEAD_VERBS:
        if query.startswith(word) and not query.startswith("打不过"):
            query = query[len(word):].lstrip("，,。 .、")
    query = re.sub(r"[，,。！？!?；;：:]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    query = query.rstrip("?？!！。.,，、~～")
    while query and query[-1] in _TAIL_CHARS:
        query = query[:-1].rstrip("?？!！。.,，、~～")
    return query[:100]


def _clean_query(text: str) -> str:
    """把一句话洗成搜索词。

    先按句读取第一个有内容的短句：「大肥鱼我要打今天办公室，告诉我用什么
    干员」要的是「办公室」，不是整句请求。第一句只剩称呼时看下一句。
    """
    raw = (text or "").strip()
    clauses = [c for c in re.split(r"[，,；;。!！?？]+", raw) if c.strip()] or [raw]
    for clause in clauses:
        cleaned = _clean_one(clause)
        if len(cleaned) >= 2:
            return cleaned
    return _clean_one(raw)[:100]


def _stage_code(lines) -> str:
    """最近几行里最像关卡代号的那个词（JT8-3 / H4）；找不到返回空串。"""
    for line in reversed([str(x or "") for x in lines]):
        text = line.casefold()
        if "b23.tv" in text or "http://" in text or "https://" in text:
            continue
        for match in _STAGE_RE.finditer(line):
            code = re.sub(r"\s+", "", match.group(1)).upper()
            if len(code) < 2 or not any(ch.isdigit() for ch in code):
                continue
            if code in _STAGE_SKIP:
                continue
            return code
    return ""


def _game_name(lines) -> str:
    """这几行点名的游戏；没点名就按「干员/从者」这类专属词推断。"""
    text = "\n".join(str(x or "") for x in lines).casefold()
    for name in _GAME_NAMES:
        if name.casefold() in text:
            return name
    for term, name in _TERM_GAME:
        if term.casefold() in text:
            return name
    return ""


def _profile(lines) -> tuple[list[str], str]:
    """这几行在聊哪个游戏/哪类攻略：返回（领域词, 游戏名）。"""
    text = "\n".join(str(x or "") for x in lines).casefold()
    found = [word for word in _DOMAIN_TERMS if word.casefold() in text]
    return found, _game_name(lines)


def _asks(text: str) -> bool:
    body = (text or "").casefold()
    return any(word in body for word in _ASK_WORDS)


def _topic_ask(text: str) -> bool:
    """这句是不是在问「刚才那个对象」：强指代直接算，弱指代要带攻略动作。"""
    body = _normalize(text)
    if not _asks(body):
        return False
    if any(word in body for word in _STRONG_TOPIC_REF):
        return True
    if any(word in body for word in _WEAK_TOPIC_REF):
        return any(word in body.casefold() for word in _STRATEGY_WORDS)
    return False


def needs_context_search(text: str, context_lines=()) -> bool:
    """这句短追问在游戏语境里值不值得补搜一次。纯规则、0 token。

    只认两种：
    - 明确指代刚才那一关（「这关boss是谁」「这个阵容怎么配」）；
    - 很短的追问（「办公室呢」），且上一轮确实在聊关卡/攻略。
    纯指代（「是谁说出来的」「名字叫什么」）不在这里：它们问的是上一句里的人/事，
    搜索引擎答不了。
    """
    body = _normalize(text)
    if not (2 <= len(body) <= 18):
        return False
    if any(word in body for word in ("别搜", "不要搜", "不用搜", "别查", "不要查")):
        return False
    if _topic_ask(body):
        return bool(_profile(context_lines)[0])
    # 「办公室怎么打 / 这关用什么干员」：短攻略问句，靠上一轮知道是哪个游戏。
    if _profile(context_lines)[0] and _asks(body) and any(
        word in body.casefold() for word in _STRATEGY_WORDS
    ):
        return True
    # 「办公室呢 / 这关呢 / 那个呢」：短、带语气词，靠上一轮补主语。
    if body.endswith(("呢", "吗", "啊", "呀", "？", "?")) and len(body) <= 10:
        return bool(_profile(context_lines)[0])
    return False


def expand_query(text: str, context_lines=()) -> str:
    """把当前这一句补成能搜的问题；没有上下文就原样返回。"""
    raw = query_of(text)
    if not raw:
        return ""
    lines = [str(x or "") for x in context_lines if str(x or "").strip()]
    context_found, context_game = _profile(lines)
    current_found, current_game = _profile([text])
    # 这一句自己点名了游戏/攻略词，就用自己的；没有才接上一轮。
    found = current_found or context_found
    game = current_game or context_game
    stage = _stage_code([text]) or _stage_code(lines)
    contextual = (
        needs_context_search(text, lines)
        or _topic_ask(text)
        or bool(stage)
        or (_asks(text) and bool(found))
    )
    # 没有游戏/攻略上下文时保持原来的搜索词：普通事实问题（810975是谁打出来的）
    # 不该被这里的清洗改词。
    if not contextual or (not found and not game):
        return raw[:128]
    base = _clean_query(raw)
    if refers_to_topic(text) and stage:
        # 「这关boss是谁」→「JT8-3 boss」。只换第一个指代词，
        # 后面的「这关」如果还有，留给搜索侧理解。
        base = re.sub(
            r"这(?:一)?关|这(?:个)?(?:关卡|图|boss|阵容)",
            f" {stage} ",
            base,
            count=1,
        )
        base = re.sub(r"\s+", " ", base).strip()
    if not base:
        return raw[:128]
    # 先补最能限定主题的词：「明日方舟 关卡」比「攻略」更能压住「办公室」
    # 这种有日常含义的短词。
    extras: list[str] = []
    if game:
        extras.append(game)
    extras.append("关卡")
    for word in found:
        extras.append(word)
    extras.append("攻略")
    query = base
    for word in extras:
        if word and word.casefold() not in query.casefold():
            query += " " + word
    return query[:128]


def context_hint(text: str, context_lines=()) -> str:
    """给模型的一句提示：这是接着刚才的游戏关卡，不是新话题。"""
    lines = [str(x or "") for x in context_lines if str(x or "").strip()]
    found, game = _profile(lines)
    if not found:
        return ""
    if not (
        needs_context_search(text, lines)
        or _topic_ask(text)
        or (_asks(text) and bool(_profile([text])[0]))
    ):
        return ""
    body = _clean_query(query_of(text))
    if not body:
        return ""
    stage = _stage_code([text]) or _stage_code(lines)
    if refers_to_topic(text) and stage:
        target = f"「{stage}」这一关"
        subject = f"「{stage}」"
        example = f"{stage} {game or '游戏'} 关卡 攻略"
    elif len(body) <= 14:
        target = "刚才那个游戏关卡"
        subject = f"「{body}」"
        example = f"{body} {game or '游戏'} 关卡 攻略"
    else:
        target = "刚才那个游戏关卡"
        subject = "里面的关卡简称"
        example = "里面的关卡名 + 游戏名 + 攻略"
    game_name = game or "那个游戏"
    return (
        f"（这一句是接着刚才的{game_name}话题说的：{target}，不是新东西；"
        f"{subject}按游戏里的关卡/关卡名理解，不是日常字面意思（办公室、房间、"
        "基地设施这些），别反问「这是什么」，也别答成日常含义。"
        "上一关搜到的干员/阵容/打法不能直接套到这一关；"
        f"拿不准就先搜「{example}」。）"
    )


def shift_hint(current_query: str, previous_query: str) -> str:
    """话题换了、重新搜过之后提醒一句：上一轮的答案别套过来。

    只在两边都沾游戏/攻略词时给：普通事实问题换话题不需要这一句。
    """
    current = (current_query or "").strip()
    previous = (previous_query or "").strip()
    if not current or not previous:
        return ""
    if re.sub(r"\W+", "", current) == re.sub(r"\W+", "", previous):
        return ""
    if not (_profile([current])[0] or _profile([previous])[0]):
        return ""
    return (
        f"（这一轮问的是新问题，已经重新查了「{current[:48]}」；"
        f"上一轮搜的是「{previous[:48]}」。上一轮的干员/阵容/答案不能套到这一轮，"
        "以这次查到的为准。）"
    )
