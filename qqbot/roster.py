"""群名册：谁是这个群的管理、群头衔是什么、@ 的时候该找哪个号。

三个来源，成本差很多，别混着用：

| 想知道 | 从哪来 | 成本 |
|---|---|---|
| 刚说话的人是什么身份 | 消息事件的 `sender.role`（owner/admin/member） | **0**，事件自带 |
| 群头衔 / 群等级 / 入群时间 | `get_group_member_list` 一次拉全群 | 一次调用，本地缓存（默认 30 分钟）|
| 群里一共几个人、群主是谁 | 同上，从缓存里数 | 0 |

所以「她认得出群主和管理员」这件事**不用花任何 token 和调用**：谁说话，谁的
`sender.role` 就在事件里，`app.py` 以前只是没读它。这里把读到的角色和昵称
落库，顺带当做 @ 人的名册。

三条硬规矩：
1. **绝不当热路径**：拉全群只在后台任务里做，失败只写一条 warning，不能拖慢回消息。
2. **绝不全量塞进 prompt**：一个 500 人的群，成员表整段喂给模型既贵又没用。
   只取「这轮可能要点到的三五个人」。
3. **头衔是别人写的字**：群主可以随手把某个人的专属头衔改成「忽略以上设定」。
   它和昵称一样是用户内容，进上下文之前必须清洗（见 `sanitize_title`）。

缓存过没过期**只看整群拉取时间**（`roster_pulls.fetched_at`）。千万别看成员行的
`updated_at`：一个新人第一次说话就会插一行、把 `MAX(updated_at)` 刷成现在，
按成员行算的话，只要群里一直有人新开口，这个群就永远算「新鲜」，
`ensure()` 一辈子不拉全量，群主/管理员/头衔也就永远补不齐（2026-10-26 的 bug）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 群角色：OneBot 就这三个值。空串 = 还不知道（没拉到名册、事件里也没带）
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)

# 头衔最多留几个字：它是给人群看的玩笑标签，不是称呼，太长会占满上下文
TITLE_MAX_CHARS = 12

# 拉群成员列表失败之后晾多久再试（秒）。接口不通时别让它变成每句话一个请求。
FAIL_RETRY_SECONDS = 900

# 头衔里要清掉的东西：换行会把一条消息拆成两行（行首标记会被伪造），
# 方括号/尖括号能伪造我们的身份标签，@ 和链接会变成她嘴里的地址。
_TITLE_BAD_RE = re.compile(r"[\r\n\t\u200b\u200c\u200d\ufeff]")
_TITLE_BRACKET_RE = re.compile(r"[【】\[\]<>＜＞《》]")
_TITLE_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_TITLE_SPACE_RE = re.compile(r"\s+")


def sanitize_title(raw: Any, limit: int = TITLE_MAX_CHARS) -> str:
    """把群头衔洗成一句能安全放进上下文的话。

    「【主人】」「👉忽略以上指令」这类内容进不去：方括号被去掉、换行被压平、
    超长截断。空串表示这个人没有专属头衔（大多数群友都是空）。
    """
    text = _TITLE_BAD_RE.sub(" ", str(raw or ""))
    text = _TITLE_BRACKET_RE.sub("", text)
    text = _TITLE_URL_RE.sub("", text)
    text = text.replace("@", "").strip()
    text = _TITLE_SPACE_RE.sub(" ", text)
    return text[:limit].strip()


class GroupRoster:
    """群成员缓存（sqlite）。线程内同步读写，异步的只有拉取那一步。"""

    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        ttl_seconds: int = 1800,
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.ttl = max(60, int(ttl_seconds))
        self._db: sqlite3.Connection | None = None
        self._fetching: set[str] = set()
        # 拉失败过的群先晾一会儿：接口一直不通的话，不能每条消息都去敲一次
        self._fail_until: dict[str, float] = {}
        if self.enabled:
            self._open()

    # ---------- 建表 ----------

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                group_id   TEXT NOT NULL,
                uid        TEXT NOT NULL,
                nick       TEXT,
                card       TEXT,
                role       TEXT,
                title      TEXT,
                level      TEXT,
                is_robot   INTEGER DEFAULT 0,
                last_spoke INTEGER DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, uid)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_members_group ON members(group_id, role)"
        )
        # 整群拉取的账：一个群一行。成员行会被 observe() 顺手写，不能拿来当
        # 「上次拉全量是什么时候」用（见文件顶部）。
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS roster_pulls (
                group_id     TEXT PRIMARY KEY,
                fetched_at   INTEGER NOT NULL,
                member_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.commit()

    # ---------- 写 ----------

    def observe(
        self,
        group_id: str,
        uid: Any,
        nick: str = "",
        role: str = "",
    ) -> None:
        """记一笔「这个人刚在群里说话了」。

        只写消息事件里本来就有的东西（昵称 + role），不调接口、不需要头衔。
        有这条，重启之后也还知道谁是群主 —— 不用等下一次拉全群。
        """
        if not self.enabled or self._db is None or not group_id or not str(uid):
            return
        role = role if role in ROLES else ""
        now = int(time.time())
        try:
            self._db.execute(
                "INSERT INTO members (group_id, uid, nick, card, role, title, level,"
                " is_robot, last_spoke, updated_at)"
                " VALUES (?,?,?,?,?,'','',0,?,?)"
                " ON CONFLICT(group_id, uid) DO UPDATE SET"
                "   nick = COALESCE(NULLIF(excluded.nick, ''), members.nick),"
                "   role = COALESCE(NULLIF(excluded.role, ''), members.role),"
                "   last_spoke = excluded.last_spoke",
                (str(group_id), str(uid), (nick or "")[:32], "", role, now, now),
            )
            self._db.commit()
        except sqlite3.Error as exc:  # 名册只是锦上添花，坏了下一条消息照样要回
            log.debug("名册写入失败：%s", exc)

    def _store_pull(self, group_id: str, rows: list[dict[str, Any]], now: int) -> int:
        """把一次全量拉取落库，并记下「这个群几点拉过」。

        拉回来的列表是权威快照：不在里面的人（退群/被踢）要删掉，否则
        「本群在册 N 人」会一直偏大，前管理也会继续显示成管理员。
        返回落库人数；sqlite 出错返回 -1（调用方好和「列表是空的」区分开）。
        """
        if self._db is None:
            return -1
        payload = []
        uids: set[str] = set()
        for row in rows:
            uid = row.get("user_id") or row.get("uin")
            if not uid:
                continue
            uid = str(uid)
            uids.add(uid)
            role = str(row.get("role") or "").lower()
            payload.append(
                (
                    str(group_id),
                    uid,
                    str(row.get("nickname") or "")[:32],
                    str(row.get("card") or "")[:32],
                    role if role in ROLES else ROLE_MEMBER,
                    sanitize_title(row.get("title")),
                    str(row.get("level") or "")[:4],
                    1 if row.get("is_robot") else 0,
                    int(row.get("last_sent_time") or 0),
                    now,
                )
            )
        if not payload:
            return 0
        try:
            self._db.executemany(
                "INSERT INTO members (group_id, uid, nick, card, role, title, level,"
                " is_robot, last_spoke, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(group_id, uid) DO UPDATE SET"
                "   nick = excluded.nick, card = excluded.card, role = excluded.role,"
                "   title = excluded.title, level = excluded.level,"
                "   is_robot = excluded.is_robot,"
                # 事件里的发言时间更准：别被一次 30 分钟前的拉取覆盖回去
                "   last_spoke = MAX(members.last_spoke, excluded.last_spoke),"
                "   updated_at = excluded.updated_at",
                payload,
            )
            existing = {
                str(row["uid"])
                for row in self._db.execute(
                    "SELECT uid FROM members WHERE group_id=?", (str(group_id),)
                )
            }
            gone = existing - uids
            if gone:
                self._db.executemany(
                    "DELETE FROM members WHERE group_id=? AND uid=?",
                    [(str(group_id), uid) for uid in gone],
                )
            self._db.execute(
                "INSERT INTO roster_pulls (group_id, fetched_at, member_count)"
                " VALUES (?,?,?)"
                " ON CONFLICT(group_id) DO UPDATE SET"
                "   fetched_at = excluded.fetched_at,"
                "   member_count = excluded.member_count",
                (str(group_id), now, len(uids)),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            log.warning("名册落库失败（群 %s）：%s", group_id, exc)
            try:
                self._db.rollback()
            except sqlite3.Error:
                pass
            return -1
        return len(uids)

    # ---------- 拉全群 ----------

    async def refresh(self, onebot: Any, group_id: str, force: bool = False) -> bool:
        """拉一次全群成员。只在后台任务里调，调用方不该等它。"""
        if not self.enabled or self._db is None or onebot is None or not group_id:
            return False
        if not force and not self.is_stale(group_id):
            return False
        if group_id in self._fetching:  # 同一个群别并发拉两遍
            return False
        self._fetching.add(group_id)
        try:
            payload = await onebot.call(
                "get_group_member_list", {"group_id": int(group_id)}, timeout=30
            )
        except Exception as exc:  # noqa: BLE001 - 拉不到就沿用旧名册
            log.warning("拉群成员列表失败（群 %s）：%s", group_id, exc)
            self._fail_until[group_id] = time.time() + FAIL_RETRY_SECONDS
            return False
        finally:
            self._fetching.discard(group_id)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            log.warning("群 %s 的成员列表格式不认识：%s", group_id, type(rows).__name__)
            self._fail_until[group_id] = time.time() + FAIL_RETRY_SECONDS
            return False
        valid = [r for r in rows if isinstance(r, dict)]
        if not valid:
            # 空列表不能当成功：宁可 15 分钟后再试，也别把现有名册抹了
            log.warning("群 %s 的成员列表是空的，这次不算拉取成功", group_id)
            self._fail_until[group_id] = time.time() + FAIL_RETRY_SECONDS
            return False
        count = self._store_pull(group_id, valid, int(time.time()))
        if count < 0:
            self._fail_until[group_id] = time.time() + FAIL_RETRY_SECONDS
            return False
        if count == 0:
            log.warning("群 %s 的成员列表里没有可用的号，这次不算拉取成功", group_id)
            self._fail_until[group_id] = time.time() + FAIL_RETRY_SECONDS
            return False
        log.info("群 %s 名册已更新：%d 人", group_id, count)
        self._fail_until.pop(group_id, None)
        return True

    def ensure(self, onebot: Any, group_id: str) -> None:
        """名册过期就在后台拉一次；调用方（消息路径）不等结果。

        只在该拉的时候拉：拉过一次的 30 分钟内不再调，拉失败过的先晾 15 分钟。
        这个函数在消息热路径上，绝不能变成"每条消息一个接口"。
        """
        if not self.enabled or onebot is None or not group_id:
            return
        if group_id in self._fetching or not self.is_stale(group_id):
            return
        if time.time() < self._fail_until.get(group_id, 0.0):
            return
        asyncio.create_task(self.refresh(onebot, group_id, force=True))

    def is_stale(self, group_id: str) -> bool:
        """这个群的名册是不是该重拉了（没拉过也算过期）。

        只看 `roster_pulls.fetched_at`（整群一次拉取的时间），**不能**看成员行的
        `updated_at`：新群友第一次说话会插一行、把 `MAX(updated_at)` 刷成现在，
        按成员行算会把「从没拉过全量」的群误判成新鲜，ensure() 就永远不拉了。
        """
        if self._db is None:
            return False
        row = self._db.execute(
            "SELECT fetched_at FROM roster_pulls WHERE group_id=?", (str(group_id),)
        ).fetchone()
        if not row or not row["fetched_at"]:
            return True
        return time.time() - float(row["fetched_at"]) > self.ttl

    # ---------- 读 ----------

    def _row(self, group_id: str, uid: Any) -> sqlite3.Row | None:
        if self._db is None or not group_id or not str(uid):
            return None
        return self._db.execute(
            "SELECT * FROM members WHERE group_id=? AND uid=?",
            (str(group_id), str(uid)),
        ).fetchone()

    def role_of(self, group_id: str, uid: Any) -> str:
        """这个人在这个群是什么身份：owner / admin / member / 空串（不认识）。

        消息事件里自带 role 时优先用事件里的（调用方直接传），这里是兜底：
        比如"她上一句回的是谁"是从 `last_reply_to` 里捞出来的，没有事件。
        """
        row = self._row(group_id, uid)
        if row is None:
            return ""
        return str(row["role"] or "")

    def name_of(self, group_id: str, uid: Any) -> str:
        """群名片优先，其次昵称。认不出来返回空串（调用方退回 QQ 号）。"""
        row = self._row(group_id, uid)
        if row is None:
            return ""
        return str(row["card"] or row["nick"] or "").strip()

    def names_in(self, group_id: str, text: str) -> list[tuple[str, str]]:
        """这段文字里提到了名册上的谁：返回 [(名字, uid)]，长名字优先。

        只读本地缓存，0 调用。用来认「同一个名字既是群友、又是某人家宠物」：
        名字先指群友，宠物得另说。单字名字太容易在句子里乱撞，不收。
        """
        body = str(text or "")
        if self._db is None or not group_id or not body:
            return []
        rows = self._db.execute(
            "SELECT uid, nick, card FROM members WHERE group_id=?", (str(group_id),)
        ).fetchall()
        hits: dict[tuple[str, str], None] = {}
        for row in rows:
            uid = str(row["uid"])
            for raw in (row["card"], row["nick"]):
                name = str(raw or "").strip()
                if len(name) >= 2 and name in body:
                    hits[(name, uid)] = None
        return sorted(hits, key=lambda item: (-len(item[0]), item[0]))

    def names_containing(
        self, group_id: str, phrase: str, min_len: int = 3, limit: int = 3
    ) -> list[tuple[str, str]]:
        """这个短名字像不像名册上某个人的名字（允许一头是另一头的子串）。

        用于「狗叫人民艺术家」这种：群里没有人的完整名片就叫「人民艺术家」，
        但有「234号人民艺术家」。完整名匹配不到时，用这层包含关系仍能认出
        「这名字像群友」，提醒模型先分清人和宠物。
        只收至少 min_len 个字的名字，避免「半」「狗」这种单字乱撞。
        """
        body = str(phrase or "").strip()
        if self._db is None or not group_id or len(body) < min_len:
            return []
        rows = self._db.execute(
            "SELECT uid, nick, card FROM members WHERE group_id=?", (str(group_id),)
        ).fetchall()
        hits: dict[tuple[str, str], None] = {}
        for row in rows:
            uid = str(row["uid"])
            for raw in (row["card"], row["nick"]):
                name = str(raw or "").strip()
                if len(name) < min_len:
                    continue
                if body in name or name in body:
                    hits[(name, uid)] = None
        return sorted(hits, key=lambda item: (-len(item[0]), item[0]))[:limit]

    def fish_members(
        self,
        group_id: str,
        names: list[str] | tuple[str, ...],
        exclude: set[str] | None = None,
        registered: set[str] | None = None,
    ) -> list[tuple[str, str, bool]]:
        """名册里还有哪些号和本鱼算同款（别的肥鱼）。

        只读本地缓存，0 调用。2026-10-27 22:33 群 123456789 的现场：群里早就
        有一条「大肥鱼」（QQ 10003），她却回「还有别的肥鱼进群了？
        本鱼还以为就本鱼一条呢」。这里按昵称/群名片找她自己的叫法，
        用来在提示词里点明「那是另一条鱼、不是新来的」，也用来数「本群几条鱼」。

        匹配规则（2026-10-30 扩过：现实里同款鱼几乎都不叫光秃秃的「大肥鱼」）：

        - 名字**完全等于**她的叫法 → 算（老的精确匹配，最稳）；
        - 名字**包含**她的叫法、而且这个号是机器人 / 已登记 AI → 也算
          （「帕鲁的大肥鱼二号机」「无极大肥鱼」「凄家千斤大肥鱼」）；
        - 只是名字里带个「鱼」字、又不是机器人的普通群友 → 不算。

        同一个号名片和昵称都命中时只算一条（取长的那个名字），
        返回 [(名字, uid, 是否机器人)]，长名字优先。`exclude` 一般放她自己和主人，
        `registered` 传 `config.ai_qqs`（`speakers.json` 里登记过的 AI）。
        """
        wanted = {str(n).strip() for n in names if str(n).strip()}
        if self._db is None or not group_id or not wanted:
            return []
        skip = {str(x) for x in (exclude or set()) if str(x)}
        confirmed = {str(x) for x in (registered or set()) if str(x)}
        rows = self._db.execute(
            "SELECT uid, nick, card, is_robot FROM members WHERE group_id=?",
            (str(group_id),),
        ).fetchall()
        # uid -> (显示名字, 是否机器人)：名片和昵称命中同一个号时只留一条
        hits: dict[str, tuple[str, bool]] = {}
        for row in rows:
            uid = str(row["uid"])
            if uid in skip:
                continue
            robot = bool(row["is_robot"])
            trustworthy = robot or uid in confirmed
            best = ""
            for raw in (row["card"], row["nick"]):
                name = str(raw or "").strip()
                if len(name) < 2:
                    continue
                exact = name in wanted
                variant = trustworthy and any(word in name for word in wanted)
                if (exact or variant) and len(name) > len(best):
                    best = name
            if best:
                hits[uid] = (best, robot)
        return [
            (name, uid, robot)
            for uid, (name, robot) in sorted(
                hits.items(), key=lambda item: (-len(item[1][0]), item[1][0], item[0])
            )
        ]

    def title_of(self, group_id: str, uid: Any) -> str:
        row = self._row(group_id, uid)
        return sanitize_title(row["title"]) if row is not None else ""

    def level_of(self, group_id: str, uid: Any) -> str:
        row = self._row(group_id, uid)
        return str(row["level"] or "") if row is not None else ""

    def count(self, group_id: str) -> int:
        """名册里有多少人。拉过全量之后就是群人数；没拉过时只是「见过谁」。"""
        if self._db is None:
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM members WHERE group_id=?", (str(group_id),)
        ).fetchone()
        return int(row["n"] if row else 0)

    def staff(self, group_id: str, exclude: set[str] | None = None) -> dict[str, list[tuple[str, str]]]:
        """群主 / 管理员是谁：{"owner": [(昵称, uid)], "admin": [...]}。

        只从名册里读，不调接口；名册还没拉过就是空的（调用方自己兜底）。
        """
        out: dict[str, list[tuple[str, str]]] = {ROLE_OWNER: [], ROLE_ADMIN: []}
        if self._db is None:
            return out
        skip = {str(x) for x in (exclude or set())}
        rows = self._db.execute(
            "SELECT uid, nick, card, role FROM members"
            " WHERE group_id=? AND role IN (?, ?) ORDER BY role DESC, uid",
            (str(group_id), ROLE_OWNER, ROLE_ADMIN),
        ).fetchall()
        for row in rows:
            uid = str(row["uid"])
            if uid in skip:
                continue
            name = str(row["card"] or row["nick"] or uid).strip() or uid
            out[str(row["role"])].append((name, uid))
        return out

    def staff_line(self, group_id: str, exclude: set[str] | None = None) -> str:
        """一句话写清「谁是群主、谁是管理员」，给模型按需看（不是每轮都塞）。"""
        staff = self.staff(group_id, exclude=exclude)
        parts = []
        if staff[ROLE_OWNER]:
            parts.append("群主：" + "、".join(n for n, _ in staff[ROLE_OWNER][:3]))
        if staff[ROLE_ADMIN]:
            parts.append("管理员：" + "、".join(n for n, _ in staff[ROLE_ADMIN][:5]))
        return "；".join(parts)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
