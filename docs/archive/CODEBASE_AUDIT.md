# CODEBASE AUDIT: Brutal Module-by-Module Analysis

> Generated: July 2026 (Updated after P0/P1 completion sprint)
> Purpose: Single source of truth for what's broken, half-built, useless, salvageable, or good.
> Rating scale: 🔴 BROKEN | 🟡 HALF-BUILT | ⚪ USELESS | 🟢 GOOD | 🔵 OVERENGINEERED | ✅ FIXED

---

## NEW MODULES (BUILT IN THIS SPRINT)

### `anaphora.py` — 🟢 GOOD (NEW)

Cross-query follow-through / pronoun resolution for the `ask()` pipeline. A 195-line module that handles:
- Pronoun resolution ("tell me more about it" → "tell me more about friday")
- Action repetition ("do that again" → re-asks previous question)
- More-like-this ("show me more like this" → similarity query)
- Focus narrowing, continuation+pronoun, implicit subject switch
- LLM fallback when deterministic patterns fail

Integrated into `ask.py.resolve_followup()` as the last check before returning `None`. All 25 tests pass.

---

### `protocol.py` + `cli_protocol.py` — 🟢 GOOD (NEW)

Named Protocols / Macro Procedures — user-defined multi-step action sequences.

- `Protocol` / `ProtocolStep` dataclasses with CRUD
- `ProtocolEngine` — CRUD + `run()` through the existing `resolve_executor`/`dispatch` pipeline
- `{variable}` placeholder extraction and substitution
- `_MiniTask` adapter between protocol steps and the runtime dispatcher
- Persisted in `named_protocols` DB table
- CLI: `friday protocol list|create|show|run|delete`
- Step failure strategy (abort/skip)
- Dry-run mode

23 tests pass. Wired into `cli.py`.

---

### `watcher.py` + `cli_wait.py` — 🟢 GOOD (NEW)

Persistent Watchers — "tell me when this test passes."

- 4 condition checkers: `shell_exit_code`, `file_modified`, `http_status`, `process_running`
- Interval filtering (only checks when due)
- Subprocess timeout (default 60s, configurable)
- Actionable ambient feed events with `friday wait ack <name>` command
- `--repeat` flag for auto-rearming watchers
- DB migration via ALTER TABLE for existing instances
- Daemon integration: `_stage_watcher_check()` in the ALWAYS section
- CLI: `friday wait list|create|show|check|delete|ack`

30 tests pass. Wired into `cli.py` + `daemon.py`.

---

### `impact.py` + `cli_impact.py` — 🟢 GOOD (NEW)

Change Impact Analysis — "what breaks if I modify this file?"

- `ImpactReport` dataclass with 10+ evidence sections
- Git analysis: commit count, recent commits, blame author distribution, last modified
- Repository resolution via path prefix matching (no false-positive name matching)
- Related repos from `relationships` table
- Cross-project correlations from `correlation_results` table
- Knowledge from knowledge store
- Co-occurring repos via single JOIN with proper CSV matching
- Architecture/components data
- CLI: `friday impact <file> [--json] [--summary]`

19 tests pass. Wired into `cli.py`.

---

### `narrative.py` + `cli_narrative.py` — 🟢 GOOD (NEW)

Codebase Narrative / Git Archaeology — tells the story of a project's evolution.

- Full git log analysis (2000 commits with numstat)
- Author breakdown + bus factor calculation
- Phase detection (splits history into 3-5 development phases)
- Activity patterns (peak hour, batch vs single commits, weekend work)
- File evolution (adds, deletes, most-changed files)
- Milestones (commits with 200+ changes)
- Snapshot history from DB (identity/architecture/README changes)
- Rich rendering with ASCII bar charts
- CLI: `friday narrative <repo> [--json] [--summary]`

O(n²) → O(n) dict lookup fix applied for stat matching. 18 tests pass. Wired into `cli.py`.

---

## CORE INFRASTRUCTURE

### `db.py` — 🔵 OVERENGINEERED / 🟢 FUNCTIONAL

**~1200 lines of schema + migrations.** The schema is thorough — every table, index, FK — and the migration system handles additive changes gracefully. But:

