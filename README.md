# Friday V3 — Persistent AI Operating Partner

Friday is a persistent, ambient AI engineering partner that observes your workspace,
understands your engineering context, learns your patterns, and executes tasks
autonomously.

## What It Does

```
Reality → Observation → Context → Knowledge → Understanding →
Initiatives → Insights → Brain (ask) → Planning → Task Graph →
Capability Resolver → Scheduler → Runtime → Review → Repair
```

**The Brain:** Full deterministic reasoning pipeline — knowledge, understanding,
planning, execution, repair. All layers are layered, testable, and follow the
frozen Architecture Laws.

**The Senses:** 8 observers that watch your repos (Git), terminal, desktop
(Hyprland), GitHub, calendar, web research, file artifacts, and Friday's own
execution.

**The Hands:** 18 executors — shell, git, filesystem, Python, testing,
documentation, synthesis, Hyprland WM control, browser (CDP), Claude Code,
Codex, Gemini, OpenCode, Aider, DeepSeek, dynamic auto-generated workers,
and formed skill replay.

**The Learning Loop:** Action Log → Sequence Miner → Intent Labeler →
Skill Formation → Auto-Dispatch. Friday learns your patterns and replays
them without retraining.

**Self-Improvement:** Meta-engine detects capability gaps, generates worker
code via LLM, sandbox-tests it, and deploys with human-in-the-loop approval.

## Quick Start

```bash
# Ingest a repository
friday ingest /path/to/repo

# Ask questions about your workspace
friday ask "what is this project?"
friday ask "how do the repos relate?"

# Execute a task
friday execute "add logging middleware"

# Start the ambient daemon
friday daemon start

# View and manage skills
friday skills list
friday skills run <name>

# Check and manage autonomy
friday autonomy status
friday autonomy enable

# View action history
friday actions recent
```

## CLI Commands

| Command | Purpose |
|---------|---------|
| `friday ask` | Conversational queries about workspace |
| `friday ingest` | Ingest a repository |
| `friday observe` | Run observation cycle |
| `friday context` | Build workspace context |
| `friday plan` | Generate engineering plans |
| `friday graph` | Generate task graphs |
| `friday execute` | Execute a task or goal |
| `friday runtime` | Run a scheduled mission |
| `friday repair` | Review and repair failed executions |
| `friday daemon` | Start/stop/manage the background daemon |
| `friday skills` | List and invoke formed skills |
| `friday patterns` | Mine and label action patterns |
| `friday actions` | View raw action log |
| `friday autonomy` | Manage graduated autonomy permissions |
| `friday meta` | Self-improvement (gap analysis, worker generation) |
| `friday knowledge` | View knowledge base |
| `friday understanding` | View understanding derivations |
| `friday initiatives` | View engineering initiatives |
| `friday insights` | View cross-project insights |
| `friday observer` | Manage observers |
| `friday worker` | Manage worker registry |
| `friday review` | Review and approve plans/graphs |
| `friday integrate` | Cross-project integration analysis |

## Architecture

- **Deterministic core:** All analysis layers are deterministic (Law 21)
- **Downward-only dependencies:** Higher layers never depend on lower layers (Law 19)
- **Append-only history:** No data is ever deleted, only appended (Law 20)
- **LLM-optional:** LLM is used only for understanding and synthesis; the core pipeline works without it
- **25 Architecture Laws:** Constitutional framework for all development

## Test Status

**1656 passing, 9 pre-existing failures, 16 skipped** (as of 2026-07-27).

All 9 remaining failures are pre-existing (timing-dependent performance tests,
operator profile CLI isolation, discovery/patterns). No active regressions.

## Documentation

See the [docs/](./docs) directory for architecture, laws, known limitations,
audit reports, and current state assessment.
