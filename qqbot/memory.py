"""长期记忆：把对话落到 SQLite，跨重启、跨天都还记得。

三层：
1. **最近 N 轮** —— 直接拼进上下文，保证对话连贯
2. **关键词回溯** —— jieba 分词建倒排索引，按 IDF 加权重合度捞旧事
3. **语义回溯** —— 本地 embedding，关键词没命中时补一层（"我下周出门" ≈ "去杭州出差"）

为什么不用 SQLite 的 FTS5：trigram 分词器对中文两字词无效（搜「加班」搜不到），
而中文两字词恰恰最常见。所以自己做倒排表。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

# 这些词太常见，拿去检索没有任何区分度
from . import antislop
from .facts import claim_norm, normalize as fact_norm
from .forget import (
    LONG_STRENGTH,
    MIN_LONG_CHARS,
    SHORT_MAX,
    body_of,
    bump as bump_strength,
    classify,
    effective,
    worth_promoting,
)
from .stopwords import STOPWORDS as _BASE_STOPWORDS, is_laugh

log = logging.getLogger(__name__)

# 记忆检索额外要挡的词：这些在"回忆旧事"里到处都是
# 群聊标签/引用备注/图片占位，以及"怎么办/出来/名字"这类泛化词：
# 它们到处都是，却几乎不携带"这轮在聊什么"的信息，拿去检索只会占槽位。
_KEYWORD_NOISE = {
    "群友", "引用", "一条", "图片", "聊天记录", "表情", "语音", "视频", "文件",
    "转发", "合并", "怎么办", "这么", "出来", "名字", "群主", "管理", "证明",
    "好用", "几次", "说话", "厉害", "不敢", "那本",
}
_EXTRA_STOPWORDS = {
    "最近", "一直", "不太", "不是", "问题", "出去", "差不多", "主人", "本鱼",
} | _KEYWORD_NOISE
STOPWORDS = _BASE_STOPWORDS | _EXTRA_STOPWORDS

MAX_KW = 12          # 每条消息最多存几个关键词
RECALL_DAYS = 120    # 只往回看这么久

# 关键词证据弱到什么程度就允许语义插队。
# 0.45 的依据：实测弱命中集中在 0.25~0.40，0.45 以上基本都有明确主题词。
WEAK_KEYWORD_SCORE = 0.45
# 单个关键词在会话里的占比超过这个值，说明它只是群里的口头禅（"出来""怎么办"）。
WEAK_KEYWORD_RATIO = 0.02


def _short_query(query: str) -> bool:
    """短到没有上下文的陈述句，不值得再走语义召回。

    "这是对的 没有白查" 这种 8 个字、又不是问句的句子，语义向量本身就很飘；
    实测会捞出"黄黄的圆玩偶""认不出图被叫废物"之类完全无关的旧事。
    问句/带疑问词的短句保留语义召回（"猫不吃饭怎么办"确实可能想起旧事）。
    """
    body = "".join((query or "").split())
    if len(body) >= 12:
        return False
    if any(ch in body for ch in "？?"):
        return False
    if body.endswith(
        ("吗", "呢", "嘛", "怎么办", "怎么样", "如何", "什么", "哪", "谁", "为什么")
    ):
        return False
    return True


# 「你刚才说 / 你昨天讲的那个 / 本鱼说过」：优先找她自己说过的话。
# 不然问「你昨晚说的笑话」，top-3 会全是别人「来点笑话」的请求。
_SELF_QUERY = re.compile(
    r"你(?:刚才|刚刚|上次|昨晚|昨天|之前|以前|上回)?(?:说|讲|发|提|问)的"
    r"|你(?:说|讲)过|本鱼(?:说|讲)过|你(?:说|讲)的(?:那个|那句|笑话|话)"
)

# 请求回声：「来点 / 给我 / 讲个」这类句子只是在要东西，不是内容本身。
# 记忆题里同分时降一点权，别让三条请求把真正的内容挤出 top-3。
_REQUEST_ECHO = re.compile(
    r"^(?:来点|来个|来一个|给我|帮我|讲个|讲讲|说个|说说|发个|发我|"
    r"整一|整点|写个|画个|生成|推荐|查一下|搜一下)"
)

_JIEBA = None

# 事实的来源可信度：本人确认 > 模型主动记 > 本地规则 > 群共识 > 群里多人重复 > 网上查到的
FACT_SOURCE_CONFIDENCE = {
    "user": 0.95, "llm": 0.8, "local": 0.7,
    "group": 0.55, "repeat": 0.5, "web": 0.45,
    # B站评论区观众说法：比网页摘要更低，只当线索，不当事实
    "bili_comment": 0.3,
    # 人工打包过的梗合集：比单条观众说法可信，但仍不是事实
    "bili_comment_pack": 0.7,
}
# 被另一个人/另一条来源再次确认一次，可信度加这么多（有上限）
FACT_CONF_STEP = 0.1
FACT_CONF_MAX = 1.0
CONFIRM_SEP = "|"

# 搜索结果的缓存：不是事实，是"上次网上查到的东西"，会过期
KNOWLEDGE_TTL_DAYS = 7
KNOWLEDGE_MAX_ROWS = 300
KNOWLEDGE_TEXT_MAX = 400
# 同一条克里斯梗讲过后多久内不重复
LORE_USE_DAYS = 14

# 「这份缓存和当前这个问题有多像」的两个门槛。
# 只有 same 可以当"同一个问题查过"直接复用、并挡住一次搜索；
# related 只配当一句"上次查到过"的提示，绝不能拿来回答。
#   same   ：话题词重合 >=2，且占两边较长一方的 75% 以上（或问句归一化后一样）；
#   related：缓存问题的话题词至少一半出现在新问题里（专名/短话题才够得着）。
# 2026-11-07 实锤：用「较短一方」算，会把「JT8-3 攻略 干员推荐」和
# 「JT8-3 boss」判成同一个问题，boss 这种新问法就被旧缓存挡住、不去搜。
KNOWLEDGE_SAME_COVER = 0.75
KNOWLEDGE_RELATED_COVER = 0.5

# 消息占位标签带出来的词（「[图片] 这是谁」「[B站视频] 标题」）。它们不是话题：
# 任何一句带「视频」的话都会撞上，所以不参与缓存匹配。
_NOT_TOPIC_WORDS = {
    "图片", "表情", "表情包", "视频", "语音", "文件", "卡片", "链接", "B站", "合并转发",
}

# 只用来限定「在聊哪个游戏/哪类问题」的通用词：它们重合不能证明是同一个问题。
# 2026-11-07 实锤：两轮搜索都补了「明日方舟 关卡 干员 攻略」，旧缓存把
# 「黒流树海」和「办公室」当成同一个问题，新关卡直接没搜。
_GENERIC_TOPIC_WORDS = {
    "明日", "方舟", "明日方舟", "原神", "崩坏", "星穹", "铁道", "fgo", "游戏",
    "关卡", "干员", "攻略", "推荐", "阵容", "配队", "打法", "副本", "深渊",
    "怎么", "什么", "多少", "哪个", "哪里", "为什么", "如何", "是谁", "是什么",
    "一个", "这个", "那个", "一下",
}


def _distinctive(words: set[str]) -> set[str]:
    """去掉通用词之后，真正能把两个问题区分开的词。"""
    return {w for w in words if w and w not in _GENERIC_TOPIC_WORDS}


def _claim_score(overlap: int, confidence: float, hits: int) -> float:
    """群共识和网上缓存共用这一个打分。

    关键词重合为主；可信度不分来源（网上 0.45 < 群共识 0.55）；被不同来源
    确认过的再加一点。两边用同一个公式，阈值才有可比性。
    """
    return overlap * 2.0 + float(confidence or 0.0) + min(int(hits or 1), 3) * 0.1


def _claims_match(text_a: str, norm_a: str | None, text_b: str, norm_b: str | None) -> bool:
    """两条说法是不是同一件事。

    优先看归一化后是否一模一样（群共识里「同一句话」就是按这个认的）；
    否则看关键词被包含的程度 —— 「克里斯是B站主播：玩暗区突围」和
    「克里斯是B站主播」算同一条，光共用一个「克里斯」不算。
    """
    if norm_a and norm_b and norm_a == norm_b:
        return True
    a, b = set(keywords(text_a)), set(keywords(text_b))
    if not a or not b:
        return False
    shared = a & b
    if len(shared) < 2:
        return False
    return len(shared) / min(len(a), len(b)) >= 0.75


def _weak_keyword_evidence(
    items: list[dict[str, Any]],
    usable: list[str],
    df: dict[str, int],
    n_turns: int,
) -> bool:
    """这次关键词召回的证据够不够硬。

    关键词优先的前提是关键词真的指向某个主题；如果最高分只有 0.3，
    或者整句只剩一个「出来/怎么办」这种到处都有的词，就该让语义补一条。
    不用无条件"语义保底"：关键词证据强的时候，语义插队只是加噪音。
    """
    if not items:
        return True
    best = max(float(item.get("score") or 0.0) for item in items)
    if best < WEAK_KEYWORD_SCORE:
        return True
    if len(usable) == 1:
        # 小会话里 df=1 也可能占比很高；先要求绝对次数超过 2，再看比例。
        common = max(2, int(n_turns * WEAK_KEYWORD_RATIO))
        if int(df.get(usable[0]) or 0) > common:
            return True
    return False


def _jieba():
    global _JIEBA
    if _JIEBA is None:
        # jieba 0.42 还在 import pkg_resources，setuptools 81 起会告警。
        # 这不是我们能用代码修好的，压掉这一条就行 —— 别让用户每次启动看一段红字。
        import warnings

        warnings.filterwarnings(
            "ignore", message="pkg_resources is deprecated as an API", category=UserWarning
        )
        import jieba

        jieba.setLogLevel(logging.WARNING)
        _JIEBA = jieba
    return _JIEBA


_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)

# 说话人标签 / 昵称：索引和查询前先剥掉。
# 不剥的话「群友」会出现在 40%+ 的消息关键词里，昵称还会被 jieba 切碎
# （MYGO型帕鲁 -> MYG / O型 / 帕鲁），搜谁的名字都变成搜标签。
_SPEAKER_TAG = re.compile(r"^(?:【[^】]{1,16}】\s*)+")
_SPEAKER_NAME = re.compile(r"^[^：:\n]{1,24}[：:]\s*")
# 引用/转发备注、图片占位：这是消息的壳，不是这个人说的话。
_META_NOTE = re.compile(
    r"（(?:引用了前一条|引用了合并转发|被引用的那条是合并转发|转发)[^\n）]*）"
)
_MERGE_NOTE = re.compile(r"（这是连着发的几条，当成一段话一起看）")
_PLACEHOLDER = re.compile(
    r"\[(?:图片|表情|语音|视频|文件|合并转发|聊天记录|卡片消息|动画表情|回复|"
    r"戳一戳|链接|位置|音乐|红包|转账)\]"
)

# 群里真正会搜的单字实体。白名单而不是"所有名词"：
# jieba 会把「人/区/话/事/问/群」也标成 n，放进来会多出几万条泛词索引。
_SINGLE_NOUN = {
    "梗", "号", "图", "片", "剧", "歌", "书", "车", "房", "钱", "球",
    "枪", "猫", "狗", "鱼", "鸟", "鼠", "键", "刀", "剑", "牌", "币",
    "鲸", "兔", "马", "牛", "羊", "鸡", "鸭", "鹅", "猪", "蛇", "龙",
    "虎", "熊", "狼", "鹿", "龟", "蛙", "虫", "饭", "水", "茶", "酒",
    "雨", "雪", "风", "云", "花", "草", "树", "海", "山", "河", "星",
    "月",
}
_POSSEG = None


def _posseg():
    global _POSSEG
    if _POSSEG is None:
        _jieba()
        import jieba.posseg as pseg

        _POSSEG = pseg
    return _POSSEG


def clean_for_keywords(text: str) -> str:
    """去掉消息的壳，只留这个人真正说的话。

    剥：开头的【群友】/【主人】标签、昵称、引用/转发备注、[图片] 之类的占位。
    不动：正文、换行、引用备注之外的引用内容（那部分由调用方决定留不留）。
    """
    body = (text or "").strip()
    had_tag = bool(_SPEAKER_TAG.match(body))
    body = _SPEAKER_TAG.sub("", body)
    if had_tag:
        body = _SPEAKER_NAME.sub("", body, count=1)
    body = _META_NOTE.sub(" ", body)
    body = _MERGE_NOTE.sub(" ", body)
    body = _PLACEHOLDER.sub(" ", body)
    return body.strip()


def keywords(text: str, *, single_nouns: bool = False) -> list[str]:
    """抽关键词：jieba 分词后去掉停用词、标点、太短太长的。

    single_nouns=True 时放行单字实体白名单（猫/鱼/枪/梗/号）。中文里最关键的
    实体常常就是一个字，按 2 字起收会把「猫不吃饭怎么办」变成「吃饭/怎么办」。
    只放行白名单里的具体名词，避免「人/区/话/事」这类单字泛词淹没索引。
    """
    body = clean_for_keywords(text)
    if not body:
        return []
    out: list[str] = []
    # 多字词仍用 cut：posseg 会把「这枪真难用」切成「枪/真难/用」，
    # 反而把「这枪/难用」这种原有词组拆坏。词性只用来补单字名词。
    tokens: list[tuple[str, str]] = [(w, "") for w in _jieba().cut(body)]
    if single_nouns:
        for w in _posseg().cut(body):
            word = w.word.strip()
            if len(word) == 1 and word in _SINGLE_NOUN:
                tokens.append((word, w.flag))
    for tok, flag in tokens:
        tok = tok.strip()
        if not tok or len(tok) > 12:
            continue
        if len(tok) < 2:
            if not single_nouns or tok not in _SINGLE_NOUN:
                continue
        if tok in STOPWORDS or tok.isdigit() or is_laugh(tok):
            continue
        if _PUNCT.search(tok):
            continue
        if tok not in out:
            out.append(tok)
        if len(out) >= MAX_KW:
            break
    return out


def _knowledge_kind(qnorm: str, qk: set[str], tnorm: str, tk: set[str]) -> str:
    """问句和一条缓存是什么关系：""（无关）/ "related"（同一话题）/ "same"（同一个问题）。

    只看**问题本身的关键词**（``topic_kw``），不看网页摘要。摘要里全是
    「视频」「认证」「粉丝」这类页面自带的词，拿它们匹配等于把不相干的问题
    串成一条：2026-10-26 实测「突然的陀螺 B站 视频」命中了 B 站 UP 主主页
    （共用「视频」），「一个身份证可以实名认证几个QQ号」也命中同一条
    （共用「认证」）—— 回复直接答成了另一个人。

    same 才可以复用、挡住一次搜索；related 只配当"上次查到过"的提示。
    """
    if not qnorm or not tnorm:
        return ""
    if qnorm == tnorm:
        return "same"
    if not qk or not tk:
        return ""
    shared = qk & tk
    if not shared:
        return ""
    # 两边各有自己的新实词（「黒流树海」vs「办公室」、「boss」vs「推荐」）：
    # 这是在问不同的东西，不能算同一个问题。通用词（关卡/干员/攻略/游戏名）
    # 重合再多也不算。
    q_only = _distinctive(qk - tk)
    t_only = _distinctive(tk - qk)
    changed_question = bool(q_only or t_only)
    # 同一个问题：两边都有足够的词重合（对称），且没有各自的新实词。
    if (
        not changed_question
        and len(shared) >= 2
        and len(shared) / max(len(qk), len(tk)) >= KNOWLEDGE_SAME_COVER
    ):
        return "same"
    # 只是同一话题：看**缓存问题本身**被覆盖了多少。新问题只有两个词、碰巧共用
    # 一个泛词（「便宜」「哪些」）时不能算 —— 那些正是串味的来源。
    return "related" if len(shared) / len(tk) >= KNOWLEDGE_RELATED_COVER else ""


def knowledge_match(query: str, topic: str, topic_kw: str | None = None) -> str:
    """这份缓存和当前问题什么关系（"" / "related" / "same"）。纯本地、0 token。"""
    query, topic = (query or "").strip(), (topic or "").strip()
    if not query or not topic:
        return ""
    tk = (
        set(keywords(topic)) if topic_kw is None else set((topic_kw or "").split())
    ) - _NOT_TOPIC_WORDS
    return _knowledge_kind(fact_norm(query), set(keywords(query)), fact_norm(topic), tk)


class Memory:
    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        embedder: Any = None,
        semantic_z: float = 1.4,
        semantic_cos: float = 0.35,
        semantic_recall_bump: float = 0.05,
        semantic_candidates: int = 3000,
        semantic_floor: int = 1,
        forget_rate: float = 0.08,
        forget_threshold: float = 0.15,
        promote_hits: int = 3,
        promote_min_gap_hours: float = 12.0,
        promote_min_score: float = 0.5,
        promote_min_chars: int = MIN_LONG_CHARS,
    ) -> None:
        self.enabled = enabled
        self.path = path
        self.embedder = embedder
        # 遗忘曲线：短期按指数衰减，反复想起会转长期
        self.forget_rate = forget_rate
        self.forget_threshold = forget_threshold
        self.promote_hits = promote_hits
        # 转长期的门槛：隔多久、多像、多长
        self.promote_min_gap_hours = max(0.0, float(promote_min_gap_hours))
        self.promote_min_score = max(0.0, min(1.0, float(promote_min_score)))
        self.promote_min_chars = max(1, int(promote_min_chars))
        self.semantic_z = semantic_z
        self.semantic_cos = semantic_cos
        # 语义命中也给一点"最近访问"：不然天天被想起的事 24 天后照样淡掉。
        self.semantic_recall_bump = max(0.0, float(semantic_recall_bump))
        self.semantic_candidates = max(100, int(semantic_candidates))
        # 普通聊天里语义至少占几条槽位（0 = 老行为：关键词满了就不跑语义）
        self.semantic_floor = max(0, int(semantic_floor))
        self._db: sqlite3.Connection | None = None
        if enabled:
            self._connect()

    # ---------- 连接与建表 ----------

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS turns (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT    NOT NULL,
                role    TEXT    NOT NULL,
                content TEXT    NOT NULL,
                ts      INTEGER NOT NULL,
                msg_id  TEXT,
                embedding BLOB,
                owner_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session, id);
            CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

            CREATE TABLE IF NOT EXISTS facts (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                uid  TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                ts   INTEGER NOT NULL,
                scope TEXT DEFAULT 'personal',
                group_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_facts_uid ON facts(uid, id);

            -- 搜索结果的缓存：按 topic 存一句归一化后的话 + 出处 + 过期时间。
            -- 它不进 facts，也不参与"关于某人"的画像；只是省一次重复搜索。
            CREATE TABLE IF NOT EXISTS knowledge (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                topic   TEXT    NOT NULL,
                text    TEXT    NOT NULL,
                url     TEXT,
                source  TEXT    DEFAULT 'web',
                norm    TEXT,
                kw      TEXT,
                topic_kw TEXT,
                ts      INTEGER NOT NULL,
                expires_at INTEGER,
                hits    INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_exp ON knowledge(expires_at);
            CREATE INDEX IF NOT EXISTS idx_knowledge_norm ON knowledge(norm);

            -- 群里的"说法"候选：同一个群、不同的人、同一句话。
            -- 第一个人说只留在这里；第二个人说同样的话，才升级成 scope=group 的事实。
            CREATE TABLE IF NOT EXISTS claims (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT    NOT NULL,
                norm     TEXT    NOT NULL,
                text     TEXT    NOT NULL,
                speakers TEXT,
                hits     INTEGER DEFAULT 1,
                first_ts INTEGER,
                last_ts  INTEGER,
                promoted INTEGER DEFAULT 0,
                UNIQUE(group_id, norm)
            );
            CREATE INDEX IF NOT EXISTS idx_claims_last ON claims(last_ts);

            CREATE TABLE IF NOT EXISTS keywords (
                kw      TEXT    NOT NULL,
                turn_id INTEGER NOT NULL,
                PRIMARY KEY (kw, turn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kw_turn ON keywords(turn_id);

            -- 克里斯语料工具讲过的条目：防止翻来覆去只讲同几个梗
            CREATE TABLE IF NOT EXISTS lore_use (
                candidate_id INTEGER PRIMARY KEY,
                ts           INTEGER NOT NULL,
                hits         INTEGER DEFAULT 1
            );
            """
        )
        db.commit()
        # 老库升级：补上 embedding / owner_id 列
        cols = {r["name"] for r in db.execute("PRAGMA table_info(turns)").fetchall()}
        if "embedding" not in cols:
            db.execute("ALTER TABLE turns ADD COLUMN embedding BLOB")
            db.commit()
            log.info("已为旧记忆库补上 embedding 列")
        if "owner_id" not in cols:
            db.execute("ALTER TABLE turns ADD COLUMN owner_id TEXT")
            db.commit()
            # 私聊的历史可以确定归属，顺手回填；群聊分不清是谁说的，留空
            db.execute(
                "UPDATE turns SET owner_id = substr(session, 9) "
                "WHERE owner_id IS NULL AND session LIKE 'private:%'"
            )
            db.commit()
            log.info("已为旧记忆库补上 owner_id 列（私聊历史已回填）")
        if "kind" not in cols:
            # 遗忘：short 会随时间衰减，long 永久保留
            for ddl in (
                "ALTER TABLE turns ADD COLUMN kind TEXT DEFAULT 'short'",
                "ALTER TABLE turns ADD COLUMN strength REAL DEFAULT 1.0",
                "ALTER TABLE turns ADD COLUMN last_access INTEGER",
                "ALTER TABLE turns ADD COLUMN hits INTEGER DEFAULT 0",
            ):
                db.execute(ddl)
            db.execute("UPDATE turns SET last_access = ts WHERE last_access IS NULL")
            db.commit()
            log.info("已为旧记忆库补上遗忘相关列")
        if "hit_ts" not in cols:
            # 上一次"算数的想起"发生在什么时候。转长期要求两次隔着 promote_min_gap_hours，
            # 所以老数据的 last_access 先当 hit_ts 用，免得升级那一刻立刻攒够次数。
            db.execute("ALTER TABLE turns ADD COLUMN hit_ts INTEGER")
            db.execute("UPDATE turns SET hit_ts = last_access WHERE hit_ts IS NULL AND hits > 0")
            db.commit()
            log.info("已为旧记忆库补上 hit_ts 列")
        db.execute("CREATE INDEX IF NOT EXISTS idx_turns_owner ON turns(owner_id, id)")
        # 事实升级：补上「谁说的 / 从哪来 / 多可信 / 被确认过几次」
        # uid 一直是"关于谁"，这次把"谁说的"单独拆出来（speaker_uid），
        # 顺便加上归一化键 norm，用来认「同一句话」被不同人/不同来源确认。
        fcols = {r["name"] for r in db.execute("PRAGMA table_info(facts)").fetchall()}
        added: list[str] = []
        for col, ddl in (
            ("speaker_uid", "ALTER TABLE facts ADD COLUMN speaker_uid TEXT"),
            ("source", "ALTER TABLE facts ADD COLUMN source TEXT"),
            ("confidence", "ALTER TABLE facts ADD COLUMN confidence REAL"),
            ("hits", "ALTER TABLE facts ADD COLUMN hits INTEGER"),
            ("last_ts", "ALTER TABLE facts ADD COLUMN last_ts INTEGER"),
            ("confirmers", "ALTER TABLE facts ADD COLUMN confirmers TEXT"),
            ("norm", "ALTER TABLE facts ADD COLUMN norm TEXT"),
            ("scope", "ALTER TABLE facts ADD COLUMN scope TEXT"),
            ("group_id", "ALTER TABLE facts ADD COLUMN group_id TEXT"),
        ):
            if col not in fcols:
                db.execute(ddl)
                added.append(col)
        if added:
            db.execute("UPDATE facts SET speaker_uid=uid WHERE speaker_uid IS NULL")
            db.execute("UPDATE facts SET source='local' WHERE source IS NULL")
            db.execute("UPDATE facts SET confidence=0.7 WHERE confidence IS NULL")
            db.execute("UPDATE facts SET hits=1 WHERE hits IS NULL")
            db.execute("UPDATE facts SET last_ts=ts WHERE last_ts IS NULL")
            db.execute("UPDATE facts SET scope='personal' WHERE scope IS NULL")
            # norm 没法用 SQL 算，逐条回填
            for r in db.execute(
                "SELECT id, uid, text, confirmers FROM facts "
                "WHERE norm IS NULL OR confirmers IS NULL"
            ).fetchall():
                db.execute(
                    "UPDATE facts SET norm=?, confirmers=? WHERE id=?",
                    (
                        fact_norm(r["text"]),
                        r["confirmers"] or f"u:{r['uid']}:local",
                        r["id"],
                    ),
                )
            log.info("已为旧记忆库补上事实来源列（%s）", "、".join(added))
        db.execute("CREATE INDEX IF NOT EXISTS idx_facts_norm ON facts(uid, norm)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_group ON facts(scope, group_id, norm)"
        )
        # 搜索缓存也补一个关键词列：每次消息都现切 200 条 jieba 太慢，
        # 写入时算好，读取时做集合求交就行。
        kcols = {r["name"] for r in db.execute("PRAGMA table_info(knowledge)").fetchall()}
        if "kw" not in kcols:
            db.execute("ALTER TABLE knowledge ADD COLUMN kw TEXT")
            for r in db.execute("SELECT id, topic, text FROM knowledge").fetchall():
                db.execute(
                    "UPDATE knowledge SET kw=? WHERE id=?",
                    (" ".join(keywords(r["topic"] + " " + r["text"])), r["id"]),
                )
            log.info("已为旧记忆库补上搜索缓存关键词列")
        # 搜索缓存升级：和群共识共用一套「可信度 + 被谁确认过」的逻辑。
        # confidence 按来源算（web 0.45，比群共识还低）；claim_norm 用来认
        # 「同一句话」；corroborated=1 表示群里也有人这么说 —— 互相印证过。
        kcols = {r["name"] for r in db.execute("PRAGMA table_info(knowledge)").fetchall()}
        added_k: list[str] = []
        for col, ddl in (
            ("confidence", "ALTER TABLE knowledge ADD COLUMN confidence REAL"),
            ("claim_norm", "ALTER TABLE knowledge ADD COLUMN claim_norm TEXT"),
            ("last_ts", "ALTER TABLE knowledge ADD COLUMN last_ts INTEGER"),
            ("confirmers", "ALTER TABLE knowledge ADD COLUMN confirmers TEXT"),
            ("corroborated", "ALTER TABLE knowledge ADD COLUMN corroborated INTEGER"),
        ):
            if col not in kcols:
                db.execute(ddl)
                added_k.append(col)
        if added_k:
            for r in db.execute("SELECT id, text, ts FROM knowledge").fetchall():
                db.execute(
                    "UPDATE knowledge SET confidence=COALESCE(confidence, ?), "
                    "claim_norm=COALESCE(claim_norm, ?), last_ts=COALESCE(last_ts, ts), "
                    "confirmers=COALESCE(confirmers, 'w:web'), "
                    "corroborated=COALESCE(corroborated, 0) WHERE id=?",
                    (FACT_SOURCE_CONFIDENCE["web"], fact_norm(r["text"]), r["id"]),
                )
            log.info("已为旧记忆库补上搜索缓存的可信度列（%s）", "、".join(added_k))
        # 话题词：只从**问题**里抽，不从网页摘要里抽。复用/挡搜索只看这一列 ——
        # 摘要里的「视频」「认证」会把不相干的问句串到同一条缓存上（见
        # _knowledge_kind）。老库按 topic 回填一次。
        kcols = {r["name"] for r in db.execute("PRAGMA table_info(knowledge)").fetchall()}
        if "topic_kw" not in kcols:
            db.execute("ALTER TABLE knowledge ADD COLUMN topic_kw TEXT")
            for r in db.execute("SELECT id, topic FROM knowledge").fetchall():
                db.execute(
                    "UPDATE knowledge SET topic_kw=? WHERE id=?",
                    (
                        " ".join(w for w in keywords(r["topic"]) if w not in _NOT_TOPIC_WORDS),
                        r["id"],
                    ),
                )
            log.info("已为旧记忆库补上搜索缓存的话题词列")
        db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_claim ON knowledge(claim_norm)")
        db.commit()
        self._db = db
        log.info("记忆库已打开: %s", self.path)

    @property
    def db(self) -> sqlite3.Connection | None:
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ---------- 写入 ----------

    def add_turn(
        self,
        session: str,
        role: str,
        content: str,
        msg_id: str | None = None,
        ts: int | None = None,
        embedding: bytes | None = None,
        owner_id: str | None = None,
    ) -> None:
        if not self.enabled or self._db is None or not content.strip():
            return
        db = self._db
        if msg_id is not None:
            dup = db.execute(
                "SELECT 1 FROM turns WHERE session=? AND msg_id=? LIMIT 1", (session, msg_id)
            ).fetchone()
            if dup:
                return
        # 该记多久：事实类永久，闲聊类会淡忘；她自己说的话一律只算短期
        memo_kind, memo_strength = classify(content, role)
        ts_value = int(ts if ts is not None else time.time())
        cur = db.execute(
            "INSERT INTO turns(session, role, content, ts, msg_id, embedding, owner_id, "
            "kind, strength, last_access, hits) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (
                session,
                role,
                content,
                ts_value,
                msg_id,
                embedding,
                owner_id,
                memo_kind,
                memo_strength,
                ts_value,
            ),
        )
        turn_id = cur.lastrowid
        # 两边都建索引：用户说的和本鱼回答里的事实都值得被想起来。
        # 单字名词也收（猫/鱼/枪/梗），否则这类查询只能靠语义兜底。
        for kw in keywords(content, single_nouns=True):
            db.execute("INSERT OR IGNORE INTO keywords(kw, turn_id) VALUES (?,?)", (kw, turn_id))
        db.commit()

    def reindex(self) -> int:
        """重建关键词索引（改了分词规则、或者旧数据没索引时用）。"""
        if not self.enabled or self._db is None:
            return 0
        db = self._db
        db.execute("DELETE FROM keywords")
        db.commit()
        rows = db.execute("SELECT id, content FROM turns").fetchall()
        for i, r in enumerate(rows, 1):
            for kw in keywords(r["content"], single_nouns=True):
                db.execute("INSERT OR IGNORE INTO keywords(kw, turn_id) VALUES (?,?)", (kw, r["id"]))
            # 分批提交：机器人还在跑，不能把写锁攥十几秒。
            if i % 200 == 0:
                db.commit()
        db.commit()
        log.info("重建索引完成：%d 条对话", len(rows))
        return len(rows)

    def backfill_embeddings(self, batch: int = 32) -> int:
        """给还没有向量的旧对话补算 embedding。"""
        if self._db is None or self.embedder is None:
            return 0
        if not getattr(self.embedder, "available", False):
            return 0
        db = self._db
        rows = db.execute("SELECT id, content FROM turns WHERE embedding IS NULL").fetchall()
        if not rows:
            return 0
        done = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            blobs = self.embedder.encode([r["content"] for r in chunk])
            if not blobs:
                break
            for r, blob in zip(chunk, blobs):
                db.execute("UPDATE turns SET embedding=? WHERE id=?", (blob, r["id"]))
            db.commit()
            done += len(chunk)
            log.info("补算向量 %d/%d", done, len(rows))
        return done

    # ---------- 关于某人的稳定事实（语义卡片） ----------
    #
    # uid 是"关于谁"（subject）；speaker_uid 是"谁说的"（来源）。
    # 同一条事实被另一个人、或另一条路径（本地规则 / 她自己记）再次确认时，
    # 不新增一行，而是把可信度抬一点（有上限）——人多不等于真，但确实更值得信。

    def add_fact(
        self,
        uid: str,
        kind: str,
        text: str,
        speaker_uid: str | None = None,
        source: str = "local",
        limit_per_user: int = 20,
        ts: int | None = None,
        scope: str = "personal",
        group_id: str | None = None,
    ) -> bool:
        """记一条事实。

        scope=personal：关于某个人（uid），跨群/私聊共享；
        scope=group：关于这个群体本身（外号、规矩、共识），只在这个群里用。
        """
        if scope == "group" and group_id:
            uid = ""
        else:
            scope = "personal"
        if not self.enabled or self._db is None or not text.strip():
            return False
        if scope == "personal" and not uid:
            return False
        text = text.strip()[:80]
        norm = fact_norm(text)
        if not norm:
            return False
        now = int(time.time() if ts is None else ts)
        uid = str(uid)
        owner = str(group_id) if scope == "group" else uid
        if scope == "group":
            confirmer = f"g:{speaker_uid}:{source}" if speaker_uid else f"gs:{source}"
        else:
            confirmer = f"u:{speaker_uid}:{source}" if speaker_uid else f"s:{source}"

        if scope == "group":
            row = self._db.execute(
                "SELECT id, confirmers, confidence, hits FROM facts "
                "WHERE COALESCE(scope, 'personal')='group' AND group_id=? AND norm=? LIMIT 1",
                (owner, norm),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT id, confirmers, confidence, hits FROM facts "
                "WHERE COALESCE(scope, 'personal')='personal' AND uid=? AND norm=? LIMIT 1",
                (owner, norm),
            ).fetchone()
        if row is None and scope == "personal":
            # 老代码可能在我们迁移之后又插了没有 norm 的行：按原文再认一次，顺手补列
            legacy = self._db.execute(
                "SELECT id FROM facts WHERE uid=? AND text=? AND norm IS NULL LIMIT 1",
                (owner, text),
            ).fetchone()
            if legacy is not None:
                self._db.execute(
                    "UPDATE facts SET norm=?, speaker_uid=COALESCE(speaker_uid, ?), "
                    "source=COALESCE(source, 'local'), confidence=COALESCE(confidence, 0.7), "
                    "hits=COALESCE(hits, 1), last_ts=COALESCE(last_ts, ts), "
                    "confirmers=COALESCE(confirmers, ?), "
                    "scope=COALESCE(scope, 'personal') WHERE id=?",
                    (norm, owner, f"u:{owner}:local", legacy["id"]),
                )
                self._db.commit()
                row = self._db.execute(
                    "SELECT id, confirmers, confidence, hits FROM facts WHERE id=?",
                    (legacy["id"],),
                ).fetchone()
        if row:
            keys = [k for k in str(row["confirmers"] or "").split(CONFIRM_SEP) if k]
            if confirmer not in keys:
                keys.append(confirmer)
                conf = min(
                    FACT_CONF_MAX,
                    float(row["confidence"] or FACT_SOURCE_CONFIDENCE["local"]) + FACT_CONF_STEP,
                )
                self._db.execute(
                    "UPDATE facts SET confirmers=?, confidence=?, hits=?, last_ts=? WHERE id=?",
                    (CONFIRM_SEP.join(keys), conf, int(row["hits"] or 1) + 1, now, row["id"]),
                )
                self._db.commit()
                log.info(
                    "事实又被确认了一次（%d 个来源，可信度 %.2f）：%s",
                    len(keys), conf, text,
                )
                return True
            # 同一个人再说一遍：只更新"最近提起"，不涨可信度
            self._db.execute("UPDATE facts SET last_ts=? WHERE id=?", (now, row["id"]))
            self._db.commit()
            return False

        conf = FACT_SOURCE_CONFIDENCE.get(source, FACT_SOURCE_CONFIDENCE["local"])
        # 网上已经查到过同一条说法：等于多了一个来源，可信度往上走一格。
        # 这和「群里第二个人说同样的话」是同一套确认逻辑，只是来源不同。
        confirmers = [confirmer]
        if self._knowledge_confirms(text, norm):
            confirmers.append("w:web")
            conf = min(FACT_CONF_MAX, conf + FACT_CONF_STEP)
            log.info("这条说法网上已经查到过（可信度 %.2f）：%s", conf, text[:40])
        self._db.execute(
            "INSERT INTO facts(uid, kind, text, ts, speaker_uid, source, confidence, "
            "hits, last_ts, confirmers, norm, scope, group_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, kind or "其他", text, now, str(speaker_uid) if speaker_uid else None,
                source, conf, 1, now, CONFIRM_SEP.join(confirmers), norm, scope,
                str(group_id) if scope == "group" else None,
            ),
        )
        self._db.commit()
        # 每个归属最多留这么多条；超了先淘汰最不可信的，再淘汰最老的
        if scope == "group":
            rows = self._db.execute(
                "SELECT id FROM facts WHERE COALESCE(scope, 'personal')='group' "
                "AND group_id=? ORDER BY confidence ASC, id ASC LIMIT -1 OFFSET ?",
                (owner, limit_per_user),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id FROM facts WHERE COALESCE(scope, 'personal')='personal' "
                "AND uid=? ORDER BY confidence ASC, id ASC LIMIT -1 OFFSET ?",
                (owner, limit_per_user),
            ).fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            ph = ",".join("?" * len(ids))
            self._db.execute(f"DELETE FROM facts WHERE id IN ({ph})", ids)
            self._db.commit()
        log.info(
            "记住了一条事实（%s，%s，%s，可信度 %.2f）：%s",
            "群共识" if scope == "group" else "关于某人", kind, source, conf, text,
        )
        return True

    def add_group_fact(
        self,
        group_id: str,
        text: str,
        speaker_uid: str | None = None,
        source: str = "group",
        kind: str = "共识",
        ts: int | None = None,
    ) -> bool:
        """记一条群共识（外号、群规、大家都这么叫）。"""
        if not group_id:
            return False
        return self.add_fact(
            "",
            kind,
            text,
            speaker_uid=speaker_uid,
            source=source,
            ts=ts,
            scope="group",
            group_id=str(group_id),
        )

    def observe_claim(
        self,
        group_id: str,
        text: str,
        speaker_uid: str | None,
        ts: int | None = None,
    ) -> dict[str, Any] | None:
        """看见一句群里的第三人称说法。

        第一个人说，只记成候选；**第二个人**说同样的话，才升级成群共识
        （kind=说法，source=repeat）。升级后每多一个人说，可信度 +0.1。
        返回升级信息（给日志用），没升级返回 None。
        """
        if not self.enabled or self._db is None or not group_id or not text.strip():
            return None
        now = int(time.time() if ts is None else ts)
        norm = claim_norm(text)
        if not norm:
            return None
        gid = str(group_id)
        speaker = str(speaker_uid or "")
        row = self._db.execute(
            "SELECT id, text, speakers, hits, promoted FROM claims "
            "WHERE group_id=? AND norm=?",
            (gid, norm),
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO claims(group_id, norm, text, speakers, hits, "
                "first_ts, last_ts, promoted) VALUES (?,?,?,?,1,?,?,0)",
                (gid, norm, text.strip()[:80], speaker, now, now),
            )
            # 候选只留 60 天，最多 2000 条：没被第二个人接上的话就自然忘掉
            self._db.execute("DELETE FROM claims WHERE last_ts < ?", (now - 60 * 86400,))
            rows = self._db.execute(
                "SELECT id FROM claims ORDER BY last_ts DESC LIMIT -1 OFFSET ?",
                (2000,),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                ph = ",".join("?" * len(ids))
                self._db.execute(f"DELETE FROM claims WHERE id IN ({ph})", ids)
            self._db.commit()
            return None

        speakers = [s for s in str(row["speakers"] or "").split(CONFIRM_SEP) if s]
        if not speaker or speaker in speakers:
            # 同一个人反复说，不算新证据
            self._db.execute("UPDATE claims SET last_ts=? WHERE id=?", (now, row["id"]))
            self._db.commit()
            return None
        speakers.append(speaker)
        hits = int(row["hits"] or 1) + 1
        promoted = int(row["promoted"] or 0)
        self._db.execute(
            "UPDATE claims SET speakers=?, hits=?, last_ts=? WHERE id=?",
            (CONFIRM_SEP.join(speakers), hits, now, row["id"]),
        )
        result = None
        if len(speakers) >= 2:
            # 第一句算插入、当前这句算确认：可信度 0.5 → 0.6
            self.add_group_fact(
                gid, row["text"], speaker_uid=speakers[0],
                source="repeat", kind="说法", ts=now,
            )
            self.add_group_fact(
                gid, row["text"], speaker_uid=speaker,
                source="repeat", kind="说法", ts=now,
            )
            if not promoted:
                self._db.execute("UPDATE claims SET promoted=1 WHERE id=?", (row["id"],))
                log.info(
                    "群里不止一个人这么说，记成群里的说法：%s", row["text"][:40]
                )
            result = {
                "text": row["text"],
                "speakers": len(speakers),
                "promoted": not promoted,
            }
        self._db.commit()
        return result

    def claim_state(
        self, group_id: str, text: str
    ) -> dict[str, Any] | None:
        """这句"说法"现在有几个人说过、有没有升级。"""
        if not self.enabled or self._db is None or not group_id or not text.strip():
            return None
        norm = claim_norm(text)
        if not norm:
            return None
        row = self._db.execute(
            "SELECT speakers, hits, promoted FROM claims WHERE group_id=? AND norm=? LIMIT 1",
            (str(group_id), norm),
        ).fetchone()
        if row is None:
            return None
        speakers = [s for s in str(row["speakers"] or "").split(CONFIRM_SEP) if s]
        return {
            "speakers": len(speakers),
            "hits": int(row["hits"] or 1),
            "promoted": bool(row["promoted"]),
        }

    def facts_of(
        self,
        uid: str,
        limit: int = 3,
        min_confidence: float = 0.0,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """取关于某人的事实：可信度高的优先，其次最近提起的。

        kind 只取某一类（比如「约好」）：主人答应过的事，和别的偏好不能混在一起用。
        """
        if not self.enabled or self._db is None or not uid:
            return []
        sql = (
            "SELECT kind, text, COALESCE(source, 'local') AS source, "
            "COALESCE(confidence, 0.7) AS confidence, COALESCE(hits, 1) AS hits, "
            "COALESCE(last_ts, ts) AS last_ts FROM facts "
            "WHERE uid=? AND COALESCE(scope, 'personal')='personal' "
            "AND COALESCE(confidence, 0.7) >= ? "
        )
        params: list[Any] = [str(uid), float(min_confidence)]
        if kind:
            sql += "AND kind=? "
            params.append(str(kind))
        sql += "ORDER BY confidence DESC, COALESCE(last_ts, ts) DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def facts_for_user(self, uid: str, limit: int = 10) -> list[dict[str, Any]]:
        """给本人看的语义卡片：带 id，方便按序号删；只取 personal。

        和 `facts_of()` 的区别是**多带 id** —— 群友说「忘掉第 2 条」时，
        序号必须能落到具体一行上，不能靠文字再猜一次。
        """
        if not self.enabled or self._db is None or not uid:
            return []
        rows = self._db.execute(
            "SELECT id, kind, text, COALESCE(source, 'local') AS source, "
            "COALESCE(last_ts, ts) AS last_ts FROM facts "
            "WHERE uid=? AND COALESCE(scope, 'personal')='personal' "
            "ORDER BY confidence DESC, COALESCE(last_ts, ts) DESC, id DESC LIMIT ?",
            (str(uid), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_facts(self, uid: str, ids: list[int] | None = None) -> list[str]:
        """删掉关于某人的 personal 事实；ids=None 删全部。返回删掉的文字。

        只动 `uid=本人` 的 personal 卡片：群共识（scope=group）和别人的卡
        一律不碰。调用方负责确认 uid 是本人。
        """
        if not self.enabled or self._db is None or not uid:
            return []
        owner = str(uid)
        if ids is None:
            rows = self._db.execute(
                "SELECT id, text FROM facts "
                "WHERE uid=? AND COALESCE(scope, 'personal')='personal'",
                (owner,),
            ).fetchall()
            self._db.execute(
                "DELETE FROM facts "
                "WHERE uid=? AND COALESCE(scope, 'personal')='personal'",
                (owner,),
            )
        else:
            id_list = [int(i) for i in ids]
            if not id_list:
                return []
            ph = ",".join("?" * len(id_list))
            rows = self._db.execute(
                f"SELECT id, text FROM facts WHERE uid=? "
                f"AND COALESCE(scope, 'personal')='personal' AND id IN ({ph})",
                (owner, *id_list),
            ).fetchall()
            if rows:
                self._db.execute(
                    f"DELETE FROM facts WHERE uid=? "
                    f"AND COALESCE(scope, 'personal')='personal' AND id IN ({ph})",
                    (owner, *id_list),
                )
        self._db.commit()
        return [str(r["text"]) for r in rows]

    def facts_of_group(
        self, group_id: str, limit: int = 2, query: str = ""
    ) -> list[dict[str, Any]]:
        """取这个群的共识。

        不是全倒出来：和当前话题有共同关键词的优先；没有重合的，只有被至少
        两个人确认过（hits>=2）才值得提，免得每句话都插一段群规。
        """
        if not self.enabled or self._db is None or not group_id or limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT id, kind, text, source, COALESCE(confidence, 0.55) AS confidence, "
            "COALESCE(hits, 1) AS hits, COALESCE(last_ts, ts) AS last_ts FROM facts "
            "WHERE COALESCE(scope, 'personal')='group' AND group_id=? "
            "ORDER BY confidence DESC, COALESCE(last_ts, ts) DESC LIMIT 100",
            (str(group_id),),
        ).fetchall()
        if not rows:
            return []
        qk = set(keywords(query)) if query else set()
        if not qk:
            return []
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            fk = set(keywords(r["text"]))
            overlap = len(qk & fk)
            hits = int(r["hits"] or 1)
            if overlap == 0 and hits < 2:
                continue
            score = _claim_score(overlap, r["confidence"] or 0.55, hits)
            scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -int(x[1]["last_ts"] or 0)))
        return [
            {
                "kind": r["kind"],
                "text": r["text"],
                "source": r["source"] or "group",
                "confidence": r["confidence"],
                "hits": r["hits"],
            }
            for _score, r in scored[:limit]
        ]

    def facts_count(self, uid: str) -> int:
        if not self.enabled or self._db is None or not uid:
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM facts "
            "WHERE uid=? AND COALESCE(scope, 'personal')='personal'",
            (str(uid),),
        ).fetchone()
        return int(row["n"] or 0)

    # ---------- 搜索结果的缓存（knowledge） ----------
    #
    # 这不是"事实"：网页会变、会过时，也不该混进"关于某人"的画像。
    # 只存一句归一化的话 + 出处 + 过期时间，下次遇到相关话题时提示她
    # "上次查到过这个，可能过时了"，省一次重复搜索，也让回答有出处。

    def add_knowledge(
        self,
        topic: str,
        text: str,
        url: str = "",
        source: str = "web",
        ttl_days: int | None = None,
        ts: int | None = None,
    ) -> bool:
        """记一条网上查到的内容。

        和群共识用同一套逻辑：按 claim 归一化去重（同一个说法只留一行）；
        群里已经有人这么说过，就把这次网上的确认记到那条事实上（可信度 +0.1）。
        """
        if not self.enabled or self._db is None:
            return False
        if ttl_days is not None and int(ttl_days) <= 0:
            return False
        topic = (topic or "").strip()[:120]
        text = (text or "").strip()[:KNOWLEDGE_TEXT_MAX]
        if not topic or not text:
            return False
        now = int(time.time() if ts is None else ts)
        ttl = KNOWLEDGE_TTL_DAYS if ttl_days is None else max(1, int(ttl_days))
        expires_at = now + ttl * 86400
        norm = fact_norm(topic) + "|" + fact_norm(url or text[:60])
        claim_norm = fact_norm(text)
        # kw 是整条缓存（问题 + 网页摘要）的词，给"这条说法网上见过没"做粗筛；
        # topic_kw 只放问题本身的词 —— 复用时只看它，摘要里的泛词不参与匹配。
        kw = " ".join(keywords(topic + " " + text))
        topic_kw = " ".join(w for w in keywords(topic) if w not in _NOT_TOPIC_WORDS)
        # 同一个说法（哪怕换了个问法）只留一行：群里同一个梗被问第二遍不该又存一条
        row = self._db.execute(
            "SELECT id, topic, hits FROM knowledge WHERE norm=? OR claim_norm=? LIMIT 1",
            (norm, claim_norm),
        ).fetchone()
        corroborated = self._confirm_facts(text, claim_norm)
        if row:
            # 换了个问法、但第一条结果还是同一个网页：只刷新内容/时间，
            # **不把 topic 改成新问题**。否则缓存会被一次次重贴标签，最后
            # 谁也说不清这条到底是"哪个问题查出来的"——那正是串味的来源。
            same_topic = fact_norm(row["topic"]) == fact_norm(topic)
            if same_topic:
                self._db.execute(
                    "UPDATE knowledge SET topic=?, text=?, url=COALESCE(?, url), kw=?, "
                    "topic_kw=?, ts=?, last_ts=?, expires_at=?, hits=?, claim_norm=?, "
                    "corroborated=MAX(COALESCE(corroborated, 0), ?) WHERE id=?",
                    (
                        topic, text, url or None, kw, topic_kw, now, now, expires_at,
                        int(row["hits"] or 1) + 1, claim_norm, int(corroborated), row["id"],
                    ),
                )
            else:
                self._db.execute(
                    "UPDATE knowledge SET text=?, url=COALESCE(?, url), kw=?, ts=?, "
                    "last_ts=?, expires_at=?, hits=?, claim_norm=?, "
                    "corroborated=MAX(COALESCE(corroborated, 0), ?) WHERE id=?",
                    (
                        text, url or None,
                        " ".join(keywords(row["topic"] + " " + text)),
                        now, now, expires_at, int(row["hits"] or 1) + 1,
                        claim_norm, int(corroborated), row["id"],
                    ),
                )
        else:
            self._db.execute(
                "INSERT INTO knowledge(topic, text, url, source, norm, kw, topic_kw, ts, "
                "expires_at, hits, confidence, claim_norm, last_ts, confirmers, corroborated) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,'w:web',?)",
                (
                    topic, text, url or None, source, norm, kw, topic_kw, now, expires_at,
                    FACT_SOURCE_CONFIDENCE.get(source, FACT_SOURCE_CONFIDENCE["web"]),
                    claim_norm, now, int(corroborated),
                ),
            )
        # 顺手清掉过期很久的，再按条数封顶（搜索结果不该无限长）
        self._db.execute(
            "DELETE FROM knowledge WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now - 30 * 86400,),
        )
        rows = self._db.execute(
            "SELECT id FROM knowledge ORDER BY ts DESC LIMIT -1 OFFSET ?",
            (KNOWLEDGE_MAX_ROWS,),
        ).fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            ph = ",".join("?" * len(ids))
            self._db.execute(f"DELETE FROM knowledge WHERE id IN ({ph})", ids)
        self._db.commit()
        return True

    def _confirm_facts(self, text: str, claim_norm: str) -> bool:
        """网上这条说法，群里是不是已经有人这么说过。

        是的话，把「网上也查得到」记成那次说法的一个确认方（可信度 +0.1）。
        这只在有同一条说法时才发生，不是"搜到什么都往上贴"。
        """
        if self._db is None or not text:
            return False
        rows = self._db.execute(
            "SELECT id, text, norm, confirmers, confidence FROM facts "
            "ORDER BY COALESCE(last_ts, ts) DESC LIMIT 300"
        ).fetchall()
        for r in rows:
            if not _claims_match(text, claim_norm, r["text"], r["norm"]):
                continue
            keys = [k for k in str(r["confirmers"] or "").split(CONFIRM_SEP) if k]
            if "w:web" not in keys:
                keys.append("w:web")
                conf = min(
                    FACT_CONF_MAX,
                    float(r["confidence"] or FACT_SOURCE_CONFIDENCE["web"]) + FACT_CONF_STEP,
                )
                self._db.execute(
                    "UPDATE facts SET confirmers=?, confidence=?, last_ts=? WHERE id=?",
                    (CONFIRM_SEP.join(keys), conf, int(time.time()), r["id"]),
                )
                log.info("网上查到的印证了那条说法（可信度 %.2f）：%s", conf, r["text"][:40])
            return True
        return False

    def _knowledge_confirms(self, text: str, norm: str) -> bool:
        """反过来：这次记下的说法，网上是不是已经查到过。"""
        if self._db is None or not text:
            return False
        kws = set(keywords(text))
        rows = self._db.execute(
            "SELECT text, claim_norm, kw FROM knowledge LIMIT 300"
        ).fetchall()
        for r in rows:
            # 先用存好的关键词粗筛，省得每条都现切一遍 jieba
            fk = set(str(r["kw"] or "").split())
            if r["claim_norm"] != norm and len(kws & fk) < 2:
                continue
            if _claims_match(text, norm, r["text"], r["claim_norm"]):
                return True
        return False

    def knowledge_for(
        self, query: str, limit: int = 1, now: float | None = None, strict: bool = False
    ) -> list[dict[str, Any]]:
        """捞和当前问题相关的网上缓存。

        ``strict=False``：同一话题也返回（只配当"上次查到过"的提示）；
        ``strict=True``：只返回**同一个问题**（问了同一件事），才配当答案复用。
        匹配只看问题本身的话题词，不看网页摘要（见 ``_knowledge_kind``）。
        """
        if not self.enabled or self._db is None or not query.strip() or limit <= 0:
            return []
        now = time.time() if now is None else float(now)
        rows = self._db.execute(
            "SELECT topic, text, url, topic_kw, ts, COALESCE(hits, 1) AS hits, "
            "COALESCE(confidence, ?) AS confidence, "
            "COALESCE(corroborated, 0) AS corroborated FROM knowledge "
            "WHERE expires_at IS NULL OR expires_at > ? "
            "ORDER BY COALESCE(hits, 1) DESC, ts DESC LIMIT 200",
            (FACT_SOURCE_CONFIDENCE["web"], now),
        ).fetchall()
        if not rows:
            return []
        qnorm = fact_norm(query)
        qk = set(keywords(query))
        scored: list[tuple[str, float, sqlite3.Row]] = []
        for r in rows:
            raw_kw = r["topic_kw"]
            tk = (
                set(str(raw_kw).split())
                if raw_kw is not None
                else set(keywords(r["topic"]))
            ) - _NOT_TOPIC_WORDS
            kind = _knowledge_kind(qnorm, qk, fact_norm(r["topic"]), tk)
            if not kind or (strict and kind != "same"):
                continue
            overlap = len(qk & tk)
            score = _claim_score(overlap, r["confidence"], r["hits"])
            scored.append((kind, score, r))
        # 同一个问题排在最前，再按可信度/命中数排
        scored.sort(key=lambda x: (0 if x[0] == "same" else 1, -x[1], -int(x[2]["ts"] or 0)))
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kind, score, r in scored:
            key = fact_norm(r["topic"])[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "topic": r["topic"],
                    "text": r["text"],
                    "url": r["url"] or "",
                    "ts": r["ts"],
                    "hits": r["hits"],
                    "confidence": float(r["confidence"] or 0.0),
                    "corroborated": bool(r["corroborated"]),
                    "match": kind,
                    "score": round(score, 2),
                }
            )
            if len(out) >= limit:
                break
        return out

    def knowledge_search(
        self,
        needle: str,
        limit: int = 20,
        now: float | None = None,
        *,
        url_like: str = "",
    ) -> list[dict[str, Any]]:
        """按原文搜知识库（不做话题相似度）。

        用于「名字 → 直播间」这类精确找链接：普通 `knowledge_for` 只认话题，
        几十条知识里带同一个名字的链接不一定会排进前几条。这里只做 LIKE，
        返回最近的若干条，调用方自己从正文/URL 里抠房间号。

        ``url_like`` 可只留正文/URL 里带这个片段的条目（如 ``live.bilibili.com``）。
        直播房号这类「旧链接」经常被大量新语料挤到 20 条之外，按链接过滤比
        单纯放大 limit 更稳。
        """
        text = str(needle or "").strip()
        if not self.enabled or self._db is None or len(text) < 2:
            return []
        now = time.time() if now is None else float(now)
        pattern = f"%{text}%"
        sql = (
            "SELECT topic, text, url, ts FROM knowledge "
            "WHERE (topic LIKE ? OR text LIKE ? OR url LIKE ?) "
            "AND (expires_at IS NULL OR expires_at > ?)"
        )
        args: list[Any] = [pattern, pattern, pattern, now]
        hint = str(url_like or "").strip()
        if hint:
            like = f"%{hint}%"
            sql += " AND (topic LIKE ? OR text LIKE ? OR url LIKE ?)"
            args.extend([like, like, like])
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self._db.execute(sql, args).fetchall()
        return [
            {
                "topic": str(row["topic"] or ""),
                "text": str(row["text"] or ""),
                "url": str(row["url"] or ""),
                "ts": float(row["ts"] or 0),
            }
            for row in rows
        ]

    def knowledge_lookup(
        self, query: str, now: float | None = None, strict: bool = False
    ) -> dict[str, Any] | None:
        """「这个是不是已经查过」的统一入口：闸门和搜索工具都用它。

        ``strict=True`` 表示"同一个问题查过"：只共用一个词不算，只算同一个话题
        但换了问题也不算（「暗区突围出装」不该复用「暗区突围主播水平」的答案）。
        """
        hits = self.knowledge_for(query, 1, now=now, strict=strict)
        return hits[0] if hits else None

    def mark_lore_used(self, ids: list[int], ts: int | None = None) -> None:
        """记下这次给模型讲过的克里斯语料条目，避免连续重复。"""
        if not self.enabled or self._db is None:
            return
        now = int(time.time() if ts is None else ts)
        rows = [(int(i), now) for i in ids if int(i) > 0]
        if not rows:
            return
        self._db.executemany(
            "INSERT INTO lore_use(candidate_id, ts, hits) VALUES (?, ?, 1) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "ts=excluded.ts, hits=lore_use.hits+1",
            rows,
        )
        self._db.commit()

    def lore_used_recent(
        self, days: int = LORE_USE_DAYS, now: float | None = None
    ) -> set[int]:
        """最近讲过的条目 id；讲过的 14 天内优先不重复。"""
        if not self.enabled or self._db is None:
            return set()
        cutoff = int(
            (time.time() if now is None else now) - max(1, int(days)) * 86400
        )
        rows = self._db.execute(
            "SELECT candidate_id FROM lore_use WHERE ts >= ?", (cutoff,)
        ).fetchall()
        return {int(row[0]) for row in rows}

    def knowledge_count(self) -> int:
        if not self.enabled or self._db is None:
            return 0
        row = self._db.execute("SELECT COUNT(*) AS n FROM knowledge").fetchone()
        return int(row["n"] or 0)

    # ---------- 读取 ----------

    def recent(self, session: str, limit: int) -> list[dict[str, Any]]:
        """最近 N 条，按时间正序返回（直接拼进 messages）。"""
        if not self.enabled or self._db is None or limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT role, content, ts FROM turns WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit),
        ).fetchall()
        return [
            {"role": r["role"], "content": r["content"], "ts": r["ts"]}
            for r in reversed(rows)
        ]

    def brief(self, session: str, uid: str | None = None) -> dict[str, Any]:
        """给提示词用的轻量状态：本会话最早/最近一条、关于这个人几条事实。

        她知道自己"记得从哪开始"，被问「你怎么记不住」时才不会编一个性格解释。
        """
        if not self.enabled or self._db is None:
            return {}
        row = self._db.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last "
            "FROM turns WHERE session=?",
            (session,),
        ).fetchone()
        return {
            "turns": int(row["n"] or 0),
            "first": int(row["first"] or 0),
            "last": int(row["last"] or 0),
            "facts": self.facts_count(str(uid)) if uid else 0,
        }

    def since(self, ts: int, limit: int = 200) -> list[dict[str, Any]]:
        """某时间点之后的对话，按时间正序返回（发说说找"今天的经历"用）。"""
        if not self.enabled or self._db is None or limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT role, content, session, ts FROM turns WHERE ts>=? ORDER BY id DESC LIMIT ?",
            (int(ts), int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def range(
        self, start: int, end: int, limit: int = 400
    ) -> list[dict[str, Any]]:
        """某段自然日内的对话，按时间正序返回（写日记找"今天的事"用）。

        和 `since()` 的区别：这个有上界，避免把第二天凌晨的话写进昨天的日记；
        取的是区间内最近的 `limit` 条。
        """
        if not self.enabled or self._db is None or limit <= 0 or end <= start:
            return []
        rows = self._db.execute(
            "SELECT role, content, session, ts FROM turns "
            "WHERE ts>=? AND ts<? ORDER BY id DESC LIMIT ?",
            (int(start), int(end), int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def _recent_floor(self, session: str, exclude_recent: int) -> int:
        """上下文窗口里最老那条的 id；recall 只用比它更老的记忆。"""
        if exclude_recent <= 0 or self._db is None:
            return 2**62
        row = self._db.execute(
            "SELECT MIN(id) AS m FROM (SELECT id FROM turns WHERE session=? ORDER BY id DESC LIMIT ?)",
            (session, exclude_recent),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else 2**62

    def recall(
        self,
        session: str,
        query: str,
        limit: int = 3,
        exclude_recent: int = 12,
        min_score: float = 0.25,
        owner_id: str | None = None,
        now: float | None = None,
        mode: str = "chat",
    ) -> list[dict[str, Any]]:
        """先按关键词捞，不够再用语义补。

        owner_id 给了就**按人**回溯（跨私聊/群聊），没给才按会话。
        「上下文窗口」始终按会话，只有长期记忆按人。

        mode="memory" 给「你还记得… / 我上次说的…」这类记忆题用：
        - 多给几条；
        - 命中用户提问时，把紧接着的回答一起带回来；
        - 问「你昨天说的」时优先她自己说过的话；
        - 关键词被"来点笑话"这类请求回声占满时，仍然跑一次语义。

        普通聊天保持老行为，避免噪音变多。

        召回本身不等于"想起来"：只有同一个人的同一句话，隔了足够久、
        又因为**同一个词**被捞到，才给这条记忆记一次数（见 _touch）。
        """
        if not self.enabled or self._db is None:
            return []
        now = time.time() if now is None else float(now)
        memory_mode = mode == "memory"
        limit = max(1, int(limit))
        prefer_self = bool(memory_mode and _SELF_QUERY.search(query or ""))

        kws = keywords(query, single_nouns=True)
        if not kws:
            if _short_query(query) and not memory_mode:
                return []
            newest_recent = self._recent_floor(session, exclude_recent)
            out = self._semantic_only(session, query, limit, exclude_recent, owner_id, now)
            if memory_mode:
                out = self._expand_pairs(out, newest_recent, {x["tid"] for x in out}, 2)
            return self._touch(out, owner_id, now)

        import math

        db = self._db
        # 回溯范围：按人（跨私聊/群聊）并上当前会话，保住本群其他人的上下文
        if owner_id:
            scope_sql = "(t.owner_id = ? OR t.session = ?)"
            scope_params = (owner_id, session)
        else:
            scope_sql, scope_params = "t.session = ?", (session,)

        # 最近 exclude_recent 条不参与（已经在上下文里了）；传 0 表示不排除
        newest_recent = self._recent_floor(session, exclude_recent)
        cutoff = int(now) - RECALL_DAYS * 86400

        # 1. 这些关键词在回溯范围内出现过几次（用来算 IDF）
        ph = ",".join("?" * len(kws))
        df_rows = db.execute(
            f"""
            SELECT k.kw AS kw, COUNT(DISTINCT k.turn_id) AS df
            FROM keywords k JOIN turns t ON t.id = k.turn_id
            WHERE k.kw IN ({ph}) AND {scope_sql}
            GROUP BY k.kw
            """,
            (*kws, *scope_params),
        ).fetchall()
        df = {r["kw"]: r["df"] for r in df_rows}
        # 从没出现过的词帮不上忙，直接不算进分母
        usable = [k for k in kws if df.get(k)]
        if not usable:
            if _short_query(query) and not memory_mode:
                return []
            out = self._semantic_only(session, query, limit, exclude_recent, owner_id, now)
            if memory_mode:
                out = self._expand_pairs(out, newest_recent, {x["tid"] for x in out}, 2)
            return self._touch(out, owner_id, now)

        n_turns = db.execute(
            f"SELECT COUNT(*) AS n FROM turns t WHERE {scope_sql}", scope_params
        ).fetchone()["n"] or 1

        # 只剩一个关键词时，它必须足够稀有那么单独匹配才有意义。
        # 否则「团子最近怎么样」里的「最近」会把一堆无关对话捞出来（归一化后得分还是 1.0）。
        if len(usable) == 1:
            rare_enough = max(2, int(n_turns * 0.05))
            if df[usable[0]] > rare_enough:
                if _short_query(query) and not memory_mode:
                    return []
                out = self._semantic_only(
                    session, query, limit, exclude_recent, owner_id, now
                )
                if memory_mode:
                    out = self._expand_pairs(
                        out, newest_recent, {x["tid"] for x in out}, 2
                    )
                return self._touch(out, owner_id, now)

        idf = {k: math.log((n_turns + 1) / (df[k] + 1)) + 1.0 for k in usable}
        total_weight = sum(idf.values()) or 1.0

        # 2. 找出命中的历史回合，按加权得分排序
        ph2 = ",".join("?" * len(usable))
        hit_rows = db.execute(
            f"""
            SELECT k.turn_id AS tid, k.kw AS kw, t.content AS content, t.ts AS ts,
                   t.role AS role, t.session AS session, t.owner_id AS owner_id
            FROM keywords k JOIN turns t ON t.id = k.turn_id
            WHERE k.kw IN ({ph2}) AND {scope_sql}
              AND t.id < ? AND (t.kind = 'long' OR t.ts >= ?)
            """,
            (*usable, *scope_params, newest_recent, cutoff),
        ).fetchall()

        per_turn: dict[int, dict[str, Any]] = {}
        for r in hit_rows:
            entry = per_turn.setdefault(
                r["tid"],
                {
                    "tid": r["tid"],
                    "content": r["content"],
                    "ts": r["ts"],
                    "role": r["role"],
                    "session": r["session"],
                    "owner_id": r["owner_id"],
                    "score": 0.0,
                    "kws": [],
                },
            )
            entry["score"] += idf[r["kw"]]
            entry["kws"].append(r["kw"])

        scored = []
        for tid, e in per_turn.items():
            score = e["score"] / total_weight
            if memory_mode and _REQUEST_ECHO.search(body_of(e["content"])):
                # 请求回声只是"要东西"，不是内容本身；同分时降一点权
                score -= 0.12
            if prefer_self and e.get("role") == "assistant":
                score += 0.18
            if score >= min_score:
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], -x[1]["ts"]))

        # 同一个请求被很多人重复时，只留最靠前的一条，别占满所有槽位
        picked: list[tuple[float, dict[str, Any]]] = []
        seen_norm: set[str] = set()
        for score, e in scored:
            norm = fact_norm(e["content"])
            if norm and norm in seen_norm:
                continue
            if norm:
                seen_norm.add(norm)
            picked.append((score, e))
            if len(picked) >= max(limit * 3, 9):
                break

        out = []
        used: set[int] = set()
        for score, e in picked[:limit]:
            out.append(
                {
                    "tid": e["tid"],
                    "content": e["content"],
                    "ts": e["ts"],
                    "role": e.get("role", "user"),
                    "session": e.get("session"),
                    "owner_id": e.get("owner_id"),
                    "score": round(score, 2),
                    "via": "keyword",
                }
            )
            used.add(e["tid"])
            log.info(
                "回忆命中(关键词 %.2f, %s): %s",
                score,
                "/".join(e["kws"]),
                e["content"][:40],
            )

        # 记忆题：把"提问 + 紧接着的回答"成对带回来。
        # 只带提问不带回答，问「你昨晚说的笑话」只会看到「来点笑话」。
        if memory_mode and out:
            out = self._expand_pairs(out, newest_recent, used, 2)

        # 普通聊天：关键词和语义一起凑到 limit，但语义至少留 semantic_floor 条。
        # 关键词再准也可能"字面命中、答非所问"（一个「认真」捞回三条旧回复），
        # 所以不再等"关键词不够"才跑语义，而是固定留一条在场上。
        # 证据特别弱（最高分 <0.45 或只剩泛词）时，语义再多留一条。
        weak = (not memory_mode) and _weak_keyword_evidence(out, usable, df, n_turns)
        if memory_mode:
            need_semantic = max(2, limit - len(out))
        else:
            floor = max(0, self.semantic_floor)
            if weak:
                floor = max(floor, 2)
            need_semantic = max(floor, limit - len(out))
        if need_semantic > 0:
            sem_items: list[dict[str, Any]] = []
            for item in self._semantic_recall(
                session, query, need_semantic, newest_recent, cutoff, used, owner_id
            ):
                if item["tid"] in used:
                    continue
                used.add(item["tid"])
                sem_items.append(item)
            if memory_mode:
                out.extend(sem_items)
            else:
                # 语义先占位，剩下的槽位给关键词；总数仍卡在 limit。
                keep = max(0, limit - len(sem_items))
                out = (out[:keep] + sem_items)[:limit]

        # 到这儿统一过遗忘阈值：太淡的滤掉，够格的才记一次"又想起"
        return self._touch(out, owner_id, now)

    def _expand_pairs(
        self,
        items: list[dict[str, Any]],
        newest_recent: int,
        used: set[int],
        max_extra: int = 2,
    ) -> list[dict[str, Any]]:
        """给命中的 turn 补上同会话里紧挨着的那一条。

        命中用户提问 → 补她当时的回答；命中她的回答 → 补对方的提问。
        这样「你昨天说的那个」能拿到内容，而不是只拿到「来点笑话」。
        """
        if self._db is None or max_extra <= 0:
            return items
        extra: list[dict[str, Any]] = []
        for item in list(items):
            if len(extra) >= max_extra:
                break
            tid = item.get("tid")
            session = item.get("session")
            if tid is None or not session:
                continue
            if item.get("role") == "user":
                row = self._db.execute(
                    "SELECT id, session, role, content, ts, owner_id FROM turns "
                    "WHERE session=? AND id>? AND id<? AND role='assistant' "
                    "ORDER BY id LIMIT 1",
                    (session, tid, newest_recent),
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT id, session, role, content, ts, owner_id FROM turns "
                    "WHERE session=? AND id<? AND id<? AND role='user' "
                    "ORDER BY id DESC LIMIT 1",
                    (session, tid, newest_recent),
                ).fetchone()
            if row is None or row["id"] in used:
                continue
            used.add(row["id"])
            extra.append(
                {
                    "tid": row["id"],
                    "content": row["content"],
                    "ts": row["ts"],
                    "role": row["role"],
                    "session": row["session"],
                    "owner_id": row["owner_id"],
                    "score": round(max(0.05, float(item.get("score") or 0.0) - 0.05), 2),
                    "via": "pair",
                    "pair_of": tid,
                }
            )
            log.info("问答对补全: %s", str(row["content"])[:40])
        return items + extra

    def _touch(
        self,
        rows: list[dict[str, Any]],
        owner_id: str | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """过一遍遗忘阈值；够格的才算"又想起一次"，顺手加固。

        以前这里只要被召回就 +1，于是群里一个热词反复出现，
        一下午就能把"对咯"顶成长期记忆。现在的门槛：

        1. 必须是**用户**说的话（她自己接的梗不算）
        2. 按人记忆时，必须是**这个人**说的（别人的话不该记到他头上）
        3. 必须是关键词命中，且重合度够（语义"有点像"不算）
        4. 内容够具体：不是寒暄、不是一句问话、长度过关
        5. 离上一次"算数的想起"至少隔 promote_min_gap_hours
        """
        if not rows or self._db is None:
            return rows
        now = time.time() if now is None else float(now)
        kept: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            tid = row.get("tid")
            if tid is None:
                kept.append(row)
                continue
            if tid in seen:
                kept.append(row)   # 同一次召回命中两次，只算一次
                continue
            seen.add(tid)
            rec = self._db.execute(
                "SELECT kind, strength, last_access, hits, hit_ts, role, owner_id, content "
                "FROM turns WHERE id=?",
                (tid,),
            ).fetchone()
            if rec is None:
                continue
            kind = str(rec["kind"] or "short")
            left = effective(
                rec["strength"], kind, rec["last_access"] or 0, now, self.forget_rate
            )
            if left < self.forget_threshold:
                log.debug("这条已经淡忘了：%s", str(rec["content"])[:30])
                continue
            ok, why = self._counts_as_recall(rec, row, owner_id, now)
            if not ok:
                log.debug("召回但不算想起（%s）：%s", why, str(rec["content"])[:30])
                # 语义召回不算"又想起一次"（不升级、不涨 hits），但至少让
                # "最近碰到过"往前挪一点。否则天天被语义想起的事，24 天后
                # 还是会被遗忘曲线清掉。
                if (
                    row.get("via") == "semantic"
                    and self.semantic_recall_bump > 0
                    and left >= self.forget_threshold
                ):
                    bumped = min(
                        SHORT_MAX,
                        float(rec["strength"] or 0.0) + self.semantic_recall_bump,
                    )
                    self._db.execute(
                        "UPDATE turns SET strength=?, last_access=? WHERE id=?",
                        (bumped, int(now), tid),
                    )
                kept.append(row)
                continue
            hits = int(rec["hits"] or 0) + 1
            new_kind = kind
            new_strength = bump_strength(rec["strength"], kind)
            # 反复想起、且每次都隔了足够久的事，才变成长期记忆
            if kind != "long" and self.promote_hits > 0 and hits >= self.promote_hits:
                new_kind, new_strength = "long", LONG_STRENGTH
                log.info("隔了 %d 次又想起，转成长期记忆：%s", hits, str(rec["content"])[:30])
            self._db.execute(
                "UPDATE turns SET hits=?, last_access=?, strength=?, kind=?, hit_ts=? WHERE id=?",
                (hits, int(now), new_strength, new_kind, int(now), tid),
            )
            kept.append(row)
        self._db.commit()
        return kept

    def _counts_as_recall(
        self,
        rec: sqlite3.Row,
        row: dict[str, Any],
        owner_id: str | None,
        now: float,
    ) -> tuple[bool, str]:
        """这一次召回算不算"又想起一次"。"""
        if str(rec["kind"] or "short") == "long":
            return False, "已经是长期"
        if str(rec["role"] or "") != "user":
            return False, "不是用户说的"
        if owner_id is not None:
            if str(rec["owner_id"] or "") != str(owner_id):
                return False, "不是这个人说的"
        elif rec["owner_id"] is not None:
            # 按会话召回，但这条有明确归属：读可以，别拿它改长期记忆
            return False, "跨归属读取"
        if row.get("via") != "keyword":
            return False, "只是语义相似"
        score = float(row.get("score") or 0.0)
        if score < self.promote_min_score:
            return False, f"关键词重合度 {score:.2f} 不够"
        if not worth_promoting(rec["content"], self.promote_min_chars):
            return False, "太短或像寒暄/问句"
        gap = self.promote_min_gap_hours * 3600.0
        last_hit = float(rec["hit_ts"] or 0)
        if last_hit and now - last_hit < gap:
            return False, "离上次想起太近"
        return True, ""

    def reclassify(self) -> int:
        """按当前规则重判所有记忆的长期/短期（老库迁移用）。

        注意：这会把"被反复想起"攒出来的长期也一起重判，只留显式事实。
        """
        if not self.enabled or self._db is None:
            return 0
        rows = self._db.execute("SELECT id, role, content FROM turns").fetchall()
        changed = 0
        for r in rows:
            kind, strength = classify(r["content"], r["role"])
            self._db.execute(
                "UPDATE turns SET kind=?, strength=? WHERE id=?", (kind, strength, r["id"])
            )
            changed += 1
        self._db.commit()
        log.info("重判了 %d 条记忆的长期/短期", changed)
        return changed

    def prune(self, rate: float = 0.08, threshold: float = 0.15) -> int:
        """把已经淡忘的短期记忆删掉（长期记忆永不动）。"""
        if not self.enabled or self._db is None:
            return 0
        now = time.time()
        rows = self._db.execute(
            "SELECT id, kind, strength, last_access FROM turns "
            "WHERE kind != 'long' OR kind IS NULL"
        ).fetchall()
        doomed = [
            r["id"]
            for r in rows
            if effective(r["strength"], r["kind"] or "short", r["last_access"] or 0, now, rate)
            < threshold
        ]
        if doomed:
            ph = ",".join("?" * len(doomed))
            self._db.execute(f"DELETE FROM turns WHERE id IN ({ph})", doomed)
            self._db.execute(f"DELETE FROM keywords WHERE turn_id IN ({ph})", doomed)
            self._db.commit()
            log.info("清掉了 %d 条已经淡忘的记忆", len(doomed))
        return len(doomed)

    def review_long(self) -> list[dict[str, Any]]:
        """按现在的标准复查长期记忆：哪些其实不配。

        判据只有一条：**用户自己说的、按现行规则仍算明确事实**。
        当初靠"被反复召回"升上来的不算数 —— 那个机制以前太松，
        所以旧 hits 不能当免死金牌。真正重要的事降级后还会被重新想起，再攒回来。
        """
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            "SELECT id, role, content, owner_id, hits FROM turns WHERE kind='long' ORDER BY id"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            kind, strength = classify(r["content"], r["role"])
            reason = ""
            if str(r["role"] or "") != "user":
                reason = "本鱼自己的回复"
            elif kind != "long":
                reason = "不是明确的事实"
            out.append(
                {
                    "id": r["id"],
                    "role": r["role"],
                    "content": r["content"],
                    "hits": r["hits"],
                    "keep": not reason,
                    "reason": reason,
                    "strength": strength,
                }
            )
        return out

    def tighten(self, apply: bool = False) -> dict[str, Any]:
        """把 review_long 判为不配的长期记忆降回短期。"""
        rows = self.review_long()
        doomed = [r for r in rows if not r["keep"]]
        if apply and doomed and self._db is not None:
            for r in doomed:
                # 内容和索引都留着，只是不再享受"永久 + 不过时间窗口"的待遇
                self._db.execute(
                    "UPDATE turns SET kind='short', strength=?, hits=0, hit_ts=NULL WHERE id=?",
                    (float(r["strength"] or 1.0), r["id"]),
                )
            self._db.commit()
            log.info("收紧了长期记忆：降级 %d 条", len(doomed))
        return {
            "total": len(rows),
            "keep": len(rows) - len(doomed),
            "demote": len(doomed),
            "applied": bool(apply),
            "items": doomed,
        }

    def _semantic_only(
        self, session: str, query: str, limit: int, exclude_recent: int,
        owner_id: str | None = None, now: float | None = None,
    ) -> list[dict[str, Any]]:
        """关键词完全没得用时，直接走语义。"""
        if self._db is None:
            return []
        newest_recent = self._recent_floor(session, exclude_recent)
        cutoff = int(time.time() if now is None else now) - RECALL_DAYS * 86400
        return self._semantic_recall(
            session, query, limit, newest_recent, cutoff, set(), owner_id
        )

    def _semantic_recall(
        self,
        session: str,
        query: str,
        need: int,
        newest_recent: int,
        cutoff: int,
        used: set[int],
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from .embed import Embedder

        if self._db is None or self.embedder is None or need <= 0:
            return []
        # 用 ready 而不是 available：模型还在后台加载时直接跳过，
        # 不能让一条消息卡在等模型上（这台机器加载要 9 秒）
        if not getattr(self.embedder, "ready", False):
            log.debug("语义召回跳过：模型还没加载完")
            return []

        try:
            blobs = self.embedder.encode([query])
            q_blob = blobs[0] if blobs else None
        except Exception as exc:  # noqa: BLE001
            log.warning("查询编码失败: %s", exc)
            return []
        if not q_blob:
            return []

        if owner_id:
            scope_sql = "(owner_id = ? OR session = ?)"
            scope_params = (owner_id, session)
        else:
            scope_sql, scope_params = "session = ?", (session,)

        rows = self._db.execute(
            f"""
            SELECT id, content, ts, role, session, owner_id, embedding FROM turns
            WHERE {scope_sql} AND id < ? AND (kind = 'long' OR ts >= ?) AND embedding IS NOT NULL
            ORDER BY id DESC LIMIT ?
            """,
            (*scope_params, newest_recent, cutoff, self.semantic_candidates),
        ).fetchall()

        candidates = []
        meta: dict[int, Any] = {}
        for r in rows:
            if r["id"] in used:
                continue
            vec = Embedder.to_vector(r["embedding"])
            if vec is None:
                continue
            candidates.append((r["id"], vec))
            meta[r["id"]] = r

        picked = Embedder.rank(q_blob, candidates, self.semantic_z, self.semantic_cos)
        out = []
        for tid, z in picked[:need]:
            r = meta[tid]
            out.append(
                {
                    "tid": tid,
                    "content": r["content"],
                    "ts": r["ts"],
                    "role": r["role"],
                    "session": r["session"],
                    "owner_id": r["owner_id"],
                    "score": z,
                    "via": "semantic",
                }
            )
            log.info("回忆命中(语义 z=%.2f): %s", z, r["content"][:40])
        return out

    # ---------- 维护 ----------

    def stats(self) -> dict[str, Any]:
        if not self.enabled or self._db is None:
            return {"enabled": False}
        db = self._db
        total = db.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
        kwn = db.execute("SELECT COUNT(*) AS n FROM keywords").fetchone()["n"]
        sessions = db.execute(
            "SELECT session, COUNT(*) AS n FROM turns GROUP BY session ORDER BY n DESC"
        ).fetchall()
        span = db.execute("SELECT MIN(ts) AS a, MAX(ts) AS b FROM turns").fetchone()
        size = self.path.stat().st_size if self.path.exists() else 0
        embedded = db.execute(
            "SELECT COUNT(*) AS n FROM turns WHERE embedding IS NOT NULL"
        ).fetchone()["n"]
        by_kind = {
            (r["kind"] or "short"): r["n"]
            for r in db.execute(
                "SELECT COALESCE(kind,'short') AS kind, COUNT(*) AS n FROM turns GROUP BY 1"
            ).fetchall()
        }
        now = time.time()
        fading = 0
        for r in db.execute(
            "SELECT kind, strength, last_access FROM turns WHERE COALESCE(kind,'short') != 'long'"
        ).fetchall():
            if effective(r["strength"], r["kind"] or "short", r["last_access"] or 0, now,
                         self.forget_rate) < self.forget_threshold:
                fading += 1
        facts_n = db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
        group_facts = db.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE COALESCE(scope, 'personal')='group'"
        ).fetchone()["n"]
        knowledge = db.execute("SELECT COUNT(*) AS n FROM knowledge").fetchone()["n"]
        return {
            "enabled": True,
            "turns": total,
            "long": by_kind.get("long", 0),
            "short": by_kind.get("short", 0),
            "fading": fading,
            "keywords": kwn,
            "embedded": embedded,
            "facts": facts_n,
            "group_facts": group_facts,
            "knowledge": knowledge,
            "sessions": {r["session"]: r["n"] for r in sessions},
            "first": span["a"],
            "last": span["b"],
            "size_kb": size // 1024,
        }

    def clear(self, session: str | None = None) -> int:
        if not self.enabled or self._db is None:
            return 0
        db = self._db
        if session:
            ids = [r["id"] for r in db.execute("SELECT id FROM turns WHERE session=?", (session,))]
            db.execute("DELETE FROM turns WHERE session=?", (session,))
        else:
            ids = [r["id"] for r in db.execute("SELECT id FROM turns")]
            db.execute("DELETE FROM turns")
        for tid in ids:
            db.execute("DELETE FROM keywords WHERE turn_id=?", (tid,))
        db.commit()
        return len(ids)


def _recall_speaker(item: dict[str, Any]) -> str:
    """这条是谁说的。群聊正文里自带【群友】昵称：，私聊只有"对方"。"""
    content = str(item.get("content") or "")
    if item.get("role") == "assistant":
        return "本鱼"
    m = re.match(r"^【[^】]{1,16}】\s*([^：:\n]{1,24})[：:]", content)
    if m:
        return m.group(1).strip() or "群友"
    session = str(item.get("session") or "")
    return "对方" if session.startswith("private:") else "群友"


def _recall_where(item: dict[str, Any]) -> str:
    session = str(item.get("session") or "")
    if session.startswith("group:"):
        return "群" + session.split(":", 1)[1]
    if session.startswith("private:"):
        return "私聊"
    return "记忆"


def format_recall(items: list[dict[str, Any]], max_chars: int = 160) -> str:
    """把回忆结果整理成给模型看的一段话。

    带上时间、群/私聊、说话人，模型才知道这是"谁在什么时候说的"，
    而不是一段凭空出现的文本。语义命中和问答对补全也各有标记。
    开头还要点明「原话里的我=说话人」：旧话原样喂回去，人称不重锚，
    模型就会把别人说的「我」当成自己（2026-10-27 的「奖励我」实锤）。

    她自己以前的套话（鱼八股）跳过：那是模型的模板，不是她的话。喂回去只会
    让她照着学，等于拿旧八股教新八股。
    """
    if not items:
        return ""
    lines = [
        "你们以前聊过这些（下面是当时的原话，不是现在说的；引用前先按说话人"
        "换人称：别人说的「我」指他自己，本鱼说的「我」指本鱼。只在接得上时"
        "提一句，别生硬复读）："
    ]
    for it in items:
        when = time.strftime("%m-%d %H:%M", time.localtime(it["ts"]))
        body = " ".join(str(it.get("content") or "").split())
        if it.get("role") == "assistant" and antislop.find(body):
            log.debug("回忆跳过她自己的套话：%s", body[:40])
            continue
        # 群聊正文自带「【群友】昵称：」，说话人已经单独列了，别重复一遍
        body = re.sub(r"^【[^】]{1,16}】\s*[^：:\n]{1,24}[：:]\s*", "", body)
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        tag = ""
        if it.get("via") == "semantic":
            tag = "（语义相似，可能不准）"
        elif it.get("via") == "pair":
            tag = "（紧接着的那句）"
        lines.append(
            f"- {when}｜{_recall_where(it)}｜{_recall_speaker(it)}：{body}{tag}"
        )
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
