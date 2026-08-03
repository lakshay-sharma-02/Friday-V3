# Friday V4 — Wave 11: Research & Reflection (Friday Reasons Across Your World)

> **Parent:** PLAN.md · **Sibling docs:** WAVE_9_AGENCY_CORE.md,
> WAVE_10_MEMORY_IDENTITY.md
> **Context:** Solo developer, hygiene-first. Waves 9–10 gave Friday a brain
> and a relationship; Wave 11 gives it *depth* — analysis, synthesis, briefings,
> and ambient push.
> **Estimate:** 5–6 weeks solo.
> **Prereqs:** ✅ SHIPPED — Wave 9 `reasoning/` (evidence engine, 6
> providers) and Wave 10 `memory/` both exist; this wave's architecture
> diagram already references them as inputs.

---

## 1. Why This Wave Exists

The MCU Friday doesn't just answer — it **already did the research before you
asked**:

> 🎤 "Friday, analyze the integration cost between vivaha and MindWell."
> 🎧 "I already did that last week. Shared auth would save ~3 days. The
>    main risk is MindWell's custom session handling. Want the full breakdown?"

V3 had these capabilities (`architecture.py` 1.3K LOC, `cross_project.py`,
`synthesis.py`, `briefing.py`, `narrative.py`) — but as yet another set of
CLI-first monoliths with no voice/ambient wiring. Wave 11 rebuilds them as
clean, V4-native analysis modules **surfaced through the surfaces built in
Waves 1–8** (voice, web dashboard, desktop notifications).

This wave also fixes the last architectural gap from the original analysis:
**polling everywhere**. Ambient push replaces poll loops with real-time
delivery.

---

## 2. Architecture

```
        ┌─────────────────────────────────────────────┐
        │              AMBIENT PUSH                    │
        │  voice · desktop notify · web dashboard      │
        └──────────────┬──────────────────────────────┘
                       │  push_event / subscribe
        ┌──────────────▼──────────────────────────────┐
        │          research/  (analysis core)          │
        │  architecture · cross_project · impact       │
        └──────────────┬──────────────────────────────┘
                       │  findings
        ┌──────────────▼──────────────────────────────┐
        │          synthesis/  (answers & briefings)   │
        │  synthesis · briefing · narrative            │
        └──────────────┬──────────────────────────────┘
                       │
        ┌──────────────▼──────────────────────────────┐
        │      reasoning/  (Wave 9) · memory/ (Wave 10)│
        └──────────────┬──────────────────────────────┘
                       │
        ┌──────────────▼──────────────────────────────┐
        │               db.py  (V4 state)             │
        └─────────────────────────────────────────────┘
```

---

## 3. Deliverables

### 3.1 Research & Analysis (`friday_v4/research/`)

| File | Purpose |
|------|---------|
| `architecture.py` | per-repo tech/structure/quality analysis (V3 `architecture.py` concept) |
| `cross_project.py` | correlation + integration-cost estimates between projects (V3 `cross_project.py`) |
| `impact.py` | change impact analysis (V3 `impact.py`) |
| `code_search.py` | cross-repo code search (V3 `code_search.py`) |
| `readme.py` | repo purpose recovery from READMEs (V3 `readme.py`) |

**Design reference:** V3 `architecture.py`, `cross_project.py`, `impact.py`,
`code_search.py`, `readme.py`, `portfolio.py` (meaningful_overlap).

**Key Rules:**
- Analysis is **evidence-cited** (feeds the Wave 9 evidence engine) — every
  claim carries the repo/file/line it came from.
- Integration-cost estimates are **range + confidence**, never false precision
  ("~3 days, confidence: medium").
- Analysis runs are cached with invalidation (repo hash + time) — Friday
  "already did that" only when it actually did.

**MCU Friday Feel:**
```
🎤 [You]: "Friday, analyze the integration cost between vivaha and MindWell."
🎧 [Friday]: "I analyzed both repos last week. Shared auth would save ~3
             days of duplicated work. Main risk: MindWell's custom session
             handling diverges from vivaha's. Full breakdown:
             [2 files overlap · 1 divergence · estimate medium-confidence]"
```

### 3.2 Synthesis (`friday_v4/synthesis/`)