- **The schema has 40+ tables.** Most are NEVER queried. `plan_evolution`, `scheduler_evolution`, `runtime_evolution`, `initiative_evolution`, `understanding_evolution`, `insight_evolution`, `task_evolution` — every single one has an append-only "evolution" table that tracks state transitions. These are **dead weight**. Nothing reads `insight_evolution`. Nothing queries `plan_evolution`. The Law 18 "append-only everything" mandate was followed to the letter, creating ~15 evolution tables that store data nobody consumes.
- **`_migrate()` is 300+ lines** with 15 individual migration blocks. Every new feature adds another `ALTER TABLE` + `CREATE TABLE IF NOT EXISTS`. This function is a ticking time bomb — the order of operations is fragile (`_ensure_observations_pk` rebuilds the entire table).
- **`connect()` runs the entire schema + all migrations EVERY connection.** For an in-memory test DB this is fine. For the daemon which opens 5+ connections per cycle, this is wasted SQL execution every time.
- **The schema mixes concerns.** Tables for the M8 knowledge pipeline sit next to tables for the ambient feed, next to tables for autonomous actions, next to tables for skill formation. No namespacing.

**What to do**: Collapse the evolution tables into a single generic `layer_history` table. Split schema into versioned migration files instead of one monolithic string.

---

### `errors.py` — 🟢 GOOD

**Clean `FridayError` dataclass** with typed `ErrorType` enum, `error_from_exception()` auto-classification, and `format_friday_error()` for user-facing messages. This is genuinely well-designed. Only ~200 lines. No issues.

---

### `services/llm.py` — 🟡 HALF-BUILT

The LLM abstraction layer. Has:
- `_call(system, user)` — single-model call with fallback chain
- `FALLBACK_PROVIDERS` — ordered list of providers (Groq → OpenRouter → Gemini)
- `_enabled()` — checks if any provider is configured

**But**:
- `_call()` re-implements HTTP for EVERY provider. It should use the OpenAI-compatible client. Instead it has per-provider URL/key/model env var mapping with custom HTTP logic.
- **The `reasoning/engine.py` ensemble completely bypasses this module** — it has its own `_call_one()` that reimplements the same HTTP logic from scratch using `urllib.request`. Two separate HTTP implementations for the same thing.
- Retry logic is basic (linear fallback, no backoff). Rate limit handling is absent.
- No token counting, no streaming support.
- `_call()` returns `Optional[str]` — no structured output, no function calling support. Every consumer has to parse JSON from raw text.

---

## CORE SYSTEMS

### `daemon.py` — 🟢 GOOD (post-refactor)

**Recently rewritten** — now has `CycleContext` dataclass, stage-based `_run_cycle()`, unified service poller, proper `threading.Lock`, always-run maintenance. 17/17 tests pass. See `CORE_ARCHITECTURE_LAWS.md` for details on the rewrite.

### `ambient.py` — 🔵 OVERENGINEERED

The ambient event feed. The event model, factory functions, CRUD operations, and feed pruning are all functional. But:

- **Salience computation is overengineered**: 8 tuning parameters, `log2` rarity normalization, SQL COUNT queries on every `push_event()`. **Nobody has ever tuned these parameters** because there's no feedback loop to measure whether the ranking helps.
- **`compute_salience()` runs 2 SQL aggregation queries per event push.** This WILL be a bottleneck if the feed has any real traffic.
- **The `from_observation()` factory has a fragile confidence mapping** — hardcoded dict `{"Observed": 1.0, "Derived": 0.7, "Inferred": 0.4}`. Adding a new confidence level silently falls through to 0.5.
- **Event factories are inconsistent**: `suggestion_event()` returns `None` for "nothing to report" but `gap_event()` returns a full event with `title="No capability gaps"`.

**What to do**: Remove salience computation entirely (sort by timestamp). Collapse the 14 separate `push_event()` calls in the daemon into a single batch insert.

### `proactive.py` — ✅ FIXED

Was: variable capture bug in `_get_signal_summary()`, cooldown SQL LIKE fragility.

**Now**:
- Variable capture bug fixed — `_get_signal_summary()` no longer captures `high_sev` by closure from a loop. All accessors (`get_signal_count`, `get_observation_count`, `get_warning_count`) are protected by `threading.Lock`.
- Cooldown check still uses SQL but the query is parameterized and doesn't depend on detail format.
- **Still overlaps with `notification.py`** — both template the same cycle data. This is a P3 consolidation item, not a correctness issue.

### `proactive_reply.py` — ✅ FIXED

Was: module-level `_PENDING` mutable shared state, race condition across concurrent channels.

**Now**:
- `_PENDING` is protected by `threading.Lock`. All access (`handle_reply`, `clear_pending`, `get_pending`) snapshots under lock and releases before executing actions.
- `classify_intent()` LLM call remains but is deferred to P3 optimization.

