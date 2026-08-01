# Friday V3 — Complete Codebase Inventory

**Generated:** 2026-07-30  
**Project:** Friday V3 — Persistent AI Operating Partner  
**Language:** Python ≥3.12  
**Lines:** ~68K (`src/friday/`)  
**Tests:** 1,656 passing / 9 pre-existing failures / 16 skipped  
**Entry:** `friday.cli:main`  
**DB:** SQLite (`~/.friday/friday.db`, 31+ tables, 9 migrations)  

**Architecture Pipeline:**
```
Reality → Observation → Context → Knowledge → Understanding →
Initiatives → Insights → Brain (ask) → Planning → Task Graph →
Capability Resolver → Scheduler → Runtime → Review → Repair
                              ↓
                   Learning Loop: Action Log → Sequence Miner →
                   Intent Labeler → Skill Formation → Auto-Dispatch
                              ↓
                   Self-Evolution: Gap Analyzer → SI Planner →
                   Sandbox → Deploy
```

---

## 🏛️ GROUP 1: Core Architecture & Constitutional Docs

These documents define WHAT Friday IS and the rules by which it must be built. Highest authority in the repo.

| File | Status | Purpose |
|------|--------|---------|
| `docs/CORE_ARCHITECTURE_LAWS.md` | ✅ Current | 25 Architecture Laws — the constitution. Frozen. |
| `docs/ARCHITECTURE.md` | ✅ Current | Architecture overview — pipeline, layers, design invariants |
| `docs/FRIDAY_CORE_FROZEN.md` | ✅ Current | Lists frozen modules & what "bug fix only" means |
| `docs/KNOWN_LIMITATIONS.md` | ✅ Current | 10 intentional design boundaries (single-instance, no retention, etc.) |
| `docs/CURRENT_STATE_ASSESSMENT.md` | ✅ Current | Comprehensive audit as of 2026-07-27 |
| `docs/FRIDAY_SYSTEM_AUDIT.md` | ✅ Current | Pre-Execution-Era red team audit (5 HIGH, 10 MEDIUM, 8 LOW) |
| `docs/REDTEAM_AUDIT.md` | ✅ Current | Post-M6 hardening red team — 596 adversarial questions, 0 hallucinations |
| `docs/HARDENING_SPRINT_REPORT.md` | ✅ Current | Hardening sprint deliverable (transaction safety, crash recovery, perf harness) |
| `docs/CODEBASE_AUDIT.md` | ✅ Current | Broader codebase audit |

---

## 👁️ GROUP 2: Observation Layer (The Senses — 8+ Observers)

**Module:** `src/friday/observation/`  
**Purpose:** Read-only observation of reality. Laws 1, 2 — never infer, never execute.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `observation/engine.py` | ✅ Frozen — transaction-safe observation run |
| **Interface** | `observation/interface.py` | ✅ Frozen — Observer abstract base |
| **Models** | `observation/model.py` | ✅ Frozen — Observation, Change, Confidence |
| **Registry** | `observation/registry.py` | ✅ Current — `default_registry()` |
| **GitObserver** | `observation/git_observer.py` | ✅ Current — watches repos for git changes |
| **TerminalObserver** | `observation/terminal_observer.py` | ✅ Current — captures terminal activity |
| **ArtifactObserver** | `observation/artifact_observer.py` | ✅ Current — detects generated files |
| **HyprlandObserver** | `observation/hyprland_observer.py` | ✅ Current — desktop state (windows, workspaces) |
| **GitHubObserver** | `observation/github_observer.py` | ✅ Current — monitors GitHub activity |
| **ResearchObserver** | `observation/research_observer.py` | ✅ Current — web research observation |
| **CalendarObserver** | `observation/calendar_observer.py` | ✅ Current — Google Calendar (read only) |
| **RuntimeObserver** | `observation/runtime_observer.py` | ✅ Current — watches own execution (Learning Loop) |
| **WorkspaceObserver** | `observation/workspace_observer.py` | ✅ Current — filesystem change detection |

---

## 🧠 GROUP 3: Context Layer

**Module:** `src/friday/context/`  
**Purpose:** Groups observations into sessions and timelines. Law 3 — never infers.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `context/engine.py` | ✅ Frozen — session build with transaction safety |
| **Models** | `context/models.py` | ✅ Frozen — EngineeringSession |
| **Session builder** | `context/session.py` | ✅ Frozen |
| **Timeline builder** | `context/timeline.py` | ✅ Frozen |
| **Summarizer** | `context/summarize.py` | ✅ Frozen |
| **Correlator** | `context/correlate.py` | ✅ Frozen |

---

## 📚 GROUP 4: Knowledge Layer

**Module:** `src/friday/knowledge/`  
**Purpose:** Durable engineering knowledge from evidence. Law 4 — evidence-only.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `knowledge/engine.py` | ✅ Complete — build/query knowledge |
| **Models** | `knowledge/models.py` | ✅ Complete — Knowledge, 10 types, status lifecycle |
| **Confidence** | `knowledge/confidence.py` | ✅ Complete — evidence-driven confidence |
| **Evolution** | `knowledge/evolution.py` | ⚠️ **Bugged** — `knowledge evolution` crashes (missing import) |
| **Execution knowledge** | `knowledge/execution.py` | ✅ Complete |
| **Patterns** | `knowledge/patterns.py` | ✅ Complete |
| **Relationships** | `knowledge/relationships.py` | ✅ Complete — project relationship detection |
| **Static analysis** | `knowledge/static.py` | ✅ Complete — ⚠️ has Law 19 violation (imports identity) |
| **Store** | `knowledge/store.py` | ✅ Complete |
| **Trends** | `knowledge/trends.py` | ✅ Complete |

