"""主人的偏好设置：靠聊天就能调她的行为。

改的是这些（存在 prefs.json，重启不丢）：

| 字段 | 一句话触发 | 效果 |
|---|---|---|
| sticker_frequency | 「多发点表情包 / 少发点」 | 全局表情包频率倍率 0~2 |
| sticker_tags     | 「少发委屈的」 | 某个情绪标签的权重 |
| sticker_files    | 「多发这个表情包」 | 某一张图的权重（0 = 不再发）|
| response_tier    | 「你回得太勤了 / 太安静了」 | 覆盖响应档位 0~3（只到本峰谷时段结束）|
| verbosity        | 「话太长了 / 多聊点」 | short / normal / chatty |
| owner_title      | 「以后叫我老板」 | 她对主人的称呼 |
| muted_groups     | 「这个群别插话 / 可以插话了」 | 长期不插话的群 |
| silent_groups    | 「不要在这个群说话了」 | 完全静默的群：连 @ 都不回，直到解禁 |
| silent_all       | 「不要在所有群里说话了」 | 总闸：所有群都不说话（盖过单群名单）|
| group_whitelist_add | 「把本群加白名单」 | 聊天里加进来的群（即使 .env 没写也回）|
| group_whitelist_off | 「把本群移出白名单」 | 聊天里从白名单移出去的群（.env 里写了也不回）|
| group_blacklist  | 「把本群加黑名单」 | 一律不回话的群（优先级最高）|
| presence_push_all | 「关闭上下线推送 / 打开上下线推送」 | 群聊上下线推送的总开关（默认开）|
| presence_push_off | 「关闭本群上下线推送 / 打开本群上下线推送」 | 不收上线问候和下线公告的群 |
| presence_dm_uids  | 「打开上线通知 / 关闭上线通知」（好友私聊里） | 订阅「本鱼上线时私聊告诉我」的好友；默认空 |
| dive_groups      | 「这个群潜水 30 分钟」 | 每个群的潜水截止时间（期间连 @ 都不回）|
| private_bans     | 「别理他了 / 别理 123456789」 | 私聊黑名单：这些号再私聊一律不回（0 token）|
| mod_strikes      | （她自己的判断） | 广告/骚扰警告到第几次；到数才自动拉黑 |
| feature_flags    | 「关闭合并转发 / 打开风控模式」 | 功能开关，重启后仍按主人的选择 |

`pick_weight()` 给表情包用：标签权重 × 单图权重 × 全局倍率，0 就是不发。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "sticker_frequency": 1.0,
    "sticker_tags": {},
    "sticker_files": {},
    "response_tier": None,
    # 手动档位是在哪个峰谷时段调的（peak / offpeak）：跨过边界就作废。
    # None = 没有时段概念（峰谷关着，或者调用方明确要常驻）。
    "response_tier_phase": None,
    "verbosity": "normal",
    "owner_title": "",
    "muted_groups": [],
    "silent_groups": [],
    "silent_all": False,
    "group_whitelist_add": [],
    "group_whitelist_off": [],
    "group_blacklist": [],
    "presence_push_all": True,
    "presence_push_off": [],
    "presence_dm_uids": [],
    "dive_groups": {},
    "private_bans": {},
    "mod_strikes": {},
    "dm_pace": 1.0,
    "thinking": None,
    "feature_flags": {},
    # 直播陪看：盯哪个房间、开播/下播通知发到哪个会话（group:123 / private:456）。
    # 房间号非空 = 重启后继续盯；说「不看直播了」会清空。
    "bili_live_room": "",
    # 正在陪看的会话（可以多个群看同一个房间）
    "bili_live_sessions": [],
    # 老字段：只留第一个会话，读写都跟 sessions 同步（老存档里只有它）
    "bili_live_session": "",
    # 上一次盯过的房间；停了也留着，「看看弹幕」时可以接着看
    "bili_live_last_room": "",
}


class Prefs:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.data = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("偏好设置写不进去：%s", exc)

    def reset(self) -> None:
        """全部恢复默认。"""
        self.data = dict(DEFAULTS)
        self.save()
        log.info("偏好设置已重置")

    # ---------- 表情包 ----------

    def sticker_weight(self, tag: str, filename: str) -> float:
        """这张图现在的权重。0 表示不发。"""
        base = float(self.data.get("sticker_frequency") or 1.0)
        tag_w = float((self.data.get("sticker_tags") or {}).get(tag, 1.0))
        file_w = float((self.data.get("sticker_files") or {}).get(filename, 1.0))
        return max(0.0, base * tag_w * file_w)

    def bump_sticker(self, filename: str, delta: float) -> float:
        files = dict(self.data.get("sticker_files") or {})
        files[filename] = _clamp(float(files.get(filename, 1.0)) + delta, 0.0, 5.0)
        self.data["sticker_files"] = files
        self.save()
        return files[filename]

    def bump_tag(self, tag: str, delta: float) -> float:
        tags = dict(self.data.get("sticker_tags") or {})
        tags[tag] = _clamp(float(tags.get(tag, 1.0)) + delta, 0.0, 5.0)
        self.data["sticker_tags"] = tags
        self.save()
        return tags[tag]

    def bump_frequency(self, delta: float) -> float:
        value = _clamp(float(self.data.get("sticker_frequency") or 1.0) + delta, 0.0, 2.0)
        self.data["sticker_frequency"] = value
        self.save()
        return value

    # ---------- 回复风格 ----------

    def set_tier(self, tier: int, phase: str | None = None) -> int:
        """记手动档位。

        ``phase`` 是调档时所在的峰谷时段（``peak`` / ``offpeak``）：记下来，
        等跨过峰谷边界时自动作废，把档位交还给峰谷。峰谷关着时传 ``None``，
        就是常驻覆盖。
        """
        tier = max(0, min(3, tier))
        self.data["response_tier"] = tier
        self.data["response_tier_phase"] = phase if phase in {"peak", "offpeak"} else None
        self.save()
        return tier

    def tier(self) -> int | None:
        value = self.data.get("response_tier")
        return int(value) if isinstance(value, int) else None

    def tier_phase(self) -> str | None:
        """手动档位当初是在哪个峰谷时段调的；没记就是 None。"""
        value = self.data.get("response_tier_phase")
        return value if value in {"peak", "offpeak"} else None

    def clear_tier(self) -> None:
        """交还峰谷：清掉手动档位（本来就没有就不写盘）。"""
        if self.data.get("response_tier") is None and self.data.get("response_tier_phase") is None:
            return
        self.data["response_tier"] = None
        self.data["response_tier_phase"] = None
        self.save()

    def set_verbosity(self, value: str) -> str:
        value = value if value in {"short", "normal", "chatty"} else "normal"
        self.data["verbosity"] = value
        self.save()
        return value

    def verbosity(self) -> str:
        return str(self.data.get("verbosity") or "normal")

    def set_owner_title(self, title: str) -> str:
        self.data["owner_title"] = (title or "").strip()[:12]
        self.save()
        return self.data["owner_title"]

    def owner_title(self) -> str:
        return str(self.data.get("owner_title") or "")

    # ---------- 群 ----------

    def mute_group(self, group_id: str) -> bool:
        groups = set(self.data.get("muted_groups") or [])
        groups.add(str(group_id))
        self.data["muted_groups"] = sorted(groups)
        self.save()
        return True

    def unmute_group(self, group_id: str) -> bool:
        groups = set(self.data.get("muted_groups") or [])
        groups.discard(str(group_id))
        self.data["muted_groups"] = sorted(groups)
        self.save()
        return True

    def group_muted(self, group_id: str) -> bool:
        return str(group_id) in set(self.data.get("muted_groups") or [])

    # 完全静默：比「别插话」更硬 —— 群里一句话都不说，连 @ 都不回，直到主人解禁。
    # 和潜水（`dive_groups`）的区别是**不过期**，重启也在。

    def silence_group(self, group_id: str) -> bool:
        groups = set(self.data.get("silent_groups") or [])
        groups.add(str(group_id))
        self.data["silent_groups"] = sorted(groups)
        self.save()
        return True

    def unsilence_group(self, group_id: str) -> bool:
        groups = set(self.data.get("silent_groups") or [])
        if str(group_id) not in groups:
            return False
        groups.discard(str(group_id))
        self.data["silent_groups"] = sorted(groups)
        self.save()
        return True

    def group_silent(self, group_id: str) -> bool:
        """这个群是不是完全静默（单群名单，或主人开了「所有群都不说话」）。"""
        if self.silent_all():
            return True
        return str(group_id) in set(self.data.get("silent_groups") or [])

    # 「所有群都不说话」：一个总闸，盖过单群名单；关掉就回到各群自己的状态。

    def silent_all(self) -> bool:
        return bool(self.data.get("silent_all"))

    def set_silent_all(self, enabled: bool) -> bool:
        before = self.silent_all()
        self.data["silent_all"] = bool(enabled)
        if before != bool(enabled):
            self.save()
        return bool(enabled)

    # ---------- 群白名单 / 黑名单 ----------
    #
    # `.env` 的 GROUP_WHITELIST 是底稿（改它要重启）；这里三张表是聊天里改的：
    #   group_whitelist_add  聊天里加进来的群（.env 没写也放行）
    #   group_whitelist_off  聊天里从白名单移出去的群（.env 写了也不放行）
    #   group_blacklist      黑名单，优先级最高
    # 判定顺序在 app._allowed_group()：黑名单 > 移出 > 加入 > .env > 没设就放行。
    # 三张表分开存，是为了「移出白名单」在 .env 名单里也能真的生效；
    # 全塞进一张表就分不清"本来就不在"和"被主人移出去了"。

    def group_whitelist_add(self) -> list[str]:
        return sorted(
            {str(gid) for gid in (self.data.get("group_whitelist_add") or []) if str(gid)}
        )

    def group_whitelist_off(self) -> list[str]:
        return sorted(
            {str(gid) for gid in (self.data.get("group_whitelist_off") or []) if str(gid)}
        )

    def group_blacklist(self) -> list[str]:
        return sorted(
            {str(gid) for gid in (self.data.get("group_blacklist") or []) if str(gid)}
        )

    def _write_group_lists(
        self, add: set[str], off: set[str], black: set[str]
    ) -> None:
        self.data["group_whitelist_add"] = sorted(add)
        self.data["group_whitelist_off"] = sorted(off)
        self.data["group_blacklist"] = sorted(black)
        self.save()

    def allow_group(self, group_id: str) -> None:
        """把群加进白名单：去掉「移出」和黑名单的记录，以后它就能回话。"""
        gid = str(group_id or "").strip()
        if not gid:
            return
        add = set(self.group_whitelist_add())
        off = set(self.group_whitelist_off()) - {gid}
        black = set(self.group_blacklist()) - {gid}
        add.add(gid)
        self._write_group_lists(add, off, black)

    def disallow_group(self, group_id: str, env_listed: bool = False) -> None:
        """把群移出白名单。

        ``env_listed`` 是「.env 的 GROUP_WHITELIST 里有它」：有就记进
        ``group_whitelist_off``，否则光从 add 里删掉还是会被 .env 放行。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return
        add = set(self.group_whitelist_add()) - {gid}
        off = set(self.group_whitelist_off())
        black = set(self.group_blacklist()) - {gid}
        if env_listed:
            off.add(gid)
        self._write_group_lists(add, off, black)

    def block_group(self, group_id: str) -> None:
        """拉进群黑名单：谁都叫不动，@ 也不回。"""
        gid = str(group_id or "").strip()
        if not gid:
            return
        add = set(self.group_whitelist_add()) - {gid}
        black = set(self.group_blacklist())
        black.add(gid)
        self._write_group_lists(add, set(self.group_whitelist_off()), black)

    def unblock_group(self, group_id: str) -> None:
        """移出群黑名单（「移出白名单」的记录不动：那是我另外下的决定）。"""
        gid = str(group_id or "").strip()
        if not gid:
            return
        black = set(self.group_blacklist()) - {gid}
        self._write_group_lists(
            set(self.group_whitelist_add()), set(self.group_whitelist_off()), black
        )

    # ---------- 群聊上下线推送 ----------
    #
    # 总开关默认开；关掉后所有群都不收上线问候和下线公告。
    # presence_push_off 记单个群的设置，总开关关着时先不动它 ——
    # 重新打开总开关，各群原来的选择还在。

    def set_presence_push_all(self, enabled: bool) -> bool:
        self.data["presence_push_all"] = bool(enabled)
        self.save()
        return bool(enabled)

    def presence_push_all(self) -> bool:
        value = self.data.get("presence_push_all", True)
        return bool(value) if isinstance(value, bool) else True

    def set_presence_push_off(self, group_ids: Any) -> None:
        """一次性重写"不收推送"的群名单（整体关群/整体恢复用）。"""
        groups = {str(gid) for gid in (group_ids or []) if str(gid)}
        self.data["presence_push_off"] = sorted(groups)
        self.save()

    def set_presence_push(self, group_id: str, enabled: bool) -> bool:
        groups = set(self.data.get("presence_push_off") or [])
        if enabled:
            groups.discard(str(group_id))
        else:
            groups.add(str(group_id))
        self.data["presence_push_off"] = sorted(groups)
        self.save()
        return bool(enabled)

    def presence_push(self, group_id: str) -> bool:
        # 总开关关掉时，所有群都算关 —— 和"每个群单独关掉"同一个效果。
        return self.presence_push_all() and (
            str(group_id) not in set(self.data.get("presence_push_off") or [])
        )

    # ---------- 好友上线通知 ----------
    #
    # 和上面的"群聊上下线推送"完全独立：那群全关了，好友私聊订阅也照常。
    # 默认空；只有在私聊里、且是 QQ 好友，本人说「打开上线通知」才进来。
    # 只发上线，不发下线 —— 下线时进程可能已经没了，不假装能发。

    def presence_dm_uids(self) -> list[str]:
        return sorted(
            {str(uid) for uid in (self.data.get("presence_dm_uids") or []) if str(uid)}
        )

    def presence_dm(self, uid: Any) -> bool:
        return str(uid) in set(self.data.get("presence_dm_uids") or [])

    def set_presence_dm(self, uid: Any, enabled: bool) -> bool:
        uids = {str(item) for item in (self.data.get("presence_dm_uids") or []) if str(item)}
        if enabled:
            uids.add(str(uid))
        else:
            uids.discard(str(uid))
        self.data["presence_dm_uids"] = sorted(uids)
        self.save()
        return bool(enabled)

    # ---------- 群潜水（定时完全静音） ----------

    def start_dive(self, group_id: str, minutes: int) -> float:
        """让这个群潜水 N 分钟，返回截止时间。重复设置就是改截止时间。"""
        minutes = max(1, min(24 * 60, int(minutes)))
        until = time.time() + minutes * 60
        dives = dict(self.data.get("dive_groups") or {})
        dives[str(group_id)] = until
        self.data["dive_groups"] = dives
        self.save()
        return until

    def end_dive(self, group_id: str) -> bool:
        """提前结束潜水。之前在潜就返回 True。"""
        dives = dict(self.data.get("dive_groups") or {})
        until = dives.pop(str(group_id), None)
        if until is None:
            return False
        self.data["dive_groups"] = dives
        self.save()
        return True

    def dive_until(self, group_id: str) -> float:
        """这个群潜到什么时候；没在潜或已到点返回 0。到点顺手清掉。"""
        dives = dict(self.data.get("dive_groups") or {})
        gid = str(group_id)
        try:
            until = float(dives.get(gid, 0) or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until and until <= time.time():
            dives.pop(gid, None)
            self.data["dive_groups"] = dives
            self.save()
            return 0.0
        return until

    # ---------- 私聊黑名单 ----------
    #
    # 主人烦了谁，一句话就把这个号的私聊关掉：他再发消息一律不回，连模型都不调。
    # 存的不是"拉黑"这个动作，而是**这个号以后还要不要回**；
    # 和群无关（他在群里说话照旧按 @/插话门控），也不影响主人自己。

    def ban_private(
        self, uid: str, name: str = "", by: str = "", reason: str = ""
    ) -> bool:
        """把这个号关进私聊黑名单；返回 True 表示是新禁的（之前不在名单里）。

        ``by`` 记是谁动的手（``owner`` / ``auto``），``reason`` 记一句原因 ——
        主人过阵子看名单时，能分清哪几个是她自己拉黑的。
        """
        uid = str(uid or "").strip()
        if not uid:
            return False
        bans = dict(self.data.get("private_bans") or {})
        added = uid not in bans
        old = bans.get(uid) if isinstance(bans.get(uid), dict) else {}
        # 重复禁（比如改口说「别理 123456789」）不该把之前记的昵称抹掉
        name = str(name or "").strip()[:32] or str((old or {}).get("name") or "")
        bans[uid] = {
            "name": name,
            "ts": time.time(),
            "by": str(by or (old or {}).get("by") or ""),
            "reason": str(reason or (old or {}).get("reason") or ""),
        }
        self.data["private_bans"] = bans
        self.save()
        return added

    def private_ban_info(self, uid: str) -> dict:
        entry = self.private_bans().get(str(uid)) or {}
        return dict(entry) if isinstance(entry, dict) else {}

    def unban_private(self, uid: str) -> dict | None:
        """解除一个号的私聊禁言；返回原来的那条记录，本来就不在名单里返回 None。"""
        uid = str(uid or "").strip()
        if not uid:
            return None
        bans = dict(self.data.get("private_bans") or {})
        old = bans.pop(uid, None)
        if old is None:
            return None
        self.data["private_bans"] = bans
        self.save()
        return old if isinstance(old, dict) else {}

    def private_banned(self, uid: str) -> bool:
        return str(uid) in (self.data.get("private_bans") or {})

    def private_bans(self) -> dict[str, dict]:
        """当前黑名单（副本，别直接改它）。"""
        raw = self.data.get("private_bans")
        return dict(raw) if isinstance(raw, dict) else {}

    def private_ban_name(self, uid: str) -> str:
        entry = self.private_bans().get(str(uid)) or {}
        return str(entry.get("name") or "") if isinstance(entry, dict) else ""

    def private_ban_ts(self, uid: str) -> float:
        entry = self.private_bans().get(str(uid)) or {}
        try:
            return float(entry.get("ts") or 0) if isinstance(entry, dict) else 0.0
        except (TypeError, ValueError):
            return 0.0

    # ---------- 广告 / 骚扰的警告次数 ----------
    #
    # 她自己判断（moderation.py，0 token）：先警告三次，第四次才真拉黑。
    # 旧账有保质期 —— 一个月前骂过一句、今天又骂一句，不该直接算两次。

    def strike(self, uid: str, kind: str = "", window_days: int = 7) -> int:
        """记一次警告，返回这是窗口期内的第几次。"""
        uid = str(uid or "").strip()
        if not uid:
            return 0
        strikes = dict(self.data.get("mod_strikes") or {})
        old = strikes.get(uid) if isinstance(strikes.get(uid), dict) else {}
        now = time.time()
        window = max(1, int(window_days)) * 86400
        try:
            last = float((old or {}).get("last") or 0)
        except (TypeError, ValueError):
            last = 0.0
        count = 1
        if old and last and now - last <= window:
            count = int((old or {}).get("n") or 0) + 1
        strikes[uid] = {"n": count, "last": now, "kind": str(kind or "")}
        self.data["mod_strikes"] = strikes
        self.save()
        return count

    def strike_count(self, uid: str) -> int:
        entry = (self.data.get("mod_strikes") or {}).get(str(uid)) or {}
        return int(entry.get("n") or 0) if isinstance(entry, dict) else 0

    def clear_strikes(self, uid: str) -> None:
        strikes = dict(self.data.get("mod_strikes") or {})
        if strikes.pop(str(uid), None) is not None:
            self.data["mod_strikes"] = strikes
            self.save()

    # ---------- 主动程度 ----------

    def set_pace(self, factor: float) -> float:
        """乘一个系数：>1 更主动（冷场门槛和冷却都会缩短）。"""
        value = float(self.data.get("dm_pace") or 1.0) * factor
        value = max(0.4, min(3.0, value))
        self.data["dm_pace"] = round(value, 2)
        self.save()
        return self.data["dm_pace"]

    def pace(self) -> float:
        return float(self.data.get("dm_pace") or 1.0)

    # ---------- 思考档位 ----------

    def set_thinking(self, value: str) -> str:
        value = value if value in {"off", "low", "high"} else "low"
        self.data["thinking"] = value
        self.save()
        return value

    def thinking(self) -> str | None:
        value = self.data.get("thinking")
        return value if value in {"off", "low", "high"} else None

    # ---------- 功能开关 ----------

    def set_feature(self, name: str, enabled: bool) -> bool:
        """记住主人对某个功能的开关；None = 没设置过，沿用 .env。"""
        flags = dict(self.data.get("feature_flags") or {})
        flags[str(name)] = bool(enabled)
        self.data["feature_flags"] = flags
        self.save()
        return bool(enabled)

    def feature(self, name: str) -> bool | None:
        flags = self.data.get("feature_flags") or {}
        value = flags.get(str(name)) if isinstance(flags, dict) else None
        return value if isinstance(value, bool) else None

    def risk_mode(self) -> bool:
        """风控规避模式：只读查询。开关本身走 feature_flags，不另存一份。

        没在聊天里设置过时，回落到 `.env` 的 `ENABLE_RISK_MODE`：上线问候 /
        下线公告这些只拿得到 prefs 的路径，也要按同一个口径停。
        """
        value = self.feature("risk_mode")
        if value is not None:
            return bool(value)
        return os.environ.get("ENABLE_RISK_MODE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "开",
        }

    def frequency(self) -> float:
        return float(self.data.get("sticker_frequency") or 1.0)

    # ---------- 直播陪看 ----------

    def bili_live_room(self) -> str:
        return str(self.data.get("bili_live_room") or "").strip()

    def bili_live_sessions(self) -> list[str]:
        """正在陪看的会话（可以多个群看同一个房间）。

        老数据里是单个字符串 `bili_live_session`；新数据是列表。
        列表为空时再回退去看老字段，这样不用写迁移脚本。
        """
        out: list[str] = []
        raw = self.data.get("bili_live_sessions")
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                text = str(item or "").strip()
                if text and text not in out:
                    out.append(text)
        if not out:
            legacy = str(self.data.get("bili_live_session") or "").strip()
            if legacy:
                out.append(legacy)
        return out

    def bili_live_session(self) -> str:
        """兼容旧接口：返回第一个会话。新代码用 `bili_live_sessions()`。"""
        sessions = self.bili_live_sessions()
        return sessions[0] if sessions else ""

    def bili_live_last_room(self) -> str:
        return str(self.data.get("bili_live_last_room") or "").strip()

    def _sync_legacy_session(self) -> None:
        """老字段 `bili_live_session` 跟列表同步：只存第一个，或者空。"""
        sessions = self.bili_live_sessions()
        self.data["bili_live_session"] = sessions[0] if sessions else ""

    def set_bili_live(self, room: str, session: str = "") -> None:
        """从零开始盯一个房间（换房间时用）：只通知这一个会话。"""
        room = str(room or "").strip()
        one = str(session or "").strip()
        self.data["bili_live_room"] = room
        self.data["bili_live_sessions"] = [one] if one else []
        self._sync_legacy_session()
        if room:
            self.data["bili_live_last_room"] = room
        self.save()

    def add_bili_live_session(self, session: str) -> None:
        """多群看同一个房间：把会话加进名单。"""
        text = str(session or "").strip()
        if not text:
            return
        sessions = self.bili_live_sessions()
        if text in sessions:
            return
        sessions.append(text)
        self.data["bili_live_sessions"] = sessions
        self._sync_legacy_session()
        self.save()

    def remove_bili_live_session(self, session: str) -> bool:
        """摘掉一个会话；返回是否已经没人看了。"""
        text = str(session or "").strip()
        sessions = [s for s in self.bili_live_sessions() if s != text]
        self.data["bili_live_sessions"] = sessions
        self._sync_legacy_session()
        if not sessions:
            self.data["bili_live_room"] = ""
            self.save()
            return True
        self.save()
        return False

    def clear_bili_live(self) -> None:
        if self.data.get("bili_live_room") or self.bili_live_sessions():
            self.data["bili_live_room"] = ""
            self.data["bili_live_sessions"] = []
            self.data["bili_live_session"] = ""
            self.save()

    # ---------- 给提示词用 ----------

    def sticker_note(self) -> str:
        """把「多发表情 / 少发表情」变成软提示，不再用硬闸门卡她。"""
        freq = self.frequency()
        if freq <= 0:
            return "主人把表情包总频率调到 0 了，这轮别贴图"
        if freq < 0.75:
            return "主人最近说少发表情包，除非特别合适，这轮尽量别贴"
        if freq > 1.25:
            return "主人最近说多发表情包，合适就贴"
        return ""

    def style_note(self) -> str:
        """一句话描述当前说话风格，拼在消息末尾。"""
        bits = []
        verbosity = self.verbosity()
        if verbosity == "short":
            # 「说短点」管的是平时接话；讲笑话/段子要按它本来的长度讲完，
            # 不然包袱会被 40 字卡掉（2026-11-04 主人反馈「有些笑话被长度拦住」）。
            bits.append(
                "主人让你说话再短一点，一两句就够"
                "（讲笑话/现成段子时按它本来的长度讲完，别截包袱）"
            )
        elif verbosity == "chatty":
            bits.append("主人让你放开点聊，可以多说两句")
        title = self.owner_title()
        if title:
            bits.append(f"叫他「{title}」")
        sticker = self.sticker_note()
        if sticker:
            bits.append(sticker)
        return "；".join(bits)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
