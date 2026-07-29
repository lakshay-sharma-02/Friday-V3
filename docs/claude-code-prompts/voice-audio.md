# Voice & Audio — Prompt for Claude Code

## Intent
FRIDAY doesn't just answer in text. She *speaks*. The primary interaction mode should eventually be voice. This isn't a gimmick — it changes the relationship from "tool you type at" to "partner you talk to."

## What to build

### Phase 1: TTS (Friday speaks back)
- When Friday responds via `ask()` or `persona/engine.py`, pipe the response through a TTS engine
- Use `edge-tts` (no API key, works offline-ish, good voices, pip-installable)
- Pick a voice that matches FRIDAY's personality — clear, warm, confident. Female voice. British or neutral accent.
- CLI flag `--voice` / config `FRIDAY_VOICE=true`
- In the daemon, when proactive messages fire, speak them (not just print)

**Key design decisions:**
- Speaking is ASYNC — never block the response from appearing in text
- If the user is in a terminal, speak via `aplay` / `ffplay` / `mpv` on a temp WAV
- Cache TTS output so repeated phrases don't re-request
- Volume control via config

Check `src/friday/persona/engine.py` — the greeting already says "I'm Friday, your AI operating partner." That should be *spoken* when the daemon starts.

### Phase 2: STT (Friday listens)
- Listen for a wake word ("Hey Friday") using `openWakeWord` or `porcupine` (offline, local)
- After wake word, record audio until silence, transcribe with `whisper.cpp` or `faster-whisper` (local, fast)
- Feed transcribed text into the same `ask()` pipeline
- This runs as a daemon sidecar thread — always listening, never blocking

**Key design decisions:**
- Wake word detection is ALWAYS-ON but whisper transcription is only triggered after wake
- STT thread communicates with the main process via a queue — never shares state
- Configurable wake word sensitivity
- Multiple wake words? "Hey Friday" and "Friday" both work

### Phase 3: Speaker ID (Friday knows WHO is talking)
- Simple voice-print matching against enrolled speakers
- On first use: "I don't recognize your voice. Say your name."
- Store a short embedding of the speaker's voice
- On subsequent wake-word triggers, identify who's speaking
- Feeds into the operator name in `operator_preferences`

## Files to touch
- `src/friday/services/voice.py` (new) — TTS + STT engine wrapper
- `src/friday/services/speaker_id.py` (new) — speaker recognition
- `src/friday/daemon.py` — add voice listener thread
- `src/friday/persona/engine.py` — hook TTS into responses
- `src/friday/services/__init__.py` — export new services
- `src/friday/proactive.py` — speak proactive messages
- `pyproject.toml` — add `edge-tts`, `faster-whisper`, `pvporcupine` or `openwakeword` as optional deps
- `tests/test_voice.py` (new)

## Acceptance criteria
1. `FRIDAY_VOICE=true` + daemon running → Friday speaks greeting on startup
2. `friday ask --voice "What's my engineering context?"` → speaks the answer
3. Proactive messages are spoken, not just printed
4. "Hey Friday what's the status" → transcribes, routes to ask(), speaks answer
5. Two different people say "Hey Friday" → Friday addresses each by name
6. All voice features gracefully degrade if audio device unavailable