---

## 💡 GROUP 5: Understanding Layer

**Module:** `src/friday/understanding/`  
**Purpose:** Derived engineering understanding above Knowledge. Law 5 — derived only.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `understanding/engine.py` | ✅ Complete — 21 detector types |
| **Models** | `understanding/models.py` | ✅ Complete |
| **Confidence** | `understanding/confidence.py` | ✅ Complete (triplicated with initiative/insight) |
| **Derivation** | `understanding/derivation.py` | ✅ Complete |

---

## 🎯 GROUP 6: Initiative Layer

**Module:** `src/friday/initiative/`  
**Purpose:** Long-lived engineering direction. Law 6 — never executes.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `initiative/engine.py` | ✅ Complete — 16 initiative types |
| **Models** | `initiative/models.py` | ✅ Complete |
| **Confidence** | `initiative/confidence.py` | ✅ Complete (triplicated) |
| **Derivation** | `initiative/derivation.py` | ✅ Complete |

---

## 🔬 GROUP 7: Insight Layer

**Module:** `src/friday/insight/`  
**Purpose:** Ephemeral cross-workspace pattern detection. Law 7 — disappears if unsupported.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `insight/engine.py` | ✅ Complete |
| **Models** | `insight/models.py` | ✅ Complete |
| **Confidence** | `insight/confidence.py` | ✅ Complete (triplicated) |
| **Derivation** | `insight/derivation.py` | ✅ Complete |

---

## 🧠 GROUP 8: Brain / Ask Pipeline

**Purpose:** The reasoning surface. Law 8 — reason only, never mutate reality.

| Component | File | Status |
|-----------|------|--------|
| **Ask pipeline** | `src/friday/ask.py` | ✅ Frozen — Q&A reasoning pipeline |
| **Objective judgment** | `src/friday/objective.py` | ✅ Frozen — ObjectiveDecision, EvidenceScope |
| **Evidence scope** | `src/friday/evidence_scope.py` | ✅ Frozen — coverage/bias/missing guard |
| **Portfolio** | `src/friday/portfolio.py` | ✅ Frozen — cross-project synthesis |
| **Identity** | `src/friday/identity.py` | ✅ Frozen — project identity model |
| **Strategy** | `src/friday/strategy.py` | ✅ Complete |
| **Architecture analysis** | `src/friday/architecture.py` | ✅ Complete |
| **README parsing** | `src/friday/readme.py` | ✅ Complete |
| **Summary** | `src/friday/summary.py` | ✅ Complete |
| **Discovery** | `src/friday/discovery.py` | ✅ Complete — repo discovery |
| **Git metadata** | `src/friday/gitmeta.py` | ✅ Complete |
| **Query** | `src/friday/query.py` | ✅ Complete |
| **Tech analysis** | `src/friday/tech.py` | ✅ Complete |
| **Vocabulary** | `src/friday/vocabulary.py` | ✅ Complete |
| **Ingestion** | `src/friday/ingest.py` | ✅ Complete — repo ingestion |
| **Judgment** | `src/friday/judgment.py` | ✅ Complete |
| **Synthesis** | `src/friday/synthesis.py` | ✅ Complete |

---

## 📋 GROUP 9: Planning & Task Graph Layer

**Purpose:** Declarative planning. Law 9 — answers "what should happen." Law 10 — Task Graph is the execution IR.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `planning/engine.py` | ✅ Complete |
| **Models** | `planning/models.py` | ✅ Complete |
| **Derive** | `planning/derive.py` | ✅ Complete |
| **Patterns** | `planning/patterns.py` | ✅ Complete |
| **Compiler** | `planning/compiler.py` | ✅ Complete |
| **Graph Engine** | `planning/graph_engine.py` | ✅ Complete |
| **Graph Schema** | `planning/graph_schema.py` | ✅ Complete — versioned schema (only one with schema_version) |
| **Task Graph Schema Doc** | `docs/task_graph_schema.md` | ✅ Current |

---

## 👷 GROUP 10: Worker Registry

**Module:** `src/friday/worker/`  
**Purpose:** Capability metadata. Law 11 — workers never think.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `worker/engine.py` | ✅ Complete — registry + manifest |
| **Models** | `worker/models.py` | ✅ Complete |
| **Genesis** | `worker/genesis.py` | ✅ Complete |

---

## 🔧 GROUP 11: Capability Resolver

**Module:** `src/friday/resolver/`  
**Purpose:** Assigns tasks to workers. Law 12 — single ownership of assignment.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `resolver/engine.py` | ✅ Complete — CapabilityResolver |
| **Models** | `resolver/models.py` | ✅ Complete |
| **Confidence** | `resolver/confidence.py` | ✅ Complete |
| **Resolver** | `resolver/resolver.py` | ✅ Complete — rank/score/select |

---

## ⏱️ GROUP 12: Scheduler

**Module:** `src/friday/scheduler/`  
**Purpose:** Determines ordering. Law 13 — time only.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `scheduler/engine.py` | ✅ Complete |
| **Models** | `scheduler/models.py` | ✅ Complete |
| **State** | `scheduler/state.py` | ✅ Complete |
| **Timeline** | `scheduler/timeline.py` | ✅ Complete |
| **Scheduler** | `scheduler/scheduler.py` | ✅ Complete |

---

## 🚀 GROUP 13: Runtime / Executors (The Hands — 18 Executors)

