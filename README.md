# Friday V3

Persistent workspace-understanding operating partner — analyzes repos, answers questions about your engineering landscape, and executes structured tasks.

## Install

```bash
pip install -e .
```

Requires Python ≥ 3.12. Dependencies install automatically (`rich` for TUI rendering).

## Quick start

```bash
# Ingest repositories into the knowledge base
friday ingest /path/to/projects

# Ingest the current directory
friday ingest .

# Ask questions (no LLM required for basic queries)
friday ask "what is this project"
friday ask "which projects use Python"
friday ask "what are the most active projects"

# Interactive chat
friday chat

# Observe workspace state and refresh knowledge
friday observe --summary

# Show project identities
friday identity
friday identity <project-name>

# Execute a goal end-to-end
friday execute "create a file named hello.txt containing 'hello world'"
```

## Commands

| Command | Description |
|---------|-------------|
| `friday ingest <paths>` | Scan directories and store repository knowledge |
| `friday summary` | Print workspace knowledge summary |
| `friday ask "<question>"` | Ask a question about your projects |
| `friday chat` | Interactive conversational loop |
| `friday analyze <repo>` | Extract and persist architecture knowledge |
| `friday observe [--summary]` | Refresh the workspace knowledge stack |
| `friday observers` | List all registered observers |
| `friday identity [project]` | List/explain project identities |
| `friday portfolio [themes\|overlap\|ranking]` | Workspace reasoning |
| `friday strategy [impact\|platform\|learning]` | Strategic judgment |
| `friday plan <goal>` | Generate an engineering plan |
| `friday execute <goal>` | Plan → resolve → schedule → run |
| `friday workers` | List registered worker capability profiles |
| `friday capability [discover\|list]` | Capability discovery |
| `friday audit` | Show why each repo has weak evidence |
| `friday doctor` | Check system health |

## How it works

Friday builds a persistent knowledge base from your repositories:
1. **Ingest** — reads repo structure, READMEs, languages, git history
2. **Knowledge** — accumulates engineering patterns and facts
3. **Understanding** — derives long-term direction, philosophy, effort
4. **Planning** — generates task graphs from goals
5. **Resolve** — assigns tasks to workers (local shell/python/testing, or AI CLIs)
6. **Execute** — runs the plan via native executors

No LLM is required for basic question-answering. An LLM (Claude, GPT, etc.) can optionally be configured for richer synthesis via `FRIDAY_LLM_MODEL`.

## Design

Deterministic core with optional LLM augmentation. Knowledge is append-only and idempotent. Workers are capability-scored from a registry — no hardcoded routing.
