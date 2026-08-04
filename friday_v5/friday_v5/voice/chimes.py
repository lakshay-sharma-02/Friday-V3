"""Signature audio cues — the sound *around* FRIDAY's words.

Mirrors VOICE_EXPERIENCE.md. All cues are generated in pure Python
(sine synthesis → WAV bytes); no asset files needed.

  listen → subtle double-chime  (Iron Man HUD activating)
  done   → single acknowledgment chime
  alert  → sharp staccato double-burst
  error  → brief descending tone
  think  → quiet ambient hum (barely audible)
"""

from __future__ import annotations

import logging
import math
import struct
import threading
from typing import Sequence

logger = logging.getLogger("friday_v5.voice.chimes")

_SAMPLE_RATE = 22050


# ---------------------------------------------------------------------------
# WAV construction helpers
# ---------------------------------------------------------------------------


def _pcm16_wav(samples: Sequence[float], sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Build a 16-bit mono WAV byte stream from integer samples."""
    num_samples = len(samples)
    data_size = num_samples * 2
    out = bytearray()
    out += b"RIFF"
    out += struct.pack("<I", 36 + data_size)
    out += b"WAVE"
    out += b"fmt " + struct.pack("<I", 16) + struct.pack("<H", 1)
    out += struct.pack("<H", 1) + struct.pack("<I", sample_rate)
    out += struct.pack("<I", sample_rate * 2) + struct.pack("<H", 2)
    out += struct.pack("<H", 16) + b"data"
    out += struct.pack("<I", data_size)
    for s in samples:
        out += struct.pack("<h", max(-32768, min(32767, int(s))))
    return bytes(out)


def _tone(freq: float, duration_s: float, volume: float = 0.5,
          decay: float = 8.0) -> list[float]:
    """Sine wave with exponential decay envelope."""
    n = int(_SAMPLE_RATE * duration_s)
    return [
        32767 * volume * math.sin(2 * math.pi * freq * (i / _SAMPLE_RATE))
        * math.exp(-(i / _SAMPLE_RATE) * decay)
        for i in range(n)
    ]


def _silence(duration_s: float) -> list[int]:
    return [0] * int(_SAMPLE_RATE * duration_s)


def _descend(start_freq: float, end_freq: float, duration_s: float,
             volume: float = 0.5) -> list[float]:
    """A pitch-glide from start_freq down to end_freq."""
    n = int(_SAMPLE_RATE * duration_s)
    return [
        32767 * volume
        * math.sin(2 * math.pi
                   * max(start_freq - (start_freq - end_freq) * (i / n),
                         end_freq)
                   * (i / _SAMPLE_RATE))
        * math.exp(-(i / _SAMPLE_RATE) * 5)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Cue definitions
# ---------------------------------------------------------------------------


def get_chime(chime_type: str = "listen") -> bytes:
    """Get a signature audio cue as WAV bytes."""
    if chime_type == "listen":
        # C6 (1047 Hz) then E6 (1319 Hz), 120 ms each, 50 ms gap
        samples = _tone(1047, 0.12, 0.5) + _silence(0.05) + _tone(1319, 0.15, 0.5)
    elif chime_type == "done":
        # A5 (880 Hz), 200 ms, gentle
        samples = _tone(880, 0.2, 0.5, decay=6)
    elif chime_type == "alert":
        # Two sharp C7 (2093 Hz) staccato bursts
        samples = (_tone(2093, 0.08, 0.7, decay=20) + _silence(0.06)
                   + _tone(2093, 0.12, 0.7, decay=15))
    elif chime_type == "error":
        # Descending E5 (659 Hz) → C5 (523 Hz) over 400 ms
        samples = _descend(659, 523, 0.4, 0.5)
    elif chime_type == "think":
        # Very quiet A3 (220 Hz) hum, 20% volume
        samples = _tone(220, 0.3, 0.2, decay=4)
    else:
        samples = _tone(440, 0.1, 0.5)

    return _pcm16_wav(samples)


def play_chime(chime_type: str = "listen") -> None:
    """Play a signature cue through speakers. Non-blocking.

    Launches the playback in a subprocess so the caller never blocks
    and the cue can't crash the pipeline.
    """
    wav_data = get_chime(chime_type)
    try:
        from .audio import play_wav_bytes
        threading.Thread(
            target=play_wav_bytes, args=(wav_data,), daemon=True
        ).start()
    except Exception as exc:
        logger.warning(f"Chime playback failed: {exc}")