### `notification.py` — 🟡 HALF-BUILT

- **Duplicate template system**: Has its own templates for events that `proactive.py` also templates. Two files doing the same job.
- **Fifth implementation of "send message → check response"** pattern (after three service poll loops + proactive).
- **Notification dedup is fragile**: uses `count_recent_of_type()` SQL query with a hardcoded 6-hour window. No per-event-type configurability.

### `memory.py` — 🟢 FUNCTIONAL / 🟡 BLEMISHES

The best module in the codebase. `MemoryEngine`, `WorkingMemory`, CRUD, recency decay, LLM extraction, context building — all well-structured.

**But**:
- **`_extract_deterministic()` is regex-as-architecture**: `r"my name is (\w+)"` will match "I'm trying to understand this code" as a name extraction. The `false_positive` tuple is a band-aid, not a solution.
- **No de-duplication across memory keys.** If the LLM extracts `operator_father_name` and `father_name`, you get two conflicting memories. No canonical key resolution.
- **No vector/semantic search.** Despite having a `query` parameter, recall is pure SQL `LIKE %...%`.

### `conversation_learner.py` — 🟡 HALF-BUILT

- **Expensive LLM calls for minimal gain**: every daemon cycle batches up to 50 conversation exchanges and sends them to the LLM to extract 4 fields (name, preferred_technology, preferred_channel, no_notifications). This is calling an LLM on up to 50 exchanges to learn a name you could extract with `re.search(r"my name is (\w+)")`.
- **Memory extraction runs but doesn't mark entries as processed** — the result says `processed=0` with a comment "NOTE: We do NOT mark entries as processed here." This means the next daemon cycle re-processes the SAME entries. Every cycle. You're paying for LLM calls on the same data repeatedly.
- **`_extract_from_llm_response` parses JSON manually** — strips markdown fences, tries `json.loads`, falls back to brace-finding. No structured output / function calling.

### `ask.py` — 🟢 GOOD / 🟡 COMPLEX

The question-answering pipeline. Well-structured with evidence providers, understanding, synthesis. The operator identity routing is solid.

**Issues**:
- **`_SYSTEM` prompt is duplicated**: a static fallback AND a dynamic prompt built from `FRIDAY_PERSONA`. Two paths that can diverge.
- **`_synthesize()` has grown complex**: ~150 lines with ensemble reasoning, learned context injection, persona building, objective contract handling.
- **Over-engineered for most questions**: most `ask()` calls (chitchat, operator identity) go through the deterministic path, but still pay the cost of building the full evidence context.

---

## SERVICES LAYER

### `services/llm.py` — 🟡 HALF-BUILT (see above)

### `services/telegram.py` — 🟢 GOOD

Clean REST API wrapper using `urllib`. `TelegramConfig.from_env()`, `_get_updates()`, `_send_message()`, `_edit_message()`. No dependencies beyond stdlib. Well-structured.

### `services/slack.py` — 🟢 GOOD

Similar structure to Telegram. Uses `slack_sdk` optionally for auth test, falls back to REST. Clean.

### `services/discord.py` — 🟢 GOOD

Pure REST API calls via `urllib`. Same pattern as Telegram. Clean.

### `services/email.py` — 🟡 HALF-BUILT

IMAP/SMTP wrapper. Has observer for inbox polling and executor for sending. But:
- **IMAP polling is fragile**: no connection reuse, no OAuth support, hardcoded port 993.
- **Only handles plain text email**: no HTML rendering, no attachments.
- **Probably never used** — the daemon doesn't start an email poll thread.

---

## RUNTIME LAYER

### `runtime/dispatcher.py` — 🟢 GOOD

Clean, dumb dispatcher. Takes a `RuntimeTask` + `Worker`, calls `worker.execute()`, returns `ExecutionResult`. No planning, no retries, no repair. Exactly what it should be.

### `runtime/confirm_gate.py` — 🔵 OVERENGINEERED / 🟢 FUNCTIONAL

The safety gate. The two-axis (reversibility × blast_radius) and three-axis (+ observation_confidence) matrices are genuinely well-designed. But:

- **~400 lines for what could be 100.** The `_AXIS_TO_LEVEL_3D` matrix has 32 entries, many of which are redundant (NONE confidence maps are identical to LOW). This is a lot of code to say "unknown actions are treated cautiously."
- **`lookup_observation_confidence()` queries the actions table** on every classification. For a daemon running 60s cycles, this is an unnecessary SQL query on every action.
- **The `_ACTION_CLASSIFICATIONS` dict has 35+ entries**, each with a hardcoded `ActionClassification`. For a system that prides itself on determinism, this is a lot of manual classification.

