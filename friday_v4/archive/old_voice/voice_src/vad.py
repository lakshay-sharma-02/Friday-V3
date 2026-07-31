"""Voice Activity Detection for Friday V4.

Detects when someone is speaking in an audio stream.
Gates the STT pipeline — only transcribes when speech is detected.

Two-tier VAD:
  1. Silero VAD (primary) — ML-based, best accuracy, moderate CPU
  2. WebRTC VAD (fallback) — lightweight, low CPU, decent accuracy

Modes:
  0: Disabled (always listening, highest CPU)
  1: Normal (WebRTC VAD, <1% CPU, moderate accuracy)
  2: Aggressive (higher threshold, fewer false positives)
  3: Silero VAD (ML-based, best accuracy, ~5% CPU)
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("friday_v4.voice.vad")


# ---------------------------------------------------------------------------
# Silero VAD Provider
# ---------------------------------------------------------------------------


class SileroVAD:
    """Silero VAD — ML-based voice activity detection.
    
    Pre-trained PyTorch model, runs on CPU. Very accurate but
    slightly higher CPU usage than WebRTC VAD.
    
    Model is downloaded on first use (~2MB) and cached.
    """

    MODEL_URL = "https://github.com/snakers4/silero-vad/releases/download/v5.1/silero_vad.onnx"
    MODEL_PATH = Path.home() / ".friday" / "silero_vad.onnx"
    is_available = False
    _model = None

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._try_load()

    def _download_with_retry(self, url: str, path: Path, retries: int = 3,
                               backoff: float = 2.0) -> bool:
        """Download a file with retries and exponential backoff.
        
        Network failures, timeouts, and HTTP errors are retried up to
        `retries` times with `backoff` seconds between attempts.
        """
        import time
        import urllib.error
        import urllib.request
        
        for attempt in range(retries):
            try:
                urllib.request.urlretrieve(url, path)
                return True
            except (urllib.error.URLError, urllib.error.HTTPError,
                    OSError, TimeoutError) as exc:
                logger.warning(
                    f"Download failed (attempt {attempt+1}/{retries}): {exc}"
                )
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
        return False

    def _try_load(self) -> bool:
        """Download and load Silero VAD model (ONNX format)."""
        try:
            # Try ONNX runtime first (lighter than PyTorch)
            import onnxruntime

            # Download model if not cached
            if not self.MODEL_PATH.exists():
                self.MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                logger.info("Downloading Silero VAD model...")
                if self._download_with_retry(self.MODEL_URL, self.MODEL_PATH):
                    logger.info("Silero VAD model downloaded")
                else:
                    logger.warning("Silero VAD download failed after retries")
                    self.is_available = False
                    return False

            self._model = onnxruntime.InferenceSession(
                str(self.MODEL_PATH),
                providers=["CPUExecutionProvider"],
            )
            self.is_available = True
            logger.info("Silero VAD loaded")
            return True

        except ImportError:
            logger.debug("onnxruntime not available for Silero VAD")
            self.is_available = False
            return False
        except Exception as exc:
            logger.debug(f"Silero VAD load failed: {exc}")
            self.is_available = False
            return False

    def is_speech(self, audio_frame: np.ndarray, sample_rate: int = 16000
                  ) -> bool:
        """Check if an audio frame contains speech.
        
        Args:
            audio_frame: 16kHz mono audio, ~30ms (480 samples)
            sample_rate: Must be 16000 or 8000
        
        Returns:
            True if speech probability > threshold
        """
        if not self.is_available or self._model is None:
            return False

        try:
            # Prepare input: shape (1, N), float32
            if len(audio_frame.shape) == 1:
                audio_frame = audio_frame.reshape(1, -1)
            audio_input = audio_frame.astype(np.float32)

            # Run inference
            inputs = {self._model.get_inputs()[0].name: audio_input}
            outputs = self._model.run(None, inputs)

            # Get speech probability
            prob = outputs[0][0][0]
            return prob > self.threshold

        except Exception as exc:
            logger.debug(f"Silero VAD inference failed: {exc}")
            return False

    def get_speech_segments(self, audio: np.ndarray, sample_rate: int = 16000,
                            frame_ms: int = 30) -> list[tuple[float, float]]:
        """Find speech segments in audio.
        
        Returns:
            List of (start_sec, end_sec) tuples for speech segments
        """
        if not self.is_available:
            return [(0.0, len(audio) / sample_rate)]  # Assume everything is speech

        frame_len = int(sample_rate * frame_ms / 1000)
        segments = []
        in_speech = False
        start = 0.0

        for i in range(0, len(audio) - frame_len, frame_len):
            frame = audio[i:i + frame_len]
            speech = self.is_speech(frame, sample_rate)
            timestamp = i / sample_rate

            if speech and not in_speech:
                in_speech = True
                start = timestamp
            elif not speech and in_speech:
                in_speech = False
                segments.append((start, timestamp))

        if in_speech:
            segments.append((start, len(audio) / sample_rate))

        # Merge segments with small gaps (< 300ms)
        merged = []
        for seg in segments:
            if merged and (seg[0] - merged[-1][1]) < 0.3:
                merged[-1] = (merged[-1][0], seg[1])
            else:
                merged.append(seg)

        # Filter out very short segments (< 200ms, likely noise)
        merged = [s for s in merged if (s[1] - s[0]) > 0.2]

        return merged


# ---------------------------------------------------------------------------
# WebRTC VAD Provider
# ---------------------------------------------------------------------------


class WebRTCVAD:
    """WebRTC VAD — lightweight voice activity detection.
    
    Very low CPU usage (<1%). Based on Gaussian mixture models
    in the WebRTC project. Good accuracy for clean speech.
    """

    is_available = False
    _vad = None

    # Aggressiveness modes: 0=least, 3=most
    MODES = {
        0: 0,  # Normal
        1: 1,  # Low Bitrate
        2: 2,  # Aggressive
        3: 3,  # Very Aggressive
    }

    def __init__(self, mode: int = 1):
        self.mode = mode
        self._try_load()

    def _try_load(self) -> bool:
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(self.MODES.get(self.mode, 1))
            self.is_available = True
            logger.info(f"WebRTC VAD loaded (mode {self.mode})")
            return True
        except ImportError:
            logger.debug("webrtcvad not available")
            self.is_available = False
            return False

    def is_speech(self, audio_frame: bytes, sample_rate: int = 16000) -> bool:
        """Check if a 30ms PCM16 audio frame contains speech.
        
        Args:
            audio_frame: 480 bytes of 16-bit PCM audio (30ms at 16kHz)
            sample_rate: 8000, 16000, or 32000
        
        Returns:
            True if speech detected
        """
        if not self.is_available or self._vad is None:
            return False
        try:
            return self._vad.is_speech(audio_frame, sample_rate)
        except Exception:
            return False

    def set_mode(self, mode: int) -> None:
        """Change aggressiveness mode."""
        self.mode = mode
        if self._vad:
            try:
                import webrtcvad
                self._vad = webrtcvad.Vad(self.MODES.get(mode, 1))
            except Exception:
                pass

    def get_speech_segments(self, audio_bytes: bytes, sample_rate: int = 16000,
                            frame_ms: int = 30) -> list[tuple[float, float]]:
        """Find speech segments in PCM16 audio."""
        if not self.is_available:
            return [(0.0, len(audio_bytes) / 2 / sample_rate)]

        frame_len = int(sample_rate * frame_ms / 1000) * 2  # 16-bit = 2 bytes
        segments = []
        in_speech = False
        start = 0.0

        for i in range(0, len(audio_bytes) - frame_len, frame_len):
            frame = audio_bytes[i:i + frame_len]
            speech = self.is_speech(frame, sample_rate)
            # i is byte offset; 16-bit PCM = 2 bytes per sample
            # timestamp = (byte_offset / bytes_per_sample) / sample_rate
            sample_offset = i // 2
            timestamp = sample_offset / sample_rate

            if speech and not in_speech:
                in_speech = True
                start = timestamp
            elif not speech and in_speech:
                in_speech = False
                segments.append((start, timestamp))

        if in_speech:
            segments.append((start, len(audio_bytes) / 2 / sample_rate))

        # Merge small gaps
        merged = []
        for seg in segments:
            if merged and (seg[0] - merged[-1][1]) < 0.3:
                merged[-1] = (merged[-1][0], seg[1])
            else:
                merged.append(seg)

        return [s for s in merged if (s[1] - s[0]) > 0.2]


# ---------------------------------------------------------------------------
# VoiceActivityDetector — main VAD frontend
# ---------------------------------------------------------------------------


class VoiceActivityDetector:
    """Detect speech in audio streams.
    
    Auto-selects the best available VAD engine.
    
    Usage:
        vad = VoiceActivityDetector(mode=3)  # Silero VAD
        if vad.is_speech(audio_frame):
            print("Someone is speaking!")
    """

    def __init__(self, mode: int = 1):
        self.mode = mode
        self._silero: Optional[SileroVAD] = None
        self._webrtc: Optional[WebRTCVAD] = None
        self._active_vad = None
        self._init()

    def _init(self):
        """Initialize VAD providers in priority order."""
        # Try Silero first (best accuracy)
        if self.mode >= 3:
            try:
                self._silero = SileroVAD()
                if self._silero.is_available:
                    self._active_vad = self._silero
                    return
            except Exception:
                pass

        # Fallback to WebRTC
        try:
            self._webrtc = WebRTCVAD(mode=min(self.mode, 2))
            if self._webrtc.is_available:
                self._active_vad = self._webrtc
                return
        except Exception:
            pass

    @property
    def is_available(self) -> bool:
        return self._active_vad is not None

    @property
    def provider_name(self) -> str:
        if isinstance(self._active_vad, SileroVAD):
            return "silero"
        if isinstance(self._active_vad, WebRTCVAD):
            return "webrtc"
        return "none"

    def is_speech(self, audio: bytes | np.ndarray, sample_rate: int = 16000
                  ) -> bool:
        """Check if audio contains speech.
        
        Accepts both PCM16 bytes (WebRTC VAD) and float32 numpy arrays
        (Silero VAD). Auto-detects the format.
        """
        if not self._active_vad:
            return False

        if isinstance(audio, np.ndarray):
            return self.is_speech_np(audio, sample_rate)
        else:
            return self.is_speech_bytes(audio, sample_rate)

    def is_speech_bytes(self, audio_frame: bytes, sample_rate: int = 16000
                        ) -> bool:
        """Check if a PCM16 audio frame contains speech."""
        if isinstance(self._active_vad, WebRTCVAD):
            return self._active_vad.is_speech(audio_frame, sample_rate)
        # Convert to numpy for Silero
        if isinstance(self._active_vad, SileroVAD):
            arr = np.frombuffer(audio_frame, dtype=np.int16).astype(np.float32) / 32768.0
            return self._active_vad.is_speech(arr, sample_rate)
        return False

    def is_speech_np(self, audio: np.ndarray, sample_rate: int = 16000
                     ) -> bool:
        """Check if a float32 numpy audio frame contains speech."""
        if isinstance(self._active_vad, SileroVAD):
            return self._active_vad.is_speech(audio, sample_rate)
        # Convert to bytes for WebRTC
        if isinstance(self._active_vad, WebRTCVAD):
            audio_bytes = (audio * 32768).astype(np.int16).tobytes()
            return self._active_vad.is_speech(audio_bytes, sample_rate)
        return False

    def get_speech_segments(self, audio: np.ndarray, sample_rate: int = 16000
                            ) -> list[tuple[float, float]]:
        """Find speech segments in audio."""
        if isinstance(self._active_vad, SileroVAD):
            return self._active_vad.get_speech_segments(audio, sample_rate)
        if isinstance(self._active_vad, WebRTCVAD):
            audio_bytes = (audio * 32768).astype(np.int16).tobytes()
            return self._active_vad.get_speech_segments(audio_bytes, sample_rate)
        return [(0.0, len(audio) / sample_rate)]
