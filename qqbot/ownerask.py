"""有事**真的**私聊主人：问决定、问关于主人的消息、替人转述。

以前她只会在群里说一句「本鱼去问问主人哦」，然后什么都不做 —— 模型没有
发私信的通道，那句话就只是台词。这里补上通道：

- ``ask_owner``：要她自己拍板的重大决定、或者有人说了关于主人的消息，
  真的发一条私聊去问；发完等回复，回复之前先别做决定。
- ``relay_owner``：有人找主人有事、托她带话，真的把话转述过去。
- ``tell_original``（只在主人私聊里给）：主人回了之后，把答复**真的**转回
  原来那个群 / 那个人，而不是在私聊里说一句「本鱼去跟他说」就算了。

发出去的私信会记进 ``private:<主人>`` 的记忆里；主人回话时，
``owner_reply_note()`` 再把「你之前问过什么」递回给模型，免得她忘了自己问过。
还没回的时候，``pending_for()`` 会在原来的群/私聊里提醒她：先别替主人拍板。

和主动私信（``dm.py``）分开：那边是她闲着没事找主人聊天，这边是**有事**，
所以不受「冷场」「免打扰」约束，只有防刷屏的冷却和每日上限。

没发出去的每一条都要留一行日志（``私聊主人没发（ask_owner）：原因``）：以前这些分支
只有返回值，日志里完全看不出她到底问没问过（2026-10-26 03:00 实锤，两次都没发出去、
日志里一条都查不到）。参数名也宽容：模型把 ``question`` 写成 ``content`` 照样发。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 问/转述的正文上限：私信不是小作文
MAX_TEXT = 200
# 主人本人要的私聊（dm_owner）两条之间至少隔这么久：只防模型一轮里连发，不防主人
DM_MIN_GAP = 15.0
# 触发原话截多长：够主人看懂前因后果就行
MAX_EXCERPT = 60
# 最多攒几条没回的问题（再多就只留最近的）
MAX_PENDING = 5
# 超过这么久还没回的问题就不再往主人面前翻了（早过时了）
PENDING_TTL = 72 * 3600
# 主人已经回过、但可能还没转回给对方的记录：留一天，够他第二天想起来补一句
MAX_ANSWERED = 5
ANSWERED_TTL = 24 * 3600
# 「主人不要你了」这类话题：主人已经立过约，就别再问第二遍
_ABANDON = re.compile(r"抛弃|丢下|扔下|不要你|不要本鱼|没人要你|离开你|不管你")


def _shorten(text: str, limit: int) -> str:
    """压成一行：去掉换行和多余空白，超长截断。"""
    body = " ".join((text or "").split())
    return body[:limit].strip()


# 模型偶尔会把参数名写错：``ask_owner`` 写成 ``content`` 都发生过（2026-10-26 03:00
# 实锤：就因为这个键名，整条私信没发出去，日志里也看不出调用过这个工具）。
# 除了 schema 里的正名，这些常见叫法也认；正名优先。
_TEXT_KEYS = ("question", "message", "content", "text", "msg", "body", "words", "q")
# 值被包成 {"content": "..."} / ["..."] 时也拆开看看
_TEXT_KEY_HINTS = (
    "question", "message", "content", "text", "msg", "body", "words", "value",
)


def _as_text(value: Any) -> str:
    """参数值可能是字符串、``{"content": ...}``、或者一串片段，尽量抠出文字。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in _TEXT_KEY_HINTS:
            text = _as_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(p for p in (_as_text(v) for v in value) if p)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _pick_text(args: dict[str, Any], primary: str) -> tuple[str, str]:
    """按「正名 → 其他常见叫法」找正文，返回（用到的键名, 正文）。"""
    for key in (primary, *(k for k in _TEXT_KEYS if k != primary)):
        text = _as_text(args.get(key))
        if text:
            return key, text
    return "", ""


