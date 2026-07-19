# Observation Engine — Architecture (Milestone 7)

## Goal

A **deterministic Observation Engine** that continuously understands changes in
the engineering environment. It reads the environment, records what it sees as
flat facts, and reports only the *meaningful differences* since the previous run.

Phase 7 freezes the Brain (RetrievalRequirements, Engineering Judgment, Evidence
Assembly, `ask.py`, `identity.py`, `portfolio.py`). Observation is a new
subsystem that does **not** interpret, advise, or re-analyze. It is pure
observation.

## Constraints (all honored)

- No daemons, no background services, no polling loop, no file watchers.
- No LLM, no embeddings, no planner, no agents.
- Fully deterministic: same input state → same observations and report.
- Pull-based: `friday observe` / `friday observers` / `friday observer <name>`
  are the only triggers; nothing runs on its own.

## Components

```
src/friday/observation/
  model.py          Observation / Change / Confidence / Health dataclasses
  interface.py      Observer base class + ObserverHealth
  git_observer.py   GitObserver — the first concrete observer
  registry.py       ObserverRegistry + default_registry()
  engine.py         ObservationEngine (generic, observer-agnostic) + format_run
  __init__.py       public surface

src/friday/db.py    observations table + ObservationRow + CRUD helpers
src/friday/observe.py   M5 snapshot machinery preserved; observe_via_engine()
                        routes `friday observe` through the engine.
src/friday/cli.py    `observers`, `observer <name>`, and `observe` (routed).
```

### Observation model (`model.py`)

One `Observation` is one fact:

| field         | meaning                                                  |
|---------------|----------------------------------------------------------|
| `source`      | observer name (the `Observation.source`)                |
| `subject`     | what it is about (repository name, `"workspace"`)        |
| `aspect`      | the facet (`branch`, `dirty`, `commit_count`, …)         |
| `value`       | the observed value (always a string)                     |
| `confidence`  | `Observed` / `Derived` / `Inferred`                      |
| `observed_at` | UTC timestamp                                            |
| `scope`       | qualifier (repository path)                              |
| `cause`       | evidence-backed reason (required for Inferred)           |

`id` is deterministic: `observed_at:source:subject:aspect`. Re-writing the same
fact in one run is idempotent (`INSERT OR REPLACE`).

### Confidence (`Confidence` enum)

- **Observed** — directly measured with the tool (git status, commit count).
- **Derived** — computed deterministically from observed facts this run
  (commit-count delta, days-since-last-commit, merge/revert counts).
- **Inferred** — a judgment from observed/derived facts (dormant repository,
  repeated reverts). Always carries a `cause` so it is auditable.

### Observer interface (`interface.py`)

```python
class Observer:
    name: str
    def collect(self, conn) -> list[Observation]: ...   # fresh facts this run
    def summarize(self, conn) -> str: ...               # one-line summary
    def health(self, conn) -> ObserverHealth: ...        # can I do my job?
```

Observers are pure readers. A healthy observer returns `[]` rather than raising
when there is nothing to see. `ObserverHealth` carries `healthy`, `status`
(`healthy`/`degraded`/`down`), `method`, and `detail`.

### GitObserver (`git_observer.py`)

Deterministically reads git via the `git` CLI (no GitPython). For each stored
repository it emits:

| aspect           | confidence | how |
|------------------|------------|-----|
| `branch`         | Observed   | `git symbolic-ref` / `rev-parse` |
| `dirty`          | Observed   | `git status --porcelain` |
| `commit_count`   | Observed   | `git rev-list --count` |
| `remote_url`     | Observed   | `git remote get-url` |
| `last_commit_date`| Observed  | `git log -1 --format=%cI` |
| `idle_days`      | Derived    | today − last commit date |
| `activity`       | Derived    | `active` / `dormant` (≥ 30 idle days) |
| `dormant`        | Inferred   | `idle_days ≥ 30`, cause = idle time |
| `merge_events`   | Derived    | recent merge commits in `git log` |
| `revert_events`  | Derived    | recent commit messages mentioning "revert" |
| `repeated_reverts`| Inferred  | ≥ 2 reverts in lookback, cause = count |
| `branch_switch`  | Derived    | current branch ≠ prior run's branch |
| `repository_count` / `dirty_count` | Workspace | Observed / Derived |