### `runtime/engine.py` — 🟢 GOOD / 🟡 COMPLEX

The execution runtime. Well-structured with `RuntimeEngine.run()`, wave-based execution, verification reconciliation, and proper event logging.

**Issues**:
- **`_persist()` is ~60 lines** doing INSERTs, UPDATEs, snapshots, events, and evolution — all under a lock. This is the single write bottleneck for the entire runtime.
- **Circular import handling is fragile**: `ensure_runtime_bootstrapped` import is inside `__init__` with a blanket `except Exception: pass`. If bootstrapping fails, the runtime silently runs without workers.
- **`_reconcile_verification()` re-derives task states** from artifacts — a good idea, but it runs file-system checks against `self._workspace`. If the workspace changed mid-execution (e.g., git pull), verification may see stale state.

### `runtime/models.py` — 🟢 GOOD

Clean dataclasses for `ExecutionResult`, `RuntimeTask`, `Worker`, `VerificationResult`. State enums (`RunState`, `SessionState`) are minimal and correct.

**Note**: `PythonExecutor` and `ShellExecutor` are marked DEPRECATED in comments but still exported. Dead code. Remove them.

### `runtime/event_bus.py` — 🟢 GOOD

Simple in-process event bus. `EventBus.subscribe()` / `publish()`. No persistence, no ordering guarantees. Appropriate for its use case.

---

## PIPELINE LAYER

### `planning/engine.py` — 🟢 WIRED (was 🟡 HALF-BUILT)

`PlanEngine` with `PlanBuildResult`. ~200 lines of plan generation logic.

**Now**: Plans are generated, compiled into task graphs, resolved against workers, scheduled, and executed via the daemon's `_stage_execution_pipeline()` or the `friday execute --pending` CLI. The pipeline is end-to-end operational.

**Remaining**:
- `_derive()` has both deterministic AND LLM-based milestone generation — two paths that can produce inconsistent plan structures.
- No end-to-end test for the full pipeline (plan → compile → resolve → schedule → execute).

### `planning/graph_engine.py` — 🟢 WIRED (was 🟡 HALF-BUILT)

Compiles a Plan into a task DAG. Has `compile()`, topological sorting, critical path detection.

**Now**: Task graphs are compiled from plans and consumed by the scheduler → resolver → runtime pipeline. The `task_graphs` table's `source` column remains but the suggestion→graph flow is untested.

### `planning/graph_schema.py` — 🟢 GOOD

Clean schema definitions for task graphs. Well-documented. Appropriate for its purpose.

### `planning/patterns.py` — 🟢 GOOD

Pattern classification for engineering goals (rename, extract, refactor, etc.). Deterministic, well-structured, no issues.

### `scheduler/engine.py` — 🟢 WIRED (was 🟡 HALF-BUILT)

`TaskScheduler` with wave-based scheduling, dependency resolution, and cycle detection.

**Now**: Schedules are consumed by `RuntimeEngine.run()` via the daemon's execution pipeline. The `friday execute` CLI provides manual triggering.

**Remaining**: `ScheduleResult.scheduled_at` vs `runs` relationship is confusing — two tables (`scheduler_runs` + `scheduler_history`) tracking similar data.

### `resolver/engine.py` — 🟢 WIRED (was 🟡 HALF-BUILT)

`CapabilityResolver` with `ResolveResult`, symbolic task resolution, strategy-based worker selection.

**Now**: Resolver assignments are consumed by the scheduler → runtime pipeline. Assignments flow from resolution → scheduling → execution.

**Remaining**: `_resolve_symbolic()` mutates task data in-place — side effect hidden inside a function called "resolve."

### `worker/engine.py` — 🟢 GOOD

`WorkerRegistry` with registration, versioning, history tracking, and bootstrap. `ensure_runtime_bootstrapped()` auto-creates built-in workers on first run. Well-structured.

**Caveat**: ~400 lines for what's essentially a CRUD wrapper around the `workers` table. Some complexity from append-only history + version tracking.

### `worker/genesis.py` — 🟡 HALF-BUILT

Worker proposal system for capability gaps. Has `draft_worker()` that creates `proposed_workers` rows. But:
- **Proposals are created but never auto-approved.** They sit at `pending` status unless manually reviewed via `friday worker approve`. The feedback loop is manual.
- **The LLM-based manifest generation** (`_draft_from_goal()`) is powerful but the output has no validation — malformed manifests go into the proposals table.

