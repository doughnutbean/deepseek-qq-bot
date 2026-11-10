"""群友自己的权限：个人免打扰、记忆自助、称呼、本群推送、好友上线通知。

三条边界，改之前先看：

1. **只作用于本人 / 本群**。全局档位、表情、主动、思考、黑名单、推送总开关
   仍然只归主人；这里不碰。
2. **隐私不设门槛**。看/删关于自己的记忆是本人的权利，0 门槛；
   熟悉度只解锁两个低风险动作：称呼（20 / 眼熟，且来过 2 天）、
   本群推送（60 / 熟人，且来过 10 天）。好感为负时不给解锁。
3. **全部本地整句匹配，0 token**。不调模型判断、不让模型执行；
   认不出就当普通聊天。

记忆的查看/删除只在私聊做：群里念出来等于把一个人的事实卡片公开给全群。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .commands import parse as parse_command
from .commands import parse_duration_minutes
from .familiar import Person, TIERS

log = logging.getLogger(__name__)

# 个人免打扰：默认 30 分钟，最短 5 分钟，最长 2 小时（别变成永久静音）
QUIET_DEFAULT_MINUTES = 30
QUIET_MIN_MINUTES = 5
QUIET_MAX_MINUTES = 120

# 熟悉度门槛（0~100）+ 至少来过几天。只给低风险动作，不拿它当安全边界。
NICKNAME_MIN_FAVOR = 20   # 眼熟
# 8 句 / 1 天就能到 20；只看分数的话刷一下午就有，所以再加"来过两天"。
NICKNAME_MIN_DAYS = 2
GROUP_PUSH_MIN_FAVOR = 60  # 熟人
GROUP_PUSH_MIN_DAYS = 10
# 最近一次说话超过这么久，不再算"还在处"
STALE_DAYS = 30

# 称呼长度上限（和主人的 owner_title 一样短，免得一句话全是名字）
NICKNAME_MAX_CHARS = 8

UNLOCKS: tuple[tuple[str, int, str], ...] = (
    ("nickname", NICKNAME_MIN_FAVOR, "称呼"),
    ("group_push", GROUP_PUSH_MIN_FAVOR, "本群上下线推送"),
)

_TAIL = "。！？!?~～…，,.；;、 \t"
_NICK_TAILS = (
    "好吗", "行吗", "可以吗", "可以么", "好不好", "行不行", "成吗", "好么",
    "就行", "就好", "即可", "好了",
    "吧", "啊", "哦", "呀", "嘛", "呢", "了", "哈", "呗", "啦",
)
_NICK_RESERVED = (
    "主人", "群主", "管理员", "官方", "本鱼", "大肥鱼", "机器人", "系统", "全体",
)
_NICK_BAD_CHARS = set("【】[]{}<>@/\\|`~\n\r\t")
# 「干嘛 / 干什么 / 干啥」是问「叫我有什么事」，不是起名 —— 以前只挡了「什么」，
# 群友随口一句「叫我干嘛」就把称呼设成了「干嘛」（2026-10-27 实锤）。
_NICK_QUESTION = (
    "什么", "啥", "谁", "哪", "怎么", "多少", "干嘛", "干吗", "干啥", "做啥", "弄啥",
)
_NICK_EMPTY_WORDS = {"一声", "一下", "一遍", "我", "你"}
# 「叫我起来 / 叫我吃饭」是日常话，不是起名：认不出就当聊天，别回「名字不行」
_NICK_AMBIGUOUS = {"起来", "醒醒", "起床", "吃饭", "过来", "出去", "停下", "别"}
# 「叫我一声吧」被正则吞成「一声」+「吧」时，只剩语气词也不是名字
_NICK_BARE_PARTICLE = re.compile(r"^[吧啊呀嘛呗啦哦呢么了哈捏喔咯诶]+$")

_MEMORY_LIST = frozenset(
    {
        "你记得我什么",
        "你记得我啥",
        "你记了我什么",
        "看看你记得我什么",
        "我的记忆",
        "我的记忆卡片",
    }
)
_MEMORY_DELETE = re.compile(
    r"^(?:忘掉|删掉|删除|去掉)\s*(?:关于我的)?\s*第?\s*(?P<index>\d{1,2})\s*条"
    r"(?:记忆|事实)?$"
)
_MEMORY_FORGET_ALL = frozenset(
    {
        "忘掉关于我的全部",
        "忘掉关于我的全部记忆",
        "忘掉我的一切",
        "清空关于我的记忆",
        "删除关于我的全部记忆",
        "删除关于我的全部",
    }
)
_MEMORY_CONFIRM = frozenset(
    {
        "确认忘掉我",
        "确认忘掉关于我的全部",
        "确认删除关于我的全部",
        "确认清空我的记忆",
    }
)
_NO_MEMORY_ON = frozenset(
    {"别记我", "不要记我", "别记我的事", "以后别记我", "不用记我"}
)
_NO_MEMORY_OFF = frozenset({"可以记我", "可以记我了", "记我吧", "恢复记我"})

_QUIET_SET = re.compile(r"^别接我(?:的)?话(?:\s*(?P<dur>.+))?$")
_QUIET_OFF = frozenset(
    {"可以接我了", "可以接我话", "可以接我的话了", "接我话吧", "继续接我话"}
)

# 好友上线通知：本人私聊里自助开/关。这里只认句子；
# "是不是 QQ 好友"在 app.py 里查好友列表，非好友会回一句拒绝。
# 只发上线，不发下线；名字里带"上线"就够了，别和群聊的"上下线推送"混在一起。
_PRESENCE_DM = re.compile(
    r"^(?:(?P<on>打开|开启|订阅|启用|恢复)|(?P<off>关闭|关掉|取消|退订|停止|不用|不要|别))"
    r"\s*(?:订阅)?\s*(?:私聊)?\s*上线\s*(?:的)?\s*(?:通知|提醒)"
    r"\s*(?:我)?\s*(?:吧|啊|呀|嘛|呗|啦|哦)?$"
)

_NICK_CLEAR = frozenset(
    {"还是叫昵称", "还是叫我昵称", "叫回昵称", "取消称呼", "别叫我外号", "不要叫我外号"}
)
_NICK_CLEAR_RE = re.compile(r"^(?:以后)?\s*(?:别|不要|不用)\s*叫我.*$")
_NICK_SET = re.compile(
    r"^(?:以后|请)?\s*(?:叫我|称呼我(?:为)?|喊我)\s*(?:一声)?\s*(?P<name>.+)$"
)

_RIGHTS = frozenset(
    {"我的权限", "我能用什么", "解锁了什么", "权限列表", "我的功能"}
)


def _body(text: str) -> str:
    """去掉首尾标点、压空白；后面所有规则都看这个。"""
    body = (text or "").strip().strip(_TAIL)
    return re.sub(r"\s+", " ", body).strip()


# ---------- 熟悉度/好感解锁 ----------

def _score(person: Person | None) -> int:
    return person.familiarity if person is not None else 0


def _fmt_affection(value: float | int) -> str:
    """好感是浮点（疲劳会入账 0.5/0.25），展示时整数就不带小数。"""
    number = float(value)
    return f"{number:+.1f}" if number != int(number) else f"{int(number):+d}"


def unlock_need(key: str) -> int:
    for item, need, _label in UNLOCKS:
        if item == key:
            return need
    return 0


def unlock_min_days(key: str) -> int:
    if key == "nickname":
        return NICKNAME_MIN_DAYS
    if key == "group_push":
        return GROUP_PUSH_MIN_DAYS
    return 0


def unlocked(
    person: Person | None,
    key: str,
    *,
    affection: float = 0.0,
    now: float | None = None,
) -> bool:
    """分数够 + 来过够天数 + 最近还在 + 好感不为负。"""
    need = unlock_need(key)
    if need <= 0:
        return True
    if person is None:
        return False
    if person.familiarity < need:
        return False
    if person.days < unlock_min_days(key):
        return False
    moment = time.time() if now is None else float(now)
    if person.is_stale(moment, STALE_DAYS, prior=True):
        return False
    if float(affection) < 0:
        return False
    return True


def _target_tier(need: int) -> Any:
    """熟悉度门槛对应的档位：20→眼熟，60→熟人。"""
    return TIERS[min(len(TIERS) - 1, max(0, need // 20))]


def _missing(
    person: Person | None,
    key: str,
    *,
    affection: float = 0.0,
    now: float | None = None,
) -> list[str]:
    """哪几条没满足；给回执用，不阴阳。"""
    need = unlock_need(key)
    min_days = unlock_min_days(key)
    if person is None:
        target = _target_tier(need)
        return [
            f"先在群里聊 {max(target.min_msgs, 8)} 句、满 {max(target.min_days, min_days)} 天"
        ]
    reasons: list[str] = []
    if person.familiarity < need:
        reasons.append(f"熟悉度还差 {need - person.familiarity} 分")
    if person.days < min_days:
        reasons.append(f"还要来过 {min_days - person.days} 天")
    moment = time.time() if now is None else float(now)
    if person.is_stale(moment, STALE_DAYS, prior=True):
        reasons.append("最近一个月没见，先聊几句")
    if float(affection) < 0:
        reasons.append("现在好感是负的，先缓和一下")
    return reasons or ["再聊两句就差不多了"]


def denied_text(
    person: Person | None,
    key: str,
    *,
    affection: float = 0.0,
    now: float | None = None,
) -> str:
    """门槛不够时的回执：说清楚差什么。"""
    need = unlock_need(key)
    label = next((name for item, _n, name in UNLOCKS if item == key), key)
    target = _target_tier(need)
    score = _score(person)
    days = person.days if person is not None else 0
    reasons = _missing(person, key, affection=affection, now=now)
    return (
        f"🔒 「{label}」要熟悉度 {need}（{target.name}）、"
        f"来过 {unlock_min_days(key)} 天、好感不为负；"
        f"你现在 {score}/{need}、{days} 天、好感 {_fmt_affection(affection)}。"
        + "；".join(reasons)
        + "。"
    )


def rights_text(
    person: Person | None,
    *,
    affection: float = 0.0,
    now: float | None = None,
) -> str:
    """「我的权限」：一屏看完解锁了什么、还差什么。"""
    score = _score(person)
    level = person.tier.name if person is not None else "生人"
    got = [
        name
        for key, _need, name in UNLOCKS
        if unlocked(person, key, affection=affection, now=now)
    ]
    lines = [
        f"🔓 你在本鱼这儿：熟悉 {score}/100（{level}）｜好感 {_fmt_affection(affection)}",
        "已解锁：" + ("、".join(got) if got else "（还没有）"),
    ]
    for key, _need, label in UNLOCKS:
        if unlocked(person, key, affection=affection, now=now):
            continue
        reasons = _missing(person, key, affection=affection, now=now)
        lines.append(f"还差「{label}」：" + "；".join(reasons))
    lines.append("好感为负会挡住解锁，聊回来就好；不用门槛的：别接我的话、记忆自助")
    return "\n".join(lines)


# ---------- 称呼 ----------

def clean_nickname(value: str) -> str:
    """去掉引号/尾巴，只留名字本身。"""
    name = (value or "").strip().strip(" 。！？!?~～…，,.；;、\"'“”‘’「」")
    while True:
        for tail in _NICK_TAILS:
            if name.endswith(tail) and len(name) > len(tail):
                name = name[: -len(tail)].rstrip()
                break
        else:
            break
    return name.strip()


def validate_nickname(value: str) -> tuple[str, str]:
    """返回 (清洗后的名字, 错误原因)。错误原因为空串表示可以用。"""
    name = clean_nickname(value)
    if not name:
        return "", "没听清你要本鱼叫你什么"
    if name in _NICK_EMPTY_WORDS:
        return "", "这个名字本鱼叫不出口，换一个"
    if len(name) > NICKNAME_MAX_CHARS:
        return "", f"名字太长啦（最多 {NICKNAME_MAX_CHARS} 个字）"
    if any(ch in _NICK_BAD_CHARS for ch in name):
        return "", "这个名字里有符号，本鱼叫不出口"
    if any(word in name for word in _NICK_RESERVED) or name.upper() == "AI":
        return "", "「主人 / 群主 / 管理员」这类称呼不能给你用，换一个"
    if name.isdigit():
        return "", "纯数字本鱼记不住，换一个"
    return name, ""


# ---------- 本地解析 ----------

def parse_member_permission(text: str) -> tuple[str, dict] | None:
    """群友自己的权限命令。只认整句，认不出返回 None（当普通聊天）。"""
    body = _body(text)
    if not body:
        return None

    # 记忆：先于别的规则，免得「忘掉第 2 条」被当成聊天
    if body in _MEMORY_LIST:
        return "memory_list", {}
    match = _MEMORY_DELETE.match(body)
    if match:
        return "memory_delete", {"index": int(match.group("index"))}
    if body in _MEMORY_FORGET_ALL:
        return "memory_forget_all", {}
    if body in _MEMORY_CONFIRM:
        return "memory_confirm", {}
    if body in _NO_MEMORY_ON:
        return "no_memory_on", {}
    if body in _NO_MEMORY_OFF:
        return "no_memory_off", {}

    # 个人免打扰
    match = _QUIET_SET.match(body)
    if match:
        raw = (match.group("dur") or "").strip()
        minutes = parse_duration_minutes(raw) if raw else QUIET_DEFAULT_MINUTES
        if minutes is None:
            minutes = QUIET_DEFAULT_MINUTES
        minutes = max(QUIET_MIN_MINUTES, min(QUIET_MAX_MINUTES, int(minutes)))
        return "quiet_on", {"minutes": minutes}
    if body in _QUIET_OFF:
        return "quiet_off", {}

    # 好友上线通知：先认这个，别被后面的群推送规则抢走。
    # 这里只判断句子；是不是好友、私聊还是群聊，都在 app.py 里处理。
    match = _PRESENCE_DM.match(body)
    if match:
        return ("presence_dm_on" if match.group("on") else "presence_dm_off"), {}

    # 称呼：取消在前，起名在后（「别叫我老板」不能反手设成老板）
    if body in _NICK_CLEAR or _NICK_CLEAR_RE.match(body):
        return "nickname_clear", {}
    match = _NICK_SET.match(body)
    if match:
        raw = match.group("name").strip()
        # 先按起名规矩洗净尾巴：「叫我一声吧」不能把「吧」当成名字
        name = clean_nickname(raw)
        # 疑问词要在**剥尾巴之前**看 —— 「干嘛」的「嘛」会被当语气词剥掉，
        # 只剩一个「干」就成了名字（2026-10-27 实锤：群里说「叫我干嘛」被设成称呼）
        if any(word in raw for word in _NICK_QUESTION) or any(
            word in name for word in _NICK_QUESTION
        ):
            # 「叫我什么来着 / 叫我干嘛」是在问她，不是起名
            return None
        if not name or name in _NICK_EMPTY_WORDS or name in _NICK_AMBIGUOUS:
            # 「叫我起来」「叫我一声」是日常话，不是起名
            return None
        if _NICK_BARE_PARTICLE.fullmatch(name):
            # 「叫我一声吧」「叫我一下啊」：只剩语气词，也不是名字
            return None
        return "nickname_set", {"name": name}

    # 本群推送：直接复用主人那边已经验过的 parse()，只放行 group_push
    got = parse_command(body)
    if got is not None:
        if got[0] == "group_push":
            return "group_push", got[1]
        if got[0] == "presence_push_all":
            return "group_push_global", {}

    if body in _RIGHTS:
        return "rights", {}
    return None


# ---------- 状态文件 ----------

_DEFAULTS: dict[str, dict[str, Any]] = {
    "quiet": {},
    "nicknames": {},
    "no_memory": {},
}


class MemberState:
    """群友自己的状态：按「群:人」隔离的免打扰/称呼，按人隔离的记忆开关。

    存 `data/member_state.json`，和主人的 `prefs.json` 分开：
    主人说「恢复正常」不该把群友的「别记我」一起清掉。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = {key: {} for key in _DEFAULTS}
        self.load()

    @staticmethod
    def _key(group_id: Any, uid: Any) -> str:
        return f"{group_id}:{uid}"

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.data = {}
        for section, default in _DEFAULTS.items():
            value = raw.get(section)
            self.data[section] = dict(value) if isinstance(value, dict) else dict(default)
        self.prune()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("群友权限状态写不进去：%s", exc)

    def prune(self, now: float | None = None) -> None:
        """只清过期的免打扰；称呼和「别记我」不过期。"""
        moment = time.time() if now is None else now
        quiet = self.data.setdefault("quiet", {})
        for key, entry in list(quiet.items()):
            try:
                until = float((entry or {}).get("until") or 0)
            except (TypeError, ValueError):
                until = 0.0
            if not until or until <= moment:
                quiet.pop(key, None)

    # ---------- 免打扰 ----------

    def quiet_until(self, group_id: Any, uid: Any) -> float:
        key = self._key(group_id, uid)
        entry = self.data.get("quiet", {}).get(key)
        if not isinstance(entry, dict):
            return 0.0
        try:
            until = float(entry.get("until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until and until <= time.time():
            self.data["quiet"].pop(key, None)
            self.save()
            return 0.0
        return until

    def quiet(self, group_id: Any, uid: Any) -> bool:
        return self.quiet_until(group_id, uid) > time.time()

    def set_quiet(self, group_id: Any, uid: Any, minutes: int) -> float:
        minutes = max(QUIET_MIN_MINUTES, min(QUIET_MAX_MINUTES, int(minutes)))
        until = time.time() + minutes * 60
        self.data.setdefault("quiet", {})[self._key(group_id, uid)] = {
            "until": until,
            "ts": time.time(),
        }
        self.save()
        return until

    def clear_quiet(self, group_id: Any, uid: Any) -> bool:
        key = self._key(group_id, uid)
        had = self.data.setdefault("quiet", {}).pop(key, None) is not None
        if had:
            self.save()
        return had

    # ---------- 称呼 ----------

    def nickname(self, group_id: Any, uid: Any) -> str:
        entry = self.data.get("nicknames", {}).get(self._key(group_id, uid))
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("name") or "")

    def set_nickname(self, group_id: Any, uid: Any, name: str) -> None:
        self.data.setdefault("nicknames", {})[self._key(group_id, uid)] = {
            "name": name,
            "ts": time.time(),
        }
        self.save()

    def clear_nickname(self, group_id: Any, uid: Any) -> bool:
        key = self._key(group_id, uid)
        had = self.data.setdefault("nicknames", {}).pop(key, None) is not None
        if had:
            self.save()
        return had

    # ---------- 记忆开关 ----------

    def no_memory(self, uid: Any) -> bool:
        return str(uid) in self.data.get("no_memory", {})

    def set_no_memory(self, uid: Any, enabled: bool) -> bool:
        key = str(uid)
        if enabled:
            self.data.setdefault("no_memory", {})[key] = {"ts": time.time()}
        else:
            self.data.setdefault("no_memory", {}).pop(key, None)
        self.save()
        return bool(enabled)
