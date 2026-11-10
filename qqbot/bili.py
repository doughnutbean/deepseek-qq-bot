"""B 站链接的卡片 + 封面 + 档案（0 token；网络全部本地，超时就当没查到）。

三层，一层比一层重
------------------
1. **卡片**：标题 / UP / 时长 / 播放 / 分区 / 合集 / 简介（`view` 接口，1 次 HTTP）。
2. **封面**：`view` 里的 `pic` 下载一张图给她看（vision，按图计费；可关）。
3. **档案**：官方 AI 总结 + 分段时间轴 + AI 字幕，落盘 7 天（要小号 cookie，
   见 `bili_archive.py`）。这一层**在后台跑**，不挡回复；下一轮同一视频再被问
   就直接从档案里答，不用重抓。

三条硬边界
----------
- **绝不影响回复**：查卡片 4 秒硬超时，封面 3 秒，档案后台跑；失败一律当成没查到。
- **绝不把网址递回去**：她回复里的链接会被 QQ 风控，卡片里也不放 URL。
- **绝不装作看过**：卡片只说卡片；有了档案才说「字幕/总结里看到」；封面不算画面。

为什么只认 bilibili 系域名
-------------------------
``_load`` 里有一道 ``is_bili`` 检查：链接是群友给的，等于外部输入，不能拿来
当通用 HTTP 客户端去敲内网地址（SSRF）。封面和字幕地址同理（`is_media_url`）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .bili_api import BiliApi, ensure_buvid3, is_media_url
from .bili_archive import (
    DEFAULT_MAX_CHARS,
    ArchiveStore,
    archive_knowledge_text,
    build_archive,
    fmt_ts,
    format_archive,
)
from .bili_frames import (
    CELLS,
    compose_zoom,
    format_stamp,
    grab_multi,
    load_kept,
    select_window,
)
from .linkcard import is_bili, parse_bili_id
from .media import image_block

log = logging.getLogger(__name__)

# 直播页不是视频页：没有 BV/av 号，`view` 接口查不到。`web_fetch` 要单独
# 走直播接口，否则只会回「那条B站链接没打开」。
_LIVE_ROOM_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d{1,12})", re.I)


def live_room_of(url: str) -> str:
    match = _LIVE_ROOM_RE.search(url or "")
    return match.group(1) if match else ""


VIEW_API = "https://api.bilibili.com/x/web-interface/view"

# 不带 Referer 时 B 站偶尔会回 -352（风控）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 查询失败也记一下短缓存：坏链接被反复贴时，别每次都去敲一遍
FAIL_CACHE_SECONDS = 300


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_duration(seconds: int) -> str:
    return fmt_ts(seconds)


def format_count(count: int) -> str:
    value = max(0, _int(count))
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return str(value)


def normalize_view(payload: Any) -> dict[str, Any] | None:
    """把 /x/web-interface/view 的返回压成一张卡。code 非 0 或没有 bvid 就是 None。"""
    if not isinstance(payload, dict):
        return None
    if _int(payload.get("code")) != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("bvid"):
        return None
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    staff = [
        str((item or {}).get("name") or "").strip()
        for item in (data.get("staff") or [])
        if isinstance(item, dict)
    ]
    season = data.get("ugc_season") if isinstance(data.get("ugc_season"), dict) else {}
    first_page = pages[0] if pages and isinstance(pages[0], dict) else {}
    return {
        "bvid": str(data.get("bvid") or ""),
        "aid": str(data.get("aid") or ""),
        "cid": str(first_page.get("cid") or ""),
        "title": str(data.get("title") or "").strip(),
        "desc": str(data.get("desc") or "").strip(),
        "pic": str(data.get("pic") or "").strip(),
        "tname": str(data.get("tname") or "").strip(),
        "season": str(season.get("title") or "").strip(),
        "staff": [name for name in staff if name][:3],
        "owner": str(owner.get("name") or "").strip(),
        "owner_mid": str(owner.get("mid") or ""),
        "duration": _int(data.get("duration")),
        "pages": len(pages),
        "parts": [str((p or {}).get("part") or "") for p in pages if isinstance(p, dict)][:5],
        "view": _int(stat.get("view")),
        "like": _int(stat.get("like")),
        "danmaku": _int(stat.get("danmaku")),
        "reply": _int(stat.get("reply")),
        "pubdate": _int(data.get("pubdate")),
    }


def _title_of(info: dict[str, Any]) -> str:
    """标题带书名号，但别套娃：《鸣潮》世界观PV 不该变成《《鸣潮》世界观PV》。"""
    body = str(info.get("title") or "").strip()[:60] or "（没标题）"
    if "《" in body:
        return body
    return f"《{body}》"


def format_note(
    info: dict[str, Any],
    max_chars: int = 200,
    archive: dict[str, Any] | None = None,
    archive_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """一张卡 → 给模型看的两三句。不含 URL，长度封顶。有档案就一起给。"""
    body = f"标题{_title_of(info)}"
    bits: list[str] = []
    if info.get("owner"):
        bits.append(f"UP：{str(info['owner'])[:20]}")
    if info.get("tname"):
        bits.append(f"分区：{str(info['tname'])[:12]}")
    if info.get("duration"):
        bits.append(f"时长 {format_duration(info['duration'])}")
    if _int(info.get("pages")) > 1:
        bits.append(f"分P {_int(info['pages'])}")
    if info.get("view"):
        bits.append(f"播放 {format_count(info['view'])}")
    if info.get("reply"):
        bits.append(f"评论 {format_count(info['reply'])}条")
    if bits:
        body += "，" + "，".join(bits)
    if info.get("season"):
        body += f"；合集《{str(info['season'])[:24]}》"
    if info.get("staff"):
        body += "；参与：" + "、".join(str(name)[:12] for name in info["staff"])
    desc = " ".join(str(info.get("desc") or "").split()).rstrip("。．.!！?？…，,、；;")
    if desc:
        room = max(0, int(max_chars) - len(body) - 40)
        if room >= 20:
            body += f"；UP 写的简介：{desc[:room]}"

    archive_text = format_archive(archive, archive_chars)
    if archive_text:
        return f"（刚才那条消息里是个B站视频。卡片：{body}。{archive_text}）"
    return (
        f"（刚才那条消息里是个B站视频，你只看到这些：{body}。"
        "简介是UP自己写的，不代表视频内容；你没看过画面和弹幕，别装作看过）"
    )


def failure_note() -> str:
    return "（那条B站链接没打开，你只知道标题，别猜里面的内容）"


def cover_note() -> str:
    """封面图进 prompt 时补一句：那是封面，不是视频画面。"""
    return "（上面那张是视频封面。封面不算视频内容，别把封面里的东西说成视频里发生的）"


def asks_about_video(text: str) -> bool:
    """这句是不是在追问视频内容（本地关键词，0 token，不靠模型判断）。"""
    blob = (text or "").strip()
    if not blob:
        return False
    return any(
        word in blob
        for word in (
            "评论",
            "弹幕",
            "字幕",
            "总结",
            "讲了什么",
            "说了什么",
            "看见什么",
            "看见啥",
            "画面",
        )
    )


def asks_about_frames(text: str) -> bool:
    """这句是不是在问画面（本地关键词，0 token）。"""
    blob = (text or "").strip()
    if not blob:
        return False
    return any(
        word in blob
        for word in ("画面", "长什么样", "截图", "镜头", "看起来", "样子", "画得")
    )


# 「03:10 / 3:10 / 1:03:10 / 3分10秒 / 95秒」——时间点都由本地正则认，0 token
_CLOCK_RE = re.compile(
    r"(\d{1,3})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?"
)
_MINUTE_RE = re.compile(r"(\d{1,3})\s*分(?:钟)?\s*(?:(\d{1,2})\s*秒)?")
_SECOND_RE = re.compile(r"(\d{1,3})\s*秒")
# 带时间点、又带这些词，才当成「要看某一段」，不是随口提了个时间
_ZOOM_WORDS = (
    "那段",
    "这段",
    "这里",
    "这边",
    "凑近",
    "放大",
    "仔细",
    "那一刻",
    "这帧",
    "那一帧",
    "画面",
    "截图",
)


def _times_in(text: str) -> list[float]:
    """抠出句子里的时间点（秒）。先认 mm:ss，再从剩下的文本里认「X分Y秒」。"""
    blob = str(text or "")
    times: list[float] = []
    spans: list[tuple[int, int]] = []

    def taken(start: int) -> bool:
        return any(left <= start < right for left, right in spans)

    for match in _CLOCK_RE.finditer(blob):
        first, second, third = match.groups()
        if third is not None:  # h:mm:ss
            value = int(first) * 3600 + int(second) * 60 + int(third)
        else:  # mm:ss
            value = int(first) * 60 + int(second)
        times.append(float(value))
        spans.append(match.span())
    for match in _MINUTE_RE.finditer(blob):
        if taken(match.start()):
            continue
        value = int(match.group(1)) * 60 + int(match.group(2) or 0)
        times.append(float(value))
        spans.append(match.span())
    for match in _SECOND_RE.finditer(blob):
        if taken(match.start()):
            continue
        times.append(float(match.group(1)))
    return times


def frame_window(text: str) -> tuple[float, float] | None:
    """这句在说视频的哪一段？认不出返回 None。

    - 「03:10-04:00」→ 那 50 秒；
    - 「03:10 那段」→ 03:08 起 45 秒（稍微往前带一点，防止正好是切点）；
    - 一段最长 180 秒：再长就该看整段分镜，不是放大图。
    """
    times = sorted(_times_in(text))
    if not times:
        return None
    if len(times) >= 2:
        start, end = times[0], times[1]
        if end - start < 5:
            end = start + 45
        return (start, min(end, start + 180))
    start = max(0.0, times[0] - 2)
    return (start, start + 45)


def asks_about_zoom(text: str) -> bool:
    """这句是不是「凑近看某一段」（有具体时间点 + 看片段的口吻）。"""
    blob = (text or "").strip()
    if not blob or frame_window(blob) is None:
        return False
    return any(word in blob for word in _ZOOM_WORDS)


_USER_WORDS = ("up主", "主播", "是谁", "粉丝", "关注", "主页", "空间", "作者", "这个人", "开播", "直播间")
_VIDEO_WORDS = (
    "视频",
    "切片",
    "bv",
    "播放",
    "合集",
    "这期",
    "那期",
    "回放",
    "投稿",
    "最新",
    "新视频",
    "最近发",
)


def guess_search_kind(query: str) -> str:
    """本地猜搜视频还是 UP 主；看不出来就 'auto'（两个都搜）。"""
    blob = (query or "").lower()
    user = any(word in blob for word in _USER_WORDS)
    video = any(word in blob for word in _VIDEO_WORDS)
    if user and not video:
        return "user"
    if video and not user:
        return "video"
    return "auto"


_NOISE_WORDS = (
    "帮我看看",
    "帮我找找",
    "帮我搜搜",
    "帮我看",
    "帮我找",
    "帮我",
    "给我看看",
    "给我找",
    "搜一下",
    "搜索一下",
    "搜索",
    "找一下",
    "找找",
    "看看",
    "看一下",
    "看下",
    "瞧瞧",
    "记录一下",
    "数一数",
    "数一下",
    "数数",
    "算一算",
    "统计一下",
    "多少句",
    "几句",
    "说了多少",
    "是谁",
    "是什么",
    "什么样",
    "这个",
    "那个",
    "他的",
    "她的",
    "你的",
    "视频",
    "切片",
    "up主",
    "b站",
    "一下",
    "最新",
    "最近",
)

# 认「这是哪个 UP」时忽略的词：它们是意图/品类，不是人名。
_UP_STOPWORDS = {
    "最新",
    "最近",
    "更新",
    "新视频",
    "新投稿",
    "投稿",
    "视频",
    "切片",
    "直播",
    "直播回放",
    "回放",
    "是谁",
    "是什么",
    "口头禅",
    "看看",
    "找找",
}

# 群里的「克里斯」= 配置里的 CHRIS_MID（和 live_tools 同一组别名）。
_CHRIS_ALIASES = {
    "克里斯",
    "chris",
    "chris1i",
    "chris4i",
    "克里斯一",
    "克区",
    "克神",
    "克宝",
}

# 从一句话里猜 UP 时，粉丝太少的不认：可能只是同名路人，认错了整段结果都歪。
_MIN_UP_FANS = 1000

# 话里带这些词就当在问「新的」：模型不一定记得传 order=new，本地补上。
_NEWEST_WORDS = (
    "最新",
    "最近",
    "新视频",
    "新投稿",
    "更新",
    "刚发",
    "上场",
    "上一场",
    "刚才",
    "今天",
    "昨天",
)

# 只有话里明显在说「人」时才把搜索换成 UP 投稿：不然「影之刃零 实机」也会
# 被当成在问「影之刃零」这个官号的投稿，把好好的实机视频换掉。
_UP_HINTS = (
    "up主",
    "主播",
    "本人",
    "作者",
    "切片",
    "口头禅",
    "投稿",
    "作品",
    "直播间",
    "这个号",
)


_PRONOUNS = (
    "他的",
    "她的",
    "它的",
    "这个UP",
    "那个UP",
    "这个up",
    "那个up",
    "这个人",
    "那个人",
    "他",
    "她",
)


def resolve_pronouns(query: str, name: str) -> str:
    """「他的切片」→「Chris1i 切片」：用最近聊到的 UP 名补代词。

    自然语言里人不会每句都重复名字；不知道「他」是谁时才真的搜不到。
    """
    if not name or not query:
        return query
    out = query
    for pronoun in _PRONOUNS:
        out = out.replace(pronoun, f"{name} ")
    return re.sub(r"\s+", " ", out).strip()


def _clean_keyword(text: str) -> str:
    """把「Chris1i 是谁」这种整句清成能搜的词；全是空话时返回空串。

    模型经常把整句请求塞进 query（「看看克里斯最新直播切片，数一数他说了
    多少句…」），B 站按标题搜这句话只会回一堆无关推荐，所以意图词要摘掉，
    只留名字和主题。摘空了就让调用方明说「没给关键词」，别再拿空话去搜。
    """
    out = (text or "").strip()
    for word in _NOISE_WORDS:
        out = re.sub(re.escape(word), " ", out, flags=re.IGNORECASE)
    return re.sub(r"[\s，,。！？!?、]+", " ", out).strip()


def _relevant_items(keyword: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按查询里的实词过一遍：B 站对乱码词也会塞一堆推荐，别当真。"""
    terms = [part for part in re.split(r"[\s,，、。]+", keyword) if len(part) >= 2]
    if not terms:
        return items
    out: list[dict[str, Any]] = []
    for item in items:
        blob = (
            f"{item.get('title') or item.get('name') or ''} "
            f"{item.get('author') or item.get('sign') or ''}"
        ).lower()
        if any(term.lower() in blob for term in terms):
            out.append(item)
    return out