---

## OBSERVATION LAYER

### `observation/engine.py` — 🟢 GOOD

`ObservationEngine` with `run()`, observer registry, change detection, and formatting. Clean orchestration.

### `observation/interface.py` — 🟢 GOOD

Clean `Observer` abstract class, `Health`, `ObserverHealth` dataclass. Minimal boilerplate.

### `observation/model.py` — 🟢 GOOD

`Observation`, `Change`, `Confidence` dataclasses. Simple and correct.

### `observation/git_observer.py` — 🟢 GOOD

Deterministic git fact collector. No LLM, no state. Reads from disk every time.

### `observation/terminal_observer.py` — 🟢 GOOD

Reads shell history files. Deterministic. Clean.

### `observation/github_observer.py` — 🟡 HALF-BUILT

Fetches GitHub metadata (issues, PRs, CI status). Has a `RepositorySnapshot` model. But:
- **Uses `github` PyPI package** — a dependency that may not be installed. Falls back to REST but the code is tangled.
- **Rate limiting is unhandled**: no backoff, no cache. Hitting GitHub API on every daemon cycle WILL get rate-limited.

### `observation/artifact_observer.py` — 🟢 GOOD

Scans for engineering artifacts (docs, manifests, etc.). Deterministic filesystem walk. Clean.

### `observation/research_observer.py` — 🟢 GOOD

Reads research docs. Deterministic. Clean.

### `observation/calendar_observer.py` — 🟡 HALF-BUILT

Calendar event observer. Has a `CalendarObserver` class but:
- **No actual calendar integration** — it reads nothing. The class structure exists but there's no Google Calendar / CalDAV backend.
- **`CalendarEvent` model is defined but never instantiated.**

### `observation/runtime_observer.py` — 🟢 GOOD

Observes runtime sessions. Deterministic. Clean.

### `observation/workspace_observer.py` — 🟢 GOOD

Observes workspace metadata (directory structure, file counts). Deterministic. Clean.

### `observation/hyprland_observer.py` — 🟢 GOOD

Observes Hyprland window manager state. Deterministic. Clean.

---

## PRESENTATION LAYER

### `presentation/cli_format.py` — 🟢 GOOD

Utility functions for terminal output: `header()`, `green()`, `red()`, `card()`, `status_dot()`, etc. ~200 lines. Clean, well-structured.

### `presentation/models.py` — 🟢 GOOD

Dataclasses for presentation data. Clean.

### `presentation/style.py` — 🟢 GOOD

Style constants (colors, borders). Minimal.

### `presentation/formatters/` — 🟢 GOOD

Execution and knowledge formatters. Deterministic. Clean.

### `presentation/renderers/` — 🟢 GOOD

Mission, knowledge, and shared renderers. Deterministic. Clean.

### `presentation/widgets/` — 🟢 GOOD

Panel, table, timeline, progress, header, footer widgets. All clean.

### `presentation/ambient/` — 🟡 HALF-BUILT

Dashboard, feed_widget, status_panel for the ambient system. Functional but the dashboard uses Rich library widgets extensively. **If Rich isn't installed, the entire dashboard crashes.** No graceful fallback.

### `presentation/command_center.py` — ~~⚪ USELESS~~ **DELETED**

50-line stub. Never meaningfully invoked. **Removed in P0 sprint.**

---

## PERSONA & REASONING

### `persona/engine.py` — 🟢 GOOD

`IdentityEngine` — routes messages through learned context, memory, and conversation history to produce persona-aware responses. Well-structured. ~300 lines.

### `persona/prompts.py` — 🟢 GOOD

Well-written personality prompts (`FRIDAY_PERSONA`, `EVIDENCE_DIRECTIVE`, `UNDERSTANDING_DIRECTIVE`). Clean separation of concerns. ~120 lines.

**Caveat**: The personality is fragmented — each module that calls the LLM (`proactive.py`, `conversation_learner.py`, `reasoning/engine.py`) has its own system prompt that doesn't reference `FRIDAY_PERSONA`.

### `reasoning/engine.py` — ✅ FIXED

Was: 3-model ensemble with 25s latency, Jaccard word overlap confidence, duplicate HTTP impl.

**Now**: Thin wrapper around `services/llm._call()` — single model, no ensemble, no duplicate HTTP. The `EnsembleReasoner` falls through to the single-model `_call()` path in `ask.py._synthesize()`. Confidence is derived from the single model's output. Removed ~$0.20 per `ask()` invocation in API costs and eliminated the 25s timeout bottleneck.