**Module:** `src/friday/runtime/`  
**Purpose:** Invokes workers. Law 14 — invoke only.

| Component | File | Status |
|-----------|------|--------|
| **Engine** | `runtime/engine.py` | ✅ Complete |
| **Models** | `runtime/models.py` | ✅ Complete |
| **Events** | `runtime/events.py` | ✅ Complete |
| **Event Bus** | `runtime/event_bus.py` | ✅ Complete |
| **History** | `runtime/history.py` | ✅ Complete |
| **State** | `runtime/state.py` | ✅ Complete |
| **Benchmark** | `runtime/benchmark.py` | ✅ Complete |
| **Contract** | `runtime/contract.py` | ✅ Complete |
| **Journal** | `runtime/journal.py` | ✅ Complete |
| **Verification** | `runtime/verification.py` | ✅ Complete |
| **Workers** | `runtime/workers.py` | ✅ Complete |
| **Dispatcher** | `runtime/dispatcher.py` | ✅ Complete |
| **Discovery** | `runtime/discovery.py` | ✅ Complete |
| **Executor** | `runtime/executor.py` | ✅ Complete |
| **Symbolic** | `runtime/symbolic.py` | ✅ Complete |
| **Executors (all 18)** | `runtime/executors.py` | ✅ Complete |
| **Confirm Gate** | `runtime/confirm_gate.py` | ✅ Complete — 3 levels (AUTO/CONFIRM/DOUBLE_CONFIRM) |
| **Browser Executor** | `runtime/browser_executor.py` | ✅ Complete — CDP automation |
| **Hyprland Executor** | `runtime/hyprland_executor.py` | ✅ Complete — WM control |
| **Clipboard Executor** | `runtime/clipboard_executor.py` | ✅ Complete |

**18 Executors wired in:**

| # | Executor | Type |
|---|----------|------|
| 1 | ShellExecutor | Built-in |
| 2 | GitExecutor | Built-in |
| 3 | FilesystemExecutor | Built-in |
| 4 | PythonExecutor | Built-in |
| 5 | TestingExecutor | Built-in |
| 6 | DocumentationExecutor | Built-in |
| 7 | SynthesisExecutor | Built-in |
| 8 | HyprlandExecutor | Desktop |
| 9 | BrowserExecutor | Desktop (CDP) |
| 10 | ClaudeCodeExecutor | AI adapter |
| 11 | CodexExecutor | AI adapter |
| 12 | GeminiExecutor | AI adapter |
| 13 | OpenCodeExecutor | AI adapter |
| 14 | AiderExecutor | AI adapter |
| 15 | DeepSeekExecutor | AI adapter |
| 16 | CLIExecutor | Meta |
| 17 | DynamicWorkerExecutor | Meta (auto-generated) |
| 18 | ReplayExecutor | Meta (formed skills) |

---

## 🔍 GROUP 14: Review & Repair

**Purpose:** Law 15 — Review owns truth. Law 16 — Repair is evidence-driven.

| Component | File | Status |
|-----------|------|--------|
| **Review (module)** | `src/friday/review/__init__.py` | ✅ Complete — evidence-based review |
| **Repair Engine** | `repair/engine.py` | ✅ Complete |
| **Repair Models** | `repair/models.py` | ✅ Complete |

---

## 🛠️ GROUP 15: Services

**Module:** `src/friday/services/`  
**Purpose:** External service integrations

| Service | File | Status |
|---------|------|--------|
| **LLM** | `services/llm.py` | ✅ Complete — LLM proxy |
| **Email** | `services/email.py` | ✅ Complete |
| **Slack** | `services/slack.py` | ✅ Complete |
| **Discord** | `services/discord.py` | ✅ Complete |
| **Telegram** | `services/telegram.py` | ✅ Complete |

---

## 🔄 GROUP 16: Meta-Engine / Self-Evolution

**Module:** `src/friday/meta/`  
**Purpose:** Self-improvement — detects capability gaps, generates workers, deploys with HITL.

| Component | File | Status |
|-----------|------|--------|
| **Capability** | `meta/capability.py` | ✅ Complete |
| **Deploy** | `meta/deploy.py` | ✅ Complete |
| **Gap Analyzer** | `meta/gap_analyzer.py` | ✅ Complete |
| **Sandbox** | `meta/sandbox.py` | ⚠️ Working — thin isolation (git worktree copy) |
| **SI Planner** | `meta/si_planner.py` | ✅ Complete — LLM + deterministic fallback |
| **Verification** | `meta/verification.py` | ✅ Complete |
| **CLI** | `cli_meta.py` | ✅ Complete — `friday upgrade` commands |

---

## 📈 GROUP 17: Learning Pipeline (Action Log → Skill Formation)

**Purpose:** Mines action history → forms reusable skills → auto-dispatches.

| Component | File | Status |
|-----------|------|--------|
| **Action Log** | `src/friday/action_log.py` | ✅ Complete |
| **Sequence Miner** | `src/friday/sequence_miner.py` | ✅ Complete |
| **Intent Labeler** | `src/friday/intent_labeler.py` | ✅ Complete |
| **Skill Formation** | `src/friday/skill_formation.py` | ✅ Complete — forms replayable skills |
| **Context Prompter** | `src/friday/context_prompter.py` | ✅ Complete |
| **CLI Patterns** | `cli_patterns.py` | ✅ Complete |
| **CLI Actions** | `cli_actions.py` | ✅ Complete |
| **CLI Skills** | `cli_skills.py` | ✅ Complete |

---

