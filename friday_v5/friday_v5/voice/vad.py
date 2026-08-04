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
import urllib.error
import urllib.request
from pathlib import Path
from typing import ClassVar, Optional

import numpy as np

logger = logging.getLogger("friday_v5.voice.vad")

# Silero model — small (~2 MB) ONNX, downloaded once to ~/.friday/models/
# The repo ships the ONNX inside the source tree (not as release assets);
# the release-asset URLs (v5.1/v6.x/silero_vad.onnx) 404.
_SILERO_URLS = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2/"
    "src/silero_vad/data/silero_vad.onnx",
    "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2/"
    "src/silero_vad/data/silero_vad_16k_op15.onnx",
)
_SILERO_PATH = Path.home() / ".friday" / "models" / "silero_vad.onnx"


# ---------------------------------------------------------------------------
# Silero VAD (ONNX)
# ---------------------------------------------------------------------------


class SileroVAD:
    """ML-based voice activity detection via ONNX runtime.

    Handles BOTH model generations transparently:

    * **v6.x (stateful)** — inputs ``input``/``state``/``sr``, outputs
      ``output`` + recurrent ``stateN``. The recurrent state is carried
      across frames (like a realtime streaming VAD), and ``state`` is
      reset whenever a silence gap clears the buffer.
    * **v5.x (stateless)** — single ``input`` tensor, one probability out.

    ``is_speech`` pads whatever it receives to a whole 512-sample window
    so a 480-sample mic frame can't silently crash inference.
    ``is_available`` is only set True after a live trial inference
    succeeds — a file that merely exists but is corrupt/unsupported never
    claims to be usable.
    """

    #: ONNX static window for v5/v6 silero_vad.onnx.
    WINDOW = 512
    is_available = False
    _model = None

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._stateful = False
        self._state: Optional[np.ndarray] = None
        self._try_load()

    def _download_with_retry(self, urls, path: Path,
                             retries: int = 3, backoff: float = 2.0) -> bool:
        """Try each candidate URL in order; atomic tmp→rename on success.

        A partial/interrupted download must never leave a corrupt file at
        the final path (it would be treated as "already downloaded" and
        fail to load forever).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        for url in urls:
            for attempt in range(retries):
                try:
                    urllib.request.urlretrieve(url, tmp)
                    if tmp.exists() and tmp.stat().st_size < 64 * 1024:
                        raise RuntimeError(
                            f"Suspiciously small download ({tmp.stat().st_size} bytes)")
                    tmp.replace(path)
                    logger.info(f"Silero VAD model downloaded ({path.name})")
                    return True
                except (urllib.error.URLError, urllib.error.HTTPError,
                        OSError, TimeoutError, RuntimeError) as exc:
                    logger.warning(
                        f"Download failed ({url}, attempt {attempt+1}): {exc}")
                    tmp.unlink(missing_ok=True)
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
                if not self._download_with_retry(_SILERO_URLS, _SILERO_PATH):
                    logger.warning("Silero VAD download failed")
                    return False
            session = onnxruntime.InferenceSession(
                str(_SILERO_PATH), providers=["CPUExecutionProvider"])
            input_names = {i.name for i in session.get_inputs()}
            # Trial inference — prove the model actually runs before
            # claiming availability (existence ≠ loadable).
            audio = np.zeros((1, self.WINDOW), dtype=np.float32)
            if "sr" in input_names:
                self._stateful = True
                self._state = np.zeros((2, 1, 128), dtype=np.float32)
                sr = np.array([16000], dtype=np.int64)
                session.run(None, {"input": audio, "state": self._state, "sr": sr})
            else:
                session.run(None, {next(iter(input_names)): audio})
            self._model = session
            self.is_available = True
            logger.info("Silero VAD loaded (stateful)" if self._stateful
                        else "Silero VAD loaded (stateless)")
            return True
        except Exception as exc:
            logger.warning(
                f"Silero VAD load failed ({exc}) — falling back to "
                "WebRTC/energy VAD")
            return False

    def _frame_audio(self, audio: np.ndarray) -> np.ndarray:
        """Convert mono/1-D audio into whole 512-sample windows."""
        if len(audio.shape) > 1:
            audio = audio.reshape(-1)
        n_frames = max(len(audio) // self.WINDOW, 1)
        padded = np.zeros((1, n_frames * self.WINDOW), dtype=np.float32)
        padded[0, :min(len(audio), padded.shape[1])] = audio[:padded.shape[1]]
        return padded

    def _reset_state(self) -> None:
        """Clear recurrent state (new utterance / silence gap)."""
        if self._stateful:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def is_speech(self, audio: np.ndarray, sample_rate: int = 16000) -> bool:
        if not self.is_available or self._model is None:
            return False
        try:
            if len(audio) == 0:
                return False
            audio = self._frame_audio(audio)
            if self._stateful:
                sr = np.array([16000], dtype=np.int64)
                outputs = self._model.run(
                    None, {"input": audio, "state": self._state, "sr": sr})
                self._state = np.asarray(outputs[1], dtype=np.float32)
                probs = np.asarray(outputs[0]).ravel()
            else:
                name = self._model.get_inputs()[0].name
                outputs = self._model.run(None, {name: audio})
                probs = np.asarray(outputs[0]).ravel()
            # One probability per 512-window; a frame with multiple windows
            # counts as speech if ANY window is speech.
            return float(probs.max()) > self.threshold
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

    MODES: ClassVar[dict[int, int]] = {0: 0, 1: 1, 2: 2, 3: 3}

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
