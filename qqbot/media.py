"""把 QQ 消息里的图片变成模型能吃的 base64 数据块。

协议端给的 image 段落长这样（字段不一定齐全）：
    {"type": "image",
     "data": {"file": "xxxx.jpg", "url": "https://multimedia.nt.qq.com.cn/...",
              "path": "C:\\...\\xxxx.jpg", "file_size": "123456", "summary": "[图片]"}}

来源优先级：http 链接 > 本地 path > 调 OneBot 的 get_image 接口换 base64。

超过 MAX_IMAGE_MB 的图**不再直接丢掉**：群里动图动辄 9~10 MB，丢了她就只能回
"图没加载出来"。静图先用 Pillow 取一帧、缩到视觉模型够用的边长、转成 JPEG，
压到上限以内再送；动图则先抽最多 9 帧拼成一张九宫格 —— 视觉接口只解第一帧，
拼图才能让她看到整段动作，而 token 仍按一张图算。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiohttp

from .config import ROOT

log = logging.getLogger(__name__)

# DeepSeek 只认这四种
_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def guess_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _download(
    session: aiohttp.ClientSession,
    url: str,
    hard_bytes: int,
    reason: list[str] | None = None,
) -> bytes | None:
    """下载一张图；失败时把**给人看的原因**追加到 reason（可选）。"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                log.warning("图片下载失败 HTTP %s", resp.status)
                if reason is not None:
                    reason.append(f"下载失败 HTTP {resp.status}")
                return None
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buf.extend(chunk)
                if len(buf) > hard_bytes:
                    log.warning("图片超过 %s 字节，放弃下载", hard_bytes)
                    if reason is not None:
                        reason.append(f"超过 {hard_bytes / 1048576:.0f} MB 上限")
                    return None
            return bytes(buf)
    except Exception as exc:  # noqa: BLE001
        log.warning("图片下载异常: %s", exc)
        if reason is not None:
            reason.append(f"下载出错（{type(exc).__name__}）")
        return None


def _decode_base64_uri(raw: str) -> bytes | None:
    if not raw.startswith("base64://"):
        return None
    try:
        return base64.b64decode(raw[len("base64://"):])
    except Exception:  # noqa: BLE001
        return None


async def _via_onebot(onebot, file_name: str) -> bytes | None:
    """get_image 会返回 base64:// 或本地路径。"""
    try:
        resp = await onebot.call("get_image", {"file": file_name}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log.warning("get_image 调用失败: %s", exc)
        return None

    data = (resp or {}).get("data") or {}
    for key in ("file", "base64", "url"):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        decoded = _decode_base64_uri(value)
        if decoded:
            return decoded
        path = Path(value)
        if path.is_file():
            try:
                return path.read_bytes()
            except OSError:
                pass
    return None


_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}

# 超过上限的图先压缩再送，所以下载时得把尺子放宽：留 4 倍余量、至少 32 MB，
# 一张十兆的动图才拿得到完整文件。再大就不值得为一张图吃内存和带宽了。
_FETCH_SLACK = 4
_FETCH_MIN_BYTES = 32 * 1024 * 1024
# 视觉模型认图用不到原图那么大，缩到这个边长以内足够（只缩不放）
_SHRINK_SIDES = (1280, 1024, 768, 512)
_SHRINK_QUALITIES = (85, 70, 55, 40)

# 动图（GIF/WebP）不再只送第一帧：抽最多 9 帧拼成一张九宫格。
# 模型只看得到一张图，拼图也按 1 张图计费，但能从格子里看出动作顺序。
_GRID_FRAMES = 9
_GRID_SIDE = 1280
_GRID_GUTTER = 6
_GRID_HEADER = 26
_GRID_FRAME_SIDE = 640
_GRID_BG = (246, 246, 246)
_GRID_QUALITIES = (88, 80, 70, 60, 50)
# 视觉 patch 网格：模型按 42px 一格数格子，图宽对齐到 42 的倍数能少付一列
_GRID_PATCH = 42
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)


@lru_cache(maxsize=4)
def _grid_font(size: int):
    """拼图上的帧号/表头字体；找不到就退回 PIL 默认字体。"""
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


def _frame_indices(total: int, count: int = _GRID_FRAMES) -> list[int]:
    """从 total 帧里均匀挑 count 帧（含首尾）；帧少就全要。"""
    total = max(1, int(total))
    count = max(1, min(int(count), total))
    if count == 1:
        return [0]
    picked = {
        min(total - 1, int(round(index * (total - 1) / (count - 1))))
        for index in range(count)
    }
    return sorted(picked)


