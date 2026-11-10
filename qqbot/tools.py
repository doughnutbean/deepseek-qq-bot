"""DeepSeek 的 Search / Fetch 工具。

注意这不是 chat/completions 的一部分，是独立的两个 HTTP 接口：
    POST /v1/tools/search   {query, count, ori_question, need_content}
    POST /v1/tools/fetch    {url}
计费按「成功请求次数」：5 元 / 1000 次，搜不到结果不计费。
所以只有真正拿到结果才往账本里记一笔；调用方通过 charges 传入计数器。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .linkcard import is_bili

log = logging.getLogger(__name__)

# 给模型看的搜索结果最多这么长，避免把上下文撑爆。
# 一轮回复里搜索结果是整段塞进 prompt 的，而且工具循环会再发一遍，按字算钱。
MAX_TOOL_OUTPUT = 1500

# 抓网页正文可以长一点 —— 那是模型/用户明确要看的内容，不是随手搜的摘要
MAX_FETCH_OUTPUT = 4000

# 查询里出现这些词，说明"上次查的"可能已经过时，再久就别拿旧答案糊弄
FRESH_WORDS = (
    "最新", "今天", "昨天", "现在", "目前", "实时", "刚刚", "本周", "这周",
    "最近", "价格", "多少钱", "涨价", "降价", "版本", "更新", "比分", "赛程",
    "天气", "股价", "汇率", "上映", "发售", "票房", "新闻",
)


def _needs_fresh(query: str) -> bool:
    return any(word in query for word in FRESH_WORDS)


def _fmt_date(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw) < 10:
        return ""
    return raw[:10]


class SearchTools:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        knowledge: Any = None,
        knowledge_ttl_days: int = 7,
        reuse_minutes: float = 120.0,
        chris_db: str = "",
        memory_db: str = "",
    ) -> None:
        self.api_key = api_key
        base = base_url.rstrip("/")
        # 官方 base_url 既可写 https://api.deepseek.com，也可写 .../v1；
        # Search/Fetch 的路径本身带 /v1，这里统一去掉尾巴，避免 /v1/v1/...
        if base.endswith("/v1"):
            base = base[:-3]
        self.base_url = base
        self.timeout = timeout
        # 搜索结果缓存（Memory.add_knowledge）。可以为 None：命令行/测试不需要落库。
        self.knowledge = knowledge
        self.knowledge_ttl_days = knowledge_ttl_days
        # 多久以内查过的问题可以直接复用（0 = 每次真搜），时效性查询例外
        self.reuse_minutes = max(0.0, float(reuse_minutes))
        self._client: httpx.AsyncClient | None = None
        # B 站链接的专用抓取（app 在初始化后挂上来）：能拿总结/字幕/热评，还免费
        self.bili: Any = None
        # 这个会话最近聊到的 UP 名（app 每轮更新）：给「他的切片」补代词用
        self.bili_context: str = ""
        # 克里斯本地语料库（data/chris_comments.db）：口头禅/梗/笑话，0 token
        self.chris_db = str(chris_db or "")
        # 记忆库（data/memory.db）：只用来记「这条梗最近讲过没」
        self.memory_db = str(memory_db or "")
        # 群梗表（app 启动后挂上来）：把「群里都管他叫区」一起带出来
        self.memes: Any = None
        # 用法画像表（qqbot/usagebook.py）：查「这句怎么用」
        self.usage_book: Any = None
        self.chris_group: str = ""
        # 克里斯笑话反馈账（qqbot/chris_feedback.py）：挑完告诉她哪条刚讲，
        # 下一轮才把群友的反应折成排序权重。可以为 None（命令行/测试）。
        self.chris_feedback: Any = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------- 给模型用的工具定义 ----------

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "搜索互联网，获取实时信息。凡是涉及新闻、价格、版本号、"
                        "赛事、天气、最近发生的事，或者你拿不准的事实，都先搜一下再回答；"
                        "不确定就先搜，别凭印象编。只找 B 站视频或 UP 主时改用 "
                        "bili_search（站内、免费、更准）。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索词，1~128 字。支持 site: 限定站点、after:2025 限定时间等语法。",
                            },
                            "count": {
                                "type": "integer",
                                "description": "返回条数，1~50，默认 3（官方接口上限 50）。",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": (
                        "抓取指定网页的正文（Markdown）。当搜索结果的摘要不够用时，"
                        "用它打开具体网页。B 站视频链接会走专用接口，能拿到 AI 总结、"
                        "字幕和热评，比普通网页抓取全。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "要抓取的 http/https 网址"}
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "bili_search",
                    "description": (
                        "在 B 站站内搜索视频或 UP 主（本地接口，免费）。话题只要沾 B 站"
                        "（B站、UP主、切片、投稿、直播、番剧、鬼畜、BV号），或者你想找"
                        "某个视频/梗的出处，就先用它搜；不确定搜视频还是 UP 主就用 auto。"
                        "query 只写名字或主题，别把整句请求塞进来（「看看克里斯最新"
                        "切片」就写「克里斯」）。问某个 UP 最新/最近发了什么，用 "
                        "up=UP名 + order=new：它会读那个 UP 自己的投稿列表，比按标题"
                        "搜准；只写站点不写 UP 名的主题（「影之刃零 实机」）才用 "
                        "query，默认按热度/相关度排，用户没强调「最新」不要传 "
                        "order=new。站内搜不花钱，别省着。其他网站、新闻、"
                        "事实用 web_search。"
                        "搜到后要判断视频里讲了什么，再用 web_fetch 打开 "
                        "https://www.bilibili.com/video/<BV号> 看 AI 总结/字幕。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "搜索词，1~60 字；只写 UP 名/主题，别写整句请求"
                                ),
                            },
                            "kind": {
                                "type": "string",
                                "enum": ["auto", "video", "user"],
                                "description": (
                                    "搜什么：auto（默认，按词判断，判断不出就视频和 UP 主"
                                    "都搜）、video 只搜视频、user 只搜 UP 主"
                                ),
                            },
                            "count": {
                                "type": "integer",
                                "description": "返回几条，1~10，默认 5",
                            },
                            "order": {
                                "type": "string",
                                "enum": ["relevant", "new"],
                                "description": (
                                    "relevant 默认综合排序（热度/相关度），大多数搜索"
                                    "用这个；new 只在用户明确强调「最新/最近/上场/"
                                    "今天」时用，和 up 一起用时直接读那个 UP 的投稿列表"
                                ),
                            },
                            "up": {
                                "type": "string",
                                "description": (
                                    "只看这个 UP 的投稿，填 UP 名或 UID（可选）。"
                                    "问「他最新发了什么」时优先用它，别把主题塞进 query"
                                ),
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "chris_lore",
                    "description": (
                        "查本地克里斯（Chris1i / 克区 / 克神 / 克宝）语料库：他本人的"
                        "口头禅、粉丝之间的梗、评论区笑话和说法。本地 SQLite，0 token 秒回。"
                        "只要问题提到克里斯，或者问「来点克里斯笑话」「克里斯的口头禅」"
                        "「粉丝之间有什么梗」「克里斯是区吗」，先调它；不要在网页搜"
                        "「主播口头禅」，网上没有。返回的梗可以直接讲，不用逐条核对。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "要查的词或原问法，1~60 字",
                            },
                            "kind": {
                                "type": "string",
                                "enum": ["auto", "joke", "meme", "catchphrase", "fact"],
                                "description": (
                                    "auto 按问法判断；joke 笑话/段子；meme 多人重复梗；"
                                    "catchphrase 他本人口头禅；fact 粉丝说法/称呼"
                                ),
                            },
                            "count": {
                                "type": "integer",
                                "description": "返回几条，1~15，默认 8",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "meme_usage",
                    "description": (
                        "查群里一个说法**怎么用**：它一般在什么场合出现、别人通常怎么接、"
                        "有没有真实用例。群里冒出没见过的说法、拿不准该不该接、"
                        "或者不知道怎么接时先查一下，别硬猜。本地表、0 token 秒回。"
                        "返回「没查到」就按没把握处理：宁可短一句，也别装懂。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "term": {
                                "type": "string",
                                "description": "要查的说法，2~20 字，照原样抄，别改写",
                            },
                            "group": {
                                "type": "string",
                                "description": "群号（可省；省了就用当前群的画像）",
                            },
                        },
                        "required": ["term"],
                    },
                },
            },
        ]

    # ---------- 实际执行 ----------

    def _cached(self, query: str) -> str | None:
        """同一个问题查过就不再花那 5 厘钱。

        只认**同一个问题**（归一化后问句一样，或话题词高度重合），不认"共用一个
        词"：2026-10-26 实测「突然的陀螺 B站 视频」命中了 B 站 UP 主主页（共用
        「视频」），「一个身份证可以实名认证几个QQ号」也命中同一条（共用「认证」），
        于是把别人的答案当成这次的结果。宁可多花 5 厘，也别串答案。

        时效性强的查询（价格/版本/天气…）超过 reuse_minutes 就不复用，宁可重新搜。
        """
        if self.knowledge is None:
            return None
        try:
            hit = self.knowledge.knowledge_lookup(query, strict=True)
        except Exception:  # noqa: BLE001 - 缓存读坏了不能挡住正常搜索
            log.debug("搜索缓存读取失败（照常联网）", exc_info=True)
            return None
        if not hit:
            return None
        ts = float(hit.get("ts") or 0)
        age_min = (time.time() - ts) / 60 if ts else 1e9
        if age_min > self.reuse_minutes and _needs_fresh(query):
            log.info("缓存里这个答案可能过时了（%.0f 分钟前），重新搜：%s", age_min, query)
            return None
        when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "之前"
        stale = "，可能过时" if age_min > self.reuse_minutes else ""
        log.info("搜索命中本地缓存（%s，没花搜索费）：%s", when, query)
        lines = [
            f"搜索「{query}」的结果（{when} 查到过{stale}，直接复用）：",
            str(hit.get("text") or ""),
        ]
        if hit.get("url"):
            lines.append(f"来源：{hit['url']}")
        lines.append("（链接只给你自己看和抓网页用；回复里别贴网址，说「据某某网」就行）")
        return "\n".join(lines)

    async def search(
        self,
        query: str,
        count: int = 3,
        ori_question: str = "",
        charges: dict[str, int] | None = None,
    ) -> str:
        query = query.strip()[:128]
        if not query:
            return "（搜索词为空）"
        # 查过的同一个问题不再花钱：只有"真的在说同一件事"才复用。这种复用
        # 只是省钱，不能改变答案 —— 宁可多花 5 厘，也别把别的搜索结果串进来。
        cached = self._cached(query)
        if cached is not None:
            return cached
        payload: dict[str, Any] = {
            "query": query,
            "count": max(1, min(int(count or 3), 50)),
            "need_content": False,
        }
        if ori_question:
            payload["ori_question"] = ori_question.strip()[:128]

        try:
            resp = await self._http().post(f"{self.base_url}/v1/tools/search", json=payload)
        except httpx.HTTPError as exc:
            log.warning("搜索请求失败: %s", exc)
            return f"（搜索失败：网络错误 {type(exc).__name__}）"

        if resp.status_code != 200:
            log.warning("搜索返回 %s: %.200s", resp.status_code, resp.text)
            if resp.status_code == 402:
                return "（搜索失败：账户余额不足）"
            if resp.status_code == 429:
                return "（搜索失败：请求太频繁，稍后再试）"
            return f"（搜索失败：HTTP {resp.status_code}）"

        try:
            results = resp.json().get("results") or []
        except ValueError:
            return "（搜索失败：返回内容不是 JSON）"

        if not results:
            return f"搜「{query}」没有任何结果。"

        # 官方计费规则：HTTP 200 且 results 非空才算一次成功请求。
        if charges is not None:
            charges["search"] = int(charges.get("search", 0)) + 1

        lines: list[str] = [f"搜索「{query}」的结果："]
        used = 0
        for i, item in enumerate(results, 1):
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            summary = (item.get("summary") or item.get("snippet") or "").strip()
            date = _fmt_date(item.get("date"))
            block = f"\n[{i}] {title}\n{url}"
            if date:
                block += f"\n发布时间：{date}"
            if summary:
                block += f"\n{summary}"
            if used + len(block) > MAX_TOOL_OUTPUT:
                break
            lines.append(block)
            used += len(block)

        lines.append("\n（链接只给你自己看和抓网页用；回复里别贴网址，说「据某某网」就行）")
        # 顺手缓存一条：下次同类问题可以先给"上次查到的"，省一次搜索。
        # 只存第一条结果的标题+摘要，不存整段返回，也不进长期记忆。
        self._remember(query, results)
        return "\n".join(lines)

    def _remember(self, query: str, results: list[Any]) -> None:
        if self.knowledge is None or not results or self.knowledge_ttl_days <= 0:
            return
        item = results[0] if isinstance(results[0], dict) else None
        if not item:
            return
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        summary = (item.get("summary") or item.get("snippet") or "").strip()
        if title and summary:
            claim = f"{title}：{summary}"
        else:
            claim = title or summary
        claim = claim.strip()[:300]
        if not claim:
            return
        try:
            self.knowledge.add_knowledge(
                query, claim, url=url, ttl_days=self.knowledge_ttl_days
            )
        except Exception:  # noqa: BLE001
            log.debug("搜索缓存写入失败（不影响本次回答）", exc_info=True)

    async def fetch(self, url: str, charges: dict[str, int] | None = None) -> str:
        url = url.strip()
        if not url:
            return "（抓取失败：网址为空）"
        if len(url) > 8192:
            return "（抓取失败：网址超过 8192 字符）"
        try:
            parts = urlsplit(url)
        except ValueError:
            return "（抓取失败：网址格式不对）"
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return "（抓取失败：只支持带 host 的 http/https 链接）"
        if parts.username or parts.password:
            return "（抓取失败：网址不能携带用户名或密码）"

        # B 站链接走本地档案：普通 fetch 只能拿到静态页的标题，评论/总结/字幕
        # 都在动态接口里（2026-10-26 实测她因此回答「本鱼抓不到」）。
        if self.bili is not None and is_bili(url):
            try:
                text = await self.bili.digest(url)
            except Exception:  # noqa: BLE001 - 档案出问题就退回普通抓取
                log.info("B 站档案抓取失败，退回普通抓取", exc_info=True)
                text = ""
            if text:
                log.info("抓取走 B 站档案：%s", url[:80])
                return text
        try:
            resp = await self._http().post(f"{self.base_url}/v1/tools/fetch", json={"url": url})
        except httpx.HTTPError as exc:
            log.warning("抓取请求失败: %s", exc)
            return f"（抓取失败：网络错误 {type(exc).__name__}）"

        if resp.status_code != 200:
            return f"（抓取失败：HTTP {resp.status_code}，可能是该网页拒绝抓取）"

        try:
            data = resp.json()
        except ValueError:
            return "（抓取失败：返回内容不是 JSON）"

        title = (data.get("title") or "").strip()
        content = (data.get("content") or "").strip()
        if not content:
            return "（抓取失败：页面没有可提取的正文）"

        # 和 Search 一样：只有 200 且真的抓到了正文才计费。
        if charges is not None:
            charges["fetch"] = int(charges.get("fetch", 0)) + 1

        body = content[: MAX_FETCH_OUTPUT - 200]
        return f"网页标题：{title}\n网址：{url}\n正文：\n{body}"

    async def bili_search(
        self,
        query: str,
        kind: str = "auto",
        count: int = 5,
        order: str = "",
        up: str = "",
    ) -> str:
        """B 站站内搜索（视频 / UP 主）。没接上时明确说，别让她以为搜过了。"""
        if self.bili is None:
            return "（B 站搜索没接上：搜索功能关着，或者卡片模块没启用）"
        return await self.bili.search_text(
            query,
            kind,
            count,
            context_name=self.bili_context,
            order=order,
            up=up,
        )

    def _lore_memory_path(self) -> Path:
        return Path(self.memory_db) if self.memory_db else Path("data/memory.db")

    def _lore_recent(self) -> set[int]:
        path = self._lore_memory_path()
        if not path.is_file():
            return set()
        try:
            db = sqlite3.connect(str(path))
            try:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS lore_use ("
                    "candidate_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, "
                    "hits INTEGER DEFAULT 1)"
                )
                db.commit()
                cutoff = int(time.time()) - 14 * 86400
                rows = db.execute(
                    "SELECT candidate_id FROM lore_use WHERE ts >= ?", (cutoff,)
                ).fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            return set()
        return {int(row[0]) for row in rows}

    def _lore_mark(self, ids: list[int]) -> None:
        path = self._lore_memory_path()
        if not ids:
            return
        try:
            db = sqlite3.connect(str(path))
            try:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS lore_use ("
                    "candidate_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, "
                    "hits INTEGER DEFAULT 1)"
                )
                now = int(time.time())
                db.executemany(
                    "INSERT INTO lore_use(candidate_id, ts, hits) VALUES (?, ?, 1) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET "
                    "ts=excluded.ts, hits=lore_use.hits+1",
                    [(int(cid), now) for cid in ids if int(cid) > 0],
                )
                db.commit()
            finally:
                db.close()
        except sqlite3.Error:
            log.debug("lore_use 记录失败（不影响回答）", exc_info=True)

    def meme_usage(self, term: str, group: str = "") -> str:
        """查一个说法怎么用：结论优先，没有结论给真实用例。

        只读 `UsageBook`（每天从群聊 + 字幕算出来的画像），0 token、不推理。
        查不到就明说，让她按「没把握」处理——不编。
        """
        term = str(term or "").strip()
        if not term:
            return "（要查的说法是空的）"
        book = getattr(self, "usage_book", None)
        if book is None:
            return "（本鱼这边的用法表没接上，别硬猜）"
        try:
            row = book.lookup(str(group or ""), term)
        except Exception:  # noqa: BLE001 - 画像坏了不能把查梗变成报错
            row = None
        if not row:
            return f"「{term}」：本鱼没查过这个说法，按没把握处理——短一句，别装懂。"
        scene = str(row.get("scene") or "").strip()
        if scene:
            return f"「{term}」：{scene}（群里统计出来的，别当铁律）"
        sample = ""
        try:
            sample = str(book.example_for(str(group or ""), term) or "")
        except Exception:  # noqa: BLE001
            sample = ""
        if sample:
            return (
                f"「{term}」：本鱼没总结出固定用法，但群里上次这么用——"
                f"{sample}（只是例子，不是现在有人这么说）"
            )
        return f"「{term}」：本鱼没查到用法，别装懂。"

    def chris_lore(
        self, query: str, kind: str = "auto", count: int = 8
    ) -> str:
        """查本地克里斯语料库（0 token）。找不到就明确说没有，别让她编。"""
        path = Path(self.chris_db) if self.chris_db else Path("data/chris_comments.db")
        if not path.is_file():
            return "（本地还没有克里斯语料库）"
        query = (query or "").strip()[:80]
        if not query:
            return "（查询词为空）"
        kind = str(kind or "auto").lower()
        if kind == "auto":
            if any(w in query for w in ("口头禅", "口癖", "常说")):
                kind = "catchphrase"
            elif any(w in query for w in ("笑话", "段子", "好笑")):
                kind = "joke"
            elif any(w in query for w in ("梗", "黑话", "粉丝")):
                kind = "meme"
            elif any(w in query for w in ("是谁", "是什么", "区吗", "女的")):
                kind = "fact"
            else:
                kind = "all"

        groups = {
            "joke": ("joke",),
            "meme": ("meme",),
            "catchphrase": ("catchphrase",),
            "fact": ("claim", "term"),
            "all": ("catchphrase", "term", "meme", "joke", "claim"),
        }
        kinds = groups.get(kind, groups["all"])
        marks = ",".join("?" * len(kinds))
        base = (
            "SELECT id, text, kind, status, pinned, score, seen_videos FROM candidate "
            f"WHERE kind IN ({marks})"
        )

        # 新一轮挑选：先把上轮攒的群友反馈折进权重，再拿这一轮的快照排序。
        # 只认「笑话」（kind=joke）：梗/口头禅走各自的统计，不在这里混。
        # 反馈只改**下一次**挑出来的顺序，这一轮已经递出去的几条不受影响。
        weights: dict[int, float] = {}
        feedback = getattr(self, "chris_feedback", None)
        if feedback is not None and "joke" in kinds:
            try:
                weights = feedback.begin_round() or {}
            except Exception:  # noqa: BLE001 - 反馈坏了不能挡住语料
                weights = {}
                log.debug("克里斯反馈权重取失败", exc_info=True)

        def fetch(terms: list[str]) -> list[sqlite3.Row]:
            sql = base
            params: list[Any] = list(kinds)
            if terms:
                sql += " AND (" + " OR ".join("text LIKE ?" for _ in terms) + ")"
                params += [f"%{term}%" for term in terms]
            # 整个候选池都取回来再排：权重能把一条低分梗顶上来，不能只按
            # 原始分截前 300 条（候选表现在也就几百条，排序是本地内存操作）。
            params.append(1000)
            sql += " ORDER BY score DESC LIMIT ?"
            db = sqlite3.connect(str(path))
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(sql, params).fetchall()
            finally:
                db.close()

            def rank(row: sqlite3.Row) -> tuple[Any, ...]:
                hits = sum(1 for term in terms if term in str(row["text"]))
                weight = float(weights.get(int(row["id"]), 1.0))
                return (
                    -hits,
                    0 if row["status"] == "approved" else 1,
                    0 if int(row["seen_videos"] or 0) > 0 else 1,
                    -int(row["pinned"] or 0),
                    -float(row["score"] or 0) * weight,
                )

            rows.sort(key=rank)
            return rows

        cleaned = re.sub(
            r"克里斯|chris\w*|克区|克神|克宝|来点|讲个|说个|一个|笑话|段子|"
            r"口头禅|口癖|梗|黑话|粉丝|之间|是什么|有什么|哪些|吗|呢|吧|的",
            " ",
            query,
            flags=re.IGNORECASE,
        )
        terms = [
            t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", cleaned)
            if t not in {"什么", "怎么", "这个", "那个"}
        ]
        rows = fetch(terms)
        if not rows and terms:
            rows = fetch([])
        if not rows:
            return "（本地语料库里没有这方面的条目；别硬编）"

        # 骂人/骚扰类只当语料：泛问「口头禅」不往外拿；明确问这一句才给，并加警告
        from .live_memes import classify_risk

        def harmful(row: sqlite3.Row) -> bool:
            try:
                return bool(classify_risk(str(row["text"])))
            except Exception:  # noqa: BLE001 - 风控挂了不能挡住语料
                return False

        if terms:
            rows = [
                row for row in rows
                if not harmful(row)
                or any(term in str(row["text"]) for term in terms)
            ]
        else:
            rows = [row for row in rows if not harmful(row)]

        used = self._lore_recent()
        if used:
            fresh = [row for row in rows if int(row["id"]) not in used]
            if fresh:
                rows = fresh
        rows = rows[: max(1, min(int(count or 8), 15))]
        self._lore_mark([int(row["id"]) for row in rows])
        # 记下这一轮挑给她的笑话：她讲完、群友接话时，反馈账只在这几条里认。
        if feedback is not None:
            try:
                feedback.note_offer(
                    getattr(self, "chris_group", ""),
                    [
                        (int(row["id"]), str(row["text"]))
                        for row in rows
                        if str(row["kind"]) == "joke"
                    ],
                )
            except Exception:  # noqa: BLE001 - 反馈记账失败不能挡住回答
                log.debug("克里斯反馈记录挑中条目失败", exc_info=True)

        label = {
            "catchphrase": "口头禅",
            "term": "叫法",
            "meme": "梗",
            "joke": "笑话",
            "claim": "说法",
        }
        header = "克里斯本地语料（本地库，可直接讲，不用再搜网页）："
        lines = [header]
        # 整条给全，但不让总长度把最后一条切成半句：超预算就少给几条。
        # 以前是先把整段 `[:MAX_TOOL_OUTPUT]` 硬切，第 8 条可能只剩前半个笑话。
        used = len(header)
        budget = MAX_TOOL_OUTPUT - 320  # 给「群里已有的叫法」留位置
        for row in rows:
            # 笑话最长 200 字（见 chris_distill.MAX_CHARS）；以前截 140 会把
            # 长笑话的包袱切掉，她只能讲半条或者干脆不讲。这里留够整条。
            limit = 200 if str(row["kind"]) == "joke" else 140
            text = str(row["text"]).replace("\n", " ")[:limit]
            if harmful(row):
                text += "（带攻击性，只当学舌，别对群友用）"
            extra = f"·{row['seen_videos']}个视频" if row["seen_videos"] else ""
            line = f"- [{label.get(row['kind'], row['kind'])}{extra}] {text}"
            if used + len(line) + 1 > budget:
                log.info("克里斯语料太长，这一轮少给几条（已给 %d 条）", len(lines) - 1)
                break
            lines.append(line)
            used += len(line) + 1

        # 把群里自己长出来的叫法并进来（「都管他叫区」这种 B 站评论里不一定有）
        book = getattr(self, "memes", None)
        if book is not None and any(
            word in query for word in ("克里斯", "克区", "克神", "克宝", "chris")
        ):
            try:
                note = str(
                    book.brief(getattr(self, "chris_group", ""), query) or ""
                ).strip()
            except Exception:  # noqa: BLE001 - 梗表读坏了不能挡住语料
                note = ""
            if note:
                lines.append("群里已有的叫法/规矩：" + note[:240])
        return "\n".join(lines)[:MAX_TOOL_OUTPUT]

    async def run(
        self,
        name: str,
        arguments: str,
        ori_question: str = "",
        turn: Any = None,
        **_kwargs: Any,
    ) -> str | None:
        """执行模型发起的工具调用。arguments 是 JSON 字符串。不是自己的工具就返回 None。"""
        if turn is not None:
            ctx = turn.as_ctx()
            ori_question = ctx.get("ori_question") or ori_question
            _kwargs = {**ctx, **_kwargs}
        import json

        if name not in {"web_search", "web_fetch", "bili_search", "chris_lore"}:
            return None

        # LLM 每轮会把这个 dict 传进来，成功一次就 +1，最后由账本统一算钱。
        # 单轨（2026-11-05）之后 llm 只传 turn，旧的 `_tool_charges` 不再单独传；
        # 计数器在 ctx.services 里叫 `tool_charges`，两种都认，别再丢账。
        charges = _kwargs.get("_tool_charges") or _kwargs.get("tool_charges")

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "（工具参数不是合法 JSON）"

        if name == "web_search":
            query = str(args.get("query", "")).strip()
            if not query:
                return "（搜索词为空）"
            result = await self.search(
                query, int(args.get("count", 3) or 3), ori_question, charges=charges
            )
            # 聊天里的联网搜索：把「可能是梗」的结果留给上层收候选，不阻塞回答。
            capture = _kwargs.get("meme_capture")
            if callable(capture):
                try:
                    capture(query, result)
                except Exception:  # noqa: BLE001 - 收候选失败不能影响聊天
                    log.debug("聊天搜索梗候选记录失败", exc_info=True)
            return result

        if name == "chris_lore":
            query = str(args.get("query", "")).strip()
            if not query:
                return "（查询词为空）"
            try:
                count = int(args.get("count", 8) or 8)
            except (TypeError, ValueError):
                count = 8
            return self.chris_lore(
                query, str(args.get("kind") or "auto"), count
            )

        if name == "meme_usage":
            term = str(args.get("term", "")).strip()
            if not term:
                return "（要查的说法是空的）"
            return self.meme_usage(term, str(args.get("group") or ""))

        if name == "bili_search":
            query = str(args.get("query", "")).strip()
            if not query:
                return "（搜索词为空）"
            try:
                count = int(args.get("count", 5) or 5)
            except (TypeError, ValueError):
                count = 5
            return await self.bili_search(
                query,
                str(args.get("kind") or "auto"),
                count=count,
                order=str(args.get("order") or ""),
                up=str(args.get("up") or ""),
            )

        url = str(args.get("url", "")).strip()
        if not url:
            return "（网址为空）"
        return await self.fetch(url, charges=charges)
