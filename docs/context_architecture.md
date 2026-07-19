# Engineering Context Layer — Architecture (Milestone 7.2)

## Goal

The Observation Engine (M7, frozen) produces raw facts. An individual
observation — "Git commit at 09:15", "pytest at 09:17", "README edit at 09:20" —
is useful but flat. The Context Layer turns a stream of observations into
**engineering work**: sessions of contiguous activity, correlated to a
evidence-backed label, summarized into a day.

```
World
  ↓
Observation Engine        (frozen — GitObserver etc.)
  ↓
Raw Observations           (observations table)
  ↓
Engineering Context Layer  ← BUILD THIS (M7.2)
  ↓
Knowledge Base / Brain     (untouched)
```

The Brain is **not** modified. The Context Layer is the bridge: it is the unit
the Brain reasons about instead of isolated facts.

## Constraints (all honored)

- No daemon, no polling, no watcher. Pull-based: `friday context` runs the
  engine on demand.
- No LLM, no embeddings, no planner, no agents.
- Fully deterministic: identical observations → identical sessions, timeline,
  summary.
- **READ/WRITE separation**: only `friday context build` writes; every read
  command is structurally unable to mutate persistent state.
- Append-only persistence; sessions reference observation ids, never duplicate
  raw observations.
- Conservative correlation: when two activities are both plausible, prefer
  UNKNOWN (split) over a wrong fusion. Sessions are easy to merge later; a
  wrongly fused session is not.

## Components

```
src/friday/context/
  __init__.py       public surface
  models.py         EngineeringSession, SessionActivity, ContextSummary,
                    TimelineEntry, Confidence (alias of observation's)
  session.py        build_sessions — deterministic grouping
  correlate.py      correlate / build_correlated — activity labeling
  timeline.py       build_timeline — chronological axis + idle gaps
  summarize.py      summarize_day — daily summary
  engine.py         ContextEngine — read obs → build → persist → query

src/friday/db.py    sessions table + SessionRow + CRUD (append-only)
src/friday/cli.py   context, context today, sessions, timeline, session <id>
```

### Models (`models.py`)

- `SessionActivity` — conservative label enum:
  `UNKNOWN, COMMITTING, FEATURE_WORK, DOCUMENTATION, DEBUGGING, TESTING,
  REFACTORING, REVIEW, IDLE`. A label is assigned only on unambiguous evidence.
- `EngineeringSession` — `start_time, end_time, repositories, observations
  (ids), activity, confidence, primary_repo, branch, summary, built_at`.
  `duration_min` derived; `id` deterministic
  (`built_at:primary_repo:start_time`) for idempotent append-only storage.
- `TimelineEntry` — `session` or `idle` slot with start/end/label.
- `ContextSummary` — `day, session_count, repositories, estimated_active_min,
  context_switches, longest_session_min, most_active_repo, current_focus`.
- `Confidence` — **alias** of the Observation Engine's `Confidence` enum
  (`Observed/Derived/Inferred`). One vocabulary across the chain.

### Session builder (`session.py`)

`build_sessions(observations)` groups ordered observations into sessions:

1. Sort by `observed_at`. The Observation Engine stamps one shared timestamp per
   run, so each run is one instantaneous *event* carrying all its facts.
2. Walk events; start a new session when:
   - gap from previous event > `SESSION_GAP_MIN` (90 min), **or**
   - same repo, branch known, and branch changed (branch switch = new context),
     **or**
   - the event's repo does not overlap the current session's repos.
3. `primary_repo` = most frequently observed repo in the session.

Workspace-only facts (no repo) never extend a repo session and never form a
session of their own. Result: conservative splitting — the safe default.

### Correlation (`correlate.py`)

`correlate(session)` assigns one label from the session's own facts (read from
the Observation objects attached during build). Rules, in priority order:

| Signal                                         | Label            | Confidence |
|------------------------------------------------|------------------|------------|
| `repeated_reverts` true                        | DEBUGGING        | Inferred   |
| `revert_events >= 2`                           | DEBUGGING        | Inferred   |
| `branch_switch`/`merge_events` + `commit_count>0` | FEATURE_WORK  | Derived    |
| `commit_count > 0` (no doc/branch signal)      | COMMITTING       | Observed   |
| `readme_changed` (no commit)                   | DOCUMENTATION    | Derived    |
| `dirty` only (no commit)                       | TESTING          | Derived    |
| nothing definitive                             | UNKNOWN          | Derived    |

Every rule is evidence-backed; nothing is invented. The conservative fallback is
`UNKNOWN`.

### Timeline (`timeline.py`)

