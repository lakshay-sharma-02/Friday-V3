"""VoiceEngine — single-threaded voice state machine (Voice Wave 2.2).

Design: one background loop thread owns ALL state. Everything the voice
pipeline does arrives as a command on a queue:

    frame   → audio callback pushes raw audio (the only cross-thread edge)
    speak   → speak text aloud (proactive / routed responses)
    ptt     → push-to-talk start / stop
    stop    → teardown

There are no locks, no `_speech_gen` counters, no competing watcher
threads: the loop thread is the only reader/writer of state, timers,
and the TTS clock — so races (stale state resets, double speech-end
threads, barge-in vs speak) cannot happen by construction.

States: IDLE → HOTWORD → LISTENING → PROCESSING → SPEAKING → IDLE.
Barge-in (VAD speech while speaking, past a 1.5 s anti-echo window)
returns to LISTENING and interrupts TTS.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .audio import FRAME_SIZE, SAMPLE_RATE
from .chimes import play_chime


def config_from_file(path: Optional[Path] = None) -> PipelineConfig:
    """Build a PipelineConfig from ~/.friday/v4_config.json (if present).

    Uses the loader's default-filling, so a missing/invalid file yields
    the built-in defaults — never raises.
    """
    try:
        from ..config import load_config
        loaded = load_config(path)
        return PipelineConfig(
            hotword=loaded.voice.hotword,
            hotword_sensitivity=loaded.voice.hotword_sensitivity,
            vad_mode=loaded.voice.vad_mode,
            stt_model=loaded.voice.stt_model,
            tts_provider=loaded.voice.tts_provider,
            tts_voice=loaded.voice.tts_voice,
            tts_speed=getattr(loaded.voice, "tts_speed", 1.15),
            silence_timeout_seconds=loaded.voice.silence_timeout_seconds,
            max_utterance_seconds=loaded.voice.max_utterance_seconds,
            enable_chimes=loaded.voice.enable_chimes,
        )
    except Exception:
        return PipelineConfig()

logger = logging.getLogger("friday_v4.voice.core")

#: Frames between interruption checks while speaking (~300 ms).
_INTERRUPT_CHECK_EVERY = 10
#: Speaker-bleed guard: ignore VAD for this long after TTS starts.
_REFRACTORY_SECONDS = 1.5
#: Loop wake interval — bounds timer granularity (silence timeout, speech end).
_LOOP_POLL = 0.05


class PipelineState(Enum):
    IDLE = "idle"
    HOTWORD = "hotword"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


@dataclass
class PipelineConfig:
    """Configuration for the voice pipeline."""
    hotword: str = "hey friday"
    hotword_sensitivity: float = 0.7
    vad_mode: int = 1
    stt_model: str = "base.en"
    tts_provider: str = "piper"
    tts_voice: str = ""
    tts_speed: float = 1.15
    silence_timeout_seconds: float = 2.0
    max_utterance_seconds: float = 30.0
    enable_chimes: bool = True


OnTranscription = Callable[[str], None]
OnStateChange = Callable[[PipelineState], None]
OnError = Callable[[str], None]


class VoiceEngine:
    """End-to-end voice interaction engine.

    Owns the microphone→VAD→hotword→STT→router→TTS flow in a single
    background thread. Components are injectable for tests; when None,
    the real implementations are constructed on ``start()``.

    Public surface mirrors the old ``VoicePipeline`` so the router and
    CLIs keep working: ``start/stop/state/speak/push_to_talk/
    stop_recording_and_process`` plus the callback attributes.
    """

    def __init__(self, config: Optional[PipelineConfig] = None,
                 audio=None, vad=None, hotword=None, stt=None, tts=None):
        self.config = config or PipelineConfig()
        # Injected components (tests); None → built lazily in start().
        self._audio = audio
        self._vad = vad
        self._hotword = hotword
        self._stt = stt
        self._tts = tts

        self._state = PipelineState.IDLE
        self.on_transcription: Optional[OnTranscription] = None
        self.on_state_change: Optional[OnStateChange] = None
        self.on_error: Optional[OnError] = None
        self.route_function: Optional[Callable[[str], str]] = None

        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # Loop-owned state (written only by the loop thread).
        self._recording: list[np.ndarray] = []
        self._last_speech_time: float = 0.0
        self._listen_start: float = 0.0
        self._tts_start_time: float = 0.0
        self._interrupt_frames: int = 0
        self._waiting_speech_end: bool = False
        self._last_text: str = ""

    # ── Public surface ──────────────────────────────────────────────

    @property
    def state(self) -> PipelineState:
        return self._state

    @state.setter
    def state(self, new_state: PipelineState) -> None:
        old = self._state
        self._state = new_state
        if old != new_state and self.on_state_change:
            try:
                self.on_state_change(new_state)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tts(self):
        return self._tts

    @property
    def active_provider(self) -> str:
        return (self._tts.active_provider_name
                if self._tts is not None else "none")

    def start(self) -> bool:
        """Start the engine loop and microphone capture.

        Returns False when no audio backend is available.
        """
        if self._running:
            return False
        if self._audio is None:
            from .audio import AudioStream
            self._audio = AudioStream(
                sample_rate=SAMPLE_RATE, frame_size=FRAME_SIZE)
        if not self._audio.start(self._on_frame):
            self._audio = None
            logger.warning("No audio backend — voice pipeline cannot start")
            return False

        if self._vad is None:
            from .vad import VoiceActivityDetector
            self._vad = VoiceActivityDetector(mode=self.config.vad_mode)
        if self._hotword is None:
            from .hotword import HotwordDetector
            self._hotword = HotwordDetector(
                self.config.hotword, self.config.hotword_sensitivity)
        if self._stt is None:
            from .stt import SpeechToText
            self._stt = SpeechToText(model=self.config.stt_model)
        if self._tts is None:
            from .tts import TextToSpeech, TTSConfig
            self._tts = TextToSpeech(TTSConfig(
                primary_provider=self.config.tts_provider,
                voice=self.config.tts_voice,
                speed=self.config.tts_speed, cache_enabled=True))
        if self._tts is not None and not self._tts.is_available:
            logger.warning("No TTS available — responses will be text-only")

        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="friday-voice", daemon=True)
        self._thread.start()
        self.state = PipelineState.IDLE
        logger.info("Voice engine started")
        return True

    def stop(self) -> None:
        """Stop the loop and release the microphone.

        Joins the loop thread, which is the single closer of the audio
        stream — a dangling PortAudio callback thread can no longer fire
        into a torn-down interpreter at process exit.
        """
        if not self._running:
            return
        self._queue.put(("stop",))
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._running = False
        self.state = PipelineState.IDLE
        logger.info("Voice engine stopped")

    def wait_until_stopped(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    # ── Speaking ────────────────────────────────────────────────────

    def speak(self, text: str, mode=None) -> bool:
        """Speak text aloud (non-blocking). Interrupts current speech."""
        if not text or not self._running:
            return False
        if self._state in (PipelineState.LISTENING,
                           PipelineState.PROCESSING):
            return False  # never talk over the user
        self._queue.put(("speak", text, mode))
        return True

    def push_to_talk(self) -> str:
        """Begin push-to-talk recording. Returns '' (transcribe on stop)."""
        if self._running:
            self._queue.put(("ptt_start",))
        return ""

    def stop_recording_and_process(self) -> str:
        """Stop the current recording, transcribe, return the text."""
        if not self._running:
            return ""
        event = threading.Event()
        self._queue.put(("ptt_stop", event))
        event.wait(timeout=10)
        return self._last_text

    # ── Audio callback (cross-thread edge — enqueue only) ──────────

    def _on_frame(self, frame: np.ndarray) -> None:
        if self._running:
            self._queue.put(("frame", frame))

    # ── The loop ────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            while self._running:
                try:
                    cmd = self._queue.get(timeout=_LOOP_POLL)
                except queue.Empty:
                    cmd = None
                if cmd is None:
                    self._tick()
                    continue
                kind = cmd[0]
                if kind == "stop":
                    break
                if kind == "frame":
                    self._handle_frame(cmd[1])
                elif kind == "speak":
                    self._handle_speak(cmd[1], cmd[2])
                elif kind == "ptt_start":
                    self._start_listening()
                elif kind == "ptt_stop":
                    self._handle_ptt_stop(cmd[1])
                self._tick()
        finally:
            # Single closer of the audio stream (see stop() docstring).
            if self._audio is not None:
                try:
                    self._audio.stop()
                except Exception:
                    pass
            if self._tts is not None:
                try:
                    self._tts.stop()
                except Exception:
                    pass

    def _tick(self) -> None:
        """Periodic checks: speech-end, recording timeouts."""
        if self._waiting_speech_end and self._tts is not None:
            if not self._tts.is_speaking:
                self._waiting_speech_end = False
                if self._state == PipelineState.SPEAKING:
                    self.state = PipelineState.IDLE
        if self._state == PipelineState.LISTENING:
            now = time.time()
            if now - self._last_speech_time > self.config.silence_timeout_seconds \
                    or now - self._listen_start > self.config.max_utterance_seconds:
                self._stop_recording()

    # ── Frame handling per state ────────────────────────────────────

    def _handle_frame(self, frame: np.ndarray) -> None:
        state = self._state
        if state == PipelineState.IDLE:
            self._check_hotword(frame)
        elif state == PipelineState.LISTENING:
            self._record_utterance(frame)
        elif state == PipelineState.SPEAKING:
            self._check_interruption(frame)
        # HOTWORD / PROCESSING: ignore frames.

    def _check_hotword(self, frame: np.ndarray) -> None:
        if not self._hotword:
            return
        try:
            if self._hotword.process(frame):
                logger.info("Hotword detected!")
                self.state = PipelineState.HOTWORD
                if self.config.enable_chimes:
                    play_chime("listen")
                self._start_listening()
        except Exception as exc:
            logger.debug(f"Hotword error: {exc}")

    def _start_listening(self) -> None:
        self._recording = []
        now = time.time()
        self._last_speech_time = now
        self._listen_start = now
        self.state = PipelineState.LISTENING

    def _record_utterance(self, frame: np.ndarray) -> None:
        self._recording.append(frame.copy())
        if self._vad and self._vad.is_speech(frame, SAMPLE_RATE):
            self._last_speech_time = time.time()

    def _stop_recording(self) -> None:
        if not self._recording:
            self.state = PipelineState.IDLE
            return
        audio = np.concatenate(self._recording)
        self._recording = []
        if self.config.enable_chimes:
            play_chime("done")
        self._process_audio(audio, route=True)

    def _process_audio(self, audio: np.ndarray, route: bool) -> str:
        """Transcribe audio; optionally route + speak the response."""
        self.state = PipelineState.PROCESSING
        if not self._stt:
            self._finish_error("No STT available")
            return ""
        result = self._stt.transcribe(audio, SAMPLE_RATE)
        if not result.success or not result.text.strip():
            logger.debug(f"STT: no speech detected ({result.error})")
            self.state = PipelineState.IDLE
            return ""
        text = result.text.strip()
        logger.info(f"Transcribed: \"{text}\" (confidence: {result.confidence:.2f})")
        if self.on_transcription:
            try:
                self.on_transcription(text)
            except Exception:
                pass
        if route and self.route_function:
            try:
                response = self.route_function(text) or ""
            except Exception as exc:
                logger.error(f"Routing error: {exc}")
                self._finish_error(f"Routing error: {exc}")
                return text
            if response:
                self._speak_internal(response)
            else:
                # No response text → return to IDLE. Without this the state
                # machine would sit in PROCESSING forever (hotword detection
                # and push-to-talk both die until restart).
                self.state = PipelineState.IDLE
        else:
            self.state = PipelineState.IDLE
        return text

    def _finish_error(self, error: str) -> None:
        logger.error(error)
        if self.on_error:
            try:
                self.on_error(error)
            except Exception:
                pass
        self.state = PipelineState.IDLE

    # ── Speaking internals ──────────────────────────────────────────

    def _handle_speak(self, text: str, mode) -> None:
        self._speak_internal(text, mode)

    def _speak_internal(self, text: str, mode=None) -> None:
        if not self._tts:
            self.state = PipelineState.IDLE
            return
        self._tts_start_time = time.time()
        self._interrupt_frames = 0
        try:
            self._tts.speak(text, mode)
        except Exception as exc:
            logger.error(f"TTS speak failed: {exc}")
            self._finish_error(f"TTS speak failed: {exc}")
            return
        self._waiting_speech_end = True
        self.state = PipelineState.SPEAKING

    # ── Barge-in ────────────────────────────────────────────────────

    def _check_interruption(self, frame: np.ndarray) -> None:
        if not self._vad or not self._tts:
            return
        self._interrupt_frames += 1
        if self._interrupt_frames < _INTERRUPT_CHECK_EVERY:
            return
        self._interrupt_frames = 0
        # Anti-echo: ignore speaker bleed shortly after playback begins.
        if time.time() - self._tts_start_time < _REFRACTORY_SECONDS:
            return
        if self._vad.is_speech(frame, SAMPLE_RATE):
            logger.debug("User interrupted — stopping TTS")
            self._tts.stop()
            self._waiting_speech_end = False
            self._start_listening()

    # ── Push-to-talk ────────────────────────────────────────────────

    def _handle_ptt_stop(self, event: threading.Event) -> None:
        self._last_text = ""
        if self._state == PipelineState.LISTENING and self._recording:
            audio = np.concatenate(self._recording)
            self._recording = []
            self._last_text = self._process_audio(audio, route=False)
        else:
            self.state = PipelineState.IDLE
        event.set()
