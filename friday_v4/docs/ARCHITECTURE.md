# Friday V4 — Architecture Reference

**Status:** Planning
**Date:** 2026-07-30
**Supersedes:** V3 architecture document for V4-specific decisions

---

## Architectural Invariants (from V3, preserved in V4)

All 25 V3 Architecture Laws remain in effect. The following are especially
critical for V4:

1. **Law 1 — Reality First:** Reality is the only source of truth.
2. **Law 4 — Knowledge Is Evidence:** Every knowledge entry cites evidence.
3. **Law 8 — Brain Never Mutates Reality:** Brain reasons; V4 surfaces add
   new interaction modes but don't give the Brain new mutation powers.
4. **Law 18 — Single Responsibility:** Every V4 layer owns exactly one
   responsibility.
5. **Law 19 — Downward Dependencies Only:** V4 may depend on V3; V3 must
   never depend on V4.
6. **Law 21 — Determinism First:** Voice/mobile/desktop are interaction
   surfaces; the reasoning core remains deterministic.

---

## V4-Specific Architecture Decisions

### ADR-1: V4 Lives in a Separate Package

**Decision:** `friday_v4/` is a separate Python package that imports from
`friday` (V3). It is NOT a fork or subpackage of V3.

**Rationale:** This enforces Law 19 (downward dependencies). V4 can be
installed alongside V3, removed without affecting V3, and developed with
its own dependency tree. V3 remains independently testable.

**Consequence:** V4 must use V3's public API only. Internal V3 modules
are not accessible. If V4 needs a new V3 API, it's added to V3's public
surface — never by reaching into a private module.

### ADR-2: V3 Daemon Becomes V4's Backend

**Decision:** V4 does NOT replace the V3 daemon. It wraps it. The V4 daemon
adds voice, desktop, collaboration, and security checks on top of the V3
cycle. The V3 daemon continues to run the core observation → learning
pipeline.

**Rationale:** The V3 daemon is a frozen module with 1,656 passing tests.
Replacing it risks regression. Wrapping it preserves the frozen contract.

**Consequence:** The V4 daemon starts the V3 daemon as a subprocess and
adds its own services. When V4 is stopped, V3 daemon continues running.

### ADR-3: Voice Is an Interaction Mode, Not a New Pipeline

**Decision:** Voice interfaces use V3's existing `IdentityEngine.process()`
as the routing layer. STT converts speech to text, feeds it to the persona
engine, and TTS converts the response back to speech.

**Rationale:** V3 already has a complete conversation pipeline with persona,
routing (ask/execute/chitchat), memory, and operator profiling. Duplicating
this for voice would violate Law 18 (single responsibility).

**Consequence:** All V3 personality features (name learning, preference
extraction, relationship depth, memory) work automatically with voice.
No new routing logic needed.

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
                     V3 IdentityEngine.process("what's new?")
                                   ↓
                     V3 ask pipeline → Answer text
                                   ↓
                     TTS (text-to-speech) → "3 repos changed today..."
                                   ↓
                     Speaker → User hears
```

### Desktop Monitoring Flow
```
Desktop Adapter → Window change detected
                       ↓
           V4 Desktop Observer → V3 Ambient Event
                       ↓
           V3 Working Memory → Dashboard
                       ↓
           (Optional) Voice notification: "You just switched to VS Code"
```

### Security Scanning Flow
```
V4 Daemon Cycle Completion
       ↓
Trigger security scan on changed repos
       ↓
Scanner → vulnerabilities? → Push to ambient feed
       ↓                       ↓
Secrets detector → secrets? → Push to ambient feed (HIGH priority)
       ↓
Quality gates → issues? → Push to ambient feed
       ↓
All clean → Ambient event: "Security scan passed"
```

### Multi-Instance Sync Flow
```
Instance A observes rep o
       ↓
Instance A writes observation to local DB
       ↓
Instance A broadcasts observation via WebSocket
       ↓
Instance B receives observation
       ↓
Instance B merges via CRDT (LWW per source:subject:aspect)
       ↓
Instance B writes merged observation to local DB
       ↓
Instance B can now answer: "Instance A also saw changes in repo X"
```

---

## Module Dependency Graph

```
friday_v4/
│
├── voice/           → friday.persona.engine
│                    → friday.db
│                    → (external) whisper / deepgram
│                    → (external) piper / elevenlabs
│
├── desktop/         → friday.db
│   ├── wm/         → (platform-specific APIs)
│   └── ide/        → (editor extension APIs)
│
├── mobile/          → friday.db
│                    → (external) websockets
│
├── collab/          → friday.db
│                    → (external) websockets
│                    → (external) zeroconf / mdns
│
├── security/        → friday.db
│                    → friday.ambient
│                    → (external) OSV / Grype / truffleHog
│
├── intelligence/    → friday.db
│                    → friday.knowledge
│                    → friday.observation
│
├── network/         → friday.db
│                    → (external) paramiko / ssh
│                    → (external) aiohttp
│
└── proactive/       → friday.db
                     → friday.memory
                     → friday.ambient
                     → friday.observation
```

---

## Testing Strategy

### V3 Tests (1,656)
- Never modified by V4
- Run in every V4 CI pipeline
- V4 failure is a V4 bug, not a V3 regression

### V4 Unit Tests (target: 500+)
- Mock all V3 dependencies (friday.* modules)
- Mock all external services (STT, TTS, scanners)
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

All V4 modules follow V3's error pattern:

1. **Never crash the daemon** — wrap all external calls in try/except
2. **Never fabricate** — if a service is unavailable, report the failure
3. **Log everything** — use V3's `_log()` for daemon events

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
    "discovery": "mdns",
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
