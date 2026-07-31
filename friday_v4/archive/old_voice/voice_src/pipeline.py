"""Voice Pipeline — Orchestrates the full voice interaction flow.

Wires together:
  Microphone → VAD → Hotword Detection → STT → VoiceRouter → TTS

Manages state transitions, threading, interruption handling, and
audio cue playback.

States:
  IDLE       → Listening for hotword (low CPU)
  HOTWORD    → Heard "Hey Friday", waiting for utterance
  LISTENING  → Recording user speech
  PROCESSING → Transcribing + routing through persona
  SPEAKING   → TTS playback (can be interrupted)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import numpy as np

from .audio import AudioStream, FRAME_SIZE, SAMPLE_RATE
from .hotword import HotwordDetector
from .stt import SpeechToText, STTResult
from .tts import TextToSpeech, TTSConfig, VoiceMode, play_chime
from .vad import VoiceActivityDetector

logger = logging.getLogger("friday_v4.voice.pipeline")


# ---------------------------------------------------------------------------
# Pipeline State
# ---------------------------------------------------------------------------


class PipelineState(Enum):
    IDLE = "idle"               # Listening for hotword (low CPU)
    HOTWORD = "hotword"         # Heard "Hey Friday", preparing
    LISTENING = "listening"     # Recording user speech
    PROCESSING = "processing"   # Transcribing + routing
    SPEAKING = "speaking"       # TTS playback


@dataclass
class PipelineConfig:
    """Configuration for the voice pipeline."""
    hotword: str = "hey friday"
    hotword_sensitivity: float = 0.7
    vad_mode: int = 1
    stt_model: str = "base.en"
    tts_provider: str = "kokoro"
    tts_voice: str = ""
    silence_timeout_seconds: float = 2.0  # Stop recording after this much silence
    max_utterance_seconds: float = 30.0   # Max recording duration
    enable_chimes: bool = True


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

OnTranscription = Callable[[str], None]      # Called when speech is transcribed
OnStateChange = Callable[[PipelineState], None]  # Called on state transitions
OnError = Callable[[str], None]               # Called on errors


# ---------------------------------------------------------------------------
# VoicePipeline
# ---------------------------------------------------------------------------


class VoicePipeline:
    """End-to-end voice interaction pipeline.
    
    Runs in background threads. Listens for hotword, captures speech,
    transcribes, routes through V3 persona, and speaks responses.
    
    Usage:
        pipeline = VoicePipeline()
        pipeline.start()
        # ... pipeline runs in background ...
        pipeline.stop()
    """

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._state = PipelineState.IDLE
        self._state_lock = threading.Lock()

        # Sub-components
        self._audio: Optional[AudioStream] = None
        self._hotword: Optional[HotwordDetector] = None
        self._vad: Optional[VoiceActivityDetector] = None
        self._stt: Optional[SpeechToText] = None
        self._tts: Optional[TextToSpeech] = None

        # Recording state
        self._recording: list[np.ndarray] = []
        self._recording_lock = threading.Lock()
        self._last_speech_time: float = 0.0
        self._hotword_detected_time: float = 0.0

        # Callbacks
        self.on_transcription: Optional[OnTranscription] = None
        self.on_state_change: Optional[OnStateChange] = None
        self.on_error: Optional[OnError] = None

        # The routing function — set by VoiceRouter or externally
        self.route_function: Optional[Callable[[str], str]] = None

        # Background threads
        self._pipeline_thread: Optional[threading.Thread] = None
        self._running = False

        # Interruption check cooldown (instance-level, not shared across instances)
        self._interrupt_check_cooldown: int = 0
        self._INTERRUPT_CHECK_INTERVAL: int = 10  # Every 10 frames = 300ms at 30ms/frame

        # Speech generation counter — prevents stale _wait_for_speech_end
        # from resetting state after a newer speak() call
        self._speech_gen: int = 0
        self._speech_gen_lock: threading.Lock = threading.Lock()

        # Interruption guard — prevents _wait_for_speech_end from resetting
        # state to IDLE when an interruption already moved us to LISTENING.
        # Set when interruption fires, cleared by _wait_for_speech_end if stale.
        self._interrupted: bool = False
        self._interrupted_lock: threading.Lock = threading.Lock()

    # ── Properties ─────────────────────────────────────────────────

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
        if self._tts:
            return self._tts.active_provider_name
        return "none"

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the voice pipeline in background threads."""
        if self._running:
            logger.warning("Pipeline already running")
            return False

        logger.info("Starting voice pipeline...")

        # Initialize components
        try:
            self._hotword = HotwordDetector(
                self.config.hotword,
                self.config.hotword_sensitivity,
            )
            self._vad = VoiceActivityDetector(mode=self.config.vad_mode)
            self._stt = SpeechToText()
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

        # Check availability
        if not self._tts.is_available:
            logger.warning("No TTS available — responses will be text-only")

        self._running = True
        self._pipeline_thread = threading.Thread(target=self._run, daemon=True)
        self._pipeline_thread.start()

        logger.info("Voice pipeline started")
        return True

    def stop(self):
        """Stop the voice pipeline and release resources."""
        self._running = False

        if self._audio:
            try:
                self._audio.stop()
            except (RuntimeError, Exception):
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

    def wait_until_stopped(self, timeout: Optional[float] = None):
        """Block until the pipeline stops."""
        if self._pipeline_thread:
            self._pipeline_thread.join(timeout=timeout)

    # ── Internal: Main Loop ────────────────────────────────────────

    def _run(self):
        """Main pipeline loop — runs in background thread."""
        # Start audio stream
        self._audio = AudioStream(sample_rate=SAMPLE_RATE, frame_size=FRAME_SIZE)
        self._audio.start(self._on_audio_frame)

        self.state = PipelineState.IDLE

        # Keep thread alive
        while self._running:
            time.sleep(0.1)

    def _on_audio_frame(self, frame: np.ndarray):
        """Called for each audio frame from the microphone."""
        if not self._running:
            return

        current_state = self.state

        if current_state == PipelineState.IDLE:
            # Listen for hotword
            self._check_hotword(frame)

        elif current_state == PipelineState.LISTENING:
            # Record speech until silence
            self._record_utterance(frame)

        elif current_state == PipelineState.PROCESSING:
            # Still processing — ignore incoming audio
            pass

        elif current_state == PipelineState.SPEAKING:
            # Check for interruption (user speaking while Friday talks)
            self._check_interruption(frame)

    # ── State: IDLE → Hotword Detection ────────────────────────────

    def _check_hotword(self, frame: np.ndarray):
        """Check if the hotword was spoken."""
        if not self._hotword:
            return

        # Convert float32 numpy array to PCM16 bytes
        frame_bytes = (frame * 32768).astype(np.int16).tobytes()

        if self._hotword.process(frame_bytes):
            logger.info("Hotword detected!")
            self.state = PipelineState.HOTWORD
            self._hotword_detected_time = time.time()

            # Play listening chime
            if self.config.enable_chimes:
                play_chime("listen")

            # Start recording
            self._start_recording()

    # ── State: LISTENING → Recording ───────────────────────────────

    def _start_recording(self):
        """Begin recording utterance."""
        with self._recording_lock:
            self._recording = []
        self._last_speech_time = time.time()
        self.state = PipelineState.LISTENING

        threading.Thread(target=self._record_timeout_watcher, daemon=True).start()

    def _record_utterance(self, frame: np.ndarray):
        """Record audio frame during utterance capture."""
        with self._recording_lock:
            self._recording.append(frame.copy())

        # Check for speech in this frame
        if self._vad and self._vad.is_speech(frame, SAMPLE_RATE):
            self._last_speech_time = time.time()

    def _record_timeout_watcher(self):
        """Watch for silence or max duration to stop recording."""
        start_time = time.time()

        while self.state == PipelineState.LISTENING and self._running:
            elapsed = time.time() - self._last_speech_time
            total_duration = time.time() - start_time

            # Stop on silence timeout
            if elapsed > self.config.silence_timeout_seconds:
                logger.debug(f"Silence timeout ({elapsed:.1f}s)")
                self._stop_recording()
                return

            # Stop on max duration
            if total_duration > self.config.max_utterance_seconds:
                logger.debug(f"Max utterance duration ({total_duration:.1f}s)")
                self._stop_recording()
                return

            time.sleep(0.1)

    def _stop_recording(self):
        """Finish recording and start transcription."""
        with self._recording_lock:
            if not self._recording:
                self.state = PipelineState.IDLE
                return
            audio = np.concatenate(self._recording)
            self._recording = []

        self.state = PipelineState.PROCESSING

        # Play processing chime
        if self.config.enable_chimes:
            play_chime("done")

        # Transcribe in background
        threading.Thread(
            target=self._process_utterance,
            args=(audio,),
            daemon=True,
        ).start()

    # ── State: PROCESSING → Transcription → Routing ────────────────

    def _process_utterance(self, audio: np.ndarray):
        """Transcribe audio and route through persona."""
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

        # Call transcription callback
        if self.on_transcription:
            try:
                self.on_transcription(text)
            except Exception:
                pass

        # Route through the persona/LLM
        if self.route_function:
            self.state = PipelineState.SPEAKING
            try:
                response = self.route_function(text)
                if response and self._tts:
                    # Speak the response
                    self._tts.speak(response)
                    # Wait for speech to finish, then check interruption
                    while self._tts.is_speaking and self._running:
                        time.sleep(0.1)
                    # Don't reset to IDLE if interruption already started
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
        else:
            # No router — just return to idle
            self.state = PipelineState.IDLE

    def _finish_with_error(self, error: str):
        """Handle an error gracefully."""
        logger.error(error)
        if self.on_error:
            try:
                self.on_error(error)
            except Exception:
                pass
        self.state = PipelineState.IDLE

    # ── State: SPEAKING → Interruption ─────────────────────────────

    def _check_interruption(self, frame: np.ndarray):
        """Check if user is speaking while Friday talks.

        Only runs VAD on every Nth frame to save CPU.

        Anti-echo: The first ~1.5s after TTS starts is a "refractory"
        period where audio frames are ignored — this prevents the
        speaker output from looping back into the mic and triggering
        a false interruption.
        """
        if not self._vad or not self._tts:
            return

        # Frame cooldown: only check every N frames
        self._interrupt_check_cooldown += 1
        if self._interrupt_check_cooldown < self._INTERRUPT_CHECK_INTERVAL:
            return
        self._interrupt_check_cooldown = 0

        # Anti-echo: skip interruption check for first ~1.5s after TTS starts
        # (speaker bleed into mic would trigger false interruption)
        if time.time() - self._hotword_detected_time < 1.5:
            return

        if self._vad.is_speech(frame, SAMPLE_RATE):
            # User is speaking — interrupt TTS
            logger.debug("User interrupted — stopping TTS")
            self._tts.stop()
            with self._interrupted_lock:
                self._interrupted = True
            self._start_recording()

    # ── Public: Push-to-Talk ───────────────────────────────────────

    def push_to_talk(self) -> str:
        """Record until the user releases the key. Returns transcribed text.
        
        Usage (from CLI with hotkey):
            text = pipeline.push_to_talk()
            response = route_function(text)
            pipeline.tts.speak(response)
        """
        if not self._audio:
            return ""

        self.state = PipelineState.LISTENING
        self._start_recording()

        # We need an external signal to stop recording (key release)
        # The caller should call stop_recording() when the key is released
        return ""

    def stop_recording_and_process(self) -> str:
        """Stop the current recording and process it.
        
        Returns the transcribed text (blocking).
        """
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

        if result.success:
            return result.text.strip()
        return ""

    # ── Speaking ───────────────────────────────────────────────────

    def speak(self, text: str, mode: Optional[VoiceMode] = None):
        """Speak text aloud. Non-blocking."""
        if self._tts:
            self.state = PipelineState.SPEAKING
            with self._speech_gen_lock:
                self._speech_gen += 1
                gen = self._speech_gen
            self._tts.speak(text, mode)
            # Wait briefly then return to idle
            threading.Thread(
                target=self._wait_for_speech_end,
                args=(gen,),
                daemon=True,
            ).start()

    def _wait_for_speech_end(self, gen: int):
        """Wait for TTS to finish, then return to idle.

        Args:
            gen: Speech generation number — if a newer speak() was called,
                 this thread skips the state reset to avoid races.

        Race-safe interruption handling:
          If an interruption fired (user spoke while TTS played), the
          interruption handler set state→LISTENING and started recording.
          We must NOT reset to IDLE here — the new recording is active.
        """
        if not self._tts:
            # Still consume interruption flag
            with self._interrupted_lock:
                self._interrupted = False
            return
        while self._tts.is_speaking and self._running:
            time.sleep(0.1)
        # Only reset to IDLE if we're still the latest speech generation
        # AND no interruption is in progress
        with self._speech_gen_lock:
            if gen == self._speech_gen and self._running:
                with self._interrupted_lock:
                    interrupted = self._interrupted
                    self._interrupted = False  # Consume the flag
                if not interrupted:
                    self.state = PipelineState.IDLE
