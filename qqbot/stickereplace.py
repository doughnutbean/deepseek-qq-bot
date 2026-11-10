"""群友/主人一起换掉有水印的表情包。

本地先做一次严格的相似度校验：SIFT 特征匹配 + 单应性内点，确认新图和旧图
是同一张（允许裁掉水印、缩放、压缩），对不上就拒绝，避免拿别的图把表情包换掉。
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .media import fetch_image_bytes
from .stickeranno import MIME_EXT
from .stickers import Sticker, StickerBook

try:  # 本地严格校验；没装就退回较弱的 dHash，宁可拒也不乱换
    import cv2
except ImportError:  # pragma: no cover - 有 opencv 的环境走不到
    cv2 = None

log = logging.getLogger(__name__)

# SIFT 单应性内点至少占旧图特征点的这个比例；0.35 能放过裁边/压缩，
# 又能挡住「同一个角色、不同表情」的图（实测 084 vs 296 只有 0.26）。
MATCH_RATIO = 0.35
MIN_INLIERS = 40
# 没有 opencv 时的兜底：dHash 距离 <= 6 且归一化互相关 >= 0.92
FALLBACK_DHASH = 6
FALLBACK_NCC = 0.92
# 引用旧图找编号：dHash 距离 <= 6，且不能有第二张只差 2 以内
TARGET_DHASH = 6


def _gray(raw: bytes) -> np.ndarray | None:
    try:
        with Image.open(io.BytesIO(raw)) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            if frames > 1:
                try:
                    im.seek(frames // 2)
                except Exception:  # noqa: BLE001
                    im.seek(0)
            return np.asarray(im.convert("L"), dtype=np.uint8)
    except Exception:  # noqa: BLE001
        return None


def _fit(arr: np.ndarray, max_side: int = 800) -> np.ndarray:
    h, w = arr.shape[:2]
    if max(h, w) <= max_side:
        return arr
    scale = max_side / max(h, w)
    return np.asarray(
        Image.fromarray(arr).resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    )


def _dhash_arr(arr: np.ndarray, size: int = 8) -> int:
    im = Image.fromarray(arr).resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(im.getdata())
    bits = 0
    idx = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits |= (1 if left > right else 0) << idx
            idx += 1
    return bits


def _ncc(a: np.ndarray, b: np.ndarray, size: int = 32, shift: int = 2) -> float:
    aa = np.asarray(
        Image.fromarray(a).resize((size, size), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    bb = np.asarray(
        Image.fromarray(b).resize((size, size), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    aa = (aa - aa.mean()) / (aa.std() + 1e-6)
    bb = (bb - bb.mean()) / (bb.std() + 1e-6)
    best = -1.0
    for dy in range(-shift, shift + 1):
        for dx in range(-shift, shift + 1):
            ax = aa[max(0, dy):size + min(0, dy), max(0, dx):size + min(0, dx)]
            bx = bb[max(0, -dy):size + min(0, -dy), max(0, -dx):size + min(0, -dx)]
            if ax.size < 100 or ax.shape != bx.shape:
                continue
            best = max(best, float((ax * bx).mean()))
    return best


def image_similarity(old_raw: bytes, new_raw: bytes) -> tuple[bool, str]:
    """判断新图是不是旧图（允许去水印的裁边/压缩/缩放）。"""
    a = _gray(old_raw)
    b = _gray(new_raw)
    if a is None or b is None:
        return False, "图读不出来"
    if cv2 is None:
        dh = (_dhash_arr(a) ^ _dhash_arr(b)).bit_count()
        ncc = _ncc(a, b)
        ok = dh <= FALLBACK_DHASH and ncc >= FALLBACK_NCC
        return ok, f"dHash 距离 {dh}，相关度 {ncc:.2f}（没装 opencv）"

    a = _fit(a)
    b = _fit(b)
    try:
        sift = cv2.SIFT_create(nfeatures=1000)
        kp1, des1 = sift.detectAndCompute(a, None)
        kp2, des2 = sift.detectAndCompute(b, None)
    except Exception:  # noqa: BLE001
        return False, "特征提取失败"
    if des1 is None or des2 is None or len(kp1) < 20 or len(kp2) < 20:
        dh = (_dhash_arr(a) ^ _dhash_arr(b)).bit_count()
        ncc = _ncc(a, b)
        ok = dh <= FALLBACK_DHASH and ncc >= FALLBACK_NCC
        return ok, f"特征太少，dHash 距离 {dh}，相关度 {ncc:.2f}"

    try:
        matcher = cv2.BFMatcher(cv2.NORM_L2)
        matches = matcher.knnMatch(des1, des2, k=2)
        good = [m for m, n in matches if m.distance < 0.75 * n.distance]
        if len(good) < 8:
            return False, f"只匹配到 {len(good)} 个点，不像同一张"
        src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        _h, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        inliers = int(mask.sum()) if mask is not None else 0
    except Exception:  # noqa: BLE001
        return False, "相似度计算出错"

    threshold = max(MIN_INLIERS, int(len(kp1) * MATCH_RATIO))
    if inliers >= threshold:
        return True, f"SIFT 内点 {inliers}/{len(kp1)}（阈值 {threshold}）"
    return False, f"SIFT 内点 {inliers}/{len(kp1)}，低于 {threshold}"


def identify(book: StickerBook, raw: bytes) -> Sticker | None:
    """引用旧图时找库里的编号：先精确 sha，再 dHash 最近邻。"""
    import hashlib

    item = book.by_sha.get(hashlib.sha256(raw).hexdigest())
    if item is not None:
        return item
    arr = _gray(raw)
    if arr is None:
        return None
    target = _dhash_arr(arr)
    scored: list[tuple[int, Sticker]] = []
    for candidate in book.items:
        try:
            other = _gray(candidate.path.read_bytes())
        except OSError:
            continue
        if other is None:
            continue
        scored.append(((_dhash_arr(other) ^ target).bit_count(), candidate))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0])
    best, item = scored[0]
    if best > TARGET_DHASH:
        return None
    if len(scored) > 1 and scored[1][0] <= best + 2:
        return None  # 两张都像，别猜
    return item


class ReplaceQuota:
    """每人每天换图次数；主人不计数。"""

    def __init__(self, path: Path, daily: int = 3) -> None:
        self.path = path
        self.daily = max(0, int(daily))
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def _roll(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._data.get("day") != today:
            self._data = {"day": today, "users": {}}

    def allow(self, user_id: Any, is_owner: bool) -> tuple[bool, str]:
        if is_owner or self.daily <= 0:
            return True, ""
        self._roll()
        used = int((self._data.get("users") or {}).get(str(user_id)) or 0)
        if used >= self.daily:
            return False, f"今天换图次数用完了（{self.daily} 次/人），明天再来"
        return True, ""

    def mark(self, user_id: Any, is_owner: bool) -> None:
        if is_owner or self.daily <= 0:
            return
        self._roll()
        users = self._data.setdefault("users", {})
        users[str(user_id)] = int(users.get(str(user_id)) or 0) + 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.debug("换图额度写失败: %s", exc)


class StickerReplace:
    def __init__(
        self,
        book: StickerBook,
        onebot: Any,
        data_dir: Path,
        *,
        enabled: bool = True,
        daily: int = 3,
        max_mb: float = 3.0,
    ) -> None:
        self.book = book
        self.onebot = onebot
        self.enabled = enabled
        self.max_mb = max(0.5, float(max_mb))
        self.quota = ReplaceQuota(data_dir / "sticker_replace.json", daily)

    def _segment_images(self, segments: Any) -> list[dict]:
        if not isinstance(segments, list):
            return []
        return [s for s in segments if isinstance(s, dict) and s.get("type") == "image"]

    async def _fetch(self, seg: dict) -> tuple[bytes, str] | None:
        got = await fetch_image_bytes(self.onebot, seg, self.max_mb)
        if not got:
            return None
        return got

    async def run(
        self,
        segments: Any,
        sid: str,
        user_id: Any,
        is_owner: bool,
        group_id: str,
    ) -> tuple[bool, str, Sticker | None, str]:
        if not self.enabled:
            return False, "换表情包功能关着（ENABLE_STICKER_REPLACE=false）", None, ""
        images = self._segment_images(segments)
        sid = str(sid or "").strip().lstrip("#＃")
        target: Sticker | None = None
        new_seg: dict | None = None
        if sid:
            target = self.book.by_sid.get(sid)
            if target is None:
                return False, f"没有 #{sid} 这个编号", None, ""
            if not images:
                return False, "要附一张干净的新图", None, ""
            new_seg = images[0]
        else:
            if len(images) < 2:
                return False, (
                    "要引用带水印的旧图、再附一张干净新图，或者说「替换表情包 #编号」"
                ), None, ""
            new_seg = images[0]
            old_seg = images[-1]
            old_got = await self._fetch(old_seg)
            if old_got is None:
                return False, "旧图取不到", None, ""
            target = identify(self.book, old_got[0])
            if target is None:
                return False, "没认出旧图是哪一张，带上 #编号 再试", None, ""

        if target.locked and not is_owner:
            return False, (
                f"#{target.sid} 是人工锁定的（{target.when or target.visual}），"
                "只有主人能换"
            ), target, ""
        ok, why = self.quota.allow(user_id, is_owner)
        if not ok:
            return False, why, target, ""

        new_got = await self._fetch(new_seg)
        if new_got is None:
            return False, "新图取不到（太大或链接失效）", target, ""
        new_raw, mime = new_got
        import hashlib

        new_sha = hashlib.sha256(new_raw).hexdigest()
        existing = self.book.by_sha.get(new_sha)
        if existing is not None:
            if existing.sid == target.sid:
                return False, "新图和旧图字节一样，没换", target, ""
            return False, f"这张新图已经在库里了（#{existing.sid}）", target, ""
        try:
            old_raw = target.path.read_bytes()
        except OSError:
            return False, "旧图文件不在了", target, ""
        same, detail = image_similarity(old_raw, new_raw)
        if not same:
            return False, f"没换：新图和 #{target.sid} 不像（{detail}），怕换错", target, ""
        ext = MIME_EXT.get(mime) or target.path.suffix
        old_sha = target.sha
        entry, err = self.book.replace_image(target, new_raw, ext, user_id)
        if entry is None:
            return False, f"没换成：{err}", target, old_sha
        self.quota.mark(user_id, is_owner)
        log.info(
            "表情包换图成功：%s by %s（群 %s，%s）",
            target.sid, user_id, group_id or "-", detail,
        )
        return True, (
            f"换好了：{target.folder} #{target.sid}｜{target.path.name}"
            f"｜{detail}；旧图已备份"
        ), target, old_sha
