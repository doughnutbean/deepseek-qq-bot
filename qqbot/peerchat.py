"""同类聊天（AI 对 AI）：先看话题，再兜底。

2026-10-27 上午，群 123456789 里另一只 DeepSeek bot 靠一句「这话说得也太像 AI 了」
把她认了出来，随后两只 bot 认了亲。主人想让她能见见不同人设的同类、也能聊两句，
但不许两只模型「左右脚互蹬」空烧 token。

分寸（主人 2026-10-27 定的）：

- **松**：接不接主要看话题有没有往前走（有新信息 / 在提问 / 不是纯捧），
  不按固定条数卡死；拿不准就接。
- **紧**：兜底一定要有——一场最多 12 条（10~15 之间可调）、同一只 10 分钟 12 条、
  每群每小时 15 条、一场 ¥0.07；到顶就收，收场的由头是「给主人省 token /
  自己多攒两碗白饭」，不宣布自己在切状态。
- **早退**：聊过两句后对方开始纯捧/复读（话不投机），不用等上限，直接收场。
- **人不在不接**：最近 10 分钟没有真人说话，同类说什么都不理，
  防半夜两只 bot 自己刷。

只对登记过的【AI】（`speakers.json` 的 `ais`，QQ 上用「加AI 123456」）生效；
没登记的号按普通群友处理，所以「先登记再放进来」是使用前提。

主动搭话（2026-10-30 加）
------------------------
以前只有「同类说话、她接」；现在她也能自己先开一句（`fish_chat` 工具）。
主动开的口子更小：真人不在场不找、每群每小时最多 3 次、两次之间隔 3 分钟、
同一条鱼 10 分钟最多找一次，而且和回话共用「每群每小时 15 条 / 一场 12 条 /
一场 ¥0.07」的兜底 —— 不然她自己就能把两只 bot 的对话刷起来。
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^【[^】]*】")
_QUOTE_RE = re.compile(r"（引用了前一条：[^）]*）")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

# 纯捧 / 纯应：这种话不接，接了就是两只模型互相点头。
# 允许叠加（「确实哈哈」「是啊对的」），所以用 + 而不是单次匹配。
_AGREE_RE = re.compile(
    r"^(?:(?:确实|是啊|是的|是呀|对|对的|嗯+|哦+|喔+|哈哈+|笑死|有道理|合理|"
    r"可以|好|好的|行|懂了|收到|谢谢|厉害|牛|6+|太对了|是这样的|没错))+$"
)
# 在提问 / 在推进：这种话要接，哪怕字少
_QUESTION_RE = re.compile(
    r"[?？]|吗|呢|什么|怎么|为什么|为啥|哪|谁|是不是|要不要|你觉得|你猜|"
    r"多少|几个|能不能|有没有|如何"
)

# 收场由头：固定几句，0 token，也不用模型现编（省下来的正是它说的 token）
_EXIT_LINES = (
    "不聊啦，再聊下去主人要心疼 token 了，本鱼还得攒白饭呢。",
    "先到这儿吧，token 比白饭贵多了，本鱼得省着点花。",
    "本鱼先撤啦，主人看见账单要皱眉的——白饭还没攒够呢。",
)


@dataclass(frozen=True)
class Decision:
    """这一条同类的话接不接。"""

    allow: bool
    reason: str = ""
    exit_line: str = ""


@dataclass
class _Session:
    """一场同类聊天：约 10 分钟没动静就算散场，预算按场算。"""

    spent: float = 0.0
    replies: int = 0
    last: float = 0.0


def _norm(text: str) -> str:
    """去掉标签、昵称前缀、引用尾巴和标点，只留能比的字。"""
    body = _TAG_RE.sub("", (text or "").strip())
    body = _QUOTE_RE.sub("", body)
    for sep in ("：", ":"):
        idx = body.find(sep)
        if 0 <= idx <= 20:
            rest = body[idx + 1 :].strip()
            if rest:
                body = rest
            break
    return _PUNCT_RE.sub("", body).lower()


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _keywords(text: str) -> list[str]:
    """借记忆库的分词抽关键词；抽不动就退化成字面重合度。"""
    try:
        from .memory import keywords

        return [k for k in keywords(text) if len(k) >= 2]
    except Exception:  # noqa: BLE001 - 分词失败不能挡聊天
        return []


def topic_moves(text: str, recent: list[str] | None = None) -> str:
    """这个话题还有没有往前走。

    返回 `advance`（接）/ `stall`（原地打转，不接）/ `empty`（没内容）。

    判据很土但够用：纯捧 → stall；带问号或疑问词 → advance；
    出现最近几句里没提过的新词 → advance；跟最近几句的字面重合 ≥72% → stall。
    其余一律 advance —— 主人说了，不要卡那么死。
    """
    body = _norm(text)
    if len(body) < 2:
        return "empty"
    if _AGREE_RE.fullmatch(body):
        return "stall"
    refs = [_norm(r)[:200] for r in (recent or []) if r][-4:]
    ref_text = "".join(refs)
    ref_grams: set[str] = set()
    for r in refs:
        ref_grams |= _bigrams(r)
    if _QUESTION_RE.search(text or ""):
        return "advance"
    for kw in _keywords(body):
        if kw not in ref_text:
            return "advance"
    grams = _bigrams(body)
    if not grams:
        return "advance"
    seen = len(grams & ref_grams) / len(grams)
    if seen >= 0.72:
        return "stall"
    return "advance"


class PeerChat:
    """同类聊天的闸：话题判断 + 硬兜底。

    只记内存，重启就清零。兜底是「防意外」的，不是账本：
    真正的花费在 `usage.db`，这里只保证一对 bot 不会停不下来。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        pair_max: int = 12,
        pair_window: int = 600,
        group_max: int = 15,
        group_window: int = 3600,
        session_max: int = 12,
        session_budget: float = 0.07,
        session_gap: int = 600,
        cost_per_reply: float = 0.0045,
        human_idle: int = 600,
        exit_cooldown: int = 1800,
        outreach_max: int = 3,
        outreach_window: int = 3600,
        outreach_gap: int = 180,
        outreach_peer_gap: int = 600,
        clock: Callable[[], float] = time.time,
        picker: Callable[[tuple[str, ...]], str] = random.choice,
    ) -> None:
        self.enabled = bool(enabled)
        self.pair_max = max(0, int(pair_max))
        self.pair_window = max(0, int(pair_window))
        self.group_max = max(0, int(group_max))
        self.group_window = max(0, int(group_window))
        self.session_max = max(0, int(session_max))
        self.session_budget = max(0.0, float(session_budget))
        self.session_gap = max(0, int(session_gap))
        self.cost_per_reply = max(0.0, float(cost_per_reply))
        self.human_idle = max(0, int(human_idle))
        self.exit_cooldown = max(0, int(exit_cooldown))
        self.outreach_max = max(0, int(outreach_max))
        self.outreach_window = max(0, int(outreach_window))
        self.outreach_gap = max(0, int(outreach_gap))
        self.outreach_peer_gap = max(0, int(outreach_peer_gap))
        self.clock = clock
        self.picker = picker

        self._human_at: dict[str, float] = {}
        self._pair_hits: dict[tuple[str, str], list[float]] = {}
        self._group_hits: dict[str, list[float]] = {}
        self._sessions: dict[str, _Session] = {}
        self._exit_at: dict[str, float] = {}
        # 主动搭话单独记账：和「回话」共用每群/每场兜底，但不吃「同一只同类」那条。
        self._outreach_hits: dict[str, list[float]] = {}
        self._outreach_at: dict[str, float] = {}
        self._peer_outreach_at: dict[tuple[str, str], float] = {}

    # ---------- 外部只需要这两件事 ----------

    def observe_human(self, group_id: Any, now: float | None = None) -> None:
        """真人说了一句话：同类聊天得有人在旁边听着。"""
        if group_id:
            self._human_at[str(group_id)] = now if now is not None else self.clock()

    def allow(
        self,
        group_id: Any,
        peer_uid: Any,
        text: str,
        recent: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> Decision:
        """这条同类的话接不接；不接时可能带回一句收场由头。"""
        if not self.enabled:
            return Decision(True, "同类聊天闸关着")
        now = now if now is not None else self.clock()
        gid = str(group_id or "")
        uid = str(peer_uid or "")

        # 1) 真人不在场：不接（免得起半夜两只 bot 自己聊到天亮）
        if self.human_idle and now - self._human_at.get(gid, 0.0) > self.human_idle:
            return Decision(False, "最近没有真人说话")

        # 2) 话题：原地打转就不接，这是「松」的那一半
        sess = self._session(gid, now)
        verdict = topic_moves(text, recent)
        if verdict == "stall":
            # 已经聊过两句、对方开始纯捧/复读：话不投机，提前收场
            if sess.replies >= 2:
                return self._stop(gid, sess, now, "话不投机")
            return Decision(False, "话题在原地打转")
        if verdict != "advance":
            return Decision(False, "这条没内容")

        # 3) 硬兜底（顺序：同一只 → 本场长度 → 本群 → 预算）
        pair = self._recent(self._pair_hits.get((gid, uid), []), self.pair_window, now)
        group = self._recent(self._group_hits.get(gid, []), self.group_window, now)

        if self.pair_max and len(pair) >= self.pair_max:
            return self._stop(gid, sess, now, "同一只同类太密")
        if self.session_max and sess.replies >= self.session_max:
            return self._stop(gid, sess, now, "这场聊够了")
        if self.group_max and len(group) >= self.group_max:
            return self._stop(gid, sess, now, "本群同类一小时到顶")
        if (
            self.session_budget
            and sess.spent + self.cost_per_reply > self.session_budget + 1e-9
        ):
            return self._stop(gid, sess, now, "本场预算到顶")

        # 4) 放行，记账
        pair.append(now)
        group.append(now)
        self._pair_hits[(gid, uid)] = pair
        self._group_hits[gid] = group
        sess.replies += 1
        sess.spent += self.cost_per_reply
        sess.last = now
        self._sessions[gid] = sess
        return Decision(True, "话题在往前走")

    def outreach(
        self,
        group_id: Any,
        peer_uid: Any,
        *,
        now: float | None = None,
    ) -> Decision:
        """她主动去找一条同类说话：能不能开这个口。

        比 `allow()` 更紧 —— 回话是对方先开口，主动搭话是她自己点火：

        - 最近 10 分钟没有真人说话 → 不开（防半夜两只 bot 自己聊起来）；
        - 本群 1 小时最多 3 次、两次之间至少 3 分钟；
        - 同一条鱼 10 分钟最多找一次；
        - 每群每小时的 15 条、一场 12 条、一场 ¥0.07 的兜底和回话共用。

        放行时按一条回复记账（`session.replies/spent`），所以主动找人也会
        算进那一场的对话长度和预算。
        """
        if not self.enabled:
            return Decision(True, "同类聊天闸关着")
        now = now if now is not None else self.clock()
        gid = str(group_id or "")
        uid = str(peer_uid or "")

        # 1) 真人不在场：不主动开新话题
        if self.human_idle and now - self._human_at.get(gid, 0.0) > self.human_idle:
            return Decision(False, "最近没有真人说话，不主动找同类")

        # 2) 主动搭话自己的频率闸
        out = self._recent(self._outreach_hits.get(gid, []), self.outreach_window, now)
        if self.outreach_max and len(out) >= self.outreach_max:
            return Decision(False, "本群主动找同类太频繁")
        if self.outreach_gap and now - self._outreach_at.get(gid, 0.0) < self.outreach_gap:
            return Decision(False, "刚主动找过同类，缓一缓")
        if (
            self.outreach_peer_gap
            and now - self._peer_outreach_at.get((gid, uid), 0.0)
            < self.outreach_peer_gap
        ):
            return Decision(False, "刚找过这条鱼")

        # 3) 和回话共用的硬兜底
        group = self._recent(self._group_hits.get(gid, []), self.group_window, now)
        if self.group_max and len(group) >= self.group_max:
            return Decision(False, "本群同类一小时到顶")
        sess = self._session(gid, now)
        if self.session_max and sess.replies >= self.session_max:
            return Decision(False, "这场聊够了")
        if (
            self.session_budget
            and sess.spent + self.cost_per_reply > self.session_budget + 1e-9
        ):
            return Decision(False, "本场预算到顶")

        # 4) 放行，记账（主动搭话也算这一场的一条）
        out.append(now)
        self._outreach_hits[gid] = out
        self._outreach_at[gid] = now
        self._peer_outreach_at[(gid, uid)] = now
        group.append(now)
        self._group_hits[gid] = group
        sess.replies += 1
        sess.spent += self.cost_per_reply
        sess.last = now
        self._sessions[gid] = sess
        return Decision(True, "主动搭话")

    def outreach_many(
        self,
        group_id: Any,
        peer_uids: list[Any] | tuple[Any, ...],
        *,
        now: float | None = None,
    ) -> tuple[Decision, list[str]]:
        """主人点名式主动搭话：一条消息 @ 多条同类。

        和 `outreach()` 的区别：这是一次动作，不是多次搭话 ——
        每小时/每场/预算只记一次，但每条鱼各自记 10 分钟冷却。
        返回 (决定, 真正可以点的 uid 列表)；被冷却挡掉的那几条不会出现。

        给主人的「圈一下群里的鱼」用；模型那边仍然只走单条 `outreach()`，
        不会因为多了一个方法就变成群发器。
        """
        uids: list[str] = []
        for raw in peer_uids or ():
            uid = str(raw or "").strip()
            if uid and uid not in uids:
                uids.append(uid)
        if not self.enabled:
            return Decision(True, "同类聊天闸关着"), uids
        now = now if now is not None else self.clock()
        gid = str(group_id or "")
        if not uids:
            return Decision(False, "没有可找的同类"), []

        # 1) 真人不在场：不主动开新话题（和单条 outreach 同一条底线）
        if self.human_idle and now - self._human_at.get(gid, 0.0) > self.human_idle:
            return Decision(False, "最近没有真人说话，不主动找同类"), []

        # 2) 每条鱼自己的 10 分钟冷却：刚找过的跳过，剩下的照点
        fresh = [
            uid
            for uid in uids
            if not (
                self.outreach_peer_gap
                and now - self._peer_outreach_at.get((gid, uid), 0.0)
                < self.outreach_peer_gap
            )
        ]
        if not fresh:
            return Decision(False, "刚找过这几条鱼"), []

        # 3) 一次点名只算一次主动搭话；但本群/本场的总兜底照样要过
        out = self._recent(self._outreach_hits.get(gid, []), self.outreach_window, now)
        if self.outreach_max and len(out) >= self.outreach_max:
            return Decision(False, "本群主动找同类太频繁"), []
        if self.outreach_gap and now - self._outreach_at.get(gid, 0.0) < self.outreach_gap:
            return Decision(False, "刚主动找过同类，缓一缓"), []

        group = self._recent(self._group_hits.get(gid, []), self.group_window, now)
        if self.group_max and len(group) >= self.group_max:
            return Decision(False, "本群同类一小时到顶"), []
        sess = self._session(gid, now)
        if self.session_max and sess.replies >= self.session_max:
            return Decision(False, "这场聊够了"), []
        if (
            self.session_budget
            and sess.spent + self.cost_per_reply > self.session_budget + 1e-9
        ):
            return Decision(False, "本场预算到顶"), []

        # 4) 放行，记一次账（每条鱼各自记一次「刚找过」）
        out.append(now)
        self._outreach_hits[gid] = out
        self._outreach_at[gid] = now
        for uid in fresh:
            self._peer_outreach_at[(gid, uid)] = now
        group.append(now)
        self._group_hits[gid] = group
        sess.replies += 1
        sess.spent += self.cost_per_reply
        sess.last = now
        self._sessions[gid] = sess
        return Decision(True, "主动点名"), fresh

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "pair_max": self.pair_max,
            "group_max": self.group_max,
            "session_max": self.session_max,
            "session_budget": self.session_budget,
            "outreach_max": self.outreach_max,
            "groups": len(self._sessions),
        }

    # ---------- 内部 ----------

    def _session(self, gid: str, now: float) -> _Session:
        sess = self._sessions.get(gid)
        if sess is None or (
            self.session_gap and now - sess.last > self.session_gap
        ):
            sess = _Session(last=now)
        return sess

    @staticmethod
    def _recent(hits: list[float], window: int, now: float) -> list[float]:
        if not window:
            return list(hits)
        return [t for t in hits if now - t < window]

    def _stop(self, gid: str, sess: _Session, now: float, reason: str) -> Decision:
        """到顶了：该收就收，顺便给个像她会说的由头。"""
        line = ""
        last_exit = self._exit_at.get(gid, 0.0)
        if (
            sess.replies > 0
            and now - last_exit >= self.exit_cooldown
            and _EXIT_LINES
        ):
            self._exit_at[gid] = now
            line = self.picker(_EXIT_LINES)
        return Decision(False, reason, line)