def _flatten(frame):
    """GIF 的透明底贴白，转成 RGB；其它模式直接转。"""
    from PIL import Image

    if frame.mode in ("RGBA", "LA") or (
        frame.mode == "P" and "transparency" in frame.info
    ):
        rgba = frame.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return frame.convert("RGB")


def _extract_frames(raw: bytes, count: int = _GRID_FRAMES):
    """按顺序解出最多 count 帧；不是动图/帧不够/解不开都返回 []。

    顺序 seek 是关键：GIF 的 disposal 要靠前一帧拼出当前画面，乱跳会拿到
    半透明或残缺的帧。每帧先缩到 640 宽以内，95 帧的大图也不会把内存吃光。
    """
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        total = int(getattr(im, "n_frames", 1) or 1)
        if total <= 1:
            return []
        wanted = set(_frame_indices(total, count))
        frames = []
        for index in range(max(wanted) + 1):
            try:
                im.seek(index)
            except Exception:  # noqa: BLE001 - 后面读不出来就用已经拿到的
                break
            if index not in wanted:
                continue
            try:
                frame = _flatten(im)
                if max(frame.size) > _GRID_FRAME_SIDE:
                    frame.thumbnail(
                        (_GRID_FRAME_SIDE, _GRID_FRAME_SIDE), Image.Resampling.LANCZOS
                    )
                frames.append(frame)
            except Exception:  # noqa: BLE001 - 坏帧跳过，别拖垮整张图
                continue
        return frames if len(frames) >= 2 else []


def _align_grid_width(canvas):
    """把拼图宽度对齐到 42 的整数倍（模型按 42px 一格数格子）。

    思路抄自 `bili_frames._align_width`：宽度 1278 会按 31 列收钱，
    1260（30×42）只要 30 列，整体缩 1.4% 就能省一列。
    """
    try:
        from PIL import Image

        width, height = canvas.size
        if width <= _GRID_PATCH:
            return canvas
        target = width - (width % _GRID_PATCH)
        if target <= 0 or target == width:
            return canvas
        return canvas.resize(
            (target, max(1, int(round(height * target / width)))),
            Image.Resampling.LANCZOS,
        )
    except Exception:  # noqa: BLE001 - 缩放失败就用原图，不影响出图
        return canvas


