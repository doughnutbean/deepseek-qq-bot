"""好友上线通知：好友在私聊里自助订阅，机器人每次上线时私聊告诉对方。

和群聊的「上下线推送」是两套东西：

- 群聊：上线问候 + 下线公告，按群开关，走模型；
- 好友私聊：只发**上线**、固定一句、0 token，只给 QQ 好友，默认关。
  下线不发：正常停止时进程马上要退，异常掉线时进程已经没了，
  不适合再承诺一句「我下线了」。

风控口径（详见 docs/使用说明.md）：

- 只在**停机够久**之后发（默认 30 分钟），热重启不吵人；
- 每人冷却 6 小时、每天最多 1 条；每次最多 20 人；间隔 3~5 秒；
- 夜间 23:00~08:00 不发；
- 只发 QQ 好友；主人和机器人自己不订阅。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .pricetime import BEIJING

log = logging.getLogger(__name__)

ONLINE_TEXT = "本鱼上线了，有事喊本鱼～"
STATE_PATH = DATA_DIR / "presence_dm_state.json"
HEARTBEAT_SECONDS = 300


def _path(path: Path | str | None) -> Path:
    return Path(path) if path else STATE_PATH


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_state(path: Path | str | None = None) -> dict[str, Any]:
    try:
        raw = json.loads(_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    sent = raw.get("sent")
    return {
        "last_alive_at": _as_float(raw.get("last_alive_at")),
        "sent": sent if isinstance(sent, dict) else {},
    }


def save_state(state: dict[str, Any], path: Path | str | None = None) -> None:
    target = _path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("上线通知状态写不进去：%s", exc)


def touch(path: Path | str | None = None, *, now: float | None = None) -> None:
    """记一次"我还活着"。心跳每 5 分钟一次，正常退出时再记一次。

    这样下次启动算停机时长时，不会把"上次启动到现在"整段都算成停机。
    """
    state = load_state(path)
    state["last_alive_at"] = time.time() if now is None else float(now)
    save_state(state, path)


def _minutes(hhmm: str) -> int | None:
    try:
        hour, minute = hhmm.strip().split(":")
        return int(hour) * 60 + int(minute)
    except (AttributeError, ValueError):
        return None


def _quiet_range(spec: str) -> tuple[int, int] | None:
    if not spec or "-" not in spec:
        return None
    start, end = spec.split("-", 1)
    a, b = _minutes(start), _minutes(end)
    if a is None or b is None or a == b:
        return None
    return a, b


def in_quiet_hours(now: datetime, spec: str) -> bool:
    """spec 形如 "23:00-08:00"；空串/写错就当没设夜间静音。"""
    rng = _quiet_range(spec)
    if rng is None:
        return False
    current = now.hour * 60 + now.minute
    start, end = rng
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _prune(state: dict[str, Any], *, now: float, keep_days: int = 7) -> None:
    sent = state.get("sent")
    if not isinstance(sent, dict):
        state["sent"] = {}
        return
    cutoff = now - keep_days * 86400
    for uid, record in list(sent.items()):
        try:
            ts = float((record or {}).get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < cutoff:
            sent.pop(uid, None)


async def notify_online(
    onebot,
    prefs,
    config,
    friend_ids: set[str] | None,
    *,
    now: float | None = None,
    state_path: Path | str | None = None,
    sleeper=asyncio.sleep,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """上线时给订阅的好友发一句；返回报告，不把异常抛给启动流程。

    friend_ids 是 QQ 好友号集合；None 表示好友列表查不到 —— 宁可不发，
    也不猜。
    """
    moment = time.time() if now is None else float(now)
    state = load_state(state_path)
    last_alive = float(state.get("last_alive_at") or 0)
    state["last_alive_at"] = moment
    _prune(state, now=moment)

    report: dict[str, Any] = {
        "sent": [],
        "failed": [],
        "skipped": [],
        "total": 0,
        "text": ONLINE_TEXT,
    }

    def _done() -> dict[str, Any]:
        save_state(state, state_path)
        return report

    if not getattr(config, "presence_dm_enabled", False):
        report["disabled"] = "off"
        return _done()

    # 风控规避模式：好友上线通知也是主动找人，先不发。
    if getattr(config, "enable_risk_mode", False) or (
        prefs is not None
        and getattr(prefs, "risk_mode", lambda: False)()
    ):
        report["disabled"] = "risk_mode"
        return _done()

    uids = prefs.presence_dm_uids() if prefs is not None else []
    if not uids:
        return _done()

    min_minutes = max(0, int(getattr(config, "presence_dm_min_downtime_minutes", 30)))
    if last_alive <= 0:
        # 第一次跑，没有"上次活着"的基线；先记下，这次不发。
        report["skipped"].append("first_run")
        return _done()
    downtime = max(0.0, moment - last_alive)
    report["downtime_minutes"] = round(downtime / 60, 1)
    if min_minutes and downtime < min_minutes * 60:
        report["skipped"].append("downtime_too_short")
        return _done()

    local_now = datetime.fromtimestamp(moment, BEIJING)
    if in_quiet_hours(local_now, str(getattr(config, "presence_dm_quiet_hours", ""))):
        report["skipped"].append("quiet_hours")
        return _done()

    if friend_ids is None:
        report["skipped"].append("friend_list_unavailable")
        return _done()

    owner = str(getattr(config, "owner_qq", "") or "")
    bot = str(getattr(config, "bot_qq", "") or "")
    cooldown = max(0, int(getattr(config, "presence_dm_cooldown_minutes", 360))) * 60
    daily_cap = max(0, int(getattr(config, "presence_dm_daily_cap", 1)))
    today = _today(local_now)
    banned = getattr(prefs, "private_banned", None)
    candidates: list[str] = []

    for uid in uids:
        uid = str(uid)
        if uid in {owner, bot}:
            report["skipped"].append(f"{uid}:self_or_owner")
            continue
        if uid not in friend_ids:
            report["skipped"].append(f"{uid}:not_friend")
            continue
        if callable(banned) and banned(uid):
            report["skipped"].append(f"{uid}:banned")
            continue
        record = state["sent"].get(uid)
        if not isinstance(record, dict):
            record = {}
        last = _as_float(record.get("ts"))
        if cooldown and last and moment - last < cooldown:
            report["skipped"].append(f"{uid}:cooldown")
            continue
        if (
            daily_cap
            and record.get("date") == today
            and int(_as_float(record.get("count"))) >= daily_cap
        ):
            report["skipped"].append(f"{uid}:daily_cap")
            continue
        candidates.append(uid)

    limit = max(0, int(getattr(config, "presence_dm_max_recipients", 20)))
    if limit and len(candidates) > limit:
        report["skipped"].extend(f"{uid}:max_recipients" for uid in candidates[limit:])
        candidates = candidates[:limit]

    report["total"] = len(candidates)
    if not candidates:
        return _done()

    picker = rng or random
    for index, uid in enumerate(candidates):
        try:
            target = int(uid)
        except (TypeError, ValueError):
            report["skipped"].append(f"{uid}:bad_uid")
            continue
        try:
            await onebot.send_private(
                target, [{"type": "text", "data": {"text": ONLINE_TEXT}}]
            )
        except Exception as exc:  # noqa: BLE001 - 一个失败不挡后面的人
            log.warning("上线通知：发给 %s 失败：%s", uid, exc)
            report["failed"].append(uid)
            continue
        report["sent"].append(uid)
        record = state["sent"].get(uid)
        if not isinstance(record, dict):
            record = {}
        count = int(_as_float(record.get("count"))) + 1 if record.get("date") == today else 1
        state["sent"][uid] = {"ts": moment, "date": today, "count": count}
        if index < len(candidates) - 1:
            await sleeper(picker.uniform(3.0, 5.0))

    return _done()


async def heartbeat_loop(
    stop_event: asyncio.Event,
    *,
    state_path: Path | str | None = None,
    interval: int = HEARTBEAT_SECONDS,
) -> None:
    """每 5 分钟记一次"还活着"；停机时长靠它算得准。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(1, int(interval)))
            return
        except asyncio.TimeoutError:
            pass
        touch(state_path)
