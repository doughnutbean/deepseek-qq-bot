"""DeepSeek 调用层：普通对话 + 工具调用（联网搜索）+ 图像理解。

关于模型名：`deepseek-chat` / `deepseek-reasoner` 已在 2026-07-24 停用，
现在用 `deepseek-flash`（带视觉能力，便宜快）或 `deepseek-v4-pro`（更强更贵）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from openai import AsyncOpenAI

from . import antislop, saycheck
from .config import Config
from .friends import FriendTools
from .selftools import SelfTools
from .spendtool import is_spend_question
from .stickertools import StickerTools
from .tool_spec import validate_providers, warn_problems
from .tools import SearchTools
from .turn import Turn

log = logging.getLogger(__name__)

# 一次提问里最多允许模型连续调用几轮工具，防止死循环烧钱
MAX_TOOL_ROUNDS = 3

# 输出上限：不开思考时 2048 够用；开思考要另外给思维链留预算，
# 否则难题会把 2048 全花在 reasoning 上，正文为空、工具调用也可能被截断
MAX_TOKENS = 2048
THINKING_MAX_TOKENS = 8192

# DeepSeek 的工具调用协议标签。正常情况下它们只出现在 tool_calls 字段里、
# 由接口解析掉；但「工具轮数用完、不再给工具」的那一轮，模型会把
# 「我还想再搜」直接写成协议原文塞进正文（2026-10-24 群聊、2026-10-27
# 15:45 私聊「多看几个切片」各实锤一次），发到 QQ 就是一串乱码。
_TOOL_PROTOCOL = re.compile(r"</?[|｜]{1,2}\s*DSML\s*[|｜]{1,2}", re.IGNORECASE)
# 其他模型漏出来的特殊 token，同样不该出现在聊天里
_SPECIAL_TOKEN = re.compile(r"<\|[A-Za-z_][A-Za-z0-9_]*\|>")

# 撞上上面那种情况时补的一句：只许拿已经查到的资料回答
_NO_TOOL_NUDGE = (
    "（后台提示：这轮不能再调用工具了。不要输出任何工具调用标签，"
    "直接用已经拿到的资料，把答案说完。）"
)

# 把 Markdown 痕迹抹掉：QQ 里不渲染 **粗体**、### 标题、`代码`、表格管线
_MD_PATTERNS = [
    (re.compile(r"```[\s\S]*?```"), lambda m: m.group(0).strip("`").strip()),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"\1"),
    # 注意用 [ \t] 而不是 \s：\s 会连行尾换行一起吃掉，段落间距就没了
    (re.compile(r"^#{1,6}[ \t]*", re.MULTILINE), ""),
    (re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE), "· "),
    (re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
]


def has_tool_markup(text: str) -> bool:
    """这段正文里有没有混进工具调用协议（它不该出现在正文里）。"""
    return bool(_TOOL_PROTOCOL.search(text) or _SPECIAL_TOKEN.search(text))


def strip_tool_markup(text: str) -> str:
    """切掉漏进正文的工具调用协议。

    协议一旦开头，后面全是 `invoke` / `parameter` 这些参数，没有一句是给人看的，
    所以从第一个标签起整段丢掉；标签前面真说了什么（「本鱼再找找」）留着。
    """
    match = _TOOL_PROTOCOL.search(text)
    if match:
        text = text[: match.start()]
    return _SPECIAL_TOKEN.sub("", text)


def clean_for_qq(text: str) -> str:
    text = strip_tool_markup(text.strip()).strip()
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    # 群聊/私聊身份标记只给模型看，别让她写进回复
    text = text.replace("【主人】", "").replace("【群友】", "").replace("【非主人】", "")
    # 主人的真名只在人设里让她知道，一句都不许发出去；说漏了就用网名顶上
    text = text.replace("杨镕铭", "帕鲁")
    text = text.replace("\r\n", "\n").strip()
    return text


def split_messages(text: str, max_segments: int = 3) -> list[str]:
    """按换行拆成多条独立消息 —— QQ 里分两条发，比一条里换行更像人。

    代码块、表格、列表不拆（拆了就断）；单行太长也不拆，交给长度分段。
    """
    body = (text or "").strip()
    if not body:
        return [text]
    if "```" in body or "\t" in body:
        return [text]
    parts = [line.strip() for line in body.splitlines() if line.strip()]
    if len(parts) < 2 or len(parts) > max(1, max_segments):
        return [text]
    if any(len(p) > 80 for p in parts):
        return [text]
    if any(p.startswith(("- ", "* ", "1.", "2.", "3.", "•")) for p in parts):
        return [text]
    return parts


def split_text(text: str, limit: int) -> list[str]:
    """QQ 单条消息有长度上限，按段落切开而不是硬截断。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        candidate = f"{buf}\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks[:3]  # 最多刷 3 条，再多就是刷屏了


