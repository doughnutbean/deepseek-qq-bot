"""链接感知：认出消息里的分享卡片和网址（纯本地，0 token、不联网）。

为什么需要这个文件
------------------
群里分享 B 站视频几乎都是**卡片**（json / xml / light_app 段），而
``segments_to_text`` 原来只认 text/at/image/face 这些 —— 卡片消息解出来是空串，
于是整条消息在「没文字又没图就 return」那一步被静默丢掉：她连标题都没见过，
只能对着空气回「你发的啥」。

这里只做**解析**，不做抓取（抓 B 站的是 ``bili.py``）：

- ``card_line(seg)``：一张卡片 →「[B站视频] 标题」这样一行，并进正文；
- ``bili_urls(segments, text)``：正文 + 卡片里的网址 → 归一化后的 URL 列表。

正文里的裸链接也认：群里经常直接贴 ``https://b23.tv/xxx`` 或者连 https 都不带的
``b23.tv/xxx``。短链不在这里展开（要联网），交给 bili.py。
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# 会被当成「一张分享卡片」的消息段类型。light_app 是小程序卡片，同一种东西。
CARD_KINDS = frozenset({"json", "xml", "share", "light_app"})

# 认不出的卡片结构：同一种只记一次日志（进程内）。用来发现 QQ 换了卡片格式，
# 不然她只是继续「看不到」，而日志里什么线索都没有。
_SEEN_BAD_CARDS: set[str] = set()

# 卡片里标题最多带这么多字进正文：正文是每轮都发的，别让一张卡片吃掉半屏上下文
MAX_CARD_TITLE = 50

# B 站系域名。短链跳转由 bili.py 负责，这里只做判断。
_BILI_HOSTS = (
    "bilibili.com",
    "b23.tv",
    "bili2233.cn",
    "bili22.cn",
    "bili23.cn",
    "bilibili.tv",
)

# 短链域名（不带 https:// 也算链接）
_SHORT_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn")

_URL_RE = re.compile(r"https?://[^\s<>\"'，。、；：！？（）()【】\[\]《》「」]+", re.I)
_BARE_SHORT_RE = re.compile(
    r"(?<![0-9A-Za-z./])((?:" + "|".join(re.escape(h) for h in _SHORT_HOSTS) + r")/[0-9A-Za-z]+)"
)
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:/|^)av(\d{1,12})", re.I)
_AID_RE = re.compile(r"[?&]aid=(\d{1,12})")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

# 这些标题只是应用名，不是内容标题。真标题常在 desc 里（「@UP | 标题」）。
_APP_TITLES = frozenset({"哔哩哔哩", "bilibili", "哔哩哔哩弹幕网"})

# 卡片里的图标/封面域名：它们不是内容链接，别拿来当 jumpUrl。
_IMAGE_HOSTS = ("qpic.cn", "qlogo.cn", "hdslb.com", "biliimg.com", "gtimg.cn")

# QQ 小程序卡片的 app 字段是包名（com.tencent.miniapp_01），不能当来源展示。
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$", re.I)

# 卡片里真正装内容的那一层。腾讯的分享卡片结构万变不离这几个 key。
_NODE_KEYS = (
    "news",
    "music",
    "video",
    "detail_1",
    "detail_2",
    "doc",
    "collection",
    "forum",
    "miniapp",
)


def _clean(text: Any, limit: int = 0) -> str:
    """压掉换行和多余空白，按需截断。"""
    if text is None:
        return ""
    body = re.sub(r"\s+", " ", str(text)).strip()
    return body[:limit] if limit > 0 else body


def _from_prompt(text: Any) -> str:
    """卡片里的 prompt/brief 常写成「[分享] 标题」，去掉前缀还能当标题用。"""
    body = _clean(text)
    if not body:
        return ""
    body = re.sub(r"^[\[【][^\]】]{1,8}[\]】]\s*", "", body)
    return body if len(body) >= 2 else ""


def _is_image_url(url: str) -> bool:
    lowered = url.lower().split("?")[0].split("#")[0]
    if lowered.endswith(_IMAGE_EXT):
        return True
    host = host_of(url)
    return any(host == h or host.endswith(f".{h}") for h in _IMAGE_HOSTS)


def _first_url(value: Any) -> str:
    """在任意结构里找内容链接：跳过图标/封面，优先 B 站。

    2026-10-26 的实锤：QQ 小程序卡片的 ``icon`` 排在 ``qqdocurl`` 前面，旧逻辑
    拿到第一个 http 就返回，结果把图标当成了链接，真正的 ``b23.tv`` 被丢掉 ——
    正文只剩「[分享·com.tencent.] 哔哩哔哩」，她于是顺着话头编「只拿到是个 B 站链接」。
    """
    candidates: list[str] = []
    for item in _walk(value):
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        if _is_image_url(candidate):
            continue
        candidates.append(candidate)
    for candidate in candidates:
        if is_bili(candidate):
            return candidate
    return candidates[0] if candidates else ""


def _title_from_desc(desc: Any) -> str:
    """小程序卡片的 desc 常写成「@UP | 标题」；把标题那半截取出来。"""
    body = _clean(desc)
    if not body:
        return ""
    if "|" in body:
        left, right = body.rsplit("|", 1)
        right = right.strip(" 　-—:：")
        if len(right) >= 2:
            author = left.strip().lstrip("@").strip()
            return f"@{author}：{right}" if author else right
    return body if len(body) >= 2 else ""


def _is_app_title(title: str, payload: dict[str, Any]) -> bool:
    """标题只是应用名（哔哩哔哩/QQ音乐）时，别把它当内容标题。"""
    body = _clean(title).lower()
    if not body:
        return False
    if body in _APP_TITLES:
        return True
    app_desc = _clean(payload.get("desc")).lower()
    return bool(app_desc) and body == app_desc and len(body) <= 12


def _source_name(raw: Any, payload: dict[str, Any], title: str) -> str:
    """来源名：包名不给人看，退回 payload.desc / 标题。"""
    name = _clean(raw, 24)
    if name and not _APP_ID_RE.match(name):
        return name
    for guess in (_clean(payload.get("desc"), 24), title):
        if guess and not _APP_ID_RE.match(guess):
            return guess
    return ""


def _looks_bili(card: dict[str, str]) -> bool:
    blob = f"{card.get('title', '')} {card.get('source', '')} {card.get('desc', '')}".lower()
    return "哔哩哔哩" in blob or "bilibili" in blob


def _walk(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def _loads(raw: Any) -> Any:
    """卡片里的 JSON 有时候是字符串、有时候已经是 dict。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    body = raw.strip()
    if not body.startswith(("{", "[")):
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _parse_json_card(data: dict[str, Any]) -> dict[str, str]:
    payload = _loads(data.get("data")) or _loads(data)
    if not isinstance(payload, dict):
        return {}
    node: dict[str, Any] = {}
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in _NODE_KEYS:
            inner = meta.get(key)
            if isinstance(inner, dict):
                node = inner
                break
        if not node:
            # 结构不认识：把 meta 整层当内容层，下面按 key 逐级找
            node = meta
    title = _clean(node.get("title") or payload.get("title"))
    desc = _clean(node.get("desc") or node.get("summary") or payload.get("desc"))
    url = _first_url(payload) or _first_url(node)
    # 小程序卡片只给应用名时，从 desc 的「@UP | 标题」里把真标题捞出来
    if _is_app_title(title, payload):
        title = _title_from_desc(desc) or title
    source = _source_name(
        node.get("tag") or payload.get("app") or payload.get("desc"),
        payload,
        title,
    )
    if not title:
        # 有些小程序卡片把标题只放在 prompt/brief 里
        title = _from_prompt(payload.get("prompt") or payload.get("brief") or payload.get("desc"))
    return {"title": title, "desc": desc, "url": url, "source": source}