def _date_of(ts: Any) -> str:
    """发布时间戳 → `10-25`；不是今年的写 `2025-11-27`，免得旧视频看着像最新。"""
    stamp = _int(ts)
    if stamp <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return ""
    if moment.year == datetime.now().year:
        return moment.strftime("%m-%d")
    return moment.strftime("%Y-%m-%d")


def _format_videos(items: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for index, item in enumerate(items, 1):
        bits = [
            f"[{index}] {item.get('title')}",
            f"UP：{item.get('author') or '未知'}",
        ]
        date = _date_of(item.get("pubdate"))
        if date:
            bits.append(date)
        if item.get("duration"):
            bits.append(str(item.get("duration")))
        bits.append(f"播放 {item.get('play') or 0}")
        bits.append(str(item.get("bvid") or ""))
        out.append("｜".join(bit for bit in bits if bit))
    return out


def _format_users(items: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for index, item in enumerate(items, 1):
        bits = [
            f"[{index}] {item.get('name')}",
            f"UID {item.get('mid') or '未知'}",
            str(item.get("sign") or "")[:40],
            f"粉丝 {item.get('fans') or 0}",
        ]
        if item.get("videos"):
            bits.append(f"投稿 {item.get('videos')}")
        out.append("｜".join(bit for bit in bits if bit))
    return out


def title_from_card_text(text: str) -> str:
    """从正文里的 `[B站视频] 标题` 取标题（卡片没带链接时的兜底）。"""
    for line in (text or "").splitlines():
        match = re.match(r"^\s*\[B站视频\]\s*(.+)$", line)
        if match:
            title = match.group(1).strip()
            if title:
                return title[:80]
    return ""


def best_title_match(
    query: str, items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """标题相似度最高的那条；差太多就不认（宁可说没找到，也别抓错视频）。"""
    from difflib import SequenceMatcher

    def norm(text: str) -> str:
        return re.sub(r"[\s\W_]+", "", str(text or "")).lower()

    target = norm(query)
    if not target:
        return None
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in items:
        title = norm(item.get("title"))
        if not title:
            continue
        score = SequenceMatcher(None, target, title).ratio()
        if target in title or title in target:
            score = max(score, 0.72)
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 0.4 else None


class BiliCards:
    """按需查 B 站视频卡片。带内存缓存 + 并发闸门 + 硬超时。"""

    def __init__(
        self,
        enabled: bool = True,
        timeout: float = 4.0,
        cache_minutes: int = 60,
        max_chars: int = 200,
        max_links: int = 2,
        cover_enabled: bool = True,
        cover_max_mb: float = 1.5,
        cover_timeout: float = 3.0,
        archive_enabled: bool = False,
        sessdata: str = "",
        archive_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        archive_days: int = 7,
        archive_max_chars: int = 700,
        api_timeout: float = 6.0,
        rate: float = 1.0,
        archive_timeout: float = 25.0,
        extras_enabled: bool = True,
        knowledge: Any = None,
        knowledge_enabled: bool = True,
        knowledge_days: int = 30,
        frames_enabled: bool = False,
        frames_timeout: float = 60.0,
        frames_count: int = CELLS,
        frames_keep_hours: float = 24.0,
        zoom_enabled: bool = True,
        chris_mid: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.timeout = max(0.5, float(timeout))
        self.cache_seconds = max(0, int(cache_minutes)) * 60
        self.max_chars = max(80, int(max_chars))
        self.max_links = max(1, int(max_links))
        self.cover_enabled = bool(cover_enabled)
        self.cover_max_mb = max(0.2, float(cover_max_mb))
        self.cover_timeout = max(0.5, float(cover_timeout))
        self.archive_enabled = bool(archive_enabled) and archive_dir is not None
        self.archive_max_chars = max(200, int(archive_max_chars))
        self.api_timeout = max(1.0, float(api_timeout))
        self.rate = max(0.0, float(rate))
        self.archive_timeout = max(3.0, float(archive_timeout))
        # 热评 / 弹幕峰值：多 1~5 个请求，仍在后台；风控严时可一键关
        self.extras_enabled = bool(extras_enabled)
        # 学到的内容写进知识库（Memory 实例，可为 None）：0 token，本地 sqlite
        self.knowledge = knowledge
        self.knowledge_enabled = bool(knowledge_enabled)
        self.knowledge_days = max(1, int(knowledge_days))
        # 画面：被问「长什么样/截图」时抽帧（默认关，要下载视频）
        self.frames_enabled = bool(frames_enabled)
        self.frames_timeout = max(5.0, float(frames_timeout))
        self.frames_count = max(4, min(16, int(frames_count)))
        # 追问「03:10 那段」时用留底帧放大；留底目录在 data/bili/frames/<bvid>
        self.zoom_enabled = bool(zoom_enabled)
        self.frames_keep_hours = max(0.0, float(frames_keep_hours))
        # 群里说的「克里斯」指的就是这个号（配置里的 CHRIS_MID）。B 站用户搜索
        # 是字面匹配，「克里斯」会撞到一个 98 粉的同名号，靠搜索永远认不到本人；
        # 和 live_tools 一样，这组别名直接走配置。
        self.chris_mid = str(chris_mid or "").strip()
        # B 站设备指纹：和 bili_api 共用同一个 buvid.json。
        # 只带 SESSDATA 的请求会被 412（2026-11-04 实锤），必须一起带上。
        # 没显式给 state_dir 就落在档案目录，保证和 BiliApi 读的是同一个文件。
        _buvid_home = state_dir or archive_dir
        self._state_dir = Path(_buvid_home) if _buvid_home else None
        self._buvid3 = ensure_buvid3(self._state_dir)
        self._frame_cache: dict[
            tuple[str, int, int], tuple[float, list[dict[str, Any]]]
        ] = {}
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        # 同一时刻最多查几张：群里连发一串链接时别把连接池打满
        self._sem = asyncio.Semaphore(4)
        self.hits = 0
        self.misses = 0

        self._store = (
            ArchiveStore(archive_dir, ttl_days=archive_days) if archive_dir else None
        )
        # 分镜单帧的留底根目录：和档案同一个盘，zoom 只读这里
        self._frames_root = (
            self._store.dir / "frames"
            if (self._store is not None and self.zoom_enabled)
            else None
        )
        # 封面图块（base64）：同一个视频被反复贴时别再下一遍。条目很少，超了清空。
        self._cover_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._api: BiliApi | None = None
        self._sessdata = (sessdata or "").strip()
        # 本轮查到、但还没有档案的视频：交给 kick_archive() 在后台补（不挡回复）
        self.pending: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._archive_sem = asyncio.Semaphore(1)

    # ---------- 卡片 ----------

    def _pick(self, urls: list[str]) -> list[str]:
        picked: list[str] = []
        for url in urls:
            if is_bili(url) and url not in picked:
                picked.append(url)
        return picked

    def _archive_of(self, info: dict[str, Any]) -> dict[str, Any] | None:
        if not self.archive_enabled or self._store is None:
            return None
        bvid = str(info.get("bvid") or "")
        return self._store.load(bvid) if bvid else None

    async def note(self, urls: list[str]) -> str:
        """把一批网址变成一句提示。没有 B 站链接 / 关着 → 空串。"""
        if not self.enabled:
            return ""
        picked = self._pick(urls)
        if not picked:
            return ""
        notes: list[str] = []
        failed = 0
        for url in picked[: self.max_links]:
            info = await self.info(url)
            if info:
                archive = self._archive_of(info)
                notes.append(
                    format_note(info, self.max_chars, archive, self.archive_max_chars)
                )
                if not archive and self.archive_enabled:
                    bvid = str(info.get("bvid") or "")
                    if bvid and all(
                        str(item.get("bvid") or "") != bvid for item in self.pending
                    ):
                        self.pending.append(info)
            else:
                failed += 1
        if not notes:
            return failure_note() if failed else ""
        if failed:
            notes.append("（还有一条B站链接没打开）")
        return "\n".join(notes)

    async def note_sync(
        self, urls: list[str], timeout: float | None = None
    ) -> str:
        """一次到位：查卡片 + 同步抓档案（最多等 timeout 秒），再给文案。

        timeout<=0 时退回 `note()`：只给卡片，档案留给后台。
        同步抓不到（超时/风控）的那条不落盘、丢进 pending 让后台继续，
        下一轮还能重试。
        """
        if not self.enabled:
            return ""
        if timeout is not None and float(timeout) <= 0:
            return await self.note(urls)
        picked = self._pick(urls)
        if not picked:
            return ""
        notes: list[str] = []
        failed = 0
        for url in picked[: self.max_links]:
            info = await self.info(url)
            if not info:
                failed += 1
                continue
            archive = self._archive_of(info)
            if archive is None:
                archive = await self._build(info, timeout=timeout)
                bvid = str(info.get("bvid") or "")
                if archive.get("ok"):
                    if self._store is not None and bvid:
                        self._store.save(bvid, archive)
                    self._learn(archive)
                elif bvid and all(
                    str(item.get("bvid") or "") != bvid for item in self.pending
                ):
                    self.pending.append(info)
            notes.append(
                format_note(info, self.max_chars, archive, self.archive_max_chars)
            )
        if not notes:
            return failure_note() if failed else ""
        if failed:
            notes.append("（还有一条B站链接没打开）")
        return "\n".join(notes)

    async def note_sync_title(self, title: str, timeout: float | None = None) -> str:
        """卡片只有标题、没带链接：按标题在 B 站搜，找到再同步抓档案。"""
        keyword = (title or "").strip()[:80]
        if not self.enabled or not keyword:
            return ""
        try:
            results = await self.api().search_video(keyword, limit=5)
        except Exception:  # noqa: BLE001 - 搜不到就当没有
            log.info("B站按标题搜索失败", exc_info=True)
            return ""
        pick = best_title_match(keyword, results)
        if not pick:
            return ""
        url = f"https://www.bilibili.com/video/{pick['bvid']}"
        text = await self.note_sync([url], timeout=timeout)
        if not text:
            return ""
        return (
            f"（卡片没带链接，按标题「{keyword[:20]}」在 B 站搜到的可能是这条）\n{text}"
        )

    async def search_text(
        self,
        query: str,
        kind: str = "auto",
        count: int = 5,
        context_name: str = "",
        order: str = "",
        up: str = "",
    ) -> str:
        """B 站站内搜索，返回给模型看的几行（0 token，本地 HTTP）。

        kind：video / user / auto。auto 时先按关键词猜，猜不出来两个都搜一遍，
        让模型自己挑——总比「她想找 UP 主，结果搜了一屏视频」强。
        context_name：这个会话最近聊到的 UP 名，用来把「他的切片」补成具体名字。
        order=new：调用方想「按时间」的提示。但默认是综合排序（热度/相关度），
            只有话里点名「最新/最近/上场/今天」时才真的按发布时间；主题搜索
            里模型顺手传的 new 不算强调（2026-10-30 主人要求）。这种强调下如果
            认得出是问某个 UP，会改读他自己的投稿列表（动态页，含直播回放）。
        up：只看某个 UP 的投稿（名字或 UID）；搜不到就退回普通搜索。
        """
        keyword = (query or "").strip()[:60]
        if not self.enabled or not keyword:
            return ""
        keyword = resolve_pronouns(keyword, context_name)
        try:
            limit = max(1, min(10, int(count or 5)))
        except (TypeError, ValueError):
            limit = 5
        kind = (kind or "auto").strip().lower()
        if kind not in {"video", "user", "auto"}:
            kind = "auto"
        # 默认综合排序（热度/相关度）。只有话里点名「最新/最近/上场/今天」时
        # 才按发布时间 —— order=new 是模型自己的判断，主题搜索时经常顺手传，
        # 不能当成用户强调了「最新」（2026-10-30 主人要求：默认按热度）。
        newest = any(word in keyword for word in _NEWEST_WORDS)
        up_name = str(up or "").strip().lstrip("@")
        if kind == "auto":
            kind = guess_search_kind(keyword)

        search_kw = _clean_keyword(keyword)

        # 1. 只看某个 UP：先认名字再按作者筛，别把「提到他」的切片混进「他的投稿」
        if up_name:
            found = await self._videos_of_up(up_name, limit, newest)
            if found:
                name, videos = found
                head = f"B 站 UP「{name}」的投稿"
                head += "（按发布时间从新到旧）：" if newest else "（按相关度排）："
                lines = [head, *_format_videos(videos), self._search_footer()]
                return "\n".join(lines)
            log.info("B站按 UP 过滤没找到，退回普通搜索：%s", up_name)

        if not search_kw:
            return (
                "（这次没搜：query 里只有「看看」「最新」这类空话，"
                "给个 UP 名或片子名再搜）"
            )

        # 问「最新/最近」：先认 UP，再读他自己的投稿列表。站内搜索是按标题
        # 匹配的，order=new 只会把「别人提到他的新视频」排前面 —— 2026-10-30
        # 实锤：问「克里斯最新直播切片」，搜回来一屏别人的切片，本人投稿一条没有。
        if newest and kind in {"auto", "video"}:
            found = await self._latest_of_up(search_kw, limit)
            if found:
                name, videos = found
                lines = [
                    f"B 站 UP「{name}」最近更新的视频（按发布时间从新到旧）：",
                    *_format_videos(videos),
                    "（这些是他自己的投稿；要看某条讲什么，用 web_fetch 打开 "
                    "https://www.bilibili.com/video/<BV号> 看 AI 总结/字幕）",
                ]
                return "\n".join(lines)

        if kind == "auto":
            half = max(2, limit // 2)
            videos = await self._search_one(search_kw, "video", half, newest)
            if videos:
                preferred = await self._prefer_up(search_kw, videos, half)
                if isinstance(preferred, str):
                    return preferred
                videos = preferred
            users = await self._search_one(search_kw, "user", half)
            if not videos and not users:
                fallback = await self._up_fallback(search_kw, limit, newest)
                return fallback or f"B 站没搜到和「{keyword}」相关的。"
            lines = [f"B 站搜索「{keyword}」的结果："]
            if videos:
                lines.append("【视频】")
                lines.extend(_format_videos(videos))
            if users:
                lines.append("【UP 主】")
                lines.extend(_format_users(users))
                live = await self._live_note(users, search_kw)
                if live:
                    lines.append(live)
        else:
            items = await self._search_one(
                search_kw, kind, limit, newest if kind == "video" else False
            )
            if kind == "video":
                preferred = await self._prefer_up(search_kw, items, limit)
                if isinstance(preferred, str):
                    return preferred
                items = preferred
            if not items:
                if kind == "video":
                    fallback = await self._up_fallback(search_kw, limit, newest)
                    if fallback:
                        return fallback
                return f"B 站没搜到和「{keyword}」相关的。"
            lines = [f"B 站搜索「{keyword}」的结果："]
            lines.extend(
                _format_users(items) if kind == "user" else _format_videos(items)
            )
            if kind == "user":
                live = await self._live_note(items, search_kw)
                if live:
                    lines.append(live)
        lines.append(self._search_footer())
        return "\n".join(lines)

    @staticmethod
    def _search_footer() -> str:
        return (
            "（只有 B 站站内结果；要背景资料、新闻或别的网站再用 web_search。"
            "要看某条视频讲了什么、有多少句某句话，用 web_fetch 打开 "
            "https://www.bilibili.com/video/<BV号>：会走本地档案拿 AI 总结/字幕，"
            "别只看标题猜）"
        )

    async def _search_one(
        self, keyword: str, kind: str, limit: int, newest: bool = False
    ) -> list[dict[str, Any]]:
        try:
            api = self.api()
            if kind == "user":
                items = await api.search_user(keyword, limit=limit)
            elif newest:
                try:
                    items = await api.search_video(
                        keyword, limit=limit, order="pubdate"
                    )
                except TypeError:
                    # 测试/旧注入的假 API 没有 order 参数：退回旧签名
                    items = await api.search_video(keyword, limit=limit)
            else:
                items = await api.search_video(keyword, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 搜不到不能影响聊天
            log.info("B站搜索失败（%s）", type(exc).__name__)
            return []
        return _relevant_items(keyword, items)

    @staticmethod
    def _terms_of(keyword: str) -> list[str]:
        return [
            part for part in re.split(r"[\s,，、。]+", keyword or "") if len(part) >= 2
        ]

    def _known_up(self, text: str) -> tuple[str, str] | None:
        """「克里斯」这类群里的叫法 → (显示名, 配置里的 mid)。"""
        if not self.chris_mid:
            return None
        low = (text or "").casefold()
        if any(alias in low for alias in _CHRIS_ALIASES):
            return ("Chris1i", self.chris_mid)
        return None

    def _up_terms(self, keyword: str) -> list[str]:
        """可能是人名的词：去掉「最新/视频/切片」这类意图词，长的先试。"""
        terms = [
            term
            for term in self._terms_of(keyword)
            if term.casefold() not in _UP_STOPWORDS
        ]
        return sorted(terms, key=len, reverse=True)[:3]

    @staticmethod
    def _pick_user(
        users: list[dict[str, Any]], name: str, exact: bool = False
    ) -> dict[str, Any] | None:
        """同名号很多时别拿第一个：先要名字对得上，再按粉丝量排。

        「克里斯」在 B 站能搜到一个 98 粉的同名号，粉丝最多的才是本人；
        「asaki」第一条也有小号抢位（2026-10-30 实测）。
        """
        low = (name or "").strip().casefold()
        if not low:
            return None
        if exact:
            return next(
                (
                    item
                    for item in users
                    if str(item.get("name") or "").casefold() == low
                ),
                None,
            )
        related = [
            item
            for item in users
            if low in str(item.get("name") or "").casefold()
        ]
        pool = related or list(users)
        return max(pool, key=lambda item: _int(item.get("fans"))) if pool else None

    async def _up_exact(self, terms: list[str]) -> dict[str, Any] | None:
        """查询里有没有一个词正好是某个 UP 的名字（「Chris1i 口头禅」里的 Chris1i）。"""
        for term in sorted(terms, key=len, reverse=True):
            known = self._known_up(term)
            if known is not None:
                return {"name": known[0], "mid": known[1], "fans": 0}
            users = await self._search_one(term, "user", 3)
            for user in users:
                if str(user.get("name") or "").casefold() == term.casefold():
                    return user
        return None

    async def _best_up(self, terms: list[str]) -> dict[str, Any] | None:
        """从查询的词里认一个 UP（粉丝太少的不认，可能只是同名路人）。"""
        for term in terms:
            known = self._known_up(term)
            if known is not None:
                return {"name": known[0], "mid": known[1], "fans": 0}
            up = self._pick_user(await self._search_one(term, "user", 5), term)
            if up and _int(up.get("fans")) >= _MIN_UP_FANS:
                return up
        return None

    async def _uploads(self, mid: str, limit: int) -> list[dict[str, Any]]:
        """UP 自己的最近投稿（动态页）。接口失败/假 API 没有这个方法 → 空。"""
        fn = getattr(self.api(), "uploaded_videos", None)
        if fn is None or not mid:
            return []
        try:
            videos = await fn(str(mid), limit)
        except Exception as exc:  # noqa: BLE001 - 读不到就退回站内搜索
            log.info("B站读 UP 投稿失败（%s）", type(exc).__name__)
            return []
        return list(videos or [])

    async def _uploads_by_search(
        self, name: str, mid: str, limit: int
    ) -> list[dict[str, Any]]:
        """动态页拿不到投稿时的兜底：站内搜索，只留这个 UP 自己的。

        为什么要这条兜底（2026-11-04 对照实验）：同一个请求、同一个 buvid3，
        家庭宽带 IP 拿 `polymer/web-dynamic/v1/feed/space` 返回 200，
        阿里云 IP 一律 `-412 request was banned` —— B 站按 IP 段封了这个接口。
        投稿页 `space/wbi/arc/search` 更糟（服务器 -403、家里 412），
        唯一两边都通的是站内搜索 `wbi/search/type`。
        """
        fn = getattr(self.api(), "search_video", None)
        if fn is None or not name:
            return []
        try:
            rows = await fn(name, limit=max(12, limit * 3), order="pubdate")
        except Exception as exc:  # noqa: BLE001
            log.info("B站搜索兜底失败（%s）", type(exc).__name__)
            return []
        rows = list(rows or [])
        mine = [r for r in rows if mid and str(r.get("mid") or "") == str(mid)]
        if not mine:
            mine = [r for r in rows if str(r.get("author") or "") == name]
        if mine:
            log.info("B站投稿页不可用，改用搜索兜底：%s（%d 条）", name, len(mine))
        return mine[:limit]

    async def _latest_of_up(
        self, keyword: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """「最新/最近」问的是 UP 本人时，读他自己的投稿，而不是标题搜索。

        2026-10-30 实锤：问「看看克里斯最新直播切片」，站内搜回来的全是别人
        剪的切片；「最新」只该看他的投稿列表（含直播回放）。
        """
        known = self._known_up(keyword)
        if known is not None:
            videos = await self._uploads(known[1], limit)
            if not videos:
                videos = await self._uploads_by_search(known[0], known[1], limit)
            return (known[0], videos) if videos else None
        terms = self._up_terms(keyword)
        # 只有一句话里只剩一个「名字」时才猜 UP：主题词多的（「影之刃零 实机」）
        # 搜视频更准，别把游戏官号当成人名。
        if len(terms) != 1:
            return None
        up = await self._best_up(terms)
        if up is None:
            return None
        name = str(up.get("name") or "")
        mid = str(up.get("mid") or "")
        videos = await self._uploads(mid, limit)
        if not videos:
            videos = await self._uploads_by_search(name, mid, limit)
        return (name, videos) if videos else None

    async def _prefer_up(
        self, keyword: str, items: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]] | str:
        """查询是「UP 名 + 别的词」时，只留跟这个 UP 有关的。

        2026-10-27 实锤：搜「Chris1i 口头禅」，站内只按标题匹配，回来的是
        「德语口头禅」这类无关视频；她只能瞎猜。这里认出 Chris1i 后，
        如果结果里没一条是他自己的，就换成他最近的投稿（再不行才列提到他的）。
        2026-10-30 补：只有话里明显在说「人」时才换；「影之刃零 实机」这种
        主题搜索不能被同名官号的投稿列表顶掉。
        """
        terms = self._terms_of(keyword)
        if len(terms) < 2:
            return items
        low = keyword.casefold()
        known = self._known_up(keyword)
        if known is not None:
            up: dict[str, Any] | None = {"name": known[0], "mid": known[1]}
        elif any(word in low for word in _UP_HINTS):
            up = await self._up_exact(terms)
        else:
            return items
        if up is None:
            return items
        name = str(up.get("name") or "")
        name_cf = name.casefold()
        mid = str(up.get("mid") or "")
        owned = [
            item
            for item in items
            if (mid and str(item.get("mid") or "") == mid)
            or str(item.get("author") or "").casefold() == name_cf
        ]
        if owned:
            return owned
        videos = await self._uploads(mid, limit)
        if videos:
            return self._fallback_text(keyword, name, videos, mid=mid)
        found = await self._videos_of_up(name, limit, True, exact=True, mention=True)
        if found:
            return self._fallback_text(keyword, *found)
        return items

    @staticmethod
    def _fallback_text(
        keyword: str,
        name: str,
        videos: list[dict[str, Any]],
        mid: str = "",
    ) -> str:
        """兜底文案：分清「他自己的投稿」和「别人提到他的视频」，别混着说。"""
        own = bool(mid) and all(
            str(item.get("mid") or "") == mid
            or str(item.get("author") or "").casefold() == name.casefold()
            for item in videos
        )
        if own:
            head = (
                f"B 站没搜到标题里带「{keyword}」的，下面是他最近的投稿"
                "（按发布时间从新到旧）："
            )
            tail = (
                "（这些是他自己的投稿，不一定提到上面那个词；"
                "要判断内容得用 web_fetch 打开看）"
            )
        else:
            head = (
                f"B 站没搜到标题里带「{keyword}」的，下面是最近提到 {name} 的视频"
                "（按发布时间从新到旧）："
            )
            tail = (
                f"（这些只是最近提到 {name} 的视频，不等于他常说的口头禅；"
                "要判断口头禅得点开看内容）"
            )
        return "\n".join(
            [head, *_format_videos(videos), tail, BiliCards._search_footer()]
        )

    async def _videos_of_up(
        self,
        name: str,
        limit: int,
        newest: bool,
        *,
        exact: bool = False,
        mention: bool = False,
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """按 UP 名找最近投稿；默认只保留作者确实是这个 UP 的。

        exact=True：名字必须完全对上（兜底时用，别拿「口头禅」当 UP 名）。
        mention=True：也收标题里提到他的视频（切片/联动是别人发的）。
        newest=True：先读他自己的投稿列表（动态页），拿不到才退回标题搜索。
        """
        known = self._known_up(name)
        if known is not None:
            up_name, mid = known
        else:
            users = await self._search_one(name, "user", 3)
            if not users:
                return None
            match = self._pick_user(users, name, exact=exact)
            if match is None:
                return None
            up_name = str(match.get("name") or name)
            mid = str(match.get("mid") or "")
        if newest:
            videos = await self._uploads(mid, limit)
            if videos:
                return (up_name, videos)
        try:
            api = self.api()
            if newest:
                videos = await api.search_video(up_name, limit=20, order="pubdate")
            else:
                videos = await api.search_video(up_name, limit=20)
        except Exception as exc:  # noqa: BLE001
            log.info("B站 UP 投稿搜索失败（%s）", type(exc).__name__)
            return None
        name_cf = up_name.casefold()
        kept = [
            item
            for item in videos
            if (mid and str(item.get("mid") or "") == mid)
            or str(item.get("author") or "").casefold() == name_cf
            or (mention and name_cf in str(item.get("title") or "").casefold())
        ]
        kept.sort(key=lambda item: _int(item.get("pubdate")), reverse=True)
        return (up_name, kept[:limit]) if kept else None

    async def _up_fallback(
        self, keyword: str, limit: int, newest: bool
    ) -> str:
        """视频一条都没搜到时的兜底：从查询里认出 UP 名叫什么，列他最近的视频。"""
        up = await self._up_exact(self._up_terms(keyword))
        if up is None:
            return ""
        name = str(up.get("name") or "")
        mid = str(up.get("mid") or "")
        videos = await self._uploads(mid, limit)
        if videos:
            return self._fallback_text(keyword, name, videos, mid=mid)
        found = await self._videos_of_up(name, limit, True, exact=True, mention=True)
        return self._fallback_text(keyword, *found) if found else ""

    async def _live_note(
        self, users: list[dict[str, Any]], keyword: str
    ) -> str:
        """UP 主搜索附一句直播间状态；没有接口/没直播间就不写。"""
        low = keyword.casefold()
        match = next(
            (item for item in users if str(item.get("name") or "").casefold() == low),
            users[0] if users else None,
        )
        if not match:
            return ""
        fn = getattr(self.api(), "live_status", None)
        if fn is None:
            return ""
        try:
            live = await fn(str(match.get("mid") or ""))
        except Exception:  # noqa: BLE001 - 直播查不到不能影响搜索
            return ""
        if not live:
            return ""
        name = str(match.get("name") or "")
        room = live.get("room_id") or ""
        if live.get("live"):
            title = str(live.get("title") or "未命名")
            online = format_count(live.get("online") or 0)
            return f"直播：{name} 正在直播《{title}》｜人气 {online}｜直播间 {room}"
        return (
            f"直播：{name} 现在没播（直播间 {room}）；"
            "B 站没有公开的开播时间表，别猜几点开。"
        )

    async def info(self, url: str, now: float | None = None) -> dict[str, Any] | None:
        """查一个链接的卡片。命中缓存不发请求；任何异常都返回 None。"""
        if not self.enabled or not is_bili(url):
            return None
        bvid, aid = parse_bili_id(url)
        key = bvid or (f"av{aid}" if aid else "") or url
        stamp = time.time() if now is None else float(now)
        cached = self._cache.get(key)
        if cached is not None:
            ts, value = cached
            ttl = self.cache_seconds if value else FAIL_CACHE_SECONDS
            if stamp - ts < ttl:
                self.hits += 1
                return value
        value: dict[str, Any] | None = None
        self.misses += 1
        try:
            async with self._sem:
                value = await asyncio.wait_for(self._load(url), timeout=self.timeout)
        except (TimeoutError, asyncio.TimeoutError):
            log.info("B站链接查超时（%.1fs）：%s", self.timeout, url[:80])
        except Exception as exc:  # noqa: BLE001 - 查不到不能影响聊天
            log.info("B站链接查失败（%s）：%s", type(exc).__name__, url[:80])
        self._cache[key] = (stamp, value)
        if value:
            log.info(
                "B站卡片：%s《%s》%s",
                value.get("bvid", ""),
                str(value.get("title") or "")[:30],
                format_duration(_int(value.get("duration"))),
            )
        return value

    async def _load(self, url: str) -> dict[str, Any] | None:
        """真正的网络请求：短链 → BV → view。测试里整个替换掉。"""
        if not is_bili(url):
            return None
        bvid, aid = parse_bili_id(url)
        if not bvid and not aid:
            final = await self._resolve(url)
            bvid, aid = parse_bili_id(final)
            if not bvid and not aid:
                log.info("短链展开后没认出来：%s", url[:80])
                return None
        params = {"bvid": bvid} if bvid else {"aid": aid}
        async with httpx.AsyncClient(
            timeout=self.timeout, headers=self._headers(), follow_redirects=True
        ) as client:
            resp = await client.get(VIEW_API, params=params)
            if resp.status_code != 200:
                log.info("B站 view 返回 HTTP %s", resp.status_code)
                return None
            try:
                payload = resp.json()
            except ValueError:
                log.info("B站 view 返回不是 JSON")
                return None
        return normalize_view(payload)

    def _headers(self) -> dict[str, str]:
        """请求头：UA / Referer + cookie（SESSDATA + buvid3）。

        2026-11-04：`x/web-interface/view` 只带 SESSDATA 也会 412，
        必须 `SESSDATA + buvid3` 一起带；一个都不带更是必 412。
        之前这里直接用 `_HEADERS`（没有任何 cookie），于是视频信息全被拦，
        表现成「打不开视频」。
        """
        headers = dict(_HEADERS)
        parts: list[str] = []
        if self._sessdata:
            parts.append(f"SESSDATA={self._sessdata}")
        if self._buvid3:
            parts.append(f"buvid3={self._buvid3}")
        if parts:
            headers["Cookie"] = "; ".join(parts)
        return headers

    async def _resolve(self, url: str) -> str:
        """短链展开：只发一次 GET，跟着跳转拿最终地址。"""
        async with httpx.AsyncClient(
            timeout=self.timeout, headers=self._headers(), follow_redirects=True
        ) as client:
            resp = await client.get(url)
            return str(resp.url)

    # ---------- 封面 ----------

    async def covers(self, urls: list[str], limit: int | None = None) -> list[dict[str, Any]]:
        """把视频封面下载成图片块（进 prompt 给她看）。失败/关掉 → 空列表。

        只是封面，不是视频画面；调用方拿到非空结果时补一句 `cover_note()`。
        """
        if not self.enabled or not self.cover_enabled:
            return []
        picked = self._pick(urls)[: max(1, int(limit or 1))]
        blocks: list[dict[str, Any]] = []
        for url in picked:
            info = await self.info(url)
            pic = str((info or {}).get("pic") or "")
            if not pic or not is_media_url(pic):
                continue
            bvid = str((info or {}).get("bvid") or "")
            stamp = time.time()
            cached = self._cover_cache.get(bvid) if bvid else None
            if cached is not None and stamp - cached[0] < self.cache_seconds:
                blocks.append(cached[1])
                continue
            try:
                block = await asyncio.wait_for(
                    self._cover_block(pic), timeout=self.cover_timeout
                )
            except (TimeoutError, asyncio.TimeoutError):
                log.info("B站封面下载超时（%.1fs）", self.cover_timeout)
                continue
            except Exception as exc:  # noqa: BLE001
                log.info("B站封面下载失败（%s）", type(exc).__name__)
                continue
            if block:
                blocks.append(block)
                if bvid:
                    if len(self._cover_cache) >= 20:
                        self._cover_cache.clear()
                    self._cover_cache[bvid] = (stamp, block)
        if blocks:
            log.info("B站封面已加载 %d 张", len(blocks))
        return blocks

    async def _cover_block(self, pic: str) -> dict[str, Any] | None:
        limit = int(self.cover_max_mb * 1024 * 1024)
        async with httpx.AsyncClient(
            timeout=self.cover_timeout, headers=self._headers(), follow_redirects=True
        ) as client:
            async with client.stream("GET", pic) as resp:
                if resp.status_code != 200:
                    log.info("B站封面 HTTP %s", resp.status_code)
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > limit:
                        log.info("B站封面超过 %.1f MB，放弃", self.cover_max_mb)
                        return None
        return image_block(bytes(buf), limit)

    # ---------- 画面 ----------

    async def shots(self, url: str, grids: int = 1) -> list[dict[str, Any]]:
        """按镜头抽 12 格 × N 张分镜图给 vision。关着 / 没视频 / 失败 → 空列表。

        `grids=1` 是原来的单张 12 格；2/3 张用于 10~20 分钟视频，一次下载后
        按时间切成多张，覆盖整条视频。只有真的被问「画面」时才该调：要下载
        整段视频流（十分钟 360p 约 20~40MB）。抽出来的单帧会留底 24 小时。
        """
        if not self.enabled or not self.frames_enabled:
            return []
        info = await self.info(url)
        bvid = str((info or {}).get("bvid") or "")
        cid = str((info or {}).get("cid") or "")
        if not bvid or not cid:
            return []
        grids = max(1, min(3, int(grids)))
        duration = _int((info or {}).get("duration"))
        # 短视频给 1080p，10~20 分钟给 720p；最终格子是 400px，高画质主要让
        # zoom 和小字更清楚。高画质超时/超体积会自动退回 360p。
        qn = 80 if duration <= 600 else 64
        cache_key = (bvid, grids, qn)
        cached = self._frame_cache.get(cache_key)
        if cached is not None and time.time() - cached[0] < 86400:
            return list(cached[1])
        keep_dir = self._frames_root / bvid if self._frames_root else None
        try:
            raws = await asyncio.wait_for(
                grab_multi(
                    self.api(),
                    bvid,
                    cid,
                    duration,
                    grids=grids,
                    cells=self.frames_count,
                    qn=qn,
                    max_mb=320,
                    download_timeout=60.0,
                    work_root=(self._store.dir / "tmp" if self._store else None),
                    keep_dir=keep_dir,
                    keep_hours=self.frames_keep_hours,
                ),
                timeout=self.frames_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            log.info("B站抽帧超时（%.0fs）：%s", self.frames_timeout, bvid)
            return []
        except Exception as exc:  # noqa: BLE001 - 没画面不能影响回复
            log.info("B站抽帧失败（%s）：%s", type(exc).__name__, bvid)
            return []
        blocks: list[dict[str, Any]] = []
        for raw in raws or []:
            block = image_block(raw, 2 * 1024 * 1024)
            if block:
                blocks.append(block)
        if not blocks:
            return []
        if len(self._frame_cache) >= 12:
            self._frame_cache.clear()
        self._frame_cache[cache_key] = (time.time(), blocks)
        log.info(
            "B站画面已抽帧：%s（%d 张 × %d 格，qn=%d，留底=%s）",
            bvid,
            len(blocks),
            self.frames_count,
            qn,
            bool(keep_dir),
        )
        return blocks

    async def zoom(
        self, url: str, start: float, end: float
    ) -> tuple[list[dict[str, Any]], str]:
        """「03:10 那段呢」：从留底帧里挑这一段拼一张放大图。

        没有留底（之前没被问过画面 / 过期了）就返回空 —— 调用方会退回整段分镜。
        返回 (图片块, 给模型看的说明)；说明里写的是**实际挑到的**时间。
        """
        if not self.enabled or not self.frames_enabled or not self.zoom_enabled:
            return [], ""
        if self._frames_root is None:
            return [], ""
        info = await self.info(url)
        bvid = str((info or {}).get("bvid") or "")
        if not bvid:
            return [], ""
        keep_dir = self._frames_root / bvid
        shots = await asyncio.to_thread(load_kept, keep_dir)
        if not shots:
            return [], ""
        duration = float(_int((info or {}).get("duration")))
        if duration > 0:
            start = max(0.0, min(float(start), duration))
            end = max(0.0, min(float(end), duration))
        if end <= start:
            return [], ""
        picked = select_window(shots, start, end)
        if not picked:
            return [], ""
        raw = await asyncio.to_thread(compose_zoom, picked)
        if not raw:
            return [], ""
        block = image_block(raw, 2 * 1024 * 1024)
        if not block:
            return [], ""
        low = format_stamp(picked[0].at)
        high = format_stamp(picked[-1].at)
        note = (
            f"（这是你放大的 {low}–{high} 这段里的 {len(picked)} 帧，"
            "每格左上角是时间点；只有这几帧，不是整段视频）"
        )
        log.info("B站放大某段：%s %s-%s（%d 帧）", bvid, low, high, len(picked))
        return [block], note

    # ---------- 档案（后台） ----------

    def kick_archive(self) -> None:
        """把本轮查到、还没档案的视频丢到后台。同步返回，不阻塞回复。"""
        if not self.archive_enabled or not self.pending:
            return
        # 已经在跑的任务别无限堆：群里刷一串链接时，这一轮先不接，留给下一轮
        if len(self._tasks) >= 4:
            log.info("B站档案任务已满（%d 个在跑），这轮先不抓", len(self._tasks))
            return
        infos = self.pending[:2]
        self.pending.clear()
        for info in infos:
            task = asyncio.create_task(self._archive_one(info))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _build(
        self, info: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """抓一份档案（不落盘、不学习）：后台任务和 web_fetch 共用。"""
        return await build_archive(
            self.api(),
            info,
            max_chars=self.archive_max_chars,
            timeout=self.archive_timeout if timeout is None else timeout,
            extras=self.extras_enabled,
        )

    async def digest(self, url: str, timeout: float | None = None) -> str:
        """给 `web_fetch` / 追问用：同步抓一份完整档案，返回给模型看的资料。

        普通网页抓取对 b23 短链只能拿到静态页的标题（2026-10-26 实测她就卡在
        这里，回「本鱼抓不到」）。工具调用本来就在等她，这里多等几秒没关系。

        直播页（live.bilibili.com/<room>）不是视频，没有 BV/av 号，不能走
        `view` 接口；直接查直播房间接口，返回开播状态。
        """
        if not self.enabled or not is_bili(url):
            return ""
        room = live_room_of(url)
        if room:
            return await self._live_digest(room)
        info = await self.info(url)
        if not info:
            return failure_note()
        bvid = str(info.get("bvid") or "")
        archive = self._archive_of(info)
        if archive is None:
            archive = await self._build(info, timeout=timeout)
            if self._store is not None and bvid:
                self._store.save(bvid, archive)
            if archive.get("ok"):
                self._learn(archive)

        base = f"标题{_title_of(info)}"
        if info.get("owner"):
            base += f"，UP：{str(info['owner'])[:20]}"
        if info.get("duration"):
            base += f"，时长 {format_duration(_int(info['duration']))}"
        if info.get("view"):
            base += f"，播放 {format_count(_int(info['view']))}"
        desc = " ".join(str(info.get("desc") or "").split())
        if desc:
            base += f"；简介：{desc[:120]}"
        lines = ["（这是 B 站视频的官方资料，不是网页正文）", base]
        detail = format_archive(archive, self.archive_max_chars)
        if detail:
            lines.append(detail)
        else:
            lines.append("（这个视频暂时没有总结、字幕和热评，只能给到标题、封面和简介）")
        return "\n".join(lines)

    async def _live_digest(self, room: str) -> str:
        """直播间的实时状态；房号不存在时明确说，不让模型猜。"""
        fn = getattr(self.api(), "live_room", None)
        if fn is None:
            return "（直播状态这次没查成：接口没接上；别猜开播状态）"
        try:
            info = await fn(room)
        except Exception as exc:  # noqa: BLE001 - 查不到就明说
            log.info("B站直播间状态查失败（%s）：%s", type(exc).__name__, room)
            info = None
        if not info:
            return (
                f"（B站直播间 {room} 没查到：房号可能不存在，或者接口暂时没返回；"
                "别猜开播状态）"
            )
        title = str(info.get("title") or "未命名")
        if info.get("live"):
            online = int(info.get("online") or 0)
            tail = f"；人气 {format_count(online)}" if online else ""
            return (
                "（这是 B 站直播间的实时状态，不是视频档案）\n"
                f"直播间 {info.get('room_id') or room}：正在播《{title}》{tail}。"
            )
        return (
            "（这是 B 站直播间的实时状态，不是视频档案）\n"
            f"直播间 {info.get('room_id') or room}：现在没播（上次标题《{title}》）。"
        )

    async def _archive_one(self, info: dict[str, Any]) -> None:
        bvid = str(info.get("bvid") or "")
        if not bvid:
            return
        async with self._archive_sem:
            archive = await self._build(info)
        if self._store is not None:
            self._store.save(bvid, archive)
        if archive.get("ok"):
            self._learn(archive)
        log.info(
            "B站档案：%s %s（%s）",
            bvid,
            "ok" if archive.get("ok") else "没拿到",
            "、".join(archive.get("visibility") or []) or "无",
        )

    def _learn(self, archive: dict[str, Any]) -> None:
        """档案写进知识库：以后群里聊到同一个视频/UP，她能自己想起来。"""
        if not self.knowledge_enabled or self.knowledge is None:
            return
        text = archive_knowledge_text(archive)
        if not text:
            return
        bvid = str(archive.get("bvid") or "")
        title = str(archive.get("title") or "").strip()
        up = str(archive.get("up") or "").strip()
        topic = f"{title}（B站视频" + (f"，UP：{up}" if up else "") + "）"
        try:
            self.knowledge.add_knowledge(
                topic,
                text,
                url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                source="bili",
                ttl_days=self.knowledge_days,
            )
        except Exception:  # noqa: BLE001 - 记不住不能影响档案
            log.info("B站档案写知识库失败", exc_info=True)

    def api(self) -> BiliApi:
        """惰性建 BiliApi：不查档案的人不付这个对象。"""
        if self._api is None:
            self._api = BiliApi(
                sessdata=self._sessdata,
                timeout=self.api_timeout,
                rate=self.rate,
                state_dir=self._store.dir if self._store else None,
            )
        return self._api

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._api is not None:
            await self._api.aclose()

    def status(self) -> dict[str, Any]:
        out = {
            "enabled": self.enabled,
            "cache": len(self._cache),
            "hits": self.hits,
            "misses": self.misses,
            "cover": self.cover_enabled,
            "archive": self.archive_enabled,
        }
        if self._api is not None:
            out["api"] = self._api.status()
        if self._store is not None:
            out["store"] = self._store.status()
        return out
