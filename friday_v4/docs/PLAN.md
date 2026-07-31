# Friday V4 — Master Plan

> **Vision:** An ambient, proactive, multi-modal AI partner that understands,
> anticipates, and acts across every surface of your engineering life.
>
> Inspired by MCU Tony Stark's FRIDAY — voice-controlled, environment-aware,
> proactively intelligent, deeply integrated, and personally loyal.

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

**Date:** July 30, 2026
**Status:** Planning Phase
**Parent:** Friday V3 (frozen core, ~106K LOC, 1656 tests)
**Approach:** Hybrid — keep V3's architecture laws and pipeline, re-architect
the communication layer, add multi-instance support.

### Why V4?

Friday V3 is an extraordinary foundation — a fully realized AI operating partner
with a complete pipeline from observation to execution to self-improvement. But
it was designed for a single-user, single-machine, CLI-driven world.

V4 is the leap to:
- **Voice-first interaction** — speak to Friday like Tony Stark
- **Cross-platform presence** — every device, every OS
- **Proactive intelligence** — anticipates needs before you ask
- **Multi-instance collaboration** — Friday works with teams too
- **Security & quality** — code health as a first-class capability

### The Hybrid Promise

- ✅ V3's **25 Architecture Laws** remain inviolable
- ✅ V3's **frozen core** (Brain, Observation, Context, Knowledge) stays untouched
- ✅ V3's **pipeline** (Reality → Repair) continues to run as-is
- 🆕 V4 adds **new layers above and beside** the V3 core
- 🆕 V4 re-architects **communication** (voice, mobile, web)
- 🆕 V4 adds **multi-instance** coordination on top of V3's single-instance DB

---

## 2. Design Philosophy

### Core Tenets

1. **Voice-Native** — Every V4 interface speaks. Text is the fallback, not the primary.
2. **Ambient, Not Intrusive** — Friday is present but quiet. It interrupts only for what matters.
3. **Cross-Surface** — Desktop, mobile, web, terminal, IDE — Friday is everywhere you code.
4. **Proactive, Not Reactive** — V3 reacts to cycles. V4 anticipates needs.
5. **Federated, Not Centralized** — V4 runs locally but can coordinate with peers.
6. **Secure By Default** — Every new capability assumes least privilege.
7. **Learn Continuously** — Every interaction trains the model of you.

### What Stays from V3

| V3 Feature | V4 Strategy |
|-----------|-------------|
| 25 Architecture Laws | **Frozen.** Inviolable. |
| Observation Engine | **Frozen.** New observers plug in. |
| Context Engine | **Frozen.** New signals added inside. |
| Knowledge Engine | **Frozen.** New knowledge types. |
| Understanding Engine | **Frozen.** New detectors. |
| The Brain (ask pipeline) | **Frozen.** Bug fixes only. |
| Planning → Runtime pipeline | **Extended.** V4 adds new executor types. |
| Executors (18) | **Kept.** V4 adds more. |
| Persona Engine | **Extended.** Voice personality layer on top. |
| Ambient Feed | **Extended.** Multi-channel routing. |
| Autonomy System | **Extended.** Per-instance + per-user config. |

### What Changes

| V3 Limitation | V4 Solution |
|-------------|-------------|
| Single-instance DB | Multi-instance with CRDT-based observation sync |
| CLI-only interaction | Voice + Mobile + Web + Desktop + CLI |
| Hyprland-only WM | Cross-platform desktop abstraction (Hyprland/GNOME/KDE/macOS/Windows) |
| Polling daemon cycle | WebSocket + push-based real-time events |
| No IDE integration | VS Code / IntelliJ extension |
| No voice | Speech-to-text + text-to-speech + hotword detection |
| No mobile | Companion app (React Native) |
| No collaboration | Team workspaces, shared observations, permissions |
| No security | Dependency scanning, secret detection, quality gates |

---

## 3. V3 Inheritance

### What We Keep from V3

The entire V3 codebase at `src/friday/` is inherited. V3 modules are
**imported, not forked.** The `FRIDAY_CORE_FROZEN.md` contract (frozen
modules: Brain, Observation Engine, Context, evidence_scope, portfolio,
identity) remains in effect. V4 code lives in `friday_v4/` and depends
on V3's public APIs — never on its internals.

