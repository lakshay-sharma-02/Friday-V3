"""Audio Stream Management for Friday V4 — Voice Wave 2.0.

Primary backend: `sounddevice` (pure-ctypes over the already-present
libportaudio.so — no compile, no pyaudio wheel pain).
Fallback: `pyaudio` (classic, if installed).

Responsibilities:
  - Device enumeration (inputs / outputs)
  - Managed microphone input stream with automatic device selection
  - Audio *playback* helpers for WAV bytes / files (used by TTS + chimes)

Design:
  - Non-blocking capture via callback; frames delivered as float32 [-1, 1]
  - Fail-fast when no backend is importable — never crash callers
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger("friday_v5.voice.audio")

# Audio format constants (Whisper standard)
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 480 samples
FRAME_BYTES = FRAME_SIZE * 2  # 16-bit = 2 bytes per sample


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


@dataclass
class AudioDeviceInfo:
    """Information about an audio device."""
    index: int
    name: str
    inputs: int = 0
    outputs: int = 0
    sample_rate: int = 0
    is_default: bool = False


def _sounddevice_available() -> bool:
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


def _pyaudio_available() -> bool:
    try:
        import pyaudio  # noqa: F401
        return True
    except Exception:
        return False


def _backend_name() -> str:
    if _sounddevice_available():
        return "sounddevice"
    if _pyaudio_available():
        return "pyaudio"
    return "none"


def list_input_devices() -> list[AudioDeviceInfo]:
    """List all available microphone input devices."""
    try:
        import sounddevice as sd
        devices = []
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
        for i, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0:
                devices.append(AudioDeviceInfo(
                    index=i,
                    name=info["name"],
                    inputs=info["max_input_channels"],
                    outputs=info["max_output_channels"],
                    sample_rate=int(info["default_samplerate"]),
                    is_default=i == default_in,
                ))
        return devices
    except ImportError:
        pass

    try:
        import pyaudio
        p = pyaudio.PyAudio()
        devices = []
        try:
            default_in = p.get_default_input_device_info()["index"]
        except Exception:
            default_in = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                devices.append(AudioDeviceInfo(
                    index=i,
                    name=info["name"],
                    inputs=info["maxInputChannels"],
                    outputs=info["maxOutputChannels"],
                    sample_rate=int(info["defaultSampleRate"]),
                    is_default=i == default_in,
                ))
        p.terminate()
        return devices
    except ImportError:
        pass

    logger.warning("No audio backend available for device enumeration")
    return []


def list_output_devices() -> list[AudioDeviceInfo]:
    """List all available speaker output devices."""
    try:
        import sounddevice as sd
        devices = []
        try:
            default_out = sd.default.device[1]
        except Exception:
            default_out = None
        for i, info in enumerate(sd.query_devices()):
            if info["max_output_channels"] > 0:
                devices.append(AudioDeviceInfo(
                    index=i,
                    name=info["name"],
                    inputs=info["max_input_channels"],
                    outputs=info["max_output_channels"],
                    sample_rate=int(info["default_samplerate"]),
                    is_default=i == default_out,
                ))
        return devices
    except ImportError:
        pass

    try:
        import pyaudio
        p = pyaudio.PyAudio()
        devices = []
        try:
            default_out = p.get_default_output_device_info()["index"]
        except Exception:
            default_out = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                devices.append(AudioDeviceInfo(
                    index=i,
                    name=info["name"],
                    inputs=info["maxInputChannels"],
                    outputs=info["maxOutputChannels"],
                    sample_rate=int(info["defaultSampleRate"]),
                    is_default=i == default_out,
                ))
        p.terminate()
        return devices
    except ImportError:
        pass

    return []


# ---------------------------------------------------------------------------
# Audio playback helpers (shared by TTS + chimes)
# ---------------------------------------------------------------------------

#: Track the currently-playing subprocess so TTS.stop() can interrupt it.
_active_player: list = []
_player_lock = threading.Lock()


def stop_playback() -> None:
    """Kill any currently-playing audio subprocess (used for interruption)."""
    clear_play_queue()
    with _player_lock:
        procs = list(_active_player)
        _active_player.clear()
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


# ── Streaming playback queue ──────────────────────────────────────────────
# Plays wav files back-to-back on a single worker thread. TTS synthesizes
# sentence N+1 while sentence N plays, so the user hears the first words
# almost immediately instead of waiting for the full response to render.

_play_queue: list = []          # paths of wavs awaiting playback
_play_queue_lock = threading.Lock()
_play_queue_cv = threading.Condition(_play_queue_lock)
_play_thread: Optional[threading.Thread] = None


def _play_queue_worker() -> None:
    """Single worker: pop a wav, play it (interruptible), loop."""
    while True:
        with _play_queue_cv:
            while not _play_queue:
                _play_queue_cv.wait()
            path = _play_queue.pop(0)
        if path is None:  # sentinel → shutdown
            break
        try:
            play_wav_file(path)
        except Exception:
            pass


def queue_wav(path: str) -> None:
    """Append a wav file to the streaming playback queue."""
    global _play_thread
    with _play_queue_cv:
        _play_queue.append(path)
        if not _play_thread or not _play_thread.is_alive():
            thread = threading.Thread(target=_play_queue_worker,
                                      name="tts-player", daemon=True)
            thread.start()
            _play_thread = thread
        _play_queue_cv.notify()


def flush_play_queue() -> None:
    """Wait for the streaming queue to drain (blocking, bounded)."""
    deadline = time.time() + 30
    while time.time() < deadline:
        with _play_queue_lock:
            if not _play_queue:
                return
        time.sleep(0.05)


def clear_play_queue() -> None:
    """Drop any queued-but-not-yet-played wavs (used on stop())."""
    with _play_queue_cv:
        _play_queue.clear()


def _run_player(cmd: list[str]) -> None:
    """Launch an audio player subprocess and register it for interruption."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        with _player_lock:
            _active_player.append(proc)
        try:
            proc.wait(timeout=60)
        finally:
            with _player_lock:
                if proc in _active_player:
                    _active_player.remove(proc)
    except Exception as exc:
        logger.warning(f"Audio playback failed: {exc}")


