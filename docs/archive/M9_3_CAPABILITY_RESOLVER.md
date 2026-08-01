# Milestone 9.3 — Capability Resolver

**Status:** COMPLETE — Execution Readiness verified.
**Architecture:** FROZEN. Only the Capability Resolver layer was added.

---

## 1. Architecture

The Capability Resolver is the **ONLY** layer permitted to map a `Task` → `Worker`.
It reads two existing, unchanged layers and writes one new layer:

```
Task Graph  (read-only)        Worker Registry  (read-only)
        \                                /
         \                              /
          v                            v
        +------------------------------+
        |     Capability Resolver       |
        |  (deterministic matching)     |
        +------------------------------+
                     |
                     v
        Execution Assignment  (persisted: resolver_assignments)
                     |
                     v
        (Runtime — future milestone: NOT present)
```

- **Tasks** never know workers.
- **Planning** never knows workers.
- **Task Graph** never knows workers.
- **Worker Registry** is a passive catalog; never invoked, never executed.

The Resolver **executes nothing**: no shell, no worker calls, no LLM, no repo
access, no file edits. It produces a `WHO → WHICH task → WHY` decision only.

### Files added

| File | Role |
|------|------|
| `src/friday/resolver/__init__.py` | Package exports |
| `src/friday/resolver/models.py` | `Assignment`, `ResolutionResult`, `ScoreBreakdown`, `SelectionStrategy`, `ResolutionStatus`, `SCHEMA_VERSION` |
| `src/friday/resolver/resolver.py` | Pure scoring + ranking + selection (no I/O) |
| `src/friday/resolver/engine.py` | `CapabilityResolver`, `ResolveResult` — reads Task Graph + Worker Registry, persists assignments |
| `src/friday/resolver/confidence.py` | Deterministic confidence derivation |
| `src/friday/cli_resolver.py` | CLI: `friday resolve`, `friday resolver` |
| `tests/test_resolver.py` | 65 regression tests |

The CLI dispatch already existed in `src/friday/cli.py` (`resolve`, `resolver`
subcommands) and was **not modified** for this milestone.

---

## 2. Database additions

Three dedicated tables (Worker Registry is **not** overloaded):

### `resolver_assignments`
One `Task → Worker` mapping per resolution.

| Column | Type | Notes |
|--------|------|-------|
| `assignment_id` | TEXT PK | `{graph_id}:{task_id}` |
| `graph_id` | TEXT FK → task_graphs | |
| `task_id` | TEXT FK → tasks | |
| `worker_id` | TEXT FK → workers (SET NULL) | `NULL` when UNRESOLVED |
| `status` | TEXT | `assigned` / `unresolved` |
| `confidence` | TEXT | `high` / `medium` / `low` |
| `reason` | TEXT | Why this worker |
| `matched_capabilities` | JSON | |
| `missing_capabilities` | JSON | |
| `selection_strategy` | TEXT | `single` / `parallel` / `sequential` |
| `schema_version` | TEXT | Law 24 contract version (`1.0`) |
| `created_at` | TEXT | |
| `updated_at` | TEXT | |

`UNIQUE(graph_id, task_id)` — re-resolution updates the live row in place
(via `UPDATE`, **not** `INSERT OR REPLACE`) so history is never cascade-deleted.

### `resolver_history`
Append-only snapshot of every resolution run — never updated, only inserted.
Surrogate `hid INTEGER PRIMARY KEY AUTOINCREMENT` guarantees a new row even when
two runs share the same `resolved_at` (sub-millisecond re-resolution).

`assignment_id` FK is `ON DELETE SET NULL` so history survives any assignment
deletion.

### `resolver_evolution`
Append-only decision-churn log: `from_worker_id → to_worker_id`, `change_type`
(`reassigned` / `unresolved`), `reason`. Primary key
`(evolved_at, task_id, from_worker_id, to_worker_id)`.

A one-time migration (`_ensure_resolver_history_pk`) rebuilds the history table
on existing databases to add the surrogate PK and `SET NULL` FK.

---

## 3. Resolution algorithm

```
INPUT:  Task (required_capabilities, task_type, plan_type)
        Worker Registry (active workers + their capability profiles)

1. Collect required_capabilities from the Task.
2. Collect supported capabilities from each ACTIVE Worker.
3. Score each worker (capability + language + task-type + plan-type
   + availability + confidence − penalties).
4. REJECT workers missing any mandatory capability.
   REJECT disabled workers (never eligible).
5. Rank deterministically.
6. Produce Assignment (+ candidates + alternatives).
```

**No randomness. No LLM. No time-dependent heuristics. Same input → same output.**

---

## 4. Scoring formula