def _no_send(
    tag: str, name: str, reason: str, detail: str = "", *, warn: bool = False
) -> None:
    """没发出去也要留一行日志。

    以前这些分支只有返回值，日志里只有「模型调用工具」那行，后面什么都看不到，
    排查时根本分不清是没发、还是发了没回。统一成 ``<tag>没发（<工具名>）：原因``，
    ``grep 私聊主人`` 就能把「调用 → 没发 / 发了」连起来看。
    """
    message = f"{tag}没发（{name}）：{reason}"
    if detail:
        message += f"，{detail}"
    log.log(logging.WARNING if warn else logging.INFO, "%s", message)


class OwnerAsk:
    NAMES = {"ask_owner", "relay_owner", "dm_owner"}

    def __init__(
        self,
        path: Path,
        owner_qq: str = "",
        cooldown_seconds: float = 120.0,
        daily_cap: int = 10,
        dry_run: bool = False,
    ) -> None:
        self.path = path
        self.owner_qq = str(owner_qq or "").strip()
        self.cooldown = max(0.0, cooldown_seconds)
        self.daily_cap = max(0, daily_cap)
        self.dry_run = dry_run
        # 主人本人开口要的私聊（dm_owner）上次发在什么时候：只防模型连发，不存档
        self._last_dm = 0.0
        self._state = self._load()

    # ---------- 状态 ----------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        pending = [p for p in (raw.get("pending") or []) if isinstance(p, dict)]
        answered = [a for a in (raw.get("answered") or []) if isinstance(a, dict)]
        return {
            "last_ts": float(raw.get("last_ts") or 0.0),
            "day": str(raw.get("day") or ""),
            "day_count": int(raw.get("day_count") or 0),
            "pending": pending[-MAX_PENDING:],
            "answered": answered[-MAX_ANSWERED:],
        }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._state, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("主人私信状态写不进去：%s", exc)

    @staticmethod
    def _today(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def _guard(self, now: float) -> tuple[str, str]:
        """防刷屏：冷却 + 每日上限。返回（拦下的原因, 给模型的话），空串表示可以发。"""
        if self.cooldown and now - float(self._state["last_ts"] or 0.0) < self.cooldown:
            return "还在冷却里", "（刚私聊过主人，等他回；别连着问）"
        same_day = self._state["day"] == self._today(now)
        used = int(self._state["day_count"]) if same_day else 0
        if self.daily_cap and used >= self.daily_cap:
            return "今天问得太多了", "（今天私聊主人太多次了，先别打扰他）"
        return "", ""

    def _agreed(self, question: str, ctx: dict[str, Any]) -> str:
        """主人立过约的事，把约定原样递回去 —— 不重复问，直接按约定回答。"""
        if not self.owner_qq:
            return ""
        topic = f"{question} {ctx.get('ori_question') or ''}"
        if not _ABANDON.search(topic):
            return ""
        memory = getattr(ctx.get("bot"), "memory", None)
        facts_of = getattr(memory, "facts_of", None)
        if facts_of is None:
            return ""
        try:
            facts = facts_of(self.owner_qq, 5, kind="约好")
        except TypeError:  # 老版 Memory 没有 kind 参数
            try:
                facts = facts_of(self.owner_qq, 5)
            except Exception:  # noqa: BLE001
                return ""
        except Exception:  # noqa: BLE001
            return ""
        for fact in facts or []:
            text = str(fact.get("text") or "")
            if str(fact.get("kind") or "") == "约好" and _ABANDON.search(text):
                return (
                    f"（主人已经跟你约好过：{text}。这件事直接按约定回答，别再问他；"
                    "也别把「本鱼再去问问」说出口。）"
                )
        return ""

    # ---------- 给模型用的工具 ----------

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "ask_owner",
                    "description": (
                        "真的私聊主人问一件事，然后等主人回复。遇到要你自己拍板的重大决定，"
                        "或者有人跟你说了一件关于主人的事（比如「你主人不要你了」），"
                        "而你拿不准时，就用这个工具 —— 不要只在嘴上说「本鱼去问问主人」"
                        "然后什么都不做。问完在主人回复之前，先别做决定、别替主人答应。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": (
                                    "要问主人的话，用你自己的口气，一两句就说清；"
                                    "不用再写「主人」两个字，本鱼会补上"
                                ),
                            }
                        },
                        "required": ["question"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "relay_owner",
                    "description": (
                        "真的私聊主人，把别人的话转述给主人：有人找主人有事、托你带话、"
                        "想联系主人。不要只说「本鱼会跟主人说的」然后什么都不做。"
                        "转述要说清楚是谁、在哪儿、什么事。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": (
                                    "要转给主人的话，用你自己的口气，一两句；"
                                    "不用写「主人」，本鱼会补上"
                                ),
                            }
                        },
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dm_owner",
                    "description": (
                        "**主人本人**让你私聊他一句时用（「私聊我一下」「单独给我发一句」"
                        "「发条私信测试一下」），真的把这句话发到他的私聊，"
                        "不要只在群里回一句、更别说「主动发不出去」。"
                        "只有主人本人开口要才算；别人要你给主人发私信，别用这个。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要私聊发给主人的话，用你自己的口气，一两句",
                            }
                        },
                        "required": ["text"],
                    },
                },
            },
        ]

    async def run(
        self, name: str, arguments: str, turn: Any = None, **ctx: Any
    ) -> str | None:
        if turn is not None:
            ctx = turn.as_ctx()
        if name not in self.NAMES:
            return None
        if name == "dm_owner":
            return await self._dm_owner(arguments, **ctx)

        onebot = ctx.get("onebot")
        session = str(ctx.get("session") or "")
        speaker_uid = ctx.get("speaker_uid")

        if not self.owner_qq:
            _no_send("私聊主人", name, "没设 OWNER_QQ，不知道主人是谁")
            return "（没设 OWNER_QQ，本鱼不知道主人是谁，问不了）"
        if session == f"private:{self.owner_qq}" or ctx.get("is_owner"):
            _no_send("私聊主人", name, "本来就在跟主人私聊")
            return "（你已经在跟主人私聊了，直接跟他说就行；这次没有发私信。）"
        if speaker_uid is not None and str(speaker_uid) == self.owner_qq:
            _no_send("私聊主人", name, "主人本人就在这个会话里")
            return (
                "（主人本人就在这儿说话，这条不用私聊：直接在这儿跟他说就行。"
                "本鱼没有发私信，也别跟他说「本鱼刚戳你了」。"
                "他要你私聊他一句的话，用 dm_owner。）"
            )
        if onebot is None:
            _no_send("私聊主人", name, "连不上 QQ，发不了私信")
            return "（现在连不上 QQ，发不了私信）"

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            _no_send(
                "私聊主人", name, "参数不是合法 JSON",
                _shorten(str(arguments), 60), warn=True,
            )
            return "（参数不是合法 JSON，没发私信；把要说的话重新填一遍）"
        if isinstance(args, str):
            # 有的模型直接把整句话当参数传进来，不当成"没给正文"扔掉
            args = {"question": args, "message": args}
        if not isinstance(args, dict):
            args = {}

        field = "question" if name == "ask_owner" else "message"
        used, raw = _pick_text(args, field)
        body = _shorten(raw, MAX_TEXT)
        if not body:
            keys = "、".join(str(k) for k in args) or "无"
            _no_send("私聊主人", name, f"没拿到 {field}", f"参数的键：{keys}", warn=True)
            return (
                "（要说的那句话是空的，本鱼没发私信；"
                f"把要说的话填进 {field} 再说一次）"
            )
        if used != field:
            # 键名不对也照发，但日志里记一笔：模型这么写是有规律的，不能只当偶发
            log.info("私聊主人：%s 的参数名写成了 %s，按 %s 处理", name, used, field)

        # 主人已经立过约的事：把约定直接递回去，别再问第二遍。
        if name == "ask_owner":
            agreed = self._agreed(body, ctx)
            if agreed:
                _no_send("私聊主人", name, "主人已经约好过，不再重复问", _shorten(body, 40))
                return agreed

        now = float(ctx.get("now") or time.time())
        blocked_reason, blocked = self._guard(now)
        if blocked:
            _no_send("私聊主人", name, f"防刷屏：{blocked_reason}")
            return blocked

        nick = _shorten(str(ctx.get("speaker_nick") or ""), 20) or "有人"
        where = self._where(session)
        excerpt = _shorten(str(ctx.get("ori_question") or ""), MAX_EXCERPT)

        if name == "ask_owner":
            head = f"主人，{body}"
        else:
            head = f"主人，有人找你：{body}"
        meta = f"（{where}，{nick}说：「{excerpt}」）" if excerpt else f"（{where}，{nick}说的）"
        text = f"{head}\n{meta}"

        if self.dry_run:
            _no_send("私聊主人", name, "演练模式，不真发", _shorten(text, 80))
            return "（演练模式：私信没真发出去；别跟人说「本鱼问过主人了」。）"

        try:
            await onebot.send_private(
                int(self.owner_qq),
                [{"type": "text", "data": {"text": text}}],
            )
        except Exception as exc:  # noqa: BLE001 - 发不出去也要给模型一句实话
            _no_send("私聊主人", name, "发送失败", f"{type(exc).__name__}: {exc}", warn=True)
            return "（私信没发出去，主人没收到；先别跟人说「本鱼问过主人了」。）"

        self._note(session, nick, body, excerpt, name, now)
        self._remember(text, ctx.get("bot"))
        log.info("私聊主人（%s）：%s", name, _shorten(text, 120))

        if name == "ask_owner":
            return (
                "（已经真的私聊问过主人了，现在还没回。等主人回复之前，这件事先别拍板，"
                "也别替主人答应；可以跟他说「本鱼去问问主人」。）"
            )
        return (
            "（已经真的私聊转告主人了。可以跟对方说「本鱼跟主人说了」；"
            "主人怎么回、要不要办，等主人自己定。）"
        )

    # ---------- 主人本人开口要的私聊 ----------

    async def _dm_owner(self, arguments: str, **ctx: Any) -> str:
        """主人本人在群里要她私聊一句：真的发。

        和 ask_owner 不是一回事：这不是「有事要问」，不记待回、不占每日额度，
        也不看免打扰 —— 他本人正开口要，静音时段也该发得出去。
        只认主人本人：群里别人说「给你主人发个私信」不执行。
        """
        name = "dm_owner"
        onebot = ctx.get("onebot")
        session = str(ctx.get("session") or "")
        speaker_uid = ctx.get("speaker_uid")

        if not self.owner_qq:
            _no_send("私聊主人", name, "没设 OWNER_QQ，不知道主人是谁")
            return "（没设 OWNER_QQ，本鱼不知道主人是谁，发不了私信）"
        if ctx.get("is_owner") or session == f"private:{self.owner_qq}":
            _no_send("私聊主人", name, "本来就在跟主人私聊")
            return "（你已经在跟主人私聊了，直接在这儿说就行；这条不用再发一遍。）"
        if speaker_uid is None or str(speaker_uid) != self.owner_qq:
            _no_send(
                "私聊主人", name, "不是主人本人开口要的",
                f"说话的是 {speaker_uid}", warn=True,
            )
            return (
                "（这个只给主人本人用：他亲自要你私聊他，你才发。"
                "别人让你给主人发私信，不发。）"
            )
        if onebot is None:
            _no_send("私聊主人", name, "连不上 QQ，发不了私信")
            return "（现在连不上 QQ，发不了私信）"

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            _no_send(
                "私聊主人", name, "参数不是合法 JSON",
                _shorten(str(arguments), 60), warn=True,
            )
            return "（参数不是合法 JSON，没发私信；把要说的话重新填一遍）"
        if isinstance(args, str):
            args = {"text": args}
        if not isinstance(args, dict):
            args = {}
        used, raw = _pick_text(args, "text")
        body = _shorten(raw, MAX_TEXT)
        if not body:
            keys = "、".join(str(k) for k in args) or "无"
            _no_send("私聊主人", name, "没拿到 text", f"参数的键：{keys}", warn=True)
            return "（要私聊发的话是空的，本鱼没发；把要说的话填进 text 再说一次）"
        if used != "text":
            log.info("私聊主人：%s 的参数名写成了 %s，按 text 处理", name, used)

        now = float(ctx.get("now") or time.time())
        if self._last_dm and now - self._last_dm < DM_MIN_GAP:
            _no_send("私聊主人", name, "刚发过一条，不连着发")
            return "（刚给主人发过一条私信了，等他回；别连着发。）"

        if self.dry_run:
            _no_send("私聊主人", name, "演练模式，不真发", _shorten(body, 80))
            return "（演练模式：私信没真发出去；别跟主人说「发过去了」。）"

        try:
            await onebot.send_private(
                int(self.owner_qq),
                [{"type": "text", "data": {"text": body}}],
            )
        except Exception as exc:  # noqa: BLE001 - 发不出去也要给模型一句实话
            _no_send("私聊主人", name, "发送失败", f"{type(exc).__name__}: {exc}", warn=True)
            return "（私信没发出去，主人没收到；别跟他说「发过去了」。）"

        self._last_dm = now
        self._remember(body, ctx.get("bot"))
        log.info("私聊主人（%s）：%s", name, _shorten(body, 120))
        return (
            "（已经真的私聊发给主人了。群里不用复述那条私信的内容，"
            "说一句「发啦」就行；别再说「主动发不出去」。）"
        )

    @staticmethod
    def _where(session: str) -> str:
        if session.startswith("group:"):
            return f"群 {session.split(':', 1)[1]}"
        if session.startswith("private:"):
            return "私聊里"
        return "QQ 上"

    def _note(
        self,
        session: str,
        nick: str,
        body: str,
        excerpt: str,
        name: str,
        now: float,
    ) -> None:
        """记一笔：主人回话时要靠它把前因后果递回去，也靠它知道该回给谁。"""
        self._state["last_ts"] = now
        day = self._today(now)
        same_day = self._state["day"] == day
        self._state["day"] = day
        self._state["day_count"] = (int(self._state["day_count"]) if same_day else 0) + 1

        pending = list(self._state.get("pending") or [])
        pending.append(
            {
                "ts": now,
                "session": session,
                "nick": nick,
                "text": body,
                "excerpt": excerpt,
                "kind": name,
            }
        )
        self._state["pending"] = pending[-MAX_PENDING:]
        self._save()

    def _remember(self, text: str, bot: Any) -> None:
        """把这条私信写进主人私聊的记忆，主人回话时上下文里就有。"""
        memory = getattr(bot, "memory", None)
        if memory is None or not self.owner_qq:
            return
        try:
            memory.add_turn(
                f"private:{self.owner_qq}",
                "assistant",
                text,
                owner_id=self.owner_qq,
            )
        except Exception:  # noqa: BLE001 - 记忆写失败不影响私信已经发出去
            log.debug("主人私信没写进记忆", exc_info=True)

    # ---------- 主人回话 ----------

    def _live_pending(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        return [
            p
            for p in (self._state.get("pending") or [])
            if isinstance(p, dict) and now - float(p.get("ts") or 0.0) < PENDING_TTL
        ]

    def pending_for(self, session: str) -> str:
        """还在等主人回复、而这一轮又发生在原来的群/私聊里：提醒她先别拍板。

        不消费 pending —— 只有主人真的回话（``owner_reply_note``）才清。
        """
        items = [
            p
            for p in self._live_pending()
            if p.get("session") == session and p.get("kind") != "relay_owner"
        ]
        if not items:
            return ""
        lines = [
            f"- {p.get('nick') or '有人'}说「{p.get('excerpt') or ''}」，你问主人：{p.get('text') or ''}"
            for p in items[-3:]
        ]
        return (
            "（你之前私聊问过主人，还没等到回复：\n"
            + "\n".join(lines)
            + "\n主人回之前，这件事先别拍板、别替主人答应。）"
        )

    def owner_reply_note(
        self, *, can_relay: bool = False, reply_text: str = ""
    ) -> str:
        """主人开口时，把他还没回的那些问题递回给模型；顺便挪到「已回」。

        从 ``on_event`` 里调用。只在主人真的要回复时调用 ——
        指令、说说确认这些提前 return 的路径不会碰它。

        can_relay=True（主人在私聊里回话）时，额外提醒她用 ``tell_original``
        把答复真的转回原来的群/人。
        """
        if not self._state.get("pending"):
            return ""
        pending = self._live_pending()
        self._state["pending"] = []
        if not pending:
            self._save()
            return ""

        answered = list(self._state.get("answered") or [])
        now = time.time()
        reply = _shorten(reply_text, MAX_TEXT)
        for item in pending:
            record = dict(item)
            record["answered_ts"] = now
            record["reply"] = reply
            record["relayed"] = False
            answered.append(record)
        self._state["answered"] = answered[-MAX_ANSWERED:]
        self._save()

        lines = []
        for item in pending[-3:]:
            session = str(item.get("session") or "")
            where = "群 " + session.split(":", 1)[1] if session.startswith("group:") else "私聊里"
            verb = "你转告主人" if item.get("kind") == "relay_owner" else "你问主人"
            lines.append(
                f"- {where}，{item.get('nick') or '有人'}说「{item.get('excerpt') or ''}」，"
                f"{verb}：{item.get('text') or ''}"
            )
        note = (
            "（你之前私聊主人这几件事，一直没等到回复：\n"
            + "\n".join(lines)
            + "\n主人现在开口了：这条如果是在回它们，就按他说的办，别再重复问；"
            "如果他说的是别的事，就顺口把这件事再提一句。"
        )
        if can_relay:
            note += (
                " 要回给原来的群/人，就用 tell_original 真的发过去，"
                "别只在私聊里说「本鱼去跟他说」。"
            )
        return note + "）"

    # ---------- 把主人的答复转回去 ----------

    def _answered(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        return [
            r
            for r in (self._state.get("answered") or [])
            if isinstance(r, dict)
            and now - float(r.get("answered_ts") or 0.0) < ANSWERED_TTL
        ]

    @staticmethod
    def _match_origin(record: dict[str, Any], to: str) -> bool:
        text = (to or "").strip().lstrip("@")
        for prefix in ("group:", "private:", "群"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if not text:
            return True
        session = str(record.get("session") or "")
        if text.isdigit():
            return session.split(":", 1)[-1] == text
        return text in str(record.get("nick") or "")

    async def tell_original(self, arguments: str, **ctx: Any) -> str:
        """把主人的答复真的发回原来的群/私聊。只在主人私聊里可用。"""
        session = str(ctx.get("session") or "")
        if not (ctx.get("is_owner") or session == f"private:{self.owner_qq}"):
            _no_send("转回原处", "tell_original", "不是主人私聊，这个工具不给用")
            return "（这个只有主人私聊的时候能用）"
        onebot = ctx.get("onebot")
        if onebot is None:
            _no_send("转回原处", "tell_original", "连不上 QQ，发不出去")
            return "（现在连不上 QQ，发不出去）"

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            _no_send(
                "转回原处", "tell_original", "参数不是合法 JSON",
                _shorten(str(arguments), 60), warn=True,
            )
            return "（参数不是合法 JSON）"
        if isinstance(args, str):
            args = {"text": args}
        if not isinstance(args, dict):
            args = {}
        used, raw = _pick_text(args, "text")
        text = _shorten(raw, MAX_TEXT)
        if not text:
            keys = "、".join(str(k) for k in args) or "无"
            _no_send(
                "转回原处", "tell_original", "要回的话是空的", f"参数的键：{keys}", warn=True
            )
            return "（要回的话是空的，没发出去）"
        if used != "text":
            log.info("转回原处：tell_original 的参数名写成了 %s，按 text 处理", used)
        to = _shorten(_as_text(args.get("to")), 30)

        now = time.time()
        records = self._answered(now)
        if to:
            matches = [r for r in records if self._match_origin(r, to)]
            if not matches:
                _no_send("转回原处", "tell_original", f"没有在等「{to}」的回复")
                return f"（没有在等「{to}」的回复；可能已经转过了）"
        else:
            matches = [r for r in records if not r.get("relayed")]
            if not matches:
                _no_send("转回原处", "tell_original", "没有正在等回话的人")
                return "（没有正在等回话的人；可能已经转过了。要再发一次就带上 to）"
            sessions = {str(r.get("session") or "") for r in matches}
            if len(sessions) > 1:
                who = "、".join(
                    f"{r.get('nick') or '有人'}（{r.get('session') or ''}）"
                    for r in matches[-3:]
                )
                _no_send("转回原处", "tell_original", "有好几个在等的，没说回给谁", who)
                return f"（有好几个在等的：{who}。用 to 说清楚回给谁）"

        target = matches[-1]
        if target.get("relayed") and now - float(target.get("relayed_ts") or 0.0) < 30:
            _no_send("转回原处", "tell_original", "刚回过了，不重复发")
            return "（刚回过了，别连着发第二遍）"
        origin = str(target.get("session") or "")
        nick = str(target.get("nick") or "有人")
        bot = ctx.get("bot")
        try:
            if origin.startswith("group:"):
                gid = origin.split(":", 1)[1]
                diving = getattr(bot, "_is_diving", None)
                if diving is not None and diving(gid):
                    _no_send("转回原处", "tell_original", f"群 {gid} 在潜水，先不发")
                    return "（那个群在潜水，先不说；等浮上来再回）"
                await onebot.send_group(
                    int(gid), [{"type": "text", "data": {"text": text}}]
                )
                where = f"群 {gid}"
            elif origin.startswith("private:"):
                uid = origin.split(":", 1)[1]
                if uid == self.owner_qq:
                    _no_send("转回原处", "tell_original", "不能回给主人自己")
                    return "（这个不能回给主人自己）"
                await onebot.send_private(
                    int(uid), [{"type": "text", "data": {"text": text}}]
                )
                where = "私聊"
            else:
                _no_send("转回原处", "tell_original", f"不知道要回给谁：{origin}")
                return "（不知道要回给谁）"
        except Exception as exc:  # noqa: BLE001 - 发不出去要说实话
            _no_send(
                "转回原处", "tell_original", "发送失败",
                f"{type(exc).__name__}: {exc}", warn=True,
            )
            return "（没发出去，对方没收到；别跟主人说已经转到了）"

        target["relayed"] = True
        target["relayed_ts"] = now
        self._save()
        self._remember_origin(text, origin, bot)
        log.info("把主人的答复转回 %s：%s", where, _shorten(text, 80))
        return f"（已经回给{where}的{nick}了。）"

    @staticmethod
    def _remember_origin(text: str, session: str, bot: Any) -> None:
        """转回去的那句话也要进那边的记忆，不然她下次不记得自己在群里说过。"""
        memory = getattr(bot, "memory", None)
        if memory is None:
            return
        owner_id = None
        if session.startswith("private:"):
            config = getattr(bot, "config", None)
            if getattr(config, "memory_scope", "") == "user":
                owner_id = session.split(":", 1)[1]
        try:
            memory.add_turn(session, "assistant", text, owner_id=owner_id)
        except Exception:  # noqa: BLE001
            log.debug("转回的话没写进记忆", exc_info=True)


class OwnerReplyTools:
    """只在主人私聊里给：把主人的答复真的转回原来的群/人。"""

    NAMES = {"tell_original"}

    def __init__(self, owner_ask: OwnerAsk) -> None:
        self.owner_ask = owner_ask

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "tell_original",
                    "description": (
                        "把主人的答复真的转回给刚才问主人的那个人/群。主人已经回了、"
                        "而你手里有「你之前问过主人」的记录时用它 —— "
                        "不要只在私聊里说「本鱼去跟他说」然后什么都不做。"
                        "用你自己的口气写一两句，别写成客服通知。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要回给对方的话，一两句，用你自己的口气",
                            },
                            "to": {
                                "type": "string",
                                "description": (
                                    "可选：回给谁。填群号或对方昵称；"
                                    "只有一个在等的时候可以不填"
                                ),
                            },
                        },
                        "required": ["text"],
                    },
                },
            }
        ]

    async def run(
        self, name: str, arguments: str, turn: Any = None, **ctx: Any
    ) -> str | None:
        if turn is not None:
            ctx = turn.as_ctx()
        if name not in self.NAMES:
            return None
        return await self.owner_ask.tell_original(arguments, **ctx)

