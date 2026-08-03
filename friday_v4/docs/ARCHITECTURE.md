# Friday V4 — Architecture Reference

**Status:** Active development (ADR record — PLAN.md is the living authority)
**Date:** 2026-07-30 (updated 2026-08-01 for the V3-boundary decision)
**Supersedes:** V3 architecture document for V4-specific decisions

> **⚠️ Update note (2026-08-01):** The V3-boundary decision in PLAN.md
> supersedes the older "selective V3 imports" language in this doc. V4 now
> imports **no V3 code** — the single touchpoint is `proactive/v3source.py`,
> a read-only sqlite bridge. The ADRs below reflect that. Waves 9–11 (see
> `WAVE_9_AGENCY_CORE.md`, `WAVE_10_MEMORY_IDENTITY.md`,
> `WAVE_11_RESEARCH_REFLECTION.md`) add the reasoning/memory/execution core.

---

## ⭐ Status: V4 Is the Main Project

**V4 is the project being actively built.** V3 is largely built but
inconsistent — it is NOT the foundation V4 is subordinated to. Per the
V3-boundary decision (PLAN.md §3), V4 imports **no V3 code**: the single
touchpoint is `proactive/v3source.py`, a read-only sqlite bridge to
`~/.friday/friday.db`. Everything V4 needs (voice, desktop, security,
proactive intelligence, and — from Waves 9–11 — reasoning, memory, and
execution) is built V4-native.

## Architectural Invariants

Laws carried forward from V3 that V4 chooses to honor (V4 is the main
project; these are adopted because they're sound, not because V3 mandates
them):

1. **Reality First:** Reality is the only source of truth.
2. **Knowledge Is Evidence:** Every knowledge entry cites evidence.
3. **Reasoning Never Mutates Reality:** Reasoning is deterministic; V4's
   interaction modes don't mutate the world behind the operator's back.
4. **Single Responsibility:** Every V4 layer owns exactly one
   responsibility.
5. **Downward Dependencies Only:** V4 layers depend downward only. V3 is
   never a dependency — the read-only `v3source.py` bridge is the only
   touchpoint and nothing depends on V4.
6. **Determinism First:** Voice/mobile/desktop are interaction surfaces;
   the reasoning core remains deterministic.
7. **Wire Before Done:** A feature is not complete until it is wired into
   every consumer that should reach it — voice/CLI/web entry points,
   daemon schedules, reasoning providers, and briefings. "Follow-up
   suggestion" is not an acceptable definition of done.

---

## The Wiring Law

**Every capability ships with its wiring.** A layer is done when its module
+ tests pass **and** every existing consumer that should reach it does:

- **Entry points** — voice (`VoiceRouter`), CLI (`friday4 talk/ask/…`), and
  web (`/api/talk`) all route through the shared `nl_router`/reasoning
  brain; a new intent type reaches all of them.
- **Daemon schedules** — anything time-driven (decay sweeps, scans,
  samplers) gets a daemon component in the same change.
- **Reasoning providers** — new state (memories, relationships, skills)
  gains a provider so ASK answers cite it.
- **Consumers** — briefings, proactive suggestions, and status feeds
  surface the new state where the design says they should.
- **CLI surface** — a new layer gets its `friday4 <layer> …` command
  (as promised by its wave doc), not just a library API.

**Checklist before a layer is called done:**
- [ ] Every entry point that should reach it does (voice / CLI / web)?
- [ ] Does the daemon run the scheduled maintenance it needs (decay,
      refresh)?
- [ ] Does the reasoning layer have a provider citing its state?
- [ ] Does a CLI command expose it (`friday4 <layer> …`)?
- [ ] Is the existing consumer (briefing/status/proactive) updated?
- [ ] Are the wiring tests hermetic and green?

**Origin:** `memory/` (Wave 10) shipped while `reasoning/providers.py`
still read the raw `memories` table instead of the typed layer, the daemon
ran no decay sweep, no `friday4 memory` CLI existed, and `VoiceRouter`
dropped ASK intents before they reached reasoning. Those gaps were caught
as follow-up suggestions instead of at build time — this law makes that a
process violation.

---

## V4-Specific Architecture Decisions

### ADR-1: V4 Lives in a Separate Package (Main Project)

**Decision:** `friday_v4/` is the main project — a separate Python package
that imports **no V3 code**. V3 is optional legacy *data* read through the
`proactive/v3source.py` bridge (`mode=ro` sqlite). V4 is NOT a fork or
subpackage of V3 and is NOT built on a "frozen core" it must preserve.

