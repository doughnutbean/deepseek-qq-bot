"""表情包：按情绪从本地目录里随机挑一张发出去。

目录结构：
    stickers/
        开心/01.png
        委屈/01.png
        ...

想换成自己的图，直接把图片丢进对应目录就行（png/jpg/gif 都支持），
文件名随意，程序启动时会自动扫描。

逐图语义（`stickers/index.json`，由 `tools/annotate_stickers.py` 生成）：
目录名只说明"大概哪一类"，真正决定该不该贴的是图里的字和适用情境。
有索引时，每轮会按对话内容检索出几张候选交给模型（写 `[表情:#编号]`），
索引缺失/向量没就绪时，退回按目录名随机挑。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# 模型输出里用来标记表情的写法：[表情:开心] / [表情：开心]
# 也兼容模型偶尔写歪的 [s表情] / [表情]（没有情绪，只把标记抹掉，不发图）。
# 注意：必须全文匹配，不能只认句尾 —— 模型经常把标记插在句子中间，
# 只认句尾的话 [表情:xx] 会原样漏进聊天里。
# 标签上限 32 个字符：捡来的图编号是 `st_` + 8 位 sha（11 个字），撞号还会
# 变成 `st_xxx-2`，老图有 `gj-meme-1594`（12 个字）。这里原来只给到 8 个字，
# 于是 [表情:#st_f10fd3f0] 整个认不出来 —— 标记原样漏进聊天，图也没发出去。
MARKER_RE = re.compile(
    r"[\[【［]\s*(?:[sS]\s*)?表情(?:\s*[:：]\s*(?P<tag>[^\[\]【】［］\s]{1,32}))?\s*[\]】］]"
)
# 认不出来的「表情:…」开头标记（标签超长/写歪）。它没法解码成图，但绝不能
# 悄悄漏进聊天 —— 至少留一条日志，下次出事能查到。
STRAY_MARKER_RE = re.compile(r"[\[【［]\s*(?:[sS]\s*)?表情\s*[:：]")

SUPPORTED = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 给模型看的情绪说明。
# 只保留「纯情绪」类别：表情卡表达的是**当下心情**，不携带任何具体事件或判断。
# 想加新情绪就新建同名目录，这里没有对应说明时只显示名字。
MOOD_HINTS: dict[str, str] = {
    "开心": "高兴、被夸、遇到好事",
    "得意": "炫耀、自满、觉得自己很行",
    "害羞": "被夸、被道谢、被调戏、不好意思",
    "撒娇": "黏人、想被哄、贴贴抱抱",
    "委屈": "受委屈、想哭、被欺负",
    "生气": "愠怒、鼓脸、不满",
    "无语": "被离谱到、懒得说话",
    "困惑": "没听懂、满脑子问号",
    "疲惫": "累、困、想躺平摸鱼",
    "慌张": "心虚、被抓包、手忙脚乱",
}

# 向量检索的余弦下限。低于它的候选不进菜单 —— 宁可让模型按标签挑，
# 也别把一堆不搭的图摆在她面前。bge-small 上 0.15 已经很宽松。
MIN_SCORE = 0.15
# 同一个情绪最多给几张候选，免得菜单里六张全是"无语"
PER_EMOTION = 2
# 最近贴过的几张不再进候选，跨标签避重
RECENT_KEEP = 8
# 向量文本口径的版本：改了 Sticker.doc 就要 +1，让旧向量缓存作废
VECTOR_VERSION = 2
# 认「这张是不是本鱼自己的图」时，dHash 汉明距离的上限。
# 和 `stickeringest.NEAR_DHASH` 一样：64 位差 4 位以内算同一张，
# 腾讯把 webp 转成 jpeg 再发回来，通常只差 0~3 位。
NEAR_DHASH = 4


@dataclass
class Sticker:
    """一张表情包 + 它的语义注解（来自 stickers/index.json）。"""

    sid: str
    id: str
    folder: str
    file: str
    sha: str
    path: Path
    text: str = ""
    visual: str = ""
    joke: str = ""
    emotion: str = ""
    intensity: int = 2
    target: str = "无"
    when: str = ""
    never: str = ""
    tags: tuple[str, ...] = ()
    source: str = ""
    locked: bool = False
    # 来源：index.json 里捡来的图带 "origin": "stolen"；主人自己的图没有这个字段
    origin: str = ""
    # 目录标签的说明（tags.json）。像「自画像」这种，图本身看不出用途，
    # 只有目录说明写着「被问长什么样、要自拍时」—— 这句必须进检索文本。
    tag_desc: str = ""

    @property
    def doc(self) -> str:
        """喂给 embedding 的文本：分类、情绪、梗、情境、图里的字、画面。

        目录名和目录说明（主人手工分的类，比如「自画像」「涨价」）也放进去：
        有些图光看画面看不出用途，目录说明才是它真正的触发场景。
        """
        raw = [
            self.folder,
            self.tag_desc,
            self.emotion,
            *self.tags,
            self.joke,
            self.when,
            self.text,
            self.visual,
        ]
        bits = list(dict.fromkeys(b.strip() for b in raw if b and b.strip()))
        return " ".join(bits)[:300]


class StickerBook:
    def __init__(
        self,
        folder: Path,
        enabled: bool = True,
        vectors_path: Path | None = None,
        menu_enabled: bool = True,
        candidates: int = 6,
    ) -> None:
        self.folder = folder
        self.enabled = enabled
        self.moods: dict[str, list[Path]] = {}
        # 标签类型：emotion（纯情绪，随时可用）/ scene（场景，只在对应情况用）
        self.tags: dict[str, dict] = {}
        # 记录每种情绪上次发的是哪张，避免连着发同一张
        self._last: dict[str, Path] = {}
        # 主人可以调某张图/某个标签的权重（0 = 不发）；由 Bot 注入
        self.weight_fn: Any = None
        # 剥标记用的正则：标准 [表情:xx] + 裸标签 [害羞]，按当前标签表缓存
        self._marker_cache: tuple[tuple[str, ...], re.Pattern[str]] | None = None
        # ---- 逐图语义索引 ----
        self.items: list[Sticker] = []
        self.by_sid: dict[str, Sticker] = {}
        self.by_sha: dict[str, Sticker] = {}
        self.by_path: dict[Path, Sticker] = {}
        # 向量：sha -> float32 向量；menu 用矩阵一次算完
        self._vec: dict[str, Any] = {}
        self._mat: Any = None
        self._mat_sids: list[str] = []
        self._index_hash = ""
        self._vectors_path = vectors_path
        self._embedder: Any = None
        self.menu_enabled = menu_enabled
        self.candidates = max(0, int(candidates))
        self._recent: deque[str] = deque(maxlen=RECENT_KEEP)
        # 认图用的 dHash：sha -> 64 位整数。懒算，run.py 启动时预热。
        # 预热在线程里跑，消息可能同时到，加把锁别再改字典时遍历。
        self._dhash: dict[str, int] = {}
        self._dhash_ready = False
        self._dhash_lock = threading.Lock()
        if enabled:
            self._scan()
            self._load_tags()
            self._load_index()


    def _scan(self) -> None:
        if not self.folder.is_dir():
            log.info("没有表情包目录：%s", self.folder)
            return
        for sub in sorted(self.folder.iterdir()):
            if not sub.is_dir():
                continue
            # 下划线开头的是暂存/审核目录，"我挑出来的"是待归类区，都不是正式标签
            if sub.name.startswith("_") or sub.name == "我挑出来的":
                continue
            files = [p for p in sorted(sub.iterdir()) if p.suffix.lower() in SUPPORTED]
            # 同名不同格式（x.jpg 和 x.webp）只留一个，优先 webp：体积小、兼容好
            best: dict[str, object] = {}
            for f in files:
                prev = best.get(f.stem)
                if prev is None or (f.suffix.lower() == ".webp" and prev.suffix.lower() != ".webp"):
                    best[f.stem] = f
            files = sorted(best.values())
            if files:
                self.moods[sub.name] = files
        if self.moods:
            total = sum(len(v) for v in self.moods.values())
            log.info("表情包已加载：%d 个标签，共 %d 张", len(self.moods), total)

    @property
    def available(self) -> bool:
        return bool(self.moods)

    def mood_names(self) -> list[str]:
        return list(self.moods.keys())

    def _load_tags(self) -> None:
        path = self.folder / "tags.json"
        if path.exists():
            try:
                self.tags = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("读不到 tags.json: %s", exc)
        # 没写进 tags.json 的目录，按纯情绪处理
        for name in self.moods:
            self.tags.setdefault(name, {"desc": MOOD_HINTS.get(name, ""), "kind": "emotion"})

    # ---------- 逐图索引 / 向量检索 ----------

    def _load_index(self) -> None:
        """读 stickers/index.json（tools/annotate_stickers.py 生成）。"""
        path = self.folder / "index.json"
        if not path.exists():
            log.info("没有逐图索引，表情包按标签随机挑")
            return
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("读不到表情包索引 %s: %s", path.name, exc)
            return
        self._index_hash = hashlib.sha256(raw).hexdigest()
        missing = 0
        for entry in data.get("items") or []:
            if not isinstance(entry, dict):
                continue
            item = self._make_item(entry)
            if item is None:
                missing += 1
                continue
            self._remember_item(item)
        if missing:
            log.info("表情包索引里有 %d 条读不出来/图不在了（重跑 tools/annotate_stickers.py）", missing)
        if self.items:
            log.info("表情包逐图索引：%d 张，%d 个编号", len(self.items), len(self.by_sid))

    def _make_item(self, entry: dict) -> Sticker | None:
        rel = str(entry.get("file") or "")
        try:
            file_path = (self.folder / rel).resolve()
        except (OSError, ValueError):
            return None
        if self.folder.resolve() not in file_path.parents or not file_path.is_file():
            return None
        try:
            item = Sticker(
                sid=str(entry.get("sid") or entry.get("id") or file_path.stem),
                id=str(entry.get("id") or file_path.stem),
                folder=str(entry.get("folder") or file_path.parent.name),
                file=rel,
                sha=str(entry.get("sha") or ""),
                path=file_path,
                text=str(entry.get("text") or ""),
                visual=str(entry.get("visual") or ""),
                joke=str(entry.get("joke") or ""),
                emotion=str(entry.get("emotion") or ""),
                intensity=int(entry.get("intensity") or 2),
                target=str(entry.get("target") or "无"),
                when=str(entry.get("when") or ""),
                never=str(entry.get("never") or ""),
                tags=tuple(str(t) for t in (entry.get("tags") or [])),
                source=str(entry.get("source") or ""),
                locked=bool(entry.get("locked")),
                origin=str(entry.get("origin") or ""),
                tag_desc=self.desc_of(str(entry.get("folder") or file_path.parent.name)),
            )
        except (TypeError, ValueError):
            return None
        return item if item.sha else None

    def _remember_item(self, item: Sticker) -> None:
        self.items.append(item)
        self.by_sid.setdefault(item.sid, item)
        self.by_path.setdefault(item.path, item)
        self.by_sha.setdefault(item.sha, item)

    def _index_data(self) -> dict:
        path = self.folder / "index.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        return {"version": 2, "items": []}

    def _write_index(self, data: dict) -> bool:
        path = self.folder / "index.json"
        data["version"] = 2
        data["built"] = datetime.now().isoformat(timespec="seconds")
        try:
            raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
            tmp = path.with_suffix(".json.tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
        except OSError as exc:
            log.warning("写表情包索引失败: %s", exc)
            return False
        self._index_hash = hashlib.sha256(raw).hexdigest()
        return True

    def ensure_folder(self, name: str, desc: str = "", kind: str = "emotion") -> None:
        """新增分类目录 + tags.json 条目（捡图用）。已存在就只补空描述。"""
        if not name:
            return
        path = self.folder / name
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("建表情包目录失败 %s: %s", name, exc)
            return
        self.moods.setdefault(name, [])
        current = self.tags.get(name)
        if current is None:
            self.tags[name] = {"desc": desc, "kind": kind}
            self._write_tags()
        elif desc and not current.get("desc"):
            current["desc"] = desc
            self._write_tags()

    def _write_tags(self) -> None:
        path = self.folder / "tags.json"
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.tags, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(path)
        except OSError as exc:
            log.warning("写表情包标签失败: %s", exc)

    async def ingest_item(self, entry: dict) -> Sticker | None:
        """把一条新注解并进 index.json + 内存索引 + 向量（捡图用）。

        调用方负责先把图片落盘、把目录/tags 建好；这里只接受已经能读到的图。
        """
        if not self.enabled:
            return None
        item = self._make_item(entry)
        if item is None:
            log.warning("入库失败：图读不到 %s", entry.get("file"))
            return None
        if item.sha in self.by_sha:
            return self.by_sha[item.sha]
        data = self._index_data()
        items = [
            it for it in (data.get("items") or [])
            if not isinstance(it, dict) or str(it.get("sha")) != item.sha
        ]
        items.append(entry)
        data["items"] = items
        if not self._write_index(data):
            return None
        self._remember_item(item)
        bucket = self.moods.setdefault(item.folder, [])
        if item.path not in bucket:
            bucket.append(item.path)
        if self._embedder is not None and getattr(self._embedder, "ready", False):
            blobs = await self._embedder.encode_async([item.doc])
            if blobs:
                import numpy as np

                self._vec[item.sha] = np.frombuffer(blobs[0], dtype=np.float32).copy()
                self._build_matrix()
                self._save_vectors()
        return item

    def replace_image(
        self, item: Sticker, raw: bytes, ext: str, by: Any
    ) -> tuple[dict | None, str]:
        """把一张已入库的图换成新字节（去水印用）。

        保留原注解/目录/编号，只更新文件、sha 和替换记录；旧文件备份到
        `stickers/_originals/<目录>/`。返回 (新 index 条目, 错误)。
        """
        if not self.enabled:
            return None, "表情包功能关着"
        old_path = item.path
        if not old_path.is_file():
            return None, "旧图文件不在了"
        ext = ext if ext.startswith(".") else f".{ext}" if ext else old_path.suffix
        new_sha = hashlib.sha256(raw).hexdigest()
        old_sha = item.sha
        if new_sha == old_sha:
            return None, "新图和旧图字节一样，没换"
        dest = old_path.with_suffix(ext)
        if dest != old_path and dest.exists():
            stamp = datetime.now().strftime("%H%M%S")
            dest = old_path.with_name(f"{old_path.stem}_{stamp}{ext}")

        backup_dir = self.folder / "_originals" / item.folder
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{item.sid}{old_path.suffix}"
            if backup.exists():
                backup = backup_dir / (
                    f"{item.sid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                    f"{old_path.suffix}"
                )
            backup.write_bytes(old_path.read_bytes())
        except OSError as exc:
            return None, f"备份旧图失败：{exc}"

        try:
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(dest)
            if dest != old_path:
                old_path.unlink(missing_ok=True)
        except OSError as exc:
            return None, f"保存新图失败：{exc}"

        rel = f"{item.folder}/{dest.name}"
        data = self._index_data()
        items = data.get("items") or []
        entry = next(
            (
                it for it in items
                if isinstance(it, dict) and str(it.get("sid") or it.get("id")) == item.sid
            ),
            None,
        )
        if entry is None:
            entry = {
                "sid": item.sid,
                "id": item.id,
                "folder": item.folder,
                "file": item.file,
                "sha": item.sha,
                "text": item.text,
                "visual": item.visual,
                "joke": item.joke,
                "emotion": item.emotion,
                "intensity": item.intensity,
                "target": item.target,
                "when": item.when,
                "never": item.never,
                "tags": list(item.tags),
                "source": item.source,
                "locked": item.locked,
            }
            items.append(entry)
        entry.update(
            {
                "file": rel,
                "sha": new_sha,
                "original_sha": old_sha,
                "watermark_replaced": True,
                "replaced_at": datetime.now().isoformat(timespec="seconds"),
                "replaced_by": str(by),
            }
        )
        data["items"] = items
        if not self._write_index(data):
            return None, "写索引失败"

        self.by_sha.pop(old_sha, None)
        self.by_sha[new_sha] = item
        self.by_path.pop(old_path, None)
        self.by_path[dest] = item
        item.file = rel
        item.sha = new_sha
        item.path = dest
        bucket = self.moods.get(item.folder) or []
        self.moods[item.folder] = [dest if p == old_path else p for p in bucket]
        vec = self._vec.pop(old_sha, None)
        if vec is not None:
            self._vec[new_sha] = vec
            self._build_matrix()
            self._save_vectors()
        log.info(
            "表情包换图：%s %s -> %s（%s）",
            item.sid, old_path.name, dest.name, by,
        )
        return entry, ""

    def attach_embedder(self, embedder: Any) -> None:
        """复用 Bot 的本地 embedding（和语义回忆是同一个模型，别加载两份）。"""
        self._embedder = embedder

    def warm(self) -> int:
        """算好每张图的向量（启动时在后台线程里调，别卡消息）。"""
        if not self.enabled or not self.menu_enabled or not self.items:
            return 0
        if self._embedder is None or not getattr(self._embedder, "ready", False):
            return 0
        import numpy as np

        self._load_vectors()
        missing = [it for it in self.items if it.sha not in self._vec]
        if missing:
            blobs = self._embedder.encode([it.doc for it in missing])
            if len(blobs) != len(missing):
                log.warning("表情包向量算失败：%d/%d", len(blobs), len(missing))
                return 0
            for it, blob in zip(missing, blobs):
                self._vec[it.sha] = np.frombuffer(blob, dtype=np.float32).copy()
            self._save_vectors()
        self._build_matrix()
        log.info("表情包向量就绪：%d 张", len(self._mat_sids))
        return len(self._mat_sids)

    # ---------- 认图：这张是不是本鱼自己的表情包 ----------
    #
    # 2026-10-27 实锤：主人引用了一张她收来的表情包问「你从哪偷的这张图啊」，
    # 她回「这张不是本鱼发的呀」。原因是收到的图只走视觉描述，从没跟
    # `stickers/index.json` 对过 —— 她认不出自己库里的图，更不知道自己收过。
    # 这里只做本地比对（0 token）：先 sha256 精确，再 dHash 近似兜住转码。

    @staticmethod
    def _hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    def warm_dhash(self) -> int:
        """给图库每张图算 dHash（后台线程里调，别卡消息）。返回算好几张。"""
        if not self.enabled or not self.items:
            return 0
        from .stickeringest import dhash_bytes  # 延迟导入：两边共用一个实现

        computed: dict[str, int] = {}
        for item in self.items:
            if item.sha in self._dhash:
                continue
            try:
                raw = item.path.read_bytes()
            except OSError:
                continue
            dh = dhash_bytes(raw)
            if dh is not None:
                computed[item.sha] = int(dh)
        with self._dhash_lock:
            self._dhash.update(computed)
            self._dhash_ready = True
            return len(self._dhash)

    def item_of(self, path: Any) -> Sticker | None:
        """按文件路径找回索引条目（她刚发出去的是哪张）。"""
        if path is None:
            return None
        try:
            return self.by_path.get(Path(path).resolve())
        except OSError:
            return None

    def recognize(self, raw: bytes) -> tuple[Sticker, str] | None:
        """认一张收到的图是不是图库里的。返回 (条目, 认法) 或 None。

        - 先按 sha256 精确认（她自己发出去的原图）；
        - 再按 dHash 近似认（QQ 转码/压缩后 sha 变了，画面没变）；
        - 近似认必须**唯一**：第二像的只差 2 位以内就不猜，宁可当新图，
          也不能把 A 说成 B（认错图比认不出更糟）。
        """
        if not raw or not self.enabled:
            return None
        digest = hashlib.sha256(raw).hexdigest()
        item = self.by_sha.get(digest)
        if item is not None:
            return item, "sha"

        if not self.items:
            return None
        from .stickeringest import dhash_bytes

        dh = dhash_bytes(raw)
        if dh is None:
            return None
        with self._dhash_lock:
            ready = self._dhash_ready
            peers = list(self._dhash.items())
        if not ready:
            self.warm_dhash()
            with self._dhash_lock:
                peers = list(self._dhash.items())
        if not peers:
            return None
        best: tuple[int, str] | None = None
        second = 99
        for sha, other in peers:
            dist = self._hamming(int(dh), int(other))
            if best is None or dist < best[0]:
                if best is not None:
                    second = min(second, best[0])
                best = (dist, sha)
            else:
                second = min(second, dist)
        if best is None or best[0] > NEAR_DHASH:
            return None
        if second <= best[0] + 2:
            log.info(
                "收到的图跟图库里两张都像（%d / %d 位），不猜是哪张", best[0], second
            )
            return None
        item = self.by_sha.get(best[1])
        if item is None:
            return None
        return item, f"dhash {best[0]}"

    def _load_vectors(self) -> None:
        path = self._vectors_path
        if path is None or not path.exists() or not self._index_hash:
            return
        import numpy as np

        try:
            with np.load(path, allow_pickle=False) as data:
                built = str(data["built"][0]) if data["built"].size else ""
                if built != self._built_key:
                    return  # 索引/文本口径变过，旧向量作废
                for sha, vec in zip(data["sha"], data["vec"]):
                    self._vec[str(sha)] = vec.astype(np.float32)
        except (OSError, ValueError, KeyError) as exc:
            log.debug("表情包向量缓存读失败: %s", exc)

    @property
    def _built_key(self) -> str:
        return f"{self._index_hash}:{VECTOR_VERSION}"

    def _save_vectors(self) -> None:
        path = self._vectors_path
        if path is None or not self._index_hash:
            return
        import numpy as np

        shas = [it.sha for it in self.items if it.sha in self._vec]
        if not shas:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(
                path,
                built=np.array([self._built_key]),
                sha=np.array(shas),
                vec=np.stack([self._vec[s] for s in shas]),
            )
        except OSError as exc:
            log.debug("表情包向量缓存写失败: %s", exc)

    def _build_matrix(self) -> None:
        """按编号建矩阵：同一张图放在两个目录时，两个编号都能被点到。"""
        import numpy as np

        sids: list[str] = []
        rows: list[Any] = []
        for it in self.items:
            if it.sha in self._vec:
                sids.append(it.sid)
                rows.append(self._vec[it.sha])
        if not rows:
            self._mat = None
            self._mat_sids = []
            return
        self._mat = np.stack(rows)
        self._mat_sids = sids

    async def menu(self, query: str, k: int | None = None) -> str:
        """按本轮对话检索几张候选，拼成给模型看的小菜单。不可用时返回空串。"""
        if (
            not self.enabled
            or not self.menu_enabled
            or self._embedder is None
            or self._mat is None
            or not self._mat_sids
            or self.candidates <= 0
        ):
            return ""
        q = re.sub(r"\s+", " ", (query or "")).strip()
        if len(q) < 2:
            return ""
        try:
            blobs = await self._embedder.encode_async([q[:400]])
        except Exception:  # noqa: BLE001
            log.debug("表情包检索编码失败", exc_info=True)
            return ""
        if not blobs:
            return ""
        import numpy as np

        vec = np.frombuffer(blobs[0], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm <= 0:
            return ""
        scores = self._mat @ (vec / norm)
        picked = self._pick_candidates(scores, self.candidates if k is None else k)
        if not picked:
            return ""
        return self._menu_text(picked)

    async def search(self, query: str, k: int = 5) -> list[Sticker]:
        """按图里的字/画面/梗/情境找图（工具和命令用，不走菜单那套避重）。"""
        q = re.sub(r"\s+", " ", (query or "")).strip()
        if len(q) < 2 or not self.items:
            return []
        k = max(1, min(8, int(k)))
        low = q.casefold()
        exact = [it for it in self.items if low in it.doc.casefold()]
        ranked: list[tuple[float, Sticker]] = []
        if (
            self._embedder is not None
            and getattr(self._embedder, "ready", False)
            and self._mat is not None
            and self._mat_sids
        ):
            try:
                blobs = await self._embedder.encode_async([q[:200]])
            except Exception:  # noqa: BLE001
                blobs = []
            if blobs:
                import numpy as np

                vec = np.frombuffer(blobs[0], dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    scores = self._mat @ (vec / norm)
                    for idx in scores.argsort()[::-1]:
                        score = float(scores[int(idx)])
                        if score < MIN_SCORE:
                            break
                        item = self.by_sid.get(self._mat_sids[int(idx)])
                        if item is not None:
                            ranked.append((score, item))

        out: list[Sticker] = []
        seen: set[str] = set()
        for item in exact:
            if item.sid not in seen:
                out.append(item)
                seen.add(item.sid)
        for _score, item in ranked:
            if len(out) >= k:
                break
            if item.sid not in seen:
                out.append(item)
                seen.add(item.sid)
        if not out:
            # 向量没就绪/查询太短：退回逐图文本包含
            for item in self.items:
                doc = item.doc.casefold()
                if any(part and part in doc for part in re.split(r"[\s,，、/]+", low)):
                    out.append(item)
                    if len(out) >= k:
                        break
        return out[:k]

    def _pick_candidates(self, scores: Any, k: int) -> list[tuple[Sticker, float]]:
        if k <= 0 or self._mat is None:
            return []
        out: list[tuple[Sticker, float]] = []
        per_emotion: dict[str, int] = {}
        seen_sha: set[str] = set()
        for idx in scores.argsort()[::-1]:
            score = float(scores[int(idx)])
            if score < MIN_SCORE:
                break  # 已排序，后面的只会更低
            sid = self._mat_sids[int(idx)]
            item = self.by_sid.get(sid)
            if item is None or item.sha in seen_sha:
                continue
            if item.sid in self._recent:
                continue
            if not self._weight_ok(item):
                continue
            emo = item.emotion or item.folder
            if per_emotion.get(emo, 0) >= PER_EMOTION:
                continue
            per_emotion[emo] = per_emotion.get(emo, 0) + 1
            seen_sha.add(item.sha)
            out.append((item, score))
            if len(out) >= k:
                break
        return out

    def _weight_ok(self, item: Sticker) -> bool:
        if self.weight_fn is None:
            return True
        try:
            return float(self.weight_fn(item.emotion or item.folder, item.path.name)) > 0
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _clip(text: str, n: int) -> str:
        body = re.sub(r"\s+", " ", (text or "")).strip()
        return body if len(body) <= n else body[: n - 1] + "…"

    def _menu_text(self, picked: list[tuple[Sticker, float]]) -> str:
        lines = [
            "（这轮可贴的表情包；图库分类只是这张图归在哪类，不是你现在的心情。"
            "先按对话把正文想好，再挑一张不跟正文打架的图——别让图反过来决定语气。"
            "想贴就写 [表情:#编号]，最多一张，都不合适就别贴）"
        ]
        for item, _score in picked:
            label = item.folder
            if item.emotion and item.emotion != item.folder:
                label = f"{item.folder}/{item.emotion}"
            bits = [f"#{item.sid} [{label}]"]
            # 场景标签（自画像/涨价/掉线…）用主人写的目录说明当触发条件，
            # 比逐图注解里的 when 更准；情绪标签才用逐图的 when。
            if self.kind_of(item.folder) == "scene" and item.tag_desc:
                desc = item.tag_desc
            else:
                desc = item.when or item.joke or item.visual
            if desc:
                bits.append(self._clip(desc, 32))
            if item.text:
                bits.append("图字：" + self._clip(item.text, 22))
            if item.never:
                bits.append("忌：" + self._clip(item.never, 20))
            lines.append("｜".join(bits))
        return "\n".join(lines)

    def _remember(self, sid: str) -> None:
        if not sid:
            return
        try:
            self._recent.remove(sid)
        except ValueError:
            pass
        self._recent.append(sid)

    def kind_of(self, name: str) -> str:
        return self.tags.get(name, {}).get("kind", "emotion")

    def desc_of(self, name: str) -> str:
        return self.tags.get(name, {}).get("desc", "") or MOOD_HINTS.get(name, "")

    def _describe(self, kinds: set[str]) -> list[str]:
        out = []
        for name in self.moods:
            if self.kind_of(name) not in kinds:
                continue
            desc = self.desc_of(name)
            out.append(f"{name}（{desc}）" if desc else name)
        return out

    def describe_moods(self) -> str:
        """纯情绪标签：任何时候都能用。"""
        return "、".join(self._describe({"emotion"}))

    def describe_scenes(self) -> str:
        """场景标签：只在对话内容对上时才用。"""
        return "、".join(self._describe({"scene"}))

    def tag_of(self, mood: str) -> str | None:
        """把 `[表情:标签/编号]` 归到目录标签（权重/频率按标签算）。"""
        raw = (mood or "").strip()
        if not raw:
            return None
        key = raw.lstrip("#＃").strip()
        item = self.by_sid.get(key)
        if item is not None:
            return item.folder
        if raw in self.moods:
            return raw
        # 和 pick 一样的模糊匹配：「有点委屈」→ 委屈
        for candidate in self.moods:
            if candidate in raw or raw in candidate:
                return candidate
        return None

    def pick(self, mood: str) -> Path | None:
        """`[表情:#编号]` 精确取那张；否则按标签（旧行为）随机挑。"""
        raw = (mood or "").strip()
        if not raw:
            return None
        # 编号：菜单里给的是 #014 / #ds_001，模型可能把 # 漏掉
        key = raw.lstrip("#＃").strip()
        item = self.by_sid.get(key) if key else None
        if item is not None:
            if item.sid in self._recent:
                log.info("表情包 %s 最近刚发过，跳过", item.sid)
                return None
            if not self._weight_ok(item):
                log.info("表情包 %s 权重为 0，跳过", item.sid)
                return None
            self._remember(item.sid)
            return item.path
        if raw[:1] in ("#", "＃"):
            if key in self.moods:
                # 模型偶尔把标签写成 #无语；这是标签，不是编号
                raw = key
            else:
                # 明确写了编号却对不上：别拿别的图凑数，这一条就不贴
                log.info("表情包编号 %r 不在索引里，跳过", raw)
                return None

        name = raw
        files = self.moods.get(name)
        if not files:
            # 模糊匹配一下，免得模型写「有点委屈」这种
            for candidate, group in self.moods.items():
                if candidate in name or name in candidate:
                    name, files = candidate, group
                    break
        if not files:
            return None

        # 权重：主人可以说「多发这个」「少发委屈的」，0 就是彻底不发
        scored: list[tuple[Path, float]] = []
        for path in files:
            weight = 1.0
            if self.weight_fn is not None:
                try:
                    weight = float(self.weight_fn(name, path.name))
                except Exception:  # noqa: BLE001
                    weight = 1.0
            if weight > 0:
                scored.append((path, weight))
        if not scored:
            return None

        # 有别的图就避开最近发过的（跨标签），再避开同一标签上次那张
        if len(scored) > 1:
            recent_paths = {
                self.by_sid[sid].path for sid in self._recent if sid in self.by_sid
            }
            filtered = [item for item in scored if item[0] not in recent_paths]
            scored = filtered or scored
        if len(scored) > 1:
            filtered = [item for item in scored if item[0] != self._last.get(name)]
            scored = filtered or scored
        chosen = random.choices([p for p, _ in scored], weights=[w for _, w in scored])[0]
        self._last[name] = chosen
        item = self.by_path.get(chosen.resolve())
        if item is not None:
            self._remember(item.sid)
        return chosen

    @staticmethod
    def to_segment(path: Path) -> dict:
        """OneBot 11 的图片段，用 base64 传，避免路径转义问题。"""
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "image", "data": {"file": f"base64://{b64}"}}

    def _marker_re(self) -> re.Pattern[str]:
        """剥标记用的正则。

        除了标准的 [表情:标签]，模型还会像人打字一样写 QQ 裸表情码
        （[害羞] / 【开心】）—— 括号里正好是**已加载的标签**时也按标记处理。
        只认标签表里有的名字，[图片]/[表情包]/[笑] 这类不动，免得误伤正文；
        标签最长 4 个字（「别的模型」），更长的裸码不会出现。
        关掉表情包时裸码只是普通文字，不剥。
        """
        if not self.enabled or not self.available:
            return MARKER_RE
        names = tuple(n for n in self.moods if 1 <= len(n) <= 4)
        cached = self._marker_cache
        if cached is not None and cached[0] == names:
            return cached[1]
        if not names:
            self._marker_cache = (names, MARKER_RE)
            return MARKER_RE
        # 长的先匹配，免得短标签抢在前面
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        bare = r"[\[【［]\s*(?P<bare>" + alt + r")\s*[\]】］]"
        pattern = re.compile(rf"(?:{MARKER_RE.pattern})|(?:{bare})")
        self._marker_cache = (names, pattern)
        return pattern

    def split_marker(self, text: str) -> tuple[str, str | None]:
        """把回复里的表情标记剥出来。返回 (正文, 情绪或None)。

        标记有两种写法：标准的 [表情:标签]，以及模型写歪的 QQ 裸码 [害羞]
        （只认已加载的标签，见 _marker_re）。另外还可能只写个 [s表情] / [表情]。
        这些标记都不能出现在聊天正文里，所以：

        1. 全文扫，不只看句尾
        2. 所有标记一律剥掉（包括没有情绪的残次品）
        3. 情绪取最后一个带标签的；一个标签都没有就不发图
        """
        raw = text or ""
        pattern = self._marker_re()
        matches = list(pattern.finditer(raw))
        if not matches:
            if STRAY_MARKER_RE.search(raw):
                log.warning("表情标记认不出来，原样留在正文里：%r", raw[-40:])
            return raw, None

        body = pattern.sub("", raw)
        if STRAY_MARKER_RE.search(body):
            # 还有没剥掉的标记（比如标签写到 32 个字以上）：这条日志是排查入口
            log.warning("还有没剥掉的表情标记：%r", body[-40:])
        # 标记拿掉后别在中间留下两个空格
        body = re.sub(r"[ \t]{2,}", " ", body).strip()
        if not self.enabled or not self.available:
            return body, None

        mood: str | None = None
        for match in matches:
            groups = match.groupdict()
            tag = (groups.get("tag") or groups.get("bare") or "").strip()
            if tag:
                mood = tag
        if mood is None:
            log.debug("剥掉了没有情绪标签的表情标记：%r", raw[-30:])
        return body, mood
