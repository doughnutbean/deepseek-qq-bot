"""模型自己调用的直播工具：看 / 停 / 查状态。

之前直播只能靠主人发「看直播 房间号」这种整句指令；主人说「测试一下看克里斯
的直播」时，模型手里没有能「真的开始盯」的工具，只能嘴上说「直播陪看关着」。
这里把 `BiliLiveWatcher` 包成几个工具：

- `bili_live_watch(target)`：target 可以是房间号、直播间链接、UP 主名字；
- `bili_live_status(target="")`：现在盯的是谁、开没开播；带 target 只查不换；
- `bili_live_history(target="", day="")`：今天/昨天/最近的按天开播/下播记录；
- `bili_live_danmaku()`：最近弹幕；
- `bili_live_stop()`：不看了。

名字解析顺序：
1. 群里的「克里斯」等别名 → 配置的 `CHRIS_MID` 查当前直播间；
2. 群知识库里带 `live.bilibili.com/<房间号>` 的旧资料；
3. B 站搜 UP 主，只认**同名**的号，再查他的直播间；
4. 认不准就把候选返回给模型，让它问主人，绝不拿第一个结果凑数。

所有工具只给主人（`llm.reply` 只对 `ctx.is_owner` 挂这个 provider），
而且 `run()` 里再检查一次；群友说「去看某某直播」不会把主人正在看的房间换掉。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from .bili_live import BiliLiveWatcher, extract_room_id
from .live_history import LiveHistory, is_recent_day, parse_day
from .tool_spec import make_tool

log = logging.getLogger(__name__)

_LIVE_URL_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d{1,12})", re.I)

# 模型/命令常把时间词一起递进来：「现在asaki大人在播吗」里的「现在」不是名字。
# 先摘掉开头的时间词和句尾的提问框架，再按房号/名字解析（2026-10-30 主人 todo）。
_TIME_WORDS = ("现在", "今天", "今晚", "此刻", "这会儿", "目前", "刚才")
_ASK_TAIL_RE = re.compile(
    r"(?:现在|今天|今晚|此刻|这会儿)?\s*(?:还)?\s*"
    r"(?:在播吗|在直播吗|在看直播吗|开播了吗|开播没|在播没|直播了吗|播了吗)$"
)

# 群里说「克里斯」时，指的就是 B 站 UP「Chris1i」。用户搜索是字面匹配，
# 「克里斯」会在 B 站搜到一个 98 粉的同名号；所以这组别名直接走配置里的
# CHRIS_MID 查当前直播间，不再靠同名搜索撞运气。
_CHRIS_ALIASES = {
    "克里斯",
    "chris",
    "chris1i",
    "chris4i",
    "克里斯一",
    "克区",
    "克神",
    "克宝",
}


class LiveTools:
    """一个很薄的工具提供者：schema + run，和 SearchTools/StickerTools 同一套接口。"""

    def __init__(
        self,
        watcher: BiliLiveWatcher,
        prefs: Any,
        config: Any,
        ensure_loop: Callable[[], None],
        *,
        memory: Any = None,
        api_factory: Callable[[], Any] | None = None,
        send_danmaku: Callable[[str], Any] | None = None,
        history: LiveHistory | None = None,
    ) -> None:
        self.watcher = watcher
        self.prefs = prefs
        self.config = config
        self.ensure_loop = ensure_loop
        self.memory = memory
        self.api_factory = api_factory
        # 发弹幕是写操作：真的往 B 站发一条，只有主人能用（app 里再接额度/语料）
        self._send = send_danmaku
        # 开播/下播记录：查询只读本地库，不换房间、不联网。
        self.history = history

    def schemas(self) -> list[dict[str, Any]]:
        return [
            make_tool(
                "bili_live_watch",
                "开始盯/切换一个 B 站直播间，只读房间标题和公开弹幕（不看画面、"
                "不做语音识别、0 token）。主人说「看直播」「去看某人的直播」"
                "「盯一下直播间」，或「进入/换到/切换到某人的直播陪看」时都用它，"
                "会直接切到新房间；不要只嘴上答应「盯上了」。target 可以是房间号、"
                "直播间链接或 UP 主名字；名字认不准会返回候选，先问主人再重试。"
                "只有主人能用。",
                {
                    "target": {
                        "type": "string",
                        "description": (
                            "房间号（如 123456789）、直播间链接，或 UP 主名字"
                            "（如 Chris1i）。名字有歧义时不要猜。"
                        ),
                    }
                },
                ["target"],
            ),
            make_tool(
                "bili_live_status",
                "查看直播开播状态。不传 target 时看现在盯的是哪个 B 站直播间、"
                "开没开播、攒了多少弹幕；传 target 时只查某个人/房间现在开没"
                "开播，不会换掉正在盯的房间。只回答「现在」；问今天/昨天播过"
                "没有、播了多久、开播记录，要用 bili_live_history。",
                {
                    "target": {
                        "type": "string",
                        "description": (
                            "可选：房间号、直播间链接或 UP 主名字。想知道"
                            "「克里斯在播吗」就传「克里斯」；不传就查当前房间。"
                        ),
                    }
                },
            ),
            make_tool(
                "bili_live_history",
                "查直播开播/下播记录和某一天的直播状态（只读，不换房间；按名字查时"
                "只解析一下直播间，不搜网页）。主人问「今天开播了吗」「今天播了"
                "多久」「几点开播几点下播」「昨天播过吗」「开播记录」「最近开播」"
                "时用它。target 不传就看当前/上次陪看的房间；day 支持今天/昨天/"
                "前天/YYYY-MM-DD，传「最近」或「记录」列最近几场。只记陪看期间"
                "观察到的开播/下播；没记录就直说，别编时间。",
                {
                    "target": {
                        "type": "string",
                        "description": (
                            "可选：房间号、直播间链接或 UP 主名字。不传就查"
                            "当前/上次陪看的房间。"
                        ),
                    },
                    "day": {
                        "type": "string",
                        "description": (
                            "可选：今天 / 昨天 / 前天 / YYYY-MM-DD；传「最近」"
                            "或「记录」列出最近几场。不传 = 今天。"
                        ),
                    },
                },
            ),
            make_tool(
                "bili_live_danmaku",
                "查看当前盯着的 B 站直播间最近几条弹幕（只读内存缓冲，0 token）。"
                "主人问「弹幕在聊什么」「看看弹幕」「有人刷什么」时用它；"
                "没在盯直播间会提示先看谁的，不要自己编弹幕。",
            ),
            make_tool(
                "bili_live_stop",
                "停止盯当前 B 站直播间。主人说「不看了」「关掉直播」「退出/结束"
                "某人的直播陪看」时用；会真的停掉并清房间号。",
            ),
            make_tool(
                "live_send_danmaku",
                "在**正在陪看**的那个 B 站直播间发一条弹幕（写操作，会真的发到 B 站）。"
                "主人说「发个弹幕」「发条弹幕吐槽一下」「去直播间说句话」时用。"
                "text 可选：给了就按主人说的发；不给就从本地克里斯语料里挑一条短句。"
                "B 站普通用户上限 20 字，太长会被拒；有频率限制（一分钟左右一条）。"
                "没在陪看直播时不要调，先让主人说看哪个直播间。",
                {
                    "text": {
                        "type": "string",
                        "description": (
                            "要发的弹幕内容，≤20 字。主人说「发个弹幕 说XXX」就传 XXX；"
                            "主人只说「发个弹幕」就留空，由本鱼自己挑。"
                        ),
                    }
                },
            ),
        ]

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        *,
        turn: Any = None,
        _tool_charges: dict[str, int] | None = None,
        **ctx: Any,
    ) -> str | None:
        # 新入口优先：给了 Turn 就按它来（老的单参调用仍然兼容，测试在用）
        if turn is not None:
            ctx = turn.as_ctx()
        if name not in {
            "bili_live_watch",
            "bili_live_status",
            "bili_live_history",
            "bili_live_danmaku",
            "bili_live_stop",
            "live_send_danmaku",
        }:
            return None
        # 双保险：schema 只给主人，这里再挡一次，防止别的路径把 provider 挂进来。
        # 群里主人说话时 is_owner 是 False（那个只代表「私聊里的主人」），
        # 所以还要认 owner_live —— 否则主人在群里说「发个弹幕」会被这里拒掉。
        if not (ctx.get("is_owner") or ctx.get("owner_live")):
            return "只有主人能让本鱼动直播间。"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name == "live_send_danmaku":
            if self._send is None:
                return "发弹幕没接上，先别试。"
            return await self._send(str(args.get("text") or "").strip())
        if name == "bili_live_status":
            return await self.status(str(args.get("target") or "").strip())
        if name == "bili_live_history":
            return await self.history_text(
                str(args.get("target") or "").strip(),
                str(args.get("day") or "").strip(),
            )
        if name == "bili_live_danmaku":
            return self.danmaku()
        if name == "bili_live_stop":
            return self._stop()
        target = str(args.get("target") or "").strip()
        return await self._watch(target, str(ctx.get("session") or ""))

    # ---------- 名字 / 房号解析 ----------

    @staticmethod
    def _clean_target(target: str) -> str:
        """摘掉时间词和提问框架，只留下主播名/房号。"""
        text = str(target or "").strip()
        for word in _TIME_WORDS:
            if text.startswith(word):
                text = text[len(word) :].strip()
                break
        text = _ASK_TAIL_RE.sub("", text).strip()
        # 模型可能把「Chris1i 的直播」「克里斯直播间」整串递进来
        return re.sub(r"(?:的)?(?:直播间|直播)$", "", text).strip()

    async def _resolve(self, target: str) -> tuple[str, str, list[str]]:
        """(房间号, 怎么认出来的, 候选)。认不出时房间号为空。"""
        text = self._clean_target(target)
        if not text:
            return "", "", []
        room = extract_room_id(text)
        if room:
            return room, "房号", []

        api = self.api_factory() if self.api_factory is not None else None

        # 1. 群里的「克里斯」就是 B 站「Chris1i」：先用配置的 mid 查当前房间。
        #    B 站用户搜索是字面匹配，「克里斯」会撞到一个 98 粉的同名号，
        #    靠同名搜索永远认不到 Chris1i。
        if api is not None and text.casefold() in _CHRIS_ALIASES:
            room = await self._room_from_chris(api)
            if room:
                return room, "B站主播 Chris1i", []

        # 2. 知识库里的旧资料：里面常带直播间链接（如「克里斯直播间」）
        room = self._room_from_memory(text)
        if room:
            return room, "记忆里的直播间链接", []

        # 3. B 站搜 UP 主：先看正在播的，再看粉丝量。别拿第一个结果凑数，
        #    也别让一个 116 粉的同名小号抢走 188 万粉的「Asaki大人」
        #    （2026-10-30 实测：搜 asaki 第一条是 Asaki大人，第二条是同名小号）。
        if api is None:
            return "", "", []
        try:
            users = await api.search_user(text, 8)
        except Exception:  # noqa: BLE001 - 搜不到就让模型问
            log.info("直播工具搜 UP 主失败：%s", text[:40], exc_info=True)
            return "", "", []
        key = text.casefold()
        related = [
            item
            for item in (users or [])
            if key in str(item.get("name") or "").casefold()
        ][:5]
        if not related:
            related = list(users or [])[:5]
        checked: list[dict[str, Any]] = []
        for item in related:
            try:
                live = await api.live_status(str(item.get("mid") or ""))
            except Exception:  # noqa: BLE001 - 单个查不到就跳过
                live = None
            checked.append(
                {
                    "name": str(item.get("name") or ""),
                    "fans": int(item.get("fans") or 0),
                    "exact": str(item.get("name") or "").casefold() == key,
                    "live": bool((live or {}).get("live")),
                    "room": str((live or {}).get("room_id") or ""),
                }
            )
        live_ones = [item for item in checked if item["live"] and item["room"]]
        if len(live_ones) == 1:
            best = live_ones[0]
            return best["room"], f"B站 UP「{best['name']}」正在播", []
        if len(live_ones) > 1:
            exact_live = [item for item in live_ones if item["exact"]]
            if len(exact_live) == 1:
                best = exact_live[0]
                return best["room"], f"B站 UP「{best['name']}」正在播", []
            return (
                "",
                "",
                [f"{item['name']}（{item['fans']} 粉，正在播）" for item in live_ones],
            )
        # 都没开播：粉丝量断层领先的先用；否则只认同名且不是几十粉的小号
        ranked = sorted(
            [item for item in checked if item["room"]],
            key=lambda item: item["fans"],
            reverse=True,
        )
        if ranked:
            top = ranked[0]
            second = ranked[1]["fans"] if len(ranked) > 1 else 0
            if top["fans"] >= 50000 and top["fans"] >= second * 5:
                return top["room"], f"B站 UP「{top['name']}」（现在没播）", []
            exact = [item for item in ranked if item["exact"] and item["fans"] >= 1000]
            if len(exact) == 1:
                return (
                    exact[0]["room"],
                    f"B站 UP「{exact[0]['name']}」（现在没播）",
                    [],
                )
        candidates = [
            f"{item['name']}（{item['fans']} 粉"
            + ("，正在播" if item["live"] else "")
            + "）"
            for item in checked[:5]
        ]
        return "", "", candidates

    async def _room_from_chris(self, api: Any) -> str:
        mid = str(getattr(self.config, "chris_mid", "") or "").strip()
        if not mid:
            return ""
        try:
            live = await api.live_status(mid)
        except Exception:  # noqa: BLE001 - 查不到就退回知识库/搜索
            log.info("直播工具查 Chris1i 直播间失败", exc_info=True)
            return ""
        return str((live or {}).get("room_id") or "")

    def _room_from_memory(self, target: str) -> str:
        if self.memory is None:
            return ""
        rows: list[dict[str, Any]] = []
        try:
            # 只看带直播链接的条目：带正确房号的旧知识常被大量新语料挤到
            # 最近 20 条之外（2026-10-29 实测：克里斯那条正好第 21 条）。
            rows = self.memory.knowledge_search(
                target, 20, url_like="live.bilibili.com"
            )
        except TypeError:
            # 兼容只接受 (needle, limit) 的旧 Memory / 测试假对象
            rows = []
        except Exception:  # noqa: BLE001 - 记忆失败不能挡住工具
            log.debug("直播工具查记忆失败", exc_info=True)
            return ""
        rooms = self._rooms_from_rows(rows)
        if not rooms:
            try:
                rows = self.memory.knowledge_search(target, 200)
            except Exception:  # noqa: BLE001
                log.debug("直播工具查记忆失败", exc_info=True)
                return ""
            rooms = self._rooms_from_rows(rows)
        # 只认唯一一个；多个直播间链接就宁可让模型问，不随便挑
        return rooms[0] if len(rooms) == 1 else ""

    @staticmethod
    def _rooms_from_rows(rows: list[dict[str, Any]]) -> list[str]:
        rooms: list[str] = []
        for row in rows:
            blob = " ".join(
                str(row.get(key) or "") for key in ("topic", "text", "url")
            )
            for match in _LIVE_URL_RE.finditer(blob):
                candidate = match.group(1)
                if candidate not in rooms:
                    rooms.append(candidate)
        return rooms

    @staticmethod
    def _not_found(target: str, candidates: list[str]) -> str:
        if candidates:
            return (
                f"没认准「{target}」是哪个直播间。B 站搜到的有："
                + "、".join(candidates)
                + "。你说是哪一个？或者直接把直播间号发我。"
            )
        return (
            f"没找到「{target}」的直播间。可以发房间号，"
            "或者先 bili_search 确认一下 UP 主。"
        )

    # ---------- 动作 ----------

    async def status(self, target: str = "") -> str:
        """给命令/工具用的公开包装：不传 target 就看当前房间。"""
        target = str(target or "").strip()
        if target:
            return await self._status_of(target)
        return self.watcher.status_text()

    def _history_line(self, room: str) -> str:
        if self.history is None or not room:
            return ""
        try:
            return self.history.day_line(room)
        except Exception:  # noqa: BLE001 - 记录库坏了不能挡住状态查询
            return ""

    async def history_text(self, target: str = "", day: str = "") -> str:
        """「今天开播了吗 / 开播记录」：只读本地记录，不换房间、不联网。"""
        target = self._clean_target(target)
        if target:
            room, _why, candidates = await self._resolve(target)
            if not room:
                return self._not_found(target, candidates)
        else:
            room = str(self.watcher.room_id or "").strip()
            if not room:
                getter = getattr(self.prefs, "bili_live_last_room", None)
                if callable(getter):
                    try:
                        room = str(getter() or "").strip()
                    except Exception:  # noqa: BLE001
                        room = ""
            if not room and self.history is not None:
                room = self.history.latest_room()
            if not room:
                return "还没陪看过直播间，没有开播记录。先「看直播 房间号」。"
        if self.history is None:
            return "开播记录没接上，先查不了。"
        if is_recent_day(day):
            return self.history.recent_report(room)
        parsed = parse_day(day or "今天")
        if not parsed:
            return (
                f"没认出「{day}」是哪天；用今天/昨天/前天，"
                "或者 2026-11-08 这种日期。"
            )
        return self.history.day_report(room, parsed)

    def danmaku(self) -> str:
        """读当前房间的实时缓冲；没有就明说，不编。"""
        room = self.watcher.room_id
        if not room:
            return (
                "现在没在盯直播间，弹幕箱是空的。先告诉我看谁的"
                "（用 bili_live_watch，target 可以是名字/房号/链接）。"
            )
        info = self.watcher.info
        if not info.fetched_at:
            return "刚盯上，还没拿到房间状态；过几秒再问。"
        if not info.live:
            return f"直播间 {room} 现在没开播，没有弹幕。"
        text = self.watcher.recent_text(15)
        if text:
            return f"直播间 {room} 最近弹幕：\n{text}"
        return "盯上了，但还没收到弹幕；等一会儿再问。"

    async def watch(self, target: str, session: str = "") -> str:
        """给命令入口用的公开包装：解析名字/房号并开始盯。"""
        return await self._watch(target, session)

    async def _watch(self, target: str, session: str) -> str:
        target = self._clean_target(target)
        if not target:
            return (
                "要看哪个直播间？给我房间号、直播间链接，或者主播名字都行。"
            )
        room, why, candidates = await self._resolve(target)
        if not room:
            return self._not_found(target, candidates)

        self.watcher.start(room, session)
        # 同一个房间再开一次 = 把当前会话加进名单，别把别的群挤掉。
        # 2026-11-04 实锤：一群先开、二群再按名字开，这里 set_bili_live 把
        # 名单重置成只有二群，重启恢复就只剩二群，一群收不到开播通知。
        if self.prefs.bili_live_room() == room:
            self.prefs.add_bili_live_session(session)
        else:
            self.prefs.set_bili_live(room, session)
        self.config.enable_bili_live = True
        self.ensure_loop()

        title = ""
        live = False
        fetched = False
        try:
            info = await self.watcher.fetch_info(room)
            title = info.title
            live = info.live
            fetched = True
        except Exception:  # noqa: BLE001 - 房间信息拿不到也先盯上
            log.debug("直播工具拿房间信息失败：%s", room, exc_info=True)

        how = f"（{why}）" if why else ""
        if fetched and live:
            head = f"盯上了 {room}{how}：《{title or '未命名'}》现在正在播。"
        elif fetched:
            head = f"盯上了 {room}{how}：现在没播，开播会叫你。"
        else:
            head = f"盯上了 {room}{how}：开播状态下一轮确认。"
        if getattr(self.watcher, "frames_enabled", False):
            view = "只看得到标题、弹幕和后台一直在抓的最近几帧画面，听不到声音"
        else:
            view = "只看房间标题和弹幕，看不到画面、也听不到声音"
        return head + view + "；开播/下播会在这个会话里说一声。"

    async def _status_of(self, target: str) -> str:
        """只读查某个主播/房间，不改变当前盯的房间。"""
        target = self._clean_target(target)
        room, why, candidates = await self._resolve(target)
        if not room:
            return self._not_found(target, candidates)
        try:
            info = await self.watcher.fetch_info(room)
        except Exception:  # noqa: BLE001 - 查不到状态就明说
            log.debug("直播工具查开播状态失败：%s", room, exc_info=True)
            suffix = self._history_line(room)
            return (
                f"找到了「{target}」的直播间 {room}，但这次开播状态没查到"
                "（接口没返回），先别猜。"
                + (f"\n📅 {suffix}" if suffix else "")
            )
        title = info.title or "未命名"
        how = f"（{why}）" if why else ""
        suffix = self._history_line(room)
        suffix_text = f"\n📅 {suffix}" if suffix else ""
        if info.live:
            online = f"｜人气 {info.online}" if info.online else ""
            if getattr(self.watcher, "frames_enabled", False):
                # 开了直播画面之后再说「看不到画面」就是旧描述了：后台一直在抓，
                # 说「看看画面」/调 bili_live_frames 就能拿最近几帧（不是连续视频）。
                view = (
                    "只读到标题和公开状态；画面后台一直在抓，"
                    "说「看看画面」或调 bili_live_frames 就能看最近几帧，"
                    "听不到声音。"
                )
            else:
                view = "只读到标题和公开状态，看不到画面。"
            return (
                f"「{target}」的直播间 {room}{how}：正在播《{title}》{online}。"
                f"{view}" + suffix_text
            )
        return f"「{target}」的直播间 {room}{how}：现在没播。" + suffix_text

    def _stop(self) -> str:
        room = self.watcher.room_id
        if not room:
            return "本来就没在看直播。"
        self.watcher.stop()
        self.prefs.clear_bili_live()
        self.config.enable_bili_live = False
        log.info("主人（工具）不盯直播了：房间 %s", room)
        return f"不盯直播间 {room} 了。"
