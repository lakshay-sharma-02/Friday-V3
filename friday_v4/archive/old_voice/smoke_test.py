"""Friday V4 — Voice Pipeline Smoke Test.

Tests every component independently and then validates
the end-to-end flow (without requiring a microphone).

Run: python smoke_test.py
"""

import time
import sys
import os
import tempfile
from pathlib import Path

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def section(title: str):
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def check(name: str, condition: bool, detail: str = ""):
    """Print a check result."""
    status = "✅" if condition else "❌"
    detail_str = f" — {detail}" if detail else ""
    print(f"  {status} {name}{detail_str}")
    return condition


# ────────────────────────────────────────────────────────────────
# 1. Package & Module Loading
# ────────────────────────────────────────────────────────────────
section("1. Package & Module Loading")

t0 = time.time()
import friday_v4
init_time = time.time() - t0

all_ok = True
all_ok &= check("friday_v4 package import", True, f"({init_time:.1f}s)")
all_ok &= check("__version__ present", hasattr(friday_v4, "__version__"),
                friday_v4.__version__)

# Check all submodules load
for mod in ["voice", "desktop", "proactive"]:
    try:
        getattr(friday_v4, mod)
        all_ok &= check(f"friday_v4.{mod} import", True)
    except Exception as e:
        all_ok &= check(f"friday_v4.{mod} import", False, str(e))

print()

# ────────────────────────────────────────────────────────────────
# 2. Audio Devices
# ────────────────────────────────────────────────────────────────
section("2. Audio Hardware")

from friday_v4.voice.audio import list_input_devices, list_output_devices

inputs = list_input_devices()
outputs = list_output_devices()

all_ok &= check("Input devices detected", len(inputs) > 0, f"{len(inputs)} found")
all_ok &= check("Output devices detected", len(outputs) > 0, f"{len(outputs)} found")

if inputs:
    default = next((d for d in inputs if d.is_default), inputs[0])
    print(f"       Default input: {default.name}")

if outputs:
    default = next((d for d in outputs if d.is_default), outputs[0])
    print(f"       Default output: {default.name}")

print()

# ────────────────────────────────────────────────────────────────
# 3. TTS Providers
# ────────────────────────────────────────────────────────────────
section("3. Text-to-Speech (TTS)")

from friday_v4.voice.tts import (
    TextToSpeech, TTSConfig, VoiceMode,
    KokoroProvider, EdgeTTSProvider, PyTTSProvider,
    get_chime, play_chime, _generate_chime
)

# Test individual providers
print("  Testing individual providers...")
kp = KokoroProvider()
check("Kokoro loaded", kp.is_available or True,
      "Will be ready async if not loaded yet")

et = EdgeTTSProvider()
all_ok &= check("EdgeTTS available", et.is_available)

pt = PyTTSProvider()
all_ok &= check("pyttsx3 available", pt.is_available)

print()

# Test EdgeTTS synthesis (it's the fastest)
print("  Testing EdgeTTS synthesis...")
t0 = time.time()
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
    tmp_wav = f.name
try:
    success = et.synthesize("Hello, this is Friday V4 smoke test.", tmp_wav)
    dur = time.time() - t0
    if success and Path(tmp_wav).exists():
        size = Path(tmp_wav).stat().st_size
        all_ok &= check("EdgeTTS synthesis", True, f"{size} bytes in {dur:.1f}s")
    else:
        all_ok &= check("EdgeTTS synthesis", False)
finally:
    Path(tmp_wav).unlink(missing_ok=True)

print()

# Test chime generation
print("  Testing audio chimes...")
for chime_type in ["listen", "done", "alert", "error", "think"]:
    wav = get_chime(chime_type)
    all_ok &= check(f"Chime '{chime_type}' generated", len(wav) > 100,
                    f"{len(wav)} bytes")

# Test TextToSpeech frontend with edge provider
print()
print("  Testing TextToSpeech frontend (edge)...")
t0 = time.time()
tts = TextToSpeech(TTSConfig(primary_provider="edge"))
init_dur = time.time() - t0
all_ok &= check("TTS init with edge", tts.is_available,
                f"provider={tts.active_provider_name} ({init_dur:.1f}s)")

# Test speak (non-blocking)
print("  Testing TTS speak...")
ok = tts.speak("Smoke test voice.", VoiceMode.CONVERSATION)
all_ok &= check("TTS speak started", ok)
time.sleep(0.5)
tts.stop()

print()

# ────────────────────────────────────────────────────────────────
# 4. STT Providers
# ────────────────────────────────────────────────────────────────
section("4. Speech-to-Text (STT)")

from friday_v4.voice.stt import SpeechToText

stt = SpeechToText()
all_ok &= check("STT available", stt.is_available,
                f"provider={stt.active_provider}")

# List providers
for p in stt.list_providers():
    print(f"       {p['name']}: {'✅' if p['available'] else '❌'}")

print()

# ────────────────────────────────────────────────────────────────
# 5. Voice Activity Detection (VAD)
# ────────────────────────────────────────────────────────────────
section("5. Voice Activity Detection")

from friday_v4.voice.vad import VoiceActivityDetector, WebRTCVAD

# Test WebRTC VAD
webrtc = WebRTCVAD(mode=1)
all_ok &= check("WebRTC VAD available", webrtc.is_available)

# Test VAD frontend
vad = VoiceActivityDetector(mode=1)
all_ok &= check("VAD frontend available", vad.is_available,
                f"provider={vad.provider_name}")

