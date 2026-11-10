"""从聊天里抽"关于某人的稳定事实"。

模型每次都带着几十轮上下文，但它不会主动记"这个人喜欢咖啡、别叫他鸡哥"，
下次换个人聊又得从头猜。

这里用本地规则抓最明显的几种说法（0 token）：

- 我喜欢 / 我爱 → 喜欢
- 我讨厌 / 我受不了 / 我怕 → 雷点
- 别叫我 X / 不要叫我 X → 雷点
- 记得 / 别忘了 / 说好了 → 约好

更微妙的交给模型自己调 `save_fact` 记。

抽出来的是**短语**，不是半句话：以「你」「的」「说」开头、带引号、以「了/吗」
结尾的，一律不要。宁可漏记，也别把「喜欢『你怎么样』」写进她的长期人设。
"""

from __future__ import annotations

import re

from .forget import body_of

# 抓到的短语不超过这么多字，太长的多半是句子不是事实
MAX_PHRASE = 14
# 喜欢的东西一般是个名词短语；12 字以上基本是在说一件事
MAX_LIKE = 12
# 雷点通常更短：「开局一群人大锤杀人」这种是场景，不是"怕什么"
MAX_DISLIKE = 8

# 短语不能跨分句：逗号/顿号/冒号后面通常已经是另一句话了
# （以前允许逗号，才会抓出「的，是站在我面前动手的那位」这种东西）
_PHRASE = r"[^\s，。！？,.!?；;、：:\n]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("喜欢", re.compile(r"我(?:很|最|超|挺|特别)?(?:喜欢|爱)(?:吃|喝|玩|看|听)?(" + _PHRASE + r"{1,12})")),
    ("雷点", re.compile(r"我(?:很|最|超|特别)?(?:讨厌|不喜欢|受不了|怕)(" + _PHRASE + r"{1,12})")),
    ("雷点", re.compile(r"(?:别|不要|不许)(?:再)?叫我(" + _PHRASE + r"{1,10})")),
    # 「记不记得」「还不记得」是在问，不是在约定
    ("约好", re.compile(r"(?<![不没])(?:记得|别忘了|说好了|约好了)(" + _PHRASE + r"{2,%d})" % MAX_PHRASE)),
    # 保证/立约：没有「记得」两个字，但同样是以后要按它来的约定
    (
        "约好",
        re.compile(
            r"我(?:永远|绝对|肯定|保证|发誓|也)?(?:都)?"
            r"((?:不会|绝不|不可能)(?:抛弃|不要|丢下|离开|扔下|不管)(?:你|本鱼))"
        ),
    ),
    # 自我介绍/住处/工作/宠物/生日：这些不是"喜欢什么"，但同样是长期有用的画像。
    # 只认第一人称，且后面不能是疑问词（「我叫什么」不是自我介绍）。
    ("身份", re.compile(r"我叫(" + _PHRASE + r"{1,12})")),
    ("住处", re.compile(r"我住在(" + _PHRASE + r"{1,14})")),
    ("工作", re.compile(r"我在(" + _PHRASE + r"{1,14})(?:上班|上学|工作|读书)")),
    (
        "生日",
        re.compile(
            r"我(?:的)?生日(?:是)?"
            r"([0-9一二三四五六七八九十]{1,2}[月号][0-9一二三四五六七八九十]{1,3}[日号]?"
            r"|[0-9]{1,4}年[0-9]{1,2}月[0-9]{1,2}[日号]?)"
        ),
    ),
    ("年龄", re.compile(r"我(?:今年)?([0-9]{1,2})岁")),
]

# 这些短语太泛，记了等于没记
_JUNK = {
    "你", "他", "她", "它", "这个", "那个", "这样", "那样", "什么", "怎么",
    "哈哈", "了", "的", "我", "你啦", "笑",
}