def play_wav_bytes(wav_data: bytes) -> None:
    """Play WAV bytes through the best available player.

    Tries in order: paplay (PulseAudio), aplay (ALSA), afplay (macOS),
    then ffplay/ffmpeg. Pure subprocess — no audio lib dependency.
    """
    if not wav_data:
        return
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data)
        path = f.name
    try:
        play_wav_file(path)
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def play_wav_file(path: str) -> None:
    """Play a WAV file using the best available audio player.

    Runs the player as a killable subprocess (see stop_playback) so
    TTS interruption actually cuts audio.
    """
    system = platform.system()
    try:
        if system == "Linux":
            if Path("/usr/bin/paplay").exists():
                _run_player(["paplay", path])
                return
            if Path("/usr/bin/aplay").exists():
                _run_player(["aplay", path])
                return
        elif system == "Darwin":
            _run_player(["afplay", path])
            return
        elif system == "Windows":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)  # type: ignore[attr-defined]
            return

        # Universal fallback
        for tool in ("ffplay", "ffmpeg"):
            p = Path("/usr/bin") / tool
            if p.exists():
                if tool == "ffplay":
                    _run_player([str(p), "-nodisp", "-autoexit", path])
                else:
                    _run_player([str(p), "-i", path, "-f", "alsa", "default"])
                return
    except Exception as exc:
        logger.warning(f"Audio playback failed: {exc}")


# ---------------------------------------------------------------------------
# AudioStream — microphone input
# ---------------------------------------------------------------------------


