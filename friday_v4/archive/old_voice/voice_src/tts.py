"""Text-to-Speech engine for Friday V4.

Three-tier TTS with automatic fallback:
  1. Kokoro (primary) — fastest CPU TTS, excellent quality, 54 voices
  2. XTTS-v2 (premium) — voice-cloned FRIDAY voice (Kerry Condon)
  3. edge-tts (fallback) — Microsoft neural voices (requires internet)
  4. pyttsx3 (emergency) — system TTS, always available

Design:
  - Thread-safe: speak() runs in background thread, doesn't block
  - Interruptible: stop() cancels current speech immediately (<50ms)
  - Voice caching: frequent phrases rendered ahead of time
  - Voice modes: conversation, briefing, alert, whisper, off
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.voice.tts")


# ---------------------------------------------------------------------------
# Voice Mode — adapts tone to context
# ---------------------------------------------------------------------------


class VoiceMode(Enum):
    CONVERSATION = "conversation"  # Warm, natural, normal pace
    BRIEFING = "briefing"          # Professional, clear, steady
    ALERT = "alert"                # Urgent, direct, faster
    WHISPER = "whisper"            # Soft, quiet, slow
    OFF = "off"                    # Silent


def auto_voice_mode(text: str) -> VoiceMode:
    """Select VoiceMode based on response content and context.
    
    Matches VOICE_EXPERIENCE.md spec:
      - ALERT:   urgent/vulnerability/critical keywords
      - BRIEFING: status reports, longer factual responses (>200 chars)
      - WHISPER:  late-night hours (23:00-07:00)
      - CONVERSATION: everything else (default)
    """
    import datetime
    lower = text.lower()
    # Alert keywords
    urgent_words = [
        "vulnerability", "critical", "urgent", "warning", "security",
        "breach", "exploit", "cve", "attack", "threat", "emergency",
        "incident", "outage", "downtime", "crash", "failing", "error",
    ]
    for w in urgent_words:
        if w in lower:
            return VoiceMode.ALERT
    if "high severity" in lower or "blocking" in lower or "failure" in lower:
        return VoiceMode.ALERT

    # Briefing mode for long factual responses
    if len(text) > 200 or lower.startswith("here") or lower.startswith("i found"):
        return VoiceMode.BRIEFING

    # Whisper mode late night
    hour = datetime.datetime.now().hour
    if hour < 7 or hour >= 23:
        return VoiceMode.WHISPER

    return VoiceMode.CONVERSATION


# ---------------------------------------------------------------------------
# Audio output helpers
# ---------------------------------------------------------------------------


def _play_wav(path: str) -> None:
    """Play a WAV file using the best available audio player.
    
    Tries: paplay (PulseAudio), aplay (ALSA), afplay (macOS),
           then ffplay (ffmpeg).
    """
    system = platform.system()
    try:
        if system == "Linux":
            # Try PulseAudio first, then ALSA
            if Path("/usr/bin/paplay").exists():
                subprocess.run(
                    ["paplay", path],
                    check=False, capture_output=True, timeout=60,
                )
                return
            if Path("/usr/bin/aplay").exists():
                subprocess.run(
                    ["aplay", path],
                    check=False, capture_output=True, timeout=60,
                )
                return
        elif system == "Darwin":
            subprocess.run(
                ["afplay", path],
                check=False, capture_output=True, timeout=60,
            )
            return
        elif system == "Windows":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return

        # Universal fallback: ffplay
        if Path("/usr/bin/ffplay").exists():
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", path],
                check=False, capture_output=True, timeout=60,
            )
            return
        if Path("/usr/bin/ffmpeg").exists():
            subprocess.run(
                ["ffmpeg", "-i", path, "-f", "alsa", "default"],
                check=False, capture_output=True, timeout=60,
            )
            return
    except Exception as exc:
        logger.warning(f"Audio playback failed: {exc}")


def _list_audio_devices() -> list[dict]:
    """List available audio output devices."""
    devices = []
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                devices.append({
                    "index": i,
                    "name": info["name"],
                    "channels": info["maxOutputChannels"],
                    "sample_rate": int(info["defaultSampleRate"]),
                })
        p.terminate()
    except ImportError:
        # Fall back to system tools
        try:
            result = subprocess.run(
                ["pactl", "list", "sinks", "--format=json"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                sinks = json.loads(result.stdout)
                for sink in sinks:
                    devices.append({
                        "index": sink.get("index", 0),
                        "name": sink.get("name", "unknown"),
                        "description": sink.get("description", ""),
                    })
        except Exception:
            pass
    return devices


# ---------------------------------------------------------------------------
# Voice Provider — abstract base for TTS backends
# ---------------------------------------------------------------------------


class VoiceProvider:
    """Abstract base for a TTS backend.
    
    Each backend implements synthesize() and reports latency/quality metadata.
    """

    name: str = "base"
    latency_ms: int = 0  # Average synthesis latency
    quality: str = "medium"  # low, medium, high, premium
    requires_internet: bool = False
    is_available: bool = False

    def synthesize(self, text: str, output_path: str,
                   voice: str = "default", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        """Synthesize text to a WAV file. Returns True on success."""
        raise NotImplementedError

    def get_voices(self) -> list[str]:
        """List available voice names for this provider."""
        return []


# ---------------------------------------------------------------------------
# Kokoro Provider — primary TTS (fast CPU, excellent quality)
# ---------------------------------------------------------------------------


class KokoroProvider(VoiceProvider):
    """Kokoro TTS — fastest CPU-optimized model with 54 voices.
    
    - ~30x real-time on CPU
    - 54 built-in voices across 8 languages
    - Apache 2.0 license
    - No voice cloning (use XTTS for that)
    
    First load downloads models from HuggingFace (~12s).
    Loads async so TTS init doesn't block.
    """

    name = "kokoro"
    quality = "high"
    requires_internet = False
    is_available = False

    # Kokoro voice presets
    VOICE_MAP = {
        VoiceMode.CONVERSATION: "af_bella",    # Warm, friendly
        VoiceMode.BRIEFING: "am_michael",      # Professional male
        VoiceMode.ALERT: "am_adam",            # Urgent male
        VoiceMode.WHISPER: "af_heart",         # Soft, gentle
    }

    _pipeline = None
    _load_thread: Optional[threading.Thread] = None

    def __init__(self):
        self.latency_ms = 200  # ~200ms for typical sentence on CPU
        self._load_lock = threading.Lock()
        self._pipeline = None
        self.is_available = False
        # Check if kokoro is installed WITHOUT importing it (import pulls
        # in transformers+torch which takes 14s on CPU).
        import importlib.util
        if importlib.util.find_spec("kokoro") is not None:
            self._start_load()
        else:
            logger.debug("kokoro not installed — skipping async load")
            self.is_available = False

    def _start_load(self) -> None:
        """Load the Kokoro model ONCE in a background thread.

        First load downloads model files from HuggingFace (~12s) plus
        torch/transformers import (~30s on CPU). The provider becomes
        available when the thread completes. A second concurrent load
        is deliberately impossible: synthesize() waits on this thread
        instead of spawning its own.
        """
        def _load():
            try:
                from kokoro import KPipeline
                with self._load_lock:
                    self._pipeline = KPipeline(lang_code='a')
                    self.is_available = True
                logger.info("Kokoro TTS loaded (async complete)")
            except Exception as exc:
                logger.warning(f"Kokoro TTS load failed: {exc}")

        self._load_thread = threading.Thread(target=_load, daemon=True)
        self._load_thread.start()

    def _wait_loaded(self, timeout: float = 180.0) -> bool:
        """Block until the background model load finishes (or times out).

        Never takes _load_lock while joining — the load thread needs that
        lock to publish the pipeline, so holding it would deadlock.
        """
        if self.is_available and self._pipeline is not None:
            return True
        if self._load_thread is not None:
            self._load_thread.join(timeout=timeout)
        return self.is_available and self._pipeline is not None

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        # Single in-flight load: wait for the background thread instead
        # of spawning a second KPipeline (torch init is not concurrent-safe
        # and doubles startup on CPU).
        if not self._wait_loaded():
            return False

        voice = voice or self.VOICE_MAP.get(mode, "af_bella")
        try:
            # Single in-flight load: wait for the background thread instead
            # of spawning a second KPipeline (torch init is not concurrent-safe
            # and doubles startup on CPU).
            if not self._wait_loaded():
                return False

            # Kokoro generates audio tensors via its generator.
            # `result.audio` is a torch.Tensor on disk CPU; detach→numpy
            # so write_wav (pure-Python, numpy-only) works without torch
            # leaking into the audio path.
            generator = self._pipeline(text, voice=voice, speed=1.0)
            import numpy as np
            all_audio = []
            sr = 24000
            for result in generator:
                audio = result.audio
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                all_audio.append(audio)

            if not all_audio:
                return False

            full_audio = np.concatenate(all_audio) if len(all_audio) > 1 else all_audio[0]
            # Use pure-Python WAV writer (no soundfile dependency)
            from .utils import write_wav
            write_wav(output_path, full_audio, sr)
            logger.debug(f"Kokoro synthesized {len(text)} chars to {output_path}")
            return True

        except Exception as exc:
            logger.warning(f"Kokoro synthesis failed: {exc}")
            return False

    def get_voices(self) -> list[str]:
        return list(self.VOICE_MAP.values())


# ---------------------------------------------------------------------------
# XTTS Provider — premium voice-cloned FRIDAY voice
# ---------------------------------------------------------------------------


class XTTSProvider(VoiceProvider):
    """XTTS-v2 with voice-cloned FRIDAY voice.
    
    - Requires ~4-6GB RAM, slower on CPU (~5-10s per sentence)
    - Voice cloning from 10-30s audio sample
    - The authentic FRIDAY experience
    - Falls back to Kokoro for speed
    
    Voice model location: ~/.friday/voices/friday/
    """

    name = "xtts"
    quality = "premium"
    requires_internet = False
    is_available = False

    _tts = None
    _voice_path: Optional[str] = None

    # Default voice paths to check
    VOICE_DIRS = [
        Path.home() / ".friday" / "voices",
        Path(__file__).parent.parent.parent.parent / "voices",
    ]

    def __init__(self):
        self.latency_ms = 5000  # Slower on CPU
        self._find_voice()
        self._try_load()

    def _find_voice(self) -> Optional[str]:
        """Find the FRIDAY voice clone in known locations."""
        for voice_dir in self.VOICE_DIRS:
            if not voice_dir.exists():
                continue
            # Look for .wav files with "friday" in the name
            for f in voice_dir.glob("*friday*"):
                if f.suffix in (".wav", ".mp3", ".flac"):
                    self._voice_path = str(f)
                    return self._voice_path
            # Also check the default "friday.wav"
            friday_wav = voice_dir / "friday.wav"
            if friday_wav.exists():
                self._voice_path = str(friday_wav)
                return self._voice_path
        return None

    def _try_load(self) -> bool:
        """Try to load XTTS-v2 with the FRIDAY voice."""
        try:
            from TTS.api import TTS
            self._tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
            self.is_available = True
            logger.info("XTTS-v2 loaded (FRIDAY voice ready)")
            return True
        except ImportError:
            logger.warning("XTTS-v2 not installed. Install: pip install coqui-tts")
            self.is_available = False
            return False
        except Exception as exc:
            logger.warning(f"XTTS-v2 load failed: {exc}")
            self.is_available = False
            return False

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self.is_available or self._tts is None:
            return False

        speaker = self._voice_path or voice or "default"
        try:
            self._tts.tts_to_file(
                text=text,
                speaker_wav=speaker,
                language="en",
                file_path=output_path,
            )
            logger.debug(f"XTTS synthesized {len(text)} chars with FRIDAY voice")
            return True
        except Exception as exc:
            logger.warning(f"XTTS synthesis failed: {exc}")
            return False

    def set_voice_clone(self, wav_path: str) -> bool:
        """Set a FRIDAY voice clone WAV file."""
        p = Path(wav_path)
        if p.exists() and p.suffix in (".wav", ".mp3", ".flac"):
            self._voice_path = str(p)
            return True
        return False


# ---------------------------------------------------------------------------
# Edge-TTS Provider — internet fallback (Microsoft neural voices)
# ---------------------------------------------------------------------------


class EdgeTTSProvider(VoiceProvider):
    """Microsoft Edge TTS — excellent quality, requires internet.
    
    Fallback when no local model is available. Sounds nearly human.
    """

    name = "edge-tts"
    quality = "high"
    requires_internet = True
    is_available = False

    VOICE_MAP = {
        VoiceMode.CONVERSATION: "en-IE-EmilyNeural",  # Irish female — closest to FRIDAY's voice!
        VoiceMode.BRIEFING: "en-IE-EmilyNeural",       # Irish female, professional
        VoiceMode.ALERT: "en-GB-SoniaNeural",          # British female, clear urgency
        VoiceMode.WHISPER: "en-GB-MaisieNeural",       # Soft British female
    }

    def __init__(self):
        self._try_check()
        self.latency_ms = 1500  # Network-dependent

    def _try_check(self) -> bool:
        """Check if edge-tts is available."""
        try:
            import edge_tts
            self.is_available = True
            logger.info("Edge TTS available")
            return True
        except ImportError:
            self.is_available = False
            return False

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self.is_available:
            return False

        voice_name = voice or self.VOICE_MAP.get(mode, "en-GB-SoniaNeural")
        try:
            import edge_tts
            import asyncio

            async def _synth():
                communicate = edge_tts.Communicate(text, voice_name)
                await communicate.save(output_path)

            asyncio.run(_synth())
            logger.debug(f"Edge TTS synthesized {len(text)} chars")
            return True
        except Exception as exc:
            logger.warning(f"Edge TTS failed: {exc}")
            return False

    def get_voices(self) -> list[str]:
        return list(self.VOICE_MAP.values())


# ---------------------------------------------------------------------------
# PyTTS Provider — emergency fallback (always available, basic quality)
# ---------------------------------------------------------------------------


class PyTTSProvider(VoiceProvider):
    """System TTS — always available, robotic quality.
    
    Last-resort fallback when no other provider works.
    """

    name = "pyttsx3"
    quality = "low"
    requires_internet = False
    is_available = False
    _engine = None

    def __init__(self):
        self._try_load()
        self.latency_ms = 100

    def _try_load(self) -> bool:
        try:
            import pyttsx3
            import os
            try:
                import espeakng_loader
                os.environ["ESPEAK_DATA_PATH"] = espeakng_loader.get_data_path()
            except ImportError:
                pass
            self._engine = pyttsx3.init()
            self.is_available = True
            # Set reasonable defaults
            self._engine.setProperty("rate", 180)
            self._engine.setProperty("volume", 0.9)
            return True
        except Exception:
            self.is_available = False
            return False

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self.is_available:
            return False
        # pyttsx3 speaks directly — no file output
        # We save to file using a trick: redirect to audio file
        # This is imperfect but functional as a last resort
        try:
            self._engine.save_to_file(text, output_path)
            self._engine.runAndWait()
            return Path(output_path).exists()
        except Exception:
            # Final fallback: speak directly
            try:
                self._engine.say(text)
                self._engine.runAndWait()
                return True
            except Exception:
                return False

    def speak_direct(self, text: str) -> bool:
        """Speak directly without saving to file (for pyttsx3 only).
        
        This is the PRIMARY path for pyttsx3 since its file-based
        synthesis is unreliable. TextToSpeech._speak_sync uses this
        when the active provider is PyTTSProvider.
        """
        if not self.is_available or self._engine is None:
            return False
        try:
            self._engine.say(text)
            self._engine.runAndWait()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Audio Cue Provider — signature sounds (chimes, beeps)
# ---------------------------------------------------------------------------

# Base64-encoded minimal WAV files for signature sounds.
# These are tiny (1KB each) and embedded so no external files needed.

_SILENCE_WAV = None  # Will be generated


def _generate_chime(frequency: float = 880, duration_s: float = 0.15,
                     sample_rate: int = 22050, volume: float = 0.6) -> bytes:
    """Generate a simple sine-wave chime as a WAV byte array.

    Used for signature audio cues: listening chime, processing chime, etc.
    Pure Python, no external dependencies.
    """
    import struct
    import math

    num_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        # Sine wave with exponential decay envelope
        envelope = math.exp(-t * 8)
        value = int(32767 * volume * math.sin(2 * math.pi * frequency * t) * envelope)
        samples.append(value)

    # Build WAV
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_samples * num_channels * bits_per_sample // 8

    from .utils import write_wav
    wav_bytes = bytearray()
    wav_bytes += b"RIFF"
    wav_bytes += struct.pack("<I", 36 + data_size)
    wav_bytes += b"WAVE"
    wav_bytes += b"fmt "
    wav_bytes += struct.pack("<I", 16)
    wav_bytes += struct.pack("<H", 1)
    wav_bytes += struct.pack("<H", num_channels)
    wav_bytes += struct.pack("<I", sample_rate)
    wav_bytes += struct.pack("<I", byte_rate)
    wav_bytes += struct.pack("<H", block_align)
    wav_bytes += struct.pack("<H", bits_per_sample)
    wav_bytes += b"data"
    wav_bytes += struct.pack("<I", data_size)
    for sample in samples:
        wav_bytes += struct.pack("<h", sample)

    return bytes(wav_bytes)


def _generate_silence(duration_ms: float = 50, sample_rate: int = 22050) -> bytes:
    """Generate silence as WAV bytes for gap padding."""
    import struct
    num_samples = int(sample_rate * duration_ms / 1000)
    data_size = num_samples * 2
    wav = bytearray()
    wav += b"RIFF"
    wav += struct.pack("<I", 36 + data_size)
    wav += b"WAVE"
    wav += b"fmt " + struct.pack("<I", 16) + struct.pack("<H", 1)
    wav += struct.pack("<H", 1) + struct.pack("<I", sample_rate)
    wav += struct.pack("<I", sample_rate * 2) + struct.pack("<H", 2)
    wav += struct.pack("<H", 16) + b"data"
    wav += struct.pack("<I", data_size)
    wav += b"\x00" * data_size
    return bytes(wav)


# Cache generated chimes
_CHIME_CACHE: dict[str, bytes] = {}


def get_chime(chime_type: str = "listen") -> bytes:
    """Get a signature audio cue as WAV bytes.
    
    MCU FRIDAY-style audio signatures (VOICE_EXPERIENCE.md):
      - "listen":  Subtle double-chime — like Iron Man HUD activating
                   Two quick tones: C6 (1047Hz) then E6 (1319Hz), 120ms each
      - "done":    Single acknowledgment chime — confirmation without words
                   A5 (880Hz), 200ms
      - "alert":   Sharp urgent chime — gets your attention
                   Two louder C7 (2093Hz) staccato bursts
      - "error":   Brief descending tone — something went wrong
                   E5 (659Hz) → C5 (523Hz) over 400ms
      - "think":   Subtle ambient processing hum (very quiet)
                   A3 (220Hz), 300ms at 20% volume
    """
    global _CHIME_CACHE

    if chime_type in _CHIME_CACHE:
        return _CHIME_CACHE[chime_type]

    if chime_type == "listen":
        # Double chime: C6 (1047Hz) then E6 (1319Hz) — Iron Man HUD style
        # Each tone is 120ms with a 50ms gap
        c1 = _generate_chime(1047, 0.12, volume=0.5)
        gap = _generate_silence(50)
        c2 = _generate_chime(1319, 0.15, volume=0.5)
        result = _concatenate_wav(c1, gap, c2)
    elif chime_type == "done":
        # Single mid chime: A5 (880Hz), 200ms, gentle
        result = _generate_chime(880, 0.2, volume=0.5)
    elif chime_type == "alert":
        # Two sharp C7 (2093Hz) staccato bursts, higher volume
        c1 = _generate_chime(2093, 0.08, volume=0.7)
        gap = _generate_silence(60)
        c2 = _generate_chime(2093, 0.12, volume=0.7)
        result = _concatenate_wav(c1, gap, c2)
    elif chime_type == "error":
        # Descending: E5 (659Hz) → C5 (523Hz) over 400ms
        import math, struct
        sample_rate = 22050
        num_samples = int(sample_rate * 0.4)
        samples = []
        for i in range(num_samples):
            t = i / sample_rate
            freq = 659 - (136 * t / 0.4)  # Descend from 659 to 523
            if freq < 523:
                freq = 523
            envelope = math.exp(-t * 5)
            value = int(32767 * 0.5 * math.sin(2 * math.pi * freq * t) * envelope)
            samples.append(value)
        data_size = num_samples * 2
        result = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
        result += b"fmt " + struct.pack("<I", 16) + struct.pack("<H", 1)
        result += struct.pack("<H", 1) + struct.pack("<I", sample_rate)
        result += struct.pack("<I", sample_rate * 2) + struct.pack("<H", 2)
        result += struct.pack("<H", 16) + b"data"
        result += struct.pack("<I", data_size)
        for s in samples:
            result += struct.pack("<h", s)
    elif chime_type == "think":
        # Very quiet A3 (220Hz) hum, 300ms, 20% volume
        result = _generate_chime(220, 0.3, volume=0.2)
    else:
        result = _generate_chime(440, 0.1)

    _CHIME_CACHE[chime_type] = result
    return result


def _concatenate_wav(*wavs: bytes) -> bytes:
    """Concatenate multiple WAV byte streams into one valid WAV.
    
    Extracts raw PCM data from each WAV, concatenates, and builds
    a new WAV header with the combined data length.
    """
    import struct
    # Find data payloads (skip 44-byte headers)
    combined_data = bytearray()
    sample_rate = 22050
    for w in wavs:
        # Find data chunk size
        data_size = struct.unpack("<I", w[40:44])[0]
        data_start = 44  # Standard WAV header is 44 bytes
        combined_data.extend(w[data_start:data_start + data_size])

    # Build new header
    total_data_size = len(combined_data)
    header = bytearray()
    header += b"RIFF"
    header += struct.pack("<I", 36 + total_data_size)
    header += b"WAVE"
    header += b"fmt " + struct.pack("<I", 16) + struct.pack("<H", 1)
    header += struct.pack("<H", 1) + struct.pack("<I", sample_rate)
    header += struct.pack("<I", sample_rate * 2) + struct.pack("<H", 2)
    header += struct.pack("<H", 16) + b"data"
    header += struct.pack("<I", total_data_size)
    return bytes(header) + bytes(combined_data)


def play_chime(chime_type: str = "listen") -> None:
    """Play a signature audio cue through speakers.
    
    Non-blocking. Launches a subprocess to play the sound.
    """
    wav_data = get_chime(chime_type)
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_data)
            tmp_path = f.name
        _play_wav(tmp_path)
        # Clean up after a brief delay (don't delete while playing)
        def _clean():
            time.sleep(1)
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
        threading.Thread(target=_clean, daemon=True).start()
    except Exception as exc:
        logger.warning(f"Chime playback failed: {exc}")


# ---------------------------------------------------------------------------
# TextToSpeech — main TTS frontend
# ---------------------------------------------------------------------------


@dataclass
class TTSConfig:
    """Configuration for the TTS engine."""
    primary_provider: str = "edge"            # kokoro, xtts, edge, pyttsx3
    voice: str = ""                             # Voice name/ID
    mode: VoiceMode = VoiceMode.CONVERSATION    # Current voice mode
    volume: float = 0.9                         # 0.0 to 1.0
    speed: float = 1.0                          # 0.5 to 2.0
    cache_enabled: bool = True
    cache_dir: str = str(Path.home() / ".friday" / "voice_cache")
    max_cache_entries: int = 1000


class TextToSpeech:
    """Friday's voice — speak text aloud through the best available TTS engine.
    
    Auto-selects the best available provider on init. Falls back gracefully
    if the primary provider is unavailable.
    
    Usage:
        tts = TextToSpeech()
        tts.speak("Hello, I'm Friday.")
        tts.speak("Alert: vulnerabilities found.", mode=VoiceMode.ALERT)
        tts.stop()  # Interrupt current speech
    """

    def __init__(self, config: Optional[TTSConfig] = None):
        self.config = config or TTSConfig()
        self._providers: list[VoiceProvider] = []
        self._current_provider: Optional[VoiceProvider] = None
        self._speak_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Initialize providers in priority order
        self._init_providers()

        # Cache directory
        if self.config.cache_enabled:
            Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)

        logger.info(f"TTS initialized. Active provider: {self.active_provider_name}")

    def _init_providers(self) -> None:
        """Initialize TTS providers and select the best available.
        
        If a primary_provider is configured, it is tried first. Once a
        working provider is found, expensive models (Kokoro, XTTS) are
        skipped to save RAM unless they are the selected primary.
        """
        preferred = self.config.primary_provider

        # Provider registry — map name → class
        _REGISTRY: dict[str, type[VoiceProvider]] = {
            "kokoro": KokoroProvider,
            "xtts": XTTSProvider,
            "edge": EdgeTTSProvider,
            "pyttsx3": PyTTSProvider,
        }

        # Order: preferred first, then the rest in priority order
        provider_order = [preferred] if preferred else []
        provider_order += [
            n for n in ("kokoro", "xtts", "edge", "pyttsx3")
            if n != preferred
        ]

        # Heavy models that consume significant RAM
        _HEAVY = {"kokoro", "xtts"}

        for name in provider_order:
            # Skip heavy models if we already have a working provider
            # and the user didn't explicitly request this one
            if name in _HEAVY and self._current_provider is not None \
               and preferred != name:
                logger.debug(
                    f"Skipping {name} (not primary, already have "
                    f"{self._current_provider.name})"
                )
                continue

            cls = _REGISTRY[name]
            try:
                provider = cls()
                # Heavy async-loading providers (Kokoro) report is_available
                # only after a background torch import. When explicitly
                # requested as primary, block once so the user actually
                # gets the voice they asked for (first init ~30-60s).
                if name == preferred and not provider.is_available \
                   and hasattr(provider, "_wait_loaded"):
                    provider._wait_loaded()
                self._providers.append(provider)
                if provider.is_available:
                    self._current_provider = provider
                    logger.info(f"TTS active provider: {provider.name}")
                    if name == preferred:
                        # Primary found — done. Never let a later fallback
                        # (edge/pyttsx3) overwrite the requested provider.
                        break
            except Exception as exc:
                logger.debug(f"Provider {name} init failed: {exc}")

        if self._current_provider:
            logger.info(f"TTS active provider: {self._current_provider.name}")
        else:
            logger.warning("No TTS provider available! Speech disabled.")

    @property
    def active_provider_name(self) -> str:
        return self._current_provider.name if self._current_provider else "none"

    @property
    def is_speaking(self) -> bool:
        return self._speak_thread is not None and self._speak_thread.is_alive()

    @property
    def is_available(self) -> bool:
        return self._current_provider is not None

    def speak(self, text: str, mode: Optional[VoiceMode] = None) -> bool:
        """Speak text aloud. Non-blocking — returns immediately.
        
        If already speaking, the current speech is interrupted.
        """
        if not text or not self._current_provider:
            return False

        # Stop current speech
        self.stop()

        # Apply mode
        effective_mode = mode or self.config.mode

        # Launch in background thread
        self._stop_event.clear()
        self._speak_thread = threading.Thread(
            target=self._speak_sync,
            args=(text, effective_mode),
            daemon=True,
        )
        self._speak_thread.start()
        return True

    def speak_and_wait(self, text: str, mode: Optional[VoiceMode] = None) -> None:
        """Speak text and block until finished."""
        self.speak(text, mode)
        if self._speak_thread:
            self._speak_thread.join(timeout=30)

    def stop(self) -> None:
        """Stop current speech immediately (<50ms response)."""
        self._stop_event.set()
        if self._speak_thread:
            self._speak_thread.join(timeout=2)
            self._speak_thread = None

    def play_chime(self, chime_type: str = "listen") -> None:
        """Play a signature audio cue (non-blocking)."""
        threading.Thread(
            target=play_chime, args=(chime_type,), daemon=True
        ).start()

    def set_mode(self, mode: VoiceMode) -> None:
        """Change voice mode (affects tone, speed, volume)."""
        self.config.mode = mode

    def set_voice(self, voice: str) -> None:
        """Set a specific voice name/ID."""
        self.config.voice = voice

    def _speak_sync(self, text: str, mode: VoiceMode) -> None:
        """Synthesize and play text. Runs in background thread."""
        if self._stop_event.is_set():
            return

        # Check cache for frequent responses
        cache_key = f"{text}_{mode.value}_{self.config.voice}"
        cached_path = self._check_cache(cache_key)

        if cached_path and Path(cached_path).exists():
            _play_wav(cached_path)
            return

        # Special path for pyttsx3: speak_direct is more reliable
        if isinstance(self._current_provider, PyTTSProvider):
            self._current_provider.speak_direct(text)
            return

        # Synthesize to temp file
        output_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                output_path = f.name

            success = self._current_provider.synthesize(
                text=text,
                output_path=output_path,
                voice=self.config.voice,
                mode=mode,
            )

            if success and not self._stop_event.is_set():
                _play_wav(output_path)

                # Cache the result
                if self.config.cache_enabled and success:
                    self._save_cache(cache_key, output_path)
            elif not success:
                # Try fallback providers
                self._fallback_speak(text, mode)

        except Exception as exc:
            logger.error(f"Speech synthesis failed: {exc}")
            self._fallback_speak(text, mode)
        finally:
            # Clean up temp file
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def _fallback_speak(self, text: str, mode: VoiceMode) -> None:
        """Try fallback TTS providers when primary fails."""
        for provider in self._providers:
            if provider is self._current_provider:
                continue
            if provider.is_available and not self._stop_event.is_set():
                logger.info(f"Falling back to {provider.name}")
                # PyTTSProvider has a more reliable speak_direct path
                if isinstance(provider, PyTTSProvider):
                    provider.speak_direct(text)
                    return
                output_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        output_path = f.name
                    if provider.synthesize(text, output_path, mode=mode):
                        _play_wav(output_path)
                    return
                except Exception:
                    continue
                finally:
                    if output_path:
                        try:
                            Path(output_path).unlink(missing_ok=True)
                        except Exception:
                            pass

    @staticmethod
    def _cache_key_hash(key: str) -> str:
        """Deterministic hash for cache keys. Uses MD5, not Python's built-in
        hash() which uses random per-process seeds."""
        return hashlib.md5(key.encode()).hexdigest()

    def _check_cache(self, key: str) -> Optional[str]:
        """Check if a synthesized phrase is cached."""
        if not self.config.cache_enabled:
            return None
        cache_path = Path(self.config.cache_dir) / f"{self._cache_key_hash(key)}.wav"
        return str(cache_path) if cache_path.exists() else None

    def _save_cache(self, key: str, wav_path: str) -> None:
        """Cache a synthesized phrase for future use."""
        try:
            cache_path = Path(self.config.cache_dir) / f"{self._cache_key_hash(key)}.wav"
            # Simple LRU: copy to cache
            import shutil
            shutil.copy2(wav_path, cache_path)
            # Clean old cache entries
            self._trim_cache()
        except Exception:
            pass

    def _trim_cache(self) -> None:
        """Remove oldest cache entries when over limit."""
        cache_dir = Path(self.config.cache_dir)
        if not cache_dir.exists():
            return
        entries = sorted(cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if len(entries) > self.config.max_cache_entries:
            for entry in entries[:len(entries) - self.config.max_cache_entries]:
                try:
                    entry.unlink()
                except Exception:
                    pass

    def list_providers(self) -> list[dict]:
        """List all TTS providers and their status."""
        return [
            {
                "name": p.name,
                "available": p.is_available,
                "quality": p.quality,
                "latency_ms": p.latency_ms,
                "requires_internet": p.requires_internet,
            }
            for p in self._providers
        ]

    def list_devices(self) -> list[dict]:
        """List available audio output devices."""
        return _list_audio_devices()
