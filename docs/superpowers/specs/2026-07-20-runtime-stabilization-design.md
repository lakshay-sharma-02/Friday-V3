# Runtime Stabilization — Design

**Status**: Draft for review. Acceptance criteria: interfaces and contracts
specified and reviewed; design satisfies the milestone it was compiled from.

Frozen architecture. This milestone HARDENS the existing execution pipeline; it
does not add a new subsystem, engine, planner, or database. Every new module is
a pure, deterministic projection or a faithful read-out of already-persisted
rows. The single behavioral change: a task that exits 0 but produced no expected
artifact is reported FAILED (truthful verification), and its transitive
descendants are cancelled.

The milestone closes a series of execution-pipeline correctness bugs:

| Phase | Bug | Fix location |
|-------|-----|--------------|
| 1 | Scheduler dependency direction reversed | `scheduler/engine.py` (wave ordering) |
| 2 | Executor capability model routed everything to Claude | `resolver/` capability ranking |
| 3 | No robust executor fallback — one external failure aborted mission | `runtime/executors.py` `fallback_chain`/`execute_with_fallback` |
| 4 | Fresh DB did not auto-bootstrap built-in executors | `worker/engine.py` `ensure_runtime_bootstrapped` |
| 1.5 | No execution contract — success guessed from prose | `runtime/contract.py` + verify step in dispatch/reconcile |
| 3 (verify) | Exit 0 ≠ mission success | `runtime/verification.py` + `engine._reconcile_verification` |
| 3 (plan) | Whole mission was one AI blob | `planning/patterns.py` symbolic planner + `compiler` override |
| 4 (exec) | Planner emitted no file paths | `runtime/symbolic.py` symbolic→payload + verify |
| 4 (journal) | No faithful record of what happened | `runtime/journal.py` |

This design documents the interfaces and contracts. Implementation already
exists in the working tree; the spec is authored to match and ratify it.

---

## 1. Execution contract — `runtime/contract.py` (Phase 1.5)

The planner already populates `outputs` (expected artifacts), `verification`
(steps), and `acceptance_criteria` (success conditions) on each Task. The
runtime verifies the contract against observed reality instead of guessing from
the natural-language goal.

`TaskContract` is pure metadata. It changes nothing about how an executor runs;
it only tells verification what must be true afterward.

```python
@dataclass
class TaskContract:
    expected_artifacts: List[str] = field(default_factory=list)
    verification_steps: List[dict] = field(default_factory=list)
    success_conditions: List[str] = field(default_factory=list)

    def to_dict(self) -> dict: ...

def contract_for_task(task, create_type: Optional[str] = None) -> TaskContract:
    """Project a Task/RuntimeTask into its contract.

    expected_artifacts derived deterministically, in priority order:
      1. Structured `outputs` entries that are explicit file paths (authoritative
         contract stamped by the planner).
      2. Fallback: file-path tokens named in title/description/goal prose, so a
         legacy planner that did not stamp `outputs` still yields a checkable
         contract.
    No LLM, no network, no guessing beyond explicit tokens.
    """

def resolve_artifact_paths(contract: TaskContract, workspace: str = "."
                            ) -> List[str]:
    """Resolve the contract's expected artifacts to absolute workspace paths."""
```

Contract derivation rules (deterministic, order matters):
- `_looks_like_path(s)` = has a file extension AND (is absolute, starts with
  `./`/`../`, or contains `~`). Bare words like "logging" are never artifacts.
- `_scan_paths(text)` extracts path tokens from JSON payloads
  (`{"path": ...}`) and from `name.ext` / path-ish prose, de-duplicated.
- If the structured `outputs` contract is non-empty it wins; prose scanning is
  strictly a fallback.

`RuntimeTask` carries the contract fields (`outputs`, `acceptance_criteria`,
`verification`) copied from the planning Task by the engine, so `contract_for_task`
accepts both planning Tasks and runtime tasks uniformly.

---

## 2. Truthful verification — `runtime/verification.py` (Phase 3)

