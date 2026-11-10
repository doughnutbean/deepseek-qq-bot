"""直播快照：拉十几秒直播流，按时间抽几帧，拼成一张带**真实时钟**的图。

和视频分镜（`bili_frames.py`）的区别
----------------------------------
- 视频有"全片"，可以按镜头切点铺满整段；直播只有"现在"，只能看接下来这十几秒；
- 取样按**时间均匀**（默认 1.33 秒一格），不找切点：电竞/聊天的画面一直在动，
  找切点只会抽到一堆相似帧；
- 时间戳是**真实时钟**（`22:23:31`），不是片内秒数 —— 她要说"几点那会儿"；
- 流量只 1~2MB（qn150 的 12 秒），不是整场录下来。

边界
----
拉流、ffmpeg 都有硬超时；失败返回 None，调用方当"这次没画面"，不影响回复。
只抓得到"现在/接下来"，听不到声音，也不做 ASR。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bili_api import BiliApi
from .bili_frames import Shot, compose, ffmpeg_path

log = logging.getLogger(__name__)

LIVE_PLAY_API = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
# qn150 = 高清：够看清画面和字，流量只有原画的零头
DEFAULT_QN = 150
DEFAULT_SECONDS = 12.0
DEFAULT_INTERVAL = 1.33
FRAME_WIDTH = 960
CELL_WIDTH = 400
# ffmpeg 拉流最多等"要抓的秒数 + 这么多"：连不上时别把整轮回复拖死
_EXTRA_TIMEOUT = 20.0
_HEADERS = (
    "Referer: https://live.bilibili.com/\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36\r\n"
)


@dataclass(frozen=True)
class LiveShot:
    """一次直播快照：分镜图 + 这组帧覆盖的时间段（当天第几秒）。

    `frame` 是其中一帧的原图（960 宽），发到聊天里当「截图」用 —— 分镜图是给
    模型看时间线的，聊天里发一张整图更像人截的。
    """

    image: bytes
    start: float
    end: float
    frames: int
    at: float  # 抓到的时间（time.time()），给缓存判断用
    frame: bytes | None = None


def clock_label(seconds_of_day: float) -> str:
    """当天第几秒 → `22:23:31`。直播必须带时:分:秒，早上的 5 点不能写成「05:00」。"""
    total = int(seconds_of_day) % 86400
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _urls_of(codec: dict) -> list[str]:
    base = str(codec.get("base_url") or "")
    if not base:
        return []
    out = []
    for info in codec.get("url_info") or []:
        host = str(info.get("host") or "")
        extra = str(info.get("extra") or "")
        if host:
            out.append(host + base + extra)
    return out


def parse_playurl(payload: object) -> list[str]:
    """`getRoomPlayInfo` → 可拉的流地址（flv 优先，HLS fmp4 兜底）。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    info = (data or {}).get("playurl_info") if isinstance(data, dict) else None
    playurl = (info or {}).get("playurl") if isinstance(info, dict) else None
    streams = (playurl or {}).get("stream") if isinstance(playurl, dict) else None
    flv: list[str] = []
    hls: list[str] = []
    for stream in streams or []:
        if not isinstance(stream, dict):
            continue
        protocol = str(stream.get("protocol_name") or "")
        for fmt in stream.get("format") or []:
            if not isinstance(fmt, dict):
                continue
            name = str(fmt.get("format_name") or "")
            for codec in fmt.get("codec") or []:
                if not isinstance(codec, dict) or codec.get("codec_name") != "avc":
                    continue
                urls = _urls_of(codec)
                if protocol == "http_stream" and name == "flv":
                    flv.extend(urls)
                elif protocol == "http_hls" and name == "fmp4":
                    hls.extend(urls)
    seen: set[str] = set()
    out = []
    for url in [*flv, *hls]:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


async def resolve_urls(api: BiliApi, room_id: str, qn: int = DEFAULT_QN) -> list[str]:
    """问一次播放地址；接口失败/没开播返回空列表。"""
    room = str(room_id or "").strip()
    if not room:
        return []
    payload = await api.request_json(
        LIVE_PLAY_API,
        {
            "room_id": room,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": int(qn),
            "platform": "web",
            "ptype": 8,
            "dolby": 5,
        },
        signed=False,
    )
    urls = parse_playurl(payload or {})
    if not urls:
        code = (payload or {}).get("code") if isinstance(payload, dict) else None
        log.info("直播播放地址没拿到（code=%s，房间 %s）", code, room)
    return urls