**Total lines removed**: ~80 (ensemble logic, `_call_one()`, `_word_overlap()`, `confidence_label()`).

---

## UTILITY MODULES

### `action_log.py` — 🟢 GOOD

Append-only action event log. `ActionEvent` dataclass, `log_action()`, `query_actions()`. Clean, minimal, correct.

### `sequence_miner.py` — 🟢 GOOD

Deterministic n-gram miner for action sequences. Sessionization, normalization, frequency counting. Well-documented algorithm. ~200 lines. No issues.

### `intent_labeler.py` — 🟡 HALF-BUILT

LLM-based workflow intent labeling. Has LLM path + deterministic fallback. But:
- **LLM call for every pattern** — if the daemon mines 50 patterns in a cycle, that's 50 LLM calls. `_call_llm()` doesn't batch.
- **Deterministic fallback labels are useless**: "workspace_switch × 3" tells you nothing about what the user was doing.

### `context_prompter.py` — 🟢 GOOD

Builds the LEARNED CONTEXT block for LLM prompts. Reads from workflow intents, recent sessions, active projects, operator preferences, memory, conversation history. Well-structured. ~200 lines.

### `cross_project.py` — 🟢 GOOD / 🟡 LAGGING

Two-pass correlation (structural + semantic). Clean pipeline with doc scanning, scoring, and insight creation.

**Issues**:
- **LLM semantic pass is slow**: calls the LLM for every pair above threshold. With 10 repos, that's 45 pairs. With 20 repos, 190 pairs. Doesn't scale.
- **`scan_project_docs()` walks ALL ingested repos on EVERY cycle** — expensive filesystem I/O for repos that haven't changed.

### `skill_formation.py` — 🟢 GOOD

Forms replayable skills from labeled workflow intents. Includes `form_skills()`, `auto_dispatch_skills()`, `detect_skill_drift()`. Well-structured, deterministic where possible, LLM-guided where needed. ~300 lines.

### `autonomous_planner.py` — 🟢 GOOD / 🟡 LAGGING

Creates `ActionPlan` records from daemon cycle findings. Includes skill repair pipeline (diagnose → repair → verify). Well-structured.

**Issues**:
- **`plan_and_dispatch()` calls `detect_skill_drift()` TWICE** — once in the daemon cycle and once in the planner. Redundant.
- **Skill repair can delete formed skills** without operator confirmation when health is "unhealthy" and success rate < 30%. This is an irreversible action below the confirmation gate — it should require CONFIRM level.

### `meta/gap_analyzer.py` — 🟢 GOOD

Analyzes capability gaps from runtime failures. Deterministic. ~280 lines. Clean.

### `meta/si_planner.py` — ⚪ USELESS (imported by tests)

Self-improvement planner. Stub — `_validate_code()` is tested in `test_meta.py` but the module is never called from production code. ~100 lines.

### `meta/sandbox.py` — ⚪ USELESS (imported by tests)

Sandbox for self-improvement experiments. Stub — `Sandbox` class is tested in `test_meta.py` but never called from production code. ~80 lines.

### `meta/deploy.py` — ⚪ USELESS (imported by tests)

Deployment module for self-improvement pipeline. Stub — deploy functions are tested in `test_meta.py` but never called from production code. ~60 lines.

### `meta/verification.py` — ⚪ USELESS (imported by tests)

Verification module for self-improvement. `ReplayVerdict` enum is tested in `test_meta.py` but never used in production. ~70 lines.

### `meta/loop.py` — ~~⚪ USELESS~~ **DELETED**

Stub self-improvement loop. Never called. **Removed in P0 sprint.** `cli_meta.py` cleaned up to remove `_cmd_run_cycle` and the `run_cycle` import.

### `operator/engine.py` — 🟢 GOOD

Operator profile management. `build_operator_profile()`, `should_notify()`, `get_preferred_channel()`. Clean CRUD around `operator_preferences` table. ~200 lines.

### `operator/derivation.py` — 🟡 HALF-BUILT

Derives operator attributes from evidence. Has the structure but the derivation rules are minimal (just name + preferences from the profile). No behavioral derivation (e.g., "user works late nights based on commit timestamps").

### `doctor.py` — 🟢 GOOD

System diagnostics module. Checks binary availability, env vars, DB health. Deterministic. Clean.

---

## CLI LAYER

### `cli.py` — 🟢 GOOD

