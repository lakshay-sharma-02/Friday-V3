# Collaboration & External — Prompt for Claude Code

## Intent
FRIDAY guides Tony through repairs remotely, showing schematics, walking step-by-step. She coordinates with other systems. Your Friday has Telegram/Slack/Discord bots but can't guide someone through a process, translate in real-time, or help during a meeting. Also missing: PR review assistant, pair programming support.

## What to build

### Phase 1: Remote Guidance Mode

Create `src/friday/guide.py`. A step-by-step guided walkthrough that Friday can lead someone through remotely.

**What it does:**
- `friday guide create "deploy emergency fix"` → creates a guided procedure from a protocol OR from scratch
- Each step has: instruction text, expected outcome, verification check, timeout
- Guide executes one step at a time, WAITING for confirmation before proceeding
- Can be delivered over any channel: CLI, Telegram, Slack, Discord, voice
- `friday guide start "deploy fix" --channel telegram` → sends step 1 via Telegram, waits for "done" reply
- Operator replies "done" → step 2 is sent, etc.
- If step fails → Friday suggests a workaround or asks if they want to abort

**Key design:**
- Reuses `protocol.py` Protocol → convert Protocol steps into guided walkthrough steps
- Each step has a `verification` field (shell command to run to verify success)
- Guide session is persisted in `guide_sessions` table: `(id, protocol_name, current_step, status, channel, created_at)`
- Operator can pause/resume: `friday guide pause`, `friday guide resume`
- Operator can ask for help on a step: "What does this mean?" → Friday explains the current step in more detail

### Phase 2: Real-time Translation

Add `src/friday/translate.py`. FRIDAY translates in Civil War — she understands multiple languages and can communicate across them.

**What it does:**
- `friday translate "hello world" --to es` → "hola mundo"
- `friday ask --lang es "¿cómo está mi proyecto?"` → asks in Spanish, gets answer in Spanish
- Automatic language detection on incoming messages → response in the same language
- Translation is used internally: if a Telegram message comes in French, Friday stores the English version internally but responds in French

**Implementation:**
- Use `argos-translate` (local, no API, pip-installable) OR LibreTranslate (self-hosted) OR Google Translate API (if key available)
- All translation is optional — if no translation engine, everything stays in the original language
- Cache translations in `translation_cache` table: `(text_hash, source_lang, target_lang, translated_text)`
- Language detection via `lingua-language-detector` or `fasttext` (lightweight, local)

**Key design:**
- Operator's language is stored as a preference (`operator_preferences` table, `language` key)
- Default language is English
- Friday detects the language of incoming messages automatically
- Translation is transparent — the operator never sees "translated from X" unless they ask
- CLI: `friday translate`, `friday ask --lang`

### Phase 3: PR Review Assistant

Create `src/friday/pr_review.py`. Autonomous PR review that runs as a daemon hook.

**What it does:**
- Watches for new PRs via the GitHub observer
- When a new PR is detected, analyzes the diff
- Produces a structured review:
  - Summary: what this PR does, size, risk level
  - Concerns: potential issues (large deletions, security-sensitive changes, config changes)
  - Suggestions: optional improvements (naming, structure, patterns)
  - Test gaps: files changed without corresponding test changes
- Posts the review as a comment on the PR (via GitHub API)
- Configurable: `friday pr review --auto` (auto-post), `friday pr review --preview` (show without posting)

**Key design:**
- Reuses `review_spontaneous.py` from Daily Operations — the diff analysis logic
- LLM optional: with LLM, produces rich suggestions; without, produces a factual change summary
- Never blocks CI — review is informational only
- Operator can disable: `friday profile set pr_reviews false`

### Phase 4: Meeting Transcription & Summarization

Create `src/friday/meeting.py`. Records and summarizes meetings.

**What it does:**
- `friday meeting start` → starts audio recording from the default microphone
- Records to a temp WAV file
- On `friday meeting stop` → transcribes the audio using whisper (local)
- Produces a summary: attendees (from speaker ID), topics discussed, decisions made, action items
- Stores in `meeting_notes` table
- Pushes summary to ambient feed

**Key design:**
- Whisper runs locally, no data leaves the machine
- Meeting mode has a prominent indicator: "🔴 Recording — Friday is listening. Say 'Friday stop' to end."
- If wake word detection is active, meeting mode temporarily disables it (to avoid feedback loops)
- Summary is best-effort — if transcription fails, "Meeting recording failed" with no data loss

### Phase 5: Pair Programming Assist

Create `src/friday/pair.py`. Friday acts as a pair programming partner that explains as you code.

**What it does:**
- Monitors your editor's active file (via Hyprland window title → file path heuristic)
- When you open a file, Friday reads it and prepares context
- You can ask: "Explain this function" → Friday reads the function and explains it
- "What does this pattern do?" → Friday recognizes the pattern from codebase knowledge
- "How should I implement X?" → Friday suggests an approach based on existing patterns in your codebase
- Runs passively — never interrupts, only responds when asked

**Key design:**
- Relies on the active window observer (Hyprland) to know what file you're looking at
- Pre-reads files you have open so answers are instant
- All responses are through the normal `ask()` pipeline
- No editor plugin required — works entirely from desktop awareness

## Files to touch
- `src/friday/guide.py` (new) — guided walkthrough engine
- `src/friday/translate.py` (new) — translation + language detection
- `src/friday/pr_review.py` (new) — autonomous PR review
- `src/friday/meeting.py` (new) — recording, transcription, summarization
- `src/friday/pair.py` (new) — pair programming assistant
- `src/friday/cli.py` — add `friday translate`, `friday guide`, `friday pr`, `friday meeting`, `friday pair`
- `src/friday/daemon.py` — hook PR review into cycle
- `src/friday/db.py` — add `guide_sessions`, `translation_cache`, `meeting_notes` tables
- `pyproject.toml` — add `argos-translate`, `lingua-language-detector` as optional deps
- `tests/test_guide.py` (new)
- `tests/test_translate.py` (new)
- `tests/test_pr_review.py` (new)
- `tests/test_meeting.py` (new)
- `tests/test_pair.py` (new)

## Acceptance criteria
1. `friday translate "hello world" --to es` → "hola mundo" (cached after first call)
2. Incoming Telegram message in Spanish → Friday responds in Spanish
3. `friday pr review --preview` → shows diff summary, concerns, suggestions
4. GitHub observer detects new PR → PR review auto-generated
5. `friday meeting start` + `friday meeting stop` → transcription + summary produced
6. `friday pair explain` → reads active file, explains the current function
7. Guide walkthrough executes step-by-step with verification between steps
8. Guide pause/resume survives daemon restart
