"""本机实现：把现有模块包成端口。

以后要接外部引擎（dsh-see-now / 本地 ASR 服务 / TTS），在这里加一个
`ExternalFrames` 之类的类即可，业务代码不用改。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capabilities, NullScreen, NullSpeech, NullVoice

log = logging.getLogger(__name__)

__all__ = ["WatcherFrames", "build_local"]


class WatcherFrames:
    """直播快照：直接转给 `BiliLiveWatcher.snapshot()`。

    这一层看着薄，但它把「业务怎么要画面」和「画面谁来抓」分开了：
    业务只说 `await caps.frames.snapshot()`，换成外部采集器时只换这个类。
    """

    def __init__(self, watcher: Any) -> None:
        self._watcher = watcher

    async def snapshot(self) -> Any | None:
        if self._watcher is None:
            return None
        try:
            return await self._watcher.snapshot()
        except Exception:  # noqa: BLE001 - 抓画面失败当这次没有
            log.debug("取直播画面失败", exc_info=True)
            return None


def build_local(watcher: Any = None) -> Capabilities:
    """按本机实际情况装配：画面有（盯着直播时），屏幕/ASR/TTS 先空着。"""
    return Capabilities(
        frames=WatcherFrames(watcher),
        screen=NullScreen(),
        speech=NullSpeech(),
        voice=NullVoice(),
    )
