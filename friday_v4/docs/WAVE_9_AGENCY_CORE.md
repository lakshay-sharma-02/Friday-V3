# Friday V4 — Wave 9: Agency Core (The Brain)

> **Parent:** PLAN.md · **Sibling docs:** WAVE_10_MEMORY_IDENTITY.md,
> WAVE_11_RESEARCH_REFLECTION.md
> **Context:** Solo developer, no deadline, hygiene-first. V3 proved the
> capability map but drowned it in 60K LOC of monolith. V4 rebuilds the
> capability with polish.
> **Estimate:** 6–8 weeks solo (see [Wave Splits](#wave-splits) below).
> **Status:** ✅ SHIPPED (see [What Actually Shipped](#10-what-actually-shipped))

---

## 1. Why This Wave Exists

Waves 1–8 built the **surfaces**: voice, desktop, security, dashboard, collab,
mobile. But today, when you say something *real* to Friday V4, the voice
router falls back to canned responses. There is no brain behind the surfaces.

Wave 9 builds the mind:

```
  understand → decide → plan → act → remember → improve
```

The MCU promise — "Friday, ship the auth refactor by Friday" — is a *planning +
execution + reporting* loop, not a lookup. V3 had every piece of this loop
(`mission.py` 1.2K LOC, `autonomous_planner.py` 1.2K LOC, `runtime/executors.py`,
`confirm_gate.py`, `sandbox.py`) but buried them under CLI glue, a 5.5K-line
`db.py`, and a 3K-line `ask.py`.

**Wave 9 is the "port the capability, not the code" decision applied to the
brain.** Every module is V4-native: pure-stdlib-first, never-crash,
degrade-silently, hermetic tests. V3 assets are listed per module as *design
reference*, never as imports.

---

## 2. The V4 Architecture Laws (recap — all modules obey these)

1. **V4 Is The Product** — no `import friday`; V3 is read-only data.
2. **Pure-Stdlib First** — stdlib implementation always works; LLM enhances, never gates.
3. **Never Crash** — every external call wrapped; failure degrades silently.
4. **Reality First** — answers cite evidence; no fabricated facts.
5. **Reasoning Never Mutates Reality** — actions require explicit confirmation gates.
6. **Hermetic Tests** — no real `~/.friday` writes in the test suite.

---

## 3. Wave Splits

The full Agency Core is too much for one shippable wave (solo, hygiene-first).
It ships as three:

| Wave | Core | Est. | MCU Feeling |
|------|------|------|-------------|
| **9 (this)** | Foundation DB + Understanding + Reasoning + Missions + Execution | 6–8 weeks | 🤖 *"Friday actually does things"* |
| **10** | Memory/Identity + Skills (self-improvement) | 5–7 weeks | 👤 *"Friday knows you"* |
| **11** | Research/Synthesis + Briefings + Ambient push | 5–6 weeks | 🔬 *"Friday reasons across your world"* |

Each wave is independently shippable. You can stop after Wave 9 and have a
Friday that takes commands, plans goals, and executes them safely.

---

## 4. Deliverables

### 4.0 Foundation — V4 Database ✅ (unblocker — do first)

V4 currently persists to scattered JSON state files (`v4_notifier_state.json`,
`collab/state.json`). A brain needs a real store.

**Deliverable:** `friday_v4/db.py` — one stdlib `sqlite3` module, one schema,
`FRIDAY_V4_DB` env override, migrations table.

| Table | Purpose |
|-------|---------|
| `missions` / `mission_steps` | persistent goals & progress |
| `actions` | audited executions (what, when, result, undo info) |
| `memories` / `facts` | long-term knowledge about operator, projects, preferences |
| `relationships` | depth, tone, preferences per person |
| `skills` | learned workflows + confidence + verification |
| `sessions` / `exchanges` | conversation history, context |

**Design reference:** V3 `db.py` (5,541 LOC) reduced to a few hundred clean
lines. One migration table. `connect()` gains `mode=ro` support for the V3
bridge (`v3source.py` stays the only V3 touchpoint).

**Key Rules:**
- All reads/writes go through typed helpers (`insert_mission`, `recent_actions`, …).
- Schema versioning via `PRAGMA user_version` + migration list.
- Tests use `tmp_path` connections — never `~/.friday`.

**MCU Friday Feel:**
```
🎤 [Friday]: "I'll remember that. Mission 'auth refactor' now has 4 steps,
             step 1 complete. I logged every action — nothing ran silently."
```

### 4.1 Understanding Layer — NLU (`friday_v4/understanding/`) 🆕

V3 had `cli_nl.py` + `intent_labeler.py` scattered across the CLI. V4 gets one
shared NLU surface used by **voice, CLI, and web alike** — so "Friday, run the
tests" works identically everywhere.

| File | Purpose |
|------|---------|
| `intent.py` | intent classification + slot filling (deterministic rules first, LLM enhance) |
| `entities.py` | entity extraction (repos, files, commands, people, times) |
| `confidence.py` | ambiguity handling ("did you mean the auth tests or the full suite?") |
| `resolver.py` | utterance → canonical action object (the single command language) |

**⚠️ Naming note:** V3 has `src/friday/understanding/` but that meant the
*knowledge-derivation* engine (observations → derived facts). This V4
`understanding/` means **NLU / intent classification** — a completely
different concept. If the collision ever confuses, rename to `nlu/`.

**Design reference:** V3 `cli_nl.py` (`_classify_action_or_question`),
`intent_labeler.py`, `context_prompter.py`.

**Key Rules:**
- Deterministic keyword/rules pass runs **first**; LLM only fills gaps.
- Ambiguous intents return a `confidence` + clarification, never a guess.
- Every surface (voice router, `friday4 talk`, web chat, CLI) calls the same
  `resolve(text)` entry point.

**MCU Friday Feel:**
```
🎤 [You]: "Friday, what's the status of my projects?"
🎧 [Friday]: (intent=status, scope=all repos) → real answer from evidence,
             not a canned fallback.
```

### 4.2 Reasoning Core — the answer engine (`friday_v4/reasoning/`)

| File | Purpose |
|------|---------|
| `engine.py` | `EnsembleReasoner`: deterministic + LLM with agreement levels |
| `ask.py` | evidence-scoped Q&A with provider registry |
| `evidence.py` | evidence selection + citation — no answer without evidence |
| `judgment.py` | objective judgment: no hallucination, no overclaim |

**Design reference:** V3 `ask.py` (3,040 LOC, 16 providers: `_p_compare`,
`_p_overlap`, `_p_integration`, `_p_drift`…), `reasoning/engine.py`
(`EnsembleReasoner`), `evidence_scope.py`, `objective.py`.

**Key Rules:**
- Every answer cites evidence; empty evidence → "I don't know yet" (never
  fabrication).
- Providers are registered (`@provider("compare")`) — one concept per provider,
  tiny files, no 3K-line monolith.
- Judgment red-team tests carried over from V3 philosophy: forbid overclaim,
  forbid unsupported certainty.

**MCU Friday Feel:**
```
🎤 [You]: "Friday, what's the deal with vivaha and MindWell?"
🎧 [Friday]: "They share React + Supabase and similar auth flows — 3 files
             overlap. I flagged this as a cross-project correlation last
             week. Evidence: architecture analysis of both repos, 2026-07-28."
```

### 4.3 Mission & Planning (`friday_v4/missions/`)

| File | Purpose |
|------|---------|
| `models.py` | `Mission`, `MissionStep` dataclasses with statuses |
| `engine.py` | persistent missions: create, advance, adapt, replan |
| `planner.py` | goal → step decomposition (deterministic fallback + LLM) |
| `scheduler.py` | time-aware sequencing of steps |
| `progress.py` | progress feed → surfaces (voice briefing, web card, desktop notify) |

**Design reference:** V3 `mission.py` (`MissionEngine`: create → start →
advance → adapt → replan), `autonomous_planner.py` (`plan_from_drift`,
`plan_from_gaps`, `_should_execute`).

**Key Rules:**
- Missions persist in the V4 DB; restart-safe.
- Adaptation is explicit: a mission never silently changes steps — it reports
  "plan changed because…".
- Deterministic planner always works; LLM planner enhances step quality.

**MCU Friday Feel:**
```
🎤 [You]: "Friday, I need to ship the auth refactor by Friday."
🎧 [Friday]: "Got it. Created mission: 4 steps, scheduled across 3 days.
             Step 1 (migrate session handling, 4 files) — want me to
             start prepping the test files?"
```

### 4.4 Execution Layer (`friday_v4/execution/`)

The moment V4 currently never reaches — **actually doing things**, safely.

| File | Purpose |
|------|---------|
| `executors.py` | shell, git, file, python, testing executors (V4-native) |
| `gate.py` | confirmation gate with permission levels |
| `sandbox.py` | restricted env, path allowlists, timeouts |
| `audit.py` | every action logged: what, when, result, undo payload |
| `undo.py` | reversible actions where possible |

**Design reference:** V3 `runtime/executors.py` (`BuiltinShellExecutor`,
`GitExecutor`, `FileExecutor`, `TestingExecutor`), `runtime/confirm_gate.py`,
`sandbox.py`, `cli_undo.py`.

**Key Rules:**
- **Permission levels:** auto (read-only: status, diff), confirm (writes,
  test runs), never (prod/deploy/push without explicit operator override).
- Every action is audited to `actions` table with undo payload when available.
- Executors are stdlib-first; subprocess tools (pytest, git, ruff) discovered
  venv-aware via `security/tooling.py`-style helper.

**MCU Friday Feel:**
```
🎤 [You]: "Friday, run the tests."
🎧 [Friday]: "Running pytest on friday_v4 — 390 passed, 0 failed.
             Want me to run the linter too?"
```

---

## 5. Architecture Diagram

```
                    ┌──────────────────────────────┐
                    │         SURFACES             │
                    │  Voice · CLI · Web · Desktop │
                    └──────────────┬───────────────┘
                                   │  resolve(text)
                    ┌──────────────▼───────────────┐
                    │    understanding/  (NLU)     │
                    │  intent → entities → action  │
                    └───────┬──────────────┬───────┘
              ask / reason │              │ plan / execute
   ┌───────────────────────▼──┐   ┌───────▼───────────────────┐
   │      reasoning/          │   │       missions/           │
   │  ensemble reasoner       │   │  planner → engine → steps │
   │  evidence-scoped Q&A     │   └───────┬───────────────────┘
   └──────────────────────────┘           │
                                  ┌───────▼───────────────────┐
                                  │      execution/           │
                                  │  gate → sandbox → audit   │
                                  └───────┬───────────────────┘
                                          │
                          ┌───────────────▼───────────────────┐
                          │       db.py  (V4 state, sqlite)   │
                          │  missions · actions · sessions    │
                          └───────────────────────────────────┘
```

---

## 6. Test Strategy

- **db.py:** migrations, typed helpers, read-only mode — hermetic (`tmp_path`).
- **understanding/:** intent fixtures (voice + text), ambiguity → clarification.
- **reasoning/:** every answer must cite evidence; red-team overclaim cases
  (carried from V3's `tools/redteam.py` philosophy).
- **missions/:** restart-safety, plan adaptation reports "plan changed because…".
- **execution/:** sandbox escape attempts, gate permission matrix
  (auto/confirm/never), audit trail completeness, undo round-trip.

Target: **+120 tests** for this wave (V4 suite → ~510+).

---

## 7. File Count

- **~28 source files** + **~12 test files** across 6 new packages
  (`db.py`, `understanding/`, `reasoning/`, `missions/`, `execution/`).
- Plus CLI wiring: `friday4 ask`, `friday4 plan`, `friday4 execute`,
  `friday4 mission`, `friday4 db status` — and the long-missing
  top-level `friday4 status` (unified layer overview, documented in
  PLAN.md Appendix B but never implemented) finally gets built here.

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM reasoning hallucination | Medium | High | Evidence-first: no evidence → "I don't know" |
| Execution safety breach | Low | High | Sandbox + permission levels + audit + undo |
| Scope creep (re-porting too much V3) | High | Medium | Each module is a capability slice; cut what doesn't earn its place |
| Mission replanning surprises user | Medium | Medium | Explicit "plan changed because…" reporting |
| DB migration churn | Medium | Low | `user_version` migrations, hermetic tests |

---

## 9. Success Criteria

1. "Friday, run the tests" → gate → executes → reports result. ✅
2. "Friday, what's the status of my projects?" → evidence-cited answer, no canned fallback. ✅
3. "Friday, I need to ship the auth refactor by Friday" → persistent mission, progress trackable, restart-safe. ✅
4. Every executed action appears in `friday4 actions` with undo info. ✅
5. Zero V3 imports (except `v3source.py`), 510+ hermetic V4 tests passing. ✅

---

## 10. What Actually Shipped

**Status: ✅ shipped** — the full wave, plus two additions the design
didn't anticipate.

### As designed

- **`db.py`** — sqlite with `PRAGMA user_version` migrations (now at
  schema v3, 9 tables: missions, mission_steps, actions, memories,
  relationships, skills, sessions, exchanges, working_memory). Typed
  helpers + read-only mode for the V3 bridge.
- **`understanding/`** — `intent.py` (deterministic rules first),
  `entities.py`, `confidence.py` (ambiguity → clarification), `resolver.py`
  (utterance → canonical action).
- **`reasoning/`** — `engine.py` (EnsembleReasoner: known/unknown,
  evidence-cited), `evidence.py` (Answer/Evidence), `judgment.py`
  (no-overclaim red-team), `providers.py` (6 providers: identity, status,
  activity, mission, memory, conversation), `question.py` (8 question
  types). **No answer without evidence** — empty evidence → honest
  "I don't know yet."
- **`missions/`** — `engine.py` (create/start/advance/adapt/replan,
  restart-safe), `models.py`, `planner.py` (deterministic fallback),
  `scheduler.py`, `progress.py`. Adaptation reports "plan changed because…".
- **`execution/`** — `executors.py` (shell/git/file/python/testing),
  `gate.py` (auto/confirm/never permission levels), `sandbox.py` (path
  allowlists, timeouts), `audit.py` (every action → `actions` table),
  `undo.py` (reversible actions).
- **CLI** — `friday4 talk "…"` (one-shot + interactive REPL + `--manual`),
  `friday4 ask`, `friday4 execute`, `friday4 status` (unified overview +
  `friday4 status db`), `friday4 db status`.

### Additions beyond the design

1. **`nl_router.py` — the shared NL→act handler.** The design listed
   `resolver.py` as "the single command language" but nothing that *did*
   the resolved action. `TextCommandHandler.handle(text)` interprets an
   utterance and runs it through the real pipeline (gate → sandbox →
   audit), logs every exchange verbatim (the brain learns from what you
   actually said), reports ambiguity honestly, and never auto-completes a
   manual mission step. Every surface calls the same entry point:
   - `friday4 talk "…"` (`cli_nl.py`)
   - the **voice router** (`voice/router.py:_try_nlu_route`) — voice now
     inherits the brain: "run the tests" spoken → executed, "who am I"
     spoken → persona answer. Voice confirmations are asked aloud
     (`voice_confirm` → TTS + STT).
   - future web chat.
2. **`friday4 talk` became the NL surface.** The original voice session
   moved to `friday4 voice talk` after argparse rejected two `talk`
   subparsers (the CLI crashed on every invocation until fixed).
3. **Daemon integration** — `friday4 daemon` wires the voice router with a
   conversation-log DB connection, so spoken utterances persist into the
   brain's log.

### Deviations / notes

- **EnsembleReasoner** is deterministic-only for now — no LLM provider
  registered (`providers.py` is the registry where one slots in later).
  The architecture law "LLM enhances, never gates" holds: the
  deterministic set answers honestly, and an empty evidence set says
  "I don't know yet" rather than hallucinating.
- **Test count:** wave target was +120 → ~510. Actual suite after the
  wave: ~740 tests, all green.
- **`friday4 plan`/`mission` CLI subcommands** were listed in the design
  but missions are surfaced through `friday4 talk "ship X by Friday"`
  (planner) and `friday4 status` (progress) instead — one command language,
  no separate mission CLI needed. Add `friday4 mission` if the REPL
  workflow ever needs direct mission CRUD.

**What I learned:** the highest-value piece of this wave was
`nl_router.py`, which the design didn't name — the act part of
"understand → decide → plan → act" is what makes the surfaces feel alive.
Voice inheriting the brain (spoken "run the tests" → executed) is the
single most MCU-feeling moment in the product so far.

---

*This wave is the point where the roadmap stops being about interfaces and
starts being about the hard 60% — a reasoning core that answers without
hallucinating, executors that act without breaking things. That's where the
real MCU Friday lives.*
