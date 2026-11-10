"""@ 别人的本地硬门。

前提：**@ 是"降低歧义"的工具，不是"提高曝光"的工具。**

群里 @ 一下是"把话递给某个人"：她回的不一定是最新说话的人（中间夹了别人），
不点名就会被读成在回最后那位。这种情况下点名是对的；为了让某个人看见而
@ 人，就是骚扰。

所以这里不判断"该不该说话"（那是她的判断、在上游），只判断三件事：

1. **这句话值不值得点名** —— 只在"接的不是最新说话人"这类歧义场景放行；
2. **点名了会不会变成骚扰** —— 冷却、小时/每日上限、对方在不在场；
3. **对方是不是明确说过别 @ 他** —— 说过就永久跳过，本地识别、不花 token。

模型只是"想"点名（在回复里写 `[at:123456]`），**发不发得出去由这里说了算**：
门外一律降级成"老张，你说得对"这种纯文字，不报错、也不硬发。

最要紧的一条是 `missed`：**@ 了某人、他没回，这个人 24 小时内不再 @。**
骚扰的本质不是 @ 的次数多，而是"没应声还在继续 @"。把这条做成硬规则，
比把每小时上限从 3 调到 1 有用得多。

## 三种场合，两套账（2026-10-27 起；2026-11-10 把账分干净）

以前所有 @ 都按"她主动去点人"记账，结果最该点的时候也点不出去：一群人先后 @ 她，
她只能回一个；在热闹的链里说"你说得对"，谁也不知道她在回谁。现在分开算：

**这几本账只记「她主动去点人」那一路**：同群冷却、每小时上限、每人每天、
同一个人冷却。回话和链里指代**一律不占额度**，只挂一条"点了没回" ——
对话本来就是一人一次、等对方应声；拿总次数去卡一整场聊天，只会让她后半场
全退成纯文字（2026-11-10：同一场对话里 `今天点他 8 次了` 挡了一整晚）。

- **插话**（没人找她，她自己冒一句）：`_at_note` 根本不递候选，也不放行 ——
  这是纯粹为了让别人注意到她的 @，正是这个门要挡的东西。
- **回话**（`reply_back`：他刚 @ / 引用 / 叫了她）：群冷却、小时上限、每人每天、
  群主/管理员那几条都不拦 —— 是他们先找的她，不是她去点名。
- **链里指代**（`in_chain`：这一轮好几个人在说话）：同样不数次数。按"每小时点了
  几个人"计费，等于罚她把话说清楚；链里 @ 的作用就是"这句是接谁的"。

判定顺序也照这个来：先看是不是回话（他先找的她），再看是不是在链里，最后才是
`missed` / 在场 / 冷却这些"她主动去点人"才该管的事。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .roster import ROLE_ADMIN, ROLE_OWNER

log = logging.getLogger(__name__)

# 模型点名要用的写法。只认方括号：正文里光秃秃的 "@张三" 是打字，不当指令。
# 名字写法（`[at:老张]`）也一并剥掉 —— 认不出来就不发，但绝不能把方括号
# 原样发到群里。
_MARK_RE = re.compile(r"\[(?:at[:：]|@)\s*([^\]\n]{1,24})\s*\]", re.I)
_UID_RE = re.compile(r"^\d{4,12}$")
# 标点开头时不要再补逗号
_LEAD_PUNCT = "，,。！？!?、；;：:～~ "

# 对方明确拒绝被点名的说法。只认「@ / 艾特 / 点我」这种指向自己的，
# **不认「别叫我 X」** —— 那是称呼（「别叫我老板」），不是拒绝被叫到。
# 「别叫我」在群里太常见了，认了就会把一票人误拉进拒绝名单。
_REFUSE_RE = re.compile(
    r"(?:别|不要|不许|不用|少)(?:再|总|老|老是)?(?:@|艾特|at|圈|点)(?:我|人家)",
    re.I,
)


def split_markers(text: str) -> tuple[str, list[str]]:
    """把回复里的 `[at:号]` 全摘出来，返回 (正文, 号列表)。

    摘干净是硬要求：门外降级时如果留下方括号，群里就会看见一句
    「[at:123456] 你说得对」——比不点名还糟。
    """
    if not text:
        return text, []
    targets: list[str] = []

    def _take(match: re.Match[str]) -> str:
        token = (match.group(1) or "").strip()
        if _UID_RE.match(token) and token not in targets:
            targets.append(token)
        return ""

    clean = _MARK_RE.sub(_take, text)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"(?m)^[ \t]+", "", clean).strip()
    return clean, targets


def prefix_name(text: str, name: str) -> str:
    """门外降级：把「@」换成一句带名字的话，别让这句变得没头没尾。"""
    body = (text or "").lstrip()
    name = (name or "").strip()
    if not name:
        return body
    if not body:
        return name
    if body[0] in _LEAD_PUNCT:
        return f"{name}{body}"
    return f"{name}，{body}"


def looks_like_refusal(text: str) -> bool:
    """这句话是不是「别 @ 我」。本地判断，0 token。"""
    body = (text or "").strip()
    return bool(body) and bool(_REFUSE_RE.search(body)) and len(body) <= 40


class AtGate:
    """@ 人的状态机：冷却、上限、在不在场、点名没回、拒绝名单。"""

    # 拒绝名单记多久：说一次「别@我」管 30 天，别把人家永久钉死
    REFUSE_DAYS = 30
    # 每小时/每日上限的统计窗口
    HOUR = 3600.0
    DAY = 86400.0

    def __init__(
        self,
        path: Path,
        enabled: bool = False,
        group_cooldown: int = 300,
        group_hourly_cap: int = 3,
        person_cooldown: int = 1800,
        person_daily_cap: int = 4,
        present_seconds: int = 300,
        miss_ignore_hours: float = 24.0,
        warmup_hours: float = 6.0,
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.group_cooldown = max(0, int(group_cooldown))
        self.group_hourly_cap = max(0, int(group_hourly_cap))
        self.person_cooldown = max(0, int(person_cooldown))
        self.person_daily_cap = max(0, int(person_daily_cap))
        self.present_seconds = max(0, int(present_seconds))
        self.miss_ignore = max(0.0, float(miss_ignore_hours)) * 3600.0
        self.warmup = max(0.0, float(warmup_hours)) * 3600.0
        # 群 -> 上次点名时间 / 最近一小时的记录
        self.groups: dict[str, dict[str, Any]] = {}
        # "群:号" -> 最近 24 小时的记录
        self.people: dict[str, list[float]] = {}
        # "群:号" -> 点了没回的时间
        self.missed: dict[str, float] = {}
        # 号 -> 拒绝到什么时候
        self.blocked: dict[str, float] = {}
        # 群 -> 第一次见到它的时间（新群先认人，别一进来就点名）
        self.first_seen: dict[str, float] = {}
        self._load()

    # ---------- 状态存取 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self.groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
        self.people = raw.get("people") if isinstance(raw.get("people"), dict) else {}
        self.missed = raw.get("missed") if isinstance(raw.get("missed"), dict) else {}
        self.blocked = raw.get("blocked") if isinstance(raw.get("blocked"), dict) else {}
        self.first_seen = (
            raw.get("first_seen") if isinstance(raw.get("first_seen"), dict) else {}
        )
        self._prune(time.time())

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "groups": self.groups,
                        "people": self.people,
                        "missed": self.missed,
                        "blocked": self.blocked,
                        "first_seen": self.first_seen,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.debug("@ 状态存盘失败：%s", exc)

    def _prune(self, now: float) -> None:
        """把过期的记录清掉，别让状态文件越滚越大。"""
        for gid, state in list(self.groups.items()):
            marks = [t for t in (state.get("marks") or []) if now - t < self.HOUR]
            if marks:
                state["marks"] = marks
            else:
                state.pop("marks", None)
            if not state:
                self.groups.pop(gid, None)
        for key, marks in list(self.people.items()):
            kept = [t for t in marks if now - t < self.DAY]
            if kept:
                self.people[key] = kept
            else:
                self.people.pop(key, None)
        for key, when in list(self.missed.items()):
            if self.miss_ignore and now - when > self.miss_ignore:
                self.missed.pop(key, None)
        for uid, until in list(self.blocked.items()):
            if until <= now:
                self.blocked.pop(uid, None)

    # ---------- 记录 ----------

    def note_group_seen(self, group_id: str, now: float | None = None) -> None:
        """第一次见到这个群时记一笔（冷启动/刚被拉进群都算），给 warmup 用。"""
        gid = str(group_id or "")
        if not gid or gid in self.first_seen:
            return
        self.first_seen[gid] = now if now is not None else time.time()
        self.save()

    def note_spoke(self, group_id: str, uid: Any) -> None:
        """这个人说话了：如果他身上挂着"点了没回"，就把它解掉。"""
        key = f"{group_id}:{uid}"
        if key in self.missed:
            self.missed.pop(key, None)
            self.save()

    def note_sent(
        self,
        group_id: str,
        uid: Any,
        now: float | None = None,
        *,
        proactive: bool = True,
    ) -> None:
        """真的 @ 出去了：记账，并挂上"等回应"。

        `proactive=False`（回话 / 链里指代）只挂"点了没回"，**不占**同群冷却、
        每小时上限、每人每天这几本账 —— 那是给"她主动去点人"记的。

        以前一本账记所有 @：一场对话里来回点七八次，当天的「每人每天」额度就
        用光了，后面明明在接话却点不出去，降级成纯文字（2026-11-10 实锤：
        同一个对话里 `今天点他 8 次了` 挡了一整晚的 @）。对话里的 @ 本来就该
        一人一次、等对方应声，别让"总次数"把一整场聊天掐断。
        """
        now = now if now is not None else time.time()
        gid, ukey = str(group_id), str(uid)
        if proactive:
            state = self.groups.setdefault(gid, {})
            state["last"] = now
            marks = [t for t in (state.get("marks") or []) if now - t < self.HOUR]
            marks.append(now)
            state["marks"] = marks
            key = f"{gid}:{ukey}"
            self.people[key] = [
                t for t in self.people.get(key, []) if now - t < self.DAY
            ] + [now]
        if self.miss_ignore:
            self.missed[f"{gid}:{ukey}"] = now
        self.save()

    def block(self, uid: Any, now: float | None = None) -> None:
        """对方说过「别 @ 我」：记下来，这段时间里不再点他。"""
        now = now if now is not None else time.time()
        self.blocked[str(uid)] = now + self.REFUSE_DAYS * self.DAY
        self.save()

    def blocked_until(self, uid: Any) -> float:
        return float(self.blocked.get(str(uid)) or 0.0)

    # ---------- 判定 ----------

    def allow(
        self,
        group_id: str,
        uid: Any,
        role: str = "",
        now: float | None = None,
        last_spoke: float | None = None,
        is_self: bool = False,
        is_owner: bool = False,
        in_chain: bool = False,
        reply_back: bool = False,
    ) -> tuple[bool, str]:
        """能不能 @ 这个人。返回 (放不放行, 原因)；原因会进日志，写人话。

        `reply_back`：他刚 @ / 引用 / 叫了她，这句是**回话**。群冷却、小时上限、
        新群预热、群主/管理员的限制都不拦 —— 一群人先后找她，不能只回
        第一个；回一句也不会让群主以为出了事。个人冷却留着（一对一互相 @ 由它挡）。

        主人是例外，**哪一档都不点**：@ 段显示群名片，会把「主人」这个称呼顶掉。

        `in_chain`：这一轮好几个人在说话，@ 是**指代**。链里不数次数。两个都为真时
        按回话算（他先找的她）。

        共同点：**对话里的 @ 不占「每人每天」额度**。那一档防的是"她主动去点
        同一个人十几次"；一整场对话里来回点，本来就是一人一次、等对方应声，
        再拿总次数卡，就会把聊天掐断（2026-11-10：同一场对话里
        `今天点他 8 次了` 挡了一整晚的 @，后半场全退成纯文字）。
        """
        now = now if now is not None else time.time()
        gid, ukey = str(group_id or ""), str(uid or "")
        if not self.enabled:
            return False, "点名没开"
        if not gid or not ukey or not _UID_RE.match(ukey):
            return False, "号不对"
        if is_self:
            return False, "不点自己"
        # 主人永远不点：@ 段显示的是群名片，会把「主人」这个叫法顶掉 ——
        # 2026-10-27 主人反馈「@ 之后直接叫昵称，连主人都不叫了」。
        # 「主人」这个词本身就是称呼 + 指代，用不着点名。
        if is_owner:
            return False, "不点主人"
        if role in (ROLE_OWNER, ROLE_ADMIN) and not reply_back:
            # 群主和管理员被机器人 @ 一下，会以为出了事；而且他们真有权限，
            # 点错一次代价比点错群友大得多。他们自己 @ 她、回一句时放行。
            return False, "不点群主/管理员"
        if self.blocked_until(ukey) > now:
            return False, "他说过别@他"

        first = float(self.first_seen.get(gid) or 0.0)
        if self.warmup and first and now - first < self.warmup and not reply_back:
            left = int((self.warmup - (now - first)) / 3600) + 1
            return False, f"刚进群，先认认人（还要 {left} 小时）"

        # 点了没回：最核心的一条。他明明在里面说话，会顺手把这条解掉。
        when = float(self.missed.get(f"{gid}:{ukey}") or 0.0)
        if self.miss_ignore and when and now - when < self.miss_ignore:
            left = int((self.miss_ignore - (now - when)) / 3600) + 1
            return False, f"上次点了没回（还要 {left} 小时）"

        if self.present_seconds:
            if not last_spoke or now - float(last_spoke) > self.present_seconds:
                return False, "他这会儿没说话"

        state = self.groups.get(gid) or {}
        # 这本账只记「她主动去点」那一路：对话里的 @ 走 note_sent(proactive=False)，
        # 不写进来，所以这里看到的就是真·主动点名的次数。
        marks = [t for t in self.people.get(f"{gid}:{ukey}", []) if now - t < self.DAY]

        if in_chain:
            # 链里 @ 是在说"这句接谁的"：群冷却、小时上限、每人每天都不该拦
            return True, "链里指代"

        if reply_back:
            # 回话不再卡"同一人 30 分钟"：他刚说完、她在接，这就是对话该有的样子。
            # 用 30 分钟去限一场来回聊，结果就是「开头 @ 了两句，后面全退成文字」
            # —— 2026-10-27 实测日志里全是被这条挡掉的（同一个人还要等 1700 秒）。
            # 防刷屏交给 `missed`：点了他、他没再开口，就不再点（他再说话自动解开）。
            return True, "回@他"

        # 主动点名：只在这条路上数次数
        if self.person_daily_cap and len(marks) >= self.person_daily_cap:
            return False, f"今天点他 {len(marks)} 次了"
        last_group = float(state.get("last") or 0.0)
        if self.group_cooldown and last_group and now - last_group < self.group_cooldown:
            left = int(self.group_cooldown - (now - last_group))
            return False, f"本群刚点过，还要等 {left} 秒"
        hour_marks = [t for t in (state.get("marks") or []) if now - t < self.HOUR]
        if self.group_hourly_cap and len(hour_marks) >= self.group_hourly_cap:
            return False, f"本小时点过 {len(hour_marks)} 次了"

        if self.person_cooldown and marks and now - max(marks) < self.person_cooldown:
            left = int(self.person_cooldown - (now - max(marks)))
            return False, f"同一个人还要等 {left} 秒"

        return True, "可以用"

    def describe(self, group_id: str = "", now: float | None = None) -> str:
        """回执里给她/主人看的一句话状态。"""
        now = now if now is not None else time.time()
        if not self.enabled:
            return "点名：关（她不会 @ 人）"
        bits = [
            f"主动点名：同群隔 {self.group_cooldown // 60} 分钟、每小时最多 "
            f"{self.group_hourly_cap} 次、同一个人隔 {self.person_cooldown // 60} 分钟、"
            f"每人每天最多 {self.person_daily_cap} 次",
            "回@ 他和链里指代不占这些额度（他没再开口就不再点）",
            f"点了没回 {int(self.miss_ignore / 3600)} 小时内不再点",
        ]
        if group_id:
            marks = [
                t
                for t in ((self.groups.get(str(group_id)) or {}).get("marks") or [])
                if now - t < self.HOUR
            ]
            bits.append(f"本群这一小时主动点了 {len(marks)} 次")
        return "点名：开（" + "；".join(bits) + "）"
