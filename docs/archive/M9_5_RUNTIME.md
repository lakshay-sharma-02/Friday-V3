# Milestone 9.5 — Execution Runtime

**Status:** COMPLETE. **89 new regression tests green**; full suite **1022 passed**
(the only 2 failures are pre-existing `test_calendar_observer` tests, untouched
by this milestone).

Everything before M9.5 was deterministic reasoning. This milestone introduces
**actual execution**. The Runtime is the ONLY layer that *performs* work.

---

## 1. Architecture

The Runtime consumes a frozen `ExecutionSchedule` (from M9.4) and executes it.
It NEVER plans, schedules, resolves capabilities, reviews, repairs, retries, or
learns. Those are owned by upstream layers (or future milestones that do not
exist yet).

```
Plan
  -> Task Graph (M9.1, frozen)
  -> Capability Resolver (M9.3, frozen)
  -> Scheduler (M9.4, frozen)
  -> Runtime (this milestone)         <-- execution happens here
  -> Execution Results
```

Files added:

| File | Role |
|------|------|
| `src/friday/runtime/models.py` | `Worker` (generic execution interface) + `MockWorker`/`PythonWorker`/`ShellWorker` adapters, `ExecutionResult`, `RuntimeTask`, `RunState`/`SessionState`, `ExecutionReport`. |
| `src/friday/runtime/state.py` | The execution state machine (PENDING→RUNNING→SUCCESS/FAILED/CANCELLED). No retry/repair transitions. |
| `src/friday/runtime/dispatcher.py` | `dispatch(task, worker)` — calls `worker.execute(task)` and returns the `ExecutionResult`. No planning/scheduling/retry/repair. |
| `src/friday/runtime/executor.py` | Wave-by-wave parallel execution; waits for each wave; on failure cancels transitive descendants. |
| `src/friday/runtime/engine.py` | Orchestration + persistence + `ExecutionReport`. |
| `src/friday/runtime/events.py` | Append-only event log (session/task lifecycle). |
| `src/friday/runtime/history.py` | Append-only per-task state snapshots. |
| `src/friday/runtime/__init__.py` | Public exports. |
| `src/friday/cli_runtime.py` | `friday runtime "<goal>"`, `friday runtime_session`, `friday runtime_show <id>`, `friday runtime_export`. |

---

## 2. Worker Contract (the key architectural decision)

The Runtime depends on exactly ONE interface:

```python
class Worker:
    worker_id: str = ""
    def execute(self, task) -> ExecutionResult: ...
```

`ExecutionResult` is opaque to the Runtime:

```python
@dataclass
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: List[str] = []
    exit_code: Optional[int] = None
    duration_ms: int = 0
    error: str = ""
```

The Runtime core **never** branches on which provider it received. There is
deliberately **no** `if provider == "claude":` and **no** `match provider:`.
Concrete backends are plain adapters that implement `Worker.execute(task)`:

- `MockWorker` — in-memory, deterministic (dogfood/tests).
- `PythonWorker` — runs a Python snippet (subprocess).
- `ShellWorker` — runs a shell command (subprocess).
- `ClaudeWorker` / `GeminiWorker` / `CodexWorker` / `FutureWorker` — external
  backends that would implement the same interface. **None are referenced by
  name anywhere in the Runtime core.** The Runtime receives a `Worker` (via a
  `worker_resolver` callable mapping `worker_id -> Worker`) and calls
  `execute()`. New backends slot in without touching the Runtime.

This is the one architectural change made from the original roadmap: keep the
Runtime provider-agnostic so it is unchanged even years from now.

---

## 3. Execution Lifecycle

```
RuntimeEngine.run(schedule)
  |
  |-- open session (runtime_sessions: state=running)
  |-- emit session_started
  |-- seed PENDING rows (runtime_tasks) for every task
  |-- cancel tasks the Scheduler already BLOCKED (no worker) -> CANCELLED
  |
  |-- for each wave (1..N), in order:
  |     |-- mark runnable tasks RUNNING (main thread)
  |     |-- execute workers in PARALLEL (thread pool)   <-- real work here
  |     |-- persist results from the MAIN thread (single DB connection)
  |     |-- on any FAILED task: cancel its transitive descendants
  |
  |-- close session (state=finished)
  |-- emit session_finished
  |-- return ExecutionReport
```

**Threading note:** Worker execution (the actual `worker.execute`) runs
concurrently in a thread pool — that is the parallelism. All database writes are
serialized on the main thread (the executor collects results from the threads,
then persists). This keeps a single sqlite connection correct while still
running tasks in parallel. The DB bookkeeping is trivial; the concurrent part is
the work itself.

---

## 4. State Machine

```
PENDING -> RUNNING -> SUCCESS
                     -> FAILED
                     -> CANCELLED   (ancestor failed; never executed)
```

- `PENDING`: queued.
- `RUNNING`: worker invoked.
- `SUCCESS` / `FAILED`: terminal, from the worker's `success` flag.
- `CANCELLED`: a descendant of a failed task; never executed (blocked chain).

There is **no retry, no repair, no rollback**. A failed task stays FAILED; its
descendants stay CANCELLED. Review (acceptance) and Repair (recovery) are future
concerns that **do not exist** in this milestone.

---

## 5. Database

All tables in `db.py` (migrated on `connect()`), append-only where required:

- **`runtime_sessions`** — one per schedule run (session_id, schedule_id, state,
  started_at, finished_at, schema_version).
- **`runtime_events`** — append-only lifecycle events (session_started,
  task_started, task_finished, task_failed, session_finished).
- **`runtime_tasks`** — latest state per task (keyed by `execution_id`; updated
  in place across re-runs so a task has one current-state row).
- **`runtime_results`** — append-only outcome of each execution (stdout/stderr/
  artifacts/exit_code/duration/error).
- **`runtime_history`** — append-only per-task state snapshots.
- **`runtime_evolution`** — state-change log across sessions (append-only).

Every persisted row carries `schema_version` (Law 24).

---

## 6. Dogfood Transcript

Registered: MockWorker (default dogfood adapter), plus PythonWorker/ShellWorker
for real execution.

- **Independent graph** `A, B, C, D` → wave 1, 4 success.
- **Chain** `A→B→C` → waves 1/2/3, all success, ordered by start time.
- **Diamond** `A→{B,C}→D` → waves 1/2/3, 4 success.
- **Parallel graph** `A, B → C` → wave 1 (A,B), wave 2 (C), 3 success.
- **Mixed** 5-task graph → 5 success.
- **Worker failure** `A fails` → B, C (descendants) CANCELLED, 1 failure.
- **PythonWorker** runs `print('hello from python')` → captured in `runtime_results.stdout`.
- **ShellWorker** runs `echo shell-ran` → captured in stdout.
- **PythonWorker** `sys.exit(3)` → FAILED, exit_code 3, propagates as failure.

Run:

```
friday runtime "<goal>"        # Plan->Graph->Resolve->Schedule->Runtime
friday runtime_session         # list sessions
friday runtime_show <id>       # timeline, task states, workers, duration
friday runtime_export          # JSON of all sessions/results/events
```

Sample (dogfood chain):

```
Runtime session: sess:<hash>
Schedule:        taskgraph:plan:...
Tasks executed:  3
Succeeded:       3
Failed:          0
Cancelled:       0
Duration (ms):   12
Workers used:    worker:mock
```

---

## 7. Regression Coverage

**89 tests** (`tests/test_runtime.py`), all green. Covers:

- State machine (valid/invalid transitions, terminal states)
- Session lifecycle (created/finished, ids, events, persisted row)
- Parallel execution (wave concurrency, true timing, different workers)
- Wave ordering (chain, diamond, wait-for-completion)
- Dependency blocking (failure cancels descendants, unrelated unaffected, no retry)
- Worker failures / exceptions (exception→failure, missing worker→failure, missing cancels dependents)
- Execution events (append-only count, monotonic ordering, task_failed emitted)
- History (append-only, records each state)
- Serialization (result/report/task to_dict, schema version on rows)
- Deterministic replay (same report, same order)
- Mock workers (success/forced-fail/hint-fail/artifacts)
- Multiple workers (routing, unknown→none)
- Worker exceptions (dispatcher conversion, none-worker)
- Cancellation (not executed, persisted)
- Execution report (counts, with-failure, no-analysis fields)
- Runtime restart / resumability (reloadable rows, listable after restart)
- Large graphs (40-chain, 50-wide, deterministic)
- Dogfood: real adapters (PythonWorker/ShellWorker, failure propagation)
- Dispatcher purity (no retry, returns result only)
- Engine boundaries (requires schedule input, does not mutate schedule)
- Schema version on every row
- Session id format, multiple distinct sessions
- Worker-conflict serialization (upstream-owned)
- Append-only evolution
- Cancellation reason recorded
- Independent/chain/diamond/parallel/mixed dogfood graphs
- Worker-failure blocks descendants
- BLOCKED-at-schedule-time (Runtime respects Scheduler, no reassignment)
- Concurrent sessions (no DB corruption)
- Export / round-trip DB helpers
- Empty schedule
- BLOCKED task never executed even if a worker exists
- Re-run creates a new session (not a duplicate)

---

## 8. Known Limitations

- **No retry / repair / review.** A failed task stays failed; descendants stay
  cancelled. Recovery is explicitly out of scope (future milestone).
- **Single DB connection per RuntimeEngine instance.** Concurrent *sessions*
  should each use their own connection (realistic multi-process case); the
  engine serializes its own writes but does not multiplex one connection across
  threads.
- **Worker adapters for external providers** (ClaudeWorker/GeminiWorker/
  CodexWorker) are specified by the contract but not implemented here — the
  Runtime is generic and would accept them without modification. The CLI and
  dogfood use `MockWorker` (and `PythonWorker`/`ShellWorker`) as stand-ins.
- **Artifacts** are recorded as a list of references/paths supplied by the
  worker; the Runtime does not fetch or verify them.
- **No execution-side analysis or learning.** The `ExecutionReport` is
  outcomes-only (counts, states, workers, artifacts). No recommendations.

---

## 9. Explicit Confirmation

- The Runtime performs **execution ONLY**. It contains no planning, scheduling,
  capability resolution, review, repair, retry, or learning.
- **Review remains absent** (no acceptance/verification of outcomes).
- **Repair Loop remains absent** (no recovery from failure).
- **Learning from execution remains absent** (no knowledge updates, no model
  feedback).
- Everything the Runtime needed (graph, assignments, ordering, worker mapping)
  already existed upstream; the Runtime simply executes the schedule it is
  given, in wave order, respecting dependencies and worker assignments.
