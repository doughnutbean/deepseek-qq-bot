"""一条梗在群里**到底怎么用**：从真实上下文里挖证据，不靠编。

2026-10-30 主人问「你是怎么判断的」。人工判断其实只做了四件事：

1. **元陈述**：群里有人明说 ——「这是克里斯的招牌名言」「有人晒出货了
   就叫他发0人在意」。这种句子比任何统计都硬。
2. **前置**：这句出现之前大家在聊什么 —— 出货 / 大金 / 爆率。
3. **后置**：别人紧接着接了什么 —— 「0人在意」×2。
4. **跨群**：同一句在别的群是不是也这么用。

这四件都能在本地算出来（0 token、不联网）。挖出来的东西**只当证据打印**，
不自动写回梗表 —— 写错了会污染她所有的回复，得人点头。
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

from .memes import GENERIC_REPLY, clean, is_command, norm
from .stopwords import is_meaningful

# 后置接法：太长的不是「接梗」，是在聊别的事
MAX_REPLY_LEN = 12
# 前置窗口：看这句前面几条
BEFORE_ROWS = 2
# 前置词至少出现这么多次，免得一次巧合就当成规律
MIN_BEFORE = 3
# lift 封顶：稀有词会把倍数拉到几千，封一下再排序
MAX_LIFT = 20.0
# 一条证据最多留这么多
TOP_N = 5
# 一个人接得越广，越可能是「万能回复」，不是接这一句的
GENERIC_FANOUT = 3
# 超过这么长的消息算「整段复读」，里面的词不算自己用梗
QUOTED_LEN = 15
# 接法至少要有两个人接过，否则只是某个人接了一句
MIN_REPLY_SPEAKERS = 2
# 群里明说「这是梗 / 口头禅 / 招牌」的句子：比统计可信，优先看
_META_RE = re.compile(
    r"(梗|口头禅|名言|招牌|暗号|名场面|都说|群里都|都这么叫|这么接"
    r"|叫他发|叫他喊|就叫他|都让他|都会接|就发)"
)
# @昵称 切开后剩下的是人名碎片（「@繁星也会落幕的大隐鲸」→ 繁星/落幕），
# 拿去当前置词只会挖出噪音
_AT = re.compile(r"[@＠][^\s，,。.！!？?：:；;、~～…（）()\[\]【】「」《》<>\"“”'’]*")


def _tokens(text: str) -> list[str]:
    """切词：jieba 有就用，没有就按两字滑窗兜底（只是少点精度，不会崩）。"""
    text = _AT.sub("", text or "")
    try:
        import jieba  # type: ignore

        words = jieba.lcut(text)
    except Exception:  # noqa: BLE001 - 没装 jieba 也要能用
        words = [text[i : i + 2] for i in range(len(text) - 1)]
    out: list[str] = []
    for word in words:
        word = str(word or "").strip()
        if len(word) >= 2 and is_meaningful(word, 2):
            out.append(word)
    return out


def _generic_replies(rows: list[dict[str, Any]], window: float = 60.0) -> set[str]:
    """接在很多不同的话后面 = 万能回复，不是「接这一句」的接法。

    「区」「哈哈哈」这种谁说完都能接一句；只看 60 秒内出现次数，
    它们会把每一条梗的接法都占满。
    """
    fanout: dict[str, set[str]] = defaultdict(set)
    n = len(rows)
    for i, row in enumerate(rows):
        body = clean(row.get("text", ""))
        if not body or len(body) > MAX_REPLY_LEN:
            continue
        uid = str(row.get("uid") or "")
        t0 = float(row.get("ts") or 0)
        # 往前找最近的一条**别人**说的
        for j in range(i - 1, -1, -1):
            prev = rows[j]
            if t0 - float(prev.get("ts") or 0) > window:
                break
            if str(prev.get("uid") or "") == uid:
                continue
            key = norm(prev.get("text", ""))
            if key:
                fanout[body].add(key)
            break
        if len(fanout[body]) >= GENERIC_FANOUT * 2:
            # 已经够广了，别继续攒（大群会拖慢启动）
            fanout[body] = set(list(fanout[body])[: GENERIC_FANOUT * 2])
    return {body for body, keys in fanout.items() if len(keys) >= GENERIC_FANOUT}


def _variants(key: str) -> list[str]:
    """同一个说法的常见换字：群里「和我玩」，字幕里他说「跟我玩」。

    不做通用近义词，只认这种一对一的换字（和/跟），别的交给写法表。
    """
    out = [key]
    for a, b in (("和", "跟"), ("跟", "和")):
        if a in key:
            alt = key.replace(a, b)
            if alt not in out:
                out.append(alt)
    return out


def _match(body: str, key: str) -> bool:
    return any(v in body for v in _variants(key))


class UsageMiner:
    """按群挖「这个说法怎么用」：前置词、后置接法、元陈述、跨群分布。"""

    def __init__(
        self,
        rows_by_group: dict[str, list[dict[str, Any]]],
        *,
        window: float = 120.0,
        before: int = BEFORE_ROWS,
        min_before: int = MIN_BEFORE,
        top: int = TOP_N,
        now: float | None = None,
    ) -> None:
        self.now = time.time() if now is None else float(now)
        self.window = max(10.0, float(window))
        self.before = max(0, int(before))
        self.min_before = max(1, int(min_before))
        self.top = max(1, int(top))
        self.rows: dict[str, list[dict[str, Any]]] = {}
        for gid, rows in (rows_by_group or {}).items():
            ordered = sorted(
                (dict(r) for r in rows if isinstance(r, dict)),
                key=lambda r: (int(r.get("ts") or 0), str(r.get("uid") or "")),
            )
            if ordered:
                self.rows[str(gid)] = ordered
        # 归一行缓存：批量挖几十个词时，别每个词都把整群 norm 一遍
        self._norm: dict[str, list[str]] = {
            gid: [norm(r.get("text", "")) for r in rows]
            for gid, rows in self.rows.items()
        }
        # 全群基线：同样的切词在整份语料里的词频，用来算「前置词是不是真的特殊」
        self._base: Counter[str] = Counter()
        self._base_total = 0
        for rows in self.rows.values():
            for row in rows:
                words = _tokens(norm(row.get("text", "")))
                self._base.update(words)
                self._base_total += len(words)
        # 万能回复：一句短话接在很多不同的话后面（「区」「哈哈哈」这种），
        # 就不是「接这一句」的接法。先算一遍，后面直接排除。
        self._generic = {gid: _generic_replies(rows) for gid, rows in self.rows.items()}

    # ---------- 挖 ----------

    def norm_rows(self, group_id: str) -> list[str]:
        """这个群归一化之后的正文（批量挖词时先用它粗筛）。"""
        return self._norm.get(str(group_id)) or []

    def profile(
        self,
        term: str,
        group_id: str = "",
        subject: str = "",
        min_before: int | None = None,
        before: int | None = None,
    ) -> dict[str, Any]:
        key = norm(term)
        out: dict[str, Any] = {
            "term": str(term or "").strip(),
            "key": key,
            "groups": {},
            "hits": 0,
            "speakers": 0,
            "before": [],
            "after": [],
            "meta": [],
            "suggestion": "",
        }
        if not key:
            return out
        targets = [str(group_id)] if group_id else list(self.rows)
        span = self.before if before is None else max(0, int(before))
        before_count: Counter[str] = Counter()
        before_occasions = 0
        after: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"n": 0, "uids": set(), "gap": []}
        )
        speakers: set[str] = set()
        meta: list[dict[str, Any]] = []
        quoted = 0  # 出现在整段复读里（不是自己单独发）
        examples: list[dict[str, Any]] = []

        for gid in targets:
            rows = self.rows.get(gid) or []
            bodies = self._norm.get(gid) or []
            hits = 0
            for i, row in enumerate(rows):
                body = bodies[i] if i < len(bodies) else norm(row.get("text", ""))
                if not _match(body, key):
                    continue
                hits += 1
                speakers.add(str(row.get("uid") or ""))
                # 整段复读（那条几百字的克里斯合集）里带一句，不算「用这个梗」：
                # 拿它当前置上下文只会挖出「兄弟/尊重/不要」这种同一段里的词。
                if len(body) > QUOTED_LEN:
                    quoted += 1
                    continue
                # 用例：真实的前后文。结论不够的时候，直接把「群里上次这么用」
                # 递给她看，比给她一句概括更像人——模型看三个例子会自己归纳。
                if len(examples) < 6:
                    snippet = _snippet(rows, i)
                    if snippet:
                        examples.append(snippet)
                # 前置：这句前面几条说了什么
                if span:
                    start = max(0, i - span)
                    ctx_rows = rows[start:i]
                    if ctx_rows:
                        before_occasions += 1
                        words: list[str] = []
                        for ctx in ctx_rows:
                            words.extend(_tokens(norm(ctx.get("text", ""))))
                        before_count.update(words)
                # 后置：60 秒内别人接的短句
                t0 = float(row.get("ts") or 0)
                uid = str(row.get("uid") or "")
                for nxt in rows[i + 1 :]:
                    gap = float(nxt.get("ts") or 0) - t0
                    if gap > self.window:
                        break
                    if str(nxt.get("uid") or "") == uid:
                        continue
                    reply = clean(nxt.get("text", ""))
                    if not reply or len(reply) > MAX_REPLY_LEN or reply == key:
                        continue
                    if reply in GENERIC_REPLY or reply in self._generic.get(gid, set()):
                        continue
                    if "@" in reply:
                        continue
                    slot = after[reply]
                    slot["n"] += 1
                    slot["uids"].add(str(nxt.get("uid") or ""))
                    slot["gap"].append(round(gap))
                # 元陈述：谁明说过这是梗（指令行不算：「/梗 猛攻区」是在下指令）
                raw = str(row.get("text") or "")
                if (
                    _META_RE.search(raw)
                    and 4 <= len(raw) <= 60
                    and not is_command(raw)
                    # 问句不算「明说」：「这个你拿什么跟我玩，是啥梗？」是在问，不是解释
                    and not re.search(r"[?？]|什么|啥|吗|怎么", raw)
                ):
                    meta.append(
                        {
                            "text": " ".join(raw.split())[:60],
                            "nick": str(row.get("nick") or ""),
                            "ts": int(row.get("ts") or 0),
                        }
                    )
            if hits:
                out["groups"][gid] = hits
                out["hits"] += hits

        out["speakers"] = len(speakers)
        out["quoted"] = quoted
        out["window"] = int(self.window)
        out["before"] = self._lift(before_count, before_occasions, key, min_before)
        out["after"] = sorted(
            (
                {"text": text, "n": slot["n"], "uids": len(slot["uids"])}
                for text, slot in after.items()
                if len(slot["uids"]) >= MIN_REPLY_SPEAKERS
            ),
            key=lambda r: (-r["n"], -r["uids"], r["text"]),
        )[: self.top]
        out["meta"] = meta[:3]
        # 用例优先挑「后面还有人接」的：那才看得出这句怎么用；
        # 都没有才退而求其次用只有前半截的。
        with_after = [e for e in examples if e.get("after")]
        out["examples"] = (with_after or examples)[:2]
        out["suggestion"] = self._suggest(out)
        out["tier"], out["scene"] = classify(out, subject)
        return out

    def _lift(
        self,
        count: Counter[str],
        occasions: int,
        key: str = "",
        min_before: int | None = None,
    ) -> list[dict[str, Any]]:
        """前置词：按「比平时高多少倍」排，不按次数排。

        只按次数会被「兄弟」「还行」这种万能词占满；lift = 出现在这句
        前面时的占比 ÷ 它在全群语料里的占比。
        和这句自己重字的词（「搞笑」之于「不要给我搞笑」）直接跳过。
        """
        if not occasions or not self._base_total:
            return []
        out: list[dict[str, Any]] = []
        for word, n in count.items():
            if key and (word in key or key in word):
                continue
            if n < (self.min_before if min_before is None else max(1, int(min_before))):
                continue
            p_cond = n / occasions
            p_all = self._base.get(word, 0) / self._base_total
            if p_all <= 0:
                continue
            lift = p_cond / p_all
            if lift < 1.2:  # 比平时还常见不到 1.2 倍，说明它跟这句没关系
                continue
            shown = min(lift, MAX_LIFT)
            out.append(
                {
                    "word": word,
                    "n": n,
                    "lift": round(shown, 2),
                    "score": round(shown * math.log1p(n), 2),
                }
            )
        out.sort(key=lambda r: (-r["score"], r["word"]))
        return out[: self.top]

    def _suggest(self, prof: dict[str, Any]) -> str:
        """拼一句「建议场景」：只拼有证据的部分，缺就少说。"""
        bits: list[str] = []
        before = [r["word"] for r in prof.get("before") or []][:2]
        after = prof.get("after") or []
        if before:
            bits.append("、".join(f"「{w}」" for w in before) + " 之后常说的一句")
        if after:
            reply = f"「{after[0]['text']}」"
            bits.append(f"群里常接 {reply}")
        if not bits:
            return ""
        where = ""
        groups = prof.get("groups") or {}
        if len(groups) >= 2:
            where = "（几个群都这么用）"
        return "；".join(bits) + where


def _snippet(rows: list[dict[str, Any]], i: int, span: float = 120.0) -> dict[str, Any] | None:
    """这条消息的前一句 / 它 / 后一句，凑成一个小用例。

    只收时间上挨得近的（默认 120 秒内），别把半小时后的闲聊拼进来；
    正文压短，注入时一屏能看完。
    """
    cur = rows[i]
    t0 = float(cur.get("ts") or 0)

    def _line(row: dict[str, Any]) -> dict[str, str] | None:
        text = " ".join(str(row.get("text") or "").split())[:30]
        if not _clean_example(text):
            return None
        return {
            "nick": str(row.get("nick") or row.get("uid") or "")[:12],
            "text": text,
        }

    before = None
    if i > 0 and t0 - float(rows[i - 1].get("ts") or 0) <= span:
        before = _line(rows[i - 1])
    after = None
    if i + 1 < len(rows) and float(rows[i + 1].get("ts") or 0) - t0 <= span:
        after = _line(rows[i + 1])
    line = _line(cur)
    if not line:
        return None
    return {"before": before, "line": line, "after": after}


# 用例里不要代码、指令、纯符号：这些给模型看只会带跑
_JUNK_EXAMPLE = re.compile(r"[{}\[\]<>=\\/]|^[/#！!。.]|^[0-9]+$")


def _clean_example(text: str) -> bool:
    body = str(text or "").strip()
    if not body or len(body) > 24:
        return False
    if _JUNK_EXAMPLE.search(body):
        return False
    # 至少得有一个汉字，纯表情/纯英文短语当例子没意义
    return any("\u4e00" <= ch <= "\u9fff" for ch in body)


# 场景词表：语料里常见的「事件」→ 人话里的场合。
# 统计能告诉你「这句常跟在哪些词后面」，但它给不出「显摆/打赢/离谱请求」这种
# 说法 —— 这一层是**人写的词表**，小、可审、可改，不调模型。
# 词表里没有的事件会退回统计式的说法，不硬编。
_EVENT_WORDS: dict[str, str] = {
    "出货": "出货",
    "大金": "出货",
    "大红": "出货",
    "爆率": "出货",
    "摸金": "出货",
    "保险": "出货",
    "满改": "出货",
    "击杀": "打赢",
    "打赢": "打赢",
    "拿下": "打赢",
    "清图": "打赢",
    "八杀": "打赢",
    "六套": "打赢",
    "付费留言": "离谱请求",
    "sc": "离谱请求",
    "握手": "离谱请求",
    "要求": "离谱请求",
}
# 从这句本身看它是哪种话：问你/你 … 是挑衅，带疑问就是问句，其余当口头禅
_CHALLENGE_RE = re.compile(r"你(?:拿|算|凭|有|能|会)")
_QUESTION_RE = re.compile(r"[?？]|不[不帅行会]|什么|怎么|哪|谁|吗|呢")


def scene_of(prof: dict[str, Any], subject: str = "") -> str:
    """画像 + 小词表 → 一句人话（「谁说、什么场合、什么类型」）。

    这就是把「我这三种判断」落成本地规则的地方：
      · 谁说的   ← 前置词里反复出现的那个名字（克里斯）
      · 什么场合 ← 前置词里命中 _EVENT_WORDS 的事件
      · 哪种话   ← 句式（问句 / 挑衅 / 口头禅）
    证据不够（没有事件词、没有名字）就返回空，让调用方退回统计式说法。
    """
    term = str(prof.get("term") or "").strip()
    if not term:
        return ""
    before = [str(r.get("word") or "") for r in (prof.get("before") or [])]
    event = ""
    for word in before:
        for key, kind in _EVENT_WORDS.items():
            if key in word.lower():
                event = kind
                break
        if event:
            break
    who = subject
    if not who:
        for word in before:
            if word and word.lower() not in _EVENT_WORDS and not _EVENT_WORDS.get(word.lower()):
                # 事件词表里没有的「名词」才有资格当主语；「小东西」这种称呼不算
                if len(word) >= 2 and word not in ("兄弟", "小东西", "老铁", "群友"):
                    who = word
                    break
    challenge = bool(_CHALLENGE_RE.search(term))
    question = bool(_QUESTION_RE.search(term))

    if event == "出货" and challenge:
        return f"{who}出货/打赢后的挑衅，是显摆不是真要比试" if who else "出货/打赢后的挑衅，是显摆不是真要比试"
    if event == "出货" and question:
        return f"{who}显摆时爱问的一句" if who else "出货/显摆时爱问的一句"
    if event == "打赢" and challenge:
        return f"{who}打赢之后的挑衅" if who else "打赢之后的挑衅"
    if event == "离谱请求":
        return "对方说离谱话时的口头禅"
    return ""


def classify(prof: dict[str, Any], subject: str = "") -> tuple[str, str]:
    """画像 → (档位, 一句话用法)。

    三档，对应「机器敢不敢替人下结论」：
    - hard：群里有人明说过这件事，直接采信；
    - soft：同一槽位反复出现 / 多人接过，采信但标明是统计；
    - weak：证据不够，**不产出场景**（宁可空着，也别写错污染所有回复）。
    """
    term = prof.get("term") or ""
    before = prof.get("before") or []
    after = prof.get("after") or []
    meta = prof.get("meta") or []
    top_before = before[0] if before else None
    top_after = after[0] if after else None

    if meta:
        sentence = str(meta[0].get("text") or "").strip()
        if sentence:
            return "hard", sentence[:40]
    # 能归类就先说人话（谁、什么场合、哪种话），比统计式说法更像人
    plain = scene_of(prof, subject)
    if plain:
        return "soft", plain
    # 保守：宁可少写，也别写错——写错会污染她所有同类回复。
    # 接法要「够多、多人、还得占相当比例」，偶然挨着的一句不算。
    hits = max(1, int(prof.get("hits") or 0) - int(prof.get("quoted") or 0))
    if (
        top_after
        and int(top_after.get("n") or 0) >= 3
        and int(top_after.get("uids") or 0) >= MIN_REPLY_SPEAKERS
        and int(top_after["n"]) / hits >= 0.25
    ):
        scene = f"群里常接「{top_after['text']}」"
        if top_before and int(top_before.get("n") or 0) >= 5 and float(top_before.get("lift") or 0) >= 5:
            scene = f"多在「{top_before['word']}」之后出现，" + scene
        return "soft", scene
    if (
        top_before
        and int(top_before.get("n") or 0) >= 5
        and float(top_before.get("lift") or 0) >= 5
        and int(top_before["n"]) / hits >= 0.3
    ):
        return "soft", f"多在「{top_before['word']}」之后出现"
    return "weak", ""


def render(prof: dict[str, Any]) -> str:
    """把证据摊成人能读的几段，CLI 直接打印。"""
    term = prof.get("term") or ""
    if not prof.get("hits"):
        return f"「{term}」：这份语料里没找到，别硬标。"
    lines = [
        f"「{term}」：{prof['hits']} 次 / {prof['speakers']} 人 / "
        f"{len(prof.get('groups') or {})} 个群"
    ]
    groups = prof.get("groups") or {}
    if groups:
        detail = "、".join(f"{gid}×{n}" for gid, n in sorted(groups.items(), key=lambda kv: -kv[1]))
        lines.append(f"· 分布：{detail}")
    if prof.get("meta"):
        lines.append("· 群里有人明说（最硬的证据）：")
        for row in prof["meta"]:
            lines.append(f"    「{row['text']}」（{row['nick']}）")
    if prof.get("before"):
        lines.append("· 出现之前常在聊：")
        for row in prof["before"]:
            lines.append(f"    {row['word']}（{row['n']} 次，lift {row['lift']}）")
    if prof.get("after"):
        lines.append(f"· 之后别人接的（{int(prof.get('window') or 60)} 秒内）：")
        for row in prof["after"]:
            lines.append(f"    「{row['text']}」×{row['n']}（{row['uids']} 人）")
    if prof.get("suggestion"):
        lines.append(f"· 建议场景：{prof['suggestion']}")
    lines.append("（这些只是证据，写不写回梗表由人定：python tools/memes_cli.py annotate ...）")
    return "\n".join(lines)
