"""Voice module tests — Voice Wave 2.0.

Unit-level tests with mocked audio I/O (no microphone needed). Covers the
new provider-facade architecture: WAV utils, config, TTS auto-mode, chimes,
pipeline state, STT, hotword, VAD, audio device listing, and router routing.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ==========================================================================
# Utils tests
# ==========================================================================


class TestUtils:
    def test_write_and_read_wav_roundtrip(self):
        from friday_v4.voice.utils import write_wav, read_wav
        audio = np.sin(np.linspace(0, 2 * np.pi * 440, 16000)).astype(np.float32) * 0.5
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            write_wav(path, audio, 16000)
            read_audio, sr = read_wav(path)
            assert sr == 16000
            assert len(read_audio) == len(audio)
            diff = np.abs(read_audio - audio).mean()
            assert diff < 0.001  # int16 quantization loss
        finally:
            Path(path).unlink(missing_ok=True)

    def test_write_wav_clips_properly(self):
        from friday_v4.voice.utils import write_wav, read_wav
        audio = np.array([-2.0, -1.5, 0.0, 1.5, 2.0], dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            write_wav(path, audio, 16000)
            read_audio, _ = read_wav(path)
            assert read_audio.min() >= -1.01
            assert read_audio.max() <= 1.01
        finally:
            Path(path).unlink(missing_ok=True)


# ==========================================================================
# Config tests
# ==========================================================================


class TestConfig:
    def test_load_config_defaults(self):
        from friday_v4.config import load_config
        config = load_config(Path("/nonexistent/config.json"))
        assert config.voice.enabled is True
        assert config.voice.tts_provider == "kokoro"
        assert config.voice.stt_model == "base.en"
        assert config.voice.vad_mode == 1

    def test_load_config_merges_partial(self):
        from friday_v4.config import load_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"voice": {"tts_provider": "edge", "vad_mode": 3}}, f)
            path = f.name
        try:
            config = load_config(Path(path))
            assert config.voice.tts_provider == "edge"
            assert config.voice.vad_mode == 3
            assert config.voice.hotword == "hey friday"  # default retained
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_config_invalid_json_returns_defaults(self):
        from friday_v4.config import load_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json!!!")
            path = f.name
        try:
            config = load_config(Path(path))
            assert config.voice.tts_provider == "kokoro"
        finally:
            Path(path).unlink(missing_ok=True)


# ==========================================================================
# TTS tests
# ==========================================================================


class TestTTSUtils:
    def test_auto_voice_mode_alert(self):
        from friday_v4.voice.tts import auto_voice_mode, VoiceMode
        assert auto_voice_mode("Critical vulnerability found") == VoiceMode.ALERT
        assert auto_voice_mode("Security breach detected in deps") == VoiceMode.ALERT
        assert auto_voice_mode("CVE-2024-1234 affects your package") == VoiceMode.ALERT

    def test_auto_voice_mode_briefing(self):
        from friday_v4.voice.tts import auto_voice_mode, VoiceMode
        long_text = ". ".join(["status report"] * 15)
        assert auto_voice_mode(long_text) == VoiceMode.BRIEFING

    def test_auto_voice_mode_conversation_default(self):
        from friday_v4.voice.tts import auto_voice_mode, VoiceMode
        import datetime
        result = auto_voice_mode("Hello, how are you?")
        hour = datetime.datetime.now().hour
        if hour < 7 or hour >= 23:
            assert result == VoiceMode.WHISPER
        else:
            assert result == VoiceMode.CONVERSATION

    def test_chime_generation(self):
        from friday_v4.voice.chimes import get_chime
        for chime_type in ("listen", "done", "alert", "error", "think"):
            data = get_chime(chime_type)
            assert len(data) > 44
            assert data[:4] == b"RIFF"
            assert data[8:12] == b"WAVE"

    def test_provider_registry_selection(self):
        """TTS facade should init and prefer the configured provider."""
        from friday_v4.voice.tts import TextToSpeech, TTSConfig
        # Force pyttsx3 (always available or gracefully unavailable) — the
        # facade must never crash even when every provider is missing.
        tts = TextToSpeech(TTSConfig(primary_provider="pyttsx3", cache_enabled=False))
        assert isinstance(tts.list_providers(), list)

    def test_tts_speak_no_provider_returns_false(self):
        from friday_v4.voice.tts import TextToSpeech, TTSConfig
        with patch("friday_v4.voice.tts.PyTTSProvider") as mock_py, \
             patch("friday_v4.voice.tts.EdgeTTSProvider") as mock_edge, \
             patch("friday_v4.voice.tts.KokoroONNXProvider") as mock_kokoro:
            mock_py.return_value.is_available = False
            mock_edge.return_value.is_available = False
            mock_kokoro.return_value.is_available = False
            tts = TextToSpeech(TTSConfig(primary_provider="kokoro", cache_enabled=False))
            assert tts.is_available is False
            assert tts.speak("hello") is False


# ==========================================================================
# Chimes
# ==========================================================================


class TestChimes:
    def test_chime_playback_doesnt_crash(self):
        from friday_v4.voice.chimes import play_chime
        play_chime("done")  # non-blocking; just must not raise
        time.sleep(0.1)


# ==========================================================================
# Pipeline tests
# ==========================================================================


class TestPipelineState:
    def test_state_transition_calls_callback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineState, PipelineConfig
        pipeline = VoicePipeline(PipelineConfig(hotword_sensitivity=0.0))
        states = []

        def on_state(s):
            states.append(s)

        pipeline.on_state_change = on_state
        pipeline.state = PipelineState.LISTENING
        assert PipelineState.LISTENING in states

    def test_interruption_flag_consumed(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        pipeline = VoicePipeline(PipelineConfig())
        with pipeline._interrupted_lock:
            pipeline._interrupted = True
        with pipeline._speech_gen_lock:
            gen = pipeline._speech_gen
        pipeline._wait_for_speech_end(gen)
        with pipeline._interrupted_lock:
            assert pipeline._interrupted is False

    def test_speech_gen_prevents_stale_reset(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        pipeline = VoicePipeline(PipelineConfig())
        with pipeline._speech_gen_lock:
            pipeline._speech_gen = 2
        pipeline._wait_for_speech_end(1)  # stale gen → no state reset
        assert pipeline.state != "idle"

    def test_start_stop_lifecycle(self):
        """Pipeline starts and stops without crashing (no mic in CI)."""
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        pipeline = VoicePipeline(PipelineConfig())
        result = pipeline.start()  # False is fine without audio hardware
        pipeline.stop()
        assert result is not None


# ==========================================================================
# STT tests
# ==========================================================================


class TestSTT:
    def test_stt_result_dataclass(self):
        from friday_v4.voice.stt import STTResult
        r = STTResult(text="hello", confidence=0.9, success=True)
        assert r.text == "hello"
        assert r.success is True

    def test_stt_init_graceful_without_providers(self):
        from friday_v4.voice.stt import SpeechToText
        stt = SpeechToText()
        assert isinstance(stt.list_providers(), list)

    def test_faster_whisper_init_doesnt_crash_when_missing(self):
        from friday_v4.voice.stt import FasterWhisperProvider
        provider = FasterWhisperProvider()
        assert hasattr(provider, "is_available")


# ==========================================================================
# Hotword tests
# ==========================================================================


class TestHotword:
    def test_hotword_init_graceful(self):
        from friday_v4.voice.hotword import HotwordDetector
        hw = HotwordDetector("hey friday", 0.5)
        assert hasattr(hw, "is_available")
        assert hw.provider_name in ("openwakeword", "energy", "none")

    def test_energy_detector_basic(self):
        from friday_v4.voice.hotword import EnergyDetector
        det = EnergyDetector(threshold=0.01, min_frames=1)
        silence = np.zeros(480, dtype=np.float32)
        assert not det.process(silence)
        loud = np.ones(480, dtype=np.float32) * 0.9
        assert det.process(loud)

    def test_sensitivity_bounds(self):
        from friday_v4.voice.hotword import EnergyDetector
        det = EnergyDetector()
        det.set_sensitivity(0.0)
        assert det._threshold <= 0.05
        det.set_sensitivity(1.0)
        assert det._threshold >= 0.005


# ==========================================================================
# VAD tests
# ==========================================================================


class TestVAD:
    def test_vad_init_graceful(self):
        from friday_v4.voice.vad import VoiceActivityDetector
        vad = VoiceActivityDetector(mode=1)
        assert vad.is_available  # energy fallback always available
        assert vad.provider_name in ("silero", "webrtc", "energy")

    def test_webrtc_vad_init_graceful(self):
        from friday_v4.voice.vad import WebRTCVAD
        vad = WebRTCVAD(mode=1)
        assert hasattr(vad, "is_available")

    def test_energy_vad_detects_speech(self):
        from friday_v4.voice.vad import EnergyVAD
        vad = EnergyVAD(threshold=0.01)
        assert not vad.is_speech(np.zeros(480, dtype=np.float32))
        assert vad.is_speech(np.ones(480, dtype=np.float32) * 0.5)


# ==========================================================================
# Audio tests
# ==========================================================================


class TestAudio:
    def test_list_devices_doesnt_crash(self):
        from friday_v4.voice.audio import list_input_devices, list_output_devices
        inputs = list_input_devices()
        outputs = list_output_devices()
        assert isinstance(inputs, list)
        assert isinstance(outputs, list)

    def test_audio_stream_init(self):
        from friday_v4.voice.audio import AudioStream
        stream = AudioStream()
        assert stream.is_active is False


# ==========================================================================
# Router tests
# ==========================================================================


class TestRouter:
    def test_router_init(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline)
        assert router.route("") == ""

    def test_router_greeting_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("hello")
        assert len(response) > 10

    def test_router_status_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        assert len(router.route("what's new")) > 0

    def test_router_identity_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("who are you")
        assert "friday" in response.lower()

    def test_router_desktop_command_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("switch to workspace 3")
        assert response is not None

    @patch("friday_v4.desktop.wm_abstraction.WindowManager")
    def test_router_launch_app_command(self, mock_wm_cls):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        wm = MagicMock()
        wm.is_available = True
        wm.focus_smart.return_value = None
        wm.launch_app.return_value = True
        mock_wm_cls.return_value = wm

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("launch spotify")

        wm.focus_smart.assert_called_once()
        wm.launch_app.assert_called_once()
        assert "spotify" in response.lower()

    @patch("friday_v4.desktop.wm_abstraction.WindowManager")
    def test_router_launch_focuses_if_running(self, mock_wm_cls):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        wm = MagicMock()
        wm.is_available = True
        wm.focus_smart.return_value = "spotify"
        mock_wm_cls.return_value = wm

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("launch spotify")

        wm.launch_app.assert_not_called()
        assert "focused" in response.lower()


# ==========================================================================
# Proactive integration
# ==========================================================================


class TestProactiveIntegration:
    def test_proactive_notify_graceful_when_disabled(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        assert router.proactive_notify() is None
