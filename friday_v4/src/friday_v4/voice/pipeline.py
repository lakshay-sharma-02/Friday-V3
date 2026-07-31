"""Voice Pipeline — orchestrates the full voice interaction flow.

Microphone → VAD → Hotword Detection → STT → VoiceRouter → TTS

States:
  IDLE       → listening for hotword (low CPU)
  HOTWORD    → heard "hey friday", preparing
  LISTENING  → recording user speech
  PROCESSING → transcribing + routing
  SPEAKING   → TTS playback (interruptible)

Voice Wave 2.0: cleaner threading, anti-echo refractory window, race-safe
interruption via a speech-generation counter.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import numpy as np

from .audio import AudioStream, FRAME_SIZE, SAMPLE_RATE
from .chimes import play_chime
from .hotword import HotwordDetector
from .stt import SpeechToText
from .tts import TextToSpeech, TTSConfig, VoiceMode

logger = logging.getLogger("friday_v4.voice.pipeline")


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
    tts_provider: str = "kokoro"
    tts_voice: str = ""
    silence_timeout_seconds: float = 2.0
    max_utterance_seconds: float = 30.0
    enable_chimes: bool = True


OnTranscription = Callable[[str], None]
OnStateChange = Callable[[PipelineState], None]
OnError = Callable[[str], None]


class VoicePipeline:
    """End-to-end voice interaction pipeline.

    Runs in background threads. Listens for the hotword, captures speech,
    transcribes, routes through the router, and speaks responses.
    """

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._state = PipelineState.IDLE
        self._state_lock = threading.Lock()

        self._audio: Optional[AudioStream] = None
        self._hotword: Optional[HotwordDetector] = None
        self._vad = None
        self._stt: Optional[SpeechToText] = None
        self._tts: Optional[TextToSpeech] = None

        self._recording: list[np.ndarray] = []
        self._recording_lock = threading.Lock()
        self._last_speech_time: float = 0.0
        self._hotword_detected_time: float = 0.0
        self._tts_start_time: float = 0.0

        # Callbacks
        self.on_transcription: Optional[OnTranscription] = None
        self.on_state_change: Optional[OnStateChange] = None
        self.on_error: Optional[OnError] = None
        self.route_function: Optional[Callable[[str], str]] = None

        self._pipeline_thread: Optional[threading.Thread] = None
        self._running = False

        # Interruption: frame cooldown + speech generation counter +
        # interruption guard so stale threads never clobber state.
        self._interrupt_cooldown = 0
        self._INTERRUPT_CHECK_INTERVAL = 10  # every 10 frames (~300 ms)
        self._speech_gen = 0
        self._speech_gen_lock = threading.Lock()
        self._interrupted = False
        self._interrupted_lock = threading.Lock()

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def state(self) -> PipelineState:
        return self._state

    @state.setter
    def state(self, new_state: PipelineState):
        with self._state_lock:
            old_state = self._state
            self._state = new_state
        if old_state != new_state and self.on_state_change:
            try:
                self.on_state_change(new_state)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tts(self) -> Optional[TextToSpeech]:
        return self._tts

    @property
    def active_provider(self) -> str:
        return self._tts.active_provider_name if self._tts else "none"

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the voice pipeline in background threads."""
        if self._running:
            logger.warning("Pipeline already running")
            return False

        try:
            from .vad import VoiceActivityDetector
            self._hotword = HotwordDetector(
                self.config.hotword, self.config.hotword_sensitivity)
            self._vad = VoiceActivityDetector(mode=self.config.vad_mode)
            self._stt = SpeechToText(model=self.config.stt_model)
            self._tts = TextToSpeech(TTSConfig(
                primary_provider=self.config.tts_provider,
                voice=self.config.tts_voice,
                cache_enabled=True,
            ))
        except Exception as exc:
            logger.error(f"Pipeline init failed: {exc}")
            if self.on_error:
                self.on_error(f"Pipeline init failed: {exc}")
            return False

        if not self._tts.is_available:
            logger.warning("No TTS available — responses will be text-only")

        self._running = True
        self._pipeline_thread = threading.Thread(target=self._run, daemon=True)
        self._pipeline_thread.start()
        logger.info("Voice pipeline started")
        return True

    def stop(self) -> None:
        """Stop the pipeline and release resources."""
        self._running = False
        if self._audio:
            try:
                self._audio.stop()
            except Exception:
                pass
            self._audio = None
        if self._hotword:
            try:
                self._hotword.cleanup()
            except Exception:
                pass
            self._hotword = None
        self.state = PipelineState.IDLE
        logger.info("Voice pipeline stopped")

    def wait_until_stopped(self, timeout: Optional[float] = None) -> None:
        if self._pipeline_thread:
            self._pipeline_thread.join(timeout=timeout)

    # ── Main loop ───────────────────────────────────────────────────────

    def _run(self) -> None:
        self._audio = AudioStream(sample_rate=SAMPLE_RATE, frame_size=FRAME_SIZE)
        self._audio.start(self._on_audio_frame)
        self.state = PipelineState.IDLE
        while self._running:
            time.sleep(0.1)

    def _on_audio_frame(self, frame: np.ndarray) -> None:
        if not self._running:
            return
        state = self.state
        if state == PipelineState.IDLE:
            self._check_hotword(frame)
        elif state == PipelineState.LISTENING:
            self._record_utterance(frame)
        elif state == PipelineState.SPEAKING:
            self._check_interruption(frame)

    # ── IDLE → hotword ──────────────────────────────────────────────────

    def _check_hotword(self, frame: np.ndarray) -> None:
        if not self._hotword:
            return
        frame_bytes = (frame * 32768).astype(np.int16).tobytes()
        if self._hotword.process(frame_bytes):
            logger.info("Hotword detected!")
            self.state = PipelineState.HOTWORD
            self._hotword_detected_time = time.time()
            if self.config.enable_chimes:
                play_chime("listen")
            self._start_recording()

    # ── LISTENING → recording ───────────────────────────────────────────

    def _start_recording(self) -> None:
        with self._recording_lock:
            self._recording = []
        self._last_speech_time = time.time()
        self.state = PipelineState.LISTENING
        threading.Thread(target=self._record_timeout_watcher, daemon=True).start()

    def _record_utterance(self, frame: np.ndarray) -> None:
        with self._recording_lock:
            self._recording.append(frame.copy())
        if self._vad and self._vad.is_speech(frame, SAMPLE_RATE):
            self._last_speech_time = time.time()

    def _record_timeout_watcher(self) -> None:
        start_time = time.time()
        while self.state == PipelineState.LISTENING and self._running:
            elapsed = time.time() - self._last_speech_time
            total = time.time() - start_time
            if elapsed > self.config.silence_timeout_seconds:
                self._stop_recording()
                return
            if total > self.config.max_utterance_seconds:
                self._stop_recording()
                return
            time.sleep(0.1)

    def _stop_recording(self) -> None:
        with self._recording_lock:
            if not self._recording:
                self.state = PipelineState.IDLE
                return
            audio = np.concatenate(self._recording)
            self._recording = []
        self.state = PipelineState.PROCESSING
        if self.config.enable_chimes:
            play_chime("done")
        threading.Thread(target=self._process_utterance, args=(audio,),
                         daemon=True).start()

    # ── PROCESSING → transcription → routing ───────────────────────────

    def _process_utterance(self, audio: np.ndarray) -> None:
        if not self._stt:
            self._finish_with_error("No STT available")
            return
        result = self._stt.transcribe(audio, SAMPLE_RATE)
        if not result.success or not result.text.strip():
            logger.debug(f"STT: no speech detected ({result.error})")
            self.state = PipelineState.IDLE
            return

        text = result.text.strip()
        logger.info(f"Transcribed: \"{text}\" (confidence: {result.confidence:.2f})")
        if self.on_transcription:
            try:
                self.on_transcription(text)
            except Exception:
                pass

        if not self.route_function:
            self.state = PipelineState.IDLE
            return

        self.state = PipelineState.SPEAKING
        try:
            response = self.route_function(text)
            if response and self._tts:
                self._tts.speak(response)
                while self._tts.is_speaking and self._running:
                    time.sleep(0.1)
                with self._interrupted_lock:
                    interrupted = self._interrupted
                    self._interrupted = False
                if not interrupted:
                    self.state = PipelineState.IDLE
            else:
                self.state = PipelineState.IDLE
        except Exception as exc:
            logger.error(f"Routing error: {exc}")
            self._finish_with_error(f"Routing error: {exc}")

    def _finish_with_error(self, error: str) -> None:
        logger.error(error)
        if self.on_error:
            try:
                self.on_error(error)
            except Exception:
                pass
        self.state = PipelineState.IDLE

    # ── SPEAKING → interruption (barge-in) ─────────────────────────────

    def _check_interruption(self, frame: np.ndarray) -> None:
        if not self._vad or not self._tts:
            return
        self._interrupt_cooldown += 1
        if self._interrupt_cooldown < self._INTERRUPT_CHECK_INTERVAL:
            return
        self._interrupt_cooldown = 0

        # Anti-echo: ignore the first ~1.5 s after TTS playback begins so
        # speaker bleed into the mic can't trigger a false interruption.
        # (Timed from when speak() started, not when the hotword fired.)
        if time.time() - self._tts_start_time < 1.5:
            return

        if self._vad.is_speech(frame, SAMPLE_RATE):
            logger.debug("User interrupted — stopping TTS")
            self._tts.stop()
            with self._interrupted_lock:
                self._interrupted = True
            self._start_recording()

    # ── Public: push-to-talk & speak ───────────────────────────────────

    def push_to_talk(self) -> str:
        """Begin push-to-talk recording. Returns '' (transcribe on stop)."""
        if not self._audio:
            return ""
        self._start_recording()
        return ""

    def stop_recording_and_process(self) -> str:
        """Stop the current recording and return transcribed text (blocking)."""
        with self._recording_lock:
            if not self._recording:
                self.state = PipelineState.IDLE
                return ""
            audio = np.concatenate(self._recording)
            self._recording = []
        self.state = PipelineState.PROCESSING
        if not self._stt:
            self.state = PipelineState.IDLE
            return ""
        result = self._stt.transcribe(audio, SAMPLE_RATE)
        self.state = PipelineState.IDLE
        return result.text.strip() if result.success else ""

    def speak(self, text: str, mode: Optional[VoiceMode] = None) -> None:
        """Speak text aloud. Non-blocking; resets state when finished."""
        if not self._tts:
            return
        self.state = PipelineState.SPEAKING
        with self._speech_gen_lock:
            self._speech_gen += 1
            gen = self._speech_gen
        self._tts_start_time = time.time()
        self._tts.speak(text, mode)
        threading.Thread(target=self._wait_for_speech_end, args=(gen,),
                         daemon=True).start()

    def _wait_for_speech_end(self, gen: int) -> None:
        if not self._tts:
            with self._interrupted_lock:
                self._interrupted = False
            return
        while self._tts.is_speaking and self._running:
            time.sleep(0.1)
        with self._speech_gen_lock:
            is_latest = gen == self._speech_gen
        if is_latest and self._running:
            with self._interrupted_lock:
                interrupted = self._interrupted
                self._interrupted = False
            if not interrupted:
                self.state = PipelineState.IDLE
