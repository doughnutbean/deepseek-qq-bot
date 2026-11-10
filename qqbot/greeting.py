"""上线问候：整个机器人启动完成后，按人设和当前时间写一句，发到每个群。

和下线公告不同，这句是**真的走模型**的：
- 机器人（LLM 客户端）已经初始化、NapCat 也连上了，才开始写；
- system 里带完整 `persona.md` + 当前北京时间、星期和时段；
- 只让模型写一句 40 字以内的问候，要一个**具体的日常画面**，不要「本鱼回来了」套话；
- 写完再复用下线公告那套群发逻辑（`broadcast_text`），每群一条。

模型没写出可用内容（空串 / 只有套话）会**重试一次**；还不行这次就不发。
宁可这次没有上线问候，也不发一句固定的兜底句——那种"花了 token 却发了张模板"最亏。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from .farewell import broadcast_text, fetch_groups, push_groups
from .llm import clean_for_qq
from .pricetime import BEIJING

log = logging.getLogger(__name__)

# 这些是"没写新东西"的套话：模型只回这些就当这次没写成，重试/不发。
_CANNED_PHRASES = (
    "本鱼回来了",
    "刚才好像没了",
    "现在回来了",
    "本鱼回来啦",
)

# 时段只用于 prompt 和检查，不要求模型把钟点写进问候。
_PERIODS = (
    (5, 8, "清晨"),
    (8, 11, "上午"),
    (11, 13, "中午"),
    (13, 17, "下午"),
    (17, 19, "傍晚"),
    (19, 23, "晚上"),
)
_WEEKDAYS = "一二三四五六日"

# 模型可能顺手贴表情标签、网址、动作说明或“本鱼：”这种前缀；问候都不需要。
_TAG_RE = re.compile(r"\[(?:表情|图片|动画表情|语音|视频|文件)[^\]]*\]")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_LABEL_RE = re.compile(r"^(?:本鱼|问候|上线问候|群发)[：:]\s*")
_ACTION_RE = re.compile(r"^[（(][^）)]{0,30}[）)]\s*")

MAX_GREETING_CHARS = 60


def period_of(now: datetime) -> str:
    """把小时换成中文时段。23 点到 5 点算深夜。"""
    for start, end, name in _PERIODS:
        if start <= now.hour < end:
            return name
    return "深夜"


def build_prompt(
    persona: str, now: datetime, *, retry: bool = False
) -> tuple[str, str]:
    """拼上线问候的 system / user。单独拆出来，测试不用连模型。

    `retry=True` 是第一次只写出套话/空串后的第二次：要求更具体，
    不许再用「本鱼回来了」这类句子。
    """
    weekday = f"周{_WEEKDAYS[now.weekday()]}"
    system = (
        f"{persona}\n\n"
        "现在你刚完成热更新、重新上线。你要给所有群发一句上线问候，"
        "让群友知道你回来了。\n"
        f"当前是北京时间 {now:%Y-%m-%d} {now:%H:%M}，{weekday}，{period_of(now)}。\n"
        "要求：\n"
        "- 只写一句，40 字以内，口语，像群里的朋友打招呼，不要公告腔、客服腔。\n"
        "- 可以按当前时段说早上好/中午好/晚上好/这么晚还没睡这类话，"
        "但不要报具体钟点，也不要在句子里写几点几分。\n"
        "- 自称本鱼；这是发到所有群的，不要叫主人、不要@任何人、不要提群名。\n"
        "- 必须带一个具体的画面/动作/声音（从人设里的「小日常」找），只挑一个，"
        "别堆砌，也别编人设之外的新设定。\n"
        "- 不要写「本鱼回来了」「刚才好像没了」「现在回来了」这类套话；"
        "不要复述 prompt 里的例子。\n"
        "- 不要 Markdown、不要引号、不要括号里的动作说明、不要解释。\n"
        "- 直接输出这一句问候。"
    )
    user = "写一句你重新上线后发到群里的问候，带一个具体的日常画面。"
    if retry:
        system += (
            "\n\n上一次写成了套话，或者没有具体的画面。"
            "这次必须换一个具体的动作、物件或声音，不要用「回来了」「刚才没了」开头，"
            "也不要解释。"
        )
        user = "换一句具体的，别用套话。"
    return system, user


def sanitize_greeting(text: str) -> str:
    """把模型输出收拾成能直接发 QQ 的一句：去引号/表情标签/网址/换行。"""
    body = clean_for_qq(text or "")
    body = _TAG_RE.sub("", body)
    body = _URL_RE.sub("", body)
    body = body.strip().strip("\"'“”‘’「」")
    body = _LABEL_RE.sub("", body)
    body = _ACTION_RE.sub("", body)
    body = " ".join(part.strip() for part in body.splitlines() if part.strip())
    body = body.strip()
    if len(body) > MAX_GREETING_CHARS:
        body = body[: MAX_GREETING_CHARS - 1].rstrip("，。！？～ ") + "～"
    return body


def looks_canned(text: str) -> bool:
    """这句话是不是只有套话、没有具体画面。空串也算不能用。"""
    body = (text or "").strip()
    if not body:
        return True
    return any(phrase in body for phrase in _CANNED_PHRASES)


async def startup_greeting(
    bot,
    onebot,
    *,
    stop_event: asyncio.Event | None = None,
    delay: float = 2.0,
    sleeper=asyncio.sleep,
    now: datetime | None = None,
) -> dict[str, Any]:
    """等一小会儿，让模型写问候并群发。

    run.py 负责等 NapCat 连上再调用；这里再停 `delay` 秒，避免上线问候
    和连接日志/后台任务抢同一瞬间。`stop_event` 已置位就直接跳过，
    免得主人刚按停止，问候又紧跟着发出去。
    """
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "skipped": "stopping"}
    if delay > 0:
        await sleeper(delay)
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "skipped": "stopping"}
    if getattr(bot.config, "enable_risk_mode", False) or getattr(
        bot.prefs, "risk_mode", lambda: False
    )():
        log.info("风控规避模式开着，上线问候不发")
        return {
            "ok": True,
            "groups": 0,
            "sent": [],
            "failed": [],
            "skipped": 0,
            "disabled": "risk_mode",
            "text": "",
        }

    try:
        groups = await fetch_groups(onebot)
    except Exception as exc:  # noqa: BLE001 - 问候失败不影响聊天
        log.warning("上线问候取群列表失败（不影响使用）：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    prefs = getattr(bot, "prefs", None)
    groups, skipped = push_groups(groups, prefs)
    if (
        not groups
        and skipped
        and prefs is not None
        and prefs.presence_push_all()
    ):
        # 老数据/手改 prefs：所有群都单独关了，总开关自动跟上
        prefs.set_presence_push_all(False)
        log.info("所有群都单独关了上下线推送，总开关自动关")
    all_off = prefs is not None and not prefs.presence_push_all()
    if all_off:
        log.info("群聊上下线推送总开关关着，上线问候不发")
        return {
            "ok": True,
            "groups": 0,
            "sent": [],
            "failed": [],
            "skipped": skipped,
            "disabled": "all",
            "text": "",
        }
    if skipped:
        log.info("上线问候：%d 个群关了上下线推送，跳过", skipped)
    if not groups:
        if skipped:
            log.info("所有群都关了上下线推送，上线问候不发了")
            return {
                "ok": True,
                "groups": 0,
                "sent": [],
                "failed": [],
                "skipped": skipped,
                "text": "",
            }
        log.warning("机器人不在任何群里，上线问候没地方发")
        return {
            "ok": True,
            "groups": 0,
            "sent": [],
            "failed": [],
            "skipped": 0,
            "text": "",
        }

    now = now or datetime.now(BEIJING)
    # task 会进账本，日报里能看出这次花销是上线问候，不是聊天。
    ctx: dict[str, Any] = {"task": "startup_greeting"}
    text = ""
    for attempt in range(2):
        system, user = build_prompt(bot.config.persona, now, retry=attempt > 0)
        try:
            raw = await bot.llm.compose(system, user, ctx)
        except Exception as exc:  # noqa: BLE001 - 问候不能拖住启动
            log.warning("上线问候生成失败（第 %d 次）：%s", attempt + 1, exc)
            raw = ""
        text = sanitize_greeting(raw)
        if text and not looks_canned(text):
            break
        log.info(
            "上线问候第 %d 次没写出具体内容（空串/套话）：%s；%s",
            attempt + 1,
            (raw or "")[:60] or "（空）",
            "重试一次" if attempt == 0 else "这次不发",
        )
        text = ""
    try:
        bot._record_usage("startup", ctx)
    except Exception:  # noqa: BLE001 - 记账失败不影响发问候
        log.debug("上线问候记账失败", exc_info=True)

    if not text:
        # 宁可这次没有问候，也不发一句固定套话；花掉的 token 已经记账。
        log.warning("上线问候没写出可用内容，这次不发（避免固定套话）")
        return {
            "ok": False,
            "error": "empty_greeting",
            "groups": len(groups),
            "sent": [],
            "failed": [],
            "skipped": skipped,
            "text": "",
        }

    log.info("上线问候：%s", text)

    if stop_event is not None and stop_event.is_set():
        log.info("机器人正在下线，上线问候不发了")
        return {"ok": False, "skipped": "stopping", "text": text}

    report = await broadcast_text(
        onebot,
        groups,
        text,
        label="上线问候",
        delay_range=(0.7, 1.3),
        sleeper=sleeper,
    )
    report["skipped"] = skipped
    return report
