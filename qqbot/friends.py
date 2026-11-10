"""好友：申请怎么批、名单存哪、什么时候删人。

保守原则（都能在 .env 里调，但默认就这样）：

- **默认关**：要主人私聊说「可以加好友了」才开，重启也记得（存 prefs.json）。
- **只批「聊过」以上**（40 句 + 3 天，见 familiar.py）：生人、眼熟不批；
  还要**最近 30 天有互动**、**好感不为负**（`affection.raw_score >= 0`，-0.5 也算负）。
  批不批只看这三条 + 开关 + 当天额度；拒绝是委婉话术，不把责任推给主人。
- **一天最多批 3 个**：防一觉醒来涌进一堆广告号。
- **批了不先说话**：通过之后她一声不吭，等对方开口。她怯，不该她先热络。
- **删人**：只能删「正在私聊里骚扰她的那个人」，一天一个，删完立刻私聊告诉主人。
  主人可以直接点名删谁。

名单存在 friends.json：`friends` 是好友，`stats` 是当天用掉的额度。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_ACCEPT_CAP = 3
DEFAULT_DELETE_CAP = 1
MIN_TIER = 2  # 2 = 聊过
# 最近一次说话超过这么久，不再算"还在处"
STALE_DAYS = 30


class Friends:
    def __init__(self, path: Path, config: Any) -> None:
        self.path = Path(path)
        self.config = config
        self.data: dict[str, Any] = {"friends": {}, "stats": {}}
        self.load()

    # ---------- 存 ----------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        friends = raw.get("friends")
        stats = raw.get("stats")
        self.data = {
            "friends": friends if isinstance(friends, dict) else {},
            "stats": stats if isinstance(stats, dict) else {},
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            log.exception("好友名单写入失败：%s", self.path)

    # ---------- 额度 ----------

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _stat(self) -> dict[str, Any]:
        stats = self.data.setdefault("stats", {})
        if stats.get("day") != self._today():
            stats.clear()
            stats.update({"day": self._today(), "approved": 0, "deleted": 0})
        return stats

    @property
    def accept_cap(self) -> int:
        return int(getattr(self.config, "friend_daily_cap", DEFAULT_ACCEPT_CAP))

    @property
    def delete_cap(self) -> int:
        return int(getattr(self.config, "friend_delete_daily", DEFAULT_DELETE_CAP))

    def enabled(self) -> bool:
        return bool(getattr(self.config, "enable_friend_accept", False))

    # ---------- 名单 ----------

    def get(self, uid: Any) -> dict[str, Any] | None:
        return self.data["friends"].get(str(uid))

    def is_friend(self, uid: Any) -> bool:
        return str(uid) in self.data["friends"]

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self.data["friends"])

    def _remember(self, uid: Any, nick: str, by: str) -> None:
        self.data["friends"][str(uid)] = {"nick": nick, "since": int(time.time()), "by": by}
        self.save()

    def forget(self, uid: Any) -> None:
        if self.data["friends"].pop(str(uid), None) is not None:
            self.save()

    # ---------- 好友申请 ----------

    def decide(
        self,
        uid: Any,
        level: int,
        *,
        affection: float | None = None,
        person: Any = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """批不批。理由只进日志，给对方看的是另一套委婉话术。

        `affection` / `person` 不传 = 旧行为（只看档位），供老测试/工具用；
        app 里会传真实的好感和熟人档位，按「档位 + 最近互动 + 好感」判。
        """
        if not self.enabled():
            return False, "开关关着"
        if self.is_friend(uid):
            return False, "已经是好友"
        need = int(getattr(self.config, "friend_min_tier", MIN_TIER))
        if level < need:
            return False, f"不够熟（{level}<{need}）"
        moment = time.time() if now is None else float(now)
        if person is not None and hasattr(person, "is_stale"):
            stale_days = int(getattr(self.config, "familiar_stale_days", STALE_DAYS))
            if person.is_stale(moment, stale_days, prior=True):
                return False, "最近一个月没见"
        if affection is not None and float(affection) < 0:
            return False, "好感是负的"
        if self._stat()["approved"] >= self.accept_cap:
            return False, "今天批够了"
        return True, "够熟"

    async def approve(
        self,
        onebot: Any,
        flag: str,
        uid: Any,
        nick: str,
        level: int,
        *,
        affection: float | None = None,
        person: Any = None,
        now: float | None = None,
    ) -> str:
        ok, why = self.decide(
            uid, level, affection=affection, person=person, now=now
        )
        if not ok:
            log.info("好友申请 %s(%s) 不批：%s", nick, uid, why)
            return why
        try:
            await onebot.call("set_friend_add_request", {"flag": flag, "approve": True}, check=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("同意好友申请失败 %s: %s", uid, exc)
            return f"同意失败：{exc}"
        self._remember(uid, nick, "auto")
        stat = self._stat()
        stat["approved"] += 1
        self.save()
        log.info(
            "通过了 %s(%s) 的好友申请（档位 %s，今天第 %s 个）", nick, uid, level, stat["approved"]
        )
        return "ok"

    # ---------- 删人 ----------

    def can_delete(self) -> bool:
        return self._stat()["deleted"] < self.delete_cap

    async def delete(self, onebot: Any, uid: Any, reason: str = "") -> str:
        uid_s = str(uid)
        if not self.can_delete():
            return "今天已经删过一个了，剩下的明天再说"
        if not self.is_friend(uid_s):
            return "本来就不是好友"
        try:
            await onebot.call("delete_friend", {"user_id": int(uid_s)}, check=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("删好友失败 %s: %s", uid_s, exc)
            return f"删失败了：{exc}"
        nick = (self.get(uid_s) or {}).get("nick") or uid_s
        self.forget(uid_s)
        stat = self._stat()
        stat["deleted"] += 1
        self.save()
        log.info("删了好友 %s(%s)，原因：%s", nick, uid_s, reason or "没写")
        return f"删掉了 {nick}"


class FriendTools:
    """给模型用的删人工具。

    只在私聊里给：**删的永远是"正在跟她说话的那个人"**，除非是主人点名。
    这样就算有人在群里哄她，也删不到别人。
    """

    NAMES = {"delete_friend"}

    def __init__(self, config: Any) -> None:
        self.config = config

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "delete_friend",
                    "description": (
                        "把一个好友从你的 QQ 里删掉（对方会从你的好友列表消失）。"
                        "只在私聊里用：对方反复要你的设定、发广告、开黄腔、骂人、"
                        "让你做越界的事，你又确实不想再理他了，才用。"
                        "普通吵架、抬杠、问烦了，都不要删。一天只能删一个。"
                        "默认删「正在跟你说话的这个」；只有主人明确点名删谁时，才填 nick。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nick": {
                                "type": "string",
                                "description": "要删的人的好友备注/昵称。只有主人点名时才填，否则留空",
                            },
                            "reason": {
                                "type": "string",
                                "description": "一句话原因，20 字以内，会转告主人",
                            },
                        },
                        "required": ["reason"],
                    },
                },
            }
        ]

    async def run(
        self,
        name: str,
        arguments: str,
        onebot: Any = None,
        is_owner: bool = False,
        speaker_uid: Any = None,
        session: str = "",
        friends: Friends | None = None,
        **_kwargs: Any,
    ) -> str | None:
        if name not in self.NAMES:
            return None
        if not isinstance(friends, Friends):
            return None
        if onebot is None:
            return "（现在连不上 QQ）"
        if not str(session).startswith("private:"):
            return "（群里不删人，要删让他私聊）"

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "（参数不是合法 JSON）"
        if not isinstance(args, dict):
            args = {}
        reason = str(args.get("reason", "")).strip()[:30]

        owner = str(getattr(self.config, "owner_qq", "") or "")
        target = str(speaker_uid or "")
        wanted = str(args.get("nick", "")).strip()
        if is_owner and wanted:
            target = self._resolve(friends, wanted) or target
            if not friends.is_friend(target):
                return f"（好友里没找到「{wanted}」）"
        if not target:
            return "（不知道要删谁）"
        if owner and target == owner:
            return "（主人不能删）"

        note = await friends.delete(onebot, target, reason or ("主人让删的" if is_owner else ""))
        if not note.startswith("删掉了"):
            return f"（{note}）"

        # 删好友是一次定性的负面事件：好感里记一笔（重罚，绕过每日额度）。
        # 不 import affection，避免循环依赖；app 初始化时把对象挂到 self.affection。
        affection = getattr(self, "affection", None)
        if affection is not None:
            try:
                affection.penalize(target, -10, "delete_friend")
            except Exception:  # noqa: BLE001
                log.debug("删好友后记好感失败", exc_info=True)

        if not is_owner:
            await self._tell_owner(onebot, owner, note, reason)
        return f"（{note}。）"

    @staticmethod
    def _resolve(friends: Friends, wanted: str) -> str:
        wanted = wanted.strip().lstrip("@")
        exact = [uid for uid, v in friends.all().items() if str(v.get("nick", "")) == wanted]
        if len(exact) == 1:
            return exact[0]
        if wanted.isdigit():
            return wanted
        fuzzy = [uid for uid, v in friends.all().items() if wanted and wanted in str(v.get("nick", ""))]
        return fuzzy[0] if len(fuzzy) == 1 else ""

    async def _tell_owner(self, onebot: Any, owner: str, note: str, reason: str) -> None:
        """删完必须报备，不然主人会觉得好友莫名其妙少了。"""
        if not owner:
            return
        text = f"{note}。他说的是：{reason or '（没说什么）'}。本鱼是不是太凶了嘛……"
        try:
            await onebot.send_private(int(owner), [{"type": "text", "data": {"text": text}}])
        except Exception:  # noqa: BLE001
            log.exception("删好友后给主人报备失败")