## 🧑 GROUP 18: Persona & Operator Profile

**Purpose:** User modeling and identity routing.

| Component | File | Status |
|-----------|------|--------|
| **Identity Engine** | `persona/engine.py` | ✅ Complete — human-like routing |
| **Prompts** | `persona/prompts.py` | ✅ Complete |
| **Operator Engine** | `operator/engine.py` | ⚠️ Partial — basic preferences |
| **Operator Models** | `operator/models.py` | ✅ Complete |
| **Operator Derivation** | `operator/derivation.py` | ✅ Complete |
| **Operator Depth** | `operator/depth.py` | ✅ Complete |

---

## 🌐 GROUP 19: Integration & Cross-Project Intelligence

| Component | File | Status |
|-----------|------|--------|
| **Integration Engine** | `integration/engine.py` | ✅ Complete |
| **Cross-Project** | `src/friday/cross_project.py` | ✅ Complete — structural + semantic correlation |

---

## 🤖 GROUP 20: Agentic Action Layer

**Purpose:** Natural language → autonomous agent with step decomposition.

| Component | File | Status |
|-----------|------|--------|
| **Agent Executor** | `src/friday/agent.py` | ✅ Complete — `run_agent()` with self-upgrade routing |
| **Mission Engine** | `src/friday/mission.py` | ✅ Complete — persistent missions, mission → steps |
| **CLI Agent** | `cli_agent.py` | ✅ Complete |
| **Autonomous Planner** | `src/friday/autonomous_planner.py` | ✅ Complete |

---

## 🎙️ GROUP 21: Ambient Intelligence & Proactive Features

**Purpose:** Make Friday feel alive — proactive notifications, ambient feed, memory.

| Component | File | Status |
|-----------|------|--------|
| **Ambient Engine** | `src/friday/ambient.py` | ✅ Complete |
| **Notification** | `src/friday/notification.py` | ✅ Complete |
| **Dashboard CLI** | `cli_dashboard.py` | ✅ Complete |
| **Proactive Engine** | `src/friday/proactive.py` | ✅ Complete |
| **Proactive Reply** | `src/friday/proactive_reply.py` | ✅ Complete |
| **Memory** | `src/friday/memory.py` | ✅ Complete |
| **Conversation Learner** | `src/friday/conversation_learner.py` | ✅ Complete |
| **Sentiment** | `src/friday/sentiment.py` | ✅ Complete |
| **Relationship** | `src/friday/relationship.py` | ✅ Complete |
| **Presence** | `src/friday/presence.py` | ✅ Complete |
| **CLI Status** | `cli_status.py` | ✅ Complete |

---

## 🖥️ GROUP 22: CLI Surface (~45+ Commands)

| CLI File | Commands | Status |
|----------|----------|--------|
| `cli.py` | **Main entry** — routes all subcommands | ✅ Complete |
| `cli_nl.py` | `friday` (natural language dispatch) | ✅ Complete |
| `cli_daemon.py` | `daemon start/stop/status` | ✅ Complete |
| `cli_meta.py` | `upgrade plan/run/list/enable/disable/rollback` | ✅ Complete |
| `cli_planning.py` | `plan`, `plans` | ✅ Complete |
| `cli_knowledge.py` | `knowledge build/list/explain/verify` | ✅ Complete |
| `cli_understanding.py` | `understanding build/evolution` | ✅ Complete |
| `cli_initiative.py` | `initiatives build/timeline` | ✅ Complete |
| `cli_insight.py` | `insights build` | ✅ Complete |
| `cli_runtime.py` | `runtime` | ✅ Complete |
| `cli_worker.py` | `worker register/export` | ✅ Complete |
| `cli_resolver.py` | `resolve` | ✅ Complete |
| `cli_scheduler.py` | `schedule` | ✅ Complete |
| `cli_graph.py` | `graph list/export/explain` | ✅ Complete |
| `cli_strategy.py` | `strategy` | ✅ Complete |
| `cli_review.py` | `review` | ✅ Complete |
| `cli_repair.py` | `repair` | ✅ Complete |
| `cli_observe.py` | `observe`, `observer` | ✅ Complete |
| `cli_identity.py` | `identity` | ✅ Complete |
| `cli_portfolio.py` | `portfolio` | ✅ Complete |
| `cli_execute.py` | `execute` | ✅ Complete |
| `cli_integration.py` | `integrate` | ✅ Complete |
| `cli_skills.py` | `skills list/run` | ✅ Complete |
| `cli_actions.py` | `actions recent` | ✅ Complete |
| `cli_patterns.py` | `patterns` | ✅ Complete |
| `cli_autonomy.py` | `autonomy status/enable/disable` | ✅ Complete |
| `cli_synthesize.py` | `synthesize` | ✅ Complete |
| `cli_email.py` | `email` | ✅ Complete |
| `cli_slack.py` | `slack` | ✅ Complete |
| `cli_discord.py` | `discord` | ✅ Complete |
| `cli_telegram.py` | `telegram` | ✅ Complete |
| `cli_dashboard.py` | `dashboard` | ✅ Complete |
| `cli_agent.py` | `agent` | ✅ Complete |
| `cli_presentation.py` | `present` | ✅ Complete |
| `cli_standup.py` | `standup` | ✅ Complete |
| `cli_guide.py` | `guide` | ✅ Complete |
| `cli_translate.py` | `translate` | ✅ Complete |
| `cli_pr.py` | `pr review` | ✅ Complete |
| `cli_calendar.py` | `calendar` | ✅ Complete |
| `cli_search.py` | `search` | ✅ Complete |
| `cli_sandbox.py` | `sandbox` | ✅ Complete |
| `cli_mission.py` | `mission` | ✅ Complete |
| `cli_telemetry.py` | `telemetry` | ✅ Complete |
| `cli_screen.py` | `screen` | ✅ Complete |
| `cli_status.py` | `status` | ✅ Complete |
| `cli_wait.py` | `wait` | ✅ Complete |
| `cli_impact.py` | `impact` | ✅ Complete |
| `cli_narrative.py` | `narrative` | ✅ Complete |
| `cli_briefing.py` | `briefing` | ✅ Complete |
| `cli_undo.py` | `undo` | ⚠️ Partial — stub? |
| `cli_watch.py` | `watch` | ✅ Complete |
| `cli_profile.py` | `profile` | ✅ Complete |
| `cli_protocol.py` | `protocol` | ✅ Complete |
| `cli_capability.py` | `capability` | ✅ Complete |
| `cli_suggest.py` | `suggest` | ✅ Complete |

