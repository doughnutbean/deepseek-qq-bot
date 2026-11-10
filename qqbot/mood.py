"""生气的设定：同一个人把同一件事问第三遍，她会烦；烦到冷战就不回了。

和「闭嘴 / 潜水」的区别：那两个是**主人下的静音指令**（谁说都一样、到点自动解），
这个是**她自己的情绪** —— 得她自己消、或者被哄好。所以单独一个状态机，
不跟 `_is_quiet` / `_is_diving` 混在一起。

四档（怒气 0~100，按人算、按话题记）：

| 怒气 | 档位 | 她什么样 | 怎么回去 |
|---|---|---|---|
| <30  | 0 正常 | 照常 | — |
| 30~54 | 1 轻烦 | 「这个本鱼刚才说过啦」 | 几分钟自己消 |
| 55~84 | 2 烦躁 | 短、装聋、贴 [表情:生气] | 认真哄一次 |
| >=85  | 3 冷战 | 硬拒：真的不发，或只回「……」 | 两次认真哄，中途别又问 |

**难点是"同一件事"怎么认。** 这里两层：

1. jieba 关键词（直接复用 `memory.keywords`，和长期记忆同一套词表）
2. 字符相似（difflib）—— 中文短问句经常被停用词吃光（「你是谁」全是停用词），
   关键词是空的，只能看字面。换个语气词、加个"啊"都还得算同一件事。

雷点话题（`.env` 里的 `MOOD_TRIGGER_WORDS`：人机、掉线这些）第二遍会额外加一截，
所以「被别人问第 4 遍」就到冷战，普通话题要第 5 遍。

刻意**不做**语义 embedding：那要在消息路径上跑模型推理（几百毫秒），
而且「上周问的」和「刚才问的」本来就该分开算。这个状态机只看最近十几分钟。

谁被记：按 QQ 号记，不按会话 —— 他私聊问两遍、群里又问一遍，那是第三遍。
群里的话题热度另说：这里只管"他被她烦到了没有"。
"""

from __future__ import annotations

import difflib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory import keywords

log = logging.getLogger(__name__)

LEVEL_NAMES = {0: "正常", 1: "轻烦", 2: "烦躁", 3: "冷战"}

# 怒气分档的阈值，改这里就是在改"多容易生气"
# 30 起步：同一件事问第三遍才轻烦（第二遍只记下，不甩脸）
THRESHOLDS = (30.0, 55.0, 85.0)
MAX_ANGER = 100.0

# 每个用户最多记几个话题，太多了没用，还会让文件越来越肥
MAX_TOPICS = 8
# 最多记多少个用户（群里人多，别无限长）
MAX_USERS = 500

# 这些词太泛，光靠它们会把所有问句都算成"同一件事"
_WEAK = {"什么", "怎么", "为什么", "是不是", "可以", "现在", "今天", "这个", "那个", "一个"}

# 正经事：人设里「但真事会收」优先级最高，这时候气必须让路
_URGENT = re.compile(
    r"(救命|出事了|不舒服|难受|疼|发烧|急诊|医院|报警|崩溃|撑不住|想不开|自杀|"
    r"急死|怎么办啊|帮帮我)"
)

# 认真哄：认错 / 说清原因 / 承诺不再问
_SINCERE = re.compile(
    r"(对不起|抱歉|我错了|是我不对|我不好|我不该|不该问|不该这么问|"
    r"知道错了|我嘴欠|原谅我|真的不问了|不问了|下次不|以后不)"
)
# 敷衍的哄：只有一句「别生气了」
_TOKEN = re.compile(r"(别生气|不要生气|不生气|别气|消消气|不气不气|哄哄|乖|摸摸|抱抱|亲亲|白饭)")
_REASON = re.compile(r"(因为|刚才|刚才那|是我|知道|明白|理解|忙|累|急|忘了|没注意|下次|以后)")

