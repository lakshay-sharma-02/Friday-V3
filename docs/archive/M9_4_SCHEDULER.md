# Milestone 9.4 — Task Scheduler

**Status:** COMPLETE (code + 52 regression tests green).
**Execution is still FORBIDDEN.** The Scheduler computes *when* work becomes
runnable. It never executes, never invokes workers, never calls an LLM, never
touches repositories or files, never runs shell commands. Execution begins only
in Milestone 9.5 (Runtime).

---

## 1. Architecture

The Scheduler is the **only** layer permitted to compute execution *ordering*
from a validated Task Graph + Capability Assignments. It consumes frozen outputs
and produces a first-class `ExecutionSchedule` object — the sole input the future
Runtime will consume. The Runtime never recalculates dependencies, waves, or
ordering.

```
Plan
  -> Task Graph (frozen, M9.1)
  -> Capability Resolver (frozen, M9.3)   [assigns workers]
  -> Scheduler (this milestone)           [orders execution]
  -> ExecutionSchedule  ----------------------->  (Runtime, M9.5)
```

Files added:

| File | Role |
|------|------|
| `src/friday/scheduler/models.py` | `ExecutionSchedule`, `ScheduledTask`, `TaskState`. Pure data + (de)serialization. Carries `SCHEMA_VERSION`. |
| `src/friday/scheduler/scheduler.py` | Pure scheduling math: topological sort, waves, root depths, dependency counts, critical path, priority, worker-conflict serialization. No I/O. |
| `src/friday/scheduler/engine.py` | Orchestration + persistence. Reads Task Graph + Assignments + Worker Registry (read-only), writes schedule + append-only history + evolution. Idempotent. |
| `src/friday/scheduler/state.py` | Derives each task's **initial** runnable state. The Scheduler only creates the initial state; the Runtime advances it. |
| `src/friday/scheduler/timeline.py` | Builds the ordered execution timeline / wave summary / critical-path status from a schedule. Pure. |
| `src/friday/scheduler/__init__.py` | Public exports. |
| `src/friday/cli_scheduler.py` | `friday schedule "<goal>"`, `friday scheduler [list|explain|export]`. |

The Scheduler loads the Task Graph **raw** (tasks + edges directly from the DB),
NOT through the frozen `TaskGraphEngine.graph_by_id`, because that path re-runs
graph-contract validation and would reject an intentionally invalid graph
(cycle / dangling edge) before the Scheduler's own rejection rules can report it.
The Scheduler owns scheduling validation; it does not re-validate the frozen
contract.

---

## 2. Scheduling Algorithm

Deterministic. Same graph → same schedule, every run. No randomness, no time
dependence, no LLM, no I/O inside the math functions.

1. **Load** tasks + edges raw from the DB.
2. **Cycle detection** (`detect_cycle`) — DFS with a fixed sorted adjacency;
   returns a stable cycle slice. Cycle ⇒ `CycleDetectedError` (reject).
3. **Dangling-edge check** — every edge endpoint must be a known task. Dangling
   ⇒ `InvalidGraphError` (reject).
4. **Assignment check** — every task must have a capability assignment with
   `status == assigned`. Missing ⇒ `MissingAssignmentError` (reject). The
   Resolver owns assignment; the Scheduler never reassigns.
5. **Waves** (`compute_waves` + `_root_depths`): wave = longest forward
   distance from a root + 1. Independent roots → wave 1. Each dependency hop →
   later wave.
6. **Dependency count** (`compute_dependency_count`): *transitive* ancestor
   count per task. A 3-chain A→B→C gives C count 2 (A and B), not 1.
7. **Critical path** (`compute_critical_path`): longest path by node count.
   Empty when there are no edges (so a single independent task is **not** on the
   critical path — this keeps priority ordering correct).
8. **Priority** (`compute_priority`):
   `critical_path_bonus(1000) + dependency_depth_bonus(100/wave) + explicit_band`
   where `explicit_band ∈ {critical:40, high:30, medium:20, low:10}`.
9. **Initial state** (`compute_initial_state`): BLOCKED (no assignment / disabled
   worker) → NOT_READY (has predecessors) → READY (no predecessors, assigned,
   active worker).
10. **Worker-conflict serialization** (`serialize_worker_conflicts`): tasks
    sharing a worker inside one wave get a deterministic sub-order by task id;
    `estimated_start`/`estimated_finish` carry the sequence so the Runtime runs
    them sequentially, never in parallel.
11. **Persist** schedule + append-only history snapshot.

### Global order
`order_tasks` sorts by `(wave asc, priority desc, task_id asc)` — the canonical
execution order the Runtime walks.

---

## 3. State Model

`TaskState` (in `models.py`):

| State | Set by | Meaning |
|-------|--------|---------|
| `NOT_READY` | Scheduler (initial) | predecessors incomplete |
| `READY` | Scheduler (initial) | predecessors done, worker present + active |
| `BLOCKED` | Scheduler (initial) | missing assignment, disabled worker, or cycle |
| `SCHEDULED` | **Runtime (M9.5)** | placed on the timeline |
| `COMPLETE` | **Runtime (M9.5)** | done |
| `FAILED` | **Runtime (M9.5)** | failed |
| `CANCELLED` | **Runtime (M9.5)** | cancelled |

