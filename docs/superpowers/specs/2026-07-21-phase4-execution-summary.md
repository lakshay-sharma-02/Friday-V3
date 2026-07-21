# Phase 4 — End-to-End Engineering Execution (Summary)

Architecture was frozen. This phase improves **execution experience only**:
making FRIDAY complete real engineering missions from a natural-language goal
to a verified repository, with evidence-based verification, deterministic
failure recovery, a mission journal, and metrics.

No planners / compilers / resolvers / runtime cores were redesigned. No new
engines, memory, or autonomous behaviour were added.

---

## 1. Files modified

| File | Change | Why |
|------|--------|-----|
| `src/friday/runtime/symbolic.py` | `_grep` refuses empty pattern; `_remove_payload` routes cleanup to a safe op; python branch uses `ast` for whole-node dead-code removal; empty `remove_safely` symbol → safe no-op | Prevent repo-wide wipe; correct dead-code removal |
| `src/friday/runtime/executors.py` | `delete_symbol` guards non-empty symbol; whole-statement-block removal | Safety + correct removal |
| `src/friday/runtime/executor.py` | `review` added to `_NON_BLOCKING_TYPES` | AI review failure must not abort the engineering change |
| `src/friday/planning/patterns.py` | maintenance classifier extracts a concrete symbol from the goal; threads it into `remove_safely` | Enables real (not blanket) dead-code removal |
| `src/friday/cli_execute.py` | journal built on the *live* `conn` (not a fresh default-path connection); `conn.close()` moved after journaling | Journal was empty (`tasks_total: 0`) because rows were read from the wrong/closed DB |
| `tests/test_execution_dogfood.py` | assert real removal + file parses; assert new module created; add "never wipes repo" safety test | Align tests with truthful execution |

---

## 2. End-to-end execution flow (`friday execute "<goal>"`)

```
goal
  → TaskGraphEngine.generate          # deterministic plan + compiled graph
  → CapabilityResolver.resolve_graph  # repo-aware worker assignment
  → TaskScheduler.schedule_graph     # waves + dependency ordering
  → RuntimeEngine.run                # wave-by-wave execution + retries
      → build_payload (symbolic→executor payload, read-only grep)
      → dispatch / execute_with_fallback
      → verify_symbolic / verify_creation_task (evidence, not status)
      → cancel transitive descendants on blocking failure
  → build_journal + collect_metrics  # structured journal + metrics block
  → _render_report (Mission Control / plain text)
```

One command. Everything else automatic. Return code 0 = success, 1 = failure.

---

## 3. Mission journal format (`mission_journal_<session>.json`)

Produced for **every** execution (success or failure):

```
schema_version, generated_at, session_id, graph_id, mission
planner_time_ms, execution_time_ms, verification_time_ms
summary:  completed, tasks_total, succeeded, failed, cancelled,
          retried, verification_failures, workers_used,
          stopped_at, stop_reason
executor_assignments: [{task_id, worker_id}]
graph:    {nodes, edges}
tasks:    [{task_id, worker_id, wave, status, attempts,
            duration_ms, exit_code, error, verification_passed,
            artifacts, evidence}]
failures: [{task_id, worker_id, error, evidence}]
```

Truthful read-out only — no analysis, no LLM. A failed mission explains exactly
where and why execution stopped (`stopped_at` / `stop_reason`).

---

## 4. Failure recovery behaviour

- **Retries (transient only):** timeouts / rate-limits / dropped connections
  retried up to `MAX_ATTEMPTS=3`. Deterministic logic failures (linter exit 1
  on bad code) are *not* retried.
- **Non-blocking failures:** `configuration`, `cleanup`, `review` failures are
  recorded truthfully but do **not** cancel the dependency chain. An
  unavailable/refusing AI reviewer never aborts the actual code change.
- **Blocking failures:** any other task failure cancels all transitive
  descendants and stops the mission there; `stopped_at` / `stop_reason` set.
- **AI fallback chain:** a failed/hung AI executor falls through other AI
  executors → deterministic built-ins. Only if *all* candidates fail is overall
  failure returned.
- **Never hides failures:** verification re-derives task state from evidence
  (symbol counts, test summary, git diff). An executor exiting 0 with no
  artifact is flipped to FAILED. No "Mission Complete" with no file on disk.

---

## 5. Integration tests (`tests/test_execution_dogfood.py`)

Opt-in (`-m live_pipeline` / `FRIDAY_RUN_LIVE_TESTS=1`) so a plain `pytest`
finishes fast. Each seeds a TINY temporary repo and runs the FULL pipeline
against it — the real FRIDAY repo is never touched.

Six spec missions:
1. `Rename RuntimeTask to MissionTask` — old symbol count 0, new present.
2. `Add structured logging to RuntimeEngine` — feature graph to completion.
3. `Extract scheduler utilities into a new module` — new module file created.
4. `Remove dead code` — `DEAD_FN` gone, live code intact, file still parses.
5. `Add retry support to Claude executor` — feature graph to completion.
6. `Fix failing scheduler tests` — bugfix graph; **truthfully NOT completed**
   (seeded test still fails; regression/verify/review cancelled).

Plus:
- `test_journal_records_failures_truthfully` — failure journal is faithful.
- `test_remove_dead_code_without_symbol_never_wipes_repo` — blank removal is
  refused (regression for the catastrophic wipe bug).

Result: **8 passed.**

---

## 6. Metrics collected

Reported after every mission (`format_metrics`) and averaged across the
workspace's prior journals (`_print_average_metrics`):

```
planner_time, execution_time, verification_time, retry_count,
executor_failures, verification_failures, missions_completed,
missions_failed, tasks (succeeded/total, cancelled)
```

Metrics are derived from the same persisted journal rows — no separate
counters that can drift.

---

## 7. Demonstrated missions (evidence)

All six executed successfully against seeded repos (per-mission runtime ≈13s
with the `claude` binary present for the review step; review is non-blocking
so an absent/refusing reviewer does not abort the change):

| Mission | Result |
|---------|--------|
| Rename RuntimeTask → MissionTask | 8/8 tasks, old=0 new>0 |
| Add logging to RuntimeEngine | 6/6 completed |
| Extract scheduler utils → module | new module file created |
| Remove dead code (`DEAD_FN`) | symbol removed, file parses, live code kept |
| Add retry to Claude executor | 6/6 completed |
| Fix failing scheduler tests | truthfully NOT completed (test still fails) |

The `friday execute` command produces a complete journal + metrics and a
correct return code for each.

---

## Critical bug fixed this phase

**"Remove dead code" wiped every file in the repository.** With no concrete
symbol, `build_payload` called `_grep(workspace, '')`; an empty grep pattern
matches *every* `.py` file; the removal payload then dropped every line of
every matched file, reducing each to `'\n'`. Root-caused and fixed at three
layers: `_grep` refuses empty patterns, `build_payload` refuses blank
removal, and the maintenance classifier now extracts the concrete symbol from
the goal (e.g. `Remove DEAD_FN`). A dedicated regression test guards it.
