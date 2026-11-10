"""克里斯笑话的反馈账：群友笑了就多讲，嫌老就少讲，**下一轮挑选才生效**。

为什么要单独一层：

- `chris_lore` 只读 `data/chris_comments.db` 的 candidate，不该被聊天写脏；
- 反馈不能直接改 `candidate.score` —— 那是点赞/蒸馏/人工审核的证据，
  也不该让 `pack` 跟着变；
- 权重必须能等：这一轮已经递出去的几条不受影响，下一次挑笑话时才折算。
  所以这里把「收到的反馈」和「生效的权重」分开，`begin_round()` 才合并。

口径（全本地、0 token）：

- 只在 `chris_lore` 刚挑给她的语料里认：她发出去的话完整出现某条（≥8 字），
  才算「这轮讲了这条」；
- 之后 3 分钟内、同群其他人的回应，按笑声/嫌弃词判正负；
- 同一个人只算一次，一条最多记 3 个人；中性话不记；
- 反馈先写 `pending`，`begin_round()` 才并进 `weights`；
- 权重乘在 score 上（0.25~4.0），不碰原始分。

文件是运行时状态（`data/chris_feedback.json`，不进 Git），删掉就等于全部回滚。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1

# 她讲完之后，等群友反应的时间窗。太短会漏掉「愣了一下才笑」，太长会把闲聊算进来。
WINDOW_SECONDS = 180.0
# 一条笑话最多采几个人的反应，防止刷屏把权重刷爆。
MAX_REACTIONS = 3
# 跟刚挑给她的语料匹配时，至少连续重合多少个字才算讲了这条。
MIN_MATCH_CHARS = 8
# 群友把包袱接了一遍：6 个字的重合已经够说明「听懂了、愿意接」，
# 但整条笑话还是要求 8 个字，免得短句撞车。
ECHO_MIN_CHARS = 6
# 挑给她之后多久内发出去还算「这轮讲的」。
OFFER_TTL_SECONDS = 600.0
# 权重范围：低到 0.25 还会偶尔出现，高到 4.0 也不会永远只讲同一条。
MIN_WEIGHT = 0.25
MAX_WEIGHT = 4.0
# 一次好/差评各折算多少。差评稍重：群友明确说「听过了」比笑一下更值得记。
DELTA_POSITIVE = 0.2
DELTA_NEGATIVE = -0.3

# 笑、转发、催更；这些词出现在回应里就算「这轮接住了」。
_POSITIVE = re.compile(
    r"哈{2,}|呵{2,}|嘿{2,}|233+|6{3,}|"
    r"笑死|笑疯|笑出声|笑不活|绷不住|没绷住|憋不住|"
    r"太草|好草|草死|草生|草了|^草+$|草(?:[，。！？!?]|$)|"
    r"(?:太|好|有)梗|梗王|好活|整活|"
    r"好玩(?!吗|么)|好笑(?!吗|么)|有意思(?!吗|么)|有笑点|"
    r"再来(?:一个|一条|一点)|多来(?:点|几个|几条)|"
    r"收了|偷了|存了|太典|典中典|太乐|乐死"
)

# 嫌弃/听过了。放在笑声前面判：「不好笑」里也有「好笑」。
# 「没听过 / 没看过」是觉得新鲜，不是嫌弃，用后视断言排掉。
_NEGATIVE = re.compile(
    r"(?<!没)(?<!没有)听过|(?<!没)(?<!没有)看过|"
    r"老梗|旧梗|烂梗|破梗|过时|太土|好土|土了|土掉渣|"
    r"没(?:有|啥|什么)?意思|无聊|不(?:太|怎么)?好笑|不咋地|"
    r"什么玩意|尴尬|好尬|太尬|尬住|下头|"
    r"好冷|太冷|冷场|冷笑话|有点冷|冷到了|"
    r"换一个|换条|别讲了|别说了|烦不烦|没劲|没新意|"
    r"就这(?:[?？!！。啊呀]|$)"
)

# 「不是不好笑」是双重否定，别被当成差评。
_DOUBLE_NEGATIVE = re.compile(r"不是(?:不好笑|没意思|无聊)")

# 归一化只留可比较的字：空格、标点、表情、大小写都不影响「她讲的是哪条」。
_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """去掉标点/空白/大小写，只留能比较的字。"""
    return _NOISE.sub("", str(text or "")).lower()


def classify(text: str, told: str = "") -> tuple[float, str]:
    """把一条群友回应判成好评/差评/中性。返回 (权重增量, 原因)。"""
    body = str(text or "").strip()
    if not body:
        return 0.0, ""
    if _DOUBLE_NEGATIVE.search(body):
        return DELTA_POSITIVE, "double-negative"
    if _NEGATIVE.search(body):
        return DELTA_NEGATIVE, "negative"
    if _POSITIVE.search(body):
        return DELTA_POSITIVE, "positive"
    # 有人把包袱原样接了一遍，也是接住了；只认笑话的尾巴，避免太长误判。
    told_norm = normalize(told)
    if len(told_norm) >= ECHO_MIN_CHARS:
        body_norm = normalize(body)
        tail = told_norm[-ECHO_MIN_CHARS:]
        if tail in body_norm and len(body_norm) <= len(told_norm) + 24:
            return DELTA_POSITIVE, "echo"
    return 0.0, ""


def best_match(answer: str, items: dict[int, str]) -> tuple[int, str] | None:
    """她这次讲的是哪一条：只认完整包含（去标点后），取最长的。"""
    body = normalize(answer)
    best: tuple[int, str, int] | None = None
    for cid, text in items.items():
        norm = normalize(text)
        if len(norm) < MIN_MATCH_CHARS:
            continue
        if norm in body and (best is None or len(norm) > best[2]):
            best = (int(cid), str(text), len(norm))
    return (best[0], best[1]) if best else None


class ChrisFeedback:
    """反馈账本：pending（收到的评价）→ weights（下一轮生效的权重）。"""

    def __init__(
        self,
        path: Path | None,
        *,
        enabled: bool = True,
        window: float = WINDOW_SECONDS,
        max_reactions: int = MAX_REACTIONS,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.enabled = bool(enabled)
        self.window = max(10.0, float(window))
        self.max_reactions = max(1, int(max_reactions))
        self._round = 0
        self._weights: dict[int, dict[str, Any]] = {}
        self._pending: list[dict[str, Any]] = []
        # 最近挑给她的语料：group_id -> {ts, items: {candidate_id: text}}
        self._offers: dict[str, dict[str, Any]] = {}
        # 等反应的最近一条笑话：group_id -> {ts, id, text, uids, count}
        self._watch: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[int, float] | None = None
        self._mtime = 0.0
        self._load()

    # ---------- 挑给她 / 她讲了 ----------

    def note_offer(
        self,
        group_id: str,
        items: list[tuple[int, str]],
        now: float | None = None,
    ) -> None:
        """记下这一轮挑给她的几条，之后只在这几条里认「她讲了哪条」。"""
        if not self.enabled:
            return
        gid = str(group_id or "")
        if not gid:
            return
        book = self._offers.setdefault(gid, {"ts": 0.0, "items": {}})
        for cid, text in items or []:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            text = str(text or "").strip()
            if cid <= 0 or len(normalize(text)) < MIN_MATCH_CHARS:
                continue
            # 重新插到末尾：同一个候选被再次挑中时，别在裁剪时被当成旧的丢掉。
            book["items"].pop(cid, None)
            book["items"][cid] = text
        # 只留最近 12 条：同一轮连着挑两三次时也够用，又不会无限涨。
        if len(book["items"]) > 12:
            for key in list(book["items"])[:-12]:
                book["items"].pop(key, None)
        book["ts"] = time.time() if now is None else float(now)

    def observe_reply(
        self,
        group_id: str,
        answer: str,
        now: float | None = None,
    ) -> int:
        """她刚发出一条回复：如果里面整段出现了刚挑给她的某条，开始等反应。"""
        if not self.enabled:
            return 0
        gid = str(group_id or "")
        body = str(answer or "")
        if not gid or len(normalize(body)) < MIN_MATCH_CHARS:
            return 0
        now = time.time() if now is None else float(now)
        offer = self._offers.get(gid)
        if not offer:
            return 0
        if now - float(offer.get("ts") or 0) > OFFER_TTL_SECONDS:
            self._offers.pop(gid, None)
            return 0
        hit = best_match(body, offer.get("items") or {})
        if hit is None:
            return 0
        cid, text = hit
        self._watch[gid] = {
            "ts": now,
            "id": cid,
            "text": text,
            "uids": set(),
            "count": 0,
        }
        log.info("克里斯笑话反馈：这轮讲的是 #%s，开始等群友反应", cid)
        return cid

    def observe_message(
        self,
        group_id: str,
        user_id: Any,
        text: str,
        now: float | None = None,
    ) -> float:
        """群里来了一条别人的话：在时间窗内就判一次好评/差评，先记 pending。"""
        if not self.enabled:
            return 0.0
        gid = str(group_id or "")
        watch = self._watch.get(gid)
        if not watch:
            return 0.0
        now = time.time() if now is None else float(now)
        if now - float(watch.get("ts") or 0) > self.window:
            self._watch.pop(gid, None)
            return 0.0
        uid = str(user_id or "")
        if not uid or uid in watch["uids"]:
            return 0.0
        delta, why = classify(text, str(watch.get("text") or ""))
        if not delta:
            return 0.0
        watch["uids"].add(uid)
        watch["count"] = int(watch.get("count") or 0) + 1
        self._pending.append(
            {
                "id": int(watch["id"]),
                "delta": round(delta, 3),
                "ts": int(now),
                "why": why,
            }
        )
        if len(self._pending) > 500:  # 长期没人再要笑话时也别无限涨
            self._pending = self._pending[-500:]
        if watch["count"] >= self.max_reactions:
            self._watch.pop(gid, None)
        self._save()
        log.info(
            "克里斯笑话反馈：#%s 记了一笔%s（%s），下一轮挑选才生效",
            watch["id"],
            "好评" if delta > 0 else "差评",
            why,
        )
        return delta

    # ---------- 新一轮：把 pending 折算成权重 ----------

    def begin_round(self) -> dict[int, float]:
        """开始新一轮挑选：先把上轮攒的反馈并进权重，再返回这一轮的快照。

        这一轮已经递出去的条目不受影响；反馈只影响「下一次」挑出来的顺序。
        """
        if not self.enabled:
            return {}
        self._reload_if_changed()
        if self._pending:
            for item in self._pending:
                self._fold(item)
            self._pending = []
            self._snapshot = None
            self._save()
        self._round += 1
        return self.weights()

    def weights(self) -> dict[int, float]:
        """当前**已生效**的权重（不含 pending）。"""
        if self._snapshot is None:
            self._snapshot = {
                cid: round(float(row.get("w") or 1.0), 3)
                for cid, row in self._weights.items()
            }
        return dict(self._snapshot)

    def pending_count(self) -> int:
        return len(self._pending)

    def watch_count(self) -> int:
        return len(self._watch)

    def status(self) -> dict[str, Any]:
        return {
            "round": self._round,
            "applied": len(self._weights),
            "pending": len(self._pending),
            "watching": len(self._watch),
            "path": str(self.path) if self.path is not None else "",
            "weights": {
                cid: dict(row) for cid, row in self._weights.items()
            },
        }

    def reset(self) -> None:
        """清空所有反馈（回滚到人人都 1.0）。"""
        self._round = 0
        self._weights.clear()
        self._pending.clear()
        self._offers.clear()
        self._watch.clear()
        self._snapshot = None
        self._save()

    # ---------- 存盘 ----------

    def _fold(self, item: dict[str, Any]) -> None:
        try:
            cid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            return
        try:
            delta = float(item.get("delta") or 0)
        except (TypeError, ValueError):
            return
        if cid <= 0 or not delta:
            return
        row = self._weights.setdefault(cid, {"w": 1.0, "pos": 0, "neg": 0, "ts": 0})
        weight = float(row.get("w") or 1.0) + delta
        row["w"] = round(max(MIN_WEIGHT, min(MAX_WEIGHT, weight)), 3)
        if delta > 0:
            row["pos"] = int(row.get("pos") or 0) + 1
        else:
            row["neg"] = int(row.get("neg") or 0) + 1
        row["ts"] = int(item.get("ts") or time.time())

    def _reload_if_changed(self) -> None:
        """命令行 `feedback --reset` 在机器人跑着时改文件，下一轮也能看见。"""
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            # 文件被删掉也当回滚：下一轮重新从 1.0 开始，别一直拿着内存里的旧账。
            if self._mtime:
                self._weights.clear()
                self._pending.clear()
                self._snapshot = None
                self._mtime = 0.0
            return
        if mtime != self._mtime:
            self._load()

    def _load(self) -> None:
        self._snapshot = None
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("克里斯反馈账读不出来，先当空的", exc_info=True)
            return
        if not isinstance(data, dict):
            return
        try:
            self._round = int(data.get("round") or 0)
        except (TypeError, ValueError):
            self._round = 0
        weights = data.get("weights")
        if isinstance(weights, dict):
            cleaned: dict[int, dict[str, Any]] = {}
            for key, row in weights.items():
                try:
                    cid = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(row, dict):
                    try:
                        weight = max(
                            MIN_WEIGHT, min(MAX_WEIGHT, float(row.get("w") or 1.0))
                        )
                    except (TypeError, ValueError):
                        weight = 1.0
                    cleaned[cid] = {
                        "w": round(weight, 3),
                        "pos": int(row.get("pos") or 0),
                        "neg": int(row.get("neg") or 0),
                        "ts": int(row.get("ts") or 0),
                    }
            self._weights = cleaned
        pending = data.get("pending")
        if isinstance(pending, list):
            self._pending = [item for item in pending if isinstance(item, dict)]
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": VERSION,
                "round": self._round,
                "weights": {str(cid): row for cid, row in self._weights.items()},
                "pending": self._pending,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.path)
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = 0.0
        except OSError:
            log.warning("克里斯反馈账写不进去", exc_info=True)
