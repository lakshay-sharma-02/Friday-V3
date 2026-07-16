# Friday System Audit — Pre-Execution-Era Red Team

**Date:** 2026-07-16
**Scope:** M1 (ingestion) → M9 (planning / task graph / worker registry)
**Method:** Parallel read-only auditors (Parts A–J) + lead-auditor verification of HIGH-severity findings.
**Mandate:** Audit only. No code changed. No fixes applied.

---

## 1. Executive Summary

Friday is a well-structured, layered AI engineering assistant. The architecture constitution (`docs/CORE_ARCHITECTURE_LAWS.md`) is taken seriously and mostly honored. The pipeline from Reality → Observation → Context → Knowledge → Understanding → Initiatives → Insights → Brain → Planning → Task Graph → Worker Registry is internally coherent, with strong dangling-citation guards that prevent most "impossible states."

However, the audit found **real, reproducible defects** — including a CLI command that always crashes, a database table with no primary key that silently duplicates rows on every ingest, a confidence-lifecycle that inflates purely from re-running builds, and a missing execution layer (Capability Resolver / Scheduler / Runtime) that means Friday cannot yet enter the Execution Era.

**The system is NOT production-ready for execution, but the data foundation is sound enough to continue building on** — provided the HIGH-severity items below are fixed before any execution layer lands.

### Bug severity tally (verified)
| Severity | Count | Highlights |
|---|---|---|
| HIGH | 5 | `knowledge evolution` crash; `observations` no PK; knowledge verification inflation; context build non-idempotent; missing cross-ref FKs |
| MEDIUM | 10 | capability vocab drift; understanding status regression; worker manifest int-cast crash; partial-write on graph generate; lazy-import circular risk; dead DB writers; snapshots/relationships dup risk; silent enum coercion; JSON reload bypasses validation; delete_knowledge footgun |
| LOW | 8 | duplicate confidence algorithm (triplicated); deprecated `intent` path; CLI inconsistent `knowledge` action; `worker register` positional; no schema_version on Plan/Worker/Knowledge/Understanding/Initiative/Insight; undocumented GRAPH_STATUSES |

---

## 2. Architecture Health Score: **7.5 / 10**

- **Layer boundaries:** Mostly clean. One clear Law 19 violation (Knowledge → Brain `identity`).
- **Dependency direction:** Downward-only holds except the one violation above.
- **Frozen modules:** Respected; no redesign detected.
- **Brain/observation reality-mutation:** Verified clean (Law 2 / Law 8 hold).
- **Capability ownership:** Laws 11–14 unenforceable — Resolver/Scheduler/Runtime absent (gap, not violation).

**Confirmed Law violations:**
- **Law 19 (downward deps only):** `src/friday/knowledge/static.py:67,186` imports `..identity` (Brain tier) via function-local import to dodge a hard circular import. Latent circular coupling.
- **Law 24 (versioned contracts):** Plan, Worker Manifest, Knowledge, Understanding, Initiative, Insight carry no `schema_version`. Only Task Graph is versioned.

---

## 3. Pipeline Health Score: **6.5 / 10**

Transitions are structurally sound and idempotent at the row level (deterministic IDs + `INSERT OR REPLACE`). Dangling-citation guards in Understanding/Initiative/Insight are excellent. But two HIGH-severity idempotency bugs corrupt state on re-run.

**Transition matrix:**
| Transition | Contract | Evidence propagation | Confidence propagation | Idempotent | Issues |
|---|---|---|---|---|---|
| Observation→Context | OK | OK | OK | **NO** | duplicate sessions per build |
| Context→Knowledge | OK | **PARTIAL** | OK (inflates) | OK rows / NO lifecycle | trends have empty evidence |
| Knowledge→Understanding | OK | OK | OK | OK | status can regress |
| Understanding→Initiatives | OK | OK | minor | OK | per-contributor repo ignored |
| Initiatives→Insights | OK | OK | OK | OK | — |
| Insights→Planning | OK | OK | divergent heuristic | OK | — |
| Planning→Task Graph | OK | OK | pass-through | OK | — |
| Task Graph→Worker Registry | n/a (parallel) | n/a | n/a | n/a | **capability vocab mismatch** |

---

## 4. CLI Coverage

**Every engine feature is reachable through the CLI.** No orphan modules at the top level (all `cli_*.py` imported by `cli.py`). No missing routes for implemented features. The `brain` module referenced in docs does not exist on disk — only in docstrings.

