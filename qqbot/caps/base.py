"""通用能力的端口层：业务只依赖这里的接口，不直接 import 具体实现。

为什么要这一层（2026-11-05 定的三条之一）：
「屏幕理解 / ASR / TTS / 画面抽帧」这类是通用能力，未来大概率要换成现成的
（比如 dsh-see-now 那种独立导出的库，或者一个本地 HTTP 服务）。如果业务代码
直接 `import live_frames` 到处调，将来换实现就得改一片。

规矩：
- 业务代码只拿 `Capabilities` 里那四个对象；
- 具体实现放 `capabilities/local.py`（现在）或未来的 `capabilities/external.py`；
- 端口方法一律 async、失败返回 None/空串，不许抛 —— 能力挂了不能把聊天打挂。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Capabilities",
    "FrameSource",
    "Screen",
    "Speech",
    "Voice",
    "NullFrames",
    "NullScreen",
    "NullSpeech",
    "NullVoice",
]


@runtime_checkable
class FrameSource(Protocol):
    """「我要一张最近的画面」——直播快照、视频分镜都归这。"""

    async def snapshot(self) -> Any | None:
        """拿一张图（实现自己决定是拼图还是单帧）；拿不到返回 None。"""


@runtime_checkable
class Screen(Protocol):
    """看屏幕（桌面侧）。服务器上没有，默认空实现。"""

    async def capture(self) -> bytes | None:
        """截一张当前屏幕；不支持就返回 None。"""


@runtime_checkable
class Speech(Protocol):
    """听声音：音频 → 文字（ASR）。"""

    async def transcribe(self, path: Any) -> str:
        """转写一段音频；没有引擎就返回空串。"""


@runtime_checkable
class Voice(Protocol):
    """说话：文字 → 语音（TTS）。"""

    async def speak(self, text: str) -> None:
        """把一句话念出来；没有引擎就什么都不做。"""


class NullFrames:
    async def snapshot(self) -> Any | None:
        return None


class NullScreen:
    async def capture(self) -> bytes | None:
        return None


class NullSpeech:
    async def transcribe(self, path: Any) -> str:
        return ""


class NullVoice:
    async def speak(self, text: str) -> None:
        return None


@dataclass
class Capabilities:
    """这一台机器上「能用的通用能力」。

    默认全是空实现：没配 ASR/TTS 就安安静静地没有，不影响聊天。
    """

    frames: FrameSource
    screen: Screen
    speech: Speech
    voice: Voice

    @classmethod
    def empty(cls) -> "Capabilities":
        return cls(
            frames=NullFrames(),
            screen=NullScreen(),
            speech=NullSpeech(),
            voice=NullVoice(),
        )
