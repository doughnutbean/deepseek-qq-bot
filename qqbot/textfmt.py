"""跟"回执"有关的小工具。

`ack()` 原来写在 app.py 里（`_ack`），拆领域模块时它被两边用到，
就挪到这里，保持只有一份实现。
"""

from __future__ import annotations

__all__ = ["ack"]


def ack(label: str, detail: str) -> str:
    """指令回执：第一行说明这是指令，第二行写清楚改了什么。"""
    return "✅ 指令已执行：" + label + "\n" + detail
