"""B 站直播陪看：房间标题、弹幕，以及（可选）每十几秒抽几帧画面。

为什么以前只做标题和弹幕
------------------------
房间标题 + 开播状态：一个公开 HTTP 接口，0 token；弹幕：公开 WebSocket，匿名就能收。
画面要拉流、抽帧，一次 1~2MB，所以默认关（`ENABLE_LIVE_FRAMES`）：
开了之后只在**群友/主人问画面**、或**弹幕突然爆点**时抓 12 秒拼一张，
不是常驻录屏；没有声音、不做 ASR。

她拿到的东西很窄，但足够参与：知道在播什么、大家在刷什么、画面里在干嘛。
网络失败只记日志、下一轮重试，绝不影响正常聊天。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from . import live_frames
from .bili_api import BiliApi
from .bili_frames import Shot, compose
from .config import DATA_DIR
from .live_frames import LiveShot
from .live_history import LiveHistory

if TYPE_CHECKING:
    from .live_memes import LiveMemeBook

log = logging.getLogger(__name__)

# 房间信息：优先用信息更全的 getInfoByRoom（带主播名），失败再退老接口。
ROOM_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
ROOM_INFO_FALLBACK = "https://api.live.bilibili.com/room/v1/Room/get_info"
DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
DANMU_CONF_URL = "https://api.live.bilibili.com/room/v1/Danmu/getConf"
DANMAKU_URL = "wss://broadcastlv.chat.bilibili.com/sub"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://live.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 一句弹幕最多留多少字：再长就不是弹幕了，多半是刷屏/广告
_MAX_DANMAKU_CHARS = 80
# 弹幕只在最近这段时间内算「现在大家在说」
_DANMAKU_TTL = 600

# 聊天里出现这些词，才把最近弹幕一起递给她；否则只给一行「你在看什么」。
# 别放太短的词（「播」会命中「播放」「传播」）。
_LIVE_WORDS = (
    "直播",
    "主播",
    "弹幕",
    "开播",
    "下播",
    "在看",
    "人气",
    "B站",
    "b站",
    "bilibili",
    "哔哩哔哩",
)

# 群友也能问：提到直播/主播，又带这些词，才抓画面（不要求是主人）。
# 只有「画面/在干嘛」、没有直播主语时不抓：群里那句话很可能在说视频或表情包。
_FRAME_WORDS = (
    "画面",
    "在干嘛",
    "在干什么",
    "长什么样",
    "什么情况",
    "看一眼",
    "看看",
    "镜头",
    "打到哪",
    "在玩什么",
)


def asks_about_live_frames(text: str) -> bool:
    """这句是不是在问直播画面（本地关键词，0 token，谁问都算）。"""
    blob = str(text or "")
    if not any(word in blob for word in ("直播", "主播", "直播间")):
        return False
    return any(word in blob for word in _FRAME_WORDS)


# 盯着直播时，主人/同群的人会直接说「看看画面」「在干嘛」——不带「直播」两个字。
# 这种只说给**正在陪看的那个会话**听（app 用 notify_session 判），别的话痨群不接。
_LOOSE_FRAME_WORDS = (
    "画面",
    "在干嘛",
    "在干什么",
    "长什么样",
    "什么情况",
    "看一眼",
    "再看看",
    "再看",
    "重看",
    "看错",
    "看岔",
    "镜头",
    "打到哪",
    "在玩什么",
)


def wants_live_frame(text: str) -> bool:
    """没有「直播/主播」主语，但明显是在要画面（给同会话的追备用）。"""
    blob = str(text or "")
    return any(word in blob for word in _LOOSE_FRAME_WORDS)


# 问屏幕上的数字/文字：3×3 分镜每格只有 400px，长数字会糊成一片；这种问题
# 要送全尺寸原帧（960 宽），不送拼图（2026-11-06 实锤：-4854000000 被读成 -485 万）。
_READ_SCREEN_WORDS = (
    "数字",
    "多少",
    "亏",
    "赚",
    "字",
    "屏幕",
    "显示",
    "余额",
    "打赏",
    "看错",
)


def asks_about_screen_text(text: str) -> bool:
    """这句是不是在看屏幕上的字/数字（决定送原帧还是分镜拼图）。"""
    blob = str(text or "")
    return any(word in blob for word in _READ_SCREEN_WORDS)


def peak_due(
    times: list[float],
    now: float,
    *,
    minimum: int = 8,
    window: float = 10.0,
    cooldown: float = 120.0,
    last: float = 0.0,
) -> int:
    """弹幕爆点判定：最近 `window` 秒够 `minimum` 条、且离上次抓图超过 `cooldown`。

    返回这段时间的条数（够就 >0，不够就 0）；纯函数，方便测试。
    """
    if now - last < cooldown:
        return 0
    recent = sum(1 for stamp in times if now - stamp <= window)
    return recent if recent >= max(1, int(minimum)) else 0


class LiveError(RuntimeError):
    """直播间接口失败（接口拒绝、断流、返回结构不对）。"""


@dataclass
class LiveInfo:
    """一次房间信息快照。没有画面、没有音频，只有这些公开字段。"""

    room_id: str
    title: str = ""
    anchor: str = ""
    live: bool = False
    online: int = 0
    area: str = ""
    start_time: int = 0
    fetched_at: float = 0.0


def extract_room_id(value: str) -> str:
    """从「12345」/「直播间 12345」/「https://live.bilibili.com/12345」里挖房号。

    只认 B 站直播域；短链 b23.tv 不在这里展开（那要再发一次请求，而且可能
    指向视频而不是直播间）。认不出返回空串。
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = text.replace("／", "/")
    match = re.search(r"live\.bilibili\.com/(?:blanc/)?(\d{1,12})", text, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:room_id|roomid)=(\d{1,12})", text, re.I)
    if match:
        return match.group(1)
    # 只认「纯数字」或「房间号：12345」；别从 Chris1i 这种名字里抠出 1
    if re.fullmatch(r"\d{1,12}", text):
        return text
    match = re.fullmatch(
        r"(?:直播间|房间号|房间|room|id)\s*[:：]?\s*(\d{1,12})", text, re.I
    )
    if match:
        return match.group(1)
    return ""


