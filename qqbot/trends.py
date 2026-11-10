"""热梗 / 热点知识库：每天抓一次，群里聊到相关话题时递一小抄。

数据源只留两路（都实测稳定、都带简介）：

- 贴吧热议：https://tieba.baidu.com/hottopic/browse/topicList
- B 站热门：https://api.bilibili.com/x/web-interface/popular

百度热搜默认不抓：风控凶、只有标题。这里不做模型调用，也不主动发消息；
只有群/私聊里出现「什么梗 / 最近火 / 吃瓜」这类触发词时，才把最新几条
标题+简介拼成 ≤480 字的小抄，挂在**本轮 user 消息末尾**（不碰 system 前缀，
否则 DeepSeek 的前缀缓存会整段失效）。

设计边界：

- 采集失败只记日志，不影响聊天；连续失败 5 次的源自动跳过。
- 解析到 0 条就当失败，绝不静默写空库（否则会以为在更新、其实早坏了）。
- 卡片 7 天保鲜；最多 300 条；按「热度 + 新鲜度」淘汰。
- 小抄只是标题和简介，提示词里明确要求：拿不准就联网核实，别顺着标题编。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SOURCE_URL = {
    "tieba": "https://tieba.baidu.com/hottopic/browse/topicList",
    "bilibili": "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
}
SOURCE_REFERER = {
    "tieba": "https://tieba.baidu.com/",
    "bilibili": "https://www.bilibili.com/",
}
SOURCE_WEIGHT = {"tieba": 1.25, "bilibili": 1.0}

# 同一句话的“指纹”：去空白、去标点，取前 40 字
_PUNCT_RE = re.compile(r"[\s\u3000，。！？、；：\"'“”‘’《》（）()【】\[\]…—\-~·!?,.;:]+")


def flatten(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def dedupe_key(title: str) -> str:
    text = _PUNCT_RE.sub("", flatten(title, 200).lower())
    return text[:40] if len(text) >= 2 else ""


def source_score(source: str, rank: int, desc: str) -> float:
    weight = SOURCE_WEIGHT.get(source, 1.0)
    rank = max(1, int(rank or 99))
    desc_bonus = min(0.35, len(desc or "") / 600)
    return round(weight * (1 / math.log2(rank + 1)) + desc_bonus, 3)


def parse_tieba(payload: Any, limit: int = 20) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    topic = data.get("bang_topic") if isinstance(data, dict) else None
    items = topic.get("topic_list") if isinstance(topic, dict) else None
    if not isinstance(items, list) or not items:
        raise RuntimeError("贴吧返回空列表")
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(items[:limit], 1):
        if not isinstance(item, dict):
            continue
        title = flatten(html.unescape(str(item.get("topic_name") or "")), 120)
        if not title:
            continue
        desc = flatten(
            html.unescape(str(item.get("topic_desc") or item.get("abstract") or "")), 260
        )
        url = flatten(html.unescape(str(item.get("topic_url") or "")), 500)
        out.append(
            {
                "title": title,
                "desc": desc,
                "url": url,
                "source": "tieba",
                "rank": int(item.get("idx_num") or rank),
            }
        )
    if not out:
        raise RuntimeError("贴吧没有可用条目")
    return out


def parse_bilibili(payload: Any, limit: int = 20) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("B站返回不是 JSON 对象")
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"B站返回 code={payload.get('code')}")
    data = payload.get("data")
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise RuntimeError("B站返回空列表")
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(items[:limit], 1):
        if not isinstance(item, dict):
            continue
        title = flatten(html.unescape(str(item.get("title") or "")), 120)
        if not title:
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        up = flatten(str(owner.get("name") or ""), 40)
        desc = flatten(html.unescape(str(item.get("desc") or "")), 220)
        if up and up not in desc:
            desc = f"UP:{up} {desc}".strip()
        url = flatten(html.unescape(str(item.get("short_link_v2") or "")), 500)
        if not url and item.get("bvid"):
            url = f"https://www.bilibili.com/video/{item.get('bvid')}"
        out.append(
            {
                "title": title,
                "desc": desc,
                "url": url,
                "source": "bilibili",
                "rank": rank,
            }
        )
    if not out:
        raise RuntimeError("B站没有可用条目")
    return out


FetchJson = Callable[[str, str], Awaitable[Any]]


class HotTrends:
    """热梗知识库。采集、保鲜、检索都在本地完成，不调模型。"""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        collect_hour: int = 6,
        check_minutes: int = 15,
        keep_days: int = 7,
        max_topics: int = 300,
        max_chars: int = 480,
        trigger_words: list[str] | None = None,
        fetcher: FetchJson | None = None,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.collect_hour = max(0, min(23, int(collect_hour)))
        self.check_minutes = max(1, int(check_minutes))
        self.keep_days = max(1, int(keep_days))
        self.max_topics = max(20, int(max_topics))
        self.max_chars = max(100, int(max_chars))
        self.trigger_words = [w for w in (trigger_words or []) if w]
        self.fetcher = fetcher
        self._lock = asyncio.Lock()
        self._store = self._load()

    # ---------- 持久化 ----------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": 0.0,
            "last_collected_at": 0.0,
            "last_collected_day": "",
            "last_attempt_day": "",
            "attempts_today": 0,
            "topics": [],
            "sources": {
                key: {"ok": 0, "fail": 0, "streak": 0, "last_error": "", "last_ok_at": 0.0}
                for key in SOURCE_URL
            },
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            base = self._empty()
            base.update({k: v for k, v in data.items() if k in base})
            if not isinstance(base.get("topics"), list):
                base["topics"] = []
            if not isinstance(base.get("sources"), dict):
                base["sources"] = self._empty()["sources"]
            return base
        except (OSError, ValueError, TypeError) as exc:
            log.warning("热梗库读取失败，重建空库：%s", exc)
            try:
                self.path.replace(self.path.with_name(f"{self.path.name}.bad-{int(time.time())}"))
            except OSError:
                pass
            return self._empty()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._store, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("热梗库写入失败：%s", exc)

    # ---------- 抓取 ----------

    async def _get_json(self, url: str, referer: str) -> Any:
        if self.fetcher is not None:
            return await self.fetcher(url, referer)
        headers = dict(BASE_HEADERS)
        if referer:
            headers["Referer"] = referer
        async with httpx.AsyncClient(
            timeout=12.0, headers=headers, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
            if text.lstrip().startswith("<"):
                raise RuntimeError("被风控返回了 HTML 验证页")
            return resp.json()

    async def _fetch_source(self, key: str) -> list[dict[str, Any]]:
        payload = await self._get_json(SOURCE_URL[key], SOURCE_REFERER[key])
        if key == "tieba":
            return parse_tieba(payload, 20)
        return parse_bilibili(payload, 20)

    # ---------- 合并 / 保鲜 ----------

    @staticmethod
    def _topic_score(item: dict[str, Any]) -> float:
        return source_score(
            str(item.get("source") or ""), int(item.get("rank") or 99), str(item.get("desc") or "")
        )

    def _merge(self, incoming: list[dict[str, Any]], now: float) -> None:
        by_key: dict[str, dict[str, Any]] = {}
        for topic in self._store.get("topics", []):
            if isinstance(topic, dict) and topic.get("key"):
                by_key[str(topic["key"])] = topic
        for item in incoming:
            key = dedupe_key(str(item.get("title") or ""))
            if not key:
                continue
            score = self._topic_score(item)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = {
                    "key": key,
                    "title": item["title"],
                    "desc": item.get("desc", ""),
                    "url": item.get("url", ""),
                    "sources": [item["source"]],
                    "rank": {item["source"]: item["rank"]},
                    "score": score,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "seen": 1,
                }
                continue
            existing["last_seen_at"] = now
            existing["seen"] = int(existing.get("seen") or 0) + 1
            existing["score"] = max(float(existing.get("score") or 0), score)
            sources = existing.setdefault("sources", [])
            if item["source"] not in sources:
                sources.append(item["source"])
            ranks = existing.setdefault("rank", {})
            if isinstance(ranks, dict):
                ranks[item["source"]] = item["rank"]
            # 只有更长的简介才替换，避免短简介把贴吧那段好用的说明冲掉
            if len(str(item.get("desc") or "")) >= 8 and len(
                str(item.get("desc") or "")
            ) > len(str(existing.get("desc") or "")):
                existing["title"] = item["title"]
                existing["desc"] = item.get("desc", "")
                existing["url"] = item.get("url", "")
        self._store["topics"] = list(by_key.values())

    def _prune(self, now: float) -> None:
        keep = self.keep_days * 86400
        alive = [
            t
            for t in self._store.get("topics", [])
            if isinstance(t, dict)
            and now - float(t.get("last_seen_at") or t.get("first_seen_at") or now) <= keep
        ]
        if len(alive) > self.max_topics:
            alive.sort(
                key=lambda t: (
                    float(t.get("score") or 0)
                    + 1 / (1 + max(0.0, now - float(t.get("last_seen_at") or now)) / 86400)
                ),
                reverse=True,
            )
            alive = alive[: self.max_topics]
        self._store["topics"] = alive

    # ---------- 采集 ----------

    async def collect(self) -> dict[str, Any]:
        """抓一次。失败只写日志；返回逐源结果，供状态查看。"""
        if not self.enabled:
            return {"enabled": False}
        if self._lock.locked():
            return {"skipped": "上一轮还在跑"}
        async with self._lock:
            now = time.time()
            day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            if self._store.get("last_attempt_day") != day:
                self._store["last_attempt_day"] = day
                self._store["attempts_today"] = 0
                for item in (self._store.get("sources") or {}).values():
                    if isinstance(item, dict):
                        item["streak"] = 0
            self._store["attempts_today"] = int(self._store.get("attempts_today") or 0) + 1
            incoming: list[dict[str, Any]] = []
            report: dict[str, Any] = {}
            sources = self._store.setdefault("sources", {})
            for key in ("tieba", "bilibili"):
                health = sources.setdefault(
                    key, {"ok": 0, "fail": 0, "streak": 0, "last_error": "", "last_ok_at": 0.0}
                )
                if int(health.get("streak") or 0) >= 5:
                    report[key] = "skipped(连续失败)"
                    continue
                try:
                    items = await self._fetch_source(key)
                    health["ok"] = int(health.get("ok") or 0) + 1
                    health["streak"] = 0
                    health["last_error"] = ""
                    health["last_ok_at"] = now
                    health["last_count"] = len(items)
                    incoming.extend(items)
                    report[key] = f"ok {len(items)}"
                except Exception as exc:  # noqa: BLE001
                    health["fail"] = int(health.get("fail") or 0) + 1
                    health["streak"] = int(health.get("streak") or 0) + 1
                    health["last_error"] = str(exc)[:160]
                    report[key] = f"fail {type(exc).__name__}: {str(exc)[:80]}"
                    log.warning("热梗源 %s 抓取失败：%s", key, exc)
            if incoming:
                self._merge(incoming, now)
                self._prune(now)
                self._store["last_collected_at"] = now
                self._store["last_collected_day"] = day
            else:
                log.warning("热梗库这一轮没有拿到任何条目，稍后再试")
            self._store["updated_at"] = now
            self._save()
            log.info("热梗库更新：%s；现有 %d 条", report, len(self._store.get("topics", [])))
            return report

    async def maybe_collect(self, now: datetime | None = None) -> bool:
        """到点且今天还没抓过才抓。启动补抓也走这里。"""
        if not self.enabled:
            return False
        now = now or datetime.now()
        if now.hour < self.collect_hour:
            return False
        day = now.strftime("%Y-%m-%d")
        if self._store.get("last_collected_day") == day:
            return False
        if self._store.get("last_attempt_day") != day:
            # 新的一天：把昨天的失败计数和连续失败清掉，重新给两次机会
            self._store["last_attempt_day"] = day
            self._store["attempts_today"] = 0
            for item in (self._store.get("sources") or {}).values():
                if isinstance(item, dict):
                    item["streak"] = 0
        if int(self._store.get("attempts_today") or 0) >= 3:
            return False
        await self.collect()
        return True

    # ---------- 小抄 ----------

    def _fresh(self, now: float) -> list[dict[str, Any]]:
        keep = self.keep_days * 86400
        rows = [
            t
            for t in self._store.get("topics", [])
            if isinstance(t, dict)
            and now - float(t.get("last_seen_at") or t.get("first_seen_at") or now) <= keep
        ]
        rows.sort(
            key=lambda t: (
                float(t.get("last_seen_at") or 0),
                float(t.get("score") or 0),
            ),
            reverse=True,
        )
        return rows

    def brief(self, text: str) -> str:
        """命中触发词时返回一小段热点；否则空串（不占 token）。"""
        if not self.enabled or not text or not self.trigger_words:
            return ""
        lowered = str(text).lower()
        if not any(word.lower() in lowered for word in self.trigger_words):
            return ""
        now = time.time()
        rows = self._fresh(now)
        if not rows:
            return ""
        updated = datetime.fromtimestamp(
            float(self._store.get("last_collected_at") or now)
        ).strftime("%m-%d %H:%M")
        header = f"【最近网上的热点（本地知识库，{updated} 更新）】"
        footer = (
            "（括号里是早上整理的一句话解释，不一定准，拿不准就联网核实，别顺着标题编；"
            "别人闲聊时不要主动蹦热搜。）"
        )
        lines = [header]
        used = len(header) + len(footer)
        for topic in rows[:5]:
            title = flatten(topic.get("title"), 60)
            explain = self._explain_of(topic, now)
            desc = "" if explain else flatten(topic.get("desc"), 40)
            line = f"· {title}" + (f"｜{desc}" if desc else "")
            if explain:
                line += f"（{explain}）"
            if used + len(line) > self.max_chars:
                break
            lines.append(line)
            used += len(line)
        if len(lines) == 1:
            return ""
        lines.append(footer)
        return "\n".join(lines)[: self.max_chars]

    def _explain_of(self, topic: dict[str, Any], now: float) -> str:
        """这条卡片上「为什么火」的一句话解释，过期就当没有。"""
        text = flatten(topic.get("explain"), 50)
        at = float(topic.get("explain_at") or 0)
        if not text or not at or now - at > self.keep_days * 86400:
            return ""
        return text

    def apply_explanations(
        self,
        items: dict[int, str],
        topics: list[dict[str, Any]] | None = None,
        now: float | None = None,
    ) -> int:
        """把模型写的一句话解释写回卡片。

        `items` 是 {第几条(1-based): 解释}，序号对应 `topics` 的顺序
        （默认就是 `brief()` 用的那批）。写回 N 条，返回 N。
        """
        now = time.time() if now is None else float(now)
        rows = topics if topics is not None else self._fresh(now)
        by_key = {
            str(t.get("key")): t
            for t in self._store.get("topics", [])
            if isinstance(t, dict) and t.get("key")
        }
        count = 0
        for idx, text in items.items():
            try:
                pos = int(idx)
            except (TypeError, ValueError):
                continue
            if not (1 <= pos <= len(rows)):
                continue
            card = by_key.get(str(rows[pos - 1].get("key")))
            body = flatten(text, 60)
            if card is None or not body or body in ("不清楚", "不确定", "不知道"):
                continue
            card["explain"] = body
            card["explain_at"] = now
            count += 1
        if count:
            self._store["last_explain_day"] = datetime.fromtimestamp(now).strftime(
                "%Y-%m-%d"
            )
            self._save()
        return count

    def explained_today(self, now: float | None = None) -> bool:
        day = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")
        return self._store.get("last_explain_day") == day

    def status(self) -> dict[str, Any]:
        rows = self._fresh(time.time())
        return {
            "enabled": self.enabled,
            "topics": len(self._store.get("topics", [])),
            "fresh": len(rows),
            "explained": sum(1 for t in rows if t.get("explain")),
            "last_collected_day": self._store.get("last_collected_day", ""),
            "last_collected_at": self._store.get("last_collected_at", 0),
            "last_explain_day": self._store.get("last_explain_day", ""),
            "sources": self._store.get("sources", {}),
        }

    def top(self, limit: int = 10) -> list[dict[str, Any]]:
        """按新鲜度/热度取前几条，供 CLI 或调试查看。"""
        return self._fresh(time.time())[: max(0, int(limit))]
