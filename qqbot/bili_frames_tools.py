"""让模型自己决定「要不要看 B 站视频的画面」。

以前只有一条路：这句话里出现「画面 / 长什么样 / 截图」等关键词，`_bili_shots`
才抽帧。换个说法（「这视频里拍了啥」「里面是不是有只猫」）就命中不了，
她只能拿字幕和简介硬答，或者干脆说没看过。

这里把「看画面」做成工具：模型自己判断这句是不是真的需要画面，再调
`bili_frames`。抽帧还是走 `BiliCards.shots()/zoom()`（要下载整段视频，几十 MB，
最多等 `BILI_FRAMES_TIMEOUT` 秒），所以：

- 按天限额（`BILI_FRAMES_TOOL_DAILY`，0 = 不封顶）；
- 太长的视频不抽（默认 40 分钟，下载/抽帧基本会超时）；
- 关键词路径已经把分镜塞进这一轮 prompt 时（`bili_frames_seen`），工具直接
  返回「这轮已经看过了」，不重复下载、不重复花 vision；
- 抽到画面后由 `describe` 做一次视觉调用，把画面描述成文字交回主模型。

只读：不改 B 站状态、不发东西；失败如实说，不编画面。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .bili import format_duration, frame_window, live_room_of
from .tool_spec import make_tool

log = logging.getLogger(__name__)

# 同一条视频抽帧失败后，多久内不再重试：每次都要等下载超时，连着试很蠢。
FAIL_COOLDOWN = 600.0

DESCRIPTION = (
    "看一条 **B 站视频**的真实画面（不是直播）：会下载这条视频，按镜头抽 12 格"
    "拼成一张分镜图，先看一眼，再把画面里确凿可见的东西告诉你（谁、在做什么、"
    "场景、屏幕上的字），并标注每格左上角的时间点。只在真的需要知道视频画面时用："
    "有人说「看看画面」「长什么样」「这视频里拍了啥」「03:10 那段是什么」，或者"
    "光靠字幕/简介答不上来。会下载整段视频、最多等 60 秒，并占用当天额度；"
    "不要每句话都调，也不要拿它代替 bili_search / web_fetch。"
    "**正在陪看的直播间用 bili_live_frames，不要用这个。**"
    "url 不传就用这个会话最近聊到的 B 站视频。返回画面描述；抽不到会如实说。"
)


class DailyQuota:
    """按天计数（本地 JSON）；daily<=0 表示不封顶。"""

    def __init__(self, path: Path | None, daily: int = 6) -> None:
        self.path = Path(path) if path else None
        self.daily = max(0, int(daily))
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def _roll(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._data.get("day") != today:
            self._data = {"day": today, "count": 0}

    def used(self) -> int:
        self._roll()
        try:
            return max(0, int(self._data.get("count") or 0))
        except (TypeError, ValueError):
            return 0

    def allow(self) -> tuple[bool, str]:
        if self.daily <= 0:
            return True, ""
        if self.used() >= self.daily:
            return False, (
                f"今天看画面的额度用完了（{self.daily} 次/天），明天再说；"
                "这轮先用字幕、简介和封面回答，别编画面。"
            )
        return True, ""

    def mark(self) -> None:
        if self.daily <= 0:
            return
        self._roll()
        self._data["count"] = self.used() + 1
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # 额度写不进去也不能挡住这一轮
            log.debug("B站画面额度写失败：%s", exc)


class BiliFramesTools:
    """一个很薄的 provider：schema + run，和 LiveTools/SearchTools 同一套接口。"""

    def __init__(
        self,
        cards: Any,
        describe: Callable[[list[dict[str, Any]], str, str], Awaitable[str]],
        last_link: Callable[[str], str] | None = None,
        live_hint: Callable[[str, bool], bool] | None = None,
        state_path: str | Path | None = None,
        daily: int = 6,
        max_minutes: int = 40,
    ) -> None:
        self.cards = cards
        # describe(图片块, 标题, 用户在问什么) -> 画面描述（做一次视觉调用）
        self._describe = describe
        # 这一轮消息里没带链接时，用会话里最近聊到的那条（app.last_bili）
        self._last_link = last_link
        # 这个会话是不是正在陪看直播：是的话，没 url 也别让她拿视频工具瞎找
        self._live_hint = live_hint
        self.quota = DailyQuota(
            Path(state_path) if state_path else None, daily=daily
        )
        self.max_minutes = max(1, int(max_minutes))
        self._failed: dict[str, float] = {}

    def schemas(self) -> list[dict[str, Any]]:
        return [
            make_tool(
                "bili_frames",
                DESCRIPTION,
                {
                    "url": {
                        "type": "string",
                        "description": (
                            "视频链接或 BV 号；不传就用这个会话最近聊到的 B 站视频"
                        ),
                    },
                    "moment": {
                        "type": "string",
                        "description": (
                            "只看某一段时传时间点，如「03:10」或「03:10-04:00」；"
                            "不传就看整段分镜"
                        ),
                    },
                },
            )
        ]

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        *,
        turn: Any = None,
        **kwargs: Any,
    ) -> str | None:
        if name != "bili_frames":
            return None
        ctx: dict[str, Any] = turn.as_ctx() if turn is not None else dict(kwargs)
        if ctx.get("catchup"):
            return "这是上线补收的旧消息，不补看画面；先按现有资料回答。"
        if not getattr(self.cards, "frames_enabled", False):
            return (
                "看画面没开（ENABLE_BILI_FRAMES 关着），现在只能看标题、简介、"
                "字幕和封面。"
            )
        if ctx.get("bili_frames_seen"):
            return (
                "这一轮已经给你看过分镜图了（就在上面），直接按画面回答，"
                "不用再抽一次。"
            )
        try:
            args = json.loads(arguments or "{}")
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}

        url = str(args.get("url") or "").strip()
        if not url and callable(self._last_link):
            try:
                url = str(self._last_link(str(ctx.get("session") or "")) or "").strip()
            except Exception:  # noqa: BLE001 - 查会话最近视频失败就当没传
                url = ""
        if not url:
            live = False
            if callable(self._live_hint):
                try:
                    live = bool(
                        self._live_hint(
                            str(ctx.get("session") or ""),
                            bool(ctx.get("owner_live") or ctx.get("is_owner")),
                        )
                    )
                except Exception:  # noqa: BLE001 - 陪看状态查不到就按普通视频处理
                    live = False
            if live:
                return (
                    "你现在在陪看直播，直播画面用 bili_live_frames"
                    "（或直接说「看看画面」）；bili_frames 只抽普通视频。"
                )
            return (
                "还不知道要看哪个视频：让对方先发链接/BV 号，或者先说视频名，"
                "再用 bili_search 找到它。"
            )
        if live_room_of(url):
            return (
                "这是 B 站直播间，不是视频；直播画面用 bili_live_frames"
                "（或直接说「看看画面」），bili_frames 只抽视频。"
            )

        info = await self.cards.info(url)
        if not info:
            return (
                "这条 B 站视频没打开（可能不是视频，或者接口超时），看不了画面；"
                "先用标题、简介和字幕回答，别猜画面。"
            )
        bvid = str(info.get("bvid") or "")
        title = str(info.get("title") or "未命名")
        duration = int(info.get("duration") or 0)
        if duration > self.max_minutes * 60:
            return (
                f"这条视频 {format_duration(duration)}，超过 {self.max_minutes} 分钟，"
                "抽帧会超时；先看字幕/简介，或者让对方说个具体时间点。"
            )
        failed = self._failed.get(bvid) if bvid else None
        if failed is not None and time.time() - failed < FAIL_COOLDOWN:
            return (
                "这条视频刚才抽画面没成功，别连着试；这轮先用字幕/简介回答，"
                "别编画面。"
            )
        ok, why = self.quota.allow()
        if not ok:
            return why

        moment = str(args.get("moment") or "").strip()
        blocks: list[dict[str, Any]] = []
        note = ""
        if moment:
            window = frame_window(moment)
            if window:
                blocks, note = await self.cards.zoom(url, window[0], window[1])
        if not blocks:
            blocks = await self.cards.shots(url)
            cells = int(getattr(self.cards, "frames_count", 12) or 12)
            note = (
                f"（按镜头抽了 {cells} 格分镜，每格左上角是时间点；"
                "没有声音，也不是整段视频）"
            )
        if not blocks:
            if bvid:
                self._failed[bvid] = time.time()
            return (
                "抽画面没成功（视频流拿不到或超时）；这轮先用字幕、简介回答，"
                "别编画面。"
            )

        self.quota.mark()
        # 同一个 Turn 里模型可能连着调两次：第二次别再花一次视觉调用。
        if turn is not None:
            try:
                turn.services["bili_frames_seen"] = True
            except (AttributeError, TypeError):
                pass
        log.info(
            "B站画面工具：%s（%d 张图，今日额度 %d/%s）",
            bvid or url[:40],
            len(blocks),
            self.quota.used(),
            self.quota.daily or "不限",
        )
        question = str(ctx.get("ori_question") or ctx.get("text") or "").strip()
        try:
            desc = await self._describe(blocks, title, question)
        except Exception:  # noqa: BLE001 - 视觉调用失败不能把这一轮打挂
            log.info("B站画面描述失败：%s", bvid, exc_info=True)
            desc = ""
        desc = str(desc or "").strip()
        if not desc:
            return "画面抽到了，但这次没看清；先用字幕/简介回答，别编画面。"
        return f"（《{title[:40]}》的画面：{desc[:600]}）\n{note}"