class AudioStream:
    """Managed microphone input stream.

    Reads audio frames in a background thread/callback and delivers them
    to a user callback as float32 numpy arrays at 16 kHz.

    Usage:
        stream = AudioStream()
        stream.start(lambda frame: print(frame.shape))
        ...
        stream.stop()
    """

    def __init__(self, device_index: Optional[int] = None,
                 sample_rate: int = SAMPLE_RATE,
                 frame_size: int = FRAME_SIZE):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._callback: Optional[Callable[[np.ndarray], None]] = None
        self._running = False
        self._lock = threading.Lock()
        self._backend = _backend_name()
        self._stream = None       # sounddevice or pyaudio stream
        self._pyaudio = None      # pyaudio instance (pyaudio backend)

    @property
    def is_active(self) -> bool:
        return self._running

    @property
    def backend(self) -> str:
        return self._backend

    def start(self, callback: Callable[[np.ndarray], None]) -> bool:
        """Start listening on the microphone.

        Args:
            callback: Called with each audio frame (float32, [-1, 1]).

        Returns:
            True if started successfully.
        """
        if self._running:
            logger.warning("Audio stream already active")
            return False
        if self._backend == "none":
            logger.warning("No audio backend available — cannot capture audio")
            return False

        self._callback = callback
        self._running = True

        if self._backend == "sounddevice":
            return self._start_sounddevice()
        return self._start_pyaudio()

    # -- sounddevice backend ------------------------------------------------

    def _start_sounddevice(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:
            logger.warning(f"sounddevice unavailable: {exc}")
            self._running = False
            return False

        device_idx = self._resolve_device_sd(sd)

        def _sd_callback(indata, frames, time_info, status):
            if status:
                logger.debug(f"sounddevice status: {status}")
            if self._running and self._callback is not None:
                try:
                    audio = indata[:, 0].astype(np.float32)
                    self._callback(audio)
                except Exception as exc:
                    logger.debug(f"Audio callback error: {exc}")

        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.frame_size,
                device=device_idx,
                callback=_sd_callback,
            )
            self._stream = stream
            stream.start()
            logger.info("Audio stream started (sounddevice)")
            return True
        except Exception as exc:
            logger.warning(f"sounddevice stream failed: {exc}")
            self._running = False
            return False

    def _resolve_device_sd(self, sd) -> Optional[int]:
        if self.device_index is not None:
            return self.device_index
        try:
            default_in = sd.default.device[0]
            if default_in is not None:
                return default_in
        except Exception:
            pass
        try:
            for i, info in enumerate(sd.query_devices()):
                if info["max_input_channels"] > 0:
                    return i
        except Exception:
            pass
        return None

    # -- pyaudio backend ----------------------------------------------------

    def _start_pyaudio(self) -> bool:
        try:
            import pyaudio
        except Exception as exc:
            logger.warning(f"pyaudio unavailable: {exc}")
            self._running = False
            return False

        p = pyaudio.PyAudio()
        device_idx = self.device_index
        if device_idx is None:
            try:
                device_idx = p.get_default_input_device_info()["index"]
            except Exception:
                for i in range(p.get_device_count()):
                    info = p.get_device_info_by_index(i)
                    if info["maxInputChannels"] > 0:
                        device_idx = i
                        break
        if device_idx is None:
            logger.error("No microphone input device found")
            p.terminate()
            self._running = False
            return False

        def _py_callback(in_data, frame_count, time_info, status):
            if self._running and self._callback is not None:
                try:
                    audio = np.frombuffer(in_data, dtype=np.int16).astype(
                        np.float32) / 32768.0
                    self._callback(audio)
                except Exception as exc:
                    logger.debug(f"Audio callback error: {exc}")
            return (None, 0)

        try:
            self._pyaudio = p
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                input_device_index=device_idx,
                frames_per_buffer=self.frame_size,
                stream_callback=_py_callback,
            )
            self._stream = stream
            stream.start_stream()
            logger.info("Audio stream started (pyaudio)")
            return True
        except Exception as exc:
            logger.warning(f"pyaudio stream failed: {exc}")
            try:
                p.terminate()
            except Exception:
                pass
            self._running = False
            return False

    # -- lifecycle ----------------------------------------------------------

    def stop(self) -> None:
        """Stop listening and release the microphone."""
        self._running = False
        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        p = getattr(self, "_pyaudio", None)
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        self._stream = None
        self._pyaudio = None

    def read_blocking(self, duration_seconds: float = 3.0) -> np.ndarray:
        """Read audio for a fixed duration (blocking).

        Returns a float32 numpy array of concatenated frames, or an
        empty array on failure.
        """
        frames: list[np.ndarray] = []
        event = threading.Event()

        def collect(frame: np.ndarray):
            frames.append(frame)
            if len(frames) * self.frame_size / self.sample_rate >= duration_seconds:
                event.set()

        if not self.start(collect):
            return np.array([], dtype=np.float32)
        event.wait(timeout=duration_seconds + 1)
        self.stop()

        if frames:
            return np.concatenate(frames)
        return np.array([], dtype=np.float32)
