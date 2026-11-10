"""B 站评论的纯解析层：payload → 数据库行、游标推进、完整度口径。

不联网、不碰 SQLite，方便 `tools/test_chris_comments.py` 离线测。
采集策略和字段口径见 `docs/克里斯语料.md`。

为什么单独一层
--------------
B 站评论接口的字段很松：`data` 可能是空、`replies` 可能是 null、`rpid` 可能是
字符串、子评论的 `parent` 可能缺失。把这些判断写进爬虫里，测试只能靠联网；
放这里以后，所有边界都用假 payload 覆盖。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

# 一页固定 20。实测（2026-10-28）热评和时间序首页都是 20，翻页也是 20；
# 上游别处写的「每页最多 3 条」在当前接口下不成立，这里不写死 3。
PAGE_SIZE = 20

# 覆盖率的容差：服务端 all_count 含子评论，且会漏算/多算一两条
# （实测 49 根 + 117 子 = 166，all_count = 167）。按 0.5% 或至少 1 条算达标。
COVERAGE_TOLERANCE = 0.005
MIN_TOLERANCE = 1


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def uid_hash(mid: Any, salt: str = "") -> str:
    """发布者 UID → 16 位哈希。只用于「不同的人都在说」的计数，不建用户画像。"""
    return hashlib.sha256(f"{salt}|{_int(mid)}".encode()).hexdigest()[:16]


def page_data(payload: Any) -> dict[str, Any]:
    """`{code:0,data:{...}}` → data；错误码/空壳一律返回 {}。"""
    if not isinstance(payload, dict):
        return {}
    if _int(payload.get("code")) != 0:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def page_replies(payload: Any) -> list[dict[str, Any]]:
    """一页评论里的 replies；过滤掉不是对象的条目。"""
    replies = page_data(payload).get("replies")
    if not isinstance(replies, list):
        return []
    return [r for r in replies if isinstance(r, dict)]


def page_cursor(payload: Any) -> dict[str, Any]:
    cursor = page_data(payload).get("cursor")
    return cursor if isinstance(cursor, dict) else {}


def next_offset(cursor: dict[str, Any]) -> str:
    """cursor.pagination_reply.next_offset；没有就是空串。"""
    reply = cursor.get("pagination_reply")
    if not isinstance(reply, dict):
        return ""
    offset = reply.get("next_offset")
    return offset if isinstance(offset, str) else (str(offset) if offset else "")


def page_is_end(cursor: dict[str, Any]) -> bool:
    return bool(cursor.get("is_end"))


def all_count(cursor: dict[str, Any]) -> int:
    return _int(cursor.get("all_count"))


def in_window(ctime: int, since: int = 0, until: int = 0) -> bool:
    """评论时间是否落在 [since, until]；0 表示该侧不限制。"""
    if since and ctime < since:
        return False
    if until and ctime > until:
        return False
    return True


def _message(reply: dict[str, Any]) -> str:
    content = reply.get("content")
    if isinstance(content, dict):
        return _str(content.get("message"))
    return ""


def root_record(reply: dict[str, Any], aid: int, now: int, salt: str = "") -> dict[str, Any]:
    """一级评论 → comment 表的一行。rpid 缺失时返回空 dict。"""
    rpid = _int(reply.get("rpid"))
    if not rpid:
        return {}
    return {
        "rpid": rpid,
        "aid": int(aid),
        "root": 0,
        "parent": 0,
        "ctime": _int(reply.get("ctime")),
        "message": _message(reply),
        "like": _int(reply.get("like")),
        "rcount": _int(reply.get("rcount")),
        "uid_hash": uid_hash(reply.get("mid"), salt),
        "first_seen": now,
        "last_seen": now,
        "deleted": 0,
    }


def sub_record(
    reply: dict[str, Any], aid: int, root: int, now: int, salt: str = ""
) -> dict[str, Any]:
    """楼中楼 → comment 表的一行。`parent` 可能是另一条楼中楼，`root` 始终是根。"""
    rpid = _int(reply.get("rpid"))
    if not rpid:
        return {}
    return {
        "rpid": rpid,
        "aid": int(aid),
        "root": int(root),
        "parent": _int(reply.get("parent")) or int(root),
        "ctime": _int(reply.get("ctime")),
        "message": _message(reply),
        "like": _int(reply.get("like")),
        "rcount": 0,
        "uid_hash": uid_hash(reply.get("mid"), salt),
        "first_seen": now,
        "last_seen": now,
        "deleted": 0,
    }


def coverage(root_count: int, sub_count: int, expected: int) -> float:
    """（一级 + 子评论）/ 服务端 all_count；分母为 0 时算 1.0。"""
    if expected <= 0:
        return 1.0
    return min(1.0, (max(0, root_count) + max(0, sub_count)) / float(expected))


def complete_enough(local: int, expected: int) -> bool:
    """本地条数是否够到「全」。差 1 条属正常误差（all_count 含子评论且会变）。"""
    if expected <= 0:
        return True
    slack = max(MIN_TOLERANCE, math.ceil(expected * COVERAGE_TOLERANCE))
    return local >= expected - slack


def normalize_space_videos(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """`space/wbi/arc/search` → (视频列表, 该 UP 的投稿总数)。

    视频项保留 aid/bvid/title/pubdate/comment（评论总数，plan 用来估预算）。
    """
    data = page_data(payload)
    if not data:
        return [], 0
    section = data.get("list")
    vlist = section.get("vlist") if isinstance(section, dict) else None
    page = data.get("page")
    total = _int(page.get("count")) if isinstance(page, dict) else 0
    if not isinstance(vlist, list):
        return [], total
    out: list[dict[str, Any]] = []
    for item in vlist:
        if not isinstance(item, dict):
            continue
        bvid = _str(item.get("bvid")).strip()
        aid = _int(item.get("aid"))
        if not bvid or not aid:
            continue
        out.append(
            {
                "aid": aid,
                "bvid": bvid,
                "title": _str(item.get("title")).strip(),
                "pubdate": _int(item.get("created")),
                "comment": _int(item.get("comment")),
            }
        )
    return out, total
