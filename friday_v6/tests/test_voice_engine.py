"""Voice engine tests — single-threaded event-loop redesign (Wave 2.2).

Regression tests for the redesign:
  1. STT facade must activate the faster-whisper provider after its async
     load finishes (the `_load_thread` promotion bug — voice stayed deaf).
  2. Kokoro download URLs must point at assets that exist on the release.
  3. TTS speak() must synthesize through the active provider and play.
  4. VoiceEngine: one loop, one queue, full state machine + barge-in,
     clean stop (no dangling PortAudio callback → no SIGABRT at exit).
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from friday_v6.voice.pipeline import PipelineConfig, PipelineState
from friday_v6.voice.utils import write_wav

# ==========================================================================
# Fakes
# ==========================================================================

class FakeAudio:
    """Records the callback; frames are delivered by the test."""

    def __init__(self):
        self.callback = None
        self.started = False
        self.stopped = False

    def start(self, callback) -> bool:
        self.callback = callback
        self.started = True
        return True

    def stop(self) -> None:
        self.stopped = True

    def feed(self, frame: np.ndarray) -> None:
        if self.callback:
            self.callback(frame)


class FakeVAD:
    def __init__(self, speech: bool = False):
        self.speech = speech

    def is_speech(self, frame, sample_rate) -> bool:
        return self.speech


class FakeHotword:
    def __init__(self, trigger: bool = False):
        self.trigger = trigger

    def process(self, frame) -> bool:
        return self.trigger

    def cleanup(self) -> None:
        pass


class FakeSTT:
    def __init__(self, text: str = "hello world"):
        self.text = text
        self.calls = 0

    @property
    def is_available(self) -> bool:
        return True

    def transcribe(self, audio, sample_rate):
        self.calls += 1
        return SimpleNamespace(success=True, text=self.text,
                               confidence=0.95, error="")


class FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []
        self.is_speaking = False
        self.stopped = False

    def speak(self, text, mode=None) -> bool:
        self.spoken.append(text)
        self.is_speaking = True
        return True

    def stop(self) -> None:
        self.stopped = True
        self.is_speaking = False

    @property
    def active_provider_name(self) -> str:
        return "fake"

    @property
    def is_available(self) -> bool:
        return True


def _silence_frame(n: int = 480) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


def _loud_frame(n: int = 480) -> np.ndarray:
    return np.ones(n, dtype=np.float32) * 0.5


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ==========================================================================
# STT async-load promotion (the deaf-voice bug)
# ==========================================================================

class _SlowFakeWhisper:
    """WhisperModel stand-in that loads slower than the 0.5s fast-path
    window, forcing the async `_finish`/promotion path."""

    def __init__(self, *args, **kwargs):
        time.sleep(0.7)

    def transcribe(self, audio, **kwargs):
        seg = SimpleNamespace(text="hello world", avg_logprob=-0.1)
        info = SimpleNamespace(language="en", duration=1.0)
        return [seg], info


class TestSTTPromotion:
    def test_facade_activates_after_async_load(self):
        """SpeechToText must promote faster-whisper once its background load
        completes — even when the model takes longer than the 0.5s fast path."""
        fake_module = MagicMock()
        fake_module.WhisperModel = _SlowFakeWhisper
        with patch.dict(sys.modules, {"faster_whisper": fake_module}), \
             patch("importlib.util.find_spec", return_value=True):
            from friday_v6.voice.stt import SpeechToText
            stt = SpeechToText()
            assert _wait_for(lambda: stt.is_available, timeout=8.0), \
                "STT facade never activated the async-loaded provider"
            result = stt.transcribe(np.zeros(1600, dtype=np.float32), 16000)
            assert result.success and result.text == "hello world"

    def test_provider_exposes_load_thread(self):
        """FasterWhisperProvider must keep a handle to its load thread so the
        facade can join it (the missing-attribute promotion bug)."""
        fake_module = MagicMock()
        fake_module.WhisperModel = _SlowFakeWhisper
        with patch.dict(sys.modules, {"faster_whisper": fake_module}), \
             patch("importlib.util.find_spec", return_value=True):
            from friday_v6.voice.stt import FasterWhisperProvider
            provider = FasterWhisperProvider()
            assert hasattr(provider, "_load_thread"), \
                "_load_thread missing — promotion join() fails silently"
            assert isinstance(provider._load_thread, threading.Thread)


# ==========================================================================
# STT segment-generator handling (the deaf-voice bug)
# ==========================================================================

class TestSTTGeneratorSegments:
    def test_generator_segments_not_double_consumed(self):
        """faster-whisper yields segments lazily (a generator). The provider
        must materialize it ONCE — iterating it twice (text, then confidence)
        exhausted the iterator, zeroed avg_confidence and rejected every
        real transcription as "low confidence"."""
        import numpy as np

        from friday_v6.voice.stt import FasterWhisperProvider

        class _GenModel:
            def transcribe(self, audio, **kwargs):
                def _gen():
                    yield SimpleNamespace(text="hello world",
                                          avg_logprob=-0.05)
                info = SimpleNamespace(language="en", duration=1.0)
                return _gen(), info  # like real faster-whisper

        provider = FasterWhisperProvider.__new__(FasterWhisperProvider)
        provider._model = _GenModel()
        provider.is_available = True

        result = provider.transcribe(np.zeros(1600, dtype=np.float32), 16000)
        assert result.success is True, f"transcription rejected: {result.error}"
        assert result.text == "hello world"
        assert result.confidence > 0.3

    def test_provider_rejects_low_confidence_but_keeps_text(self):
        """Low-confidence utterances are still surfaced with the text (the
        confidence gate must not silently drop every result)."""
        import numpy as np

        from friday_v6.voice.stt import FasterWhisperProvider

        class _LowConfModel:
            def transcribe(self, audio, **kwargs):
                def _gen():
                    yield SimpleNamespace(text="maybe this",
                                          avg_logprob=-2.5)
                return _gen(), SimpleNamespace(language="en", duration=1.0)

        provider = FasterWhisperProvider.__new__(FasterWhisperProvider)
        provider._model = _LowConfModel()
        provider.is_available = True

        result = provider.transcribe(np.zeros(1600, dtype=np.float32), 16000)
        assert result.text == "maybe this"  # text preserved
        assert result.success is False       # but flagged low-confidence


# ==========================================================================
# Kokoro download URLs
# ==========================================================================

class TestKokoroURLs:
    def test_model_url_points_to_v10_asset(self):
        """The kokoro model URL must reference an asset that exists on the
        model-files-v1.0 release (v0.19.onnx 404s; v1.0 is current)."""
        from friday_v6.voice.tts import _KOKORO_MODEL_URL, _KOKORO_VOICES_URL
        assert _KOKORO_MODEL_URL.endswith("kokoro-v1.0.onnx"), _KOKORO_MODEL_URL
        # kokoro_onnx loads voices via np.load() — needs the .bin, not json
        assert _KOKORO_VOICES_URL.endswith("voices-v1.0.bin"), _KOKORO_VOICES_URL

    def test_download_is_atomic_and_size_checked(self, tmp_path):
        """A truncated download must never land at the final path — a
        partial model would be treated as 'already downloaded' and fail
        to load forever."""
        from friday_v6.voice.tts import KokoroONNXProvider
        dest = tmp_path / "model.onnx"

        class _FakeURL:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            def __call__(self, url, filename):
                chunk = self._chunks.pop(0)
                with open(filename, "wb") as f:
                    f.write(chunk)

        # First attempt: tiny (truncated) → must retry; second: big enough
        fake = _FakeURL([b"tiny", b"x" * (1024 * 1024 + 1)])
        with patch("urllib.request.urlretrieve", side_effect=fake):
            KokoroONNXProvider._download("http://example.com/model", dest)
        assert dest.exists()
        assert dest.stat().st_size == 1024 * 1024 + 1
        assert not (tmp_path / "model.onnx.part").exists()


# ==========================================================================
# TTS speak path
# ==========================================================================

class _FakeProvider:
    name = "fake"
    quality = "high"
    requires_internet = False
    is_available = True
    latency_ms = 0

    def synthesize(self, text: str, output_path: str, voice: str = "",
                   mode=None) -> bool:
        write_wav(output_path, np.zeros(1600, dtype=np.float32), 16000)
        return True


class TestTTSSpeak:
    def test_speak_synthesizes_and_plays(self, tmp_path):
        from friday_v6.voice.tts import TextToSpeech, TTSConfig

        tts = TextToSpeech(TTSConfig(
            primary_provider="pyttsx3", cache_enabled=False,
            cache_dir=str(tmp_path / "cache")))
        tts._current_provider = _FakeProvider()
        with patch("friday_v6.voice.tts.play_wav_file") as mock_play:
            assert tts.speak("hello") is True
            _wait_for(lambda: mock_play.called, timeout=3.0)
        assert mock_play.called, "spoken audio never reached the player"

    def test_multi_sentence_not_cached_partially(self, tmp_path):
        """Multi-sentence responses stream chunk-by-chunk and must NOT write
        a partial (first-sentence-only) cache entry under the full-text key —
        the next identical ask would replay just the first sentence."""
        from pathlib import Path

        from friday_v6.voice.tts import TextToSpeech, TTSConfig

        cache_dir = tmp_path / "cache"
        tts = TextToSpeech(TTSConfig(
            primary_provider="pyttsx3", cache_enabled=True,
            cache_dir=str(cache_dir)))
        tts._current_provider = _FakeProvider()
        with patch("friday_v6.voice.tts.queue_wav") as mock_queue, \
             patch("friday_v6.voice.tts.flush_play_queue"), \
             patch("friday_v6.voice.tts.play_wav_file"):
            assert tts.speak("First sentence. Second, longer sentence here.") is True
            # Both chunks must be synthesized and queued (streaming)
            _wait_for(lambda: mock_queue.call_count >= 2, timeout=3.0)
        assert mock_queue.call_count >= 2
        # No partial cache may exist under the full-text key
        assert list(Path(cache_dir).glob("*.wav")) == [], \
            "multi-sentence response was partially cached"


# ==========================================================================
# VoiceEngine state machine
# ==========================================================================

class TestVoiceEngine:
    def _make_engine(self, config=None, **kwargs):
        from friday_v6.voice.core import VoiceEngine
        cfg = config or PipelineConfig(
            hotword="hey friday", silence_timeout_seconds=0.2,
            max_utterance_seconds=2.0, enable_chimes=False)
        audio = kwargs.pop("audio", FakeAudio())
        vad = kwargs.pop("vad", FakeVAD())
        hotword = kwargs.pop("hotword", FakeHotword())
        stt = kwargs.pop("stt", FakeSTT())
        tts = kwargs.pop("tts", FakeTTS())
        return VoiceEngine(cfg, audio=audio, vad=vad, hotword=hotword,
                           stt=stt, tts=tts, **kwargs), audio, vad, hotword, stt, tts

    def test_start_without_audio_backend_returns_false(self):
        """No audio backend (start() fails) → engine refuses to start."""
        class BrokenAudio:
            def start(self, callback) -> bool:
                return False

            def stop(self) -> None:
                pass

        engine, _, _, _, _, _ = self._make_engine(audio=BrokenAudio())
        assert engine.start() is False
        assert engine.is_running is False

    def test_full_conversation_flow(self):
        """IDLE → HOTWORD → LISTENING → PROCESSING → SPEAKING → IDLE,
        with the route response spoken aloud."""
        engine, audio, vad, hotword, stt, tts = self._make_engine()
        engine.route_function = lambda text: f"you said {text}"
        assert engine.start() is True
        try:
            # IDLE: silence keeps us idle
            audio.feed(_silence_frame())
            # Hotword fires → LISTENING (keep trigger high across frames —
            # the loop consumes frames asynchronously)
            hotword.trigger = True
            for _ in range(5):
                audio.feed(_loud_frame())
                time.sleep(0.005)
            hotword.trigger = False
            assert _wait_for(lambda: engine.state == PipelineState.LISTENING)
            # Speech frames keep the session alive (real mic ≈ 30 ms cadence)
            vad.speech = True
            for _ in range(30):
                audio.feed(_loud_frame())
                time.sleep(0.005)
            vad.speech = False
            # Silence for timeout → PROCESSING → SPEAKING
            assert _wait_for(lambda: engine.state == PipelineState.SPEAKING,
                             timeout=2.0), f"state stuck at {engine.state}"
            assert tts.spoken == ["you said hello world"], tts.spoken
            # Speech ends → back to IDLE
            tts.is_speaking = False
            assert _wait_for(lambda: engine.state == PipelineState.IDLE)
            assert stt.calls == 1
        finally:
            engine.stop()

    def test_barge_in_interrupts_speech(self):
        """Speaking: a VAD-speech frame after the refractory window stops
        TTS and returns to LISTENING."""
        engine, audio, vad, hotword, stt, tts = self._make_engine(
            PipelineConfig(hotword="", silence_timeout_seconds=1.0,
                           enable_chimes=False))
        assert engine.start() is True
        try:
            engine.speak("long announcement")
            assert _wait_for(lambda: engine.state == PipelineState.SPEAKING)
            # Within the refractory window — no interruption yet
            vad.speech = True
            for _ in range(5):
                audio.feed(_loud_frame())
                time.sleep(0.01)
            assert not tts.stopped
            # Wait past the 1.5 s anti-echo window, then feed frames
            # → barge-in fires (needs 10+ frames)
            time.sleep(1.6)
            for _ in range(15):
                audio.feed(_loud_frame())
                time.sleep(0.01)
            assert tts.stopped
        finally:
            engine.stop()

    def test_speak_interrupts_previous_speech(self):
        engine, audio, vad, hotword, stt, tts = self._make_engine()
        assert engine.start() is True
        try:
            engine.speak("first")
            assert _wait_for(lambda: engine.state == PipelineState.SPEAKING)
            engine.speak("second")
            assert _wait_for(lambda: tts.spoken == ["first", "second"])
            tts.is_speaking = False
            assert _wait_for(lambda: engine.state == PipelineState.IDLE)
        finally:
            engine.stop()

    def test_stop_is_clean_and_reusable(self):
        """stop() joins the loop, closes audio; start() again works."""
        engine, audio, vad, hotword, stt, tts = self._make_engine()
        assert engine.start() is True
        engine.stop()
        assert audio.stopped
        assert not engine.is_running
        assert engine.start() is True
        engine.stop()

    def test_push_to_talk_records_and_transcribes(self):
        engine, audio, vad, hotword, stt, tts = self._make_engine(
            PipelineConfig(hotword="", silence_timeout_seconds=5.0,
                           enable_chimes=False))
        assert engine.start() is True
        try:
            engine.push_to_talk()
            assert _wait_for(lambda: engine.state == PipelineState.LISTENING)
            vad.speech = True
            audio.feed(_loud_frame())
            vad.speech = False
            text = engine.stop_recording_and_process()
            assert text == "hello world"
            assert _wait_for(lambda: engine.state == PipelineState.IDLE)
        finally:
            engine.stop()

    def test_empty_route_response_returns_to_idle(self):
        """A route function returning '' must not leave the engine stuck in
        PROCESSING — hotword detection and push-to-talk would stay dead
        until the process restarts."""
        engine, audio, vad, hotword, stt, tts = self._make_engine(
            PipelineConfig(hotword="hey friday", silence_timeout_seconds=0.2,
                           enable_chimes=False))
        engine.route_function = lambda text: ""  # no response to speak
        assert engine.start() is True
        try:
            hotword.trigger = True
            for _ in range(5):
                audio.feed(_loud_frame())
                time.sleep(0.005)
            hotword.trigger = False
            assert _wait_for(lambda: engine.state == PipelineState.LISTENING)
            vad.speech = True
            for _ in range(10):
                audio.feed(_loud_frame())
                time.sleep(0.005)
            vad.speech = False
            # Silence → stop recording → PROCESSING → route("") → must IDLE
            assert _wait_for(lambda: engine.state == PipelineState.IDLE,
                             timeout=2.0), \
                f"engine stuck in {engine.state} after empty route response"
            assert stt.calls == 1
        finally:
            engine.stop()
