# Voice Interface Specification — Phase 1

> **Goal:** Talk to Friday like Tony Stark talks to FRIDAY.
> **Effort:** 2-3 weeks
> **Dependencies:** V3 persona engine, STT model, TTS model

---

## Overview

The voice interface is the most transformative V4 capability. It changes
Friday from a tool you type at to a partner you speak with.

### Success Looks Like

```
User:  "Hey Friday, what's the status of my projects?"
       [0.5s processing]
Friday:"3 repositories have changed since your last observation.
        codebuff has 12 new commits, vivaha has 3, and Aether
        has 1. I also noticed a new cross-project correlation
        between vivaha and MindWell — they both use Supabase."

User:  "Show me the vivaha changes."
Friday:[Opens browser to GitHub compare view]
       "I've opened the vivaha compare view. 3 commits:
       'fix login redirect', 'update deps', and 'add tests for auth'."
```

---

## Component Specifications

### 1. Speech-to-Text (STT)

**Primary:** Local Whisper (`whisper-small`)
**Fallback:** API-based (Deepgram)
**Config:** `~/.friday/v4_config.json`

```python
class SpeechToText:
    """Convert microphone audio to text.

    Model selection:
        whisper-tiny:    500MB RAM, ~1s latency, moderate accuracy (fallback)
        whisper-small:   2GB RAM, ~2s latency, high accuracy (default)
        whisper-medium:  5GB RAM, ~4s latency, very high accuracy
        deepgram:        API, ~0.5s latency, best accuracy (requires API key)
    """

    def __init__(self, model: str = "whisper-small", device: str = "cpu"):
        ...

    def transcribe(self, audio: bytes, sample_rate: int = 16000) -> str:
        """Transcribe audio bytes to text. Returns empty string on failure."""

    def transcribe_file(self, path: str) -> str:
        """Transcribe a WAV file (for testing)."""

    def is_available(self) -> bool:
        """Check if the STT model is loaded and ready."""

    @property
    def latency_ms(self) -> int:
        """Average transcription latency in milliseconds."""
```

**Integration:**
- Audio comes from VAD → Hotword → STT pipeline
- Text output feeds into `VoiceRouter` → `IdentityEngine.process()`
- Failure: STT unavailable → text fallback with error message

### 2. Text-to-Speech (TTS)

**Primary:** Local Piper
**Fallback:** `pyttsx3` (system TTS)
**Config:** `~/.friday/v4_config.json`

```python
class TextToSpeech:
    """Convert response text to spoken audio.

    Model selection:
        piper:         Local, fast, decent quality (default)
        pyttsx3:       System TTS (espeak on Linux, SAPI on Windows, NSSpeech on macOS)
        elevenlabs:    API, best quality, voice cloning (requires API key)
    """

    def __init__(self, model: str = "piper", voice: str = "default"):
        ...

    def speak(self, text: str) -> None:
        """Synthesize and play text through speakers. Fire-and-forget."""

    def speak_async(self, text: str, callback=None) -> None:
        """Synthesize and play in background thread. Calls callback on completion."""

    def synthesize(self, text: str, output_path: str) -> None:
        """Synthesize to WAV file without playing (for caching/preview)."""

    def stop(self) -> None:
        """Stop current speech immediately."""

    def is_speaking(self) -> bool:
        """Check if currently speaking."""

    @property
    def latency_ms(self) -> int:
        """Average synthesis latency in milliseconds."""
```

**Integration:**
- Response from `IdentityEngine.process()` → split into sentences → TTS
- Long responses are interruptible (new user input cancels current speech)
- Ambient notifications can trigger brief spoken alerts

### 3. Voice Activity Detection (VAD)

```python
class VoiceActivityDetector:
    """Detect when someone is speaking in an audio stream.

    Uses WebRTC VAD (silero VAD as fallback). Returns speech segments
    with timestamps for the STT pipeline.

    Modes:
        0: Disabled (always listening, highest CPU)
        1: Normal (WebRTC VAD, low CPU, moderate accuracy)
        2: Aggressive (higher threshold, less false positives)
        3: Silero VAD (ML-based, best accuracy, higher CPU)
    """

    def __init__(self, mode: int = 1, sample_rate: int = 16000):
        ...

    def process_frame(self, frame: bytes) -> bool:
        """Process a 30ms audio frame. Returns True if speech detected."""

    def get_speech_segments(self) -> list[tuple[float, float]]:
        """Returns [(start_sec, end_sec), ...] of detected speech."""

    def reset(self) -> None:
        """Reset VAD state (call between utterances)."""
```