| File | Purpose |
|------|---------|
| `synthesis.py` | synthesize findings into structured answers/briefings (V3 `synthesis.py`) |
| `reports.py` | generate daily/weekly reports (V3 `reports.py`) |

**Design reference:** V3 `synthesis.py`, `cli_synthesize.py`,
`presentation/reports.py`.

**Key Rules:**
- Synthesis is **composition of evidence**, never invention: every paragraph
  maps to cited findings.
- Reports are deterministic given the same evidence set (testable).

**MCU Friday Feel:**
```
🎤 [You]: "Wrap it up for the day."
🎧 [Friday]: "Here's your day: 4 files edited, 12 tests passing,
             1 PR reviewed and merged, 2 vulnerabilities fixed.
             I'll run full security scans overnight."
```

### 3.3 Briefings (`friday_v4/briefing/`)

| File | Purpose |
|------|---------|
| `briefing.py` | morning/evening briefings from real V4 state (V3 `briefing.py` concept) |
| `narrative.py` | day narrative / timeline (V3 `narrative.py` concept) |

**Design reference:** V3 `briefing.py`, `narrative.py`.

**Key Rules:**
- Briefings are built from **real V4 state** (missions, security findings,
  drift, memory) — never template fluff.
- Length adapts to relationship depth + time of day (Wave 10 tone rules).
- Briefings are *offered*, never forced — ambient-not-intrusive.

**MCU Friday Feel:**
```
🎧 [Friday]: "Good morning. While you were away, codebuff had 8 commits.
             Aether's build is failing on main — type error in the scheduler.
             MindWell has 2 high-severity vulnerabilities. Want the walkthrough?"
```

### 3.4 Ambient Push (`friday_v4/ambient/`)

The final architectural fix: **real-time push instead of polling**.

| File | Purpose |
|------|---------|
| `bus.py` | in-process event bus (subscribe/publish) |
| `channels.py` | fan-out to voice (speak), desktop (notify), web (SSE/WS) |
| `queue.py` | durable event queue with replay on reconnect |