def grab_frames(
    source: str | Path,
    work_dir: Path,
    seconds: float = DEFAULT_SECONDS,
    interval: float = DEFAULT_INTERVAL,
) -> list[Shot]:
    """拉 `source` 的 `seconds` 秒，每 `interval` 秒抽一帧（480 宽），返回带时钟的 Shot。

    本地文件也能传（测试用）；网络流要带 Referer/UA，不然 B 站 CDN 直接 403。
    """
    exe = ffmpeg_path()
    if not exe:
        log.info("没装 ffmpeg，看不了直播画面")
        return []
    seconds = max(3.0, float(seconds))
    interval = max(0.5, float(interval))
    work = Path(work_dir)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        work = Path(work_dir)
    for old in work.glob("live_*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    started = time.time()
    clock = time.localtime(started)
    base = clock.tm_hour * 3600 + clock.tm_min * 60 + clock.tm_sec
    cmd = [exe, "-y", "-v", "error", "-nostdin"]
    if str(source).startswith(("http://", "https://")):
        # -headers 只对网络协议有效；本地文件带上它会直接报「Option headers not found」
        cmd += ["-rw_timeout", "15000000", "-headers", _HEADERS]
    cmd += [
        "-i",
        str(source),
        "-t",
        f"{seconds:.1f}",
        "-vf",
        f"fps=1/{interval:.3f},scale={FRAME_WIDTH}:-2",
        "-q:v",
        "3",
        str(work / "live_%02d.jpg"),
    ]
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=seconds + _EXTRA_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    files = sorted(work.glob("live_*.jpg"))
    return [
        Shot(path, base + index * interval)
        for index, path in enumerate(files)
        if path.is_file() and path.stat().st_size > 0
    ]


# 变化分：借 dsh-see-now 的思路，64×36 HSV 网格逐通道做差。
# 实操刻度：静止画面 ≈ 0、光标晃动 1~2、换画面 30+。阈值只用来"丢重复帧"，
# 不需要精确 —— 丢错了顶多少一帧，不会把有用的帧判死。
CHANGE_GRID = (64, 36)
CHANGE_THRESHOLD = 4.0
CHANGE_MIN_KEEP = 3


def _small_hsv(path: Path) -> Any:
    """把一帧缩成 64×36 的 HSV 小图（约 0.2ms），失败返回 None。"""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as img:
            small = img.convert("HSV").resize(CHANGE_GRID, Image.Resampling.BILINEAR)
            return np.asarray(small, dtype=np.int16)
    except Exception:  # noqa: BLE001 - 读不出来就当这帧没数据
        return None


def change_score(before: Any, after: Any) -> float:
    """两张小图"变了多少"（0~255）。超过 255/2 的色相差按环形算。"""
    try:
        import numpy as np

        if before is None or after is None or before.shape != after.shape:
            return 255.0
        diff = np.abs(before - after)
        hue = np.minimum(diff[..., 0], 255 - diff[..., 0])  # 色相是环形的
        return float(
            0.45 * hue.mean() + 0.35 * diff[..., 1].mean() + 0.20 * diff[..., 2].mean()
        )
    except Exception:  # noqa: BLE001
        return 255.0


def filter_repeats(
    shots: list[Shot],
    *,
    min_keep: int = CHANGE_MIN_KEEP,
    threshold: float = CHANGE_THRESHOLD,
) -> list[Shot]:
    """丢掉"几乎没动"的帧，至少留 `min_keep` 帧，顺序不变。

    12 帧里常常有 6~8 帧画面根本没变（主播站着不动、只是弹幕在滚），全拼进
    一张图只会把每格压小、信息更少。这里按"和上一张留下的帧差多少"筛选。
    """
    shots = list(shots)
    keep_n = max(1, int(min_keep))
    if len(shots) <= keep_n:
        return shots
    smalls = [_small_hsv(s.path) if getattr(s, "path", None) else None for s in shots]

    keep = [0]
    for index in range(1, len(shots)):
        if change_score(smalls[keep[-1]], smalls[index]) >= threshold:
            keep.append(index)

    if len(keep) < keep_n:
        # 画面一动不动：按"相邻帧差值"从大到小补，保证图里还有几帧可看
        rest = [
            (change_score(smalls[i - 1], smalls[i]), i)
            for i in range(1, len(shots))
            if i not in keep
        ]
        rest.sort(reverse=True)
        for _, index in rest[: keep_n - len(keep)]:
            keep.append(index)
        keep.sort()

    return [shots[i] for i in keep]


async def snapshot(
    api: BiliApi,
    room_id: str,
    *,
    seconds: float = DEFAULT_SECONDS,
    interval: float = DEFAULT_INTERVAL,
    work_dir: Path,
    qn: int = DEFAULT_QN,
) -> LiveShot | None:
    """完整流程：拿播放地址 → 依次试 host → 抽帧 → 拼图。任何一步失败返回 None。"""
    urls = await resolve_urls(api, room_id, qn)
    if not urls:
        return None
    try:
        for url in urls:
            shots = await asyncio.to_thread(
                grab_frames, url, work_dir, seconds, interval
            )
            if not shots:
                continue
            # 先按"画面变了多少"丢掉重复帧：12 帧里常常有 6~8 帧几乎没动，
            # 拼进去只是把每格压得更小、信息更少（思路抄自 dsh-see-now 的变化分）
            kept = await asyncio.to_thread(filter_repeats, shots)
            image = await asyncio.to_thread(
                compose,
                kept,
                cell_width=CELL_WIDTH,
                cols=3,
                label_size=18,
                labeler=clock_label,
            )
            if not image:
                continue
            frame = None
            try:
                frame = kept[len(kept) // 2].path.read_bytes()
            except OSError:
                frame = None
            log.info(
                "直播快照：房间 %s，%d→%d 帧（去掉 %d 帧静止画面），%s–%s，%d KB",
                room_id,
                len(shots),
                len(kept),
                len(shots) - len(kept),
                clock_label(kept[0].at),
                clock_label(kept[-1].at),
                len(image) // 1024,
            )
            return LiveShot(
                image=image,
                start=kept[0].at,
                end=kept[-1].at,
                frames=len(kept),
                at=time.time(),
                frame=frame,
            )
        return None
    finally:
        await asyncio.to_thread(cleanup, work_dir)


def cleanup(work_dir: Path) -> None:
    """删掉抽帧临时文件（图已经拼成字节了，单帧不用留）。"""
    for path in Path(work_dir).glob("live_*.jpg"):
        try:
            path.unlink()
        except OSError:
            pass
    if not any(Path(work_dir).iterdir()):
        shutil.rmtree(work_dir, ignore_errors=True)