# 冷战档的台词。全是短的、不带「～」的 —— 生气的人不拖音。
_LINES_OWNER = (
    "说过了",
    "本鱼不想说了",
    "……",
    "哼",
    "不说了",
    "这个刚说过哦",
)
_LINES_GROUP = (
    "……",
    "这个刚说过",
    "诶……",
    "不说了",
)

# 纯占位消息（只发了一张图/一个表情）不是「又问了一遍」：
# 这种消息在正文里只剩 `[图片]` / `[表情]` / `[表情包]`，以前会被当成同一个
# 话题反复累加，群里连发几张图就变成「这件事第 2 遍/第 3 遍」。
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情包?|动画表情|语音|视频|文件|合并转发|B站视频|分享|卡片|链接|音乐|位置)"
    r"[：:]?[^\]]*\]"
)


def _is_placeholder_only(text: str) -> bool:
    """这句话是不是只剩占位符（外加标点/空白），没有任何真内容。"""
    body = _PLACEHOLDER_RE.sub("", str(text or ""))
    return not body.strip(" \t\r\n，,。.！!？?~～、；;：:")


@dataclass
class Signal:
    """这一条消息的情绪判定结果。app 只读它，不改状态。"""

    level: int = 0
    anger: float = 0.0
    hits: int = 1
    repeated: bool = False
    soothe: str | None = None  # "sincere" / "token" / None
    refuse: bool = False       # 这一轮要不要硬拒（不调模型）
    line: str | None = None    # 硬拒时发的话；None = 真的不发
    sticker: str | None = None
    topic: str = ""

    @property
    def name(self) -> str:
        return LEVEL_NAMES.get(self.level, "正常")


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "")


def _same_topic(a_kw: list[str], a_text: str, b_kw: list[str], b_text: str) -> bool:
    """两句问的是不是同一件事。宁可漏判，也别把不同的问题算成一件事。"""
    # 纯占位消息没有话题，不能跟任何东西算同一件
    if _is_placeholder_only(a_text) or _is_placeholder_only(b_text):
        return False
    a = set(a_kw) - _WEAK
    b = set(b_kw) - _WEAK
    if a and b:
        overlap = len(a & b) / min(len(a), len(b))
        if overlap >= 0.6:
            return True
    na, nb = _norm(a_text), _norm(b_text)
    if not na or not nb:
        return False
    # 短问句：包含就算同一件事（「你是谁」vs「你是谁啊」）
    if min(len(na), len(nb)) <= 4:
        return na == nb or (min(len(na), len(nb)) >= 3 and (na in nb or nb in na))
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def classify_soothe(text: str) -> str | None:
    """这句是在哄她，还是在敷衍她？

    `sincere` 才会真的消气；一句光秃秃的「别生气了」不算 —— 那是让他闭嘴。
    """
    body = (text or "").strip()
    if not body:
        return None
    if _SINCERE.search(body):
        return "sincere"
    if _TOKEN.search(body):
        # 「别生气了，我刚才不该一直问」这种，光有口令没有理由，还是敷衍
        if len(body) >= 12 and _REASON.search(body):
            return "sincere"
        return "token"
    return None


