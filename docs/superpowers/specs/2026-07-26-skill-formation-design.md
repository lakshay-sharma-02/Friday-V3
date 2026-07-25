# Pillar B Stage 4 — Skill Formation

## Objective

Take labeled workflow intents (Pillar B Stage 3) and form them into deployable,
replayable skills — making the learning pipeline produce real capabilities that
Friday can execute without the user's manual re-entry of each repeated workflow.

## Design Decisions

### 1. Skill behavior: Replay actions, not generated code

When a skill is invoked, Friday replays the observed action sequence using the
same executors that performed it originally (HyprlandExecutor, BrowserExecutor).
No LLM-generated worker module, no codegen — the skill *is* the step sequence
plus concrete exemplar values for each step.

Rationale: codegen adds failure modes (syntax errors, hallucinated APIs) without
increasing value for this use case. The user's repeated workflows are already
valid action sequences — replay them through the verified execution path.

### 2. Target resolution: Enrich miner to store exemplar distributions

The sequence miner normalizes targets for matching (`workspace_switch: "3"` →
`workspace_switch: "<workspace>"`). To replay, we need the concrete values.
Rather than re-querying raw actions during formation, the miner stores a
per-position distribution of observed concrete values alongside each pattern.

### 3. Storage: Single registry + formed_skills payload table

- `workers` table holds all dispatchable things (built-in, LLM-generated, formed
  skill). A new `worker_kind='formed_skill'` column distinguishes them.
  `implementation` field for formed skills stores a FK to `formed_skills.id`,
  not a module path.
- `formed_skills` table stores the task graph and per-step exemplar
  distributions. One registry, one promotion path, one dispatch resolver — but
  the data shape specific to skills lives where it fits.

## Schema

### workers table addition

```sql
ALTER TABLE workers ADD COLUMN worker_kind TEXT NOT NULL DEFAULT 'function';
```
Existing rows get `'function'`. Formed skills get `'formed_skill'`.

For `worker_kind='formed_skill'`, the `implementation` field stores a
string-encoded integer FK: `"formed_skill:<id>"`.

### New formed_skills table

```sql
CREATE TABLE formed_skills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_intent_id INTEGER NOT NULL REFERENCES workflow_intents(id)
                      ON DELETE CASCADE,
    task_graph      TEXT NOT NULL,       -- JSON: abstracted step sequence
                                         -- [(action_type, normalized_target), ...]
    exemplars       TEXT NOT NULL DEFAULT '{}',
                     -- JSON: per-step value distributions
                     -- {'0': {'3': 5, '5': 1}, '1': {'firefox': 6, ...}, ...}
                     -- key = step index, value = {concrete_value: count}
    invocation_count INTEGER NOT NULL DEFAULT 0,
    last_invoked_at TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

## Distribution-Aware Exemplar Handling

### Miner enrichment

The sequence miner already normalizes targets for matching. Alongside each
normalized n-gram, it now also tracks the *raw concrete values* observed at
each position, aggregated across all occurrences:

```
step 0: workspace_switch → {concrete: {"3": 5, "5": 1}, normalized: "<workspace>"}
step 1: app_launch → {concrete: {"firefox": 6}, normalized: "<app>"}
```

This is stored per-position, not per-n-gram instance — the distribution is
accumulated across all sessions where this pattern appeared.

### Formation-time decisions

At skill formation time, each step's exemplar distribution is evaluated:

1. **High consensus (≥80%)** → the winning value is set as the step's default
   exemplar. At replay time, this value is used without asking.
2. **Low consensus (<80%)** → the step is marked as a **required parameter**.
   At invocation time, Friday asks the user for the value (or accepts it as an
   explicit invocation argument). The step is NOT silently defaulted to the
   mode — doing so would hide a real parameter that varies by intent.

This distribution concentration also feeds the overall skill confidence:
- If every step is high-consensus, the skill gets a confidence bonus (+1 tier).
- If any step is low-consensus, the skill's confidence is capped at "medium"
  regardless of the workflow intent's LLM confidence.

### Staleness decision (explicit, v1 scope)

**Formation runs once per workflow intent.** Once a skill is formed, it is not
re-derived automatically when the underlying pattern changes. This means:

- If the user shifts from Firefox to Chromium for "Start dev server", the skill
  silently diverges from current behavior.
- Re-formation requires a manual `friday patterns form --refresh` or clearing
  the skill and re-running formation.

**ponytail:** This is a known limitation. A future version should detect drift
by comparing exemplar distributions between formation time and the most recent
N occurrences, and either re-form automatically or flag the skill as "stale."
Not solving in v1 because the drift-detection loop needs more data than v1 will
accumulate, and premature automation of re-formation would silently overwrite
intentional user customization.

## Skill Formation Pipeline

New module `src/friday/skill_formation.py`, entry point `form_skills(conn)`.

### Algorithm

For each workflow_intent with confidence `"high"` or `"medium"`:

1. **Check if already formed**: query `formed_skills` via
   `workflow_intent_id`. Skip if a formed skill exists for this intent.

2. **Build task_graph**: convert the workflow_intent's abstracted step sequence
   (from `pattern_summary` / `pattern_seq`) into a JSON list of
   `(action_type, normalized_target)` tuples.

3. **Resolve exemplars**: query the distributon stored alongside the original
   mined pattern. For each step position:
   - Calculate consensus percentage = max_count / total_count
   - If ≥80%: set as fixed default for replay.
   - If <80%: mark as required parameter; default to mode but flag for user
     prompt at invocation time.

4. **Calculate overall confidence**:
   - Start from the intent's LLM confidence.
   - Apply the consensus cap: if any step is low-consensus, cap at "medium".
   - Map to worker status: high → `beta`, medium → `proposed` (needs manual
     `promote` to become dispatchable at all).

5. **Insert formed_skills row** with task_graph, exemplars, timestamps.

6. **Register worker row** with `worker_kind='formed_skill'`,
   `implementation_ref = f"formed_skill:{id}"`, and the mapped status.

### Daemon integration

In `daemon.py`'s `_run_cycle()`, add a step after Pillar B Stage 3 (intent
labeling) that calls `form_skills(conn)`. This runs in the same try/except
pattern — failure never breaks the cycle.

### CLI integration

Add to `friday patterns`:
- `friday patterns form` — run formation pipeline, display formed skills
- `friday patterns form --force` — re-form even if already formed (for
  staleness workaround)

New `friday skills` subcommand (optional, v1):
- `friday skills` — list formed skills with status
- `friday skills run <name>` — invoke a formed skill by worker name

## Execution Path

### resolve_executor() branch

In `runtime/executors.py`, the `resolve_executor()` function gets a new branch
after the hardcoded table and before the dynamic fallback:

```python
if row.get("worker_kind") == "formed_skill":
    from ..skill_formation import build_replay_executor
    return build_replay_executor(conn, impl_ref, workspace=workspace)
