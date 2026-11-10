"""通用能力端口（见 base.py 的说明）。"""

from __future__ import annotations

from .base import (
    Capabilities,
    FrameSource,
    NullFrames,
    NullScreen,
    NullSpeech,
    NullVoice,
    Screen,
    Speech,
    Voice,
)
from .local import WatcherFrames, build_local

__all__ = [
    "Capabilities",
    "FrameSource",
    "NullFrames",
    "NullScreen",
    "NullSpeech",
    "NullVoice",
    "Screen",
    "Speech",
    "Voice",
    "WatcherFrames",
    "build_local",
]
