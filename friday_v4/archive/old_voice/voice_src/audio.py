"""Audio Stream Management for Friday V4.

Manages microphone input and speaker output streams. Handles device
enumeration, selection, reconnection, and format conversion.

Design:
  - Non-blocking reads via background thread + callback
  - Automatic device selection (first valid input device)
  - Graceful handling of device disconnection
  - Format conversion: PCM16 ↔ float32 numpy arrays
"""

from __future__ import annotations

import logging
import platform
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger("friday_v4.voice.audio")

# Audio format constants
SAMPLE_RATE = 16000  # Whisper standard
FRAME_DURATION_MS = 30  # Standard VAD frame
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 480 samples
FRAME_BYTES = FRAME_SIZE * 2  # 16-bit = 2 bytes per sample


# ---------------------------------------------------------------------------
# Audio Device Info
# ---------------------------------------------------------------------------


@dataclass
class AudioDeviceInfo:
    """Information about an audio device."""
    index: int
    name: str
    inputs: int  # Number of input channels
    outputs: int  # Number of output channels
    sample_rate: int
    is_default: bool = False


def list_input_devices() -> list[AudioDeviceInfo]:
    """List all available microphone input devices."""
    devices = []
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                devices.append(AudioDeviceInfo(
                    index=i,
                    name=info["name"],
                    inputs=info["maxInputChannels"],
                    outputs=info["maxOutputChannels"],
                    sample_rate=int(info["defaultSampleRate"]),
                    is_default=i == p.get_default_input_device_info()["index"],
                ))
        p.terminate()
    except ImportError:
        logger.warning("pyaudio not available for device enumeration")
    except Exception as exc:
        logger.warning(f"Device enumeration failed: {exc}")
    return devices


def list_output_devices() -> list[AudioDeviceInfo]:
    """List all available speaker output devices."""
    devices = []
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                devices.append(AudioDeviceInfo(
                    index=i,
                    name=info["name"],
                    inputs=info["maxInputChannels"],
                    outputs=info["maxOutputChannels"],
                    sample_rate=int(info["defaultSampleRate"]),
                    is_default=i == p.get_default_output_device_info()["index"],
                ))
        p.terminate()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(f"Output device enumeration failed: {exc}")
    return devices


# ---------------------------------------------------------------------------
# AudioStream — microphone input
# ---------------------------------------------------------------------------


class AudioStream:
    """Managed microphone input stream.
    
    Reads audio frames in a background thread and delivers them to
    a callback. Handles device disconnection gracefully.
    
    Usage:
        stream = AudioStream()
        stream.start(callback=lambda frames: print(f"Got {len(frames)} samples"))
        ...
        stream.stop()
    """

    def __init__(self, device_index: Optional[int] = None,
                 sample_rate: int = SAMPLE_RATE,
                 frame_size: int = FRAME_SIZE):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._stream: Optional["pyaudio.Stream"] = None
        self._pyaudio: Optional["pyaudio.PyAudio"] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._callback: Optional[Callable[[np.ndarray], None]] = None
        self._frame_queue: queue.Queue = queue.Queue(maxsize=100)
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self, callback: Callable[[np.ndarray], None]) -> bool:
        """Start listening on the microphone.
        
        Args:
            callback: Called with each audio frame (float32 numpy array, [-1, 1])
        
        Returns:
            True if started successfully
        """
        if self.is_active:
            logger.warning("Audio stream already active")
            return False

        # Fail fast if pyaudio is missing — don't spawn a thread that
        # dies with a bare ImportError inside _run().
        try:
            import pyaudio  # noqa: F401
        except ImportError as exc:
            logger.warning(f"pyaudio not available — cannot capture audio ({exc})")
            return False

        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        """Background audio capture loop."""
        import pyaudio

        retries = 0
        max_retries = 3

        while self._running and retries < max_retries:
            try:
                self._pyaudio = pyaudio.PyAudio()

                # Select device
                device_idx = self.device_index
                if device_idx is None:
                    try:
                        device_idx = self._pyaudio.get_default_input_device_info()["index"]
                    except Exception:
                        # Find first valid input device
                        for i in range(self._pyaudio.get_device_count()):
                            info = self._pyaudio.get_device_info_by_index(i)
                            if info["maxInputChannels"] > 0:
                                device_idx = i
                                break

                if device_idx is None:
                    logger.error("No microphone input device found")
                    break

                device_info = self._pyaudio.get_device_info_by_index(device_idx)
                logger.info(f"Using audio device: {device_info['name']}")

                # Open stream
                self._stream = self._pyaudio.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=device_idx,
                    frames_per_buffer=self.frame_size,
                    stream_callback=self._py_callback,
                )

                self._stream.start_stream()
                retries = 0  # Reset retry count on success

                # Keep the thread alive while running
                while self._running and self._stream.is_active():
                    time.sleep(0.1)

            except Exception as exc:
                retries += 1
                logger.warning(f"Audio stream error (attempt {retries}/{max_retries}): {exc}")
                if retries < max_retries:
                    time.sleep(2 * retries)  # Exponential backoff
            finally:
                self._cleanup()

    def _py_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback — called from audio thread."""
        if self._running and self._callback:
            try:
                # Convert PCM16 bytes to float32 numpy array
                audio = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
                self._callback(audio)
            except Exception as exc:
                logger.debug(f"Audio callback error: {exc}")
        return (None, 0)

    def _cleanup(self):
        """Release PyAudio resources."""
        try:
            if self._stream:
                if self._stream.is_active():
                    self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._pyaudio:
                self._pyaudio.terminate()
        except Exception:
            pass
        self._stream = None
        self._pyaudio = None

    def stop(self):
        """Stop listening and release microphone."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._cleanup()

    def read_blocking(self, duration_seconds: float = 3.0) -> np.ndarray:
        """Read audio for a fixed duration (blocking).
        
        Used for push-to-talk mode where we record until
        the user releases the key.
        
        Returns:
            Float32 numpy array, shape (samples,)
        """
        frames: list[np.ndarray] = []
        event = threading.Event()

        def collect(frame: np.ndarray):
            frames.append(frame)
            if len(frames) * self.frame_size / self.sample_rate >= duration_seconds:
                event.set()

        self.start(collect)
        event.wait(timeout=duration_seconds + 1)
        self.stop()

        if frames:
            return np.concatenate(frames)
        return np.array([], dtype=np.float32)
