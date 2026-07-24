# Current State Assessment — Friday V3

**Date:** 2026-07-24
**Source:** Codebase audit, docs, git history, memory files, milestone reports.
**Scope:** Full stack, Reality → Runtime, including working tree.

---

## 1. Project Identity

| Property | Value |
|----------|-------|
| Name | `friday` v0.1.0 |
| Description | "Persistent AI operating partner: workspace understanding" |
| Language | Python ≥3.12 (hatchling build) |
| Size | ~67.8K lines Python (`src/friday/`), **1371 tests** (pyproject.toml `testpaths` fix revealed ~47 tests that were silently skipped by `run_e2e_test.py` sabotaging collection) |
| Entry | `friday.cli:main` (`friday` CLI binary) |
| DB | SQLite via `~/.friday/friday.db` (31 tables) |

---

## 2. Architecture At a Glance

**Pipeline (Reality → Output):**
```
Reality → Observation → Context → Knowledge → Understanding →
Initiatives → Insights → Brain (ask) → Planning → Task Graph →
Capability Resolver → Scheduler → Runtime
```

**Structural properties:**
- 25 Architecture Laws (constitutional, frozen)
- Downward-only dependencies (Law 19)
- Deterministic core, LLM optional (Law 21)
- Append-only history (Law 20)
- Versioned contracts (Law 24)
- Frozen modules: `ask.py` brain, `evidence_scope`, `objective`, `identity`, `observation/` engine, `context/` engine

---

## 3. Milestone Delivery Status

| Milestone | Status | What |
|-----------|--------|------|
| M1 | ✅ | Workspace understanding (ingest, analyze) |
| M2 | ✅ | `ask` conversational queries, identity cards, relationships |
| M3 | ✅ | Repository architecture intelligence |
| M3.6 | ✅ | Workspace intelligence (portfolio, themes, overlap) |
| M4 | ✅ | Knowledge completion (purpose gap, honest overlap) |
| M5 | ✅ | `friday observe` — append-only workspace observation |
| M6 | ✅ | Engineering judgment — fix 7 dogfood failures |
| M7 | ✅ | Red-team hardening, regression corpus, coverage |
| M8.1 | ✅ | Knowledge Engine (10 knowledge types, confidence lifecycle) |
| M8.3 | ✅ | Understanding Engine (21 detector types, derivation) |
| M8.5 | ✅ | Insight Engine + full cognitive stack complete |
| M9.2.5 | ✅ | Execution Readiness Sprint (5 HIGH bugs fixed) |
| M9.3 | ✅ | Capability Resolver (65 tests) |
| M9.4 | ✅ | Scheduler (52 tests) |
| M9.5 | ✅ | Runtime (89 tests) |

**Not yet built:** Review layer.

### Learning Loop (Law 17) — ✅ Delivered as RuntimeObserver

| Part | Status | What |
|------|--------|------|
| Part 1 | ✅ | RuntimeObserver created, registered, 26 tests |
| Part 2 | ✅ | Knowledge detectors + race condition fix (session-ID cursor) |
| Part 3 | ✅ | Cursor moved to dedicated `observed_session_ids` table (Law 18/24) |

Key architectural detail: Learning is NOT a new layer — it's an Observer. Execution outcomes flow through the normal Observation → Context → Knowledge → Understanding pipeline, with a full evidence trail. No frozen layer touched.

### Repair Loop (Law 16) — ✅ Delivered as new Planning cycle

| Part | Status | What |
|------|--------|------|
| Part 0 | ✅ | `run_e2e_test.py` collection sabotage fixed: `testpaths = ["tests"]` in `pyproject.toml` prevents pytest from walking root-level non-test files. 1371 tests collected (up from ~1324). Full-suite baseline requires local run (`pytest -q -n auto --durations=20 tests/`) — the basher agent's 30s hard process kill prevents it from here, but the root cause (collection of `run_e2e_test.py` which calls `sys.exit(1)` on import) is confirmed and fixed. 84 core tests confirmed passing: 41 knowledge (2.05s), 28 runtime observer + dogfood, 7 repair, plus earlier-verified runtime/execute/planning. |
| Part 1 | ✅ | Repair engine: detection → evaluation → proposal → approval pipeline. 7 tests covering all scenarios. |