_XML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_XML_SUMMARY_RE = re.compile(r"<summary[^>]*>(.*?)</summary>", re.S | re.I)
_XML_URL_RE = re.compile(r"<url[^>]*>(.*?)</url>", re.S | re.I)
_XML_ATTR_URL_RE = re.compile(r'\burl="([^"]+)"', re.I)
_XML_SOURCE_RE = re.compile(r'<source[^>]*\bname="([^"]*)"', re.I)
_XML_BRIEF_RE = re.compile(r'\bbrief="([^"]*)"', re.I)


def _parse_xml_card(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("data") if isinstance(data, dict) else data
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = str(raw or "")
    if not text:
        return {}

    def pick(pattern: re.Pattern[str]) -> str:
        match = pattern.search(text)
        if not match:
            return ""
        body = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", match.group(1), flags=re.S)
        return _clean(html.unescape(body))

    title = pick(_XML_TITLE_RE)
    brief = pick(_XML_BRIEF_RE)
    if not title and brief:
        # brief 一般长这样：「[分享] 标题」，去掉前缀还能用
        title = re.sub(r"^\[[^\]]*\]\s*", "", brief)
    url = pick(_XML_ATTR_URL_RE) or pick(_XML_URL_RE)
    return {
        "title": title,
        "desc": pick(_XML_SUMMARY_RE),
        "url": url,
        "source": pick(_XML_SOURCE_RE),
    }


def parse_card(seg: Any) -> dict[str, str]:
    """把一个消息段解析成 {title, desc, url, source}。不是卡片 / 解析不出就返回 {}。"""
    if not isinstance(seg, dict) or seg.get("type") not in CARD_KINDS:
        return {}
    data = seg.get("data")
    if not isinstance(data, dict):
        data = {"data": data} if data else {}
    if seg.get("type") == "xml":
        card = _parse_xml_card(data)
    elif seg.get("type") == "share":
        card = {
            "title": _clean(data.get("title")),
            "desc": _clean(data.get("desc") or data.get("content")),
            "url": _clean(data.get("url") or data.get("jumpUrl")),
            "source": _clean(data.get("source") or data.get("tag")),
        }
    else:
        card = _parse_json_card(data)
    if not card.get("title") and not card.get("url"):
        return {}
    return card


def _dump(seg: Any, limit: int = 300) -> str:
    """把消息段压成一行，供日志用（看新格式长什么样）。"""
    data = seg.get("data") if isinstance(seg, dict) else seg
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(data)
    return re.sub(r"\s+", " ", raw)[:limit]


def card_line(seg: Any, limit: int = MAX_CARD_TITLE) -> str:
    """卡片 → 进正文的一行话。认不出标题就返回空串（不塞垃圾进上下文）。"""
    card = parse_card(seg)
    title = _clean(card.get("title"), limit)
    if not title:
        # 认不出就记一条原始段：这张卡片到底是哪种结构，看日志就能定。
        # 同一种结构只提醒一次，别让一个不认识的卡片刷满日志。
        key = _dump(seg, 80)
        if key not in _SEEN_BAD_CARDS:
            _SEEN_BAD_CARDS.add(key)
            log.warning("卡片没解析出标题（可能是新格式）：%s", _dump(seg))
        return ""
    if is_bili(card.get("url", "")):
        return f"[B站视频] {title}"
    source = _clean(card.get("source"), 12)
    # source 经常就是标题本身，重复了就别加
    line = f"[分享·{source}] {title}" if source and source not in title else f"[分享] {title}"
    if _looks_bili(card) and not is_bili(card.get("url", "")):
        # 只看见「哔哩哔哩」几个字时，必须明说没拿到链接：
        # 2026-10-26 她就是从「[分享·com.tencent.] 哔哩哔哩」编出「只拿到是个 B 站链接」的。
        line += "（没拿到B站链接）"
    return line


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_bili(url: str) -> bool:
    host = host_of(url)
    return bool(host) and any(host == h or host.endswith(f".{h}") for h in _BILI_HOSTS)


def parse_bili_id(url: str) -> tuple[str, str]:
    """从网址里抽 (bvid, aid)。抽不到就是两个空串。"""
    if not url:
        return "", ""
    match = _BV_RE.search(url)
    if match:
        return match.group(1), ""
    match = _AV_RE.search(url) or _AID_RE.search(url)
    if match:
        return "", match.group(1)
    return "", ""


def _normalize_url(raw: str) -> str:
    url = raw.strip().rstrip(".,;:)）】」》")
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def urls_of_text(text: str) -> list[str]:
    """正文里的网址：完整链接 + 不带协议头的短链。"""
    if not text:
        return []
    found = [_normalize_url(m.group(0)) for m in _URL_RE.finditer(text)]
    found += [_normalize_url(m.group(1)) for m in _BARE_SHORT_RE.finditer(text)]
    return _dedupe(found)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def urls_of_segments(segments: Any, text: str = "") -> list[str]:
    """一条消息里所有网址：正文 + 卡片。segments 允许是 CQ 码字符串。"""
    found: list[str] = list(urls_of_text(text))
    if isinstance(segments, str):
        found += urls_of_text(segments)
    elif isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                found += urls_of_text(str((seg.get("data") or {}).get("text") or ""))
                continue
            card = parse_card(seg)
            if card.get("url"):
                found.append(_normalize_url(card["url"]))
    return _dedupe(found)


def bili_urls(segments: Any, text: str = "") -> list[str]:
    """只要 B 站的链接；短链原样返回，展开交给 bili.py。"""
    return [url for url in urls_of_segments(segments, text) if is_bili(url)]
