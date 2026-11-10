"""群聊主动插话的门控。

核心是分层：
1. **本地规则**（零成本）先粗筛：关键词命中、有人提问、随机抽样
2. 筛中之后才**调一次模型**，让模型看上下文自己决定要不要说话
3. 模型可以回 [不插话]，那就真的不说

外加硬性限流（冷却时间 + 每小时上限），防止刷屏。

2026-10-25 P1：群级"对话链"换成**按人跟进窗口**。
- 她回了谁，就给谁开一个窗口（默认 90 秒、最多再回 6 条）；
- 只有这个人接着说的、且还在同一件事上的话，才走"接话"；
- 多人同时跟她说话，各开各的窗口，互不覆盖；
- 换话题/去跟别人说话 → 关掉这个人的窗口，日志写原因；
- 模型 [不插话] 只表示这一轮不说，不影响窗口。
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .anaphora import is_followup, refers_to_topic
from .stopwords import STOPWORDS, filter_keywords
from .tags import SPEAKER_TAGS

log = logging.getLogger(__name__)

SKIP_TOKEN = "[不插话]"

# 「别人家的肥鱼」：别人自己养的那条，不是她本人。
# 只认带「谁家」的说法；光一个「肥鱼」仍然按叫她处理（见 app._called_her_name）。
_OTHER_OWNER_FISH_RE = re.compile(
    r"(?:我|俺|他|她|别人|人家)家(?:的)?(?:那条|这条|那只|这只)?(?:大)?肥鱼"
)


def other_owner_fish(text: str) -> str:
    """这句是不是在聊别人家养的那条肥鱼。是就返回原话，否则空串。"""
    match = _OTHER_OWNER_FISH_RE.search(text or "")
    return match.group(0) if match else ""


# 英文/数字词 + 中文双字，用来粗判"是不是还在说同一件事"
_LATIN_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _topic_terms(text: str) -> set[str]:
    """很粗的话题词：英文/数字串 + 中文相邻双字（去掉停用词）。"""
    body = (text or "").lower()
    terms = set(_LATIN_RE.findall(body))
    for run in _CJK_RE.findall(body):
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in STOPWORDS:
                terms.add(bigram)
    return terms


@dataclass
class FollowupWindow:
    """她刚回过的一个人。

    窗口不是"这个人可以随便接话"，而是"他刚才跟她说了什么、她回了什么"。
    下一条消息要能对得上这个锚，才继续接。
    """

    uid: str
    nick: str
    until: float
    anchor: set[str] = field(default_factory=set)
    replies: int = 0
    opened: float = 0.0
    last_reply_at: float = 0.0
    last_answer: str = ""


class GroupWatcher:
    def __init__(
        self,
        keywords: list[str],
        chance: float = 0.03,
        cooldown: int = 300,
        hourly_cap: int = 6,
        buffer_size: int = 12,
        followup_seconds: int = 90,
        followup_max_replies: int = 6,
        followup_immediate_seconds: int = 30,
        gap: int = 5,
        keyword_min_len: int = 2,
    ) -> None:
        # 关键词是子串匹配（k in text），「那个」「就是」这种词会命中一切
        self.keywords, self.dropped_keywords = filter_keywords(
            list(keywords), keyword_min_len
        )
        if self.dropped_keywords:
            log.warning("这些关键词太泛了，已忽略：%s", "、".join(self.dropped_keywords))
        self.chance = max(0.0, min(chance, 1.0))
        self.cooldown = max(0, cooldown)
        self.hourly_cap = max(0, hourly_cap)
        self.buffers: dict[str, deque] = {}
        # 和 buffers 对齐的 uid：群聊上下文按 uid 过滤，别让同名的人串进来
        self.buffer_uids: dict[str, deque] = {}
        self.buffer_size = buffer_size
        self.last_spoke: dict[str, float] = {}
        # 上次主动开新窗口/插话的时间，冷却按它算，不按跟进接话算
        self.last_new: dict[str, float] = {}
        self.hour_marks: dict[str, deque] = {}
        # 跟进窗口：她回谁，就给谁开一个
        self.followup_seconds = max(0, followup_seconds)
        # 0 = 不限制；默认一条窗口最多再回 6 条
        self.followup_max_replies = max(0, followup_max_replies)
        # 她刚回完的多少秒内，这个人接着说的下一句算"即时接话"
        self.followup_immediate_seconds = max(0, followup_immediate_seconds)
        # 两条回复之间的最小间隔
        self.gap = max(0, gap)
        # gid -> uid -> FollowupWindow
        self.windows: dict[str, dict[str, FollowupWindow]] = {}
        # 她当时明确没接的消息序号（在回别人/叫别人/换话题）：后续上下文里摘掉
        self.ignored: dict[str, deque[int]] = {}
        # 每个群的入队序号。合批时靠它把"这一轮刚收到的"从历史里摘出来：
        # 不摘的话，同一批消息会在提示词里出现两遍（一遍当历史、一遍当正文）。
        self._seq: dict[str, int] = {}

    # ---------- 上下文缓冲 ----------
    #
    # 群里每条消息都记一笔（带时间/uid），有两个用处：
    # 1. 主动插话时当上下文
    # 2. 她突然被 @ 时，把"被叫之前大家在聊什么"补进提示词

    def add(
        self,
        group_id: str,
        nickname: str,
        text: str,
        ts: float | None = None,
        uid: Any = None,
    ) -> int:
        """把群里的每条消息都记下来，供接话/被叫到时当上下文（不进长期记忆）。

        返回这条消息的序号：合批时要靠它把这一批从历史里排除掉，
        否则同一批话会同时出现在"聊天记录"和"刚刚收到的"两处。
        """
        seq = self._seq.get(group_id, 0) + 1
        self._seq[group_id] = seq
        buf = self.buffers.setdefault(group_id, deque(maxlen=self.buffer_size))
        buf.append((ts if ts is not None else time.time(), f"{nickname}：{text}", seq))
        uids = self.buffer_uids.setdefault(
            group_id, deque(maxlen=self.buffer_size)
        )
        uids.append(str(uid) if uid is not None and str(uid) else "")
        return seq

    def warm_from_log(self, group_log: Any, limit: int = 20, days: float = 1) -> int:
        """启动/重连后把 group_log 的最近消息灌回缓冲。

        以前一重启，群缓冲是空的，"刚才大家在聊什么"直接没了。这里只回放
        最近一小段（默认 1 天、每群 20 条），真正进提示词时还会再按
        `within_seconds` 过滤，不会把半天前的话题当成"刚才"。
        """
        if group_log is None or limit <= 0:
            return 0
        n = 0
        try:
            groups = group_log.groups()
        except Exception:  # noqa: BLE001
            return 0
        for gid in groups:
            try:
                rows = group_log.recent(gid, limit=limit, days=days)
            except Exception:  # noqa: BLE001
                continue
            for row in rows:
                self.add(
                    str(gid),
                    str(row.get("nick") or "群友"),
                    str(row.get("text") or ""),
                    ts=row.get("ts"),
                    uid=row.get("uid"),
                )
                n += 1
        return n

    def _buffer_rows(
        self,
        group_id: str,
        limit: int = 10,
        within_seconds: int = 0,
        skip_seqs: set[int] | None = None,
    ) -> list[tuple[float, str, int]]:
        """最近几条群消息的原始行（时间、正文、序号），从旧到新。"""
        if limit <= 0:
            return []
        buf = self.buffers.get(group_id)
        if not buf:
            return []
        rows = list(buf)
        if within_seconds > 0:
            cutoff = time.time() - within_seconds
            rows = [row for row in rows if row[0] >= cutoff]
        if skip_seqs:
            rows = [row for row in rows if row[2] not in skip_seqs]
        return rows[-limit:]

    def context(
        self,
        group_id: str,
        limit: int = 10,
        within_seconds: int = 0,
        skip_seqs: set[int] | None = None,
        exclude_ignored: bool = False,
    ) -> list[str]:
        """最近几条群消息（从旧到新）。

        within_seconds > 0 时只取这个时间窗内的，避免把半小时前的话题
        当成"刚才在聊"塞给她；skip_seqs 里的消息已经被当成"这一轮"单独
        交给模型了，这里不再重复出现。exclude_ignored=True 时，把"她当时
        明确没接"的消息也摘掉（给"跟她的对话"用，不给纯背景用）。
        """
        merged_skip = set(skip_seqs or ())
        if exclude_ignored:
            merged_skip |= self.ignored_seqs(group_id)
        return [
            line
            for _, line, _ in self._buffer_rows(
                group_id,
                limit=limit,
                within_seconds=within_seconds,
                skip_seqs=merged_skip,
            )
        ]

    def window_context(
        self,
        group_id: str,
        uid: Any,
        limit: int = 10,
        skip_seqs: set[int] | None = None,
        now: float | None = None,
    ) -> list[str]:
        """只留"她 + 这个窗口里的人"说过的话，给接话提示词用。

        群友在旁边闲聊的内容会进缓冲区，但不该混进这条跟进窗口，
        否则模型会以为那些也是"正在聊的话题"。
        """
        if limit <= 0:
            return []
        buf = self.buffers.get(group_id)
        if not buf:
            return []
        uids = self.buffer_uids.get(group_id)
        rows = list(buf)
        uid_list = list(uids) if uids is not None and len(uids) == len(rows) else []
        target = str(uid) if uid is not None else ""
        merged_skip = set(skip_seqs or ()) | self.ignored_seqs(group_id)
        kept: list[str] = []
        for idx, (_, line, seq) in enumerate(rows):
            if seq in merged_skip:
                continue
            who = self._speaker_of(line)
            if who == "本鱼":
                kept.append(line)
                continue
            if uid_list:
                if uid_list[idx] and uid_list[idx] == target:
                    kept.append(line)
                continue
            # 老缓冲/单测没有 uid：退回昵称匹配
            win = self.windows.get(group_id, {}).get(target)
            if win and who == win.nick:
                kept.append(line)
        return kept[-limit:]

    def known_names(self, group_id: str) -> set[str]:
        """这个群里最近说过话的人（用来判断"这句话在叫谁"）。"""
        names: set[str] = set()
        for _, line, _ in self.buffers.get(group_id, ()):
            head = line.split("：", 1)[0].strip()
            if head and head != "本鱼":
                names.add(head)
        return names

    @staticmethod
    def addressed_to_other(text: str, names: set[str]) -> str | None:
        """这句话是不是在叫别人。

        只认很明确的称呼写法，宁可漏判也不误判：
        1. 名字在开头，且后面跟着标点/空格（「小明，你觉得呢」）
        2. 名字后面紧跟「你 / 在吗 / 觉得 / 怎么看」（「小红你会写吗」）

        「小明说得好」「小明昨天说的那个」这种只是提到某人，不算叫他。
        """
        body = (text or "").strip()
        if not body or not names:
            return None
        separators = "，,：:、~～!！?？ "
        for name in sorted(names, key=len, reverse=True):
            if not name or name not in body:
                continue
            idx = body.find(name)
            end = idx + len(name)
            tail2 = body[end : end + 2]
            if idx == 0:
                nxt = body[end : end + 1]
                if nxt and nxt in separators:
                    return name
                if "你" in tail2 or "在吗" in tail2 or "在么" in tail2:
                    return name
                continue
            if any(w in tail2 for w in ("你", "觉得", "怎么看")) or "在吗" in tail2:
                return name
        return None

    # 一眼就是"水"的短句：不值得为她起一轮模型调用
    _FILLERS = {
        "哈", "哈哈哈", "哈哈哈哈", "hhh", "hahaha", "草", "乐", "典", "6",
        "嗯", "嗯嗯", "哦", "哦哦", "好的", "收到", "行", "+1", "?", "？",
        "笑死", "难绷", "蚌埠住了", "确实", "草草",
    }

    # 消息段转成文字后的占位符：这些不是"话"，只说明这里有个附件
    _PLACEHOLDER_RE = re.compile(
        r"\[(?:图片|表情包|动画表情|表情|语音|视频|文件|卡片消息|合并转发)[^\]]*\]"
    )

    @classmethod
    def is_sticker_only(cls, text: str) -> bool:
        """整条消息只有图/表情包，一个字都没说。

        群友甩一张表情包，多半是应一声或随手发图，不是一个话轮。
        """
        body = cls._PLACEHOLDER_RE.sub("", text or "")
        body = re.sub(r"[\s，。！？!?~～…、,.（）()\[\]【】]", "", body)
        return not body

    @classmethod
    def is_low_value(cls, text: str) -> bool:
        """纯水、纯表情、纯符号的消息，不值得跳出一句真话。"""
        # 纯占位符先单独判掉：之前 "[表情包]" 三个字躲过了下面的规则
        if cls.is_sticker_only(text):
            return True
        body = "".join(
            ch for ch in (text or "").strip()
            if ch not in "，。！？!?~～…、,. \t[]（）()"
        )
        if not body:
            return True
        if len(body) <= 1:
            return True
        if body.lower() in cls._FILLERS:
            return True
        # 同一个字重复（哈哈哈哈、6 6 6）
        if len(set(body)) == 1:
            return True
        # 去掉重复后只剩一两个字，也算水
        return len(set(body)) <= 2 and len(body) <= 4

    # ---------- 门控 ----------

    def _recent_count(self, group_id: str, now: float) -> int:
        marks = self.hour_marks.setdefault(group_id, deque())
        while marks and now - marks[0] > 3600:
            marks.popleft()
        return len(marks)

    def note_spoke(self, group_id: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.last_spoke[group_id] = now
        self.last_new[group_id] = now
        self.hour_marks.setdefault(group_id, deque()).append(now)

    # ---------- 按人跟进窗口 ----------
    #
    # 插话是"从外面插进去"，一次就够；但一旦聊起来了，接下来几句属于同一个对话。
    # 现在不记"一条群级链"，而是记"她刚才回了谁、回了什么"：
    #
    # - 她每回复一个人，就给这个人开一个窗口（TTL + 最多再回 N 条）
    # - 这个人的下一条消息，只有引用她/叫她名字、刚回完 30 秒内、
    #   或与窗口锚（他那句 + 她回答）重合，才继续接
    # - 换话题/去跟别人说话 → 关掉这个人的窗口，日志写原因
    # - 多人同时跟她说话 → 各开各的窗口，互不覆盖
    # - 模型 [不插话] 只表示这一轮不说，不影响窗口

    def open_window(
        self,
        group_id: str,
        uid: Any,
        nickname: str = "",
        incoming: str = "",
        answer: str = "",
        now: float | None = None,
        reset: bool = True,
        count_reply: bool = True,
    ) -> FollowupWindow | None:
        """她刚回复了 uid：开/续这个人的窗口。

        `reset=True` 用于"新一轮对话"（@ 她、她主动插话、引用她）；
        `reset=False` 用于同一条窗口里的接话续期。
        `count_reply=False` 只给"先开个临时窗口判断要不要回"用。
        """
        if uid is None or str(uid) == "":
            return None
        now = now if now is not None else time.time()
        uid_str = str(uid)
        anchor = _topic_terms(incoming) | _topic_terms(answer)
        owner = self.windows.setdefault(group_id, {})
        win = owner.get(uid_str)
        if win is None:
            win = FollowupWindow(
                uid=uid_str,
                nick=nickname or uid_str,
                until=now + self.followup_seconds,
                anchor=anchor,
                replies=1 if count_reply else 0,
                opened=now,
                last_reply_at=now,
                last_answer=(answer or "")[:120],
            )
            owner[uid_str] = win
        else:
            win.nick = nickname or win.nick
            win.until = now + self.followup_seconds
            win.anchor = anchor
            win.last_reply_at = now
            win.last_answer = (answer or "")[:120]
            if reset:
                win.opened = now
                win.replies = 1 if count_reply else 0
            elif count_reply:
                win.replies += 1
        self.last_spoke[group_id] = now
        return win

    def extend_window(
        self,
        group_id: str,
        uid: Any,
        incoming: str = "",
        answer: str = "",
        now: float | None = None,
    ) -> FollowupWindow | None:
        """同一窗口里的接话：续期、更新锚、计一条回复。"""
        return self.open_window(
            group_id,
            uid,
            nickname="",
            incoming=incoming,
            answer=answer,
            now=now,
            reset=False,
            count_reply=True,
        )

    def window_of(
        self, group_id: str, uid: Any, now: float | None = None
    ) -> FollowupWindow | None:
        """这个人现在有没有一个有效的跟进窗口；过期就顺手清掉。"""
        if uid is None or str(uid) == "":
            return None
        now = now if now is not None else time.time()
        owner = self.windows.get(group_id, {})
        win = owner.get(str(uid))
        if win is None:
            return None
        if self.followup_seconds and now >= win.until:
            owner.pop(str(uid), None)
            log.debug("跟进窗口过期：%s（群 %s）", win.nick, group_id)
            return None
        if self.followup_max_replies and win.replies >= self.followup_max_replies:
            owner.pop(str(uid), None)
            log.info(
                "跟进窗口关闭（聊够了 %d 条）：%s（群 %s）",
                win.replies,
                win.nick,
                group_id,
            )
            return None
        return win

    def close_window(
        self,
        group_id: str,
        uid: Any,
        reason: str = "",
        now: float | None = None,
    ) -> bool:
        """关掉这个人的窗口。返回是否真的关掉了一个窗口。"""
        if uid is None or str(uid) == "":
            return False
        owner = self.windows.get(group_id, {})
        win = owner.pop(str(uid), None)
        if win is None:
            return False
        if reason:
            log.info(
                "跟进窗口关闭（%s）：%s（群 %s，已回 %d 条）",
                reason,
                win.nick,
                group_id,
                win.replies,
            )
        return True

    def clear_windows(self, group_id: str, reason: str = "") -> int:
        """群静默/潜水/换话题时，把这个群所有窗口清掉。"""
        owner = self.windows.pop(group_id, None) or {}
        if owner and reason:
            log.info(
                "跟进窗口清空（%s）：群 %s，%d 个",
                reason,
                group_id,
                len(owner),
            )
        return len(owner)

    def window_fit(
        self,
        group_id: str,
        uid: Any,
        text: str,
        named: bool = False,
        quoted_her: bool = False,
        now: float | None = None,
    ) -> tuple[bool, str, list[str], float]:
        """这条消息，是不是在接她跟这个人的上一轮。

        返回 (过门, 原因, 重合词, 覆盖率)。本地门，0 token：
        - 引用她 / 叫她名字 → 一定过
        - 她刚回完的同一个人，30 秒内接着说的下一句 → 过（即时接话）
        - 与窗口锚（他那句 + 她的回答）重合：至少 2 个中文双字、
          或 1 个 4 字母以上的英文词，且覆盖这句的 25% → 过
        - 其余不过；低价值/空消息不算"话题已转"，不会关窗口。
        """
        now = now if now is not None else time.time()
        if quoted_her:
            return True, "引用她", [], 0.0
        if named:
            return True, "叫她的名字", [], 0.0
        win = self.window_of(group_id, uid, now)
        if win is None:
            return False, "没有跟进窗口", [], 0.0
        body = (text or "").strip()
        if not body:
            return False, "空消息", [], 0.0
        if self.is_low_value(body):
            # 不是即时接话的纯水/表情，不值得为它起一轮模型调用。
            # 即时接话（下面那条）不过这一关："为啥"这种短跟进也算话。
            return False, "低价值", [], 0.0
        # 「这关 / 这图 / 这boss」明确指刚才那一轮：这是接着说，不是换话题。
        # 2026-11-07 11:03「这关boss是谁」被下面的话题重合判成「话题已转」，
        # 她直接沉默 —— 那句话明明是在接 JT8-3。纯指代问句（「为什么」「名字
        # 叫什么」）也同理：窗口还在，就是在接上一轮。
        if refers_to_topic(body) or is_followup(body):
            return True, "指代刚才的话题", [], 0.0
        gap = now - win.last_reply_at
        if (
            self.followup_immediate_seconds
            and gap <= self.followup_immediate_seconds
        ):
            return True, f"她刚回完（{gap:.0f}s）", [], 0.0
        terms = _topic_terms(body)
        shared = terms & win.anchor
        coverage = (len(shared) / len(terms)) if terms else 0.0
        strong = any(len(t) >= 4 and t.isascii() for t in shared)
        if strong or (len(shared) >= 2 and coverage >= 0.25):
            return True, f"与上一轮重合 {len(shared)} 词", sorted(shared)[:6], coverage
        return False, "话题已转", sorted(shared)[:6], coverage

    def should_followup(
        self, group_id: str, uid: Any, now: float | None = None
    ) -> tuple[bool, str]:
        """本地限流：窗口还在、没聊够、两条之间别太密。"""
        now = now if now is not None else time.time()
        win = self.window_of(group_id, uid, now)
        if win is None:
            return False, "没有跟进窗口"
        gap = now - self.last_spoke.get(group_id, 0.0)
        if gap < self.gap:
            return False, f"接话间隔太短（{int(self.gap - gap)}s）"
        return True, f"跟进窗口（已回 {win.replies} 条）"

    def note_ignored(self, group_id: str, seq: int | None) -> None:
        """记下一条她已经决定不接的消息序号。

        典型场景：窗口里的人去回别人/叫别人、或者换了话题。这一句当时
        被静默，但消息本身已经进了群聊缓冲；如果不摘掉，下一轮接话时
        它还会以"刚才聊的"身份出现，模型容易把两个人的话并成一句。
        """
        if not seq:
            return
        bucket = self.ignored.setdefault(
            group_id, deque(maxlen=max(32, self.buffer_size * 4))
        )
        value = int(seq)
        if value not in bucket:
            bucket.append(value)

    def ignored_seqs(self, group_id: str) -> set[int]:
        return set(self.ignored.get(group_id, ()))

    def window_prompt(
        self,
        group_id: str,
        uid: Any,
        batch: str | list[str],
        skip_seqs: set[int] | None = None,
        limit: int = 10,
        now: float | None = None,
        other_fish: str = "",
        mention: str = "",
    ) -> str:
        """拼"接跟进窗口"这一轮的话。

        与旧链提示词的区别：不再说"你们还在同一个话题里"，只摆出
        她刚才回了这个人什么，让模型自己判断这一轮是不是还在说同一件事。

        `mention` 是这句里命中的她的名字（大肥鱼/肥鱼/鲸鱼…）。**提到名字
        不等于在叫她**：可能是在喊她，也可能是在聊别人家的鱼、别的同名东西
        （2026-10-27 00:12「我用大肥鱼word转pdf」就是这么被误当成喊她的）。
        所以提示词只写"提到了"，把"是不是在说她"交回上下文判断。

        `other_fish` 是「我家肥鱼」这类带"谁家"的原话：更明确是别人家那条，
        提示词直接说"多半是别人自己养的那条"。
        """
        if isinstance(batch, str):
            batch = [batch] if batch.strip() else []
        win = self.window_of(group_id, uid, now)
        who = win.nick if win else "他"
        ctx = self.window_context(group_id, uid, limit=limit, skip_seqs=skip_seqs)
        header = (
            f"这是群里最近的聊天记录（只挑了你和{who}说的；"
            "行首「本鱼：」是你自己，其他都是别人）：\n"
            + "\n".join(ctx)
            + "\n\n"
            if ctx
            else ""
        )
        burst = (
            "**这一轮刚收到的：**\n" + "\n".join(batch) + "\n\n"
            if batch
            else ""
        )
        if other_fish:
            if win and win.last_answer:
                intro = (
                    f"你刚才回了{who}：「{win.last_answer}」。"
                    f"这一轮{who}提到了「{other_fish}」——先看是在说别人家那条，"
                    "还是在叫你。\n\n"
                )
            else:
                intro = (
                    f"这一轮是{who}提到了「{other_fish}」——多半是别人自己养的那条肥鱼，"
                    "不一定是在叫你。\n\n"
                )
        elif mention:
            if win and win.last_answer:
                intro = (
                    f"你刚才回了{who}：「{win.last_answer}」。"
                    f"这一轮{who}提到了「{mention}」——先分清楚是在叫你，还是在说别人。\n\n"
                )
            else:
                intro = (
                    f"这一轮是{who}提到了「{mention}」——先分清楚是在叫你，还是在说别人。\n\n"
                )
        elif win and win.last_answer:
            intro = (
                f"你刚才回了{who}：「{win.last_answer}」。"
                f"这一轮看起来是{who}接着说的。\n\n"
            )
        else:
            intro = f"这一轮是{who}在叫你/引用你，不是在接你上一句。\n\n"
        if other_fish:
            other_note = (
                f"\n（「{other_fish}」说的是别人家养的那条，不是你：可以凑过去搭一句"
                "——好奇问问、说说本鱼自己都行；别抢着认领、别当成在叫你。"
                "真分不清就问一句「是说你家的，还是叫本鱼呀」。）\n"
            )
        elif mention:
            other_note = (
                f"\n（这句里出现了「{mention}」，但不一定是在说你：名字单独出现、在句首，"
                "或者正在问你的事，就是在叫你，正常接；说的是别人家的鱼、别的同名东西，"
                "就别抢着认领——凑一句也行，但别把别人的事揽到本鱼头上。）\n"
            )
        else:
            other_note = ""
        return (
            header + burst + intro +
            "把「这一轮刚收到的」当成一个整体看：\n"
            "- 还在接着你们刚才说的那件事 → 简短接一句（一到两句），"
            "对着整段回，别只回最后半句，也别重复你刚才说过的内容\n"
            "- 已经转去聊别的事了 → 只输出 " + SKIP_TOKEN + "（这次安静就好，"
            "别追着把话题拽回来，也不算跑题）\n"
            "- 他在回别的群友 / 叫别人名字 → 也只输出 " + SKIP_TOKEN + "\n"
            "- 只有图/表情包、一个字没说的 → 默认 " + SKIP_TOKEN + "（只甩了张图，"
            "多半是应一声，不是话轮），除非它明显是在回应你刚才那句\n\n"
            "示例（你刚才说的是「用过一点啦，好不好得看拿来干嘛嘛」）：\n"
            "这一轮：我昨天试了下感觉还行 → 你：是吧～你主要拿来写代码还是聊天\n"
            "这一轮：就是速度有点慢 / 还老排队 → 你：排队是有点烦，本鱼这边也常卡\n"
            "这一轮：对了你们周末干嘛 → 你：" + SKIP_TOKEN + "\n"
            "这一轮：@小明 你觉得呢 → 你：" + SKIP_TOKEN + "\n"
            "这一轮：[图片] → 你：" + SKIP_TOKEN + "\n\n"
            + other_note
            + f"只输出你要说的话，或 {SKIP_TOKEN}，不要解释。"
        )

    def should_speak(
        self,
        group_id: str,
        text: str,
        now: float | None = None,
        called: bool = False,
    ) -> tuple[bool, str]:
        """本地粗筛（非跟进窗口）。返回 (是否值得让模型判断, 原因)。

        `called=True` 表示有人在**叫她**（@ 之外，喊她名字那种）：这时候
        冷却不该拦她 —— 「刚说过话所以装作没听见」是最伤人的一种沉默。
        每小时份额仍然照算，防止有人拿她的名字刷屏。

        `chance=0`（档位 1）只认关键词和 `called`：不做问句保底、不做随机抽样。
        问句保底是档位 2 及以上才有的"探索预算"。
        """
        now = now if now is not None else time.time()
        body = text.strip()
        if not body:
            return False, "空消息"

        # 纯图/表情包没有文字，是"应一声"，不是话轮；跟进窗口里由
        # window_fit 单独处理，这里只管普通插话。
        if self.is_sticker_only(body):
            return False, "纯表情/图片，不接"

        last = self.last_new.get(group_id, 0.0)
        if self.cooldown and not called and now - last < self.cooldown:
            return False, f"冷却中（还要 {int(self.cooldown - (now - last))}s）"

        used = self._recent_count(group_id, now)
        if self.hourly_cap and used >= self.hourly_cap:
            return False, f"本小时已插话 {used} 次，到上限"

        if called:
            return True, "有人在叫她"

        lowered = body.lower()
        hit = next((k for k in self.keywords if k in lowered), None)
        if hit:
            return True, f"命中关键词「{hit}」"

        # 问句保底只在"随机抽样"档（tier 2+，chance > 0）生效：
        # tier 1 是"只关键词"，不能再顺手判 15% 的问句。
        is_question = ("？" in body) or ("?" in body) or body.endswith(("吗", "呢", "嘛"))
        if self.chance > 0 and is_question and random.random() < max(self.chance, 0.15):
            return True, "有人在提问"

        if random.random() < self.chance:
            return True, "随机抽样"

        return False, "没兴趣"

    @staticmethod
    def _speaker_of(line: str) -> str:
        head = line.split("：", 1)[0]
        # 标签表从 tags.py 拿，别在这里手抄：加一种身份（群主/管理员）时
        # 手抄的那份不会跟着变，合批就会把人名解析成「管理员】老张」。
        for tag in SPEAKER_TAGS:
            head = head.replace(tag, "")
        return head.strip()

    @classmethod
    def speakers_of(cls, batch: list[str]) -> list[str]:
        """这一批里有哪几个人在说话（按出现顺序去重）。"""
        names: list[str] = []
        for line in batch:
            who = cls._speaker_of(line)
            if who and who not in names:
                names.append(who)
        return names

    def build_prompt(
        self,
        group_id: str,
        batch: str | list[str],
        skip_seqs: set[int] | None = None,
        limit: int = 10,
        within_seconds: int = 0,
        exclude_ignored: bool = False,
    ) -> str:
        """拼普通主动插话这一轮的话。

        `batch` 是**刚刚收到的那一批**（按先后、每条都带说话人标签）。
        合批真正的关键不在"只调一次模型"，而在提示词里把这一批标成一个回合：
        否则模型看到的只是"最近的聊天记录"，自然顺着最后一句接，
        前面那些提问/补充/反驳全被当成旧背景。skip_seqs 用来把这一批
        从历史里摘掉，避免同一批话在提示词里出现两遍。
        """
        if isinstance(batch, str):
            batch = [batch] if batch.strip() else []

        ctx = self.context(
            group_id,
            limit=limit,
            within_seconds=within_seconds,
            skip_seqs=skip_seqs,
            exclude_ignored=exclude_ignored,
        )
        header = (
            "这是群里最近的聊天记录（行首「本鱼：」是你自己，其他都是别人；"
            "别的鱼自称本鱼也不算你）：\n" + "\n".join(ctx) + "\n\n"
            if ctx
            else ""
        )
        burst = (
            "**这一轮刚收到的（按先后，可能不止一条、不止一个人）：**\n"
            + "\n".join(batch)
            + "\n\n"
            if batch
            else ""
        )
        return (
            header + burst +
            "你不是客服，是群里一个话不多的群友。现在决定要不要开口。\n\n"
            "**默认闭嘴。** 只有这两种情况才说话：\n"
            "1. 话题直接跟你有关：AI、模型、DeepSeek、鲸鱼、本鱼，或者有人叫你\n"
            "2. 有人说了明显不对的事实，你能一句话纠正\n\n"
            "吃饭、追剧、别人的代码报错、纯水群、跟你无关的闲聊——一律不接。\n\n"
            "**先把「这一轮刚收到的」当成一个回合理解**：他们在聊什么、几条之间是什么关系"
            "（在问、在答、在补充、在反驳、在换话题），再决定接不接、接哪件。\n"
            "- 几条说的是同一件事 → 要接就接这件事，别只回最后半句\n"
            "- 其实是不相干的几件事 → 只挑跟你有关、或明显说错的那件\n"
            "- 不要一句一句地回，也别因为最后一句看着好接就只接它\n\n"
            "判断示例：\n"
            "这一轮：小明：今天中午吃啥 / 小红：食堂吧 → 你：" + SKIP_TOKEN + "\n"
            "这一轮：小红：我最近在看重启人生 → 你：" + SKIP_TOKEN + "\n"
            "这一轮：小明：我这个 rust 借用检查又报错了 / 小红：怎么改啊 → 你：" + SKIP_TOKEN + "\n"
            "这一轮：小红：那个 deepseek V4 到底行不行啊 / 小明：听说涨价了 → "
            "你：用过一点啦，好不好得看拿来干嘛嘛\n\n"
            f"现在轮到你了。只输出你要说的话；不想说就只输出 {SKIP_TOKEN}，不要解释。"
        )


def is_skip(reply: str) -> bool:
    """模型是否选择了不说话。"""
    if not reply:
        return True
    cleaned = reply.strip()
    return cleaned.startswith(SKIP_TOKEN) or cleaned == "" or len(cleaned) <= 1
