"""停用词：哪些词不该被当成"关键词"。

两条链路共用：

- **记忆检索**：这些词到处都是，拿它当搜索词只会捞出噪音
- **群聊触发**：关键词是子串匹配（`k in text`），一旦混进「那个」「就是」，
  几乎每条消息都会命中，插话判断就废了

分三层：本地手挑（语气词/连接词/功能词）+ 通用中文停用词表
（`qqbot/stopwords_zh.py`，stopwords-iso，MIT）+ 通用表的群聊例外
（`ZH_KEEP`，见 `tools/audit_stopwords.py` 的审计）。笑声叠字单独由
`is_laugh` 处理 —— 词表枚举不了「哈哈哈哈」。
"""

from __future__ import annotations

import re

from .stopwords_zh import ZH_COMMIT, ZH_COMMON, ZH_LICENSE, ZH_SOURCE

# 语气词 / 感叹
PARTICLES = {
    "啊", "吧", "呢", "吗", "哦", "嗯", "呀", "嘛", "啦", "哈", "呜", "呃", "诶",
    "哇", "唉", "哎", "嘿", "喂", "诶", "噢", "喔", "咯", "咧", "呗", "嗷", "嘤",
    "呐", "捏", "鸭", "8", "emmm", "hhh", "233",
}

# 连接词 / 语篇标记
CONNECTIVES = {
    "然后", "但是", "可是", "不过", "而且", "并且", "因为", "所以", "因此", "于是",
    "虽然", "即使", "如果", "要是", "否则", "不然", "反而", "其实", "反正", "总之",
    "另外", "此外", "同时", "接着", "之后", "之前", "后来", "最后", "首先", "其次",
    "也就是说", "换句话说", "比如说", "例如", "比如", "至于", "关于", "对于",
}

# 功能词：代词、助动词、时间词、泛化动词、程度词
FUNCTION_WORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里",
    "有", "没有", "和", "跟", "就", "都", "要", "会", "能", "可以", "不", "没",
    "也", "还", "又", "再", "很", "太", "挺", "更", "最", "非常", "特别", "有点",
    "说", "做", "去", "来", "上", "下", "给", "把", "被", "让", "对", "从", "到",
    "什么", "怎么", "为什么", "哪个", "哪些", "哪里", "多少", "一下", "一点", "一些",
    "一个", "现在", "今天", "明天", "昨天", "时候", "东西", "事情", "问题",
    "觉得", "知道", "感觉", "应该", "可能", "真的", "就是", "还是", "已经",
    "记得", "想起", "忘记", "告诉", "上次", "刚才", "刚刚", "继续", "开始",
    "结束", "需要", "希望", "打算", "准备", "帮忙", "回来", "出去", "进去",
    "自己", "别人", "大家", "有人", "没人", "怎么", "如何", "是否",
}

LOCAL_STOPWORDS = PARTICLES | CONNECTIVES | FUNCTION_WORDS

# 通用表在群聊里会误伤的实词：语法上不是纯功能词，拿来检索能指到具体东西。
# 依据 tools/audit_stopwords.py 对真实索引的抽样（结果、月、本地有实义用法）；
# 代词类（对方/人家/本人/他人）不在此列 —— 指代不等于话题。
# 改这个名单要重跑审计和 tools/test_keywords.py。
ZH_KEEP = {"区", "结果", "月", "本地"}
# 逐条理由：审计报告里会带上，别只写结论。
ZH_KEEP_WHY = {
    "区": "群核心梗（克里斯是区），单字称呼，蒸馏候选靠它",
    "结果": "名词用法真实存在（「分析结果的成因」），连接词那一半交给频次机制",
    "月": "时间实体（9月11号），单字名词原本就放行",
    "本地": "技术语境（本地跑模型）是话题，不是虚词",
}

STOPWORDS = LOCAL_STOPWORDS | (ZH_COMMON - ZH_KEEP)

# 笑声 / 重复语气词：哈哈、哈哈哈、呵呵、嘿嘿、嗯嗯、啊啊啊、呜呜……
# 单个「哈」「嗯」在 PARTICLES 里，这里管叠字和英文写法。
# 整词必须全是同一个语气字，所以「哈哈镜」这种真词不会被误伤。
_REPEAT_LAUGH = re.compile(r"^([哈呵嘿嘻呜啊哦嗯诶哎咦呀哇嗷嘤])\1+$")
_ENGLISH_LAUGH = re.compile(r"^(?:h{3,}|(?:ha){2,}|(?:he){2,}|(?:hi){2,}|lol|lmao)$")
# 纯反应、零话题：拿来检索只会把整片笑声捞回来
# （2026-10-28 实测：关键词「哈哈哈」一次命中 5 条笑）。
_LAUGH_WORDS = {"笑死", "笑死我了", "笑不活了"}

# 全角/半角标点、符号、空白
_PUNCT = set("，。！？、；：,.!?;:…—-_~～·`'\"“”‘’（）()【】[]{}<>《》 \t\n\r|/\\*#@$%^&+=")


def is_laugh(word: str) -> bool:
    """整词只是笑声/重复语气词（哈哈、呵呵、嗯嗯、hahaha、笑死）。"""
    body = (word or "").strip().lower()
    if not body:
        return False
    if body in _LAUGH_WORDS:
        return True
    if _REPEAT_LAUGH.match(body):
        return True
    return bool(_ENGLISH_LAUGH.match(body))


def is_meaningful(word: str, min_len: int = 2) -> bool:
    """这个词能不能当关键词 / 检索词。

    默认要求至少 2 个字：中文单字太容易在别的词里出现（「鱼」会命中「鲸鱼」）。
    真的想要单字，把 min_len 调成 1。
    """
    body = (word or "").strip().lower()
    if not body:
        return False
    if body in STOPWORDS or is_laugh(body):
        return False
    # 全标点 / 纯数字都不该当关键词
    if all(ch in _PUNCT for ch in body):
        return False
    if body.isdigit():
        return False
    if len(body) < max(1, min_len):
        return False
    return True


def filter_keywords(words: list[str], min_len: int = 2) -> tuple[list[str], list[str]]:
    """返回 (留下的, 被扔掉的)。扔掉的原样返回，好在日志里说明。"""
    kept: list[str] = []
    dropped: list[str] = []
    for raw in words or []:
        word = (raw or "").strip().lower()
        if not word:
            continue
        if is_meaningful(word, min_len):
            if word not in kept:
                kept.append(word)
        else:
            dropped.append(raw.strip())
    return kept, dropped
