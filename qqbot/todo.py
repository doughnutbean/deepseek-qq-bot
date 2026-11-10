"""主人临时的待办清单：**只存内存，重启就清空**。

主人说「todo：买牛奶」记一条，「todo」看清单，「todo 删 2」删一条，
「todo 清空」全清。它故意不落盘：

- 写进 `prefs.json` 会跟着重启活下来，和「重启后清空」的约定相反；
- 写进记忆库又太重 —— 想长期记住的事该说「记住：……」（走 memory）。

所以这里没有 save / load，也没有清理旧文件的逻辑。进程重启就是空清单。
"""

from __future__ import annotations

# 一次运行最多记多少条、每条多少字（防止上下文里塞进一整篇小作文）
MAX_ITEMS = 30
MAX_TEXT = 120
# 注入聊天上下文时最多带几条（剩下的只报数量，别每轮都花一大笔）
BRIEF_ITEMS = 8


class TodoList:
    """一次运行内的待办清单。没有文件、没有恢复入口 —— 重启即清空。"""

    def __init__(self) -> None:
        self._items: list[str] = []

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def items(self) -> tuple[str, ...]:
        return tuple(self._items)

    def add(self, text: str) -> str | None:
        """加一条；空白 / 超长 / 满上限都不加，返回 None。"""
        clean = " ".join(str(text or "").split())
        if not clean or len(clean) > MAX_TEXT or len(self._items) >= MAX_ITEMS:
            return None
        self._items.append(clean)
        return clean

    def remove(self, index: int) -> str | None:
        """删第 index 条（从 1 数）；越界返回 None。"""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        if not 1 <= index <= len(self._items):
            return None
        return self._items.pop(index - 1)

    def clear(self) -> int:
        """清空并返回清掉了几条。"""
        count = len(self._items)
        self._items.clear()
        return count

    def brief(self, limit: int = BRIEF_ITEMS) -> str:
        """拼成一行给模型看；太多只带前 limit 条，剩下报数量。"""
        shown = self._items[: max(0, limit)]
        text = "；".join(f"{i}. {item}" for i, item in enumerate(shown, 1))
        rest = len(self._items) - len(shown)
        if rest > 0:
            text += f"；还有 {rest} 条没列"
        return text
