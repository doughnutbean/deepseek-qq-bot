"""让她能管理自己的账号：个性签名 / 头像 / 昵称 / QQ 空间。

这些是真正会改动 QQ 账号状态的操作，所以设了硬边界：
**只有主人私聊时才开放**，群里的任何人（包括别人 @ 她）都改不了。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .qzone import Qzone, QzoneError

log = logging.getLogger(__name__)

MAX_SIGNATURE = 60
MAX_NICKNAME = 20
MAX_QZONE = 300


class SelfTools:
    NAMES = {
        "set_signature",
        "set_avatar",
        "set_nickname",
        "post_qzone",
        "delete_qzone",
        "remember_image",
        "save_fact",
    }
    # 会改动 QQ 账号状态的才需要主人；记图是内部记忆，群里谁发的都能记
    OWNER_ONLY = {"set_signature", "set_avatar", "set_nickname", "post_qzone", "delete_qzone"}

    def __init__(self, config: Any) -> None:
        self.config = config
        # 改完昵称的回调：App 接上自己的 SelfName，让她当场就知道新名字
        self.on_nickname: Any = None

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "set_signature",
                    "description": "修改你自己的 QQ 个性签名。只有主人明确让你换签名时才调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "新的签名，60 字以内"}
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_avatar",
                    "description": (
                        "修改你自己的 QQ 头像。如果主人刚发了一张图并让你用这张，"
                        "file 传 __last_image__；如果主人给了图片链接，直接传那个链接。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "图片链接，或 __last_image__ 代表主人刚发的那张图",
                            }
                        },
                        "required": ["file"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_nickname",
                    "description": "修改你自己的 QQ 昵称。只有主人明确要求时才调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nickname": {"type": "string", "description": "新昵称，20 字以内"}
                        },
                        "required": ["nickname"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "post_qzone",
                    "description": (
                        "在你的 QQ 空间发一条说说。只有主人明确让你发的时候才调用，"
                        "不要自己主动发。内容用你自己的口吻，最好 100 字以内。"
                        "发说说要配一张图，配图从表情包的标签里挑一个最贴近这张图感觉的。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "说说正文，100 字以内最好",
                            },
                            "image": {
                                "type": "string",
                                "description": (
                                    "配图标签，从表情包的标签里选一个（比如 开心、疲惫、委屈、干饭）；"
                                    "如果要用主人刚发的那张图，填 __last_image__"
                                ),
                            },
                            "private": {
                                "type": "boolean",
                                "description": "true 表示仅自己可见；默认 false（好友可见）",
                            },
                        },
                        "required": ["content", "image"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_qzone",
                    "description": (
                        "删掉你 QQ 空间里**最新的一条**说说。只有主人明确说删的时候才调用，"
                        "删了就没了，不确定就先问他。"
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_fact",
                    "description": (
                        "记住一条关于当前说话人的长期事实：他喜欢什么、雷点是什么、"
                        "你们约好了什么。只记以后还用得上的，别记流水账。"
                        "「约好」只记对方明确跟你定下的事（时间、要你提醒的事、"
                        "保证/承诺，比如「永远不会抛弃你」），别把玩笑、角色扮演、"
                        "别人之间的约定记成约好。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["喜欢", "雷点", "约好", "身份"],
                                "description": "这条事实属于哪一类",
                            },
                            "text": {
                                "type": "string",
                                "description": "一句话，20 字以内，例如「喜欢喝美式」",
                            },
                        },
                        "required": ["kind", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remember_image",
                    "description": (
                        "记住一张图是什么意思，下次同一张图再出现就不用重新看了。"
                        "第一次看到某个表情包/梗图时调用一次，一张图只记一次。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "desc": {
                                "type": "string",
                                "description": "这张图画的是什么、适合什么场合，40 字以内",
                            }
                        },
                        "required": ["desc"],
                    },
                },
            },
        ]

    async def run(
        self,
        name: str,
        arguments: str,
        onebot: Any = None,
        is_owner: bool = False,
        last_image: str | None = None,
        turn: Any = None,
        **_kwargs: Any,
    ) -> str | None:
        if turn is not None:
            _kwargs = turn.as_ctx()
            onebot = _kwargs.get('onebot')
            is_owner = _kwargs.get('is_owner')
            last_image = _kwargs.get('last_image')
        if name not in self.NAMES:
            return None
        if name in self.OWNER_ONLY and not is_owner:
            log.warning("非主人尝试调用 %s，已拒绝", name)
            return "（这个只有主人能让我改哦）"
        if name != "remember_image" and onebot is None:
            return "（现在连不上 QQ，改不了）"

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "（参数不是合法 JSON）"

        try:
            if name == "set_signature":
                return await self._signature(onebot, str(args.get("text", "")).strip())
            if name == "set_avatar":
                return await self._avatar(onebot, str(args.get("file", "")).strip(), last_image)
            if name == "set_nickname":
                return await self._nickname(onebot, str(args.get("nickname", "")).strip())
            if name == "save_fact":
                return await self._save_fact(
                    _kwargs.get("memory"),
                    _kwargs.get("speaker_uid"),
                    str(args.get("kind", "")).strip(),
                    str(args.get("text", "")).strip(),
                )
            if name == "remember_image":
                return await self._remember_image(
                    _kwargs.get("image_cache"),
                    _kwargs.get("image_hashes") or [],
                    str(args.get("desc", "")).strip(),
                )
            if name == "delete_qzone":
                return await self._qzone_delete(onebot, **_kwargs)
            if name == "post_qzone":
                return await self._qzone(
                    onebot,
                    str(args.get("content", "")).strip(),
                    bool(args.get("private", False)),
                    str(args.get("image", "")).strip(),
                    last_image=last_image,
                    **_kwargs,
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("修改资料失败: %s", name)
            return f"（改失败了：{type(exc).__name__}）"

        return None

    async def _signature(self, onebot: Any, text: str) -> str:
        if not text:
            return "（签名是空的）"
        if len(text) > MAX_SIGNATURE:
            text = text[:MAX_SIGNATURE]
        resp = await onebot.call("set_self_longnick", {"longNick": text})
        data = (resp or {}).get("data") or {}
        if isinstance(data, dict) and data.get("result") not in (None, 0):
            return f"（签名没改成：{data.get('errMsg') or data.get('result')}）"
        log.info("签名已改为: %s", text)
        return f"改好了，签名现在是「{text}」。"

    async def _avatar(self, onebot: Any, file: str, last_image: str | None) -> str:
        target = file
        if file in {"__last_image__", "last_image", ""}:
            if not last_image:
                return "（没有找到主人刚发的图）"
            target = last_image
        if not target:
            return "（没给图片）"
        await onebot.call("set_qq_avatar", {"file": target}, timeout=60)
        log.info("头像已更换: %s", target)
        return "换好了，本鱼换头像啦。"

    async def _nickname(self, onebot: Any, nickname: str) -> str:
        if not nickname:
            return "（昵称是空的）"
        nickname = nickname[:MAX_NICKNAME]
        await onebot.call("set_qq_profile", {"nickname": nickname})
        log.info("昵称已改为: %s", nickname)
        # 名字是运行时状态：改完当场覆盖，别等她下一轮去接口里读
        if self.on_nickname is not None:
            try:
                self.on_nickname(nickname)
            except Exception:  # noqa: BLE001 - 回调坏了也不能让改名失败
                log.warning("改完昵称，自己的名字没接上", exc_info=True)
        return f"改好了，本鱼现在叫「{nickname}」。"

    async def _save_fact(self, memory: Any, uid: Any, kind: str, text: str) -> str:
        """记一条关于当前说话人的事实。"""
        if memory is None or not uid:
            return "（不知道这是谁，记不了）"
        if not text:
            return "（内容是空的）"
        added = memory.add_fact(
            str(uid), kind or "其他", text, speaker_uid=str(uid), source="llm"
        )
        return f"记住了：{text}" if added else "（这条已经记过了）"

    async def _remember_image(
        self, cache: Any, hashes: list[str], desc: str
    ) -> str:
        """把这一轮新图的含义记下来，下次同一张图就不用再看了。"""
        if not desc:
            return "（没给描述）"
        if cache is None:
            return "（图片记忆没开）"
        fresh = [h for h in (hashes or []) if h]
        if not fresh:
            return "（这一轮没有新图，不用记）"
        for h in fresh[:4]:
            cache.put(h, desc)
        log.info("记住图片: %s", desc[:40])
        return f"记住了：{desc}"

    async def _qzone_delete(self, onebot: Any, **_kwargs: Any) -> str:
        """删最新一条说说。"""
        if not getattr(self.config, "enable_qzone", False):
            return "（空间功能没开）"
        uin = str(getattr(self.config, "bot_qq", "") or "")
        if not uin:
            return "（不知道自己的 QQ 号）"
        qz = Qzone(onebot, uin, private=getattr(self.config, "qzone_private", 0))
        feed = await qz.feed_list(3)
        if not feed.get("ok"):
            return "（读不到说说列表，删不了）"
        items = feed.get("items") or []
        if not items:
            return "（空间里本来就没有说说）"
        newest = items[0]
        result = await qz.delete(newest["tid"])
        if not result.get("ok"):
            return f"（没删掉，返回码 {result.get('code')}）"
        return f"删掉了：「{newest['content'][:40]}」"

    async def _qzone(
        self,
        onebot: Any,
        content: str,
        private: bool,
        image: str = "",
        last_image: str | None = None,
        stickers: Any = None,
        memory: Any = None,
        session: str = "",
        owner_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        if not getattr(self.config, "enable_qzone", False):
            return "（空间功能没开，先把 ENABLE_QZONE 打开）"
        if not content:
            return "（内容是空的）"
        content = content[:MAX_QZONE]
        uin = str(getattr(self.config, "bot_qq", "") or "")
        if not uin:
            return "（不知道自己的 QQ 号）"

        # 配图：模型给的是表情包标签，跟发表情包一样的挑法（会避开上一张）
        picked = None
        used_tag = ""
        if image in {"__last_image__", "last_image"} and last_image:
            picked = Path(last_image)
            used_tag = "主人发的图"
        elif image and stickers is not None:
            picked = stickers.pick(image)
            used_tag = image if picked else ""
        if picked is None and last_image:
            picked = Path(last_image)
            used_tag = used_tag or "主人发的图"
        if picked is not None and not picked.exists():
            picked = None

        qz = Qzone(onebot, uin, private=getattr(self.config, "qzone_private", 0))

        # 频率闸：一天几条、两条隔多久、哪个时段不发
        guard = _kwargs.get("qzone_guard")
        if guard is not None:
            allowed, why = guard.check()
            if not allowed:
                return f"（现在不能发：{why}。等能发了再说吧）"

        # 默认不直接发：先存草稿，等主人点头。
        # 「以后想发就私信主人」靠的就是这一步。
        if getattr(self.config, "qzone_require_approval", True):
            bot = _kwargs.get("bot")
            draft = {
                "content": content,
                "path": str(picked) if picked is not None else "",
                "tag": used_tag,
                "private": 1 if private else getattr(self.config, "qzone_private", 0),
            }
            if bot is not None:
                bot.qzone_draft = draft
            scope = "仅自己可见" if draft["private"] else "好友可见"
            shot = f"配图「{used_tag}」" if used_tag else "没有配图"
            log.info("说说草稿已存: %s", content[:40])
            return (
                f"草稿存好了（{scope}，{shot}）：{content}\n"
                "把这段话原样念给主人听，问他要不要发。"
            )

        richval = ""
        if picked is not None:
            try:
                richval = (await qz.upload_image(picked, content[:20]))["richval"]
            except QzoneError as exc:
                log.warning("配图上传失败，改发纯文字: %s", exc)

        try:
            result = await qz.publish(content, private=1 if private else None, richval=richval)
        except QzoneError as exc:
            return f"（空间发不出去：{exc}）"
        if not result.get("ok"):
            log.warning("说说发布失败 code=%s raw=%s", result.get("code"), result.get("raw"))
            return f"（空间那边没接收，返回码 {result.get('code')}）"

        shot = f"配图用了「{used_tag}」那张" if richval else "这次只发了文字（没配上图）"
        log.info("说说已发布（配图=%s）: %s", bool(richval), content[:40])
        # 直接发这条路以前不记频率闸，连发也不会被拦；补上
        if guard is not None:
            guard.note()

        # 写进长期记忆：以后她可以想起"我在空间发过什么"
        if memory is not None and session:
            note = f"（我在 QQ 空间发了条说说）{content}"
            if used_tag:
                note += f"（配图：{used_tag}）"
            try:
                memory.add_turn(session, "assistant", note, owner_id=owner_id)
            except Exception:  # noqa: BLE001
                log.warning("说说内容没写进记忆", exc_info=True)

        scope = "只有本鱼自己能看到" if private else "好友可见"
        return f"发好啦，{scope}，{shot}：{content}"
