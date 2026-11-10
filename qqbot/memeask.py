"""不懂就问：没见过的短词先问群友，确认是梗再搜一次入库。

群里的梗靠复读统计；网络梗靠人。模型遇到真没见过的短词时，用 ``ask_meme``
问一句「这个「X」是梗还是什么东西呀？」。群友回「是梗」后，由 app 搜一次、
把解释写进 ``MemeBook`` 的 global 表（归一化 + 去重），以后所有会话都能用。

几条边界：

- 只问短词（1~12 字），不把整句、功能指令当梗问；
- 只问群聊，私聊里没人能回答；
- 一个群同时只挂一个问题；全局每天最多问 ``per_day`` 个，同群有冷却；
- 问过的词 7 天内不再问；没人回就让它过期，不追着问；
- 群友回「不是梗」→ 记进 rejected，以后不再收、不再问；
- 群友说是梗 → 就算网上搜不到也信群友（群内黑话/新梗网上未必有）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .memes import MAX_LEN, is_command, norm

log = logging.getLogger(__name__)

PENDING_TTL = 2 * 3600
ASKED_TTL = 7 * 86400
MAX_ANSWER_CHARS = 120

# 人话里的「不是梗」：必须先于肯定判断，否则「不是梗」会被里面的「是梗」抢走
_NEGATE = re.compile(
    r"不是梗|不算梗|没这个梗|没有这个梗|不是这个梗|不是吧|"
    r"普通词|人名|地名|角色名|不是[^，。！？\n]{0,6}梗"
)
_AFFIRM = re.compile(
    r"是梗|是个梗|这是梗|有梗|算梗|梗啊|梗吧|网络用语|流行语|黑话|缩写|"
    r"谐音|出自|来自|来源是|意思是|指的是|某个梗|一个梗"
)
_EXPLAIN = re.compile(r"是.{0,10}的梗|梗是|梗叫|梗出自")
# 「这是梗吗 / 什么梗」是在问，不是在答；别把它当成群友确认了
_QUESTION = re.compile(r"是不是|是梗吗|是梗么|算梗吗|什么梗|啥梗")
_BARE_MEME = re.compile(r"^\s*梗[呀啊哦吧呢。！!？?~～]*\s*$")
_SHORT_YES = re.compile(
    r"^\s*(是|对|嗯|是的|对啊|对的|算是|有|确实|真|真的是)[呀啊哦吧呢。！!？?~～]*\s*$"
)
_SHORT_NO = re.compile(r"^\s*(不是|不对|没有|没|不算)[呀啊哦吧呢。！!？?~～]*\s*$")


def _day_key(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


class MemeAsk:
    """群里的「问梗」小状态机：问了谁、等谁回、回的是什么。"""

    NAME = "ask_meme"

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        ttl_seconds: float = PENDING_TTL,
        cooldown_seconds: float = 600.0,
        per_day: int = 5,
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.ttl = max(60.0, float(ttl_seconds))
        self.cooldown = max(0.0, float(cooldown_seconds))
        self.per_day = max(0, int(per_day))
        # 这一轮刚问出去、还没拼进回复的问题：session -> question
        self._fresh: dict[str, str] = {}
        self._state = self._load()

    # ---------- 状态 ----------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "asks": [],
            "asked": {},
            "day": "",
            "day_count": 0,
            "last_by_group": {},
        }

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        base = self._empty()
        asks = [a for a in (raw.get("asks") or []) if isinstance(a, dict)]
        asked = {
            str(k): float(v)
            for k, v in (raw.get("asked") or {}).items()
            if str(k).strip()
        }
        last = {
            str(k): float(v)
            for k, v in (raw.get("last_by_group") or {}).items()
            if str(k).strip()
        }
        base.update(
            {
                "asks": asks,
                "asked": asked,
                "day": str(raw.get("day") or ""),
                "day_count": int(raw.get("day_count") or 0),
                "last_by_group": last,
            }
        )
        return base

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("问梗状态写入失败：%s", exc)

    def _purge(self, now: float) -> None:
        """清掉过期的等待、7 天前的「问过」、一天前的冷却记录。"""
        live: list[dict[str, Any]] = []
        for ask in self._state.get("asks") or []:
            if not isinstance(ask, dict):
                continue
            if now - float(ask.get("ts") or 0.0) >= self.ttl:
                continue
            live.append(ask)
        # 一个群只留最后一个（旧版本/异常状态里可能攒了多个）
        by_group: dict[str, dict[str, Any]] = {}
        for ask in live:
            by_group[str(ask.get("group") or "")] = ask
        self._state["asks"] = list(by_group.values())
        self._state["asked"] = {
            str(k): float(v)
            for k, v in (self._state.get("asked") or {}).items()
            if now - float(v) < ASKED_TTL
        }
        self._state["last_by_group"] = {
            str(k): float(v)
            for k, v in (self._state.get("last_by_group") or {}).items()
            if now - float(v) < 86400
        }

    def pending(self, group_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        now = time.time() if now is None else float(now)
        self._purge(now)
        gid = str(group_id or "")
        found = [a for a in self._state["asks"] if str(a.get("group")) == gid]
        return found[-1] if found else None

    def pending_note(self, group_id: str, *, now: float | None = None) -> str:
        ask = self.pending(group_id, now=now)
        if not ask:
            return ""
        return (
            f"（你刚问过群里「{ask.get('term')}」是不是梗，还在等回音；"
            "别再问一遍，也别自己猜。）"
        )

    def can_ask(
        self, group_id: str, term: str, *, now: float | None = None
    ) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        if not self.enabled:
            return False, "问梗关着"
        if self.per_day <= 0:
            return False, "今天不问"
        gid = str(group_id or "")
        if not gid:
            return False, "不知道是哪个群"
        if self.pending(gid, now=now):
            return False, "这个群还在等上一个回答"
        last = float((self._state.get("last_by_group") or {}).get(gid) or 0.0)
        if self.cooldown and now - last < self.cooldown:
            return False, "刚问过，先等等"
        same_day = self._state.get("day") == _day_key(now)
        used = int(self._state.get("day_count") or 0) if same_day else 0
        if self.per_day and used >= self.per_day:
            return False, "今天问得够多了"
        asked_at = float((self._state.get("asked") or {}).get(term) or 0.0)
        if asked_at and now - asked_at < ASKED_TTL:
            return False, "这个词问过了"
        return True, ""

    def ask(
        self,
        group_id: str,
        term: str,
        *,
        uid: Any = None,
        nick: str = "",
        now: float | None = None,
    ) -> str:
        """记下这个问题，返回要问出去的那句话；问不了返回空串。"""
        now = time.time() if now is None else float(now)
        gid = str(group_id or "")
        ok, why = self.can_ask(gid, term, now=now)
        if not ok:
            log.info("问梗跳过（%s）：%s", why, term)
            return ""
        question = f"这个「{term}」是梗还是什么东西呀？"
        self._state["asks"].append(
            {
                "group": gid,
                "term": term,
                "question": question,
                "uid": str(uid or ""),
                "nick": str(nick or "")[:20],
                "ts": now,
            }
        )
        self._state["asked"][term] = now
        self._state["last_by_group"][gid] = now
        same_day = self._state.get("day") == _day_key(now)
        self._state["day"] = _day_key(now)
        self._state["day_count"] = (int(self._state.get("day_count") or 0) if same_day else 0) + 1
        self._fresh[f"group:{gid}"] = question
        self._save()
        return question

    def take_fresh(self, session: str) -> str:
        """取走这一轮刚问出去的问题（拼回复用）；没有就空串。"""
        return self._fresh.pop(str(session or ""), "")

    def cancel(self, group_id: str) -> None:
        """这条问题没发出去（回复出错/潜水）：撤掉等待，别让后面的「是」误命中。"""
        gid = str(group_id or "")
        before = len(self._state.get("asks") or [])
        self._state["asks"] = [
            a
            for a in (self._state.get("asks") or [])
            if str(a.get("group")) != gid
        ]
        self._fresh.pop(f"group:{gid}", None)
        if len(self._state["asks"]) != before:
            self._save()

    def match(
        self,
        group_id: str,
        text: str,
        *,
        mentioned: bool = False,
        quoted_uid: Any = "",
        bot_qq: Any = "",
        speaker_uid: Any = "",
        now: float | None = None,
    ) -> tuple[str, str, str] | None:
        """判断这条消息是不是在回答我们刚问的梗。

        返回 ``(term, "yes"|"no", 原话)``；不是回答就 None。
        只有明说「是梗 / 不是梗 / 出自…」的，或者明确引用/ @ 了本鱼的短回答才算，
        群里随便一句「是」「不是」不会被吃进来。
        """
        now = time.time() if now is None else float(now)
        ask = self.pending(group_id, now=now)
        if not ask:
            return None
        body = str(text or "").strip()
        if not body or is_command(body):
            return None
        # 明确 @ / 引用本鱼；或者最初用这个词的人在两分钟内回了个短「是/不是」
        same_asker = bool(ask.get("uid")) and str(ask.get("uid")) == str(speaker_uid)
        fresh = now - float(ask.get("ts") or 0.0) <= 120
        direct = (
            bool(mentioned)
            or (
                bool(quoted_uid)
                and bool(bot_qq)
                and str(quoted_uid) == str(bot_qq)
            )
            or (same_asker and fresh)
        )
        if _QUESTION.search(body) and not _NEGATE.search(body):
            return None
        if _NEGATE.search(body):
            verdict = "no"
        elif _AFFIRM.search(body) or _EXPLAIN.search(body) or _BARE_MEME.match(body):
            verdict = "yes"
        elif direct and _SHORT_YES.match(body):
            verdict = "yes"
        elif direct and _SHORT_NO.match(body):
            verdict = "no"
        else:
            return None
        term = str(ask.get("term") or "")
        if not term:
            return None
        self._state["asks"] = [a for a in self._state["asks"] if a is not ask]
        self._fresh.pop(f"group:{str(group_id)}", None)
        self._save()
        return term, verdict, body[:MAX_ANSWER_CHARS]

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        self._purge(now)
        return {
            "enabled": self.enabled,
            "pending": len(self._state.get("asks") or []),
            "asked": len(self._state.get("asked") or {}),
            "day": self._state.get("day", ""),
            "today": int(self._state.get("day_count") or 0),
        }

    # ---------- 给模型用的工具 ----------

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": MemeAsk.NAME,
                    "description": (
                        "遇到你真的没见过的短词、缩写、黑话、梗，别猜、别硬解释，"
                        "用这个问群友一句「这是梗还是什么东西」。只问一个词；"
                        "群友回了「是梗」，系统会自己查一次并记进梗表，不用你搜。"
                        "普通词、人名、地名、已经知道的梗不要问。只有群聊能用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "term": {
                                "type": "string",
                                "description": "没看懂的短词，1~12 个字；不要传整句话",
                            }
                        },
                        "required": ["term"],
                    },
                },
            }
        ]

    async def run(
        self, name: str, arguments: str, turn: Any = None, **ctx: Any
    ) -> str | None:
        if turn is not None:
            ctx = turn.as_ctx()
        if name != self.NAME:
            return None
        if not self.enabled:
            return "（问梗关着，先自己判断或者说不确定。）"
        session = str(ctx.get("session") or "")
        if not session.startswith("group:"):
            return "（私聊里没人能回答这个；先别问，不知道就说不知道。）"
        gid = session.split(":", 1)[1]
        bot = ctx.get("bot")
        diving = getattr(bot, "_is_diving", None)
        if diving is not None and diving(gid):
            return "（这个群在潜水，先别说话。）"
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "（参数不是合法 JSON，没问出去）"
        if isinstance(args, str):
            args = {"term": args}
        if not isinstance(args, dict):
            args = {}
        raw = args.get("term") or args.get("text") or args.get("word") or ""
        term = norm(str(raw).strip().strip("「」『』"))
        if not term or len(term) > MAX_LEN:
            return "（只问 1~12 个字的短词；整句、普通词别问。）"
        if is_command(term):
            return "（像命令/功能词，不用问。）"

        memes = getattr(bot, "memes", None)
        if memes is not None and getattr(memes, "knows", None) and memes.knows(term):
            return "（这个你已经知道/记过了，不用问。）"
        trends = getattr(bot, "trends", None)
        if trends is not None:
            try:
                if trends.brief(term):
                    return "（热梗库里已经有这个，不用问。）"
            except Exception:  # noqa: BLE001 - 热梗库坏了不能挡住问梗
                log.debug("问梗：查热梗库失败", exc_info=True)
        aliases = getattr(bot, "aliases", None)
        if aliases is not None:
            try:
                if term in (aliases.all_aliases() or {}):
                    return "（外号表里已经有这个，不用问。）"
            except Exception:  # noqa: BLE001
                log.debug("问梗：查外号表失败", exc_info=True)

        now = time.time()
        question = self.ask(
            gid,
            term,
            uid=ctx.get("speaker_uid"),
            nick=str(ctx.get("speaker_nick") or ""),
            now=now,
        )
        if not question:
            return "（现在先不问；等群友回上一个，或者过一会儿再说。）"
        log.info(
            "问梗：群 %s 问「%s」（%s）",
            gid,
            term,
            str(ctx.get("speaker_nick") or "?")[:12],
        )
        return (
            f"（这轮就把这句话问出去：{question}"
            "问完等群友回，别自己猜、别自己搜；群友说是梗，系统会替你查一次并记进梗表。）"
        )