# ---------- 宠物名：谁家的狗/猫叫什么 ----------
# 旧的「我养了 X」只抓得到「养了一只猫」，抓不到「我的狗叫丝彤姜酒」。
# 2026-10-29 群 123456789 的现场：两个人各说各的狗名，她全记混了；
# 库里只有一条「养了条狗名字叫丝彤姜酒」，另一位的狗名根本没进 facts。
# 这里把「谁家的宠物叫什么」单独抽出来，仍然按说话人的 uid 入库。
_PET_KIND = r"狗子|猫猫|狗狗|狗|猫|宠物|仓鼠|兔子|鹦鹉|乌龟"
_PET_NAME_RE = re.compile(
    r"(?:我的|我家的|我家|我)?"
    r"(?:这|那|一|两|三|几)?(?:条|只|个)?"
    r"(?P<pet>" + _PET_KIND + r")"
    r"(?:的名字|名字)?(?:叫|是|取名叫)"
    r"(?P<name>[^\s，。！？,.!?；;、\n]{1,20})"
)
# 这些不是名字，是问句/评价/叮嘱
_PET_BAD = re.compile(
    r"[吗嘛呢吧？?]|可以|行不行|好不好|怎么样|好听|难听|不要|别|记得|记住|"
    r"什么|哪个|谁|名字"
)
# 「……不要记错了」这种尾巴不是宠物名的一部分
_PET_TAIL_JUNK = re.compile(
    r"(?:不要|别|记得|记住|记好|记牢|就行|就好|吧|哦|啊|呀|嘛).*$"
)


def _pet_phrases(body: str) -> list[str]:
    """抽「我的狗叫 X / 我家的猫叫 Y」里的宠物名，返回「狗叫X」这种短语。

    只认第一人称。别人家的宠物（「他家狗叫X」）不记到当前说话人头上：
    群里聊别人的宠物很常见，记串了就会变成「你家的狗」。
    """
    if not body or not re.search(r"狗|猫|宠物|仓鼠|兔子|鹦鹉|乌龟|养|家有", body):
        return []
    out: list[str] = []
    for match in _PET_NAME_RE.finditer(body):
        before = body[max(0, match.start() - 8): match.start()]
        if re.search(r"他家|她家|它家|你家|谁家|别人|群主|朋友", before):
            continue
        # 「小明家的狗叫X」「邻居家的猫叫Y」是别人家的：前面是「某某的」，
        # 又不是「我的/我家的」，不记到当前说话人头上。
        if re.search(r"[^，。！？,.!?；;\s]{1,6}的$", before) and not before.endswith(
            ("我的", "我家的")
        ):
            continue
        pet = match.group("pet")
        raw = match.group("name").strip(" 　，。！？,.!?；;、")
        raw = _PET_TAIL_JUNK.sub("", raw)
        for piece in re.split(r"和|以及|还是|、|,|，", raw):
            name = piece.strip(" 　，。！？,.!?；;、")
            name = re.sub(r"^[叫是]", "", name).strip()
            if not name or len(name) > 14:
                continue
            if name in _JUNK or _PET_BAD.search(name):
                continue
            if not _basic_ok(name, 14):
                continue
            phrase = f"{pet}叫{name}"
            if phrase not in out:
                out.append(phrase)
    # 「我养了一只猫」这种没名字的旧说法仍然保留：只抓「养/家有」后面
    # 不带「叫/是」的短名词短语，免得和上面的「狗叫X」记成两条。
    for match in re.finditer(r"我(?:养了|家有)([^\s，。！？,.!?；;、\n]{1,14})", body):
        thing = match.group(1).strip(" 　，。！？,.!?；;、")
        if not thing or re.search(r"叫|是", thing):
            continue
        if thing in _JUNK or _PET_BAD.search(thing):
            continue
        if not _basic_ok(thing, 14):
            continue
        if thing not in out:
            out.append(thing)
    return out

