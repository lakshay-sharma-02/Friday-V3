"""Pure-Python WAV I/O utilities — no `soundfile` dependency.

Replaces the `soundfile` dependency for reading/writing WAV files.
Used by STT (temp WAV for file-based providers) and TTS (synthesis output).
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("friday_v4.voice.utils")


def write_wav(path: str, audio: np.ndarray, sample_rate: int) -> None:
    """Write a numpy float32 array as a 16-bit PCM WAV file.
    
    Args:
        path: Output file path
        audio: Float32 numpy array, range [-1, 1]
        sample_rate: Sample rate in Hz
    """
    # Clip to prevent int16 overflow
    audio = np.clip(audio, -1.0, 1.0)
    # Convert to int16
    int16 = (audio * 32767).astype(np.int16)
    
    num_samples = len(int16)
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_samples * block_align
    
    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))          # Chunk size (PCM)
        f.write(struct.pack("<H", 1))           # PCM format
        f.write(struct.pack("<H", num_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits_per_sample))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(int16.tobytes())


def read_wav(path: str) -> Tuple[np.ndarray, int]:
    """Read a WAV file and return (float32_array, sample_rate).
    
    Args:
        path: Path to WAV file
    
    Returns:
        (audio: float32 numpy array normalized to [-1, 1], sample_rate: int)
    """
    with open(path, "rb") as f:
        data = f.read()
    
    # Parse WAV header
    # RIFF header
    if data[:4] != b"RIFF":
        raise ValueError("Not a WAV file (no RIFF header)")
    
    # Find fmt chunk
    fmt_start = 12  # After RIFF header + file size + WAVE ID
    while fmt_start < len(data) - 8:
        chunk_id = data[fmt_start:fmt_start+4]
        chunk_size = struct.unpack("<I", data[fmt_start+4:fmt_start+8])[0]
        if chunk_id == b"fmt ":
            break
        fmt_start += 8 + chunk_size
    
    # Read format info
    audio_format = struct.unpack("<H", data[fmt_start+8:fmt_start+10])[0]
    num_channels = struct.unpack("<H", data[fmt_start+10:fmt_start+12])[0]
    sample_rate = struct.unpack("<I", data[fmt_start+12:fmt_start+16])[0]
    bits_per_sample = struct.unpack("<H", data[fmt_start+22:fmt_start+24])[0]
    
    # Find data chunk
    data_start = fmt_start + 8 + chunk_size
    while data_start < len(data) - 8:
        chunk_id = data[data_start:data_start+4]
        chunk_size = struct.unpack("<I", data[data_start+4:data_start+8])[0]
        if chunk_id == b"data":
            break
        data_start += 8 + chunk_size
    
    raw_data = data[data_start+8:data_start+8+chunk_size]
    
    # Convert to float32
    if bits_per_sample == 16:
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    elif bits_per_sample == 32:
        # Could be float or int32
        if audio_format == 3:  # IEEE float
            samples = np.frombuffer(raw_data, dtype=np.float32)
        else:
            samples = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif bits_per_sample == 8:
        samples = (np.frombuffer(raw_data, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"Unsupported bits per sample: {bits_per_sample}")
    
    # Mix multi-channel to mono
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels).mean(axis=1)
    
    return samples, sample_rate
