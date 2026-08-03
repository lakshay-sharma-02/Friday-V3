# Friday V4 — Refined Roadmap

> **Context:** Solo developer, no deadline, full-stack vision, multi-platform.
> **Total estimate:** 13–17 months for Waves 1–8; **~18–22 months** including the
> Wave 9–11 Agency Core (the brain — reasoning, memory, execution, research).
> **Strategy:** Shippable waves — every wave delivers visible MCU-Friday value.
> **Minimum MCU Friday (Voice + Desktop): ~3.5–5 months.**

---

## The Honest Math

Building a production-quality voice interface, cross-platform desktop control,
collaboration layer, security scanner, IDE integration, mobile app, proactive
intelligence — **plus the agency core (reasoning, memory, execution, research)**
— solo — is an **~18–22 month endeavor** (Waves 1–8 ≈ 13–17 months; Waves 9–11
≈ 4–5 months more). That's not scary — it's liberating. No deadline means we
build it right, ship waves that feel like progress, and never crunch.

**But here's the secret:** You stop after any wave and you have something real.
- Stop after **Wave 1**: You have a voice-controlled Friday. That's already MCU-level.
- Stop after **Wave 2**: You have voice + desktop. That's Iron Man's lab.
- Keep going to **Wave 12**: You have the complete Friday — surfaces *and* brain.

---

## Wave Strategy

```
Wave 1: VOICE ─────> "Hey Friday, what's new?" ─────────> ✅ SHIPPED
Wave 2: DESKTOP ───> Friday controls your environment ──> ✅ SHIPPED
Wave 3: SECURITY ──> Friday protects your code ─────────> ✅ SHIPPED
Wave 4: SMART ─────> Friday anticipates your needs ─────> ✅ SHIPPED
Wave 5: COLLAB ────> Friday works with teams ───────────> ✅ SHIPPED
Wave 6: IDE ───────> Friday lives in your editor ───────> ✅ SHIPPED (2026-08)
Wave 7: MOBILE ────> Friday in your pocket ─────────────> 10-12 weeks
Wave 9: AGENCY ────> Friday actually does things ───────> ✅ SHIPPED
Wave 10: IDENTITY ─> Friday knows you ──────────────────> ✅ SHIPPED
Wave 11: RESEARCH ─> Friday reasons across your world ──> ✅ SHIPPED
Wave 12: POLISH ───> Production-ready ──────────────────> ✅ SHIPPED
Wave 13: THINKING CORE ──> LLM synthesis over evidence ──> ✅ SHIPPED
Wave 13a: ONE NLU POINT ──> LLM-first resolve(), rules fallback ──> ✅ SHIPPED
Wave 14: WATCH ME ──────> demonstration capture ─────────────────> ✅ SHIPPED
```

> **Renumbering note:** the original Wave 8 slot ("Polish & Scale") is now
> **Wave 12**; Waves 9–11 are the new Agency Core (the brain). IDE (6) and
> Mobile (7) keep their numbers — **Wave 8 is intentionally left unused** to
> keep the new brain waves at 9–11 (matching the WAVE doc filenames). Full
> designs: `WAVE_9_AGENCY_CORE.md`, `WAVE_10_MEMORY_IDENTITY.md`,
> `WAVE_11_RESEARCH_REFLECTION.md`.

**Total: ~18–22 months solo (the original 8-wave scope ≈ 13–17 months + ~4–5
for Waves 9–11).** Each wave ships independently.

---

## Wave 1: Voice Interaction ✅ SHIPPED (2026-08)

**Status:** Built. `friday4 talk` (hotword + push-to-talk), `friday4 voice setup/status/test`.
TTS: kokoro-onnx / piper / edge-tts / pyttsx3 with auto-fallback. STT: faster-whisper.
VAD: Silero/WebRTC. Hotword: openwakeword (`hey_jarvis` ≈ "hey friday" — custom
retraining deferred). Barge-in interruption, voice modes, chimes, caching — all in.

**Goal:** Walk up to your dev environment, say "Hey Friday, what's going on?"
and hear Friday respond aloud. This is the single most transformative feature.

### What We Build

| Module | Files | Time | Prerequisites |
|--------|-------|------|---------------|
| `SpeechToText` | `voice/stt.py` | 5 days | Faster-Whisper (model download: ~2GB for small) |
| `TextToSpeech` | `voice/tts.py` | 3 days | Piper voice models (~50MB download) |
| `VoiceActivityDetector` | `voice/vad.py` | 2 days | webrtcvad |
| `HotwordDetector` | `voice/hotword.py` | 3 days | **Porcupine API key** (free account) |
| `AudioStream` | `voice/audio.py` | 2 days | **portaudio19-dev** (Linux), brew install portaudio (macOS) |
| `VoicePipeline` | `voice/pipeline.py` | 6 days | ❗ **Hardest part** — threading, buffering, state machine |
| `VoiceRouter` | `voice/router.py` | 2 days | V3 IdentityEngine |
| `friday talk` CLI | `cli_talk.py` | 3 days | V3 CLI framework |
| `friday voice setup` | `cli_voice_setup.py` | 2 days | — |
| Setup wizard | `voice/setup.py` | 2 days | — |
| Caching | `voice/cache.py` | 2 days | LRU, disk cache |
| Interruption handling | (in pipeline) | 4 days | ❗ Thread sync, audio buffer mgmt |

