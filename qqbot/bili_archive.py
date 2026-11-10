"""把 B 站视频变成一份「档案」：官方 AI 总结 + 字幕 + 热评 + 弹幕峰值（0 token）。

为什么是这些
------------
她缺的不是「标题」，是「视频里到底说了什么、观众是什么反应」。B 站自己已经
算过一遍：`view/conclusion/get` 给摘要、带时间戳的分段提纲和 AI 字幕；
`reply/wbi/main` 给热评；`dm/web/seg.so` 给弹幕（protobuf）。全部本地 HTTP。

没登录怎么办
------------
conclusion 未登录回 -101，字幕列表回空数组。这时档案只记「没拿到」，
**不报错、不重试到死**：卡片照常，她照常说「只看到标题和封面」。

诚实契约
--------
档案里记 `visibility`（`官方AI总结` / `分段提纲` / `AI字幕` / `热评` / `弹幕峰值`）。
给模型的文案只按实际拿到的写；视频画面永远不在里面（封面归封面，画面归画面），
别让她编镜头。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterator

from .bili_api import BiliApi, is_media_url

log = logging.getLogger(__name__)

# BV 号格式：BV + 10 位（大小写字母数字）。只让这种字符串当文件名，防路径穿越。
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

# 摘要/字幕进 prompt 的总长度；太长会挤掉人设和上下文（见 promptbudget.py）
DEFAULT_MAX_CHARS = 700

# 档案格式版本：加字段就 +1。旧档案读出来当过期（不然 7 天内一直读旧格式，
# 新加的「热评/弹幕」永远不出现）。
ARCHIVE_VERSION = 2

# 弹幕一段 6 分钟；长视频最多取前 4 段（24 分钟）——再往后拉请求数就不划算了
DANMAKU_SEGMENT_SECONDS = 360
DANMAKU_MAX_SEGMENTS = 4


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fmt_ts(seconds: int) -> str:
    """秒 → 3:05 / 1:02:05。和卡片里的时长用同一个格式。"""
    total = max(0, _int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


# ---------- 解析（全部离线可测） ----------


def normalize_conclusion(payload: Any) -> dict[str, Any] | None:
    """`view/conclusion/get` → {summary, outline, subtitle}。没内容返回 None。

    data.code: 0 有摘要 / 1 无摘要（可能排队） / -1 不支持（敏感内容等）。
    """
    if not isinstance(payload, dict):
        return None
    if _int(payload.get("code")) != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    if _int(data.get("code")) != 0:
        return None
    model = data.get("model_result")
    if not isinstance(model, dict):
        return None

    summary = str(model.get("summary") or "").strip()
    outline: list[dict[str, Any]] = []
    raw_outline = model.get("outline")
    if isinstance(raw_outline, list):
        for seg in raw_outline:
            if not isinstance(seg, dict):
                continue
            points: list[dict[str, Any]] = []
            raw_points = seg.get("part_outline")
            if isinstance(raw_points, list):
                for point in raw_points:
                    if not isinstance(point, dict):
                        continue
                    text = str(point.get("content") or "").strip()
                    if text:
                        points.append({"ts": _int(point.get("timestamp")), "text": text})
            outline.append(
                {
                    "ts": _int(seg.get("timestamp")),
                    "title": str(seg.get("title") or "").strip(),
                    "points": points[:3],
                }
            )

    subtitle: list[dict[str, Any]] = []
    raw_sub = model.get("subtitle")
    if isinstance(raw_sub, list):
        for group in raw_sub:
            if not isinstance(group, dict):
                continue
            raw_lines = group.get("part_subtitle")
            if not isinstance(raw_lines, list):
                continue
            for line in raw_lines:
                if not isinstance(line, dict):
                    continue
                text = str(line.get("content") or "").strip()
                if not text:
                    continue
                subtitle.append(
                    {
                        "from": _int(line.get("start_timestamp")),
                        "to": _int(line.get("end_timestamp")),
                        "text": text,
                    }
                )

    if not summary and not outline and not subtitle:
        return None
    return {"summary": summary, "outline": outline[:8], "subtitle": subtitle}


def normalize_subtitle_body(body: Any) -> list[dict[str, Any]]:
    """字幕 JSON 的 body → [{from, to, text}]。"""
    if not isinstance(body, list):
        return []
    out: list[dict[str, Any]] = []
    for line in body:
        if not isinstance(line, dict):
            continue
        text = str(line.get("content") or "").strip()
        if not text:
            continue
        out.append(
            {
                "from": _int(line.get("from")),
                "to": _int(line.get("to")),
                "text": text,
            }
        )
    return out


def pick_subtitle(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """优先中文（含 ai-zh / zh-CN），否则第一条。"""
    if not items:
        return None
    for item in items:
        lan = str(item.get("lan") or "").lower()
        doc = str(item.get("lan_doc") or "")
        if "zh" in lan or "中文" in doc:
            return item
    return items[0]


def sample_subtitle(
    lines: list[dict[str, Any]], max_chars: int = 320
) -> str:
    """把字幕压成覆盖全片的一段：按时间均匀取样，首尾都在，不只看开头。"""
    if not lines:
        return ""
    rendered = [
        f"[{fmt_ts(_int(line.get('from')))}] {str(line.get('text') or '').strip()}"
        for line in lines
    ]
    rendered = [text for text in rendered if text.strip()]
    if not rendered:
        return ""
    room = max(80, int(max_chars))
    total = sum(len(text) for text in rendered)
    if total <= room:
        return "".join(rendered)[:room]
    count = max(3, min(12, room // 28))
    if len(rendered) <= count:
        return "".join(rendered)[:room] + "…"
    indices = [round(i * (len(rendered) - 1) / (count - 1)) for i in range(count)]
    text = "".join(rendered[index] for index in indices)
    return text[:room] + "…"


# ---------- 评论（JSON） ----------


def normalize_comments(payload: Any, limit: int = 5) -> list[dict[str, Any]]:
    """`reply/wbi/main` → [{text, like}]：置顶 + 热评一起去重，按赞数排。

    只要「像句话」的：至少 4 个字、含中文/字母/数字，纯标点和纯表情跳过。
    """
    if not isinstance(payload, dict) or _int(payload.get("code")) != 0:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items: list[Any] = []
    for key in ("top_replies", "replies"):
        raw = data.get(key)
        if isinstance(raw, list):
            items.extend(raw)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        text = ""
        if isinstance(content, dict):
            text = str(content.get("message") or "")
        text = " ".join(text.split())
        if len(text) < 4 or text in seen:
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
            continue
        seen.add(text)
        out.append({"text": text[:80], "like": _int(item.get("like"))})
    out.sort(key=lambda entry: -entry["like"])
    return out[: max(1, int(limit))]


# ---------- 弹幕（protobuf） ----------
# seg.so 返回 DmSegMobileReply：field 1 = 一条弹幕（DanmakuElem），
# 里面 field 2 = 出现时间（毫秒）、field 7 = 文本。只认这两个字段，
# 手写 varint 解析，不引 protobuf 依赖（requirements.txt 里没有）。

_MEANINGLESS = re.compile(r"^[哈啊6w2a1!！？?。.~～\s]+$", re.IGNORECASE)
_LOW_VALUE_DANMAKU = frozenset(
    {"来了", "前排", "第一", "打卡", "哈哈", "233", "666", "awsl", "离谱", "好家伙"}
)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 截断")


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field, value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise ValueError("长度越界")
            yield field, buf[pos : pos + length]
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"不认的 wire type {wire}")


def parse_danmaku_seg(raw: Any) -> list[dict[str, Any]]:
    """一段弹幕 protobuf → [{ts(毫秒), text}]。坏数据返回已解析出的部分。"""
    if not isinstance(raw, (bytes, bytearray)):
        return []
    out: list[dict[str, Any]] = []
    try:
        for field, value in _iter_fields(bytes(raw)):
            if field != 1 or not isinstance(value, bytes):
                continue
            ts = 0
            text = ""
            for sub_field, sub_value in _iter_fields(value):
                if sub_field == 2 and isinstance(sub_value, int):
                    ts = sub_value
                elif sub_field == 7 and isinstance(sub_value, bytes):
                    text = sub_value.decode("utf-8", "replace").strip()
            if text:
                out.append({"ts": max(0, ts), "text": text})
    except ValueError:
        log.info("弹幕数据有截断，已解析 %d 条", len(out))
    return out


def _low_value(text: str) -> bool:
    if len(text) < 2:
        return True
    if text.lower() in _LOW_VALUE_DANMAKU:
        return True
    return bool(_MEANINGLESS.match(text))


def summarize_danmaku(
    lines: list[dict[str, Any]],
    duration: int = 0,
    *,
    bucket: int = 10,
    top: int = 3,
    samples: int = 3,
    gap: int = 2,
) -> list[dict[str, Any]]:
    """弹幕 → 最热闹的几个时刻：[{ts(秒), count, samples}]。

    不逐条读：几千条塞进 prompt 会挤掉人设。10 秒一桶，按条数取前几桶；
    相邻的桶（间隔 < gap 个桶）并成一段，免得「1:00、1:20、1:30」说三遍同一波。
    再从段里挑几条有内容的原文当样本（纯「哈哈哈」「666」跳过）。
    """
    if not lines:
        return []
    wide = max(1, int(bucket))
    buckets: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        ts = max(0, _int(line.get("ts"))) // 1000
        if duration and ts > _int(duration) + 60:
            continue
        buckets.setdefault(ts // wide, []).append(line)
    if not buckets:
        return []

    ranked = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    groups: list[dict[str, Any]] = []
    for key, items in ranked:
        target = None
        for group in groups:
            if any(abs(key - other) < max(1, int(gap)) for other in group["keys"]):
                target = group
                break
        if target is None:
            if len(groups) >= max(1, int(top)):
                continue
            groups.append({"keys": set(), "count": 0, "items": []})
            target = groups[-1]
        target["keys"].add(key)
        target["count"] += len(items)
        target["items"].extend(items)

    out: list[dict[str, Any]] = []
    for group in sorted(groups, key=lambda entry: min(entry["keys"])):
        picked: list[str] = []
        seen: set[str] = set()
        for item in sorted(group["items"], key=lambda entry: _int(entry.get("ts"))):
            text = " ".join(str(item.get("text") or "").split())
            if not text or _low_value(text) or text in seen:
                continue
            seen.add(text)
            picked.append(text[:24])
            if len(picked) >= max(1, int(samples)):
                break
        out.append(
            {
                "ts": min(group["keys"]) * wide,
                "count": group["count"],
                "samples": picked,
            }
        )
    return out


def _clean(text: Any) -> str:
    """去掉句尾标点：外层还会补「。」，不然拼出来是「。。」「。；」。"""
    return str(text or "").strip().rstrip("。．.!！?？；;，,、 ")


def format_archive_layered(
    archive: dict[str, Any] | None, max_chars: int = DEFAULT_MAX_CHARS
) -> str:
    """档案 → 分层文案：视频内容 / 观众评论 / 观众弹幕分开标。

    和 `format_archive` 的区别不是信息量，是**说话人**：热评和弹幕是观众说的，
    不是视频里的人说的。混在一起模型会把评论者当成“她”，把弹幕当成台词。
    """
    if not isinstance(archive, dict):
        return ""
    summary = _clean(archive.get("summary"))
    outline = archive.get("outline") or []
    subtitle = archive.get("subtitle") or []
    comments = archive.get("comments") or []
    danmaku = archive.get("danmaku") or []
    visible = [str(item) for item in (archive.get("visibility") or []) if str(item).strip()]
    room = max(300, int(max_chars))
    blocks: list[str] = []

    if summary:
        blocks.append("【视频内容·官方摘要】" + summary[: min(220, room // 3)])
    if isinstance(outline, list) and outline:
        parts = []
        for seg in outline[:5]:
            if not isinstance(seg, dict):
                continue
            title = _clean(seg.get("title"))
            if title:
                parts.append(f"{fmt_ts(_int(seg.get('ts')))} {title}"[:60])
        if parts:
            blocks.append("【视频内容·分段】" + "；".join(parts))
    if isinstance(subtitle, list) and subtitle:
        text = sample_subtitle(subtitle, 220)
        if text:
            blocks.append("【视频内容·字幕】" + text)
    if isinstance(comments, list) and comments:
        texts = []
        for item in comments[:3]:
            if not isinstance(item, dict):
                continue
            text = _clean(item.get("text"))
            if text:
                like = _int(item.get("like"))
                texts.append(f"{text[:40]}（{like}赞）" if like else text[:40])
        if texts:
            blocks.append("【观众评论·不是视频里的人说的】" + "；".join(texts))
    if isinstance(danmaku, list) and danmaku:
        peaks = []
        for peak in danmaku[:3]:
            if not isinstance(peak, dict):
                continue
            samples = [
                str(sample)[:12]
                for sample in (peak.get("samples") or [])
                if str(sample).strip()
            ]
            tail = f"（{'/'.join(samples[:2])}）" if samples else ""
            peaks.append(f"{fmt_ts(_int(peak.get('ts')))} 刷了{_int(peak.get('count'))}条{tail}")
        if peaks:
            blocks.append("【观众弹幕·观众反应】" + "；".join(peaks))

    if not blocks:
        return ""
    body = "\n".join(blocks)
    if len(body) > room:
        body = body[:room].rstrip()
    source = "、".join(visible) or "B站给的资料"
    return (
        f"这些是从{source}里分层看到的。视频画面你没看过，别编画面细节。"
        "评论和弹幕是观众说的，不是视频里的人说的。\n" + body
    )


def format_archive(archive: dict[str, Any] | None, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """档案 → 给模型看的一小段。没有可用内容返回空串。

    预算有限时按优先级保：摘要 > （无摘要时的）字幕 > 分段 > 热评 > 弹幕 >
    （有摘要时的）字幕。宁可少说一样，也别把整段挤爆。
    """
    if not isinstance(archive, dict):
        return ""
    summary = _clean(archive.get("summary"))
    outline = archive.get("outline") or []
    subtitle = archive.get("subtitle") or []
    comments = archive.get("comments") or []
    danmaku = archive.get("danmaku") or []
    visible = [str(item) for item in (archive.get("visibility") or []) if str(item).strip()]

    room = max(200, int(max_chars))
    blocks: list[tuple[int, str]] = []

    if summary:
        blocks.append((0, "官方摘要：" + summary[: room // 3]))
    if isinstance(outline, list) and outline:
        parts: list[str] = []
        for seg in outline[:5]:
            if not isinstance(seg, dict):
                continue
            title = _clean(seg.get("title"))
            if title:
                parts.append(f"{fmt_ts(_int(seg.get('ts')))} {title}"[:60])
        if parts:
            blocks.append((2, "分段：" + "；".join(parts)))
    if isinstance(comments, list) and comments:
        texts: list[str] = []
        for item in comments[:3]:
            if not isinstance(item, dict):
                continue
            text = _clean(item.get("text"))
            if not text:
                continue
            like = _int(item.get("like"))
            texts.append(f"{text[:40]}（{like}赞）" if like else text[:40])
        if texts:
            blocks.append((3, "热评：" + "；".join(texts)))
    if isinstance(danmaku, list) and danmaku:
        peaks: list[str] = []
        for peak in danmaku[:3]:
            if not isinstance(peak, dict):
                continue
            ts = _int(peak.get("ts"))
            count = _int(peak.get("count"))
            samples = [
                str(sample)[:12]
                for sample in (peak.get("samples") or [])
                if str(sample).strip()
            ]
            tail = f"（{'/'.join(samples[:2])}）" if samples else ""
            peaks.append(f"{fmt_ts(ts)} 刷了{count}条{tail}")
        if peaks:
            blocks.append((4, "弹幕峰值：" + "；".join(peaks)))
    if isinstance(subtitle, list) and subtitle:
        text = sample_subtitle(subtitle, 240 if summary else room // 2)
        if text:
            blocks.append((5 if summary else 1, ("字幕开头：" if summary else "字幕：") + text))

    if not blocks:
        return ""
    body = ""
    for _priority, text in sorted(blocks):
        if body and len(body) + len(text) + 1 > room:
            continue
        body = text if not body else f"{body}。{text}"
    if not body:
        return ""
    seen = "、".join(visible) or "B站给的资料"
    return f"这些是从{seen}里看到的，视频画面你没看过，别编画面里的细节：{body}"


KNOWLEDGE_MAX_CHARS = 400


def archive_knowledge_text(
    archive: dict[str, Any] | None, max_chars: int = KNOWLEDGE_MAX_CHARS
) -> str:
    """档案 → 知识库条目：纯内容，不带「你没看过画面」这种提示语。

    knowledge 的 text 上限只有 400 字（memory.KNOWLEDGE_TEXT_MAX），所以只放
    摘要 / 分段 / 热评 / 弹幕峰值；字幕不进 —— 采样后的字幕会把真正有用的挤掉。
    """
    if not isinstance(archive, dict):
        return ""
    parts: list[str] = []
    summary = _clean(archive.get("summary"))
    if summary:
        parts.append("摘要：" + summary)
    outline = archive.get("outline") or []
    if isinstance(outline, list) and outline:
        titles = [
            _clean(seg.get("title"))[:30]
            for seg in outline[:4]
            if isinstance(seg, dict) and _clean(seg.get("title"))
        ]
        if titles:
            parts.append("分段：" + "；".join(titles))
    comments = archive.get("comments") or []
    if isinstance(comments, list) and comments:
        texts = [
            _clean(item.get("text"))[:32]
            for item in comments[:3]
            if isinstance(item, dict) and _clean(item.get("text"))
        ]
        if texts:
            parts.append("热评：" + "；".join(texts))
    danmaku = archive.get("danmaku") or []
    if isinstance(danmaku, list) and danmaku:
        peaks: list[str] = []
        for peak in danmaku[:2]:
            if not isinstance(peak, dict):
                continue
            samples = [str(sample)[:10] for sample in (peak.get("samples") or []) if str(sample).strip()]
            text = f"{fmt_ts(_int(peak.get('ts')))} {_int(peak.get('count'))}条"
            if samples:
                text += f"（{'/'.join(samples[:2])}）"
            peaks.append(text)
        if peaks:
            parts.append("弹幕峰值：" + "；".join(peaks))
    body = "。".join(parts)
    return body[: max(80, int(max_chars))]


# ---------- 抓取 ----------


async def _fetch_danmaku(
    api: BiliApi, cid: str, aid: str, duration: int
) -> list[dict[str, Any]]:
    """按视频时长取前几段弹幕（最多 4 段 = 24 分钟），汇总成峰值。"""
    seconds = max(1, _int(duration))
    segments = max(
        1,
        min(
            DANMAKU_MAX_SEGMENTS,
            (seconds + DANMAKU_SEGMENT_SECONDS - 1) // DANMAKU_SEGMENT_SECONDS,
        ),
    )
    lines: list[dict[str, Any]] = []
    for index in range(1, segments + 1):
        raw = await api.danmaku_segment(cid, aid, index)
        if raw:
            lines.extend(parse_danmaku_seg(raw))
    return summarize_danmaku(lines, duration)


async def build_archive(
    api: BiliApi,
    info: dict[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: float = 25.0,
    extras: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """按 info（normalize_view 的结果）抓一份档案。任何失败都写进 errors，不抛。

    extras=True 时额外抓热评和弹幕峰值（各一次/几段请求，仍在后台跑）。
    """
    bvid = str(info.get("bvid") or "")
    cid = str(info.get("cid") or "")
    aid = str(info.get("aid") or "")
    stamp = int(time.time() if now is None else now)
    archive: dict[str, Any] = {
        "version": ARCHIVE_VERSION,
        "bvid": bvid,
        "title": str(info.get("title") or ""),
        "up": str(info.get("owner") or ""),
        "fetched_at": stamp,
        "ok": False,
        "summary": "",
        "outline": [],
        "subtitle": [],
        "comments": [],
        "danmaku": [],
        "visibility": [],
        "errors": [],
    }
    if not bvid or not cid:
        archive["errors"].append("缺 bvid/cid，没法查总结")
        return archive

    async def run() -> None:
        # 1) 官方总结 / 字幕
        try:
            payload = await api.conclusion(bvid, cid, str(info.get("owner_mid") or ""))
            parsed = normalize_conclusion(payload)
            if parsed:
                archive["summary"] = parsed["summary"]
                archive["outline"] = parsed["outline"]
                archive["subtitle"] = parsed["subtitle"]
                if parsed["summary"]:
                    archive["visibility"].append("官方AI总结")
                if parsed["outline"]:
                    archive["visibility"].append("分段提纲")
                if parsed["subtitle"]:
                    archive["visibility"].append("AI字幕")
            else:
                # 没总结（未登录 / 视频没语音 / 还在排队）：退到字幕接口
                items = await api.subtitle_list(aid, cid) if aid else []
                picked = pick_subtitle(items)
                if not picked or not is_media_url(str(picked.get("url") or "")):
                    archive["errors"].append("没拿到总结或字幕")
                else:
                    body = await api.subtitle_body(str(picked["url"]))
                    lines = normalize_subtitle_body(body)
                    if not lines:
                        archive["errors"].append("字幕文件是空的")
                    else:
                        archive["subtitle"] = lines
                        archive["visibility"].append("AI字幕")
        except Exception as exc:  # noqa: BLE001 - 一步坏了不能拖垮整份档案
            archive["errors"].append(f"总结:{type(exc).__name__}")

        # 2) 热评（观众怎么看）
        if extras and aid:
            try:
                comments = normalize_comments(await api.comments(aid))
                if comments:
                    archive["comments"] = comments
                    archive["visibility"].append("热评")
            except Exception as exc:  # noqa: BLE001
                archive["errors"].append(f"热评:{type(exc).__name__}")

        # 3) 弹幕峰值（观众在哪一刻最热闹）
        if extras and cid:
            try:
                peaks = await _fetch_danmaku(api, cid, aid, _int(info.get("duration")))
                if peaks:
                    archive["danmaku"] = peaks
                    archive["visibility"].append("弹幕峰值")
            except Exception as exc:  # noqa: BLE001
                archive["errors"].append(f"弹幕:{type(exc).__name__}")

    try:
        # 下限给 0.1s：调用方（BiliCards）保证生产上 >=3s，这里只防死等
        await asyncio.wait_for(run(), timeout=max(0.1, float(timeout)))
    except (asyncio.TimeoutError, TimeoutError):
        archive["errors"].append("抓档案超时")
    except Exception as exc:  # noqa: BLE001 - 档案坏了不能影响聊天
        archive["errors"].append(type(exc).__name__)
        log.info("B站档案抓取失败（%s）：%s", type(exc).__name__, bvid)

    # 字幕全量存会很大（十小时视频能上百 KB）：够采样就行，别把磁盘和读盘时间吃满
    if len(archive["subtitle"]) > 3000:
        archive["subtitle"] = archive["subtitle"][:3000]
    archive["ok"] = bool(
        archive["summary"]
        or archive["outline"]
        or archive["subtitle"]
        or archive["comments"]
        or archive["danmaku"]
    )
    return archive


# ---------- 落盘 ----------


class ArchiveStore:
    """一个视频一个 JSON。成功的存 7 天，失败的只存 6 小时（别反复敲）。"""

    def __init__(
        self,
        directory: str | Path,
        ttl_days: int = 7,
        fail_ttl_hours: float = 6.0,
    ) -> None:
        self.dir = Path(directory)
        self.ttl = max(1, int(ttl_days)) * 86400
        self.fail_ttl = max(0.5, float(fail_ttl_hours)) * 3600

    def path_of(self, bvid: str) -> Path | None:
        text = str(bvid or "").strip()
        if not _BVID_RE.match(text):
            return None
        return self.dir / f"{text}.json"

    def load(self, bvid: str, now: float | None = None) -> dict[str, Any] | None:
        path = self.path_of(bvid)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.info("B站档案读不了：%s", path.name)
            return None
        if not isinstance(data, dict):
            return None
        if _int(data.get("version")) < ARCHIVE_VERSION:
            log.info("B站档案是旧格式（v%s），重新抓", data.get("version"))
            return None
        stamp = time.time() if now is None else float(now)
        age = stamp - float(data.get("fetched_at") or 0)
        ttl = self.ttl if data.get("ok") else self.fail_ttl
        if age < 0 or age > ttl:
            return None
        return data

    def save(self, bvid: str, data: dict[str, Any]) -> bool:
        path = self.path_of(bvid)
        if path is None:
            return False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return True
        except OSError:
            log.info("B站档案写不了：%s", bvid)
            return False

    def status(self) -> dict[str, Any]:
        try:
            count = sum(1 for _ in self.dir.glob("BV*.json")) if self.dir.is_dir() else 0
        except OSError:
            count = 0
        return {"dir": str(self.dir), "files": count, "ttl_days": self.ttl // 86400}
