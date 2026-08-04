"""Text-to-Speech engine for Friday V4 — Voice Wave 2.0.

Three-tier TTS with automatic fallback:
  1. Kokoro-ONNX (primary) — same Kokoro-82M quality, runs on ONNX Runtime
     (no torch!), ~10-30x realtime on CPU, 54 voices, Apache-2.0
  2. edge-tts (secondary) — Microsoft neural voices, internet required.
     en-IE-EmilyNeural = Irish female ≈ FRIDAY (Kerry Condon)
  3. pyttsx3 (emergency) — system TTS, always available

Design:
  - Thread-safe: speak() runs in a background thread, doesn't block
  - Interruptible: stop() cancels current speech (<50 ms)
  - Lazy async loading: models download in background, never block start()
  - Voice caching: frequent phrases rendered ahead of time
  - Voice modes: conversation / briefing / alert / whisper / off
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, Optional

import numpy as np

from .audio import (
    flush_play_queue,
    play_wav_file,
    queue_wav,
    stop_playback,
)
from .utils import write_wav

logger = logging.getLogger("friday_v5.voice.tts")


def _setup_espeak() -> None:
    """Point phonemizer at the espeak-ng data directory.

    kokoro-onnx pipes text through phonemizer, which shells out to
    espeak-ng. Without ESPEAK_DATA_PATH it degrades to a slow fallback
    (~4x synthesis time). Set once, before any provider synthesizes.
    """
    if os.environ.get("ESPEAK_DATA_PATH"):
        return
    try:
        import espeakng_loader
        os.environ["ESPEAK_DATA_PATH"] = espeakng_loader.get_data_path()
    except ImportError:
        pass


# Piper voice — "Jenny" (UK/Irish female, natural, offline, ~63 MB).
# Runs in-process via the `piper` Python API (piper-v1 ONNX format that
# sherpa-onnx's VITS path cannot read). Source: agentvibes/piper-custom-voices
# (Apache-2.0), voice trained by Bryce Beattie on the Dioco dataset.
_PIPER_BASE = Path.home() / ".friday" / "models" / "piper"
_PIPER_MODEL = _PIPER_BASE / "jenny.onnx"
_PIPER_TOKENS = _PIPER_BASE / "jenny.onnx.json"
_PIPER_MODEL_URL = (
    "https://huggingface.co/agentvibes/piper-custom-voices/resolve/main/jenny.onnx"
)
_PIPER_TOKENS_URL = (
    "https://huggingface.co/agentvibes/piper-custom-voices/resolve/main/jenny.onnx.json"
)

# Kokoro-ONNX model files (downloaded once to ~/.friday/models/kokoro/)
# model-files-v1.0 release: kokoro-v1.0.onnx + voices-v1.0.bin. The voices
# file MUST be the numpy .bin (kokoro_onnx does np.load on it), and the
# pip package validates against these exact asset names.
_KOKORO_BASE = Path.home() / ".friday" / "models" / "kokoro"
_KOKORO_MODEL = _KOKORO_BASE / "kokoro-v1.0.onnx"
_KOKORO_VOICES = _KOKORO_BASE / "voices-v1.0.bin"
_KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
_KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)


# ---------------------------------------------------------------------------
# Voice Mode — adapts tone to context
# ---------------------------------------------------------------------------


class VoiceMode(Enum):
    CONVERSATION = "conversation"
    BRIEFING = "briefing"
    ALERT = "alert"
    WHISPER = "whisper"
    OFF = "off"


def auto_voice_mode(text: str) -> VoiceMode:
    """Select VoiceMode based on response content and time of day."""
    lower = text.lower()
    urgent_words = [
        "vulnerability", "critical", "urgent", "warning", "security",
        "breach", "exploit", "cve", "attack", "threat", "emergency",
        "incident", "outage", "downtime", "crash", "failing", "error",
    ]
    if any(w in lower for w in urgent_words):
        return VoiceMode.ALERT
    if "high severity" in lower or "blocking" in lower or "failure" in lower:
        return VoiceMode.ALERT
    if len(text) > 200 or lower.startswith(("here", "i found")):
        return VoiceMode.BRIEFING
    hour = datetime.datetime.now().hour
    if hour < 7 or hour >= 23:
        return VoiceMode.WHISPER
    return VoiceMode.CONVERSATION


# ---------------------------------------------------------------------------
# Provider base
# ---------------------------------------------------------------------------


class VoiceProvider:
    """Abstract base for a TTS backend."""

    name: str = "base"
    quality: str = "medium"
    requires_internet: bool = False
    is_available: bool = False
    latency_ms: int = 0
    #: Speech rate multiplier (1.0 = natural). Higher shortens both
    #: synthesis compute and audio duration.
    speed: float = 1.0

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        raise NotImplementedError

    def get_voices(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Piper provider
# ---------------------------------------------------------------------------


class PiperProvider(VoiceProvider):
    """Piper neural TTS via the in-process `piper` Python API.

    The "Jenny" voice (UK/Irish female) runs in-process — no subprocess,
    no internet. Measured on a 2-core CPU: 0.14 s first audio for short
    replies, ~1.1 s for a sentence. Faster than edge-tts and kokoro,
    natural-sounding, offline, ~63 MB model.

    Model is auto-downloaded to ~/.friday/models/piper/ on first use
    (atomic, retried, never corrupt).
    """

    name = "piper"
    quality = "high"
    requires_internet = False
    latency_ms = 100

    VOICE_MAP: ClassVar[dict[VoiceMode, str]] = {
        VoiceMode.CONVERSATION: "jenny",
        VoiceMode.BRIEFING: "jenny",
        VoiceMode.ALERT: "jenny",
        VoiceMode.WHISPER: "jenny",
    }

    _voice = None
    _load_thread: Optional[threading.Thread] = None

    def __init__(self):
        self.is_available = False
        try:
            import importlib.util
            if importlib.util.find_spec("piper") is None:
                logger.debug("piper not installed — skipping load")
                return
            self._start_load()
        except Exception as exc:
            logger.debug(f"Piper init failed: {exc}")

    def _start_load(self) -> None:
        def _load():
            try:
                _PIPER_BASE.mkdir(parents=True, exist_ok=True)
                if not _PIPER_MODEL.exists():
                    self._download(_PIPER_MODEL_URL, _PIPER_MODEL)
                if not _PIPER_TOKENS.exists():
                    self._download(_PIPER_TOKENS_URL, _PIPER_TOKENS)
                from piper import PiperVoice
                self._voice = PiperVoice.load(str(_PIPER_MODEL))
                self.is_available = True
                logger.info("Piper voice loaded (async complete)")
            except Exception as exc:
                logger.warning(f"Piper load failed: {exc}")

        self._load_thread = threading.Thread(target=_load, daemon=True)
        self._load_thread.start()

    @staticmethod
    def _download(url: str, path: Path) -> None:
        """Download with retries (3 attempts, exponential backoff), atomic."""
        import time as _time
        import urllib.error
        import urllib.request
        last_exc = None
        tmp = path.with_suffix(path.suffix + ".part")
        for attempt in range(3):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(url, tmp)
                if tmp.exists() and tmp.stat().st_size < 64 * 1024:
                    raise RuntimeError(
                        f"Suspiciously small download ({tmp.stat().st_size} bytes)")
                tmp.replace(path)
                return
            except (urllib.error.URLError, urllib.error.HTTPError,
                    OSError, TimeoutError, RuntimeError) as exc:
                last_exc = exc
                tmp.unlink(missing_ok=True)
                _time.sleep(2 ** attempt)
        raise last_exc if last_exc else RuntimeError(f"Failed to download {url}")

    def _wait_loaded(self, timeout: float = 600.0) -> bool:
        if self.is_available and self._voice is not None:
            return True
        if self._load_thread is not None:
            self._load_thread.join(timeout=timeout)
        return self.is_available and self._voice is not None

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self._wait_loaded():
            return False
        piper = self._voice
        if piper is None:
            return False
        try:
            import numpy as np
            audio = piper.synthesize(text)
            chunks = [np.frombuffer(c.audio_int16_bytes, dtype=np.int16)
                      for c in audio]
            if not chunks:
                return False
            samples = np.concatenate(chunks)
            write_wav(output_path, samples.astype(np.float32) / 32768.0,
                      int(piper.config.sample_rate))
            return True
        except Exception as exc:
            logger.warning(f"Piper synthesis failed: {exc}")
            return False

    def get_voices(self) -> list[str]:
        return ["jenny"]


# ---------------------------------------------------------------------------
# Kokoro-ONNX provider
# ---------------------------------------------------------------------------


class KokoroONNXProvider(VoiceProvider):
    """Kokoro-82M via ONNX Runtime — the same quality as the old torch build,
    at a fraction of the cost. No torch import, ~10-30x realtime on CPU."""

    name = "kokoro"
    quality = "high"
    requires_internet = False
    latency_ms = 200

    VOICE_MAP: ClassVar[dict[VoiceMode, str]] = {
        VoiceMode.CONVERSATION: "af_bella",   # Warm, friendly
        VoiceMode.BRIEFING: "am_michael",     # Professional male
        VoiceMode.ALERT: "am_adam",           # Urgent male
        VoiceMode.WHISPER: "af_heart",        # Soft, gentle
    }

    _kokoro = None
    _load_thread: Optional[threading.Thread] = None

    def __init__(self):
        self._load_lock = threading.Lock()
        self.is_available = False
        try:
            import importlib.util
            if importlib.util.find_spec("kokoro_onnx") is None:
                logger.debug("kokoro-onnx not installed — skipping load")
                return
            if not _KOKORO_MODEL.exists() or not _KOKORO_VOICES.exists():
                logger.info("Kokoro model files not cached — downloading")
            self._start_load()
        except Exception as exc:
            logger.debug(f"Kokoro init failed: {exc}")

    def _start_load(self) -> None:
        def _load():
            try:
                _KOKORO_BASE.mkdir(parents=True, exist_ok=True)
                if not _KOKORO_MODEL.exists():
                    self._download(_KOKORO_MODEL_URL, _KOKORO_MODEL)
                if not _KOKORO_VOICES.exists():
                    self._download(_KOKORO_VOICES_URL, _KOKORO_VOICES)
                from kokoro_onnx import Kokoro
                with self._load_lock:
                    self._kokoro = Kokoro(str(_KOKORO_MODEL),
                                          str(_KOKORO_VOICES))
                    self.is_available = True
                logger.info("Kokoro-ONNX loaded (async complete)")
            except Exception as exc:
                logger.warning(f"Kokoro-ONNX load failed: {exc}")

        self._load_thread = threading.Thread(target=_load, daemon=True)
        self._load_thread.start()

    @staticmethod
    def _download(url: str, path: Path) -> None:
        """Download with retries (3 attempts, exponential backoff).

        Downloads to a temp file and atomically renames on success so a
        partial/interrupted download can never leave a corrupt model at
        the final path (which would be treated as "already downloaded"
        and fail to load forever).
        """
        last_exc = None
        tmp = path.with_suffix(path.suffix + ".part")
        for attempt in range(3):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(url, tmp)
                # Sanity: a truncated download (e.g. connection dropped)
                # yields a small file — refuse to keep it.
                if tmp.exists() and tmp.stat().st_size < 1024 * 1024:
                    raise RuntimeError(f"Suspiciously small download ({tmp.stat().st_size} bytes)")
                tmp.replace(path)
                return
            except (urllib.error.URLError, urllib.error.HTTPError,
                    OSError, TimeoutError, RuntimeError) as exc:
                last_exc = exc
                tmp.unlink(missing_ok=True)
                time.sleep(2 ** attempt)
        raise last_exc if last_exc else RuntimeError(f"Failed to download {url}")

    def _wait_loaded(self, timeout: float = 600.0) -> bool:
        if self.is_available and self._kokoro is not None:
            return True
        if self._load_thread is not None:
            self._load_thread.join(timeout=timeout)
        return self.is_available and self._kokoro is not None

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self._wait_loaded():
            return False
        kokoro = self._kokoro
        if kokoro is None:
            return False
        voice = voice or self.VOICE_MAP.get(mode, "af_bella")
        try:
            samples, sample_rate = kokoro.create(
                text, voice=voice, speed=self.speed, lang="en-us")
            if samples is None or len(samples) == 0:
                return False
            write_wav(output_path, np.asarray(samples, dtype=np.float32),
                      int(sample_rate))
            return True
        except Exception as exc:
            logger.warning(f"Kokoro synthesis failed: {exc}")
            return False

    def get_voices(self) -> list[str]:
        return list(self.VOICE_MAP.values())


# ---------------------------------------------------------------------------
# Edge-TTS provider
# ---------------------------------------------------------------------------


class EdgeTTSProvider(VoiceProvider):
    """Microsoft Edge neural voices — excellent quality, requires internet."""

    name = "edge-tts"
    quality = "high"
    requires_internet = True
    latency_ms = 1500

    VOICE_MAP: ClassVar[dict[VoiceMode, str]] = {
        VoiceMode.CONVERSATION: "en-IE-EmilyNeural",  # Irish female ≈ FRIDAY
        VoiceMode.BRIEFING: "en-IE-EmilyNeural",
        VoiceMode.ALERT: "en-GB-SoniaNeural",
        VoiceMode.WHISPER: "en-GB-MaisieNeural",
    }

    def __init__(self):
        try:
            import edge_tts  # noqa: F401
            self.is_available = True
        except ImportError:
            self.is_available = False

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self.is_available:
            return False
        voice_name = voice or self.VOICE_MAP.get(mode, "en-IE-EmilyNeural")
        try:
            import asyncio

            import edge_tts

            async def _synth():
                communicate = edge_tts.Communicate(text, voice_name)
                await communicate.save(output_path)

            asyncio.run(_synth())
            return True
        except Exception as exc:
            logger.warning(f"Edge TTS failed: {exc}")
            return False

    def get_voices(self) -> list[str]:
        return list(self.VOICE_MAP.values())


# ---------------------------------------------------------------------------
# pyttsx3 provider (emergency)
# ---------------------------------------------------------------------------


class PyTTSProvider(VoiceProvider):
    """System TTS — always available, robotic quality."""

    name = "pyttsx3"
    quality = "low"
    requires_internet = False
    latency_ms = 100
    _engine = None

    def __init__(self):
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 180)
            self._engine.setProperty("volume", 0.9)
            self.is_available = True
        except Exception:
            self.is_available = False

    def synthesize(self, text: str, output_path: str,
                   voice: str = "", mode: VoiceMode = VoiceMode.CONVERSATION
                   ) -> bool:
        if not self.is_available or self._engine is None:
            return False
        try:
            self._engine.save_to_file(text, output_path)
            self._engine.runAndWait()
            return Path(output_path).exists()
        except Exception:
            return self.speak_direct(text)

    def speak_direct(self, text: str) -> bool:
        if not self.is_available or self._engine is None:
            return False
        try:
            self._engine.say(text)
            self._engine.runAndWait()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# TTS frontend
# ---------------------------------------------------------------------------


@dataclass
class TTSConfig:
    """Configuration for the TTS engine."""
    primary_provider: str = "kokoro"        # kokoro, edge, pyttsx3
    voice: str = ""                          # voice name/ID override
    mode: VoiceMode = VoiceMode.CONVERSATION
    volume: float = 0.9
    speed: float = 1.0
    cache_enabled: bool = True
    cache_dir: str = str(Path.home() / ".friday" / "voice_cache")
    max_cache_entries: int = 1000


class TextToSpeech:
    """Friday's voice — speak text aloud through the best available engine."""

    def __init__(self, config: Optional[TTSConfig] = None):
        self.config = config or TTSConfig()
        self._providers: list[VoiceProvider] = []
        self._current_provider: Optional[VoiceProvider] = None
        self._speak_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        _setup_espeak()
        self._init_providers()

        if self.config.cache_enabled:
            try:
                Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

        logger.info(f"TTS initialized. Active provider: {self.active_provider_name}")

    @staticmethod
    def _probe_internet(timeout: float = 2.0) -> bool:
        """Best-effort connectivity check for auto provider selection."""
        import socket
        try:
            socket.setdefaulttimeout(timeout)
            socket.create_connection(("8.8.8.8", 53))
            return True
        except Exception:
            return False
        finally:
            socket.setdefaulttimeout(None)

    def _init_providers(self) -> list[str]:
        preferred = self.config.primary_provider
        _REGISTRY: dict[str, type[VoiceProvider]] = {
            "piper": PiperProvider,
            "edge": EdgeTTSProvider,
            "kokoro": KokoroONNXProvider,
            "pyttsx3": PyTTSProvider,
        }
        # "auto" → piper (offline, instant, natural); falls back naturally
        # to edge when piper can't load.
        if preferred == "auto":
            preferred = "piper"
            logger.info(f"Auto TTS provider → {preferred}")
        order = [preferred] if preferred in _REGISTRY else []
        order += [n for n in ("piper", "edge", "kokoro", "pyttsx3")
                  if n not in order]

        for name in order:
            try:
                provider = _REGISTRY[name]()
                provider.speed = self.config.speed
                self._providers.append(provider)
                if provider.is_available:
                    self._current_provider = provider
                    if name == preferred:
                        break  # primary found — don't let a fallback override
            except Exception as exc:
                logger.debug(f"Provider {name} init failed: {exc}")

        if not self._current_provider:
            logger.warning("No TTS provider available — speech disabled")
        return list(order)

    def _ensure_primary_loaded(self, timeout: float = 600.0) -> None:
        """Block once for the primary model if it is still downloading.

        Called lazily on the first speak(), NOT in __init__, so
        `voice status` / `voice setup` never hang on a model download.
        ``timeout`` bounds the wait (diagnostic callers like `doctor`
        pass a tighter bound than the 600 s speak-path default).
        """
        # If a fallback (edge/pyttsx3) won the init race while the primary
        # was still loading async, promote the primary once it's ready —
        # otherwise the configured provider is never used.
        preferred = self.config.primary_provider
        if preferred == "auto":
            preferred = "piper"
        for p in self._providers:
            if p.name != preferred:
                continue
            # Duck-type instead of isinstance(): the provider may be a
            # test mock (or a future drop-in), so probe for the capability
            # marker (_wait_loaded) rather than the concrete class.
            wait = getattr(p, "_wait_loaded", None)
            if not callable(wait):
                return
            try:
                wait(timeout=timeout)
            except Exception:
                pass
            if getattr(p, "is_available", False):
                self._current_provider = p
            return

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
        """Speak text aloud. Non-blocking; interrupts current speech."""
        if not text:
            return False
        # Promote the primary provider when it's ready (blocks once for a
        # model download); if it never loads, keep the current fallback.
        self._ensure_primary_loaded()
        if not self._current_provider:
            return False
        self.stop()
        effective_mode = mode or self.config.mode
        self._stop_event.clear()
        self._speak_thread = threading.Thread(
            target=self._speak_sync, args=(text, effective_mode), daemon=True)
        self._speak_thread.start()
        return True

    def speak_and_wait(self, text: str,
                       mode: Optional[VoiceMode] = None) -> None:
        self.speak(text, mode)
        if self._speak_thread:
            self._speak_thread.join(timeout=30)

    def stop(self) -> None:
        """Stop current speech immediately (<50 ms best-effort).

        Kills the active audio-player subprocess (paplay/aplay/ffplay)
        so playback actually stops, not just the synthesis thread.
        """
        self._stop_event.set()
        stop_playback()
        if self._speak_thread:
            self._speak_thread.join(timeout=2)
            self._speak_thread = None

    def _speak_sync(self, text: str, mode: VoiceMode) -> None:
        if self._stop_event.is_set():
            return
        cache_key = self._cache_key(text, mode)

        # PyTTS has a more reliable direct path
        if isinstance(self._current_provider, PyTTSProvider):
            self._current_provider.speak_direct(text)
            return

        cached = self._check_cache(cache_key)
        if cached and Path(cached).exists():
            play_wav_file(cached)
            return

        chunks = self._split_sentences(text)
        # Single-sentence text → old behaviour: synth, play, cache.
        if len(chunks) <= 1:
            self._synthesize_and_play(text, mode, cache_key)
            return

        # Multi-sentence → stream: synthesize each chunk, queue for
        # immediate playback so the first words play while later chunks
        # are still rendering. NOTE: multi-sentence responses are NOT
        # cached — caching only the first chunk under the full-text key
        # would replay just the first sentence on the next identical ask.
        synth_ok = False
        for chunk in chunks:
            if self._stop_event.is_set():
                break
            wav = self._synthesize_chunk(chunk, mode)
            if wav is None:
                continue
            synth_ok = True
            queue_wav(wav)
        flush_play_queue()
        if not synth_ok:
            # Every chunk failed → fall back to another provider.
            self._fallback_speak(text, mode)

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into TTS-sized chunks for streaming playback.

        Rules:
          - Break on sentence punctuation (. ! ?) first.
          - Break long clauses on commas.
          - Cap every chunk at ~12 words so the first chunk stays small
            (bounds time-to-first-sound) without multiplying the fixed
            per-synthesis overhead (~1.5 s/call) too many times.
        """
        import re

        def _break_commas(s: str) -> list[str]:
            parts = [p.strip() for p in re.split(r',\s+', s)]
            return [p for p in parts if p]

        parts = re.split(r'(?<=[.!?])\s+(?=[A-Z0-9])', text.strip())
        out = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            words = p.split()
            if len(words) <= 12:
                out.append(p)
                continue
            # Long sentence → split on commas first.
            sub = _break_commas(p)
            if len(sub) <= 1:
                sub = p.split()
                while sub:
                    out.append(" ".join(sub[:12]))
                    sub = sub[12:]
            else:
                cur = ""
                for clause in sub:
                    test = (cur + " " + clause).strip()
                    if len(test.split()) > 12 and cur:
                        out.append(cur)
                        cur = clause
                    else:
                        cur = test
                if cur:
                    out.append(cur)
        return out

    def _synthesize_chunk(self, text: str, mode: VoiceMode) -> Optional[str]:
        """Synthesize one chunk to a temp wav; return its path or None."""
        provider = self._current_provider
        if provider is None:
            return None
        import tempfile
        output_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                output_path = f.name
            if provider.synthesize(
                    text=text, output_path=output_path,
                    voice=self.config.voice, mode=mode):
                return output_path
            return None
        except Exception as exc:
            logger.error(f"Chunk synthesis failed: {exc}")
            return None

    def _synthesize_and_play(self, text: str, mode: VoiceMode,
                             cache_key: str) -> None:
        """Synthesize a whole short utterance, play it, and cache it."""
        provider = self._current_provider
        if provider is None:
            self._fallback_speak(text, mode)
            return
        output_path = None
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                output_path = f.name
            success = provider.synthesize(
                text=text, output_path=output_path,
                voice=self.config.voice, mode=mode)
            if success and not self._stop_event.is_set():
                play_wav_file(output_path)
                if self.config.cache_enabled:
                    self._save_cache(cache_key, output_path)
            elif not success:
                self._fallback_speak(text, mode)
        except Exception as exc:
            logger.error(f"Speech synthesis failed: {exc}")
            self._fallback_speak(text, mode)
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def _fallback_speak(self, text: str, mode: VoiceMode) -> None:
        for provider in self._providers:
            if provider is self._current_provider or not provider.is_available:
                continue
            if self._stop_event.is_set():
                return
            logger.info(f"Falling back to {provider.name}")
            if isinstance(provider, PyTTSProvider):
                provider.speak_direct(text)
                return
            import tempfile
            output_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    output_path = f.name
                if provider.synthesize(text, output_path, mode=mode):
                    play_wav_file(output_path)
                return
            except Exception:
                continue
            finally:
                if output_path:
                    try:
                        Path(output_path).unlink(missing_ok=True)
                    except Exception:
                        pass

    # -- caching -------------------------------------------------------------

    def _cache_key(self, text: str, mode: VoiceMode) -> str:
        return f"{text}|{mode.value}|{self.config.voice}"

    @staticmethod
    def _hash(key: str) -> str:
        # Deterministic across processes (not Python's random-seed hash())
        return hashlib.md5(key.encode()).hexdigest()

    def _check_cache(self, key: str) -> Optional[str]:
        if not self.config.cache_enabled:
            return None
        p = Path(self.config.cache_dir) / f"{self._hash(key)}.wav"
        return str(p) if p.exists() else None

    def _save_cache(self, key: str, wav_path: str) -> None:
        try:
            dest = Path(self.config.cache_dir) / f"{self._hash(key)}.wav"
            shutil.copy2(wav_path, dest)
            self._trim_cache()
        except Exception:
            pass

    def _trim_cache(self) -> None:
        try:
            cache_dir = Path(self.config.cache_dir)
            if not cache_dir.exists():
                return
            entries = sorted(cache_dir.glob("*.wav"),
                             key=lambda p: p.stat().st_mtime)
            if len(entries) > self.config.max_cache_entries:
                for entry in entries[:len(entries) - self.config.max_cache_entries]:
                    try:
                        entry.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    # -- introspection -------------------------------------------------------

    def list_providers(self) -> list[dict]:
        return [{
            "name": p.name,
            "available": p.is_available,
            "quality": p.quality,
            "latency_ms": p.latency_ms,
            "requires_internet": p.requires_internet,
        } for p in self._providers]

    def play_chime(self, chime_type: str = "listen") -> None:
        from .chimes import play_chime
        threading.Thread(target=play_chime, args=(chime_type,), daemon=True).start()

    def list_devices(self) -> list[dict]:
        from .audio import list_output_devices
        return [{"name": d.name, "index": d.index} for d in list_output_devices()]
