"""给模型的工具（function calling）格式规范 + 校验。

为什么要有这一层
----------------
她「会什么」有两套：本地指令（0 token，正则认）和工具（模型自己决定调）。
新功能只写指令、不写工具，遇到指令没覆盖的说法，她就会嘴上答应、实际没做
（2026-10-30 00:54「进入asaki的直播陪看」就是这么翻车的）。

所以约定：**用户能感知、需要动作的功能，必须同时给她一个工具**；
schema 统一用 `make_tool()` 生成，收尾跑 `python tools/check_tools.py`。

格式（OpenAI function calling）
-------------------------------
```
{
  "type": "function",
  "function": {
    "name": "bili_live_watch",
    "description": "什么时候用、做什么、返回什么、权限/副作用",
    "parameters": {
      "type": "object",
      "properties": {
        "target": {"type": "string", "description": "房号/链接/名字"}
      },
      "required": ["target"]
    }
  }
}
```

硬规则
------
- 名字：`^[a-z][a-z0-9_]{2,47}$`，同一机器人里唯一；建议 `<域>_<动作>`；
- 描述 12~600 字：必须写清触发说法（「主人说……时用」）、做什么、失败怎么回；
  有副作用的（发图、改资料、删人）要写出来；只有主人能用的要写明「只有主人能用」；
- 参数必须是 object；每个属性有 type + description；required 里的名字必须在
  properties 里；没有参数就写空 properties；
- 返回：中文、尽量 ≤400 字；失败说实话，不要编成功；不要返回 Markdown 链接
  （QQ 会拦）；
- 权限：写操作在 schema 暴露和 `run()` 里双重把关（参考 live_tools）；
- 注册：provider 的 `schemas()` + `run()` 一起加，再把 provider 挂进 `llm.py`
  的 ctx 列表；最后补测试和能力表。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
# 参数名可以更短（to / up / x），但同样只许小写
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
DESC_MIN = 12
DESC_MAX = 600
# 个别工具确实需要一份长清单（本地指令大全）；必须在 provider 上显式标
# `description_limit`，不许默认超长。
DESC_HARD_MAX = 4000
_VALID_TYPES = {"string", "number", "integer", "boolean", "array", "object"}
_FUNCTION_KEYS = {"name", "description", "parameters", "strict"}


def make_tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """按统一格式造一条工具 schema。"""
    params: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties or {}),
    }
    if required:
        params["required"] = list(required)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description.strip(),
            "parameters": params,
        },
    }


def validate_tool(schema: Any, max_desc: int = DESC_MAX) -> list[str]:
    """检查一条 schema；返回问题列表（空 = 合规）。"""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return ["schema 不是 dict"]
    if schema.get("type") != "function":
        problems.append("type 必须是 function")
    function = schema.get("function")
    if not isinstance(function, dict):
        return problems + ["缺 function 对象"]
    extra = set(function) - _FUNCTION_KEYS
    if extra:
        problems.append(f"function 里有未约定的字段：{sorted(extra)}")
    name = function.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append(f"name 不合规（要小写 snake_case）：{name!r}")
    description = function.get("description")
    if not isinstance(description, str) or not (
        DESC_MIN <= len(description.strip()) <= max_desc
    ):
        problems.append(
            f"description 要 {DESC_MIN}~{max_desc} 字（现在是 "
            f"{len(description or '')} 字）"
        )
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return problems + ["缺 parameters 对象"]
    if parameters.get("type") != "object":
        problems.append("parameters.type 必须是 object")
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return problems + ["parameters.properties 必须是对象"]
    for key, spec in properties.items():
        if not PARAM_NAME_RE.match(str(key)):
            problems.append(f"参数名不合规：{key!r}")
        if not isinstance(spec, dict):
            problems.append(f"参数 {key} 不是对象")
            continue
        if spec.get("type") not in _VALID_TYPES:
            problems.append(f"参数 {key} 的 type 不合法：{spec.get('type')!r}")
        if not str(spec.get("description") or "").strip():
            problems.append(f"参数 {key} 缺 description")
    required = parameters.get("required")
    if required is not None:
        if not isinstance(required, list):
            problems.append("parameters.required 必须是数组")
        else:
            unknown = [item for item in required if item not in properties]
            if unknown:
                problems.append(f"required 里有未定义的参数：{unknown}")
    return problems


def validate_provider(provider: Any) -> list[str]:
    """检查一个工具提供者：有 schemas()/run()，每条 schema 合规。"""
    label = str(getattr(provider, "label", "") or type(provider).__name__)
    problems: list[str] = []
    schemas_fn = getattr(provider, "schemas", None)
    if not callable(schemas_fn):
        return [f"{label} 缺 schemas()"]
    if not callable(getattr(provider, "run", None)):
        problems.append(f"{label} 缺 run()")
    try:
        schemas = schemas_fn()
    except Exception as exc:  # noqa: BLE001 - 校验不能把机器人搞崩
        return problems + [f"{label}.schemas() 抛错：{type(exc).__name__}: {exc}"]
    if not isinstance(schemas, list):
        return problems + [f"{label}.schemas() 要返回 list"]
    limit = int(getattr(provider, "description_limit", DESC_MAX) or DESC_MAX)
    limit = min(max(limit, DESC_MAX), DESC_HARD_MAX)
    for schema in schemas:
        for problem in validate_tool(schema, limit):
            name = ""
            if isinstance(schema, dict):
                name = str((schema.get("function") or {}).get("name") or "")
            problems.append(f"{label}/{name or '?'}：{problem}")
    return problems


def validate_providers(providers: Iterable[Any]) -> list[str]:
    """检查一组 provider：逐条校验 + 全机器人工具名唯一。"""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for provider in providers:
        problems.extend(validate_provider(provider))
        try:
            schemas = provider.schemas() or []
        except Exception:  # noqa: BLE001 - 上面已经报过
            continue
        for schema in schemas:
            name = str(((schema or {}).get("function") or {}).get("name") or "")
            if not name:
                continue
            owner = type(provider).__name__
            if name in seen:
                problems.append(f"工具名重复：{name}（{seen[name]} 和 {owner}）")
            else:
                seen[name] = owner
    return problems


def warn_problems(problems: list[str], *, once: bool = True) -> None:
    """启动/调用时把不合规的工具格式记 warning；默认同一批问题只报一次。"""
    if not problems:
        return
    if once:
        global _reported
        fresh = [item for item in problems if item not in _reported]
        _reported.update(problems)
        problems = fresh
    for problem in problems:
        log.warning("工具格式不规范：%s（见 docs/工具规范.md）", problem)


_reported: set[str] = set()
