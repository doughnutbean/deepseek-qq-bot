"""从 B 站视频的 AI 字幕里挖克里斯的口头禅。

评论区是观众说的话，字幕才是他本人说的话。这里只读
`data/bili/BV*.json` 里的 `subtitle`（官方 AI 字幕/分段），不联网。

做法：把字幕行归一化成 2~20 字的短句，统计「出现在多少个视频里」。
只收整行重复的说法，不切 n-gram —— 切出来的「是不是有」「付费留言说」
这种碎片没有意义。要求至少出现在 N 个视频里，避免一句口误被当成口头禅。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .chris_distill import fingerprint
from .stopwords import STOPWORDS

MIN_CHARS = 2
MAX_CHARS = 20

# 哪个主播都会说的，不算他的口头禅
_GENERIC = {
    "没有", "可以", "对啊", "怎么了", "这个", "那个", "就是", "不是",
    "然后", "因为", "所以", "什么", "怎么", "真的", "确实", "应该",
    "可能", "现在", "今天", "明天", "昨天", "时候", "东西", "一个",
    "我们", "你们", "他们", "自己", "知道", "觉得", "感觉", "看到",
    "听到", "一下", "有点", "还是", "已经", "反正", "意思", "问题",
    "地方", "这样", "那样", "怎么样", "为什么", "是不是", "有没有",
    "好吧", "哎呦", "好像", "这个人",
    "嗯嗯啊", "嗯啊", "嗯", "啊", "哎", "就是这样", "就是这样啊", "这样的",
}
# ASR 把同一句听成不同写法：合并成一条
_VARIANTS = {
    "what的发": "我的发",
    "what的": "我的发",
}
_TRAILING_PARTICLES = "啊呀呢吧哦嘛啦"


def _canon(text: str) -> str:
    out = _VARIANTS.get(text, text)
    # 「你拿什么跟我玩啊」和「你拿什么跟我玩」是同一句；太短的（可以啊/好吧）不剥
    if len(out) >= 4 and out[-1] in _TRAILING_PARTICLES:
        out = out[:-1]
    return out
_PUNCT = re.compile(r"[\s\u3000，。！？、；：\"'“”‘’《》（）()【】\[\]…—\-~·!?,.;:]+")
_KEEP = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
# 游戏播报/系统语音：跨视频重复，但不是他说的
_NOISE_RE = re.compile(
    r"停止射击|确认敌方|备用电源|相关入口|闯入者|爆炸范围|注意弹药|"
    r"准备突破|目标逼近|通行许可|塔台供电|掩护我|交替射击|已开启|"
    r"已解锁|已接通|一枪爆头|速战速决|撤离点|空投|补给"
)
# 背景音乐歌词：同一首 BGM 反复出现，也不是口头禅
_NOISE_LINES = {
    "像倦鸟失了归期", "我总是偷偷的哭泣", "每一天我们都是如此愉快",
    "结局紧握在我手心", "一直到天色渐晚", "消失在人海", "我们也经常争执",
    "互相不接电话", "可现实为何让我感到如此懈怠", "现在的你还好吗",
    "不要冒进",
}


def _clean(text: Any) -> str:
    out = _PUNCT.sub("", str(text or ""))
    return _KEEP.sub("", out).strip()


def load_segments(directory: str | Path) -> list[tuple[str, str]]:
    """[(bvid, 字幕行)]；文件读不了就跳过。"""
    out: list[tuple[str, str]] = []
    for path in sorted(Path(directory).glob("BV*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bvid = str(data.get("bvid") or path.stem)
        for item in data.get("subtitle") or []:
            if not isinstance(item, dict):
                continue
            text = _clean(item.get("text"))
            if text:
                out.append((bvid, text))
    return out


def catchphrase_candidates(
    directory: str | Path,
    min_videos: int = 2,
    min_hits: int = 3,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """字幕里的高频短句 → catchphrase 候选。"""
    segments = load_segments(directory)
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"videos": set(), "hits": 0, "display": Counter(), "bvid": ""}
    )

    def add(key: str, bvid: str, display: str) -> None:
        key = _canon(key)
        display = _canon(display)
        if len(key) < MIN_CHARS or key in STOPWORDS or key in _GENERIC:
            return
        if _NOISE_RE.search(key) or key in _NOISE_LINES:
            return
        # 纯英文/拼音多半是 ASR 把背景音乐听岔了，不当口头禅
        if not re.search(r"[\u4e00-\u9fff]", key):
            return
        item = stats[key]
        item["videos"].add(bvid)
        item["hits"] += 1
        item["display"][display] += 1
        if not item["bvid"]:
            item["bvid"] = bvid

    for bvid, text in segments:
        if MIN_CHARS <= len(text) <= MAX_CHARS:
            add(text, bvid, text)

    rows = [
        (key, item)
        for key, item in stats.items()
        if len(item["videos"]) >= min_videos and item["hits"] >= min_hits
    ]
    rows.sort(key=lambda kv: (-len(kv[1]["videos"]), -kv[1]["hits"]))

    # 同一句的长短版本（「我说实在」/「我说实在的」）只留长的
    chosen: list[tuple[str, dict[str, Any]]] = []
    for key, item in rows:
        if any(key != other and key in other for other, _ in chosen):
            continue
        chosen.append((key, item))
        if len(chosen) >= limit * 2:
            break

    # 长短版本同时出现时只留长的（「你拿什么跟我玩」比「你拿什么」完整）
    chosen = [
        (key, item)
        for key, item in chosen
        if not any(key != other and key in other for other, _ in chosen)
    ]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, item in chosen:
        videos = len(item["videos"])
        text = item["display"].most_common(1)[0][0]
        fp = fingerprint("catchphrase", text)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(
            {
                "kind": "catchphrase",
                "text": text[:40],
                "meaning": "",
                "evidence": json.dumps(
                    {"videos": videos, "hits": item["hits"]}, ensure_ascii=False
                ),
                "bvid": item["bvid"],
                "rpid": 0,
                "url": (
                    f"https://www.bilibili.com/video/{item['bvid']}/"
                    if item["bvid"]
                    else ""
                ),
                "score": round(videos * 2.0 + item["hits"] ** 0.5, 3),
                "seen_videos": videos,
                "seen_users": 0,
                "fingerprint": fp,
            }
        )
        if len(out) >= limit:
            break
    return out
