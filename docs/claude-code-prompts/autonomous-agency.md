# Autonomous Agency — Prompt for Claude Code

## Intent
FRIDAY doesn't give up after one try. In the MCU, when something goes wrong, she adapts — tries a different approach, routes around the failure, or wakes Tony if it's serious. Your current pipeline (Plan → Graph → Resolve → Schedule → Run) is single-shot. If it fails, it fails. No retry, no alternative approach, no multi-cycle persistence.

The goal: **persistent missions** that survive daemon restarts, adapt to intermediate results, and try multiple approaches until success is achieved or the goal is explicitly abandoned.

Also: **sub-agent delegation** — FRIDAY spawns child tasks to work in parallel.

## What to build

### Phase 1: Persistent Missions

Create `src/friday/mission.py`. A mission is a high-level goal that persists across daemon cycles:

**Data model:**
```
Mission:
  - id: uuid
  - goal: str              # "Figure out why the build fails and fix it"
  - created_at: str
  - status: ACTIVE | PAUSED | SUCCEEDED | FAILED | ABANDONED
  - plan_id: str           # current plan being executed
  - outcome: str | null    # final result summary
  - attempt_count: int     # how many times we've tried
  - last_attempt: str | null
  - context: json          # accumulated state between attempts
```

**Lifecycle:**
1. Mission created (user says "Fix the build" or autonomous planner creates one)
2. Planner generates an ActionPlan for the goal
3. Plan executes → fails
4. Mission records failure, updates context with what was learned
5. On next daemon cycle, mission is re-evaluated:
   - Can we try a different approach? → generate new plan
   - Same approach with different params? → retry with adjustments
   - Has the goal become irrelevant? → mark PAUSED
   - Failed N times (configurable, default: 5)? → mark FAILED, notify operator
6. If a mission SUCCEEDS, notify operator with outcome summary

**Adaptation strategies:**
- If `shell_exec` failed → try `python_exec` with a wrapper that uses subprocess
- If timeout → increase timeout and retry
- If dependency not found → try installing the dependency first (new sub-mission)
- If the error message suggests a known fix → try that fix (heuristic matching against error DB)

**Key design:**
- Missions table in main DB, persisted across restarts
- Mission engine runs as a post-cycle hook in the daemon
- Each mission has a `max_attempts` and `ttl` (abandon if not resolved in N days)
- User can inspect: `friday mission list`, `friday mission show <id>`, `friday mission abandon <id>`

### Phase 2: Sub-agent Delegation

The current `Worker` / `Executor` model dispatches tasks to fixed executors. Add the ability for Friday to spawn **autonomous sub-agents** — long-running child processes that work independently and report back.

**Architecture:**
- `SubAgent` dataclass: `id`, `goal`, `instructions`, `created_at`, `last_report`, `status`
- SubAgent runs as a separate Python process:
  - Has its own daemon cycle (simplified — observe → act → report)
  - Has access to a subset of Friday's capabilities (sandboxed)
  - Reports findings back via a shared `subagent_reports` table
- `SubAgentEngine` in daemon manages lifecycle:
  - Spawning (max N concurrent, default: 3)
  - Health checking (heartbeat every 30s)
  - Killing (on timeout, or when parent mission completes)
  - Collecting reports

**What sub-agents can do:**
- Research: "Go look at our API usage patterns and find optimization opportunities" → reads code, git history, produces a report
- Monitor: "Watch this CI pipeline and tell me when it goes green" → lightweight daemon that checks every 5m
- Explorer: "Analyze this directory and tell me what it does" → reads files, produces a summary
- Repair: "Try three different approaches to fix this test, report which worked"

**Key design:**
- Sub-agents communicate ONLY through the DB — no shared memory, no IPC
- Parent agent (main daemon) reads reports on next cycle
- Sub-agents are stateless — if killed, they can be restarted from their mission context
- CLI: `friday agent list`, `friday agent inspect <id>`, `friday agent kill <id>`

### Phase 3: Adaptive Plan Revision

Currently, once a Plan is compiled into a Task Graph, it's fixed. Add the ability for the runtime to **pause execution mid-graph**, analyze partial results, and revise the remaining DAG.

**Mechanism:**
- After each wave of task execution, the runtime checks if any task's output changes the assumptions of downstream tasks
- If a task produces an unexpected output (value differs from expected in the plan), flag it
- Call `planning/compiler.py` with the current state: "Here's what we know now, revise the remaining graph"
- Compiler produces a NEW task DAG for the remaining work, potentially:
  - Removing tasks that are now irrelevant
  - Adding tasks that were not anticipated
  - Reordering tasks based on new information
  - Changing worker assignments if the current worker failed

**Integration:**
- The mission engine owns the revision loop
- Each revision increments `plan_version` in the execution session
- The runtime's state machine needs a new state: `REVISING`
- Maximum N revisions per mission (configurable, default: 10)

## Files to touch
- `src/friday/mission.py` (new) — Mission dataclass, engine, lifecycle
- `src/friday/subagent.py` (new) — SubAgent engine, spawning, health
- `src/friday/runtime/engine.py` — add REVISING state, revise-on-wave hook
- `src/friday/autonomous_planner.py` — wire mission creation into planner output
- `src/friday/daemon.py` — mission engine + subagent engine post-cycle hooks
- `src/friday/db.py` — add `missions`, `subagents`, `subagent_reports` tables
- `src/friday/cli.py` — add `friday mission`, `friday agent` commands
- `tests/test_mission.py` (new)
- `tests/test_subagent.py` (new)
- `tests/test_runtime_engine.py` — add test for REVISING state

## Acceptance criteria
1. `friday mission create "Fix the flaky test in auth"` → mission created, plan generated
2. On plan failure, mission records attempt, retries with adjusted params on next cycle
3. Mission fails after N attempts → marked FAILED, operator notified
4. `friday mission list` shows all missions with status, attempts, last outcome
5. Mission can be explicitly abandoned with `friday mission abandon <id>`
6. `friday agent spawn "research" "Read src/friday/auth.py and summarize auth patterns"` → sub-agent created
7. `friday agent list` shows running agents with last heartbeat
8. Sub-agent produces a report → stored in subagent_reports
9. Runtime pauses mid-execution when unexpected output detected, calls adaptive revision
10. Revised plan is different from original (tasks added/removed/reordered)
