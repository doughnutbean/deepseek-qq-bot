"""从群聊里捡表情包：主角是她、不重复、走完整的入库流程。

触发：群里有人发图 → `Bot` 把图段丢给 `schedule()`，后台任务里做：

1. 取原图（保留 webp/gif，不压成 JPEG）
2. 精确去重（sha256 对 index.json）
3. 近似去重（dHash 汉明距离 ≤ 4，对图库和已经拒过的图）
4. 「主角是不是她」视觉判定（比入库分类更严，见 `stickeranno.HER_PROMPT`）
5. 逐图注解（图里的字 / 梗 / when / never，和命令行入库同一份提示词）
6. 选分类（现有目录优先，选不出就进「捡来的」）
7. 落盘 + 并进 index.json + 更新向量（运行时立即可用）

保护：
- 每天最多收 `STICKER_STEAL_DAILY` 张（0 = 关），两次至少隔
  `STICKER_STEAL_MIN_MINUTES` 分钟；视觉判定每天最多 `STICKER_STEAL_CHECKS` 次
- 只在群聊触发、只认 3MB 以内的图（主人点名/人工收图放宽到 10MB），
  长截图/超宽图直接跳过
- 断网/模型失败只记日志，不影响聊天
- 状态在 `data/sticker_steal.json`，重启不忘

“完整的表情包入库工作流”在这里是代码化的：去重 → 主角判定 → 分类 → 注解 →
索引 → 向量。命令行那套（`tools/annotate_stickers.py`）仍然可以补跑。
"""

from __future__ import annotations

import asyncio
import colorsys
import hashlib
import io
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .media import animation_grid, fetch_image_bytes, guess_mime
from .stickeranno import (
    MIME_EXT,
    annotate_image,
    classify_her,
    clean_annotation,
    grid_note,
)

log = logging.getLogger(__name__)

# 和 tools/dedupe_stickers.py 的默认阈值一致：64 位 dHash 差 4 位以内算同一张
NEAR_DHASH = 4
MAX_SIDE = 1600
MIN_SIDE = 40
MAX_RATIO = 2.5
FOLDER_FALLBACK = "捡来的"
FOLDER_DESC = "从群里捡来的表情包，按逐图 when 判断"
SEEN_KEEP_DAYS = 30
# 本地粗筛：她的图里蓝（头发/裙子）总得占一点；黑化版靠「大面积暗 + 白色头饰」兜底。
# 只挡明显不是她的照片，挡不住就交给视觉判定；宁可漏挡，别把她的图误杀。
BLUE_MIN = 0.05
DARK_MIN = 0.25
WHITE_MIN = 0.02
SUFFIX_MIME = {ext: mime for mime, ext in MIME_EXT.items()}


def dhash_bytes(raw: bytes, size: int = 8) -> int | None:
    """差值哈希：64 位，抗缩放/压缩；解不开返回 None。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            px = list(im.getdata())
    except Exception:  # noqa: BLE001
        return None
    bits = 0
    idx = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits |= (1 if left > right else 0) << idx
            idx += 1
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _image_size(raw: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None


def color_profile(raw: bytes) -> tuple[float, float, float] | None:
    """(蓝色占比, 暗色占比, 白色占比)，在 64×64 缩略图上算。

    只做粗筛，不判「是不是她」：蓝=鲸鱼娘的头发/裙子；黑化版没有蓝，
    用「大面积暗色 + 白色头饰」兜底。返回 None 表示图打不开。
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im.thumbnail((64, 64))
            px = list(im.getdata())
    except Exception:  # noqa: BLE001
        return None
    total = len(px) or 1
    blue = dark = white = 0
    for r, g, b in px:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if 0.5 <= h <= 0.78 and s >= 0.25 and v >= 0.2:
            blue += 1
        if max(r, g, b) < 70:
            dark += 1
        if min(r, g, b) > 200:
            white += 1
    return blue / total, dark / total, white / total


def _color_ok(profile: tuple[float, float, float]) -> bool:
    blue, dark, white = profile
    return blue >= BLUE_MIN or (dark >= DARK_MIN and white >= WHITE_MIN)