# 一句话的开头是这些字，多半是半句话/指代，不是名词短语
_BAD_START = (
    "的", "了", "是", "在", "有", "我", "你", "他", "她", "它", "这", "那",
    "谁", "什么", "怎么", "哪", "还", "也", "就", "都", "又", "说", "讲",
    "告诉", "问", "觉得", "知道",
)

# 这些结尾说明话没说完 / 是反问
_BAD_TAIL = re.compile(r"[的了吗呢吧啊呀嘛]$")

# 引号多半是把原话半截吞进来了：喜欢「你"时说的」
_QUOTES = re.compile(r"[\"'“”‘’「」『』]")

# 群共识以问句/语气词结尾的，多半是在问，不是在立规矩
_GROUP_TAIL = re.compile(r"(?:[吗嘛么呢吧][？?]?|[？?])$")

# 「约好」不是抓到"记得"两个字就算：疑问、回忆、代词/虚词开头的都不是约定
_AGREEMENT_BAD = re.compile(r"[吗么呢吧啊呀嘛？?]|什么|怎么|哪|是不是|有没有")
_AGREEMENT_BAD_START = _BAD_START + (
    "想", "要", "会", "能", "可以", "记得",
    "上次", "那天", "那次", "当时", "以前", "之前",
    "去年", "上回", "刚才", "昨天", "小时候",
)

# 雷点同理：一句话里带"了/吗"多半是在讲一件事，不是在说"我讨厌什么"
_DISLIKE_BAD = re.compile(r"[了吗呢吧啊呀嘛？?]")
_DISLIKE_BAD_START = _BAD_START


def normalize(text: str) -> str:
    """「同一句话」的归一化：去标点/空白/大小写，只留实字。

    用来认「喜欢『咖啡』」和「喜欢『 咖啡 』」是同一条事实。
    """
    body = (text or "").lower()
    return re.sub(r"[\s，。！？,.!?；;、：:（）()\[\]【】「」『』\"'“”‘’]+", "", body)


def _basic_ok(phrase: str, max_len: int) -> bool:
    if not phrase or phrase in _JUNK:
        return False
    if len(phrase) > max_len:
        return False
    if re.search(r"[，、；：,;:]", phrase):  # 跨分句
        return False
    if _QUOTES.search(phrase):
        return False
    if phrase.startswith(_BAD_START):
        return False
    return not _BAD_TAIL.search(phrase)


def is_valid_like(phrase: str) -> bool:
    """这个短语像不像"喜欢的东西"，而不是半句话。"""
    return _basic_ok(phrase, MAX_LIKE)


def is_valid_agreement(phrase: str) -> bool:
    """这个短语像不像一条"约好"，而不是一句回忆/反问/半截话。"""
    if not _basic_ok(phrase, MAX_PHRASE):
        return False
    if phrase.endswith("的"):  # 「自己什么时候重启的」
        return False
    if _AGREEMENT_BAD.search(phrase):  # 「阿列夫一吗」「什么时候」
        return False
    return not phrase.startswith(_AGREEMENT_BAD_START)  # 「你这个人」「我又不做统计」


def is_valid_dislike(phrase: str) -> bool:
    """这个短语像不像"讨厌/怕的东西"，而不是一句叙事。

    「我怕厨师长自杀了」这种是在担心别人，不是雷点。
    """
    if not _basic_ok(phrase, MAX_DISLIKE):
        return False
    if _DISLIKE_BAD.search(phrase):  # 「厨师长自杀了」
        return False
    return not phrase.startswith(_DISLIKE_BAD_START)  # 「他总是迟到」


# 自我介绍里不能出现的指代/疑问词：「我叫什么」「我叫你」「我叫他」
_IDENTITY_BAD = re.compile(r"[什么啥谁你他她它这那哪]")


def is_valid_identity(phrase: str) -> bool:
    return _basic_ok(phrase, 12) and not _IDENTITY_BAD.search(phrase)


def is_valid_place(phrase: str) -> bool:
    return _basic_ok(phrase, 14)


def is_valid_birthday(phrase: str) -> bool:
    return bool(re.search(r"[月号日年]", phrase or ""))