**Commands tested (fresh DB, `FRIDAY_DB=/tmp/friday_audit.db`):** `help`, `summary`, `observe`, `observers`, `context`(+`build`/`today`), `sessions`, `timeline`, `observer git`, `knowledge`(+`build`/`verify`/`history`/`explain`), `understanding`(+`build`/`evolution`), `initiatives`(+`build`/`timeline`), `insights`(+`build`), `plan`(+`list`/`history`/`explain`), `plans`, `graph`(+`list`/`export`/`explain`), `graphs`, `workers`, `worker`(+`export`/`register`), `audit`, `ask`. All ran; most OK.

**Defects found:**
- **CRASH — `friday knowledge evolution`** (HIGH): `NameError: name 'evolution_events_all' is not defined` on any DB. Root cause: `knowledge/evolution.py:481` calls `evolution_events_all(conn)` but it is absent from the `from ..db import (...)` block at `evolution.py:40-49` (siblings `understanding/engine.py` and `initiative/engine.py` DO import it). See §7 Bug A1.
- **Inconsistent `knowledge` action validation** (LOW): `cli.py:396-399` leaves `action` free-form with no `choices=`, so `friday knowledge bogus` silently lists instead of erroring — unlike `understanding`/`initiatives`/`insights` which constrain `choices`.
- **`worker register` positional broken** (LOW): only `--file` is wired; `friday worker register manifest.json` → argparse "unrecognized arguments".
- No wrong exit codes found (errors return 2, success 0). No bare `except:` swallowing in CLI layer.

---

## 5. Database Health: **6 / 10**

31 tables. FK enforcement is ON but per-connection only (`db.py:659`), correct for production code. Law 20 (history append-only) holds — no UPDATE/DELETE against any `*_history`/`*_evolution` table found.

**Critical issues:**
- **`observations` has NO PRIMARY KEY / UNIQUE** (`db.py:105-115`): `id TEXT NOT NULL` only. `INSERT OR REPLACE` at `db.py:1137` therefore degrades to plain INSERT → **duplicate rows on every re-ingest**. HIGH. See §7 Bug C1.
- **No FK on cross-layer references:** `tasks.plan_id`, `tasks.graph_id`, `task_edges.*`, and all `*_evolution`/`*_history` `*_id` columns have no `REFERENCES`. Deleting a plan/graph/understanding leaves orphan rows silently. HIGH. See §7 Bug C2.
- `relationships` no composite UNIQUE → duplicate edges possible (MEDIUM).
- `snapshots` no UNIQUE(observed_at, repo_path) → ambiguous "latest" window (MEDIUM).
- `delete_knowledge` hard DELETE live and exported (`store.py:92-95`) — history/live drift footgun if called (MEDIUM).
- Implicit schema migration (`_migrate`, `db.py:665-688`), no `schema_version` table (LOW/MED).
- `Knowledge.to_row()` omits `is_static` (`models.py:140-141`) — inconsistent helper, no live drift (LOW).

**Orphan tables:** none (all 31 written + read).

---

## 6. Dead Code Report

**Dead functions/classes (zero external callers, verified via repo-wide grep):**
- `db.py` (12): `replace_relationships`, `get_relationships`, `entry_points_by_kind`, `evolution_events_for`, `count_understanding`, `count_initiatives`, `get_insights_by_type`, `count_insights`, `count_plans`, `update_task_graph_status`, `latest_task_graph_snapshot`, `replace_worker_capabilities`.
- `knowledge/confidence.py`: `should_retire`, `merge_duplicate_knowledge`.
- `knowledge/engine.py`: `stable_knowledge`.
- `initiative/engine.py`: `initiatives_by_type`.
- `understanding/engine.py`: 4 internal `get_*_by_*` query methods (self-only).
- `query.py`: `repo_by_name`, `projects_sharing_config`.
- `portfolio.py`: `workspace_recommendations`, `is_real`.
- `readme.py`: `purpose_only`. `judgment.py`: `is_strong`. `evidence_scope.py`: `as_dict` (ScopeReport).
- `observation/engine.py`: `all_changes`, `all_observations`.
- `worker/engine.py`: `workers_for_capability` (FOOTGUN — this is the intended Capability Resolver entry point, dead because resolver absent), `register_builtins`, `upgrade_version`, `active_workers`.
- `insight/derivation.py`: `k_by_subject`, `u_by_subject`.

