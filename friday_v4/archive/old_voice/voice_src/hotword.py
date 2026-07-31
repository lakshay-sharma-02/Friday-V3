"""Hotword/Wake Word Detection for Friday V4.

Detects "Hey Friday" or custom wake words in a continuous audio stream.

Three-tier hotword detection:
  1. OpenWakeWord (primary) — free, local, Apache 2.0, no API key needed
  2. Porcupine (secondary) — local, fast, needs free API key
  3. Simple energy detection (emergency) — no model needed, basic

Design:
  - Runs continuously on audio stream (non-blocking, ~1ms per frame)
  - When hotword detected, signals the pipeline to start recording
  - Sensitivity configurable (0.0 = least sensitive, 1.0 = most)
  - Multiple keywords supported ("hey friday", "hey jarvis", custom)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.voice.hotword")

# OpenWakeWord keyword mapping
# Maps our keywords to OpenWakeWord's pre-trained model names
_OPENWAKEWORD_KEYWORDS = {
    "hey friday": "hey_jarvis",     # "Hey Jarvis" is built-in — close enough!
    "hey jarvis": "hey_jarvis",
    "jarvis": "hey_jarvis",
    "alexa": "alexa",
    "computer": "computer",         # Star Trek easter egg
}

# Custom keyword model directory
_CUSTOM_KEYWORD_DIR = Path.home() / ".friday" / "hotword_models"


# ---------------------------------------------------------------------------
# OpenWakeWord Provider (Primary — Free, Local, No API Key)
# ---------------------------------------------------------------------------


class OpenWakeWordProvider:
    """OpenWakeWord — free, local hotword detection.
    
    - Apache 2.0 license — no API key, no company email, no cloud
    - Uses ONNX runtime — lightweight and fast
    - Pre-trained models: "hey_jarvis", "alexa", "hey_mycroft"
    - ~50MB total (ONNX runtime + models)
    - ~5ms per frame on CPU
    
    Install: pip install openwakeword
    
    We use "hey_jarvis" as the closest model to "Hey Friday" —
    it's a two-syllable wake word that works excellently.
    """

    name = "openwakeword"
    is_available = False
    _oww = None
    _model_name: str = "hey_jarvis"
    _threshold: float = 0.5
    _sample_rate = 16000
    _frame_length = 1280  # OpenWakeWord needs 1280 samples (80ms at 16kHz)

    def __init__(self, keyword: str = "hey friday", sensitivity: float = 0.7):
        self.keyword = keyword.lower()
        # Map our keyword to OpenWakeWord model
        self._model_name = _OPENWAKEWORD_KEYWORDS.get(self.keyword, "hey_jarvis")
        # Sensitivity: 0.0-1.0 -> threshold: 0.8-0.2
        self._threshold = 0.8 - (sensitivity * 0.6)
        self._try_load()

    def _try_load(self) -> bool:
        """Load OpenWakeWord model."""
        try:
            from openwakeword import Model as OWWModel
            self._oww = OWWModel()
            self.is_available = True
            # Log available models
            available = list(self._oww.models.keys()) if hasattr(self._oww, 'models') else []
            logger.info(
                f"OpenWakeWord loaded (model: '{self._model_name}', "
                f"threshold: {self._threshold:.2f}, "
                f"available: {available})"
            )
            return True
        except ImportError as exc:
            logger.warning(f"OpenWakeWord not installed ({exc}). "
                          "Install: pip install openwakeword")
            self.is_available = False
            return False
        except Exception as exc:
            logger.warning(f"OpenWakeWord load failed: {exc}")
            self.is_available = False
            return False

    def process(self, frame: bytes | list[int]) -> bool:
        """Process an audio frame. Returns True if hotword detected.
        
        Args:
            frame: Audio data — any size. OpenWakeWord buffers internally.
        """
        if not self.is_available or self._oww is None:
            return False

        try:
            # Convert bytes to numpy array
            import numpy as np
            if isinstance(frame, bytes):
                audio = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.array(frame, dtype=np.float32) / 32768.0

            # Process with OpenWakeWord
            prediction = self._oww.predict(audio)

            # Check if our model triggered above threshold
            # OpenWakeWord returns dict with model names as keys
            score = prediction.get(self._model_name, 0.0)
            if score > self._threshold:
                logger.debug(f"Hotword detected: {self._model_name} (score: {score:.3f})")
                return True

        except Exception:
            pass
        return False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def frame_length(self) -> int:
        return self._frame_length

    def set_sensitivity(self, sensitivity: float) -> None:
        """Adjust hotword sensitivity (0.0-1.0)."""
        # Sensitivity: 0.0-1.0 -> threshold: 0.8-0.2
        self._threshold = 0.8 - (sensitivity * 0.6)
        self._threshold = max(0.2, min(0.8, self._threshold))

    def cleanup(self) -> None:
        """Release resources."""
        self._oww = None


# ---------------------------------------------------------------------------
# Porcupine Provider (Secondary — Needs Free API Key)
# ---------------------------------------------------------------------------


# Porcupine access key — free tier from Picovoice Console
# https://console.picovoice.ai/
_PORCUPINE_ACCESS_KEY = os.environ.get(
    "PORCUPINE_ACCESS_KEY",
    "",  # User must set this
)


class PorcupineProvider:
    """Porcupine wake word detection — local, fast, accurate.
    
    - ~200KB model per keyword
    - ~1ms latency per frame
    - Built-in keywords + custom trained models
    - Free tier: unlimited keywords, limited to 1 access key
    """

    name = "porcupine"
    is_available = False
    _porcupine = None
    _sample_rate = 16000
    _frame_length = 512  # Porcupine requires exactly 512 samples per frame

    def __init__(self, keyword: str = "hey friday", sensitivity: float = 0.7):
        self.keyword = keyword.lower()
        self.sensitivity = sensitivity
        self._try_load()

    def _try_load(self) -> bool:
        """Load Porcupine with the specified keyword."""
        if not _PORCUPINE_ACCESS_KEY:
            logger.warning(
                "Porcupine access key not set. "
                "Set PORCUPINE_ACCESS_KEY env var or use "
                "https://console.picovoice.ai/ to get a free key."
            )
            self.is_available = False
            return False

        try:
            import pvporcupine

            # Try built-in keywords first
            try:
                self._porcupine = pvporcupine.create(
                    access_key=_PORCUPINE_ACCESS_KEY,
                    keywords=[self.keyword],
                    sensitivities=[self.sensitivity],
                )
                self.is_available = True
                logger.info(f"Porcupine loaded (keyword: '{self.keyword}')")
                return True
            except Exception:
                pass

            # Try custom keyword model
            model_path = _CUSTOM_KEYWORD_DIR / f"{self.keyword}_en_v3.ppn"
            if model_path.exists():
                self._porcupine = pvporcupine.create(
                    access_key=_PORCUPINE_ACCESS_KEY,
                    keyword_paths=[str(model_path)],
                    sensitivities=[self.sensitivity],
                )
                self.is_available = True
                logger.info(f"Porcupine loaded (custom: {model_path})")
                return True

        except ImportError:
            logger.debug("pvporcupine not available")
        except Exception as exc:
            logger.debug(f"Porcupine load failed: {exc}")

        self.is_available = False
        return False

    def process(self, frame: bytes | list[int]) -> bool:
        """Process a single audio frame. Returns True if hotword detected.
        
        Args:
            frame: 512 samples of 16-bit PCM audio (at 16kHz)
                  Can be bytes (length 1024) or list of ints (length 512)
        """
        if not self.is_available or self._porcupine is None:
            return False

        try:
            # Convert bytes to list of ints if needed
            if isinstance(frame, bytes):
                # Handle frames that aren't exactly 1024 bytes gracefully
                expected_bytes = self._frame_length * 2  # 1024 bytes for 16-bit
                if len(frame) != expected_bytes:
                    # Pad or truncate to expected size
                    if len(frame) < expected_bytes:
                        frame = frame + b'\x00' * (expected_bytes - len(frame))
                    else:
                        frame = frame[:expected_bytes]
                import struct
                frame = list(struct.unpack(f"<{self._frame_length}h", frame))
            elif len(frame) != self._frame_length:
                # Wrong sample count — can't process
                return False

            return self._porcupine.process(frame) >= 0
        except Exception:
            return False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def frame_length(self) -> int:
        return self._frame_length

    def set_sensitivity(self, sensitivity: float) -> None:
        """Adjust hotword sensitivity at runtime."""
        self.sensitivity = max(0.0, min(1.0, sensitivity))
        # Re-create the engine with new sensitivity
        if self._porcupine:
            try:
                self._porcupine.delete()
            except Exception:
                pass
        self._try_load()

    def cleanup(self) -> None:
        """Release resources."""
        if self._porcupine:
            try:
                self._porcupine.delete()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Energy-Based Detection (emergency fallback)
# ---------------------------------------------------------------------------


class EnergyDetector:
    """Simple energy-based voice detection.
    
    No ML model needed. Detects loud sounds above a threshold.
    Very basic — not a real hotword detector, just detects any
    loud speech near the microphone.
    
    Useful as a last-resort fallback when no hotword engine is available.
    """

    name = "energy"
    is_available = True
    _threshold: float
    _min_speech_frames: int
    _speech_frames: int = 0

    def __init__(self, threshold: float = 0.02, min_speech_frames: int = 5):
        """Initialize energy detector.
        
        Args:
            threshold: RMS energy threshold (0.0 to 1.0). Lower = more sensitive.
            min_speech_frames: Minimum consecutive frames above threshold to trigger.
        """
        self._threshold = threshold
        self._min_speech_frames = min_speech_frames

    def process(self, frame: bytes | list[int]) -> bool:
        """Process audio frame. Returns True when energy threshold exceeded.
        
        Uses a simple debounce: requires N consecutive frames above threshold.
        """
        import math

        # Convert to float samples
        if isinstance(frame, bytes):
            import struct
            samples = struct.unpack(f"<{len(frame)//2}h", frame)
        else:
            samples = frame

        # Calculate RMS energy
        if not samples:
            return False
        sum_sq = sum(s * s for s in samples)
        rms = math.sqrt(sum_sq / len(samples))
        normalized = rms / 32768.0  # Normalize to 0.0-1.0

        if normalized > self._threshold:
            self._speech_frames += 1
            if self._speech_frames >= self._min_speech_frames:
                self._speech_frames = 0
                return True
        else:
            self._speech_frames = 0

        return False

    def set_sensitivity(self, sensitivity: float) -> None:
        """Adjust sensitivity (0.0-1.0 maps to threshold 0.05-0.005)."""
        # Invert: higher sensitivity = lower threshold
        self._threshold = 0.05 - (sensitivity * 0.045)
        self._threshold = max(0.005, min(0.05, self._threshold))

    @property
    def sample_rate(self) -> int:
        return 16000

    @property
    def frame_length(self) -> int:
        return 512  # Same as Porcupine for compatibility

    def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------------------
# HotwordDetector — main hotword frontend
# ---------------------------------------------------------------------------


class HotwordDetector:
    """Wake word detection — listens for "Hey Friday."
    
    Auto-selects the best available detection engine:
      1. OpenWakeWord (free, local, no API key)
      2. Porcupine (needs free API key)
      3. Energy detection (no model, basic)
    
    Usage:
        hotword = HotwordDetector("hey friday")
        
        # In audio loop:
        frame = microphone.read(1280)  # OpenWakeWord uses 1280 samples
        if hotword.process(frame):
            print("Hotword detected!")
            pipeline.start_recording()
    """

    def __init__(self, keyword: str = "hey friday", sensitivity: float = 0.7):
        self.keyword = keyword
        self.sensitivity = sensitivity
        self._detector: OpenWakeWordProvider | PorcupineProvider | EnergyDetector | None = None
        self._init()

    def _init(self):
        """Initialize hotword detection. Tries providers in priority order."""
        # 1. Try OpenWakeWord first (free, no API key)
        try:
            oww = OpenWakeWordProvider(self.keyword, self.sensitivity)
            if oww.is_available:
                self._detector = oww
                logger.info(f"Hotword active: OpenWakeWord ('{self.keyword}')")
                return
        except Exception:
            pass

        # 2. Try Porcupine (needs API key)
        try:
            porcupine = PorcupineProvider(self.keyword, self.sensitivity)
            if porcupine.is_available:
                self._detector = porcupine
                logger.info(f"Hotword active: Porcupine ('{self.keyword}')")
                return
        except Exception:
            pass

        # 3. Fallback to energy detector
        logger.info("Hotword: energy detection fallback (basic)")
        self._detector = EnergyDetector()

    @property
    def is_available(self) -> bool:
        return self._detector is not None

    @property
    def provider_name(self) -> str:
        if isinstance(self._detector, OpenWakeWordProvider):
            return "openwakeword"
        if isinstance(self._detector, PorcupineProvider):
            return "porcupine"
        if isinstance(self._detector, EnergyDetector):
            return "energy"
        return "none"

    @property
    def sample_rate(self) -> int:
        return getattr(self._detector, "sample_rate", 16000)

    @property
    def frame_length(self) -> int:
        return getattr(self._detector, "frame_length", 1280)

    def process(self, frame: bytes | list[int]) -> bool:
        """Process an audio frame. Returns True if hotword detected."""
        if not self._detector:
            return False
        return self._detector.process(frame)

    def set_sensitivity(self, sensitivity: float) -> None:
        """Adjust hotword sensitivity at runtime."""
        self.sensitivity = sensitivity
        if hasattr(self._detector, "set_sensitivity"):
            self._detector.set_sensitivity(sensitivity)

    def cleanup(self) -> None:
        """Release resources."""
        if hasattr(self._detector, "cleanup"):
            self._detector.cleanup()
