"""看每次请求的固定前缀花在哪 —— 人设、规则、能力表、表情、工具各多少。

    python tools/prompt_budget.py                 # 群聊前缀 + 今天的实测
    python tools/prompt_budget.py --scenario owner
    python tools/prompt_budget.py --sections      # 人设每一段值多少 token
    python tools/prompt_budget.py --json
    python tools/prompt_budget.py --check 14000   # 超过 14000 就返回 1
    python tools/prompt_budget.py --simulate 8000 # 人设砍到 8000 字能省多少

只读：不调模型、不改 prompt、不写库。估算口径见 qqbot/promptbudget.py；
只要 usage.db 里有缓存前缀的实测值，各段会按实测等比缩放，比例是准的。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbot.app import Bot  # noqa: E402
from qqbot.capabilities import prompt_block  # noqa: E402
from qqbot.config import DATA_DIR, ROOT, Config, load_env  # noqa: E402
from qqbot.friends import FriendTools  # noqa: E402
from qqbot.memory_tools import MemoryTools  # noqa: E402
from qqbot.ownerask import OwnerAsk, OwnerReplyTools  # noqa: E402
from qqbot.persona_layers import load_plan  # noqa: E402
from qqbot.promptbudget import (  # noqa: E402
    measure,
    scaled,
    sticker_prompt,
    total_of,
)
from qqbot.selftools import SelfTools  # noqa: E402
from qqbot.stickers import StickerBook  # noqa: E402
from qqbot.tools import SearchTools  # noqa: E402

SCENARIOS = ("group", "private", "owner")
DEFAULT_BUDGET = 14000


class _Shim:
    """只为了让 Bot._system_prompt 能算，不启动机器人。"""


def build_system(
    config: Config, stickers: StickerBook, scenario: str, plan: object | None = None
) -> str:
    shim = _Shim()
    shim.config = config
    shim.started_at = time.time()
    shim.stickers = stickers
    shim.persona_plan = plan
    shim._persona_text = lambda is_group: (  # noqa: E731 - 给 shim 补个方法
        plan.core(is_group) if plan is not None else config.persona
    )
    return Bot._system_prompt(shim, is_group=scenario == "group")


def build_tools(config: Config, scenario: str) -> list[dict]:
    """和 llm.reply 的 provider 顺序保持一致（顺序变了前缀缓存也会变）。"""
    tools: list[dict] = []
    if config.enable_search:
        tools += SearchTools.schemas()
    if config.enable_memory and config.enable_memory_tool:
        tools += MemoryTools(enabled=True).schemas()
    if scenario in ("group", "private"):
        tools += OwnerAsk.schemas()
    else:  # owner：主人就在私聊里，不用 ask_owner
        tools += SelfTools.schemas()
        tools += OwnerReplyTools.schemas()
    if scenario in ("private", "owner"):
        tools += FriendTools.schemas()
    return tools


def _where(scenario: str, owner_qq: str) -> tuple[str, tuple]:
    if scenario == "group":
        return "session LIKE 'group:%'", ()
    if scenario == "owner" and owner_qq:
        return "session = ?", (f"private:{owner_qq}",)
    if scenario == "owner":
        return "0", ()
    if owner_qq:
        return "session LIKE 'private:%' AND session <> ?", (f"private:{owner_qq}",)
    return "session LIKE 'private:%'", ()


def measured_prefix(db_path: Path, scenario: str, owner_qq: str) -> int | None:
    """最近 12 小时、最近 100 条请求里缓存命中的众数 —— 当前稳定前缀的实测大小。

    不能按"全部历史"取众数：10-24/25 的前缀小得多，行数一多就把今天
    的值投掉了。也不拿几天前的值给现在做缩放：那个场景最近没流量时，
    宁可退回估算（会明说），也不给一个假精确的数。
    """
    where, args = _where(scenario, owner_qq)
    if not db_path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = db.execute(
            f"""SELECT cached FROM usage
                WHERE cached > 0 AND ts >= strftime('%s','now','-12 hours')
                  AND {where}
                ORDER BY id DESC LIMIT 100""",
            args,
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return None
    if len(rows) < 5:  # 量太少，众数不可信
        return None
    return Counter(int(r[0]) for r in rows).most_common(1)[0][0]


def today_stats(db_path: Path, scenario: str, owner_qq: str) -> dict | None:
    where, args = _where(scenario, owner_qq)
    if not db_path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = db.execute(
            f"""SELECT count(*), avg(prompt), avg(cached), avg(prompt - cached), sum(prompt)
                FROM usage
                WHERE date(ts,'unixepoch','localtime') = date('now','localtime') AND {where}""",
            args,
        ).fetchone()
        db.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return {
        "turns": int(row[0]),
        "avg_prompt": float(row[1] or 0),
        "avg_cached": float(row[2] or 0),
        "avg_uncached": float(row[3] or 0),
        "sum_prompt": int(row[4] or 0),
    }


def run(args: argparse.Namespace) -> int:
    load_env()
    config = Config.from_env()
    stickers = StickerBook(ROOT / "stickers", enabled=config.enable_sticker)
    plan = load_plan(
        config.persona,
        ROOT / "persona.layers.json",
        off_path=DATA_DIR / "persona_layers.off",
    )
    is_group = args.scenario == "group"
    persona_text = plan.core(is_group) if plan.ok else config.persona
    system = build_system(config, stickers, args.scenario, plan)
    tools = build_tools(config, args.scenario)
    cap = prompt_block(config, today=date.today(), is_group=is_group)
    parts = measure(
        system=system,
        persona=persona_text,
        sticker=sticker_prompt(stickers),
        capability=cap,
        tools=tools,
    )
    estimated = total_of(parts)
    db_path = args.db if args.db is not None else ROOT / config.usage_db
    measured = measured_prefix(db_path, args.scenario, config.owner_qq)
    # 改了人设/工具还没重启时，账本里的"实测前缀"是旧配置的，拿它缩放会骗自己
    stale = None
    if measured and estimated and abs(measured - estimated) / estimated > 0.25:
        stale, measured = measured, None
    if measured:
        parts = scaled(parts, measured)
    total = total_of(parts)
    stats = today_stats(db_path, args.scenario, config.owner_qq)
    persona_tokens = parts[0].tokens

    if args.json:
        payload = {
            "scenario": args.scenario,
            "persona_chars": len(config.persona),
            "system_chars": len(system),
            "estimated_tokens": round(estimated),
            "measured_prefix_tokens": measured,
            "prefix_tokens": round(total),
            "layers": {
                "mode": plan.mode(),
                "core_chars": len(persona_text),
                "on_demand_chars": plan.on_demand_chars(is_group),
                "max_chars": plan.max_chars,
            },
            "parts": [
                {
                    "key": p.key,
                    "label": p.label,
                    "chars": p.chars,
                    "tokens": round(p.tokens),
                    "share": round(p.tokens / total, 4) if total else 0,
                }
                for p in parts
            ],
            "today": stats,
            "sections": [
                {"title": s.key, "chars": len(s.text), "layer": s.layer}
                for s in plan.sections
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    label = {"group": "群聊", "private": "私聊（非主人）", "owner": "私聊主人"}[args.scenario]
    print(f"固定前缀（{label}）")
    print(f"  {'段':<22}{'字符':>8}{'≈token':>9}{'占比':>8}")
    for p in parts:
        share = p.tokens / total * 100 if total else 0
        print(f"  {p.label:<20}{p.chars:>10,}{p.tokens:>9,.0f}{share:>7.1f}%")
    print(f"  {'合计':<20}{'':>10}{total:>9,.0f}")
    if measured:
        print(
            f"\n  实测缓存前缀 {measured:,} token（最近 12 小时 100 条请求的众数）"
            "→ 各段已按实测等比缩放"
        )
    elif stale:
        print(
            f"\n  账本里的实测是 {stale:,} token，和当前估算差得太多（改过配置还没重启？），"
            "先按估算"
        )
    else:
        print("\n  （最近 12 小时这个场景没流量，用的是估算）")
    if stats:
        print(
            f"  今天 {stats['turns']:,} 回合：平均输入 {stats['avg_prompt']:,.0f}"
            f"（缓存 {stats['avg_cached']:,.0f} / 未缓存 {stats['avg_uncached']:,.0f}），"
            f"合计 {stats['sum_prompt']:,} token"
        )
    if plan.mode() == "layered":
        print(
            f"  人设分层：常驻 {len(persona_text):,} 字（上表的人设就是它）｜"
            f"另有按需 {plan.on_demand_chars(is_group):,} 字，触发时才进动态"
            f"（单轮上限 {plan.max_chars} 字）"
        )
        print("  回滚：python tools/persona_layers.py --off（下一轮生效，不用重启）")
    else:
        print(f"  人设分层：已关闭（{plan.reason or '回滚开关'}），整份人设都在前缀里")

    if args.sections:
        print(f"\n人设分段（原文 {len(config.persona):,} 字；token 按常驻/按需折算）")
        persona_ratio = persona_tokens / max(1, len(persona_text))
        for section in sorted(plan.sections, key=lambda s: len(s.text), reverse=True):
            tok = len(section.text) * persona_ratio
            print(
                f"  {len(section.text):>6,} 字  {tok:>7,.0f} token  "
                f"[{'常驻' if section.layer == 'core' else '按需'}] "
                f"{section.key}"
            )
        print("  调层只要改 persona.layers.json，跑 python tools/persona_layers.py --check。")

    if args.simulate:
        target = max(1, args.simulate)
        saved = persona_tokens * (1 - target / max(1, len(persona_text)))
        base = stats["avg_prompt"] if stats and stats["avg_prompt"] else total
        print(
            f"\n常驻人设 {len(persona_text):,} → {target:,} 字："
            f"每次少 {saved:,.0f} token（-{saved / max(1.0, base) * 100:.1f}%）"
        )
        if stats and stats["avg_prompt"]:
            print(
                f"  按今天 {stats['turns']:,} 回合：每天少 {saved * stats['turns']:,.0f} token"
            )

    if args.check is not None:
        over = total > args.check
        print(
            f"\n预算 {args.check:,}：{'超了 ' if over else '没超 '}"
            f"{abs(total - args.check):,.0f} token"
        )
        return 1 if over else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="看看固定前缀花在哪")
    parser.add_argument("--scenario", choices=SCENARIOS, default="group")
    parser.add_argument("--sections", action="store_true", help="人设分段成本")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--check",
        nargs="?",
        const=DEFAULT_BUDGET,
        type=int,
        metavar="TOKENS",
        help=f"前缀超过这个数就返回 1（默认 {DEFAULT_BUDGET}）",
    )
    parser.add_argument("--simulate", type=int, metavar="CHARS", help="常驻人设砍到多少字")
    parser.add_argument("--db", type=Path, default=None, help="usage.db 路径")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
