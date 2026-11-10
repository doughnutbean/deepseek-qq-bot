"""人设分层：核心段常驻 system，叙事/怪癖/情境段按话题临时注入。

为什么要分层：人设 12,890 字，占了每次请求固定前缀的 74%。其中「来历」「那张图」
这类内容平时用不上，却每轮都重发。分层的做法是——

- `persona.md` 一个字不改，仍然是人设的唯一原文；
- `persona.layers.json` 标出哪些段常驻（core）、哪些段按需（on_demand）、
  哪几段只对群聊或只对私聊生效；
- 常驻段进 system（字节稳定，缓存照吃）；按需段只在消息命中触发词时，
  追加到这一轮的动态上下文里（不进 system，不破前缀缓存）。

**回滚有三层，从快到慢：**

1. `python tools/persona_layers.py --off`（或手动建 `data/persona_layers.off`）
   —— 每轮请求都会检查这个文件，下一句就恢复整份人设，不用重启；
2. `.env` 里写 `PERSONA_LAYERS=full` —— 重启后整份人设；
3. 分层表缺失/写坏/运行出错 —— 自动退回整份人设，只记 warning，不耽误说话。

所有分类都按「安全侧」处理：没写进分层表的段，一律当常驻。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .promptbudget import split_persona

log = logging.getLogger(__name__)

# 全文回滚的开关值
_FULL_VALUES = {"full", "off", "0", "false", "no", "legacy"}
# 一轮最多带多少字的按需设定；太小会讲不清来历，太大就白分层了
DEFAULT_MAX_CHARS = 1000
_MAX_CHARS_LIMIT = 4000
_MIN_CHARS_LIMIT = 200


@dataclass(frozen=True)
class Section:
    key: str
    text: str
    layer: str = "core"  # core / on_demand
    context: str = "always"  # always / group / private
    triggers: tuple[str, ...] = ()

    def visible(self, is_group: bool) -> bool:
        if self.context == "always":
            return True
        return (self.context == "group") == is_group


@dataclass
class Plan:
    persona: str
    sections: list[Section]
    max_chars: int = DEFAULT_MAX_CHARS
    off_path: Path | None = None
    ok: bool = True
    reason: str = ""
    _core_cache: dict[bool, str] = field(default_factory=dict, repr=False)

    # ---------- 开关 ----------

    def mode(self) -> str:
        """layered / full。每次调用都看环境变量和回滚文件，所以能即时回滚。"""
        if not self.ok:
            return "full"
        if os.environ.get("PERSONA_LAYERS", "").strip().lower() in _FULL_VALUES:
            return "full"
        if self.off_path is not None and self.off_path.exists():
            return "full"
        return "layered"

    # ---------- 组装 ----------

    def core(self, is_group: bool) -> str:
        if self.mode() == "full":
            return self.persona
        if is_group not in self._core_cache:
            body = "".join(
                s.text
                for s in self.sections
                if s.layer == "core" and s.visible(is_group)
            ).strip()
            # 全被切走这种情况只可能是表写坏了：退回整份，别把人设弄丢
            self._core_cache[is_group] = body or self.persona
        return self._core_cache[is_group]

    def match(self, text: str, *, is_group: bool) -> list[Section]:
        """按触发词找相关段；命中多的优先，同样多时短的优先（更省）。"""
        if self.mode() != "layered" or not text:
            return []
        low = text.casefold()
        scored: list[tuple[int, int, int, Section]] = []
        for index, section in enumerate(self.sections):
            if section.layer != "on_demand" or not section.visible(is_group):
                continue
            hits = sum(1 for t in section.triggers if t and t.casefold() in low)
            if hits:
                scored.append((hits, len(section.text), index, section))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [item[3] for item in scored]

    def render(self, sections: list[Section], *, budget: int | None = None) -> str:
        """把按需段拼成一段注入文本；超出预算就截断（或只带第一段）。"""
        limit = max(1, budget or self.max_chars)
        chosen: list[str] = []
        used = 0
        for section in sections:
            if used >= limit:
                break
            room = limit - used
            body = section.text.strip()
            if len(body) > room:
                if chosen:
                    break
                body = body[:room].rstrip() + "……"
            chosen.append(body)
            used += len(body)
        if not chosen:
            return ""
        return (
            "（下面是你的设定，跟这一轮有关；只给你自己看，别原样念出来：\n"
            + "\n\n".join(chosen)
            + "）"
        )

    # ---------- 账 ----------

    def core_chars(self, is_group: bool) -> int:
        return len(self.core(is_group))

    def on_demand_chars(self, is_group: bool) -> int:
        return sum(
            len(s.text)
            for s in self.sections
            if s.layer == "on_demand" and s.visible(is_group)
        )

    def core_keys(self, is_group: bool) -> list[str]:
        return [
            s.key
            for s in self.sections
            if s.layer == "core" and s.visible(is_group)
        ]


def _full(persona: str, off_path: Path | None, reason: str) -> Plan:
    return Plan(
        persona=persona,
        sections=[Section("全部", persona)],
        off_path=off_path,
        ok=False,
        reason=reason,
    )


def load_plan(
    persona: str,
    path: Path,
    *,
    off_path: Path | None = None,
) -> Plan:
    """读分层表；缺文件/写坏了都退回整份人设，绝不让机器人起不来。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("人设分层表不存在（%s），整份人设顶上", path.name)
        return _full(persona, off_path, "分层表不存在")
    except (OSError, ValueError) as exc:
        log.warning("人设分层表读不了（%s），整份人设顶上", exc)
        return _full(persona, off_path, f"分层表读不了：{exc}")

    if not isinstance(raw, dict):
        log.warning("人设分层表不是对象，整份人设顶上")
        return _full(persona, off_path, "分层表不是对象")

    core_keys = {str(k) for k in (raw.get("core") or [])}
    context_only = raw.get("context_only") or {}
    on_demand = raw.get("on_demand") or {}
    if not isinstance(context_only, dict) or not isinstance(on_demand, dict):
        log.warning("人设分层的 context_only / on_demand 不是对象，整份人设顶上")
        return _full(persona, off_path, "分层表字段类型不对")

    try:
        max_chars = int(raw.get("max_chars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = min(_MAX_CHARS_LIMIT, max(_MIN_CHARS_LIMIT, max_chars))

    sections: list[Section] = []
    known: set[str] = set()
    for title, body in split_persona(persona):
        known.add(title)
        if title in on_demand:
            triggers = on_demand.get(title)
            if isinstance(triggers, str):
                triggers = [triggers]
            if not isinstance(triggers, list):
                triggers = []
            sections.append(
                Section(
                    title,
                    body,
                    layer="on_demand",
                    context=str(context_only.get(title, "always")),
                    triggers=tuple(str(t).strip() for t in triggers if str(t).strip()),
                )
            )
        else:
            sections.append(
                Section(
                    title,
                    body,
                    layer="core",
                    context=str(context_only.get(title, "always")),
                )
            )

    # 表里写了、人设里没有的 key：不致命，但得让人看见（check 工具会红）
    extra = (set(on_demand) | core_keys | set(context_only)) - known
    if extra:
        log.warning("人设分层表里有对不上的段：%s", "、".join(sorted(extra)))

    no_trigger = [s.key for s in sections if s.layer == "on_demand" and not s.triggers]
    if no_trigger:
        log.warning("按需段没写触发词，永远不会出现：%s", "、".join(no_trigger))

    return Plan(
        persona=persona,
        sections=sections,
        max_chars=max_chars,
        off_path=off_path,
        ok=True,
    )


def status_line(plan: Plan) -> str:
    """给启动日志用的一行。"""
    mode = plan.mode()
    if mode == "full":
        why = "回滚开关" if plan.ok else plan.reason
        return f"人设分层：整份人设（{why}），{len(plan.persona)} 字"
    return (
        f"人设分层：常驻 {plan.core_chars(is_group=True)} 字 / 按需 "
        f"{plan.on_demand_chars(is_group=True)} 字（触发才注入，单轮上限 "
        f"{plan.max_chars} 字）；回滚：python tools/persona_layers.py --off"
    )