---

## ⚙️ GROUP 23: Daemon

**Purpose:** Background ambient loop — observe → learn → notify.

| Component | File | Status |
|-----------|------|--------|
| **Daemon** | `src/friday/daemon.py` | ✅ Complete — background loop, SIGHUP, auto-rollback |
| **Daemon CLI** | `cli_daemon.py` | ✅ Complete |

---

## 🛡️ GROUP 24: Autonomy & Safety

**Purpose:** Graduated autonomy with confirm gates.

| Component | File | Status |
|-----------|------|--------|
| **Autonomy Engine** | `src/friday/autonomy.py` | ⚠️ **Partial** — 3-level confirm gate exists, but: ❌ No kill switch | ❌ No autonomy escalation | ❌ No drift detection | ❌ No rollback undo |
| **Autonomy CLI** | `cli_autonomy.py` | ✅ Complete |

---

## 🖼️ GROUP 25: Communication Integrations

| Component | File | Status |
|-----------|------|--------|
| **Email (CLI + Service)** | `cli_email.py` + `services/email.py` | ✅ Complete |
| **Slack (CLI + Service)** | `cli_slack.py` + `services/slack.py` | ✅ Complete |
| **Discord (CLI + Service)** | `cli_discord.py` + `services/discord.py` | ✅ Complete |
| **Telegram (CLI + Service)** | `cli_telegram.py` + `services/telegram.py` | ✅ Complete |

---

## 📊 GROUP 26: Presentation Layer

**Module:** `src/friday/presentation/`  
**Purpose:** Rich terminal UI, web server, ambient dashboard.

| Component | File | Status |
|-----------|------|--------|
| **Models** | `presentation/models.py` | ✅ Complete |
| **Style** | `presentation/style.py` | ✅ Complete |
| **CLI Format** | `presentation/cli_format.py` | ✅ Complete |
| **HUD** | `presentation/hud.py` | ✅ Complete |
| **Arch Viz** | `presentation/arch_viz.py` | ✅ Complete |
| **Web Server** | `presentation/web_server.py` | ✅ Complete |
| **Reports** | `presentation/reports.py` | ✅ Complete |
| **Formatters/Execution** | `presentation/formatters/execution.py` | ✅ Complete |
| **Formatters/Knowledge** | `presentation/formatters/knowledge.py` | ✅ Complete |
| **Renderers/Mission** | `presentation/renderers/mission.py` | ✅ Complete |
| **Renderers/Execution** | `presentation/renderers/execution.py` | ✅ Complete |
| **Renderers/Shared** | `presentation/renderers/shared.py` | ✅ Complete |
| **Renderers/Knowledge** | `presentation/renderers/knowledge.py` | ✅ Complete |
| **Widgets** (7) | `presentation/widgets/` | ✅ Complete — header, footer, progress, workers, timeline, mission_graph, panels, tables |
| **Ambient Feed Widget** | `presentation/ambient/feed_widget.py` | ✅ Complete |
| **Status Panel** | `presentation/ambient/status_panel.py` | ✅ Complete |
| **Dashboard** | `presentation/ambient/dashboard.py` | ✅ Complete |

---

## 🛟 GROUP 27: Utilities & Supporting Modules

| Component | File | Status |
|-----------|------|--------|
| **Database** | `src/friday/db.py` | ✅ Complete — 31+ tables, migrations, all CRUD |
| **Utils** | `src/friday/utils.py` | ✅ Complete |
| **Errors** | `src/friday/errors.py` | ✅ Complete |
| **LLM Primitive** | `src/friday/llm.py` | ✅ Complete |
| **Sandbox** | `src/friday/sandbox.py` | ✅ Complete |
| **Anaphora** | `src/friday/anaphora.py` | ✅ Complete |
| **Protocol** | `src/friday/protocol.py` | ✅ Complete |
| **CLI Protocol** | `cli_protocol.py` | ✅ Complete |
| **Watcher** | `src/friday/watcher.py` | ✅ Complete |
| **Code Search** | `src/friday/code_search.py` | ✅ Complete |
| **Spontaneous Review** | `src/friday/spontaneous_review.py` | ✅ Complete |
| **Sentiment Detector** | `src/friday/sentiment.py` | ✅ Complete |
| **Relationship Engine** | `src/friday/relationship.py` | ✅ Complete |
| **Reasoning** | `src/friday/reasoning/` | ✅ Complete — EnsembleReasoner |
| **Confidence Calculator** | `src/friday/confidence/` | ✅ Complete |
| **Hyprctl Util** | `src/friday/hyprctl_util.py` | ✅ Complete |
| **Browser Util** | `src/friday/browser_util.py` | ✅ Complete |
| **Doctor** | `src/friday/doctor.py` | ✅ Complete |
| **Project Session** | `src/friday/project_session.py` | ✅ Complete |
| **Screen** | `src/friday/screen.py` | ✅ Complete |
| **Telemetry** | `src/friday/telemetry.py` | ✅ Complete |
| **Build Watcher** | `src/friday/build_watcher.py` | ✅ Complete |
| **Guide** | `src/friday/guide.py` | ✅ Complete |
| **Translate** | `src/friday/translate.py` | ✅ Complete |
| **PR Review** | `src/friday/pr_review.py` | ✅ Complete |
| **Briefing** | `src/friday/briefing.py` | ✅ Complete |
| **Narrative** | `src/friday/narrative.py` | ✅ Complete |
| **Impact** | `src/friday/impact.py` | ✅ Complete |
| **Observe** | `src/friday/observe.py` | ✅ Complete |
| **Worker (exit code 2 test)** | `src/friday/workers/workershell_exit_code_2.py` | ✅ Test worker |
| **DB Migrations** | `src/friday/migrations/sql001` through `sql009` | ✅ Current — schema evolution |

