"""每日花销日报：每天中午把昨天的账本私聊发给主人。

为什么不挂 Windows 计划任务：那样得另起一个进程去连 NapCat，和正在跑的
机器人抢同一条 WebSocket。挂在机器人自己的事件循环里，到点直接用现成的
OneBot 连接发私信 —— 不用多一个进程，也不会漏。

几个容易踩的坑，这里都兜住了：

1. 统计的是**自然日**（昨天 00:00 到今天 00:00），不是"最近 24 小时"。
   中午 12 点用 `summary(days=1)` 会把今天上午的花销也算进去。
2. 电脑休眠 / 机器人半夜重启，错过了 12 点，醒来会补发；但拖太久
   （默认 8 小时）就不补了 —— 半夜三更发一份昨天的账没意义。
3. 发没发过记在 `daily_report_state.json` 里，重启不会重复发。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_AT = "12:00"
DEFAULT_CATCHUP_HOURS = 8
# 到点后这么久之内算准点，再晚就算补发（tick 是 30 秒一次，不会卡得那么死）
ON_TIME_SECONDS = 90


def parse_at(value: str, *, label: str = "日报") -> tuple[int, int]:
    """'12:00' / '12：00' / '中午12点' / '12' → (12, 0)。认不出就退回中午 12 点。"""
    body = (value or "").strip().replace("：", ":")
    body = body.replace("中午", "").replace("点", ":").strip(": ")
    if not body:
        return 12, 0
    parts = body.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
    except (ValueError, IndexError):
        log.warning("%s时间 %r 看不懂，按 12:00 算", label, value)
        return 12, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning("%s时间 %r 超出范围，按 12:00 算", label, value)
        return 12, 0
    return hour, minute


def day_window(now: float, days_ago: int = 1) -> tuple[float, float]:
    """自然日窗口。days_ago=1 → [昨天 00:00, 今天 00:00)。"""
    midnight = datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = midnight - timedelta(days=max(0, days_ago - 1))
    start = end - timedelta(days=1)
    return start.timestamp(), end.timestamp()


def month_window(now: float) -> tuple[float, float]:
    """本月 1 号 00:00 到现在。"""
    moment = datetime.fromtimestamp(now)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now


def money(value: float) -> str:
    """金额写法：0 就是 ¥0，小到看不清就写「不到 ¥0.0001」。"""
    if value <= 0:
        return "¥0"
    if value < 0.0001:
        return "不到 ¥0.0001"
    return f"¥{value:.4f}"


def _thousands(number: int) -> str:
    return f"{int(number):,}"


def _task_label(task: str) -> str:
    """账本里的英文 task → 日报里说人话。"""
    labels = {
        "chat": "聊天",
        "proactive_judge": "主动判断",
        "followup": "补话",
        "proactive_dm": "主动私信",
        "qzone": "空间",
        "diary": "日记",
        "memory": "记忆",
        "title": "标题",
        "vision": "看图",
        "sticker": "表情包",
        "tools": "工具补记",
    }
    return labels.get(task or "", (task or "未知"))


def _session_label(session: str, owner_qq: str) -> str:
    """group:123 / private:456 → 说人话的会话名。"""
    kind, _, ident = (session or "").partition(":")
    if kind == "group" and ident:
        return f"群 {ident}"
    if kind == "private" and ident:
        return "私聊（主人）" if owner_qq and ident == owner_qq else f"私聊 {ident}"
    if kind == "tools":
        return "搜索/抓取（补记）"
    return (session or "").strip() or "未知"


class DailyReport:
    """管"今天发没发过"和"现在该不该发"，不碰数据库、不碰 OneBot。"""

    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        at: str = DEFAULT_AT,
        catchup_hours: float = DEFAULT_CATCHUP_HOURS,
        dry_run: bool = False,
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.hour, self.minute = parse_at(at)
        # 0 表示不补发：过了那一分钟就等明天
        self.catchup = max(0.0, float(catchup_hours)) * 3600
        self.dry_run = dry_run
        self._state = self._load()

    # ---------- 存档 ----------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {"last_day": str(raw.get("last_day") or "")}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._state, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("日报状态写不进去：%s", exc)

    # ---------- 时间 ----------

    def describe(self) -> str:
        return f"每天 {self.hour:02d}:{self.minute:02d}"

    def _target(self, now: float) -> float:
        moment = datetime.fromtimestamp(now)
        return moment.replace(
            hour=self.hour, minute=self.minute, second=0, microsecond=0
        ).timestamp()

    def due(self, now: float | None = None) -> tuple[bool, str]:
        """现在该不该发。返回 (要发, 原因)。同一天只发一次。"""
        now = time.time() if now is None else now
        if not self.enabled:
            return False, "没开"
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self._state.get("last_day") == today:
            return False, "今天已经发过了"
        target = self._target(now)
        if now < target:
            return False, "还没到点"
        late = now - target
        limit = self.catchup if self.catchup else 60.0
        if late > limit:
            # 拖太久就不补了。但要记一笔，否则每 30 秒都会重新算一次
            self._state["last_day"] = today
            self._save()
            log.info("日报错过太久（%.1f 小时），今天不补了", late / 3600)
            return False, f"错过了 {late / 3600:.1f} 小时"
        if late <= ON_TIME_SECONDS:
            return True, "准点"
        return True, f"补发（迟了 {int(late // 60)} 分钟）"

    def note_sent(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._state["last_day"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        self._save()


def build_report(
    usage: Any,
    now: float,
    *,
    owner_qq: str = "",
    note: str = "",
) -> str:
    """拼出私聊发出去的那段话。纯读 usage.db，不调模型，不花钱。

    **只报本机**（这个 QQ 机器人自己）。账户余额是整个账户的，含 DSH/其他
    客户端，口径不同，不混进日报；要看账户发「对账」或跑 balance_cli.py。
    """
    start, end = day_window(now, 1)
    stats = usage.summary_range(start, end)
    prev_start, prev_end = day_window(now, 2)
    before = usage.summary_range(prev_start, prev_end)
    month_start, _ = month_window(now)
    month = usage.summary_range(month_start, now)

    day_label = datetime.fromtimestamp(end - 1).strftime("%m-%d")
    lines = [f"昨日账本（{day_label}）"]
    if note.startswith("补发"):
        lines[0] += " · 补发"
    lines.append("")

    if (
        not stats.get("calls")
        and not stats.get("tool_calls")
        and not float(stats.get("vision_cost") or 0)
    ):
        lines.append("昨天一次模型都没调用，一分钱没花。")
    else:
        cost = float(stats.get("cost") or 0)
        prompt = int(stats.get("prompt") or 0)
        cached = int(stats.get("cached") or 0)
        completion = int(stats.get("completion") or 0)
        hit = cached / prompt * 100 if prompt else 0.0
        requests = int(stats.get("requests") or 0)
        call_text = f"共 {stats['calls']} 次调用"
        if requests and requests != stats["calls"]:
            call_text += f"（{requests} 次模型请求）"
        lines.append(f"本机花了 {money(cost)}，{call_text}。")
        lines.append(
            f"输入 {_thousands(prompt)}（缓存 {hit:.0f}%）"
            f"  输出 {_thousands(completion)}。"
        )
        vision_cost = float(stats.get("vision_cost") or 0)
        if vision_cost > 0:
            lines.append(
                f"其中图形工具 {int(stats.get('vision_calls') or 0)} 次 "
                f"{money(vision_cost)}（表情包注解/审核）。"
            )
        search_calls = int(stats.get("search_calls") or 0)
        fetch_calls = int(stats.get("fetch_calls") or 0)
        if search_calls or fetch_calls:
            tool_bits = []
            if search_calls:
                tool_bits.append(f"搜索 {search_calls} 次")
            if fetch_calls:
                tool_bits.append(f"抓取 {fetch_calls} 次")
            lines.append(
                "、".join(tool_bits)
                + f"，{money(float(stats.get('tool_cost') or 0))}（已算在上面）。"
            )

        sessions = usage.by_session_range(start, end, limit=4)
        if sessions:
            lines.append("")
            lines.append("最费钱的会话：")
            for row in sessions:
                lines.append(
                    f"· {_session_label(row['session'], owner_qq)}  "
                    f"{money(float(row['cost'] or 0))}（{row['calls']} 次）"
                )

        tasks = usage.by_task_range(start, end, limit=3)
        if len(tasks) > 1 or (tasks and tasks[0].get("task") != "chat"):
            lines.append("")
            lines.append(
                "按用途："
                + "，".join(
                    f"{_task_label(str(row.get('task') or ''))} "
                    f"{money(float(row['cost'] or 0))}"
                    for row in tasks
                )
            )

        models = usage.by_model_range(start, end)
        if len(models) > 1:
            lines.append("")
            lines.append(
                "按模型："
                + "，".join(
                    f"{row['model']} {money(float(row['cost'] or 0))}"
                    for row in models
                )
            )

        lines.append("")
        if before.get("calls") and float(before.get("cost") or 0) > 0:
            prev_cost = float(before["cost"])
            diff = cost - prev_cost
            pct = diff / prev_cost * 100
            if abs(diff) < 0.00005:
                lines.append(f"和前天差不多（{money(prev_cost)}）。")
            else:
                word = "多" if diff > 0 else "少"
                lines.append(
                    f"前天 {money(prev_cost)}，这次{word}了 "
                    f"{money(abs(diff))}（{abs(pct):.0f}%）。"
                )
        elif before.get("calls"):
            lines.append("前天没花钱，这次是净增的。")
        else:
            lines.append("前天没有记录，没法比。")

    lines.append(f"本月累计 {money(float(month.get('cost') or 0))}（本机）。")
    lines.append("（账户口径发「对账」看，含 DSH/其他客户端。）")
    return "\n".join(lines)