**Total: ~36 days → 7-9 weeks (including buffer)**

### Hidden Complexity (callout)
- **Audio pipeline threading** is the real challenge. VAD runs on audio frames,
  hotword detection runs on the same frames, STT needs utterance boundaries,
  and TTS needs its own output stream. Synchronizing all of this requires a
  well-designed state machine. Test with real microphone input, not just files.
- **Piper setup** requires a compiled binary or prebuilt download. Not a pip
  install — it's a C++ inference engine with Python bindings.
- **Porcupine** needs a free API key from Picovoice console. Account creation
  + key retrieval takes ~15 minutes but blocks the hotword feature.

### File Count
- **15 source files** + 10 test files = 25 files total for this wave.
- Each file needs: docstrings, type hints, error handling, tests.
- File creation alone takes ~20% of the wave time.

### Multi-Platform Status
| Platform | Effort | Dependencies |
|----------|--------|-------------|
| Linux | ✅ 1 day | `apt install portaudio19-dev python3-pyaudio` |
| macOS | ✅ 2 days | `brew install portaudio`; Piper via pip |
| Windows | ✅ 2 days | pyaudio via pip (pre-built wheels available) |

### What You Can Skip (Defer)
- Voice cloning, emotion detection, multi-language support
- Custom wake word training (use Porcupine's built-in "hey friday")
- Real-time streaming STT (process whole utterance at once)

### MCU Friday Feel
```
🎤 [You]: "Hey Friday, what's the status of my projects?"
🎧 [Friday]: "3 repositories changed since your last observation.
             codebuff has 12 new commits, vivaha has 3."
             → You feel like you're in the lab. This is it.
```

---

## Wave 2: Desktop Presence ✅ SHIPPED (2026-08)

**Status:** Built. WM abstraction + 5 adapters (Hyprland/GNOME/KDE/macOS/Windows),
system tray, global hotkeys, desktop watcher, V3 ambient → desktop notification
channel, proactive suggestion channel. `friday4 desktop status/windows/switch/
focus/launch/screenshot/platforms`. **`friday4 daemon start [--voice]`** runs the
whole ambient stack (observer + notifier + sampler) as one process.

**Goal:** Friday controls your desktop environment — windows, workspaces, apps,
system tray — on every platform you use.

### What We Build

| Module | Files | Time | Learning Curve |
|--------|-------|------|----------------|
| `DesktopAbstraction` (interface) | `desktop/wm_abstraction.py` | 3 days | Low — API design |
| `HyprlandAdapter` | `desktop/hyprland_adapter.py` | 2 days | Low — port from V3 |
| `GNOMEAdapter` | `desktop/gnome_adapter.py` | 7 days | **Medium** — D-Bus introspection, GNOME Shell API |
| `KDEAdapter` | `desktop/kde_adapter.py` | 5 days | Medium — KWin scripting, qdbus |
| `macOSAdapter` | `desktop/macos_adapter.py` | **12-14 days** | **HIGH** — Accessibility API, privacy perms, entitlements |
| `WindowsAdapter` | `desktop/windows_adapter.py` | 7-10 days | Medium — Win32 API, COM |
| `SystemTray` | `desktop/tray.py` | 3 days | Low — pystray |
| `GlobalHotkeys` | `desktop/hotkeys.py` | 3 days | Low — keyboard library |
| `friday desktop` CLI | `cli_desktop.py` | 3 days | Low |

**Total: ~25 days for first platform + 7-14 days per additional platform**

### Strategy: Progressive Platform Support

**Recommendation for solo dev:** Build adapters in this order:
1. **Hyprland** (2 days — reuse V3 code)
2. **GNOME** (7 days — most common Linux DE)
3. **macOS** (12-14 days — common dev platform, but HIGH learning curve)
4. **Windows** (7-10 days — largest user base)
5. **KDE** (5 days — lower priority, smaller user base)

Each adapter is independent. You can stop after Hyprland + GNOME and still
have a cross-platform desktop Friday for 80% of Linux users.

### Multi-Platform Test Strategy (for a solo dev)
| Platform | How to Test |
|----------|-------------|
| Hyprland | ✅ You're on it |
| GNOME | VM or dual boot |
| macOS | Borrow a Mac, or CI with macOS runner (GitHub Actions) |
| Windows | VM or CI with Windows runner |
| KDE | Docker with KDE Neon image, or CI |

### Hidden Complexity
- **macOS Accessibility API** requires the user to manually grant permissions
  in System Settings → Privacy & Security → Accessibility. This can NOT be
  automated. Your adapter code must handle the permission-denied case
  gracefully and show setup instructions.
- **GNOME Shell version fragmentation** — the D-Bus interface changed between
  GNOME 42, 43, and 44. Your adapter needs to detect the version and adapt.
- **Wayland vs X11** — Some APIs work differently (or not at all) under Wayland.
  Hyprland is Wayland-native, but GNOME on Wayland has different D-Bus paths.

### What You Can Skip (Defer)
- Full window management (start with: get active window, switch workspace)
- Multi-monitor orchestration
- IDE integration (that's Wave 6)

### MCU Friday Feel
```
🎤 [You]: "Friday, switch to my coding workspace."
🎧 [Friday]: "Switching to workspace 3."
             [Hyprland switches to workspace 3 where VS Code is open]
             → You just commanded your desktop by voice.
```

---

## Wave 3: Security & Quality ✅ SHIPPED (2026-08)

**Status:** Built. `VulnerabilityScanner` + `DependencyAuditor` + `SecretDetector`
+ `QualityGate` + `SecurityReport` in `security/`. Every scanner has a
**built-in, pure-stdlib implementation that always works** plus **optional
subprocess integrations** (pip-audit, trufflehog, ruff, mypy, bandit) that are
discovered **venv-aware** via `security/tooling.py` — tools installed in the
active venv's `bin/` are found even when they're not on PATH, so `security
status` / `doctor` report them truthfully. `friday4 security scan [path]
[--json] [--threshold N] [--no-deps|--no-secrets|--no-quality]`, `friday4
security status`. Findings stay V4-native (CLI output + exit code + daemon
state file + web dashboard; no V3 DB writes, per the V3-boundary decision).

**Goal:** Friday actively scans your projects for vulnerabilities, exposed
secrets, and code quality issues — and tells you about them proactively.

### What We Build

| Module | Files | Time | Tool |
|--------|-------|------|------|
| `VulnerabilityScanner` | `security/scanner.py` | 4 days | pip-audit (subprocess) |
| `DependencyAuditor` | `security/deps.py` | 3 days | pip-audit / safety CLI |
| `SecretDetector` | `security/secrets.py` | 3 days | truffleHog (subprocess) |
| `QualityGate` | `security/quality.py` | 3 days | ruff / mypy (subprocess) |
| `SecurityReport` | `security/reporter.py` | 2 days | — |
| `friday security scan` CLI | `cli_security.py` | 2 days | — |
| V3 ambient feed integration | — | 2 days | V3 ambient.push_event |

**Total: ~19 days → 4 weeks**

### Why Subprocess-Based Scanning
- **pip-audit** — best-in-class Python dependency scanner. CLI tool, parse JSON.
- **truffleHog** — best-in-class secret scanner. CLI tool, parse JSON.
- **ruff** — fastest Python linter. CLI tool, parse JSON.
- Running these as subprocesses avoids dependency hell and lets you use the
  best tools for each job.

### File Count
- **8 source files** + 5 test files = 13 files. Low complexity wave.

### Multi-Platform Status
| Platform | Effort | Notes |
|----------|--------|-------|
| Linux | ✅ | All tools available |
| macOS | ✅ | All tools available |
| Windows | ✅ | All tools available |

### MCU Friday Feel
```
🎤 [Friday]: "I found 3 vulnerabilities in your dependencies.
             CVE-2024-XXXX in requests v2.31.0 — HIGH severity.
             I've opened an initiative to fix these."
             → Friday is your code guardian.
```

---

## Wave 4: Proactive Intelligence ✅ SHIPPED (2026-08)

**Status:** Built. Drift detection, anomaly detection, code health diagnostics,
anticipation engine (context + patterns + session + priority), continuous
learner. Wired into V3's DB via `V3DataSource` (observations/actions/ambient —
graceful fallback when V3 absent). `friday4 proactive status/suggest/learn/brief/
observe`, `friday4 intelligence status/drift/anomaly/health/predict`. Plus
`friday4 doctor` / `friday4 status` ops tooling.

**Goal:** Friday doesn't just react — it anticipates. It notices patterns in
your behavior, predicts what you'll need, and offers help before you ask.

### What We Build

| Module | Files | Time | Complexity |
|--------|-------|------|------------|
| `DriftPredictor` | `intelligence/drift.py` | 5 days | Medium — moving avg + std-dev |
| `AnomalyDetector` | `intelligence/anomaly.py` | 5 days | Medium — statistical thresholding |
| `CodeHealthDiagnostics` | `intelligence/health.py` | 4 days | Low — metric collection |
| `AnticipationEngine` | `proactive/anticipation.py` | **12 days** | **HIGH** — pattern matching, freq analysis |
| `PriorityInference` | `proactive/priority.py` | 4 days | Medium — rule-based |
| `friday health` CLI | `cli_health.py` | 2 days | Low |

**Total: ~32 days → 6-8 weeks**

### Why the Anticipation Engine Takes 12 Days
It's not a simple lookup. It needs to:
1. Query V3 action_log for recent user actions
2. Query V3 working_memory for current context
3. Build frequency distributions: "when action X happens, action Y follows
   with probability Z"
4. Query calendar for upcoming events
5. Query desktop for current app/window
6. Combine all signals into ranked predictions
7. Format predictions as natural suggestions
8. Test that predictions are actually useful (not noise)

### Data Dependency
The anticipation engine needs history to make predictions. If the V3 daemon
hasn't been running long, there won't be enough data. The engine should
gracefully report "I'm still learning your patterns" when data is insufficient.

### File Count
- **8 source files** + 8 test files = 16 files. Medium complexity.

### MCU Friday Feel
```
🎤 [Friday]: (after you open main.py)
             "You're about to edit main.py — I ran the tests for you.
              All 156 pass. Would you like me to also run the linter?"
             → Friday saw what you were about to do and prepared.
```

---

## Wave 5: Collaboration ✅ SHIPPED (2026-08)

**Goal:** Multiple Friday instances can observe, sync, and coordinate. Team
workspaces share observations while keeping local control.

**What shipped:** ``collab/`` (``crdt.py`` LWW observation CRDT,
``peer.py`` discovery, ``sync.py`` sync, ``coordinator.py``,
``permissions.py`` ACLs) plus the ``friday4 collab`` CLI (start / status /
peers / observations / add / share / perms). **Pure stdlib**: discovery is
UDP JSON beacons and sync is a TCP JSON-lines protocol — the roadmap's
zeroconf/WebSocket choices were replaced deliberately to honor V4's
"pure-stdlib, always works" law (no new dependencies).

### What We Build

| Module | Files | Time | Notes |
|--------|-------|------|-------|
| `ObservationCRDT` | `collab/crdt.py` | 5 days | Simple LWW CRDT (append-only obs) |
| `PeerDiscovery` | `collab/peer.py` | 3 days | mDNS via zeroconf |
| `Coordinator` | `collab/coordinator.py` | 5 days | Peer management, state |
| `SyncEngine` | `collab/sync.py` | 4 days | WebSocket server + client |
| `PermissionManager` | `collab/permissions.py` | 3 days | Simple ACLs |
| `friday collab` CLI | `cli_collab.py` | 3 days | — |

**Total: ~23 days → 5 weeks**

### Why CRDT for Solo Dev?
V3 observations are append-only with deterministic IDs (Law 20). This makes
Last-Writer-Wins CRDT trivial — no conflict resolution library needed. You can
implement it in a single file.

### Firewall Consideration
WebSocket sync requires network port access. The sync engine should:
- Default to port 9876 (configurable)
- Support LAN-only binding
- Handle port conflicts gracefully

### File Count
- **7 source files** + 6 test files = 13 files. Low-medium complexity.

### MCU Friday Feel
```
[Two instances sync]
🎤 [Friday A]: "Your colleague's Friday noticed similar patterns in
               their repo. Want me to share the correlation?"
               → Friday is networked. Just like the Iron Legion.
```

---

## Wave 6: IDE Integration ✅ SHIPPED (2026-08)

**Goal:** Friday lives inside your editor — it knows which editor is
present, adapts to it, analyzes your code with a real language server,
and controls the editor (open / reveal / run). **Design:**
`WAVE_6_IDE.md`.

### What Actually Shipped

| Module | Files | Notes |
|--------|-------|-------|
| Editor detection | `desktop/ide/detection.py` | VS Code / JetBrains / Neovim / Sublime / Emacs from env (`TERM_PROGRAM`, `VSCODE_*`, `NVIM`), processes, and config dirs — adapts launcher argv per kind |
| LSP client | `desktop/ide/lsp_client.py` | **Pure stdlib** JSON-RPC 2.0 over stdio: initialize, didOpen, `textDocument/diagnostic` pull + publishDiagnostics fallback, documentSymbol. No pygls |
| Always-on analyzer | `desktop/ide/ast_analyzer.py` | `ast` fallback: syntax errors, undefined names, unused imports, shadowed builtins — Friday *always* has an opinion |
| Editor control | `desktop/ide/controller.py` | open / reveal / run, argv adapted per editor (`code -r -g file:line`, `idea --line`, `nvim +line`, `subl file:line`, `emacs +line`) + OS opener fallback |
| NL path | `Intent.IDE` + `nl_router._ide_response` | "what's wrong with src/main.py", "diagnose auth.py", "lint X", "why won't this compile" → diagnostics on every surface (talk / voice / web) |
| Reasoning | `QuestionType.CODE` + `code_provider` | "what's wrong with X" asked as a question → evidence-cited `v4.ide.*` answer |
| CLI | `cli_ide.py` | `friday4 ide detect/diagnose/symbols/open/reveal/run` — `run` goes through the gated execution pipeline |
| Composition | `executors.py` + `nl_router` | `FRIDAY_V4_IDE_PREFLIGHT=1` → diagnostics ride along with Claude Code (`--append-system-prompt`) and appear in command preflight notes |

### What We Did NOT Build (honest)
- **The TypeScript VS Code extension** (sidebar, status bar, decorations)
  is not built. The editor is still reachable *today* through the CLI
  (`code -r`, `code -g file:line`) and the LSP protocol — the extension
  would only *add* an in-UI surface. That stays a future refinement.
- No marketplace publishing, no per-editor plugin bundles.

### The MCU Friday Feel (now real)
```
[In the terminal / voice / web chat]
🧑 "what's wrong with auth.py"
🤖 "I found 2 error(s) in auth.py (via ast): line 3: undefined name 'get_token';
    line 5: shadowed builtin: list."
🧑 "diagnose src/main.py" → Friday opens the real diagnostics from the LSP
```

---

## Wave 7: Mobile & Web (10-12 weeks)

**Status (web slice):** ✅ Web Dashboard SHIPPED (2026-08). `friday4 web`
starts a pure-stdlib local dashboard (no FastAPI/flask — consistent with
V4's "pure-stdlib, always works" philosophy) visualizing daemon status,
security grade + findings, intelligence drift/anomalies, proactive
patterns, the read-only V3 bridge, voice config, and the ambient feed,
with a "run security scan" action. React Native app + push pending.

**Goal:** Friday in your pocket. Quick status, voice input, push notifications.

### What We Build

| Module | Files | Time | Complexity |
|--------|-------|------|------------|
| `MobileAPI` (FastAPI server) | `mobile/api.py` | 4 days | Low |
| Push Notification Service | `mobile/push.py` | 5 days | Medium — APNS + FCM setup |
| **Web Dashboard** | `mobile/web/` | **3 weeks** | Medium — FastAPI + HTMX or React |
| **React Native App** | `mobile/app/` | **6-8 weeks** | **HIGH** — first RN app |

**Total: ~48 days → 10-12 weeks**

### Strategy: Web First, Mobile Second

```
Week 1-3:   Web Dashboard only (valuable alone)
Week 4-10:  React Native app (if you still want it)
Week 11-12: Polish + push notifications
```

The web dashboard is **independently useful** — anyone can open a browser
to see Friday's status. The mobile app adds push notifications and voice input
but requires platform-specific development.

### Prerequisites

| Requirement | Cost | Details |
|-------------|------|---------|
| **Apple Developer Account** | **$99/year** | Required for iOS push notifications (APNS) and app distribution |
| **Google Firebase Project** | Free | Required for Android push notifications (FCM) |
| **React Native learning** | Time | First RN app has a 1-2 week learning curve minimum |

### Hidden Complexity
- **Apple Push Notification service (APNS)** requires a paid developer account,
  certificate/key management, and device token registration.
- **Firebase Cloud Messaging (FCM)** needs a google-services.json config file
  and project setup.
- **App Store / Play Store submission** can take 1-3 days for review.

### File Count
- **~25-35 files** (Python API + React Native + web frontend). Highest-complexity wave.

### MCU Friday Feel
```
📱 [Phone buzzes — Friday notification]:
   "Your CI pipeline failed on branch 'feature/auth'.
   3 tests failed. Review on desktop?"
   → Friday follows you everywhere.
```

---

## Wave 9: Agency Core — The Brain ✅ SHIPPED

**Goal:** Friday actually does things. Full design: `WAVE_9_AGENCY_CORE.md`
(§10 "What Actually Shipped" documents the build + deviations).

- ✅ `db.py` — V4-native sqlite foundation (schema v3, 9 tables, migrations)
- ✅ `understanding/` — NLU: intent → entities → canonical action (shared by voice/CLI/web)
- ✅ `reasoning/` — evidence-cited answer engine + provider registry (6 providers, no answer without evidence)
- ✅ `missions/` — persistent goals: planner → engine → steps → scheduler → progress
- ✅ `execution/` — gated, sandboxed, audited executors (shell/git/file/python/testing) with undo
- ✅ `nl_router.py` — NL → act (shared CLI/voice handler, not in the original design)
- ✅ CLI: `friday4 talk "…" / ask / execute / status` (incl. `db status`)

**MCU Friday Feel:** "Friday, run the tests." → gate → executes → reports result. ✅

---

## Wave 10: Memory & Identity — Friday Knows You ✅ SHIPPED

**Goal:** Friday builds a relationship. Full design: `WAVE_10_MEMORY_IDENTITY.md`
(§8 "Wiring Status" tracks all 16 integration gaps — every one closed).

- ✅ `memory/` — facts + working memory with provenance, confidence, decay
- ✅ `persona/` — explicit-consent name & preference learning
- ✅ `relationship/` — interaction depth → tone & verbosity
- ✅ `skills/` — shadow-first self-improvement (Replay + Shadow executors, verified promotion)
- ✅ CLI: `friday4 memory / persona / relationship / skills`
- ✅ Daemon: `MemorySweeper` + `SkillLearner` + `RelationshipRefresher` schedules

**MCU Friday Feel:** "Call me Lakshay" → remembered across restarts, tone adapts. ✅

---

## Wave 11: Research & Reflection ✅ SHIPPED (2026-08)

**Goal:** Friday reasons across your world. Full design: `WAVE_11_RESEARCH_REFLECTION.md`
(§9 "What Actually Shipped" documents the close-out + deviations).

- ✅ `research/` — architecture, cross-project correlation, impact, code search, README purpose
- ✅ `synthesis/` — deterministic, evidence-cited reports (incl. `reports.py` daily/weekly)
- ✅ `briefing/` — morning/evening briefings from real V4 state
- ✅ `ambient/` — in-process event bus + durable queue; **push replaces polling**
- ✅ Push wiring: security findings, suggestions, collab obs → shared bus
- ✅ Web SSE (`/api/events`) + dashboard EventSource with poll fallback
- ✅ CLI: `friday4 analyze / correlate / briefing / narrative / report [--daily|--weekly]`

**MCU Friday Feel:** "Friday, analyze the integration cost between vivaha and
MindWell." → cited, ranged estimate. ✅

---

## Wave 12: Polish & Scale ✅ SHIPPED (2026-08)

**Goal:** Production-ready V4 with documentation, benchmarks, installer, and
a full test suite.

| Task | Status |
|------|--------|
| Performance benchmarks (`tools/benchmarks.py`, V3 vs V4 where importable) | ✅ |
| Documentation site (`tools/build_docs_site.py` → `site/`) | ✅ |
| Installation script (`install.sh`) | ✅ |
| Migration guide (`docs/MIGRATION_GUIDE.md`) | ✅ |
| `network/` stub folded in — `ssh` executor behind gate → sandbox → audit | ✅ |
| Dogfooding + bug fixes | ⏳ ongoing |
| ~800 V4 tests (hermetic, tmp_path) | ✅ |

---

## Wave 13: Thinking Core ✅ SHIPPED (2026-08)

**Goal:** the LLM that *thinks* — Law 6's "deterministic floor, LLM
ceiling". One LLM provider enhances the answer engine; never gates it.

| Task | Status |
|------|--------|
| `llm_provider()` in `reasoning/providers.py` — synthesis over the same evidence, citations kept verbatim | ✅ |
| Engine post-pass — no LLM → byte-identical deterministic floor | ✅ |
| Evidence-less answers never sent to the LLM ("I don't know" stays real) | ✅ |
| Explicit opt-in — `FRIDAY_V4_LLM` env or injected client (reuses `nlu.LLMClient`, 9router proxy) | ✅ |
| `friday4 ask` conversation-capable — history threaded, Q&A logged | ✅ |
| Voice/talk/web inherit via `nl_router` (one entry point) | ✅ |
| Hermetic tests (`test_wave13_thinking_core.py`, 17 tests) | ✅ |
| Wave doc (`docs/WAVE_13_THINKING_CORE.md`) | ✅ |

**MCU Friday Feel:** "What's the deal between vivaha and MindWell?" →
synthesized, cited, ranged — through `friday4 talk`, through voice. ✅

## Wave 13a: ONE NLU Point ✅ SHIPPED (2026-08)

**Goal:** *speaks natural language* — no regex/keyword matching anywhere
in the input path. Every surface routes through ONE parser, `resolve()`,
which is **LLM-first**; deterministic rules are a fallback only when the
LLM is absent/offline — never the product.

| Task | Status |
|------|--------|
| `nlu/` package — `resolve(text, llm=LLMClient())` LLM-first, rules fallback | ✅ |
| `understanding/` is a thin shim — old Wave 9 implementation submodules removed; only re-export `__init__` remains | ✅ |
| All surfaces verified on ONE point — `nl_router`, `voice/router.py`, `web/dashboard.py`, `cli_nl.py` | ✅ |
| Fallback never crashes — fixed the confidence `TypeError` on ambiguous input (never-crash law) | ✅ |
| ASK/RESEARCH targets threaded through `resolve()` | ✅ |
| Research routing fixed — `X vs Y` classified as research; correlate path (str/Path bug) + verb-strip fixed | ✅ |
| `FRIDAY_V4_LLM_URL` / `_MODEL` / `_KEY` env config; explicit opt-in | ✅ |
| Hermetic tests — `test_understanding.py` (25 shim-contract tests) + `test_nlu.py` (12) + research/CLI | ✅ |
| Wave doc (`docs/WAVE_13A_ONE_NLU_POINT.md`) | ✅ |

**MCU Friday Feel:** "Friday, what's the deal between vivaha and
MindWell?" → one parser, LLM intent → research → cited answer. No
surface keyword-matches anything. ✅

---

## Consolidated Timeline (Realistic)

```
WAVE       MONTHS    CUMULATIVE    FEELING                     DIFFICULTY
───────────────────────────────────────────────────────────────────────────
Voice      ✅ SHIPPED             🎤 "I can talk to Friday!"  Medium
Desktop    ✅ SHIPPED             🖥️ "Friday controls desktop" HIGH
Security   ✅ SHIPPED             🔒 "Friday protects code"   Low
Smart      ✅ SHIPPED             🧠 "Friday knows my needs"  Medium-HIGH
Collab     ✅ SHIPPED             🤝 "Friday is networked"    Medium
IDE        2 mo      +2 mo         📝 "Friday in my editor"    HIGH
Mobile     3 mo      +3 mo         📱 "Friday on my phone"     VERY HIGH
Agency     2 mo      +2 mo         🤖 "Friday actually does things" HIGH
Identity   1.5 mo    +1.5 mo       👤 "Friday knows you"       Medium-HIGH
Research   1.5 mo    +1.5 mo       🔬 "Friday reasons across your world" Medium
Polish     ~1 mo     +1 mo         📦 "Production-ready V4"    Medium
```

**Total remaining: ~9-12 months solo** (Waves 6-7 + 9-11 + 12). Waves 1–5 are
built and live (`friday4 daemon`, `friday4 talk`, `friday4 desktop`,
`friday4 proactive`, `friday4 intelligence`, `friday4 security`,
`friday4 collab`, `friday4 doctor`).

**Web Dashboard slice: ✅ SHIPPED (2026-08)** — `friday4 web` local UI over daemon/security/intelligence/proactive/V3/voice.

**Minimum MCU Friday (Voice + Desktop + Security): ✅ ACHIEVED**

---

## Wave 14: Watch Me — Demonstration Capture ✅ SHIPPED (2026-08)

**Goal:** *no learning ceiling* — "copy any workflow." The audit log
IS the demonstration record; "watch me" tags a window on it and
parameterizes the demonstrated work into a skill that generalizes by
context (repo + command), never a literal replay.

| Task | Status |
|------|--------|
| db migration v5 — `watches` table + start/end/get/active/list/actions_between | ✅ |
| `skills/watcher.py` `WatchRecorder` — explicit capture → generalized shadow skill | ✅ |
| `skills/noticer.py` `RepetitionNoticer` — "I noticed you do this every time" offers (pure read) | ✅ |
| Generalization — `_step_matches` repo-context matching (back-compat safe) | ✅ |
| `Intent.SKILL` through the ONE NLU point (LLM-first, fallback words, resolver target) | ✅ |
| `nl_router` `_skill_response` — watch / learn / stop / summarize on all surfaces | ✅ |
| Reasoning `QuestionType.SKILLS` + `skills_provider` (ASK cites real skills) | ✅ |
| `friday4 skills watch` / `watch-stop` / `noticed` / `dispatch` | ✅ |
| Daemon `SkillLearner` runs the noticer before `learn()`, surfaces offers | ✅ |
| Stale `test_db` schema assertions fixed (v3 → v5) | ✅ |
| `tests/test_wave14_watch_me.py` — 33 hermetic tests | ✅ |
| NL accept loop — `Intent.ACCEPT` ("yes, run it") → gate → execution; voice offers the suggestion | ✅ |
| Dispatch → mission — multi-step acceptance becomes a supervised mission (first step runs now, rest tracked) | ✅ |
| Auto-dispatch on idle — daemon `DispatchOfferer` offers matching skills on a schedule (notify + ambient event, never executes) | ✅ |

**MCU feel:** "Friday, watch me do this" → operator works → "learn
this" → shadow skill formed with repo context; "what did you learn" →
"I've learned 3 skill(s)…" from the real registry. See
[`WAVE_14_WATCH_ME.md`](WAVE_14_WATCH_ME.md).

---

## What If You Skip a Wave?

| Skip | Impact | Mitigation |
|------|--------|------------|
| Desktop | Voice still works. No desktop control. | Fine for CLI-centric users. |
| Security | Voice + Desktop still work. Manual scanning. | You can still run pip-audit yourself. |
| Smart | No predictions/anticipation. Core features work. | Friday stops being proactive, stays reactive. |
| Collab | Single-instance only. Same as V3. | Doesn't affect any other feature. |
| IDE | No editor integration. CLI still works. | Voice commands can replace some IDE actions. |
| Mobile | Desktop-only. Same as V3. | Web dashboard is the alternative. |
| Agency (9) | Friday suggests but never does. | Voice + desktop still work; no execution. |
| Identity (10) | Friday has no memory of you. | Patterns still learned, but per-session only. |
| Research (11) | Friday answers only what you ask. | Briefings/reports degrade to manual summaries. |

You can skip any wave and the previous ones still work perfectly. Each wave is
independent except for depending on Voice (for voice interactions).

---

## Risk Register (Solo Developer Edition)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Burnout from scope creep** | **HIGH** | CRITICAL | Ship each wave, celebrate, pause. No deadline = no rush. |
| Voice pipeline complexity | HIGH | HIGH | Start with Whisper API + pyttsx3. Optimize to local models later. |
| macOS learning curve | HIGH | MEDIUM | Start with Hyprland. Add macOS last. Defer if blocked. |
| React Native learning curve | HIGH | MEDIUM | Build Web Dashboard first. RN app is a stretch goal. |
| Platform testing logistics | HIGH | MEDIUM | CI + VMs + borrowing friends' machines |
| Dependency hell (pyaudio) | MEDIUM | MEDIUM | Docker dev environment, platform-specific install docs |
| Anticipation engine fake predictions | MEDIUM | MEDIUM | Label predictions as guesses. Never store as facts. |
| Apple Developer Account cost | LOW | MEDIUM | Skip iOS push. Use web dashboard + polling. |
| Losing momentum mid-project | MEDIUM | HIGH | Release each wave publicly. Get dopamine from users. |

---

## "No Deadline" Philosophy

Since you have no deadline, here's how to make this sustainable:

1. **Work in waves, not days.** Each wave is 4-8 weeks. Don't count hours.
2. **Ship each wave.** Even if it's rough. Get the dopamine hit.
3. **Take breaks between waves.** 1-2 weeks off between major milestones.
4. **Cut scope, never quality.** Better to ship voice without hotword (push-to-talk
   only) than to ship buggy hotword detection.
5. **Dogfood constantly.** Use V4 yourself. The best feature ideas come from
   your own frustration.
6. **Celebrate each wave.** You're building something extraordinary alone.
   That's worth recognizing.

---

## Solo Developer Checklist

### Before Each Wave
- [ ] Read all existing V4 docs (PLAN.md, ARCHITECTURE.md, ROADMAP.md)
- [ ] Understand the V3 modules you'll integrate with
- [ ] List all files you need to create (15-25 files per wave)
- [ ] Identify the hardest part — do that first
- [ ] Check prerequisites (API keys, system deps, accounts)
- [ ] Estimate: is this wave realistic? If not, cut scope.

### After Each Wave
- [ ] Run V4 tests (hermetic, no real `~/.friday` writes)
- [ ] ruff clean on new/edited files
- [ ] Dogfood for 3 days — use it in real work
- [ ] Fix the 3 most annoying bugs
- [ ] Write a short "what I learned" doc
- [ ] Take a break (1-7 days)
- [ ] Celebrate — you shipped a wave of FRIDAY

---

## Prerequisites Cheat Sheet

| Wave | What You Need Before Starting |
|------|-------------------------------|
| Voice | Porcupine API key (free, ~5 min), portaudio dev headers, 5GB disk for models |
| Desktop | Root/admin access for D-Bus/Win32 APIs; macOS: physical Mac for dev |
| Security | pip-audit, truffleHog, ruff installed globally |
| Smart | V3 daemon history (at least 2 weeks of observations) |
| Collab | Network ports available (default: 9876) |
| IDE | Node.js + npm for VS Code extension development |
| Mobile | Apple Developer Account ($99/yr) if iOS; Google Firebase project if Android |
| Agency (9) | ✅ done — ships `db.py` foundation; Python ≥3.12 |
| Identity (10) | ✅ done — builds on Wave 9 DB tables |
| Research (11) | Wave 9 reasoning (done) + Wave 10 memory (done) — both inputs ready |
| Polish (12) | All of the above working correctly |

---

## Recap: What Makes This Feasible for a Solo Dev

1. **Each wave is independent.** You can stop anytime with something real.
2. **Voice + Desktop = Minimum MCU Friday in 5.5 months.** That's the unlock.
3. **No deadline means no crunch.** Build at your pace, ship when it's ready.
4. **V3 is the design reference, not the runtime.** V3 proved the capability
   map (mission, executors, memory, skill formation, analysis). V4 rebuilds
   that capability V4-native — with hygiene, polish, and hermetic tests (see
   Waves 9–11).
5. **Start with what you know.** Build the platforms you use first (Linux +
   Hyprland). Add others progressively.

---

*This roadmap is a living document. Each wave reveals what the next wave
should actually be. The plan is the direction; reality is the path.*
