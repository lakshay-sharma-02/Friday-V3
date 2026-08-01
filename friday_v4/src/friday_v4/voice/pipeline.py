"""VoicePipeline — facade over the single-threaded VoiceEngine.

Voice Wave 2.2 moved the state machine into ``core.VoiceEngine`` (one
loop, one queue, no locks). This module keeps the old ``VoicePipeline``
name and API so the router, CLIs, and callers keep working unchanged.

    Microphone → VAD → Hotword → STT → VoiceRouter → TTS
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .core import (
    PipelineConfig,
    PipelineState,
    VoiceEngine,
    OnError,
    OnStateChange,
    OnTranscription,
)

logger = logging.getLogger("friday_v4.voice.pipeline")


class VoicePipeline:
    """End-to-end voice interaction pipeline (facade over VoiceEngine)."""

    def __init__(self, config: Optional[PipelineConfig] = None,
                 audio=None, vad=None, hotword=None, stt=None, tts=None):
        self.config = config or PipelineConfig()
        self._engine = VoiceEngine(
            self.config, audio=audio, vad=vad, hotword=hotword,
            stt=stt, tts=tts)

    # Callback wiring (delegates straight to the engine)
    @property
    def on_transcription(self) -> Optional[OnTranscription]:
        return self._engine.on_transcription

    @on_transcription.setter
    def on_transcription(self, cb: Optional[OnTranscription]) -> None:
        self._engine.on_transcription = cb

    @property
    def on_state_change(self) -> Optional[OnStateChange]:
        return self._engine.on_state_change

    @on_state_change.setter
    def on_state_change(self, cb: Optional[OnStateChange]) -> None:
        self._engine.on_state_change = cb

    @property
    def on_error(self) -> Optional[OnError]:
        return self._engine.on_error

    @on_error.setter
    def on_error(self, cb: Optional[OnError]) -> None:
        self._engine.on_error = cb

    @property
    def route_function(self) -> Optional[Callable[[str], str]]:
        return self._engine.route_function

    @route_function.setter
    def route_function(self, fn: Optional[Callable[[str], str]]) -> None:
        self._engine.route_function = fn

    @property
    def state(self) -> PipelineState:
        return self._engine.state

    @state.setter
    def state(self, new_state: PipelineState) -> None:
        # Kept for compatibility with code that set state directly
        # (tests); the engine owns state, so this is a no-op passthrough
        # that still fires the callback via the engine.
        self._engine.state = new_state

    @property
    def is_running(self) -> bool:
        return self._engine.is_running

    @property
    def tts(self):
        return self._engine.tts

    @property
    def active_provider(self) -> str:
        return self._engine.active_provider

    def start(self) -> bool:
        return self._engine.start()

    def stop(self) -> None:
        self._engine.stop()

    def wait_until_stopped(self, timeout: Optional[float] = None) -> None:
        self._engine.wait_until_stopped(timeout)

    def speak(self, text: str, mode=None) -> bool:
        return self._engine.speak(text, mode)

    def push_to_talk(self) -> str:
        return self._engine.push_to_talk()

    def stop_recording_and_process(self) -> str:
        return self._engine.stop_recording_and_process()

    def __getattr__(self, name):
        # Any other legacy attributes live on the engine.
        return getattr(self._engine, name)
