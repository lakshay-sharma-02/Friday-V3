# Friday V4 — Phases 1+2+4 Completion Design

**Date:** 2026-08-01
**Scope:** Complete + improve Phases 1 (Voice), 2 (Desktop), 4 (Smart/Proactive+Intelligence). Add ops tooling.

## Current State (verified)

- **Built:** voice layer (core state machine, 4-provider TTS, STT, VAD, hotword, router), desktop layer (WM abstraction, 5 adapters, tray, hotkeys, watcher, notifier channels), proactive (context/anticipation/pattern/session/priority), intelligence (drift/anomaly/health/predictor/learner), integrated `friday4` CLI, 197 tests passing.
- **Not built:** `security/`, `collab/`, `mobile/`, `network/`, `desktop/ide/` (all guarded stubs).
- **Gaps found:**
  1. **No daemon** — nothing runs persistently. Voice, watcher, notifier, observer all have `start()` but no orchestrator. "Ambient Friday" impossible today.
  2. **No V3 wiring** — docs promise anticipation reads V3 observations/action_log; reality: proactive/intelligence fully self-contained.
  3. **No ops tooling** — no `doctor`, no unified `status`.
  4. **Docs drift** — PLAN/ROADMAP still show built items unchecked.

## Design

### 1. Daemon (`friday4 daemon`) — NEW

Single persistent orchestrator, one process, graceful degradation.

```
DaemonService
├── AnticipationEngine (shared, warm)
│   ├── start_observer()      # desktop watcher → pattern learner
│   └── get_suggestions()     # polled by channels
├── DesktopNotificationChannel (V3 ambient → desktop, if V3 present)
├── ProactiveSuggestionChannel (engine suggestions → desktop)
├── DesktopWatcher (window/app/workspace change → engine observer)
├── IntelligenceSampler (drift/anomaly samples on interval)
├── VoicePipeline (optional, via --voice; hotword idle listening)
└── shutdown: signal/thread-safe, joins all channels, ends session
```

CLI:
- `friday4 daemon start [--voice] [--no-notifications] [--poll N]` — foreground w/ log lines (user has no systemd; Ctrl+C stops cleanly). Writes `~/.friday/v4_daemon.pid` + status JSON for `status`/`stop`.
- `friday4 daemon status` — state, uptime, subcomponent health, V3 presence, recent notifications count.
- `friday4 daemon stop` — reads PID file, sends SIGTERM; graceful.

SIGINT/SIGTERM handler → ordered shutdown. If any component fails, log + continue (never crash the daemon).

**Why foreground:** user is on a minimal 2-core/3.2GB box with no systemd tooling established; `start` in a terminal (or tmux) is the honest v1. A `--background` flag double-forks only if trivial.

### 2. V3 Wiring (proactive ↔ friday.db) — NEW module `src/friday_v4/proactive/v3source.py`

Docs promise: "anticipation engine queries V3 action_log, observations, daemon status". Current engines are self-contained; wire them with graceful fallback:

- **`V3DataSource`** — lazily imports `friday.db.connect` / `friday.ambient`; probes `~/.friday/friday.db`; exposes:
  - `is_available() -> bool`
  - `recent_observations(hours, limit) -> list[dict]` — from `observations` table (source, subject, aspect, value, observed_at)
  - `recent_actions(hours, limit) -> list[dict]` — from `actions` table (action_type, target, project, observed_at)
  - `repo_count() -> int`, `recent_ambient_events(hours) -> list[dict]` — `ambient_feed`
  - `daemon_state() -> dict` — reads `~/.friday/daemon.status` JSON
- **`AnticipationEngine`** — accepts optional `data_source`; `get_context_summary()` and suggestion generation include a **"workspace digest"** when V3 available: "N repos observed, X new observations in 24h, daemon state". Fallback: current behavior.
- **`DeepContextEngine._enrich_git`** — unchanged (fast local git probe). V3 data is additive, never replaces local signals.
- **`friday4 proactive status`** — shows V3 source availability + sample counts.

Failure mode: V3 DB missing/corrupt/import fails → `is_available()=False`, all callers degrade silently (same pattern as `DesktopNotificationChannel._load_v3`).

### 3. Voice completion — small deltas

- `friday4 voice status` already exists; add **provider download status** (piper/kokoro model files: present/downloading/missing).
- `friday4 talk` — add `--push-to-talk-key` already present; add **exit word handling** already present. **Nothing major missing** — voice is the most complete phase. Verify + dogfood only.
- **Keep `hey_jarvis` hotword** (openwakeword) — retraining custom needs user voice samples; documented trade-off.

### 4. Desktop completion — small deltas

- `friday4 desktop status` exists; add **adapter availability report** to `doctor`.
- `DesktopNotificationChannel` + `ProactiveSuggestionChannel` exist; daemon wires them.
- Nothing else missing.

### 5. Ops tooling — NEW

**`friday4 doctor`** — one-command diagnostics:
```
System        OS/kernel, RAM, disk free, Python
Audio         input/output devices, sounddevice lib, default device
Voice         providers available (piper/edge/kokoro/pyttsx3), STT model, hotword
Models        piper jenny, kokoro onnx+voices (present/downloading/missing)
Desktop       WM detected (hyprland/gnome/kde/...), adapter available, watcher test
V3            friday.db present, connect works, ambient import, observations/actions/feed counts
Proactive     sessions dir, history size, patterns learned, queue size
Intelligence  drift baselines count, anomaly count
Exit code     0 all-ok, 1 degraded, 2 broken — for scripting
```
`--json` flag for machine-readable output.

**`friday4 status`** — unified layer overview (voice/desktop/proactive/intelligence/V3/daemon state) in one screen.

### 6. Docs

- Update `PLAN.md` Phase 0/1/2/6 checkboxes to reality; mark Phase 3/4/5/7 unchecked.
- Update `ROADMAP.md` Wave 1/2/4 items to shipped; add "Daemon" to Wave 8-ish ops list.
- Note `hey_jarvis` vs `hey friday` trade-off in VOICE_WAVE_2.md.

## Non-Goals

- Security/Collab/Mobile/IDE/Network waves — out of scope (stubs stay).
- systemd/launchd units — foreground daemon v1.
- Custom hotword retraining — needs user voice, future.
- V3 core changes — never. V4 imports V3, V3 never imports V4.

## Testing

- `tests/test_daemon.py` — DaemonService with injected fakes: starts/stops cleanly, joins channels, SIGTERM path, status file written, missing-component degradation.
- `tests/test_v3source.py` — V3DataSource against a temp sqlite DB with the V3 schema (observations/actions/ambient_feed fixtures); unavailable → all calls return empty/False.
- `tests/test_anticipation_v3.py` — engine with fake V3 source adds digest to summary; without → old behavior.
- Existing 197 tests must stay green.
