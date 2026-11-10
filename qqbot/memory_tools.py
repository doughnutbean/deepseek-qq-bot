"""群聊/私聊都能用的「记住一条事实」工具。

以前 `save_fact` 只在主人私聊里给（`selftools`），群友说「记住我住杭州」
只能靠本地正则碰运气。这里补一个只写"当前说话人"的小工具：

- 只能记关于**当前说话人**的事实，不能替别人立档案；
- 只收短句，不收隐私/敏感信息；
- `source="llm"`：可信度比本地规则高一点，但比不上本人确认。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_KINDS = ("喜欢", "雷点", "约好", "身份", "住处", "工作", "宠物", "生日", "年龄", "其他")
_SENSITIVE = re.compile(
    r"身份证|手机号|电话|住址|地址|密码|工资|银行卡|开盒|人肉|裸照|自杀|自残|病历"
)


class MemoryTools:
    NAMES = {"remember_fact"}

    def __init__(self, enabled: bool = True, blocked: Any = None) -> None:
        self.enabled = bool(enabled)
        # blocked(uid) -> True：这个人说过「别记我」，模型再怎么想也不写
        self.blocked = blocked

    @property
    def available(self) -> bool:
        return self.enabled

    def schemas(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "remember_fact",
                    "description": (
                        "记住一条关于**当前跟你说话的人**的长期事实（他喜欢什么、"
                        "雷点、你们约好的事、他是谁/住哪/做什么/养了什么）。"
                        "只在对方明确让你记住、或这句话明显以后还用得上时调用一次；"
                        "别记流水账，别替第三方建档，别记隐私。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": list(_KINDS),
                                "description": "这条事实属于哪一类",
                            },
                            "text": {
                                "type": "string",
                                "description": "一句话，20 字以内，例如「喜欢喝美式」「住在杭州」",
                            },
                        },
                        "required": ["kind", "text"],
                    },
                },
            }
        ]

    async def run(
        self,
        name: str,
        arguments: str,
        memory: Any = None,
        speaker_uid: Any = None,
        speaker_nick: str = "",
        turn: Any = None,
        **_kwargs: Any,
    ) -> str | None:
        if turn is not None:
            _kwargs = turn.as_ctx()
            memory = _kwargs.get('memory')
            speaker_uid = _kwargs.get('speaker_uid')
            speaker_nick = _kwargs.get('speaker_nick')
        if name not in self.NAMES:
            return None
        if not self.enabled or memory is None:
            return "（记忆没开，记不了）"
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "（参数不是合法 JSON）"
        kind = str(args.get("kind") or "").strip()
        text = str(args.get("text") or "").strip()
        if kind not in _KINDS:
            kind = "其他"
        if not text:
            return "（没说要记什么）"
        if _SENSITIVE.search(text):
            return "（这个不记）"
        if not speaker_uid:
            return "（不知道你是谁，记不了）"
        if self.blocked is not None and self.blocked(speaker_uid):
            return "（他说了「别记我」，这条不记）"
        text = text[:40]
        ok = memory.add_fact(
            str(speaker_uid),
            kind,
            text,
            speaker_uid=str(speaker_uid),
            source="llm",
        )
        if ok:
            log.info("模型主动记住（%s）：%s", kind, text)
            return f"（记住啦：{text}）"
        return "（这条本鱼已经记过了）"
