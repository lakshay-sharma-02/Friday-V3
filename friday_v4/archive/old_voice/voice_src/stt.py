"""Speech-to-Text engine for Friday V4.

Three-tier STT with automatic fallback:
  1. faster-whisper (primary) — fast local inference, excellent accuracy
  2. whisper.cpp (alternative) — fastest CPU inference (C++ backend)
  3. speech_recognition (fallback) — Google/CMU Sphinx, always available

Design:
  - Non-blocking: transcribe() runs in thread pool
  - VAD-gated: only processes audio with detected speech
  - Confidence gating: rejects low-confidence transcriptions
  - Language: English only (faster, smaller models)
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger("friday_v4.voice.stt")


# ---------------------------------------------------------------------------
# STT Result
# ---------------------------------------------------------------------------


@dataclass
class STTResult:
    """Result of a speech-to-text transcription."""
    text: str
    confidence: float  # 0.0 to 1.0
    language: str = "en"
    duration_ms: int = 0
    success: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# FasterWhisper Provider
# ---------------------------------------------------------------------------


class FasterWhisperProvider:
    """faster-whisper — fast local STT using CTranslate2.
    
    Model: base.en (English-only, ~500MB, runs on CPU in ~500ms)
    Fallback: tiny.en (English-only, ~150MB, runs in ~200ms)
    
    Uses VAD (Silero VAD) to gate transcription — only processes
    segments with detected speech.
    
    IMPORTANT: Model loading is ASYNC with timeout. First-load model
    downloads from HuggingFace may take 10-30s, so we never block
    the pipeline startup on model download. The provider becomes
    available once the download completes in background.
    """

    name = "faster-whisper"
    is_available = False
    _model = None
    _load_lock = threading.Lock()

    # Model sizes: tiny.en, base.en, small.en, medium.en, large-v3
    MODEL_SIZE = os.environ.get("FRIDAY_STT_MODEL", "base")

    def __init__(self):
        # Check if package is installed WITHOUT importing it
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is not None:
            self._try_load_async()
        else:
            logger.debug("faster-whisper not installed — skipping load")
            self.is_available = False

    def _try_load_async(self) -> None:
        """Load faster-whisper model in background thread with timeout.
        
        First load downloads model files from HuggingFace (~10-30s).
        We try synchronously with a short timeout for cached models;
        if it takes longer we finish in background and mark available
        once complete.
        """
        import queue

        result_q: queue.Queue = queue.Queue()

        def _load():
            try:
                from faster_whisper import WhisperModel
                model = WhisperModel(
                    self.MODEL_SIZE,
                    device="cpu",
                    compute_type="int8",
                )
                result_q.put(("ok", model))
            except Exception as exc:
                result_q.put(("error", exc))

        t = threading.Thread(target=_load, daemon=True)
        self._load_thread = t
        t.start()

        # Brief non-blocking wait for cached model, then continue async.
        # The pipeline must start INSTANTLY — STT becomes available once
        # the background load completes (~8s for cached Whisper base).
        try:
            status, result = result_q.get(timeout=0.5)
            if status == "ok":
                with self._load_lock:
                    self._model = result
                    self.is_available = True
                logger.info(
                    f"faster-whisper loaded (model: {self.MODEL_SIZE}, fast)"
                )
        except queue.Empty:
            self.is_available = False
            # Model finishing in background — don't log warning, user
            # won't speak for several seconds anyway
            logger.debug("faster-whisper model loading in background...")

            def _finish_load():
                t.join(timeout=120)
                if t.is_alive():
                    logger.warning("faster-whisper download timed out after 120s")
                    return
                try:
                    status, result = result_q.get_nowait()
                    if status == "ok":
                        with self._load_lock:
                            self._model = result
                            self.is_available = True
                        logger.info(
                            f"faster-whisper loaded (model: {self.MODEL_SIZE}, "
                            f"async complete)"
                        )
                    else:
                        logger.warning(f"faster-whisper load failed: {result}")
                except Exception:
                    pass

            threading.Thread(target=_finish_load, daemon=True).start()

    def _try_load(self) -> bool:
        """Synchronous load (used by transcribe if not yet loaded)."""
        if self.is_available and self._model is not None:
            return True
        try:
            from faster_whisper import WhisperModel
            with self._load_lock:
                self._model = WhisperModel(
                    self.MODEL_SIZE,
                    device="cpu",
                    compute_type="int8",
                )
                self.is_available = True
            logger.info(
                f"faster-whisper loaded (model: {self.MODEL_SIZE}, sync)"
            )
            return True
        except Exception as exc:
            logger.debug(f"faster-whisper sync load failed: {exc}")
            return False

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000
                   ) -> STTResult:
        """Transcribe audio array to text."""
        # If model is still loading async, try to finish sync
        if not self.is_available or self._model is None:
            if not self._try_load():
                return STTResult(text="", confidence=0.0, success=False,
                                 error="faster-whisper not loaded")

        try:
            segments, info = self._model.transcribe(
                audio,
                beam_size=3,
                language="en",
                vad_filter=True,  # Built-in VAD filtering
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    threshold=0.5,
                ),
            )

            result_text = " ".join(seg.text for seg in segments)
            # Average confidence across segments
            confidences = [
                getattr(seg, "avg_logprob", 0) for seg in segments
            ]
            # Convert logprob to confidence score (rough approximation)
            avg_confidence = (
                np.exp(sum(confidences) / len(confidences))
                if confidences else 0.0
            )
            # Clamp to reasonable range
            avg_confidence = max(0.0, min(1.0, avg_confidence))

            if not result_text.strip():
                return STTResult(
                    text="", confidence=0.0, success=False,
                    error="No speech detected",
                )

            # Confidence gate — reject low-confidence transcriptions
            if avg_confidence < 0.3:
                return STTResult(
                    text=result_text, confidence=avg_confidence,
                    success=False, error=f"Low confidence: {avg_confidence:.2f}",
                )

            return STTResult(
                text=result_text.strip(),
                confidence=avg_confidence,
                language=info.language or "en",
                duration_ms=int(info.duration * 1000) if hasattr(info, "duration") else 0,
                success=True,
            )

        except Exception as exc:
            logger.warning(f"faster-whisper transcription failed: {exc}")
            return STTResult(
                text="", confidence=0.0, success=False,
                error=f"Transcription error: {exc}",
            )


# ---------------------------------------------------------------------------
# whisper.cpp Provider (alternative CPU-optimized backend)
# ---------------------------------------------------------------------------


class WhisperCPPProvider:
    """whisper.cpp — fastest CPU STT via C++ inference.
    
    Even faster than faster-whisper on CPU. Requires the whisper.cpp
    binary to be installed separately.
    
    Binary location: ~/.friday/stt_models/whisper.cpp/main
    Models: ~/.friday/stt_models/ggml-base.en.bin
    """

    name = "whisper.cpp"
    is_available = False

    # Check for whisper.cpp binary
    _BINARY_PATHS = [
        Path.home() / ".friday" / "stt_models" / "whisper.cpp" / "main",
        Path("/usr/local/bin/whisper"),
        Path("/usr/bin/whisper"),
    ]

    _MODEL_PATHS = [
        Path.home() / ".friday" / "stt_models" / "ggml-base.en.bin",
        Path.home() / ".friday" / "stt_models" / "ggml-tiny.en.bin",
    ]

    def __init__(self):
        self._binary = self._find_binary()
        self._model = self._find_model()
        self.is_available = self._binary is not None and self._model is not None
        if self.is_available:
            logger.info(f"whisper.cpp available: {self._binary}")

    def _find_binary(self) -> Optional[str]:
        for p in self._BINARY_PATHS:
            if p.exists() and p.stat().st_size > 0:
                return str(p)
        # Also check PATH
        import shutil
        return shutil.which("whisper")

    def _find_model(self) -> Optional[str]:
        for p in self._MODEL_PATHS:
            if p.exists():
                return str(p)
        return None

    def transcribe(self, audio_path: str) -> STTResult:
        """Transcribe a WAV file using whisper.cpp subprocess."""
        if not self.is_available:
            return STTResult(text="", confidence=0.0, success=False,
                             error="whisper.cpp not available")

        try:
            import subprocess
            import json

            result = subprocess.run(
                [
                    self._binary,
                    "--model", self._model,
                    "--file", audio_path,
                    "--language", "en",
                    "--output-json",
                ],
                capture_output=True, text=True, timeout=30,
            )

            if result.returncode != 0:
                return STTResult(
                    text="", confidence=0.0, success=False,
                    error=f"whisper.cpp exited {result.returncode}",
                )

            # Parse JSON output
            output = json.loads(result.stdout)
            text = output.get("text", "").strip()
            if not text:
                return STTResult(
                    text="", confidence=0.0, success=False,
                    error="No speech detected",
                )

            return STTResult(
                text=text,
                confidence=0.8,  # whisper.cpp doesn't output confidence
                success=True,
            )

        except subprocess.TimeoutExpired:
            return STTResult(
                text="", confidence=0.0, success=False,
                error="whisper.cpp timed out",
            )
        except Exception as exc:
            logger.warning(f"whisper.cpp failed: {exc}")
            return STTResult(
                text="", confidence=0.0, success=False,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# SpeechRecognition Provider (fallback)
# ---------------------------------------------------------------------------


class SpeechRecognitionProvider:
    """Google Speech Recognition / CMU Sphinx fallback.
    
    Last-resort when no local Whisper model is available.
    Google SR requires internet; Sphinx is offline but less accurate.
    """

    name = "speech_recognition"
    is_available = False

    def __init__(self):
        self._try_check()

    def _try_check(self) -> bool:
        try:
            import speech_recognition as sr
            self.is_available = True
            logger.info("speech_recognition available")
            return True
        except ImportError:
            self.is_available = False
            return False

    def transcribe(self, audio_path: str) -> STTResult:
        """Transcribe a WAV file using available recognizer."""
        if not self.is_available:
            return STTResult(
                text="", confidence=0.0, success=False,
                error="speech_recognition not installed",
            )

        try:
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            with sr.AudioFile(audio_path) as source:
                audio = recognizer.record(source)

            # Try Google first (requires internet, higher accuracy)
            try:
                text = recognizer.recognize_google(audio)
                return STTResult(
                    text=text, confidence=0.7, success=True,
                )
            except (sr.UnknownValueError, sr.RequestError):
                pass

            # Fallback to Sphinx (offline)
            try:
                text = recognizer.recognize_sphinx(audio)
                return STTResult(
                    text=text, confidence=0.5, success=True,
                )
            except sr.UnknownValueError:
                return STTResult(
                    text="", confidence=0.0, success=False,
                    error="Could not understand audio",
                )

        except Exception as exc:
            logger.warning(f"speech_recognition failed: {exc}")
            return STTResult(
                text="", confidence=0.0, success=False,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# SpeechToText — main STT frontend
# ---------------------------------------------------------------------------


class SpeechToText:
    """Friday's ears — transcribe speech to text.
    
    Auto-selects the best available provider. Falls back gracefully.
    
    Usage:
        stt = SpeechToText()
        result = stt.transcribe(audio_array)
        if result.success:
            print(f"User said: {result.text}")
    """

    def __init__(self):
        self._providers = []
        self._active: Optional[FasterWhisperProvider] = None
        self._init_providers()
        # faster-whisper loads in the background (torch/ctranslate2 import
        # takes 15-60s). Promote it to active when it finishes — otherwise
        # it would never be used even though it becomes available.
        self._promote_async_loads()

    def _promote_async_loads(self):
        """Watch background-loading providers and activate them on completion."""
        pending = [p for p in self._providers
                   if isinstance(p, FasterWhisperProvider) and not p.is_available]

        def _wait():
            for provider in pending:
                try:
                    provider._load_thread.join(timeout=180)
                except Exception:
                    pass
                if provider.is_available and self._active is None:
                    self._active = provider
                    logger.info(f"STT active provider (async): {provider.name}")
                    break

        if pending:
            threading.Thread(target=_wait, daemon=True).start()

    def _init_providers(self):
        """Initialize STT providers in priority order."""
        try:
            provider = FasterWhisperProvider()
            self._providers.append(provider)
            if provider.is_available:
                self._active = provider
        except Exception as exc:
            logger.debug(f"faster-whisper init failed: {exc}")

        try:
            provider = WhisperCPPProvider()
            self._providers.append(provider)
            if provider.is_available and self._active is None:
                self._active = provider
        except Exception as exc:
            logger.debug(f"whisper.cpp init failed: {exc}")

        try:
            provider = SpeechRecognitionProvider()
            self._providers.append(provider)
            if provider.is_available and self._active is None:
                self._active = provider
        except Exception as exc:
            logger.debug(f"speech_recognition init failed: {exc}")

        if self._active:
            logger.info(f"STT active provider: {self._active.name}")
        else:
            logger.debug("No STT provider available yet (loading in background)")

    @property
    def is_available(self) -> bool:
        return self._active is not None

    @property
    def active_provider(self) -> str:
        return self._active.name if self._active else "none"

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000
                   ) -> STTResult:
        """Transcribe audio array to text.
        
        Args:
            audio: numpy array of audio samples (float32, range [-1, 1])
            sample_rate: Sample rate in Hz (must be 16000)
        
        Returns:
            STTResult with transcribed text and confidence
        """
        if not self.is_available or self._active is None:
            return STTResult(
                text="", confidence=0.0, success=False,
                error="No STT provider available",
            )

        # Ensure correct sample rate — use numpy interpolation (no scipy dependency)
        if sample_rate != 16000:
            target_len = int(len(audio) * 16000 / sample_rate)
            indices = np.linspace(0, len(audio) - 1, target_len)
            audio = np.interp(indices, np.arange(len(audio)), audio)
            sample_rate = 16000

        if isinstance(self._active, FasterWhisperProvider):
            return self._active.transcribe(audio, sample_rate)
        else:
            # Save to temp WAV and use file-based provider
            # Uses pure-Python WAV writer (no soundfile dependency)
            tmp_path = None
            try:
                from .utils import write_wav
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp_path = f.name
                write_wav(tmp_path, audio, sample_rate)

                if isinstance(self._active, WhisperCPPProvider):
                    return self._active.transcribe(tmp_path)
                elif isinstance(self._active, SpeechRecognitionProvider):
                    return self._active.transcribe(tmp_path)
                return STTResult(text="", confidence=0.0, success=False,
                                 error="Unknown provider")
            finally:
                if tmp_path:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except Exception:
                        pass

    def transcribe_file(self, path: str) -> STTResult:
        """Transcribe a WAV file directly."""
        from .utils import read_wav
        audio, sr = read_wav(path)
        return self.transcribe(audio, sr)

    def list_providers(self) -> list[dict]:
        """List all STT providers and their status."""
        return [
            {
                "name": p.name,
                "available": p.is_available,
            }
            for p in self._providers
        ]
