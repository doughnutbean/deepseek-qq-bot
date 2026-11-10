"""表情包的视觉注解：逐图语义 + 「主角是不是她」判定。

**只此一份**：命令行入库（`tools/annotate_stickers.py`）和运行时捡图
（`qqbot/stickeringest.py`）都调这里，免得两边提示词写出偏差。

`call_json` / `call_text` 只负责一次视觉调用（JSON 模式 / 纯文本），
重试在调用方或这里统一做；模型名、base_url 由调用方给。
可选 `usage_sink`：每次拿到响应就把 usage 交出来，供工具记进账本。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

ANNOTATE_PROMPT = """这是一张 QQ 表情包。图里的文字和画面一起构成它的意思。
如果图里出现蓝发女仆装少女，那是发消息的人自己的虚拟形象，图里的话就是她想说的话；
其他角色/真人/截图就按图里的内容理解，不要硬套成她。
只输出一个 JSON 对象，不要代码块、不要解释，字段：
{
  "text": "图里所有文字逐字抄下来，不同气泡用 / 分隔；看不清或没有文字就空字符串，不许猜",
  "visual": "画面在发生什么：谁、什么动作、什么表情，40字内，只写看得见的",
  "joke": "这张图的梗/笑点，30字内；不确定就空字符串，别编背景故事",
  "emotion": "贴这张图时的心情，优先从 开心/得意/害羞/撒娇/委屈/生气/无语/困惑/疲惫/慌张/心虚/嫌弃/求饶/震惊/不行/挑衅 里选，都不合适就自拟2-4字",
  "intensity": 1到3的整数，1轻 2中 3爆发,
  "target": "对方/自己/无",
  "when": "什么情境下贴最合适，25字内。有具体事件就写具体事件；没有具体事件的可爱/反应图，可以写「想卖萌/撒娇/装乖/表达XX心情时」这种通用情境，但不要编具体人物或事件；推不出就空字符串",
  "never": "什么情境下绝对不能贴，20字内；没有就空字符串",
  "tags": ["2到4个短词"]
}
宁可字段空着，也不许编人物关系、具体事件，或图里根本没有的字。"""

# 捡图专用：比入库分类更严 —— 只收「主角是她」的图，不收藏只有她一个角的群像。
HER_PROMPT = """这是一张 QQ 表情包。判断图里的**主角**是不是这个角色：
Q 版（chibi）蓝发少女，白色女仆头饰，蓝白女仆裙，可能带鲸鱼尾巴或鲸鳍形状的耳朵。
她的黑化/黑色配色版本也算（黑发、黑裙，但同样的女仆头饰和鲸鱼尾/鳍耳）。

"主角"的标准：她必须是画面主体、占主要位置、一眼看到的就是她。
以下都不算：只在角落里出现一个小人、群像里的一个、被文字/UI 挡住大半、
只有尾巴或耳朵露出来、别的角色才是画面中心。