`build_timeline(sessions)` emits oldest-first entries. Between sessions whose gap
≥ `IDLE_GAP_MIN` (30 min) it inserts an explicit `idle` entry. No reordering, no
inferred work.

### Summary (`summarize.py`)

`summarize_day(sessions, day)` returns `ContextSummary`:
- `session_count`, `repositories` (insertion order),
- `estimated_active_min` (sum of session durations),
- `context_switches` (adjacent primary-repo changes),
- `longest_session_min`, `most_active_repo` (by summed duration),
- `current_focus` (latest session's repo + activity).

### Engine (`engine.py`)

READ and WRITE are strictly separated:

- **WRITE** — `build(source="git", as_of=None)` → `ContextBuildResult`. Reads
  observations of `source`, groups, correlates, and persists. The ONLY mutating
  entrypoint. Returns `total / created / updated / latest_observation` counts
  (does not print). `as_of` keys the build window; rebuilding the same window
  replaces (idempotent), a new window appends.
- **READ** — `sessions()`, `session(id)`, `sessions_for_day(day)`, `timeline()`,
  `summary(day)`, `is_stale()`. These never mutate persistent state. `is_stale()`
  is a pure read comparing `latest_observation_time` vs `latest_session_built_at`.
- `rebuild_all` removed; callers use `build()` directly.

No read method calls `build()` or `insert_sessions`. A read command is
structurally unable to write.

### Storage (`db.py`)

New `sessions` table, append-only:

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    repositories TEXT NOT NULL, primary_repo TEXT, observations TEXT NOT NULL,
    activity TEXT NOT NULL, confidence TEXT NOT NULL, duration_min REAL NOT NULL,
    branch TEXT, summary TEXT, built_at TEXT NOT NULL
);
```

`observations` holds comma-joined **observation ids** — raw facts are never
copied. `insert_sessions` is `INSERT OR REPLACE` on `id` (idempotent per
window). `sessions_all`, `sessions_on_day`, `get_session` added.

## CLI — READ / WRITE split

```
friday context build     # WRITE: build sessions from observations, persist, print summary
friday context [today]   # READ-ONLY: show current context (prompts to build if none/stale)
friday sessions           # READ-ONLY: all sessions, newest first, with ids
friday timeline           # READ-ONLY: chronological sessions + idle gaps
friday session <id>       # READ-ONLY: one session + its evidence observation ids
```

`friday context build` is the only command that writes. `friday context` (without
`build`), `sessions`, `timeline`, and `session <id>` are strictly read-only: if no
sessions exist they print a "run `friday context build`" prompt; if observations
exist newer than the last build they print an "out of date — run `friday context
build`" notice. They never build automatically.

### Three independent stages

```
friday observe          → append-only raw observations
friday context build    → derive + persist engineering sessions   (WRITE)
friday context          → read-only display                        (READ)
```

Each stage is deterministic and independent; a read never mutates state.

## Extensibility

Future observers (Terminal, GitHub, Browser, Calendar, Filesystem) plug into the
**Observation Engine** unchanged. The Context Layer reads only the observations
they produce — correlation keys on generic aspects (`commit_count`, `dirty`,
`branch_switch`, …) and stays observer-agnostic. Adding a GitObserver-like
observer requires zero changes here.

## Tests & benchmarks

- `tests/test_context.py` (22) — unit tests: grouping rules, correlation,
  timeline ordering, summary correctness, append-only persistence,
  reference-not-copy.
- `tests/test_context_benchmarks.py` (15) — the eight required benchmark
  scenarios plus idle-gap, append-only, no-duplicates, conservative-unknown,
  engine-reads-observations.
- `tests/test_context_read_write.py` (10) — READ/WRITE separation regression:
  build writes; `context`/`sessions`/`timeline`/`session` only read; stale
  warning appears; repeated reads never modify the DB; repeated builds idempotent.

## Cross-layer dependency

Context imports `Confidence` from `observation.model` to maintain one vocabulary
across the Reality→Observation→Context→Brain chain. This is **intentional aliasing**,
not architectural coupling:

```python
# context/models.py
from ..observation.model import Confidence as _ObsConfidence
Confidence = _ObsConfidence
```

If Observation's `Confidence` enum changes, Context's alias updates automatically.
The dependency is unidirectional: Observation knows nothing of Context.

## Determinism guarantees

- All grouping keys on UTC timestamps and deterministic repo/branch equality.
- `id` is a pure function of (`built_at, primary_repo, start_time`).
- Correlation reads only present facts; no randomness, no network, no LLM.
- Re-running the same window yields identical persisted rows (idempotent).