---

## 📁 GROUP 28: Database Migrations

| Migration | Tables | Status |
|-----------|--------|--------|
| `sql001_core_tables.sql` | repositories, languages, technologies, relationships, architecture, components, entry_points | ✅ Applied |
| `sql002_observations_sessions.sql` | observations, sessions | ✅ Applied |
| `sql003_knowledge_understanding.sql` | knowledge, understanding | ✅ Applied |
| `sql004_initiatives_insights.sql` | initiatives, insights | ✅ Applied |
| `sql005_plans_tasks.sql` | plans, tasks | ✅ Applied |
| `sql006_workers_state.sql` | workers, state | ✅ Applied |
| `sql007_resolver_scheduler.sql` | resolver_assignments, scheduler_tasks, scheduler_runs | ✅ Applied |
| `sql008_runtime.sql` | runtime_sessions, runtime_events, runtime_tasks, runtime_results | ✅ Applied |
| `sql009_capability_flags.sql` | capability_flags | ✅ Applied |

---

## ✅ GROUP 29: Tests

**Total:** 130+ test files in `tests/`  
**Status:** 1,656 passing / 9 pre-existing failures / 16 skipped  
**Key test areas:**

| Area | Test Files | Count |
|------|-----------|-------|
| Observation | `test_observe.py`, `test_observation*.py`, observer-specific tests | ~15 |
| Context | `test_context*.py` | ~5 |
| Knowledge | `test_knowledge.py`, `test_knowledge_evolution.py`, `test_knowledge_renderer.py` | ~5 |
| Understanding | `test_understanding.py` | ~2 |
| Initiative | `test_initiative.py` | ~2 |
| Insight | `test_insight.py`, `test_insight_dogfood.py` | ~2 |
| Brain/Ask | `test_ask.py` | ~2 |
| Planning | `test_planning.py`, `test_planning_dogfood.py`, `test_planning_derive_integration.py` | ~4 |
| Graph | `test_graph*.py`, `test_graph_schema.py` | ~4 |
| Worker | `test_worker*.py`, `test_worker_manifest.py`, `test_worker_registry.py` | ~4 |
| Resolver | `test_resolver.py` | ~2 |
| Scheduler | `test_scheduler.py` | ~2 |
| Runtime | `test_runtime*.py` | ~5 |
| Review/Repair | `test_review.py`, `test_repair.py` | ~2 |
| Executor | `test_browser_executor.py`, `test_hyprland_executor.py`, `test_clipboard_executor.py`, `test_execute.py` | ~5 |
| Agent | `test_agent.py`, `test_collaboration.py` | ~2 |
| Meta | `test_meta*.py`, `test_meta_capability.py` | ~3 |
| Skill Formation | `test_skill_formation.py`, `test_cli_skills.py`, `test_skills_pipeline_smoke.py` | ~3 |
| Pattern Learning | `test_patterns.py`, `test_action_log.py`, `test_intent_labeler.py`, `test_sequence_miner.py`, `test_context_prompter.py` | ~5 |
| Autonomy | `test_autonomy*.py`, `test_kill_switch.py`, `test_drift_detection.py`, `test_autonomy_escalation.py`, `test_confirm_gate.py` | ~6 |
| Persona/Identity | `test_identity*.py`, `test_persona_routing.py` | ~3 |
| Services | `test_email.py`, `test_slack.py`, `test_discord.py`, `test_telegram.py`, `test_llm.py` | ~5 |
| CLI | `test_cli_product_surface.py`, `test_capability_cli.py` | ~2 |
| Integration | `test_integration.py`, `test_m816_integration.py`, `test_m815_integration.py` | ~3 |
| Architecture | `test_architecture.py` | ~1 |
| Regression | `test_regressions.py` (+ 12 fixtures in `tests/regressions/`) | ~2 |
| Performance | `test_performance_regression.py` | ~1 |
| Crash Recovery | `test_crash_recovery.py` | ~1 |
| Dogfood | `test_dogfood*.py` | ~4 |
| Others | `test_continuity.py`, `test_discovery.py`, `test_smoke.py`, `test_daemon.py`, `test_db_indexes.py`, etc. | ~20 |

---

## 📖 GROUP 30: Documentation

