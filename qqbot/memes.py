"""群梗表：这个群里反复复读的短串，按群存，命中才注入。

和热梗库（`trends.py`）分工：

- `trends`：**外面**的热点（贴吧/B站），每天抓一次；
- `memes`：**这个群自己的**梗，从群日志里统计出来，0 token、不联网。

两类证据：

- **复读型**：A 说完几十秒内，另一个人接着发同一串（实测「区 → 区」38 次、30 人）。
  这类自动收：跨天还在用 = 长期；只在一场里爆火 = 当场（24 小时衰减）。
- **语境型**：单串高频但没有复读结构（「饿啊」是追着击杀喊的）。统计只出候选，
  场景由主人标一次，存在 `scene` 里。

几条边界：

- 只记录**说法 + 场景 + 样例**，不让她主动解释梗；`brief()` 里明确要求别解释；
- 带骂人/黄腔/刷屏性质的（含谐音代称）标成「只认不用」：知道意思，不主动喊、
  不对真人用、不跟着复读；`risk_of()` 给复读等不经过模型的路径做本地硬门；
- 功能指令（打卡/积分/#sl）和已标记机器人/AI 的发言一律不算；
- **跨群都在用的会升级成「普遍说法」**（`global`）：不再是某个群的黑话，
  所有会话（含私聊）都能用；
- 梗按群隔离，跨群不串味；
- **联网验证只验「群共识站住」的**：跨天、多人、多次复读都够了才花搜索费；
  单日爆火和刚冒头的串先当临时用法，不验；
- **网络梗靠人**：没见过的短词先问群友，群友确认是梗后再搜一次，
  归一化去重后进 `global`（所有会话都能用，留一年）；
- 统计失败只记日志，不影响聊天（跟 `trends` 一样）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import meme_schema, memetone, memevariant

log = logging.getLogger(__name__)

PUNCT_RE = re.compile(r"[\s\u3000，。！？、；：\"'“”‘’《》（）()【】\[\]…—\-~·!?,.;:]+")
MAX_LEN = 12
# 别的机器人的指令 / 群功能词：多个人重复是因为大家都在用功能，不是梗
FUNCTION_WORDS = (
    "打卡", "积分", "签到", "运势", "面板", "数据", "查询", "抽卡", "刷新",
    "记录", "词条", "帮助", "菜单", "报名", "接龙", "投票", "公告", "排行",
    "库存", "价格", "教程", "攻略", "战绩", "绑定", "抽奖", "答题", "提问",
    "今日", "我的", "查看", "获取", "领取", "兑换", "指令",
)
COMMAND_PREFIX = ("#", "/", "ww", "@", "！", "!", "。", "・")


def norm(text: str) -> str:
    return PUNCT_RE.sub("", (text or "")).lower()


def one_line(text: Any, limit: int) -> str:
    """压成一行：网梗解释/群友原话可能带换行，注入时不能把上下文撑断。"""
    return " ".join(str(text or "").split())[: max(0, int(limit))]


def clean(text: str) -> str:
    body = norm(text)
    return body if 1 <= len(body) <= MAX_LEN else ""


def is_command(text: str) -> bool:
    if text.startswith(COMMAND_PREFIX):
        return True
    return any(word in text for word in FUNCTION_WORDS)


_SINGLE_RE = r"(?<![\u4e00-\u9fffA-Za-z0-9]){}(?![\u4e00-\u9fffA-Za-z0-9])"

# 群里有人解释梗：「X 是 Y 的梗」「X 出自 Y」
_EXPLAIN_RE = re.compile(
    r"([^\s，。！？,.!?]{1,10})(?:是|来自|出自)([^\s，。！？,.!?]{1,12})的(?:梗|黑话)"
)


def _mentions(key: str, body: str) -> bool:
    """正文里有没有提到这个梗。

    单字梗（「区」）必须**单独成词**才算：否则「小区」「区域」全会被当成梗。
    多字直接按子串认。
    """
    if len(key) != 1:
        return key in body
    return bool(re.search(_SINGLE_RE.format(re.escape(key)), body))


# 谐音代称：不是「梗/玩笑」，是本群把原词固定说成另一个词。
# 例：「‘蛆’的谐音」「区是蛆的谐音」「谐音：蛆」。
_MEANS_BEFORE_RE = re.compile(
    r"[“\"'「『]?([^“”\"'「」『』\s，。！？,.!?]{1,12})[”\"'」』]?\s*的\s*谐音"
)
_MEANS_EXPLAIN_RE = re.compile(
    r"([^\s，。！？,.!?]{1,12})(?:是|就是)([^\s，。！？,.!?]{1,12})的谐音"
)
_MEANS_AFTER_RE = re.compile(
    r"谐音(?:是|为|：|:)\s*[“\"'「『]?([^“”\"'「」『』\s，。！？,.!?]{1,12})"
)
_MEANS_PREFIX = re.compile(
    r"^(?:就是|这是|那是|所谓|也就是|叫做|叫|把|用|说成|写成|读作|谐音是|谐音为|是)+"
)
_MEANS_STOP = {
    "这个", "那个", "原词", "本词", "意思", "一种", "说法", "什么", "谐音",
    "代称", "原话", "原字", "本字",
}
# 代称的「什么时候用」里，这些词太泛，不能当触发词
_SCENE_STOP = {
    "提到", "贬称", "时候", "这个", "那个", "一种", "说法", "意思", "用法",
    "场景", "说人", "打得", "不是", "就是", "可以", "群里", "大家", "平时",
    "用来", "表示", "称呼", "叫法", "流行", "谐音", "代称", "比如", "相关",
}


def parse_means(text: Any, key: str = "") -> str:
    """从解释里抠出「这个代称代替的原词」。

    只认带「谐音」的句子；抠不出来返回空串。`key` 是代称本身，
    如果抠出来的原词和代称一样（「区的谐音」）就丢掉。
    """
    body = str(text or "")
    if "谐音" not in body:
        return ""
    key = norm(key)
    candidates: list[str] = []
    match = _MEANS_EXPLAIN_RE.search(body)
    if match:
        candidates.append(match.group(2))
    for match in _MEANS_BEFORE_RE.finditer(body):
        candidates.append(match.group(1))
    for match in _MEANS_AFTER_RE.finditer(body):
        candidates.append(match.group(1))
    for raw in candidates:
        cand = norm(str(raw or "").strip().strip("“”\"'「」『』"))
        cand = _MEANS_PREFIX.sub("", cand).strip()
        if cand.endswith("的"):
            cand = cand[:-1]
        if (
            cand
            and cand != key
            and 1 <= len(cand) <= MAX_LEN
            and cand not in _MEANS_STOP
        ):
            return cand
    return ""


def _substitution_means(item: dict[str, Any]) -> str:
    """这条梗是不是谐音代称；是就返回它代替的原词。"""
    means = norm(str(item.get("means") or ""))
    if means:
        return means
    key = norm(str(item.get("text") or ""))
    for body in (item.get("scene"), item.get("verify_note"), item.get("example")):
        means = parse_means(body, key)
        if means:
            return means
    return ""


def _trigger_words(item: dict[str, Any]) -> list[str]:
    """代称的「什么时候用」里，能当触发话题的实词。

    只从 example / scene 里取 2 字以上的词，去掉泛词和代称/原词本身。
    例：「区」的 example「克里斯是区」→ 触发词「克里斯」：聊到克里斯就可以用「区」。
    """
    if not _is_substitution(item):
        return []
    key = str(item.get("text") or "")
    means = _substitution_means(item)
    body = " ".join(
        str(item.get(key) or "") for key in ("example", "scene")
    ).strip()
    if not body:
        return []
    try:
        import jieba

        words = jieba.lcut(body)
    except Exception:  # noqa: BLE001 - 分词不是硬依赖
        return []
    out: list[str] = []
    for word in words:
        word = word.strip()
        if len(word) < 2 or word in (key, means) or word in _SCENE_STOP:
            continue
        if word not in out:
            out.append(word)
    return out[:5]


def _is_substitution(item: dict[str, Any]) -> bool:
    return item.get("kind") in {"substitution", "homophone"} or bool(
        _substitution_means(item)
    )


def _mentions_loose(key: str, body: str) -> bool:
    """代称按「词」认，不按严格边界。

    「克里斯是区」「太区了」里「区」贴着别的汉字，用严格单字边界会漏；
    但又不能把「小区」「区域」算进来，所以用 jieba 分词后按 token 比。
    没装 jieba 时退回子串匹配（宁可多认，也别漏掉本群默认叫法）。
    """
    if not key or key not in body:
        return False
    if len(key) != 1:
        return True
    try:
        import jieba

        return key in jieba.lcut(body)
    except Exception:  # noqa: BLE001 - 分词不是硬依赖
        return _mentions(key, body)


def _alias_hit(item: dict[str, Any], body: str, spell: Any = None) -> bool:
    """消息里的缩写/数字谐音，是不是这条梗的另一种写法。

    `f8fq` 的骨架 `fbfq` == 「飞八分钱」的骨架；主人标过的写法（「鱼八分钱」）
    走 `spell` 的显式链接。只认骨架相等，不做模糊匹配 —— 认错比漏认更麻烦。
    """
    codes = memevariant.codes_in(body)
    text = str(item.get("text") or "")
    if not codes or not text:
        return False
    if text.isdigit():
        # 「666」这种纯数字梗只按原样认：它的骨架是 lll，会误吃随手的字母串
        return False
    wanted = {memevariant.skeleton(text)}
    for alias in item.get("alias") or []:
        wanted.add(memevariant.skeleton(str(alias)))
    wanted.discard("")
    if not wanted:
        return False
    for code in codes:
        if memevariant.skeleton(code) in wanted:
            return True
        if spell is not None:
            target = spell.resolve(code)
            if target and memevariant.skeleton(target) in wanted:
                return True
    return False


def _matches(item: dict[str, Any], body: str, spell: Any = None) -> bool:
    """消息里提到了代称本身、它代替的原词，或者它的使用场景，都算命中。"""
    key = str(item.get("text") or "")
    sub = _is_substitution(item)
    if key and (_mentions_loose(key, body) if sub else _mentions(key, body)):
        return True
    means = _substitution_means(item)
    if means and (_mentions_loose(means, body) if sub else _mentions(means, body)):
        return True
    if _alias_hit(item, body, spell):
        return True
    if not sub:
        return False
    return any(word in body for word in _trigger_words(item))


# 固定接话里不能收的：这些是「谁都可能接一句」的通用应答，不是固定句式
GENERIC_REPLY = {
    "对", "嗯", "哦", "好", "是", "可以", "确实", "真的", "行", "没有", "不",
    "啊", "唉", "哈哈哈", "哈哈", "笑死", "草", "6", "666", "牛逼", "牛",
    "神了", "我去", "卧槽", "绝了", "离谱", "好家伙", "笑死我了", "太强了",
    "🤣", "😭", "😡", "😅", "😂",
}
# 像「触发词」的说法：问句 / 呼语。只有这种才值得单独收成一条梗
_TRIGGER_HINT = re.compile(r"[不没吗呢吧啥]|什么|怎么|谁|哪|来点|来个|有人")


def looks_like_trigger(text: str) -> bool:
    return bool(_TRIGGER_HINT.search(text or ""))


def parse_verify(answer: str) -> tuple[bool | None, str]:
    """解析梗验证的一行结论：是｜解释 / 不是 / 不确定。"""
    body = str(answer or "").strip()
    if not body:
        return None, ""
    line = body.splitlines()[0].strip()
    if line.startswith("不是"):
        return False, ""
    if line.startswith("不确定"):
        return None, ""
    if line.startswith("是"):
        note = line[1:].lstrip("｜|:： ").strip()
        return True, note[:60]
    return None, ""


def _replies_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """取一条梗的接法，统一成 [{text, n}]，次数多的在前。"""
    raw = item.get("reply") or []
    out: list[dict[str, Any]] = []
    for reply in raw:
        if isinstance(reply, dict):
            text = str(reply.get("text") or "").strip()
            n = int(reply.get("n") or 0)
        else:
            text = str(reply or "").strip()
            n = 0
        if text:
            out.append({"text": text, "n": n})
    out.sort(key=lambda r: -int(r["n"]))
    return out


def _parts_of(item: dict[str, Any]) -> list[str]:
    """这条梗由哪几个梗拼出来（延伸/融合）；没有就是空列表。

    「鱼八分钱」= 飞八分钱 + 大肥鱼；「菲巴番茄」= 菲比啾比 + 飞八分钱。
    只记原料名字，意思/风险在用到时再顺着原料查（见 `MemeBook._risk_of_item`）。
    """
    raw = item.get("parts") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for part in raw:
        text = str(part or "").strip()
        if text and text not in out:
            out.append(text[:MAX_LEN])
    return out[:5]


def _risk_of_item(item: dict[str, Any]) -> str:
    """这条梗自己的场景/接法有没有风险；**不含**它拼的那些原料。

    原料的风险由 `MemeBook._risk_of_item` 顺着 `parts` 查。
    """
    replies = [str(reply.get("text") or "") for reply in _replies_of(item)]
    return memetone.risk(
        item.get("text"),
        item.get("scene"),
        item.get("example"),
        item.get("means"),
        *replies,
    )


def _merge_replies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨群合并时把接法按次数加起来。"""
    merged: dict[str, int] = {}
    for item in items:
        for reply in _replies_of(item):
            merged[reply["text"]] = merged.get(reply["text"], 0) + int(reply["n"])
    ranked = sorted(merged.items(), key=lambda kv: -kv[1])
    return [{"text": text, "n": n} for text, n in ranked[:3]]


