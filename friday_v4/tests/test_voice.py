"""Voice module tests — Voice Wave 2.0.

Unit-level tests with mocked audio I/O (no microphone needed). Covers the
new provider-facade architecture: WAV utils, config, TTS auto-mode, chimes,
pipeline state, STT, hotword, VAD, audio device listing, and router routing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

# ==========================================================================
# Utils tests
# ==========================================================================


class TestUtils:
    def test_write_and_read_wav_roundtrip(self):
        from friday_v4.voice.utils import read_wav, write_wav
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
        from friday_v4.voice.utils import read_wav, write_wav
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
        assert config.voice.tts_provider == "piper"
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
            assert config.voice.tts_provider == "piper"
        finally:
            Path(path).unlink(missing_ok=True)

    def test_write_default_config_matches_dataclass(self, tmp_path):
        """Regression: write_default_config must agree with DesktopConfig
        (system_tray True) — previously drifted to False."""
        from friday_v4.config import write_default_config
        path = tmp_path / "v4_config.json"
        write_default_config(path)
        data = json.loads(path.read_text())
        assert data["desktop"]["system_tray"] is True
        assert data["voice"]["tts_provider"] == "piper"


# ==========================================================================
# TTS tests
# ==========================================================================


class TestTTSUtils:
    def test_auto_voice_mode_alert(self):
        from friday_v4.voice.tts import VoiceMode, auto_voice_mode
        assert auto_voice_mode("Critical vulnerability found") == VoiceMode.ALERT
        assert auto_voice_mode("Security breach detected in deps") == VoiceMode.ALERT
        assert auto_voice_mode("CVE-2024-1234 affects your package") == VoiceMode.ALERT

    def test_auto_voice_mode_briefing(self):
        from friday_v4.voice.tts import VoiceMode, auto_voice_mode
        long_text = ". ".join(["status report"] * 15)
        assert auto_voice_mode(long_text) == VoiceMode.BRIEFING

    def test_auto_voice_mode_conversation_default(self):
        import datetime

        from friday_v4.voice.tts import VoiceMode, auto_voice_mode
        result = auto_voice_mode("Hello, how are you?")
        hour = datetime.datetime.now().hour
        if hour < 7 or hour >= 23:
            assert result == VoiceMode.WHISPER
        else:
            assert result == VoiceMode.CONVERSATION

    def test_split_sentences_short_text_single_chunk(self):
        from friday_v4.voice.tts import TextToSpeech
        tts = TextToSpeech.__new__(TextToSpeech)
        assert tts._split_sentences("Hello.") == ["Hello."]
        assert tts._split_sentences("") == []

    def test_split_sentences_caps_at_12_words(self):
        from friday_v4.voice.tts import TextToSpeech
        tts = TextToSpeech.__new__(TextToSpeech)
        long = "word " * 30 + "end."
        chunks = tts._split_sentences(long)
        assert len(chunks) >= 2
        assert all(len(c.split()) <= 12 for c in chunks)

    def test_split_sentences_breaks_on_commas(self):
        from friday_v4.voice.tts import TextToSpeech
        tts = TextToSpeech.__new__(TextToSpeech)
        text = ("The server load is currently at forty percent across "
                "all three instances, and I have rotated the credentials "
                "as you asked.")
        chunks = tts._split_sentences(text)
        assert len(chunks) >= 2

    def test_auto_provider_prefers_piper(self):
        """Auto → piper first, then edge, then kokoro."""
        from friday_v4.voice.tts import TextToSpeech
        tts = TextToSpeech.__new__(TextToSpeech)
        tts.config = type("C", (), {"primary_provider": "auto", "speed": 1.0})()
        tts._providers = []
        tts._current_provider = None
        names = TextToSpeech._init_providers(tts)
        assert names.index("piper") < names.index("edge") < names.index("kokoro")

    def test_provider_priority_order(self):
        """Default priority: piper > edge > kokoro > pyttsx3."""
        from friday_v4.voice.tts import TextToSpeech
        tts = TextToSpeech.__new__(TextToSpeech)
        tts.config = type("C", (), {"primary_provider": "piper", "speed": 1.0})()
        tts._providers = []
        tts._current_provider = None
        names = TextToSpeech._init_providers(tts)
        assert names == ["piper", "edge", "kokoro", "pyttsx3"]

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


class TestConfigFromFile:
    def test_missing_file_yields_defaults(self):
        """No config file → built-in defaults, never raises."""
        from friday_v4.voice.core import config_from_file
        cfg = config_from_file(Path("/nonexistent/v4_config.json"))
        assert cfg.hotword == "hey friday"
        assert cfg.tts_provider == "piper"
        assert cfg.vad_mode == 1

    def test_config_file_fields_wired(self, tmp_path):
        from friday_v4.voice.core import config_from_file
        p = tmp_path / "v4_config.json"
        p.write_text(json.dumps({
            "voice": {
                "hotword": "computer",
                "tts_provider": "edge",
                "vad_mode": 3,
                "silence_timeout_seconds": 1.5,
                "max_utterance_seconds": 20.0,
                "enable_chimes": False,
            }
        }))
        cfg = config_from_file(p)
        assert cfg.hotword == "computer"
        assert cfg.tts_provider == "edge"
        assert cfg.vad_mode == 3
        assert cfg.silence_timeout_seconds == 1.5
        assert cfg.max_utterance_seconds == 20.0
        assert cfg.enable_chimes is False

    def test_invalid_config_file_yields_defaults(self, tmp_path):
        from friday_v4.voice.core import config_from_file
        p = tmp_path / "v4_config.json"
        p.write_text("not json {{{")
        cfg = config_from_file(p)
        assert cfg.hotword == "hey friday"


class TestPipelineState:
    def test_state_transition_calls_callback(self):
        from friday_v4.voice.pipeline import PipelineConfig, PipelineState, VoicePipeline
        pipeline = VoicePipeline(PipelineConfig(hotword_sensitivity=0.0))
        states = []

        def on_state(s):
            states.append(s)

        pipeline.on_state_change = on_state
        pipeline.state = PipelineState.LISTENING
        assert PipelineState.LISTENING in states

    def test_start_stop_lifecycle(self):
        """Pipeline starts and stops without crashing (no mic in CI)."""
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
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

    def test_empty_keyword_disables_hotword(self):
        """Push-to-talk passes an empty keyword — the hotword must be
        fully disabled, never silently armed with a default model."""
        from friday_v4.voice.hotword import HotwordDetector
        hw = HotwordDetector("", 0.7)
        assert hw.is_available is False
        assert hw.provider_name == "none"
        assert hw.process(np.zeros(480, dtype=np.float32)) is False

    def test_whitespace_keyword_disables_hotword(self):
        from friday_v4.voice.hotword import HotwordDetector
        hw = HotwordDetector("   ", 0.7)
        assert hw.is_available is False
        assert hw.provider_name == "none"

    def test_openwakeword_empty_keyword_graceful(self):
        from friday_v4.voice.hotword import OpenWakeWordProvider
        p = OpenWakeWordProvider("", 0.7)
        assert p.is_available is False


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

    def test_silero_frame_audio_pads_480_to_512(self):
        """480-sample mic frames must be padded to whole 512-sample windows."""
        from friday_v4.voice.vad import SileroVAD
        vad = SileroVAD()
        out = vad._frame_audio(np.zeros(480, dtype=np.float32))
        assert out.shape == (1, 512)
        # 1440 = 2 whole 512-windows (1024); the 416-sample remainder is
        # dropped — it is handled by the next streamed frame.
        out2 = vad._frame_audio(np.zeros(1440, dtype=np.float32))
        assert out2.shape == (1, 1024)

    def test_silero_is_speech_short_audio_no_crash(self):
        from friday_v4.voice.vad import SileroVAD
        vad = SileroVAD()
        # Even with no model loaded, is_speech must not raise.
        assert vad.is_speech(np.zeros(480, dtype=np.float32)) is False
        assert vad.is_speech(np.array([], dtype=np.float32)) is False


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
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline)
        assert router.route("") == ""

    def test_router_greeting_fallback(self):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("hello")
        assert len(response) > 10

    def test_router_status_fallback(self):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        assert len(router.route("what's new")) > 0

    def test_router_identity_fallback(self):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("who are you")
        assert "friday" in response.lower()

    def test_router_desktop_command_fallback(self):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("switch to workspace 3")
        assert response is not None

    @patch("friday_v4.desktop.wm_abstraction.WindowManager")
    @patch("friday_v4.desktop.wm_abstraction.shutil.which")
    def test_router_launch_app_command(self, mock_which, mock_wm_cls):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        wm = MagicMock()
        wm.is_available = True
        wm.focus_smart.return_value = None
        wm.launch_app.return_value = True
        mock_wm_cls.return_value = wm
        mock_which.return_value = "/usr/bin/spotify"  # installed

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("launch spotify")

        wm.focus_smart.assert_called_once()
        wm.launch_app.assert_called_once()
        assert "spotify" in response.lower()

    def test_router_ask_reaches_reasoning(self):
        """ASK intents (action='chat') must reach the reasoning brain —
        the Wiring Law fix that made spoken questions get real answers
        instead of the canned fallback."""
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter

        from friday_v4.nl_router import TalkResult

        class _FakeHandler:
            def __init__(self, **kw):
                pass

            def handle(self, text, **kw):
                return TalkResult(text, "ask", "chat",
                                  response="You're Lakshay. You prefer "
                                           "Python for tooling.")

        with patch("friday_v4.nl_router.TextCommandHandler", _FakeHandler):
            pipeline = VoicePipeline(PipelineConfig())
            router = VoiceRouter(pipeline, enable_proactive=False)
            response = router.route("who am I?")

        assert "Lakshay" in response
        assert "prefer" in response

    def test_router_greeting_still_canned(self):
        """Non-ASK chat (greetings) keeps the canned fallback flavor."""
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("hello")
        assert "Friday" in response or "help" in response.lower()

    @patch("friday_v4.desktop.wm_abstraction.WindowManager")
    @patch("friday_v4.desktop.wm_abstraction.shutil.which")
    def test_router_launch_focuses_if_running(self, mock_which, mock_wm_cls):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        wm = MagicMock()
        wm.is_available = True
        wm.focus_smart.return_value = "spotify"
        mock_wm_cls.return_value = wm
        mock_which.return_value = "/usr/bin/spotify"  # installed

        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("launch spotify")

        wm.launch_app.assert_not_called()
        assert "focused" in response.lower()

    def test_router_persists_spoken_utterances_with_conn(self, tmp_path):
        """Spoken utterances must land in the conversation log when a DB
        conn is provided — the Wiring Law: voice is a first-class
        entrypoint into the brain (persona identity + conversation
        providers read what the operator *said*)."""
        from friday_v4 import db
        from friday_v4.persona import recent_statements
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter

        conn = db.connect(tmp_path / "v4.db")
        try:
            pipeline = VoicePipeline(PipelineConfig())
            router = VoiceRouter(pipeline, enable_proactive=False, conn=conn)
            router.route("call me Lakshay")
            stmts = recent_statements(conn, limit=5)
            # The router lowercases before routing, so the persisted
            # exchange is the lowercased utterance — the exact words the
            # brain received.
            assert any(s["content"] == "call me lakshay" for s in stmts)
        finally:
            conn.close()

    def test_router_no_conn_is_graceful(self, tmp_path):
        """Without a conn (hermetic tests / degraded mode) the router
        must still work and never touch the real conversation log."""
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        response = router.route("who are you")
        assert "friday" in response.lower()


# ==========================================================================
# Push-to-talk hotkey binding
# ==========================================================================


class TestPushToTalk:
    def test_bind_push_to_talk_with_keyboard(self):
        """With the keyboard lib present, press starts recording and
        release transcribes + routes the text."""
        from friday_v4.cli_talk import _bind_push_to_talk

        fake_kb = MagicMock()
        fake_kb.is_pressed.return_value = True
        with patch.dict(sys.modules, {"keyboard": fake_kb}):
            pipeline = MagicMock()
            pipeline.stop_recording_and_process.return_value = "hello friday"
            on_text = MagicMock()

            assert _bind_push_to_talk(pipeline, on_text) is True

            # handlers were registered for the base key
            press_handler = fake_kb.on_press_key.call_args[0][1]
            release_handler = fake_kb.on_release_key.call_args[0][1]

            press_handler(type("Ev", (), {"name": "space"})())
            pipeline.push_to_talk.assert_called_once()

            release_handler(type("Ev", (), {"name": "space"})())
            pipeline.stop_recording_and_process.assert_called_once()
            on_text.assert_called_once_with("hello friday")

    def test_bind_push_to_talk_without_keyboard(self):
        """Without the keyboard lib, binding fails gracefully (False)."""
        from friday_v4.cli_talk import _bind_push_to_talk

        with patch.dict(sys.modules, {"keyboard": None}):
            pipeline = MagicMock()
            assert _bind_push_to_talk(pipeline, lambda t: None) is False
            pipeline.push_to_talk.assert_not_called()

    def test_bind_push_to_talk_modifier_required(self):
        """Press without the modifier must not start recording."""
        from friday_v4.cli_talk import _bind_push_to_talk

        fake_kb = MagicMock()
        fake_kb.is_pressed.return_value = False  # ctrl not held
        with patch.dict(sys.modules, {"keyboard": fake_kb}):
            pipeline = MagicMock()
            assert _bind_push_to_talk(pipeline, lambda t: None) is True

            press_handler = fake_kb.on_press_key.call_args[0][1]
            press_handler(type("Ev", (), {"name": "space"})())
            pipeline.push_to_talk.assert_not_called()

    def test_bind_push_to_talk_custom_key(self):
        """--push-to-talk-key must register hooks on the configured base key
        and honor the extra modifier."""
        from friday_v4.cli_talk import _bind_push_to_talk

        fake_kb = MagicMock()
        fake_kb.is_pressed.return_value = True
        with patch.dict(sys.modules, {"keyboard": fake_kb}):
            pipeline = MagicMock()
            pipeline.stop_recording_and_process.return_value = "hi"
            on_text = MagicMock()

            assert _bind_push_to_talk(
                pipeline, on_text, key="ctrl+shift+m") is True
            # hooks registered for the base key, not the full combo
            assert fake_kb.on_press_key.call_args[0][0] == "m"
            assert fake_kb.on_release_key.call_args[0][0] == "m"

            press_handler = fake_kb.on_press_key.call_args[0][1]
            press_handler(type("Ev", (), {"name": "m"})())
            pipeline.push_to_talk.assert_called_once()

    def test_bind_push_to_talk_processes_when_ctrl_released_first(self):
        """Releasing Ctrl before Space must still transcribe (no lost audio)."""
        from friday_v4.cli_talk import _bind_push_to_talk

        fake_kb = MagicMock()
        fake_kb.is_pressed.return_value = True  # ctrl held at press
        with patch.dict(sys.modules, {"keyboard": fake_kb}):
            pipeline = MagicMock()
            pipeline.stop_recording_and_process.return_value = "hello friday"
            on_text = MagicMock()
            assert _bind_push_to_talk(pipeline, on_text) is True

            press_handler = fake_kb.on_press_key.call_args[0][1]
            release_handler = fake_kb.on_release_key.call_args[0][1]

            press_handler(type("Ev", (), {"name": "space"})())
            pipeline.push_to_talk.assert_called_once()

            # Ctrl released first — is_pressed now False, but the base key
            # release must still process the recording.
            fake_kb.is_pressed.return_value = False
            release_handler(type("Ev", (), {"name": "space"})())
            pipeline.stop_recording_and_process.assert_called_once()
            on_text.assert_called_once_with("hello friday")


# ==========================================================================
# Proactive integration
# ==========================================================================


class TestProactiveIntegration:
    def test_proactive_notify_graceful_when_disabled(self):
        from friday_v4.voice.pipeline import PipelineConfig, VoicePipeline
        from friday_v4.voice.router import VoiceRouter
        pipeline = VoicePipeline(PipelineConfig())
        router = VoiceRouter(pipeline, enable_proactive=False)
        assert router.proactive_notify() is None
