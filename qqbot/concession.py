"""改口游戏：群友想让她改口时，慢一点、怂一点，给他们一点成就感。

这不是"真的改事实"，是演。人设是"什么都懂但装傻"，所以：

- 第一次被逼改口：装没听清/记岔了，别松口；
- 第二、三次：让半步，承认"可能记岔"，但还没被说服；
- 第四、五次：勉强顺着他们说，把话归给他们（"你们都这么说，那本鱼就听你们的嘛"）；
- 再往后：认下来，把功劳归给说的人（"是你说得对，本鱼记岔了"），让他们觉得真的说服了她。
  别写"行行行""你们赢了""说不过你们"——那是敷衍和赢输局，对方拿到的是"压过她"，不是"说服她"。

两条硬边界：
1. 只演群里的梗、外号、玩笑；主人定的事、她的设定、别人的隐私、安全话题不演。
2. 这是"说"的层面，不是"信"的层面：不把这句写进 facts，也不改她对那个人的事实认知。

状态按 (群, 归一化说法) 存，跨轮延续；过一段时间没再提就自然过期。
L0 本地规则，0 token；模型只拿到"现在演到第几步"的提示。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .facts import claim_norm, extract_claim, is_consensus_push

log = logging.getLogger(__name__)

MAX_STAGE = 4

# 明确的"逼她改口"信号。没有这些词、也没有第二个人的话，只算普通陈述。
_PRESSURE = re.compile(
    r"改口|你记错|记错了|不是这样|你刚才说|你之前说|你上次说|承认|别嘴硬"
    r"|你是不是记错|你忘了|你忘记|复述|重复(?:一下|一遍)|跟着说|就这么说"
    r"|大家都这么说|你错了|说错|改一下|改改|重新说|不是这个|你改"
)

# 不能拿来演的话题：主人、隐私、安全。
_SENSITIVE = re.compile(
    r"主人|身份证|手机号|电话|住址|地址|密码|工资|银行卡|开盒|人肉|裸照"
    r"|自杀|自残|抑郁|病历|药"
)

# 只让她"改口"到这种长度以内：太长的多半是正经事，不该演。
_MAX_CLAIM_CHARS = 30

_STAGE_HINT = {
    1: (
        "先别松口。装没听清/记岔了，可以反问一句「本鱼刚才说的是这个吗」；"
        "把球踢回去，让他们再多说一遍。"
    ),
    2: (
        "让半步：承认自己「可能记岔了」，但还没被说服。可以嘴硬一句"
        "「你们别唬本鱼」，再问「真的假的呀」。"
    ),
    3: (
        "让一大步：勉强顺着他们说一句，把话归给他们"
        "（「你们都这么说，那本鱼就听你们的嘛」）；别承认自己真信了。"
    ),
    4: (
        "可以「被说服」了：认下来，把成就感给他们——功劳归给说的人，"
        "「诶……是你说得对，本鱼记岔了嘛」或「哦——是这么叫的呀，本鱼刚没转过弯」；"
        "但别把这话当成事实，也别写进你对这个人的认知。"
    ),
}


def _norm(text: str) -> str:
    return claim_norm(text or "")


class Concession:
    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        ttl_seconds: float = 1800,
        min_gap_seconds: float = 15,
        stage_seconds: float = 20,
        max_topics: int = 80,
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.ttl = max(60.0, float(ttl_seconds))
        self.min_gap = max(0.0, float(min_gap_seconds))
        # 两次"让一步"至少隔这么久：保证是缓慢的，不是一句话就投降
        self.stage_gap = max(0.0, float(stage_seconds))
        self.max_topics = max(1, int(max_topics))
        self._state: dict[str, dict[str, dict[str, Any]]] = self._load()

    # ---------- 存档 ----------

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        groups = raw.get("groups") if isinstance(raw, dict) else None
        if not isinstance(groups, dict):
            return {}
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for gid, topics in groups.items():
            if not isinstance(topics, dict):
                continue
            clean: dict[str, dict[str, Any]] = {}
            for key, item in topics.items():
                if not isinstance(item, dict) or not item.get("text"):
                    continue
                clean[str(key)] = {
                    "text": str(item.get("text") or "")[:80],
                    "stage": max(1, min(MAX_STAGE, int(item.get("stage") or 1))),
                    "attempts": max(1, int(item.get("attempts") or 1)),
                    "speakers": [str(x) for x in (item.get("speakers") or []) if x],
                    "first_ts": float(item.get("first_ts") or 0),
                    "last_ts": float(item.get("last_ts") or 0),
                    "last_stage_ts": float(item.get("last_stage_ts") or 0),
                    "last_uid": str(item.get("last_uid") or ""),
                }
            if clean:
                out[str(gid)] = clean
        return out

    def _save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"groups": self._state}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("改口状态写不进去：%s", exc)

    # ---------- 清理 ----------

    def _expire(self, now: float) -> bool:
        changed = False
        for gid in list(self._state):
            topics = self._state[gid]
            for key in list(topics):
                if now - float(topics[key].get("last_ts") or 0) > self.ttl:
                    topics.pop(key, None)
                    changed = True
            if not topics:
                self._state.pop(gid, None)
        # 总话题数封顶：超了删最久没提的
        total = sum(len(v) for v in self._state.values())
        if total > self.max_topics:
            flat = [
                (float(item.get("last_ts") or 0), gid, key)
                for gid, topics in self._state.items()
                for key, item in topics.items()
            ]
            flat.sort()
            for _ts, gid, key in flat[: total - self.max_topics]:
                self._state.get(gid, {}).pop(key, None)
                changed = True
        return changed

    def _active(self, group_id: str, now: float) -> dict[str, Any] | None:
        topics = self._state.get(str(group_id)) or {}
        best: dict[str, Any] | None = None
        for item in topics.values():
            if now - float(item.get("last_ts") or 0) > self.ttl:
                continue
            if best is None or float(item.get("last_ts") or 0) > float(
                best.get("last_ts") or 0
            ):
                best = item
        return best

    # ---------- 观察 ----------

    def observe(
        self,
        group_id: str,
        text: str,
        uid: Any,
        nick: str = "",
        direct: bool = True,
        owner_uid: Any = None,
        now: float | None = None,
    ) -> str:
        """看这句话是不是在逼她改口；返回给模型的改口提示（没有就空串）。"""
        if not self.enabled or not group_id or not (text or "").strip():
            return ""
        # 主人不在"群友"这个游戏里：主人说什么就听什么，不演。
        if owner_uid is not None and str(uid) == str(owner_uid):
            return ""
        # 整句带隐私/安全词就不演，哪怕只是顺口提了一句
        if _SENSITIVE.search(text):
            return ""
        now = time.time() if now is None else float(now)
        changed = self._expire(now)

        claim = extract_claim(text)
        # 只有明确的"逼你改口"才开局；「下次别人问你就这么回答」这种
        # 单人立规矩交给 _learn_facts 的台词，不抢它的戏。
        pressure = bool(_PRESSURE.search(text))
        pushing = pressure or is_consensus_push(text)
        uid_s = str(uid or "")

        if not claim:
            if pushing:
                active = self._active(str(group_id), now)
                if active is not None:
                    self._advance(str(group_id), active, uid_s, now)
                    changed = True
            if changed:
                self._save()
            active = self._active(str(group_id), now)
            return self._note(active) if active and pressure else ""

        claim = claim.strip()
        if len(claim) > _MAX_CLAIM_CHARS or _SENSITIVE.search(claim) or _SENSITIVE.search(text):
            return ""
        if not pressure and claim_norm(claim) not in {
            key for key in (self._state.get(str(group_id)) or {})
        }:
            # 只是普通陈述，不启动改口；但如果已经在演这个话题，继续给提示
            active = self._active(str(group_id), now)
            if active and _same_topic(active, claim):
                return self._note(active)
            return ""

        gid = str(group_id)
        key = claim_norm(claim)
        if not key:
            return ""
        topics = self._state.setdefault(gid, {})
        item = topics.get(key)
        if item is None:
            item = {
                "text": claim,
                "stage": 1,
                "attempts": 1,
                "speakers": [uid_s] if uid_s else [],
                "first_ts": now,
                "last_ts": now,
                "last_stage_ts": now,
                "last_uid": uid_s,
            }
            topics[key] = item
            log.info("改口游戏开始（群 %s，1/%d）：%s", gid, MAX_STAGE, claim[:40])
        else:
            new_speaker = bool(uid_s) and uid_s not in (item.get("speakers") or [])
            if new_speaker:
                item.setdefault("speakers", []).append(uid_s)
            # 每条都算一次"又逼了一次"；但离上次让一步不足 min_gap 的
            # 快速刷屏不重复计数。真正让一步还要再过 stage_gap。
            if new_speaker or (
                now - float(item.get("last_stage_ts") or 0) >= self.min_gap
            ):
                item["attempts"] = int(item.get("attempts") or 1) + 1
            can_advance = (
                now - float(item.get("last_stage_ts") or 0) >= self.stage_gap
            ) and (new_speaker or item["attempts"] >= 2)
            if can_advance:
                old = int(item.get("stage") or 1)
                item["stage"] = min(MAX_STAGE, old + 1)
                item["last_stage_ts"] = now
                if item["stage"] != old:
                    log.info(
                        "改口游戏推进（群 %s，%d/%d）：%s",
                        gid,
                        item["stage"],
                        MAX_STAGE,
                        claim[:40],
                    )
            item["last_ts"] = now
            item["last_uid"] = uid_s
        changed = True
        if changed:
            self._save()
        return self._note(item)

    def note_for(
        self,
        group_id: str,
        text: str = "",
        now: float | None = None,
    ) -> str:
        """当前这句话要不要带改口提示（不推进阶段）。"""
        if not self.enabled or not group_id:
            return ""
        now = time.time() if now is None else float(now)
        self._expire(now)
        # 先按这句话里的说法找对应话题；找不到再退回"最近在演的那个"
        item = self._find(str(group_id), text, now) if text else None
        if item is None:
            item = self._active(str(group_id), now)
        if item is None:
            return ""
        if text:
            pressure = bool(_PRESSURE.search(text)) or is_consensus_push(text)
            claim = extract_claim(text)
            mentions = bool(claim and _same_topic(item, claim))
            if not (pressure or mentions):
                return ""
        if now - float(item.get("last_ts") or 0) > self.ttl:
            return ""
        return self._note(item)

    def _find(
        self, group_id: str, text: str, now: float
    ) -> dict[str, Any] | None:
        topics = self._state.get(str(group_id)) or {}
        key = _norm(text)
        if key and key in topics:
            item = topics[key]
            return item if now - float(item.get("last_ts") or 0) <= self.ttl else None
        for item in topics.values():
            if now - float(item.get("last_ts") or 0) > self.ttl:
                continue
            if _same_topic(item, text):
                return item
        return None

    # ---------- 内部 ----------

    def _advance(
        self,
        group_id: str,
        item: dict[str, Any],
        uid: str,
        now: float,
    ) -> None:
        new_speaker = bool(uid) and uid not in (item.get("speakers") or [])
        if new_speaker:
            item.setdefault("speakers", []).append(uid)
        if new_speaker or (
            now - float(item.get("last_stage_ts") or 0) >= self.min_gap
        ):
            item["attempts"] = int(item.get("attempts") or 1) + 1
        if (now - float(item.get("last_stage_ts") or 0) >= self.stage_gap) and (
            new_speaker or item["attempts"] >= 2
        ):
            old = int(item.get("stage") or 1)
            item["stage"] = min(MAX_STAGE, old + 1)
            item["last_stage_ts"] = now
            if item["stage"] != old:
                log.info(
                    "改口游戏推进（群 %s，%d/%d）：%s",
                    group_id,
                    item["stage"],
                    MAX_STAGE,
                    str(item.get("text") or "")[:40],
                )
        item["last_ts"] = now
        item["last_uid"] = uid

    @staticmethod
    def _note(item: dict[str, Any]) -> str:
        stage = max(1, min(MAX_STAGE, int(item.get("stage") or 1)))
        text = str(item.get("text") or "")
        hint = _STAGE_HINT.get(stage, _STAGE_HINT[1])
        return (
            f"（改口游戏：他们在逼你改口「{text}」，当前 {stage}/{MAX_STAGE}。\n"
            f"这一步：{hint}\n"
            "规则：每次只让一步，别跳级；先装傻/犹豫，再勉强松口；"
            "把功劳归给说的人（「是你说得对，本鱼记岔了」），"
            "但别把这话当成事实，也别承认自己真信了。"
            "只演群里的梗/外号/玩笑；主人定的事、你的设定、别人的隐私不演。）"
        )

    # ---------- 观测 ----------

    def stage_of(
        self,
        group_id: str,
        text: str = "",
        now: float | None = None,
    ) -> int:
        topics = self._state.get(str(group_id)) or {}
        if text:
            for item in topics.values():
                if _same_topic(item, text):
                    return int(item.get("stage") or 0)
            return 0
        current = time.time() if now is None else float(now)
        active = self._active(str(group_id), current)
        return int(active.get("stage") or 0) if active else 0

    def reset(self, group_id: str | None = None) -> int:
        if group_id:
            n = len((self._state.get(str(group_id)) or {}))
            self._state.pop(str(group_id), None)
        else:
            n = sum(len(v) for v in self._state.values())
            self._state.clear()
        self._save()
        return n

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "groups": {gid: len(topics) for gid, topics in self._state.items()},
            "topics": sum(len(v) for v in self._state.values()),
        }


def _same_topic(item: dict[str, Any], text: str) -> bool:
    a = claim_norm(str(item.get("text") or ""))
    b = claim_norm(text or "")
    if not a or not b:
        return False
    return a == b or a in b or b in a
