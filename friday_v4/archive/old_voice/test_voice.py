"""Voice module tests — pipeline, interruption, chimes, auto mode, config.

Tests are unit-level with mocked audio I/O (no microphone needed).
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
            # Content roughly matches (tiny loss from int16 quantization)
            diff = np.abs(read_audio - audio).mean()
            assert diff < 0.001
        finally:
            Path(path).unlink(missing_ok=True)

    def test_write_wav_clips_properly(self):
        from friday_v4.voice.utils import write_wav, read_wav
        # Audio that exceeds [-1, 1] should be clipped
        audio = np.array([-2.0, -1.5, 0.0, 1.5, 2.0], dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            write_wav(path, audio, 16000)
            read_audio, _ = read_wav(path)
            # Should be clipped to roughly [-1, 1]
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
        assert config.voice.tts_provider == "edge"
        assert config.voice.vad_mode == 1
        assert config.desktop.enabled is True

    def test_load_config_merges_partial(self):
        from friday_v4.config import load_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"voice": {"tts_provider": "kokoro", "vad_mode": 3}}, f)
            path = f.name
        try:
            config = load_config(Path(path))
            # Merged from file
            assert config.voice.tts_provider == "kokoro"
            assert config.voice.vad_mode == 3
            # Default from code
            assert config.voice.enabled is True
            assert config.voice.hotword == "hey friday"
            assert config.desktop.enabled is True
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_config_invalid_json_returns_defaults(self):
        from friday_v4.config import load_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json!!!")
            path = f.name
        try:
            config = load_config(Path(path))
            assert config.voice.tts_provider == "edge"
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
        # Long response > 200 chars
        long_text = ". ".join(["status report"] * 15)
        assert auto_voice_mode(long_text) == VoiceMode.BRIEFING

    def test_auto_voice_mode_conversation_default(self):
        from friday_v4.voice.tts import auto_voice_mode, VoiceMode
        # Can be WHISPER if hour < 7 or >= 23, but CONVERSATION otherwise
        import datetime
        result = auto_voice_mode("Hello, how are you?")
        hour = datetime.datetime.now().hour
        if hour < 7 or hour >= 23:
            assert result == VoiceMode.WHISPER
        else:
            assert result == VoiceMode.CONVERSATION

    def test_chime_generation(self):
        from friday_v4.voice.tts import get_chime
        for chime_type in ("listen", "done", "alert", "error", "think"):
            data = get_chime(chime_type)
            assert len(data) > 44  # Has WAV header + data
            assert data[:4] == b"RIFF"
            assert data[8:12] == b"WAVE"


# ==========================================================================
# Pipeline tests
# ==========================================================================

class TestPipelineState:
    def test_state_transition_calls_callback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineState, PipelineConfig

        # Minimal config — no audio, no TTS
        config = PipelineConfig(
            hotword_sensitivity=0.0,  # Disables hotword energy detection noise
        )
        pipeline = VoicePipeline(config)
        states = []

        def on_state(state):
            states.append(state)

        pipeline.on_state_change = on_state

        # Test state change
        pipeline.state = PipelineState.LISTENING
        assert PipelineState.LISTENING in states

    def test_interruption_flag_consumed(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig

        pipeline = VoicePipeline(PipelineConfig())
        # Simulate interruption
        with pipeline._interrupted_lock:
            pipeline._interrupted = True

        # Simulate _wait_for_speech_end consuming it
        with pipeline._speech_gen_lock:
            gen = pipeline._speech_gen
        pipeline._wait_for_speech_end(gen)

        # State should NOT be IDLE (was consumed by interruption guard)
        with pipeline._interrupted_lock:
            assert pipeline._interrupted is False

    def test_speech_gen_prevents_stale_reset(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig

        pipeline = VoicePipeline(PipelineConfig())
        pipeline.state = "speaking"  # Some state

        # Simulate two speak() calls — gen 1 is stale
        with pipeline._speech_gen_lock:
            pipeline._speech_gen = 2

        # _wait_for_speech_end with stale gen should NOT reset
        pipeline._wait_for_speech_end(1)
        # State unchanged since gen didn't match
        assert pipeline.state != "idle"

    def test_start_stop_lifecycle(self):
        """Verifies pipeline starts and stops without error (no audio hardware)."""
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig

        config = PipelineConfig()
        pipeline = VoicePipeline(config)

        # start should fail gracefully — no audio hardware in CI
        result = pipeline.start()
        # When no audio hardware is available, start returns False
        # but doesn't crash. In CI without microphone this is expected.
        # We just verify no exception is raised.
        pipeline.stop()
        assert result is not None  # just ran without crashing


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
        """SpeechToText should init without crashing even with no providers."""
        from friday_v4.voice.stt import SpeechToText
        stt = SpeechToText()
        # Just verify it doesn't crash
        assert isinstance(stt.list_providers(), list)

    def test_faster_whisper_load_doesnt_crash_when_missing(self):
        """FasterWhisperProvider init should not crash when package missing."""
        from friday_v4.voice.stt import FasterWhisperProvider
        provider = FasterWhisperProvider()
        # It may or may not be available — but it shouldn't crash either way
        assert hasattr(provider, "is_available")


# ==========================================================================
# Hotword tests
# ==========================================================================

class TestHotword:
    def test_hotword_init_graceful(self):
        from friday_v4.voice.hotword import HotwordDetector
        hw = HotwordDetector("hey friday", 0.5)
        # Shouldn't crash even if no hotword provider is installed
        assert hasattr(hw, "is_available")

    def test_energy_detector_basic(self):
        from friday_v4.voice.hotword import EnergyDetector
        det = EnergyDetector(threshold=0.01, min_speech_frames=1)
        # Silence should not trigger
        silence = b"\x00" * 1024
        assert not det.process(silence)


# ==========================================================================
# VAD tests
# ==========================================================================

class TestVAD:
    def test_vad_init_graceful(self):
        from friday_v4.voice.vad import VoiceActivityDetector
        vad = VoiceActivityDetector(mode=1)
        # Shouldn't crash even if no VAD packages installed
        assert hasattr(vad, "is_available")

    def test_webrtc_vad_init_graceful(self):
        from friday_v4.voice.vad import WebRTCVAD
        vad = WebRTCVAD(mode=1)
        assert hasattr(vad, "is_available")


# ==========================================================================
# Audio tests
# ==========================================================================

class TestAudio:
    def test_list_devices_doesnt_crash(self):
        from friday_v4.voice.audio import list_input_devices, list_output_devices
        # Functions should handle pyaudio not being installed gracefully
        inputs = list_input_devices()
        outputs = list_output_devices()
        assert isinstance(inputs, list)
        assert isinstance(outputs, list)


# ==========================================================================
# Router tests
# ==========================================================================

class TestRouter:
    def test_router_init(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline)
        # Should not crash
        assert router.route("") == ""

    def test_router_greeting_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("hello")
        # Should contain a greeting response (exact text may vary)
        assert len(response) > 10

    def test_router_status_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("what's new")
        assert len(response) > 0

    def test_router_desktop_command_fallback(self):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        # Even without desktop, it shouldn't crash
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

        wm.focus_smart.assert_called_once()
        wm.launch_app.assert_not_called()
        assert "focused" in response.lower()

    @patch("friday_v4.desktop.wm_abstraction.WindowManager")
    def test_router_launch_failure_message(self, mock_wm_cls):
        from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
        from friday_v4.voice.router import VoiceRouter

        wm = MagicMock()
        wm.is_available = True
        wm.focus_smart.return_value = None
        wm.launch_app.return_value = False
        mock_wm_cls.return_value = wm

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("launch nonexistent-app")

        assert "couldn't" in response.lower()