```
Score =
    + 10 × (matched mandatory capabilities)        [_W_CAPABILITY]
    + 5  if task language ∈ worker.supported_languages   [_W_LANGUAGE]
    + 5  if task_type ∈ worker.supported_task_types     [_W_TASK_TYPE]
    + 3  if plan_type ∈ worker.supported_plan_types     [_W_PLAN_TYPE]
    + 5  if worker.status == active                    [_W_AVAILABLE]
    + {high:5, medium:2, low:0}[worker.confidence]     [_W_CONFIDENCE]
    − 20 × (missing mandatory capabilities)           [_P_MISSING_CAP]
    − 20 if worker disabled                           [_P_DISABLED]
    − 5  if task needs a language the worker lacks     [_P_UNSUPPORTED_LANG]
    − 5  if worker lacks the task_type                 [_P_UNSUPPORTED_TASK]
    − 3  if worker lacks the plan_type                 [_P_UNSUPPORTED_PLAN]
```

`total = capability + language + task_type + plan_type + availability
             + confidence − penalty`

Implemented in `ScoreBreakdown.total` (fully reproducible from inputs).

### Tie-break order (deterministic)
1. Capability score (higher better)
2. Confidence band (`high` > `medium` > `low`)
3. Estimated speed (`fast` > `medium` > `slow`)
4. Estimated cost (`low` > `medium` > `high`)
5. Alphabetical worker id

### Matching rule
Capability match is **EXACT** and case-insensitive against the frozen Worker
Registry vocabulary. `Rust` matches `Rust`; it does **not** match `Programming`.
No fuzzy logic, no embeddings, no synonyms. Unknown capabilities in a task's
requirements are reported as **missing** (never silently dropped, never invented).

---

## 5. Multi-worker support

`SelectionStrategy` decides **eligibility only** (the future Scheduler decides
timing — it does not exist here):

| Strategy | Chosen workers | Use |
|----------|----------------|-----|
| `SINGLE` | top-ranked only | default |
| `PARALLEL` | all eligible | graph marks task parallel |
| `SEQUENTIAL` | all eligible | explicit dependency chain |

The Resolver records `candidates` (eligible worker ids) and `alternatives`
(ranked runners-up) for every assignment. No execution.

---

## 6. Failure handling — UNRESOLVED

If no active worker satisfies the mandatory capabilities:

```
status      = unresolved
worker_id   = None          (NEVER invented)
missing     = all required capabilities not satisfied
reason      = "No eligible worker satisfied the mandatory capabilities."
```

No worker is hallucinated. No degradation to a wrong worker. The decision is
recorded with `worker_id = NULL` and surfaced by `friday resolver explain`.

---

## 7. Confidence derivation

Deterministic, derived from (never guessed):

- **Capability coverage** — fraction of required caps the worker has
- **Task coverage** — worker supports the task's `task_type`
- **Plan coverage** — worker supports the `plan_type`
- **Worker confidence** — registry-assigned profile
- **Historical compatibility** — prior `assigned` resolutions (`resolver_history`)

Bands: `high` / `medium` / `low`. Full mandatory coverage + task supported +
high worker confidence (or ≥3 successful history) → `high`. Any missing
mandatory cap → at most `medium` (or `low` at zero coverage). No LLM, no guessing.

---

## 8. CLI

```bash
# Generate Plan -> Task Graph -> Assignments (WRITE, but no execution)
friday resolve "<goal>"

# List all assignments (READ)
friday resolver

# Explain one assignment: task, worker, capability score, matched/missing caps,
# confidence, reason, alternatives
friday resolver explain <id>
friday resolver explain <n>          # n-th newest
friday resolver explain <graph_id>   # all tasks in a graph

# JSON export of all assignments
friday resolver export
```

`friday resolve` calls `TaskGraphEngine.generate()` (existing) then
`CapabilityResolver.resolve_graph()` (new). It performs **no execution**.

---

## 9. Regression coverage

**65 tests, all passing** (`tests/test_resolver.py`). Coverage areas required by
the spec, each verified:

| Area | Tests |
|------|-------|
| Exact capability matching | `test_exact_match_*` (single, no-fuzzy, case-insensitive, multiple, all-matched) |
| Language matching | `test_language_match`, `test_language_mismatch_penalty`, `test_no_language_in_caps_no_penalty` |
| Task-type matching | `test_task_type_match`, `test_task_type_mismatch_penalty` |
| Plan-type matching | `test_plan_type_match`, `test_plan_type_mismatch_penalty` |
| Tie-breaking | `test_tie_break_by_capability_score`, `_confidence`, `_speed`, `_cost`, `_alphabetical_id` |
| Disabled workers | `test_disabled_worker_rejected`, `test_disabled_worker_penalty` |
| Missing capabilities | `test_no_eligible_worker_unresolved`, `test_unresolved_has_all_required_in_missing` |
| Unknown capabilities | `test_unknown_capability_rejected_by_validate`, `test_unknown_capability_not_in_score` |
| Unknown workers (empty pool) | `test_empty_worker_pool_unresolved` |
| Parallel assignment | `test_parallel_all_eligible`, `test_parallel_only_eligible` |
| Sequential assignment | `test_sequential_all_eligible` |
| JSON export | `test_assignment_to_dict`, `test_assignment_to_row`, `test_score_breakdown_to_dict` |
| History (append-only) | `test_history_append_only` |
| Evolution | `test_evolution_on_reassignment` |
| Idempotency | `test_idempotent_resolution` |
| Append-only (no deletion) | `test_assignments_append_only` |
| Brain compatibility (no LLM) | `test_no_llm_invoked` |
| Task Graph compatibility | `test_resolver_reads_task_fields` |
| Worker Registry compatibility | `test_resolver_uses_active_workers_only` |
| No hallucinated workers | `test_no_hallucinated_workers` |
| No duplicate assignments | `test_no_duplicate_assignments` |
| Stable / deterministic output | `test_stable_output` |
| Single strategy | `test_single_strategy_only_top_worker` |
| UNRESOLVED reason | `test_unresolved_reason_populated` |
| Multi-worker | `test_parallel_vs_single_candidate_count`, `test_alternatives_in_select` |
| Engine integration | `test_engine_resolve_graph`, `test_engine_assignments_read` |
| ResolutionResult export | `test_resolution_result_to_dict` |
| Multiple tasks | `test_multi_task_resolution` |
| Custom workers | `test_custom_worker_resolves` |
| Uber-worker | `test_uber_worker_matches_all` |
| Confidence bands | `test_confidence_high/medium/low_missing/no_required_caps`, `test_confidence_at_least` |
| Schema version | `test_schema_version` |
| Availability / confidence components | `test_active_worker_availability_score`, `test_disabled_worker_no_availability`, `test_high_confidence_worker_score`, `test_low_confidence_worker_score` |
| Builtin count | `test_builtin_worker_count` |

Plus `SCHEMA_VERSION` constant and `SelectionStrategy`/`ResolutionStatus` enum
round-trips.

---

## 10. Dogfood transcript

Workers registered (builtins): **Claude, Codex, Gemini, GPT, OpenRouter,
Python, Shell, Git, Filesystem, Search,** + 2 more = **12 builtin workers**
(LLM / function / CLI / tool / service kinds — provider-agnostic).

Goals resolved (each task → exactly one best assignment, no execution):

| Goal | Example tasks → worker |
|------|------------------------|
| Implement OAuth | implementation → Codex (fast, Rust/Python/TypeScript) |
| Refactor Rust Parser | refactor → Claude / Codex (Rust + Refactoring) |
| Write Documentation | documentation → Claude / Gemini |
| Run Tests | testing → Python / Codex |
| Optimize SQL | implementation+SQL → Codex / Claude |
| Review PR | review → Claude (Code Review) |
| Architecture Design | design → Claude (Architecture) |
| Verify | verification → Codex / Claude |

Every task received **exactly one** best assignment. Re-resolution is
deterministic (identical output) and append-only (history + evolution accrue).

---

## 11. Execution readiness — explicit confirmation

- ✅ **ONLY** the Capability Resolver was added (`src/friday/resolver/*`,
  `src/friday/cli_resolver.py`, 3 DB tables, tests).
- ✅ The **Runtime remains impossible** — no execution path, no worker
  invocation, no shell, no file writes outside the DB.
- ✅ The **Scheduler does not exist** — `SelectionStrategy` records eligibility;
  timing is a future milestone.
- ✅ **Workers remain passive registry entries** — `status` is read, never
  acted upon.
- ✅ **No LLM** in the resolver path (`test_no_llm_invoked` asserts no
  `openai`/`anthropic`/`llm.invoke` references).
- ✅ **Deterministic** — same input → same output (`test_stable_output`,
  `test_idempotent_resolution`).
- ✅ **No hallucinated workers** — UNRESOLVED yields `worker_id = None`.
- ✅ **Append-only** — history and evolution never mutate or delete.

### Full test count
**65 / 65 resolver tests pass.** (Full suite: 881 passing; 2 pre-existing
calendar-observer date-dependent failures unrelated to M9.3 — they touch no
resolver or DB code.)

### Files added
- `src/friday/resolver/__init__.py`
- `src/friday/resolver/models.py`
- `src/friday/resolver/resolver.py`
- `src/friday/resolver/engine.py`
- `src/friday/resolver/confidence.py`
- `src/friday/cli_resolver.py`
- `tests/test_resolver.py`

### Database additions
- `resolver_assignments` (live assignment rows)
- `resolver_history` (append-only run snapshots, surrogate `hid` PK)
- `resolver_evolution` (assignment churn log)
- Migration `_ensure_resolver_history_pk` for existing databases

### Scoring formula
See §4. `total = capability + language + task_type + plan_type + availability
+ confidence − penalty`.

### Assignment examples
See §9 (dogfood) and `tests/test_resolver.py::test_multi_task_resolution`,
`test_custom_worker_resolves`, `test_parallel_vs_single_candidate_count`.

**Execution begins only in Milestone 9.5.** This milestone decides WHO can
execute. It does not execute.