class Mood:
    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        repeat_delta: float = 22.0,
        trigger_delta: float = 32.0,
        decay_per_min: float = 2.5,
        window_minutes: int = 15,
        trigger_words: tuple[str, ...] | list[str] = (),
        silence_chance: float = 0.5,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.repeat_delta = repeat_delta
        self.trigger_delta = trigger_delta
        self.decay_per_min = decay_per_min
        self.window_seconds = max(1, window_minutes) * 60
        self.trigger_words = tuple(w for w in trigger_words if w)
        self.silence_chance = max(0.0, min(1.0, silence_chance))
        self._data: dict[str, Any] = {"users": {}}
        self._load()

    # ---------- 落盘 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
            return
        users: dict[str, Any] = {}
        removed = 0
        for uid, user in raw["users"].items():
            if not isinstance(user, dict):
                continue
            topics: list[dict[str, Any]] = []
            for topic in user.get("topics") or []:
                if not isinstance(topic, dict):
                    continue
                # 老数据里可能有以前把纯图/纯表情记成的话题，加载时直接清掉，
                # 不然它还会以「第 N 遍」的身份蹦出来。
                if _is_placeholder_only(str(topic.get("text") or "")):
                    removed += 1
                    continue
                topics.append(topic)
            user["topics"] = topics
            users[str(uid)] = user
        self._data = {"users": users}
        if removed:
            log.info("情绪状态：清掉 %d 条纯图片/纯表情占位话题", removed)

    def _save(self) -> None:
        # 不节流：文件就几 KB，一条消息写一次远比丢状态划算 ——
        # 刚被问烦就重启的话，"她还在生气"这件事得留得住。
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:  # 写不进去也不该拖垮聊天
            log.warning("情绪状态写不进去：%s", exc)

    # ---------- 内部：怒气衰减与分档 ----------

    def _decayed(self, topic: dict[str, Any], now: float) -> float:
        anger = float(topic.get("anger") or 0.0)
        minutes = max(0.0, now - float(topic.get("ts") or now)) / 60.0
        return max(0.0, anger - self.decay_per_min * minutes)

    @staticmethod
    def _level(anger: float) -> int:
        if anger >= THRESHOLDS[2]:
            return 3
        if anger >= THRESHOLDS[1]:
            return 2
        if anger >= THRESHOLDS[0]:
            return 1
        return 0

    def _topics(self, uid: Any, create: bool = False) -> list[dict[str, Any]]:
        users = self._data["users"]
        key = str(uid)
        if key not in users:
            if not create:
                return []
            # 群里人多，别让状态文件无限长：超了就丢最久没说话的那个
            if len(users) >= MAX_USERS:
                oldest = min(
                    users,
                    key=lambda k: max(
                        (float(t.get("ts") or 0) for t in users[k].get("topics") or []),
                        default=0.0,
                    ),
                )
                users.pop(oldest, None)
            users[key] = {"topics": [], "refused": 0, "soothed": 0, "seen": 0}
        return users[key].setdefault("topics", [])

    def _hottest(self, uid: Any, now: float) -> dict[str, Any] | None:
        topics = self._topics(uid)
        if not topics:
            return None
        alive = [t for t in topics if self._decayed(t, now) > 0.5]
        if not alive:
            return None
        return max(alive, key=lambda t: self._decayed(t, now))

    # ---------- 对外：看状态 ----------

    def level_of(self, uid: Any, now: float | None = None) -> int:
        if not self.enabled:
            return 0
        now = now or time.time()
        topic = self._hottest(uid, now)
        return self._level(self._decayed(topic, now)) if topic else 0

    def prompt(self, uid: Any, now: float | None = None, text: str = "") -> str:
        """给 `_dynamic_context` 的一行。只读，不改状态。

        这里绝对不能进 system prompt：那个前缀必须字节稳定，否则缓存全废。

        `text` 是这一轮实际说的话。**只有这句话还在那个气头上时才提示**：
        以前只按 uid 取最烫的话题，他明明换了个新问题，提示里还写着
        「这件事他已经问第 N 遍了」，模型就会把旧账算到新问题上，
        对着第一次问的人说「你问过好几遍了」。这是群里「记混谁说的/谁问的」
        的一大来源，所以这里必须再比一次话题。
        """
        if not self.enabled:
            return ""
        # 这一轮只是贴了张图/表情：别把上一个话题的怒气扣到这上面，
        # 也别让模型以为「这件事他又问了一遍」。
        if text and _is_placeholder_only(text):
            return ""
        now = now or time.time()
        topic = self._hottest(uid, now)
        if topic is None:
            return ""
        # 换了话题 / 只是一句无关的话：别把上一个话题的怒气扣到这一句头上。
        # text 为空表示调用方只想要状态（指令回执、测试），沿用旧行为。
        if text and not _same_topic(
            topic.get("kw") or [], topic.get("text") or "", keywords(text), text
        ):
            return ""
        anger = self._decayed(topic, now)
        level = self._level(anger)
        hits = int(topic.get("hits") or 1)
        # 刚被哄过：语气要松一点，但别一秒回春 —— 那不像人
        user = (self._data.get("users") or {}).get(str(uid)) or {}
        just_soothed = now - float(user.get("soothe_ts") or 0) < 180
        if just_soothed:
            return (
                "（他刚哄过你，气消了一点。别马上又黏上去，嘴硬一句、或者「……那好吧」就够；"
                "也别再翻刚才那件事）"
            )
        if level == 1:
            return (
                f"（这件事他已经问第 {hits} 遍了，你有点无奈。"
                "别把刚才的解释再说一遍，但该给判断还是给，别用一句「不知道」打发）"
            )
        if level == 2:
            return (
                f"（这件事他已经问第 {hits} 遍了，你烦了：句子要短、别展开、别哄他，"
                "可以装没听见，或者反问一句「你到底想问哪个嘛」。"
                "要是还想贴表情，这时候贴 [表情:生气] 才对）"
            )
        return (
            "（你还为这件事生气，刚才已经硬拒过一次了。别主动示好；"
            "他要是认真道歉，你可以松一点，但别立刻热络，嘴硬一句也行）"
        )

    # ---------- 对外：吃一条消息 ----------

    def observe(
        self,
        uid: Any,
        text: str,
        *,
        is_group: bool = False,
        is_owner: bool = False,
        now: float | None = None,
    ) -> Signal:
        """判这一条：是不是又在问同一件事、烦到哪一档、要不要硬拒。"""
        if not self.enabled:
            return Signal()
        now = now or time.time()
        body = (text or "").strip()[:120]
        if not body or _is_placeholder_only(body):
            # 纯图片/纯表情不是「又问了一遍」：不建话题、不累加次数。
            return Signal(level=self.level_of(uid, now))

        # 真事优先：人设里「但真事会收」压过所有生气，直接清零
        if _URGENT.search(body):
            changed = self.clear(uid)
            if changed:
                log.info("【生气】碰到正经事，气先消了：%s", body[:30])
            return Signal(topic=body[:20])

        soothe = classify_soothe(body)
        topics = self._topics(uid, create=True)
        user = self._data["users"][str(uid)]
        user["seen"] = int(user.get("seen") or 0) + 1

        # 找同一个话题（只在时间窗内认，上周问的不算）
        kws = keywords(body)
        matched: dict[str, Any] | None = None
        best_score = 0.0
        for topic in topics:
            if now - float(topic.get("ts") or 0) > self.window_seconds:
                continue
            if _same_topic(topic.get("kw") or [], topic.get("text") or "", kws, body):
                score = self._decayed(topic, now)
                if matched is None or score > best_score:
                    matched, best_score = topic, score

        if matched is None and soothe:
            # 纯哄、没提那件事（「对不起，我刚才不该一直问」）：
            # 文字上认不出是哪个话题，就消在**最气的那件**上 —— 他哄的多半就是它。
            # 这句也不能算成新话题，否则"同一件事"的次数会越哄越多。
            matched = self._hottest(uid, now)
            if matched is None:
                return Signal(topic=body[:20])
        elif matched is None:
            # 新话题：第一遍不生气的，先记下来
            matched = {"text": body, "kw": kws, "anger": 0.0, "hits": 1, "ts": now}
            topics.append(matched)
            # 只留最近/最烫的几个，别让文件无限长
            if len(topics) > MAX_TOPICS:
                topics.sort(key=lambda t: float(t.get("ts") or 0))
                del topics[: len(topics) - MAX_TOPICS]
        else:
            anger = self._decayed(matched, now)
            matched["hits"] = int(matched.get("hits") or 1) + 1
            matched["anger"] = min(MAX_ANGER, anger + self.repeat_delta)
            matched["text"] = body
            matched["kw"] = kws
            lowered = body.lower()
            if matched["hits"] == 2 and any(w.lower() in lowered for w in self.trigger_words):
                # 雷点话题（AI、掉线这些）：第二遍就额外加一截，
                # 只加这一次 —— 否则第 3 遍直接冷战，太快了
                matched["anger"] = min(MAX_ANGER, matched["anger"] + self.trigger_delta)

        # 哄：先判这句是不是在哄，再决定消多少
        if soothe == "sincere":
            matched["anger"] = max(0.0, float(matched.get("anger") or 0.0) - 40.0)
            user["soothed"] = int(user.get("soothed") or 0) + 1
            user["soothe_ts"] = now
        elif soothe == "token":
            level_before = self._level(float(matched.get("anger") or 0.0))
            # 敷衍的「别生气了」在冷战期基本没用
            cut = {0: 20.0, 1: 20.0, 2: 8.0, 3: 0.0}.get(level_before, 0.0)
            matched["anger"] = max(0.0, float(matched.get("anger") or 0.0) - cut)

        matched["ts"] = now
        anger = float(matched.get("anger") or 0.0)
        level = self._level(anger)
        hits = int(matched.get("hits") or 1)
        repeated = hits > 1

        if level > 0:
            log.info(
                "【生气】%s 同一件事第 %d 遍：怒气 %.0f（%s）%s",
                uid,
                hits,
                anger,
                LEVEL_NAMES[level],
                "（在哄）" if soothe else "",
            )

        # 冷战 + 他又在问同一件事 → 硬拒
        refuse = (
            level >= 3
            and hits > 1
            and soothe is None
            and now - float(user.get("last_refuse") or 0) >= 60
        )
        line = None
        sticker = None
        if refuse:
            user["last_refuse"] = now
            user["refused"] = int(user.get("refused") or 0) + 1
            sticker = "生气"
            if random.random() >= self.silence_chance:
                pool = _LINES_OWNER if is_owner and not is_group else _LINES_GROUP
                line = random.choice(pool)

        self._save()
        return Signal(
            level=level,
            anger=anger,
            hits=hits,
            repeated=bool(repeated),
            soothe=soothe,
            refuse=refuse,
            line=line,
            sticker=sticker,
            topic=str(matched.get("text") or "")[:20],
        )

    # ---------- 对外：主人手动消气 ----------

    def clear(self, uid: Any | None = None) -> int:
        """把气消掉（不带 uid = 全清）。返回原本还在生气的用户数。"""
        users = self._data.get("users") or {}
        changed = 0
        now = time.time()
        for key, user in list(users.items()):
            if uid is not None and key != str(uid):
                continue
            topics = user.get("topics") or []
            if any(self._level(self._decayed(t, now)) > 0 for t in topics):
                changed += 1
            user["topics"] = []
        self._save()
        return changed

    def stats(self) -> dict[str, Any]:
        """给指令回执 / 日志看的状态。"""
        now = time.time()
        users = self._data.get("users") or {}
        hot = []
        for uid, user in users.items():
            topics = user.get("topics") or []
            if not topics:
                continue
            top = max(topics, key=lambda t: self._decayed(t, now))
            anger = self._decayed(top, now)
            level = self._level(anger)
            if level > 0:
                hot.append(
                    {
                        "uid": uid,
                        "level": level,
                        "name": LEVEL_NAMES[level],
                        "anger": round(anger, 1),
                        "hits": int(top.get("hits") or 1),
                        "topic": str(top.get("text") or "")[:20],
                        # 这两个是给人看的：今天被拒了几次、被哄好过几次
                        "refused": int(user.get("refused") or 0),
                        "soothed": int(user.get("soothed") or 0),
                    }
                )
        hot.sort(key=lambda x: -x["anger"])
        return {"users": len(users), "hot": hot}