### 4. Hotword Detection

```python
class HotwordDetector:
    """Wake word detection — "Hey Friday".

    Uses Porcupine (local, fast, ~200KB model file). Runs continuously
    on the audio stream. When hotword is detected, signals the pipeline
    to start recording the following utterance.

    Built-in keywords:
        - "hey friday" (primary)
        - "friday" (shorter variant)
        - Custom keywords via Porcupine's platform

    Sensitivity: 0.0 (least sensitive) to 1.0 (most sensitive).
    Default: 0.7
    """

    def __init__(self, keyword: str = "hey friday", sensitivity: float = 0.7):
        ...

    def process_frame(self, frame: bytes) -> bool:
        """Process a single audio frame. Returns True if hotword detected."""

    def set_sensitivity(self, sensitivity: float) -> None:
        """Adjust hotword sensitivity at runtime."""

    @property
    def is_wake_word_configured(self) -> bool:
        """Check if a wake word model is loaded."""
```

### 5. Voice Pipeline (Orchestrator)

```python
class VoicePipeline:
    """End-to-end voice interaction pipeline.

    This is the orchestrator that wires together:
        Microphone → VAD → Hotword Detection → STT → Persona Engine → TTS

    Runs in a background thread. The main thread continues unaffected.
    """

    def __init__(self,
                 stt_model: str = "whisper-small",
                 tts_model: str = "piper",
                 hotword: str = "hey friday",
                 vad_mode: int = 1):
        ...

    def start(self) -> None:
        """Start listening for hotword in background thread."""

    def stop(self) -> None:
        """Stop listening and release audio resources."""

    def push_to_talk(self, callback=None) -> None:
        """Start recording (for push-to-talk mode). Stops when user releases key."""

    def speak(self, text: str) -> None:
        """Say something aloud."""
```

### 6. Voice Router

```python
class VoiceRouter:
    """Routes voice input through V3's persona engine.

    Bridges the voice pipeline to V3's existing conversation handling.
    Automatically benefits from all V3 persona features:
    - Name learning
    - Preference extraction
    - Relationship depth modulation
    - Memory recall
    - ask/execute/chitchat routing
    """

    def __init__(self, identity_engine: IdentityEngine):
        ...

    def process_voice(self, text: str) -> str:
        """Process transcribed speech through the persona engine.

        Returns the response text ready for TTS.
        """

    @property
    def engine(self) -> IdentityEngine:
        """Access to the underlying V3 identity engine."""
```

---

## Microphone & Audio

### Requirements

- Sample rate: 16kHz (Whisper standard)
- Bit depth: 16-bit PCM
- Channels: Mono
- Frame size: 30ms (VAD standard), 512 samples

### Implementation

```python
class AudioStream:
    """Managed microphone stream with automatic device selection.

    Lists available input devices on init. Selects the first valid device,
    or a user-specified device from config. Handles device disconnection
    gracefully (attempts reconnection).
    """

    def __init__(self, device_index: int | None = None):
        ...

    def read_frame(self) -> bytes:
        """Read a single 30ms audio frame."""

    def close(self) -> None:
        """Release the microphone resource."""

    @staticmethod
    def list_devices() -> list[dict]:
        """List available input devices."""

    @property
    def sample_rate(self) -> int:
        return 16000
```

---

## Conversation Flow

### Hotword-Initiated Flow
```
[Silence] ──VAD── [Silence] ──Hotword── [SPEECH] ──VAD── [Silence]
                        │                    │                    │
                  "Hey Friday"          "What's new?"       End utterance
                        │                    │                    │
                        ▼                    ▼                    ▼
                  Wake signal           STT transcribe        Process
                  start recording       → "what's new"        response
                                                                │
                                                                ▼
                                                           TTS speak
```

### Push-to-Talk Flow
```
[Hold key] ── [SPEECH] ── [Release key]
     │           │              │
     ▼           ▼              ▼
 Start      STT transcribe   Process
recording   → command text   response → TTS
```

### Interruption Handling

If the user speaks while Friday is responding:

1. VAD detects speech from microphone
2. TTS stops current utterance
3. New utterance is transcribed and processed
4. New response is spoken

This mirrors natural conversation — you can cut Friday off.

---

## Audio Caching

To reduce latency:

1. **Frequent responses cached** — "Sure", "Got it", "Here's what I found"
2. **STT results cached** — identical audio → identical text (exact match)
3. **TTS output cached** — identical text → identical audio (per voice)

Cache lives in `~/.friday/voice_cache/` with LRU eviction (1000 entries).

---

## Error Handling & Fallbacks