```

### ReplayExecutor

A new executor class (not a standalone module — lives in `skill_formation.py` or
a small adjunct) that:

1. Loads the `formed_skills` row by impl_ref.
2. For each step in the task_graph:
   - Resolves the exemplar value (fixed default or prompts user if param).
   - Dispatches through the existing executor (HyprlandExecutor for desktop
     actions, BrowserExecutor for browser actions) using
     `resolve_executor("worker:hyprctl")` / `resolve_executor("worker:browser")`.
   - Applies the confirm gate (`confirm_gate.gate()`) before execution.
   - Uses verify-by-diff to confirm the action had the intended effect.
3. Records the result (success/failure per step).
4. Increments `invocation_count` and updates `last_invoked_at`.

No new execution path — reuses all existing action infrastructure.

## Graduated Trust Mapping

| Intent confidence | Consensus bonus/cap | Worker status  | Behavior when invoked |
|---|---|---|---|
| high, all steps ≥80% | confidence bonus → "very high" | beta | Executable after `approve`, eligible for `promote` to active |
| high, any step <80% | capped at medium | proposed | Must be promoted manually before any dispatch |
| medium, all steps ≥80% | unchanged | proposed | Must be promoted manually |
| medium, any step <80% | capped at medium | proposed | Must be promoted manually |
| low / fallback | — | skipped | Not formed |

## Files Changed

| File | Change |
|---|---|
| `src/friday/db.py` | Add `worker_kind` migration, `formed_skills` CREATE TABLE |
| `src/friday/sequence_miner.py` | Enrich n-gram extraction to store per-position concrete value distributions |
| `src/friday/skill_formation.py` | **New** — formation pipeline: build task_graph, resolve exemplars, register worker |
| `src/friday/cli_patterns.py` | Add `form` action, `--force` flag |
| `src/friday/daemon.py` | Add skill formation step after intent labeling in `_run_cycle()` |
| `src/friday/runtime/executors.py` | Add `worker_kind='formed_skill'` branch in `resolve_executor()` |
| `tests/test_skill_formation.py` | **New** — tests for formation pipeline |
| `tests/test_sequence_miner.py` | Update for exemplar distribution enrichment |

## Future Considerations (not v1)

- **Drift detection**: compare current exemplar distributions against the
  formation-time snapshot; re-form or flag when they diverge significantly.
- **Multi-user coordination**: skills formed from one user's intent data could
  be shared across sessions or users.
- **Rollback**: ability to un-form a skill and retry with updated exemplars.
- **Step failure recovery**: if a replay step fails, the current executor
  already reports failure; future versions could add branch logic (skip, retry
  with alternative exemplar, abort workflow).