def extract(text: str) -> list[tuple[str, str]]:
    """返回 [(kind, phrase)]，可能为空。"""
    body = (text or "").strip()
    if not body:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(body):
            phrase = match.group(1).strip(" 　，。！？,.!?；;、")
            if not phrase or phrase in _JUNK or phrase in seen:
                continue
            if len(phrase) > MAX_PHRASE:
                continue
            if kind == "喜欢" and not is_valid_like(phrase):
                continue
            if kind == "约好" and not is_valid_agreement(phrase):
                continue
            if kind == "雷点" and not is_valid_dislike(phrase):
                continue
            if kind == "身份" and not is_valid_identity(phrase):
                continue
            if kind in ("住处", "工作", "宠物") and not is_valid_place(phrase):
                continue
            if kind == "生日" and not is_valid_birthday(phrase):
                continue
            if kind == "年龄" and not (1 <= len(phrase) <= 2 and phrase.isdigit()):
                continue
            seen.add(phrase)
            out.append((kind, phrase))
    # 宠物名单独抽：正则表里那几条抓不到「我的狗叫X」，这里补上。
    # 同一个 body 里两条狗（「叫X和Y」）会拆成两条事实，都挂在说话人名下。
    for phrase in _pet_phrases(body):
        if phrase not in seen:
            seen.add(phrase)
            out.append(("宠物", phrase))
    return out


def describe(kind: str, phrase: str) -> str:
    """存进库里的那句话。"""
    if kind == "喜欢":
        return f"喜欢「{phrase}」"
    if kind == "雷点":
        return f"别拿「{phrase}」提他" if phrase else "有雷点"
    if kind == "约好":
        return f"约好「{phrase}」"
    if kind == "身份":
        return f"叫「{phrase}」"
    if kind == "住处":
        return f"住在「{phrase}」"
    if kind == "工作":
        return f"在「{phrase}」上班/上学"
    if kind == "宠物":
        return f"养了「{phrase}」"
    if kind == "生日":
        return f"生日是「{phrase}」"
    if kind == "年龄":
        return f"{phrase} 岁"
    # 群共识（scope=group）直接存原话：它本身已经是一句完整的话
    return phrase


# 群共识：不是"某个人喜欢什么"，而是"这个群里大家都这么认/这么叫/这么规定"。
# 只认有明确标记的句子（都管/默认/规矩/禁止…），"群里都在聊原神"这种当前状态不算。
_GROUP_MARK = (
    r"(?:都(?:管|叫|喊|说|认为|觉得|知道|用|认)"
    r"|一般|统一|基本|默认|规定|规矩|规则|共识|传统|禁止|不准|只(?:收|发|聊))"
)
_GROUP_PATTERNS = [
    re.compile(r"((?:我们|咱|这个|本)群(?:里|内|的)?" + _GROUP_MARK + r"[^。！？\n]{2,24})"),
    re.compile(r"(群里(?:的)?" + _GROUP_MARK + r"[^。！？\n]{2,24})"),
]


def extract_group(text: str) -> list[str]:
    """抽「群共识」：关于这个群体本身的说法，返回原话。

    只在群聊里用。抽到的是别人替**群体**立的规矩/叫法，不是某一个人的偏好，
    所以入库时走 scope=group，并且要有第二个人说同样的话才涨可信度。
    """
    body = (text or "").strip()
    if not body:
        return []
    spans: list[tuple[int, int, str]] = []
    for pattern in _GROUP_PATTERNS:
        for match in pattern.finditer(body):
            phrase = match.group(1).strip(" 　，。！？,!?；;、")
            if len(phrase) < 6 or len(phrase) > 30:
                continue
            if _QUOTES.search(phrase) or _GROUP_TAIL.search(phrase):
                continue
            if "http" in phrase:
                continue
            spans.append((match.start(1), match.end(1), phrase))
    # "我们群里…" 会同时命中「我们群里…」和「群里…」，只留最长的那个
    spans.sort(key=lambda x: (x[0], -x[1]))
    out: list[str] = []
    last_end = -1
    for start, end, phrase in spans:
        if start < last_end:
            continue
        out.append(phrase)
        last_end = end
    return out