**Duplicate algorithms:**
- **Confidence aggregation triplicated** across `understanding/confidence.py`, `initiative/confidence.py`, `insight/confidence.py` (~140 lines, `initiative`/`insight` byte-identical, `understanding` differs only in multiplier). Should be one shared module parameterized by multiplier.

**TODO/FIXME:** 10 `TODO: remove` markers in `ask.py` for a legacy `intent` compat path (`ask.py:34,108,1847,1861,1974,1982,1993,2005,2017,2040`). Deprecated `_label_of`, `_needs_for_intent`, legacy `intent` branch at `ask.py:1557` still live.

---

## 7. Hidden Bug Report

### A1 — `friday knowledge evolution` always crashes (HIGH)
- **Root cause:** `src/friday/knowledge/evolution.py:481` calls `evolution_events_all(conn)`, but the symbol is not imported (import block `evolution.py:40-49` omits it; `db.py:1488` defines it).
- **Severity:** HIGH — advertised subcommand, crashes on fresh AND populated DB.
- **Repro:** `rm -f ~/.friday/friday.db; friday knowledge evolution` → `NameError`.
- **Files:** `knowledge/evolution.py:481`; missing import from `db.py:1488`.
- **Fix:** add `evolution_events_all` to the `from ..db import (...)` block at `evolution.py:40-49` (mirror `understanding/engine.py:20`, `initiative/engine.py:310`).

### C1 — `observations` table has no primary key → duplicate rows (HIGH)
- **Root cause:** `db.py:105-115` declares `id TEXT NOT NULL` with no PK/UNIQUE; `INSERT OR REPLACE` at `db.py:1137` cannot match → plain insert each call.
- **Severity:** HIGH — silent data amplification corrupts `latest_observations` / `observation_state_as_of`.
- **Repro:** call `insert_observations` twice with same rows → 2× rows, same `id`.
- **Files:** `db.py:105-115`, `db.py:1131-1146`.
- **Fix:** add `PRIMARY KEY (id)` (or `UNIQUE(id)`) to `observations`.

### B1 — Knowledge verification_count inflates on every build (HIGH)
- **Root cause:** `knowledge/engine.py:138` calls `verify_knowledge` (which does `verification_count += 1`, `confidence.py:46`) for every pre-existing knowledge row on every build, with no genuine-new-verification gate.
- **Severity:** HIGH — corrupts the STABLE/VERIFIED lifecycle (Law 4 confidence-through-evidence) purely from re-running builds.
- **Repro:** `friday knowledge build` ×3 → verification_count triples, statuses over-promote (OBSERVED→VERIFIED→STABLE) with no new evidence.
- **Files:** `knowledge/engine.py:136-143`, `knowledge/confidence.py:44-54`.
- **Fix:** only increment on a genuine new confirmatory signal, or make status a deterministic function of stable evidence rather than an accumulator.

### B2 — Context `build()` is not idempotent (HIGH)
- **Root cause:** session id includes `built_at` (`context/models.py:105-107`); build stamps fresh `as_of` each run → new deterministic id → `INSERT OR REPLACE` inserts a NEW row. Docstring falsely claims idempotency (`context/engine.py:85-88`).
- **Severity:** HIGH — violates Law 20 idempotency; duplicates sessions, breaks `is_stale()` reasoning.
- **Repro:** `friday context build` twice → session count doubles.
- **Files:** `context/engine.py:80-127`, `context/models.py:105-107`.
- **Fix:** key session on stable `(source, primary_repo, start_time, end_time)` tuple; `INSERT OR REPLACE` on that.

### C2 — No FK constraints on task/evolution cross-references (HIGH)
- **Root cause:** `tasks`, `task_edges`, and all `*_evolution`/`*_history` reference columns lack `REFERENCES` (`db.py` schema). FK enforcement is ON but has nothing to enforce.
- **Severity:** HIGH — referential integrity silently absent; deleting a plan/graph/understanding leaves dangling tasks/edges/events.
- **Files:** `db.py:453-509` (tasks/edges), `db.py:168-359` (evolution), `db.py:392-430` (plan history/evolution).
- **Fix:** declare `REFERENCES` (+ ON DELETE CASCADE where appropriate): `tasks.plan_id→plans`, `tasks.graph_id→task_graphs`, `task_edges.graph_id→task_graphs`, `task_edges.from_task/to_task→tasks`, and `*_id→` live tables.