**⚠️ Naming note:** V4 already talks about "the ambient feed" everywhere
(the web dashboard, `desktop/notifier.py` polling V3's `friday.ambient`).
This V4 `ambient/` package is an **in-process event bus**, not the V3 feed.
If the collision confuses, rename to `events/` or `bus/`.

**Design reference:** V3 `ambient.py` (event feed model), `event_bus.py`,
`notification.py` — rebuilt V4-native with a clean in-process bus.

**Key Rules:**
- Components publish typed events; channels subscribe — no direct coupling.
- Events are queued durably (V4 DB) so a disconnected surface (mobile later,
  web tab closed) replays on reconnect.
- Priority-aware: critical events interrupt; routine events queue for the
  next briefing.

**MCU Friday Feel:**
```
📱 [Phone buzzes — Friday notification]: "Your CI failed on feature/auth.
   3 tests failed. Review on desktop?"
   → Friday follows you everywhere. (Mobile app still a future wave; the
     push transport is ready now.)
```

---

## 4. File Count & CLI

- **~15 source files** + **~10 test files** across 4 new packages
  (`research/`, `synthesis/`, `briefing/`, `ambient/`).
- CLI wiring: `friday4 analyze <repo>`, `friday4 correlate <a> <b>`,
  `friday4 briefing morning|evening`, `friday4 narrative`, `friday4 report`.

---

## 5. Test Strategy

- **research/:** fixture repos with known structure → analysis assertions;
  integration-cost estimates are range+confidence; cache invalidation.
- **synthesis/:** determinism (same evidence → same report); citation integrity.
- **briefing/:** briefings reflect real state; tone adapts to depth/time-of-day.
- **ambient/:** bus fan-out, durable queue replay, priority gating.

Target: **+80 tests** (V4 suite → ~680).

---

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Analysis noise (too many correlations) | High | Medium | Confidence thresholds, dedup, briefing-gating |
| Integration-cost overclaim | Medium | High | Range + confidence, evidence-cited |
| Push spam | High | Medium | Priority gating + durable queue + relationship-aware volume |
| Cache staleness | Medium | Low | Repo-hash + time invalidation |

---

## 7. Success Criteria

1. "Friday, analyze the integration cost between X and Y" → cited, ranged estimate. ✅
2. Morning briefing reflects real state (missions, security, drift) and adapts tone. ✅
3. Events push to voice/desktop/web in real time; disconnected surfaces replay. ✅ (SSE `/api/events` + durable queue)
4. Reports are deterministic and evidence-cited. ✅ (incl. `report --daily/--weekly`)
5. Zero V3 imports (except `v3source.py`), hermetic tests, ~680 total. ✅ (suite now ~800)

---

## 8. Wave Map (9 → 10 → 11)

| Wave | Builds | MCU Feeling |
|------|--------|-------------|
| 9 | DB, NLU, reasoning, missions, execution | 🤖 *Friday actually does things* |
| 10 | memory, persona, relationship, skills | 👤 *Friday knows you* |
| 11 | research, synthesis, briefings, ambient push | 🔬 *Friday reasons across your world* |

After Wave 11, the original 8-wave roadmap's **Wave 8 (Polish & Scale)** is
**renumbered to Wave 12** and moved after the new waves — now with a complete
product to polish (benchmarks, docs site, installer, migration guide, 500+
test goal superseded by ~680).

**⚠️ Bookkeeping:** ✅ DONE — ROADMAP.md now lists Waves 9–11 with Polish
renumbered to Wave 12, and Waves 9–10 marked shipped. The `network/` stub
(SSH/webhook/cloud, no wave assigned) gets its explicit decision here:
**kept as a stub, folded into Wave 12 Polish** (network executors depend
on the Wave 9 `execution/` sandbox/gate/audit contract, which is stable
now — SSH/file/cloud executors slot into `executors.py` without a new
wave). Not archived: it's a documented extension point for `execution/`.

---

*This completes the MCU capability map: voice, desktop control, security,
ambient awareness, learning, research, mission control, and personality — all
V4-native, stdlib-first, and hermetic-tested. The shell from Waves 1–8 and the
mind from Waves 9–11 are finally one product.*

---

## 9. What Actually Shipped (close-out, 2026-08)

**Status: ✅ SHIPPED.** Wave 11's four packages, CLI, and daemon wiring are
built, tested, and pushed onto the shared ambient bus. The close-out added the
design's remaining surfaces and the push wiring (push replaces polling):

| Deliverable | Status | Notes |
|---|---|---|
| `research/` (architecture, cross_project, impact, code_search, readme) | ✅ | evidence-cited, cached with invalidation |
| `synthesis/synthesis.py` + `synthesis/reports.py` | ✅ | `reports.py` added in close-out: `friday4 research report --daily\|--weekly` from real V4 state |
| `briefing/` (briefing, narrative) | ✅ | real state, tone-adapted by relationship depth |
| `ambient/` (bus + durable queue in V4 DB, channels) | ✅ | `queue.py` folded into `bus.py` (documented deviation) |
| CLI `analyze/correlate/briefing/narrative/report` | ✅ | `report --daily/--weekly` added |
| **Push wiring** — security findings, suggestions, collab obs → shared bus | ✅ | `SecurityScanner`, `ProactiveSuggestionChannel`, `Coordinator` all publish; daemon injects one shared bus |
| **Web SSE** `GET /api/events` + dashboard EventSource | ✅ | durable-queue replay via `since` cursor; 10s poll kept as fallback |
| Tests | ✅ | `test_wave11_closeout.py` + prior W11 tests; suite ~800 |

### Wiring status (the Wiring Law table)

- [x] **Entry points** — `friday4 research …` CLI + `report --daily/--weekly`; NL paths (ask/research) already route.
- [x] **Daemon schedules** — `AmbientWorker` publishes briefings; security + suggestions publish findings/ideas onto the shared bus.
- [x] **Reasoning providers** — Wave 9 providers unchanged (evidence floor); research feeds them richer evidence.
- [x] **Consumers** — dashboard (`/api/ambient-events`, `/api/briefing`, new `/api/events` SSE) + briefing reads real state.
- [x] **Hermetic tests** — green, `tmp_path`-hermetic.

### Deviations

- `ambient/queue.py` (design §3.4) folded into `bus.py` — the durable queue
  is a table in the V4 DB, owned by `AmbientBus`; a separate file added no
  value.
- Web SSE uses the durable queue as the transport (polling the queue every
  1s), not an in-process pub/sub — `friday4 web` runs in its own process, so
  the durable queue is the correct cross-process channel.
