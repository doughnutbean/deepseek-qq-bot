"""B 站接口底座：WBI 签名、限速、失败退避（0 token，只发 HTTP）。

哪些接口要签名
--------------
2023-03 起 Web 端部分接口要求 `w_rid` + `wts`：`/x/player/wbi/v2`（字幕列表）、
`/x/web-interface/view/conclusion/get`（官方 AI 总结）。签名缺了**不会回错误码**，
而是回一个 `v_voucher` —— 看着像成功、其实没数据，所以这里统一签名，别绕。

为什么不装 bilibili-api-python
------------------------------
项目里全是 httpx + async，那个库是同步 requests，引进来要多一层线程池和一套
新依赖。签名本身几十行，还有官方文档给的固定测试向量（见
`tools/test_bili_archive.py`），自己写更好核对。

风控上做什么（社区 B 站爬取工具的共识）
--------------------------------------
- 真实 UA + Referer，cookie 里带一个**固定的** `buvid3`（`data/bili/buvid.json`，
  不每次换，换得勤反而像脚本）；
- 全局最小间隔（默认 1s），不并发打接口；
- 连续失败进冷却（10 分钟），冷却期里一个请求都不发；
- 失败也由调用方落盘，同一个视频不会反复敲门（见 `bili_archive.py`）；
- SESSDATA 只从配置来，**日志、异常、返回值里都不出现**。

边界
----
所有接口失败都返回 None，绝不抛给聊天链路；调用方自己决定降不降级。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlencode

import httpx

log = logging.getLogger(__name__)

API = "https://api.bilibili.com"
NAV_API = f"{API}/x/web-interface/nav"
CONCLUSION_API = f"{API}/x/web-interface/view/conclusion/get"
PLAYER_API = f"{API}/x/player/wbi/v2"
REPLY_API = f"{API}/x/v2/reply/wbi/main"
SUB_REPLY_API = f"{API}/x/v2/reply/reply"
SPACE_ARC_API = f"{API}/x/space/wbi/arc/search"
DYNAMIC_SPACE_API = f"{API}/x/polymer/web-dynamic/v1/feed/space"
VIEW_API = f"{API}/x/web-interface/view"
DANMAKU_API = f"{API}/x/v2/dm/web/seg.so"
SEARCH_API = f"{API}/x/web-interface/wbi/search/type"
PLAYURL_API = f"{API}/x/player/wbi/playurl"
# 首页推荐：新版走 WBI；老接口做一次失败兜底，不再重试
HOME_FEED_API = f"{API}/x/web-interface/wbi/index/top/feed/rcmd"
HOME_FEED_LEGACY_API = f"{API}/x/web-interface/index/top/rcmd"
# 写操作（点赞/关注/评论/收藏）：必须带 cookie 里的 bili_jct 当 csrf
LIKE_API = f"{API}/x/web-interface/archive/like"
RELATION_API = f"{API}/x/relation/modify"
REPLY_ADD_API = f"{API}/x/v2/reply/add"
FAV_DEAL_API = f"{API}/x/v3/fav/resource/deal"
FAV_FOLDER_LIST_API = f"{API}/x/v3/fav/folder/created/list-all"
FAV_FOLDER_ADD_API = f"{API}/x/v3/fav/folder/add"
# 直播状态走直播站自己的接口，不用 WBI；失败一律当没查到
LIVE_API = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
LIVE_ROOM_API = "https://api.live.bilibili.com/room/v1/Room/get_info"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# WBI 签名的重排映射表（B 站前端硬编码，官方文档也抄了这份）
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# 字幕文件只认这些域：URL 是 B 站接口给的，但仍然当外部输入看（别拿它当通用下载器）
_MEDIA_HOSTS = (".hdslb.com", ".bilibili.com", ".bilivideo.com", ".bilibili.tv")

# 密钥每天换一次：缓存一小时足够，别每个请求都去 nav
WBI_TTL_SECONDS = 3600
# 评论 cursor 接口的兜底 key 对：BiliSpider 长期用常量签这个接口；动态 nav key
# 偶发 -352 时，用它再签一次。只用于评论翻页，别的接口仍走 nav。
CURSOR_FIXED_IMG_KEY = "839c8b697b0d44dc80e9a604592bb432"
CURSOR_FIXED_SUB_KEY = "02cd020b04d64aacad6b3a08d06f8eb0"
# 连续失败几次进冷却
FAIL_LIMIT = 3
COOLDOWN_SECONDS = 600


def mixin_key(img_key: str, sub_key: str) -> str:
    """img_key + sub_key 按表重排，取前 32 位。长度不够就空串（调用方当成失败）。"""
    raw = f"{img_key}{sub_key}"
    if len(raw) < 64:
        return ""
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB[:32])


def sign_params(
    params: dict[str, Any], mixin: str, wts: int | None = None
) -> dict[str, str]:
    """补 wts、过滤 `!'()*`、k 排序、算 w_rid。返回可直接编码的参数字典。"""
    clean = {
        str(key): "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in params.items()
    }
    clean["wts"] = str(int(time.time()) if wts is None else int(wts))
    query = urlencode(sorted(clean.items()), quote_via=quote, safe="")
    clean["w_rid"] = hashlib.md5(f"{query}{mixin}".encode()).hexdigest()
    return clean


def sign_query(
    params: dict[str, Any], mixin: str, wts: int | None = None
) -> str:
    """签名后的完整 query（签名部分 + `&w_rid=`），和官方文档的示例同形。"""
    if not mixin:
        return ""
    signed = sign_params(params, mixin, wts)
    w_rid = signed.pop("w_rid")
    query = urlencode(sorted(signed.items()), quote_via=quote, safe="")
    return f"{query}&w_rid={w_rid}"


def sign_with_keys(
    params: dict[str, Any], img_key: str, sub_key: str, wts: int | None = None
) -> str:
    """给测试/调试用：直接拿 img_key/sub_key 签。"""
    return sign_query(params, mixin_key(img_key, sub_key), wts)


def is_media_url(url: str) -> bool:
    """字幕/图片地址是否在 B 站自己的域下（防 SSRF）。http/https 都收：
    `view` 的 `pic` 至今还常给 `http://i1.hdslb.com/...`，只认 https 会漏掉封面。"""
    text = (url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    if not text.startswith(("https://", "http://")):
        return False
    host = (urlsplit(text).hostname or "").lower()
    return any(host == suffix[1:] or host.endswith(suffix) for suffix in _MEDIA_HOSTS)


def normalize_search(
    payload: Any, limit: int = 5, kind: str = "video"
) -> list[dict[str, Any]]:
    """`search/type` → 视频 [{bvid,title,author,play,duration}] 或用户 [{mid,name,sign,fans}]。"""
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("result")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items[: max(1, int(limit))]:
        if not isinstance(item, dict):
            continue
        if kind == "user":
            mid = str(item.get("mid") or "").strip()
            name = str(item.get("uname") or "").strip()
            if not mid or not name:
                continue
            out.append(
                {
                    "mid": mid,
                    "name": name,
                    "sign": str(item.get("usign") or "").strip(),
                    "fans": item.get("fans") or 0,
                    "videos": item.get("videos") or 0,
                }
            )
            continue
        bvid = str(item.get("bvid") or "").strip()
        title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
        if not bvid.startswith("BV"):
            continue
        out.append(
            {
                "bvid": bvid,
                "title": title,
                "author": re.sub(
                    r"<[^>]+>", "", str(item.get("author") or "")
                ).strip(),
                "mid": str(item.get("mid") or "").strip(),
                "pubdate": _num(item.get("pubdate")),
                "play": item.get("play") or 0,
                "duration": str(item.get("duration") or "").strip(),
            }
        )
    return out


def normalize_home_feed(payload: Any, limit: int = 12) -> list[dict[str, Any]]:
    """首页推荐 → 普通视频候选；直播、广告、侧边栏一律丢掉。

    这里只做“候选初筛”，标题/推荐理由都是不可信素材；真正选片前还要查
    `view` 核对播放/点赞/评论，并判重。
    """
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("item") or data.get("items") or []
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("goto") not in (None, "", "av"):
            continue
        if item.get("is_ad") or item.get("ad_info") or item.get("business_info"):
            continue
        bvid = str(item.get("bvid") or "").strip()
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or bvid in seen:
            continue
        title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
        if not title:
            continue
        stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        reason = item.get("rcmd_reason")
        reason_text = (
            str(reason.get("content") or "").strip()
            if isinstance(reason, dict)
            else str(item.get("reason") or "").strip()
        )
        out.append(
            {
                "bvid": bvid,
                "title": title[:160],
                "author": str(owner.get("name") or item.get("author") or "").strip()[:80],
                "mid": str(owner.get("mid") or "").strip(),
                "play": _num(stat.get("view", item.get("play"))),
                "like": _num(stat.get("like")),
                "reply": _num(stat.get("reply", item.get("review"))),
                "danmaku": _num(stat.get("danmaku", item.get("danmaku"))),
                "duration": _num(item.get("duration")),
                "pubdate": _num(item.get("pubdate")),
                "reason": reason_text[:100],
                "source": "home",
            }
        )
        seen.add(bvid)
        if len(out) >= max(1, int(limit)):
            break
    return out


def normalize_space_feed(payload: Any, limit: int = 12) -> list[dict[str, Any]]:
    """`polymer/.../feed/space` → UP 最近的投稿（只认视频动态）。

    为什么要这个接口：`space/wbi/arc/search`（UP 投稿页）现在对脚本经常回
    412，`chris_crawler` 里也得靠「风控就暂停」兜着；动态页是 B 站自己首页
    信息流用的，反而稳定，且一次就能拿到最近 12 条投稿（含直播回放）。
    返回字段和 `normalize_search` 对齐，`_format_videos` 直接能用。
    """
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return []
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "DYNAMIC_TYPE_AV":
            continue
        modules = item.get("modules")
        if not isinstance(modules, dict):
            continue
        dynamic = modules.get("module_dynamic")
        major = dynamic.get("major") if isinstance(dynamic, dict) else None
        archive = major.get("archive") if isinstance(major, dict) else None
        if not isinstance(archive, dict):
            continue
        bvid = str(archive.get("bvid") or "").strip()
        if not bvid.startswith("BV"):
            continue
        author = modules.get("module_author")
        author = author if isinstance(author, dict) else {}
        stat = archive.get("stat")
        stat = stat if isinstance(stat, dict) else {}
        out.append(
            {
                "bvid": bvid,
                "title": str(archive.get("title") or "").strip(),
                "author": str(author.get("name") or "").strip(),
                "mid": str(author.get("mid") or "").strip(),
                "pubdate": _num(author.get("pub_ts")),
                "play": _num(stat.get("play")),
                "duration": str(archive.get("duration_text") or "").strip(),
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


def normalize_live(payload: Any, mid: str = "") -> dict[str, Any] | None:
    """`getRoomInfoOld` → {live, room_id, title, online, url}。没直播间返回 None。"""
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    room = _num(data.get("roomid"))
    if not room:
        return None
    # liveStatus 才是「正在播」；roomStatus 只表示房间存在，别把没播的算成在播
    flag = data.get("liveStatus")
    if flag is None:
        flag = data.get("roomStatus")
    return {
        "mid": str(mid),
        "room_id": room,
        "live": bool(_num(flag)),
        "title": str(data.get("title") or "").strip(),
        "online": _num(data.get("online")),
        "url": str(data.get("url") or "").strip(),
    }


def normalize_live_room(payload: Any, room: str = "") -> dict[str, Any] | None:
    """`get_info` → {live, room_id, title, online}。房间不存在返回 None。"""
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    room_id = _num(data.get("room_id")) or _num(room)
    if not room_id:
        return None
    flag = data.get("live_status")
    if flag is None:
        flag = data.get("liveStatus")
    return {
        "room_id": str(room_id),
        "live": bool(_num(flag)),
        "title": str(data.get("title") or "").strip(),
        "online": _num(data.get("online")),
    }


def normalize_playurl(payload: Any, qn: int = 0) -> str:
    """`playurl` → 一条视频流地址。

    `qn=0` 时取最低码率（旧行为）；指定 qn 时优先取同档位，没有就取不高于
    该档位里最高的一档。以前无论传什么 qn 都取最低码率，等于画质档位没生效。
    """
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    dash = data.get("dash")
    if isinstance(dash, dict):
        videos = [item for item in (dash.get("video") or []) if isinstance(item, dict)]
        if videos:
            def video_id(item: dict[str, Any]) -> int:
                return _num(item.get("id") or item.get("quality"))

            videos.sort(key=lambda item: _num(item.get("bandwidth")))
            wanted = max(0, int(qn or 0))
            if wanted:
                eligible = [
                    item for item in videos if 0 < video_id(item) <= wanted
                ]
                exact = [item for item in eligible if video_id(item) == wanted]
                if exact:
                    pool = exact
                elif eligible:
                    best_id = max(video_id(item) for item in eligible)
                    pool = [item for item in eligible if video_id(item) == best_id]
                else:
                    # 只有比请求更高的档位：宁可给最低的，别给 1080p 巨流
                    pool = videos[:1]
                videos = pool
            for item in videos:
                url = str(item.get("baseUrl") or item.get("base_url") or "").strip()
                if url and is_media_url(url):
                    return url
    durl = data.get("durl")
    if isinstance(durl, list):
        for item in durl:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url and is_media_url(url):
                return url
    return ""


def _num(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    """写接口统一回执：ok / code / error / uncertain，调用方只看这四个字段。"""
    if not isinstance(payload, dict):
        return {"ok": False, "code": -1, "error": "没有返回", "uncertain": True}
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = -1
    if code == 0:
        return {"ok": True, "code": 0, "data": payload.get("data")}
    return {
        "ok": False,
        "code": code,
        "error": str(payload.get("message") or payload.get("msg") or f"code={code}")[:120],
        "uncertain": bool(payload.get("uncertain")),
    }


def ensure_buvid3(state_dir: str | Path | None) -> str:
    """取一个固定的 buvid3，没有就生成并落盘；重启也不换。

    为什么要持久化：B 站把 buvid3 当设备指纹，换得勤更像脚本，更容易被风控。

    为什么所有 B 站请求都要带它（2026-11-04 实锤）：
    `x/web-interface/view` 这类接口**只带 SESSDATA 也会 412**，
    必须是 `SESSDATA + buvid3` 才 200。bili.py 之前一个 cookie 都不带，
    于是视频信息整片 412，她表现成「打不开视频」。
    """
    path: Path | None = None
    if state_dir:
        path = Path(state_dir) / "buvid.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = str((data or {}).get("buvid3") or "").strip()
            if value:
                return value
        except (OSError, ValueError):
            pass

    value = f"{uuid.uuid4()}".upper() + "infoc"
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"buvid3": value}), encoding="utf-8")
        except OSError:
            log.debug("buvid3 落盘失败（不影响使用）", exc_info=True)
    return value


class BiliApi:
    """一个很薄的接口客户端。所有失败返回 None，不抛。"""

    def __init__(
        self,
        sessdata: str = "",
        timeout: float = 6.0,
        rate: float = 1.0,
        state_dir: str | Path | None = None,
        fail_limit: int = FAIL_LIMIT,
        cooldown: float = COOLDOWN_SECONDS,
        bili_jct: str = "",
    ) -> None:
        self.sessdata = (sessdata or "").strip()
        self.bili_jct = (bili_jct or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.rate = max(0.0, float(rate))
        self._state_dir = Path(state_dir) if state_dir else None
        self._client: httpx.AsyncClient | None = None
        # 间隔闸门：所有请求排队过一个门，保证最小间隔（不保证网络层不重叠）
        self._gate = asyncio.Lock()
        self._last = 0.0
        self._mixin = ""
        self._mixin_at = 0.0
        self._fails = 0
        self._blocked_until = 0.0
        self._fail_limit = max(1, int(fail_limit))
        self._cooldown = max(0.0, float(cooldown))
        self._buvid3 = ""
        self.calls = 0
        self.errors = 0
        self._ensure_buvid3()

    # ---------- cookie ----------

    def _ensure_buvid3(self) -> None:
        self._buvid3 = ensure_buvid3(self._state_dir)

    def _headers(self) -> dict[str, str]:
        headers = dict(BASE_HEADERS)
        parts: list[str] = []
        if self.sessdata:
            parts.append(f"SESSDATA={self.sessdata}")
        if self.bili_jct:
            parts.append(f"bili_jct={self.bili_jct}")
        if self._buvid3:
            parts.append(f"buvid3={self._buvid3}")
        if parts:
            headers["Cookie"] = "; ".join(parts)
        return headers

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers=self._headers(),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------- 闸门 ----------

    def _cooling(self) -> bool:
        return time.monotonic() < self._blocked_until

    def blocked_for(self) -> float:
        """还要冷却多少秒；采集器据此决定是等还是停。"""
        return max(0.0, self._blocked_until - time.monotonic())

    async def _throttle(self) -> None:
        async with self._gate:
            gap = self.rate - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()

    def _record(self, ok: bool) -> None:
        if ok:
            self._fails = 0
            return
        self.errors += 1
        self._fails += 1
        # 不要把 _fails 清零：清零 + 冷却会让「失败→冷却→再失败」无限循环，
        # 采集器永远等不到停止条件（2026-10-28 实锤卡在投稿列表第 20 页）。
        if self._fails >= self._fail_limit and not self._cooling():
            self._blocked_until = time.monotonic() + self._cooldown
            log.warning("B站接口连着失败，暂停 %d 秒再试", int(self._cooldown))

    # ---------- 请求 ----------

    async def request_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        signed: bool = True,
        mixin: str = "",
    ) -> dict[str, Any] | None:
        """发一个 GET。返回整个 JSON（code 交给调用方判断）；网络层失败返回 None。

        `mixin` 非空时用它签名，不去 nav 取 key（评论接口的兜底路径用）。
        """
        if self._cooling():
            log.debug("B站接口冷却中，跳过：%s", url)
            return None
        query = ""
        if signed:
            use_mixin = mixin or await self._ensure_mixin()
            if not use_mixin:
                return None
            query = sign_query(params or {}, use_mixin)
            target = f"{url}?{query}"
        else:
            target = url
            query = urlencode(params or {})
            if query:
                target = f"{url}?{query}"

        await self._throttle()
        self.calls += 1
        try:
            resp = await self._http().get(target)
        except Exception as exc:  # noqa: BLE001 - 查不到不能影响聊天
            self._record(False)
            log.info("B站接口异常（%s）：%s", type(exc).__name__, urlsplit(url).path)
            return None
        if resp.status_code != 200:
            self._record(False)
            log.info("B站接口 HTTP %s：%s", resp.status_code, urlsplit(url).path)
            return None
        try:
            payload = resp.json()
        except ValueError:
            self._record(False)
            log.info("B站接口返回不是 JSON：%s", urlsplit(url).path)
            return None
        if not isinstance(payload, dict):
            self._record(False)
            return None
        code = payload.get("code")
        if code in (-352, -412, -403):
            self._record(False)
            log.info("B站接口被风控 code=%s：%s", code, urlsplit(url).path)
            return None
        self._record(True)
        return payload

    async def post_json(
        self, url: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """发一个带 csrf 的写请求。失败返回 dict（code/message），不抛。

        写操作和读操作共用同一个限速门；没有 `bili_jct` 直接拒绝，
        绝不拿 SESSDATA 硬试 —— 那样只会把号送进风控。
        """
        if self._cooling():
            return {"code": -1, "message": "接口冷却中，先不发"}
        if not self.bili_jct:
            return {"code": -101, "message": "没配 bili_jct，写不了"}
        body = {k: v for k, v in (data or {}).items() if v is not None}
        body["csrf"] = self.bili_jct
        await self._throttle()
        self.calls += 1
        try:
            resp = await self._http().post(url, data=body)
        except Exception as exc:  # noqa: BLE001
            self._record(False)
            log.info("B站写接口异常（%s）：%s", type(exc).__name__, urlsplit(url).path)
            # 网络断了：请求可能已经到服务器，调用方必须按“不确定”处理，不能重试
            return {"code": -1, "message": "网络异常，结果不确定", "uncertain": True}
        if resp.status_code != 200:
            self._record(False)
            log.info("B站写接口 HTTP %s：%s", resp.status_code, urlsplit(url).path)
            return {
                "code": -1,
                "message": f"HTTP {resp.status_code}",
                "uncertain": resp.status_code >= 500,
            }
        try:
            payload = resp.json()
        except ValueError:
            self._record(False)
            return {"code": -1, "message": "返回不是 JSON", "uncertain": True}
        if not isinstance(payload, dict):
            return {"code": -1, "message": "返回为空", "uncertain": True}
        code = payload.get("code")
        if code in (-352, -412, -403):
            self._record(False)
        elif code == 0:
            self._record(True)
        return payload

    async def _ensure_mixin(self) -> str:
        now = time.monotonic()
        if self._mixin and now - self._mixin_at < WBI_TTL_SECONDS:
            return self._mixin
        payload = await self.request_json(NAV_API, signed=False)
        if not payload:
            return ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        wbi = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi, dict):
            log.info("B站 nav 没给 wbi_img（code=%s）", payload.get("code"))
            return ""
        img_key = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        mixin = mixin_key(img_key, sub_key)
        if not mixin:
            return ""
        self._mixin = mixin
        self._mixin_at = now
        return mixin

    # ---------- 具体接口 ----------

    async def conclusion(self, bvid: str, cid: str, up_mid: str = "") -> dict[str, Any] | None:
        """官方 AI 总结（摘要 + 分段提纲 + AI 字幕）。没登录/视频不支持就 None。"""
        params: dict[str, Any] = {"bvid": bvid, "cid": cid}
        if up_mid:
            params["up_mid"] = up_mid
        return await self.request_json(CONCLUSION_API, params)

    async def subtitle_list(self, aid: str, cid: str) -> list[dict[str, Any]]:
        """视频的字幕列表（每项有 lan / lan_doc / subtitle_url）。"""
        payload = await self.request_json(PLAYER_API, {"aid": aid, "cid": cid})
        if not payload:
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        items = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("subtitle_url") or "").strip()
            if not url or not is_media_url(url):
                continue
            out.append(
                {
                    "lan": str(item.get("lan") or ""),
                    "lan_doc": str(item.get("lan_doc") or ""),
                    "url": url
                    if url.startswith(("http://", "https://"))
                    else f"https:{url}",
                }
            )
        return out

    async def subtitle_body(self, url: str) -> list[dict[str, Any]]:
        """字幕 JSON 的 body：每句 {from, to, content}。"""
        if not is_media_url(url):
            return []
        if self._cooling():
            return []
        await self._throttle()
        self.calls += 1
        try:
            resp = await self._http().get(url)
        except Exception as exc:  # noqa: BLE001
            self._record(False)
            log.info("字幕下载异常（%s）", type(exc).__name__)
            return []
        if resp.status_code != 200:
            self._record(False)
            log.info("字幕下载 HTTP %s", resp.status_code)
            return []
        try:
            payload = resp.json()
        except ValueError:
            self._record(False)
            return []
        self._record(True)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, list):
            return []
        return [item for item in body if isinstance(item, dict)]

    async def request_bytes(
        self, url: str, params: dict[str, Any] | None = None
    ) -> bytes | None:
        """发一个 GET，只要字节（弹幕是 protobuf）。失败返回 None。"""
        if self._cooling():
            return None
        await self._throttle()
        self.calls += 1
        try:
            resp = await self._http().get(url, params=params)
        except Exception as exc:  # noqa: BLE001
            self._record(False)
            log.info("B站二进制接口异常（%s）", type(exc).__name__)
            return None
        if resp.status_code != 200:
            self._record(False)
            log.info("B站二进制接口 HTTP %s", resp.status_code)
            return None
        if not resp.content:
            self._record(False)
            return None
        self._record(True)
        return resp.content

    async def comments(self, aid: str, ps: int = 20) -> dict[str, Any] | None:
        """热门评论（mode=3 按点赞）。未登录/被风控返回 None。"""
        return await self.request_json(
            REPLY_API,
            {
                "oid": aid,
                "type": 1,
                "mode": 3,
                "pagination_str": '{"offset":""}',
                "plat": 1,
                "web_location": 1315875,
                "ps": max(1, min(50, int(ps))),
            },
        )

    @staticmethod
    def _cursor_fixed_mixin() -> str:
        """评论兜底签名用的 mixin；key 对失效时返回空串。"""
        return mixin_key(CURSOR_FIXED_IMG_KEY, CURSOR_FIXED_SUB_KEY)

    async def comments_page(
        self, aid: str, mode: int = 2, offset: str = "", ps: int = 20
    ) -> dict[str, Any] | None:
        """评论 cursor 分页（`reply/wbi/main`）。

        mode=2 按时间（翻全量用），mode=3 按热度（首页 20 条热评）。
        `offset` 用上一页 `cursor.pagination_reply.next_offset`；空串是第一页。
        动态 key 被 -352 拒时，用固定 key 对再签一次（BiliSpider 的稳定套路）。
        """
        params = {
            "type": 1,
            "oid": aid,
            "mode": int(mode),
            "plat": 1,
            "web_location": 1315875,
            "ps": max(1, min(50, int(ps))),
            "pagination_str": json.dumps(
                {"offset": offset or ""}, separators=(",", ":")
            ),
        }
        payload = await self.request_json(REPLY_API, params)
        if payload is None and not self._cooling():
            fixed = self._cursor_fixed_mixin()
            if fixed:
                payload = await self.request_json(REPLY_API, params, mixin=fixed)
        return payload

    async def sub_comments(
        self, aid: str, root: int, pn: int = 1, ps: int = 20
    ) -> dict[str, Any] | None:
        """楼中楼（`reply/reply`），按根评论 `root` + 页码翻。"""
        return await self.request_json(
            SUB_REPLY_API,
            {
                "type": 1,
                "oid": aid,
                "root": int(root),
                "pn": max(1, int(pn)),
                "ps": max(1, min(50, int(ps))),
            },
        )

    async def video_view(self, bvid: str = "", aid: int = 0) -> dict[str, Any] | None:
        """视频基础信息（aid/title/pubdate/owner）；不需要签名。bvid/aid 给一个即可。"""
        params: dict[str, Any] = {"bvid": bvid} if bvid else {"aid": int(aid)}
        return await self.request_json(VIEW_API, params, signed=False)

    async def space_videos(
        self, mid: str, pn: int = 1, ps: int = 30
    ) -> dict[str, Any] | None:
        """UP 主投稿列表（按发布时间倒序）。`ps` 实测 30 可用，50 返回空。"""
        return await self.request_json(
            SPACE_ARC_API,
            {
                "mid": str(mid),
                "ps": max(1, min(30, int(ps))),
                "pn": max(1, int(pn)),
                "order": "pubdate",
                "platform": "web",
                "web_location": 1550101,
            },
        )

    async def uploaded_videos(self, mid: str, limit: int = 12) -> list[dict[str, Any]]:
        """UP 最近投稿（动态页，一页 12 条；含直播回放）。

        注意：`polymer/web-dynamic/v1/feed/space` 对数据中心 IP 是封的
        （2026-11-04 对照实验：家庭 IP 200、阿里云 412 `request was banned`），
        所以云上跑的时候这里基本是空 —— 上层 `bili._uploads` 会退回站内搜索。
        """
        middle = str(mid or "").strip()
        if not middle:
            return []
        payload = await self.request_json(
            DYNAMIC_SPACE_API,
            {"host_mid": middle, "offset": ""},
            signed=False,
        )
        return normalize_space_feed(payload, limit)

    async def danmaku_segment(self, cid: str, aid: str, index: int) -> bytes | None:
        """一段弹幕（每段 6 分钟，protobuf）。"""
        return await self.request_bytes(
            DANMAKU_API,
            {"type": 1, "oid": cid, "pid": aid, "segment_index": max(1, int(index))},
        )

    async def search_video(
        self, keyword: str, limit: int = 5, order: str = ""
    ) -> list[dict[str, Any]]:
        """按标题搜视频。order='pubdate' 时按发布时间从新到旧（要 SESSDATA）。

        没登录/被风控时，带 order 的请求会回一个只有 `v_voucher` 的空壳；
        这时退回普通搜索，别让「看最新视频」整条挂掉。
        """
        params: dict[str, Any] = {
            "search_type": "video",
            "keyword": keyword[:80],
            "page": 1,
        }
        if order:
            params["order"] = order
        payload = await self.request_json(SEARCH_API, params)
        items = normalize_search(payload, limit, "video")
        if not items and order:
            log.info("B站按时间排序没结果（可能没登录），退回普通搜索：%s", keyword[:40])
            payload = await self.request_json(
                SEARCH_API,
                {"search_type": "video", "keyword": keyword[:80], "page": 1},
            )
            items = normalize_search(payload, limit, "video")
        return items

    async def search_user(self, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
        """搜 UP 主。"""
        payload = await self.request_json(
            SEARCH_API,
            {"search_type": "bili_user", "keyword": keyword[:80], "page": 1},
        )
        return normalize_search(payload, limit, "user")

    async def home_feed(self, limit: int = 12) -> list[dict[str, Any]]:
        """她小号的首页推荐（一页，最多 12 条）；失败回空，不重试。

        这是“像人一样刷”和“按关键词搜”的区别：首页带登录态后的个性化，
        也是唯一能自然拿到“推荐理由”的地方。没有 SESSDATA 时通常回空壳，
        调用方应退回兴趣搜索。
        """
        params = {
            "fresh_type": 4,
            "ps": min(20, max(1, int(limit))),
            "fresh_idx": 1,
            "fresh_idx_1h": 1,
            "brush": 1,
            "feed_version": "V8",
            "homepage_ver": 1,
            "web_location": 1430650,
        }
        payload = await self.request_json(HOME_FEED_API, params, signed=True)
        items = normalize_home_feed(payload, limit)
        if items:
            return items
        # 新版偶发空壳时，老接口只试一次；不循环、不换号
        payload = await self.request_json(
            HOME_FEED_LEGACY_API,
            {"version": 1, "ps": min(20, max(1, int(limit))), "fresh_idx": 1},
            signed=False,
        )
        return normalize_home_feed(payload, limit)

    async def live_status(self, mid: str) -> dict[str, Any] | None:
        """这个 UP 的直播间状态；没直播间/接口失败都返回 None，不影响搜索。"""
        if not mid or self._cooling():
            return None
        await self._throttle()
        try:
            resp = await self._http().get(LIVE_API, params={"mid": str(mid)})
            if resp.status_code != 200:
                log.debug("B站直播接口 HTTP %s：%s", resp.status_code, mid)
                return None
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 直播查不到不能影响聊天
            log.debug("B站直播接口异常（%s）：%s", type(exc).__name__, mid)
            return None
        return normalize_live(payload, mid)

    async def live_room(self, room_id: str) -> dict[str, Any] | None:
        """按房间号查直播状态；房间不存在/接口失败返回 None。"""
        room = str(room_id or "").strip()
        if not room or self._cooling():
            return None
        await self._throttle()
        try:
            resp = await self._http().get(LIVE_ROOM_API, params={"room_id": room})
            if resp.status_code != 200:
                log.debug("B站直播间接口 HTTP %s：%s", resp.status_code, room)
                return None
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 直播查不到不能影响聊天
            log.debug("B站直播间接口异常（%s）：%s", type(exc).__name__, room)
            return None
        return normalize_live_room(payload, room)

    async def playurl(self, bvid: str, cid: str, qn: int = 16) -> str:
        """取一条视频流地址（默认 360p；抽帧只要 400px 一格，够用且省流量）。"""
        payload = await self.request_json(
            PLAYURL_API,
            {
                "bvid": bvid,
                "cid": cid,
                "fnval": 16,
                "fnver": 0,
                "fourk": 0,
                "qn": max(16, min(80, int(qn))),
            },
        )
        return normalize_playurl(payload, qn=qn)

    # ---------- 写操作（只有她自己的小号、且开了互动才走） ----------

    async def like_video(
        self, aid: str = "", bvid: str = "", like: bool = True
    ) -> dict[str, Any]:
        """点赞/取消点赞。返回统一回执，不抛。"""
        payload = await self.post_json(
            LIKE_API,
            {
                "aid": str(aid or "").strip() or None,
                "bvid": str(bvid or "").strip() or None,
                "like": 1 if like else 0,
            },
        )
        return _write_result(payload)

    async def follow_user(self, mid: str, act: int = 1) -> dict[str, Any]:
        """关注（act=1）/取关（act=2）。"""
        payload = await self.post_json(
            RELATION_API,
            {
                "fid": str(mid or "").strip() or None,
                "act": 1 if int(act) == 1 else 2,
                "re_src": 11,
            },
        )
        return _write_result(payload)

    async def comment_video(self, aid: str, message: str) -> dict[str, Any]:
        """发一条顶层原创评论；不回复别人、不 @ 人由调用方保证。"""
        payload = await self.post_json(
            REPLY_ADD_API,
            {
                "oid": str(aid or "").strip() or None,
                "type": 1,
                "message": str(message or "").strip()[:150],
                "plat": 1,
            },
        )
        return _write_result(payload)

    async def nav_mid(self) -> str:
        """当前登录账号的 UID；未登录/失败返回空串。"""
        payload = await self.request_json(NAV_API, signed=False)
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return str(data.get("mid") or "").strip()

    async def favorite_folders(self, mid: str) -> list[dict[str, Any]]:
        """账号自己创建的收藏夹；失败返回空，不猜。"""
        if not str(mid or "").strip():
            return []
        payload = await self.request_json(
            FAV_FOLDER_LIST_API, {"up_mid": str(mid).strip()}, signed=False
        )
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("list") if isinstance(data.get("list"), list) else []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                folder_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "id": folder_id,
                    "title": str(row.get("title") or "").strip(),
                    "privacy": _num(row.get("privacy")),
                    "count": _num(row.get("media_count")),
                }
            )
        return out

    async def create_favorite_folder(
        self, title: str, *, privacy: int = 1
    ) -> dict[str, Any]:
        """新建收藏夹；默认私密。返回 ok/id。"""
        payload = await self.post_json(
            FAV_FOLDER_ADD_API,
            {"title": str(title or "").strip()[:20], "privacy": 1 if int(privacy) else 0},
        )
        result = _write_result(payload)
        if result.get("ok"):
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            try:
                result["id"] = int(data.get("id") or 0)
            except (TypeError, ValueError):
                result["id"] = 0
        return result

    async def collect_video(self, aid: str, folder_id: int) -> dict[str, Any]:
        """收藏到指定收藏夹；folder_id=0 时直接拒绝，不猜。"""
        if not int(folder_id or 0):
            return {"ok": False, "code": -1, "error": "没配收藏夹 ID，先不收藏"}
        payload = await self.post_json(
            FAV_DEAL_API,
            {
                "rid": str(aid or "").strip() or None,
                "type": 2,
                "add_media_ids": int(folder_id),
            },
        )
        return _write_result(payload)

    def status(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "errors": self.errors,
            "login": bool(self.sessdata),
            "cooling": self._cooling(),        }
