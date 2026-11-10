"""别人拉她进群：先问清情况，再交给主人拍板。

协议端会把「邀请你加入群聊」推成普通私聊（文字或卡片），也可能走真正的
request 事件。两种都在本地接住（0 token），不让她自己答应、也不直接拒绝：

1. 私聊回对方：问群大概多少人、平时活跃吗、主要聊什么；
2. 对方答了，把原话连同邀请人、群号一起私聊发给主人，等主人拍板；
3. 主人回「进群」：有 flag 就点同意；没有 flag（被当成普通私聊推过来的）
   就让他重新发一次群邀请，并记下「已同意」，下次真邀请进来直接点同意；
   回「不进群」：有 flag 就点拒绝，没有就只回对方一句。

状态存在 ``data/group_invites.json``：主人可能隔一阵才看到，重启不能丢。
过期的邀请不往主人那儿报（问完 30 分钟没答 / 报了 6 小时没拍板 / 同意后
24 小时没重拉）。文件里还记着「她最近进过 / 退过哪些群」，主人问
「那个新群你进去了吗」「你退群了吗」时由 ``app._dynamic_context`` 带给她
（7 天保鲜）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 问完等对方回答多久；报给主人后等他拍板多久；同意后等对方重拉多久
ASK_SECONDS = 30 * 60
DECISION_SECONDS = 6 * 3600
PREAPPROVE_SECONDS = 24 * 3600
# 进过的群记多久：主人问「那个新群你进去了吗」时，她得答得出来。
# 太久远的记录只会把提示词撑长，7 天够了。
JOIN_KEEP_SECONDS = 7 * 86400
# 对方的回答原样转发，太长的截断（够主人判断了，也免得刷屏）
MAX_ANSWER_CHARS = 300

# 问完发出去的话（她自己的口吻，0 token）
ASK_TEXT = (
    "哎，拉本鱼进群呀？先跟本鱼说说呗：这个群大概多少人？"
    "平时聊得活跃吗？主要聊什么的？本鱼得先跟主人说一声，他点头本鱼才进哦。"
)
# 对方答完、已经报给主人
WAIT_TEXT = "好，本鱼去跟主人说一声，等他说～"
# 主人点头、已经进群
JOINED_TEXT = "主人点头啦，本鱼进群咯～"
# 主人没点头
DECLINED_TEXT = "主人说先不进啦，谢谢你想着本鱼～"
# 主人点头了，但这次没有可点的「同意」（得让对方重发一次邀请）
REINVITE_TEXT = "主人说可以！你发个群邀请过来吧，本鱼点同意～"

# 阶段
STAGE_ASKED = "asked"
STAGE_REPORTED = "reported"
STAGE_APPROVED = "approved"


class GroupInvites:
    """待办群邀请。一个人同时只记一条，新的覆盖旧的。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = {}
        # 最近同意/进过的群：只用来回答「你进去了吗」，不参与拍板。
        self.joined: list[dict[str, Any]] = []
        # 最近退过的群：退群后 ``joined`` 里那条要拿掉，不然她会说「我还在」。
        self.left: list[dict[str, Any]] = []
        self.load()

    # ---------- 存 ----------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        invites = raw.get("invites") if isinstance(raw, dict) else None
        self.data = {}
        joined = raw.get("joined") if isinstance(raw, dict) else None
        self.joined = [
            rec for rec in (joined or []) if isinstance(rec, dict) and rec.get("group_id")
        ]
        left = raw.get("left") if isinstance(raw, dict) else None
        self.left = [
            rec for rec in (left or []) if isinstance(rec, dict) and rec.get("group_id")
        ]
        if not isinstance(invites, dict):
            return
        for uid, rec in invites.items():
            if isinstance(rec, dict) and rec.get("stage"):
                self.data[str(uid)] = rec

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "invites": self.data,
                        "joined": self.joined[-30:],
                        "left": self.left[-30:],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            log.exception("群邀请记录写入失败：%s", self.path)

    # ---------- 时间 ----------

    def _expired(self, rec: dict[str, Any], now: float) -> bool:
        stage = rec.get("stage")
        if stage == STAGE_ASKED:
            return now - float(rec.get("asked_at") or 0) > ASK_SECONDS
        if stage == STAGE_REPORTED:
            return now - float(rec.get("reported_at") or 0) > DECISION_SECONDS
        if stage == STAGE_APPROVED:
            return now - float(rec.get("approved_at") or 0) > PREAPPROVE_SECONDS
        return True

    def prune(self, now: float | None = None) -> None:
        moment = time.time() if now is None else float(now)
        stale = [uid for uid, rec in self.data.items() if self._expired(rec, moment)]
        for uid in stale:
            self.data.pop(uid, None)
        if stale:
            self.save()

    # ---------- 邀请 ----------

    def start(
        self,
        uid: Any,
        nick: str,
        group_id: str = "",
        flag: str = "",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """记下一条邀请。同一个人已经问过/报过就不重复问，返回 None。"""
        moment = time.time() if now is None else float(now)
        key = str(uid)
        self.prune(moment)
        old = self.data.get(key)
        if old and not self._expired(old, moment):
            if old.get("stage") == STAGE_ASKED:
                # 又发了一次邀请：补上 flag / 群号，别再问一遍
                if flag:
                    old["flag"] = flag
                if group_id:
                    old["group_id"] = group_id
                self.save()
                return None
            if old.get("stage") == STAGE_REPORTED:
                if flag and not old.get("flag"):
                    old["flag"] = flag
                    self.save()
                return None
        rec = {
            "uid": key,
            "nick": str(nick or key),
            "group_id": str(group_id or ""),
            "flag": str(flag or ""),
            "stage": STAGE_ASKED,
            "answer": "",
            "asked_at": moment,
        }
        self.data[key] = rec
        self.save()
        return rec

    def record_answer(
        self, uid: Any, text: str, now: float | None = None
    ) -> dict[str, Any] | None:
        """对方回答了「多少人 / 活跃吗 / 聊什么」：存原话，转成待主人拍板。"""
        moment = time.time() if now is None else float(now)
        rec = self.data.get(str(uid))
        if not rec or self._expired(rec, moment):
            return None
        if rec.get("stage") != STAGE_ASKED:
            return None
        answer = " ".join(str(text or "").split()).strip()
        if not answer:
            return None
        rec["answer"] = answer[:MAX_ANSWER_CHARS]
        rec["stage"] = STAGE_REPORTED
        rec["reported_at"] = moment
        self.save()
        return rec

    def latest_reported(self, now: float | None = None) -> dict[str, Any] | None:
        """主人这一句「进群 / 不进群」对应哪一条：最近报上去的那条。"""
        moment = time.time() if now is None else float(now)
        live = [
            rec
            for rec in self.data.values()
            if rec.get("stage") == STAGE_REPORTED and not self._expired(rec, moment)
        ]
        return max(live, key=lambda r: float(r.get("reported_at") or 0), default=None)

    def decide(self, uid: Any) -> dict[str, Any] | None:
        """主人拍完板：把这条从待办里拿掉。"""
        rec = self.data.pop(str(uid), None)
        if rec is not None:
            self.save()
        return rec

    # ---------- 没有 flag 时：先记「同意」，等对方重拉 ----------

    def preapprove(self, uid: Any, group_id: str = "", now: float | None = None) -> None:
        moment = time.time() if now is None else float(now)
        key = str(uid)
        rec = self.data.setdefault(key, {"uid": key, "nick": key})
        rec["group_id"] = str(group_id or rec.get("group_id") or "")
        rec["stage"] = STAGE_APPROVED
        rec["approved_at"] = moment
        self.save()

    def take_preapproval(
        self, uid: Any, group_id: str = "", now: float | None = None
    ) -> dict[str, Any] | None:
        """对方重新拉了：这条是不是主人已经点过头的？是就吃掉。"""
        moment = time.time() if now is None else float(now)
        rec = self.data.get(str(uid))
        if not rec or self._expired(rec, moment):
            return None
        if rec.get("stage") != STAGE_APPROVED:
            return None
        want = str(group_id or "")
        if want and rec.get("group_id") and want != rec["group_id"]:
            return None
        self.data.pop(str(uid), None)
        self.save()
        return rec

    # ---------- 她自己的进群状态（回答主人「那个群你进去了吗」） ----------
    #
    # 2026-10-27 实锤：主人邀请她进群、她在 01:59:30 真的点了同意，
    # 两分钟后问「那个新群你进去了吗」，她却只能顺着主人的话猜。
    # 这里把「进过的群」记下来，由 app._dynamic_context 带给模型；
    # 纯本地，0 token。

    def record_join(
        self, group_id: str, now: float | None = None, by: str = ""
    ) -> None:
        """记一条「她已经进了这个群」（同一个群只留最新一条）。"""
        gid = str(group_id or "").strip()
        if not gid:
            return
        moment = time.time() if now is None else float(now)
        self.joined = [
            rec for rec in self.joined if str(rec.get("group_id")) != gid
        ]
        self.joined.append(
            {"group_id": gid, "ts": int(moment), "by": str(by or "")}
        )
        self.joined = self.joined[-30:]
        # 又进去了：之前那条「退了群」的记录作废，别让她嘴上说退了、实际在群里。
        self.left = [rec for rec in self.left if str(rec.get("group_id")) != gid]
        self.save()

    def record_leave(
        self, group_id: str, now: float | None = None, by: str = ""
    ) -> None:
        """记一条「她已经退了这个群」；同时把「进过这个群」的记录拿掉。

        退群是主人明确下的指令，不是猜的；这里只记状态，不影响群名单。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return
        moment = time.time() if now is None else float(now)
        self.joined = [
            rec for rec in self.joined if str(rec.get("group_id")) != gid
        ]
        self.left = [rec for rec in self.left if str(rec.get("group_id")) != gid]
        self.left.append(
            {"group_id": gid, "ts": int(moment), "by": str(by or "")}
        )
        self.left = self.left[-30:]
        self.save()

    def recent_joins(
        self, hours: float = 48, now: float | None = None
    ) -> list[dict[str, Any]]:
        """最近进过的群（默认 48 小时内），新的在前。"""
        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0.0, float(hours)) * 3600
        return sorted(
            (
                rec
                for rec in self.joined
                if float(rec.get("ts") or 0) >= cutoff
            ),
            key=lambda r: float(r.get("ts") or 0),
            reverse=True,
        )

    def recent_left(
        self, hours: float = 48, now: float | None = None
    ) -> list[dict[str, Any]]:
        """最近退过的群（默认 48 小时内），新的在前。"""
        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0.0, float(hours)) * 3600
        return sorted(
            (
                rec
                for rec in self.left
                if float(rec.get("ts") or 0) >= cutoff
            ),
            key=lambda r: float(r.get("ts") or 0),
            reverse=True,
        )

    def status_lines(
        self, now: float | None = None, limit: int = 4
    ) -> list[str]:
        """给模型看的「她自己的进群状态」：待拍板 / 等重拉 / 最近进了哪些群。

        空清单表示没有值得说的；调用方自己决定要不要注入。只读，
        不顺手改状态（过期判断照 _expired，但不落盘）。
        """
        moment = time.time() if now is None else float(now)
        out: list[str] = []
        pending = sorted(
            (
                rec
                for rec in self.data.values()
                if not self._expired(rec, moment)
            ),
            key=lambda r: float(
                r.get("reported_at") or r.get("asked_at") or r.get("approved_at") or 0
            ),
            reverse=True,
        )
        for rec in pending:
            stage = str(rec.get("stage") or "")
            nick = str(rec.get("nick") or rec.get("uid") or "")
            gid = str(rec.get("group_id") or "")
            where = f"群 {gid}" if gid else "群号还不知道"
            if stage == STAGE_ASKED:
                out.append(f"{nick} 拉你进群（{where}），你问过情况、还在等他答")
            elif stage == STAGE_REPORTED:
                out.append(f"{nick} 拉你进群（{where}），他答过了，你在等主人拍板")
            elif stage == STAGE_APPROVED:
                out.append(f"你同意过 {nick} 的邀请（{where}），等他重新发一次")
        for rec in self.recent_joins(now=moment):
            gid = str(rec.get("group_id") or "")
            at = datetime.fromtimestamp(float(rec.get("ts") or 0)).strftime("%m-%d %H:%M")
            out.append(f"你在 {at} 进了群 {gid}（点了同意）")
        for rec in self.recent_left(now=moment):
            gid = str(rec.get("group_id") or "")
            at = datetime.fromtimestamp(float(rec.get("ts") or 0)).strftime("%m-%d %H:%M")
            out.append(f"你在 {at} 退了群 {gid}")
        return out[: max(0, int(limit))]


def owner_message(rec: dict[str, Any]) -> str:
    """报给主人的那条私聊：谁、哪个群、对方原话、怎么回。"""
    uid = str(rec.get("uid") or "")
    nick = str(rec.get("nick") or uid)
    gid = str(rec.get("group_id") or "")
    answer = str(rec.get("answer") or "").strip() or "（没说什么）"
    lines = [
        "主人，有人拉本鱼进群。",
        f"谁：{nick}（{uid}）",
        f"群号：{gid or '不知道'}",
        f"他说：{answer}",
        "进不进？回「进群」或「不进群」。",
    ]
    return "\n".join(lines)
