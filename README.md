# Friday V3 — Persistent AI Operating Partner

Friday is a persistent, ambient AI engineering partner that observes your workspace, understands your engineering context, and executes tasks autonomously.

## Pipeline

```
Reality → Observation → Context → Knowledge → Understanding →
Initiatives → Insights → Brain (ask) → Planning → Task Graph →
Capability Resolver → Scheduler → Runtime → Review → Repair
```

## Quick Start

```bash
friday ingest <repo>     # Ingest a repository
friday ask "what is this project?"  # Ask questions
friday execute "<goal>"  # Execute a task
friday daemon start      # Start the ambient observation daemon
```

## Architecture

- **Deterministic core:** All analysis layers are deterministic (Law 21)
- **Downward-only dependencies:** Higher layers never depend on lower layers (Law 19)
- **Append-only history:** No data is ever deleted, only appended (Law 20)
- **LLM-optional:** LLM is used only for understanding and synthesis; the core pipeline works without it

## Documentation

See the [docs/](./docs) directory for architecture, known limitations, and audit reports.
