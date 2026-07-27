# Current State Assessment — Friday V3

**Date:** 2026-07-27
**Source:** Codebase audit, live test runs, git history, conversation thread.
**Scope:** Full stack, Reality → Runtime → Repair → Learning, including working tree.

---

## 1. Project Identity

| Property | Value |
|----------|-------|
| Name | `friday` v0.1.0 |
| Description | "Persistent AI operating partner: workspace understanding" |
| Language | Python ≥3.12 (hatchling build) |
| Size | ~68K lines Python (`src/friday/`) |
| Tests | **1656 passing, 9 known-pre-existing failures, 16 skipped** (last full run 2026-07-27) |
| Entry | `friday.cli:main` (`friday` CLI binary) |
| DB | SQLite via `~/.friday/friday.db` (31+ tables) |

---

## 2. Pipeline Status

```
Reality → Observation → Context → Knowledge → Understanding →
Initiatives → Insights → Brain (ask) → Planning → Task Graph →
Capability Resolver → Scheduler → Runtime → Review → Repair
```

**Every layer is built and wired.** The full pipeline has been verified end-to-end through smoke tests and a Phase 1 execution run against a real repo (Vivaha).

---

## 3. What Exists (Full Detailed Map)

### The Brain (Reasoning Pipeline) — COMPLETE
- Knowledge Engine (10 knowledge types, confidence lifecycle)
- Understanding Engine (21 detector types, derivation)
- Initiative Engine (16 initiative types, confidence aggregation)
- Insight Engine (cross-workspace pattern detection)
- Planning Engine (deterministic plan derivation)
- Task Graph (evidence-grounded task generation)
- Worker Registry (capability-based resolution)
- Capability Resolver (scored worker matching)
- Scheduler (dependency-aware wave scheduling)
- Runtime (execution with fallback chains)
- Review & Repair loops

### The Senses (Observers) — 8 total
- `GitObserver` — watches repos for changes
- `TerminalObserver` — captures terminal activity
- `ArtifactObserver` — detects generated files
- `HyprlandObserver` — watches desktop state (windows, workspaces, apps)
- `GitHubObserver` — monitors GitHub activity
- `ResearchObserver` — web research
- `CalendarObserver` — Google Calendar integration
- `RuntimeObserver` — watches its own execution (closes Learning Loop)

### The Hands (Executors) — 18 total
- 7 built-in executors: Shell, Git, Filesystem, Python, Testing, Documentation, Synthesis
- 2 desktop executors: HyprlandExecutor (WM control), BrowserExecutor (CDP automation)
- 6 AI adapters: Claude Code, Codex, Gemini, OpenCode, Aider, DeepSeek
- 3 meta executors: CLIExecutor (base), DynamicWorkerExecutor (auto-generated), ReplayExecutor (formed skills)
- **All 18 executors wired into action_log + record_action_outcome** for autonomy escalation

### The Learning Pipeline (Pillar B) — WIRED END-TO-END
- Action Log → Sequence Miner → Intent Labeler → Skill Formation → Auto-Dispatch
- Daemon runs this every cycle automatically
- Formed skills can replay their workflows via ReplayExecutor

### Self-Improvement (Meta-Engine)
- Gap Analyzer: detects capability gaps from execution failures
- SI Planner: LLM generates worker code to fill gaps
- Sandbox: tests generated code in isolation
- Deploy/Approve/Promote: human-in-the-loop with staged rollout

### Cross-Project Intelligence
- Structural correlation (language overlap, tech stacks, config patterns)
- Semantic correlation (LLM-based conceptual overlap discovery)
- Surfaces correlations as Insights

### The Daemon
- Background observation loop with polling and SIGHUP triggers
- Runs: observe → knowledge → understand → initiate → insight → patterns → intents → skills → auto-dispatch
- Desktop notifications for findings
- Filesystem change detection triggers immediate cycles

### 40+ CLI Commands
- `friday ask`, `friday ingest`, `friday observe`, `friday context`
- `friday plan`, `friday graph`, `friday resolve`, `friday schedule`
- `friday runtime`, `friday execute`, `friday repair`
- `friday daemon`, `friday integrate`, `friday meta`
- `friday patterns`, `friday skills`, `friday autonomy`
- `friday knowledge`, `friday understanding`, `friday initiatives`, `friday insights`
- `friday observer`, `friday worker`, `friday review`
- `friday actions` (raw action log visibility)