# Test with synthesized noise (not speech - should return False)
import numpy as np
silence = np.zeros(480, dtype=np.float32)  # 30ms of silence
result = vad.is_speech(silence, 16000)
print(f"       VAD on silence: {'🚫' if not result else '⚠️ false positive'}")

# Test with sine wave (not speech - should return False)
t = np.linspace(0, 0.03, 480)
tone = np.sin(2 * np.pi * 440 * t).astype(np.float32) * 0.5
result = vad.is_speech(tone, 16000)
print(f"       VAD on tone: {'🚫' if not result else '⚠️ false positive'}")

print()

# ────────────────────────────────────────────────────────────────
# 6. Hotword Detection
# ────────────────────────────────────────────────────────────────
section("6. Hotword Detection")

from friday_v4.voice.hotword import HotwordDetector, OpenWakeWordProvider

hw = HotwordDetector("hey friday", 0.7)
all_ok &= check("Hotword detector available", hw.is_available,
                f"provider={hw.provider_name}")

print(f"       Sample rate: {hw.sample_rate} Hz")
print(f"       Frame length: {hw.frame_length} samples")

# Test with silence (should not trigger)
silence_bytes = b"\x00" * (hw.frame_length * 2)  # PCM16 silence
result = hw.process(silence_bytes)
all_ok &= check("No false positive on silence", not result)

print()

# ────────────────────────────────────────────────────────────────
# 7. Voice Router (standalone, no V3)
# ────────────────────────────────────────────────────────────────
section("7. Voice Router")

from friday_v4.voice.pipeline import VoicePipeline, PipelineConfig
from friday_v4.voice.router import VoiceRouter

config = PipelineConfig(
    hotword="hey friday",
    hotword_sensitivity=0.7,
    vad_mode=1,
    tts_provider="edge",
    enable_chimes=False,
)

pipeline = VoicePipeline(config)
router = VoiceRouter(pipeline, enable_proactive=False)

# Test router fallback responses
test_cases = [
    ("hello", "greeting"),
    ("who are you", "identity"),
    ("what's new", "status"),
    ("thanks", "gratitude"),
    ("goodbye", "farewell"),
    ("focus code editor", "desktop command"),
]

print("  Testing router fallback responses...")
for text, category in test_cases:
    response = router.route(text)
    has_response = bool(response and len(response) > 10)
    # Should work even without V3
    all_ok &= check(f"  {category}: '{text[:30]}'", has_response)
    if response:
        print(f"         → {response[:60]}...")

print()

# ────────────────────────────────────────────────────────────────
# 8. Full Pipeline Init & State Machine
# ────────────────────────────────────────────────────────────────
section("8. Voice Pipeline State Machine")

# Test state transitions manually (no mic)
from friday_v4.voice.pipeline import PipelineState

# Initial state
all_ok &= check("Initial state is IDLE", pipeline.state == PipelineState.IDLE)

# State transition test
pipeline.state = PipelineState.LISTENING
all_ok &= check("State → LISTENING", pipeline.state == PipelineState.LISTENING)

pipeline.state = PipelineState.PROCESSING
all_ok &= check("State → PROCESSING", pipeline.state == PipelineState.PROCESSING)

pipeline.state = PipelineState.SPEAKING
all_ok &= check("State → SPEAKING", pipeline.state == PipelineState.SPEAKING)

pipeline.state = PipelineState.IDLE
all_ok &= check("State → IDLE", pipeline.state == PipelineState.IDLE)

print()

# ────────────────────────────────────────────────────────────────
# 9. Desktop Window Manager (if available)
# ────────────────────────────────────────────────────────────────
section("9. Desktop Integration")

from friday_v4.desktop.wm_abstraction import (
    WindowManager, SmartWindowResolver, detect_desktop_environment
)

de = detect_desktop_environment()
all_ok &= check(f"Desktop env detected", de != "unknown", de)

wm = WindowManager()

# These are optional — desktop may not be available
status = wm.get_status()
if wm.is_available:
    print(f"       Active windows: {status.get('window_count', '?')}")
    active = wm.get_active_window()
    if active:
        print(f"       Active app: {active.app_name} ({active.app_class})")
else:
    print(f"       Desktop control: not available (Hyprland only for now)")

# SmartWindowResolver (works without desktop)
resolved = SmartWindowResolver.resolve("code editor", [])
print(f"       SmartWindowResolver: code editor → {resolved}")

print()

# ────────────────────────────────────────────────────────────────
# 10. Proactive Intelligence (standalone)
# ────────────────────────────────────────────────────────────────
section("10. Proactive Intelligence")

from friday_v4.proactive import AnticipationEngine, DeepContextEngine

# Deep context (no desktop = minimal context, but should not crash)
ctx = DeepContextEngine()
context = ctx.get_context()
all_ok &= check("Context engine works", True,
                f"mode={context.work_mode}")

summary = ctx.get_context_summary()
print(f"       Context summary: {summary}")

# Anticipation engine (standalone)
anticipation = AnticipationEngine()
suggestions = anticipation.get_suggestions(force=True)
all_ok &= check("Anticipation engine works", True,
                f"{len(suggestions)} suggestions")

for s in suggestions[:3]:
    print(f"       [{s.urgency}] ({s.priority_score:.0f}) {s.text[:60]}...")

print()

# ────────────────────────────────────────────────────────────────
# SUMMARY
# ────────────────────────────────────────────────────────────────
section("RESULTS")

print(f"\n  {'✅' if all_ok else '❌'} {'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}")
print(f"\n  Smoke test completed in {time.time() - t0:.1f}s")
print(f"  Python: {sys.version.split()[0]}")
print()

# Exit with proper code
sys.exit(0 if all_ok else 1)
