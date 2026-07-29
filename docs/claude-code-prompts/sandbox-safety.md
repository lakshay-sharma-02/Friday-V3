# Sandbox & Safety — Prompt for Claude Code

## Intent
FRIDAY runs simulations before Tony commits to an action. "What if I reroute power through the secondary manifold?" — she shows him the outcome without actually doing it. Your system has `blast_radius` and `confirm_gate.py` for permission gating, but no what-if sandbox and no dry-run mode. Also missing: rollback/undo for most actions.

## What to build

### Phase 1: What-if Sandbox

Create `src/friday/sandbox.py`. A simulation engine that predicts outcomes without side effects.

**What it does:**
- Given a proposed action (shell command, git operation, filesystem change), simulate the outcome
- For git operations: run in a worktree clone (git worktree add) — safe, isolated, real
- For shell commands: run in a Docker container (if Docker available) OR a temporary directory
- For filesystem changes: snapshot the target path, run the operation, diff the result, rollback
- For code changes: do nothing (the planning layer should validate, not mutate)
- Output: "If you run this command, here's what will change:" + diff/predicted-outcome

**Simulation types:**
- `friday what-if "rm -rf node_modules && npm install"` → run in tmp dir with copy of project, show outcome
- `friday what-if --git "rebase --onto main feature"` → run in worktree, show conflicts or success
- `friday what-if "pytest tests/"` → show expected test outcome (from last run + current diff analysis)
- `friday what-if --dry "friday auth:disable"` → trace through the executor pipeline without running

**Key design:**
- Sandbox is ALWAYS opt-in — never silently simulates
- Simulation result is clearly marked: "⚠ SIMULATION — No changes were made"
- If sandbox infrastructure unavailable (no Docker, no git worktree), fall back to: "Can't simulate this — requires [Docker]"
- Sandbox timeout: 30s default, configurable
- Store simulation history in `simulation_log` table: `(action, sandbox_type, success, outcome_summary, duration_ms)`

### Phase 2: Dry-run Mode

Add `--dry-run` flag to every mutating CLI command.

**How it works:**
- `friday execute "deploy the app" --dry-run` → shows what WOULD happen without executing
- Traces: planner output, resolver assignment, schedule, executor dispatch — but stops before actual execution
- Output: "Would run: Step 1 (shell: pytest), Step 2 (docker build), Step 3 (docker push)"
- Also shows: autonomy level required, estimated duration, blast radius
- Works for: `friday execute`, `friday plan`, `friday resolve`, `friday runtime`, `friday protocol run`

**Implementation:**
- Add `dry_run: bool = False` parameter to `RuntimeEngine.dispatch()`
- Each step logs what it WOULD do but returns success without side effects
- The executor base class has a `dry_run` mode that's inherited by all executors
- Shell executor: prints the command but doesn't run it
- Git executor: prints the git command but doesn't execute it
- Filesystem executor: prints the file operations but doesn't touch anything

### Phase 3: Rollback / Undo

Add a rollback capability to `src/friday/runtime/engine.py`.

**What it does:**
- Before executing a mutating action, snapshot the state that will be changed
- Snapshot types:
  - File contents (for filesystem operations) — copy to `.friday/rollback/`
  - Git state (for git operations) — record HEAD SHA before the operation
  - DB state (for DB operations) — no automatic rollback (too risky), require explicit backup
- `friday undo` → undo the last mutating action
- `friday undo <action_id>` → undo a specific action by its execution ID
- `friday undo --list` → show recent undoable actions

**Key design:**
- Rollback is BEST-EFFORT — some actions can't be cleanly rolled back
- Each action type declares its reversibility (reuse the existing `Reversibility.REVERSIBLE / IRREVERSIBLE` from `confirm_gate.py`)
- IRREVERSIBLE actions don't create rollback snapshots (no false sense of safety)
- Rollback creates its OWN action entry in the execution log — "undo of <action>" is itself a logged action
- Rollback itself can be rolled back (undo of undo = redo)

## Files to touch
- `src/friday/sandbox.py` (new) — simulation engine, what-if dispatcher, sandbox factory
- `src/friday/runtime/engine.py` — add dry_run propagation, rollback snapshot/restore
- `src/friday/runtime/executor.py` — add dry_run flag to base executor
- `src/friday/runtime/executors.py` — each executor handles dry_run
- `src/friday/cli.py` — add `friday what-if`, `friday undo`, `--dry-run` to execute/* commands
- `src/friday/runtime/dispatcher.py` — wire dry_run + rollback through dispatch pipeline
- `src/friday/db.py` — add `simulation_log`, `rollback_snapshots` tables
- `tests/test_sandbox.py` (new)
- `tests/test_dry_run.py` (new)
- `tests/test_rollback.py` (new)

## Acceptance criteria
1. `friday what-if "rm -rf node_modules && npm install"` → creates temp copy, runs command, shows diff of changes
2. `friday what-if --git "rebase --onto main feature"` → uses git worktree, shows conflicts or success
3. Simulation result clearly marked as simulation (no accidental real execution)
4. `friday execute "deploy" --dry-run` → shows each step without executing
5. Every executor (shell, git, filesystem, etc.) respects dry_run flag
6. `friday execute "update config"` → creates rollback snapshot before mutating
7. `friday undo` → restores snapshot from last mutating action
8. `friday undo --list` → shows undoable actions with timestamps
9. IRREVERSIBLE actions don't create rollback snapshots
10. Rollback itself is logged and can be rolled back (undo of undo = redo)