### Graduated Autonomy — PARTIALLY BUILT
- ✅ 3-level confirm gate (AUTO / CONFIRM / DOUBLE_CONFIRM) with per-action-type mapping
- ✅ `friday autonomy enable/disable` CLI
- ✅ `friday autonomy status` to view permissions
- ✅ `record_action_outcome()` in ALL 18 executors (success/failure feeds confidence tracking)
- ⬜ No kill switch for runaway agents
- ⬜ No rollback/undo mechanism
- ⬜ No autonomy escalation (CONFIRM→AUTO on sustained success)
- ⬜ No per-user permission overrides (currently hardcoded in `_ACTION_LEVELS`)

---

## 4. Phase 0 — Foundation Fixes (Completed 2026-07-26)

| Area | Files Changed | Root Cause |
|------|-------------|-----------|
| **Compiler creation-task handling** | `planning/compiler.py` | `TaskType.DEPLOYMENT` missing from `_CREATION_TASK_TYPES`. Both compile paths now downgrade creation tasks without real file paths to ANALYSIS. |
| **Verification leniency** | `runtime/verification.py` | Empty `task_type` fields on mock tasks hit "planning gap". Added `""` to `_NON_ARTIFACT_TASK_TYPES`. Added `task_outputs` check for generic outputs. |
| **Resolver test isolation** | `tests/test_runtime_stabilization.py` | `mock.patch.dict('_AI_BINARY_MAP', clear=True)` was broken — empty map allowed ALL workers through. Switched to `mock.patch('shutil.which')`. |
| **Action log wiring** | `runtime/executors.py` | All 9 remaining executors wired with `_autonomy_record_outer()` calls at every return point. |
| **Missing import** | `runtime/executors.py` | `normalize_worker_input` was called in `DynamicWorkerExecutor` but never imported. Added `from ..worker.models import normalize_worker_input`. |

**Test fix results:**

| Metric | Before | After |
|--------|--------|-------|
| Tests passing | **1,628** | **1,656** |
| Tests failing | **37** | **9** |
| Tests fixed | — | **28** |

**9 remaining failures are all pre-existing** — performance regression tests (timing-dependent), operator profile CLI tests, and unrelated discovery/patterns tests. None are caused by our changes.

---

## 5. Half-Built Areas

| Area | What's There | What's Missing |
|------|-------------|---------------|
| **Desktop control** | Hyprland WM only | No Windows/macOS/GNOME/KDE support. No multi-monitor orchestration. |
| **Browser automation** | CDP navigate/click/type/read | No form auto-fill, no cookie/session management, no multi-tab control, Brave/Chrome only. |
| **Calendar** | CalendarObserver reads events | Can't create events, can't auto-schedule tasks into free slots |
| **Pattern learning** | Mines sequences, labels intents, forms skills | No drift detection — skills can degrade and Friday won't notice. No feedback loop to refine old skills. |
| **Skill confidence** | Per-step exemplar distributions | Can't learn which skills actually help the user. No quality scoring. |
| **Meta-engine** | LLM generates worker code | Generated code often fails validation. Sandbox is thin. Engine can't improve itself. |
| **Operator profile** | Preferences + patterns | No deep user modeling. No automatic preference discovery. |
| **Git integration** | Reads git state | No PR management, no code review automation, no CI/CD integration |
| **Self-monitoring** | Action log exists | No anomaly detection, no "Friday seems slow" introspection |

---

## 6. Known Issues (from KNOWN_ISSUES.md)

**26 items tracked.** State breakdown:

| State | Count | Items |
|-------|-------|-------|
| FIXED | 10 | #1, #7, #11, #12, #13, #15, #18, #20, #21, #22, #26 |
| DOCUMENTED | 4 | #8, #10, #14, #24 |
| RESOLVED | 2 | #9, #19 |
| SKIPPED | 2 | #23, #25 |
| OPEN (LOW/MEDIUM) | 4 | #4, #5, #6, #16 |
| TESTING | 1 | #17 |
| NEW (post-Phase-0) | 3 | #27 (9 pre-existing test failures), #28 (mocked executor tests), #29 (no isolation for auto-worker tests) |

