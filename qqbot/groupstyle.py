"""群聊语言风格画像：只统计「怎么说」，不学「说什么」。

和 `stylelearn.py` 的分工：

- `stylelearn.py` 抽的是「A 说一句、B 接一句」的**接话对**，要主人审批后才注入；
- 本模块只做**统计**：句长、标点、语气、连发节奏、高频口头语。零模型调用，
  每 30 分钟从 `group_log.db` 重算一次，渲染成 ≤220 字的提示。

默认关闭（`ENABLE_GROUP_STYLE=false`）。原因是这个项目的人设很细，
风格注入会改变说话方式；先用 `python tools/style_profile.py` 看画像，
确认不像“统计报表”再开。

几条硬规矩：

- 只用群友发言；`group_log` 本来就不收机器人自己的话。
  别的 AI / 机器人（`speakers.json` 里的名单）也要剔掉：2026-10-27 那个群
  最近 187 条里 41% 是另一只 bot 发的，算出的「群风格」其实是它的口音。
- 去掉昵称前缀、引用尾巴、图片/转发占位、链接、命令。
- 样本 < `min_sample` 不启用，宁可不说。
- 只给“说法/节奏”的建议，最后一句明确“不改变要说的内容”。
- 不做“自我批评”（那需要机器人自己的发言语料，本项目没存）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^【[^】]*】")
_QUOTE_RE = re.compile(r"（引用了前一条：[^）]*）")
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|动画表情|语音|视频|文件|卡片消息|合并转发)[^\]]*\]"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_CMD_RE = re.compile(r"^[/#]")
_MEANINGFUL_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_SHORT = 6
_LONG = 16
_BURST_GAP = 45

_ADOPT = {
    "观察为主": "这些只是统计出来的习惯，当参考：不冲突时靠过去，别扭就按你自己的说法。",
    "贴近": "尽量按这个群的习惯来：长度、标点、分条节奏都可以跟着走，内容还是你自己想。",
    "强模仿": "把长度和标点当硬规则执行；拿不准时选更短、更口语的那个说法。",
}


def clean(text: str) -> str:
    body = (text or "").strip()
    body = _TAG_RE.sub("", body)
    body = _QUOTE_RE.sub("", body)
    body = _PLACEHOLDER_RE.sub("", body)
    body = _URL_RE.sub("", body)
    if _CMD_RE.match(body):
        return ""
    if "：" in body[:24]:
        head, _, rest = body.partition("：")
        if len(head) <= 16 and rest.strip():
            body = rest
    return body.strip()


def _meaningful(text: str) -> bool:
    return len(text) >= 2 and bool(_MEANINGFUL_RE.search(text))


def _emoji_present(text: str) -> bool:
    count = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x1F300 <= cp <= 0x1FAFF
            or 0x2600 <= cp <= 0x27BF
            or 0x1F000 <= cp <= 0x1F2FF
            or cp in (0x2764, 0x203C, 0x2049)
        ):
            count += 1
            if count > 3:
                return False
    return count > 0


def _quantile(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    idx = round((len(sorted_values) - 1) * p)
    return sorted_values[max(0, min(len(sorted_values) - 1, idx))]


def analyze(
    rows: list[dict[str, Any]],
    *,
    min_sample: int = 30,
    now: float | None = None,
) -> dict[str, Any] | None:
    """把 group_log 的行压成一个画像；样本不够返回 None。"""
    now = now if now is not None else time.time()
    cleaned: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        body = clean(str(row.get("text") or ""))
        if _meaningful(body):
            cleaned.append((row, body))
    n = len(cleaned)
    if n < max(1, min_sample):
        return None

    texts = [body for _, body in cleaned]
    lens = sorted(len(body) for body in texts)
    mean = sum(lens) / n
    median = _quantile(lens, 0.5)
    p90 = _quantile(lens, 0.9)

    short = sum(1 for L in lens if L <= _SHORT)
    long = sum(1 for L in lens if L >= _LONG)

    no_end = dot = q = ex = tilde = ellipsis = comma = laugh = repeat = emoji = bracket = 0
    for body in texts:
        if not re.search(r"[。！？…~～!?.]", body):
            no_end += 1
        if "。" in body:
            dot += 1
        if re.search(r"[?？]", body):
            q += 1
        if re.search(r"[!！]", body):
            ex += 1
        if re.search(r"[~～]", body):
            tilde += 1
        if "…" in body or re.search(r"\.{3,}", body):
            ellipsis += 1
        if re.search(r"[，,]", body):
            comma += 1
        if re.search(r"[哈哼嘿嘻呵]{2,}|[草靠卧我日]{2,}", body):
            laugh += 1
        if re.search(r"(.)\1{2,}", body):
            repeat += 1
        if _emoji_present(body):
            emoji += 1
        if re.search(r"[（(][^）)]{1,6}[）)]", body):
            bracket += 1

    def pct(value: int) -> int:
        return round(value * 100 / n)

    # 连发节奏：同一个人 45 秒内的相邻消息算一次“开口”
    ordered = sorted(cleaned, key=lambda item: int(item[0].get("ts") or 0))
    bursts = 0
    prev_uid = None
    last_ts = 0
    for row, _ in ordered:
        uid = str(row.get("uid") or "")
        ts = int(row.get("ts") or 0)
        same = uid == prev_uid and ts >= last_ts and ts - last_ts <= _BURST_GAP
        if not same:
            bursts += 1
        prev_uid = uid
        last_ts = ts
    per_burst = round(len(ordered) / max(1, bursts), 2)

    # 高频词：复用长期记忆那套 jieba + 停用词，取文档频次
    words: list[str] = []
    try:
        from .memory import keywords

        counts: Counter[str] = Counter()
        for body in texts:
            for word in set(keywords(body)):
                counts[word] += 1
        need = max(2, int(n * 0.02))
        words = [
            word
            for word, count in counts.most_common(8)
            if count >= need and 2 <= len(word) <= 8 and not word.isdigit()
        ][:6]
    except Exception:  # noqa: BLE001 - 分词失败不该影响回复
        log.debug("群风格高频词统计失败", exc_info=True)

    senders = len({str(row.get("uid") or "") for row, _ in cleaned})
    times = [int(row.get("ts") or 0) for row, _ in cleaned if int(row.get("ts") or 0) > 0]
    span_days = round((max(times) - min(times)) / 86400, 1) if len(times) > 1 else 0.0

    return {
        "n": n,
        "senders": senders,
        "span_days": span_days,
        "mean": round(mean, 1),
        "median": median,
        "p90": p90,
        "short_ratio": round(short / n, 3),
        "long_ratio": round(long / n, 3),
        "punct": {
            "no_end": pct(no_end),
            "dot": pct(dot),
            "q": pct(q),
            "ex": pct(ex),
            "tilde": pct(tilde),
            "ellipsis": pct(ellipsis),
            "comma": pct(comma),
            "laugh": pct(laugh),
            "repeat": pct(repeat),
            "emoji": pct(emoji),
            "bracket": pct(bracket),
        },
        "per_burst": per_burst,
        "words": words,
        "analyzed_at": now,
    }


def render(
    profile: dict[str, Any] | None,
    *,
    max_chars: int = 220,
    adopt: str = "观察为主",
    include_words: bool = True,
) -> str:
    if not profile or not profile.get("n"):
        return ""
    g = profile
    p = g.get("punct") or {}
    median = max(1, int(g.get("median") or 1))
    p90 = max(median + 2, int(g.get("p90") or median))
    upper = min(p90, max(median * 3, median + 6))
    lines = [
        "【这个群的语言习惯（本地统计，不是群友原话）】",
        _ADOPT.get(adopt, _ADOPT["观察为主"]),
        f"最近 {g.get('n')} 条里，句长常见 {median} 字左右（平均 {g.get('mean')} 字），"
        f"一般不超过 {upper} 字。",
    ]
    if float(g.get("short_ratio") or 0) >= 0.4:
        lines.append("短句很多——只接半句、不凑完整句子，在这个群是正常的。")
    if float(g.get("long_ratio") or 0) >= 0.3:
        lines.append("也有不少超过 15 字的长句——想展开时可以写长一点。")

    if p.get("no_end", 0) >= 70:
        lines.append(f"{p['no_end']}% 的消息句末不带标点；别每条都加句号。")
    elif p.get("dot", 0) >= 45:
        lines.append(f"{p['dot']}% 的消息句末带句号；该带就带。")
    else:
        lines.append("标点用得比较随意，不用刻意规范。")

    marks = []
    if p.get("q", 0) >= 20:
        marks.append(f"问号多（{p['q']}%）")
    if p.get("ex", 0) >= 20:
        marks.append(f"感叹号多（{p['ex']}%）")
    if p.get("tilde", 0) >= 10:
        marks.append(f"爱拖波浪号（{p['tilde']}%）")
    if p.get("ellipsis", 0) >= 10:
        marks.append(f"爱用省略号（{p['ellipsis']}%）")
    if p.get("comma", 0) <= 15:
        marks.append(f"几乎不用逗号（{p['comma']}%）")
    if marks:
        lines.append("其他标点：" + "、".join(marks) + "。")

    tone = []
    if p.get("laugh", 0) >= 8:
        tone.append(f"常用哈哈/草这类笑声（{p['laugh']}%）")
    if p.get("repeat", 0) >= 12:
        tone.append(f"爱把字拉长（{p['repeat']}%）")
    if p.get("emoji", 0) >= 15:
        tone.append(f"偶尔用 emoji 点缀（{p['emoji']}%）")
    if p.get("bracket", 0) >= 8:
        tone.append(f"爱用（笑）（doge）这种括号补一句（{p['bracket']}%）")
    if tone:
        lines.append("语气：" + "；".join(tone) + "。")

    per_burst = float(g.get("per_burst") or 1)
    if per_burst >= 1.8:
        lines.append(f"分条：一个人常连着发（平均一次 {per_burst} 条）；想表达两个意思就发两条。")
    else:
        lines.append(f"分条：多数一条说完（平均一次 {per_burst} 条）；没必要时不要连发。")

    if include_words:
        words = [w for w in (g.get("words") or []) if w]
        if words:
            lines.append(
                "群里常出现的说法："
                + "、".join(f"「{w}」" for w in words[:6])
                + "。当语气参考，情绪对得上再用，别硬塞、别连着复读。"
            )

    lines.append("只调说法和节奏，不改变你要说的内容；不要向群友提起这个统计。")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # 截断时也要保住最后那句边界：不能让模型只看到半截统计、
    # 却没看到「不改变要说的内容」。
    boundary = "只调说法和节奏，不改变你要说的内容；不要向群友提起这个统计。"
    keep = max(20, max_chars - len(boundary) - 2)
    return text[:keep].rstrip() + "…\n" + boundary


class GroupStyle:
    """按群缓存画像；每 refresh_minutes 分钟才重算一次。"""

    def __init__(
        self,
        path: Path,
        group_log: Any,
        *,
        enabled: bool = False,
        refresh_minutes: int = 30,
        sample: int = 400,
        min_sample: int = 30,
        max_chars: int = 300,
        adopt: str = "观察为主",
        include_words: bool = True,
        exclude_uids: set[str] | None = None,
    ) -> None:
        self.path = path
        self.group_log = group_log
        self.enabled = enabled
        self.refresh_seconds = max(60, int(refresh_minutes) * 60)
        self.sample = max(50, int(sample))
        self.min_sample = max(10, int(min_sample))
        self.max_chars = max(80, int(max_chars))
        self.adopt = adopt if adopt in _ADOPT else "观察为主"
        self.include_words = include_words
        self.exclude = {str(x) for x in (exclude_uids or ()) if str(x)}
        self._groups: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        groups = data.get("groups") if isinstance(data, dict) else None
        if isinstance(groups, dict):
            for gid, item in groups.items():
                if isinstance(item, dict) and isinstance(item.get("hint"), str):
                    self._groups[str(gid)] = item

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 只留最近活跃的 60 个群，避免文件无限长
            items = sorted(
                self._groups.items(),
                key=lambda kv: float(kv[1].get("at") or 0),
                reverse=True,
            )[:60]
            data = {"version": 1, "groups": dict(items)}
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("群风格缓存写入失败：%s", exc)

    def warm(self) -> None:
        """预热 jieba 词典，避免第一次群回复卡在分词上。"""
        try:
            from .memory import keywords

            keywords("预热")
        except Exception:  # noqa: BLE001
            pass

    def refresh(self, group_id: str, *, force: bool = False) -> str:
        if not self.enabled or not group_id or not getattr(self.group_log, "enabled", False):
            return ""
        now = time.time()
        cached = self._groups.get(str(group_id)) or {}
        if (
            not force
            and cached.get("adopt") == self.adopt
            and now - float(cached.get("at") or 0) < self.refresh_seconds
        ):
            return str(cached.get("hint") or "")
        rows = self.group_log.rows(str(group_id), limit=self.sample)
        if self.exclude:
            rows = [
                r for r in rows if str(r.get("uid") or "") not in self.exclude
            ]
        profile = analyze(rows, min_sample=self.min_sample, now=now)
        if profile is None:
            self._groups[str(group_id)] = {
                "at": now,
                "adopt": self.adopt,
                "profile": None,
                "hint": str(cached.get("hint") or ""),
            }
            self._save()
            return str(cached.get("hint") or "")
        hint = render(
            profile,
            max_chars=self.max_chars,
            adopt=self.adopt,
            include_words=self.include_words,
        )
        self._groups[str(group_id)] = {
            "at": now,
            "adopt": self.adopt,
            "profile": profile,
            "hint": hint,
        }
        self._save()
        log.info("群风格画像已更新：%s（%d 条样本）", group_id, profile.get("n"))
        return hint

    def brief(self, group_id: str) -> str:
        if not self.enabled or not group_id:
            return ""
        return self.refresh(str(group_id))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "groups": len(self._groups),
            "adopt": self.adopt,
            "min_sample": self.min_sample,
        }