# ---------- 群里的"说法"：第三人称的陈述句 ----------
# 不判断真假，只负责认出"这是一句可以被不同的人重复的话"。
# 真正的门槛在 Memory.observe_claim：同一个群、不同的人、同一句话。
#
# 用词性找主语，而不是拿整句去套正则：
#   「知道克里斯是区」→ 主语是「克里斯」，不是「知道克里斯」；
#   「我是真的服了」「我在想办法」→ 主语是「我」，直接不要。
_COPULA = re.compile(r"(就是|是|叫|等于|算)")
_OBJ_CUT = re.compile(
    r"[，。！？,.!?；;\s]|这是|这不是|那是|以后|下次|就|才|所以|但是|如果|的话"
    r"|你|我|他|她|它|这|那"
)
_CLAIM_BAD = re.compile(
    r"[？?]|什么|啥|干嘛|怎么|为什么|哪[个里些]?|谁|多少|是不是|有没有|能不能|要不要|会不会|对不对|好不好|行不行|还是"
)
_CLAIM_TAIL = re.compile(r"[吗嘛么呢了啦咯吧]$")
# 宾语只有一个语气词的是附和/反问，不是说法：「JoJoy是的」「符文法师是吧」
_CLAIM_OBJ_JUNK = re.compile(r"^[的了吧啊呀呢嘛哦哈嗯诶哎]+$")
_CLAIM_REQUEST = re.compile(
    r"^(?:帮我|给我|来点|来张|来个|发我|发个|写|画|生成|做一|整一|讲个|说个|唱|搜|查|找一|推荐)"
)
_CLAIM_FILLER = re.compile(r"^(?:哈哈哈|笑死|草|6|嗯|哦|啊|诶|哎)")
_CLAIM_SENSITIVE = re.compile(
    r"身份证|手机号|电话|住址|地址|密码|工资|银行卡|开盒|人肉|裸照"
)
# 有人想把一句话立成"大家都这么说"的规矩
_CONSENSUS_PUSH = re.compile(
    r"(?:下次|以后|回头).{0,10}(?:问|说|回答)"
    r"|就这么(?:说|回答)"
    r"|你(?:给我)?记(?:住|下)"
    r"|这是(?:事实|真的)"
    r"|大家都(?:这么|说)"
    r"|群里都(?:这么|说)"
)

_PSEG = None


def _pseg():
    global _PSEG
    if _PSEG is None:
        import warnings

        warnings.filterwarnings(
            "ignore", message="pkg_resources is deprecated as an API", category=UserWarning
        )
        import jieba.posseg as pseg

        _PSEG = pseg
    return _PSEG


def _subject_before(text: str) -> str:
    """取动词前面最近的名词性词（≥2 字）。代词/动词打头就返回空。

    相邻的名词会拼起来：jieba 常把「病院坂」切成「病院/坂」，
    拼回去才是一个完整的主语。

    「相邻」是**在原文里真的挨着**，不是词表里挨着：中间隔着空格/@ 的
    两个词不是一个词。以前不看位置直接拼，会把「@欧鲸鲸 小鱼是笨蛋呀」
    里的称呼和主语粘成「欧鲸鲸小鱼是笨蛋呀」（2026-10-28 实锤）。
    """
    text = text.strip()
    if not text:
        return ""
    try:
        words = list(_pseg().cut(text))
    except Exception:  # noqa: BLE001
        return ""
    # 词表不带位置，自己按出现顺序找回每个词在原文里的位置
    spans: list[tuple[int, int, str, str]] = []
    cursor = 0
    for word, flag in words:
        word = word.strip()
        if not word:
            continue
        start = text.find(word, cursor)
        if start < 0:
            continue
        spans.append((start, start + len(word), word, flag))
        cursor = start + len(word)

    parts: list[str] = []
    left = -1  # 已拼进来那段的最左端位置
    for start, stop, word, flag in reversed(spans):
        if flag.startswith(("n", "eng")):
            if parts and stop != left:  # 中间断开了（空格/@/标点）
                break
            parts.append(word)
            left = start
            if sum(len(p) for p in parts) >= 8:
                break
            continue
        if parts:
            break
        if flag.startswith(("r", "v", "p", "c", "u", "d", "m", "q", "t")):
            return ""
    if not parts:
        return ""
    # @ 后面那个名字是**称呼对象**，不是这句话的主语。
    # 「@JoJoy 是的」里的「是的」是回话，不是"JoJoy 是"（2026-10-28 实锤）。
    if text[:left].rstrip().endswith("@"):
        return ""
    subject = "".join(reversed(parts))
    if len(subject) < 2 or subject in _JUNK:
        return ""
    return subject


