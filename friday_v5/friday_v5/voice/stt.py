"""Speech-to-Text engine for Friday V4 — Voice Wave 2.0.

Three-tier STT with automatic fallback:
  1. faster-whisper (primary) — CTranslate2 int8, no torch, built-in Silero VAD
  2. whisper.cpp (alternative) — fastest CPU C++ inference (subprocess)
  3. speech_recognition (fallback) — Google / CMU Sphinx, always available

Design:
  - Lazy async loading: model downloads in background, never blocks start()
  - Confidence-gated: low-confidence transcriptions are rejected
  - Config: `FRIDAY_STT_MODEL` env var or `voice.stt_model` config
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("friday_v5.voice.stt")


@dataclass
class STTResult:
    """Result of a speech-to-text transcription."""
    text: str = ""
    confidence: float = 0.0
    language: str = "en"
    duration_ms: int = 0
    success: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# faster-whisper provider
# ---------------------------------------------------------------------------


class FasterWhisperProvider:
    """faster-whisper — fast local STT via CTranslate2 (no torch).

    Model: base.en (English-only, ~400 MB RAM, int8 CPU). Fallback: tiny.en.
    Loading is async with timeout; the pipeline never blocks on downloads.
    """

    name = "faster-whisper"
    is_available = False
    _model = None
    _load_lock = threading.Lock()
    _load_thread: Optional[threading.Thread] = None

    MODEL_SIZE = os.environ.get("FRIDAY_STT_MODEL", "base.en")

    def __init__(self, model: str | None = None):
        if model:
            self.MODEL_SIZE = model
        try:
            import importlib.util
            if importlib.util.find_spec("faster_whisper") is not None:
                self._try_load_async()
            else:
                logger.debug("faster-whisper not installed — skipping load")
        except Exception as exc:
            logger.debug(f"faster-whisper init failed: {exc}")

    def _try_load_async(self) -> None:
        import queue
        result_q: queue.Queue = queue.Queue()

        def _load():
            try:
                from faster_whisper import WhisperModel
                model = WhisperModel(self.MODEL_SIZE, device="cpu",
                                     compute_type="int8")
                result_q.put(("ok", model))
            except Exception as exc:
                result_q.put(("error", exc))

        t = threading.Thread(target=_load, daemon=True)
        self._load_thread = t
        t.start()

        # Brief non-blocking wait for a cached model; else finish async.
        try:
            status, result = result_q.get(timeout=0.5)
            if status == "ok":
                with self._load_lock:
                    self._model = result
                    self.is_available = True
        except Exception:
            pass

        if not self.is_available:
            def _finish():
                t.join(timeout=180)
                if t.is_alive():
                    logger.warning("faster-whisper download timed out (180s)")
                    return
                try:
                    status, result = result_q.get_nowait()
                    if status == "ok":
                        with self._load_lock:
                            self._model = result
                            self.is_available = True
                    else:
                        logger.warning(f"faster-whisper load failed: {result}")
                except Exception:
                    pass
            threading.Thread(target=_finish, daemon=True).start()

    def _try_load_sync(self) -> bool:
        if self.is_available and self._model is not None:
            return True
        try:
            from faster_whisper import WhisperModel
            with self._load_lock:
                self._model = WhisperModel(self.MODEL_SIZE, device="cpu",
                                           compute_type="int8")
                self.is_available = True
            return True
        except Exception as exc:
            logger.debug(f"faster-whisper sync load failed: {exc}")
            return False

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000
                   ) -> STTResult:
        if self._model is None:
            if not self._try_load_sync():
                return STTResult(error="faster-whisper not loaded")
        model = self._model
        if model is None:
            return STTResult(error="faster-whisper not loaded")
        try:
            # faster-whisper returns a LAZY generator of segments — it must be
            # materialized once before iterating, otherwise the second pass
            # (confidence extraction) sees an exhausted iterator, yielding an
            # empty confidences list → avg_confidence 0.0 → every real
            # transcription rejected as "low confidence". (Tests used lists,
            # which masked this.)
            segments_gen, info = model.transcribe(
                audio, beam_size=3, language="en",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500,
                                "threshold": 0.5},
            )
            segments = list(segments_gen)
            result_text = " ".join(seg.text for seg in segments)
            confidences = [getattr(seg, "avg_logprob", 0) for seg in segments]
            avg_confidence = (np.exp(sum(confidences) / len(confidences))
                              if confidences else 0.0)
            avg_confidence = max(0.0, min(1.0, float(avg_confidence)))

            if not result_text.strip():
                return STTResult(error="No speech detected")
            if avg_confidence < 0.3:
                return STTResult(text=result_text.strip(),
                                 confidence=avg_confidence,
                                 error=f"Low confidence: {avg_confidence:.2f}")
            return STTResult(
                text=result_text.strip(),
                confidence=avg_confidence,
                language=info.language or "en",
                duration_ms=int(getattr(info, "duration", 0) * 1000),
                success=True,
            )
        except Exception as exc:
            return STTResult(error=f"Transcription error: {exc}")


# ---------------------------------------------------------------------------
# whisper.cpp provider (subprocess)
# ---------------------------------------------------------------------------


class WhisperCPPProvider:
    """whisper.cpp — fastest CPU STT via C++ inference (subprocess)."""

    name = "whisper.cpp"
    is_available = False

    _BINARY_PATHS: tuple[Path, ...] = (
        Path.home() / ".friday" / "stt_models" / "whisper.cpp" / "main",
        Path("/usr/local/bin/whisper"),
        Path("/usr/bin/whisper"),
    )
    _MODEL_PATHS: tuple[Path, ...] = (
        Path.home() / ".friday" / "stt_models" / "ggml-base.en.bin",
        Path.home() / ".friday" / "stt_models" / "ggml-tiny.en.bin",
    )

    def __init__(self):
        self._binary = self._find_binary()
        self._model = self._find_model()
        self.is_available = self._binary is not None and self._model is not None

    def _find_binary(self) -> Optional[str]:
        for p in self._BINARY_PATHS:
            if p.exists() and p.stat().st_size > 0:
                return str(p)
        # venv-aware discovery: whisper.cpp may be installed in the active
        # venv's bin even when it isn't on PATH.
        import shutil
        return shutil.which("whisper") or str(
            Path(sys.executable).parent / "whisper")

    def _find_model(self) -> Optional[str]:
        for p in self._MODEL_PATHS:
            if p.exists():
                return str(p)
        return None

    def transcribe(self, audio_path: str) -> STTResult:
        if not self.is_available:
            return STTResult(error="whisper.cpp not available")
        import json
        import subprocess
        try:
            result = subprocess.run(
                [self._binary, "--model", self._model, "--file", audio_path,
                 "--language", "en", "--output-json"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return STTResult(error=f"whisper.cpp exited {result.returncode}")
            output = json.loads(result.stdout)
            text = output.get("text", "").strip()
            if not text:
                return STTResult(error="No speech detected")
            return STTResult(text=text, confidence=0.8, success=True)
        except subprocess.TimeoutExpired:
            return STTResult(error="whisper.cpp timed out")
        except Exception as exc:
            return STTResult(error=str(exc))


# ---------------------------------------------------------------------------
# speech_recognition provider (fallback)
# ---------------------------------------------------------------------------


class SpeechRecognitionProvider:
    """Google Speech Recognition / CMU Sphinx fallback."""

    name = "speech_recognition"
    is_available = False

    def __init__(self):
        try:
            import speech_recognition  # noqa: F401
            self.is_available = True
        except ImportError:
            self.is_available = False

    def transcribe(self, audio_path: str) -> STTResult:
        if not self.is_available:
            return STTResult(error="speech_recognition not installed")
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.AudioFile(audio_path) as source:
                audio = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio)
                return STTResult(text=text, confidence=0.7, success=True)
            except (sr.UnknownValueError, sr.RequestError):
                pass
            try:
                text = recognizer.recognize_sphinx(audio)
                return STTResult(text=text, confidence=0.5, success=True)
            except sr.UnknownValueError:
                return STTResult(error="Could not understand audio")
        except Exception as exc:
            return STTResult(error=str(exc))


# ---------------------------------------------------------------------------
# SpeechToText — facade
# ---------------------------------------------------------------------------


class SpeechToText:
    """Friday's ears — transcribe speech to text with automatic fallback."""

    def __init__(self, model: str | None = None):
        self._providers: list[object] = []
        self._active: object | None = None
        self._init_providers(model)
        self._promote_async_loads()

    def _init_providers(self, model: str | None) -> None:
        try:
            faster = FasterWhisperProvider(model=model)
            self._providers.append(faster)
            if faster.is_available:
                self._active = faster
        except Exception as exc:
            logger.debug(f"faster-whisper init failed: {exc}")

        try:
            cpp = WhisperCPPProvider()
            self._providers.append(cpp)
            if cpp.is_available and self._active is None:
                self._active = cpp
        except Exception:
            pass

        try:
            sr = SpeechRecognitionProvider()
            self._providers.append(sr)
            if sr.is_available and self._active is None:
                self._active = sr
        except Exception:
            pass

    def _promote_async_loads(self) -> None:
        """Activate faster-whisper once its background load completes.

        Joining ``_load_thread`` is not enough to promote: the provider's
        own ``_finish`` thread publishes ``is_available`` a moment AFTER
        the load thread dies, so this waiter can wake first, see
        ``is_available is False``, and skip promotion — leaving the facade
        permanently deaf (``active=none`` while the provider reports
        available). Poll the flag briefly post-join to win that race.
        """
        pending = [p for p in self._providers
                   if isinstance(p, FasterWhisperProvider) and not p.is_available]

        def _wait():
            for provider in pending:
                if provider._load_thread is not None:
                    try:
                        provider._load_thread.join(timeout=180)
                    except Exception:
                        pass
                    if not provider.is_available:
                        # Race window: _finish sets is_available right after
                        # _load_thread dies. Bound the wait so a genuinely
                        # failed load still moves on quickly.
                        deadline = time.time() + 3.0
                        while (time.time() < deadline
                               and not provider.is_available):
                            time.sleep(0.02)
                if provider.is_available and self._active is None:
                    self._active = provider
                    break
        if pending:
            threading.Thread(target=_wait, daemon=True).start()

    @property
    def is_available(self) -> bool:
        return self._active is not None

    @property
    def active_provider(self) -> str:
        return getattr(self._active, "name", "none") if self._active else "none"

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000
                   ) -> STTResult:
        """Transcribe audio array to text."""
        if not self.is_available or self._active is None:
            return STTResult(error="No STT provider available")

        # Resample to 16 kHz with numpy interpolation (no scipy)
        if sample_rate != 16000:
            target_len = int(len(audio) * 16000 / sample_rate)
            indices = np.linspace(0, len(audio) - 1, target_len)
            audio = np.interp(indices, np.arange(len(audio)), audio)
            sample_rate = 16000

        if isinstance(self._active, FasterWhisperProvider):
            return self._active.transcribe(audio, sample_rate)

        # File-based providers: write temp WAV
        from .utils import write_wav
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name
            write_wav(tmp_path, audio, sample_rate)
            if isinstance(self._active, WhisperCPPProvider):
                return self._active.transcribe(tmp_path)
            if isinstance(self._active, SpeechRecognitionProvider):
                return self._active.transcribe(tmp_path)
            return STTResult(error="Unknown provider")
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def transcribe_file(self, path: str) -> STTResult:
        from .utils import read_wav
        audio, sr = read_wav(path)
        return self.transcribe(audio, sr)

    def list_providers(self) -> list[dict]:
        return [{"name": getattr(p, "name", "?"),
                 "available": getattr(p, "is_available", False)}
                for p in self._providers]