Main CLI entry point. 50+ subcommands with argparse. Well-organized with per-command imports (lazy loading). ~1100 lines but that's appropriate for the command surface.

**Issues**:
- **The `feed` and `notif` commands are defined inline** in `cli.py` rather than in dedicated `cli_feed.py` / `cli_notif.py`. Inconsistency with the pattern used by all other commands.
- **`cmd_feed()` is ~100 lines** — should be in its own module.

### `cli_daemon.py` — 🟢 GOOD

Clean CLI wrapper around daemon lifecycle functions. ~80 lines.

### `cli_autonomy.py` — 🟢 GOOD

CLI for autonomy controls (status, enable, disable, kill, resume, set, reset). ~160 lines.

### `cli_skills.py` — 🟢 GOOD

CLI for skill management. ~100 lines.

### `cli_dashboard.py` — 🟢 GOOD

CLI for the ambient dashboard. ~100 lines.

### `cli_telegram.py`, `cli_slack.py`, `cli_discord.py` — 🟡 HALF-BUILT

CLI wrappers for service integrations. Each is ~50-80 lines. Functional but minimal — basic read/send commands.

### `cli_email.py` — 🟡 HALF-BUILT

Email CLI. ~50 lines. Minimal.

### `cli_nl.py` — 🟡 HALF-BUILT

Natural language interface CLI. ~50 lines. Minimal wrapper around `ask()`.

---

## KNOWLEDGE PIPELINE

### `knowledge/engine.py` — 🟢 GOOD

`KnowledgeEngine.build()` — deterministic knowledge layer. Well-structured.

### `understanding/engine.py` — 🟢 GOOD

`UnderstandingEngine.build()` — write-only layer on top of knowledge. Clean.

### `initiative/engine.py` — 🟢 GOOD

`InitiativeEngine.build()` — write-only layer on top of understanding. Clean.

### `insight/engine.py` — 🟢 GOOD

`InsightEngine.build()` — ephemeral insights layer. Clean.

---

## UNUSED / DEAD MODULES (Worst Offenders)

| Module | Status | Reason |
|--------|--------|--------|
| `meta/loop.py` | ✅ DELETED | Stub — removed in P0 sprint |
| `meta/si_planner.py` | ⚪ USELESS | Self-improvement planner — stubbed (imported by tests, not removed) |
| `meta/sandbox.py` | ⚪ USELESS | Self-improvement sandbox — stubbed (imported by tests, not removed) |
| `meta/deploy.py` | ⚪ USELESS | Self-improvement deploy — stubbed (imported by tests, not removed) |
| `meta/verification.py` | ⚪ USELESS | Self-improvement verification — stubbed (imported by tests, not removed) |
| `presentation/command_center.py` | ✅ DELETED | 50-line stub — removed in P0 sprint |
| `planning/engine.py` | 🟢 WIRED | Plans generated → compiled → resolved → scheduled → executed via daemon `_stage_execution_pipeline()` |
| `planning/graph_engine.py` | 🟢 WIRED | Task graphs compiled from plans, consumed by scheduler/resolver/runtime in daemon |
| `scheduler/engine.py` | 🟢 WIRED | Schedules consumed by `RuntimeEngine.run()` via `friday execute` and daemon cycles |
| `resolver/engine.py` | 🟢 WIRED | Resolver assignments consumed by scheduler, full pipeline operational |
| `reasoning/engine.py` | ✅ FIXED | Ensemble removed, now thin wrapper around `services/llm._call()` |
| `proactive.py` | ✅ FIXED | Variable capture bug fixed, threading lock added |
| `proactive_reply.py` | ✅ FIXED | `_PENDING` race condition fixed with `threading.Lock` |

---

## TESTS SUMMARY

- **Total test files**: ~110
- **Total tests**: ~1600+
- **Coverage**: Extensive for the M5-M9 pipeline, infrastructure modules, and observation layer
- **Gaps**: 
  - No end-to-end test for the full daemon cycle (events → feed → notification → proactive)
  - No integration test for planning → scheduling → resolution → execution pipeline
  - `meta/*` modules have minimal test coverage (`test_meta.py` tests `gap_analyzer` only)
  - `reasoning/engine.py` has no test coverage
  - `presentation/*` has minimal test coverage

---

## SUMMARY: WHAT TO BURN, WHAT TO KEEP

### 🔥 Burn (useless/stub) — PENDING
1. `meta/si_planner.py`, `meta/sandbox.py`, `meta/deploy.py`, `meta/verification.py` — still stubbed, but imported by `test_meta.py`. Can't delete without test changes. Defer.