### Registry (`registry.py`)

Holds observers in registration order. `default_registry()` seeds `GitObserver`.
Adding a future observer (Terminal, GitHub, Browser, Calendar, Filesystem) is a
one-line `register()` call — the engine and CLI pick it up automatically.

### Engine (`engine.py`)

`ObservationEngine.run()` for one pass:

1. iterate registered observers (order-stable);
2. call `collect()` (failures are isolated — a broken observer yields a
   `degraded` health result and does not abort the run);
3. build each observer's prior state via
   `observation_state_as_of(conn, source, observed_at)`;
4. `diff_observations(prior, current)` → `Change` records (new / changed /
   removed facts; unchanged facts are silent);
5. persist current facts (idempotent per fact).

`diff_observations` is observer-independent: it diffs on `(subject, aspect)` and
preserves `confidence`/`cause`. The engine knows **nothing** about git.

### Storage (`db.py`)

New `observations` table, append-only per run batch (keyed by `observed_at`):

```sql
CREATE TABLE observations (
    id TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
    subject TEXT NOT NULL, aspect TEXT NOT NULL, value TEXT NOT NULL,
    confidence TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', detail TEXT
);
```

`ObservationRow`, `insert_observations`, `latest_observations`,
`observation_state_as_of`, `observations_all` added. The Milestone 5
`snapshots` table is unchanged and remains the historical append-only log.

### `friday observe` routing

`observe.py` keeps its public `observe()` / `diff_snapshots()` / `format_report`
intact (so M5/M6 benchmarks stay green). `cmd_observe` now calls
`observe_via_engine()`, which runs the engine, then translates the engine's
`Change` records into the same engineering-language vocabulary
(`became dirty`, `commits gained`, `branch changed`, `became dormant`,
`repeated reverts`, …) that `format_report` renders.

## Observer Contract

Observers are **fully independent**:

- Each observer's `collect()` reads ONLY from:
  - The database (prior runs' persisted observations via `observation_state_as_of`)
  - The environment (filesystem, git CLI, APIs, exported logs)
- An observer MUST NOT read observations produced by other observers in the SAME run
- The engine makes no ordering guarantees beyond insertion order in `default_registry()`

Observers are **read-only**:

- Observers never mutate the knowledge base, never write to tables (other than via returning observations)
- Observers never make decisions or interpret facts — they emit observations only
- All reasoning happens in the Brain; observers are pure readers of reality

Observers are **isolated**:

- Observer failure (exception in `collect()`) does not abort the run
- Failed observer produces a `degraded` health result; other observers continue
- Engine wraps all observer calls in exception handlers

## Extensibility

To add a new observer (e.g. `FilesystemObserver`):

1. subclass `Observer` in `src/friday/observation/filesystem_observer.py`;
2. implement `name`, `collect`, `summarize`, `health`;
3. `reg.register(FilesystemObserver())` in `default_registry()`.

No engine, CLI, model, or storage change required.

## CLI

```
friday observers            # list registered observers + health + summary
friday observer <name>      # one observer: health, summary, fresh run
friday observer <name> --summary-only
friday observe              # whole-engine run, engineering-language report
```

## Tests & benchmarks

- `tests/test_observation.py` — model, registry, engine diff, real-git observer
  behaviour (dirty, dormant, merge/revert, branch switch), cross-run diff,
  idempotency.
- `tests/test_observation_benchmarks.py` — confidence correctness, health
  method reporting, stable/concise diff, per-run timing, append-only batches.
- `tests/test_observe.py` — preserved Milestone 5/6 benchmarks (unchanged).

## Terminal Observer (Milestone 7.3)

A second built-in observer, proving the engine is generic. It is a **pure
reader** of a JSONL engineering-command activity log (default
`~/.friday/terminal_activity.jsonl`, overridable via `FRIDAY_TERMINAL_LOG`). It
never watches the shell, attaches a PTY, hooks readline, parses history, or runs
a daemon. Each log line is a pre-sanitized event:

```json
{"ts":"<ISO>","tool":"pytest","repo":"Friday","wd":"/abs","exit":1,"duration_s":4.7}
```

Only the whitelisted metadata fields (`ts/tool/repo/wd/exit/duration_s`) are
read. Command arguments, environment variables, secrets, stdout/stderr, and
interactive input are **never read and never emitted** — the observer maps only
those fields to `Observation`s, so it structurally cannot leak them.

Per event it emits `tool` (Observed), `tool_category` (Derived, via the frozen
`CATEGORIES` table), `exit_status` (Observed), `success` (Derived), `duration_s`
(Observed). Run-level signals: `repeated_test_failures` / `repeated_build_failures`
(Inferred), `long_running_build` (Inferred, ≥60s), `repo_switch` / `tool_switch`
(Derived). Categorization is a deterministic table — no LLM. Registered in
`default_registry()` via one line; the engine and CLI needed no changes.

Tests: `tests/test_terminal_observer.py` (29) — build/test/git commands, repo
switch, tool switch, repeated failures, long build, unknown tool, privacy
(no args/secrets/env), health, registration, summary, and a real end-to-end run
through `ObservationEngine`.

## Artifact Observer (Milestone 7.4)

A third built-in observer, proving the engine is generic and that NO engine
change is needed for a wholly new slice of the environment. It observes
**engineering artifacts** (repositories, manifests, documentation, archives,
research PDFs, diagrams, datasets, benchmarks, logs, binaries) from filesystem
**metadata only**. It is a pure reader: it `stat`s paths within the configured
roots and classifies each artifact deterministically. File *contents* (source,
PDF pages, markdown text, images, secrets) are **never opened, read, or
emitted**.

Design choices:

- **Configured roots only.** Default roots are `~/Projects`, `~/Downloads`,
  `~/Documents` (stable aliases). Overridable via `FRIDAY_ARTIFACT_ROOTS`
  (colon-separated). Never the whole home tree; bounded recursion
  (`MAX_DEPTH`, skips `.git`/`node_modules`/`.friday`/…).
- **Stable identity (no absolute paths).** An artifact's primary identity is
  `root_alias/relative_path` (e.g. `Projects/Aether`), not an absolute path.
  If the workspace root moves or syncs across machines, observations stay
  meaningful instead of being tied to `/home/lakshay/...`.
- **Current-state facts only.** Every run emits the SAME stable facts; the
  engine's diff produces each transition (repository created/removed, README
  added, manifest detected, project moved, archive extracted, …) automatically.
  No per-run-only "transition" facts are emitted, so a no-op re-run is
  idempotent (verified: 0 changes on a second run).
- **Privacy.** Only metadata (name, extension, size, mtime, type, relative
  path within a root) ever leaves the filesystem call. The Friday knowledge DB
  (`*.db`) and `.friday` are excluded from scanning.

Per artifact it emits `category` (Observed, deterministic table), `name`,
`ext`, `size`, `modified_at` (Observed), plus stable `readme` / `manifest`
presence per project directory (Derived), `notes_directory`, `extracted_archive`,
`research_pdf`, `large_document` (Inferred when ≥ 50 MB) and workspace counts
(`artifact_count`, `repository_count`, `documentation_count`,
`research_paper_count`, `archive_count`, `download_count`) with derived
`repository_lifecycle` / `repeated_downloads` signals. Classification is a
frozen table — no LLM. Registered in `default_registry()` via one line; the
engine and CLI needed no changes.

Tests: `tests/test_artifact_observer.py` (27) — classification (case-insensitive,
per category), stable identity (relative path, no absolute path leaked),
repository created/renamed/deleted (via engine diff), README added, manifest
detected, research PDF in Downloads, archive + extraction, workspace move across
roots (via engine diff), unknown artifact, privacy (no file contents / outside
roots / its own DB), health (healthy + down), registration, summary format, and
real end-to-end runs through `ObservationEngine` (idempotent).

## Determinism guarantees

- All timestamps are UTC ISO; reading `now` once per run.
- Git reads are read-only and path-resolved.
- Terminal reads are read-only over a pre-sanitized log; nothing is captured.
- Fact ids are deterministic; persistence is idempotent per fact.
- No randomness, no network, no LLM.