### Current Docs (actively relevant)
| File | Purpose |
|------|---------|
| `docs/CORE_ARCHITECTURE_LAWS.md` | 25 Architecture Laws — constitution |
| `docs/ARCHITECTURE.md` | Architecture overview |
| `docs/FRIDAY_CORE_FROZEN.md` | Frozen module list |
| `docs/KNOWN_LIMITATIONS.md` | Design boundaries |
| `docs/CURRENT_STATE_ASSESSMENT.md` | Comprehensive state as of 2026-07-27 |
| `docs/observation_architecture.md` | Observer design + contract |
| `docs/context_architecture.md` | Context layer design |
| `docs/knowledge_engine_complete.md` | Knowledge engine deliverable |
| `docs/FRIDAY_SYSTEM_AUDIT.md` | Pre-execution red team audit |
| `docs/REDTEAM_AUDIT.md` | Post-M6 hardening red team |
| `docs/HARDENING_SPRINT_REPORT.md` | Hardening sprint deliverables |
| `docs/task_graph_schema.md` | Task Graph schema reference |
| `docs/PHASE1_AMBIENT_FEED_DESIGN.md` | Ambient + proactive design |
| `docs/superpowers/plans/*.md` | Future plans: capability system, mission control TUI, skill formation |
| `docs/superpowers/specs/*.md` | Specs: capability system, TUI, runtime stabilization, phase 4 exec, skill formation, voice TTS |
| `docs/claude-code-prompts/*.md` | Claude Code prompt packs |

### Stale/Older Docs (milestone completions — informative but historical)
| File | Reason |
|------|--------|
| `docs/MILESTONE_8_1_COMPLETE.md` | Superseded by CURRENT_STATE_ASSESSMENT |
| `docs/MILESTONE_8_3_DELIVERABLES.md` | Superseded |
| `docs/MILESTONE_8_3_DOGFOOD.md` | Superseded |
| `docs/M9_2_5_EXECUTION_READINESS.md` | Superseded |
| `docs/M9_3_CAPABILITY_RESOLVER.md` | Now implemented — historical |
| `docs/M9_4_SCHEDULER.md` | Now implemented — historical |
| `docs/M9_5_RUNTIME.md` | Now implemented — historical |
| `docs/CODEBASE_AUDIT.md` | Superseded by FRIDAY_SYSTEM_AUDIT + CURRENT_STATE_ASSESSMENT |
| `docs/semantic_reasoning_refactor_findings.md` | Historical refactor findings |

---

## 🗑️ STALE / CLEANUP CANDIDATES

### Root-Level Scripts (Loose Files — 32 files)

These are development/debug scripts that should be cleaned up or moved to a `scripts/` directory.

| File | Type | Danger | Recommendation |
|------|------|--------|---------------|
| `calculator.py` | Utility | 🟢 Safe | Move to `scripts/` |
| `fix_tempdirs.py` | Dev tool | 🟢 Safe | Move to `scripts/` |
| `fix_db.py` | Dev tool | 🟢 Safe | Move to `scripts/` |
| `fix_tests.py` | Dev tool | 🟢 Safe | Move to `scripts/` |
| `print_tasks.py` | Debug | 🟢 Safe | Move to `scripts/` |
| `check_statements.py` | Debug | 🟢 Safe | Move to `scripts/` |
| `reset_initiatives.py` | DB tool | ⚠️ **DANGER** — modifies DB | Move to `scripts/` |
| `reset_all_initiatives.py` | DB tool | ⚠️ **DANGER** — modifies DB | Move to `scripts/` |
| `backfill_initiatives.py` | DB migration | ⚠️ **DANGER** — backfills data | Move to `scripts/` |
| `run_audit.py` | Audit | 🟢 Safe | Move to `scripts/` |
| `append_issues.py` | Dev tool | 🟢 Safe | Move to `scripts/` |
| `run_e2e_test.py` | Test | 🟢 Safe | Move to `scripts/` |
| `check_counts.py` | Debug | 🟢 Safe | Move to `scripts/` |
| `check_details.py` | Debug | 🟢 Safe | Move to `scripts/` |
| `test_watch_run.py` | Loose test | 🟢 Safe | Move to `tests/manual/` |
| `test_ensure_fail.py` | Loose test | 🟢 Safe | Move to `tests/manual/` |
| `test_calculator.py` | Loose test | 🟢 Safe | Move to `tests/manual/` |
| `test_claude_worker.py` | Loose test | 🟢 Safe | Move to `tests/manual/` |
| `test_resolver.py` | Loose test | 🟢 Safe | Already has `tests/test_resolver.py`? May be duplicate |
| `test_judgment.py` | Loose test | 🟢 Safe | Already has `tests/test_m6_judgment.py`? May be duplicate |
| `test_goal.py` | Loose test | 🟢 Safe | Move to `tests/manual/` |
| `test_lazy.sh` | Shell test | 🟢 Safe | Move to `scripts/` |
| `generate_image.py` | Utility | 🟢 Safe | Move to `scripts/` |
| `fgh.py` | Debug? | 🟢 Safe | Move to `scripts/` or delete |
| `abc.txt` | Temp file | 🟢 Safe | **Delete** |
| `gr.py` | Debug? | 🟢 Safe | Move to `scripts/` or delete |
| `hello.txt` | Temp file | 🟢 Safe | **Delete** |
| `PLANF.txt` | Notes | 🟢 Safe | **Delete** or archive |
| `friday.db` | SQLite DB | ⚠️ **DANGER** — live DB in repo root | **Move to `~/.friday/`** |
| `.env` | Environment | ⚠️ **SECURITY** — credentials | **Move to `~/.friday/` or .gitignore** |
| `FRIDAY_E2E_TEST.md` | Doc | 🟢 Safe | Move to `docs/` |
| `mission_journal_*.json` (9 files) | Runtime artifacts | 🟢 Safe | **Move to `~/.friday/`** |