**Rationale:** V3 is largely built but inconsistent. V4 is developed on its
own terms with its own roadmap. Every V3 capability V4 wants (persona,
ambient feed, db, missions, executors, memory) is rebuilt V4-native —
clean, stdlib-first, hermetic-tested.

**Consequence:** V4 has zero runtime dependency on V3. If a V3 module is
missing or broken, V4 is unaffected; V3's *capability map* is design
reference only. Waves 9–11 rebuild the brain (see WAVE docs).

### ADR-2: V4 Owns Its Daemon; V3 Pieces Reused Where Solid

**Decision:** V4 owns its own daemon and services (`friday_v4/daemon.py` —
observer + notifier + sampler + security scanner, one process). V3's
daemon is never launched or wrapped; V4's daemon is fully standalone.

**Rationale:** V3 is largely built but inconsistent. Building the daemon
V4-native keeps momentum without inheriting V3's inconsistencies, and it
makes "V4 works with zero V3 code present" literally true.

**Consequence:** V4's daemon is the product's own. Ambient notifications
read V3's feed via the read-only bridge when present; everything else is
V4 state.

### ADR-3: Voice Is an Interaction Mode, Not a New Pipeline

**Decision:** Voice is an interaction mode, not a new pipeline. The V4
`VoiceRouter` (`voice/router.py`) routes utterances desktop → proactive →
fallback today; Wave 9 upgrades it to route through the shared
`understanding/` NLU + `reasoning/` answer engine, and Wave 10 adds
persona/memory/relationship so voice responses are persona-aware.

**Rationale:** Duplicating conversation logic for voice would violate
single responsibility. One shared NLU surface (voice, CLI, web) keeps
behavior identical everywhere.

**Consequence:** Voice inherits V4's identity, memory, and reasoning —
rebuilt V4-native (Waves 9–10), never by importing V3's persona engine.

### ADR-4: Multi-Instance Uses CRDT for Observation Sync

**Decision:** Observations are synchronized between instances using
Conflict-Free Replicated Data Types (CRDT). Each instance writes locally,
and sync merges are commutative, associative, and idempotent.

**Rationale:** CRDTs avoid the complexity of distributed locking, conflict
resolution, and centralized coordination. Append-only observations (V3
Law 20) are naturally CRDT-friendly.

**Consequence:** Sync is eventually consistent. Two instances observing the
same repo simultaneously may see different observation counts until sync
completes. This is acceptable for a local-first system.

### ADR-5: Cross-Platform WM Uses Adapter Pattern

**Decision:** A `DesktopAbstraction` base class with platform-specific
adapters. Each adapter implements the same interface using the platform's
native API (Hyprland IPC, GNOME D-Bus, macOS Accessibility, Win32).

**Rationale:** V3's Hyprland executor is tightly coupled to a single WM.
The adapter pattern allows progressive platform support without altering
the consumer API.

**Consequence:** Each platform requires its own adapter implementation.
Tests use a mock adapter for platform-independent testing.

---

## V4 Data Flow

### Voice Interaction Flow
```
User speaks → Microphone → VAD (voice activity detection)
                                   ↓
                     Hotword detected? → No → Discard
                                   ↓ Yes
                     STT (speech-to-text) → "Hey Friday, what's new?"
                                   ↓
                     VoiceRouter → Wave 9 understanding/ NLU (intent)
                                   ↓
                     Wave 9 reasoning/ (evidence-cited answer)
                                   ↓
                     TTS (text-to-speech) → "3 repos changed today..."
                                   ↓
                     Speaker → User hears
```

### Desktop Monitoring Flow
```
Desktop Adapter → Window change detected
                       ↓
           V4 Desktop Observer → V4 event (Wave 11 ambient/ bus)
                       ↓
           V4 memory/db → Dashboard + proactive engine
                       ↓
           (Optional) Voice notification: "You just switched to VS Code"
```

### Security Scanning Flow
```
V4 Daemon Cycle Completion
       ↓
Trigger security scan on changed repos
       ↓
Scanner → vulnerabilities? → V4 event → desktop notification (HIGH)
       ↓                       ↓
Secrets detector → secrets? → V4 event (critical urgency)
       ↓
Quality gates → issues? → V4 event (normal urgency)
       ↓
All clean → V4 event: "Security scan passed"
```

