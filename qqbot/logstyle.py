"""控制台日志上色：别人说的话一种颜色、她说出去的话另一种，滚屏时好找位置。

几条约定：

- 只在真控制台（``isatty()``）里上色。``python run.py >> data/logs/bot.log`` 那种重定向
  自动退回无色，不然一堆 ``\\x1b[32m`` 会混进日志。
- ``data/logs/bot.log`` 用普通 Formatter，文件里永远没有转义码（它是拿来 grep 的）。
- Windows 老控制台默认不解析转义码：先开 VT 处理，开不了就退回无色，
  绝不会把 ``[1;32m`` 这样的裸码打在屏幕上。
- 尊重 ``NO_COLOR``：``.env`` 里写 ``NO_COLOR=1`` 就能关掉。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

RESET = "\x1b[0m"
HEAD = "\x1b[90m"  # 时间戳/级别/模块名压暗，内容才跳出来
INCOMING = "\x1b[1;36m"  # 青色加粗：收到
OUTGOING = "\x1b[1;32m"  # 绿色加粗：她的回复

# (颜色, 这一行以哪些前缀开头算这一类)。前缀从前往后匹配，第一个命中的算。
# 要再加一类颜色，在这里加一行就够，别去动 format()。
#
# 青色 = 别人发进来的话：「考虑接话/考虑插话」打的就是那条群消息的预览。
# 绿色只标"她真的发出去的那条"。「跟进窗口接话」「为叫她/引用她临时开跟进窗口」
# 「主动插话」是记账行，紧接着还会打一条「回复 …」，文案重复，所以不给它们上色。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (INCOMING, ("收到 ", "考虑接话（", "考虑插话（")),
    (
        OUTGOING,
        (
            "回复 ",  # 注意带空格：「回复里的网址已去掉」是提醒，不算真的回了话
            "补一句：",
            "主动私信：",  # 「主动私信（演练，不真发）」匹配不上：演练没发就不算
            "发送表情包",
        ),
    ),
)


def color_for(message: str) -> str | None:
    """这行消息该用什么颜色；不该上色的返回 None。"""
    for color, prefixes in _RULES:
        if message.startswith(prefixes):
            return color
    return None


class ColorFormatter(logging.Formatter):
    """给命中的行上色：时间戳压暗、消息染色。没命中的行原样输出。

    只切格式串尾部那段消息来染色，不碰 record（同一个 record 还要交给文件
    handler，改了就把它也染了）。
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        super().__init__(fmt, datefmt)
        self.enabled = enabled

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        # 带堆栈的行整体保持原样：绿了一半的 traceback 还不如不上色
        if not self.enabled or record.exc_info:
            return line
        message = record.getMessage()
        color = color_for(message)
        if color is None or not message:
            return line
        # 消息在格式串末尾，rfind 到的是真正那段消息；前面全是时间/级别/模块名
        cut = line.rfind(message)
        if cut <= 0:
            return line
        return f"{HEAD}{line[:cut]}{RESET}{color}{message}{RESET}"


def supports_color(stream: Any) -> bool:
    """这个流能不能安全地写转义码。"""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:  # noqa: BLE001 - 没有 isatty 的流（StringIO、None）都算不行
        return False
    return _enable_windows_vt(stream)


def _enable_windows_vt(stream: Any) -> bool:
    """Windows 控制台要先打开 VT 处理；开不了（老系统/管道）就返回 False。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 显式声明签名：HANDLE 在 64 位上是真指针，按默认的 c_int 传会被截断
        get_mode = kernel32.GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_mode.restype = wintypes.BOOL
        set_mode = kernel32.SetConsoleMode
        set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        set_mode.restype = wintypes.BOOL

        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        mode = wintypes.DWORD()
        if not get_mode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(
            set_mode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:  # noqa: BLE001
        return False


def build_handlers(stream: Any, logfile: Path | None = None) -> list[logging.Handler]:
    """控制台 handler（该上色就上色）+ 文件 handler（纯文本）。"""
    console = logging.StreamHandler(stream)
    console.setFormatter(
        ColorFormatter(DEFAULT_FORMAT, DEFAULT_DATEFMT, enabled=supports_color(stream))
    )
    handlers: list[logging.Handler] = [console]
    if logfile is not None:
        try:
            file_handler = logging.FileHandler(logfile, encoding="utf-8")
        except OSError:
            return handlers
        file_handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT))
        handlers.append(file_handler)
    return handlers