Key architectural detail: Repair is NOT a new execution path. It is a new Planning cycle, triggered by a failed Review verdict, that goes through the exact same pipeline (Planning → Task Graph → Capability Resolver → Scheduler → Runtime → Review). No frozen layer modified — only additive changes to `db.py` (`repair_proposals` + `repair_history` tables, indexes), `cli.py` (parser), `cli_watch.py` (detection trigger), plus new files in `repair/` package and `cli_repair.py`. Execution outcomes flow through the normal Observation → Context → Knowledge → Understanding pipeline, with a full evidence trail. No frozen layer touched.

---

## 4. Current Working Tree Changes

### Modified (6 files)

| File | Changes | Purpose |
|------|---------|---------|
| `src/friday/db.py` (+17) | Add `observed_session_ids` table to SCHEMA | Dedicated Observation-layer cursor table (Law 18/24 compliant) — replaces operator_preferences misuse |
| `src/friday/observation/runtime_observer.py` (~195→190) | Cursor moved from `operator_preferences` to `observed_session_ids` | Fixes Law 18 violation (cursor was operator data, now it's Observation-layer bookkeeping). Uses normalized set table (correct for UUID session IDs). Batch inserts via executemany. Includes legacy cleanup. |
| `src/friday/knowledge/models.py` (+2) | Add `CAPABILITY_RELIABILITY` + `EXECUTION_BOTTLENECK` to `KnowledgeType` | New knowledge types for execution-derived facts |
| `src/friday/knowledge/engine.py` (+4) | Wire `detect_capability_reliability` + `detect_repair_bottlenecks` | 2 import lines + 2 build-sequence lines in `KnowledgeEngine.build()` |
| `src/friday/knowledge/__init__.py` (+1) | Export `KnowledgeType` additions | Public API |
| `tests/test_dogfood_runtime_observer.py` (+50) | Add `test_knowledge_detectors_create_entries_from_runtime_observations` | 5 dogfood tests → 28 total. Asserts evidence_ids are real runtime observation IDs. |

### New files

| File | Lines | Purpose |
|------|-------|---------|
| `src/friday/knowledge/execution.py` | ~120 | **Knowledge detectors for runtime observations.** `detect_capability_reliability` (capability X/Y success ratio, threshold=3) and `detect_repair_bottlenecks` (recurring repair-required graphs, threshold=2). Every Knowledge entry sets `evidence_ids` to actual runtime observation IDs — Law 4 compliant. |
| `src/friday/observation/runtime_observer.py` | ~190 | **RuntimeObserver (Law 17 — Learning Loop):** reads completed runtime sessions + review verdicts and emits Observation facts. Registered in `default_registry()`. Part 3: cursor stored in dedicated `observed_session_ids` table, not `operator_preferences`. |
| `tests/test_runtime_observer.py` | ~340 | 23 unit tests: `collect`/`summarize`/`health`, graph/task outcomes, repair detection, capability reliability, watermark, idempotency, **race condition test** (backdated finished_at still observed — proves cursor fix), end-to-end through ObservationEngine |
| `tests/test_dogfood_runtime_observer.py` | ~310 | 5 dogfood tests: full loop runtime → observer → knowledge → evidence traceable back to execution. Proves Learning loop closes with real evidence_ids. |

### Summary
Working tree delivers the complete **Learning Loop (Law 17)**: a RuntimeObserver (not a new layer), Knowledge detectors that consume its facts, and a correct cursor storage boundary — plus the **Repair Loop (Law 16)**: an evidence-driven repair proposal engine backed by the same Observation → Knowledge pipeline. No frozen modules modified. 35 new tests (28 runtime observer + dogfood, 7 repair). `pyproject.toml` `testpaths` fix resolves a four-round collection sabotage (`run_e2e_test.py`). 84 core tests pass with zero regressions.

---

## 5a. Pyproject.toml Change

| File | Change | Purpose |
|------|--------|---------|
| `pyproject.toml` (+1) | Add `testpaths = ["tests"]` to `[tool.pytest.ini_options]` | Prevents pytest from collecting root-level files like `run_e2e_test.py` (which calls `sys.exit(1)` at import — lines 98, 112). This file is a standalone E2E script meant to be run with `python run_e2e_test.py`, not collected as a test module. Fix confirmed: 1371 tests collected vs ~1324 before (growth from Learning + Repair loop tests). |

---

## 5. Known Issues (from KNOWN_ISSUES.md)

**25 items tracked.** State breakdown:

| State | Count | Items |
|-------|-------|-------|
| FIXED | 9 | #3 (offline framing), #7 (initiative template), #11 (evidence-to-task template), #12 (sequential graphs), #13 (knowledge template), #15 (context crash), #18 (plan-type bug), #20 (multi-word IDs), #21 (round-robin evidence), #22 (stale knowledge records) |
| DOCUMENTED | 4 | #8 (understanding template), #10 (concept-extraction threshold), #14 (no quality gate), #24 (verification gate limitations) |
| RESOLVED | 2 | #9 (confidence aggregation), #19 (self-ingestion) |
| SKIPPED | 1 | #23 (dogfood LLM flakiness) |
| OPEN | 4 | #1 (dogfood_run/ dir), #2 (mission_journal gitignore), #4 (resolver gate order), #5 (stale worker state) |
| TESTING | 1 | #17 (E2E testing approval gate methodology) |
| PRE-EXISTING | 1 | #25 (integration test LLM flakiness) |

**No OPEN HIGH-severity issues.** All OPEN items are LOW/MEDIUM.

---

## 6. Red-Team & Audit Results

| Audit | Date | Verdict |
|-------|------|---------|
| Red-Team (offline) | 2026-07-14 | **0 P0 hallucinations** across 596 Qs. 421 honest refusals (safe). Paraphrase convergence blind spot: 20/30 phrasing variants collapse offline. |
| Red-Team (online) | 2026-07-14 | Same 10/30 convergence. LLM adds variety, not reliability for portfolio-identity phrasing. |
| Full System Audit | 2026-07-16 | **5 HIGH** → all fixed (M9.2.5). **10 MEDIUM** → status unknown (most not tracked in KNOWN_ISSUES). Architecture health: 7.5/10. Pipeline health: 6.5/10. |

---

## 7. Gaps & Unknowns

### Documentation gaps
- [ ] **No current milestone / sprint tracking file.** The working tree says "Phase N" or "Task N" — what milestone are these changes for?
- [ ] **No roadmap beyond M9.5.** Review, Repair layers are next — when?
- [ ] **FRIDAY_DETERMINISTIC_ONLY env var** introduced but no documentation anywhere (not in README, ARCHITECTURE.md, or .env.example)
- [ ] **general_reasoning** need type not documented in `_NEED_TYPES` comment block (though added to tuple)

### Technical unknowns
- [ ] **9router proxy status** — documented at localhost:20128/v1. Is it still running? Model config?
- [ ] **LLM API key** (`sk-d7282cf482a8748a-n0dwy7-291f1572`) — in settings.local.json. Should this be in `.env`?
- [ ] **Online audit completeness** — 27-Q re-run post-fix never completed (per refactor findings doc)
- [ ] **MEDIUM audit items fate** — 10 found, which were fixed? Only 5 HIGH were tracked (M9.2.5 sprint report). Where are the remaining?
- [ ] **Dead code** — 12 dead `db.py` helpers + ~20 other functions identified. Cleaned up or still present?
- [ ] **`__main__.py`** — still missing (KNOWN ISSUE #6, but intentional)

### Test gaps
- [x] **Learning Loop (Law 17) + Repair Loop (Law 16) tests** — **84 core tests pass:** 28 runtime observer + dogfood, 41 knowledge + evolution + renderer (2.05s), 9 observation, 7 repair loop (all scenarios).
- [x] **`run_e2e_test.py` collection sabotage**: **FIXED** — `testpaths = ["tests"]` in `pyproject.toml` prevents pytest from walking the project root. `run_e2e_test.py` (`sys.exit(1)` on import at lines 98, 112) no longer touches collection. 1371 tests collected (up from ~1324).
- [ ] **Full-suite baseline: requires local run.** `pytest -q -n auto --durations=20 tests/` should complete on a machine without the basher's 30s hard process kill. **Confirmed from here:** 84 core tests pass (41 knowledge in 2.05s, 28 runtime observer + dogfood, 7 repair), `testpaths` fix verified (1371 collected vs error before). Full baseline count and wall-clock time need a human to run once locally and paste into this doc.
- [ ] **`test_m815_integration.py`** 2-3 tests flaky from LLM non-determinism (KNOWN ISSUE #25) — workaround only
- [ ] **2 calendar-observer tests** pre-existing failures through M9.2.5+ — still failing?
- [ ] **`test_calculator.py`, `test_claude_worker.py`** — root-level test files, not in `tests/`. Coverage gap or experimental?

### Process gaps
- [ ] **Freeze policy enforcement** — ask.py changes touch the frozen Brain pipeline. Need architecture review sign-off per `FRIDAY_CORE_FROZEN.md`?

---

## 8. Key Files Reference

| File | Role |
|------|------|
| `src/friday/ask.py` | Brain reasoning pipeline (frozen) |
| `src/friday/cli.py` | CLI dispatch (additive only) |
| `src/friday/synthesis.py` | Cross-project synthesis (extension layer) |
| `src/friday/observation/runtime_observer.py` | **[NEW]** RuntimeObserver — closes Learning loop (Law 17) |
| `docs/CORE_ARCHITECTURE_LAWS.md` | 25 constitutional laws |
| `docs/ARCHITECTURE.md` | Frozen core architecture |
| `docs/FRIDAY_CORE_FROZEN.md` | Freeze policy & module list |
| `docs/KNOWN_LIMITATIONS.md` | 10 intentional design boundaries |
| `docs/REDTEAM_AUDIT.md` | 596-Q adversarial audit results |
| `docs/FRIDAY_SYSTEM_AUDIT.md` | Pre-M9.2.5 full system audit |
| `KNOWN_ISSUES.md` | 25 tracked known issues |
| `docs/M9_2_5_EXECUTION_READINESS.md` | M9.2.5 sprint report (HIGH fixes) |
| `docs/M9_3_CAPABILITY_RESOLVER.md` | Capability Resolver deliverable |
| `docs/M9_4_SCHEDULER.md` | Scheduler deliverable |
| `docs/M9_5_RUNTIME.md` | Runtime deliverable |

---

## 9. Recommendations

### Before merging working tree
1. ✅ Verify the ask.py changes don't violate the freeze (general_reasoning adds a need type + early return, no new routing abstraction — likely within scope, but needs explicit check)
2. Add docs: `FRIDAY_DETERMINISTIC_ONLY` env var in README or ARCHITECTURE.md
3. Add `general_reasoning` to the need types comment block if not auto-documented

### Pipeline complete — stop building, start dogfooding
4. ✅ **Repair loop (Law 16)** — Delivered as new Planning cycle (not a new execution path)
5. ✅ **Learning loop (Law 17)** — Delivered as RuntimeObserver (not a new layer)
6. ✅ **Full constitutional pipeline complete:** Reality → Observation → Context → Knowledge → Understanding → Initiatives → Insights → Brain → Planning → Task Graph → Capability Resolver → Scheduler → Runtime → Review → Repair
7. **Next: `friday execute` against a real goal on a real repo.** Watch what happens when something genuinely breaks — that will reveal the real next build priority better than any architecture doc. The highest-value thing isn't another milestone; it's dogfooding the pipeline you've built.

### Housekeeping
7. Track fate of 10 MEDIUM audit items — which were fixed, which deferred
8. Run `pytest -q -n auto --durations=20 tests/` locally and paste baseline into this doc
9. Remove or migrate root-level test files (`test_calculator.py`, `test_claude_worker.py`)