| Failure Mode | Behavior |
|-------------|----------|
| Microphone not found | Voice mode disabled; suggest `friday voice setup` |
| STT model load failed | Fall back to API STT, or disable voice |
| TTS model load failed | Fall back to `pyttsx3`, or text-only mode |
| Hotword model not found | Disable hotword; push-to-talk only |
| Audio device busy | Retry after 1s; fail after 3 attempts |
| STT transcription empty (audio too quiet) | "Sorry, I didn't catch that" |
| STT confidence < 0.5 | "I think you said '{text}' — is that right?" |
| Response > 500 chars | Use text in CLI; speak summary only |

---

## Testing

### Unit Tests

```python
# test_voice_pipeline.py

def test_stt_transcribes_audio():
    stt = SpeechToText(model="whisper-tiny")
    text = stt.transcribe_file("tests/fixtures/whats_new.wav")
    assert "new" in text.lower()

def test_tts_speaks_text():
    tts = TextToSpeech(model="pyttsx3")
    tts.speak("Hello")  # Should not raise

def test_vad_detects_speech():
    vad = VoiceActivityDetector(mode=1)
    assert not vad.process_frame(b"\x00" * 480)  # Silence
    assert vad.process_frame(b"\xff" * 480)       # Loud noise

def test_hotword_detection():
    hw = HotwordDetector(keyword="hey friday", sensitivity=1.0)
    # Load test audio with hotword → expect True

def test_pipeline_round_trip():
    """Full pipeline: audio → text → persona → speech."""
    pipeline = VoicePipeline(stt="whisper-tiny", tts="pyttsx3")
    pipeline.start()
    # Inject test audio
    # Verify persona response text
    # Verify TTS was called
    pipeline.stop()
```

### Integration Tests

- Record test phrases: "what's new", "deploy the server", "who am I"
- Play through pipeline, verify response matches expected
- Measure end-to-end latency (target: < 3s)

### Performance Benchmarks

| Metric | Target | Stretch |
|--------|--------|---------|
| Hotword detection latency | < 200ms | < 100ms |
| STT latency (3s of speech) | < 2s | < 1s |
| TTS latency (50 chars) | < 500ms | < 200ms |
| End-to-end round trip | < 4s | < 2s |
| CPU usage (idle) | < 5% | < 2% |
| Memory usage | < 500MB | < 300MB |

---

## CLI Integration

### `friday talk` Command

```bash
$ friday talk
🎤 Friday is listening... (say "Hey Friday" or press Ctrl+Shift+Space)
[Pipeline started. Hotword: "hey friday"]
[You]: what's the status of my projects
[Friday]: I see 3 repositories with recent changes...
[You]: show me the codebuff changes
[Friday]: Opening GitHub compare view...
[Pipeline stopped. Input "exit" or "stop" to end session.]
```

### `friday talk --push-to-talk`

```bash
$ friday talk --push-to-talk
🎤 Hold Space to talk, release to process.
[Hold Space → "check test status" → Release]
[Friday]: All 156 tests pass in friday_v3...
```

### `friday voice setup`

```bash
$ friday voice setup
🎤 Voice Setup Wizard
────────────────────
1. Testing microphone... ✅ Found (Built-in Microphone)
2. Testing speakers... ✅ Working
3. Testing hotword "Hey Friday"... 🔄 Say "Hey Friday" now... ✅ Detected!
4. Testing speech recognition... 🔄 Say "What's new" now... ✅ Understood!

Setup complete! Run `friday talk` to start.
```

---

## Phase 1 Delivery Checklist

- [ ] `SpeechToText` — transcribes audio to text (local Whisper)
- [ ] `TextToSpeech` — speaks text aloud (local Piper)
- [ ] `VoiceActivityDetector` — detects speech in audio stream
- [ ] `HotwordDetector` — "Hey Friday" wake word
- [ ] `VoicePipeline` — orchestrates the full pipeline
- [ ] `VoiceRouter` — bridges voice to V3 persona engine
- [ ] `AudioStream` — managed microphone access
- [ ] `friday talk` — interactive voice session CLI command
- [ ] `friday talk --push-to-talk` — push-to-talk mode
- [ ] `friday voice setup` — audio device setup wizard
- [ ] Interruption handling (speak while Friday is speaking)
- [ ] Audio caching (frequent responses)
- [ ] Error handling & graceful fallbacks
- [ ] Unit test suite (20+ tests)
- [ ] Integration test suite (5+ tests)
- [ ] Performance benchmarks
- [ ] V3 test regression check (all 1,656 pass)
