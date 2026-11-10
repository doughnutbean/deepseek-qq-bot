"""DeepSeek 账户余额观测与对账。

学的是小鲸鱼挂件那套思路：**余额接口只给快照，不给流水**。

- 每次观测存一条快照；
- 同一个自然日里，余额下降按下降额累加成「账户实扣」；余额上升单独记成
  充值/赠金，**不冲减**已经观测到的消费；
- 第一次观测晚于 00:10、或过去某天最后一次观测早于 23:50，就标成
  「部分数据」—— 没观测到的那段不能假装花了 0；
- 「账户实扣」是**整个 DeepSeek 账户**的，DSH、别的脚本、别的 key 都在里面；
  机器人自己的 `usage.db` 只是其中一部分。两个口径必须分开写清楚。

余额接口：`GET https://api.deepseek.com/user/balance`，返回
`{"is_available": true, "balance_infos": [{"currency": "CNY",
"total_balance": "40.37", "granted_balance": "0.00",
"topped_up_balance": "40.37"}]}`。余额是字符串，按 float 记，展示时保留
4 位小数（账本内部按 float 算，差 1e-8 以内视为 0）。

思路参考 DeepSeek-Balance-Whale-Widget（小鲸鱼挂件）的
`lib/accounting.mjs`：余额下降累计为消费、充值单独记、按北京时间分日、
缺口标 partial、充值/扣减可校正。这里只借思路，不依赖它的插件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8))
# 观测缺口超过这个秒数就算「部分数据」（和挂件一致：10 分钟）
PARTIAL_GAP_SECONDS = 10 * 60
# 今天最近一次观测超过这么久，提示后台可能没在跑（不算硬缺口，只提醒）
STALE_SECONDS = 30 * 60
DEFAULT_BASE_URL = "https://api.deepseek.com"


class BalanceError(Exception):
    """余额接口不可用 / 返回结构不对。"""


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def today_key(now: float | None = None) -> str:
    stamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(stamp, BEIJING).strftime("%Y-%m-%d")


def day_bounds(day: str) -> tuple[float, float]:
    """北京时间的自然日 [00:00, 次日 00:00)。"""
    start = datetime.strptime(str(day), "%Y-%m-%d").replace(tzinfo=BEIJING)
    stamp = start.timestamp()
    return stamp, stamp + 86400


def clock(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp), BEIJING).strftime("%H:%M")


def fmt_money(value: Any, decimals: int = 4, *, signed: bool = False) -> str:
    """金额展示：None 写「—」，小到 4 位小数看不见就写 ¥0。"""
    if value is None:
        return "—"
    number = float(value)
    if abs(number) < 0.5 / (10**decimals):
        return "¥0"
    sign = "-" if number < 0 else ("+" if signed and number > 0 else "")
    return f"{sign}¥{abs(number):.{decimals}f}"


class BalanceAPI:
    """只负责取一次余额，不碰数据库。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        api_url: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = max(1.0, float(timeout))
        url = str(api_url or "").strip()
        if not url:
            base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
            # 官方余额接口在根路径下，不在 /v1 下；有人把 base_url 写成
            # https://api.deepseek.com/v1 时这里自动退回根路径。
            if base.endswith("/v1"):
                base = base[:-3]
            url = base + "/user/balance"
        self.url = url

    def fetch(self, now: float | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise BalanceError("没配 DEEPSEEK_API_KEY")
        stamp = time.time() if now is None else float(now)
        request = urllib.request.Request(
            self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        raw = b""
        last_error = ""
        for attempt in (0, 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                # 4xx 是 key/地址不对，重试也没用；5xx 才值得再试一次
                if exc.code < 500 or attempt:
                    raise BalanceError(f"余额接口返回 {last_error}") from exc
                time.sleep(0.5)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(getattr(exc, "reason", exc))
                if attempt:
                    raise BalanceError(f"余额接口连不上：{last_error}") from exc
                time.sleep(0.5)
        if not raw:
            raise BalanceError(f"余额接口没返回内容：{last_error}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BalanceError("余额接口返回不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise BalanceError("余额接口返回结构不对")
        if data.get("is_available") is False:
            raise BalanceError("账户余额当前不可用")
        infos = data.get("balance_infos")
        if not isinstance(infos, list) or not infos:
            raise BalanceError("余额接口没返回 balance_infos")
        info = next(
            (
                item
                for item in infos
                if isinstance(item, dict)
                and str(item.get("currency") or "").upper() == "CNY"
            ),
            None,
        )
        if info is None:
            info = next((item for item in infos if isinstance(item, dict)), None)
        if not isinstance(info, dict):
            raise BalanceError("balance_infos 结构不对")
        total = _number(info.get("total_balance"))
        if total is None:
            raise BalanceError("total_balance 不是数字")
        currency = str(info.get("currency") or "CNY").upper()
        if len(currency) != 3 or not currency.isalpha():
            currency = "CNY"
        return {
            "ts": stamp,
            "currency": currency,
            "total": total,
            "granted": _number(info.get("granted_balance")),
            "topped_up": _number(info.get("topped_up_balance")),
        }


def _row_value(row: Any, key: str) -> Any:
    """sqlite3.Row 和普通 tuple 都能取。"""
    if isinstance(row, sqlite3.Row):
        return row[key]
    return getattr(row, key, None)


class BalanceStore:
    """快照账本，和 usage.db 同一个连接（由 Usage 持有）。"""

    def __init__(self, db: sqlite3.Connection | None, scope: str) -> None:
        self.db = db
        self.scope = str(scope or "default")
        if self.db is not None:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        assert self.db is not None
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS balance_snapshot (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,
                scope       TEXT    NOT NULL,
                currency    TEXT    NOT NULL,
                total       REAL    NOT NULL,
                granted     REAL,
                topped_up   REAL,
                is_available INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_balance_scope_ts
                ON balance_snapshot(scope, currency, ts);
            CREATE TABLE IF NOT EXISTS balance_correction (
                day          TEXT NOT NULL,
                scope        TEXT NOT NULL,
                currency     TEXT NOT NULL,
                credits      REAL NOT NULL DEFAULT 0,
                other_debits REAL NOT NULL DEFAULT 0,
                amount       REAL NOT NULL,
                ts           REAL NOT NULL,
                PRIMARY KEY (day, scope, currency)
            );
            """
        )
        self.db.commit()

    def observe(self, snapshot: dict[str, Any]) -> bool:
        """存一条快照。重复/倒序的样本直接丢掉，返回有没有写入。"""
        if self.db is None:
            return False
        stamp = float(snapshot.get("ts") or time.time())
        currency = str(snapshot.get("currency") or "CNY").upper()
        total = _number(snapshot.get("total"))
        if total is None:
            return False
        last = self.db.execute(
            "SELECT ts FROM balance_snapshot "
            "WHERE scope = ? AND currency = ? ORDER BY ts DESC LIMIT 1",
            (self.scope, currency),
        ).fetchone()
        if last is not None and stamp <= float(_row_value(last, "ts") or 0):
            return False
        self.db.execute(
            "INSERT INTO balance_snapshot"
            "(ts, scope, currency, total, granted, topped_up) "
            "VALUES (?,?,?,?,?,?)",
            (
                stamp,
                self.scope,
                currency,
                total,
                _number(snapshot.get("granted")),
                _number(snapshot.get("topped_up")),
            ),
        )
        self.db.commit()
        return True

    def last_snapshot(self) -> dict[str, Any] | None:
        if self.db is None:
            return None
        row = self.db.execute(
            "SELECT ts, currency, total, granted, topped_up "
            "FROM balance_snapshot WHERE scope = ? "
            "ORDER BY ts DESC LIMIT 1",
            (self.scope,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _correction(self, day: str, currency: str) -> dict[str, Any] | None:
        if self.db is None:
            return None
        row = self.db.execute(
            "SELECT credits, other_debits, amount, ts FROM balance_correction "
            "WHERE day = ? AND scope = ? AND currency = ?",
            (day, self.scope, currency),
        ).fetchone()
        return dict(row) if row is not None else None

    def day(
        self,
        day: str,
        local_cost: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """这一天的账户口径。

        `amount` 是账户实扣；`local_cost` 是本机账本估算，仅供对照，
        不参与账户金额计算。没有快照时 `amount=None`，绝不写 0。
        """
        stamp_now = time.time() if now is None else float(now)
        start, end = day_bounds(day)
        local = float(local_cost or 0.0)
        summary: dict[str, Any] = {
            "day": day,
            "currency": "CNY",
            "amount": None,
            "source": "none",
            "label": "还没观测到",
            "opening": None,
            "current": None,
            "debit": 0.0,
            "credit": 0.0,
            "first_ts": None,
            "last_ts": None,
            "count": 0,
            "partial": True,
            "partial_reason": "没有余额快照",
            "window": "",
            "correction": None,
            "local_cost": local,
            "diff": None,
            "stale": False,
        }
        if self.db is None:
            return summary
        rows = self.db.execute(
            "SELECT ts, currency, total, granted, topped_up "
            "FROM balance_snapshot "
            "WHERE scope = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (self.scope, start, end),
        ).fetchall()
        if not rows:
            return summary

        # 一个账户可能同时有 CNY/USD；机器人按人民币记账，优先 CNY。
        cny = [
            row
            for row in rows
            if str(_row_value(row, "currency") or "").upper() == "CNY"
        ]
        use = cny or list(rows)
        currency = str(_row_value(use[0], "currency") or "CNY").upper()
        use = [
            row
            for row in use
            if str(_row_value(row, "currency") or "").upper() == currency
        ]
        if not use:
            return summary

        first = use[0]
        last = use[-1]
        opening = float(_row_value(first, "total"))
        current = float(_row_value(last, "total"))
        debit = 0.0
        credit = 0.0
        for previous, current_row in zip(use, use[1:]):
            delta = float(_row_value(previous, "total")) - float(
                _row_value(current_row, "total")
            )
            if delta > 0:
                debit += delta
            elif delta < 0:
                credit += -delta
        first_ts = float(_row_value(first, "ts"))
        last_ts = float(_row_value(last, "ts"))

        correction = self._correction(day, currency)
        amount = debit
        source = "observed"
        if correction is not None:
            amount = (
                opening
                + float(correction.get("credits") or 0)
                - float(correction.get("other_debits") or 0)
                - current
            )
            amount = max(0.0, amount)
            source = "corrected"

        partial = False
        reason = ""
        if len(use) < 2 and correction is None:
            partial = True
            reason = "只有一次观测，算不出区间消费"
        if first_ts - start > PARTIAL_GAP_SECONDS:
            partial = True
            reason = reason or f"只从 {clock(first_ts)} 起算，之前的消费不在里面"
        if stamp_now >= end and end - last_ts > PARTIAL_GAP_SECONDS:
            partial = True
            reason = reason or f"观测到 {clock(last_ts)} 就断了，之后的消费不在里面"
        stale = False
        if stamp_now < end and stamp_now - last_ts > STALE_SECONDS:
            stale = True
            partial = True
            reason = reason or f"最近一次观测是 {clock(last_ts)}，后面的消费还没算进来"

        label = "账户实扣（余额观测）"
        if source == "corrected":
            label = "账户实扣（已校正）"
        elif credit > 0 and correction is None:
            label = "账户实扣（待核对充值）"

        summary.update(
            {
                "currency": currency,
                "amount": round(amount, 8),
                "source": source,
                "label": label,
                "opening": opening,
                "current": current,
                "debit": round(debit, 8),
                "credit": round(credit, 8),
                "first_ts": first_ts,
                "last_ts": last_ts,
                "count": len(use),
                "partial": partial,
                "partial_reason": reason,
                "window": (
                    f"{clock(first_ts)}–{clock(last_ts)}"
                    if first_ts != last_ts
                    else clock(first_ts)
                ),
                "correction": correction,
                "local_cost": local,
                "diff": round(amount - local, 8),
                "stale": stale,
            }
        )
        return summary

    def correct(
        self,
        day: str,
        credits: float = 0.0,
        other_debits: float = 0.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """余额校正：用「期初 + 累计到账 - 其它扣减 - 期末」重算实扣。

        余额接口不提供充值流水；如果充值和消费发生在两次观测之间，
        单看下降额会少算。把实际到账金额填进来就能还原。
        """
        if self.db is None:
            raise BalanceError("账本没开，无法校正")
        credits = float(credits or 0.0)
        other_debits = float(other_debits or 0.0)
        if credits < 0 or other_debits < 0:
            raise BalanceError("到账金额 / 其它扣减不能是负数")
        try:
            start, end = day_bounds(day)
        except ValueError as exc:
            raise BalanceError("日期格式应为 YYYY-MM-DD") from exc
        rows = self.db.execute(
            "SELECT ts, currency, total FROM balance_snapshot "
            "WHERE scope = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (self.scope, start, end),
        ).fetchall()
        cny = [
            row
            for row in rows
            if str(_row_value(row, "currency") or "").upper() == "CNY"
        ]
        use = cny or list(rows)
        if not use:
            raise BalanceError("这一天没有余额观测，无法校正")
        currency = str(_row_value(use[0], "currency") or "CNY").upper()
        use = [
            row
            for row in use
            if str(_row_value(row, "currency") or "").upper() == currency
        ]
        opening = float(_row_value(use[0], "total"))
        current = float(_row_value(use[-1], "total"))
        amount = opening + credits - other_debits - current
        if amount < -1e-9:
            raise BalanceError("校正后消费为负，请核对到账金额和统计起点")
        amount = max(0.0, amount)
        stamp = time.time() if now is None else float(now)
        self.db.execute(
            "INSERT INTO balance_correction"
            "(day, scope, currency, credits, other_debits, amount, ts) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(day, scope, currency) DO UPDATE SET "
            "credits = excluded.credits, "
            "other_debits = excluded.other_debits, "
            "amount = excluded.amount, ts = excluded.ts",
            (day, self.scope, currency, credits, other_debits, amount, stamp),
        )
        self.db.commit()
        return self.day(day)

    def clear_correction(self, day: str) -> None:
        if self.db is None:
            return
        self.db.execute(
            "DELETE FROM balance_correction WHERE day = ? AND scope = ?",
            (day, self.scope),
        )
        self.db.commit()


class BalanceService:
    """余额观测的门面：Bot / CLI / 日报都从这里取。"""

    def __init__(
        self,
        usage: Any,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        api_url: str = "",
        enabled: bool = True,
        poll_minutes: int = 10,
        timeout: float = 10.0,
        api: Any = None,
    ) -> None:
        self.usage = usage
        self.scope = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[
            :24
        ]
        db = getattr(usage, "db", None) if usage is not None else None
        self.store = BalanceStore(db, self.scope) if db is not None else None
        self.api = api or BalanceAPI(
            api_key, base_url=base_url, api_url=api_url, timeout=timeout
        )
        self.enabled = bool(enabled and self.store is not None)
        self.poll_minutes = max(1, int(poll_minutes))

    @property
    def available(self) -> bool:
        return self.store is not None

    def fetch_live(self, now: float | None = None) -> dict[str, Any]:
        return self.api.fetch(now)

    def record(self, snapshot: dict[str, Any]) -> bool:
        if self.store is None:
            return False
        return self.store.observe(snapshot)

    def fetch_and_record(self, now: float | None = None) -> dict[str, Any]:
        snapshot = self.fetch_live(now)
        self.record(snapshot)
        return snapshot

    def last_snapshot(self) -> dict[str, Any] | None:
        return self.store.last_snapshot() if self.store is not None else None

    def today(
        self, now: float | None = None, local_cost: float | None = None
    ) -> dict[str, Any]:
        stamp = time.time() if now is None else float(now)
        return self.day(today_key(stamp), local_cost=local_cost, now=stamp)

    def day(
        self,
        day: str,
        local_cost: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if self.store is None:
            return {
                "day": day,
                "amount": None,
                "source": "none",
                "label": "还没观测到",
                "partial": True,
                "partial_reason": "账本没开",
                "local_cost": float(local_cost or 0.0),
                "diff": None,
                "credit": 0.0,
            }
        return self.store.day(day, local_cost=local_cost, now=now)

    def correct(self, day: str, credits: float = 0.0, other_debits: float = 0.0) -> dict[str, Any]:
        if self.store is None:
            raise BalanceError("账本没开，无法校正")
        return self.store.correct(day, credits=credits, other_debits=other_debits)

    def clear_correction(self, day: str) -> None:
        if self.store is not None:
            self.store.clear_correction(day)


def account_line(
    account: dict[str, Any] | None, local_cost: float | None = None
) -> str:
    """一行「本机 vs 账户」；没观测到就明确说没观测到。"""
    if not account or account.get("amount") is None:
        return "账户实扣：还没观测到（后台每 10 分钟记一次余额）"
    amount = float(account["amount"])
    local = float(local_cost if local_cost is not None else account.get("local_cost") or 0)
    if int(account.get("count") or 0) < 2 and account.get("correction") is None:
        window = str(account.get("window") or "")
        suffix = f"（{window} 起算）" if window else ""
        return f"账户实扣：刚起算{suffix}，还看不出区间消费；本机 {fmt_money(local)}"
    diff = amount - local
    label = str(account.get("label") or "账户实扣（余额观测）")
    line = (
        f"{label}{fmt_money(amount)}；本机 {fmt_money(local)}；"
        f"差额 {fmt_money(diff, signed=True)}"
    )
    window = str(account.get("window") or "")
    if window:
        line += f"（{window}，{'部分数据' if account.get('partial') else '完整'}）"
    return line


def account_detail_lines(
    account: dict[str, Any] | None, local_cost: float | None = None
) -> list[str]:
    """日报 / 查账里的 1~3 行：账户实扣、充值提醒、部分数据原因。"""
    if not account:
        return []
    if account.get("amount") is None:
        return [account_line(account, local_cost)]
    lines = [account_line(account, local_cost)]
    credit = float(account.get("credit") or 0)
    correction = account.get("correction")
    if credit > 0 and not correction:
        lines.append(
            f"观测到充值/赠金 {fmt_money(credit)}，不冲减消费；"
            "要精确对账可用 tools/balance_cli.py 校正。"
        )
    reason = str(account.get("partial_reason") or "")
    if account.get("partial") and reason:
        lines.append(f"⚠ {reason}。")
    return lines