def analysis_frame(raw: bytes, mime: str) -> tuple[bytes, str]:
    """动图取中间帧；静图原样返回。

    只给 dHash/颜色粗筛用（同一张图前后必须可比）。视觉判定/注解走
    `analysis_image`：动图会抽帧拼九宫格看整段动作。
    """
    if mime not in ("image/gif", "image/webp"):
        return raw, mime
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            if frames <= 1:
                return raw, mime
            for index in (frames // 2, 0):
                try:
                    im.seek(index)
                    break
                except Exception:  # noqa: BLE001
                    continue
            frame = im.convert("RGB")
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=88, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return raw, mime


def analysis_image(raw: bytes, mime: str) -> tuple[bytes, str, int]:
    """给视觉判定/注解用的图：动图抽帧拼九宫格，静图原样。

    返回 (图, mime, 帧数)。帧数 0 表示静图或没拼成。
    dHash/颜色粗筛仍走 `analysis_frame`（中间帧），保证同一张图前后可比。
    """
    if mime in ("image/gif", "image/webp"):
        got = animation_grid(raw, 8 * 1024 * 1024)
        if got is not None:
            data, frames = got
            return data, "image/jpeg", frames
    return raw, mime, 0


class StickerIngest:
    def __init__(
        self,
        book: Any,
        onebot: Any,
        *,
        client: Any = None,
        model: str = "",
        data_dir: Path,
        enabled: bool = True,
        daily: int = 3,
        min_minutes: float = 60.0,
        max_mb: float = 3.0,
        manual_max_mb: float | None = None,
        max_checks: int = 30,
        color_filter: bool = True,
        classifier: Callable | None = None,
        annotator: Callable | None = None,
        usage_recorder: Callable[[str, str, list[dict]], Any] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.book = book
        self.onebot = onebot
        self.client = client
        self.model = model
        self.data_dir = Path(data_dir)
        self.daily = max(0, int(daily))
        self.min_seconds = max(0.0, float(min_minutes) * 60.0)
        self.max_mb = max(0.5, float(max_mb))
        # 主人明确点名「偷这个」/「人工收图」时用更大的上限：QQ 动图表情
        # 3~8MB 很常见，按后台的 3MB 上限会一律「没取到图」（2026-10-30 实锤：
        # 主人点名收一张 3.8MB / 95 帧的 GIF，回执只有「没偷成：没取到图」）。
        # 后台自动捡图仍然按 max_mb 挡，省流量也省磁盘。
        self.manual_max_mb = max(
            self.max_mb,
            float(manual_max_mb if manual_max_mb is not None else 10.0),
        )
        self.max_checks = max(0, int(max_checks))
        self.color_filter = bool(color_filter)
        self._now = now_fn or time.time
        self._state_path = self.data_dir / "sticker_steal.json"
        self._dhash_path = self.data_dir / "sticker_dhash.json"
        self._classifier = classifier
        self._annotator = annotator
        self._usage_recorder = usage_recorder
        self._state = self._load_state()
        self._dhash: dict[str, int] = self._load_dhash()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_reason = ""
        # enabled = 主开关（能不能收）；auto_enabled = 后台自动捡的额度还开着。
        # 主人手动「偷这个」只看 enabled，不占额度。
        injected = classifier is not None and annotator is not None
        self.enabled = bool(enabled) and bool(getattr(book, "enabled", True))
        self.enabled = self.enabled and (client is not None or injected)
        self.auto_enabled = self.enabled and self.daily > 0 and self.max_checks > 0

    # ---------- 调度 ----------

    def schedule(
        self,
        segments: Any,
        group_id: str,
        sender: Any,
        sender_is_self: bool = False,
        pure_image: bool = True,
    ) -> None:
        """同步入口：把这一条里的图交给后台任务，不阻塞回复。"""
        if not self.auto_enabled or sender_is_self:
            return
        if self._task is not None and not self._task.done():
            return  # 上一张还在收，先丢着；表情包不值得排队
        if not isinstance(segments, list):
            return
        images = [
            s for s in segments
            if isinstance(s, dict) and s.get("type") == "image"
            and self._looks_like_sticker(s, pure_image)
        ]
        if not images:
            return
        self._task = asyncio.create_task(self._run(images, group_id, sender))

    async def consider(
        self,
        segments: list[dict],
        group_id: str,
        sender: Any,
        pure_image: bool = True,
        force: bool = False,
    ) -> dict | None:
        """同步测试/手动调用用；正常路径走 `schedule()` 的后台任务。

        `force=True` 是主人明确说「偷这个」：跳过每日/间隔/判定次数配额，
        也跳过颜色粗筛；但去重和「主角是不是她」仍然照跑。
        """
        self.last_reason = ""
        if not self.enabled:
            self.last_reason = "捡表情包没开"
            return None
        images = [
            s for s in segments
            if isinstance(s, dict) and s.get("type") == "image"
            and (force or self._looks_like_sticker(s, pure_image))
        ]
        if not images:
            self.last_reason = "没找到图片"
            return None
        async with self._lock:
            return await self._consider(images, group_id, sender, force=force)

    @staticmethod
    def _looks_like_sticker(seg: dict, pure_image: bool) -> bool:
        """先按消息元数据挡一层，别把群里每张照片都拿去花视觉判定的钱。"""
        data = seg.get("data") or {}
        summary = str(data.get("summary") or "")
        if "表情" in summary or "动画" in summary:
            return True
        if str(data.get("sub_type") or "") in ("2", "sticker"):
            return True
        if summary and summary not in ("[图片]", "[image]"):
            return False  # 截图/文件/视频封面之类
        if not pure_image:
            return False  # 图文一起的，多半是说事，不是甩表情包
        try:
            size = int(data.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        return size == 0 or size <= 2 * 1024 * 1024

    async def _run(self, images: list[dict], group_id: str, sender: Any) -> None:
        try:
            async with self._lock:
                got = await self._consider(images, group_id, sender)
                if got:
                    log.info(
                        "捡到表情包：%s #%s（%s）",
                        got.get("folder"),
                        got.get("sid"),
                        got.get("when", "")[:20],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("捡表情包失败（不影响聊天）")

    # ---------- 主流程 ----------

    async def _consider(
        self, images: list[dict], group_id: str, sender: Any, force: bool = False
    ) -> dict | None:
        now = self._now()
        self._roll_day(now)
        if not force:
            if int(self._state.get("count") or 0) >= self.daily:
                return self._deny("今天的额度用完了")
            last = float(self._state.get("last_ts") or 0.0)
            if last and now - last < self.min_seconds:
                return self._deny("离上一张太近，等一会儿")
            if int(self._state.get("checks") or 0) >= self.max_checks:
                return self._deny("今天的判定次数用完了")

        reasons: list[str] = []
        for seg in images[:2]:
            got = await fetch_image_bytes(
                self.onebot,
                seg,
                self.manual_max_mb if force else self.max_mb,
                reason=reasons,
            )
            if not got:
                continue
            raw, mime = got
            frame, frame_mime = analysis_frame(raw, mime)
            size = _image_size(frame)
            if size is None:
                return self._deny("图打不开")
            width, height = size
            if max(width, height) > MAX_SIDE or min(width, height) < MIN_SIDE:
                log.info("捡图跳过：尺寸不合适 %dx%d", width, height)
                return self._deny(f"尺寸不合适（{width}×{height}）")
            if max(width, height) / max(1, min(width, height)) > MAX_RATIO:
                log.info("捡图跳过：长截图/超宽图 %dx%d", width, height)
                return self._deny("长截图/超宽图不收")

            sha = hashlib.sha256(raw).hexdigest()
            if sha in self.book.by_sha:
                self._mark(sha, now, "图库已有")
                return self._deny("图库里已经有这张了")
            if sha in (self._state.get("seen") or {}):
                info = (self._state.get("seen") or {}).get(sha) or {}
                why = str(info.get("why") or "")
                if force and why.startswith("注解太虚"):
                    # 上次只是注解没写好，不是图不行；主人点名时清掉缓存重看。
                    # 「不是她」「近似重复」这些仍然拦着，不放开。
                    self._state["seen"].pop(sha, None)
                    self._save_state()
                    log.info("重新识别上次注解失败的图：%s", sha[:8])
                else:
                    # 把上次的判定理由一起回去：只写「看过/拒过了」，
                    # 主人根本不知道是嫌哪一条（2026-10-30 反馈）。
                    return self._deny(
                        "这张之前已经看过/拒过了"
                        + (f"（上次判定：{why}）" if why else "")
                    )

            # 本地粗筛：颜色不像她的直接跳过，省一次视觉判定；
            # 主人明确点名「偷这个」时不筛，交给主角判定。
            if self.color_filter and not force:
                profile = color_profile(frame)
                if profile is None:
                    return self._deny("图打不开")
                if not _color_ok(profile):
                    log.info(
                        "捡图跳过：颜色不像她（蓝 %.1f%% / 暗 %.1f%% / 白 %.1f%%）",
                        profile[0] * 100,
                        profile[1] * 100,
                        profile[2] * 100,
                    )
                    return self._deny("颜色不像她（蓝/黑白比例太低）")

            dh = dhash_bytes(frame)
            if dh is None:
                return self._deny("图打不开")
            near = await self._near_duplicate(dh)
            if near:
                self._mark(sha, now, f"跟 {near} 太像", dh)
                log.info("捡图跳过：和 %s 近似重复", near)
                return self._deny(f"和已有的 {near[:8]} 太像")

            if not force:
                self._state["checks"] = int(self._state.get("checks") or 0) + 1
                self._save_state()

            vision_frame, vision_mime, vision_frames = analysis_image(raw, mime)
            is_her, why = await self._classify(vision_frame, vision_mime)
            if not is_her:
                self._mark(sha, now, f"不是她：{why}", dh)
                self._save_state()
                log.info("捡图跳过：主角不是她（%s）", why[:30])
                return self._deny(f"主角不是她：{why}")

            folders = [name for name in self.book.moods if not name.startswith("_")]
            anno = await self._annotate(
                vision_frame, vision_mime, folders, frames=vision_frames
            )
            if not anno:
                return self._deny("注解失败")
            cleaned = clean_annotation(anno, FOLDER_FALLBACK)
            if len(cleaned.get("when") or "") < 4 and not force:
                self._mark(sha, now, "注解太虚：没有 when", dh)
                self._save_state()
                return self._deny("注解太虚，没有 when")

            folder = self._choose_folder(str(anno.get("folder") or ""), cleaned["emotion"])
            ext = MIME_EXT.get(mime, ".webp")
            sid = self._unique_sid("st_" + sha[:8])
            name = f"{sid}{ext}"
            self.book.ensure_folder(folder, FOLDER_DESC, "emotion")
            dest = self.book.folder / folder / name
            try:
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                tmp.write_bytes(raw)
                tmp.replace(dest)
            except OSError as exc:
                log.warning("捡图保存失败: %s", exc)
                return self._deny("写文件失败")

            entry = {
                **cleaned,
                "sid": sid,
                "id": sid,
                "folder": folder,
                "file": f"{folder}/{name}",
                "sha": sha,
                "origin": "stolen",
                "stolen_from": {
                    "group": str(group_id),
                    "sender": str(sender),
                    "ts": int(now),
                },
            }
            item = await self.book.ingest_item(entry)
            if item is None:
                try:
                    dest.unlink()
                except OSError:
                    pass
                return self._deny("写索引失败")

            self._dhash[sha] = dh
            self._save_dhash()
            if not force:
                self._state["count"] = int(self._state.get("count") or 0) + 1
                self._state["last_ts"] = now
            self._mark(sha, now, "已收", dh)
            self._save_state()
            return entry

        if reasons:
            # 去重后按顺序拼；太长就只留最后一条（最常见的是「超过上限」）
            unique = list(dict.fromkeys(reasons))
            return self._deny("没取到图：" + "；".join(unique)[:60])
        return self._deny("没取到图")

    def _deny(self, reason: str) -> None:
        self.last_reason = reason
        return None

    # ---------- 人工收图（主人明确标注后放行） ----------

    @staticmethod
    def _first_image(segments: list[dict]) -> dict | None:
        return next(
            (
                s for s in segments
                if isinstance(s, dict) and s.get("type") == "image"
            ),
            None,
        )

    async def manual_draft(self, segments: list[dict]) -> dict:
        """人工收图第一步：只看图给一份草稿，不做主角/额度/拒过拦截。"""
        seg = self._first_image(segments)
        if seg is None:
            return {}
        got = await fetch_image_bytes(self.onebot, seg, self.manual_max_mb)
        if not got:
            return {}
        raw, mime = got
        frame, frame_mime, frames = analysis_image(raw, mime)
        folders = [name for name in self.book.moods if not name.startswith("_")]
        anno = await self._annotate(frame, frame_mime, folders, frames=frames)
        return clean_annotation(anno or {}, "无语")

    async def manual_add(
        self,
        segments: list[dict],
        fields: dict,
        group_id: str,
        sender: Any,
    ) -> tuple[dict | None, str]:
        """人工标注后直接入库：绕过主角/额度/拒过缓存/近似去重。

        精确同 sha 仍然拒绝（库里已经有一张，避免重复文件）。
        """
        if not getattr(self.book, "enabled", False):
            return None, "表情包功能关着（ENABLE_STICKER=false）"
        seg = self._first_image(segments)
        if seg is None:
            return None, "没找到图片"
        reasons: list[str] = []
        got = await fetch_image_bytes(
            self.onebot, seg, self.manual_max_mb, reason=reasons
        )
        if not got:
            detail = f"：{reasons[0]}" if reasons else "（太大或链接失效）"
            return None, f"图取不到{detail}"
        raw, mime = got
        sha = hashlib.sha256(raw).hexdigest()
        existing = self.book.by_sha.get(sha)
        if existing is not None:
            return None, f"库里已经有这张了（#{existing.sid}）"

        folder = self._choose_folder(
            str(fields.get("folder") or ""), str(fields.get("emotion") or "")
        )
        ext = MIME_EXT.get(mime, ".webp")
        sid = self._unique_sid("st_" + sha[:8])
        name = f"{sid}{ext}"
        self.book.ensure_folder(folder, FOLDER_DESC, "emotion")
        dest = self.book.folder / folder / name
        try:
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(dest)
        except OSError as exc:
            return None, f"保存失败：{exc}"

        entry = {
            **fields,
            "sid": sid,
            "id": sid,
            "folder": folder,
            "file": f"{folder}/{name}",
            "sha": sha,
            "source": "manual",
            "origin": "manual",
            "locked": True,
            "manual_at": int(self._now()),
            "manual_by": str(sender),
        }
        item = await self.book.ingest_item(entry)
        if item is None:
            try:
                dest.unlink()
            except OSError:
                pass
            return None, "写索引失败"
        frame, _ = analysis_frame(raw, mime)
        dh = dhash_bytes(frame)
        if dh is not None:
            self._dhash[sha] = dh
            self._save_dhash()
        return entry, ""

    # ---------- 判定 / 注解（可注入测试替身） ----------

    def _record_vision(
        self, sink: list[dict], session: str, task: str
    ) -> None:
        if not sink or self._usage_recorder is None:
            return
        try:
            self._usage_recorder(session, task, sink)
        except Exception:  # noqa: BLE001 - 记账失败不能影响捡图
            log.debug("捡图视觉调用记账失败", exc_info=True)

    async def _classify(self, raw: bytes, mime: str) -> tuple[bool, str]:
        if self._classifier is not None:
            got = self._classifier(raw, mime)
            return await got if asyncio.iscoroutine(got) else got
        sink: list[dict] = []
        got = await classify_her(
            self.client, self.model, raw, mime, usage_sink=sink
        )
        self._record_vision(sink, "tools:sticker_ingest", "sticker_screen")
        return got

    async def _annotate(
        self, raw: bytes, mime: str, folders: list[str], *, frames: int = 0
    ) -> dict | None:
        if self._annotator is not None:
            got = self._annotator(raw, mime, folders)
            return await got if asyncio.iscoroutine(got) else got
        sink: list[dict] = []
        got = await annotate_image(
            self.client,
            self.model,
            raw,
            mime,
            folders,
            extra=grid_note(frames) if frames else "",
            usage_sink=sink,
        )
        self._record_vision(sink, "tools:sticker_ingest", "sticker_annotate")
        return got

    # ---------- 去重 ----------

    async def _near_duplicate(self, dh: int) -> str | None:
        await asyncio.to_thread(self._ensure_library_dhash)
        for sha, other in self._dhash.items():
            if _hamming(dh, other) <= NEAR_DHASH:
                return sha
        for sha, info in (self._state.get("seen") or {}).items():
            other = int(info.get("dhash") or 0)
            if other and _hamming(dh, other) <= NEAR_DHASH:
                return f"拒过的 {sha[:8]}"
        return None

    def _ensure_library_dhash(self) -> None:
        changed = False
        for item in self.book.items:
            if item.sha in self._dhash:
                continue
            try:
                raw = item.path.read_bytes()
                frame, _ = analysis_frame(
                    raw, SUFFIX_MIME.get(item.path.suffix.lower(), "")
                )
                dh = dhash_bytes(frame)
            except OSError:
                continue
            if dh is not None:
                self._dhash[item.sha] = dh
                changed = True
        if changed:
            self._save_dhash()

    def refresh_dhash(self, item: Any, old_sha: str = "") -> None:
        """换图后更新这一张的 dHash 缓存（旧 sha 删掉，新 sha 补上）。"""
        if old_sha:
            self._dhash.pop(old_sha, None)
        try:
            raw = item.path.read_bytes()
            frame, _ = analysis_frame(raw, guess_mime(raw) or "")
            dh = dhash_bytes(frame)
        except OSError:
            dh = None
        if dh is not None:
            self._dhash[item.sha] = dh
        self._save_dhash()

    # ---------- 分类 / 命名 ----------

    def _choose_folder(self, choice: str, emotion: str) -> str:
        valid = [name for name in self.book.moods if not name.startswith("_")]
        if choice and choice in valid:
            return choice
        if emotion:
            if emotion in valid:
                return emotion
            for name in valid:
                if name in emotion or emotion in name:
                    return name
        return FOLDER_FALLBACK

    def _unique_sid(self, base: str) -> str:
        sid = base
        i = 2
        while sid in self.book.by_sid:
            sid = f"{base}-{i}"
            i += 1
        return sid

    # ---------- 状态文件 ----------

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
        except OSError as exc:
            log.debug("表情包捡图状态写失败: %s", exc)

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self._state.get("day") != day:
            self._state["day"] = day
            self._state["count"] = 0
            self._state["checks"] = 0
        seen = self._state.get("seen")
        if isinstance(seen, dict):
            cutoff = now - SEEN_KEEP_DAYS * 86400
            for sha, info in list(seen.items()):
                try:
                    if float(info.get("ts") or 0) < cutoff:
                        seen.pop(sha, None)
                except (TypeError, ValueError):
                    seen.pop(sha, None)

    def _mark(self, sha: str, now: float, why: str, dh: int = 0) -> None:
        seen = self._state.setdefault("seen", {})
        seen[sha] = {"ts": int(now), "why": str(why)[:40], "dhash": int(dh or 0)}
        self._save_state()

    def _load_dhash(self) -> dict[str, int]:
        try:
            data = json.loads(self._dhash_path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_dhash(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._dhash_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._dhash, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._dhash_path)
        except OSError as exc:
            log.debug("表情包 dHash 缓存写失败: %s", exc)