### B3 — Understanding status regresses downward on rebuild (MEDIUM)
- **Root cause:** `understanding/engine.py:149` recomputes `status_from_confidence` and overwrites a prior higher status; docstring says "only advance upward" (`:86-88`).
- **Severity:** MEDIUM — silent lifecycle regression.
- **Fix:** only advance status rank; never decrease unless RETIRED logic applies.

### B4 / J2 — Capability vocabulary drift (MEDIUM, corrected)
- **Root cause:** compiler emits **lowercase** caps (`compiler.py:_ALL_CAPS`: `frontend`, `backend`, `architecture`…) while `worker/models.py:_CAP_CANON` stores **Capitalized** forms. NOTE: `validate_capabilities`/`is_valid_capability` use a case-insensitive `_CANON_MAP` (`worker/models.py`), so `is_valid_capability("frontend")` returns `True` and registry ingestion capitalizes correctly. The residual risk is a **future Capability Resolver doing an exact (case-sensitive) set comparison** between `task.required_capabilities` (lowercase) and `worker.capabilities` (Capitalized) → no match. Severity is MEDIUM, not HIGH (the prior claim of "all False" was incorrect — the case-insensitive map mitigates ingestion/validation).
- **Severity:** MEDIUM.
- **Fix:** single canonical capability enum imported by compiler + worker + graph_schema; derive lowercase forms from it. Do not rely on every future consumer remembering case-insensitivity.

### D1 — Latent circular import masked by lazy import (MEDIUM)
- **Root cause:** `knowledge/static.py:67,186` function-local `from ..identity import build_identity` to avoid a load-time circular import. A future top-level promotion re-breaks `import friday.cli`.
- **Fix:** Knowledge must not import a Brain-tier module (Law 19). Read persisted identity rows via `db`, or move the needed slice lower.

### D2 — Lower-layer re-derivation on every build (MEDIUM)
- **Root cause:** `insight/engine.py:38,40` and `planning/engine.py:32-35` instantiate and `.build()` lower engines inside a higher build rather than reading persisted rows.
- **Fix:** higher layers should read persisted lower-layer rows, not re-invoke lower `.build()`.

### F/H bugs (MEDIUM)
- **H1 Silent enum coercion:** every `*Enum.from_str` returns a hard-coded default (e.g. `TaskType.from_str("xyz")→"implementation"`) instead of raising. Corrupt stored enums never surface. Fix: strict validator on storage read-back.
- **H2 `validate_task_graph` hard-rejects old versions; no migration (Law 24):** `graph_schema.py:171-172` `== SCHEMA_VERSION`, no migrate branch. Fix: `migrate_task_graph()` dispatcher.
- **H3 Task Graph DB reload bypasses validation:** `graph_engine._rebuild`/`_task_from_row` (`graph_engine.py:80-113`) never calls `validate_task_graph`; uses defaulting `from_str`. Fix: validate inside `_rebuild`.
- **J4 Partial write / orphan graph:** `graph_engine.generate` inserts plan before compile; compile raises on empty milestones (`compiler.py:708`) leaving plan with no graph; `insert_task_graph` (`db.py:2743`) issues header/tasks/edges as separate statements, one commit — crash leaves header with 0 tasks. No transaction. Fix: wrap `generate` + `insert_task_graph` in `BEGIN/COMMIT`; gate compile on non-empty milestones.
- **J5 Worker manifest `int()` casts unguarded:** `worker/engine.py:335-336` `int(m.get("context_window",0))` raises on non-numeric string. Fix: validate/coerce with try/except → `RegistryError`.
- **D3 Dead DB writers → unwritten tables:** `update_task_graph_status`, `latest_task_graph_snapshot`, `replace_worker_capabilities` have zero callers → task-graph status transitions + worker-capability sync never persisted. Medium for replay integrity once Runtime lands.

### G (Performance) bugs — see §10.

---

## 8. Broken Contracts