def _own_recent(ctx: dict[str, Any]) -> list[str]:
    """同一会话里她自己最近说过的话（旧在前）。

    只给 `saycheck` 查「是不是又把刚才那句换个词说了一遍」。读不到记忆就返回空：
    这个检查是锦上添花，不能因为它回不了话。带套话的旧句子剔掉 ——
    拿旧八股当参照物，会把新八股判成「正常」。
    """
    mem = ctx.get("memory")
    session = ctx.get("session")
    if mem is None or not session:
        return []
    try:
        turns = mem.recent(str(session), 40)
    except Exception:  # noqa: BLE001 - 记忆坏了也照样说话
        return []
    out = [
        str(t.get("content") or "").strip()
        for t in turns
        if t.get("role") == "assistant"
    ]
    return [t for t in out if t and not antislop.find(t)]


class LLM:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: AsyncOpenAI | None = None
        self.tools: SearchTools | None = None
        self.self_tools = SelfTools(config)
        self.friend_tools = FriendTools(config)
        self.sticker_tools = StickerTools()
        if not config.dry_run:
            self._client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, timeout=120.0)
            if config.enable_search:
                self.tools = SearchTools(
                    config.api_key,
                    config.base_url,
                    knowledge_ttl_days=config.knowledge_ttl_days,
                    reuse_minutes=config.search_reuse_minutes,
                    chris_db=config.chris_db,
                    memory_db=config.memory_db,
                )

    async def aclose(self) -> None:
        if self.tools is not None:
            await self.tools.aclose()

    @property
    def client(self) -> AsyncOpenAI | None:
        """给表情包入库这类「旁路视觉调用」复用同一个客户端。"""
        return self._client

    def _thinking_params(self) -> tuple[dict[str, Any], str | None]:
        """返回 (extra_body, reasoning_effort)。

        官方约定：`thinking` 放 extra_body，`reasoning_effort` 是顶层参数；
        强度映射：minimal/low→low，medium/high/xhigh→high，max/ultra→max。
        """
        mode = (self.config.thinking or "").strip().lower()
        if mode == "auto":
            if "deepseek" not in self.config.base_url:
                return {}, None
            mode = "off"
        if mode in {"off", "disabled", "none"}:
            return {"thinking": {"type": "disabled"}}, None
        if mode in {"minimal", "low"}:
            return {"thinking": {"type": "enabled"}}, "low"
        if mode in {"medium", "high", "xhigh", "enabled", "on"}:
            return {"thinking": {"type": "enabled"}}, "high"
        if mode in {"max", "ultra"}:
            return {"thinking": {"type": "enabled"}}, "max"
        log.warning("DEEPSEEK_THINKING=%r 不认识，按 disabled 处理", self.config.thinking)
        return {"thinking": {"type": "disabled"}}, None

    @staticmethod
    def _max_tokens(effort: str | None) -> int:
        """思考模式要额外给思维链留预算。

        2026-10-26 实测同一道代码题：low 思维链 954 token、high 3329 token；
        max_tokens 还是 2048 时，两个档位都会把预算全花在思维链上、正文为空。
        """
        return THINKING_MAX_TOKENS if effort else MAX_TOKENS

    async def _create(self, messages: list[dict[str, Any]], tools: list[dict] | None):
        """带重试的单次调用。"""
        assert self._client is not None
        last_error: Exception | None = None
        extra_body, effort = self._thinking_params()
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self._max_tokens(effort),
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if effort:
            kwargs["reasoning_effort"] = effort
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        self._last_call: tuple[float, float] | None = None
        for attempt in range(3):
            attempt_start = time.time()
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                choice = resp.choices[0] if resp.choices else None
                if getattr(choice, "finish_reason", None) == "length":
                    # 截断不报错，只会表现为「正文空」「工具参数不是合法 JSON」
                    log.warning(
                        "输出被截断（max_tokens=%s，%s）",
                        kwargs["max_tokens"],
                        "带工具" if tools else "无工具",
                    )
                # 成功的那次请求的起止时间。调用方在 await 返回后立即
                # _collect_usage（中间没有 await），单事件循环下不会串到别的会话。
                self._last_call = (attempt_start, time.time())
                return resp
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                status = getattr(exc, "status_code", None)
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    wait = 2 ** attempt
                    log.warning("DeepSeek 返回 %s，%s 秒后重试", status, wait)
                    await asyncio.sleep(wait)
                    continue
                break
        log.error("调用 DeepSeek 失败: %s", last_error)
        raise RuntimeError(f"模型调用失败: {type(last_error).__name__}") from last_error

    def _collect_usage(self, ctx: dict[str, Any], resp: Any) -> None:
        """把这次调用的 token 用量累加进 ctx，并给逐请求账本留一条 call。

        一轮回复可能调好几次模型（工具循环/套话重试），所以是累加不是覆盖；
        `ctx["calls"]` 每条对应一次真实请求，由 app 按各自的 ts_start 取峰谷价。
        """
        usage = getattr(resp, "usage", None)
        mark = getattr(self, "_last_call", None)
        self._last_call = None
        now = time.time()
        if mark is not None:
            started, ended = float(mark[0]), float(mark[1])
        else:
            started = float(ctx.get("ts_start") or now)
            ended = started
        ctx["requests"] = int(ctx.get("requests", 0) or 0) + 1
        bucket = ctx.setdefault("usage", {"prompt": 0, "completion": 0, "cached": 0})
        call: dict[str, Any] = {
            "ts_start": started,
            "ts_end": ended,
            "model": str(getattr(resp, "model", None) or self.config.model),
            "prompt": 0,
            "completion": 0,
            "cached": 0,
            "reasoning": 0,
            "status": "ok",
            "latency_ms": max(0.0, (ended - started) * 1000) if mark is not None else None,
        }
        if usage is None:
            log.warning("模型响应没有 usage，这一请求无法计费")
            call["status"] = "no_usage"
            ctx.setdefault("calls", []).append(call)
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        # 缓存命中：DeepSeek 叫 prompt_cache_hit_tokens，OpenAI 叫 prompt_tokens_details.cached_tokens
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit is None:
            details = getattr(usage, "prompt_tokens_details", None)
            hit = getattr(details, "cached_tokens", 0) if details is not None else 0
        reasoning = 0
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None:
            reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
        call.update(
            {
                "prompt": prompt,
                "completion": completion,
                "cached": int(hit or 0),
                "reasoning": reasoning,
            }
        )
        ctx.setdefault("calls", []).append(call)
        bucket["prompt"] += prompt
        bucket["completion"] += completion
        bucket["cached"] += int(hit or 0)
        ctx.setdefault("model", self.config.model)

    async def judge_image(self, blocks: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
        """看图审核：一次极短的调用，返回 ``porn`` / ``vulgar`` / ``none``。

        只有自动拉黑用得到 —— 本地看不见图，这是她唯一会为审核花 token 的地方。
        **失败返回空串**（调用方当正常处理、并自己决定要不要暂停审查）：
        宁可放过一张，也不能因为接口抖动把人误拉黑。
        """
        if self.config.dry_run or self._client is None or not blocks:
            return ""
        system = (
            "你是内容安全审核。只看图，回一个词，不要解释、不要标点："
            "porn（色情、暴露）、vulgar（脏话、辱骂文字）、none（正常）。"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图属于哪一类？只回一个词。"},
                    *blocks,
                ],
            },
        ]
        self._last_call = None
        call_start = time.time()
        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    max_tokens=8,
                    extra_body={"thinking": {"type": "disabled"}},
                ),
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - 审核失败不能影响聊天
            log.warning("看图审核失败（当正常处理）：%s", type(exc).__name__)
            return ""
        self._last_call = (call_start, time.time())
        self._collect_usage(ctx, resp)
        answer = (resp.choices[0].message.content or "").strip().lower()
        for word in ("porn", "vulgar"):
            if word in answer:
                return word
        return "none"

    async def compose(
        self,
        system: str,
        user: str,
        ctx: dict[str, Any] | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        """短生成：不带工具、不带历史（上线问候用）。

        和 reply() 分开，是因为问候不需要搜索/改资料这些工具；给模型工具它
        反而可能先查一圈再说话。usage 仍然记进 ctx，由调用方入账。
        `images` 是 OpenAI 图片内容块（B 站分镜直接给模型看）。
        """
        if ctx is None:
            ctx = {}
        if self.config.dry_run:
            await asyncio.sleep(0.2)
            return "本鱼回来了，刚才好像没了～"
        ctx.setdefault("ts_start", time.time())
        ctx.setdefault("requests", 0)
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}, *images]
        resp = await self._create(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            None,
        )
        self._collect_usage(ctx, resp)
        return clean_for_qq(resp.choices[0].message.content or "")

    async def reply(
        self,
        system: str,
        messages: list[dict[str, Any]],
        ori_question: str = "",
        ctx: dict[str, Any] | None = None,
    ) -> str:
        """messages 已经包含本轮用户消息（可能是多模态 content 数组）。

        ctx 里带 onebot / is_owner / last_image，供工具执行时使用。
        """
        if ctx is None:
            ctx = {}
        # 工具计数器要在造 Turn 之前放进去：Turn 拿走的是那一刻的 ctx 快照，
        # 晚了（以前在 providers 之前才 setdefault）工具就看不到这个 dict，
        # 搜索/抓取的次数会丢（2026-11-05 单轨化后 test_tool_cost 抓到的）。
        ctx.setdefault("tool_charges", {"search": 0, "fetch": 0})
        # 工具的新入口：这一轮是谁、在哪儿、问的什么。老代码仍读 **ctx，
        # 新工具（以及将来的外部 runtime）只认这个对象。
        turn = Turn.from_ctx({**ctx, "ori_question": ori_question})
        if self.config.dry_run:
            last = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    content = m.get("content")
                    if isinstance(content, str):
                        last = content
                    else:
                        last = " ".join(
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    break
            await asyncio.sleep(0.2)
            return f"[dry-run] 我收到了「{last}」。填好 DEEPSEEK_API_KEY 并关掉 dry-run 就会真的回答。"

        convo: list[dict[str, Any]] = [{"role": "system", "content": system}, *messages]

        # 这一轮第一次请求的时刻：峰谷按它取价，不再用回合结束时刻。
        ctx.setdefault("ts_start", time.time())
        ctx.setdefault("requests", 0)

        # 工具提供者：搜索对所有会话开放；改自己资料只对主人开放，
        # 不开放时连 schema 都不给模型，避免它在群里答应做不到的事
        providers: list[Any] = []
        if self.tools is not None:
            providers.append(self.tools)
        # 按特征找本地表情包：有图库就给，用户点名要哪张时用它
        if ctx.get("stickers") is not None and getattr(ctx["stickers"], "available", False):
            providers.append(self.sticker_tools)
        # 上线补收：回复的是几小时前的旧消息，不能让模型顺着旧话
        # 真去改资料、删好友、私聊主人或写记忆。只留只读工具。
        if ctx.get("catchup"):
            schemas = [s for p in providers for s in p.schemas()] or None
        else:
            # B站画面：模型自己判断「这句是不是真的需要看画面」，再调工具抽帧。
            # 抽帧要下整段视频，provider 里有按天额度和失败冷却；补收不给。
            frames_tools = ctx.get("frames_tools")
            if frames_tools is not None:
                providers.append(frames_tools)
            # 直播画面：只挂给「正在陪看」的会话（app 已经按会话判过）；
            # 她后台一直在抓帧，这里让她能主动「再看一眼」。
            live_frame_tools = ctx.get("live_frame_tools")
            if live_frame_tools is not None:
                providers.append(live_frame_tools)
            # 记忆工具：群聊/私聊都能用，只写"当前说话人"的事实。
            if ctx.get("memory_tools") is not None:
                providers.append(ctx["memory_tools"])
            if ctx.get("is_owner"):
                # 改资料的工具只在主人私聊里给（群里标【主人】只为认人）
                providers.append(self.self_tools)
            # 日记：只给主人私聊。群聊/群友连 schema 都拿不到，run() 里还会再挡。
            diary_tools = ctx.get("diary_tools")
            if (
                diary_tools is not None
                and ctx.get("is_owner")
                and str(ctx.get("session", "")).startswith("private:")
                and not ctx.get("catchup")
            ):
                providers.append(diary_tools)
            # 直播工具：只给主人，但**群里也给** —— 陪看不动资料，没有越权风险。
            # 2026-11-04 实锤：主人在群里说「开启直播陪看Chris」她开不起来，
            # 因为工具跟着 `is_owner` 一起被挡在群外，模型手里什么都没有，
            # 只能编一句「这会儿开不起来」。群友说话时 owner_live 是 False。
            live_tools = ctx.get("live_tools")
            if live_tools is not None and (
                ctx.get("is_owner") or ctx.get("owner_live")
            ):
                providers.append(live_tools)
            # 她自己的 B 站生活：手动刷一轮/查状态，只给主人私聊；
            # 群里不给，避免当众触发写操作。
            life_tools = ctx.get("bili_life_tools")
            if life_tools is not None and ctx.get("is_owner") and not ctx.get("catchup"):
                providers.append(life_tools)
            # 不懂就问：只有群聊里给；私聊没人能回答，别给模型这个工具
            meme_ask = ctx.get("meme_ask")
            if (
                meme_ask is not None
                and getattr(meme_ask, "enabled", False)
                and str(ctx.get("session", "")).startswith("group:")
            ):
                providers.append(meme_ask)
            # 有事真的私聊主人：群聊 / 非主人私聊里才给；
            # 主人就在私聊里，直接说就行，不用再"问主人"。
            if ctx.get("owner_ask") is not None and not ctx.get("is_owner"):
                providers.append(ctx["owner_ask"])
            # 主人私聊里：主人回话后，把答复真的转回原来的群/人
            if ctx.get("owner_reply") is not None and ctx.get("is_owner"):
                providers.append(ctx["owner_reply"])
            # 群友查账：只有群里、且这一句确实在问「今天花了多少」时，
            # 才把只读账本工具递过去；别的时候连 schema 都不给。
            spend_tools = ctx.get("spend_tools")
            if (
                spend_tools is not None
                and str(ctx.get("session", "")).startswith("group:")
                and is_spend_question(ori_question)
            ):
                providers.append(spend_tools)
            # 本地整句指令的第二层：本地 parser 没认出来的那一轮，才让模型
            # 自己判断「这是不是主人在调我」；只给主人，补收的旧消息不给
            # （过期的「打开xxx」不能隔几小时再执行）。
            command_tools = ctx.get("command_tools")
            if (
                command_tools is not None
                and ctx.get("is_owner")
                and not ctx.get("catchup")
            ):
                providers.append(command_tools)
            # 退群：只有主人能调；群号校验和二次权限在 GroupTools.run 里。
            # 补收的旧消息不给，避免几小时前的「退群」现在才生效。
            group_tools = ctx.get("group_tools")
            if (
                group_tools is not None
                and ctx.get("is_owner")
                and not ctx.get("catchup")
            ):
                providers.append(group_tools)
            # 好友状态：只读，谁问都能查。补收的旧消息不给（状态可能已经变了）。
            friend_tools = ctx.get("friend_tools")
            if friend_tools is not None and not ctx.get("catchup"):
                providers.append(friend_tools)
            # 找同款肥鱼：只在有这个工具的群里挂（app 先查过本群有没有别的鱼），
            # 私聊和上线补收不给——补收是几小时前的旧话，不能现在去 @ 人。
            fish_tools = ctx.get("fish_tools")
            if (
                fish_tools is not None
                and str(ctx.get("session", "")).startswith("group:")
                and not ctx.get("catchup")
            ):
                providers.append(fish_tools)
            # 删好友只在私聊里给，而且删的永远是"正在跟她说话的那个人"
            if str(ctx.get("session", "")).startswith("private:"):
                providers.append(self.friend_tools)
            schemas = [s for p in providers for s in p.schemas()] or None

        # 工具格式自检：新工具没按 docs/工具规范.md 写，会在这里记 warning。
        # 收尾的 tools/check_tools.py 会把同一批问题直接跑红。
        warn_problems(validate_providers(providers))

        # Search / Fetch 是按成功次数计费的，不在 usage 的 token 里。
        # 把计数器挂在 ctx 上，工具执行成功时 +1，回合结束由 app 记进账本。
        tool_charges = ctx.setdefault("tool_charges", {"search": 0, "fetch": 0})

        answer = ""
        answer_reasoning: Any = None
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            # 最后一轮不再给工具，逼它直接给答案
            use_tools = schemas if round_no < MAX_TOOL_ROUNDS else None
            resp = await self._create(convo, use_tools)
            self._collect_usage(ctx, resp)
            msg = resp.choices[0].message

            if not msg.tool_calls:
                content = msg.content or ""
                if has_tool_markup(content):
                    # 工具收走的那一轮，模型还想要工具，就把协议原文当正文吐了。
                    # 直接发出去是乱码，也不能默默丢掉这次回答：带着提示再问一次，
                    # 只许用已经查到的资料说。（多花一次请求，只在这种漏格式时发生）
                    log.warning(
                        "正文漏出工具调用格式，改成直接回答：%s",
                        content[:60].replace("\n", " "),
                    )
                    convo.append({"role": "user", "content": _NO_TOOL_NUDGE})
                    resp = await self._create(convo, None)
                    self._collect_usage(ctx, resp)
                    msg = resp.choices[0].message
                    content = msg.content or ""
                answer = clean_for_qq(content)
                answer_reasoning = getattr(msg, "reasoning_content", None)
                break

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                # 思考模式 + tools 时，reasoning_content 必须回传，否则 400
                assistant_msg["reasoning_content"] = reasoning
            assistant_msg["tool_calls"] = [
                {
                    "id": t.id,
                    "type": "function",
                    "function": {"name": t.function.name, "arguments": t.function.arguments},
                }
                for t in msg.tool_calls
            ]
            convo.append(assistant_msg)

            for call in msg.tool_calls:
                name = call.function.name
                log.info("模型调用工具 %s(%s)", name, (call.function.arguments or "")[:120])
                # 记下这轮用了什么工具：搜过东西的答复值得再补一句
                ctx.setdefault("tools_used", []).append(name)
                result: str | None = None
                for provider in providers:
                    # 单轨：工具只认 `turn`（见 qqbot/turn.py）。换 runtime 时
                    # 只要造得出一个 Turn，所有工具都不用动。
                    result = await provider.run(
                        name,
                        call.function.arguments or "{}",
                        turn=turn,
                    )
                    if result is not None:
                        break
                if result is None:
                    result = f"（未知工具：{name}）"
                convo.append({"role": "tool", "tool_call_id": call.id, "content": result})

            # 本地指令已经执行、回执也发出去了：不用再让模型复述一遍。
            # app 看到 `command_state.done` 会把这一轮正文丢掉。
            state = ctx.get("command_state") or {}
            if isinstance(state, dict) and state.get("done"):
                log.info("本地指令已执行，跳过模型复述")
                return ""
        else:
            return "（这个问题查得有点复杂，超出我的工具调用轮数了，换个问法试试？）"

        if not answer:
            return "（模型这次没有输出内容）"
        return await self._humanize(answer, convo, ctx, answer_reasoning)

    async def _humanize(
        self,
        answer: str,
        convo: list[dict[str, Any]],
        ctx: dict[str, Any],
        reasoning: Any = None,
    ) -> str:
        """命中"鱼八股"或"太像 AI 写的"就带着提示重说一次；还带就删掉那一小句。

        只多一次调用，且只在命中时发生。不改写全文，避免把人设改没。

        两个检查共用这一次重说：
        - `antislop`：固定套话（这条我认 / 本鱼记下了），硬证据；
        - `saycheck`：作文腔叠在一起、跟同一会话里她刚说过的话太像（软信号）。
        只有在 `ctx["saycheck"]` 为真时才跑后者（群里跑，私聊按人设说）；
        有套话时只按套话重说，不叠第二次调用。
        """
        hits = antislop.find(answer)
        use_style = bool(ctx.get("saycheck")) and bool(answer)
        recent = _own_recent(ctx) if use_style else []
        style = saycheck.check(answer, recent) if use_style else saycheck.Result()
        if not hits and style.ok:
            return answer
        # 气头上不重说：重说一遍多半会把气话洗成软话，情绪就断了。
        # 直接走"删掉那一小句"这条兜底，不动整句。
        if ctx.get("skip_slop_retry"):
            return antislop.strip(answer) or answer
        if hits:
            log.info("检测到套话：%s", "、".join(hits[:3]))
            ctx.setdefault("slop", []).extend(hits[:5])
            note = antislop.retry_note(hits[0])
        else:
            log.info("说人话复查：%s", "；".join(style.problems[:2]))
            ctx.setdefault("saycheck_hits", []).extend(style.problems[:3])
            note = saycheck.retry_note(style.problems)
        retry_turn: dict[str, Any] = {"role": "assistant", "content": answer}
        if reasoning:
            # 思考模式下回传，避免部分接口把它当成缺字段
            retry_turn["reasoning_content"] = reasoning
        try:
            resp = await self._create(
                [*convo, retry_turn, {"role": "user", "content": note}],
                None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("重说失败（%s），改成清理后发送", type(exc).__name__)
            if not hits:
                return answer
            return antislop.strip(answer) or antislop.FALLBACK_REPLY
        self._collect_usage(ctx, resp)
        cand = clean_for_qq(resp.choices[0].message.content or "")
        if cand and not antislop.find(cand):
            if hits or saycheck.check(cand, recent).ok:
                return cand
        if not hits:
            # 重说一遍还是像 AI：原句比改坏的那句强，按原样发，别硬改
            return answer
        fixed = antislop.strip(cand) if cand else ""
        if fixed:
            return fixed
        return antislop.strip(answer) or antislop.FALLBACK_REPLY
