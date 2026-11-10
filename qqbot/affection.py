"""好感度：她跟某个人处得怎么样。和「熟悉度」分开。

熟悉度（`familiar.py`）看聊了多久、多少句，只涨不跌；
好感度看**最近怎么相处**，会升会降，存 `data/affection.json`。

设计约束：

- **纯本地、0 token**：不调模型判断"好人/坏人"，只吃本地事件
  （认真哄、硬拒、被问烦、广告骚扰、删好友、接话、夸她）。
- **没有硬性日上限，但有疲劳**：每天前 `free_gain` 点全额；
  超过之后每 `fatigue_step` 点衰减一半（0.5× → 0.25× → …），
  最低 `min_multiplier`。负面同理，但免费额度更高（`free_loss`）。
- **总分有护栏，不是"每天清零"**：-`score_max` ~ +`score_max`
  （默认 ±30）。到顶后不会再涨，但前面的疲劳会让"刷满"很慢。
- **会衰减**：14 天没有直接互动，向 0 靠 1 点；只影响好感，
  不影响熟悉度。记仇不会记一辈子，喜欢也不会放一辈子。
- **不把数字塞进 prompt**：动态上下文只拿行为提示
  （「印象不错：可以多接一句」），数字只给查询回执和 CLI。

事件只认「冲她说的」：私聊、@ 她、触发词。群里别人自己聊天不算，
和 `mood.py` 同一套口径。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger(__name__)

MAX_USERS = 1000
# 总分护栏：到顶不再涨，但正常相处也够走很久
SCORE_MAX = 30
# 每日免费额度：前这么多点全额，之后开始疲劳
FREE_GAIN = 5
FREE_LOSS = 10
# 超过免费额度后，每 fatigue_step 点，增量乘一次 fatigue_factor
FATIGUE_STEP = 3
FATIGUE_FACTOR = 0.5
MIN_MULTIPLIER = 0.05
DECAY_DAYS = 14
PRUNE_DAYS = 365

# (最低分, 名字, 给模型的行为提示)。从高到低，最后一条兜底。
# 阈值按 ±30 的尺度铺开；旧 ±10 的分数不需要迁移，只是档位含义变宽。
LABELS: tuple[tuple[int, str, str], ...] = (
    (21, "很喜欢", "可以黏一点、主动找他"),
    (12, "不错", "可以多接一句、开个小玩笑"),
    (3, "还行", "正常接"),
    (-5, "平常", ""),
    (-14, "略有不快", "句子短一点，别主动贴上去"),
    (-23, "有点别扭", "别主动找他，能短则短"),
    (-30, "记仇", "能短则短，别撒娇"),
)

_REASONS = {
    "reply_back": "接话",
    "soothe": "认真哄",
    "praise": "夸她/道谢",
    "annoy": "问烦了",
    "refuse": "硬拒",
    "harass_warn": "广告/骚扰警告",
    "harass_ban": "骚扰拉黑",
    "delete_friend": "被删好友",
    "reset": "主人清零",
}


def _label_for(score: int) -> tuple[str, str]:
    for threshold, name, hint in LABELS:
        if score >= threshold:
            return name, hint
    return "记仇", "能短则短，别撒娇"


def _signed(value: int) -> str:
    return f"{value:+d}"


class Affection:
    def __init__(
        self,
        path: Path | str,
        *,
        enabled: bool = True,
        free_gain: float = FREE_GAIN,
        free_loss: float = FREE_LOSS,
        fatigue_step: int = FATIGUE_STEP,
        fatigue_factor: float = FATIGUE_FACTOR,
        min_multiplier: float = MIN_MULTIPLIER,
        decay_days: int = DECAY_DAYS,
        score_max: int = SCORE_MAX,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.free_gain = max(0.0, float(free_gain))
        self.free_loss = max(0.0, float(free_loss))
        self.fatigue_step = max(1, int(fatigue_step))
        self.fatigue_factor = max(0.01, min(1.0, float(fatigue_factor)))
        self.min_multiplier = max(0.0, min(1.0, float(min_multiplier)))
        self.decay_days = max(1, int(decay_days))
        self.score_max = max(1, int(score_max))
        self.score_min = -self.score_max
        self._data: dict[str, Any] = {"users": {}}
        self.load()

    # ---------- 落盘 ----------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        users = raw.get("users") if isinstance(raw, dict) else None
        self._data = {"users": dict(users) if isinstance(users, dict) else {}}
        self._prune()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("好感度状态写不进去：%s", exc)

    # ---------- 内部 ----------

    @staticmethod
    def _today(now: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(float(now)))

    def _user(self, uid: Any, create: bool = False) -> dict[str, Any] | None:
        users = self._data.setdefault("users", {})
        key = str(uid)
        user = users.get(key)
        if user is None and create:
            if len(users) >= MAX_USERS:
                self._trim_users(MAX_USERS - 1)
            user = {
                "score": 0.0,
                "last_talk": 0.0,
                "last_event": "",
                "last_event_ts": 0.0,
                "daily": {},
            }
            users[key] = user
        return user if isinstance(user, dict) else None

    def _reset_daily(self, user: dict[str, Any], now: float) -> dict[str, Any]:
        daily = user.get("daily")
        if not isinstance(daily, dict):
            daily = {}
            user["daily"] = daily
        today = self._today(now)
        if daily.get("day") != today:
            daily.clear()
            daily.update({"day": today, "gain": 0.0, "loss": 0.0, "marks": {}})
        daily.setdefault("gain", 0.0)
        daily.setdefault("loss", 0.0)
        daily.setdefault("marks", {})
        return daily

    def _decayed(self, user: dict[str, Any], now: float) -> float:
        try:
            score = float(user.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if not score:
            return 0.0
        try:
            last = float(user.get("last_talk") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        if not last:
            return score
        steps = int(max(0.0, float(now) - last) // (self.decay_days * 86400))
        if steps <= 0:
            return score
        if score > 0:
            return max(0.0, score - steps)
        return min(0.0, score + steps)

    def _fatigue(self, spent: float, free: float) -> float:
        """今天已经拿了多少之后，这一次还能按几折入账。

        前 `free` 点全额；超过后每 `fatigue_step` 点再乘一次
        `fatigue_factor`，最低 `min_multiplier`。不是硬上限：
        再刷也还能涨，只是越来越慢。
        """
        if spent < free:
            return 1.0
        over = max(0.0, spent - free)
        level = int(over // self.fatigue_step) + 1
        return max(self.min_multiplier, self.fatigue_factor ** level)

    def _clamp(self, value: float) -> float:
        return max(float(self.score_min), min(float(self.score_max), value))

    def _trim_users(self, keep: int) -> None:
        users = self._data.get("users") or {}
        if len(users) <= keep:
            return
        ranked = sorted(
            users.items(),
            key=lambda kv: float((kv[1] or {}).get("last_talk") or 0.0),
        )
        for key, _value in ranked[: max(0, len(users) - keep)]:
            users.pop(key, None)

    def _prune(self, now: float | None = None) -> None:
        moment = time.time() if now is None else float(now)
        users = self._data.get("users") or {}
        for key, user in list(users.items()):
            try:
                last = float((user or {}).get("last_talk") or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            if last and moment - last > PRUNE_DAYS * 86400:
                users.pop(key, None)
        self._trim_users(MAX_USERS)

    # ---------- 对外 ----------

    def raw_score(self, uid: Any, now: float | None = None) -> float:
        """当前有效好感（含衰减），浮点；测试/调参看这个。"""
        if not self.enabled:
            return 0.0
        user = self._user(uid)
        if user is None:
            return 0.0
        moment = time.time() if now is None else float(now)
        return self._clamp(self._decayed(user, moment))

    def score(self, uid: Any, now: float | None = None) -> int:
        """给展示用的整数好感（四舍五入）。"""
        return int(round(self.raw_score(uid, now)))

    def label(self, uid: Any, now: float | None = None) -> tuple[str, str]:
        return _label_for(self.score(uid, now))

    def summary(self, uid: Any, now: float | None = None) -> dict[str, Any]:
        """查询回执/动态上下文用的一小包。"""
        moment = time.time() if now is None else float(now)
        raw = self.raw_score(uid, moment)
        score = int(round(raw))
        name, hint = _label_for(score)
        user = self._user(uid)
        last = 0.0
        if user is not None:
            try:
                last = float(user.get("last_talk") or 0.0)
            except (TypeError, ValueError):
                last = 0.0
        return {
            "score": score,
            "raw": round(raw, 2),
            "name": name,
            "hint": hint if self.enabled else "",
            "last_talk": last,
            "idle_days": int((moment - last) // 86400) if last else 0,
            "enabled": self.enabled,
            "score_max": self.score_max,
        }

    def touch(self, uid: Any, now: float | None = None) -> None:
        """一次直接互动：刷新衰减计时；没有分数的人不建档。"""
        if not self.enabled:
            return
        user = self._user(uid)
        if user is None:
            return
        moment = time.time() if now is None else float(now)
        user["last_talk"] = moment
        self._reset_daily(user, moment)
        self._save()

    def award(
        self,
        uid: Any,
        delta: float,
        reason: str = "",
        *,
        now: float | None = None,
        once_per_day: bool = False,
        force: bool = False,
    ) -> float:
        """加/减好感，返回实际变化（受疲劳、总分护栏影响）。

        正面没有硬日上限：超过 `free_gain` 后每次增量按疲劳系数打折；
        负面同理，但免费额度是 `free_loss`。`force=True`（拉黑/删好友）
        绕过疲劳和免费额度，只受总分护栏限制。
        """
        if not self.enabled or not delta or uid is None:
            return 0.0
        moment = time.time() if now is None else float(now)
        user = self._user(uid, create=True)
        if user is None:
            return 0.0
        daily = self._reset_daily(user, moment)
        effective = self._decayed(user, moment)
        value = float(delta)
        daily_gain = float(daily.get("gain") or 0.0)
        daily_loss = float(daily.get("loss") or 0.0)
        applied = 0.0

        if value > 0:
            if once_per_day and (daily.get("marks") or {}).get(reason):
                return 0.0
            mult = self._fatigue(daily_gain, self.free_gain)
            applied = round(value * mult, 2)
            if applied <= 0:
                user["last_talk"] = moment
                self._save()
                return 0.0
            daily["gain"] = round(daily_gain + applied, 2)
            if once_per_day:
                daily.setdefault("marks", {})[reason] = True
        else:
            if force:
                applied = -abs(value)
            else:
                mult = self._fatigue(daily_loss, self.free_loss)
                applied = -round(abs(value) * mult, 2)
            if applied >= 0:
                user["last_talk"] = moment
                self._save()
                return 0.0
            daily["loss"] = round(daily_loss + abs(applied), 2)

        new_score = self._clamp(effective + applied)
        actual = round(new_score - effective, 2)
        user["score"] = new_score
        user["last_talk"] = moment
        user["last_event"] = reason
        user["last_event_ts"] = moment
        self._save()
        if actual:
            log.info(
                "【好感】%s %+.2f（%s）→ 当前 %+.1f/%.0f",
                uid,
                actual,
                _REASONS.get(reason, reason or "互动"),
                new_score,
                float(self.score_max),
            )
        return actual

    def penalize(self, uid: Any, delta: float, reason: str = "") -> float:
        """重罚：绕过疲劳和免费额度（拉黑/删好友这种一次定性的）。"""
        return self.award(uid, -abs(float(delta)), reason, force=True)

    def reset(self, uid: Any) -> bool:
        """主人纠偏：把这个人的好感清零。"""
        user = self._user(uid)
        if user is None:
            return False
        user["score"] = 0.0
        user["daily"] = {}
        user["last_talk"] = time.time()
        user["last_event"] = "reset"
        user["last_event_ts"] = time.time()
        self._save()
        log.info("【好感】主人清零：%s", uid)
        return True

    def stats(self) -> dict[str, Any]:
        users = self._data.get("users") or {}
        now = time.time()
        rows = []
        for key in users:
            score = self.score(key, now)
            name, _hint = _label_for(score)
            rows.append({"uid": key, "score": score, "name": name})
        rows.sort(key=lambda r: (-r["score"], r["uid"]))
        return {"users": len(users), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    args = list(argv if argv is not None else sys.argv[1:])
    path = DATA_DIR / "affection.json"
    aff = Affection(path)

    if args and args[0] == "--reset" and len(args) >= 2:
        ok = aff.reset(args[1])
        print("已清零" if ok else "没有这个人的记录")
        return 0 if ok else 1
    if args and args[0] == "--set" and len(args) >= 3:
        try:
            target = max(aff.score_min, min(aff.score_max, int(args[2])))
        except ValueError:
            print(f"分数要是 {aff.score_min}~{aff.score_max} 的整数")
            return 1
        user = aff._user(args[1], create=True)
        assert user is not None
        user["score"] = float(target)
        user["last_talk"] = time.time()
        user["last_event"] = "manual"
        user["last_event_ts"] = time.time()
        aff._save()
        print(f"已设为 {target:+d}")
        return 0

    stats = aff.stats()
    print(
        f"好感度记录：{stats['users']} 人（{path}；范围 {aff.score_min}~{aff.score_max}）\n"
    )
    print(f'{"好感":>6}{"档位":<8}{"QQ"}')
    for row in stats["rows"][:30]:
        print(f'{row["score"]:>+6}{row["name"]:<8}{row["uid"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