Exit 0 does NOT mean success. A creation-type task that names a file but leaves
nothing on disk is FAILED truthfully — the mission never reports success with no
file present.

```python
def expected_paths(task, workspace: str = ".") -> List[str]:
    """Resolve the file paths a task is expected to produce.

    Priority: explicit contract (Phase 1.5) -> resolve_artifact_paths(); only
    when empty, fall back to scanning goal/title/payload/acceptance prose.
    Empty when the task references no file (nothing to verify -> trust executor).
    """

def verify_task_artifacts(task, result, workspace: str = "."
                           ) -> VerificationResult:
    """Evidence check.

    - No file referenced           -> PASS ("no expected artifact").
    - File exists OR worker reported an artifact -> PASS.
    - File referenced BUT missing & no artifact -> FAIL, reason names the
      missing path(s).
    """

def verify_creation_task(task, result, workspace: str = "."
                          ) -> VerificationResult:
    """Strict check for creation-type tasks (implementation/documentation/
    testing/configuration/cleanup/migration/infrastructure/deployment).

    Creation task with an explicit referenced file that is missing = hard FAIL.
    Creation task that names NO file = PASS (planner may not have named it;
    cannot evidence-check, so trust the executor).
    """
```

Rules (the core guard):
- We NEVER fail a task we cannot evidence-check (no referenced file → PASS).
- We DO fail a task that claims success but produced no expected file.
- `_CREATION_TASK_TYPES` is a frozen set; a non-creation task falls through to
  `verify_task_artifacts`.

`VerificationResult` (defined in `runtime/models.py`) is the single verdict type:
`VerificationResult(passed: bool, reason: str)`. The dispatcher and the engine
both stamp `result.verification_passed` from it.

---

## 3. Symbolic planner — `planning/patterns.py` (Phase 3, plan layer)

Deterministic decomposition of software-engineering missions into typed
*symbolic* tasks. Pure: input is `(goal, plan)` only — never reads a repo, never
calls a worker, never uses an LLM. This is the "WHAT" layer.

Each emitted task is SYMBOLIC — it names the engineering operation and the
symbol/module it targets, never a concrete file path. The Resolver later enriches
symbolic tasks with repository-specific info before selecting an executor.
Planner = intent, Resolver = repo knowledge, Executor = work.

```python
OP_LOCATE = "locate_symbol"
OP_RENAME_DECL = "rename_declaration"
OP_FIND_REFS = "find_references"
OP_RENAME_IMPORTS = "rename_imports"
OP_UPDATE_REFS = "update_references"
OP_IDENTIFY_BOUNDARY = "identify_boundary"
OP_CREATE_MODULE = "create_module"
OP_MOVE_CODE = "move_code"
OP_REMOVE_DUPES = "remove_duplicates"
OP_LOCATE_TARGET = "locate_target"
OP_IDENTIFY_POINTS = "identify_insertion_points"
OP_MODIFY = "modify_implementation"
OP_REPRODUCE = "reproduce_failure"
OP_IDENTIFY_COMPONENT = "identify_component"
OP_IDENTIFY_UNUSED = "identify_unused"
OP_VERIFY_REFS = "verify_references"
OP_REMOVE_SAFE = "remove_safely"
OP_FORMAT = "run_formatter"
OP_TEST = "run_tests"
OP_REGRESSION = "run_regression_tests"
OP_VERIFY = "verify_fix"
OP_REVIEW = "review_changes"

@dataclass
class SymbolicTask:
    op: str
    task_type: str               # drives Resolver -> executor selection
    title: str
    symbolic: dict = field(default_factory=dict)        # op + target (no paths)
    verification: List[dict] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    parallel_next: bool = False  # run in parallel with the next task if True

@dataclass
class PatternPlan:
    name: str
    intent: str
    tasks: List[SymbolicTask]

def classify(goal: str, plan: Optional[Plan] = None) -> Optional[PatternPlan]:
    """Return a PatternPlan for a recognized engineering goal, else None.

    Pure + deterministic. Order matters (specific patterns first):
    rename -> extract -> refactor -> fix -> remove/delete/cleanup -> add ... to/into/for.
    Unrecognized goals return None; the compiler falls through to frozen generic
    milestone expansion.
    """
```

