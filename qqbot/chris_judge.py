"""用模型给评论打「好笑」分，产出 joke 候选。

可选步骤，跑一次花几分钱：先把高赞评论和重复梗收进来，按 40 条一批让模型
挑「群友愿意复制/转发的克里斯笑话」，返回 1~5 分和类型（反讽/故事/短句/口号）。
不接聊天、不自动进 knowledge；结果仍是 candidate，pack 才用。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI

from .chris_crawler import ChrisStore
from .chris_distill import clean, fingerprint, load_rows, usable
from .config import Config

log = logging.getLogger(__name__)

BATCH = 40
MAX_CHARS = 160

SYSTEM = """你是 B 站「克里斯」相关视频评论区的梗鉴赏器。判断一条评论是不是
「群友愿意复制、转发、当笑话讲」的克里斯笑话/梗。

算：
- 反讽、夸张、反转、自嘲、模仿主播或粉丝口气；
- 有具体画面/故事感的长 copypasta；
- 短但 punchline 清楚的句子。

不算：
- 纯提问、求推荐、攻略、报点、游戏机制讨论；
- 单纯夸操作、无意义应和、广告、骂人；
- 只是把主播名字塞进去的普通陈述。

宁缺毋滥。只返回严格 JSON：
{"jokes":[{"id":编号,"score":1-5,"type":"反讽|故事|短句|口号"}]}
"""

EXAMPLES = """参考（算）：
- 孩子们，我要说一个-4000万的故事…而我知道，他只是一条惊天大区
- 别让克区知道你是烂仔
- 帽子在谁脑袋上谁就是克里斯
- 四套送得快，五套送得稳，六套送得大
- 啊？克里斯是女的？

参考（不算）：
- 建议官方加一个可以加这个撤离点同撤特遣好友的功能
- GS2是不是有bug，人都跑我脸上了都没听见声音
- 这游戏声音系统就是有问题，观战队友听的比你远
"""


def collect(store: ChrisStore, min_like: int = 20, limit: int = 1500) -> list[dict[str, Any]]:
    rows = load_rows(store)
    picked: dict[str, dict[str, Any]] = {}
    for row in rows:
        text = clean(row.get("message"))
        if not usable(text) or len(text) > MAX_CHARS:
            continue
        if text in picked:
            continue
        like = int(row.get("like") or 0)
        if like < min_like:
            continue
        picked[text] = {"text": text, "like": like, "bvid": row["bvid"], "rpid": row["rpid"]}

    # 多人重复的梗即使赞不多也可能是笑话，一并送审
    for item in store.list_candidates(kind="meme", limit=200):
        text = str(item["text"])
        if text in picked or len(text) > MAX_CHARS or not usable(text):
            continue
        picked[text] = {"text": text, "like": 0, "bvid": item.get("bvid", ""), "rpid": item.get("rpid", 0)}

    out = list(picked.values())[:limit]
    return out


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def judge_jokes(
    store: ChrisStore,
    config: Config,
    min_like: int = 20,
    limit: int = 1500,
    batch: int = BATCH,
    client: AsyncOpenAI | None = None,
) -> dict[str, Any]:
    items = collect(store, min_like=min_like, limit=limit)
    if not items:
        return {"ok": False, "error": "没有可送审的评论"}
    own = client is None
    api = client or AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
    found: dict[int, dict[str, Any]] = {}
    try:
        for start in range(0, len(items), batch):
            chunk = items[start : start + batch]
            listing = "\n".join(
                f"{start + i}. {item['text']}" for i, item in enumerate(chunk)
            )
            messages = [
                {"role": "system", "content": SYSTEM + "\n" + EXAMPLES},
                {"role": "user", "content": f"评论：\n{listing}\n\n只返回 JSON。"},
            ]
            try:
                resp = await api.chat.completions.create(
                    model=config.model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=1600,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                raw = resp.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001 - 单批失败跳过
                log.warning("judge 批失败：%s", type(exc).__name__)
                continue
            data = _parse_json(raw)
            for entry in data.get("jokes") or []:
                if not isinstance(entry, dict):
                    continue
                idx = entry.get("id")
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                score = entry.get("score", 0)
                try:
                    score = int(score)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(items) and 1 <= score <= 5:
                    found[idx] = {
                        "score": score,
                        "type": str(entry.get("type") or "")[:8],
                    }
            await asyncio.sleep(0.2)
    finally:
        if own:
            await api.close()

    rows: list[dict[str, Any]] = []
    for idx, verdict in found.items():
        item = items[idx]
        text = item["text"]
        rows.append(
            {
                "kind": "joke",
                "text": text,
                "meaning": verdict["type"],
                "evidence": json.dumps(
                    {
                        "model": verdict["score"],
                        "type": verdict["type"],
                        "like": item["like"],
                    },
                    ensure_ascii=False,
                ),
                "bvid": item["bvid"],
                "rpid": int(item["rpid"] or 0),
                "url": (
                    f"https://www.bilibili.com/video/{item['bvid']}/#reply{item['rpid']}"
                    if item["bvid"] and item["rpid"]
                    else ""
                ),
                "score": round(verdict["score"] * 100 + min(item["like"], 99), 2),
                "seen_videos": 1,
                "seen_users": 1,
                "fingerprint": fingerprint("joke", text),
            }
        )
    rows.sort(key=lambda r: -r["score"])
    store.upsert_candidates(rows)
    fps = {r["fingerprint"] for r in rows}
    if fps:
        marks = ",".join("?" * len(fps))
        store.db.execute(
            f"DELETE FROM candidate WHERE kind='joke' AND status='candidate' "
            f"AND fingerprint NOT IN ({marks})",
            list(fps),
        )
    else:
        store.db.execute("DELETE FROM candidate WHERE kind='joke' AND status='candidate'")
    store.db.commit()
    return {"ok": True, "sent": len(items), "picked": len(rows)}
