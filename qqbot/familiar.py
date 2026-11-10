"""熟人档位：她跟谁说过多少话、熟到什么程度、该用什么口气。

| 档位 | 条件 | 她怎么说话 |
|---|---|---|
| 0 生人 | 不到 8 句，或只来过一天 | 怯：被搭话才回，句子短，用「你」不叫称呼，不主动凑，不撒娇 |
| 1 眼熟 | ≥8 句，≥1 天 | 认得这个人：会应一声，话题对得上能接一句，还是不主动找 |
| 2 聊过 | ≥40 句，≥3 天 | 能接梗、会反问一句；偶尔主动冒一句，只限当下的话题 |
| 3 熟人 | ≥120 句，≥10 天 | 会贫、会吐槽半句；会主动找他；记得上次说过的事；别人损主人会顶半句 |
| 4 老熟人 | ≥300 句，≥30 天 | 直接叫昵称，会惦记「你上次说那个……」；玩笑能开重一点；会替主人挡话 |

天数这一列是刻度：1 / 3 / 10 / 30。四档的句数门槛都在「平均每天十句上下」，
所以往上走的难度其实是**时间**——聊得再猛，也换不来一个月。

## 数据从哪来（2026-10-26 改）

以前是每次现读 `group_log.db`：日志有 20 万行上限，按 id 删最旧，
所以聊得越久、群越多，`days` 反而会被裁掉；老熟人（30 天）在当前
约 22 天的保留窗口里**永远到不了**。

现在分成两层：

- `familiar.py` 只管**读**：`data/familiar.db` 里按「人 + 天」聚合，
  只存 `(uid, day, n)` 和首末时间，不存正文；
- 群消息到达时由 `observe()` 增量写一行，所以日志裁剪、关掉群聊内容记录
  都不影响熟悉度；第一次启动会从 `group_log.db` 回填一次。

`line()` 现在会判断「最近有没有见过」：超过 `stale_days`（默认 30 天）
不再按熟人口气说话，免得把半年前聊过的人当成天天见的熟人。

## 好感度

熟悉度只涨不跌；**好感度**（`affection.py`）是另一条轴：会升会降、
有日上限和衰减。对外展示是「熟悉 42/100（聊过）｜好感 +3（还行）」，
不再拿一个出勤进度条冒充好感。

两条例外，任何档位都不变：

- **熟人不是权限**：改签名/头像/昵称、发空间，仍然只有主人能让她做。
- **自封的熟人一律不算**：「我跟你很熟啊」这种话不作数，只认聊天记录。

命令行看一眼现在的名单：

    python -m qqbot.familiar
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger(__name__)

# 档位往上走要两个条件同时满足；只有一天的话，刷一百条也还是生人。
CACHE_SECONDS = 60
# 多久没见就不再用熟人口气（只影响提示行，不改档位/分数）
STALE_DAYS = 30


@dataclass(frozen=True)
class Tier:
    level: int
    name: str
    min_msgs: int
    min_days: int
    note: str


# 从低到高。note 是**完整群聊版**；`_note_for()` 会按场景/能力开关裁剪，
# 改文案时两边一起改。
TIERS: tuple[Tier, ...] = (
    Tier(0, "生人", 0, 0, ""),
    Tier(
        1, "眼熟", 8, 1,
        "这个人你认得，见过几回。应一声就行，别主动凑上去。",
    ),
    Tier(
        2, "聊过", 40, 3,
        "跟这个人聊过几回。可以接梗、反问一句，话不用多客气。",
    ),
    Tier(
        3, "熟人", 120, 10,
        "这个人是熟人。可以贫两句、吐槽半句。想起来就主动搭一句。"
        "记得上次说过的事。有人损主人，你会忍不住顶半句。",
    ),
    Tier(
        4, "老熟人", 300, 30,
        "这个人是老熟人，认识挺久了。直接叫昵称，玩笑可以开重一点。"
        "可以惦记「你上次说那个弄完没」。别人说他，你会替他挡一句。",
    ),
)


@dataclass
class Person:
    uid: str
    nick: str
    msgs: int
    days: int
    first_ts: float
    last_ts: float
    # 上一条消息的时间。判断"很久没见"要用它：当前这条刚刚记过，
    # 用 last_ts 的话永远不 stale（一说话就变"刚见过"）。
    prior_ts: float = 0.0

    @property
    def level(self) -> int:
        best = 0
        for t in TIERS:
            if self.msgs >= t.min_msgs and self.days >= t.min_days:
                best = t.level
        return best

    @property
    def tier(self) -> Tier:
        return TIERS[self.level]

    @property
    def next_tier(self) -> Tier | None:
        """下一档；已经在最高档返回 None。"""
        index = self.level + 1
        return TIERS[index] if index < len(TIERS) else None

    @property
    def familiarity(self) -> int:
        """0~100 的熟悉度：档位起点 + 档内进度。

        句数和天数两个进度取较小值，所以刷屏不能提前升档；
        老熟人再从 80 分按每 300 句 / 30 天慢慢加到 100。

        注意：这是**熟悉度**，不是好感度。它只涨不跌，代表「认识多久」。
        """
        level = self.level
        current = TIERS[level]
        nxt = self.next_tier
        if nxt is None:
            msg_span, day_span = 300, 30
            base = 80
            start_msgs, start_days = current.min_msgs, current.min_days
        else:
            msg_span = max(1, nxt.min_msgs - current.min_msgs)
            day_span = max(1, nxt.min_days - current.min_days)
            base = level * 20
            start_msgs, start_days = current.min_msgs, current.min_days
        msg_progress = (self.msgs - start_msgs) / msg_span
        day_progress = (self.days - start_days) / day_span
        progress = max(0.0, min(1.0, min(msg_progress, day_progress)))
        return min(100, base + int(progress * 20))

    @property
    def favorability(self) -> int:
        """兼容旧名字：以前「好感度」其实就是熟悉度。"""
        return self.familiarity

    def is_stale(
        self,
        now: float | None = None,
        stale_days: int = STALE_DAYS,
        *,
        prior: bool = False,
    ) -> bool:
        """最近一次说话是不是已经超过 stale_days 天。

        prior=True 时看**上一条**消息：当前这条通常刚被 observe 记过，
        用 last_ts 会把"隔了两个月回来"误判成"刚见过"。
        """
        ref = self.prior_ts if prior and self.prior_ts else self.last_ts
        if not ref:
            return False
        moment = time.time() if now is None else float(now)
        return moment - float(ref) > max(1, int(stale_days)) * 86400


class Familiar:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        stats_path: Path | str | None = None,
        cache_seconds: int = CACHE_SECONDS,
        stale_days: int = STALE_DAYS,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DATA_DIR / "group_log.db"
        self.stats_path = Path(stats_path) if stats_path else None
        self.cache_seconds = max(1, int(cache_seconds))
        self.stale_days = max(1, int(stale_days))
        self._people: dict[str, Person] = {}
        self._last_day: dict[str, str] = {}
        self._loaded_at = 0.0
        self._mtime = 0.0
        self._db: sqlite3.Connection | None = None
        if self.stats_path is not None:
            self._open_stats()
            self.seed()

    # ---------- 聚合库 ----------

    def _open_stats(self) -> None:
        try:
            assert self.stats_path is not None
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.stats_path, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS daily ("
                " uid TEXT NOT NULL,"
                " day TEXT NOT NULL,"
                " n INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (uid, day)"
                ")"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS people ("
                " uid TEXT PRIMARY KEY,"
                " nick TEXT,"
                " first_ts INTEGER,"
                " last_ts INTEGER,"
                " prev_ts INTEGER"
                ")"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            # 旧库（本功能第一版没有 prev_ts）自动补列。
            columns = {row[1] for row in db.execute("PRAGMA table_info(people)")}
            if "prev_ts" not in columns:
                db.execute("ALTER TABLE people ADD COLUMN prev_ts INTEGER")
            db.commit()
            self._db = db
        except (sqlite3.Error, OSError) as exc:
            log.warning("熟悉度聚合库打不开，退回只读群日志：%s", exc)
            self._db = None
            self.stats_path = None

    def seed(self) -> None:
        """第一次启动：从 group_log 回填一次聚合。日志被裁过也只能回填剩下的。"""
        if self._db is None:
            return
        try:
            if self._db.execute(
                "SELECT 1 FROM meta WHERE key='seeded' LIMIT 1"
            ).fetchone():
                return
        except sqlite3.Error:
            return
        try:
            src = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=3
            )
        except sqlite3.Error:
            self._mark_seeded()
            return
        try:
            days_rows = src.execute(
                "SELECT uid, date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) "
                "FROM messages WHERE uid IS NOT NULL AND ts IS NOT NULL "
                "GROUP BY uid, day"
            ).fetchall()
            people_rows = src.execute(
                "SELECT uid, nick, MIN(ts), MAX(ts) FROM messages "
                "WHERE uid IS NOT NULL AND ts IS NOT NULL GROUP BY uid"
            ).fetchall()
        except sqlite3.Error:
            days_rows, people_rows = [], []
        finally:
            src.close()
        try:
            with self._db:
                self._db.executemany(
                    "INSERT INTO daily(uid, day, n) VALUES(?, ?, ?) "
                    "ON CONFLICT(uid, day) DO UPDATE SET n=MAX(n, excluded.n)",
                    days_rows,
                )
                self._db.executemany(
                    "INSERT INTO people(uid, nick, first_ts, last_ts, prev_ts) "
                    "VALUES(?, ?, ?, ?, 0) "
                    "ON CONFLICT(uid) DO UPDATE SET "
                    " nick=COALESCE(NULLIF(excluded.nick, ''), people.nick),"
                    " first_ts=MIN(people.first_ts, excluded.first_ts),"
                    " last_ts=MAX(people.last_ts, excluded.last_ts)",
                    people_rows,
                )
        except sqlite3.Error as exc:
            log.warning("熟悉度回填失败：%s", exc)
            return
        self._mark_seeded()
        log.info(
            "熟悉度回填完成：%d 人、%d 个「人+天」", len(people_rows), len(days_rows)
        )

    def _mark_seeded(self) -> None:
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO meta(key, value) VALUES('seeded', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(int(time.time())),),
                )
        except sqlite3.Error:
            pass

    def observe(self, uid: Any, nick: str = "", ts: float | int | None = None) -> None:
        """来了一条群消息：增量记「这个人的今天 +1」。

        只存 uid / 日期 / 条数，不存正文；日志裁剪后这些数还在。
        读缓存已经在内存里时顺手更新，避免每条消息都重新聚合。
        """
        if self._db is None or uid is None:
            return
        key = str(uid)
        moment = int(ts if ts is not None else time.time())
        day = _day(moment)
        name = (nick or "")[:32]
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO daily(uid, day, n) VALUES(?, ?, 1) "
                    "ON CONFLICT(uid, day) DO UPDATE SET n=n+1",
                    (key, day),
                )
                self._db.execute(
                    "INSERT INTO people(uid, nick, first_ts, last_ts, prev_ts) "
                    "VALUES(?, ?, ?, ?, 0) "
                    "ON CONFLICT(uid) DO UPDATE SET "
                    " prev_ts=CASE WHEN excluded.last_ts > people.last_ts "
                    "              THEN people.last_ts ELSE people.prev_ts END,"
                    " nick=COALESCE(NULLIF(excluded.nick, ''), people.nick),"
                    " first_ts=MIN(people.first_ts, excluded.first_ts),"
                    " last_ts=MAX(people.last_ts, excluded.last_ts)",
                    (key, name, moment, moment),
                )
        except sqlite3.Error as exc:
            log.debug("熟悉度写入失败：%s", exc)
            return
        if not self._loaded_at:
            return  # 还没加载过，下次 of() 直接从库里读
        person = self._people.get(key)
        if person is None:
            self._people[key] = Person(key, name, 1, 1, moment, moment)
            self._last_day[key] = day
            return
        person.msgs += 1
        person.first_ts = min(person.first_ts, moment) if person.first_ts else moment
        old_last = float(person.last_ts or 0)
        if moment > old_last:
            person.prior_ts = old_last
        person.last_ts = max(old_last, moment)
        if name:
            person.nick = name
        last_day = self._last_day.get(key)
        if last_day is None or day > last_day:
            person.days += 1
            self._last_day[key] = day

    # ---------- 读 ----------

    def _stale(self) -> bool:
        if self._db is not None:
            return time.time() - self._loaded_at > self.cache_seconds
        try:
            mtime = self.db_path.stat().st_mtime
        except OSError:
            return not self._people and self._loaded_at == 0.0
        return mtime != self._mtime or time.time() - self._loaded_at > self.cache_seconds

    def reload(self) -> None:
        """重算一次。聚合模式一条 SQL；没有聚合库时退回读群日志。"""
        if self._db is not None:
            self._reload_stats()
        else:
            self._reload_log()

    def _reload_stats(self) -> None:
        assert self._db is not None
        try:
            rows = self._db.execute(
                "SELECT d.uid, COALESCE(p.nick, ''), SUM(d.n), COUNT(*),"
                " COALESCE(p.first_ts, 0), COALESCE(p.last_ts, 0),"
                " COALESCE(p.prev_ts, 0), MAX(d.day)"
                " FROM daily d LEFT JOIN people p ON p.uid=d.uid"
                " GROUP BY d.uid"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        people: dict[str, Person] = {}
        last_day: dict[str, str] = {}
        for uid, nick, msgs, days, first_ts, last_ts, prev_ts, day in rows:
            key = str(uid)
            people[key] = Person(
                key,
                nick or "",
                int(msgs or 0),
                int(days or 0),
                float(first_ts or 0),
                float(last_ts or 0),
                float(prev_ts or 0),
            )
            if day:
                last_day[key] = str(day)
        self._people, self._last_day = people, last_day
        self._loaded_at = time.time()

    def _reload_log(self) -> None:
        """旧模式：一条 SQL 把每个人聚合成 句数 / 天数 / 首末时间。"""
        people: dict[str, Person] = {}
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3)
        except sqlite3.Error:
            self._people, self._loaded_at = {}, time.time()
            return
        try:
            rows = db.execute(
                "SELECT uid, nick, ts FROM messages WHERE uid IS NOT NULL AND ts IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            db.close()

        days: dict[str, set[str]] = {}
        for uid, nick, ts in rows:
            key = str(uid)
            p = people.get(key)
            if p is None:
                people[key] = Person(key, nick or "", 1, 0, ts, ts)
                days[key] = {_day(ts)}
                continue
            p.msgs += 1
            p.first_ts = min(p.first_ts, ts)
            p.last_ts = max(p.last_ts, ts)
            if nick:
                p.nick = nick
            days[key].add(_day(ts))

        for key, p in people.items():
            p.days = len(days.get(key, ()))
        self._people, self._last_day = people, {}
        self._loaded_at = time.time()
        try:
            self._mtime = self.db_path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def _ensure(self) -> None:
        if self._stale():
            self.reload()

    # ---------- 问 ----------

    def of(self, uid: Any, fresh: bool = False) -> Person | None:
        if fresh:
            self.reload()
        else:
            self._ensure()
        return self._people.get(str(uid))

    def tier(self, uid: Any) -> Tier:
        person = self.of(uid)
        return person.tier if person else TIERS[0]

    def line(
        self,
        uid: Any,
        nick: str = "",
        *,
        is_group: bool = True,
        proactive: bool = True,
        memory: bool = True,
        now: float | None = None,
    ) -> str:
        """给 `_dynamic_context` 用的一行；生人返回空串（不浪费 token）。

        `is_group` / `proactive` / `memory` 是**当前能力**：关掉的功能不能
        在提示里吹（「会主动找你」但主动关着，「记得上次」但记忆关着）。
        超过 `stale_days` 没见的人降级成「以前聊过，别装熟」。
        """
        person = self.of(uid)
        if person is None or person.level == 0:
            return ""
        name = nick or person.nick or str(uid)
        moment = time.time() if now is None else float(now)
        if person.is_stale(moment, self.stale_days, prior=True):
            ref = person.prior_ts or person.last_ts
            return (
                f"（「{name}」以前聊过，但{_ago(ref, moment)}没见了，"
                "别装熟；按刚认识的分寸来。）"
            )
        note = _note_for(person.tier, is_group=is_group, proactive=proactive, memory=memory)
        return f"（「{name}」{note}）" if note else ""

    def roster(self, limit: int = 20) -> list[Person]:
        self._ensure()
        return sorted(self._people.values(), key=lambda p: (p.level, p.msgs), reverse=True)[:limit]

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None


def _note_for(
    tier: Tier, *, is_group: bool, proactive: bool, memory: bool
) -> str:
    """按档位和当前能力拼提示；群聊专属的句子不往私聊塞。"""
    if tier.level <= 0:
        return ""
    if tier.level == 1:
        return "这个人你认得，见过几回。应一声就行，别主动凑上去。"
    if tier.level == 2:
        return "跟这个人聊过几回。可以接梗、反问一句，话不用多客气。"
    if tier.level == 3:
        parts = ["这个人是熟人。可以贫两句、吐槽半句。"]
        if proactive:
            parts.append("想起来就主动搭一句。")
        if memory:
            parts.append("记得上次说过的事。")
        if is_group:
            parts.append("有人损主人，你会忍不住顶半句。")
        return "".join(parts)
    parts = ["这个人是老熟人，认识挺久了。直接叫昵称，玩笑可以开重一点。"]
    if memory:
        parts.append("可以惦记「你上次说那个弄完没」。")
    if is_group:
        parts.append("别人说他，你会替他挡一句。")
    return "".join(parts)


def _ago(last_ts: float, now: float) -> str:
    days = max(1, int((float(now) - float(last_ts)) // 86400))
    if days < 60:
        return f"{days} 天"
    if days < 365:
        return f"{days // 30} 个月"
    return f"{days // 365} 年"


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    args = argv if argv is not None else sys.argv[1:]
    limit = 25
    for i, a in enumerate(args[:-1]):
        if a in ("-n", "--limit"):
            try:
                limit = max(1, int(args[i + 1]))
            except ValueError:
                pass

    stats = DATA_DIR / "familiar.db"
    fam = Familiar(
        DATA_DIR / "group_log.db",
        stats_path=stats if stats.exists() else None,
    )
    people = fam.roster(limit)
    if not people:
        print(f"没读到记录：{fam.stats_path or fam.db_path}")
        return 1

    source = "熟悉度聚合库" if fam.stats_path else "群聊日志（只读）"
    print(f"共 {len(fam._people)} 人（数据源：{source}），下面是前 {len(people)} 个\n")
    print(f'{"档位":<6}{"句数":>6}{"天数":>5}{"熟悉":>6}  {"最近":<12}{"昵称"}')
    now = time.time()
    for p in people:
        last = time.strftime("%m-%d %H:%M", time.localtime(p.last_ts))
        mark = "（久未联系）" if p.is_stale(now, fam.stale_days, prior=True) else ""
        print(
            f'{p.tier.name:<6}{p.msgs:>6}{p.days:>5}{p.familiarity:>6}  '
            f'{last:<12}{p.nick[:20]}{mark}'
        )

    print("\n她会看到的提示（前 3 个够档的人）:\n")
    shown = 0
    for p in people:
        if p.level < 2:
            continue
        print(" " + fam.line(p.uid, p.nick))
        shown += 1
        if shown >= 3:
            break
    fam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
