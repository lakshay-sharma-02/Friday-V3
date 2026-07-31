# Voice Wave 2.0 — Rebuild Design

> **Status:** In development
> **Date:** 2026-07-31
> **Supersedes:** VOICE_SPEC.md (original Phase 1 design) for implementation decisions.
> **Why:** The first voice wave shipped a torch-based stack that was too heavy and
> inconsistent for this machine. This rebuild is **zero-torch, ONNX/ctranslate2
> first**, hardware-optimized, and architected for clean extensibility.

---

## 1. Hardware Reality Check

This machine dictates every choice in this design:

| Resource | Value | Consequence |
|----------|-------|-------------|
| CPU | AMD A9-9425, **2 cores** | No LLM TTS (Orpheus 3B, XTTS) on CPU. ONNX + int8 everywhere. |
| RAM | **3.2 GB total** | torch is out. `faster-whisper int8`, `kokoro-onnx`, `openwakeword` all fit. |
| Audio | PipeWire/PulseAudio, libportaudio present, 1 real mic | `sounddevice` (pure ctypes over libportaudio) — no compile needed. |
| Disk | 44 GB free | Model cache budget ~2 GB. |
| OS | Linux (Hyprland), ZCode AppImage env quirk | venv must be built with `env -u APPIMAGE -u APPDIR`. |

**Golden rule: no `torch`, no `transformers`, no `TTS` (Coqui) at runtime.**
The old stack pulled torch (~2 GB, 14-30 s import) just to run Kokoro. This
rebuild runs the same model quality through ONNX at a fraction of the cost.

---

## 2. Provider Matrix (v2)

| Stage | Primary (local) | Secondary | Emergency |
|-------|-----------------|-----------|-----------|
| **TTS** | `kokoro-onnx` (54 voices, fast CPU, Apache-2.0) | `edge-tts` (internet, en-IE-EmilyNeural ≈ FRIDAY) | `pyttsx3` (system) |
| **STT** | `faster-whisper` (`base.en`, int8, ctranslate2) | `whisper.cpp` (subprocess) | `speech_recognition` |
| **Hotword** | `openwakeword` (`hey_jarvis` → "hey friday") | — | energy detection |
| **VAD** | Silero VAD (onnxruntime, mode 3) | WebRTC VAD (mode 1/2) | energy threshold |
| **Audio I/O** | `sounddevice` (libportaudio) | `pyaudio` | — |

### Why these?
- **kokoro-onnx** — same Kokoro-82M quality as the old torch build, but runs on
  `onnxruntime` (no torch). ~10-30× realtime on CPU, Apache-2.0, 54 voices,
  `af_bella` warm voice / `am_michael` briefing / `am_adam` alert map.
- **edge-tts** — free, high quality, zero local cost. `en-IE-EmilyNeural` is an
  Irish female voice — the closest free match to FRIDAY (Kerry Condon). Requires
  internet; auto-selected when `kokoro` isn't loaded yet.
- **faster-whisper** — CTranslate2 `int8` halves memory vs torch whisper, has
  built-in Silero VAD. `base.en` ≈ 400 MB RAM. `tiny.en` for constrained mode.
- **openwakeword** — Apache-2.0, no API key (old spec said Porcupine + key; this
  is strictly better). `hey_jarvis` model is the closest prebuilt phrase.
- **sounddevice** — pure-ctypes bindings to the already-present `libportaudio.so`;
  no portaudio-dev compile, no pyaudio wheel pain.

---

## 3. Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                        VoiceRouter                            │
│   desktop commands → proactive engine → V3 IdentityEngine     │
│   → fallback                                                  │
└──────────────────────────┬────────────────────────────────────┘
                           │ text-in / text-out
┌──────────────────────────▼────────────────────────────────────┐
│                      VoicePipeline (state machine)            │
│   IDLE → HOTWORD → LISTENING → PROCESSING → SPEAKING          │
│   • interruption handling (VAD barge-in)                      │
│   • anti-echo refractory window                               │
│   • silence / max-duration timers                             │
└──┬──────────┬───────────┬─────────────┬───────────┬──────────┘
   │          │           │             │           │
