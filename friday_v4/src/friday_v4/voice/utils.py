"""Pure-Python WAV I/O utilities — no `soundfile` dependency.

Used by STT (temp WAV for file-based providers) and TTS (synthesis output).
Part of the Voice Wave 2.0 rebuild (zero-torch, minimal deps).
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Tuple

import numpy as np

logger = logging.getLogger("friday_v4.voice.utils")


def write_wav(path: str, audio: np.ndarray, sample_rate: int) -> None:
    """Write a float32 numpy array as a 16-bit PCM mono WAV file.

    Args:
        path: Output file path
        audio: Float32 numpy array, range [-1, 1]
        sample_rate: Sample rate in Hz
    """
    audio = np.clip(audio, -1.0, 1.0)
    int16 = (audio * 32767).astype(np.int16)
    num_samples = len(int16)
    byte_rate = sample_rate * 2  # 16-bit mono
    data_size = num_samples * 2

    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(int16.tobytes())


def read_wav(path: str) -> Tuple[np.ndarray, int]:
    """Read a WAV file and return (float32_array, sample_rate).

    Supports 8/16/32-bit (int and float) mono/stereo WAVs, mixes
    multi-channel to mono, and normalizes to [-1, 1].
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RIFF":
        raise ValueError("Not a WAV file (no RIFF header)")

    # Find fmt chunk
    fmt_start = 12  # After RIFF header + file size + WAVE ID
    while fmt_start < len(data) - 8:
        chunk_id = data[fmt_start:fmt_start + 4]
        chunk_size = struct.unpack("<I", data[fmt_start + 4:fmt_start + 8])[0]
        if chunk_id == b"fmt ":
            break
        fmt_start += 8 + chunk_size

    audio_format = struct.unpack("<H", data[fmt_start + 8:fmt_start + 10])[0]
    num_channels = struct.unpack("<H", data[fmt_start + 10:fmt_start + 12])[0]
    sample_rate = struct.unpack("<I", data[fmt_start + 12:fmt_start + 16])[0]
    bits_per_sample = struct.unpack("<H", data[fmt_start + 22:fmt_start + 24])[0]

    # Find data chunk
    data_start = fmt_start + 8 + chunk_size
    while data_start < len(data) - 8:
        chunk_id = data[data_start:data_start + 4]
        chunk_size = struct.unpack("<I", data[data_start + 4:data_start + 8])[0]
        if chunk_id == b"data":
            break
        data_start += 8 + chunk_size

    raw_data = data[data_start + 8:data_start + 8 + chunk_size]

    if bits_per_sample == 16:
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    elif bits_per_sample == 32 and audio_format == 3:  # IEEE float
        samples = np.frombuffer(raw_data, dtype=np.float32)
    elif bits_per_sample == 8:
        samples = (np.frombuffer(raw_data, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"Unsupported bits per sample: {bits_per_sample}")

    # Mix to mono
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels).mean(axis=1)

    return samples, sample_rate
