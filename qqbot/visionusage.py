"""把工具里的图形/视觉调用也记进同一本账。

图形调用（表情包逐图注解、表情包审核）不在 `LLM.reply()` 的回合里，
所以不能靠 `_record_usage` 自动记账。这里给工具一个统一入口：

- 每次调用落一条 `usage_call`（带自己的 `ts_start`，峰谷按它取价）；
- 整次工具运行再落一条 `usage` 汇总，`task=sticker_annotate` / `sticker_screen`；
- `Usage.summary_range` 会把这两类 task 单独汇总成 `vision_cost` / `vision_calls`，
  查账和日报就能写清楚「其中图形工具多少钱」。

工具没有拿到 usage（接口异常/空响应）时不会凭空造 token，只记 0。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .config import ROOT
from .pricetime import PriceTime
from .usage import VISION_TASKS, Usage


def record_vision_run(
    cfg: Any,
    *,
    session: str,
    task: str,
    infos: list[dict[str, Any]],
    label: str = "图形调用",
) -> tuple[int, float]:
    """把一次工具运行的视觉调用写进 `data/usage.db`，返回 (次数, 金额)。

    `infos` 每条是一次成功响应：ts_start / ts_end / model / prompt /
    completion / cached / reasoning / status / latency_ms。
    """
    if not infos or not bool(getattr(cfg, "enable_usage", True)):
        return 0, 0.0
    usage = Usage(
        ROOT / cfg.usage_db,
        price_input=cfg.price_input,
        price_output=cfg.price_output,
        price_cached=cfg.price_cached,
        price_peak_multiplier=cfg.price_peak_multiplier,
        price_search_call=cfg.price_search_call,
        price_fetch_call=cfg.price_fetch_call,
        enabled=True,
    )
    try:
        price = PriceTime(
            enabled=bool(getattr(cfg, "enable_pricetime_tier", True)),
            peak_windows=getattr(
                cfg, "pricetime_peak_windows", "09:00-12:00,14:00-18:00"
            ),
            holidays=getattr(cfg, "pricetime_holidays", ""),
        )
        turn_id = f"tools:vision:{int(time.time())}:{uuid.uuid4().hex[:8]}"
        prompt = sum(int(info.get("prompt", 0) or 0) for info in infos)
        completion = sum(int(info.get("completion", 0) or 0) for info in infos)
        cached = sum(int(info.get("cached", 0) or 0) for info in infos)
        first = min(float(info.get("ts_start") or time.time()) for info in infos)
        last = max(
            float(info.get("ts_end") or info.get("ts_start") or time.time())
            for info in infos
        )
        total = 0.0
        for info in infos:
            started = float(info.get("ts_start") or first)
            ended = float(info.get("ts_end") or started)
            peak = bool(price.enabled and price.is_peak(started))
            total += usage.record_call(
                turn_id,
                session,
                str(info.get("model") or cfg.model),
                int(info.get("prompt", 0) or 0),
                int(info.get("completion", 0) or 0),
                int(info.get("cached", 0) or 0),
                ts_start=started,
                ts_end=ended,
                peak=peak,
                task=task,
                status=str(info.get("status") or "ok"),
                reasoning=int(info.get("reasoning", 0) or 0),
                latency_ms=info.get("latency_ms"),
            )
        total = round(total, 8)
        usage.record(
            session,
            str(getattr(cfg, "model", "") or ""),
            prompt,
            completion,
            cached,
            ts=last,
            ts_start=first,
            requests=len(infos),
            task=task,
            turn_id=turn_id,
            token_cost=total,
        )
        return len(infos), total
    finally:
        usage.close()