Contract between planner and resolver: the `op` token + `symbolic` dict. The
Resolver consumes `symbolic` to resolve concrete files; it never re-parses the
goal.

Task-type → verification-method map (frozen):
`analysis→static_analysis`, `refactor/implementation/cleanup→build`,
`configuration→format`, `testing/verification→tests`, `review→review`. This is
kept in sync with `compiler._cap_for_symbolic` (both define the same
type→capability bias: analysis/refactor/implementation/cleanup→python,
configuration→configuration, testing/verification→testing, review→research).

Each recognized pattern emits an explicit ordered workflow (locate → refs →
edit → format → test → review). Dependency ordering is a valid chain (no cycles).
Every step carries specific non-empty `acceptance_criteria` + `verification`
except the final `review` step (AI-primary). Recognized patterns OVERRIDE the
generic graph; unrecognized goals fall through to frozen behaviour — no new
branching in the compiler beyond "call `classify`, use result if not None".

---

## 4. Symbolic → payload — `runtime/symbolic.py` (Phase 4, exec layer)

The planner emits symbolic tasks with no file paths. This module is the
executor-side half: given a task's `symbolic` op and the concrete workspace, it
locates affected files (read-only grep) and builds the exact payload the
assigned executor understands. It never mutates the repo — the executor does.

```python
def build_payload(task: RuntimeTask, workspace: str = "."
                  ) -> str:
    """Translate a task's symbolic intent into a concrete executor payload.

    Returns existing runtime_payload unchanged for non-symbolic tasks. For
    symbolic tasks, greps the workspace and emits the payload the ASSIGNED worker
    understands: files+symbol -> FileExecutor JSON (replace_all/delete_symbol),
    python worker -> python snippet, shell/git -> shell command. If the workspace
    yields nothing, returns a safe no-op payload (the executor runs it;
    verification then fails on evidence if a file was expected).
    """

def verify_symbolic(task: RuntimeTask, result: ExecutionResult,
                    workspace: str = ".") -> Optional[VerificationResult]:
    """Evidence verification for rename/refactor symbolic tasks.

    Rename: OLD symbol count == 0 across workspace AND NEW symbol count >= 1.
    Returns None for ops we cannot evidence-check (caller falls back to artifact
    checks in verification.py).
    """
```

Op routing (deterministic):
- `rename_declaration` / `rename_imports` / `update_references` → grep symbol,
  emit `replace_all` payload; `verify_symbolic` proves old count 0 / new present.
- `create_module` / `move_code` / `update_imports` → `_create_module_payload`
  (ensure target module exists so later edits land).
- `remove_safely` → grep symbol, emit `delete_symbol`.
- `run_formatter` → `ruff format . || black . || true`.
- `run_tests` / `run_regression_tests` / `verify_fix` → `{"pytest": ["-q"]}`.

Worker-specific payload shaping: `_is_shell(worker_id)` / `_is_python(worker_id)`
emit shell / python source respectively; default (filesystem/git) emits
FileExecutor JSON. So a rename lands as a FileExecutor `replace_all` when the
built-in filesystem worker is assigned, or as an `sed` pass when a shell worker
is — the same symbolic op, one deterministic translation.

`build_payload` is called by the engine immediately before dispatch (Phase 4),
rewriting `rt.runtime_payload` from the symbolic op. Non-symbolic tasks are
untouched.

---

## 5. Mission journal — `runtime/journal.py` (Phase 4, read-out)

After a mission runs, the runtime has all evidence in the DB (session, tasks,
results, history). This module assembles it into one structured journal — a
faithful read-out of what actually happened. No analysis, no LLM.

