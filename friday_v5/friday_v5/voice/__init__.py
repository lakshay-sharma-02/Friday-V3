"""Voice — the ears and mouth, 100% local.

V5 port of V4's voice stack (zero-torch, ONNX-first), de-coupled from
V4's config file and DB. Architecture:

    Microphone → VAD → Hotword → STT → VoiceRouter → TTS

The router (V5's own) sends transcriptions to the engine, which
routes to Claude; the reply is spoken back. Everything local —
audio never leaves the machine.
"""

from .audio import AudioStream, list_input_devices, list_output_devices
from .chimes import get_chime, play_chime
from .core import config_from_env
from .hotword import HotwordDetector
from .pipeline import PipelineConfig, PipelineState, VoicePipeline
from .router import VoiceRouter
from .stt import SpeechToText, STTResult
from .tts import TextToSpeech, TTSConfig, VoiceMode, auto_voice_mode
from .vad import VoiceActivityDetector

__all__ = [
    "AudioStream",
    "list_input_devices",
    "list_output_devices",
    "get_chime",
    "play_chime",
    "config_from_env",
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
]