The Scheduler only ever creates `NOT_READY` / `READY` / `BLOCKED`. It never sets
a post-initial state. A blocked task is **never auto-reassigned** — the Resolver
owns assignment.

---

## 4. Wave Examples

Chain `A → B → C`:

```
Wave 1: A
Wave 2: B
Wave 3: C
```

Diamond `A → {B,C} → D`:

```
Wave 1: A
Wave 2: B, C
Wave 3: D
```

Independent `A, B, C`:

```
Wave 1: A, B, C     (all runnable in parallel; worker conflicts serialized)
```

---

## 5. Database

Tables (schema in `db.py`, migrated on `connect()`):

- **`scheduler_runs`** — one run-level record per scheduling run (graph,
  wave_count, task_count, critical_path_length, max_parallelism, status).
- **`scheduler_tasks`** — one row per scheduled task: `schedule_id`,
  `graph_id`, `assignment_id`, `task_id`, `worker_id`, `phase`, `status`,
  `priority`, `wave`, `dependency_count`, `estimated_start`, `estimated_finish`,
  `blocked_reason`, `confidence`, `selection_strategy`, `schema_version`,
  `created_at`, `updated_at`.
- **`scheduler_history`** — append-only snapshot of every scheduling run (never
  updated, only inserted).
- **`scheduler_evolution`** — decision changes across runs (append-only).

Every scheduled row carries `schema_version` (Law 24: contract versioning).

---

## 6. Dogfood Transcript

Registered workers: Claude, Codex, Gemini (+ builtins Python/Shell/Git/…).
Verified on:

- **Simple chain** `A→B→C` → waves 1,2,3; C deepest, highest priority.
- **Parallel graph** `A, B, C` independent → wave 1, max_parallelism = 3.
- **Diamond graph** `A→{B,C}→D` → waves 1 / 2(B,C) / 3.
- **Independent graph** → single wave, worker conflicts serialized by task id.
- **Mixed priority** → critical-path + dependency-depth drive ordering; ties
  break on task id.

Run:

```
friday schedule "Implement OAuth login"
friday scheduler                 # list runs
friday scheduler explain <id>   # waves, deps, worker, priority, CP, blocked
friday scheduler export         # JSON
```

Sample (chain):

```
Schedule: g1
Goal:            Implement OAuth login
Tasks:           3
Waves:           3
Critical path:   3
Max parallelism: 1
Blocked:         0
Run:            run:g1:2026-...

  Wave 1 [1]: A
  Wave 2 [1]: B
  Wave 3 [1]: C
```

---

## 7. Regression Coverage

**52 tests** (`tests/test_scheduler.py`), all green. Covers:

- Topological ordering (chain, edge-respecting)
- Cycle detection (reject) + reported cycle path + pure unit
- Diamond graph waves, parallel mid-wave
- Independent graph (single wave, full parallelism)
- Parallel waves
- Worker-conflict serialization (same worker sub-order + task-id tie-break)
- Critical path recorded + priority bonus
- Dependency depth priority + transitive `dependency_count`
- Priority ordering + explicit band + task-id tie-break
- Blocked tasks: missing assignment (reject), disabled worker (BLOCKED, no
  reassignment), NOT_READY with predecessors
- Missing assignments reported
- Stable / deterministic output + idempotent re-schedule
- Append-only history (grows on re-schedule)
- Schema version (`1.0`) on every row
- Round-trip serialization (`to_row` / `to_dict`)
- Export JSON
- Single task (wave 1, READY), empty graph (0 tasks/waves), unknown graph
  (reject), dangling edge (reject)
- Large graph (50-chain deterministic, 40-wide parallel)
- Timeline order + critical-path status
- Scheduler creates only initial states + no execution side effects
- Dogfood: simple chain, parallel, diamond, independent, mixed priority

---

## 8. Complexity Analysis

Let `V` = tasks, `E` = edges.

| Step | Complexity |
|------|------------|
| Cycle detection (DFS) | O(V + E) |
| Dangling-edge check | O(E) |
| Assignment check | O(V) |
| Root depths / waves | O(V + E) (memoized DFS) |
| Dependency count | O(V + E) (memoized DFS) |
| Critical path | O(V + E) (memoized longest path) |
| Priority | O(V) |
| Worker-conflict serialization | O(V log V) per wave |
| Global order (`order_tasks`) | O(V log V) |
| Persist (history snapshot) | O(V) |

Overall: **O(V + E)** for the math, **O(V log V)** for sorting. Linear in graph
size — no backtracking, no heuristics, no randomness.

---

## 9. Explicit Execution-Impossibility Confirmation

- The Runtime (M9.5) **does not exist** in this codebase.
- The Review layer **does not exist**.
- The Repair Loop **does not exist**.
- The Scheduler **never** executes, invokes workers, calls LLMs, modifies
  repositories, runs shell, or writes files outside its own schedule tables.
- Execution begins **only** in Milestone 9.5.

---

## 10. Architectural Recommendation (carried from milestone brief)

The Scheduler emits a first-class `ExecutionSchedule` object (waves, dependency
metadata, runnable state, critical path, worker utilization). That object — not
raw task IDs — is the **sole input** to the M9.5 Runtime. The Runtime therefore
never needs to recalculate dependencies or execution order, keeping the
Scheduler as the single authority on scheduling decisions. This is implemented
as `ExecutionSchedule` in `models.py` and consumed verbatim by
`timeline.build_timeline` / `wave_summary`.