**No OPEN HIGH-severity issues.**

---

## 7. What Doesn't Exist (Major Gaps)

These are genuine missing capabilities — not half-built, not documented limitations:

1. **Cross-platform system control** — Hyprland only. No Windows/macOS/GNOME/KDE.
2. **Communication integrations** — Calendar reads only. No email, messaging, meeting, or voice.
3. **File system intelligence** — No full-text search, no auto-organization, no inotify/watchdog.
4. **Network & remote access** — No SSH, no cloud APIs, no webhooks, no DB client.
5. **IDE & dev tool integration** — No VS Code/IntelliJ extension, no LSP, no Docker/K8s.
6. **Proactive intelligence** — Reacts on cycles, doesn't anticipate needs or show info unprompted.
7. **Drift detection** — Skills can silently degrade. No mechanism to notice.
8. **Kill switch** — No way to stop a runaway agent mid-execution.
9. **Rollback/undo** — "This action cannot be undone" in confirm_gate is literal.
10. **Mobile presence** — Desktop only. No phone app, no push notifications.
11. **Hardware control** — No smart home, no USB, no camera/mic.

---

## 8. Verified Claims (Corrected Audit)

| Claim in Original Audit | Actual Source Check | Verdict |
|------------------------|-------------------|---------|
| 8 observers in `default_registry()` | `observation/registry.py:23-48` | ✅ Correct |
| 17 executors | `executors.py` + `hyprland_executor.py` + `browser_executor.py` + `skill_formation.py` | ⚠️ Actually **18** — undercounted `ReplayExecutor` |
| "Confirm gate is binary" | `confirm_gate.py:_ACTION_LEVELS` | ❌ **Wrong — 3 levels** (AUTO/CONFIRM/DOUBLE_CONFIRM) with per-action-type granularity |
| "No drift detection" | `grep -r consecutive_fail src/friday/` → empty | ✅ Correct — does not exist |
| "No kill switch" | `grep -r kill_switch\|emergency_stop src/friday/` → empty | ✅ Correct — does not exist |
| "No undo/rollback" | `grep 'cannot be undone' confirm_gate.py` → present | ✅ Correct — literal |
| "Meta-engine often fails" | `generate_worker_code` has `max_attempts=3` | ⚠️ **Overstated** — no empirical data for "often" |
| "18 executors exist" | Full count of executor classes | ✅ Correct (after correction from 17) |

---

## 9. Architecture Health

| Dimension | Score | Notes |
|-----------|-------|-------|
| Pipeline completeness | **9/10** | Every layer built and wired. Single end-to-end run done. |
| Test coverage | **7/10** | 1656 passing. 9 pre-existing failures. Meta/skill-formation tests are thin. |
| Code quality | **8/10** | Deterministic core, downward-only deps, append-only history. Frozen module discipline holds. |
| Desktop control | **3/10** | Hyprland only. No cross-platform abstraction. |
| Learning pipeline | **7/10** | Wired end-to-end but no drift detection or quality feedback. |
| Production readiness | **6/10** | Foundation is solid. Missing: kill switch, undo, configurable permissions, drift detection. |

---

## 10. Recommendations

### Immediate next steps (highest impact per effort)

1. **Build kill switch** (`friday abort`) — the biggest safety gap. One afternoon of work, unlocks real unattended execution.
2. **Add drift detection** — query replay log for distribution shifts against exemplars. Small change, prevents silent skill degradation.
3. **Build autonomy escalation** — promote workers from CONFIRM→AUTO after N consecutive successes. Uses the `record_action_outcome` data already being collected.
4. **friday execute against a real multi-step goal** — let it fail, learn from the failure. The last Phase 1 run only tested a single goal.

### After those (expand surface area)
5. **Cross-platform desktop executors** — PowerShell (Windows), osascript (macOS)
6. **Communication layer** — email client, messaging SDK
7. **Network & remote** — SSH executor, webhook listener

### Housekeeping
- [ ] Track fate of 10 MEDIUM audit items from the original system audit
- [ ] Migrate root-level test files (`test_calculator.py`, `test_claude_worker.py`)
- [ ] Remove or archive `dogfood_run/` directory