def _grid_jpeg(frames: list[Any], max_bytes: int) -> bytes | None:
    """几帧拼成一张 JPEG；放不进 max_bytes 就逐档降质，仍不行返回 None。"""
    from PIL import Image, ImageDraw, ImageOps

    count = len(frames)
    columns = 2 if count == 4 else min(3, count)
    rows = (count + columns - 1) // columns
    width, height = frames[0].size
    cell_w = max(64, (_GRID_SIDE - _GRID_GUTTER * (columns + 1)) // columns)
    cell_h = max(1, int(round(cell_w * height / max(1, width))))
    room = _GRID_SIDE - _GRID_HEADER - _GRID_GUTTER * (rows + 1)
    if rows * cell_h > room:
        cell_w = max(32, int(cell_w * room / (rows * cell_h)))
        cell_h = max(1, int(round(cell_w * height / max(1, width))))

    canvas_w = columns * cell_w + _GRID_GUTTER * (columns + 1)
    canvas_h = _GRID_HEADER + rows * cell_h + _GRID_GUTTER * (rows + 1)
    canvas = Image.new("RGB", (canvas_w, canvas_h), _GRID_BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, canvas_w, _GRID_HEADER - 1], fill=(32, 32, 32))
    draw.text(
        (8, 5),
        (
            f"ANIMATED IMAGE / {count} FRAMES / READ LEFT TO RIGHT, TOP TO BOTTOM"
            " / EACH CELL = ONE FRAME"
        ),
        font=_grid_font(14),
        fill=(255, 255, 255),
    )
    for index, frame in enumerate(frames):
        x = _GRID_GUTTER + (index % columns) * (cell_w + _GRID_GUTTER)
        y = _GRID_HEADER + _GRID_GUTTER + (index // columns) * (cell_h + _GRID_GUTTER)
        draw.rectangle([x - 1, y - 1, x + cell_w, y + cell_h], outline=(190, 190, 190))
        fitted = ImageOps.contain(
            frame, (cell_w, cell_h), Image.Resampling.LANCZOS
        )
        canvas.paste(
            fitted,
            (x + (cell_w - fitted.width) // 2, y + (cell_h - fitted.height) // 2),
        )
        draw.text(
            (x + 4, y + 2),
            f"#{index + 1}",
            font=_grid_font(18),
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )

    canvas = _align_grid_width(canvas)
    for quality in _GRID_QUALITIES:
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        if len(data) <= max_bytes:
            return data
    return None


def _animation_grid(raw: bytes, max_bytes: int) -> tuple[bytes, int] | None:
    """动图 → (九宫格 JPEG, 抽了几帧)；不是动图或拼不出来返回 None。"""
    try:
        frames = _extract_frames(raw)
        if not frames:
            return None
        data = _grid_jpeg(frames, max_bytes)
        if data is None:
            return None
        return data, len(frames)
    except Exception as exc:  # noqa: BLE001 - 抽帧失败退回老的单帧逻辑
        log.info("动图抽帧失败，退回单帧：%s", exc)
        return None


def animation_grid(
    raw: bytes, max_bytes: int = 8 * 1024 * 1024
) -> tuple[bytes, int] | None:
    """公开入口：给工具/旁路复用同一套动图抽帧拼图，别各写一份。"""
    return _animation_grid(raw, max_bytes)


def _pick_frame(img) -> None:
    """动图取中间那帧：很多 GIF 第一帧是空白或封面，中间帧才是内容。"""
    try:
        frames = int(getattr(img, "n_frames", 1) or 1)
    except Exception:  # noqa: BLE001
        frames = 1
    if frames <= 1:
        return
    for index in (frames // 2, 0):
        try:
            img.seek(index)
            return
        except Exception:  # noqa: BLE001
            continue


def _shrink(raw: bytes, max_bytes: int) -> tuple[bytes, str] | None:
    """把过大的图压到 max_bytes 以内，返回 (字节, mime)；压不动返回 None。

    GIF/WebP 动图只保留一帧 —— 模型本来也只看得到一帧，少这一帧好过整张图没有。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        log.warning("图片过大需要压缩，但没装 Pillow，只能跳过")
        return None

    try:
        with Image.open(io.BytesIO(raw)) as img:
            _pick_frame(img)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                frame = bg
            else:
                frame = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.warning("图片解不开，没法压缩: %s", exc)
        return None

    for side in _SHRINK_SIDES:
        scaled = frame.copy()
        scaled.thumbnail((side, side))
        for quality in _SHRINK_QUALITIES:
            out = io.BytesIO()
            try:
                scaled.save(out, format="JPEG", quality=quality, optimize=True)
            except Exception:  # noqa: BLE001
                return None
            data = out.getvalue()
            if len(data) <= max_bytes:
                return data, "image/jpeg"
    return None


def image_block(raw: bytes, max_bytes: int) -> dict[str, Any] | None:
    """原始字节 → 视觉模型内容块。

    给「不是 QQ 消息里的图」用（比如 B 站封面）：超过上限先压缩，格式不认或
    压不动就返回 None，调用方当这张图没拿到。
    """
    if not raw:
        return None
    mime = guess_mime(raw)
    if not mime:
        return None
    if len(raw) > max_bytes:
        shrunk = _shrink(raw, max_bytes)
        if shrunk is None:
            return None
        raw, mime = shrunk
    b64 = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


async def _read_source(
    session,
    onebot,
    data: dict[str, Any],
    cap: int,
    reason: list[str] | None = None,
) -> bytes | None:
    """按 http > 本地 path > get_image 的顺序拿到原始字节。"""
    url = data.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        raw = await _download(session, url, cap, reason)
        if raw is not None:
            return raw

    local = data.get("path") or data.get("file")
    if isinstance(local, str) and local and Path(local).is_file():
        try:
            size = Path(local).stat().st_size
            if size <= cap:
                return Path(local).read_bytes()
            if reason is not None:
                reason.append(
                    f"原图 {size / 1048576:.1f} MB，超过 {cap / 1048576:.0f} MB 上限"
                )
        except OSError:
            pass

    file_name = data.get("file")
    if onebot is not None and isinstance(file_name, str) and file_name:
        return await _via_onebot(onebot, file_name)
    return None


async def fetch_image_bytes(
    onebot,
    seg: dict[str, Any],
    max_mb: float = 3.0,
    *,
    reason: list[str] | None = None,
) -> tuple[bytes, str] | None:
    """取一张图的**原始字节**（不压缩、不落 cache），给收表情包用。

    入库要保留原格式（动图不能压成一张 JPEG），所以超过上限就直接放弃，
    和 `collect_images`「压缩后再送模型」的取舍不一样。

    `reason` 不为 None 时，失败原因会追加进去（回执直接用，不用猜）。
    """
    cap = max(1, int(max_mb * 1024 * 1024))
    data = seg.get("data") or {}
    async with aiohttp.ClientSession() as session:
        raw = await _read_source(session, onebot, data, cap, reason)
    if not raw:
        if reason is not None and not reason:
            reason.append("链接和本地文件都取不到")
        return None
    if len(raw) > cap:
        if reason is not None:
            reason.append(
                f"原图 {len(raw) / 1048576:.1f} MB，超过 {max_mb:.0f} MB 上限"
            )
        return None
    mime = guess_mime(raw)
    if not mime:
        if reason is not None:
            reason.append("格式不认（只收 jpg/png/gif/webp）")
        return None
    return raw, mime


async def collect_images(
    onebot,
    segments: list[dict[str, Any]],
    max_images: int,
    max_image_mb: float,
) -> tuple[
    list[dict[str, Any]], str | None, list[str], list[bytes], dict[str, int]
]:
    """把消息里的图片转成 OpenAI 视觉格式的内容块。

    返回 (内容块, 第一张图的缓存路径, 每张图的指纹, 每张图的原始字节, 动图帧数)。
    - 动图（GIF/WebP）抽最多 9 帧拼成一张 JPEG，块里是拼图；原始字节另外返回，
      给「认自己的表情包」按原图对 dHash（拿拼图去对肯定对不上）。
    - 缓存路径存的是**原图**，给「把这张设成头像」用。
    """
    if max_images <= 0:
        return [], None, [], [], {}

    max_bytes = int(max_image_mb * 1024 * 1024)
    fetch_cap = max(max_bytes * _FETCH_SLACK, _FETCH_MIN_BYTES)
    picked = [s for s in segments if s.get("type") == "image"][:max_images]
    if not picked:
        return [], None, [], [], {}

    blocks: list[dict[str, Any]] = []
    hashes: list[str] = []
    raws: list[bytes] = []
    animations: dict[str, int] = {}
    saved_path: str | None = None
    async with aiohttp.ClientSession() as session:
        for seg in picked:
            data = seg.get("data") or {}
            raw = await _read_source(session, onebot, data, fetch_cap)

            if not raw:
                log.warning("这张图片取不到内容，跳过")
                continue

            original = raw
            mime = guess_mime(raw)
            if not mime:
                log.warning("图片格式不在 JPEG/PNG/GIF/WebP 之内，跳过")
                continue

            animated = 0
            if mime in ("image/gif", "image/webp"):
                # 动图不再只送第一帧：抽几帧拼成一张，模型才看得到动作顺序。
                grid = await asyncio.to_thread(_animation_grid, original, max_bytes)
                if grid is not None:
                    raw, animated = grid
                    mime = "image/jpeg"
                    log.info("动图抽了 %d 帧拼成一张再送", animated)

            if not animated and len(raw) > max_bytes:
                shrunk = _shrink(raw, max_bytes)
                if shrunk is None:
                    log.warning(
                        "图片 %.1f MB 超过 %.1f MB 上限，压缩也失败，跳过",
                        len(raw) / 1048576,
                        max_image_mb,
                    )
                    continue
                before_mb = len(raw) / 1048576
                raw, mime = shrunk
                log.info(
                    "图片 %.1f MB 超过 %.1f MB 上限，已压缩到 %.0f KB 再送",
                    before_mb,
                    max_image_mb,
                    len(raw) / 1024,
                )

            digest = hashlib.sha1(original).hexdigest()
            if saved_path is None:
                # 头像要用原图（动图就是动图），不是拼图。
                saved_path = _save_to_cache(original, guess_mime(original) or mime)

            hashes.append(digest)
            raws.append(original)
            if animated:
                animations[digest] = animated
            b64 = base64.b64encode(raw).decode("ascii")
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
            log.info(
                "已加载图片 %.1f KB (%s)%s",
                len(raw) / 1024,
                mime,
                f"，动图 {animated} 帧拼图" if animated else "",
            )

    return blocks, saved_path, hashes, raws, animations


def _save_to_cache(raw: bytes, mime: str) -> str | None:
    """把图片落到 cache/，供 set_qq_avatar 使用。"""
    try:
        cache = ROOT / "cache"
        cache.mkdir(exist_ok=True)
        path = cache / f"last_image{_EXT.get(mime, '.jpg')}"
        path.write_bytes(raw)
        return str(path)
    except OSError as exc:
        log.warning("缓存图片失败: %s", exc)
        return None