```python
def build_journal(session_id: str, conn, report: ExecutionReport,
                  goal: str = "", graph_id: str = "",
                  planner_time_ms: int = 0) -> dict:
    """Assemble a structured mission journal from persisted execution rows.

    Reads runtime_tasks + runtime_results. Per-task entry: wave, status,
    attempts, duration, exit_code, error, verification_passed, artifacts,
    evidence (extracted by _evidence_for). Top-level summary: completed flag
    (no failed tasks AND SessionState.FINISHED AND report.failed == 0),
    tasks_total, succeeded, failed, cancelled, retried, verification_failures,
    workers_used. Failures list names task/worker/error/evidence.
    """

def write_journal(journal: dict, path: str) -> str:
    """Write the journal as JSON; return the path written."""

def collect_metrics(journal: dict) -> dict:
    """Derive execution-quality metrics from the journal (single source).

    planner_time_ms, execution_time_ms, retry_count, executor_failures,
    verification_failures, missions_completed/failed, tasks_total/succeeded/
    cancelled. Derived from the same persisted rows as the journal so counters
    cannot drift.
    """

def format_metrics(metrics: dict) -> str:
    """Human-readable one-block metrics summary."""
```

`_evidence_for(task_row, res)` extracts: artifacts if present; else
verification failure (stderr); else the executor error/stderr; else exit_code.
The journal's `schema_version` is "1.0" — bump only on a breaking shape change.

---

## 6. Verification wiring — dispatch + reconciliation

Two enforcement points, both calling the worker-owned `verify` AND the
contract-based evidence check:

1. **Dispatch** (`runtime/dispatcher.py`): `dispatch(task, worker)` calls
   `worker.verify(task, result)` after `execute()`, stamps
   `result.verification_passed`, and records the verdict in `result.metadata`
   (`verified`, `verify_reason`). No retry, no repair, no provider branch. A
   raising `verify` degrades to a failed `VerificationResult` and never breaks
   execution reporting.

2. **Reconciliation** (`runtime/engine.py::_reconcile_verification`): after the
   wave executes, every task left in `SUCCESS` is re-checked against evidence:
   - `verify_symbolic(t, result, ws)` first (rename/refactor proof);
   - else `verify_creation_task(t, result, ws)` (artifact evidence);
   - a failing verdict flips the task to `FAILED` with a reason naming the
     missing artifact, re-stamps `verification_passed` on the persisted result,
     and cancels all transitive `PENDING` descendants.
   - Tasks already `FAILED`/`CANCELLED`/`PENDING` are untouched.

This is the single guard that prevents "Mission Complete" with no file on disk.
It runs against real persisted rows, so the contract (Phase 1.5) and the truthful
verification (Phase 3) are the same code path in both live dispatch and the
post-hoc journal.

`runtime/executors.py` provides the fallback safety net: `fallback_chain`
(one deterministic built-in → itself; an AI id → itself + other AI ids +
deterministic built-ins) and `execute_with_fallback` (first success wins; a
crash/hang/non-zero exit is a skip, never an abort; only all-candidates-failing
returns overall failure). AI executors are bounded to `FRIDAY_AI_TIMEOUT` so a
headless hang cannot stall the mission. The fallback path also runs the
executor's own `verify` so `verification_passed` is populated there too.

---

## 7. Data flow (end-to-end mission)

```
goal
  -> planner.classify(goal)        # patterns.py; None -> generic expansion
  -> compiler.build_graph(plan)     # symbolic tasks carry op + target, no paths
  -> resolver.select_assignment     # deterministic; symbolic task_type biases
  -> scheduler.schedule             # waves in dependency order (Phase 1 fix)
  -> engine._prepare                # build_payload(rt, ws) from symbolic op
  -> dispatch(task, worker)
        worker.execute(task) -> ExecutionResult
        worker.verify(task, result) -> VerificationResult  (verification_passed)
  -> engine._reconcile_verification # evidence flip SUCCESS->FAILED + cancel kids
  -> build_journal(session, conn, report)  # faithful read-out
  -> collect_metrics(journal) / format_metrics
```

