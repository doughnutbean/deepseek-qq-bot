"""人设分层：看状态、检查分层表、一键回滚。

    python tools/persona_layers.py            # 状态 + 每段归哪层
    python tools/persona_layers.py --check    # 分区/触发词/文件检查，不通过返回 1
    python tools/persona_layers.py --off      # 立刻回滚成整份人设（不用重启）
    python tools/persona_layers.py --on       # 恢复分层

--off / --on 只是建/删 data/persona_layers.off。机器人每一轮都会看这个文件，
所以出问题时不用等重启：下一句就按整份人设走。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbot.config import DATA_DIR, ROOT, Config, load_env  # noqa: E402
from qqbot.persona_layers import load_plan  # noqa: E402
from qqbot.promptbudget import estimate_tokens, split_persona  # noqa: E402

LAYERS_PATH = ROOT / "persona.layers.json"
OFF_PATH = DATA_DIR / "persona_layers.off"


def _load() -> tuple[Config, object]:
    load_env()
    cfg = Config.from_env()
    plan = load_plan(cfg.persona, LAYERS_PATH, off_path=OFF_PATH)
    return cfg, plan


def status() -> int:
    cfg, plan = _load()
    print(f"人设：{len(cfg.persona):,} 字")
    print(f"模式：{plan.mode()}（off 文件：{'在' if OFF_PATH.exists() else '不在'}）")
    if not plan.ok:
        print(f"注意：{plan.reason} —— 现在整份人设顶上")
    print(
        f"常驻：群聊 {plan.core_chars(is_group=True):,} 字 / "
        f"私聊 {plan.core_chars(is_group=False):,} 字"
    )
    print(
        f"按需：群聊 {plan.on_demand_chars(is_group=True):,} 字 / "
        f"私聊 {plan.on_demand_chars(is_group=False):,} 字"
        f"（单轮上限 {plan.max_chars} 字）"
    )
    total = 0.0
    print(f"\n{'段':<32}{'层':<10}{'上下文':<8}{'字符':>7}{'token':>8}")
    for section in plan.sections:
        total += estimate_tokens(section.text)
        print(
            f"{section.key[:30]:<32}{section.layer:<10}{section.context:<8}"
            f"{len(section.text):>7,}{estimate_tokens(section.text):>8,.0f}"
        )
    print(f"\n全量约 {total:,.0f} token（分层后常驻只剩上面 core 那几段）")
    return 0


def check() -> int:
    cfg, plan = _load()
    failed = 0

    def ok(name: str, good: bool, detail: str = "") -> None:
        nonlocal failed
        print(f"{'PASS' if good else 'FAIL'}  {name}")
        if not good:
            failed += 1
            if detail:
                print(f"      {detail}")

    ok("分层表能读", plan.ok, plan.reason)
    if not LAYERS_PATH.exists():
        ok("persona.layers.json 存在", False, str(LAYERS_PATH))
        return 1
    try:
        raw = json.loads(LAYERS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        ok("persona.layers.json 是合法 JSON", False, str(exc))
        return 1

    sections = dict(split_persona(cfg.persona))
    core = {str(k) for k in (raw.get("core") or [])}
    context_only = raw.get("context_only") or {}
    on_demand = raw.get("on_demand") or {}
    declared = core | set(on_demand) | set(context_only)

    ok("没有重复分类（core 里又出现在 on_demand）", not (core & set(on_demand)),
       "、".join(sorted(core & set(on_demand))))
    ok("分层表里没有不存在的段", not (declared - set(sections)),
       "、".join(sorted(declared - set(sections))))
    missing = set(sections) - declared
    ok("没有漏分类的段", not missing, "、".join(sorted(missing)))
    ok("on_demand 都是对象", isinstance(on_demand, dict))
    ok("context_only 值只能是 group/private",
       all(v in ("group", "private") for v in context_only.values()),
       str(context_only))
    ok("常驻段没被切空", plan.core(True).strip() != "")

    bad_triggers = [
        key
        for key, triggers in on_demand.items()
        if not isinstance(triggers, list) or not [t for t in triggers if str(t).strip()]
    ]
    ok("每个按需段都有触发词", not bad_triggers, "、".join(bad_triggers))
    dup = [k for k in on_demand if k in context_only]
    ok("按需段没和 context_only 撞车", not dup, "、".join(dup))

    core_tokens = estimate_tokens(plan.core(True))
    print(
        f"\n常驻群聊 ≈ {core_tokens:,.0f} token，"
        f"比整份少约 {estimate_tokens(cfg.persona) - core_tokens:,.0f} token"
    )
    print("全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


def turn_off() -> int:
    OFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFF_PATH.write_text("人设分层已停用；删掉这个文件就恢复。\n", encoding="utf-8")
    print(f"已回滚：整份人设（{OFF_PATH}）。下一轮请求就生效，不用重启。")
    return 0


def turn_on() -> int:
    if OFF_PATH.exists():
        OFF_PATH.unlink()
        print("已恢复分层：下一轮请求生效。")
    else:
        print("分层本来就是开的。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="人设分层：状态 / 检查 / 回滚")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="检查分层表")
    group.add_argument("--off", action="store_true", help="立刻回滚成整份人设")
    group.add_argument("--on", action="store_true", help="恢复分层")
    args = parser.parse_args(argv)
    if args.off:
        return turn_off()
    if args.on:
        return turn_on()
    if args.check:
        return check()
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
