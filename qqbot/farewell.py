"""下线前的告别：先给每个群说一句「本鱼要下线热更新」，再停进程。

双击 `停止机器人.bat` 时，`tools/stop_bot.py` 会落一个 `stop.request`；
这里的后台任务看到请求后：

1. 向协议端要 `get_group_list`（机器人当前所在的**每一个**群）；
2. 每个群发同一句固定文案（不走模型，0 token，也不受潜水/生气影响）；
3. 写 `stop.done` 回执，然后通知 `run.py` 退出（退出码 4）。

文案对齐 `示例规范.md`：自称本鱼、一句为主、40 字以内、软收尾、不用 Markdown。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR

log = logging.getLogger(__name__)

# 三个变体，同一个意思。群里看到哪一句都像她会说的话。
# 不写“几分钟/等会儿”这类具体时间：下线多久她自己也说不准。
FAREWELL_LINES = (
    "本鱼要下线热更新啦，去去就回，别当本鱼没了嘛～",
    "本鱼先去下线热更新，尾巴收一下，回来接着聊～",
    "本鱼要下线热更新啦，群里先别散，本鱼回来还要补课呢～",
)

# 控制文件和 bot.lock 都在 data/，不再散落在仓库根目录。
STOP_REQUEST = DATA_DIR / "stop.request"
STOP_DONE = DATA_DIR / "stop.done"
STOP_FLAG = DATA_DIR / "stop.flag"

# run.py 用这个退出码告诉 bot_loop.bat：这是主动下线，不要再拉起来。
EXIT_GRACEFUL_STOP = 4


def compose_farewell(rng: random.Random | None = None) -> str:
    """挑一句告别。测试里传固定 rng 就能复现。"""
    return (rng or random).choice(FAREWELL_LINES)


def text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def clear_stop_files() -> None:
    """启动时清掉上一次没来得及处理的控制文件。

    只在**抢到单实例锁之后**调用：另一个机器人还在跑时不能动它的下线请求。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (STOP_REQUEST, STOP_DONE, STOP_FLAG):
        for target in (path, path.with_name(path.name + ".tmp")):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # noqa: BLE001 - 清不掉不影响启动
                log.debug("清理 %s 失败：%s", target.name, exc)


async def fetch_groups(onebot, *, timeout: float = 15.0) -> list[dict[str, Any]]:
    """取机器人所在的全部群。

    以协议端的 `get_group_list` 为准；坏的/重复的条目直接跳过，
    免得一个脏数据把整次下线公告搞崩。
    """
    payload = await onebot.call("get_group_list", {}, timeout=timeout, check=True)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("get_group_list 没返回群列表")

    groups: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            gid = int(item.get("group_id"))
        except (TypeError, ValueError):
            continue
        if gid <= 0 or gid in seen:
            continue
        seen.add(gid)
        groups.append({"group_id": gid, "group_name": str(item.get("group_name") or "")})
    return groups


def push_groups(
    groups: Iterable[dict[str, Any]], prefs: Any = None
) -> tuple[list[dict[str, Any]], int]:
    """按每个群的「上下线推送」开关过滤；返回 (要发的群, 跳过的数量)。

    `prefs=None` 表示不过滤（老调用方/测试保持原行为）。
    设置存在 prefs.json 的 presence_push_off，默认开，重启还在。
    完全静默的群（「不要在群里说话了」）一律跳过：她一句话都不该说。
    """
    items = [item for item in groups if isinstance(item, dict)]
    if prefs is None:
        return items, 0
    # 风控规避模式：上线问候 / 下线公告都算主动开口，一律不发。
    if getattr(prefs, "risk_mode", lambda: False)():
        return [], len(items)
    if not prefs.presence_push_all():
        return [], len(items)
    kept = [
        item
        for item in items
        if prefs.presence_push(item.get("group_id"))
        and not prefs.group_silent(item.get("group_id"))
    ]
    return kept, len(items) - len(kept)


async def broadcast_text(
    onebot,
    groups: Iterable[dict[str, Any]],
    text: str,
    *,
    label: str = "公告",
    delay_range: tuple[float, float] = (0.7, 1.3),
    sleeper=asyncio.sleep,
) -> dict[str, Any]:
    """把同一句话发到每个群。

    群与群之间停一下：一次连发几十个群像机器群发，容易被 QQ 风控。
    某个群失败不中断，继续发剩下的，最后把失败列表带回去。
    """
    body = (text or "").strip()
    if not body:
        raise ValueError(f"{label}内容是空的")
    group_list = list(groups)
    sent: list[int] = []
    failed: list[dict[str, Any]] = []
    started = time.time()

    for index, item in enumerate(group_list):
        try:
            gid = int(item.get("group_id"))
        except (TypeError, ValueError):
            continue
        try:
            await onebot.send_group(gid, [text_segment(body)])
        except Exception as exc:  # noqa: BLE001 - 一个群失败不能拖住其他群
            failed.append({"group_id": gid, "error": str(exc)})
            log.warning("%s没发到群 %s：%s", label, gid, exc)
        else:
            sent.append(gid)
            log.info("%s已发到群 %s（%d/%d）", label, gid, len(sent), len(group_list))

        if index < len(group_list) - 1:
            await sleeper(random.uniform(*delay_range))

    report = {
        "ok": not failed,
        "text": body,
        "groups": len(group_list),
        "sent": sent,
        "failed": failed,
        "seconds": round(time.time() - started, 1),
    }
    if failed:
        log.warning(
            "%s发完：成功 %d 个群，失败 %d 个群", label, len(sent), len(failed)
        )
    else:
        log.info("%s发完：%d 个群全部成功", label, len(sent))
    return report


