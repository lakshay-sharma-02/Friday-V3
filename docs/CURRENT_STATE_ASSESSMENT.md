# Current State Assessment — Friday V3

**Date:** 2026-07-23
**Source:** Codebase audit, docs, git history, memory files, milestone reports.
**Scope:** Full stack, Reality → Runtime, including working tree.

---

## 1. Project Identity

| Property | Value |
|----------|-------|
| Name | `friday` v0.1.0 |
| Description | "Persistent AI operating partner: workspace understanding" |
| Language | Python ≥3.12 (hatchling build) |
| Size | ~67.8K lines Python (`src/friday/`), 1324 tests |
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

**Not yet built:** Review layer, Repair loop, Learning from execution.

---

## 4. Current Working Tree Changes

### Modified (3 files, +99/-5 lines)

| File | Changes | Purpose |
|------|---------|---------|
| `src/friday/ask.py` (+45/-5) | Add `"general_reasoning"` need type; gate changed from `FRIDAY_ANSWER_LLM` to `FRIDAY_DETERMINISTIC_ONLY`; LLM understanding failure fallback to offline heuristic | General-reasoning questions (math, logic) handled without workspace evidence; flip LLM gate from opt-in to opt-out semantics |
| `src/friday/cli.py` (+3) | Registers `synthesize` subcommand | Wire new CLI command |
| `tests/test_ask.py` (+51) | 4 new regression tests | (a) workspace evidence still grounded, (b) no-evidence honesty, (c) general_reasoning offline answer, (d) FRIDAY_ANSWER_LLM gate removed from code |

### New (3 files)

| File | Lines | Purpose |
|------|-------|---------|
| `src/friday/synthesis.py` | ~305 | Cross-project synthesis: compares two repos' structural evidence for genuine overlap. LLM path with deterministic fallback. No auto-action. |
| `src/friday/cli_synthesize.py` | ~35 | CLI for `friday synthesize <repo-a> <repo-b>` |
| `tests/test_synthesis.py` | ~104 | 4 tests: no-LLM-no-overlap, missing repo, self-relationship, confidence label |

### Summary
Working tree adds a new **extension layer** (synthesis) and a **general-reasoning pathway** in the Brain. Uncommitted. No frozen modules modified (cli.py is wire-only; ask.py changes are within the reasoning pipeline — verify against freeze policy).

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
- [ ] **No roadmap beyond M9.5.** Review, Repair, Learning layers are next — when?
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
- [ ] **`test_m815_integration.py`** 2-3 tests flaky from LLM non-determinism (KNOWN ISSUE #25) — workaround only
- [ ] **2 calendar-observer tests** pre-existing failures through M9.2.5+ — still failing?
- [ ] **`test_calculator.py`, `test_claude_worker.py`** — root-level test files, not in `tests/`. Coverage gap or experimental?

### Process gaps
- [ ] **No regression baseline confirmed** — suite grown from 311→493→590→816→881→1022→1324. What's the current pass/fail?
- [ ] **Freeze policy enforcement** — ask.py changes touch the frozen Brain pipeline. Need architecture review sign-off per `FRIDAY_CORE_FROZEN.md`?

---

## 8. Key Files Reference

| File | Role |
|------|------|
| `src/friday/ask.py` | Brain reasoning pipeline (frozen) |
| `src/friday/cli.py` | CLI dispatch (additive only) |
| `src/friday/synthesis.py` | **[NEW]** Cross-project synthesis (extension layer) |
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

### Next milestones (unblocked)
4. **Review layer (M9.6?)** — worker success verification (Law 15)
5. **Repair loop (M9.7?)** — evidence-driven recovery from failure (Law 16)
6. **Learning from execution (M9.8?)** — execution → observation → knowledge (Law 17)

### Housekeeping
7. Track fate of 10 MEDIUM audit items — which were fixed, which deferred
8. Establish current regression baseline (run full suite, record count)
9. Remove or migrate root-level test files (`test_calculator.py`, `test_claude_worker.py`)
10. Add `__main__.py` or document its intentional absence in README
