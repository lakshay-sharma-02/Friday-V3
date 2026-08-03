# Friday V4 — Master Plan

> **Vision:** An ambient, proactive, multi-modal AI partner that understands,
> anticipates, and acts across every surface of your engineering life.
>
> Inspired by MCU Tony Stark's FRIDAY — voice-controlled, environment-aware,
> proactively intelligent, deeply integrated, and personally loyal.
>
> **⚠️ Constitution first:** every wave, feature, and commit is judged
> against [**THE MCU FRIDAY STANDARD**](MCU_FRIDAY_STANDARD.md) — ten laws,
> five MCU acceptance tests, one definition of done. If a build doesn't
> move Friday toward *that*, it doesn't get built.
>
> **📍 The route:** [**THE FRIDAY MASTER PLAN**](MASTER_PLAN.md) — one
> sentence, decomposed into waves. "MCU FRIDAY is a single presence that
> speaks natural language, has no learning ceiling, no task it can't pick
> up, and adapts its personality to you." The waves in this PLAN.md are
> the how; the MASTER_PLAN is the why.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Design Philosophy](#2-design-philosophy)
3. [V3 Inheritance](#3-v3-inheritance)
4. [Architecture Overview](#4-architecture-overview)
5. [Phase Roadmap](#5-phase-roadmap)
6. [Layer Specifications](#6-layer-specifications)
7. [Dependency Diagram](#7-dependency-diagram)
8. [Risk Assessment](#8-risk-assessment)
9. [Success Criteria](#9-success-criteria)

---

## 1. Executive Summary

**Date:** August 1, 2026
**Status:** Active development — Waves 1–5 **shipped** (Voice, Desktop,
Security & Quality, Proactive Intelligence, Collaboration) plus the
`friday4 web` dashboard and `friday4 doctor` ops tooling. **Waves 9, 10,
and 11 shipped** (Agency Core, Memory & Identity, Research & Reflection)
and **Wave 12 (Polish & Scale) shipped** (benchmarks, docs site, installer,
migration guide, SSH executor). Wave 9 ships the brain: `db.py`,
`understanding/` (NLU), `reasoning/` (evidence-cited answers), `missions/`,
`execution/` (gate → sandbox → audit → undo), `nl_router.py`, `friday4
talk "…"`. Wave 10 ships identity: `memory/`, `persona/`, `relationship/`,
`skills/` (shadow-first), all wired into the daemon. Wave 11 ships
research/synthesis/briefing/ambient **push** (SSE `/api/events`; security,
suggestions, and collab observations publish onto one shared bus) plus
`report --daily/--weekly`. See `WAVE_9_AGENCY_CORE.md`,
`WAVE_10_MEMORY_IDENTITY.md`, `WAVE_11_RESEARCH_REFLECTION.md`.
**Product:** Friday V4 is **the product**. V3 is legacy heritage that V4 may
read from — never a wrapper.

### Why V4?

Friday V3 (the `friday` package) was an ambitious exploration: a persistent AI
operating partner with a full observation → execution → self-improvement
pipeline. But it grew into a single-user, single-machine, CLI-first monolith
that is hard to run, hard to package, and hard to polish.

V4 is a **clean rewrite of the product surface** — not a wrapper around V3:
- **Voice-first interaction** — speak to Friday like Tony Stark
- **Cross-platform desktop presence** — Hyprland/GNOME/KDE/macOS/Windows
- **Security & quality** — code health as a first-class capability
- **Proactive intelligence** — anticipates needs before you ask
- **A modern dashboard** — live status, security grade, drift, ambient feed
- **Owns its own runtime** — V4 daemon, V4 CLI, V4 state, V4 tests

### The V4-First Promise

- ✅ V4 is the main product — its own daemon, CLI, config, state, tests
- ✅ V3 is **optional legacy data** — read via a read-only bridge when present
- ✅ V4 never writes V3's DB, never shells into V3's CLI, never wraps V3 commands
- ✅ Missing V3 DB degrades gracefully — V4 works fully standalone
- 🆕 Every new capability (voice, desktop, security, web) is V4-native
- 🆕 V4's architecture laws: pure-stdlib-first, never crash, degrade silently

---

## 2. Design Philosophy

### Core Tenets

1. **V4 Is The Product** — V3 is legacy data, not a dependency. V4 runs,
   scans, speaks, and serves on its own.
2. **Voice-Native** — Every V4 interface speaks. Text is the fallback.
3. **Ambient, Not Intrusive** — present but quiet; interrupts only for what matters.
4. **Cross-Surface** — Desktop, web, terminal, IDE, mobile (planned).
5. **Pure-Stdlib First** — every capability has a stdlib implementation that
   always works; optional tools (whisper, piper, ruff, pip-audit…) enhance,
   never gate.
6. **Never Crash** — every external call is wrapped; missing tools/subsystems
   degrade silently (this is why `friday4 web` and `doctor` never 500).
7. **Secure By Default** — least privilege, secrets scanned, findings owned by V4.
8. **Learn Continuously** — patterns, sessions, drift, and anomalies feed suggestions.

### What We Keep From V3 (as heritage)

| V3 Asset | V4 Strategy |
|-----------|-------------|
| `~/.friday/friday.db` observations/actions/ambient | **Read-only bridge** via `V3DataSource` when the DB exists |
| Ambient feed | **Read** for the web dashboard / anticipation; never written |
| V3 CLI | **Not wrapped.** V4 ships its own `friday4` commands |
| V3 daemon | **Not launched.** V4 runs its own `friday4 daemon` |
| V3 tests | **Not a gate.** V4 has its own test suite |

### What Changes (V4-native solutions)

| V3 Limitation | V4 Solution |
|-------------|-------------|
| CLI-only interaction | Voice + Desktop + Web dashboard + CLI (`friday4`) |
| Hyprland-only WM | `desktop/` WM abstraction (Hyprland/GNOME/KDE/macOS/Windows adapters) |
| Monolithic single-process | `friday4 daemon` — one process, per-component health |
| No voice | `voice/` — STT (faster-whisper), TTS (kokoro/piper/edge/pyttsx3), VAD, hotword |
| No security | `security/` — dependency audit, secret detection, quality gates |
| No proactive layer | `proactive/` + `intelligence/` — anticipation, drift, anomalies, health |
| No dashboard | `web/` — `friday4 web` pure-stdlib local dashboard |

---

## 3. V3 Interop (Read-Only Bridge)

### What V4 Actually Imports from V3

One module, one direction, read-only:

```python
# friday_v4/proactive/v3source.py — the ONLY V3 touchpoint.
from friday_v4.proactive.v3source import V3DataSource

src = V3DataSource()              # points at ~/.friday/friday.db
if src.is_available():            # DB exists AND has V3 schema
    obs = src.recent_observations(hours=24)
    acts = src.recent_actions(hours=24)
    events = src.recent_ambient_events(hours=24)
    digest = src.workspace_digest()
```

- `V3DataSource` opens the DB **read-only** (URI `mode=ro`) and never writes.
- Every query is guarded: a missing DB, missing schema, or unreadable file
  yields empty results / `is_available() == False` — never a crash.
- V4's ambient feed, anticipation, and web dashboard all consume V3 *data*
  through this single bridge when it's present, and work fully standalone
  when it isn't.

### What V4 Does NOT Do with V3

- ✗ No `from friday import ...` anywhere in `friday_v4/src` (except the
  read-only sqlite access in `v3source.py`).
- ✗ No subprocess calls to the `friday` CLI.
- ✗ No writes to `~/.friday/friday.db`.
- ✗ No dependency on V3's tests, laws, or internal APIs.

### Test Strategy

V4 has its own suite at `friday_v4/tests/` (currently ~740 tests, growing
toward ~820 after Wave 11). V3's 1,656 tests are **not** a gate on V4 —
V4 must be testable, installable, and runnable with zero V3 code present.

---

## 4. Architecture Overview

### V4 Layered Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     FRIDAY V4 SURFACES                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │  Voice   │ │ Mobile   │ │   Web    │ │  Desktop │ │  CLI │ │
│  │ Pipeline │ │ (future) │ │ Dashboard│ │  Suite   │ │friday4│ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └──┬───┘ │
│       │            │            │            │          │      │
├───────┴────────────┴────────────┴────────────┴──────────┴──────┤
│                        FRIDAY V4 DAEMON                         │
│   observer · notifier · sampler · security scanner · proactive   │
│   (one process, per-component health, never crashes)             │
├─────────────────────────────────────────────────────────────────┤
│                FRIDAY V4 INTELLIGENCE LAYER                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐      │
│  │  Drift   │ │ Security │ │  Health  │ │ Proactive     │      │
│  │ Detection│ │ Scanning │ │ Diagnostics│ │ Anticipation │      │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘      │
│       │            │            │               │               │
├───────┴────────────┴────────────┴───────────────┴───────────────┤
│               OPTIONAL V3 DATA (read-only bridge)               │
│   ~/.friday/friday.db via V3DataSource — observations, actions,  │
│   ambient feed. Missing DB degrades gracefully; never written.   │
└─────────────────────────────────────────────────────────────────┘
```

### Module Map

```
friday_v4/
├── docs/                              # Architecture, plans, specs
│   ├── PLAN.md                        # This file
│   ├── ARCHITECTURE.md                # V4 architecture reference
│   ├── VOICE_SPEC.md                  # Voice pipeline design
│   ├── DESKTOP_SPEC.md                # Desktop integration design
│   ├── MOBILE_SPEC.md                 # Mobile app design
│   ├── COLLAB_SPEC.md                 # Multi-instance design
│   └── SECURITY_SPEC.md               # Security scanning design
│
├── src/friday_v4/
│   ├── __init__.py
│   ├── config.py                      # ✅ Config loader (file + FRIDAY_V4_* env)
│   ├── daemon.py                      # ✅ `friday4 daemon` (ambient service + SecurityScanner)
│   │
│   ├── voice/                         # ✅ Voice Interface Layer (Wave 1)
│   │   ├── stt.py                     # Speech-to-text (faster-whisper)
│   │   ├── tts.py                     # TTS (kokoro/piper/edge-tts/pyttsx3, auto-fallback)
│   │   ├── hotword.py                 # Hotword detection (openwakeword)
│   │   ├── vad.py                     # Voice activity detection
│   │   ├── pipeline.py                # Voice pipeline + barge-in
│   │   ├── router.py                  # Voice → desktop/proactive/fallback routing
│   │   ├── audio.py · chimes.py · utils.py · core.py
│   │
│   ├── desktop/                       # ✅ Desktop Suite (Wave 2)
│   │   ├── wm_abstraction.py          # Cross-platform WM abstraction
│   │   ├── hyprland/gnome/kde/macos/windows_adapter.py
│   │   ├── tray.py · hotkeys.py · watcher.py · notifier.py
│   │   └── ide/                       # ✅ IDE Integration (Wave 6)
│   │
│   ├── security/                      # ✅ Security & Quality (Wave 3)
│   │   ├── scanner.py                 # VulnerabilityScanner orchestrator
│   │   ├── deps.py                    # Dependency auditor (curated CVE DB + pip-audit)
│   │   ├── secrets.py                 # Secret detection (regex/entropy + trufflehog)
│   │   ├── quality.py                 # Quality gates (AST + ruff/mypy)
│   │   ├── reporter.py                # SecurityReport / Finding model
│   │   └── tooling.py                 # ✅ venv-aware tool discovery (find_tool)
│   │
│   ├── intelligence/                  # ✅ Advanced Intelligence (Wave 4)
│   │   ├── drift.py · anomaly.py · health.py · predictor.py · learner.py
│   │
│   ├── proactive/                     # ✅ Proactive Intelligence (Wave 4)
│   │   ├── anticipation.py            # Anticipation engine
│   │   ├── context_engine.py · session_memory.py · pattern_learner.py · priority.py
│   │   └── v3source.py                # ✅ the ONLY V3 touchpoint (read-only)
│   │
│   ├── web/                           # ✅ Web Dashboard (Wave 7 slice)
│   │   ├── server.py                  # pure-stdlib http.server + dashboard page
│   │   └── dashboard.py               # guarded JSON accessors for every subsystem
│   │
│   ├── collab/                        # ✅ Collaboration (Wave 5)
│   │   └── crdt.py · peer.py · sync.py · coordinator.py · permissions.py
│   │
│   ├── db.py                          # ✅ V4 state DB (sqlite, migrations, typed helpers)
│   │
│   ├── understanding/                 # ✅ NLU (Wave 9) — intent/entities/confidence/resolver
│   ├── reasoning/                     # ✅ Evidence-cited answers (Wave 9) — engine/evidence/judgment/providers
│   ├── missions/                      # ✅ Persistent goals (Wave 9) — engine/models/planner/scheduler/progress
│   ├── execution/                     # ✅ Gated execution (Wave 9) — executors/gate/sandbox/audit/undo
│   ├── nl_router.py                   # ✅ NL → act (Wave 9) — shared CLI/voice handler
│   │
│   ├── memory/                        # ✅ Memory & Identity (Wave 10) — facts/working/store
│   ├── persona/                       # ✅ — engine/learn/prompts (explicit-consent)
│   ├── relationship/                  # ✅ — depth/tones (interaction → tone)
│   ├── skills/                        # ✅ — replay/shadow/registry/dispatch (shadow-first)
│   │
│   ├── mobile/ · network/             # ⏳ stubs (future waves / decision)
│   │
│   └── cli_*.py                       # `friday4` subcommands: talk (NL brain),
│                                      # voice (session lives under `voice talk`),
│                                      # desktop, security, proactive, intelligence,
│                                      # doctor, daemon, web, collab, status,
│                                      # execute, ask, memory, persona,
│                                      # relationship, skills (waves 11 adds:
│                                      # analyze, correlate, briefing, narrative,
│                                      # report)
│
├── tests/                             # ✅ V4 test suite (~740 today, → ~820)
│   ├── test_voice.py · test_desktop_abstraction.py · test_security.py
│   ├── test_proactive.py · test_intelligence.py · test_anticipation_v3.py
│   ├── test_v3source.py · test_daemon.py · test_config.py · test_web.py
│   ├── test_collab_*.py · test_cli_collab.py
│   ├── test_db.py · test_understanding.py · test_execution.py · test_cli_execute.py
│   ├── test_reasoning.py · test_nl_router.py · test_cli_ask.py · test_cli_nl.py
│   ├── test_memory.py · test_persona.py · test_relationship.py · test_skills.py
│   ├── test_cli_memory.py · test_cli_status.py
│   └── test_package_imports.py · test_voice_engine.py
│
└── pyproject.toml                     # V4 project configuration
```

---

## 5. Phase Roadmap

### Phase 0 — Foundation ✅
**Goal:** V4 project structure, own CLI, own config, own tests

**Deliverables:**
- [x] `friday_v4/` directory scaffolded
- [x] `PLAN.md` / `ARCHITECTURE.md` / `ROADMAP.md`
- [x] `pyproject.toml` — V4 dependencies
- [x] `friday4` CLI entry point (V4-native commands — does NOT wrap V3 CLI)
- [x] `config.py` — defaults + file + `FRIDAY_V4_*` env overrides
- [x] `tests/` suite with `conftest.py` (hermetic, no real `~/.friday` writes)

### Phase 1 — Voice Interface ✅ (Wave 1)
**Goal:** Talk to Friday like Tony Stark talks to FRIDAY

**Deliverables:**
- [x] Speech-to-text (faster-whisper)
- [x] Text-to-speech (kokoro / piper / edge-tts / pyttsx3, auto-fallback)
- [x] Voice activity detection
- [x] Hotword detection (openwakeword)
- [x] VoiceRouter: desktop → proactive → fallback (no V3 dependency)
- [x] `friday4 talk` — interactive voice session + push-to-talk + barge-in
- [x] `friday4 voice setup/status/test`

### Phase 2 — Desktop Integration ✅ (Wave 2)
**Goal:** Friday controls your entire desktop environment

**Deliverables:**
- [x] Cross-platform WM abstraction API
- [x] Hyprland / GNOME / KDE / macOS / Windows adapters
- [x] System tray + global hotkeys
- [x] `friday4 desktop status/windows/switch/focus/launch/screenshot/platforms`
- [x] Desktop notification channel (V4 daemon → tray/notify)
- [x] `friday4 daemon` — one ambient service: observer + notifier + sampler + security

### Phase 3 — Security & Quality ✅ (Wave 3)
**Goal:** Friday actively protects and improves your code

**Deliverables:**
- [x] Dependency vulnerability scanner (built-in curated advisory DB + optional pip-audit)
- [x] Secret detection (built-in regex/entropy + optional trufflehog)
- [x] Code quality gates (built-in AST checks + optional ruff/mypy)
- [x] `friday4 security scan` / `friday4 security status`
- [x] `friday4 doctor` reports security tool availability + last scan state
- [x] venv-aware tool discovery (`security/tooling.py`) — tools in the venv bin are found even off PATH
- [ ] Automated PR annotations (future)

**Key Integration Points:**
- Daemon's `SecurityScanner` runs periodic scans, persists state, dedups notifications
- Findings stay V4-native (state file + web dashboard) — never written to V3

### Phase 4 — Collaboration ✅ (Wave 5)
**Goal:** Multiple Friday instances, team workspaces

**Deliverables:**
- [x] CRDT-based observation merge
- [x] Peer discovery (UDP beacons — stdlib, replaces mDNS)
- [x] Real-time sync (TCP JSON-lines — stdlib, replaces WebSocket)
- [x] Shared workspace permissions
- [x] Team observation feeds
- [x] `friday4 collab` — collaboration CLI (start/status/peers/obs/add/share/perms)

**Key Integration Points:**
- Sync layer sits between V4 CRDT store and remote peers (pure stdlib)
- Observations replicated across instances
- Permissions filter what each instance can see/do

### Phase 5 — Mobile & Web (Wave 7)
**Goal:** Friday in your pocket, Friday in your browser

**Deliverables:**
- [x] Web dashboard (daemon status + security grade/findings + intelligence + proactive + V3 bridge + voice) — `friday4 web`
- [x] Scan-path picker on the dashboard (target any project directory)
- [ ] React Native companion app (future)
- [ ] Push notification transport (future — real-time push designed in Wave 11's `ambient/`)

**Key Integration Points:**
- Dashboard reads V4 state files + V3 DB via the read-only bridge
- "Run security scan" action drives the daemon's own `SecurityScanner`

**Key Integration Points:**
- Dashboard reads V4 state files + V3 DB via the read-only bridge
- "Run security scan" action drives the daemon's own `SecurityScanner`

### Phase 6 — Advanced Intelligence ✅ (Wave 4)
**Goal:** Friday anticipates your needs

**Deliverables:**
- [x] Predictive drift detection + anomaly detection + code health diagnostics
- [x] Anticipation engine (context + patterns + sessions + priority)
- [x] Automated workflow suggestions (per-repo patterns)
- [x] V3 data wiring via `V3DataSource` (graceful fallback when V3 absent)
- [x] `friday4 doctor` — ops tooling (unified `friday4 status` lands in Wave 9)
- [x] `friday4 proactive status/suggest/learn/brief/observe`
- [x] `friday4 intelligence status/drift/anomaly/health/predict`

### Phase 7 — IDE Integration ✅ shipped (Wave 6)
**Goal:** Friday lives inside your editor. **Design:** `WAVE_6_IDE.md`

**Deliverables:**
- [x] `desktop/ide/detection.py` — adaptive editor detection (VS Code / JetBrains / Neovim / Sublime / Emacs)
- [x] `desktop/ide/lsp_client.py` — pure-stdlib LSP client (JSON-RPC stdio: diagnostics + symbols, no pygls)
- [x] `desktop/ide/ast_analyzer.py` — always-on fallback (syntax / undefined names / unused imports / shadowed builtins)
- [x] `desktop/ide/controller.py` — editor control (open / reveal / run, argv per editor kind)
- [x] NL path: `Intent.IDE` — "what's wrong with X" on every surface; `QuestionType.CODE` reasoning provider
- [x] `friday4 ide detect/diagnose/symbols/open/reveal/run` (run through the gated execution pipeline)
- [x] Composition: `FRIDAY_V4_IDE_PREFLIGHT` → diagnostics ride with Claude Code + command preflight notes
- [ ] TypeScript VS Code extension (sidebar/status bar) — future refinement; the editor is reachable via CLI + LSP without it

### Phase 8 — Agency Core ✅ shipped (Wave 9)
**Goal:** Friday actually does things. **Design:** `WAVE_9_AGENCY_CORE.md`

**Deliverables:**
- [x] `db.py` — V4 sqlite foundation (missions, actions, memories, skills, sessions) + migrations
- [x] `understanding/` — NLU: intent → entities → canonical action (voice/CLI/web shared)
- [x] `reasoning/` — evidence-cited answer engine + provider registry (identity/status/activity/mission/memory/conversation)
- [x] `nl_router.py` — NL → act (shared CLI/voice handler; gate → sandbox → audit)
- [x] `missions/` — persistent goals: planner → engine → steps → scheduler → progress
- [x] `execution/` — gated, sandboxed, audited executors (shell/git/file/python/testing) + undo
- [x] `friday4 talk "…"` (NL brain), `ask`, `execute`, `status` (incl. `db status`)

### Phase 9 — Memory & Identity ✅ shipped (Wave 10)
**Goal:** Friday knows you. **Design:** `WAVE_10_MEMORY_IDENTITY.md`

**Deliverables:**
- [x] `memory/` — facts + working memory (provenance, confidence, decay)
- [x] `persona/` — explicit-consent name & preference learning
- [x] `relationship/` — interaction depth → tone & verbosity
- [x] `skills/` — shadow-first self-improvement (Replay + Shadow executors)
- [x] `friday4 memory/persona/relationship/skills`
- [x] Daemon wiring: memory decay sweeper, skill learner, relationship refresher

### Phase 10 — Research & Reflection ✅ shipped (Wave 11)
**Goal:** Friday reasons across your world. **Design:** `WAVE_11_RESEARCH_REFLECTION.md`

**Deliverables:**
- [x] `research/` — architecture, cross-project correlation, impact, code search
- [x] `synthesis/` — deterministic, evidence-cited reports (incl. `reports.py` daily/weekly)
- [x] `briefing/` — morning/evening briefings from real V4 state
- [x] `ambient/` — in-process event bus + durable queue; real-time push
- [x] `friday4 analyze/correlate/briefing/narrative/report [--daily|--weekly]`
- [x] **Push wiring** — security findings, proactive suggestions, and collab
      observations publish onto the daemon's shared AmbientBus
- [x] **Web SSE** — `GET /api/events` stream + dashboard EventSource
      (durable-queue replay via `since` cursor; poll kept as fallback)

### Phase 11 — Polish & Scale ✅ shipped (Wave 12)
**Goal:** Production-ready V4

**Deliverables:**
- [x] Performance benchmarks (`tools/benchmarks.py`, V3 vs V4 where importable)
- [x] Full V4 test suite (~800, hermetic)
- [x] Documentation site (`tools/build_docs_site.py` → `site/`)
- [x] Installation script (`install.sh`)
- [x] Migration guide (`docs/MIGRATION_GUIDE.md`)
- [x] `network/` stub folded in — `ssh` executor behind gate → sandbox → audit
- [ ] Dogfooding period (ongoing)

---

## 6. Layer Specifications

### 6.1 Voice Layer

```python
# Conceptual API
from friday_v4.voice import VoicePipeline

pipeline = VoicePipeline(
    stt_model="whisper-small",     # Local or API-based
    tts_model="piper",             # Local or API-based
    hotword="hey friday",          # Wake word
    vad_mode=1,                    # Voice activity detection sensitivity
)

# Start listening (blocks, processes in background)
pipeline.start(callback=on_voice_command)

# Send notification aloud
pipeline.speak("I noticed 3 repositories changed today.")

# Push-to-talk (non-hotword mode)
with pipeline.push_to_talk() as audio:
    text = pipeline.transcribe(audio)
    response = identity_engine.process(text)
    pipeline.speak(response)
```

**STT Models (ordered by capability):**
1. Whisper (local) — fully offline, good accuracy
2. Deepgram (API) — best accuracy, low latency
3. AssemblyAI (API) — good accuracy, more features

**TTS Models:**
1. Piper (local) — fast, offline, decent quality
2. XTTS-v2 (local) — slower, better quality, voice cloning
3. ElevenLabs (API) — best quality, voice cloning

**Architecture:**
```
Microphone → VAD → Hotword Detection → STT → Text
                                                    ↓
                                              Persona Engine
                                                    ↓
Audio Input ← TTS ← Audio Output ← Response Text ←
```

### 6.2 Desktop Abstraction Layer

```python
# Conceptual API
from friday_v4.desktop import Desktop, Window, Workspace

desktop = Desktop()

# Get current context
active_window = desktop.get_active_window()
workspaces = desktop.get_workspaces()

# Control desktop
desktop.switch_to_workspace(3)
desktop.focus_window("codebuff — main.py")
desktop.launch_app("code", "/path/to/project")

# Monitor desktop
desktop.on_window_change(callback)
desktop.on_workspace_change(callback)
```

**Platform Adapters:**
| Platform | Backend | Status |
|----------|---------|--------|
| Hyprland | `hyprctl` / IPC | ✅ V4-native adapter |
| GNOME | `gdbus` / Extensions | ✅ V4-native adapter |
| KDE | `qdbus` / KWin script | ✅ V4-native adapter |
| macOS | Accessibility API / Scripting Bridge | ✅ V4-native adapter |
| Windows | Win32 API / PowerToys | ✅ V4-native adapter |

### 6.3 Collaboration Layer

```python
# Conceptual API
from friday_v4.collab import Coordinator

coord = Coordinator(
    instance_id="lakshay-desktop",
    discovery="mdns",
    db_path="~/.friday/friday.db",
)

# Join a team workspace
coord.join_workspace("team-awesome")

# Share observations
coord.share_observation(
    source="git_observer",
    subject="friday_v4",
    aspect="commits",
    value="3 new commits",
)

# Receive peer observations
coord.on_observation(lambda obs: print(f"From {obs.instance}: {obs.value}"))
```

**CRDT Merge Strategy:**
- Observations: Last-Writer-Wins per (source, subject, aspect)
- Knowledge: Additive merge with conflict resolution
- Preferences: Instance-specific (not shared)
- Permissions: Authoritative per workspace owner

### 6.4 Security Scanner

```python
# Actual API (Wave 3)
from friday_v4.security import VulnerabilityScanner

scanner = VulnerabilityScanner()
report = scanner.scan("/path/to/project")          # → SecurityReport
report.grade()                                       # 'A'..'F'
report.score()                                       # 0-100
report.counts_by_severity()                          # {'critical': 1, ...}
report.above_threshold("high")                       # actionable findings
report.to_json()                                     # machine-readable

# Sub-scanners (each returns (findings, tools_used))
from friday_v4.security import DependencyAuditor, SecretDetector, QualityGate

dep_findings, tools = DependencyAuditor().scan("/path")
sec_findings, tools = SecretDetector().scan("/path")
q_findings, tools = QualityGate().scan("/path")

# CLI
#   friday4 security scan [path] [--threshold high] [--json] [--no-*]
#   friday4 security status
```

### 6.5 Proactive Intelligence

```python
# Actual API (Wave 4)
from friday_v4.proactive.anticipation import AnticipationEngine
from friday_v4.proactive.v3source import V3DataSource

engine = AnticipationEngine(data_source=V3DataSource())  # read-only V3 bridge

# Learn from activity (desktop watcher feeds this in the daemon)
engine.observe_activity("edit_file", {"repo": "repoA", "app": "code"})

# What should Friday do next?
suggestions = engine.get_suggestions(force=True)
# → ["You usually run tests after editing — check the test report..."]

# Context summary (includes V3 digest when available)
summary = engine.get_context_summary()
```

---

## 7. Dependency Diagram

```
friday_v4
│
├── optional read-only data (only when present):
│   └── ~/.friday/friday.db          # V3 observations/actions/ambient, via V3DataSource (mode=ro)
│
├── optional tools (venv-aware discovery, enhance but never gate):
│   ├── faster-whisper               # STT
│   ├── kokoro / piper / edge-tts    # TTS
│   ├── ruff / mypy                  # quality gates
│   ├── pip-audit / trufflehog       # dependency / secret scans
│   └── openwakeword                 # hotword
│
└── requires Python ≥3.12            # stdlib-only core
```

**Key Rule:** friday_v4 never imports the `friday` package. The single V3
touchpoint is read-only sqlite access in `proactive/v3source.py`. V3 can be
deleted from disk and V4 keeps working — this is what "V4 is the product"
means.

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Voice latency too high for real-time | Medium | High | Local models as primary, API fallback; push-to-talk mode |
| V4 creeping back toward a V3 wrapper | Medium | High | Import rule: only `v3source.py` may touch V3; code review enforces |
| Optional tools missing | High | Medium | Pure-stdlib built-ins for every scanner; venv-aware `find_tool` |
| Cross-platform WM fragmentation | High | Medium | Abstract API first; adapters are independent |
| Security scanners too noisy | High | Medium | Tunable thresholds, severity gating |
| Test suite stalls below 680 | Medium | Low | Coverage-gap reviews each polish pass |
| Performance regression from V4 layers | Low | Medium | Benchmarks in CI; profiling for critical paths |

---

## 9. Success Criteria

### V4 is successful when:

1. **Voice works end-to-end:** "Hey Friday, what's the status of my projects?" → Friday responds aloud
2. **Desktop cross-platform:** switch workspaces, launch apps, report desktop state on 3+ platforms
3. **Security scanning active:** `friday4 security scan` finds real issues; daemon persists + dedups findings
4. **Web dashboard useful:** `friday4 web` shows daemon/security/intelligence at a glance, scan works from the UI
5. **V4 is standalone:** runs with zero V3 code present; V3 DB adds ambient context when available
6. **No V3 wrappers:** no `from friday import` anywhere except the read-only `v3source.py` bridge
7. **V4 has its own test suite:** ~740 V4 tests (~820 after Wave 11), hermetic (no real `~/.friday` writes)
8. **Dogfooding:** Friday V4 is used for daily engineering work

### Non-Goals for V4

- Cloud-hosted Friday (stays local-first)
- Realtime collaborative editing (not an IDE)
- General-purpose chatbot (stays engineering-focused)
- Platform-specific distribution (stores, packages — post-V4)

---

## Appendix A: V3 → V4 Relationship

V3 and V4 are separate products that share a data directory:

1. V4 installs/run independently (`friday_v4/`, its own venv)
2. V4 reads `~/.friday/friday.db` read-only when present (ambient context)
3. V3 CLI remains usable for those who want it — V4 never shells into it
4. V4 adds: `friday4 talk`, `friday4 desktop`, `friday4 security`, `friday4 web`, `friday4 collab`
5. V4 daemon runs standalone; V3 daemon may coexist (both read the DB)
6. No rollback needed — V4 never writes V3 data
7. V3's *capability map* (mission, executors, memory, skill formation, analysis)
   is the **design reference** for Waves 9–11 — rebuilt V4-native, never imported

## Appendix B: V4 CLI Commands

| Command | Description | Status |
|---------|-------------|--------|
| `friday4 talk` | Interactive voice session (hotword + push-to-talk) | ✅ |
| `friday4 voice setup/status/test` | Voice provider setup & diagnostics | ✅ |
| `friday4 desktop status/windows/switch/focus/launch/screenshot/platforms` | Desktop control | ✅ |
| `friday4 daemon start/stop/status` | Ambient service (observer + notifier + sampler + security) | ✅ |
| `friday4 security scan [path] [--threshold] [--json]` | Full security scan | ✅ |
| `friday4 security status` | Tool availability overview | ✅ |
| `friday4 proactive status/suggest/learn/brief/observe/watch` | Proactive intelligence | ✅ |
| `friday4 intelligence status/drift/anomaly/health/predict` | Intelligence layer | ✅ |
| `friday4 doctor` | One-command subsystem diagnostics | ✅ |
| `friday4 status` | Unified layer overview + `db status` | ✅ (Wave 9) |
| `friday4 web [--host] [--port]` | Local web dashboard | ✅ |
| `friday4 collab start/status/peers/obs/add/share/perms` | Collaboration layer | ✅ (Wave 5) |
| `friday4 talk "…"` | NL brain — say it, Friday does it (gate → sandbox → audit) | ✅ (Wave 9) |
| `friday4 ask "…"` | Evidence-cited answers (no answer without evidence) | ✅ (Wave 9) |
| `friday4 execute` | Direct executor access (shell/git/file/python/testing) | ✅ (Wave 9) |
| `friday4 voice talk` | Interactive voice session (hotword + push-to-talk) | ✅ |
| `friday4 memory store/recall/forget/list/status` | Memory & Identity | ✅ (Wave 10) |
| `friday4 persona profile/remember` | Persona (explicit-consent) | ✅ (Wave 10) |
| `friday4 relationship status/refresh` | Relationship depth | ✅ (Wave 10) |
| `friday4 skills list/learn/promote/shadow/status` | Shadow-first skills | ✅ (Wave 10) |
| `friday4 analyze/correlate/briefing/narrative/report` | Research & Reflection | 🆕 Wave 11 |
| `friday mobile pair` (future) | Mobile companion | ⏳ Wave 7 |
| `friday4 ide detect/diagnose/symbols/open/reveal/run` + "what's wrong with X" | IDE Integration (LSP + AST + editor control) | ✅ Wave 6 |

---

*This plan is a living document. As we build each wave, we update and refine
the subsequent waves based on what we learn. V4 is the product — its own
daemon, CLI, config, state, and tests — with V3 as optional read-only heritage.*