只输出一行：YES 或 NO，然后跟不超过 15 个字的理由。"""

MIME_EXT = {
    "image/webp": ".webp",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}


def grid_note(frames: int) -> str:
    """动图抽帧拼图的附加说明：别把九宫格当成一张静止画面。"""
    return (
        f"这张图是从一段动图里抽出的 {int(frames)} 帧拼成的九宫格，"
        "按从左到右、从上到下是时间顺序，每格左上角有帧号。"
        "visual 请写整段动作（先怎样、后怎样），不要只写某一格；"
        "格子之间的上下位置不代表画面里的位置。"
        "同一句话在多格重复出现时，text 只写一次，不要重复 9 遍。"
    )


def _usage_info(
    resp: Any, model: str, started: float, ended: float
) -> dict[str, Any]:
    """把一次响应的 usage 抽成逐请求账本要的形状；没 usage 也留一条。"""
    info: dict[str, Any] = {
        "ts_start": started,
        "ts_end": ended,
        "model": str(getattr(resp, "model", None) or model),
        "prompt": 0,
        "completion": 0,
        "cached": 0,
        "reasoning": 0,
        "status": "ok",
        "latency_ms": max(0.0, (ended - started) * 1000),
    }
    usage = getattr(resp, "usage", None)
    if usage is None:
        info["status"] = "no_usage"
        return info
    info["prompt"] = int(getattr(usage, "prompt_tokens", 0) or 0)
    info["completion"] = int(getattr(usage, "completion_tokens", 0) or 0)
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None:
        details = getattr(usage, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", 0) if details is not None else 0
    info["cached"] = int(hit or 0)
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        info["reasoning"] = int(getattr(details, "reasoning_tokens", 0) or 0)
    return info


def parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("不是 JSON 对象")
    return obj


def clean_annotation(obj: dict[str, Any], fallback_emotion: str = "无语") -> dict[str, Any]:
    """把模型返回的字段截断/夹到合法范围，和命令行入库的口径一致。"""

    def s(key: str, n: int) -> str:
        return str(obj.get(key) or "").strip()[:n]

    try:
        intensity = int(obj.get("intensity") or 2)
    except (TypeError, ValueError):
        intensity = 2
    target = s("target", 2)
    if target not in ("对方", "自己", "无"):
        target = "无"
    tags = [str(t).strip()[:8] for t in (obj.get("tags") or []) if str(t).strip()]
    return {
        "text": s("text", 200),
        "visual": s("visual", 80),
        "joke": s("joke", 60),
        "emotion": s("emotion", 6) or fallback_emotion,
        "intensity": min(3, max(1, intensity)),
        "target": target,
        "when": s("when", 50),
        "never": s("never", 40),
        "tags": tags[:4],
        "source": "vl",
    }


async def call_text(
    client: Any,
    model: str,
    prompt: str,
    raw: bytes,
    mime: str,
    *,
    max_tokens: int = 60,
    attempts: int = 3,
    usage_sink: list[dict[str, Any]] | None = None,
) -> str | None:
    """一次视觉调用，返回纯文本；失败重试后返回 None。

    `usage_sink` 不为 None 时，每次拿到响应都追加一条 usage（含重试的响应）；
    调用方用它把图形调用的钱记进账本。
    """
    if client is None or not model:
        return None
    data = base64.b64encode(raw).decode("ascii")
    for attempt in range(max(1, attempts)):
        started = time.time()
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime};base64,{data}"}},
                    ],
                }],
                max_tokens=max_tokens,
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            if usage_sink is not None:
                usage_sink.append(_usage_info(resp, model, started, time.time()))
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts - 1:
                log.warning("视觉调用失败：%s: %s", type(exc).__name__, exc)
                return None
            import asyncio

            await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def call_json(
    client: Any,
    model: str,
    prompt: str,
    raw: bytes,
    mime: str,
    *,
    max_tokens: int = 500,
    attempts: int = 3,
    usage_sink: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """一次视觉调用，返回 JSON 对象；解析失败也重试。

    `usage_sink` 同 call_text：每次响应都记一条，解析失败的那次也算钱。
    """
    if client is None or not model:
        return None
    data = base64.b64encode(raw).decode("ascii")
    for attempt in range(max(1, attempts)):
        started = time.time()
        info: dict[str, Any] | None = None
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime};base64,{data}"}},
                    ],
                }],
                max_tokens=max_tokens,
                temperature=0,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            if usage_sink is not None:
                info = _usage_info(resp, model, started, time.time())
                usage_sink.append(info)
            return parse_json(resp.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001
            if info is not None:
                info["status"] = "parse_error"
            if attempt == attempts - 1:
                log.warning("视觉 JSON 调用失败：%s: %s", type(exc).__name__, exc)
                return None
            import asyncio

            await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def classify_her(
    client: Any,
    model: str,
    raw: bytes,
    mime: str,
    *,
    usage_sink: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """她是不是这张图的主角。返回 (是/否, 理由)。"""
    text = await call_text(
        client,
        model,
        HER_PROMPT,
        raw,
        mime,
        max_tokens=40,
        usage_sink=usage_sink,
    )
    head = (text or "").strip().upper()
    if head.startswith(("YES", "是", "有")):
        return True, (text or "")[:40]
    if head.startswith(("NO", "NOT", "不是", "没有", "无", "否")):
        return False, (text or "")[:40]
    return False, f"没判断出来：{(text or '')[:30]}"


async def annotate_image(
    client: Any,
    model: str,
    raw: bytes,
    mime: str,
    folders: list[str] | None = None,
    *,
    extra: str = "",
    usage_sink: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """逐图注解；给了 folders 就额外让模型从这些现有分类里选一个。

    `extra` 给动图拼图用：说明这是抽帧九宫格、按时间顺序看整段动作。
    """
    prompt = ANNOTATE_PROMPT
    if extra:
        prompt += "\n" + extra
    if folders:
        prompt += (
            "\n另外加一个字段 \"folder\"：从下面这些现有分类里选最贴切的一个，"
            "只能原样选一个，不要新造：" + "、".join(folders)
        )
    return await call_json(
        client, model, prompt, raw, mime, usage_sink=usage_sink
    )