### ✅ Fixed (broken — completed in this sprint)
1. `reasoning/engine.py` — ensemble removed, single model via `services/llm._call()`
2. `proactive.py` — variable capture bug fixed, threading lock added
3. `proactive_reply.py` — `_PENDING` race condition fixed

### ✅ Built (half-built — completed in this sprint)
1. **Anaphora resolver** — pronoun resolution, action repeat, more-like-this, etc.
2. **Named protocols** — multi-step macro procedures with variable substitution
3. **Persistent watchers** — monitor conditions, notify when met
4. **Change impact analysis** — what breaks if I modify this file?
5. **Codebase narrative** — git archaeology for project evolution
6. **Planning→scheduler→resolver→runtime pipeline** — wired into daemon cycle + `friday execute` CLI

### 🎯 Still half-built (not started)
1. Add calendar backend to `calendar_observer.py`
2. Fix GitHub rate limiting in `github_observer.py`
3. Batch LLM calls in `intent_labeler.py`
4. Add structured output / function calling to `services/llm.py`

### 🧹 Simplify (overengineered)
1. Remove evolution tables (collapse into generic `layer_history`)
2. Remove salience computation from `ambient.py`
3. Collapse `notification.py` and `proactive.py` into one system
4. Split `db.py` schema into versioned migration files

### ✅ Keep (good)
- `errors.py`, `memory.py`, `db.py` (core), `ask.py` (core)
- `persona/engine.py`, `persona/prompts.py`
- `services/telegram.py`, `services/slack.py`, `services/discord.py`
- `runtime/dispatcher.py`, `runtime/engine.py`, `runtime/models.py`
- `observation/` (all well-structured observers)
- `knowledge/`, `understanding/`, `initiative/`, `insight/` (clean pipeline)
- `skill_formation.py`, `autonomous_planner.py`
- `daemon.py` (post-refactor)
- `cli.py`, `cli_daemon.py`, `cli_autonomy.py`
- `action_log.py`, `sequence_miner.py`, `context_prompter.py`
- **NEW**: `anaphora.py`, `protocol.py`, `watcher.py`, `impact.py`, `narrative.py`

---

## PRIORITY ACTIONS

## COMPLETED IN THIS SPRINT

| Priority | Action | Result |
|----------|--------|--------|
| P0 | Delete 5 `meta/*` stub modules | ✅ `meta/loop.py` deleted; 4 others retained (imported by tests) |
| P0 | Fix `proactive.py` variable capture bug | ✅ Fixed + threading lock added |
| P0 | Fix `proactive_reply.py` thread safety | ✅ `_PENDING` race condition fixed |
| P0 | Delete `presentation/command_center.py` | ✅ Deleted |
| P1 | Remove ensemble from `reasoning/engine.py` | ✅ 25s latency eliminated, single model via `services/llm._call()` |
| P1 | Connect planning→scheduler→resolver→runtime pipeline | ✅ Wired into daemon + `friday execute` CLI |
| P1 | Named protocols / macro procedures | ✅ Built (`protocol.py`) |
| P1 | Persistent watchers | ✅ Built (`watcher.py`) |
| P1 | Anaphora / cross-query follow-through | ✅ Built (`anaphora.py`) |
| P1 | Change impact analysis | ✅ Built (`impact.py`) |
| P1 | Codebase narrative / git archaeology | ✅ Built (`narrative.py`) |

## REMAINING BACKLOG

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| P2 | Add calendar backend to `calendar_observer.py` | 4 hr | Enables actual calendar observation |
| P2 | Fix GitHub rate limiting in `github_observer.py` | 3 hr | Stops API blocks during daemon cycles |
| P2 | Batch LLM calls in `intent_labeler.py` | 2 hr | Reduces LLM costs during pattern mining |
| P2 | Add structured output / function calling to `services/llm.py` | 4 hr | Enables reliable JSON extraction |
| P2 | Split `db.py` schema into migration files | 4 hr | Reduces connection overhead, improves maintainability |
| P2 | Remove evolution tables | 2 hr | Removes 15 unused tables |
| P2 | Remove salience from ambient feed | 1 hr | Removes 2 SQL queries per event push |
| P2 | Semantic code search | 6 hr | Search codebase by meaning, not keywords |
| P3 | Collapse notification + proactive | 1 week | Removes duplicate template system + eliminates double-ping |
| P3 | Voice interface (STT/TTS) | 1-2 weeks | Adds speech interaction modality |
