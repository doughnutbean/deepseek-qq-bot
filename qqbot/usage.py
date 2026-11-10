"""Token 用量与花费统计。

单独一个库（usage.db），跟记忆库分开 —— 关掉记忆也要能算钱。

价格按"每百万 token 多少元"配，默认值是 DeepSeek 官方的量级，
换模型/走中转站时在 .env 里改就行。

除了模型 token，联网搜索 / 抓网页也是花钱的（官方 5 元 / 1000 次成功请求），
这两项按次记进同一行：tool_cost / search_calls / fetch_calls。日报和
「今天花了多少」读的都是 cost（token + 工具费），和官网账单对得上。

`usage` 一行记一个回合：`requests` 是这一轮实际发了几次模型请求，`task` 是用途
（chat / proactive_judge / followup / qzone / proactive_dm）。

`usage_call` 一次模型请求一行：自己的 `ts_start/ts_end`、token、状态、延迟、
峰谷和金额。峰谷按**这次请求自己的开始时间**取价；`usage` 的 `cost` 是该回合
所有 `usage_call.cost` 的和。逐请求明细可以用 `tools/usage_cli.py --calls N` 看。

    from qqbot.usage import Usage
    usage.record("private:123", "deepseek-flash", prompt=1234, completion=56, cached=900)
    usage.record("group:456", "deepseek-flash", 0, 0, search_calls=2, fetch_calls=1)
    usage.summary(days=1)   # {'calls': 3, 'cost': 0.0142, ...}
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 图形工具调用（表情包逐图注解/审核）的 task 名；汇总时单独拆出来。
VISION_TASKS = ("sticker_annotate", "sticker_screen")


class Usage:
    def __init__(
        self,
        path: Path,
        price_input: float = 1.0,
        price_output: float = 4.0,
        price_cached: float = 0.02,
        price_peak_multiplier: float = 2.0,
        price_search_call: float = 0.005,
        price_fetch_call: float = 0.005,
        enabled: bool = True,
    ) -> None:
        self.path = path
        # 单位：元 / 百万 token。这里配的是**空闲时段**的价，高峰按倍率乘。
        self.price_input = price_input
        self.price_output = price_output
        self.price_cached = price_cached
        self.price_peak_multiplier = max(1.0, price_peak_multiplier)
        # Search / Fetch 按成功请求次数计费，单位：元 / 次（官方 5 元 / 1000 次）。
        self.price_search_call = max(0.0, float(price_search_call))
        self.price_fetch_call = max(0.0, float(price_fetch_call))
        self.enabled = enabled
        self._db: sqlite3.Connection | None = None
        if enabled:
            self._connect()

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 工具（表情包注解）和机器人会同时写 usage.db，等锁久一点。
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         INTEGER NOT NULL,
                ts_start   REAL,
                session    TEXT    NOT NULL,
                model      TEXT    NOT NULL,
                task       TEXT    NOT NULL DEFAULT 'chat',
                turn_id    TEXT,
                prompt     INTEGER NOT NULL,
                completion INTEGER NOT NULL,
                cached     INTEGER NOT NULL DEFAULT 0,
                cost       REAL    NOT NULL DEFAULT 0,
                tool_cost  REAL    NOT NULL DEFAULT 0,
                search_calls INTEGER NOT NULL DEFAULT 0,
                fetch_calls  INTEGER NOT NULL DEFAULT 0,
                requests     INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);

            -- 逐请求账本：一次模型请求一行，峰谷按这次请求自己的开始时间取价。
            CREATE TABLE IF NOT EXISTS usage_call (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id     TEXT    NOT NULL,
                ts_start    REAL    NOT NULL,
                ts_end      REAL    NOT NULL,
                session     TEXT    NOT NULL,
                task        TEXT    NOT NULL DEFAULT 'chat',
                model       TEXT    NOT NULL,
                prompt      INTEGER NOT NULL DEFAULT 0,
                completion  INTEGER NOT NULL DEFAULT 0,
                cached      INTEGER NOT NULL DEFAULT 0,
                reasoning   INTEGER NOT NULL DEFAULT 0,
                status      TEXT    NOT NULL DEFAULT 'ok',
                latency_ms  REAL,
                peak        INTEGER NOT NULL DEFAULT 0,
                cost        REAL    NOT NULL DEFAULT 0,
                price_version TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_usage_call_ts ON usage_call(ts_start);
            CREATE INDEX IF NOT EXISTS idx_usage_call_turn ON usage_call(turn_id);
            """
        )
        # 老库补列：usage.db 是长期账本，不能因为升级就丢了历史。
        have = {row["name"] for row in db.execute("PRAGMA table_info(usage)").fetchall()}
        for name, ddl in (
            ("tool_cost", "REAL NOT NULL DEFAULT 0"),
            ("search_calls", "INTEGER NOT NULL DEFAULT 0"),
            ("fetch_calls", "INTEGER NOT NULL DEFAULT 0"),
            ("ts_start", "REAL"),
            ("task", "TEXT NOT NULL DEFAULT 'chat'"),
            ("requests", "INTEGER NOT NULL DEFAULT 0"),
            ("turn_id", "TEXT"),
        ):
            if name not in have:
                db.execute(f"ALTER TABLE usage ADD COLUMN {name} {ddl}")
        # 历史回填：
        # - ts_start 没有就退回 ts（旧行只知道回合结束时间）；
        # - 有 token 的旧行至少发生过 1 次模型请求；纯搜索补记行保持 0；
        # - task 默认 chat；旧代码在迁移后新写的行也靠这一步补成 1。
        db.execute("UPDATE usage SET ts_start = ts WHERE ts_start IS NULL")
        db.execute(
            "UPDATE usage SET requests = 1 "
            "WHERE requests = 0 AND (prompt > 0 OR completion > 0)"
        )
        db.commit()
        self._db = db

    @property
    def db(self) -> sqlite3.Connection | None:
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ---------- 算钱 ----------

    @property
    def price_version(self) -> str:
        """写进逐请求账本的价格快照；以后改价，旧行还能按当时的价格审计。"""
        return (
            f"in={self.price_input:g},out={self.price_output:g},"
            f"cache={self.price_cached:g},peak={self.price_peak_multiplier:g}"
        )

    def cost_of(self, prompt: int, completion: int, cached: int = 0, peak: bool = False) -> float:
        """按百万 token 单价算这一笔多少钱。缓存命中的部分走缓存价。

        高峰时段整体乘以倍率（DeepSeek 官方：高峰价 = 空闲价 × 2）。
        """
        cached = max(0, min(cached, prompt))
        fresh = max(0, prompt - cached)
        cost = (
            fresh / 1_000_000 * self.price_input
            + cached / 1_000_000 * self.price_cached
            + completion / 1_000_000 * self.price_output
        )
        return cost * (self.price_peak_multiplier if peak else 1.0)

    def tool_cost_of(self, search_calls: int = 0, fetch_calls: int = 0) -> float:
        """Search / Fetch 的钱：按成功请求次数算，不乘峰谷倍率。"""
        return (
            max(0, int(search_calls)) * self.price_search_call
            + max(0, int(fetch_calls)) * self.price_fetch_call
        )

    def record_call(
        self,
        turn_id: str,
        session: str,
        model: str,
        prompt: int,
        completion: int,
        cached: int = 0,
        *,
        ts_start: float,
        ts_end: float,
        peak: bool = False,
        task: str = "chat",
        status: str = "ok",
        reasoning: int = 0,
        latency_ms: float | None = None,
    ) -> float:
        """记一次真实的模型请求，按**这次请求自己的**开始时间取峰谷价。

        没有 usage 的响应也记一行（`status=no_usage`，cost=0），不假装没发生；
        请求直接抛错、没拿到响应时没有 token 可记，不会凭空造一行。
        """
        prompt = max(0, int(prompt))
        completion = max(0, int(completion))
        cached = max(0, min(int(cached), prompt))
        cost = self.cost_of(prompt, completion, cached, peak)
        if not self.enabled or self._db is None:
            return cost
        self._db.execute(
            "INSERT INTO usage_call("
            "turn_id, ts_start, ts_end, session, task, model, prompt, completion, "
            "cached, reasoning, status, latency_ms, peak, cost, price_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(turn_id),
                float(ts_start),
                float(ts_end),
                str(session),
                (str(task or "chat")).strip() or "chat",
                str(model),
                prompt,
                completion,
                cached,
                max(0, int(reasoning or 0)),
                str(status or "ok"),
                None if latency_ms is None else float(latency_ms),
                1 if peak else 0,
                cost,
                self.price_version,
            ),
        )
        self._db.commit()
        return cost

    def record(
        self,
        session: str,
        model: str,
        prompt: int,
        completion: int,
        cached: int = 0,
        ts: float | None = None,
        peak: bool = False,
        search_calls: int = 0,
        fetch_calls: int = 0,
        ts_start: float | None = None,
        requests: int = 1,
        task: str = "chat",
        turn_id: str = "",
        token_cost: float | None = None,
    ) -> float:
        # token_cost 由调用方按逐请求账本求和后传进来；没有逐请求记录时
        # （旧测试/旧调用）才退回按回合聚合价计算。
        cost = (
            self.cost_of(prompt, completion, cached, peak)
            if token_cost is None
            else max(0.0, float(token_cost))
        )
        search_calls = max(0, int(search_calls))
        fetch_calls = max(0, int(fetch_calls))
        tool_cost = self.tool_cost_of(search_calls, fetch_calls)
        stamp = int(ts if ts is not None else time.time())
        start = int(ts_start if ts_start is not None else stamp)
        request_count = max(0, int(requests))
        task = (str(task or "chat")).strip() or "chat"
        if not self.enabled or self._db is None:
            return cost + tool_cost
        self._db.execute(
            "INSERT INTO usage(ts, ts_start, session, model, task, turn_id, prompt, "
            "completion, cached, cost, tool_cost, search_calls, fetch_calls, requests) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stamp, start, session, model, task, (str(turn_id).strip() or None),
             int(prompt), int(completion), int(cached), cost,
             tool_cost, search_calls, fetch_calls, request_count),
        )
        self._db.commit()
        return cost + tool_cost

    # ---------- 看账 ----------

    def summary_range(self, start: float, end: float) -> dict[str, Any]:
        """[start, end) 这一段的账。

        日报要的是**自然日**（昨天 00:00 到今天 00:00），不是"最近 24 小时"：
        用 summary(days=1) 的话，中午 12 点跑会把昨天中午到今天中午的账算进来，
        前半段昨天的、后半段今天的，对不上"昨天的开销"。
        """
        if not self.enabled or self._db is None:
            return {
                "calls": 0, "requests": 0, "cost": 0.0, "token_cost": 0.0, "tool_cost": 0.0,
                "prompt": 0, "completion": 0, "cached": 0,
                "search_calls": 0, "fetch_calls": 0, "tool_calls": 0,
                "vision_cost": 0.0, "vision_calls": 0, "vision_turns": 0,
                "chat_cost": 0.0,
            }
        row = self._db.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN prompt > 0 OR completion > 0
                                     THEN 1 ELSE 0 END), 0) AS calls,
                   COALESCE(SUM(requests), 0)   AS requests,
                   COALESCE(SUM(prompt), 0)     AS prompt,
                   COALESCE(SUM(completion), 0) AS completion,
                   COALESCE(SUM(cached), 0)     AS cached,
                   COALESCE(SUM(cost), 0)       AS token_cost,
                   COALESCE(SUM(tool_cost), 0)  AS tool_cost,
                   COALESCE(SUM(search_calls), 0) AS search_calls,
                   COALESCE(SUM(fetch_calls), 0)  AS fetch_calls,
                   COALESCE(SUM(CASE WHEN task IN ('sticker_annotate','sticker_screen')
                                     THEN cost + tool_cost ELSE 0 END), 0) AS vision_cost,
                   COALESCE(SUM(CASE WHEN task IN ('sticker_annotate','sticker_screen')
                                     THEN requests ELSE 0 END), 0) AS vision_calls,
                   COALESCE(SUM(CASE WHEN task IN ('sticker_annotate','sticker_screen')
                                     THEN 1 ELSE 0 END), 0) AS vision_turns
            FROM usage WHERE ts >= ? AND ts < ?
            """,
            (int(start), int(end)),
        ).fetchone()
        out = dict(row)
        out["token_cost"] = round(out["token_cost"], 6)
        out["tool_cost"] = round(out["tool_cost"], 6)
        out["vision_cost"] = round(out["vision_cost"], 6)
        out["vision_calls"] = int(out["vision_calls"])
        out["vision_turns"] = int(out["vision_turns"])
        # cost 是总花费：token + 搜索/抓取 + 图形工具。官网扣的就是这个数。
        out["cost"] = round(out["token_cost"] + out["tool_cost"], 6)
        out["chat_cost"] = round(out["cost"] - out["vision_cost"], 6)
        out["tool_calls"] = int(out["search_calls"]) + int(out["fetch_calls"])
        out["requests"] = int(out["requests"])
        return out

    def summary(self, days: float = 1) -> dict[str, Any]:
        now = time.time()
        return self.summary_range(now - days * 86400, now + 1)

    def by_model_range(self, start: float, end: float) -> list[dict[str, Any]]:
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            """
            SELECT model, COUNT(*) AS calls, SUM(requests) AS requests,
                   SUM(prompt) AS prompt, SUM(completion) AS completion,
                   SUM(cached) AS cached,
                   SUM(cost + tool_cost) AS cost, SUM(tool_cost) AS tool_cost,
                   SUM(search_calls) AS search_calls, SUM(fetch_calls) AS fetch_calls
            FROM usage WHERE ts >= ? AND ts < ? AND model != 'search/fetch'
            GROUP BY model ORDER BY cost DESC
            """,
            (int(start), int(end)),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["cost"] = round(item["cost"] or 0, 6)
            item["tool_cost"] = round(item["tool_cost"] or 0, 6)
            out.append(item)
        return out

    def by_model(self, days: float = 1) -> list[dict[str, Any]]:
        now = time.time()
        return self.by_model_range(now - days * 86400, now + 1)

    def by_task_range(
        self, start: float, end: float, limit: int = 5
    ) -> list[dict[str, Any]]:
        """按用途分组：聊天 / 主动判断 / 补话 / 空间 / 主动私信…"""
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            """
            SELECT task,
                   COALESCE(SUM(CASE WHEN prompt > 0 OR completion > 0
                                     THEN 1 ELSE 0 END), 0) AS calls,
                   COALESCE(SUM(requests), 0) AS requests,
                   SUM(cost + tool_cost) AS cost,
                   SUM(tool_cost) AS tool_cost
            FROM usage WHERE ts >= ? AND ts < ? AND session NOT LIKE 'tools:%'
            GROUP BY task ORDER BY cost DESC LIMIT ?
            """,
            (int(start), int(end), max(1, int(limit))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["cost"] = round(item["cost"] or 0, 6)
            item["tool_cost"] = round(item["tool_cost"] or 0, 6)
            item["requests"] = int(item["requests"] or 0)
            out.append(item)
        return out

    def by_task(self, days: float = 1, limit: int = 5) -> list[dict[str, Any]]:
        now = time.time()
        return self.by_task_range(now - days * 86400, now + 1, limit)

    def by_session_range(
        self, start: float, end: float, limit: int = 5
    ) -> list[dict[str, Any]]:
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            """
            SELECT session, COUNT(*) AS calls, SUM(requests) AS requests,
                   SUM(cost + tool_cost) AS cost, SUM(tool_cost) AS tool_cost,
                   SUM(search_calls) AS search_calls, SUM(fetch_calls) AS fetch_calls
            FROM usage WHERE ts >= ? AND ts < ? AND session NOT LIKE 'tools:%'
            GROUP BY session ORDER BY cost DESC LIMIT ?
            """,
            (int(start), int(end), max(1, int(limit))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["cost"] = round(item["cost"] or 0, 6)
            item["tool_cost"] = round(item["tool_cost"] or 0, 6)
            out.append(item)
        return out

    def by_session(self, days: float = 1, limit: int = 5) -> list[dict[str, Any]]:
        now = time.time()
        return self.by_session_range(now - days * 86400, now + 1, limit)

    def by_tool_range(
        self, start: float, end: float, limit: int = 10
    ) -> list[dict[str, Any]]:
        """图形工具花费：按 session/task 分组，和聊天分开看。"""
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            """
            SELECT session, task, COUNT(*) AS turns, SUM(requests) AS calls,
                   SUM(prompt) AS prompt, SUM(completion) AS completion,
                   SUM(cached) AS cached, SUM(cost + tool_cost) AS cost
            FROM usage
            WHERE ts >= ? AND ts < ? AND task IN ('sticker_annotate','sticker_screen')
            GROUP BY session, task ORDER BY cost DESC LIMIT ?
            """,
            (int(start), int(end), max(1, int(limit))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["cost"] = round(item["cost"] or 0, 6)
            item["calls"] = int(item["calls"] or 0)
            item["turns"] = int(item["turns"] or 0)
            out.append(item)
        return out

    def by_tool(self, days: float = 1, limit: int = 10) -> list[dict[str, Any]]:
        now = time.time()
        return self.by_tool_range(now - days * 86400, now + 1, limit)

    def turn_exists(self, turn_id: str) -> bool:
        if not self.enabled or self._db is None or not turn_id:
            return False
        row = self._db.execute(
            "SELECT 1 FROM usage WHERE turn_id = ? LIMIT 1", (str(turn_id),)
        ).fetchone()
        return row is not None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            "SELECT ts, ts_start, session, model, task, turn_id, requests, "
            "prompt, completion, cached, "
            "cost AS token_cost, tool_cost, search_calls, fetch_calls, "
            "(cost + tool_cost) AS cost "
            "FROM usage ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近 N 次真实模型请求（逐请求账本）。"""
        if not self.enabled or self._db is None:
            return []
        rows = self._db.execute(
            "SELECT id, turn_id, ts_start, ts_end, session, task, model, "
            "prompt, completion, cached, reasoning, status, latency_ms, "
            "peak, cost, price_version "
            "FROM usage_call ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def calls_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        """一轮里的所有请求，按发生顺序。审计/测试用。"""
        if not self.enabled or self._db is None or not turn_id:
            return []
        rows = self._db.execute(
            "SELECT id, turn_id, ts_start, ts_end, session, task, model, "
            "prompt, completion, cached, reasoning, status, latency_ms, "
            "peak, cost, price_version "
            "FROM usage_call WHERE turn_id = ? ORDER BY id",
            (str(turn_id),),
        ).fetchall()
        return [dict(r) for r in rows]