def _merge_entries(
    key: str,
    items: list[dict[str, Any]],
    tier_of: Any = None,
) -> dict[str, Any]:
    """把几个群里的同一条梗并成一条「普遍说法」。"""
    now = time.time()
    hits = sum(int(i.get("hits") or 0) for i in items)
    speakers = sum(int(i.get("speakers") or 0) for i in items)
    days = max((int(i.get("days") or 0) for i in items), default=0)
    first = min((float(i.get("first_ts") or 0) for i in items), default=now)
    last = max((float(i.get("last_ts") or 0) for i in items), default=now)
    rank = {"short": 0, "local": 1, "long": 2, "manual": 3}
    tiers = [
        (tier_of(i) if callable(tier_of) else str(i.get("state") or "short"))
        for i in items
    ]
    state = max(tiers, key=lambda t: rank.get(t, 0)) if tiers else "short"
    source = "auto"
    if state == "manual":
        source = "owner"
    scene = next((str(i.get("scene") or "") for i in items if i.get("scene")), "")
    example = next((str(i.get("example") or "") for i in items if i.get("example")), "")
    verified = next(
        (i.get("verified") for i in items if i.get("verified") is not None), None
    )
    note = next((str(i.get("verify_note") or "") for i in items if i.get("verify_note")), "")
    skip = any(bool(i.get("verify_skip")) for i in items)
    tries = max((int(i.get("verify_tries") or 0) for i in items), default=0)
    merged = {
        "text": key,
        "scene": scene,
        "example": example,
        "reply": _merge_replies(items),
        "verified": verified,
        "verify_note": note,
        "state": state,
        "hits": hits,
        "speakers": speakers,
        "days": days,
        "first_ts": first,
        "last_ts": last,
        "source": source,
        "groups": len(items),
    }
    means = next(
        (str(i.get("means") or "") for i in items if i.get("means")), ""
    ) or next((_substitution_means(i) for i in items if _substitution_means(i)), "")
    if means:
        merged["means"] = means
        merged["kind"] = "substitution"
    parts = next((_parts_of(i) for i in items if _parts_of(i)), [])
    if parts:
        merged["parts"] = parts
    elif any(i.get("kind") in {"substitution", "homophone"} for i in items):
        merged["kind"] = "substitution"
    if skip:
        merged["verify_skip"] = True
    if tries:
        merged["verify_tries"] = tries
    return merged


