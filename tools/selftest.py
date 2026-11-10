"""不连 QQ 也能跑的自检：验消息解析、Markdown 清理、长文切分。

    python tools/selftest.py

生气的设定（怒气分档 / 硬拒）单独一份：tools/test_mood.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbot.app import segments_to_text  # noqa: E402
from qqbot.llm import clean_for_qq, split_text  # noqa: E402

FAILED = 0


def check(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED += 1
        print(f"      got : {got!r}\n      want: {want!r}")


def main() -> int:
    segs = [
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": " hi"}},
        {"type": "image", "data": {"file": "x.png"}},
        {"type": "at", "data": {"qq": "222"}},
    ]
    check("解析@+图片", segments_to_text(segs, "10001"), ("hi[图片]@222", True))
    check("没被@", segments_to_text([{"type": "text", "data": {"text": "yo"}}], "10001"), ("yo", False))
    check("只@了别人", segments_to_text([{"type": "at", "data": {"qq": "222"}}], "10001"), ("@222", False))
    check("纯文本", segments_to_text([{"type": "text", "data": {"text": "  你好  "}}], "10001"), ("你好", False))

    md = "## 标题\n**重点** 和 `代码`\n\n- 第一\n- 第二"
    check("清理Markdown", clean_for_qq(md), "标题\n重点 和 代码\n\n· 第一\n· 第二")

    long_text = "\n\n".join(["段落" * 60 for _ in range(4)])
    parts = split_text(long_text, 200)
    check("长文切成多段", len(parts) > 1, True)
    check("每段不超长", all(len(p) <= 200 for p in parts), True)
    check("短文本不切", split_text("你好", 600), ["你好"])
    check("最多3段", len(split_text("字" * 5000, 100)), 3)

    print()
    print("全部通过" if FAILED == 0 else f"{FAILED} 项失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