### V3 Import Surface (V4's Public API)

```python
# These V3 modules form V4's API surface. Changes here need V4 review.
from friday.ask import ask
from friday.db import connect, get_repositories, now_iso
from friday.observation import ObservationEngine, default_registry
from friday.observe import refresh
from friday.knowledge.engine import KnowledgeEngine
from friday.ambient import push_event, AmbientEvent
from friday.persona.engine import IdentityEngine
from friday.runtime.engine import RuntimeEngine
from friday.runtime.executors import resolve_executor, execute_with_fallback
from friday.autonomy import is_kill_switch_active
from friday.memory import WorkingMemory, MemoryEngine
```

### Test Inheritance

All 1,656 V3 tests must continue to pass. V4 adds its own test suite
at `friday_v4/tests/`. A CI gate ensures no V4 change breaks V3 tests.

---

## 4. Architecture Overview

### V4 Layered Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     FRIDAY V4 SURFACES                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │  Voice   │ │  Mobile  │ │   Web    │ │  Desktop │ │  CLI │ │
│  │ Interface│ │  App     │ │ Dashboard│ │ Extension│ │(V3)  │ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └──┬───┘ │
│       │            │            │            │          │      │
├───────┴────────────┴────────────┴────────────┴──────────┴──────┤
│                  FRIDAY V4 COMMUNICATION BUS                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │  Voice   │ │ WebSocket│ │  Push    │ │ Multi-Instance   │  │
│  │ Pipeline │ │  Server  │ │Notif.   │ │  Coordinator     │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────────┬─────────┘  │
│       │            │            │                │             │
├───────┴────────────┴────────────┴────────────────┴─────────────┤
│                FRIDAY V4 INTELLIGENCE LAYER                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │  Drift   │ │ Security │ │  Health  │ │ Predictive│          │
│  │ Detection│ │ Scanning │ │ Dash.    │ │ Analytics│          │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘          │
│       │            │            │            │                 │
├───────┴────────────┴────────────┴────────────┴─────────────────┤
│                  FRIDAY V3 CORE (Frozen)                         │
│  Reality → Observation → Context → Knowledge → Understanding → │
│  Initiatives → Insights → Brain → Planning → Task Graph →      │
│  Resolver → Scheduler → Runtime → Review → Repair              │
│                                                                 │
│  V3 Modules: 273 Python files, 106K LOC, 1656 tests            │
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
├── specs/                             # Formal specifications
│   └── api.yaml                       # V4 API contract (OpenAPI)
│
├── src/friday_v4/
│   ├── __init__.py
│   │
│   ├── voice/                         # Voice Interface Layer
│   │   ├── __init__.py
│   │   ├── stt.py                     # Speech-to-text (Whisper)
│   │   ├── tts.py                     # Text-to-speech
│   │   ├── hotword.py                 # Hotword/wake word detection
│   │   ├── pipeline.py                # Voice processing pipeline
│   │   ├── router.py                  # Voice → V3 persona routing
│   │   └── vad.py                     # Voice activity detection
│   │
│   ├── desktop/                       # Desktop Integration Suite
│   │   ├── __init__.py
│   │   ├── wm_abstraction.py          # Cross-platform WM abstraction
│   │   ├── hyprland_adapter.py        # Hyprland backend
│   │   ├── gnome_adapter.py           # GNOME backend
│   │   ├── kde_adapter.py             # KDE backend
│   │   ├── macos_adapter.py           # macOS backend
│   │   ├── windows_adapter.py         # Windows backend
│   │   ├── tray.py                    # System tray integration
│   │   ├── hotkeys.py                 # Global hotkeys
│   │   └── ide/
│   │       ├── __init__.py
│   │       ├── lsp_client.py          # LSP protocol client
│   │       ├── vscode_extension/      # VS Code extension
│   │       └── jetbrains_plugin/      # IntelliJ plugin
│   │
│   ├── mobile/                        # Mobile Companion
│   │   ├── __init__.py
│   │   ├── api.py                     # REST/WS API for mobile clients
│   │   ├── push.py                    # Push notification service
│   │   └── app/                       # React Native app source
│   │
│   ├── collab/                        # Collaboration Layer
│   │   ├── __init__.py
│   │   ├── coordinator.py             # Multi-instance coordinator
│   │   ├── crdt.py                    # CRDT-based observation merge
│   │   ├── peer.py                    # Peer discovery & connection
│   │   ├── permissions.py             # Access control
│   │   └── sync.py                    # Real-time sync engine
│   │
│   ├── security/                      # Security & Quality
│   │   ├── __init__.py
│   │   ├── scanner.py                 # Vulnerability scanner
│   │   ├── secrets.py                 # Secret detection
│   │   ├── deps.py                    # Dependency auditor
│   │   ├── quality.py                 # Code quality gates
│   │   └── reporter.py               # Security report generator
│   │
│   ├── intelligence/                  # Advanced Intelligence
│   │   ├── __init__.py
│   │   ├── drift.py                   # Predictive drift detection
│   │   ├── anomaly.py                 # Anomaly detection
│   │   ├── health.py                  # Code health diagnostics
│   │   ├── predictor.py               # Predictive analytics
│   │   └── learner.py                 # Continuous learning engine
│   │
│   ├── network/                       # Network & Remote
│   │   ├── __init__.py
│   │   ├── ssh.py                     # SSH executor
│   │   ├── webhook.py                 # Webhook listener
│   │   ├── cloud.py                   # Cloud API integration
│   │   └── db_client.py               # Database client executor
│   │
│   └── proactive/                     # Proactive Intelligence
│       ├── __init__.py
│       ├── anticipation.py            # Need anticipation engine
│       ├── scheduler.py               # Intelligent scheduling
│       ├── context_engine.py          # Deep context understanding
│       └── priority.py                # Priority inference
│
├── tests/                             # V4 test suite
│   ├── test_voice_pipeline.py
│   ├── test_desktop_abstraction.py
│   ├── test_collab_crdt.py
│   ├── test_security_scanner.py
│   └── ...
│
└── pyproject.toml                     # V4 project configuration
```

---

## 5. Phase Roadmap

### Phase 0 — Foundation (Weeks 1-2)
**Goal:** V4 project structure, CI, shared infrastructure, SSoT with V3

**Deliverables:**
- [x] `friday_v4/` directory scaffolded
- [x] `PLAN.md` (this document)
- [x] `ARCHITECTURE.md` — V4 architecture reference
- [x] `pyproject.toml` — V4 dependencies
- [ ] CI pipeline (V3 tests + V4 tests)
- [ ] V3 API compatibility layer
- [x] `friday4` CLI entry point (wraps V3 CLI + adds V4 commands)

### Phase 1 — Voice Interface (Weeks 3-5)
**Goal:** Talk to Friday like Tony Stark talks to FRIDAY

**Deliverables:**
- [x] Speech-to-text pipeline (faster-whisper / whisper.cpp)
- [x] Text-to-speech pipeline (kokoro-onnx / piper / edge-tts / pyttsx3)
- [x] Voice activity detection (Silero / WebRTC)
- [x] Hotword/wake word ("Hey Friday" via openwakeword)
- [x] Voice → V3 persona engine routing (VoiceRouter: desktop → proactive → V3 → fallback)
- [x] `friday4 talk` — interactive voice session
- [x] Desktop push-to-talk hotkey

**Key Integration Points:**
- Voice pipeline outputs text → feeds into V3 `IdentityEngine.process()`
- V3 `IdentityEngine` response → read aloud via TTS
- Ambient events trigger spoken notifications

### Phase 2 — Desktop Integration (Weeks 6-8)
**Goal:** Friday controls your entire desktop environment

**Deliverables:**
- [x] Cross-platform WM abstraction API
- [x] Hyprland adapter (ported from V3)
- [x] GNOME adapter
- [x] KDE adapter
- [x] macOS adapter
- [x] Windows adapter
- [x] System tray icon (all platforms)
- [x] Global hotkey registration
- [x] `friday4 desktop` — desktop control CLI
- [x] Desktop notification channel (V3 ambient → desktop overlay)
- [x] `friday4 daemon` — persistent ambient service wiring desktop watcher + notifier + proactive observer

**Key Integration Points:**
- WM abstraction replaces V3's Hyprland-only executor
- System tray shows daemon status + feed count
- Global hotkey triggers voice session

### Phase 3 — Security & Quality (Weeks 9-11)
**Goal:** Friday actively protects and improves your code

**Deliverables:**
- [ ] Dependency vulnerability scanner (OSV/Grype/Snyk)
- [ ] Secret detection (truffleHog/Gitleaks)
- [ ] Code quality gates (linters, formatters, type checkers)
- [ ] Continuous security dashboard
- [ ] Automated PR annotations
- [ ] `friday security scan` / `friday quality check`

**Key Integration Points:**
- Results feed into V3 ambient events
- Findings create V3 initiatives ("Fix X vulnerabilities")
- Security executors register in V3 worker registry

### Phase 4 — Collaboration (Weeks 12-14)
**Goal:** Multiple Friday instances, team workspaces

**Deliverables:**
- [ ] CRDT-based observation merge
- [ ] Peer discovery via mDNS
- [ ] WebSocket real-time sync
- [ ] Shared workspace permissions
- [ ] Team observation feeds
- [ ] `friday collab` — collaboration CLI

**Key Integration Points:**
- Sync layer sits between V3 DB and remote peers
- Observations replicated across instances
- Permissions filter what each instance can see/do

### Phase 5 — Mobile & Web (Weeks 15-17)
**Goal:** Friday in your pocket, Friday in your browser

**Deliverables:**
- [ ] React Native companion app
- [ ] Push notification transport
- [ ] Quick status glance UI
- [ ] Voice input on mobile
- [ ] Web dashboard (V3 ambient feed + V4 security/health)
- [ ] `friday web` — start web server

**Key Integration Points:**
- Mobile app authenticates via local network
- Push notifications from V4 daemon → device
- Web dashboard reads V3 ambient feed + V4 intelligence data

### Phase 6 — Advanced Intelligence (Weeks 18-20)
**Goal:** Friday anticipates your needs

**Deliverables:**
- [x] Predictive drift detection (time-series analysis)
- [x] Anomaly detection in execution patterns
- [x] Code health diagnostics (complexity, coverage, churn)
- [x] Need anticipation engine (context + history → predictions)
- [x] Automated workflow suggestions
- [x] Self-improving via user correction learning
- [x] V3 data wiring: anticipation reads V3 observations/action_log via `V3DataSource` (graceful fallback)
- [x] `friday4 doctor` — one-command subsystem diagnostics
- [x] `friday4 status` — unified layer overview

### Phase 7 — IDE Integration (Weeks 21-22)
**Goal:** Friday lives inside your editor

**Deliverables:**
- [ ] VS Code extension
- [ ] LSP client for code analysis
- [ ] Inline code review
- [ ] Quick actions from editor
- [ ] Status bar integration

### Phase 8 — Polish & Scale (Weeks 23-24)
**Goal:** Production-ready V4

**Deliverables:**
- [ ] Performance benchmarks (V3 vs V4)
- [ ] Full test suite (V3 + V4)
- [ ] Documentation site
- [ ] Installation script
- [ ] Migration guide (V3 → V4)
- [ ] Dogfooding period

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
| Hyprland | `hyprctl` / IPC | ✅ Port from V3 |
| GNOME | `gdbus` / Extensions | 🔄 Planned |
| KDE | `qdbus` / KWin script | 🔄 Planned |
| macOS | Accessibility API / Scripting Bridge | 🔄 Planned |
| Windows | Win32 API / PowerToys | 📅 Later |

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
# Conceptual API
from friday_v4.security import Scanner

scanner = Scanner()
report = scanner.scan("/path/to/project")

# Results
for vuln in report.vulnerabilities:
    print(f"{vuln.severity}: {vuln.package} — {vuln.cve_id}")

for secret in report.secrets:
    print(f"Secret: {secret.type} in {secret.file}:{secret.line}")

for issue in report.quality_issues:
    print(f"{issue.checker}: {issue.message}")
```

