"""Voice Activity Detection for Friday V4 — Voice Wave 2.0.

Two-tier VAD:
  1. Silero VAD (primary, ONNX via onnxruntime) — best accuracy
  2. WebRTC VAD (fallback, C-based, negligible CPU)
  3. Energy detection (last resort, no deps)

Modes:
  0: Minimal gating (energy VAD only — near always-listening)
  1: Normal (WebRTC VAD, <1% CPU)
  2: Aggressive (higher threshold)
  3: Silero VAD (ML-based, best accuracy, ~5% CPU)
"""

from __future__ import annotations

import logging
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("friday_v4.voice.vad")

# Silero model — small (~2 MB) ONNX, downloaded once to ~/.friday/models/
_SILERO_URL = "https://github.com/snakers4/silero-vad/releases/download/v5.1/silero_vad.onnx"
_SILERO_PATH = Path.home() / ".friday" / "models" / "silero_vad.onnx"


# ---------------------------------------------------------------------------
# Silero VAD (ONNX)
# ---------------------------------------------------------------------------


class SileroVAD:
    """ML-based voice activity detection via ONNX runtime."""

    is_available = False
    _model = None

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._try_load()

    def _download_with_retry(self, url: str, path: Path,
                             retries: int = 3, backoff: float = 2.0) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(retries):
            try:
                urllib.request.urlretrieve(url, path)
                return True
            except (urllib.error.URLError, urllib.error.HTTPError,
                    OSError, TimeoutError) as exc:
                logger.warning(
                    f"Download failed (attempt {attempt+1}/{retries}): {exc}")
                if attempt < retries - 1:
                    import time
                    time.sleep(backoff * (2 ** attempt))
        return False

    def _try_load(self) -> bool:
        try:
            import onnxruntime
        except ImportError:
            logger.debug("onnxruntime not available for Silero VAD")
            return False

        try:
            if not _SILERO_PATH.exists():
                logger.info("Downloading Silero VAD model...")
                if not self._download_with_retry(_SILERO_URL, _SILERO_PATH):
                    logger.warning("Silero VAD download failed")
                    return False
            self._model = onnxruntime.InferenceSession(
                str(_SILERO_PATH), providers=["CPUExecutionProvider"])
            self.is_available = True
            logger.info("Silero VAD loaded")
            return True
        except Exception as exc:
            logger.debug(f"Silero VAD load failed: {exc}")
            return False

    def is_speech(self, audio: np.ndarray, sample_rate: int = 16000) -> bool:
        if not self.is_available or self._model is None:
            return False
        try:
            if len(audio.shape) == 1:
                audio = audio.reshape(1, -1)
            inputs = {self._model.get_inputs()[0].name: audio.astype(np.float32)}
            outputs = self._model.run(None, inputs)
            prob = float(outputs[0][0][0])
            return prob > self.threshold
        except Exception as exc:
            logger.debug(f"Silero VAD inference failed: {exc}")
            return False


# ---------------------------------------------------------------------------
# WebRTC VAD
# ---------------------------------------------------------------------------


class WebRTCVAD:
    """Lightweight C-based VAD. Very low CPU, decent accuracy."""

    is_available = False
    _vad = None

    MODES = {0: 0, 1: 1, 2: 2, 3: 3}

    def __init__(self, mode: int = 1):
        self.mode = mode
        self._try_load()

    def _try_load(self) -> bool:
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(self.MODES.get(self.mode, 1))
            self.is_available = True
            return True
        except ImportError:
            logger.debug("webrtcvad not available")
            return False
        except Exception as exc:
            logger.debug(f"WebRTC VAD load failed: {exc}")
            return False

    def is_speech(self, audio_bytes: bytes, sample_rate: int = 16000) -> bool:
        if not self.is_available or self._vad is None:
            return False
        try:
            return self._vad.is_speech(audio_bytes, sample_rate)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Energy VAD (no dependencies)
# ---------------------------------------------------------------------------


class EnergyVAD:
    """RMS-threshold speech detection. No dependencies."""

    is_available = True

    def __init__(self, threshold: float = 0.01):
        self.threshold = threshold

    def is_speech(self, audio: np.ndarray, sample_rate: int = 16000) -> bool:
        if len(audio) == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32)))))
        return rms > self.threshold


# ---------------------------------------------------------------------------
# VoiceActivityDetector — facade
# ---------------------------------------------------------------------------


class VoiceActivityDetector:
    """Auto-selects the best available VAD engine."""

    def __init__(self, mode: int = 1):
        self.mode = mode
        self._silero: Optional[SileroVAD] = None
        self._webrtc: Optional[WebRTCVAD] = None
        self._energy = EnergyVAD()
        self._active: object = self._energy
        self._init()

    def _init(self) -> None:
        # Silero for mode 3, WebRTC for 1/2, energy otherwise
        if self.mode >= 3:
            self._silero = SileroVAD()
            if self._silero.is_available:
                self._active = self._silero
                return
        if self.mode >= 1:
            self._webrtc = WebRTCVAD(mode=min(self.mode, 2))
            if self._webrtc.is_available:
                self._active = self._webrtc
                return
        self._active = self._energy

    @property
    def is_available(self) -> bool:
        return True  # energy fallback always works

    @property
    def provider_name(self) -> str:
        if isinstance(self._active, SileroVAD):
            return "silero"
        if isinstance(self._active, WebRTCVAD):
            return "webrtc"
        return "energy"

    def is_speech(self, audio: np.ndarray | bytes,
                  sample_rate: int = 16000) -> bool:
        """Detect speech in audio (float32 numpy or PCM16 bytes)."""
        if isinstance(audio, bytes):
            if isinstance(self._active, WebRTCVAD):
                return self._active.is_speech(audio, sample_rate)
            arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            return self._is_speech_np(arr, sample_rate)
        return self._is_speech_np(audio, sample_rate)

    def _is_speech_np(self, audio: np.ndarray, sample_rate: int) -> bool:
        if isinstance(self._active, SileroVAD):
            return self._active.is_speech(audio, sample_rate)
        if isinstance(self._active, WebRTCVAD):
            audio_bytes = (audio * 32768).astype(np.int16).tobytes()
            return self._active.is_speech(audio_bytes, sample_rate)
        return self._energy.is_speech(audio, sample_rate)