def _clean(text: Any, limit: int = 120) -> str:
    """压掉换行/控制字符，避免昵称或标题把提示词撑成多行。"""
    value = str(text or "")
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


# ---------- 弹幕协议 ----------
#
# 16 字节包头：总长(4) / 头长(2) / 协议版本(2) / 操作码(4) / 序号(4)。
# 操作码 2=心跳 3=心跳回包 5=消息 7=认证 8=认证回包。
# 协议版本 2 的消息体是 zlib；我们认证时只要 protover=2，服务端就不会发 brotli。


def pack_packet(operation: int, body: bytes, protover: int = 1) -> bytes:
    return struct.pack(">IHHII", 16 + len(body), 16, protover, operation, 1) + body


def parse_packets(data: bytes, _depth: int = 0) -> list[tuple[int, int, bytes]]:
    """拆包 + zlib/brotli 解压，返回 [(操作码, 协议版本, 消息体)]。

    压缩包解压出来的内容**还可能带着 16 字节包头**（B 站会把同一批通知
    打包成一个压缩包，里面每条又是完整小包）。以前直接把解压结果当 JSON，
    于是只收到人气值/公告，真正的 DANMU_MSG 全被静默丢掉 —— 2026-10-27
    实机排查到的就是这个。这里递归拆到没有包头为止。

    坏包直接跳过，不能因为一条脏数据把整个监听循环打断。
    """
    if _depth > 3:  # 防止坏数据无限套娃
        return []
    out: list[tuple[int, int, bytes]] = []
    buf = data or b""
    while len(buf) >= 16:
        total, header_len, protover, operation, _ = struct.unpack(">IHHII", buf[:16])
        if total < 16 or header_len < 16 or header_len > total or total > len(buf):
            break
        body = buf[header_len:total]
        buf = buf[total:]
        if protover == 2:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                log.debug("直播弹幕包 zlib 解压失败，跳过")
                continue
        elif protover == 3:
            # 正常认证时请求 protover=2 拿 zlib；万一服务端回 brotli，装了就能解。
            try:
                import brotli  # type: ignore
            except ImportError:
                log.debug("收到 brotli 弹幕包，但没装 brotli，跳过")
                continue
            try:
                body = brotli.decompress(body)
            except Exception:  # noqa: BLE001
                log.debug("brotli 解压失败，跳过", exc_info=True)
                continue
        if protover in (2, 3):
            if body[:1] == b"{":
                out.append((operation, 0, body))
            else:
                out.extend(parse_packets(body, _depth + 1))
        else:
            out.append((operation, protover, body))
    return out


def parse_danmaku(payload: Any) -> tuple[str, str, float] | None:
    """从一条 JSON 消息里取 (昵称, 弹幕, 时间戳)。不是弹幕返回 None。"""
    if not isinstance(payload, dict):
        return None
    cmd = str(payload.get("cmd") or "")
    if not cmd.startswith("DANMU_MSG"):
        return None
    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None
    text = _clean(info[1], _MAX_DANMAKU_CHARS)
    if not text:
        return None
    user = ""
    user_info = info[2]
    if isinstance(user_info, list) and len(user_info) > 1:
        user = _clean(user_info[1], 40)
    user = user or "观众"
    ts = 0.0
    meta = info[0]
    if isinstance(meta, list) and len(meta) > 4:
        try:
            ts = float(meta[4])
        except (TypeError, ValueError):
            ts = 0.0
    if ts > 1e12:  # 毫秒
        ts /= 1000.0
    return user, text, ts or time.time()


