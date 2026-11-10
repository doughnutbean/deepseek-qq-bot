"""梗的「用法画像」表：每天算一次，运行时只查表。

为什么要这张表：`memes.json` 里的 `scene` 只有主人手写的那几条，别的一概空着，
她遇到新梗只能自由发挥（2026-10-30 的尾巴梗就是这么来的）。
`memeusage` 能从上下文里挖证据，但**不能每轮现算**（太慢），也不该直接把
统计结论当事实写进梗表（写错会污染所有回复）。

所以分三层：
- `memeusage`：挖证据（0 token，本地）；
- `UsageBook`（这里）：把证据压成三档结论存盘，硬/软/不够；
- `memes.brief()`：手写的 `scene` 优先，空着才用这里的画像顶上，
  且文案里明说「统计出来的，别当铁律」。

表是**生成物**：整表 `build()` 可重建，手写结论永远不会被它覆盖。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MIN_HITS = 3        # 一个词至少出现这么多次才值得算
MAX_TERMS = 80      # 每个群最多算这么多条，免得每天扫太久
VERSION = 1


class UsageBook:
    """按群存「这条梗怎么用」：tier + 一句场景 + 证据摘要。"""

    def __init__(self, path: Path | None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path is not None else None
        self.enabled = bool(enabled)
        self._store: dict[str, Any] = {"version": VERSION, "built_at": 0, "groups": {}}
        self.load()

    # ---------- 读写 ----------

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("用法画像表读不出来，先当空的", exc_info=True)
            return
        if isinstance(data, dict) and isinstance(data.get("groups"), dict):
            self._store = data

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._store, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            log.warning("用法画像表写不进去", exc_info=True)

    # ---------- 生成 ----------

    def build(
        self,
        miner: Any,
        terms_by_group: dict[str, list[str]],
        subjects: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """批量挖一遍，整表重建。返回统计（给日志/CLI）。

        `subjects`：某个词是谁说的（来自字幕的 up 主）。术语归属靠它，
        不从群聊天里猜。
        """
        if not self.enabled or miner is None:
            return {"groups": 0, "kept": 0, "hard": 0, "soft": 0}
        now = time.time()
        old_groups = self._store.get("groups") or {}
        groups: dict[str, dict[str, Any]] = {}
        hard = soft = 0
        for gid, terms in (terms_by_group or {}).items():
            bodies = miner.norm_rows(gid)
            if not bodies:
                continue
            # 先按出现次数粗筛：算画像前别为一次性词跑全群
            scored: list[tuple[str, int]] = []
            for term in _dedup(terms):
                key = _norm_term(term)
                if not key:
                    continue
                n = sum(1 for body in bodies if _hit(body, key))
                if n >= MIN_HITS:
                    scored.append((str(term).strip(), n))
            scored.sort(key=lambda kv: -kv[1])
            book: dict[str, Any] = {}
            for term, n in scored[:MAX_TERMS]:
                try:
                    prof = miner.profile(
                        term,
                        gid,
                        subject=(subjects or {}).get(term, ""),
                        # 字幕比群聊干净：那里是「一个人对一堆人说话」，
                        # 没有插科打诨，前置词阈值可以放到 2。
                        min_before=2 if str(gid) == "bili" else None,
                        # 字幕是一段连续独白：触发条件常在 3~4 句以前
                        before=4 if str(gid) == "bili" else None,
                    )
                except Exception:  # noqa: BLE001 - 一条算不出来不能毁掉整表
                    log.debug("用法画像算失败：%s", term, exc_info=True)
                    continue
                tier = str(prof.get("tier") or "weak")
                scene = str(prof.get("scene") or "").strip()
                examples = prof.get("examples") or []
                # 结论没有、但有用例：也留着 —— 下回直接给她看真实前后文，
                # 让她自己归纳，而不是只看一句概括。
                if (tier == "weak" or not scene) and not examples:
                    continue
                after = prof.get("after") or [{}]
                before = prof.get("before") or [{}]
                prev = ((old_groups.get(str(gid)) or {}).get(str(term).strip())) or {}
                book[str(term).strip()] = {
                    "tier": tier,
                    "scene": scene[:60],
                    "reply": str((after[0] or {}).get("text") or "")[:20],
                    "before": str((before[0] or {}).get("word") or "")[:12],
                    "examples": examples[:2],
                    "hits": int(prof.get("hits") or 0),
                    "speakers": int(prof.get("speakers") or 0),
                    # 重建时别把她被纠正过的分抹掉：反馈是跨天累积的
                    "score": int(prev.get("score") or 0),
                    "seen": int(prev.get("seen") or 0),
                    "built_at": int(now),
                }
                if tier == "hard":
                    hard += 1
                elif tier == "soft":
                    soft += 1
            if book:
                groups[str(gid)] = book
        self._store = {"version": VERSION, "built_at": int(now), "groups": groups}
        self.save()
        return {"groups": len(groups), "kept": hard + soft, "hard": hard, "soft": soft}

    # ---------- 查 ----------

    def lookup(self, group_id: str, term: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        key = str(term or "").strip()
        if not key:
            return None
        groups = self._store.get("groups") or {}
        # 先看这个群，再看字幕池（bili）：群里没人固定接的梗，字幕里往往有槽位
        for gid in (str(group_id), "bili"):
            book = groups.get(gid) or {}
            row = book.get(key)
            if isinstance(row, dict):
                return dict(row)
        return None

    def scene_for(self, group_id: str, term: str) -> str:
        """给注入用的一句话；没有就空串（宁可不说，也别编）。

        反馈闭环降过分的不再注入：分数低说明这条结论在群里接错过。
        """
        row = self.lookup(group_id, term)
        if not row:
            return ""
        if self.verdict(group_id, term) != "ok":
            return ""
        scene = str(row.get("scene") or "").strip()
        return scene if row.get("tier") in ("hard", "soft") else ""

    def adjust(self, group_id: str, term: str, delta: int) -> int:
        """她接完之后，群里继续聊 → +1；有人纠正 → -2。

        分数只存在画像表里，不动手写的 `scene`（那是主人的，不能被统计改）。
        低到阈值就降级：先不用结论、只给用例，再低就不给，免得反复接错。
        """
        row = self._row_ref(group_id, term)
        if row is None:
            return 0
        score = int(row.get("score") or 0) + int(delta)
        row["score"] = max(-9, min(9, score))
        row["seen"] = int(row.get("seen") or 0) + 1
        self.save()
        return int(row["score"])

    def verdict(self, group_id: str, term: str) -> str:
        """按反馈分决定这条结论还能不能用：ok / downgrade / off。"""
        row = self.lookup(group_id, term)
        if not row:
            return "ok"
        score = int(row.get("score") or 0)
        if score <= -4:
            return "off"
        if score <= -2:
            return "downgrade"
        return "ok"

    def _row_ref(self, group_id: str, term: str) -> dict[str, Any] | None:
        """拿到表里那一行本身（要改），跟 lookup 一样带字幕池兜底。"""
        key = str(term or "").strip()
        if not key:
            return None
        groups = self._store.get("groups") or {}
        for gid in (str(group_id), "bili"):
            book = groups.get(gid)
            if isinstance(book, dict) and isinstance(book.get(key), dict):
                return book[key]
        return None

    def example_for(self, group_id: str, term: str) -> str:
        """没有结论的梗：给一段真实用例，让她自己看怎么用的。

        压成一行：`甲：… / 乙：「词」 / 丙：…`，只取第一条，免得把
        动态上下文撑长（这条是补空用的，不是每次都要）。
        """
        row = self.lookup(group_id, term)
        if not row:
            return ""
        if self.verdict(group_id, term) == "off":
            return ""
        examples = row.get("examples") or []
        if not isinstance(examples, list) or not examples:
            return ""
        one = examples[0]
        if not isinstance(one, dict):
            return ""
        pieces: list[str] = []
        for slot in ("before", "line", "after"):
            part = one.get(slot)
            if not isinstance(part, dict):
                continue
            nick = str(part.get("nick") or "")[:8]
            text = str(part.get("text") or "").strip()[:24]
            if text:
                pieces.append(f"{nick}：{text}" if nick else text)
        return " / ".join(pieces)[:120]

    def status(self) -> dict[str, Any]:
        groups = self._store.get("groups") or {}
        return {
            "built_at": int(self._store.get("built_at") or 0),
            "groups": len(groups),
            "terms": sum(len(b) for b in groups.values() if isinstance(b, dict)),
            "downvoted": sum(
                1
                for b in groups.values()
                if isinstance(b, dict)
                for row in b.values()
                if isinstance(row, dict) and int(row.get("score") or 0) <= -2
            ),
        }


def _dedup(terms: Any) -> list[str]:
    out: list[str] = []
    for term in terms or ():
        text = str(term or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _norm_term(term: str) -> str:
    from .memes import norm

    return norm(str(term or ""))


def _hit(body: str, key: str) -> bool:
    """换字也算命中：「和我玩」和「跟我玩」是同一句。"""
    from .memeusage import _match

    return _match(body, key)