- **Missing `schema_version`** on Plan, Worker Manifest (`version` is manifest semver, not contract version), Knowledge, Understanding, Initiative, Insight (Law 24). Only Task Graph is versioned.
- **Task Graph contract never invoked:** `validate_task_graph` (`graph_schema.py`) has zero call sites — producers and consumers skip it; malformed graphs pass silently.
- **Three divergent capability vocabularies:** `compiler._ALL_CAPS` (lowercase), `worker._CAP_CANON` (Capitalized) + extras, `graph_schema.CAPABILITIES` (lowercase) — no single source (Law 10/24).
- **`GRAPH_STATUSES` undocumented + unused** (`graph_schema.py:84`) — dead/undocumented enum.
- **JSON round-trip:** Task Graph DB reload recomputes `critical_path`/`parallel_tasks` (deterministic today, silent semantic drift risk if semantics change without version gating). `acyclic` hardcoded `True` on export without re-validating cycle.
- **Docs vs code:** `task_graph_schema.md` closed vocabularies omit `GRAPH_STATUSES`; capability/task-type lists don't match worker canonicals.

---

## 9. Missing Wiring

- **Capability Resolver — NOT IMPLEMENTED.** No `CapabilityResolver` class, no `assign` anywhere. `worker/engine.py:workers_for_capability()` is the intended hook but dead (no caller).
- **Scheduler — NOT IMPLEMENTED.** No topological ordering/sequencing code beyond `compiler` computing levels/critical-path (data only, not a scheduler).
- **Runtime / Executor — NOT IMPLEMENTED.** No code invokes a worker or mutates `Task.status`/`Graph.status`. `TASK_STATUSES`/`GRAPH_STATUSES` declared but never mutated.
- **No wiring after `TaskGraphEngine.generate()`** (`graph_engine.py:163`): graph persisted, pipeline stops.
- **CLI is catalog-only:** `cli_graph.py`/`cli_worker.py` read/export/register only.
- **Dead writers** (`update_task_graph_status`, etc.) mean status/replay fields are never written.

---

## 10. Performance Findings

- **Cold start:** `cli.py:29-43` imports the entire stack (ask, architecture, ingest, observe, all cli_*) at module load; even `friday workers` pays full cost. `observation/__init__.py` builds `default_registry()` at import (instantiates all observers). No lazy imports for expensive modules.
- **O(n²):** `compiler.py:787-790` inter-phase edge build is O(n²) over tasks. `graph_engine.py:291` `_title()` linear-scans all tasks per critical-path entry on print (display-only).
- **Dead full-table scan:** `planning/engine.py:145-146` computes `active_n` from `get_all_plans()` but the variable is never used — full scan + N object builds on every `generate()`.
- **Redundant SQL:** `planning/engine.py:152-163` `_gather_evidence()` runs 4 engine instantiations + 4 full-table queries per `generate()`. `insert_*` helpers loop + single-execute per row instead of `executemany`.
- **Unnecessary writes:** `insert_plan`/`insert_task_graph`/`insert_worker` rewrite full rows unconditionally on every call (no change-detection). `Plan.to_row()` re-renders `plan_text` via `render_text()` every persist.
- **N+1:** `graph_engine._rebuild` issues 2 queries per graph; `all_graphs()` + per-graph rebuild = 1+2N.

---

## 11. Scalability Risks

- `observations` duplicate-row growth (no PK) compounds every ingest.
- Context session duplication per build (non-idempotent) → unbounded sessions.
- `plan_history`/`task_history` keyed `(generated_at, id)` with `INSERT OR REPLACE` — re-running identical timestamp collapses history.
- O(n²) compiler edge-build and per-graph SQL N+1 will degrade on large plans.
- No WAL/`PRAGMA journal_mode` in `connect()` (`db.py:647-662`) — crash mid-multi-statement insert leaves partial rows.

---

## 12. Architectural Risks

- Law 19 violation (Knowledge→Brain `identity`) + latent circular import.
- Laws 11–14 unenforceable (no Resolver/Scheduler/Runtime).
- Capability/Scheduler/Runtime absent = Execution Era impossible.
- `validate_task_graph` never called = contract unenforced.
- Triplicated confidence algorithm = future drift risk.
- Dead `intent` compat path in `ask.py` = divergence risk from `RetrievalRequirements`.

---

## 13. Production Readiness

**Not ready for execution.** Planning, Task Graph, and Worker Registry are individually complete and well-engineered, but the execution layer (Resolver/Scheduler/Runtime) does not exist, and three HIGH-severity data-integrity bugs must be fixed before any worker ever reads the DB:
1. `observations` missing PK (duplicate rows).
2. Knowledge verification inflation (false confidence).
3. `knowledge evolution` crash (broken audit command).
4. Context build non-idempotent (duplicate sessions).
5. Missing cross-ref FKs (orphan rows).

