"""Voice Interface Layer — Speech-to-text, text-to-speech, hotword detection.

Voice Wave 2.0 rebuild: zero-torch, ONNX/ctranslate2 first, hardware
optimized for a 2-core / 3.2 GB RAM machine.

Architecture:
    Microphone → VAD → Hotword Detection → STT → VoiceRouter → TTS
"""

from .audio import AudioStream, list_input_devices, list_output_devices
from .hotword import HotwordDetector
from .pipeline import VoicePipeline, PipelineState, PipelineConfig
from .router import VoiceRouter
from .stt import SpeechToText, STTResult
from .tts import TextToSpeech, TTSConfig, VoiceMode, auto_voice_mode
from .vad import VoiceActivityDetector
from .chimes import get_chime, play_chime

__all__ = [
    "AudioStream",
    "list_input_devices",
    "list_output_devices",
    "HotwordDetector",
    "VoicePipeline",
    "PipelineState",
    "PipelineConfig",
    "VoiceRouter",
    "SpeechToText",
    "STTResult",
    "TextToSpeech",
    "TTSConfig",
    "VoiceMode",
    "auto_voice_mode",
    "VoiceActivityDetector",
    "get_chime",
    "play_chime",
]
