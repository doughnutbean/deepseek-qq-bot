"""她自己的 B 站生活：只给主人用的几个工具。

- `bili_life_scroll`：现在就去刷一轮（手动触发，不等定时）；
- `bili_life_status`：看今天刷了没、挑了几条、搜索/互动额度还剩多少；
- `bili_life_pause` / `bili_life_resume`：停/恢复后台自动轮；
- `bili_life_interest_add` / `bili_life_interest_mute`：主人说“以后多搜 X / 别再看 X”。

后台定时不经过模型，直接由 `BiliLife.tick()` 跑；这些工具只是让主人
能当场叫她刷、调她的兴趣、或者问一句进度。群友看不到、也调不动。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .tool_spec import make_tool

log = logging.getLogger(__name__)


class BiliLifeTools:
    def __init__(self, life: Any) -> None:
        self.life = life

    def schemas(self) -> list[dict[str, Any]]:
        return [
            make_tool(
                "bili_life_scroll",
                "让本鱼现在去刷一轮 B 站（只用自己的小号，按她自己的兴趣挑 0~3 条，"
                "看完留生活记忆，不会在群里刷屏）。主人说「去刷会儿B站」「自己刷一下」"
                "「看看B站有什么」「刷一轮」时用；会真的发首页/搜索请求、读档案，"
                "开了互动时还可能点赞/收藏/关注/评论。只有主人能用。"
                "返回这一轮挑了什么、她的一句话反应；一条没挑也会如实说。",
            ),
            make_tool(
                "bili_life_status",
                "查看本鱼 B 站生活的状态：今天刷了几轮、挑了几条、搜了几次、看了几条画面、"
                "最近挑的是什么、今天的搜索/互动/画面额度还剩多少、下次大概什么时候。"
                "主人问「B站刷得怎么样」「刷B站状态」时用；只读，不改任何东西。"
                "只有主人能用。",
            ),
            make_tool(
                "bili_life_today",
                "列出本鱼今天挑中的所有 B 站视频，不只最近一轮；按「想跟主人说 / 有意思 / "
                "一般」排序，并给出总数。主人问「今天刷到什么了」「今天有没有有意思的」"
                "「把今天看的列一下」时用；只读。只有主人能用。",
                {
                    "limit": {
                        "type": "integer",
                        "description": "最多列几条，1~20；不传默认 10",
                    }
                },
            ),
            make_tool(
                "bili_life_style_fix",
                "主人说本鱼刚才那句话「不像人话」「太AI了」「评论腔」「重说」时，"
                "把原句存成风格负例，下次生成会避雷。bad 是原句；如果主人给了改写版，"
                "放进 good。只记风格，不删 B 站记忆。只有主人能用。",
                {
                    "bad": {"type": "string", "description": "被主人指出不像人话的原句"},
                    "good": {"type": "string", "description": "主人给的改写版，可选"},
                },
            ),
            make_tool(
                "bili_life_pause",
                "暂停本鱼的自动刷 B 站。主人说「别刷了」「先停一下」「这两天别刷」时用；"
                "只停后台自动轮，主人手动说「去刷会儿」仍然可以刷。默认停 24 小时，"
                "也可以按主人说的天数传 hours。只有主人能用。",
                {
                    "hours": {
                        "type": "integer",
                        "description": "暂停多少小时，1~168；不传默认 24",
                    }
                },
            ),
            make_tool(
                "bili_life_resume",
                "恢复本鱼的自动刷 B 站。主人说「继续刷」「恢复B站生活」「可以刷了」时用。"
                "只有主人能用。",
            ),
            make_tool(
                "bili_life_interest_add",
                "把主人说的主题记进本鱼自己的 B 站兴趣库，以后选搜索词时会优先考虑。"
                "主人说「以后多搜鲸鱼」「多留意深海」「多看看做饭」时用；只记兴趣，"
                "不会立刻刷，也不会立刻发评论。只有主人能用。",
                {
                    "topic": {
                        "type": "string",
                        "description": "兴趣词，2~24 字，比如「深海」「家常菜」",
                    }
                },
                ["topic"],
            ),
            make_tool(
                "bili_life_interest_mute",
                "把某个主题从本鱼的 B 站兴趣里静音，以后计划不再主动搜它。"
                "主人说「别再看 X」「少搜点 X」时用；不删历史，只静音一年。"
                "只有主人能用。",
                {
                    "topic": {
                        "type": "string",
                        "description": "要静音的兴趣词，2~24 字",
                    }
                },
                ["topic"],
            ),
        ]

    async def run(
        self,
        name: str,
        arguments: str = "{}",
        turn: Any = None,
        **kwargs: Any,
    ) -> str | None:
        if turn is not None:
            kwargs = turn.as_ctx()
        if name not in {
            "bili_life_scroll",
            "bili_life_status",
            "bili_life_today",
            "bili_life_style_fix",
            "bili_life_pause",
            "bili_life_resume",
            "bili_life_interest_add",
            "bili_life_interest_mute",
        }:
            return None
        if not kwargs.get("is_owner"):
            return "只有主人能让本鱼去刷B站。"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name == "bili_life_status":
            return self._status_text()
        if name == "bili_life_today":
            try:
                limit = int(args.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            return self._today_text(limit)
        if name == "bili_life_style_fix":
            bad = str(args.get("bad") or "").strip()
            good = str(args.get("good") or "").strip()
            ok = self.life.add_style_note(bad, good)
            return "记住了，下次不这么写。" if ok else "原句太短，没记下来。"
        if name in {"bili_life_interest_add", "bili_life_interest_mute"}:
            topic = str(args.get("topic") or "").strip()
            if len(topic) < 2:
                return "给个具体点的词，比如「深海」或「家常菜」。"
            if name == "bili_life_interest_add":
                ok = self.life.add_interest(topic, weight=3.0)
                return f"记住了，以后多留意「{topic}」。" if ok else "这个词没记进去（可能兴趣库满了）。"
            ok = self.life.mute_interest(topic)
            return f"好，以后不主动搜「{topic}」了。" if ok else "没静音成功，等会儿再试。"
        if name == "bili_life_pause":
            try:
                hours = int(args.get("hours") or 24)
            except (TypeError, ValueError):
                hours = 24
            until = self.life.pause(hours)
            import time as _time

            return (
                "先不刷了，停到 "
                + _time.strftime("%m-%d %H:%M", _time.localtime(until))
                + "；主人想现在看就说「去刷会儿」。"
            )
        if name == "bili_life_resume":
            self.life.resume()
            return "好，B站生活继续。"
        dry_run = bool(args.get("dry_run"))
        try:
            result = await self.life.scroll_once("manual", dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            log.exception("手动刷B站失败")
            return f"这轮没刷成：{type(exc).__name__}。等会儿再试。"
        return self._result_text(result)

    def _result_text(self, result: dict[str, Any]) -> str:
        if result.get("skipped"):
            return {
                "disabled": "B站生活没开（缺开关或小号登录态）。",
                "busy": "上一轮还在刷，等它刷完。",
                "daily-picks": "今天已经刷够了，明天再说。",
                "paused": "现在先不刷（风控/暂停中）。",
                "outside-window": "现在不在她刷B站的时间。",
                "daily-runs": "今天已经刷过一轮了。",
                "min-gap": "两轮之间还没到间隔，晚点再刷。",
            }.get(str(result["skipped"]), "这轮没刷：先歇着。")
        picked = int(result.get("picked") or 0)
        candidates = int(result.get("candidates") or 0)
        if not result.get("ok"):
            return f"这轮没刷成：{result.get('note') or result.get('error') or '未知原因'}。"
        if not picked:
            return f"这轮看了 {candidates} 个候选，没挑到想看的。{result.get('note') or ''}".strip()
        lines = [f"刷完了：看了 {candidates} 个候选，挑了 {picked} 条。"]
        if result.get("searches"):
            lines[0] = (
                f"刷完了：搜了 {int(result.get('searches') or 0)} 次，"
                f"看了 {candidates} 个候选，挑了 {picked} 条。"
            )
        for item in (result.get("results") or [])[:3]:
            title = str(item.get("title") or "")[:40]
            reaction = str(item.get("reaction") or "").strip()
            acts = [
                str(x.get("action"))
                for x in (item.get("actions") or [])
                if x.get("ok")
            ]
            tail = f"（{'/'.join(acts)}）" if acts else ""
            lines.append(f"·《{title}》{tail}" + (f"：{reaction}" if reaction else ""))
        return "\n".join(lines)[:400]

    def _today_text(self, limit: int = 10) -> str:
        limit = max(1, min(20, int(limit)))
        items = self.life.today_picks(50)
        if not items:
            return "今天还没挑到想看的。"
        counts = {"想跟主人说": 0, "有意思": 0, "一般": 0}
        for item in items:
            tier = str(item.get("tier") or "一般")
            counts[tier] = counts.get(tier, 0) + 1
        lines = [
            f"今天挑中 {len(items)} 条"
            f"（想跟主人说 {counts['想跟主人说']} / 有意思 {counts['有意思']} / "
            f"一般 {counts['一般']}）："
        ]
        for item in items[:limit]:
            title = str(item.get("title") or "")[:26]
            tier = str(item.get("tier") or "一般")
            lines.append(f"·《{title}》{tier}")
        if len(items) > limit:
            lines.append(f"还有 {len(items) - limit} 条没列。")
        return "\n".join(lines)[:400]

    def _status_text(self) -> str:
        s = self.life.status()
        if not s.get("enabled"):
            return "B站生活没开。"
        actions = s.get("actions_today") or {}
        bits = [
            "B站生活：开着",
            f"今天 {s.get('today_runs', 0)} 轮 / {s.get('today_picks', 0)} 条",
            f"搜索 今天 {s.get('today_searches', 0)}/{s.get('daily_searches', 0)} 次，"
            f"本轮上限 {s.get('searches_per_run', 0)} 次",
        ]
        if s.get("interactions"):
            bits.append(
                f"互动 赞{actions.get('like', 0)} 藏{actions.get('collect', 0)} "
                f"关{actions.get('follow', 0)} 评{actions.get('comment', 0)}"
            )
        else:
            bits.append("互动关")
        if s.get("tell_daily"):
            bits.append(f"告诉主人 {s.get('told_today', 0)}/{s.get('tell_daily')}")
        if s.get("vision_daily"):
            bits.append(
                f"看画面 今天 {s.get('vision_today', 0)}/{s.get('vision_daily', 0)} 条，"
                f"本轮最多 {s.get('vision_max_videos', 0)} 条"
            )
        interests = [str(x) for x in (s.get("interests") or [])[:4] if x]
        if interests:
            bits.append("兴趣：" + "、".join(interests))
        if s.get("paused_until"):
            import time as _time

            if float(s["paused_until"]) > _time.time():
                bits.append(
                    "暂停到 "
                    + _time.strftime("%m-%d %H:%M", _time.localtime(float(s["paused_until"])))
                )
        if s.get("last_result"):
            bits.append(f"上次：{s['last_result']}")
        recent = [
            str(item.get("title") or "")[:24]
            for item in (s.get("recent") or [])[:3]
            if item.get("title")
        ]
        if recent:
            bits.append("最近：" + "；".join(f"《{x}》" for x in recent))
        if s.get("next_due"):
            import time as _time

            bits.append(
                "下次大概 " + _time.strftime("%m-%d %H:%M", _time.localtime(float(s["next_due"])))
            )
        return "；".join(bits)[:400]