The **data foundation is safe to continue building on** for the analysis/planning layers — none of the HIGH bugs corrupt read-only reasoning, and the lower pipeline's dangling-citation guards are solid.

---

## 14. Technical Debt

- 12 dead `db.py` helpers + ~20 other dead functions.
- Triplicated confidence aggregation (~140 lines).
- 10 `TODO: remove` legacy `intent` path in `ask.py`.
- No `schema_version` on 6 contracts.
- Implicit schema migration (no version table).
- No single canonical capability/task-type enum.
- `validate_task_graph` uninvoked.
- Dead writers (`update_task_graph_status` etc.) implying unimplemented status tracking.

---

## 15. Immediate Fixes (priority order)

1. **`observations` PK** (`db.py:105-115`) — add `PRIMARY KEY (id)`. One-line schema fix; prevents silent duplication.
2. **`knowledge evolution` crash** (`evolution.py:40-49`) — add `evolution_events_all` to import.
3. **Knowledge verification inflation** (`knowledge/engine.py:138`) — gate `verify_knowledge` on genuine new evidence.
4. **Context build idempotency** (`context/models.py:105-107`) — drop `built_at` from session id.
5. **Cross-ref FKs** (`db.py`) — add `REFERENCES` for task/evolution tables.
6. **Capability single-source** — one canonical enum for compiler/worker/graph_schema.
7. **Wrap graph generate/insert in transactions** (`graph_engine.py`, `db.py:2743`).
8. **`validate_task_graph` call site** — invoke in `graph_engine.generate` and `_rebuild`.
9. **Strict enum parsing on read-back** — raise on unknown instead of defaulting.
10. **CLI `knowledge action` choices** + `worker register` positional fix.

---

## 16. Future Improvements

- Implement Capability Resolver (consumes `workers_for_capability`), Scheduler, Runtime.
- `migrate_task_graph()` dispatcher + backward-compat readers (Law 24).
- Collapse triplicated confidence into one parameterized module.
- Add `schema_version` to Plan/Worker/Knowledge/Understanding/Initiative/Insight.
- Lazy-import heavy CLI modules; build observers on demand.
- `executemany` for bulk inserts; change-detection before `INSERT OR REPLACE`.
- WAL mode + explicit transactions in `connect()`.
- Remove dead `intent` path after benchmark migration.
- Break Knowledge→Brain circular dependency (Law 19).

---

## 17. Go / No-Go Recommendation

**NO-GO for the Execution Era.** GO for continued analysis/planning-layer development.

Friday's cognitive stack (M1–M9.0) is coherent and mostly law-abiding, but it cannot execute: the execution layer is absent, and five HIGH-severity data-integrity defects would corrupt or crash the moment a worker or auditor touches the database. Fix the five HIGH items (§15 #1–5) before building Resolver/Scheduler/Runtime. The architecture is worth building on — the layered discipline is real and the pipeline guards are strong.

---

## Audit Sign-off

- **Files inspected:** `src/friday/*.py` (all modules), `docs/CORE_ARCHITECTURE_LAWS.md`, `docs/task_graph_schema.md`, `db.py` schema (31 tables), all `cli_*.py`, all `*/engine.py` + `*/models.py`.
- **Commands tested:** ~35 CLI invocations on a fresh DB (full list §4).
- **Pipeline stages verified:** Reality → Observation → Context → Knowledge → Understanding → Initiatives → Insights → Brain → Planning → Task Graph → Worker Registry (10 transitions).
- **Contracts verified:** Task Graph (versioned), Plan, Worker Manifest, Knowledge, Understanding, Initiative, Insight, History format.
- **Hidden bugs found:** 5 HIGH, 10 MEDIUM, 8 LOW (detailed §7).
- **Dead code found:** 12 `db.py` helpers + ~20 functions + 1 triplicated algorithm + 10 TODOs.
- **Broken wiring found:** Capability Resolver, Scheduler, Runtime absent; `validate_task_graph` uninvoked; 3 dead DB writers.
- **Architecture violations:** 1 Law 19 (Knowledge→Brain), Law 24 gaps (6 contracts unversioned).
- **Production readiness score:** **5.5 / 10** (strong analysis stack, no execution layer, 5 HIGH data bugs).
- **Safe to continue building on?** Yes — for analysis/planning layers, after fixing the 5 HIGH items before any execution code lands.