class MemeBook:
    """按群存的梗表。统计、保鲜、检索全在本地，不调模型。"""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        keep_days: int = 30,
        max_per_group: int = 60,
        repeat_window: int = 30,
        min_hits: int = 3,
        min_speakers: int = 3,
        min_days: int = 2,
        short_ttl_hours: int = 24,
        max_chars: int = 360,
        verify_min_speakers: int = 5,
        verify_min_hits: int = 8,
        verify_min_days: int = 3,
        web_keep_days: int = 365,
        spell: memevariant.SpellBook | None = None,
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.keep_days = max(1, int(keep_days))
        self.max_per_group = max(5, int(max_per_group))
        self.repeat_window = max(1, int(repeat_window))
        self.min_hits = max(1, int(min_hits))
        self.min_speakers = max(1, int(min_speakers))
        self.min_days = max(1, int(min_days))
        # 固定接话的门槛：至少两个人这么接过，才算「固定句式」
        self.min_reply_hits = max(2, int(min_hits) - 1)
        self.min_reply_speakers = max(2, int(min_speakers) - 1)
        self.short_ttl = max(3600, int(short_ttl_hours) * 3600)
        self.max_chars = max(120, int(max_chars))
        # 联网验证的门槛：群共识先站住（跨天 / 多人 / 多次），才值得花一次搜索
        self.verify_min_speakers = max(3, int(verify_min_speakers))
        self.verify_min_hits = max(3, int(verify_min_hits))
        self.verify_min_days = max(2, int(verify_min_days))
        # 网络梗（群友确认 + 网上查到的）不按 30 天保鲜：它是本地小百科，留一年
        self.web_ttl = max(1, int(web_keep_days)) * 86400
        # 写法归一：骨架（f8fq → fbfq）+ 显式链接（鱼八分钱 → 飞八分钱）
        self.spell = spell
        # scan 时用来「这个缩写是不是别的词条」的外查询（app 接到梗百科上）
        self.code_resolver: Any = None
        # 「这个词是不是已知词条」：自动学写法时用（同样由 app 接）
        self.term_known: Any = None
        # 用法画像（qqbot/usagebook.py）：手写的 scene 空着时用它顶上。
        # 由 app 接；接不上就退化成「只说有这个说法」。
        self.usage: Any = None
        # 「这个词的风险是什么」：延伸/融合的原料不在梗表里时（只在梗百科）由 app 接
        self.term_risk: Any = None
        self._store = self._load()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """老条目补 meaning/usage/origin/examples/tags/status，不丢 scene。"""
        changed = False
        for book in [
            *(self._store.get("groups") or {}).values(),
            self._store.get("global") or {},
        ]:
            if not isinstance(book, dict):
                continue
            for key, item in book.items():
                if not isinstance(item, dict):
                    continue
                new = meme_schema.from_legacy(item)
                if new != item:
                    item.clear()
                    item.update(new)
                    changed = True
        if changed:
            self._save()

    # ---------- 持久化 ----------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": 0.0,
            "last_scan_day": "",
            "groups": {},
            "global": {},
            # 验过「不是梗」的串记在这里，下次统计别再收回来
            "rejected": {},
            # 联网验证的每日预算：跨天自动重置
            "verify_day": "",
            "verify_count": 0,
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            base = self._empty()
            base.update({k: v for k, v in data.items() if k in base})
            if not isinstance(base.get("groups"), dict):
                base["groups"] = {}
            if not isinstance(base.get("global"), dict):
                base["global"] = {}
            if not isinstance(base.get("rejected"), dict):
                base["rejected"] = {}
            return base
        except (OSError, ValueError, TypeError) as exc:
            log.warning("梗表读取失败，重建空表：%s", exc)
            try:
                self.path.replace(
                    self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
                )
            except OSError:
                pass
            return self._empty()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._store, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("梗表写入失败：%s", exc)

    # ---------- 统计 ----------

    def _repeats(
        self, rows: list[dict], exclude: set[str]
    ) -> dict[str, dict[str, Any]]:
        """A 发完，几十秒内另一个人接的还是 A。返回 {串: 证据}。"""
        buckets: dict[str, dict[str, Any]] = {}
        n = len(rows)
        for i, row in enumerate(rows):
            a = clean(row.get("text", ""))
            if not a or is_command(a):
                continue
            uid = str(row.get("uid") or "")
            if uid in exclude:
                continue
            for j in range(i + 1, n):
                nxt = rows[j]
                if int(nxt.get("ts") or 0) - int(row.get("ts") or 0) > self.repeat_window:
                    break
                nxt_uid = str(nxt.get("uid") or "")
                if nxt_uid == uid or nxt_uid in exclude:
                    continue
                if clean(nxt.get("text", "")) != a:
                    continue
                item = buckets.setdefault(
                    a,
                    {
                        "hits": 0,
                        "uids": set(),
                        "days": set(),
                        "sample": "",
                        "first_ts": float(row.get("ts") or 0),
                        "last_ts": 0.0,
                    },
                )
                item["hits"] += 1
                item["uids"].add(nxt_uid)
                item["days"].add(int(nxt.get("ts") or 0) // 86400)
                # 记真实的首次/最后一次，别用"扫描时间"糊过去：
                # 当场梗 24 小时没再出现就该消失，靠的是这条
                item["first_ts"] = min(
                    float(item["first_ts"]), float(row.get("ts") or 0)
                )
                item["last_ts"] = max(
                    float(item["last_ts"]), float(nxt.get("ts") or 0)
                )
                if not item["sample"]:
                    item["sample"] = str(row.get("text") or "")[:30]
                break
        return buckets

    def _pairs(
        self, rows: list[dict], exclude: set[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """A 说完，几十秒内另一个人接的短句 B（B 不等于 A）。

        返回 {A: {B: {n, uids}}} —— 这就是「固定接话」的来源：
        有人喊「帅不帅」，群里习惯接「不帅」「0人在意」；喊「老的」接「包老的」。
        """
        out: dict[str, dict[str, dict[str, Any]]] = {}
        n = len(rows)
        for i, row in enumerate(rows):
            a = clean(row.get("text", ""))
            if not a or is_command(a):
                continue
            uid = str(row.get("uid") or "")
            if uid in exclude:
                continue
            for j in range(i + 1, n):
                nxt = rows[j]
                if int(nxt.get("ts") or 0) - int(row.get("ts") or 0) > self.repeat_window:
                    break
                nxt_uid = str(nxt.get("uid") or "")
                if nxt_uid == uid or nxt_uid in exclude:
                    continue
                b = clean(nxt.get("text", ""))
                if not b or b == a or is_command(b):
                    continue
                slot = out.setdefault(a, {}).setdefault(b, {"n": 0, "uids": set()})
                slot["n"] += 1
                slot["uids"].add(nxt_uid)
                break
        return out

    def _attach_replies(
        self, book: dict[str, Any], pairs: dict[str, dict[str, dict[str, Any]]], now: float
    ) -> int:
        """把稳定的固定接话挂到梗上；像触发词的，本身也可以收成一条梗。

        相邻两句未必是「固定接法」：30 秒窗口里什么话都可能挨着。所以只认：
        至少两个人这么接过、接法不是通用应答、也不是另一条独立的梗。
        """
        added = 0
        for a, bs in pairs.items():
            good = [
                (b, ev)
                for b, ev in bs.items()
                if ev["n"] >= self.min_reply_hits
                and len(ev["uids"]) >= self.min_reply_speakers
                and b not in GENERIC_REPLY
                and b not in book
            ]
            if not good:
                continue
            good.sort(key=lambda kv: (kv[1]["n"], len(kv[1]["uids"])), reverse=True)
            replies = [{"text": b, "n": ev["n"]} for b, ev in good[:3]]
            item = book.get(a)
            if item is None:
                # 不是触发词的说法不新建：否则「神了→牛逼」这类口头禅也会进来
                if a in self._store.setdefault("rejected", {}):
                    continue
                if not looks_like_trigger(a):
                    continue
                total = sum(ev["n"] for _b, ev in good)
                if total < self.min_reply_hits:
                    continue
                uids: set[str] = set()
                for _b, ev in good:
                    uids |= ev["uids"]
                book[a] = {
                    "text": a,
                    "scene": "",
                    "example": "",
                    "reply": replies,
                    "state": "long",
                    "hits": total,
                    "speakers": len(uids),
                    "days": 1,
                    "first_ts": now,
                    "last_ts": now,
                    "source": "auto",
                }
                added += 1
                continue
            # 主人手工定过的接法不覆盖
            if item.get("source") == "owner" and item.get("reply"):
                continue
            item["reply"] = replies
            item["last_ts"] = now
        return added

    def _explain_scenes(self, book: dict[str, Any], rows: list[dict]) -> int:
        """群里有人解释梗时，把出处/谐音代称补进条目。

        - 「X 是 Y 的梗」→ scene 补「出自Y」；
        - 「X 是 Y 的谐音」→ means=Y、kind=substitution，scene 补「Y的谐音」。
        只补表里已有、且还没标过的字段 —— 宁可少写，也不乱写。
        """
        count = 0
        for row in rows:
            body = str(row.get("text") or "")
            for match in _EXPLAIN_RE.finditer(body):
                key = norm(match.group(1))
                source = match.group(2).strip()
                item = book.get(key)
                if item is None or item.get("scene") or not source:
                    continue
                item["scene"] = f"出自{source}"[:80]
                count += 1
            for match in _MEANS_EXPLAIN_RE.finditer(body):
                key = norm(match.group(1))
                means = norm(match.group(2))
                item = book.get(key)
                if item is None or not means or means == key:
                    continue
                item["means"] = means
                item["kind"] = "substitution"
                if not item.get("scene"):
                    item["scene"] = f"{means}的谐音"[:80]
                count += 1
        return count

    def _apply_substitution(self, item: dict[str, Any], *notes: Any) -> bool:
        """从解释/场景里认出谐音代称，写进 means / kind。"""
        key = norm(str(item.get("text") or ""))
        if not key:
            return False
        for body in (*notes, item.get("scene"), item.get("verify_note"), item.get("example")):
            means = parse_means(body, key)
            if means:
                item["means"] = means
                item["kind"] = "substitution"
                return True
        if item.get("means"):
            item["kind"] = "substitution"
            return True
        return False

    # ---------- 写法归一（f8fq = 飞八分钱） ----------

    def _known_terms(self, book: dict[str, Any] | None = None) -> list[str]:
        """这个群 + 通用表里的词条名：骨架归一的候选。

        `book=None` 表示「所有群都看一遍」（`knows()` 用）。
        """
        sources: list[Any] = []
        if isinstance(book, dict):
            sources.append(book)
        else:
            sources.extend((self._store.get("groups") or {}).values())
        sources.append(self._store.get("global") or {})
        out: list[str] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in source:
                if key and str(key) not in out:
                    out.append(str(key))
        return out

    def _resolve_variant(self, code: str, book: dict[str, Any] | None = None) -> str:
        """这个写法是不是已知词？是就返回正词，不是返回空串。

        顺序：主人标的显式链接 → 梗表里的骨架 → 外部（梗百科）的骨架。
        骨架撞车（两个词条同一个骨架）就当认不出：认错比漏认麻烦。
        """
        if not memevariant.is_code(code):
            return ""
        if self.spell is not None:
            target = self.spell.resolve(code)
            if target:
                return target
        wanted = memevariant.skeleton(code)
        hits = [
            key
            for key in self._known_terms(book)
            if memevariant.skeleton(key) == wanted
        ]
        if len(hits) == 1:
            return hits[0]
        if not hits and callable(self.code_resolver):
            try:
                return str(self.code_resolver(code) or "")
            except Exception:  # noqa: BLE001 - 查外库失败不能拖垮统计
                log.debug("写法归一：查外部词条失败", exc_info=True)
        return ""

    def _attach_variant(self, book: dict[str, Any], key: str) -> bool:
        """`key` 是已知词的另一种写法：挂到那条上，不再新建一条梗。"""
        target = self._resolve_variant(key, book)
        if not target:
            return False
        item = book.get(target)
        if isinstance(item, dict):
            aliases = item.get("alias")
            aliases = aliases if isinstance(aliases, list) else []
            if key not in aliases and len(aliases) < 6:
                aliases.append(key)
                item["alias"] = aliases
        return True

    def _learn_variants(self, rows: list[dict], repeated: set[str]) -> int:
        """群友解释了缩写就记进写法表：「f8fq 就是飞八分钱」。本地，0 token。

        门槛故意高一点，免得把聊天里随便一句「t02是tk」当成解释：
        - 这个写法在群里真的被复读过（`repeated`，和收梗同一个门槛）；
        - 正词是已知词条，而且带汉字（纯 ASCII 的「tk」不算词条名）；
        - 只用本地判断，不联网、不问模型。
        """
        if self.spell is None or not self.spell.enabled or not repeated:
            return 0
        known = self.term_known if callable(self.term_known) else self.knows
        added = 0
        for row in rows:
            body = str(row.get("text") or "")
            if not body:
                continue
            for pattern in memevariant.EXPLAIN_PATTERNS:
                for match in pattern.finditer(body):
                    code = memevariant.clean(match.group("code"))
                    term = memevariant.strip_explain_suffix(
                        memevariant.clean_term(match.group("term"))
                    )
                    if code not in repeated or not memevariant.is_code(code):
                        continue
                    if not term or term == code or not re.search(r"[\u4e00-\u9fff]", term):
                        continue
                    if len(term) > memevariant.MAX_TERM:
                        continue
                    try:
                        if not known(term):
                            continue
                    except Exception:  # noqa: BLE001 - 判不出已知就当不知道
                        continue
                    if self.spell.link(code, term, source="auto"):
                        added += 1
        return added

    # ---------- 延伸 / 融合（一个梗由别的梗拼出来） ----------

    def _risk_of_item(self, item: dict[str, Any], depth: int = 0) -> str:
        """这条梗的风险：自己的场景/接法 + 它拼的那些原料。

        「鱼八分钱」自己看不出问题，但原料是「飞八分钱」—— 骂人梗。
        原料的风险先在本群/通用表里找，找不到再问 `term_risk`（app 接到梗百科上）。
        最多递归两层：A 拼 B、B 又拼 C，够了，别让环形依赖绕死。
        """
        risk = _risk_of_item(item)
        if risk or depth >= 2:
            return risk
        for part in _parts_of(item):
            risk = self._part_risk(part, depth + 1)
            if risk:
                return risk
        return ""

    def _part_risk(self, term: str, depth: int = 0) -> str:
        """一个原料词的风险：本地梗表里同名的先算，再问梗百科。"""
        key = norm(term)
        if not key:
            return ""
        books: list[Any] = [self._store.get("global") or {}]
        books.extend((self._store.get("groups") or {}).values())
        for book in books:
            if not isinstance(book, dict):
                continue
            item = book.get(key)
            if isinstance(item, dict):
                return self._risk_of_item(item, depth)
        if callable(self.term_risk):
            try:
                return str(self.term_risk(term) or "")
            except Exception:  # noqa: BLE001 - 查外库失败就当没风险，别拖垮聊天
                log.debug("延伸/融合：查原料风险失败：%s", term, exc_info=True)
        return ""

    def annotate_blend(
        self,
        group_id: str,
        text: str,
        parts: list[str],
        scene: str = "",
        *,
        now: float | None = None,
    ) -> bool:
        """主人标「这个梗是哪几个梗拼出来的」（延伸/融合）。

        「组成：鱼八分钱 = 飞八分钱 + 大肥鱼」→ 写进条目的 `parts`。
        拼出来的词自己不吓人（鱼八分钱），风险顺着原料算：原料是骂人梗，
        她也只认不用。表里没有就新建一条手标条目（永不过期）。
        """
        if not group_id:
            return False
        key = norm(text)
        merged: list[str] = []
        for part in parts or []:
            one = norm(str(part))
            if one and one != key and one not in merged and len(one) <= MAX_LEN:
                merged.append(one)
        if not key or not merged:
            return False
        now = time.time() if now is None else float(now)
        if str(group_id) in ("*", "global"):
            target = self._store.setdefault("global", {})
        else:
            groups = self._store.setdefault("groups", {})
            target = groups.setdefault(str(group_id), {})
        item = target.get(key)
        if not isinstance(item, dict):
            item = {
                "text": key,
                "scene": "",
                "example": key,
                "reply": [],
                "state": "manual",
                "hits": 0,
                "speakers": 0,
                "days": 0,
                "first_ts": now,
                "last_ts": now,
                "source": "owner",
            }
            target[key] = item
        if scene:
            item["scene"] = str(scene)[:80]
        item["parts"] = merged[:5]
        item["source"] = "owner"
        item["state"] = "manual"
        item["last_ts"] = now
        self._save()
        return True

    def risk_of_term(self, term: str) -> str:
        """一个词（通常是延伸/融合的原料）的风险；本地梗表 + 梗百科。只读。"""
        return self._part_risk(term)

    def scan(
        self,
        rows_by_group: dict[str, list[dict]],
        exclude: Iterable[str] = (),
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        """扫一遍群日志，刷新各群的梗。只认复读，返回每群新增/保留数量。"""
        if not self.enabled:
            return {}
        now = time.time() if now is None else float(now)
        cutoff = int(now - self.keep_days * 86400)
        exclude_set = {str(x) for x in exclude if str(x).strip()}
        report: dict[str, int] = {}
        groups: dict[str, dict[str, Any]] = self._store.setdefault("groups", {})
        rejected = self._store.setdefault("rejected", {})

        for gid, rows in rows_by_group.items():
            fresh = [r for r in rows if int(r.get("ts") or 0) >= cutoff]
            if not fresh:
                continue
            found = self._repeats(fresh, exclude_set)
            pairs = self._pairs(fresh, exclude_set)
            book = groups.setdefault(str(gid), {})
            added = 0
            for key, ev in found.items():
                if key in rejected:
                    continue
                if ev["hits"] < self.min_hits or len(ev["uids"]) < self.min_speakers:
                    continue
                # 已知词的另一种写法（f8fq = 飞八分钱）：挂上去，别另收一条
                if self._attach_variant(book, key):
                    continue
                old = book.get(key)
                if old is None:
                    book[key] = {
                        "text": key,
                        "scene": "",
                        "example": ev["sample"],
                        "state": (
                            "local"
                            if len(ev["days"]) >= self.min_days
                            else "short"
                        ),
                        "hits": ev["hits"],
                        "speakers": len(ev["uids"]),
                        "days": len(ev["days"]),
                        "first_ts": ev.get("first_ts") or now,
                        "last_ts": ev.get("last_ts") or now,
                        "source": "auto",
                    }
                    added += 1
                else:
                    old["hits"] = ev["hits"]
                    old["speakers"] = len(ev["uids"])
                    old["days"] = len(ev["days"])
                    # 真实最后一次出现，不是扫描时间：当场梗的 24 小时靠它算
                    if ev.get("last_ts"):
                        old["last_ts"] = max(
                            float(old.get("last_ts") or 0), float(ev["last_ts"])
                        )
                    if ev.get("first_ts"):
                        old["first_ts"] = min(
                            float(old.get("first_ts") or ev["first_ts"]),
                            float(ev["first_ts"]),
                        )
                    self._sync_state(old)
            # 有固定接话的，接法挂上去；接话很稳的触发词本身也算一条梗
            added += self._attach_replies(book, pairs, now)
            self._explain_scenes(book, fresh)
            # 群里有人解释缩写（「f8fq 就是飞八分钱」）：记下来，0 token
            stable = {
                key
                for key, ev in found.items()
                if ev["hits"] >= self.min_hits and len(ev["uids"]) >= self.min_speakers
            }
            self._learn_variants(fresh, stable)
            # 场景里可能早就写着「‘蛆’的谐音」：顺手把代称标出来
            for item in book.values():
                if isinstance(item, dict) and self._apply_substitution(item):
                    self._sync_state(item)
            report[str(gid)] = added

        # 保鲜对所有群都做：没有新消息的群，当场梗也得按时过期
        for gid, book in list(groups.items()):
            if not isinstance(book, dict):
                groups.pop(gid, None)
                continue
            for key in list(book):
                item = book[key]
                # 主人标的（source=owner）不参与自动过期：他标一次就该一直在，
                # 只有他自己说「这不算梗」才删。
                tier = self._tier(item)
                if tier == "manual":
                    continue
                last = float(item.get("last_ts") or 0)
                if tier == "candidate":
                    ttl = self.short_ttl * 7
                elif tier == "alias":
                    # 代称是「这个群默认这么叫」，不是玩法：留一年，别按梗的 24h 忘
                    ttl = self.web_ttl
                elif tier == "short":
                    ttl = self.short_ttl
                else:
                    ttl = self.keep_days * 86400
                if now - last > ttl:
                    book.pop(key, None)
            if not book:
                groups.pop(gid, None)
                continue
            # 封顶：主人标的优先留，其余留活得最久、证据最强的
            if len(book) > self.max_per_group:
                keep = sorted(
                    book.values(),
                    key=lambda t: (
                        1 if self._tier(t) == "manual" else 0,
                        float(t.get("last_ts") or 0),
                        int(t.get("hits") or 0),
                    ),
                    reverse=True,
                )[: self.max_per_group]
                groups[gid] = {str(t["text"]): t for t in keep}

        self._promote()
        self._expire_global(now)
        self._store["updated_at"] = now
        self._store["last_scan_day"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        self._migrate_schema()
        self._save()
        return report

    def _tier(self, item: dict[str, Any]) -> str:
        """这条现在算哪一级。**分级只看这三个字段，别在别处另写一套判断。**

        - `manual` 手标：主人标过，永不过期
        - `alias`  谐音代称：只换了字、意思没变，不算梗（见 `_is_substitution`）
        - `long`   长期梗：**联网确认过是梗**（`verified=True`，或来源是网上的）
        - `local`  群黑话：跨 ≥`min_days` 天还在用，网上还没查到 / 查不到
        - `short`  当场梗：只在一场里复读，24 小时没再出现就忘

        和旧版最大的区别：**跨天不再等于长期**。以前 `days>=2` 就写 long、
        TTL 直接给 30 天，结果一堆没人确认过的词躺在"长期"里。
        现在跨天只升到 `local` 等验证，只有真的搜到出处才进 `long`。
        """
        if meme_schema.is_candidate(item):
            return "candidate"
        if item.get("source") == "owner" or item.get("state") == "manual":
            return "manual"
        if _is_substitution(item):
            return "alias"
        if item.get("verified") is True or item.get("source") == "web":
            return "long"
        if int(item.get("days") or 0) >= self.min_days:
            return "local"
        return "short"

    def _state_of(self, item: dict[str, Any]) -> str:
        """写回 JSON 的 state：代称不带 state（它有 kind），其余四级各写各的。"""
        tier = self._tier(item)
        return "manual" if tier == "alias" and item.get("state") == "manual" else tier

    def _sync_state(self, item: dict[str, Any]) -> None:
        if self._tier(item) == "alias":
            # 代称用 kind 表示自己是谁，不该被写上 short/local 的保鲜期
            item.pop("state", None)
            return
        item["state"] = self._state_of(item)

    def _promote(self) -> None:
        """同一个串在两个以上群都站住了 → 升级成「普遍说法」，从各群表里拿掉。

        这种就不是某个群的黑话了：所有会话（含私聊）都能用。
        已经升级过的，后面哪怕只在一个群出现，也继续更新它、不再落回群里。
        """
        groups = self._store.get("groups") or {}
        common = self._store.setdefault("global", {})
        by_key: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for gid, book in groups.items():
            if not isinstance(book, dict):
                continue
            for key, item in book.items():
                by_key.setdefault(key, []).append((gid, item))

        for key, entries in by_key.items():
            if key not in common and len(entries) < 2:
                continue
            merged = _merge_entries(
                key, [item for _gid, item in entries], tier_of=self._tier
            )
            old = common.get(key)
            if isinstance(old, dict):
                if old.get("scene"):
                    merged["scene"] = old["scene"]
                if old.get("example") and not merged.get("example"):
                    merged["example"] = old["example"]
                if self._tier(old) == "manual":
                    merged["source"] = "owner"
                    merged["state"] = "manual"
                merged["first_ts"] = min(
                    float(old.get("first_ts") or merged["first_ts"]),
                    merged["first_ts"],
                )
            common[key] = merged
            for gid, _item in entries:
                book = groups.get(gid)
                if isinstance(book, dict):
                    book.pop(key, None)

        for gid in list(groups):
            if not groups[gid]:
                groups.pop(gid, None)

    def _expire_global(self, now: float) -> None:
        common = self._store.get("global") or {}
        for key in list(common):
            item = common[key]
            tier = self._tier(item)
            if tier == "manual":
                continue
            if tier == "candidate":
                ttl = self.short_ttl * 7
            elif tier == "alias":
                ttl = self.web_ttl
            elif tier == "short":
                ttl = self.short_ttl
            else:
                ttl = self.keep_days * 86400
            if now - float(item.get("last_ts") or 0) > ttl:
                common.pop(key, None)

    # ---------- 命中 ----------

    def risk_of(self, group_id: str, text: str) -> str:
        """这句话命中的梗里有没有骂人/黄腔/刷屏的；给复读等非模型路径做硬门。"""
        if not self.enabled or not text:
            return ""
        body = norm(text)
        if not body:
            return ""
        books = (
            self._store.get("global") or {},
            (self._store.get("groups") or {}).get(str(group_id)) or {},
        )
        for book in books:
            if not isinstance(book, dict):
                continue
            for item in book.values():
                if (
                    not isinstance(item, dict)
                    or item.get("verified") is False
                    or meme_schema.is_candidate(item)
                ):
                    continue
                if not _matches(item, body, self.spell):
                    continue
                risk = self._risk_of_item(item)
                if risk:
                    return risk
        return ""

    def brief(self, group_id: str, text: str) -> str:
        """这句话提到了梗/代称，才返回一小段；否则空串（不占 token）。

        两层：`global`（跨群都在用的普遍说法，私聊也认）和这个群自己的梗。
        谐音代称是「正常叫法」，不是玩笑：说到代称或它代替的原词都会命中，
        提示里写清楚「用代称、别用原词、别解释」。
        """
        if not self.enabled or not text:
            return ""
        body = norm(text)
        if not body:
            return ""
        gid = str(group_id or "")
        common = self._pick(self._store.get("global"), body)
        local = self._pick((self._store.get("groups") or {}).get(gid), body)
        out: list[str] = []
        for items, scope in ((common, "common"), (local, "local")):
            if not items:
                continue
            subs = [item for item in items if _is_substitution(item)]
            normals = [item for item in items if not _is_substitution(item)]
            # 只有「这句话基本就是在说这个梗」才照接：
            # 一堆话里顺带提一句「帅不帅」，不抢答。
            # 但「连珠」要算「在说梗」：一句里同时甩三句克里斯台词
            # （二号机你不要给我搞笑，帅不帅帅不帅，你拿什么和我玩），
            # 把三个梗都扣掉之后没剩几个字，那就是在等她接，不是顺带提。
            keys = [norm(str(item.get("text") or "")) for item in items if item.get("text")]
            keys = [k for k in keys if k]

            def _fixed_reply_ok(item: dict[str, Any]) -> bool:
                if not item.get("reply"):
                    return True
                rest = body
                for key in keys:
                    rest = rest.replace(key, "")
                return len(rest) < 6

            # 顺带提到的不整条丢掉，只降级成「别抢答」：
            # 2026-10-30 实测，给「帅不帅」挂上接法之后，三句连发的消息里
            # 它整条消失了（rest >= 4 被滤掉）—— 标了接法反而看不见。
            chill = {
                norm(str(item.get("text") or ""))
                for item in normals
                if not _fixed_reply_ok(item)
            }
            eager = [item for item in normals if norm(str(item.get("text") or "")) not in chill]
            risky_subs = any(self._risk_of_item(item) for item in subs)
            risky_normals = any(self._risk_of_item(item) for item in normals)
            if subs:
                if risky_subs:
                    if scope == "common":
                        out.append("（这些代称里，标了「只认不用」的知道意思就行：")
                    else:
                        out.append("（这个群里的代称，标了「只认不用」的知道意思就行：")
                elif scope == "common":
                    out.append("（这些代称挺常见，正常这样叫：")
                else:
                    out.append("（这个群里把原词说成代称，这是正常叫法，不是玩笑：")
                out.extend(self._line_items(subs, len(out[-1])))
                if risky_subs:
                    out.append(
                        "（标了「只认不用」的别主动喊、别对真人用、别跟着复读；"
                        "没标的可以照常用。）"
                    )
                elif scope == "common":
                    out.append("（说到这个意思就用代称，别用原词、别解释。）")
                else:
                    out.append(
                        "（说到这个意思就用代称，别用原词、别解释；别对群外的人用。）"
                    )
            if normals:
                if risky_normals:
                    out.append("（这个群里常这么说，标了「只认不用」的知道就行：")
                elif scope == "common":
                    out.append("（这个说法挺常见：")
                else:
                    out.append("（这个群里常这么说，只在这个群用：")
                out.extend(self._line_items(normals, len(out[-1]), chill, gid))
                if risky_normals:
                    out.append(
                        "（标了「只认不用」的别主动用、别跟着复读；没标的照旧。）"
                    )
                elif any(item.get("reply") for item in eager):
                    out.append(
                        "（有固定接法、而且对方明显在等你接时才照接；"
                        "只是顺带提到就别抢答。）"
                    )
                elif any(item.get("reply") for item in normals):
                    out.append(
                        "（有接法的那几句是顺带提到的，别抢答；"
                        "有人单发这句时再按接法接，别解释。）"
                    )
                else:
                    out.append("（别当成事实讲；别主动解释这个梗。）")
        return "\n".join(out)[: self.max_chars] if len(out) > 1 else ""

    def _pick(self, book: Any, body: str) -> list[dict[str, Any]]:
        if not isinstance(book, dict) or not book:
            return []
        hits = [
            item
            for key, item in book.items()
            if key
            and not is_command(key)
            and item.get("verified") is not False
            and not meme_schema.is_candidate(item)
            and _matches(item, body, self.spell)
        ]
        hits.sort(
            key=lambda t: (int(t.get("hits") or 0), float(t.get("last_ts") or 0)),
            reverse=True,
        )
        return hits[:4]

    def _line_items(
        self,
        items: list[dict[str, Any]],
        used: int,
        chill: set[str] | None = None,
        group_id: str = "",
    ) -> list[str]:
        lines: list[str] = []
        for item in items:
            key = str(item.get("text") or "")
            parts = _parts_of(item)
            risk = self._risk_of_item(item)
            if risk:
                # 骂人/黄腔/刷屏：只让她知道有这个叫法，不展开意思、不教接法。
                line = memetone.line_note(key, risk, str(item.get("scene") or ""))
                if parts:
                    # 拼出来的词得说清是拼的，不然「鱼八分钱」看着像普通叫法
                    line += f"（{' + '.join(parts)} 拼的）"
            else:
                scene = str(item.get("scene") or "").strip()
                example = str(item.get("example") or "").strip()
                replies = _replies_of(item)
                means = _substitution_means(item)
                if means:
                    line = f"· 「{key}」就是「{means}」" + (f"：{scene}" if scene else "")
                    if not scene and example:
                        line += f"（例：{example}）"
                    if parts:
                        line += f"（{' + '.join(parts)} 拼的）"
                elif parts:
                    line = f"· 「{key}」= {' + '.join(parts)} 拼的"
                    if scene:
                        line += f"：{scene}"
                else:
                    line = f"· 「{key}」" + (f"：{scene}" if scene else "")
                    if not replies and example and not scene:
                        line += f"（例：{example}）"
                    # 手写的场景没有、接法也没有：用每天算出来的用法画像顶上。
                    # 一定要标明是统计的，别让她当铁律讲。
                    if not replies and self.usage is not None:
                        mined = ""
                        try:
                            mined = str(self.usage.scene_for(group_id, key) or "")
                        except Exception:  # noqa: BLE001 - 画像坏了不能挡住注入
                            mined = ""
                        if not scene and mined:
                            line += f"：{mined}（统计出来的，别当铁律）"
                        elif not scene or len(scene) < 20:
                            # 结论薄/没有：给真实用例，让她自己看怎么用。
                            # 例子比概括更能泛化到新语境，也更像人学会的。
                            sample = ""
                            try:
                                sample = str(self.usage.example_for(group_id, key) or "")
                            except Exception:  # noqa: BLE001
                                sample = ""
                            if sample:
                                sep = "：" if not scene else "；"
                                line += (
                                    f"{sep}群里上次这么用——{sample}"
                                    "（例子，不是现在有人这么说；照着接，别解释）"
                                )
                if replies:
                    # 按接了几次排序：最常接的那句放前面，别的当备选
                    line += f"；通常接「{replies[0]['text']}」"
                    rest = [r["text"] for r in replies[1:3]]
                    if rest:
                        line += "，也有人接「" + "」或「".join(rest) + "」"
                if chill and norm(key) in chill:
                    line += "（这句在这条消息里是顺带提的，别抢答）"
            if used + len(line) > self.max_chars:
                break
            lines.append(line)
            used += len(line)
        return lines

    # ---------- 网络梗入库 ----------

    @staticmethod
    def means_of(item: dict[str, Any]) -> str:
        """这条梗如果是谐音代称，返回它代替的原词；否则空串。"""
        return _substitution_means(item)

    def knows(self, text: str) -> bool:
        """这个词是不是已经知道/已经被否过：知道就不该再去问群友。"""
        key = norm(text)
        if not key:
            return False
        if key in (self._store.get("rejected") or {}):
            return True
        if key in (self._store.get("global") or {}):
            return True
        for book in (self._store.get("groups") or {}).values():
            if isinstance(book, dict) and key in book:
                return True
        # 缩写也算知道：「f8fq」认得出「飞八分钱」就不用再去问群友
        return bool(self._resolve_variant(key))

    def reject(self, text: str, *, now: float | None = None, source: str = "human") -> bool:
        """人确认「不是梗」：从表里拿掉，并记进 rejected，以后不再收、不再问。"""
        key = norm(text)
        if not key:
            return False
        now = time.time() if now is None else float(now)
        books: list[Any] = [self._store.get("global")]
        books.extend((self._store.get("groups") or {}).values())
        for book in books:
            if isinstance(book, dict):
                book.pop(key, None)
        self._store.setdefault("rejected", {})[key] = {
            "text": key,
            "at": now,
            "source": str(source or "human")[:20],
        }
        self._save()
        return True

    def add_web_meme(
        self,
        text: str,
        note: str = "",
        *,
        example: str = "",
        source_url: str = "",
        now: float | None = None,
    ) -> bool:
        """群友确认是梗、网上也查到解释：规范化后写进 `global`，所有会话都能用。

        - 键按 `norm()` 归一化，1~12 字，功能指令不收；
        - 同名的群梗会被**合并进 global 并从群表里拿掉**，不重复注入；
        - 已经验过「不是梗」的，先从 `rejected` 里撤销（人说了算）；
        - 已有场景不覆盖，只补空字段；固定接法按次数合并。
        """
        key = norm(text)
        if not key or not (1 <= len(key) <= MAX_LEN) or is_command(key):
            return False
        now = time.time() if now is None else float(now)
        self._store.setdefault("rejected", {}).pop(key, None)
        common = self._store.setdefault("global", {})
        groups = self._store.get("groups") or {}
        old = common.get(key)
        old = old if isinstance(old, dict) else {}
        group_items: list[dict[str, Any]] = []
        for book in list(groups.values()):
            if not isinstance(book, dict):
                continue
            item = book.pop(key, None)
            if isinstance(item, dict):
                group_items.append(item)
        for gid in list(groups):
            if isinstance(groups[gid], dict) and not groups[gid]:
                groups.pop(gid, None)
        merged = _merge_replies([old, *group_items])
        scene = str(old.get("scene") or "")
        if not scene:
            scene = next(
                (str(i.get("scene") or "") for i in group_items if i.get("scene")), ""
            )
        if not scene:
            scene = one_line(note, 80)
        sample = str(old.get("example") or "")
        if not sample:
            sample = next(
                (str(i.get("example") or "") for i in group_items if i.get("example")),
                "",
            )
        if not sample:
            sample = one_line(example, 60)
        hits = max(
            [int(old.get("hits") or 0)]
            + [int(i.get("hits") or 0) for i in group_items]
        )
        speakers = max(
            [int(old.get("speakers") or 0)]
            + [int(i.get("speakers") or 0) for i in group_items]
        )
        days = max(
            [int(old.get("days") or 0)] + [int(i.get("days") or 0) for i in group_items]
        )
        first = min(
            [float(old.get("first_ts") or now)]
            + [float(i.get("first_ts") or now) for i in group_items]
        )
        entry = dict(old)
        parts = _parts_of(old) or next(
            (_parts_of(i) for i in group_items if _parts_of(i)), []
        )
        if parts:
            entry["parts"] = parts
        entry.update(
            {
                "text": key,
                "scene": scene,
                "example": sample,
                "reply": merged,
                "verified": True,
                "verified_at": now,
                "verify_note": one_line(note or old.get("verify_note"), 100),
                # 网上确认过 = 长期梗；来源单独记，别再多一个 state
                "state": "long",
                "source": "web",
                "hits": hits,
                "speakers": speakers,
                "days": days,
                "first_ts": first,
                "last_ts": now,
            }
        )
        if source_url:
            entry["source_url"] = str(source_url)[:300]
        self._apply_substitution(entry, note)
        common[key] = entry
        self._migrate_schema()
        self._save()
        return True

    # ---------- 聊天搜索候选 ----------

    def add_candidate(
        self,
        group_id: str,
        term: str,
        query: str,
        material: str,
        *,
        now: float | None = None,
    ) -> bool:
        """聊天里搜到「可能是梗」：只写候选，不参与注入，等主人确认。"""
        if not self.enabled:
            return False
        key = norm(term)
        if not key or len(key) > 20:
            return False
        moment = time.time() if now is None else float(now)
        gid = str(group_id or "")
        if gid:
            target = self._store.setdefault("groups", {}).setdefault(gid, {})
        else:
            target = self._store.setdefault("global", {})
        old = target.get(key)
        if old is not None and not meme_schema.is_candidate(old):
            return False
        if key in (self._store.get("rejected") or {}):
            return False
        doc = meme_schema.build_candidate(key, query, material, now=moment)
        item = dict(doc)
        item.update(
            {
                "text": key,
                "scene": str(doc.get("meaning") or "")[:160],
                "example": key,
                "reply": (old or {}).get("reply") or [],
                "state": "candidate",
                "source": "web-candidate",
                "verified": None,
                "hits": int((old or {}).get("hits") or 0),
                "speakers": int((old or {}).get("speakers") or 0),
                "days": int((old or {}).get("days") or 0),
                "first_ts": float((old or {}).get("first_ts") or moment),
                "last_ts": moment,
            }
        )
        target[key] = item
        self._save()
        return True

    def candidates(self, group_id: str = "") -> list[dict[str, Any]]:
        gid = str(group_id or "")
        books: list[dict[str, Any]] = []
        if gid:
            books.append((self._store.get("groups") or {}).get(gid) or {})
        books.append(self._store.get("global") or {})
        out: list[dict[str, Any]] = []
        for book in books:
            if not isinstance(book, dict):
                continue
            out.extend(
                meme_schema.from_legacy(item)
                for item in book.values()
                if meme_schema.is_candidate(item)
            )
        return out

    def candidates_overview(
        self,
        group_id: str = "",
        limit: int = 8,
        names: dict[str, str] | None = None,
    ) -> str:
        names = names or {}
        gid = str(group_id or "")
        rows: list[tuple[str, dict[str, Any]]] = []
        if gid:
            book = (self._store.get("groups") or {}).get(gid) or {}
            rows.extend(
                (gid, item)
                for item in book.values()
                if isinstance(item, dict) and meme_schema.is_candidate(item)
            )
        common = self._store.get("global") or {}
        rows.extend(
            ("", item)
            for item in common.values()
            if isinstance(item, dict) and meme_schema.is_candidate(item)
        )
        if not rows:
            return "本鱼现在没有待确认的候选梗。"
        rows.sort(key=lambda pair: -float(pair[1].get("last_ts") or 0))
        label = str(names.get(gid) or "").strip()
        head = "本鱼搜到这些候选梗（还没确认）"
        if label:
            head += f"（{label}）"
        lines = [head + "："]
        for _scope, item in rows[: max(1, int(limit))]:
            term = str(item.get("text") or "")
            meaning = str(item.get("meaning") or item.get("scene") or "").strip()
            src = str((item.get("evidence") or {}).get("query") or "").strip()
            line = f"· 「{term}」"
            if meaning:
                line += meaning[:80]
            if src:
                line += f"（搜过：{src[:40]}）"
            lines.append(line)
        if len(rows) > limit:
            lines.append(f"……还有 {len(rows) - limit} 条。")
        lines.append("确认：确认梗：X｜删掉：删候选：X")
        return "\n".join(lines)

    def confirm_candidate(self, group_id: str, text: str) -> bool:
        key = norm(text)
        gid = str(group_id or "")
        books = [((self._store.get("groups") or {}).get(gid) or {})] if gid else []
        books.append(self._store.get("global") or {})
        for book in books:
            if not isinstance(book, dict):
                continue
            item = book.get(key)
            if not isinstance(item, dict) or not meme_schema.is_candidate(item):
                continue
            item["status"] = "confirmed"
            item["source"] = "owner"
            item["state"] = "manual"
            item["verified"] = True
            item["confirmed_at"] = time.time()
            if not item.get("meaning"):
                item["meaning"] = str(item.get("scene") or "")[:160]
            self._save()
            return True
        return False

    # ---------- 主人维护 ----------

    def list_group(self, group_id: str) -> list[dict[str, Any]]:
        book = (self._store.get("groups") or {}).get(str(group_id)) or {}
        return sorted(
            (dict(item) for item in book.values()),
            key=lambda t: (int(t.get("hits") or 0), float(t.get("last_ts") or 0)),
            reverse=True,
        )

    def has(self, group_id: str, text: str) -> bool:
        """这条梗在不在这个群/通用表里（编辑前先确认）。"""
        key = norm(text)
        if not key:
            return False
        gid = str(group_id or "")
        books: list[Any] = []
        if gid in ("*", "global"):
            books.append(self._store.get("global"))
        else:
            books.append((self._store.get("groups") or {}).get(gid))
            books.append(self._store.get("global"))
        for book in books:
            if not isinstance(book, dict):
                continue
            if key in book:
                return True
            for k in book:
                if k and (k in key or key in k):
                    return True
        return False

    def describe(self, group_id: str, text: str) -> str:
        """查一条梗/谐音的详情；只读。"""
        key = norm(text)
        if not key:
            return "要查哪条？发「查梗：词」。"
        gid = str(group_id or "")
        candidates: list[tuple[str, dict[str, Any]]] = []
        if gid in ("*", "global"):
            book = self._store.get("global") or {}
            candidates.extend(("通用", it) for it in book.values() if isinstance(it, dict))
        else:
            book = (self._store.get("groups") or {}).get(gid) or {}
            candidates.extend(("本群", it) for it in book.values() if isinstance(it, dict))
            common = self._store.get("global") or {}
            candidates.extend(("通用", it) for it in common.values() if isinstance(it, dict))
        for where, item in candidates:
            k = norm(str(item.get("text") or ""))
            if not k or not (k in key or key in k or k == key):
                continue
            doc = meme_schema.from_legacy(item)
            lines = [f"「{item.get('text')}」（{where}）"]
            if doc.get("meaning"):
                lines.append(f"意思：{doc['meaning']}")
            if doc.get("usage"):
                lines.append(f"怎么用：{doc['usage']}")
            if doc.get("origin"):
                lines.append(f"出处：{doc['origin']}")
            if item.get("scene") and item.get("scene") != doc.get("meaning"):
                lines.append(f"场景：{item['scene']}")
            means = _substitution_means(item)
            if means:
                lines.append(f"谐音代称：{item.get('text')} = {means}")
            parts = _parts_of(item)
            if parts:
                lines.append("组成：" + " + ".join(parts))
                risk = self._risk_of_item(item)
                if risk:
                    lines.append(f"风险：跟原料一样（{memetone.label(risk) or '只认不用'}）")
            replies = _replies_of(item)
            if replies:
                lines.append("固定接法：" + "、".join(f"「{r['text']}」" for r in replies))
            aliases = [str(x) for x in (item.get("alias") or []) if str(x).strip()]
            if self.spell is not None:
                aliases += [x for x in self.spell.of(item.get("text")) if x not in aliases]
            if aliases:
                lines.append("也写作：" + "、".join(aliases[:6]))
            if doc.get("tags"):
                lines.append("标签：" + "、".join(str(t) for t in doc["tags"]))
            lines.append(
                f"状态：{self._tier(item)}｜来源：{item.get('source') or 'auto'}"
                f"｜复读 {int(item.get('hits') or 0)} 次 / {int(item.get('speakers') or 0)} 人"
            )
            return "\n".join(lines)
        return f"没找到「{text}」——先「记梗 {text}」查一下，或者用「梗：{text} = 意思」新建。"

    def drop(self, group_id: str, text: str) -> bool:
        """主人说「这不算梗」：删掉一条。

        群号写 `*` / `global` 就是删「普遍说法」；写具体群号时两处都找一遍，
        因为同一条可能已经升级到全局了。
        """
        gid = str(group_id or "")
        books: list[Any] = []
        if gid in ("*", "global"):
            books.append(self._store.get("global"))
        else:
            books.append((self._store.get("groups") or {}).get(gid))
            books.append(self._store.get("global"))
        key = norm(text)
        for book in books:
            if not isinstance(book, dict):
                continue
            if key in book:
                book.pop(key, None)
                self._save()
                return True
            for k in list(book):
                if k and (k in key or key in k):
                    book.pop(k, None)
                    self._save()
                    return True
        return False

    def annotate(
        self,
        group_id: str,
        text: str,
        scene: str,
        example: str = "",
        reply: list[str] | None = None,
        *,
        means: str = "",
        now: float | None = None,
    ) -> bool:
        """主人标场景 / 固定接法。表里没有的就**新建一条**。

        统计认不出「饿啊」这种（不是复读），但它就是梗 —— 主人标一次场景、
        给一句固定接法，以后一直带着。群号写 `*` / `global` 就标成「普遍说法」。
        `means` 是谐音代称代替的原词（如「区」→「蛆」）；给了就按「本群默认用词」处理。
        """
        if not group_id:
            return False
        key = norm(text)
        if not key:
            return False
        now = time.time() if now is None else float(now)
        means_key = norm(means)
        if means_key == key or len(means_key) > MAX_LEN:
            means_key = ""
        replies = [
            {"text": str(x).strip()[:20], "n": 0}
            for x in (reply or [])
            if str(x).strip()
        ][:3]
        if str(group_id) in ("*", "global"):
            target = self._store.setdefault("global", {})
        else:
            groups = self._store.setdefault("groups", {})
            target = groups.setdefault(str(group_id), {})
        item = target.get(key)
        if item is None:
            item = {
                "text": key,
                "scene": str(scene or "")[:80],
                "example": str(example or text or "")[:60],
                "reply": replies,
                "state": "manual",
                "hits": 0,
                "speakers": 0,
                "days": 0,
                "first_ts": now,
                "last_ts": now,
                "source": "owner",
            }
            target[key] = item
        else:
            item["scene"] = str(scene or "")[:80]
            if example:
                item["example"] = str(example)[:60]
            if replies:
                item["reply"] = replies
            item["source"] = "owner"
            item["state"] = "manual"
            item["last_ts"] = now
        if means_key:
            item["means"] = means_key
            item["kind"] = "substitution"
        else:
            self._apply_substitution(item, scene, example)
        self._migrate_schema()
        self._save()
        return True

    def terms_by_group(self) -> dict[str, list[str]]:
        """每个群要不要算用法画像、算哪些词：本群词条 + 普遍说法。

        只算梗表里已经有的词（主人标的、复读收上来的）。没进表的词不猜——
        画像只回答「这条梗怎么用」，不负责发现新梗。
        """
        common = [
            str(key)
            for key in (self._store.get("global") or {})
            if key and not is_command(key)
        ]
        out: dict[str, list[str]] = {}
        for gid, book in (self._store.get("groups") or {}).items():
            if not isinstance(book, dict):
                continue
            terms = [str(k) for k in book if k and not is_command(k)]
            for key in common:
                if key not in terms:
                    terms.append(key)
            if terms:
                out[str(gid)] = terms
        return out

    def list_global(self) -> list[dict[str, Any]]:        return sorted(
            (dict(item) for item in (self._store.get("global") or {}).values()),
            key=lambda t: (int(t.get("hits") or 0), float(t.get("last_ts") or 0)),
            reverse=True,
        )

    # ---------- 联网验证 ----------

    @staticmethod
    def _day_key(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def verify_budget_left(self, per_day: int, *, now: float | None = None) -> int:
        """今天还能验几条。跨天自动重置；0 表示今天不再验。"""
        per_day = max(0, int(per_day))
        now = time.time() if now is None else float(now)
        if self._store.get("verify_day") != self._day_key(now):
            return per_day
        used = int(self._store.get("verify_count") or 0)
        return max(0, per_day - used)

    def _used_verify(self, now: float) -> None:
        day = self._day_key(now)
        if self._store.get("verify_day") != day:
            self._store["verify_day"] = day
            self._store["verify_count"] = 0
        self._store["verify_count"] = int(self._store.get("verify_count") or 0) + 1

    def _mature(self, item: dict[str, Any]) -> bool:
        """群共识够不够格「进化成梗」：跨天、多人、反复复读都要站住。

        单日爆火（甚至只被两三个人接一次）先当临时用法，不花搜索费。
        统计每天刷新，站住了才会走到联网验证。
        **门槛只看证据三件套**，不看 state —— state 是结果，不是条件。
        """
        if self._tier(item) not in ("local", "long"):
            return False
        return (
            int(item.get("hits") or 0) >= self.verify_min_hits
            and int(item.get("speakers") or 0) >= self.verify_min_speakers
            and int(item.get("days") or 0) >= self.verify_min_days
        )

    def pending_verify(self, limit: int = 5) -> list[tuple[str, str, dict[str, Any]]]:
        """够格进化成梗、还没验过「到底是不是梗」的条目：[(群号, 串, 条目)]。

        只收群共识已经站住的：跨天 ≥`verify_min_days` 天、≥`verify_min_speakers` 人、
        累计 ≥`verify_min_hits` 次复读。单日爆火和刚冒头的串不验。
        主人标的（source=owner）不验：他已经说了算。谐音代称也不验：
        它只是换了字，意思没变，没有"是不是梗"这个问题。
        `*` 表示全局表。
        """
        out: list[tuple[str, str, dict[str, Any]]] = []
        for gid, book in (self._store.get("groups") or {}).items():
            for key, item in (book or {}).items():
                if item.get("source") == "owner" or item.get("verified") is not None:
                    continue
                if item.get("verify_skip") or self._tier(item) == "alias":
                    continue
                if not self._mature(item):
                    continue
                out.append((str(gid), str(key), item))
        for key, item in (self._store.get("global") or {}).items():
            if item.get("source") == "owner" or item.get("verified") is not None:
                continue
            if item.get("verify_skip") or self._tier(item) == "alias":
                continue
            if not self._mature(item):
                continue
            out.append(("*", str(key), item))
        out.sort(key=lambda x: -int(x[2].get("hits") or 0))
        return out[: max(0, int(limit))]

    def mark_verified(
        self,
        group_id: str,
        text: str,
        is_meme: bool | None,
        note: str = "",
        *,
        now: float | None = None,
    ) -> bool:
        """记下联网验证的结论，并扣一次每日预算。

        - 是梗：`verified=True`，解释补进 scene（场景已经有的不覆盖）；
        - 不是梗、但群共识已经站住：保留条目，记「网上查不到，按群里的用法」，
          以后不再查 —— 群内黑话本来就不该被网上搜不到否掉；
        - 不是梗、共识也没站住：整条删掉，记进 `rejected`，下次统计不再收回来；
        - 不确定：只记一次尝试，试到第 3 次还拿不准就按「不是梗」处理。
        """
        now = time.time() if now is None else float(now)
        gid = str(group_id or "")
        if gid in ("*", "global"):
            target = self._store.setdefault("global", {})
        else:
            target = (self._store.get("groups") or {}).get(gid)
        if not isinstance(target, dict):
            return False
        key = norm(text)
        item = target.get(key)
        if item is None:
            return False
        mature = self._mature(item)
        self._used_verify(now)
        if is_meme is None:
            tries = int(item.get("verify_tries") or 0) + 1
            item["verify_tries"] = tries
            item["verify_at"] = now
            if tries < 3:
                self._save()
                return True
            is_meme = False
        if is_meme:
            item["verified"] = True
            item["verified_at"] = now
            item.pop("verify_skip", None)
            if note:
                item["verify_note"] = str(note)[:100]
                if not item.get("scene"):
                    item["scene"] = str(note)[:80]
        elif mature:
            item["verify_skip"] = True
            item["verify_at"] = now
            item["verify_note"] = str(note or "网上查不到，按群里的用法")[:100]
        else:
            target.pop(key, None)
            self._store.setdefault("rejected", {})[key] = {
                "text": key,
                "at": now,
            }
        if is_meme or mature:
            self._apply_substitution(item, note)
        # 分级是结果：验过是梗 → long；查不到但站住了 → local；代称 → alias
        self._sync_state(item)
        self._save()
        return True

    def status(self) -> dict[str, Any]:
        groups = self._store.get("groups") or {}
        everything = list(groups.values()) + [self._store.get("global") or {}]
        verified = sum(
            1
            for book in everything
            for item in book.values()
            if item.get("verified") is True and not meme_schema.is_candidate(item)
        )
        common = self._store.get("global") or {}
        tiers = {"short": 0, "local": 0, "long": 0, "manual": 0, "candidate": 0}
        for book in everything:
            for item in book.values():
                if not isinstance(item, dict) or _is_substitution(item):
                    continue
                tier = self._tier(item)
                if tier in tiers:
                    tiers[tier] += 1
        return {
            "enabled": self.enabled,
            "groups": len(groups),
            # memes 只数「梗」，谐音代称单独一个 aliases
            "memes": sum(
                1
                for book in everything
                for item in book.values()
                if isinstance(item, dict)
                and not _is_substitution(item)
                and not meme_schema.is_candidate(item)
            ),
            "global": len(common),
            "aliases": sum(
                1
                for book in everything
                for item in book.values()
                if isinstance(item, dict) and _is_substitution(item)
            ),
            "short": tiers["short"],
            "local": tiers["local"],
            "long": tiers["long"],
            "manual": tiers["manual"],
            "web": sum(
                1
                for item in common.values()
                if isinstance(item, dict) and item.get("source") == "web"
            ),
            "verified": verified,
            "candidate": tiers["candidate"],
            "rejected": len(self._store.get("rejected") or {}),
            "verify_day": self._store.get("verify_day", ""),
            "verify_today": int(self._store.get("verify_count") or 0),
            "verify_ready": len(self.pending_verify(10**6)),
            "last_scan_day": self._store.get("last_scan_day", ""),
            "updated_at": self._store.get("updated_at", 0),
        }

    def last_scan_day(self) -> str:
        return str(self._store.get("last_scan_day") or "")

    # ---------- 给主人看：她到底收了哪些 ----------
    #
    # 2026-10-27 主人问「收集的梗在哪可以看见」：表在 data/memes.json 里，
    # 但没人愿意翻 JSON。这里给一份能直接在聊天里看的清单（本地读表，0 token）。

    _STATE_LABEL = {
        "short": "当场梗",
        "local": "群黑话",
        "long": "长期梗",
        "manual": "手标",
    }

    @staticmethod
    def _items_of(book: dict[str, Any], *, aliases: bool = False) -> list[dict[str, Any]]:
        """清单里该出现的条目。梗和代称分开取：代称不是一个"梗"。"""
        return [
            item
            for item in book.values()
            if isinstance(item, dict)
            and item.get("verified") is not False
            and not meme_schema.is_candidate(item)
            and _is_substitution(item) == aliases
        ]

    def overview(
        self,
        group_id: str = "",
        limit: int = 8,
        names: dict[str, str] | None = None,
    ) -> str:
        """她收了哪些梗：群里只看本群，私聊看全部。只读，不写库。

        梗和**谐音代称**分两栏：代称只是把字换了个说法，意思没变，
        既不该按梗的 24 小时过期，也不该混在"梗"的条数里。
        """
        if not self.enabled:
            return "群梗统计现在是关着的（说「打开群梗」才会收）。"
        names = names or {}
        groups = self._store.get("groups") or {}
        common = self._store.get("global") or {}
        gid = str(group_id or "")
        out: list[str] = []
        if gid:
            book = groups.get(gid) if isinstance(groups.get(gid), dict) else {}
            items = self._items_of(book)
            aliases = self._items_of(book, aliases=True)
            head = f"本鱼在这个群收了 {len(items)} 条梗"
            label = str(names.get(gid) or "").strip()
            if label:
                head += f"（{label}）"
            lines = self._overview_lines(book, limit)
            if not items and not aliases:
                return head + "。目前一条都没有 —— 群里还没出现够格的复读。"
            out = [head + "：" if lines else head + "。", *lines]
            if len(items) > len(lines):
                out.append(f"……按热度排的，还有 {len(items) - len(lines)} 条没列。")
            if aliases:
                out.append("")
                out.append("谐音代称（只是换了叫法）：" + self._alias_text(aliases))
        else:
            total = sum(len(self._items_of(b)) for b in groups.values())
            alias_total = sum(
                len(self._items_of(b, aliases=True)) for b in groups.values()
            )
            if not total and not alias_total and not common:
                return "本鱼一条梗都还没收：群里得先出现复读，才够格进表。"
            head = f"本鱼现在收了 {len(groups)} 个群共 {total} 条梗"
            if alias_total:
                head += f"、{alias_total} 个谐音代称"
            out = [head + f"，另有 {len(common)} 条跨群通用的："]
            for key, book in groups.items():
                if not isinstance(book, dict) or not book:
                    continue
                items = self._items_of(book)
                aliases = self._items_of(book, aliases=True)
                if not items and not aliases:
                    continue
                label = str(names.get(str(key)) or "").strip() or str(key)
                out.append("")
                out.append(f"【{label}】{len(items)} 条")
                out.extend(self._overview_lines(book, 3))
                if aliases:
                    out.append("谐音代称：" + self._alias_text(aliases))
            if common:
                out.append("")
                out.append(
                    "【通用】"
                    + "、".join(
                        f"「{str(it.get('text') or '').strip()}」"
                        for it in list(common.values())[:6]
                        if isinstance(it, dict)
                    )
                )
        rejected = len(self._store.get("rejected") or {})
        if rejected:
            out.append("")
            out.append(f"（另外 {rejected} 个词判过不是梗，已经排除。）")
        return "\n".join(out)

    def aliases_overview(
        self,
        group_id: str = "",
        limit: int = 20,
        names: dict[str, str] | None = None,
    ) -> str:
        """只看谐音代称：群里只看本群，私聊看所有群 + 通用。只读。"""
        if not self.enabled:
            return "群梗统计现在是关着的（说「打开群梗」才会收）。"
        names = names or {}
        groups = self._store.get("groups") or {}
        common = self._store.get("global") or {}
        gid = str(group_id or "")
        limit = max(1, int(limit))
        common_aliases = self._items_of(common, aliases=True)
        if gid:
            book = groups.get(gid) if isinstance(groups.get(gid), dict) else {}
            aliases = self._items_of(book, aliases=True)
            label = str(names.get(gid) or "").strip()
            where = f"（{label}）" if label else ""
            if not aliases and not common_aliases:
                return f"本鱼在这个群还没记下谐音代称{where}。"
            counts = []
            if aliases:
                counts.append(f"{len(aliases)} 个本群专属")
            if common_aliases:
                counts.append(f"{len(common_aliases)} 个通用")
            out = [f"本鱼在这个群可用的谐音代称：{'、'.join(counts)}{where}。"]
            if aliases:
                out.append("【本群】" + self._alias_text(aliases[:limit]))
                if len(aliases) > limit:
                    out.append(f"……本群还有 {len(aliases) - limit} 个没列。")
            if common_aliases:
                out.append("【通用】" + self._alias_text(common_aliases[:limit]))
                if len(common_aliases) > limit:
                    out.append(f"……通用还有 {len(common_aliases) - limit} 个没列。")
            return "\n".join(out)

        rows: list[tuple[str, list[dict[str, Any]]]] = []
        total = 0
        for key, book in groups.items():
            if not isinstance(book, dict):
                continue
            aliases = self._items_of(book, aliases=True)
            if aliases:
                total += len(aliases)
                rows.append((str(key), aliases))
        total += len(common_aliases)
        if not total:
            return "本鱼还没记下谐音代称。"
        out = [f"本鱼现在记了 {total} 个谐音代称："]
        for key, aliases in rows:
            label = str(names.get(key) or "").strip() or key
            text = self._alias_text(aliases[:limit])
            out.append(f"【{label}】{text}")
            if len(aliases) > limit:
                out.append(f"……还有 {len(aliases) - limit} 个没列。")
        if common_aliases:
            out.append("【通用】" + self._alias_text(common_aliases[:limit]))
            if len(common_aliases) > limit:
                out.append(f"……通用还有 {len(common_aliases) - limit} 个没列。")
        return "\n".join(out)

    @staticmethod
    def _alias_text(aliases: list[dict[str, Any]]) -> str:
        bits = []
        for item in aliases:
            text = str(item.get("text") or "").strip()
            means = _substitution_means(item)
            bits.append(f"「{text}」= {means}" if means else f"「{text}」")
        return "、".join(bits)

    def _overview_lines(self, book: dict[str, Any], limit: int) -> list[str]:
        # 梗和手标排前面，再按复读热度：她一眼能看到「站住了的」
        rank = {"long": 0, "manual": 0, "local": 1, "short": 2}
        items = self._items_of(book)
        items.sort(
            key=lambda it: (
                rank.get(self._tier(it), 3),
                -int(it.get("hits") or 0),
                -float(it.get("last_ts") or 0),
            )
        )
        return [self._overview_line(item) for item in items[: max(0, limit)]]

    def _overview_line(self, item: dict[str, Any]) -> str:
        text = str(item.get("text") or "").strip()
        bits = [f"「{text}」"]
        hits = int(item.get("hits") or 0)
        speakers = int(item.get("speakers") or 0)
        if hits or speakers:
            bits.append(f"{speakers} 人复读 {hits} 次")
        tier = self._tier(item)
        if tier in self._STATE_LABEL:
            bits.append(self._STATE_LABEL[tier])
        if self._tier(item) == "local" and item.get("verify_skip"):
            bits.append("网上查不到")
        if item.get("verified") is True:
            bits.append("网上有出处")
        line = "· " + "｜".join(bits)
        parts = _parts_of(item)
        scene = str(item.get("scene") or "").strip()
        if scene:
            scene = scene[:40] + ("…" if len(scene) > 40 else "")
            line += f"：{scene}"
            if parts:
                line += f"（= {' + '.join(parts)}）"
        elif parts:
            # 延伸/融合：意思在原料里，这里写清是谁拼的
            line += f"（{' + '.join(parts)} 拼的）"
        else:
            # 空 scene = 她只知道"大家在复读"，不知道是什么意思。
            # 以前这种行什么都不显示，看着像漏了；标出来才知道该补哪条。
            line += "（意思还没搞懂）"
        replies = _replies_of(item)
        if replies:
            line += f"（通常接「{replies[0]['text']}」）"
        aliases = [str(x) for x in (item.get("alias") or []) if str(x).strip()]
        if aliases:
            line += f"（也写作 {'、'.join(aliases[:3])}）"
        return line