### 6.5 Proactive Intelligence

```python
# Conceptual API
from friday_v4.proactive import AnticipationEngine

engine = AnticipationEngine(conn)

# What should Friday do next?
suggestions = engine.suggest_next_actions()
# → ["Review 3 failed test files", "Update outdated deps", "Check drift on skill X"]

# Predict user intent from context
intent = engine.predict_intent(active_window="vscode", open_files=["main.py"])
# → "coding_session:maintenance"
```

---

## 7. Dependency Diagram

```
friday_v4
│
├── depends on friday (V3)          # V3 frozen core
│   │
│   ├── friday.ask                  # Q&A pipeline
│   ├── friday.db                   # Database
│   ├── friday.observation          # Observation engine
│   ├── friday.ambient              # Ambient feed
│   ├── friday.persona              # Identity engine
│   ├── friday.runtime              # Runtime engine
│   └── friday.autonomy             # Autonomy system
│
├── depends on external:
│   ├── whisper / deepgram          # STT
│   ├── piper / xtts                # TTS
│   ├── psutil                      # Desktop monitoring
│   ├── websockets                  # Real-time sync
│   └── (security scanners)         # OSV / Grype / truffleHog
│
└── depends on Python ≥3.12         # Same as V3
```

**Key Rule:** friday_v4 may import from friday (V3), but friday must NEVER
import from friday_v4. This preserves V3's independence and V4 can be
removed without affecting V3.

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Voice latency too high for real-time | Medium | High | Local models as primary, API fallback; push-to-talk mode |
| Multi-instance DB corruption | Low | Critical | CRDT merge, append-only replication, conflict docs |
| Breaking V3 frozen core | Medium | High | CI gate blocks V3 regressions; import-only contract |
| Cross-platform WM fragmentation | High | Medium | Abstract API first; implement adapters progressively |
| Security scanners too noisy | High | Medium | Tunable thresholds, per-project ignore lists |
| Mobile app adoption | Medium | Low | Web-first mobile experience, native app second |
| Performance regression from V4 layers | Low | Medium | Benchmarks in CI; profiling for critical paths |

