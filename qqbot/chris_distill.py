"""评论蒸馏：raw → 候选（称呼/说法/高赞）。

只读 `data/chris_comments.db`，0 网络、0 token。产出写到同一库的
`candidate` 表，人工 `approve` 后才进 `memory.db` 的 knowledge。

三条线
------
- `term`  ：评论区反复出现的称呼/黑话（谁在说、跨了几个视频）；
- `claim` ：带「是/就是/叫」的说法（如「克里斯是区」），至少两个人说；
- `quote` ：高赞评论，当观众反应/名场面素材，不当事实。

过滤复用 `live_memes.classify_risk`：广告/骂人/黄腔/刷屏一律不进候选。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .chris_crawler import ChrisStore
from .live_memes import classify_risk
from .stopwords import STOPWORDS

log = logging.getLogger(__name__)

MIN_CHARS = 4
MAX_CHARS = 200
MAX_QUOTE_CHARS = 160

# 打包进 knowledge 的固定前缀/URL：URL 固定后同一 topic 的打包条目会原地更新，
# 不会每次多一行。
PACK_PREFIX = "B站评论区常刷的克里斯梗/笑话"
PACK_URL = "https://www.bilibili.com/"
PACK_SHORT_PREFIX = "B站评论区常刷的克里斯口头禅/短梗"
PACK_LONG_PREFIX = "B站评论区常刷的克里斯笑话/长梗"
PACK_SHORT_TOPIC = "克里斯口头禅"
PACK_LONG_TOPIC = "克里斯笑话"
PACK_JOKE_TOPIC = "克里斯笑话短句"
PACK_JOKE_PREFIX = "B站评论区好笑的短句"

# 这些是弹幕/评论区通用应答，不是这个圈的词
_BORING = {
    "主播", "关注", "来了", "打卡", "签到", "晚安", "早安", "谢谢", "感谢",
    "哈哈", "哈哈哈", "哈哈哈哈", "笑死", "就这", "牛", "牛逼", "好家伙",
    "可以", "确实", "真的", "离谱", "666", "888", "第一", "前排", "沙发",
    "省流", "课代表", "火钳刘明", "一键三连", "三连", "投币", "收藏",
    "好的", "好的谢谢", "好的，谢谢", "收到", "谢谢老板",
}
# 普通到不该当「说法」的宾语
_BORING_OBJECTS = {
    "什么", "谁", "不是", "一个", "这个", "那个", "这样", "那样", "真的",
    "假的", "对的", "错的", "好的", "可以", "不行", "牛逼", "离谱",
}
# 哪个评论区都会出现的词，不算这个圈的称呼
_GENERIC_TERMS = {
    "开播", "下播", "直播", "视频", "生活", "时候", "今天", "明天", "昨天",
    "现在", "真的", "感觉", "觉得", "可以", "没有", "知道", "喜欢", "直接",
    "然后", "还是", "就是", "这样", "一样", "东西", "问题", "游戏", "队友",
    "路人", "官方", "活动", "时间", "状态", "操作", "技术", "水平", "声音",
    "画面", "弹幕", "评论", "关注", "粉丝", "主播", "少爷", "哥们", "兄弟",
    "老师", "大佬", "老板", "朋友", "大家", "自己", "别人", "这边", "那边",
    "已经", "开始", "最后", "第一", "第二", "看到", "听见", "这种", "那种",
    "不会", "不是", "但是", "所以", "因为", "如果", "而且", "之后", "之前",
    "不要", "这么", "那么", "搞笑", "就播", "一种", "一直", "一定", "主要",
}
# 名字本身不是「称呼候选」，她已经知道
_ALIAS_TERMS = {"克里斯", "chris", "chris1i", "chris4i", "克里斯一", "up", "up主"}
_ALIASES = ("克里斯", "chris", "克里斯一", "chris1i", "chris4i", "主播", "up主", "up")

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_BV_RE = re.compile(r"bv[0-9a-z]{10}", re.I)
_AT_RE = re.compile(r"@[^\s，。！？:：]{1,20}")
_EMOJI_RE = re.compile(r"\[[^\[\]]{1,12}\]")
_SPACE_RE = re.compile(r"\s+")
# 评论区固定广告：电竞陪玩/代练的拉客话术，live_memes 的通用规则抓不到
_AD_RE = re.compile(
    r"kook|服务号|服务浩|点单|下单|客服|陪玩|代练|代肝|加群|工作室|回收|出售|"
    r"招打手|接单|帮刷|保险箱另谈|已赞借楼|出金请联系|"
    r"玩.{0,8}突围就来|就来.{1,16}电竞|xhm\d*",
    re.I,
)
_REPLY_PREFIX = re.compile(r"^回复\s*[:：]?\s*")
_CN_TOKEN_RE = re.compile(r"^[\u4e00-\u9fff]{2,6}$")
_CN_CHAR_RE = re.compile(r"^[\u4e00-\u9fff]$")
_EN_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]{2,9}$")
_CLAIM_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{1,12}?)(?:是|就是|叫|叫做)"
    r"([\u4e00-\u9fffA-Za-z0-9]{1,24})"
)
_CLAIM_PREFIX = re.compile(r"^(?:我觉得|我感觉|感觉|其实|真的|确实|应该|可能|听说|据说|好像|反正)")
# 主语里带这些词，说明匹配到的是「别让克里斯知道你是…」这种长句，不是 X 是 Y
_CLAIM_SUBJECT_NOISE = re.compile(r"知道|觉得|感觉|说|看|让|给|把|被|问|想|告诉|认为|以为")
_CLAIM_PRONOMINAL = {"我", "你", "他", "她", "它", "这", "那", "咱", "俺"}


def clean(text: Any) -> str:
    """去链接、BV 号、@、表情占位、「回复 :」前缀，压成一行。"""
    out = str(text or "")
    out = _REPLY_PREFIX.sub("", out)
    out = _URL_RE.sub("", out)
    out = _BV_RE.sub("", out)
    out = _AT_RE.sub("", out)
    out = _EMOJI_RE.sub("", out)
    out = out.replace("\u200b", "").replace("\ufeff", "")
    return _SPACE_RE.sub(" ", out).strip()


def usable(text: str) -> bool:
    """长度、内容、风险三道闸；通过才参与蒸馏。"""
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        return False
    if classify_risk(text) or _AD_RE.search(text):
        return False
    if text in _BORING:
        return False
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    alnum = len(re.findall(r"[A-Za-z0-9]", text))
    return cn >= 2 or alnum >= 4


def usable_term(text: str) -> bool:
    """称呼比整句短：允许 1~3 字的独立叫法（如「区」），但必须是纯词。"""
    if not 1 <= len(text) <= MAX_CHARS:
        return False
    if classify_risk(text) or _AD_RE.search(text):
        return False
    if text in _BORING or text in STOPWORDS:
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def fingerprint(kind: str, text: str) -> str:
    return hashlib.sha1(f"{kind}|{text}".encode("utf-8")).hexdigest()[:16]


def _bvid_url(bvid: str, rpid: int) -> str:
    if not bvid:
        return ""
    anchor = f"#reply{int(rpid)}" if rpid else ""
    return f"https://www.bilibili.com/video/{bvid}/{anchor}"


def load_rows(store: ChrisStore) -> list[dict[str, Any]]:
    """所有评论（含楼中楼）。楼中楼里也常有梗，别只挖一级。"""
    rows = store.db.execute(
        """SELECT c.rpid, c.aid, c.parent, c.ctime, c.message, c.like,
                  c.uid_hash, v.bvid
           FROM comment c JOIN video v ON v.aid = c.aid
           WHERE c.message IS NOT NULL AND c.message != ''"""
    ).fetchall()
    return [dict(r) for r in rows]


def _iter_clean(rows: Iterable[dict[str, Any]]) -> Iterable[tuple[dict[str, Any], str]]:
    for row in rows:
        text = clean(row["message"])
        if usable(text):
            yield row, text


def _iter_clean_terms(
    rows: Iterable[dict[str, Any]]
) -> Iterable[tuple[dict[str, Any], str]]:
    for row in rows:
        text = clean(row["message"])
        if usable_term(text):
            yield row, text


def _tokens(text: str) -> list[str]:
    import jieba

    out: list[str] = []
    for token in jieba.lcut(text):
        token = token.strip().lower()
        if _CN_TOKEN_RE.match(token) or _EN_TOKEN_RE.match(token):
            if (
                token not in STOPWORDS
                and token not in _BORING
                and token not in _GENERIC_TERMS
                and token not in _ALIAS_TERMS
            ):
                out.append(token)
    # 单字称呼（比如「区」）：只有整条评论就是这一个字时才算，避免
    # 把「人/好/大」这类常见单字全捞进来。
    whole = text.strip().lower()
    if _CN_CHAR_RE.match(whole) and whole not in STOPWORDS and whole not in _GENERIC_TERMS:
        out.append(whole)
    return out


def term_candidates(
    rows: list[dict[str, Any]], min_users: int = 3, min_videos: int = 2, limit: int = 60
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"users": set(), "videos": set(), "hits": 0, "best": None}
    )
    for row, text in _iter_clean_terms(rows):
        for token in _tokens(text):
            item = stats[token]
            item["users"].add(row["uid_hash"])
            item["videos"].add(row["bvid"])
            item["hits"] += 1
            if item["best"] is None or int(row["like"] or 0) > int(item["best"]["like"] or 0):
                item["best"] = row

    out: list[dict[str, Any]] = []
    for token, item in stats.items():
        users = len(item["users"])
        videos = len(item["videos"])
        if users < min_users or videos < min_videos or item["hits"] < min_users:
            continue
        # 单字称呼噪声大，门槛抬高：至少 4 个人、2 个视频
        if len(token) == 1 and (users < 4 or videos < 2):
            continue
        best = item["best"] or {}
        score = users * 2.0 + videos * 1.5 + (item["hits"] ** 0.5)
        out.append(
            {
                "kind": "term",
                "text": token,
                "meaning": "",
                "evidence": json.dumps(
                    {
                        "hits": item["hits"],
                        "users": users,
                        "videos": videos,
                        "example": clean(best.get("message", ""))[:100],
                    },
                    ensure_ascii=False,
                ),
                "bvid": best.get("bvid", ""),
                "rpid": int(best.get("rpid") or 0),
                "url": _bvid_url(str(best.get("bvid") or ""), int(best.get("rpid") or 0)),
                "score": round(score, 3),
                "seen_videos": videos,
                "seen_users": users,
                "fingerprint": fingerprint("term", token),
            }
        )
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def claim_candidates(
    rows: list[dict[str, Any]], min_users: int = 2, limit: int = 60
) -> list[dict[str, Any]]:
    """X 是/就是/叫 Y 的说法。固定句式的梗交给 meme_candidates。"""
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"users": set(), "videos": set(), "hits": 0, "best": None}
    )
    for row, text in _iter_clean(rows):
        for match in _CLAIM_RE.finditer(text):
            subject = _CLAIM_PREFIX.sub("", match.group(1)).strip()
            obj = match.group(2).strip()
            if not subject or not obj or obj in _BORING_OBJECTS:
                continue
            if subject in _CLAIM_PRONOMINAL or _CLAIM_SUBJECT_NOISE.search(subject):
                continue
            joined = f"{subject}是{obj}"
            if not any(alias in joined.lower() for alias in _ALIASES):
                continue
            item = stats[joined]
            item["users"].add(row["uid_hash"])
            item["videos"].add(row["bvid"])
            item["hits"] += 1
            if item["best"] is None or int(row["like"] or 0) > int(item["best"]["like"] or 0):
                item["best"] = row

    out: list[dict[str, Any]] = []
    for claim, item in stats.items():
        users = len(item["users"])
        if users < min_users or item["hits"] < min_users:
            continue
        best = item["best"] or {}
        score = users * 3.0 + len(item["videos"]) + (item["hits"] ** 0.5)
        out.append(
            {
                "kind": "claim",
                "text": claim,
                "meaning": "",
                "evidence": json.dumps(
                    {
                        "hits": item["hits"],
                        "users": users,
                        "videos": len(item["videos"]),
                        "example": clean(best.get("message", ""))[:100],
                    },
                    ensure_ascii=False,
                ),
                "bvid": best.get("bvid", ""),
                "rpid": int(best.get("rpid") or 0),
                "url": _bvid_url(str(best.get("bvid") or ""), int(best.get("rpid") or 0)),
                "score": round(score, 3),
                "seen_videos": len(item["videos"]),
                "seen_users": users,
                "fingerprint": fingerprint("claim", claim),
            }
        )
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def _shingles(text: str) -> set[str]:
    """字符二元组，用来做近重复判断（不依赖分词）。"""
    clean_text = re.sub(r"[\W_]+", "", text).lower()
    return {clean_text[i : i + 2] for i in range(max(0, len(clean_text) - 1))}


def _contains_variant(a: str, b: str) -> bool:
    """「不要给我搞笑」和「你个小东西不要给我搞笑」是包含关系，别合并——
    短的太泛，长的才是群友实际在刷的那句。"""
    ca = re.sub(r"[\W_]+", "", a).lower()
    cb = re.sub(r"[\W_]+", "", b).lower()
    return (ca in cb or cb in ca) and abs(len(ca) - len(cb)) >= 4


def _near_duplicate(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    if inter >= 5:  # 「醒了就播」和「睡醒了就开播」共享一大截，算同一家族
        return True
    union = len(a | b)
    return union > 0 and inter / union >= 0.7


def usable_meme(text: str) -> bool:
    """固定句式的梗：比「说法」长，比整段小作文短。"""
    core = re.sub(r"\[[^\[\]]{1,30}\]", "", text)
    if len(re.findall(r"[\u4e00-\u9fff]", core)) < 4:
        return False
    return (
        6 <= len(text) <= 120
        and not classify_risk(text)
        and not _AD_RE.search(text)
        and text not in _BORING
    )


def meme_candidates(
    rows: list[dict[str, Any]],
    min_users: int = 2,
    min_hits: int = 3,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """反复出现的整句/句式（「别让克里斯知道你是烂仔」「小东西不要给我搞笑」）。

    和 claim 的区别：不要求「X 是 Y」，只要同一句被不同的人反复刷；
    近重复的变体（烂仔/烂崽）按字符二元组合并成一条。
    """
    from collections import Counter

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        text = clean(row["message"])
        if not usable_meme(text):
            continue
        key = re.sub(r"[\W_]+", "", text).lower()
        if len(key) < 6:
            continue
        item = groups.setdefault(
            key,
            {"display": Counter(), "variants": Counter(), "users": set(),
             "videos": set(), "hits": 0, "max_like": 0, "best": None},
        )
        item["display"][text] += 1
        item["variants"][text] += 1
        item["users"].add(row["uid_hash"])
        item["videos"].add(row["bvid"])
        item["hits"] += 1
        if int(row["like"] or 0) > int(item["max_like"]):
            item["max_like"] = int(row["like"] or 0)
            item["best"] = row

    merged: list[dict[str, Any]] = []
    # 只合并最常出现的一批：10 万条评论里唯一句太多，全量两两比较会卡死。
    # 先不按 min_hits 砍：变体（烂仔/烂崽）往往只出现一次，砍了就合不进来；
    # 合并完再统一按人数/次数筛。
    ranked = sorted(groups.items(), key=lambda kv: -kv[1]["hits"])[:2000]
    for _key, item in ranked:
        display = item["display"].most_common(1)[0][0]
        shingles = _shingles(display)
        target = None
        for existing in merged:
            if _near_duplicate(shingles, existing["shingles"]) and not _contains_variant(
                display, existing["display"]
            ):
                target = existing
                break
        if target is None:
            merged.append({**item, "display": display, "shingles": shingles})
        else:
            target["users"] |= item["users"]
            target["videos"] |= item["videos"]
            target["hits"] += item["hits"]
            target["variants"].update(item["variants"])
            if item["max_like"] > target["max_like"]:
                target["max_like"] = item["max_like"]
                target["best"] = item["best"]

    out: list[dict[str, Any]] = []
    for item in merged:
        users = len(item["users"])
        videos = len(item["videos"])
        if users < min_users or item["hits"] < min_hits:
            continue
        best = item["best"] or {}
        text = item["display"][:120]
        variants = [v for v, _ in item["variants"].most_common(4) if v != text][:3]
        score = (
            users * 2.0 + videos * 1.5 + (item["hits"] ** 0.5)
            + math.log1p(item["max_like"])
        )
        out.append(
            {
                "kind": "meme",
                "text": text,
                "meaning": "",
                "evidence": json.dumps(
                    {
                        "hits": item["hits"],
                        "users": users,
                        "videos": videos,
                        "max_like": item["max_like"],
                        "variants": variants,
                    },
                    ensure_ascii=False,
                ),
                "bvid": best.get("bvid", ""),
                "rpid": int(best.get("rpid") or 0),
                "url": _bvid_url(str(best.get("bvid") or ""), int(best.get("rpid") or 0)),
                "score": round(score, 3),
                "seen_videos": videos,
                "seen_users": users,
                "fingerprint": fingerprint("meme", text),
            }
        )
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def quote_candidates(
    rows: list[dict[str, Any]], min_like: int = 10, limit: int = 50
) -> list[dict[str, Any]]:
    seen: list[set[str]] = []
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: -int(r["like"] or 0)):
        like = int(row["like"] or 0)
        if like < min_like:
            break
        text = clean(row["message"])
        if not usable(text):
            continue
        shingles = _shingles(text)
        # 同一条复制粘贴的变体只留赞最高的那条（cluster：近重复合并）
        if any(_near_duplicate(shingles, other) for other in seen):
            continue
        seen.append(shingles)
        body = text[:MAX_QUOTE_CHARS]
        out.append(
            {
                "kind": "quote",
                "text": body,
                "meaning": "",
                "evidence": json.dumps(
                    {"like": like, "ctime": int(row["ctime"] or 0)}, ensure_ascii=False
                ),
                "bvid": row["bvid"],
                "rpid": int(row["rpid"]),
                "url": _bvid_url(str(row["bvid"]), int(row["rpid"])),
                "score": float(like),
                "seen_videos": 1,
                "seen_users": 1,
                "fingerprint": fingerprint("quote", body),
            }
        )
        if len(out) >= limit:
            break
    return out


def _prune_stale(store: ChrisStore, kind: str, fingerprints: set[str]) -> None:
    """这一轮没产出的旧候选（还没审的）删掉，别让上一版规则留下的噪声一直躺着。"""
    if fingerprints:
        placeholders = ",".join("?" * len(fingerprints))
        store.db.execute(
            f"DELETE FROM candidate WHERE kind=? AND status='candidate' "
            f"AND fingerprint NOT IN ({placeholders})",
            [kind, *sorted(fingerprints)],
        )
    else:
        store.db.execute(
            "DELETE FROM candidate WHERE kind=? AND status='candidate'", (kind,)
        )
    store.db.commit()


def distill(
    store: ChrisStore,
    kinds: Iterable[str] = ("term", "meme", "claim", "quote"),
    top: int = 60,
) -> dict[str, int]:
    rows = load_rows(store)
    kinds = tuple(kinds)
    result: dict[str, int] = {}
    if "term" in kinds:
        found = term_candidates(rows, limit=top)
        store.upsert_candidates(found)
        _prune_stale(store, "term", {r["fingerprint"] for r in found})
        result["term"] = len(found)
    if "meme" in kinds:
        found = meme_candidates(rows, limit=top)
        store.upsert_candidates(found)
        _prune_stale(store, "meme", {r["fingerprint"] for r in found})
        result["meme"] = len(found)
    if "claim" in kinds:
        found = claim_candidates(rows, limit=top)
        store.upsert_candidates(found)
        _prune_stale(store, "claim", {r["fingerprint"] for r in found})
        result["claim"] = len(found)
    if "quote" in kinds:
        found = quote_candidates(rows, limit=top)
        store.upsert_candidates(found)
        _prune_stale(store, "quote", {r["fingerprint"] for r in found})
        result["quote"] = len(found)
    return result


def knowledge_text(row: dict[str, Any]) -> str:
    kind = row["kind"]
    text = str(row["text"]).strip()
    if kind == "term":
        body = (
            f"B站评论区对克里斯的称呼/口头禅：「{text}」"
            f"（{int(row['seen_users'])} 人提到、{int(row['seen_videos'])} 个视频）"
        )
    elif kind == "claim":
        body = f"B站评论区关于克里斯的说法：{text}"
    elif kind == "catchphrase":
        body = (
            f"B站视频里克里斯常说的口头禅：「{text}」"
            f"（{int(row['seen_videos'])} 个视频里出现）"
        )
    elif kind == "meme":
        body = f"B站评论区常刷的克里斯梗/笑话：{text}"
        try:
            evidence = json.loads(row.get("evidence") or "{}")
        except ValueError:
            evidence = {}
        variants = [
            v for v in (evidence.get("variants") or [])
            if v and v != text
        ][:2]
        if variants:
            body += "（变体：" + "；".join(variants) + "）"
    elif kind == "quote":
        body = f"B站评论区高赞：{text}"
    else:
        body = text
    return f"{body}（观众说法，未必真）"


def approve_candidate(
    store: ChrisStore,
    cid: int,
    memory_path: Path,
    topic: str = "克里斯",
    ttl_days: int = 180,
) -> dict[str, Any]:
    """把一条候选写进 memory.db 的 knowledge；写成功才标 approved。"""
    row = store.get_candidate(cid)
    if not row:
        return {"ok": False, "error": "没有这条候选"}
    if row["status"] == "approved":
        return {"ok": True, "already": True, "id": cid}
    if row["status"] == "rejected":
        return {"ok": False, "error": "这条已被否决，先改回 candidate"}

    # 梗/笑话/高赞句/口头禅的唯一真源是 candidate 表：chris_lore 和 pack 都读它。
    # 再往 knowledge 写一份只会被同 topic 的合集挤掉，纯属双写。
    if row["kind"] in ("joke", "meme", "quote", "catchphrase"):
        store.set_candidate_status(cid, "approved", pinned=True)
        return {
            "ok": True,
            "id": cid,
            "kind": row["kind"],
            "text": row["text"],
            "knowledge": False,
        }

    from .memory import Memory

    mem = Memory(Path(memory_path))
    try:
        ok = mem.add_knowledge(
            topic=topic,
            text=knowledge_text(row),
            url=str(row.get("url") or ""),
            source="bili_comment",
            ttl_days=ttl_days,
        )
    finally:
        mem.close()
    if ok:
        store.set_candidate_status(cid, "approved", pinned=True)
    return {"ok": bool(ok), "id": cid, "kind": row["kind"], "text": row["text"]}


def _build_pack_text(
    head: str,
    items: list[dict[str, Any]],
    top: int,
    max_chars: int,
    max_item_chars: int = 160,
) -> tuple[str, list[int]]:
    chosen: list[dict[str, Any]] = []
    for row in items:
        body = str(row["text"])
        force = bool(row.get("pinned")) and row.get("kind") == "catchphrase"
        # 口头禅本来就只有两三个字（「我靠」），不能用整句梗的门槛；
        # 人工钉住的（如「握手握手」）连刷屏判定也跳过——那是他真在反复说。
        ok = (
            bool(body.strip())
            if force
            else (usable_term(body) if row.get("kind") == "catchphrase" else usable_meme(body))
        )
        if not ok or len(body) > max_item_chars:
            continue
        shingles = _shingles(body)
        if not force and any(
            _near_duplicate(shingles, other["shingles"])
            and not _contains_variant(body, other["text"])
            for other in chosen
        ):
            continue
        chosen.append(
            {"text": body, "shingles": shingles, "id": int(row["id"]),
             "kind": str(row.get("kind") or ""), "pinned": force}
        )
        if len(chosen) >= top * 3:
            break

    plain = {
        item["id"]: re.sub(r"[\W_]+", "", item["text"]).lower()
        for item in chosen
    }
    kept = [
        item for item in chosen
        if item["kind"] == "catchphrase" or not any(
            plain[item["id"]] != plain[other["id"]]
            and plain[item["id"]] in plain[other["id"]]
            for other in chosen
            if other["kind"] != "catchphrase"
        )
    ]

    text = head
    picked: list[int] = []
    for item in kept:
        piece = f"{len(picked) + 1}) {item['text']}；"
        if len(text) + len(piece) > max_chars:
            break
        text += piece
        picked.append(item["id"])
        if len(picked) >= top:
            break
    return text, picked


def pack_candidates(
    store: ChrisStore,
    memory_path: Path,
    top: int = 20,
    topic: str = "",
    ttl_days: int = 180,
    max_chars: int = 395,
    max_item_chars: int = 160,
) -> dict[str, Any]:
    """把梗和高赞句子拆成「短梗包 + 长笑话包」两条 knowledge。

    `knowledge_for` 对同一个 topic 只回一条，所以短梗和长笑话必须用不同
    topic，聊天侧一次取两条，才能既保留「醒了就播」又保留「-4000万的故事」。
    人工 approve 的排最前，自动候选补空。
    """
    memes = store.list_candidates(kind="meme", status="candidate", limit=max(top * 4, top))
    memes.sort(key=lambda r: -float(r.get("score") or 0))
    quotes = store.list_candidates(kind="quote", status="candidate", limit=max(top * 6, top))
    catchphrases = store.list_candidates(
        kind="catchphrase", status="candidate", limit=max(top * 2, top)
    )
    catchphrases += store.list_candidates(
        kind="catchphrase", status="approved", limit=max(top * 2, top)
    )
    catchphrases.sort(key=lambda r: -float(r.get("score") or 0))
    approved = [
        r for r in store.list_candidates(status="approved", limit=400)
        if r["kind"] in ("meme", "quote", "joke")
    ]
    approved.sort(
        key=lambda r: (0 if r.get("pinned") else 1, -int(r.get("reviewed_at") or 0))
    )
    if not memes and not quotes and not approved:
        return {"ok": False, "error": "没有梗/高赞候选；先跑 distill"}

    pinned_catch = [
        c for c in catchphrases if c.get("pinned") and len(str(c["text"])) <= 45
    ]
    # 字幕里真有视频支撑的排前面，群友合订本（seen_videos=0）跟在后面
    pinned_catch.sort(
        key=lambda r: (
            0 if int(r.get("seen_videos") or 0) > 0 else 1,
            -float(r.get("score") or 0),
        )
    )
    jokes = store.list_candidates(kind="joke", status="candidate", limit=200)
    jokes += store.list_candidates(kind="joke", status="approved", limit=200)
    jokes.sort(key=lambda r: -float(r.get("score") or 0))
    short_jokes = [j for j in jokes if len(str(j["text"])) <= 45][:8]
    long_jokes = [j for j in jokes if 45 < len(str(j["text"])) <= max_item_chars][:6]

    short_items = pinned_catch
    short_items += [
        a for a in approved
        if a.get("pinned") and a.get("kind") != "catchphrase"
        and len(str(a["text"])) <= 45
    ][:4]
    if not pinned_catch:
        short_items += [c for c in catchphrases if len(str(c["text"])) <= 45][:8]
    long_items = [a for a in approved if 45 < len(str(a["text"])) <= max_item_chars]
    long_items = long_items[:2] + long_jokes + long_items[2:]
    short_items += short_jokes[:3]
    short_items += [m for m in memes if len(str(m["text"])) <= 45][:2]
    long_items += [m for m in memes if 45 < len(str(m["text"])) <= max_item_chars][:5]
    short_items += [q for q in quotes if len(str(q["text"])) <= 45]
    long_items += [q for q in quotes if 45 < len(str(q["text"])) <= max_item_chars][:8]

    short_text, short_ids = _build_pack_text(
        PACK_SHORT_PREFIX + "（观众说法，未必真）：",
        short_items, top, max_chars, max_item_chars,
    )
    long_text, long_ids = _build_pack_text(
        PACK_LONG_PREFIX + "（观众说法，未必真）：",
        long_items, max(top // 2, 4), max_chars, max_item_chars,
    )
    joke_text, joke_ids = _build_pack_text(
        PACK_JOKE_PREFIX + "（观众说法，未必真）：",
        short_jokes, 8, max_chars, max_item_chars,
    )
    if not short_ids and not long_ids and not joke_ids:
        return {"ok": False, "error": "没有可打包的短句"}

    # 先清掉上一版打包（旧文案/旧来源/旧 topic），再写新的。
    conn = sqlite3.connect(str(memory_path))
    try:
        conn.execute(
            "DELETE FROM knowledge WHERE source IN ('bili_comment', 'bili_comment_pack') "
            "AND text LIKE ?",
            ("%（观众说法，未必真）：%",),
        )
        conn.commit()
    finally:
        conn.close()

    from .memory import Memory

    written: list[int] = []
    mem = Memory(Path(memory_path))
    try:
        for entry_topic, body, ids in (
            (PACK_SHORT_TOPIC, short_text, short_ids),
            (PACK_LONG_TOPIC, long_text, long_ids),
            (PACK_JOKE_TOPIC, joke_text, joke_ids),
        ):
            if not ids:
                continue
            ok = mem.add_knowledge(
                topic=entry_topic,
                text=body,
                url=PACK_URL,
                source="bili_comment_pack",
                ttl_days=ttl_days,
            )
            if ok:
                written.extend(ids)
    finally:
        mem.close()
    if not written:
        return {"ok": False, "error": "knowledge 写入失败"}
    for cid in written:
        store.set_candidate_status(cid, "approved")
    return {
        "ok": True,
        "count": len(written),
        "text": (short_text + "\n" + long_text).strip(),
    }