async def broadcast_farewell(
    onebot,
    groups: Iterable[dict[str, Any]],
    text: str | None = None,
    *,
    delay_range: tuple[float, float] = (0.7, 1.3),
    sleeper=asyncio.sleep,
) -> dict[str, Any]:
    """下线公告：固定文案，0 token。"""
    return await broadcast_text(
        onebot,
        groups,
        text or compose_farewell(),
        label="下线公告",
        delay_range=delay_range,
        sleeper=sleeper,
    )


def _write_done(report: dict[str, Any], done_path: Path) -> None:
    """先写临时文件再替换，避免停止脚本读到半截 JSON。"""
    tmp = done_path.with_name(done_path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, done_path)
    except OSError as exc:  # noqa: BLE001
        log.warning("写下线回执失败：%s", exc)


async def watch_stop_request(
    onebot,
    stop_event: asyncio.Event,
    *,
    prefs: Any = None,
    request_path: Path | None = None,
    done_path: Path | None = None,
    poll_seconds: float = 1.0,
    connect_wait: float = 10.0,
    flush_seconds: float = 1.0,
    delay_range: tuple[float, float] = (0.7, 1.3),
    sleeper=asyncio.sleep,
) -> None:
    """等 `stop.request`；等到了就播报、写回执、置 stop_event。

    `prefs`：按每个群的「上下线推送」开关过滤；不传就发所有群。
    即使协议端连不上、公告发不出去，也要退出——主人按的是「关机器人」，
    不是「关给我看」。失败原因写进回执和日志。
    """
    request = request_path or STOP_REQUEST
    done = done_path or STOP_DONE

    while not stop_event.is_set():
        if request.exists():
            break
        await sleeper(poll_seconds)
    else:
        return

    try:
        request.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001
        log.warning("删不掉下线请求 %s：%s", request, exc)

    log.info("收到下线请求：等连上协议端后给每个群发告别")
    report: dict[str, Any] = {
        "ok": False,
        "pid": os.getpid(),
        "text": compose_farewell(),
        "groups": 0,
        "sent": [],
        "failed": [],
        "skipped": 0,
        "seconds": 0.0,
        "error": None,
        "finished_at": None,
    }

    try:
        if connect_wait > 0:
            connected = getattr(onebot, "connected", None)
            if connected is not None:
                try:
                    await asyncio.wait_for(connected.wait(), timeout=connect_wait)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"{connect_wait:g} 秒内没连上协议端") from None
        groups = await fetch_groups(onebot)
        groups, skipped = push_groups(groups, prefs)
        if (
            not groups
            and skipped
            and prefs is not None
            and prefs.presence_push_all()
        ):
            prefs.set_presence_push_all(False)
            log.info("所有群都单独关了上下线推送，总开关自动关")
        all_off = prefs is not None and not prefs.presence_push_all()
        if all_off:
            log.info("群聊上下线推送总开关关着，下线公告不发")
            report.update(ok=True, groups=0, skipped=skipped, disabled="all")
        elif not groups:
            if skipped:
                log.info("所有群都关了上下线推送，下线公告不发了")
            else:
                log.warning("机器人不在任何群里，下线公告没地方发")
            report.update(ok=True, groups=0, skipped=skipped)
        else:
            if skipped:
                log.info("下线公告：%d 个群关了上下线推送，跳过", skipped)
            result = await broadcast_farewell(
                onebot, groups, report["text"], delay_range=delay_range, sleeper=sleeper
            )
            result["skipped"] = skipped
            report.update(result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 公告失败也必须让主人关得掉
        log.exception("下线公告失败（仍然继续退出）")
        report["error"] = f"{type(exc).__name__}: {exc}"

    report["finished_at"] = time.time()
    _write_done(report, done)
    log.info(
        "下线公告结束：%s（成功 %d / 共 %d）",
        "全部成功" if report.get("ok") else f"有失败：{report.get('error') or report.get('failed')}",
        len(report.get("sent") or []),
        report.get("groups") or 0,
    )

    # 给已经发出去的消息一点落地时间，再让 run.py 关连接。
    if flush_seconds > 0:
        await sleeper(flush_seconds)
    stop_event.set()