class BiliLiveWatcher:
    """盯一个 B 站直播间。只读公开接口，不登录、不发弹幕。

    ``notify`` 是「开播 / 下播」的固定话术回调，不走模型（0 token）。
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        room_id: str = "",
        notify_session: str = "",
        notify_sessions: "list[str] | None" = None,
        poll_seconds: int = 60,
        max_danmaku: int = 40,
        note_lines: int = 8,
        timeout: float = 6.0,
        notify: Callable[[str, str], Awaitable[None] | None] | None = None,
        api: BiliApi | None = None,
        store: "LiveMemeBook | None" = None,
        history: LiveHistory | None = None,
        frames_enabled: bool = False,
        frames_seconds: float = live_frames.DEFAULT_SECONDS,
        frames_interval: float = live_frames.DEFAULT_INTERVAL,
        buffer_enabled: bool = True,
        buffer_seconds: float = 120.0,
        buffer_interval: float = 15.0,
        peak_enabled: bool = True,
        peak_min: int = 8,
        peak_cooldown: float = 120.0,
        frames_dir: "Path | None" = None,
    ) -> None:
        self.enabled = bool(enabled and room_id)
        self.room_id = str(room_id or "").strip()
        # 通知名单：一个房间可以被多个群同时陪看（2026-11-04 加）
        self.sessions: set[str] = {
            str(item).strip()
            for item in (notify_sessions or [notify_session])
            if str(item or "").strip()
        }
        self.poll_seconds = max(15, min(300, int(poll_seconds or 60)))
        self.note_lines = max(1, min(20, int(note_lines or 8)))
        self.timeout = max(2.0, float(timeout or 6.0))
        self.notify = notify
        self.store = store
        # 开播/下播记录：只写本地 SQLite，不额外请求；没有就什么也不记。
        self.history = history
        self.info = LiveInfo("")
        self._danmaku: deque[tuple[float, str, str]] = deque(
            maxlen=max(1, min(200, int(max_danmaku or 40)))
        )
        self._session: aiohttp.ClientSession | None = None
        self._api = api
        self._conf: tuple[str, str, str, float] = ("", "", "", 0.0)
        self._generation = 0
        self._last_status: bool | None = None
        self._last_error = ""
        self._last_fetch = 0.0
        # 画面：默认关；开了之后按需/爆点抓，最近一次快照留 45 秒复用
        self.frames_enabled = bool(frames_enabled)
        self.frames_seconds = max(4.0, min(30.0, float(frames_seconds or 12.0)))
        self.frames_interval = max(0.5, min(5.0, float(frames_interval or 1.33)))
        self.peak_enabled = bool(peak_enabled)
        self.peak_min = max(2, int(peak_min or 8))
        self.peak_cooldown = max(30.0, float(peak_cooldown or 120.0))
        self.frames_dir = Path(frames_dir) if frames_dir else DATA_DIR / "bili" / "live"
        self._latest: LiveShot | None = None
        self._peak: tuple[LiveShot, int] | None = None
        self._peak_at = 0.0
        self._peak_task: asyncio.Task[Any] | None = None
        self._snap_lock = asyncio.Lock()
        # 滚动缓冲：看直播时每 15 秒拉 2 秒，最近 2 分钟的帧只留内存，不落盘。
        # 需要画面时直接拼，省掉 7~12 秒的拉流等待。
        self.buffer_enabled = bool(buffer_enabled) and self.frames_enabled
        self.buffer_seconds = max(30.0, min(600.0, float(buffer_seconds or 120.0)))
        self.buffer_interval = max(5.0, min(120.0, float(buffer_interval or 15.0)))
        self._buffer: deque[tuple[float, bytes, float]] = deque(maxlen=48)
        self._buffer_at = 0.0
        self._buffer_urls: tuple[float, list[str]] = (0.0, [])
        self._buffer_task: asyncio.Task[Any] | None = None
        self._buffer_bytes = 0
        self._buffer_max_bytes = 64 * 1024 * 1024

    # ---------- 开关 ----------

    def start(self, room_id: str, notify_session: str = "") -> str:
        """开始盯某个房间；返回规范化的房号（认不出返回空串）。

        同一个房间可以被多个会话同时陪看：房号没变就只是把新会话加进名单，
        弹幕缓冲、画面留底都保留；换房间才清空重来。
        """
        room = extract_room_id(room_id)
        if not room:
            return ""
        session = str(notify_session or "").strip()
        if room != self.room_id:
            # 换台：旧房间如果还开着，记成「跟踪结束」而不是「下播」。
            # 只有同一进程里真的换了房间才关；重启恢复同一个房间不能关。
            if self.room_id and self.history is not None:
                self.history.close_session(self.room_id, time.time(), "switched")
            self._danmaku.clear()
            self.info = LiveInfo(room)
            self._last_status = None
            self._latest = None
            self._peak = None
            self._buffer.clear()
            self._buffer_bytes = 0
            self._buffer_at = 0.0
            self._buffer_urls = (0.0, [])
            self.sessions = set()
        if session:
            self.sessions.add(session)
        self.room_id = room
        self.enabled = True
        self._last_error = ""
        self._generation += 1
        return room

    def restore(self, room_id: str, sessions: "list[str] | None" = None) -> str:
        """启动时恢复上次的陪看：房间 + 通知名单。房号认不出就返回空串。

        2026-11-04 改回自动续盯：重启/部署一次就得让主人重报房号太烦。
        prefs 里存着房间和名单，「不看直播了」会把房间清掉，所以下次启动
        自然就不恢复了 —— 不想恢复的停一次就行。
        """
        room = extract_room_id(room_id)
        if not room:
            return ""
        self.start(room, "")
        for item in sessions or []:
            text = str(item or "").strip()
            if text:
                self.sessions.add(text)
        self.enabled = True
        return room

    def leave(self, notify_session: str) -> bool:
        """某个会话不看了；返回是否已经没人看（True 时调用方该走 stop）。"""
        text = str(notify_session or "").strip()
        if text:
            self.sessions.discard(text)
        if not self.sessions:
            self.stop()
            return True
        return False

    @property
    def notify_session(self) -> str:
        """兼容旧字段：返回名单里的第一个会话（多群时不一定唯一）。"""
        return next(iter(sorted(self.sessions)), "")

    def sessions_list(self) -> list[str]:
        """通知名单（排序后的副本），给日志和回执用。"""
        return sorted(self.sessions)

    def stop(self) -> None:
        # 主人说「不看了」：如果还在播，这条记录只能记到「停止陪看」，
        # 不能冒充主播下播；查询时会把 stopped/switched 标出来。
        if self.room_id and self.history is not None:
            self.history.close_session(self.room_id, time.time(), "stopped")
        self.enabled = False
        self.room_id = ""
        self.sessions.clear()
        self.info = LiveInfo("")
        self._danmaku.clear()
        self._last_status = None
        self._last_error = ""
        self._latest = None
        self._peak = None
        self._buffer.clear()
        self._buffer_bytes = 0
        self._buffer_at = 0.0
        self._generation += 1

    # ---------- 给提示词 / 回执 ----------

    def prompt_note(self, text: str = "") -> str:
        """动态上下文里的直播小抄；没在盯、或还没拿到房间信息时返回空串。"""
        if not self.enabled or not self.room_id or not self.info.fetched_at:
            return ""
        title = self.info.title or f"直播间 {self.room_id}"
        anchor = f"，主播 {self.info.anchor}" if self.info.anchor else ""
        mentioned = self._mentions_live(text)
        if self.info.live:
            if self.frames_enabled:
                if self.buffer_enabled:
                    view = (
                        f"你后台一直在抓直播画面（每 {int(self.buffer_interval)} 秒抓一次，"
                        f"留最近 {max(1, int(round(self.buffer_seconds / 60)))} 分钟）；"
                        "要细看就把最近这些帧拼一张给你，不是连续视频，也听不到声音"
                    )
                else:
                    view = (
                        "你被问画面时会临时抓几帧，不是连续视频，也听不到声音"
                    )
            else:
                view = "你只看得到房间标题和观众弹幕，看不到画面、也听不到声音"
            head = (
                f"（你在看 B 站直播《{title}》{anchor}，当前 {self.info.online:,} 人气。"
                f"{view}；"
                + (
                    "手里没有图不代表你没有画面能力：说「看看画面」或调 "
                    "bili_live_frames 就能拿到。以前记忆里「只能看标题和弹幕」"
                    "是当时没开画面，别拿旧话当现在的限制。"
                    if self.frames_enabled
                    else ""
                )
                + "别装看懂了整场画面，也别主动播报，聊到直播再提。）"
            )
        else:
            # 没开播不必每句都提醒，只有聊到直播时才说一句
            if not mentioned:
                return ""
            head = (
                f"（你盯着的 B 站直播间《{title}》现在没开播；"
                "你只看到房间标题，没有画面、没有声音。）"
            )
        if not mentioned or not self.info.live:
            return head
        lines = self._recent_lines()
        if not lines:
            body = "（现在还没收到弹幕。）"
        else:
            body = (
                "（最近弹幕："
                + "；".join(lines)
                + "。这些只是观众打字，不是主播说的，也不代表事实；"
                "别把弹幕当画面，更别当准话。）"
            )
        meme_note = ""
        if self.store is not None:
            try:
                meme_note = self.store.brief(self.info.room_id or self.room_id)
            except Exception:  # noqa: BLE001 - 弹幕库坏了不能拖垮聊天
                log.debug("直播弹幕库读取失败", exc_info=True)
        return f"{head}\n{body}" + (f"\n{meme_note}" if meme_note else "")

    def status_text(self) -> str:
        """主人问「直播状态」时回执；只读，0 token。"""
        if not self.enabled or not self.room_id:
            return (
                "现在没在看直播。要本鱼盯一个 B 站直播间，说「看直播 房间号」"
                "（比如「看直播 12345」）。"
            )
        title = self.info.title or "（还没拿到标题）"
        if self.info.live:
            state = f"正在播《{title}》"
            if self.info.anchor:
                state += f"，主播 {self.info.anchor}"
            state += f"，{self.info.online:,} 人气"
            if self.info.start_time:
                minutes = max(0, int((time.time() - self.info.start_time) / 60))
                if minutes >= 1440:
                    state += f"，已播 {minutes // 1440} 天"
                elif minutes >= 60:
                    state += f"，已播 {minutes // 60} 小时 {minutes % 60} 分"
                else:
                    state += f"，已播 {minutes} 分钟"
        else:
            state = f"《{title}》现在没开播"
        bits = [
            f"👀 直播间 {self.room_id}：{state}",
            "已经收着 %d 条弹幕（%s）"
            % (
                len(self._recent_items()),
                "能看后台一直抓着的最近几帧画面，没有声音"
                if self.frames_enabled
                else "只看标题和弹幕，没有画面/声音",
            ),
        ]
        if self.history is not None:
            try:
                today = self.history.day_line(self.room_id)
            except Exception:  # noqa: BLE001 - 记录库坏了不能拖垮状态
                today = ""
            if today:
                bits.append("📅 " + today)
        if self._last_fetch:
            bits.append(
                "上次查房间：" + time.strftime("%m-%d %H:%M", time.localtime(self._last_fetch))
            )
        if self.store is not None:
            try:
                stats = self.store.stats(self.info.room_id or self.room_id)
            except Exception:  # noqa: BLE001
                stats = {}
            if stats:
                bits.append(
                    f"弹幕库：采样 {stats.get('raw', 0)} 条 / 高频词 "
                    f"{stats.get('terms', 0)} 个 / 已入库 {stats.get('approved', 0)} 个"
                )
        if self._last_error:
            bits.append(f"（上次接口出错：{self._last_error[:120]}）")
        return "\n".join(bits)

    def _mentions_live(self, text: str) -> bool:
        body = str(text or "")
        if not body:
            return False
        if any(word in body for word in _LIVE_WORDS):
            return True
        if self.info.anchor and self.info.anchor in body:
            return True
        if self.room_id and self.room_id in body:
            return True
        return False

    def _recent_items(self) -> list[tuple[float, str, str]]:
        now = time.time()
        return [
            (ts, user, text)
            for ts, user, text in self._danmaku
            if now - ts <= _DANMAKU_TTL
        ]

    def _recent_lines(self) -> list[str]:
        items = self._recent_items()[-self.note_lines :]
        return [f"{user}：{text}" for _, user, text in items]

    def mentions_live(self, text: str) -> bool:
        """这句话是不是在聊这场直播（给 app 决定要不要附画面用）。"""
        return self._mentions_live(text)

    # ---------- 画面 ----------

    async def snapshot(self, max_age: float = 45.0) -> LiveShot | None:
        """抓一张直播画面；`max_age` 秒内抓到过就直接复用，不重复拉流。

        只在「有人在看」且「正在播」时抓；失败返回 None，调用方当这次没画面。
        """
        if (
            not self.frames_enabled
            or not self.enabled
            or not self.room_id
            or not self.info.live
        ):
            return None
        fresh = self._latest
        if fresh is not None and time.time() - fresh.at <= max_age:
            return fresh
        # 滚动缓冲里有就用缓冲：不用再等 7~12 秒拉流
        buffered = await self._buffer_shot(max_age)
        if buffered is not None:
            self._latest = buffered
            return buffered
        async with self._snap_lock:
            fresh = self._latest
            if fresh is not None and time.time() - fresh.at <= max_age:
                return fresh
            shot = await live_frames.snapshot(
                self._get_api(),
                self.info.room_id or self.room_id,
                seconds=self.frames_seconds,
                interval=self.frames_interval,
                work_dir=self.frames_dir,
            )
            if shot is not None:
                self._latest = shot
            return shot

    # ---------- 滚动缓冲 ----------

    def _trim_buffer(self, now: float) -> None:
        """按时间 / 内存上限丢旧帧：只留最近 `buffer_seconds` 秒。"""
        cutoff = now - self.buffer_seconds
        while self._buffer and (
            self._buffer[0][2] < cutoff or self._buffer_bytes > self._buffer_max_bytes
        ):
            _, data, _ = self._buffer.popleft()
            self._buffer_bytes -= len(data)

    async def _buffer_shot(self, max_age: float) -> LiveShot | None:
        """用内存里的缓冲拼一张；不够新/不够多就 None（退回现拉）。"""
        now = time.time()
        self._trim_buffer(now)
        if len(self._buffer) < 3:
            return None
        if now - self._buffer[-1][2] > max(10.0, float(max_age)):
            return None
        frames = list(self._buffer)
        if len(frames) > 12:
            # 按整段均匀取，别只拿最后一口气的多帧（那样 2 分钟的缓冲只用了 10 秒）
            index = [
                round(i * (len(frames) - 1) / 11) for i in range(12)
            ]
            picks = [frames[i] for i in index]
        else:
            picks = frames
        shots = [Shot(None, clock, data) for clock, data, _ts in picks]
        image = await asyncio.to_thread(
            compose, shots, cols=3, labeler=live_frames.clock_label
        )
        if not image:
            return None
        return LiveShot(
            image=image,
            start=shots[0].at,
            end=shots[-1].at,
            frames=len(shots),
            at=now,
            frame=picks[-1][1],
        )

    async def _buffer_urls_for(self, now: float) -> list[str]:
        cached_at, cached = self._buffer_urls
        ttl = 600.0 if cached else 30.0
        if cached_at and now - cached_at < ttl:
            return cached
        urls = await live_frames.resolve_urls(
            self._get_api(), self.info.room_id or self.room_id
        )
        self._buffer_urls = (now, urls)
        return urls

    async def _buffer_step(self) -> None:
        if not (
            self.enabled
            and self.frames_enabled
            and self.buffer_enabled
            and self.info.live
        ):
            self._buffer.clear()
            self._buffer_bytes = 0
            return
        now = time.time()
        self._trim_buffer(now)
        if now - self._buffer_at < self.buffer_interval:
            return
        urls = await self._buffer_urls_for(now)
        if not urls:
            self._buffer_at = now  # 拿不到地址就等下一轮，别 2 秒敲一次接口
            return
        work = self.frames_dir / "_buffer"
        # 第一次先把缓冲垫满（抓满 12 秒），之后每轮只补 2 秒；两次都在后台跑
        seconds = max(2.0, self.frames_seconds) if len(self._buffer) < 6 else 2.0
        for url in urls[:2]:
            shots = await asyncio.to_thread(
                live_frames.grab_frames, url, work, seconds, 1.0
            )
            if not shots:
                continue
            for shot in shots:
                data = b""
                if shot.path is not None:
                    try:
                        data = shot.path.read_bytes()
                    except OSError:
                        data = b""
                if data:
                    self._buffer.append((shot.at, data, time.time()))
                    self._buffer_bytes += len(data)
                if shot.path is not None:
                    try:
                        shot.path.unlink()
                    except OSError:
                        pass
            self._trim_buffer(time.time())
            self._buffer_at = now
            log.debug("直播缓冲：+%d 帧，共 %d 帧", len(shots), len(self._buffer))
            return

    async def _buffer_loop(self) -> None:
        if not self.buffer_enabled:
            return
        while True:
            try:
                await self._buffer_step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 缓冲失败不能断弹幕
                log.debug("直播缓冲抓帧失败", exc_info=True)
            await asyncio.sleep(2.0)

    def recent_peak(self, max_age: float = 150.0) -> tuple[LiveShot, int] | None:
        """最近一次「弹幕爆点」时抓的画面；太旧（或没有）返回 None。"""
        if not self._peak:
            return None
        shot, count = self._peak
        if time.time() - shot.at > max_age:
            return None
        return shot, count

    def _check_peak(self, now: float) -> None:
        """弹幕突然密起来时，排一个后台抓图任务（不挡收弹幕）。"""
        if not (self.frames_enabled and self.peak_enabled and self.info.live):
            return
        count = peak_due(
            [stamp for stamp, _, _ in self._danmaku],
            now,
            minimum=self.peak_min,
            cooldown=self.peak_cooldown,
            last=self._peak_at,
        )
        if count <= 0:
            return
        self._peak_at = now
        if self._peak_task is not None and not self._peak_task.done():
            return
        try:
            self._peak_task = asyncio.create_task(self._peak_snapshot(count))
        except RuntimeError:  # 没有事件循环（测试里直接调）
            self._peak_task = None

    async def _peak_snapshot(self, count: int) -> None:
        try:
            shot = await self.snapshot(max_age=0.0)
        except Exception:  # noqa: BLE001 - 抓图失败不能断弹幕
            log.info("弹幕爆点抓画面失败", exc_info=True)
            return
        if shot is None:
            log.info("弹幕爆点，但直播画面没抓到（房间 %s）", self.room_id)
            return
        self._peak = (shot, count)
        log.info(
            "弹幕爆点已抓画面：%s–%s（10 秒 %d 条）",
            live_frames.clock_label(shot.start),
            live_frames.clock_label(shot.end),
            count,
        )

    def recent_text(self, limit: int = 10, *, safe_only: bool = True) -> str:
        """最近收到的弹幕，给「看看弹幕」这类只读命令用。

        只看内存缓冲，不重新拉接口；没有就返回空串，让调用方说清楚
        「还没收到」，而不是编几条。默认滤掉广告/骂人/黄腔/刷屏，
        免得把直播间的脏话原样转到 QQ 群。
        """
        items = self._recent_items()[-max(1, min(30, int(limit))) :]
        if not items:
            return ""
        classify = None
        if safe_only:
            from .live_memes import classify_risk

            classify = classify_risk
        lines = []
        for ts, user, text in items:
            if classify is not None and classify(text):
                continue
            stamp = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
            lines.append(f"{stamp} {user}：{text}" if stamp else f"{user}：{text}")
        return "\n".join(lines)

    # ---------- 后台循环 ----------

    async def run(self) -> None:
        """永久循环：房间没开播就轮询，开播就接弹幕；断了退避重连。"""
        log.info("直播陪看已启动：房间 %s", self.room_id or "（未设置）")
        self._buffer_task = asyncio.create_task(self._buffer_loop())
        try:
            while True:
                if not self.enabled or not self.room_id:
                    await asyncio.sleep(5)
                    continue
                generation = self._generation
                try:
                    await self._watch_once(generation)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 后台任务不能死
                    self._last_error = str(exc)
                    log.warning("直播监听出错（稍后重试）：%s", exc)
                if generation == self._generation:
                    await asyncio.sleep(5)
        finally:
            task = self._buffer_task
            self._buffer_task = None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    log.debug("收尾直播缓冲任务失败", exc_info=True)

    def _current(self, generation: int, room: str) -> bool:
        return (
            self.enabled
            and self.room_id == room
            and generation == self._generation
        )

    async def _watch_once(self, generation: int) -> None:
        room = self.room_id
        delay = 10
        while self._current(generation, room):
            if self.info.live:
                try:
                    await self._read_danmaku(generation, room)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 断流/接口失败都重连
                    self._last_error = str(exc)
                    log.warning("直播弹幕连接中断（%ss 后重连）：%s", delay, exc)
                if not self._current(generation, room):
                    return
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
                continue
            try:
                info = await self.fetch_info(room)
                self._apply_info(info)
                self._last_error = ""
                delay = 10
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 接口抖动不能停
                self._last_error = str(exc)
                log.warning("查直播间信息失败（稍后重试）：%s", exc)
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
                continue
            if not self.info.live:
                # 没开播时不必打太多请求：默认一分钟一次，够用了
                await asyncio.sleep(self.poll_seconds)

    async def _read_danmaku(self, generation: int, room: str) -> None:
        # 用户填的可能是短号（如 6），弹幕服务要的是真实房间号（如 7734200）
        real_room = self.info.room_id or room
        token, host = await self._danmu_conf(real_room)
        ws = await self._connect(real_room, token, host)
        last_ping = time.monotonic()
        last_info = time.monotonic()
        try:
            while self._current(generation, room) and self.info.live:
                try:
                    message = await ws.receive(timeout=1.0)
                except asyncio.TimeoutError:
                    message = None
                if message is not None:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        self._handle_packet(message.data)
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                now = time.monotonic()
                if now - last_ping >= 30:
                    await ws.send_bytes(
                        pack_packet(2, json.dumps({"roomid": int(real_room)}).encode())
                    )
                    last_ping = now
                if now - last_info >= self.poll_seconds:
                    try:
                        info = await self.fetch_info(room)
                        self._apply_info(info)
                    except Exception:  # noqa: BLE001 - 断流后下一轮会重连
                        log.debug("直播中查开播状态失败", exc_info=True)
                    last_info = now
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _danmu_conf(self, room: str) -> tuple[str, str]:
        """拿弹幕服务器的 token + 主机；5 分钟内复用。

        老接口 getConf 还能用，就不强依赖 WBI 的 getDanmuInfo：哪个先返回用哪个。
        """
        token, host, cached_room, at = self._conf
        if cached_room == room and time.monotonic() - at < 300:
            return token, host
        token, host = "", ""
        api = self._get_api()
        if api is not None:
            payload = await api.request_json(
                DANMU_INFO_URL, {"id": room, "type": 0}
            )
            data = (payload or {}).get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                token = str(data.get("token") or "")
                hosts = data.get("host_list")
                if isinstance(hosts, list) and hosts and isinstance(hosts[0], dict):
                    host = str(hosts[0].get("host") or "")
        if not token and api is not None:
            payload = await api.request_json(
                DANMU_CONF_URL,
                {"room_id": room, "platform": "pc", "player": "web"},
                signed=False,
            )
            data = (payload or {}).get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                token = str(data.get("token") or "")
                hosts = data.get("host_server_list")
                if isinstance(hosts, list) and hosts and isinstance(hosts[0], dict):
                    host = str(hosts[0].get("host") or "")
        host = host or "broadcastlv.chat.bilibili.com"
        self._conf = (token, host, room, time.monotonic())
        return token, host

    async def _connect(
        self, room: str, token: str = "", host: str = ""
    ) -> aiohttp.ClientWebSocketResponse:
        session = self._ensure_session()
        url = f"wss://{host or 'broadcastlv.chat.bilibili.com'}/sub"
        ws = await session.ws_connect(
            url,
            headers={"User-Agent": _UA, "Referer": f"https://live.bilibili.com/{room}"},
            origin="https://live.bilibili.com",
            receive_timeout=None,
            max_msg_size=4 * 1024 * 1024,
            heartbeat=None,
        )
        auth = {
            "uid": 0,
            "roomid": int(room),
            "protover": 2,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        await ws.send_bytes(pack_packet(7, json.dumps(auth).encode()))
        log.info("已连上直播间弹幕：%s（%s）", room, host or "broadcastlv")
        return ws

    def _handle_packet(self, data: bytes) -> None:
        for operation, _version, body in parse_packets(data):
            if operation != 5:
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                parsed = parse_danmaku(item)
                if parsed is not None:
                    self._add_danmaku(*parsed)

    def _add_danmaku(self, user: str, text: str, ts: float) -> None:
        # 同一句连着刷两遍不重复记；刷屏的第三遍也还会留在缓冲里
        if self._danmaku:
            last_ts, last_user, last_text = self._danmaku[-1]
            if last_text == text and last_user == user and ts - last_ts < 3:
                return
        self._danmaku.append((float(ts or time.time()), user, text))
        self._check_peak(time.time())
        if self.store is not None:
            try:
                self.store.add(
                    self.info.room_id or self.room_id,
                    user,
                    text,
                    float(ts or time.time()),
                )
            except Exception:  # noqa: BLE001 - 写库失败不能断弹幕
                log.debug("直播弹幕写库异常", exc_info=True)

    # ---------- 接口 ----------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
            )
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._api is not None:
            await self._api.aclose()
            self._api = None

    def _get_api(self) -> BiliApi:
        if self._api is None:
            self._api = BiliApi(
                timeout=self.timeout,
                rate=1.0,
                state_dir=DATA_DIR / "bili",
            )
        return self._api

    async def fetch_info(self, room: str | None = None) -> LiveInfo:
        """查一次房间信息。先走 WBI 签名的 getInfoByRoom，失败再退 Room/get_info。"""
        room_id = str(room or self.room_id)
        if not room_id:
            raise LiveError("没有房间号")
        first_error: Exception | None = None
        try:
            payload = await self._get_api().request_json(
                ROOM_INFO_URL, {"room_id": room_id}
            )
            info = self._normalize_primary(payload, room_id)
            if info is not None:
                return info
        except Exception as exc:  # noqa: BLE001 - 换备用接口
            first_error = exc
        session = self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with session.get(
                ROOM_INFO_FALLBACK,
                params={"room_id": room_id},
                headers=_HEADERS,
                timeout=timeout,
            ) as resp:
                payload = await resp.json(content_type=None)
            return self._normalize_fallback(payload, room_id)
        except Exception as exc:  # noqa: BLE001
            raise LiveError(str(exc or first_error or "直播间接口失败")) from exc

    @staticmethod
    def _normalize_primary(payload: Any, room: str) -> LiveInfo | None:
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        room_info = data.get("room_info")
        if not isinstance(room_info, dict):
            return None
        anchor_info = data.get("anchor_info")
        base = anchor_info.get("base_info") if isinstance(anchor_info, dict) else {}
        return LiveInfo(
            room_id=str(room_info.get("room_id") or room),
            title=_clean(room_info.get("title"), 80),
            anchor=_clean((base or {}).get("uname"), 40),
            live=int(room_info.get("live_status") or 0) == 1,
            online=_to_int(room_info.get("online")),
            area=_clean(room_info.get("area_name"), 30),
            start_time=_to_int(room_info.get("live_start_time")),
            fetched_at=time.time(),
        )

    @staticmethod
    def _normalize_fallback(payload: Any, room: str) -> LiveInfo:
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise LiveError("直播间接口返回异常")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LiveError("直播间接口没有数据")
        return LiveInfo(
            room_id=str(data.get("room_id") or room),
            title=_clean(data.get("title"), 80),
            anchor="",
            live=int(data.get("live_status") or 0) == 1,
            online=_to_int(data.get("online")),
            area=_clean(data.get("area_name"), 30),
            start_time=_live_time(data.get("live_time")),
            fetched_at=time.time(),
        )

    def _apply_info(self, info: LiveInfo) -> None:
        first = not self.info.fetched_at
        # 先把这一帧喂给开播记录：第一次看到在播也会开一条，不会因为
        # 「first 不播报」而漏记；下播/换场也在这里收尾。
        self._record_info(info)
        self.info = info
        self._last_fetch = time.time()
        if first:
            # 第一次拿到房间信息：只记状态，**不播报**。
            # 刚说「看直播」时已有回执；重启恢复时更不该刷一句「现在就在播」——
            # 每次上线都报一遍很蠢（2026-11-04 主人点名）。
            # 之后只在开播/下播这种**状态变化**时才发。
            self._last_status = info.live
            return
        if self._last_status is None or info.live == self._last_status:
            self._last_status = info.live
            return
        self._last_status = info.live
        title = info.title or info.room_id
        if info.live:
            anchor = f"（{info.anchor}）" if info.anchor else ""
            self._emit(f"【直播】开播了：《{title}》{anchor}")
        else:
            self._emit(f"【直播】下播了：《{title}》")

    def _record_info(self, info: LiveInfo) -> None:
        """把一次房间快照写成开播/下播记录；失败只记日志，不影响监听。"""
        history = self.history
        if history is None:
            return
        room = str(self.room_id or info.room_id or "").strip()
        if not room:
            return
        now = float(info.fetched_at or time.time())
        try:
            if info.live:
                history.observe_live(
                    room,
                    started_at=info.start_time,
                    title=info.title,
                    anchor=info.anchor,
                    online=info.online,
                    now=now,
                )
            else:
                history.observe_offline(room, now=now)
        except Exception:  # noqa: BLE001 - 记录失败不能断直播
            log.debug("直播开播记录写入失败（房间 %s）", room, exc_info=True)

    def _emit(self, text: str) -> None:
        if not self.notify or not self.sessions:
            return
        # 多群共看同一个房间：每个会话各发一份
        for session in sorted(self.sessions):
            try:
                result = self.notify(session, text)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # noqa: BLE001 - 通知失败不能影响监听
                log.warning("直播状态通知失败（%s）", session, exc_info=True)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _live_time(value: Any) -> int:
    """老接口的 live_time 是「2026-10-15 15:50:15」这种本地时间字符串。"""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError, OverflowError):
        return 0
