"""Voice Interface Layer — Speech-to-text, text-to-speech, hotword detection.

Transforms Friday from a text-only CLI tool to a voice-controlled AI partner.
Users can speak commands naturally ("Hey Friday, what's the status of my
projects?") and hear responses spoken aloud.

Architecture:
    Microphone → VAD → Hotword Detection → STT → Text
                                                      ↓
                                                Persona Engine
                                                      ↓
    Audio Output ← TTS ← Response Text ← IdentityEngine.process()
"""

from .pipeline import VoicePipeline, PipelineState, PipelineConfig
from .stt import SpeechToText, STTResult
from .tts import TextToSpeech, TTSConfig, VoiceMode, auto_voice_mode
from .hotword import HotwordDetector
from .vad import VoiceActivityDetector
from .router import VoiceRouter
from .audio import AudioStream, list_input_devices, list_output_devices

__all__ = [
    "VoicePipeline",
    "PipelineState",
    "PipelineConfig",
    "SpeechToText",
    "STTResult",
    "TextToSpeech",
    "TTSConfig",
    "VoiceMode",
    "auto_voice_mode",
    "HotwordDetector",
    "VoiceActivityDetector",
    "VoiceRouter",
    "AudioStream",
    "list_input_devices",
    "list_output_devices",
]
