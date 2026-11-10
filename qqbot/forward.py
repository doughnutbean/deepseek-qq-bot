"""把 QQ 的「合并转发」展开成模型能读的文字。

OneBot 11 里合并转发有两种常见形态：

    {"type": "forward", "data": {"id": "xxxx"}}
    {"type": "json", "data": {"data": "{\\"app\\":\\"com.tencent.multimsg\\",...}"}}

真正的聊天记录不在消息段里，要拿 id 去调 ``get_forward_msg``。本模块只做
「读取 + 文本化 + 预算控制」，不调用模型；要不要回、怎么回由 app.py 决定。

几条刻意的边界：

- 只读文字和图片。语音/视频/文件/卡片只留占位符，不猜内容。
- 节点数、字符数、调用次数、递归深度都有硬上限，防止一份超大转发把
  一轮对话撑爆。
- 转发里的人不是当前群友；这里只负责把事实读出来，提示词边界在 app.py。
- 传了 ``self_id`` 时，记录里她自己发的节点会在行首标 ``（你）``：别的鱼
  也可能叫「大肥鱼」、也自称「本鱼」，光看名字分不出是不是她。
- 读取失败不抛异常，返回 error 字段，让上层退回原来的占位符行为。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# 单次 get_forward_msg 的超时（秒）。转发是本地 NapCat 数据，不该等太久。
CALL_TIMEOUT = 10.0

# 记录里她自己发的节点行首标记。别的号也可能叫「大肥鱼」/自称「本鱼」，
# 只靠昵称认不出是不是她，所以用 QQ 号判断、单独标出来。
SELF_MARK = "（你）"

_CQ_RE = re.compile(r"\[CQ:([a-zA-Z]+)((?:,[^\]]*)?)\]")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_CQ_STRIP_RE = re.compile(r"\[CQ:[^\]]*\]")
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|语音|视频|文件|卡片|引用|嵌套|合并转发|@)[^\]]*\]"
)
_CLEAN_RE = re.compile(r"[\s\W_]+", re.UNICODE)

# 只有这些词/笑声的转发，算“没有实质聊天内容”。宁可保守：像“明天开会”
# 这种两三个字的实际内容必须留下。
_FILLER_RE = re.compile(
    r"^(?:"
    r"哈+|呵+|嘿+|嘻+|啊+|哦+|嗯+|好+|笑死|草|卧槽|牛逼|666+|233+|lol|hhh+|w+"
    r"|看看|看图|看这个|看看这个|给你们看看|分享|分享一下|分享链接|链接|转发|转发一下"
    r"|发图|发车|开车|福利|色图|涩图|色色|涩涩|好色|好涩|好看|好看吗|真好看|好康|来点|有点东西"
    r")+$",
    re.IGNORECASE,
)


@dataclass
class ForwardResult:
    """一份转发读完之后，上层需要知道的全部信息。"""

    text: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    has_discussion: bool = False
    nodes: int = 0
    chars: int = 0
    error: str = ""


def _data_of(segment: dict[str, Any]) -> dict[str, Any]:
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def _json_card(segment: dict[str, Any]) -> dict[str, Any] | None:
    """把 json 段的 data.data 解析成卡片对象；不是卡片就返回 None。"""
    if segment.get("type") != "json":
        return None
    raw = _data_of(segment).get("data")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or len(raw) > 65536:
        return None
    try:
        card = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return card if isinstance(card, dict) else None


def forward_ref(segment: dict[str, Any]) -> str:
    """从这个消息段里取转发资源编号；不是转发就返回空串。"""
    if not isinstance(segment, dict):
        return ""
    kind = segment.get("type")
    data = _data_of(segment)
    if kind == "forward":
        return str(
            data.get("id")
            or data.get("res_id")
            or data.get("resid")
            or data.get("forward_id")
            or ""
        ).strip()
    if kind == "json":
        card = _json_card(segment)
        if not card or card.get("app") != "com.tencent.multimsg":
            return ""
        meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
        detail = meta.get("detail") if isinstance(meta.get("detail"), dict) else {}
        if not detail:
            detail = meta.get("news") if isinstance(meta.get("news"), dict) else {}
        return str(
            detail.get("resid") or detail.get("resId") or detail.get("m_resid") or ""
        ).strip()
    return ""


def first_forward_ref(segments: Any) -> str:
    if not isinstance(segments, list):
        return ""
    for seg in segments:
        ref = forward_ref(seg)
        if ref:
            return ref
    return ""


def has_forward(segments: Any) -> bool:
    """这条消息里有没有合并转发（包括分享卡片形态）。"""
    return bool(first_forward_ref(segments))


def nodes_of(response: Any) -> list[dict[str, Any]]:
    """从 OneBot 的返回里取节点数组，兼容几种常见包法。"""
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return [n for n in data if isinstance(n, dict)]
    if isinstance(data, dict):
        for key in ("messages", "message"):
            nodes = data.get(key)
            if isinstance(nodes, list):
                return [n for n in nodes if isinstance(n, dict)]
    nodes = response.get("messages")
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    return []


def parse_cq_message(message: Any) -> list[dict[str, Any]]:
    """把 CQ 码字符串转成消息段数组；已经是数组就原样返回。"""
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict)]
    if not isinstance(message, str) or not message:
        return []

    def decode(value: str) -> str:
        return (
            value.replace("&#44;", ",")
            .replace("&#91;", "[")
            .replace("&#93;", "]")
            .replace("&amp;", "&")
        )

    out: list[dict[str, Any]] = []
    last = 0
    for match in _CQ_RE.finditer(message):
        if match.start() > last:
            out.append({"type": "text", "data": {"text": decode(message[last : match.start()])}})
        data: dict[str, Any] = {}
        for pair in match.group(2).split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            data[key] = decode(value)
        out.append({"type": match.group(1), "data": data})
        last = match.end()
    if last < len(message):
        out.append({"type": "text", "data": {"text": decode(message[last:])}})
    return out


def clean_for_discussion(text: str) -> str:
    """去掉 URL、CQ 码、空白和标点，只留下能说明“有人在说话”的字。"""
    value = _URL_RE.sub(" ", str(text or ""))
    value = _CQ_STRIP_RE.sub(" ", value)
    value = _PLACEHOLDER_RE.sub(" ", value)
    return _CLEAN_RE.sub("", value).lower()


def has_discussion(texts: list[str]) -> bool:
    """转发里有没有实质聊天内容（不是只有哈哈哈/表情/链接）。"""
    for text in texts:
        value = clean_for_discussion(text)
        if not value or len(value) < 2:
            continue
        if _FILLER_RE.fullmatch(value):
            continue
        return True
    return False


class _Reader:
    """一次读取的预算与状态。所有 append 都走这里，保证不会超字数。"""

    def __init__(
        self,
        onebot: Any,
        *,
        max_depth: int,
        max_nodes: int,
        max_chars: int,
        max_calls: int,
        max_images: int,
        self_id: str = "",
    ) -> None:
        self.onebot = onebot
        self.max_depth = max(0, max_depth)
        self.max_nodes = max(1, max_nodes)
        self.max_chars = max(50, max_chars)
        self.max_calls = max(1, max_calls)
        self.max_images = max(0, max_images)
        self.self_id = str(self_id or "").strip()
        self.nodes = 0
        self.chars = 0
        self.calls = 0
        self.images: list[dict[str, Any]] = []
        self.image_keys: set[str] = set()
        self.partial = False
        self.lines: list[str] = []
        self.authored: list[str] = []
        self.visited: set[str] = set()

    def append(self, text: str) -> None:
        if not text:
            return
        remaining = self.max_chars - self.chars
        if remaining <= 0:
            self.partial = True
            return
        clipped = text[:remaining]
        if len(clipped) < len(text):
            self.partial = True
        self.chars += len(clipped)
        self.lines.append(clipped)

    async def _call(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self.calls >= self.max_calls:
            raise RuntimeError("读取次数到上限")
        self.calls += 1
        response = await self.onebot.call("get_forward_msg", params, timeout=CALL_TIMEOUT)
        nodes = nodes_of(response)
        if not nodes:
            raise RuntimeError("记录为空")
        return nodes

    def _add_image(self, segment: dict[str, Any]) -> None:
        data = _data_of(segment)
        key = str(data.get("url") or data.get("file") or data.get("path") or "").strip()
        if not key:
            self.append("[图片：缺少地址]")
            self.partial = True
            return
        if key in self.image_keys:
            for idx, existing in enumerate(self.images, 1):
                if str(_data_of(existing).get("url") or _data_of(existing).get("file") or _data_of(existing).get("path") or "").strip() == key:
                    self.append(f"[图片{idx}：同图再次出现]")
                    return
        if len(self.images) >= self.max_images:
            self.append("[图片：超过本轮读取上限]")
            self.partial = True
            return
        self.image_keys.add(key)
        self.images.append({"type": "image", "data": dict(data)})
        self.append(f"[图片{len(self.images)}]")

    def _segment_text(self, segment: dict[str, Any]) -> str:
        kind = segment.get("type")
        data = _data_of(segment)
        if kind == "text":
            text = str(data.get("text") or "")
            self.authored.append(text)
            return text
        if kind == "at":
            qq = str(data.get("qq") or "")
            return "[@全体成员]" if qq == "all" else f"[@{qq or '某人'}]"
        if kind == "face":
            return "[表情]"
        if kind == "record":
            return "[语音：未转写]"
        if kind == "video":
            return "[视频：未读取内容]"
        if kind == "file":
            name = str(data.get("name") or "未读取内容")[:100]
            return f"[文件：{name}]"
        if kind == "reply":
            return "[引用消息]"
        if kind in ("json", "xml"):
            return "[卡片：未打开]"
        if kind == "forward":
            return "[嵌套合并转发：未展开]"
        if kind == "node":
            return "[内嵌消息]"
        return f"[{str(kind)[:30]}]" if kind else ""

    async def visit(self, nodes: list[dict[str, Any]], depth: int = 0) -> None:
        for item in nodes:
            if self.nodes >= self.max_nodes or self.chars >= self.max_chars:
                self.partial = True
                break
            self.nodes += 1
            node = item.get("data") if item.get("type") == "node" and isinstance(item.get("data"), dict) else item
            if not isinstance(node, dict):
                node = {}
            sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
            name = str(
                sender.get("card") or sender.get("nickname") or node.get("nickname") or node.get("name") or "某人"
            ).replace("\r", " ").replace("\n", " ")[:80]
            uid = str(
                sender.get("user_id")
                or sender.get("uin")
                or node.get("user_id")
                or node.get("uin")
                or ""
            ).strip()
            mine = bool(self.self_id and uid == self.self_id)
            segments = parse_cq_message(node.get("message") if node.get("message") is not None else node.get("content"))

            self.append("  " * depth + f"{SELF_MARK if mine else ''}{name}：")
            for segment in segments:
                if self.chars >= self.max_chars:
                    self.partial = True
                    break
                kind = segment.get("type")
                ref = forward_ref(segment)
                if kind == "forward" or ref:
                    if not ref or depth >= self.max_depth or ref in self.visited:
                        self.partial = True
                        self.append("[嵌套记录未展开：缺少编号、重复引用或达到深度上限]")
                        continue
                    self.visited.add(ref)
                    try:
                        nested = await self._call({"id": ref})
                        self.append("\n[内层聊天记录]\n")
                        await self.visit(nested, depth + 1)
                        self.append("[内层记录结束]\n")
                    except Exception as exc:  # noqa: BLE001
                        log.info("内层转发读取失败: %s", exc)
                        self.partial = True
                        self.append("[这份内层聊天记录读取失败或已失效]")
                elif kind == "node":
                    if depth >= self.max_depth:
                        self.partial = True
                        self.append("[内嵌记录达到深度上限]")
                    else:
                        await self.visit([segment], depth + 1)
                elif kind == "image":
                    self._add_image(segment)
                else:
                    self.append(self._segment_text(segment))
            self.append("\n")


async def expand_forward(
    onebot: Any,
    segments: Any,
    *,
    message_id: Any = None,
    max_depth: int = 3,
    max_nodes: int = 200,
    max_chars: int = 8000,
    max_calls: int = 8,
    max_images: int = 4,
    self_id: str = "",
) -> ForwardResult | None:
    """展开一条消息里的合并转发；没有转发就返回 None。

    ``self_id`` 是机器人自己的 QQ：传了之后，记录里她自己发的节点会标
    ``（你）``，模型才分得清「这条是本鱼」还是「别的鱼也叫大肥鱼」。

    读取失败时返回带 error 的 ForwardResult，不抛异常 —— 上层可以照常
    走原来的占位符逻辑，不会因为一份坏转发把整条消息弄丢。
    """
    ref = first_forward_ref(segments)
    if not ref and message_id is None:
        return None

    reader = _Reader(
        onebot,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_chars=max_chars,
        max_calls=max_calls,
        max_images=max_images,
        self_id=self_id,
    )
    error = ""
    try:
        nodes: list[dict[str, Any]] | None = None
        if ref:
            reader.visited.add(ref)
            try:
                nodes = await reader._call({"id": ref})
            except Exception as exc:  # noqa: BLE001
                log.info("按资源编号读转发失败（%s），改用 message_id", exc)

        if nodes is None and message_id is not None:
            try:
                nodes = await reader._call({"message_id": int(message_id)})
            except Exception as exc:  # noqa: BLE001
                log.info("按 message_id 读转发失败（%s），尝试 get_msg 回退", exc)

        if nodes is None and message_id is not None:
            # 某些协议端不认负 message_id，只能先取原消息，再从卡片里拿资源号。
            if reader.calls >= reader.max_calls:
                raise RuntimeError("读取次数到上限")
            reader.calls += 1
            original = await onebot.call("get_msg", {"message_id": int(message_id)}, timeout=CALL_TIMEOUT)
            data = original.get("data") if isinstance(original, dict) else {}
            raw_message = data.get("message") if isinstance(data, dict) else None
            refs = [forward_ref(s) for s in parse_cq_message(raw_message)]
            refs = [r for r in refs if r]
            if not refs:
                raise RuntimeError("原消息里没有可读的转发编号")
            collected: list[dict[str, Any]] = []
            for item_ref in refs:
                if item_ref in reader.visited:
                    continue
                reader.visited.add(item_ref)
                collected.extend(await reader._call({"id": item_ref}))
            nodes = collected

        if not nodes:
            raise RuntimeError("记录为空")
        await reader.visit(nodes, 0)
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or type(exc).__name__
        log.warning("合并转发读取失败: %s", error)

    text = "".join(reader.lines).strip()
    if reader.partial and text:
        text += "\n[只读到了部分内容，缺失的部分不要猜]"
    return ForwardResult(
        text=text,
        images=reader.images,
        partial=reader.partial,
        has_discussion=has_discussion(reader.authored),
        nodes=reader.nodes,
        chars=reader.chars,
        error=error,
    )
