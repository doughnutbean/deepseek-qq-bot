"""她自己回 QQ 空间里别人给的评论。

和「发说说」分开：发是主动的、要主人点头；回评论是被动的，只在有人评论她
**自己发过的说说**时动。公开可见，所以节奏比发说说更慢：

- 默认 30 分钟看一次最近 2 条说说；
- 一天最多回 3 条，两条之间至少隔 10 分钟，同一个人 6 小时内只回一次；
- 免打扰时段（默认 00:00-08:00）不回；
- 超过 48 小时的评论不回 —— 半夜打开开关不会把陈年评论全补一遍；
- 她自己的评论不回（不然自己回自己，没完没了）。

**第一次运行只记基线**：空间里已经存在的评论记成「看过」，一条都不回。
状态落在 `data/qzone_reply.json`，重启不会把回过的评论再回一遍。

    qr = QzoneReply(Path("data/qzone_reply.json"), cfg, qz=qz, compose=compose)
    result = await qr.tick()
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from .qzone_guard import parse_quiet

log = logging.getLogger(__name__)

MAX_SEEN = 400          # 去重表最多记这么多条评论，多了删最旧的
MAX_ATTEMPTS = 3        # 同一条评论最多试几次（发送失败才重试）
MAX_REPLY_CHARS = 80    # 回复正文上限：公开可见，别写长
KEEP_DAYS = 7           # 日计数留几天

# 这些原因以后再试也没用，直接标死
_TERMINAL = frozenset({"no_uin", "self", "ignored", "old"})


def _cfg(config: Any, name: str, default: Any) -> Any:
    """读配置；字段缺失或为 None 时用默认值（测试里可以只给几个字段）。"""
    value = getattr(config, name, None)
    if value is None:
        return default
    return value


def clean_reply(text: str) -> str:
    """把模型输出收拾成一条能发的评论：只留第一行、去掉「回复：」和引号。"""
    body = (text or "").strip()
    if not body:
        return ""
    line = body.splitlines()[0].strip()
    line = re.sub(r"^(?:回复|回|答|说)\s*[:：]\s*", "", line)
    line = line.strip("「」“”\"' 　")
    return line[:MAX_REPLY_CHARS]


class QzoneReply:
    def __init__(
        self,
        path: Path,
        config: Any,
        *,
        qz: Any,
        compose: Callable[[str], Awaitable[str]],
        memory: Any = None,
        session: str = "",
        owner_id: Any = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.config = config
        self.qz = qz
        self.compose = compose
        self.memory = memory
        self.session = session
        self.owner_id = owner_id
        self.notify = notify
        self.now = now
        self.quiet = parse_quiet(str(_cfg(config, "qzone_reply_quiet_hours", "")))

    # ---------- 闸门 ----------

    @property
    def enabled(self) -> bool:
        return bool(_cfg(self.config, "enable_qzone_reply", False))

    def awake(self, now: float | None = None) -> bool:
        """现在是不是可以回（免打扰时段之外）。"""
        if not self.quiet:
            return True
        moment = datetime.fromtimestamp(now if now is not None else self.now())
        minute = moment.hour * 60 + moment.minute
        for start, end in self.quiet:
            if start <= end:
                if start <= minute < end:
                    return False
            elif minute >= start or minute < end:  # 跨零点
                return False
        return True

    def _ignored(self) -> set[str]:
        raw = str(_cfg(self.config, "qzone_reply_ignore_uins", "") or "")
        return {part for part in re.split(r"[\s,，;；]+", raw) if part}

    # ---------- 状态 ----------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("initialized", False)
        for key in ("seen", "daily", "people"):
            value = raw.get(key)
            raw[key] = dict(value) if isinstance(value, dict) else {}
        return raw

    def _save(self, state: dict[str, Any]) -> None:
        self._prune(state, self.now())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("回评论状态写不进去：%s", exc)

    @staticmethod
    def _prune(state: dict[str, Any], now: float) -> None:
        seen = state.get("seen") or {}
        if len(seen) > MAX_SEEN:
            keep = sorted(
                seen.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0)
            )[-MAX_SEEN:]
            state["seen"] = dict(keep)
        cutoff = (datetime.fromtimestamp(now) - timedelta(days=KEEP_DAYS)).strftime(
            "%Y-%m-%d"
        )
        state["daily"] = {
            day: count
            for day, count in (state.get("daily") or {}).items()
            if str(day) >= cutoff
        }

    @staticmethod
    def _key(tid: str, comment: dict[str, Any]) -> str:
        cid = str(comment.get("id") or "")
        if not cid:
            cid = f"{comment.get('uin') or ''}@{comment.get('ts') or ''}"
        return f"{tid}:{cid}"

    @staticmethod
    def _today(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    # ---------- 主循环 ----------

    async def tick(self) -> dict[str, Any]:
        """看一遍最近几条说说：新评论就回，回过的跳过。返回这次做了什么。"""
        if not self.enabled:
            return {"status": "off"}
        now = self.now()
        if not self.awake(now):
            return {"status": "quiet"}

        state = self._load()
        max_posts = max(1, int(_cfg(self.config, "qzone_reply_max_posts", 2)))
        max_age = max(0, int(_cfg(self.config, "qzone_reply_max_age_hours", 48))) * 3600

        try:
            feed = await self.qz.feed_list(max_posts)
        except Exception as exc:  # noqa: BLE001 - 读不到就下轮再看
            log.warning("回评论：读说说列表失败（%s）", type(exc).__name__)
            return {"status": "feed_failed"}
        if not feed.get("ok"):
            return {"status": "feed_failed", "code": feed.get("code")}

        posts = [p for p in (feed.get("items") or []) if str(p.get("tid") or "")]
        if max_age:
            posts = [
                p
                for p in posts
                if not p.get("created") or float(p["created"]) >= now - max_age
            ]

        first_run = not state.get("initialized")
        discovered = 0
        read_ok = False
        for post in posts:
            tid = str(post["tid"])
            try:
                detail = await self.qz.comments(tid)
            except Exception as exc:  # noqa: BLE001
                log.warning("回评论：读评论失败 tid=%s（%s）", tid, type(exc).__name__)
                continue
            if not detail.get("ok"):
                continue
            read_ok = True
            for comment in detail.get("items") or []:
                key = self._key(tid, comment)
                if key in state["seen"]:
                    continue
                state["seen"][key] = {
                    "tid": tid,
                    "uin": str(comment.get("uin") or ""),
                    "name": str(comment.get("name") or ""),
                    "text": str(comment.get("text") or "")[:200],
                    "post": str(post.get("content") or "")[:200],
                    "ts": int(comment.get("ts") or now),
                    "tries": 0,
                    # 第一次运行看到的都是历史评论：记下，但一条都不回
                    "dead": bool(first_run),
                    "baseline": bool(first_run),
                }
                discovered += 1

        if first_run:
            # 一条评论都没读到时先别定基线：不然下次会把所有老评论当成新的补回。
            if posts and not read_ok:
                return {"status": "baseline_failed"}
            state["initialized"] = True
            state["since"] = int(now)
            self._save(state)
            log.info(
                "回评论：首次运行，先记下 %d 条已存在的评论，不回复（%s）",
                discovered,
                self.path.name,
            )
            return {"status": "baseline", "count": discovered}

        pending = [
            entry
            for entry in state["seen"].values()
            if isinstance(entry, dict) and not entry.get("dead") and not entry.get("replied")
        ]
        if not pending:
            if discovered:
                self._save(state)
            return {"status": "idle", "discovered": discovered}
        pending.sort(key=lambda entry: float(entry.get("ts") or 0))

        replied: list[dict[str, Any]] = []
        for entry in pending:
            reason = self._skip_reason(state, entry, now)
            if reason:
                entry["last_skip"] = reason
                if reason in _TERMINAL:
                    entry["dead"] = True
                continue

            text = await self._generate(entry)
            if not text:
                entry["dead"] = True
                continue

            try:
                result = await self.qz.reply(
                    str(entry.get("tid") or ""),
                    text,
                    name=str(entry.get("name") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - 异常也按失败算，别丢状态
                result = {"ok": False, "code": None, "raw": type(exc).__name__}
            if not result.get("ok"):
                entry["tries"] = int(entry.get("tries") or 0) + 1
                entry["error"] = str(result.get("code"))
                if entry["tries"] >= MAX_ATTEMPTS:
                    entry["dead"] = True
                log.warning(
                    "回评论：发送失败 tid=%s code=%s（第 %d 次）",
                    entry.get("tid"),
                    result.get("code"),
                    entry["tries"],
                )
                self._save(state)
                break

            entry["replied"] = True
            entry["dead"] = True
            state["last_at"] = now
            daily = state["daily"]
            today = self._today(now)
            daily[today] = int(daily.get(today) or 0) + 1
            state["people"][str(entry.get("uin") or "")] = now

            log.info(
                "回评论：回了 %s 的评论：%s",
                entry.get("name") or entry.get("uin"),
                text[:40],
            )
            self._remember(entry, text)
            replied.append(
                {
                    "tid": entry.get("tid"),
                    "name": entry.get("name"),
                    "comment": entry.get("text"),
                    "text": text,
                }
            )
            self._save(state)
            if self.notify is not None and bool(
                _cfg(self.config, "qzone_reply_notify_owner", False)
            ):
                try:
                    await self.notify(self.notify_text(entry, text))
                except Exception:  # noqa: BLE001 - 通知失败不影响回复
                    log.warning("回评论：通知主人失败", exc_info=True)

        self._save(state)
        return {
            "status": "ok" if replied else "idle",
            "replied": replied,
            "discovered": discovered,
            "pending": len(pending),
        }

    # ---------- 单条评论 ----------

    def _skip_reason(
        self, state: dict[str, Any], entry: dict[str, Any], now: float
    ) -> str | None:
        uin = str(entry.get("uin") or "")
        if not uin:
            return "no_uin"
        if uin == str(_cfg(self.config, "bot_qq", "")):
            return "self"
        if uin in self._ignored():
            return "ignored"

        max_age = max(0, int(_cfg(self.config, "qzone_reply_max_age_hours", 48))) * 3600
        ts = float(entry.get("ts") or 0)
        if max_age and ts and ts < now - max_age:
            return "old"

        per_person = (
            max(0, int(_cfg(self.config, "qzone_reply_per_person_hours", 6))) * 3600
        )
        last_person = float(state["people"].get(uin) or 0)
        if per_person and last_person and now - last_person < per_person:
            return "person"

        gap = max(0, int(_cfg(self.config, "qzone_reply_cooldown_minutes", 10))) * 60
        last_any = float(state.get("last_at") or 0)
        if gap and last_any and now - last_any < gap:
            return "gap"

        limit = int(_cfg(self.config, "qzone_reply_daily_limit", 3))
        if limit and int(state["daily"].get(self._today(now)) or 0) >= limit:
            return "daily"
        return None

    async def _generate(self, entry: dict[str, Any]) -> str:
        prompt = self.prompt(
            str(entry.get("post") or ""),
            str(entry.get("name") or ""),
            str(entry.get("text") or ""),
        )
        try:
            raw = await self.compose(prompt)
        except Exception:  # noqa: BLE001 - 生成失败就当这次不回
            log.warning("回评论：生成回复失败", exc_info=True)
            return ""
        return clean_reply(raw)

    @staticmethod
    def prompt(post: str, name: str, text: str) -> str:
        lines = ["现在不是在群里回消息，是在 QQ 空间回别人给你那条说说留下的评论。"]
        if post:
            lines.append(f"你发的那条说说：{post[:160]}")
        lines.append(f"{name or '有人'} 评论：{text[:160]}")
        lines.append(
            "用你自己的口吻回一句，40 字以内，像随口接话；别客套、别解释、"
            "不要以「回复」开头、不要 @ 人、不要带表情包标记。只输出这一句。"
        )
        return "\n".join(lines)

    @staticmethod
    def notify_text(entry: dict[str, Any], reply: str) -> str:
        name = entry.get("name") or "有人"
        return (
            f"空间有人评论了：{name}：{str(entry.get('text') or '')[:60]}\n"
            f"本鱼回了：{reply}"
        )

    def _remember(self, entry: dict[str, Any], reply: str) -> None:
        if self.memory is None or not self.session:
            return
        name = entry.get("name") or "有人"
        note = (
            f"（我在空间回了{name}的评论："
            f"{str(entry.get('text') or '')[:40]}｜我：{reply}）"
        )
        try:
            self.memory.add_turn(
                self.session, "assistant", note, owner_id=self.owner_id
            )
        except Exception:  # noqa: BLE001 - 记忆失败不影响回复
            log.warning("回评论：没写进记忆", exc_info=True)
