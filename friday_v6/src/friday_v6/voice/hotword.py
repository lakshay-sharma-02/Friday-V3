"""Hotword / Wake-Word Detection for Friday V4 — Voice Wave 2.0.

Two-tier hotword detection:
  1. OpenWakeWord (primary) — free, local, Apache-2.0, no API key
  2. Energy detection (fallback) — no model, very basic

OpenWakeWord ships a pre-trained `hey_jarvis` model, which is the closest
available free phrase to "hey friday". Sensitivity 0.0–1.0 maps to an
internal detection threshold.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, Union

import numpy as np

logger = logging.getLogger("friday_v6.voice.hotword")

# Map our keywords to OpenWakeWord's pre-trained model names


class _DetectorProtocol(Protocol):
    """Minimal interface shared by the OpenWakeWord and energy providers."""

    def process(self, frame: Union[np.ndarray, bytes, list]) -> bool: ...

    def set_sensitivity(self, sensitivity: float) -> None: ...

    def cleanup(self) -> None: ...
_OPENWAKEWORD_KEYWORDS = {
    "hey friday": "hey_jarvis",
    "hey jarvis": "hey_jarvis",
    "jarvis": "hey_jarvis",
    "alexa": "alexa",
    "computer": "computer",
}


# ---------------------------------------------------------------------------
# OpenWakeWord provider
# ---------------------------------------------------------------------------


class OpenWakeWordProvider:
    """OpenWakeWord — free, local, no-API-key wake word detection.

    - Apache-2.0, runs via ONNX Runtime
    - Pre-trained models: hey_jarvis, alexa, hey_mycroft
    - ~5 ms/frame on CPU
    """

    name = "openwakeword"
    is_available = False
    _oww = None
    _model_name: str = "hey_jarvis"
    _threshold: float = 0.5

    def __init__(self, keyword: str = "hey friday", sensitivity: float = 0.7):
        self.keyword = keyword.lower().strip() if keyword else ""
        if not self.keyword:
            logger.info("OpenWakeWord disabled (empty keyword)")
            return
        self._model_name = _OPENWAKEWORD_KEYWORDS.get(self.keyword, "hey_jarvis")
        self.set_sensitivity(sensitivity)
        self._try_load()

    def _try_load(self) -> bool:
        try:
            from openwakeword import Model as OWWModel
            self._oww = OWWModel()
            self.is_available = True
            return True
        except ImportError as exc:
            logger.warning(f"OpenWakeWord not installed ({exc}). "
                           "Install: pip install openwakeword")
            return False
        except Exception as exc:
            logger.warning(f"OpenWakeWord load failed: {exc}")
            return False

    def process(self, frame: np.ndarray | bytes | list[int]) -> bool:
        """Process an audio frame. True if hotword detected."""
        if not self.is_available or self._oww is None:
            return False
        try:
            if isinstance(frame, bytes):
                audio = np.frombuffer(frame, dtype=np.int16).astype(
                    np.float32) / 32768.0
            else:
                audio = np.asarray(frame, dtype=np.float32)
                if audio.max() > 1.0 or audio.min() < -1.0:
                    audio = audio / 32768.0
            prediction = self._oww.predict(audio)
            score = float(prediction.get(self._model_name, 0.0))
            return score > self._threshold
        except Exception:
            return False

    def set_sensitivity(self, sensitivity: float) -> None:
        """Sensitivity 0.0-1.0 → threshold 0.8-0.2."""
        sensitivity = max(0.0, min(1.0, sensitivity))
        self._threshold = max(0.2, min(0.8, 0.8 - (sensitivity * 0.6)))

    def cleanup(self) -> None:
        self._oww = None


# ---------------------------------------------------------------------------
# Energy detector (last resort)
# ---------------------------------------------------------------------------


class EnergyDetector:
    """Loud-sound detection. Not a real hotword detector — emergency only."""

    name = "energy"
    is_available = True

    def __init__(self, threshold: float = 0.02, min_frames: int = 5):
        self._threshold = threshold
        self._min_frames = min_frames
        self._speech_frames = 0

    def process(self, frame: np.ndarray | bytes | list[int]) -> bool:
        import math
        if isinstance(frame, bytes):
            # PCM16 bytes → float samples in [-1, 1]
            import struct
            raw = struct.unpack(f"<{len(frame)//2}h", frame)
            samples = np.asarray(raw, dtype=np.float32) / 32768.0
        else:
            samples = np.asarray(frame, dtype=np.float32)
            if samples.max() > 1.0 or samples.min() < -1.0:
                samples = samples / 32768.0
        if len(samples) == 0:
            return False
        rms = math.sqrt(float(np.mean(np.square(samples))))
        if rms > self._threshold:
            self._speech_frames += 1
            if self._speech_frames >= self._min_frames:
                self._speech_frames = 0
                return True
        else:
            self._speech_frames = 0
        return False

    def set_sensitivity(self, sensitivity: float) -> None:
        sensitivity = max(0.0, min(1.0, sensitivity))
        self._threshold = max(0.005, min(0.05, 0.05 - (sensitivity * 0.045)))

    def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------------------
# HotwordDetector — facade
# ---------------------------------------------------------------------------


class HotwordDetector:
    """Wake word detection with automatic provider selection."""

    def __init__(self, keyword: str = "hey friday", sensitivity: float = 0.7):
        self.keyword = keyword
        self.sensitivity = sensitivity
        self._detector: Optional[_DetectorProtocol] = None
        self._init()

    def _init(self) -> None:
        if not self.keyword or not self.keyword.strip():
            # Push-to-talk mode passes an empty keyword — hotword must be
            # fully disabled, not silently armed with a default model.
            logger.info("Hotword disabled (empty keyword)")
            self._detector = None
            return
        try:
            oww = OpenWakeWordProvider(self.keyword, self.sensitivity)
            if oww.is_available:
                self._detector = oww
                return
        except Exception:
            pass
        logger.info("Hotword: energy detection fallback (basic)")
        self._detector = EnergyDetector()

    @property
    def is_available(self) -> bool:
        return self._detector is not None

    @property
    def provider_name(self) -> str:
        if isinstance(self._detector, OpenWakeWordProvider):
            return "openwakeword"
        if isinstance(self._detector, EnergyDetector):
            return "energy"
        return "none"

    def process(self, frame) -> bool:
        if not self._detector:
            return False
        return self._detector.process(frame)

    def set_sensitivity(self, sensitivity: float) -> None:
        self.sensitivity = sensitivity
        if self._detector is not None:
            self._detector.set_sensitivity(sensitivity)

    def cleanup(self) -> None:
        if self._detector is not None:
            self._detector.cleanup()
