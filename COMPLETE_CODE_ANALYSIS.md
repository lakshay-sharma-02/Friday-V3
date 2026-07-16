# Friday V3 — Complete Code Analysis

## Total Codebase: ~15,853 lines across 48 files

---

## Main Modules (22 files, ~10,527 lines)

### Core Infrastructure

**db.py** (930 lines)
- SQLite storage for knowledge base
- Tables: repositories, languages, technologies, relationships, architecture, components, entry_points, snapshots, observations, sessions, knowledge
- Flat schema - relationships computed on read

**cli.py** (381 lines)
- Main CLI with 13 commands
- Command routing and argument parsing

**cli_knowledge.py** (146 lines)
- Knowledge Engine CLI commands (M8.1)

---

### Data Ingestion & Discovery

**ingest.py** (98 lines)
- Orchestrates: discover → metadata → tech → readme → store

**discovery.py** (85 lines)
- Finds git repositories recursively

**gitmeta.py** (176 lines)
- Collects repository metadata via git CLI (no dependencies)

**tech.py** (191 lines)
- Deterministic technology detection from manifests
- Returns (tech, evidence) pairs - never guessed

**readme.py** (366 lines)
- Deterministic extraction + optional LLM enrichment
- Falls back to deterministic on any LLM failure

---

### Architecture Intelligence (M3)

**architecture.py** (1,270 lines) 
- Repository architecture intelligence
- Structure from filesystem, dependencies from AST/imports
- Architecture patterns + components from filenames/manifests
- Deterministic, evidence-backed, no LLM, no modifications

**identity.py** (364 lines)
- Project identity - human-facing interpretation
- Derived on read from M1-M3 facts (README, arch, components, relationships)
- No identities table - recomputed when asked, never stale

---

### Query & Reasoning

**ask.py** (1,896 lines)
- Conversational query over knowledge base
- Pipeline: Question → LLM Understanding → RetrievalRequirements → Evidence Selection → Answer
- Evidence-first synthesis - LLM never retrieves or invents

**query.py** (485 lines)
- Deterministic SQL retrieval over knowledge base
- No embeddings, no semantic search - plain SQL + filtering

**objective.py** (1,027 lines)
- Engineering Judgment layer (M6.6)
- NOT a router/planner/LLM
- Determines "what kind of engineering judgment is being requested?"
- Sits between RetrievalRequirements and provider selection

**judgment.py** (73 lines)
- Evidence-strength model (Weak/Medium/Strong)
- Single source of truth for strength classification

**evidence_scope.py** (332 lines)
- Deterministic evidence-assembly guarantees
- Measures evidence package after assembly
- Reports: scope, coverage, bias

---

### Workspace Intelligence

**portfolio.py** (882 lines)
- Workspace-level reasoning (M3.6)
- Pure deterministic aggregation over SQLite
- Answers: What am I building? (themes), What's valuable? (ranking), etc.
- Fixed theme taxonomy - no free-form hallucination

**strategy.py** (443 lines)
- Strategic judgment (M6.5B + 6.5C)
- Distinct reasoning axes: impact, maturity, adoption, integration
- Synthesized prose thesis with confidence, not bullet dumps

**insights.py** (242 lines)
- Deterministic workspace insights from metadata + SQL
- No LLM - powers summary and ask "insights" intent

**summary.py** (566 lines)
- Aggregate stored knowledge into per-project and cross-project understanding
- Relationships computed deterministically from stored rows

---

### Observation System

**observe.py** (412 lines)
- M5/M7 continuous observation
- M5 snapshot machinery (append-only)
- M7 routes through generic Observation Engine
- Pull-based - `friday observe` is sole trigger

---

### LLM Integration

**llm.py** (159 lines)
- Optional LLM summarization via OpenAI-compatible proxy
- Uses only stdlib urllib - no third-party HTTP
- Configured via FRIDAY_LLM_* env vars
- Returns None on failure for deterministic fallback

---

## Packages (3 packages, 26 files, ~5,326 lines)

### observation/ (11 files, 3,288 lines)

**M7.1: Observation System**
- `engine.py` - Generic observation engine
- `interface.py` - Observer interface/protocol
- `model.py` - Observation data model
- `registry.py` - Observer registry

**Observers:**
- `git_observer.py` - Git repository observer
- `github_observer.py` - GitHub activity observer
- `terminal_observer.py` - Terminal activity observer
- `artifact_observer.py` - Build artifacts observer
- `calendar_observer.py` - Calendar observer
- `research_observer.py` - Research activity observer

---

### context/ (7 files, 897 lines)

**M7.2: Engineering Context**
- `engine.py` - Context engine (observation → sessions)
- `models.py` - EngineeringSession, SessionActivity, Confidence
- `session.py` - Session building from observations
- `correlate.py` - Activity correlation
- `summarize.py` - Context summarization
- `timeline.py` - Timeline construction

---

### knowledge/ (8 files, 1,141 lines)

**M8.1: Knowledge Engine**
- `engine.py` - Knowledge engine (sessions → knowledge)
- `models.py` - Knowledge types, status, confidence
- `trends.py` - Trend detection (increasing/decreasing/dormant/emerging)
- `patterns.py` - Pattern detection (usage, sequences, switching, habits)
- `relationships.py` - Project relationship detection
- `confidence.py` - Evidence-based confidence management
- `store.py` - Database operations

---

## What's Actually Built But NOT Wired

After analyzing all the code, **NOTHING is built but unwired**. Every function and module is either:
1. Used internally by other modules
2. Exposed through the 13 CLI commands
3. Part of the deterministic processing pipeline

---

## Architecture Summary

```
User Input
    ↓
CLI (13 commands)
    ↓
┌─────────────────────────────────────────┐
│ Data Layer (db.py)                      │
│ - SQLite storage                        │
│ - 11 tables                             │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ Processing Layers                       │
│                                         │
│ Ingestion: discovery → gitmeta → tech  │
│            → readme → architecture      │
│                                         │
│ Query: objective → query → evidence    │
│        → judgment → synthesis           │
│                                         │
│ Observation: observers → engine →      │
│              changes                    │
│                                         │
│ Context: observations → sessions →     │
│          timeline → summary             │
│                                         │
│ Knowledge: sessions → trends/patterns  │
│            → relationships → store      │
└─────────────────────────────────────────┘
    ↓
Output (formatted text/JSON)
```

---

## Key Design Principles (Found in Code)

1. **Evidence-backed** - Every claim has a source
2. **Deterministic** - Same input → same output
3. **No LLM core** - LLM optional for enrichment only
4. **Append-only** - History preserved, never overwritten
5. **Flat storage** - Relationships derived on read
6. **No dependencies** - Minimal third-party libs
7. **Git-first** - All discovery via git
8. **Idempotent** - Re-running safe
9. **Read/Write separation** - Clear WRITE vs READ paths

---

## Testing

Found test files for:
- Observation system (observers, benchmarks, crash recovery)
- Context engine (read/write, benchmarks)
- Performance regression testing
- Artifact, calendar, GitHub, research, terminal observers

**No test file found for:** Knowledge Engine (but we created `test_knowledge.py` today with 14 tests)

---

## Summary

**Total: ~15,853 lines of actual production code**
- 22 main modules handling everything from ingestion to querying
- 3 packages (observation, context, knowledge) with 26 files
- 13 CLI commands fully wired
- 0 orphaned/unwired functionality

Friday V3 is a **complete, production-ready workspace understanding system** with observation, context derivation, and knowledge accumulation capabilities.