### `dogfood_run/` Directory (100+ files)
**Status:** ⚠️ STALE — old test run outputs from milestone dogfooding  
**Size:** ~460KB  
**Recommendation:** 🗑️ **Delete entire directory** — already `.gitignore`d, user confirmed removal previously

### Stale Docs (9 files)
**Status:** Superseded by CURRENT_STATE_ASSESSMENT.md  
**Recommendation:** Move to `docs/archive/` subdirectory

### Workers Directory
| File | Status |
|------|--------|
| `src/friday/workers/workershell_exit_code_2.py` | ✅ Test worker — part of meta-engine |

---

## 🧪 ASSESSMENT BY QUALITY DIMENSION

### ✅ USEFUL — The entire pipeline works end-to-end
| Layer | Score | Notes |
|-------|-------|-------|
| Observation | 9/10 | 8 observers, all wired |
| Context | 8/10 | Frozen, working |
| Knowledge → Understanding → Initiatives → Insights | 8/10 | Pipeline complete, all derived |
| Brain/Ask | 9/10 | Deterministic, 0 hallucinations |
| Planning → Task Graph | 8/10 | Complete execution IR |
| Worker Registry | 8/10 | Capability-based |
| Resolver → Scheduler → Runtime | 7/10 | Wired but execution surface untested at scale |
| Review → Repair | 7/10 | Complete |
| CLI | 9/10 | 45+ commands, every feature reachable |
| Daemon | 8/10 | Background loop, learning pipeline |

### 🎨 PRESENTABLE
| Feature | Score | Notes |
|---------|-------|-------|
| CLI formatting | 8/10 | Rich headers, colors, structured output |
| Presentation widgets | 7/10 | Rich TUI components |
| Mission rendering | 7/10 | Graph visualization |
| Arch visualization | 6/10 | Basic |
| Dashboard | 6/10 | Ambient feed |

### ✨ POLISHED — Battle-tested areas
| Area | Score | Notes |
|------|-------|-------|
| Architecture Laws | 10/10 | Constitution-grade, enforced |
| Frozen module discipline | 9/10 | Strong regression guards |
| Pipeline evidence flow | 9/10 | Dangling citation guards |
| Deterministic core | 9/10 | LLM-optional, pure logic |
| Test coverage | 7/10 | 1,656 tests, but meta/skills thin |
| Crash recovery | 8/10 | Transactions, idempotency |

### 🔧 UNFINISHED / HALF-BUILT
| Area | What's Missing |
|------|----------------|
| **Kill switch** (`friday abort`) | Biggest safety gap |
| **Drift detection** | Skills can silently degrade |
| **Autonomy escalation** | CONFIRM→AUTO on sustained success |
| **Multi-platform desktop** | Hyprland only — no Windows/macOS/GNOME/KDE |
| **Browser automation** | No form fill, no sessions, no multi-tab |
| **Calendar** | Read only — can't create events |
| **Skill quality** | No feedback loop for skill improvement |
| **Operator profile** | Basic preferences only, no deep user modeling |
| **Communication** | Calendar reads only — no email/messaging outbound |

### 💔 BROKEN / KNOWN DEFECTS
| Issue | Severity | Status |
|-------|----------|--------|
| `friday knowledge evolution` crashes | 🔴 HIGH | Unfixed — missing import |
| `observations` table has no PK | 🔴 HIGH | Unfixed — duplicate rows |
| Knowledge verification inflates on re-build | 🔴 HIGH | Unfixed — false confidence |
| Context build not idempotent | 🔴 HIGH | Unfixed — duplicate sessions |
| No FK on cross-layer refs | 🔴 HIGH | Unfixed — orphan rows |
| Understanding status regresses on rebuild | 🟡 MEDIUM | Unfixed |
| Capability vocabulary drift (lowercase vs Capitalized) | 🟡 MEDIUM | Partially mitigated |
| Triplicated confidence algorithm | 🟡 MEDIUM | Code quality |
| 9 pre-existing test failures | 🟡 MEDIUM | Timing/perf/discovery |

### ❌ DOES NOT EXIST (Major Gaps)
| Gap | Impact |
|-----|--------|
| Cross-platform system control | Desktop control locked to Hyprland |
| Full-text search | No file content search |
| IDE integration | No VS Code/IntelliJ/LSP |
| Network/remote | No SSH, cloud APIs, webhooks |
| Mobile presence | Desktop only |
| Hardware control | No camera/mic/USB/smart home |
| Docker/K8s integration | No container management |

---

## 📐 ARCHITECTURE HEALTH SUMMARY

| Dimension | Score |
|-----------|-------|
| Pipeline completeness | 9/10 |
| Test coverage | 7/10 |
| Code quality | 8/10 |
| Desktop control | 3/10 |
| Learning pipeline | 7/10 |
| Production readiness | 6/10 |

**Confirmed Law violations (from audit):**
1. **Law 19:** `knowledge/static.py` imports `..identity` (Brain tier) — circular dependency hack
2. **Law 24:** Plan, Worker Manifest, Knowledge, Understanding, Initiative, Insight lack `schema_version`
3. **`validate_task_graph` never called** — contract unenforced
4. **3 divergent capability vocabularies** — compiler, worker, graph_schema all have their own list