# 「主人」是称呼，不是名字：群里可能同时有好几条鱼，一句「你主人是克奴」
# 到底说谁家的主人，规则层分不清（2026-10-30 实锤：群友对着御墨的鱼说，
# 被记成了「主人是克奴」，算到本鱼主人头上）。这类句子不收。
_CLAIM_SUBJECT_SKIP = {"主人"}


def _copula_claims(body: str) -> list[str]:
    """从一句话里抽出「主语 + 是/叫 + 宾语」形态的说法。"""
    out: list[str] = []
    for match in _COPULA.finditer(body):
        subject = _subject_before(body[: match.start()])
        if not subject or subject in _CLAIM_SUBJECT_SKIP:
            continue
        rest = body[match.end():]
        obj = _OBJ_CUT.split(rest, maxsplit=1)[0].strip("「」\"'“”")
        if not obj or len(obj) > 16 or _CLAIM_OBJ_JUNK.match(obj):
            continue
        claim = f"{subject}{match.group(1)}{obj}"
        if 3 <= len(claim) <= 30 and claim not in out:
            out.append(claim)
    return out


def is_consensus_push(text: str) -> bool:
    """这句话是不是在把某个说法立成"大家都这么说"的规矩。"""
    return bool(_CONSENSUS_PUSH.search(text or ""))


def warm() -> None:
    """预热 jieba 词性标注，别让第一条群消息卡在词表加载上。"""
    try:
        list(_pseg().cut("预热"))
    except Exception:  # noqa: BLE001
        pass


def extract_claim(text: str) -> str:
    """认一句"可以被群友重复的第三人称说法"，返回最像的那句；不像就返回空。

    不判断真假，也不判断"他"指谁 —— 重复的人多了，Memory.observe_claim 会把它
    记成群里的「说法」，并在提示词里标明"不止一个人这么说，不一定真"。
    """
    body = body_of(text).strip()
    if len(body) < 3 or len(body) > 60:
        return ""
    if any(x in body for x in ("http", "[图片]", "[合并转发]", "（引用")):
        return ""
    if _CLAIM_BAD.search(body) or _CLAIM_REQUEST.search(body) or _CLAIM_FILLER.search(body):
        return ""
    if _CLAIM_SENSITIVE.search(body):
        return ""
    # 第一人称偏好/约定走 extract / extract_group，别在这里重复记
    if extract(body) or extract_group(body):
        return ""
    for claim in _copula_claims(body):
        if _CLAIM_BAD.search(claim) or _CLAIM_TAIL.search(claim):
            continue
        if _CLAIM_REQUEST.search(claim):
            continue
        if extract(claim) or extract_group(claim):
            continue
        return claim
    return ""


def claim_norm(text: str) -> str:
    """同一句话的归一化：去标点、去句尾语气词。"""
    return re.sub(r"(?:啊|呀|吧|啦|哦|嘛|呢|哈|捏)+$", "", normalize(text))
