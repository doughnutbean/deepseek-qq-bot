"""视频分镜：按镜头抽 12 格，时间戳烧在每格上，拼成**一张**图。

为什么不把视频给模型
-------------------
模型只吃图片。视频理解 = 抽帧 + 字幕；一张图里塞多少「什么时候发生了什么」，
决定了她是真看过还是瞎猜。所以这里做的是：

- **按镜头切**：用 ffmpeg 的 `scene` 检测找切点，再把时间轴切成 N 段，每段取
  离中点最近的切点 —— 每一格都落在一个镜头里，不会拍到转场糊帧；
- **时间戳**：每格左上角烧上 `#07 03:12`，她才能说「03:12 那格」，而不是
  「我看过」。这是 0 token 的，画在像素上；
- **留底**：抽出来的 960 宽单帧在 `data/bili/frames/<bvid>/` 留一天，
  主人追问「03:10 那段呢」时直接重拼一张放大图，不用再下 40MB。

边界
----
下载、ffmpeg 都有硬超时；失败返回 None，调用方当「这次没有画面」，不影响回复。
默认关（`ENABLE_BILI_FRAMES`）：一次要下整段最低码率流（十分钟 360p 约 20~40MB）。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from .bili_api import BiliApi, is_media_url

log = logging.getLogger(__name__)

MAX_DOWNLOAD_MB = 200
DOWNLOAD_TIMEOUT = 40.0
FFMPEG_TIMEOUT = 30.0
SCENE_TIMEOUT = 25.0
# 超过这么长的视频不做整段解码找切点（太费 CPU），退回按时间均匀取
SCENE_MAX_SECONDS = 1800
# 一张分镜最多几格（4x4）；再多字就看不清了
CELLS = 12
MAX_CELLS = 16
# 每格多大、源帧抽多宽：网格缩到 400，zoom 用 960 原图
CELL_WIDTH = 400
FRAME_WIDTH = 960
ZOOM_CELL_WIDTH = 640
# 抽出来的单帧留多久、最多留几个视频
KEEP_HOURS = 24.0
KEEP_DIRS = 8
# 镜头切点阈值：0.3 是 ffmpeg 社区常用的「明显换镜头」
SCENE_THRESHOLD = 0.3
_GUTTER = 4
# 视觉 patch 网格：模型按 42px 一格数格子，图宽对齐到 42 的倍数才不会被 pad 掉一列
PATCH = 42
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)
_META = "meta.json"

_STREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


@dataclass(frozen=True)
class Shot:
    """一帧：文件在哪（或直接给 JPEG 字节）、对应视频第几秒。

    `data` 给直播的滚动缓冲用：帧只存在内存里，不落盘，过时就丢。
    """

    path: Path | None
    at: float
    data: bytes | None = None


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def format_stamp(seconds: float) -> str:
    """190 → 「03:10」；超过一小时写 「1:03:10」。"""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


@lru_cache(maxsize=8)
def _font(size: int):
    """带缓存的中文字体；找不到就退回 PIL 默认字体（字小，但不崩）。"""
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, int(size))
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=int(size))
    except TypeError:  # Pillow < 10.1 不认识 size
        return ImageFont.load_default()


def frame_times(
    duration: int,
    count: int = CELLS,
    cuts: list[float] | None = None,
    max_cells: int = MAX_CELLS,
) -> list[float]:
    """把时间轴切成 count 段，每段挑一个时间点（优先离段中点最近的镜头切点）。

    没有切点（短视频 / 纯色画面 / ffmpeg 不在）就取段中点，等价于均匀抽帧。
    时长为 0 时给一个兜底点。`max_cells` 是总帧数上限（多张分镜时可以放宽到 48）。
    """
    total = max(1, int(duration))
    count = max(1, min(int(count), max(1, int(max_cells))))
    if total <= 1:
        return [1.0]
    points = sorted(float(c) for c in (cuts or ()) if 0 < float(c) < total)
    out: list[float] = []
    for index in range(count):
        low = total * index / count
        high = total * (index + 1) / count
        middle = (low + high) / 2
        inside = [c for c in points if low <= c < high]
        moment = min(inside, key=lambda c: abs(c - middle)) if inside else middle
        out.append(round(max(0.5, min(total - 0.5, moment)), 2))
    return out


def parse_scene_times(stderr: str) -> list[float]:
    """从 ffmpeg `metadata=print` 的输出里抠切点时间（秒），去重排序。"""
    found = {
        round(float(match), 2)
        for match in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", stderr or "")
    }
    return sorted(t for t in found if t > 0)


def scene_cuts(
    video: Path, duration: int = 0, timeout: float = SCENE_TIMEOUT
) -> list[float]:
    """ffmpeg 找镜头切点（秒）。整段解码，长视频直接放弃，省 CPU。"""
    exe = ffmpeg_path()
    if not exe:
        return []
    if duration and int(duration) > SCENE_MAX_SECONDS:
        log.info("视频超过 %d 秒，不找镜头切点，按时间均匀取", SCENE_MAX_SECONDS)
        return []
    cmd = [
        exe,
        "-v",
        "info",
        "-nostdin",
        "-i",
        str(video),
        "-an",
        "-vf",
        f"select='gt(scene,{SCENE_THRESHOLD})',metadata=print",
        "-f",
        "null",
        "-",
    ]
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        log.info("镜头检测失败/超时，按时间均匀取")
        return []
    return parse_scene_times(done.stderr)


async def download(
    video_url: str,
    dest: Path,
    max_mb: int = MAX_DOWNLOAD_MB,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> bool:
    """把视频流下到本地（硬上限）。URL 只认 B 站自己的域。"""
    if not is_media_url(video_url):
        log.info("视频流域名不在 B 站域内，拒绝下载")
        return False
    limit = max(8, int(max_mb)) * 1024 * 1024
    total = 0
    try:
        async with httpx.AsyncClient(
            timeout=max(5.0, float(timeout)),
            headers=_STREAM_HEADERS,
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", video_url) as resp:
                if resp.status_code != 200:
                    log.info("视频流下载 HTTP %s", resp.status_code)
                    return False
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        total += len(chunk)
                        if total > limit:
                            log.info("视频流超过 %d MB，放弃", max_mb)
                            return False
                        fh.write(chunk)
    except Exception as exc:  # noqa: BLE001 - 抓不到画面不影响回复
        log.info("视频流下载失败（%s）", type(exc).__name__, exc_info=True)
        return False
    return total > 0


def extract_frames(
    video: Path,
    times: list[float],
    out_dir: Path,
    prefix: str = "",
    width: int = FRAME_WIDTH,
) -> list[Shot]:
    """按时间点各抽一帧（960 宽），抽不到的跳过；返回的 at 是**真实抽到**的时间。"""
    exe = ffmpeg_path()
    if not exe:
        log.info("没装 ffmpeg，抽不了画面")
        return []
    out: list[Shot] = []
    for index, moment in enumerate(times):
        target = out_dir / f"{prefix}f{index:02d}.jpg"
        cmd = [
            exe,
            "-y",
            "-nostdin",
            "-ss",
            f"{float(moment):.2f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={int(width)}:-2",
            "-q:v",
            "3",
            str(target),
        ]
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if target.is_file() and target.stat().st_size > 0:
            out.append(Shot(target, float(moment)))
    return out


def _paste(canvas, image, x: int, y: int, cell_w: int, cell_h: int) -> None:
    from PIL import Image

    canvas.paste(image.resize((cell_w, cell_h), Image.Resampling.LANCZOS), (x, y))


def _label(
    draw,
    x: int,
    y: int,
    index: int,
    at: float,
    size: int,
    labeler=None,
) -> None:
    """左上角写「#07 03:12」，黑描边保证在亮画面上也看得清。

    `labeler` 给直播用：那边的时间是「当天第几秒」，要写成 22:23:31，
    不能走视频的 mm:ss。
    """
    stamp = labeler(at) if labeler else format_stamp(at)
    draw.text(
        (x + 6, y + 4),
        f"#{index + 1:02d} {stamp}",
        font=_font(size),
        fill=(255, 255, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )


def _align_width(canvas):
    """把拼图宽度对齐到 42 的整数倍（模型按 42px 一格数格子）。

    视觉 token ≈ rows×(cols+1)+2，`cols` 是向上取整的 —— 宽度 1216 会按 29 列
    收钱，而 1176（28×42）只要 28 列，中间那点像素白付。这里整体缩到最近的
    42 倍数（最多缩 0.9%），一列的钱就省下来了。

    思路抄自 dsh-see-now 的档位对齐（800→798 / 1280→1260），我们这套拼图
    宽度由格子数算出来，没法直接挑数字，所以用一次高质量缩放落地。
    """
    try:
        width, height = canvas.size
        if width <= PATCH:
            return canvas
        target = width - (width % PATCH)
        if target <= 0 or target == width:
            return canvas
        target_h = max(1, int(round(height * target / width)))
        from PIL import Image

        return canvas.resize((target, target_h), Image.Resampling.LANCZOS)
    except Exception:  # noqa: BLE001 - 缩放失败就用原图，不影响出图
        return canvas


def compose(
    shots: list[Shot],
    cell_width: int = CELL_WIDTH,
    cols: int | None = None,
    label_size: int = 18,
    quality: int = 85,
    labeler=None,
) -> bytes | None:
    """几帧拼成一张 JPEG。格数、列数自适应；一帧都读不出来就 None。"""
    if not shots:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None
    images = []
    valid: list[Shot] = []
    for shot in shots:
        try:
            if shot.data:
                with Image.open(io.BytesIO(shot.data)) as img:
                    images.append(img.convert("RGB").copy())
                    valid.append(shot)
            elif shot.path is not None:
                with Image.open(shot.path) as img:
                    images.append(img.convert("RGB").copy())
                    valid.append(shot)
        except Exception:  # noqa: BLE001
            continue
    if not images:
        return None
    count = len(images)
    columns = int(cols) if cols else (3 if count <= 12 else 4)
    columns = max(1, min(columns, count))
    rows = (count + columns - 1) // columns
    cell_w = max(120, int(cell_width))
    first = images[0]
    cell_h = max(1, int(round(first.height * cell_w / max(1, first.width))))
    canvas_w = columns * cell_w + (columns + 1) * _GUTTER
    canvas_h = rows * cell_h + (rows + 1) * _GUTTER
    canvas = Image.new("RGB", (canvas_w, canvas_h), (18, 18, 18))
    draw = ImageDraw.Draw(canvas)
    for index, image in enumerate(images):
        x = _GUTTER + (index % columns) * (cell_w + _GUTTER)
        y = _GUTTER + (index // columns) * (cell_h + _GUTTER)
        _paste(canvas, image, x, y, cell_w, cell_h)
        _label(draw, x, y, index, valid[index].at, label_size, labeler)
    out = io.BytesIO()
    try:
        canvas = _align_width(canvas)
        canvas.save(out, format="JPEG", quality=int(quality), optimize=True)
    except Exception:  # noqa: BLE001
        return None
    return out.getvalue()


def compose_zoom(shots: list[Shot]) -> bytes | None:
    """放大图：最多 4 帧 2x2，每帧 640 宽，时间戳照样烧上。"""
    if not shots:
        return None
    picked = shots[:4]
    cols = 2 if len(picked) > 1 else 1
    return compose(picked, cell_width=ZOOM_CELL_WIDTH, cols=cols, label_size=26)


def select_window(
    shots: list[Shot], start: float, end: float, count: int = 4
) -> list[Shot]:
    """从留底帧里挑一段（start~end）的帧，最多 count 张，按时间排序。

    这一段里一帧都没有时，退回离这段最近的一帧 —— 总比给一张别的镜头的图强，
    调用方会按**实际挑到的**时间写文案。
    """
    if not shots:
        return []
    low, high = sorted((float(start), float(end)))
    inside = [shot for shot in shots if low - 0.01 <= shot.at <= high + 0.01]
    if not inside:
        nearest = min(shots, key=lambda shot: min(abs(shot.at - low), abs(shot.at - high)))
        return [nearest]
    inside.sort(key=lambda shot: shot.at)
    if len(inside) > count:
        picks = [round(i * (len(inside) - 1) / (count - 1)) for i in range(count)]
        inside = [inside[i] for i in picks]
    return inside


def keep_shots(
    shots: list[Shot],
    keep_dir: Path,
    bvid: str,
    duration: int = 0,
    hours: float = KEEP_HOURS,
) -> int:
    """把这一轮的帧和 meta 留底，给后面的 zoom 用。返回留下几张。"""
    if not shots:
        return 0
    try:
        keep_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.info("留底目录建不了，跳过 zoom 留底：%s", keep_dir)
        return 0
    kept = []
    for index, shot in enumerate(shots):
        if shot.path is None:
            continue
        target = keep_dir / f"f{index:02d}.jpg"
        try:
            shutil.copyfile(shot.path, target)
        except OSError:
            continue
        kept.append({"file": target.name, "at": round(shot.at, 2)})
    if not kept:
        return 0
    meta = {
        "bvid": bvid,
        "duration": int(duration or 0),
        "created": int(time.time()),
        "shots": kept,
    }
    try:
        (keep_dir / _META).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        log.info("meta 写不进去，zoom 会当没留底")
        return 0
    prune_kept(keep_dir.parent, hours=hours)
    return len(kept)


def load_kept(keep_dir: Path) -> list[Shot]:
    """读回留底的帧；meta 坏了/文件被删了就返回空。"""
    try:
        meta = json.loads((keep_dir / _META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[Shot] = []
    for item in meta.get("shots") or []:
        try:
            path = keep_dir / str(item["file"])
            at = float(item["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if path.is_file() and path.stat().st_size > 0:
            out.append(Shot(path, at))
    return out


def prune_kept(root: Path, hours: float = KEEP_HOURS, keep: int = KEEP_DIRS) -> None:
    """删过期和超量的留底目录；删失败不管（又不是业务）。"""
    try:
        dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return
    cutoff = time.time() - max(0.0, float(hours)) * 3600
    dirs.sort(key=lambda p: _mtime(p), reverse=True)
    for index, path in enumerate(dirs):
        if index >= max(1, int(keep)) or _mtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


async def grab_multi(
    api: BiliApi,
    bvid: str,
    cid: str,
    duration: int,
    grids: int = 1,
    cells: int = CELLS,
    qn: int = 16,
    max_mb: int = MAX_DOWNLOAD_MB,
    download_timeout: float = DOWNLOAD_TIMEOUT,
    work_root: Path | None = None,
    keep_dir: Path | None = None,
    keep_hours: float = KEEP_HOURS,
) -> list[bytes]:
    """完整流程：拿流 → 下载 → 找镜头 → 抽帧 → 拼 N 张分镜。

    一次下载复用给多张分镜：`grids=2/3` 时总帧数 = grids*cells，
    按时间顺序切成每张 cells 格，覆盖整条视频。
    `qn` 是画质档位：短视频可以给 1080p/720p，长视频用 480p/360p 控制下载量；
    高画质下载失败（超时/超体积）会自动退回 360p 再试一次。
    """
    grids = max(1, min(3, int(grids)))
    cells = max(4, min(MAX_CELLS, int(cells)))
    total = min(48, grids * cells)
    # 不用 mkdtemp 建子目录：Windows 沙箱下新建目录的 ACL 会挡住写文件，
    # 平铺在 work_root 里更省事，用完逐个删。
    work = Path(work_root) if work_root else Path(tempfile.mkdtemp(prefix="frames_"))
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        work = Path(tempfile.mkdtemp(prefix="frames_"))
    temp_files: list[Path] = []

    async def fetch(quality: int) -> Path | None:
        url = await api.playurl(bvid, cid, qn=quality)
        if not url:
            log.info("B站视频流拿不到：%s", bvid)
            return None
        video = work / f"{bvid}_{quality}.m4s"
        temp_files.append(video)
        ok = await download(
            url, video, max_mb=max_mb, timeout=download_timeout
        )
        return video if ok else None

    try:
        video = await fetch(qn)
        if video is None and qn != 16:
            log.info("高画质流没下来，退回 360p：%s", bvid)
            video = await fetch(16)
        if video is None:
            return []
        cuts = await asyncio.to_thread(scene_cuts, video, duration)
        times = frame_times(duration, total, cuts, max_cells=total)
        shots = await asyncio.to_thread(
            extract_frames, video, times, work, f"{bvid}_"
        )
        temp_files.extend(shot.path for shot in shots)
        if not shots:
            log.info("B站抽帧失败：%s", bvid)
            return []
        images: list[bytes] = []
        for start in range(0, len(shots), cells):
            image = await asyncio.to_thread(compose, shots[start : start + cells])
            if image:
                images.append(image)
        if images and keep_dir is not None:
            await asyncio.to_thread(
                keep_shots, shots, keep_dir, bvid, duration, keep_hours
            )
        return images
    finally:
        for path in temp_files:
            try:
                path.unlink()
            except OSError:
                pass
        if work_root is None:
            shutil.rmtree(work, ignore_errors=True)


async def grab(
    api: BiliApi,
    bvid: str,
    cid: str,
    duration: int,
    count: int = CELLS,
    work_root: Path | None = None,
    keep_dir: Path | None = None,
    keep_hours: float = KEEP_HOURS,
    qn: int = 16,
) -> bytes | None:
    """兼容旧调用：单张分镜，返回 JPEG 字节或 None。"""
    images = await grab_multi(
        api,
        bvid,
        cid,
        duration,
        grids=1,
        cells=count,
        qn=qn,
        work_root=work_root,
        keep_dir=keep_dir,
        keep_hours=keep_hours,
    )
    return images[0] if images else None
