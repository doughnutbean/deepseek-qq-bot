"""本地中文 embedding（BAAI/bge-small-zh-v1.5，512 维，CPU 推理）。

用来补关键词回溯的短板：关键词只能匹配字面相同的词，
embedding 能抓住「我下周出门」≈「要去杭州出差」这种语义关联。

模型文件在 models/bge-small-zh-v1.5/，用 tools/fetch_model.py 下载。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DIM_FALLBACK = 512


class Embedder:
    def __init__(self, model_dir: Path, enabled: bool = True) -> None:
        self.model_dir = model_dir
        self.enabled = enabled
        self._tok = None
        self._model = None
        self._lock = threading.Lock()
        self._load_failed = False

    # ---------- 模型加载（懒加载，第一次用才载入） ----------

    def _ensure(self) -> bool:
        if not self.enabled or self._load_failed:
            return False
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            if not (self.model_dir / "model.safetensors").exists():
                log.warning("embedding 模型不存在: %s", self.model_dir)
                self._load_failed = True
                return False
            try:
                import torch  # noqa: F401
                from transformers import AutoModel, AutoTokenizer

                self._tok = AutoTokenizer.from_pretrained(str(self.model_dir))
                self._model = AutoModel.from_pretrained(str(self.model_dir)).eval()
                log.info("embedding 模型已加载: %s", self.model_dir.name)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("加载 embedding 模型失败: %s", exc)
                self._load_failed = True
                return False

    @property
    def available(self) -> bool:
        """会触发加载（可能在磁盘上读几百毫秒到几秒）。命令行工具用这个。"""
        return self._ensure()

    @property
    def ready(self) -> bool:
        """只查状态，不触发加载。消息路径必须用这个，否则会卡住回复。"""
        return self._model is not None

    # ---------- 编码 ----------

    def encode(self, texts: list[str]) -> list[bytes]:
        """返回 float32 的原始字节，直接可存 BLOB。"""
        if not texts or not self._ensure():
            return []
        import torch

        assert self._tok is not None and self._model is not None
        try:
            inp = self._tok(
                texts, padding=True, truncation=True, max_length=256, return_tensors="pt"
            )
            with torch.no_grad():
                out = self._model(**inp).last_hidden_state
            mask = inp["attention_mask"].unsqueeze(-1).float()
            vec = (out * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            vec = torch.nn.functional.normalize(vec, dim=-1)
            return [v.astype(np.float32).tobytes() for v in vec.numpy()]
        except Exception as exc:  # noqa: BLE001
            log.warning("编码失败: %s", exc)
            return []

    async def encode_async(self, texts: list[str]) -> list[bytes]:
        """在子线程里跑，别卡住 asyncio 事件循环。"""
        import asyncio

        if not texts:
            return []
        return await asyncio.to_thread(self.encode, texts)

    # ---------- 比较 ----------

    @staticmethod
    def to_vector(blob: bytes | None) -> np.ndarray | None:
        if not blob:
            return None
        try:
            arr = np.frombuffer(blob, dtype=np.float32)
        except (ValueError, TypeError):
            return None
        if arr.size == 0:
            return None
        # 保险起见再归一化一次（存进来的已经归一化过）
        norm = float(np.linalg.norm(arr))
        return arr / norm if norm > 0 else None

    @classmethod
    def rank(
        cls,
        query_blob: bytes | None,
        candidates: list[tuple[int, np.ndarray]],
        z_min: float = 1.4,
        cos_min: float = 0.35,
        margin: float = 0.05,
    ) -> list[tuple[int, float]]:
        """按三个条件一起挑，缺一个都可能误判。

        1. **z-score** —— 相对这批候选的均值，相关的会明显高出（实测 1.5~2.0，无关的 1.3 左右）
        2. **绝对余弦下限** —— 全是低分候选时不要硬凑
        3. **与未入选者的差距** —— 这条最灵：相关的第一名会明显甩开第二名（0.15），
           无关的问题第一名和第二名几乎是平的（0.03）
        """
        q = cls.to_vector(query_blob)
        if q is None or not candidates:
            return []

        pairs: list[tuple[int, float]] = []
        for tid, vec in candidates:
            if vec is None or vec.shape != q.shape:
                continue
            pairs.append((tid, float(np.dot(q, vec))))
        if len(pairs) < 3:
            return []

        sims = np.asarray([s for _, s in pairs], dtype=np.float32)
        mean = float(sims.mean())
        std = float(sims.std())
        if std < 1e-6:
            return []

        qualified: list[tuple[int, float, float]] = []
        rest: list[float] = []
        for tid, s in pairs:
            z = (s - mean) / std
            if z >= z_min and s >= cos_min:
                qualified.append((tid, s, z))
            else:
                rest.append(s)
        if not qualified:
            return []

        # 第一名必须明显甩开所有没入选的
        best_sim = max(s for _, s, _ in qualified)
        if rest and (best_sim - max(rest)) < margin:
            return []

        qualified.sort(key=lambda x: -x[1])
        return [(tid, round(z, 2)) for tid, _s, z in qualified]
