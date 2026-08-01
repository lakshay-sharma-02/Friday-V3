# Voice TTS Capability — Design Doc

## Goal
Add text-to-speech to Friday so she speaks answers aloud in the terminal.

## How It Gets Built
Not by us. The **Self-Evolution Engine** (`friday upgrade`) generates, sandboxes, tests, and deploys the code.

## Self-Evolution Engine Gaps Closed (prerequisite)

| Gap | Fix |
|-----|-----|
| **Agent routing** (`agent.py`) | `run_agent()` now detects self-upgrade keywords ("make yourself capable", "upgrade yourself", etc.) and routes directly to `cli_meta.cmd_upgrade()` — no LLM decomposition, no executor dispatch. |
| **Startup auto-rollback** (`daemon.py`) | `_check_auto_rollback()` now runs once at daemon startup (not just in-cycle), preventing boot-loop after a broken capability deploy. |
| **DB migration** (`sql009_capability_flags.sql`) | Proper migration file added; table previously created ad-hoc via `CREATE TABLE IF NOT EXISTS`. |

## What Friday Will Generate
When `friday upgrade "make yourself capable of speaking"` runs, the LLM planner produces a JSON plan covering:

- **`src/friday/services/voice.py`** — TTS engine wrapper using `edge-tts` (pip-installable, offline-capable, no API key)
- **`src/friday/cli.py` modification** — add `--voice` flag to `friday ask`
- **`src/friday/daemon.py` modification** — speak greeting on startup when enabled
- **`pyproject.toml`** — add `edge-tts` as optional dependency
- **`tests/test_voice.py`** — smoke tests
- Feature-flagged (disabled by default via `FRIDAY_VOICE_ENABLED`)

## Engine Behavior
1. `friday upgrade "make yourself capable of speaking"` → user confirms
2. Sandbox created (git worktree copy)
3. LLM generates capability plan (files, deps, test, config)
4. Plan validated, files written to sandbox
5. Dependencies installed (`edge-tts`)
6. Tests run in sandbox
7. Full regression suite run
8. If all pass: rollback commit captured, capability flag registered (disabled)
9. User enables: `friday upgrade enable voice_support`

## Acceptance Criteria
1. `friday upgrade plan "make yourself capable of speaking"` shows what would change (dry-run)
2. `friday upgrade "make yourself capable of speaking"` completes pipeline successfully
3. `friday upgrade list` shows voice_support with status
4. Generated code crashes gracefully when audio device unavailable
5. All existing meta-engine tests still pass

## Post-Deploy Review
After deploy, review the generated code against:
- Graceful degradation (no crash if `edge-tts` not installed or no audio device)
- Async TTS (never block text response)
- Existing code style compliance
- No new required dependencies (optional only)