### Multi-Instance Sync Flow
```
Instance A observes repo
       ↓
Instance A writes observation to local CRDT store
       ↓
Instance A broadcasts observation via TCP JSON-lines (stdlib)
       ↓
Instance B receives observation
       ↓
Instance B merges via CRDT (LWW per source:subject:aspect)
       ↓
Instance B persists merged observation
       ↓
Instance B can now answer: "Instance A also saw changes in repo X"
```

---

## Module Dependency Graph

```
friday_v4/
│
├── voice/           → understanding/ (Wave 9) · reasoning/ (Wave 9)
│                    → (external, optional) whisper / piper / edge-tts
│
├── desktop/         → (platform-specific APIs) · db.py (Wave 9)
│   └── ide/         → (editor extension APIs, Wave 6)
│
├── mobile/          → (future Wave 7) · ambient/ bus (Wave 11)
│
├── collab/          → pure stdlib (TCP/UDP) — no external deps
│
├── security/        → db.py (Wave 9) · ambient/ bus (Wave 11)
│                    → (external, optional) pip-audit / ruff / trufflehog
│
├── intelligence/    → proactive/ · db.py (Wave 9)
│
├── network/         → SSH executor folded into execution/ (Wave 12);
│                      cloud/webhook/db-client remain future
│
├── proactive/       → db.py (Wave 9) · memory/ (Wave 10)
│   └── v3source.py  → ~/.friday/friday.db (read-only, THE only V3 touchpoint)
│
└── reasoning/ · understanding/ · missions/ · execution/
                     → (Waves 9–11 brain core) · db.py (Wave 9)
```

---

## Testing Strategy

### V3 Boundary (read-only bridge)
- `v3source.py` is the ONLY V3 touchpoint; tests cover graceful fallback
  (missing DB / missing schema → empty results, never a crash)
- V3's broader suite (1,656 tests) is V3's own concern, not V4's gate

### V4 Unit Tests (390 today → ~680 after Waves 9–11)
- Hermetic: no real `~/.friday` writes (tmp_path connections)
- Mock all external services (STT, TTS, scanners, tools)
- Test each V4 module in isolation

### V4 Integration Tests
- Voice: record test audio → STT → expect text
- Desktop: mock adapter → test abstraction
- Collab: two local instances → sync → verify
- Security: test repo with known vulnerabilities → expect findings

### Performance Tests
- Voice round-trip latency < 2 seconds (local models)
- Sync propagation < 5 seconds (same LAN)
- Security scan < 30 seconds (typical repo)

---

## Error Handling

All V4 modules follow Friday's error pattern:

1. **Never crash the daemon** — wrap all external calls in try/except
2. **Never fabricate** — if a service is unavailable, report the failure
3. **Log everything** — log all daemon events

### Service Degradation

| Service Down | V4 Behavior |
|-------------|-------------|
| STT (Whisper) | Voice mode unavailable; text input still works |
| TTS (Piper) | Voice responses disabled; text shown in CLI/feed |
| Security scanner | Cycle continues; scanning skipped; warning emitted |
| Collab peer | Sync paused; local operation continues independently |
| Desktop adapter | Desktop context unavailable; monitors screen/text only |
| Mobile push | Notifications queued; delivered on reconnection |

---

## Configuration

V4 configuration lives in `~/.friday/v4_config.json` (separate from V3's
`daemon.status`). Key configuration categories:

```json
{
  "voice": {
    "enabled": true,
    "stt_model": "whisper-small",
    "tts_model": "piper",
    "hotword": "hey friday",
    "hotword_sensitivity": 0.7,
    "vad_mode": 1,
    "push_to_talk_key": "ctrl+shift+space"
  },
  "desktop": {
    "enabled": true,
    "wm": "auto",
    "system_tray": true,
    "global_hotkeys": true
  },
  "collab": {
    "enabled": false,
    "discovery": "udp_beacons",   // pure stdlib (replaced mdns)
    "workspace_name": null,
    "sync_interval_seconds": 30
  },
  "security": {
    "enabled": true,
    "scan_on_change": true,
    "scan_interval_minutes": 60,
    "vulnerability_severity_threshold": "medium",
    "secret_detection": true
  },
  "mobile": {
    "enabled": false,
    "push_enabled": true,
    "quiet_hours_start": null,
    "quiet_hours_end": null
  },
  "intelligence": {
    "drift_detection": true,
    "anomaly_detection": true,
    "predictive_analytics": false
  }
}
```