┌──▼───┐  ┌───▼────┐  ┌───▼────┐  ┌─────▼────┐  ┌───▼────┐
│ audio│  │  vad   │  │ hotword│  │   stt    │  │  tts   │
│      │  │        │  │        │  │          │  │        │
│ sound│  │ silero │  │ open-  │  │ faster-  │  │ kokoro-│
│device│  │ /webrtc│  │ wake-  │  │ whisper  │  │ onnx   │
│      │  │        │  │ word   │  │ (int8)   │  │ edge   │
└──────┘  └────────┘  └────────┘  └──────────┘  └────────┘
```

### Design principles (vs v1)
1. **One provider per module.** `tts.py` was 1081 lines. Now each provider is a
   small class behind a common interface; the facade (`TextToSpeech`,
   `SpeechToText`, `HotwordDetector`, `VoiceActivityDetector`, `AudioStream`)
   owns auto-selection + fallback.
2. **Lazy + async loading.** Heavy models (kokoro, faster-whisper) load in a
   background thread; `start()` never blocks on downloads. `voice status` shows
   `loading… / available / unavailable`.
3. **Never crash.** Every provider import and call is wrapped; failures degrade
   to the next provider, never to a traceback.
4. **Config-first.** `~/.friday/v4_config.json` controls model sizes, voices,
   thresholds. Env vars `FRIDAY_STT_MODEL`, `FRIDAY_TTS_PROVIDER` override.

---

## 4. Module Map (v2)

```
src/friday_v4/voice/
├── __init__.py        # public API surface
├── audio.py           # AudioStream, device listing (sounddevice)
├── vad.py             # SileroVAD / WebRTCVAD / VoiceActivityDetector
├── hotword.py         # OpenWakeWordProvider / EnergyDetector / HotwordDetector
├── stt.py             # FasterWhisperProvider / WhisperCPP / SpeechRecognition / SpeechToText
├── tts.py             # KokoroONNXProvider / EdgeTTS / PyTTS / TextToSpeech
├── chimes.py          # audio cues (listen/done/alert/error/think) + playback
├── pipeline.py        # VoicePipeline state machine
├── router.py          # VoiceRouter (desktop → proactive → V3 → fallback)
└── utils.py           # pure-python WAV I/O (no soundfile dependency)
```

### Key interfaces

```python
# audio.py
class AudioStream:
    def start(self, callback: Callable[[np.ndarray], None]) -> bool
    def stop(self)
    @property is_active

# tts.py
class TextToSpeech:
    def speak(self, text, mode=None) -> bool       # non-blocking, interruptible
    def stop(self)
    @property active_provider_name
    @property is_available
    def list_providers() -> list[dict]

# stt.py
class SpeechToText:
    def transcribe(self, audio: np.ndarray, sample_rate=16000) -> STTResult
    @property is_available

# pipeline.py
class VoicePipeline:
    def start() -> bool
    def stop()
    route_function: Callable[[str], str]
    on_transcription / on_state_change / on_error
```

---

## 5. CLI Surface (unchanged UX, better internals)

```
friday talk                          # hotword session
friday talk --push-to-talk
friday talk --tts-provider kokoro|edge|pyttsx3
friday talk --silero-vad
friday talk --no-chimes
friday voice setup / status / test
```

`cli_desktop.py` still spawns `python -m friday_v4.cli_talk talk` (module path
unchanged), and the integrated `friday` CLI keeps `talk/voice/desktop/proactive`.

---

## 6. Voice Modes & Chimes

Voice modes adapt tone: `CONVERSATION` (af_bella), `BRIEFING` (>200 chars or
"here's…", am_michael), `ALERT` (vulnerability/security/critical keywords,
am_adam), `WHISPER` (23:00-07:00, af_heart). Chimes: listen (double-chime),
done (single), alert (sharp), error (descending), think (quiet hum) — generated
in pure Python, no asset files.

---

## 7. Config (v2 defaults)

```json
{
  "voice": {
    "enabled": true,
    "stt_model": "base.en",
    "tts_provider": "kokoro",
    "hotword": "hey friday",
    "hotword_sensitivity": 0.7,
    "vad_mode": 1,
    "silence_timeout_seconds": 2.0,
    "max_utterance_seconds": 30.0,
    "enable_chimes": true,
    "push_to_talk_key": "ctrl+shift+space"
  }
}
```

---

## 8. Testing Strategy

- Unit tests mock providers; assert facade fallback logic, WAV roundtrip, chime
  generation, auto voice-mode, state transitions, router routing.
- `tests/test_voice.py` recreated for the new API (the old one was archived).
- Integration smoke test (`friday voice status`) confirms provider discovery
  without hardware.
- Real mic round-trip is a manual step (requires hardware).

---

## 9. What's intentionally out of scope (v2)

- Voice cloning (XTTS/Orpheus) — needs 4-6 GB RAM; revisit when a GPU or bigger
  machine exists. The `~/.friday/voices/friday.wav` sample is preserved for that
  future work.
- Streaming/word-by-word TTS — sentence-level is enough for v2.
- Mobile push-to-talk — that's the mobile wave.

*This document supersedes the v1 implementation choices while keeping the
VOICE_SPEC.md and VOICE_EXPERIENCE.md product goals intact.*