The real repository is never touched by the planner or symbolic module; the
symbolic layer only greps to locate files and translate to a payload the
resolved executor then writes under `workspace`.

---

## 8. Interfaces / contracts summary

| Module | Public contract | Pure? | I/O |
|--------|----------------|-------|-----|
| `contract.py` | `contract_for_task`, `resolve_artifact_paths`, `TaskContract` | yes | none (parsing only) |
| `verification.py` | `expected_paths`, `verify_task_artifacts`, `verify_creation_task` | yes | path `.exists()` stats |
| `patterns.py` | `classify`, `SymbolicTask`, `PatternPlan`, `OP_*` | yes | none |
| `symbolic.py` | `build_payload`, `verify_symbolic` | read-only grep | grep subprocess |
| `journal.py` | `build_journal`, `write_journal`, `collect_metrics`, `format_metrics` | yes | DB read + file write |
| `dispatcher.py` | `dispatch(task, worker) -> ExecutionResult` | — | executor call + verify |
| `executors.py` | `fallback_chain`, `execute_with_fallback`, `resolve_executor` | — | subprocess |

Shared types (single source, `runtime/models.py`): `ExecutionResult`,
`VerificationResult(passed, reason)`, `RuntimeTask` (carries `outputs`,
`acceptance_criteria`, `verification`, `symbolic`), `RunState`, `SessionState`,
`ExecutionReport`. No new persisted table; `runtime_results.verification_passed`
column already exists and carries the verdict.

---

## 9. Tests (regression, all present in working tree)

- `test_contracts.py` — real planner→resolver→scheduler→runtime path; contract
  drives verification; happy path genuinely materializes the expected file.
- `test_patterns.py` — each recognized pattern emits the explicit workflow;
  dependency chain has no cycle; every step routes to a deterministic executor
  except final `review`; every task has specific acceptance + verification.
- `test_runtime_stabilization.py` — the four Phase 1–4 bugs fixed (scheduler
  direction, capability routing, fallback, fresh-DB bootstrap) plus Phase 6
  scenarios (linear + DAG order, fresh-DB, Claude-unavailable, deterministic-only
  never invokes Claude, mixed selects Claude for research only).
- `test_execution_dogfood.py` — six spec missions end-to-end on a tiny temp repo;
  asserts the repo ends in the expected state AND a complete journal + metrics
  were produced.

Full suite must remain green. No `cli/` import in `runtime/`.

---

## 10. Files touched (summary)

New:
- `src/friday/runtime/contract.py` — `TaskContract`, `contract_for_task`,
  `resolve_artifact_paths`.
- `src/friday/runtime/verification.py` — `expected_paths`,
  `verify_task_artifacts`, `verify_creation_task`.
- `src/friday/runtime/symbolic.py` — `build_payload`, `verify_symbolic`.
- `src/friday/runtime/journal.py` — `build_journal`, `write_journal`,
  `collect_metrics`, `format_metrics`.
- `src/friday/planning/patterns.py` — `classify`, `SymbolicTask`, `PatternPlan`,
  `OP_*` op tokens.
- Tests: `test_contracts.py`, `test_patterns.py`, `test_runtime_stabilization.py`,
  `test_execution_dogfood.py`.

Modified (contract wiring, no new subsystem):
- `src/friday/runtime/models.py` — `RuntimeTask` carries contract + symbolic
  fields; `ExecutionResult.verification_passed`; `VerificationResult`.
- `src/friday/runtime/dispatcher.py` — verify step + `verification_passed` stamp.
- `src/friday/runtime/engine.py` — `build_payload` at prepare;
  `_reconcile_verification` evidence flip + descendant cancel; journal surface.
- `src/friday/runtime/executors.py` — `fallback_chain`, `execute_with_fallback`,
  `resolve_executor`, AI executor timeout bound.
- `src/friday/planning/compiler.py` — `classify()` hook before generic expansion;
  `SymbolicTask` emit path; `_cap_for_symbolic` bias.

No new DB tables. No new engines/managers/pipelines. Architecture frozen.