---

## 9. Success Criteria

### V4 is successful when:

1. **Voice works end-to-end:** "Hey Friday, what's the status of my projects?" → Friday responds aloud
2. **Desktop cross-platform:** Friday can switch workspaces, launch apps, and report desktop state on at least 3 platforms
3. **Security scanning active:** Every daemon cycle scans changed dependencies and reports findings
4. **Multi-instance sync:** Two Friday instances can observe the same workspace without conflicts
5. **Mobile notifications:** Friday can push important alerts to your phone
6. **All V3 tests pass:** 1,656 V3 tests green
7. **V4 has its own test suite:** 500+ V4 tests
8. **Dogfooding:** The V4 team uses Friday V4 for daily engineering work

### Non-Goals for V4

- Cloud-hosted Friday (stays local-first)
- Realtime collaborative editing (not an IDE)
- General-purpose chatbot (stays engineering-focused)
- Platform-specific distribution (stores, packages — post-V4)

---

## Appendix A: V3 → V4 Migration Path

For existing V3 users:

1. Install V4 alongside V3 (`pip install friday-v4`)
2. V4 reads the same `~/.friday/` database
3. V3 CLI still works (all 40+ commands)
4. V4 adds: `friday talk`, `friday desktop`, `friday security`, `friday collab`
5. V4 daemon replaces V3 daemon (adds voice, notifications, sync)
6. Rollback: stop V4 daemon, restart V3 daemon — DB is compatible

## Appendix B: V4 CLI Commands (Planned)

| Command | Description | Phase |
|---------|-------------|-------|
| `friday talk` | Start interactive voice session | P1 |
| `friday talk --push-to-talk` | Push-to-talk voice mode | P1 |
| `friday desktop` | Desktop environment control | P2 |
| `friday desktop status` | Show desktop context | P2 |
| `friday desktop switch <ws>` | Switch workspace | P2 |
| `friday security scan [path]` | Full security scan | P3 |
| `friday security deps` | Dependency audit | P3 |
| `friday security watch` | Continuous security monitoring | P3 |
| `friday quality check` | Code quality gates | P3 |
| `friday collab join <workspace>` | Join team workspace | P4 |
| `friday collab status` | Collaboration status | P4 |
| `friday collab peers` | List connected peers | P4 |
| `friday web` | Start web dashboard | P5 |
| `friday mobile pair` | Pair with mobile app | P5 |
| `friday health` | Code health diagnostics | P6 |
| `friday predict` | Predictive insights | P6 |
| `friday anticipate` | Suggested next actions | P6 |

---

*This plan is a living document. As we build each phase, we update and refine
the subsequent phases based on what we learn. The frozen V3 core gives us a
stable foundation to iterate on.*
