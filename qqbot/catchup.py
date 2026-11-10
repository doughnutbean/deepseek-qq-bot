"""上线补收私聊：启动/重连后，把「她没接上」的私聊补回来。

为什么要它：进程不在、NapCat 被踢、或者这一轮处理到一半崩了，别人发来的
私聊就不会有人回。NapCat 重连**不会**把断线期间的消息补推一遍（2026-10-26
实锤），所以只能自己上线时去拉。

怎么判断哪条私聊没回：**看这段私聊最后一条是谁说的**。正常情况下她的每条
私聊都以她自己的话结尾；最后一条还是对方说的，就说明她没接上。这个判断
不需要 checkpoint、也不需要按 message_id 记账：

- 回完一条，最后一条就变成她说的了，下一次扫描自然不会重复；
- 发送失败 / 模型失败，最后一条还是对方的，下次上线继续补；
- 同一段私聊里对方连发的几条合成一轮，只回一条。

补回来的事件带 ``_catchup=True``：
- 跳过 ``STALE_MESSAGE_SECONDS`` 那道积压门（否则补回来的也被丢掉）；
- 跳过所有本地指令处理（指令有时效性，隔了几小时不能真照做）；
- 给模型加一句「这是掉线期间漏收的，隔了多久」，让她先说明再继续聊；
- 不开放会改状态的工具（改资料、删好友、私聊主人、写记忆）。

默认只补最近 24 小时；超过就忽略，不对几天前的消息突然诈尸。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import moderation
from .commands import GROUP_ACCESS_KINDS, parse as parse_command
from .private_requests import is_friend_request_notice, is_group_invite

log = logging.getLogger(__name__)

# 同一进程里只跑一份：启动和重连可能同时触发
_LOCK = asyncio.Lock()

MAX_RECENT = 100

# 只有「改设置/做动作」这类指令过了几小时才必须拦下来。
# `tier_delta` / `soothe` 不在这里：「怎么不理我了」「别生气了」是聊天，
# 按指令拦会把一句正常的委屈话解释成「过期指令」，反而更糟。
_TIME_SENSITIVE_KINDS = frozenset(
    {
        "feature",
        "reset",
        "private_ban",
        "private_unban",
        "group_dive",
        "group_dive_off",
        "group_mute",
        "group_unmute",
        "group_push",
        "presence_push_all",
        "speaker_add",
        "speaker_del",
        "sticker_global",
        "sticker_manual",
        "sticker_replace",
        "sticker_steal",
        "sticker_tag",
        "sticker_this",
        "thinking",
        "title",
        "todo",
        "verbosity",
        "dm_pace",
        *GROUP_ACCESS_KINDS,
    }
)


@dataclass
class Contact:
    """一个待检查的私聊。"""

    uid: str
    nickname: str
    last_ts: float
    chat_type: int = 1


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sender_uid(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, dict):
        return str(sender.get("user_id") or "")
    return str(message.get("user_id") or "")


def _sender_nick(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, dict):
        return str(sender.get("card") or sender.get("nickname") or "").strip()
    return ""


def message_text(segments: Any) -> str:
    """把消息段拼成一行纯文本（只用于判指令/日志，不替代 app 的渲染）。"""
    if isinstance(segments, str):
        return segments
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            parts.append(f"@{data.get('qq') or ''}")
        elif kind in {"image", "mface"}:
            parts.append("[图片]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "video":
            parts.append("[视频]")
        elif kind == "file":
            parts.append("[文件]")
    return "".join(parts).strip()


def _extract_contacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("contact", "recent", "list", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
    return []


def _extract_history(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    rows: Any = data
    if isinstance(data, dict):
        rows = data.get("messages") or data.get("message") or []
    if not isinstance(rows, list):
        return []
    return [x for x in rows if isinstance(x, dict)]


def pick_contacts(
    items: list[dict[str, Any]],
    *,
    bot_qq: str,
    max_age_seconds: float,
    now: float | None = None,
    owner_qq: str = "",
) -> list[Contact]:
    """从 ``get_recent_contact`` 里挑出「最后一条不是她说的」私聊。

    主人排最前，其余按最后一条消息从新到旧。群聊（chatType=2）不补。
    """
    now = time.time() if now is None else now
    picked: dict[str, Contact] = {}
    for item in items:
        try:
            chat_type = int(item.get("chatType") or 0)
        except (TypeError, ValueError):
            continue
        if chat_type not in (1, 100):
            continue
        uid = str(item.get("peerUin") or item.get("user_id") or "").strip()
        if not uid or uid == str(bot_qq):
            continue
        last = item.get("lastestMsg") or item.get("latestMsg") or {}
        if not isinstance(last, dict):
            continue
        sender = _sender_uid(last)
        # 最后一条是她说的 = 已经回过（或者至少接上了），不补
        if not sender or sender == str(bot_qq):
            continue
        ts = float(_as_int(last.get("time") or item.get("msgTime")))
        if ts <= 0:
            continue
        age = now - ts
        # 时钟偏一点不算错：未来 60 秒内的当刚发的
        if age > max_age_seconds or age < -60:
            continue
        nickname = _sender_nick(last) or str(item.get("remark") or "").strip() or uid
        picked[uid] = Contact(uid=uid, nickname=nickname, last_ts=ts, chat_type=chat_type)

    contacts = list(picked.values())
    contacts.sort(
        key=lambda c: (0 if owner_qq and c.uid == str(owner_qq) else 1, -c.last_ts)
    )
    return contacts


def trailing_messages(
    history: list[dict[str, Any]],
    *,
    bot_qq: str,
    max_age_seconds: float,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """取「她最后一条消息之后」对方发来的那些，最多回溯 max_age。

    用下标而不是时间戳决定「最后一条她说的」：同秒消息按接口返回顺序
    才分得清先后。
    """
    now = time.time() if now is None else now
    last_self = -1
    for index, message in enumerate(history):
        if _sender_uid(message) == str(bot_qq):
            last_self = index
    tail = history[last_self + 1 :]
    out: list[dict[str, Any]] = []
    for message in tail:
        sender = _sender_uid(message)
        if not sender or sender == str(bot_qq):
            continue
        ts = float(_as_int(message.get("time")))
        if ts <= 0 or now - ts > max_age_seconds or now - ts < -60:
            continue
        if not message.get("message"):
            continue
        out.append(message)
    return out


def _expired_command(text: str) -> str:
    """这句是不是「过了几小时就不该再执行」的指令；是就返回原句。"""
    parsed = parse_command(text)
    if parsed is None or parsed[0] not in _TIME_SENSITIVE_KINDS:
        return ""
    return text


def _blocked_reason(messages: list[dict[str, Any]]) -> str:
    """补收前先挡掉不该回的：广告/骚扰/冒充主人。

    好友申请、拉群邀请有自己的固定流程（不算广告），要留着给 app 处理。
    """
    segments = [
        seg for message in messages for seg in (message.get("message") or []) if isinstance(seg, dict)
    ]
    texts = [message_text(message.get("message")) for message in messages]
    if any(
        text and (is_friend_request_notice(text) or is_group_invite(text, segments))
        for text in texts
    ):
        return ""
    for text in texts:
        if text and moderation.classify(text) is not None:
            return text
    return ""


def build_event(
    contact: Contact,
    messages: list[dict[str, Any]],
    *,
    bot_qq: str,
    now: float | None = None,
) -> dict[str, Any] | None:
    """把一段漏收的私聊合成一个事件，交给正常的回复链路。"""
    if not messages:
        return None
    now = time.time() if now is None else now
    last = messages[-1]
    segments: list[dict[str, Any]] = []
    command = ""
    for message in messages:
        segs = message.get("message")
        if isinstance(segs, list):
            segments.extend(seg for seg in segs if isinstance(seg, dict))
        text = message_text(segs)
        if text and not command:
            command = _expired_command(text)
    if not segments:
        return None

    last_ts = float(_as_int(last.get("time"))) or contact.last_ts
    sender = last.get("sender") if isinstance(last.get("sender"), dict) else {}
    nickname = _sender_nick(last) or contact.nickname
    age_seconds = max(0.0, now - last_ts)
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend" if contact.chat_type == 1 else "normal",
        "self_id": str(bot_qq),
        "user_id": _as_int(contact.uid),
        "message_id": None,
        "time": int(last_ts),
        "sender": {**sender, "user_id": _as_int(contact.uid), "nickname": nickname},
        "message": segments,
        # _merged：不要重复记情绪/好感/事实，也不要再进合并窗口
        "_merged": True,
        "_merged_count": len(messages),
        # 补收标记：放行积压门、跳过本地指令、给模型加「才看见」提示
        "_catchup": True,
        "_catchup_age_seconds": age_seconds,
        "_catchup_command": command,
    }


def catchup_note(event: dict[str, Any]) -> str:
    """给模型的「这条是补回来的」说明。没有标记就返回空串。"""
    if not event.get("_catchup"):
        return ""
    age = max(0, int(float(event.get("_catchup_age_seconds") or 0)))
    if age >= 3600:
        wait = f"{age / 3600:.1f} 小时"
    elif age >= 60:
        wait = f"{max(1, age // 60)} 分钟"
    else:
        wait = "不到一分钟"
    command = str(event.get("_catchup_command") or "").strip()
    if command:
        return (
            f"（这条私聊是掉线/重启期间漏收的，隔了 {wait} 才看见。"
            f"里面有一句原本是指令「{command}」，已经过期了：**不要执行、也不要说你已经照做了**，"
            "只回一句「这条才看见，已经过时了，还要的话再说一遍」。其余的话正常接。）"
        )
    if age <= 10 * 60:
        return (
            f"（这条私聊是刚才掉线那阵子漏收的，隔了 {wait}；"
            "可以顺口提一句刚才卡了一下，然后正常接，别当作没发生过。）"
        )
    return (
        f"（这条私聊是掉线期间漏收的，隔了 {wait} 才看见。"
        "先老实说一句「掉线了才看见 / 刚回来」，再一条条接；"
        "不要装作当时在场，也不要解释技术原因或说自己在忙。）"
    )


async def _call(onebot: Any, action: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    payload = await onebot.call(action, params, timeout=timeout)
    return payload if isinstance(payload, dict) else {}


def _busy(bot: Any, uid: str) -> bool:
    """这个人是不是正在被正常链路处理（别补出一条重复的回复）。"""
    session = f"private:{uid}"
    locks = getattr(bot, "locks", None) or {}
    lock = locks.get(session) if hasattr(locks, "get") else None
    if lock is not None and getattr(lock, "locked", lambda: False)():
        return True
    key = f"p:{uid}"
    pending = getattr(bot, "pending_private", None) or {}
    if hasattr(pending, "get") and pending.get(key):
        return True
    tasks = getattr(bot, "pending_private_tasks", None) or {}
    task = tasks.get(key) if hasattr(tasks, "get") else None
    return task is not None and not task.done()


async def run_once(
    bot: Any,
    *,
    onebot: Any = None,
    now: float | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """扫一遍最近的私聊，把没接上的补回来。返回一份统计。"""
    config = bot.config
    report: dict[str, Any] = {
        "ok": True,
        "checked": 0,
        "unreplied": 0,
        "replied": 0,
        "failed": [],
        "skipped": "",
        "contacts": [],
    }
    if not bool(getattr(config, "enable_catchup_dm", False)):
        report.update(ok=False, skipped="disabled")
        return report
    if not bool(getattr(config, "allow_private", True)):
        report.update(ok=False, skipped="private_off")
        return report

    now = time.time() if now is None else now
    onebot = onebot or getattr(bot, "onebot", None)
    if onebot is None:
        report.update(ok=False, skipped="no_onebot")
        return report
    dry_run = bool(config.catchup_dry_run) if dry_run is None else dry_run
    max_age = max(0.0, float(getattr(config, "catchup_max_age_hours", 24.0))) * 3600

    async with _LOCK:
        try:
            payload = await _call(
                onebot, "get_recent_contact", {"count": MAX_RECENT}, timeout=15
            )
        except Exception as exc:  # noqa: BLE001 - 拉不到就当这次没补，不影响聊天
            log.warning("上线补收：拉最近私聊失败（这次不补）：%s", exc)
            report.update(ok=False, skipped=f"recent_failed: {type(exc).__name__}")
            return report

        items = _extract_contacts(payload)
        contacts = pick_contacts(
            items,
            bot_qq=str(getattr(config, "bot_qq", "") or ""),
            max_age_seconds=max_age,
            now=now,
            owner_qq=str(getattr(config, "owner_qq", "") or ""),
        )
        max_contacts = max(0, int(getattr(config, "catchup_max_contacts", 20)))
        if max_contacts:
            contacts = contacts[:max_contacts]
        report["checked"] = len(items)
        report["unreplied"] = len(contacts)

        prefs = getattr(bot, "prefs", None)
        banned = getattr(prefs, "private_banned", None)
        if callable(banned):
            kept = []
            for contact in contacts:
                try:
                    if banned(contact.uid):
                        log.info("上线补收：%s 在私聊黑名单里，跳过", contact.uid)
                        continue
                except Exception:  # noqa: BLE001 - 名单读失败不能拦着补收
                    log.debug("读私聊黑名单失败", exc_info=True)
                kept.append(contact)
            contacts = kept

        report["contacts"] = [
            {"uid": c.uid, "nickname": c.nickname, "age_minutes": int((now - c.last_ts) // 60)}
            for c in contacts
        ]
        if not contacts:
            log.info("上线补收：最近私聊都接上了，没有要补的")
            return report
        log.info(
            "上线补收：%d 个私聊最后一条不是她说的，开始补：%s",
            len(contacts),
            "、".join(f"{c.nickname}({c.uid})" for c in contacts[:8]),
        )
        if dry_run:
            log.info("上线补收（演练，不真回）：%s", report["contacts"])
            report["skipped"] = "dry_run"
            return report

        limit = max(0, int(getattr(config, "catchup_max_replies", 10)))
        history_count = max(1, int(getattr(config, "catchup_history_count", 100)))
        spacing = max(0.0, float(getattr(config, "catchup_reply_spacing_seconds", 2.0)))
        handled = 0
        for contact in contacts:
            if limit and handled >= limit:
                log.info("上线补收：这次已经回满 %d 个，剩下的下次再补", limit)
                break
            try:
                payload = await _call(
                    onebot,
                    "get_friend_msg_history",
                    {"user_id": _as_int(contact.uid), "count": history_count},
                    timeout=20,
                )
            except Exception as exc:  # noqa: BLE001 - 单人拉历史失败不拖累其他人
                log.warning("上线补收：拉 %s 的历史失败：%s", contact.uid, exc)
                report["failed"].append(contact.uid)
                continue
            messages = trailing_messages(
                _extract_history(payload),
                bot_qq=str(getattr(config, "bot_qq", "") or ""),
                max_age_seconds=max_age,
                now=now,
            )
            if not messages:
                log.info("上线补收：%s 没有新的漏收消息（可能已经回过）", contact.uid)
                continue
            blocked = _blocked_reason(messages)
            if blocked:
                log.info(
                    "上线补收：%s 的漏收消息像广告/骚扰，不补：%s",
                    contact.uid,
                    blocked[:40],
                )
                continue
            if _busy(bot, contact.uid):
                # 正常链路正在处理这个人（刚到的消息 / 还在合并窗口里），
                # 这时再补一条就是双回。
                log.info("上线补收：%s 正在被正常处理，跳过（下次再说）", contact.uid)
                continue
            # 最后一条必须以对方结尾：历史里如果她已经在扫描之后回了，
            # trailing_messages 会是空的；这里再兜一层。
            event = build_event(
                contact,
                messages,
                bot_qq=str(getattr(config, "bot_qq", "") or ""),
                now=now,
            )
            if event is None:
                continue
            try:
                await bot.on_event(event)
            except Exception as exc:  # noqa: BLE001 - 单个人补失败不影响别人
                log.exception("上线补收：补 %s 失败", contact.uid)
                report["failed"].append(contact.uid)
                continue
            handled += 1
            report["replied"] += 1
            log.info(
                "上线补收：补回 %s(%s) 的 %d 条消息（最后一条 %d 分钟前）",
                contact.nickname,
                contact.uid,
                len(messages),
                int((now - contact.last_ts) // 60),
            )
            if spacing:
                await asyncio.sleep(spacing)

        if report["replied"]:
            log.info(
                "上线补收完成：回了 %d 个私聊%s",
                report["replied"],
                f"，失败 {len(report['failed'])} 个" if report["failed"] else "",
            )
        return report


async def watch(
    bot: Any,
    onebot: Any,
    stop_event: asyncio.Event,
    *,
    sleeper=asyncio.sleep,
    clock=time.monotonic,
    poll: float = 2.0,
) -> None:
    """盯着连接状态：每次连上、稳定一小会儿后补一次。

    重连也算一次（NapCat 被踢几分钟、机器人进程还活着的情况）；
    同一次连接只跑一次，避免每两秒扫一遍。
    """
    config = bot.config
    if not bool(getattr(config, "enable_catchup_dm", False)):
        return
    settle = max(0.0, float(getattr(config, "catchup_settle_seconds", 20.0)))
    connected = False
    up_since = 0.0
    ran = False
    next_try = 0.0
    while not stop_event.is_set():
        moment = clock()
        if not onebot.connected.is_set():
            connected = False
            ran = False
            await sleeper(poll)
            continue
        if not connected:
            connected = True
            up_since = moment
            ran = False
            next_try = 0.0
        if not ran and moment - up_since >= settle and moment >= next_try:
            if await _login_ready(onebot):
                try:
                    await run_once(bot, onebot=onebot)
                except Exception:  # noqa: BLE001 - 补收失败不能把后台任务弄死
                    log.exception("上线补收出错（不影响聊天）")
                ran = True
            else:
                # WS 连着但 QQ 可能还在二维码页面：等 30 秒再探
                next_try = clock() + 30.0
        await sleeper(poll)


async def _login_ready(onebot: Any) -> bool:
    """WS 连上不等于 QQ 登录成功：拿不到 self_id 就先不补。"""
    try:
        payload = await onebot.call("get_login_info", {}, timeout=8)
    except Exception as exc:  # noqa: BLE001
        log.info("上线补收：还没拿到登录信息（%s），稍后再试", exc)
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return bool(isinstance(data, dict) and data.get("user_id"))
