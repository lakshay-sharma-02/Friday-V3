# Friday Brain — Architecture

> Why each layer exists, not merely what it contains.

This document describes the reasoning core ("the Brain") of Friday V3 as frozen
after the M1–M6 hardening sprints. The architecture is intentionally small and
deterministic. No agents, planners, embeddings, vector databases, graph
databases, or routing abstractions. Future improvement should come from **richer
observation and knowledge**, not from redesigning how Friday thinks.

## Pipeline

```
Question
   ↓
LLM Understanding        (optional; deterministic fallback when no LLM)
   ↓
RetrievalRequirements    (what evidence exists / is needed)
   ↓
Engineering Judgment      (ObjectiveDecision: which engineering question)
   ↓
Evidence Assembly        (providers fill the evidence bag)
   ↓
EvidenceScope guard      (coverage / bias / missing — measured AFTER assembly)
   ↓
Evidence
   ↓
LLM Synthesis            (optional; deterministic text when no LLM)
```

## Layers

### Knowledge layer
**Modules:** `db`, `readme`, `architecture`, `identity`, `summary`, `gitmeta`,
`discovery`.
**Why:** Friday reasons over a *persisted, auditable* model of the workspace, not
over live filesystem crawls per question. Ingestion (`analyze` / `ingest`) turns
repositories into structured rows: purpose, maturity, architecture, components,
entry points, technologies, relationships. This is the only place that touches
the filesystem. Everything downstream reads the knowledge base.

### LLM Understanding
**Module:** `ask.understand` (uses `llm`).
**Why:** Natural-language questions are ambiguous. The LLM maps a free-form
question to a `RetrievalRequirements` (needs, scope, lens, subjects). It is the
*only* step allowed to use an LLM for understanding. When `FRIDAY_LLM_*` is
unset, `requirements_from_question` provides a deterministic offline fallback so
the whole pipeline still works. The LLM never sees the answer or writes evidence.

### RetrievalRequirements
**Module:** `ask` (`requirements_from_question`, `RetrievalRequirements`).
**Why:** A structured contract between understanding and judgment. It names the
*evidence needs* (an open vocabulary: `themes`, `purpose`, `architecture`,
`relationships`, …) and the *span* (`repo` / `compare` / `workspace`). It is
deliberately not an intent enum — needs are descriptive, not a closed question
taxonomy. Judgment consumes this, it does not re-parse the question text.

### Engineering Judgment
**Module:** `objective` (`judge`, `ObjectiveDecision`, `EvidenceScope`).
**Why:** The same evidence bag must answer *different* engineering questions
depending on intent. "What am I building?" and "What themes keep repeating?"
both need themes, but must not collapse into one dump. Judgment maps
RetrievalRequirements → one `Objective` (an answer *shape*: explain, compare,
profile, themes, drift, …), re-ranks needs by how much that objective cares
about each (priority weights), and attaches an answer *contract* (section order).
This is the anti-collapse mechanism and it is pure/deterministic (no LLM).

`EvidenceScope` is a separate axis from the objective: it names the *evidence
span* required (PROJECT / RELATIONSHIP / WORKSPACE / PORTFOLIO / TIMELINE /
OBSERVATION), derived from the objective, never from keywords.

### Evidence Assembly
**Module:** `ask` providers (`_p_*` in `ask.py`), backed by `portfolio`,
`strategy`, `insights`, `observe`, `query`, `tech`.
**Why:** Providers are selected by need and run in objective-priority order.
Each fills part of the evidence bag. The primary need leads; supporting context
is captured in a side channel so it cannot stomp the primary answer. No
orchestrator, no planner — just need∩provider selection.

### EvidenceScope guard (coverage / bias / missing)
**Module:** `evidence_scope` (`build_scope_report`, `coverage_note`,
`build_coverage_report`).
**Why:** The final architectural class of bug was *evidence assembly*, not
routing. A workspace question could silently rest on one repository's describe
dump. After assembly, this guard measures: (1) **coverage** — how many of the
required repositories the evidence actually spans; (2) **bias** — whether one
repo dominates a workspace-wide answer; (3) **missing** — exactly which evidence
kinds are absent per repo. If coverage is thin, the answer states *how many repos
it rests on* rather than pretending completeness. Bias is surfaced, never
fabricated around. These metrics are deterministic and exposed via `--verbose`.

### Evidence
**Module:** `ask.Evidence` (`blocks`, `raw`).
**Why:** The assembled, guarded evidence package handed to synthesis. `raw`
carries the machine-readable verdict (scope, coverage, bias, missing,
coverage_report) so answers are auditable and regressions are testable.

### LLM Synthesis
**Module:** `ask` (answer rendering; offline path is template-based).
**Why:** Turns the evidence into prose. Optional — the offline path renders
deterministic, contract-ordered text. When an LLM is configured, synthesis may
rephrase, but it receives only the assembled evidence, never the question
context as a reasoning crutch.

### Observation
**Module:** `observe` (`M5`).
**Why:** Friday records the workspace as an *append-only* snapshot and reports
only meaningful diffs since the previous run (`friday observe`). This is how
timeline/drift/trend questions get their signal. Observation reads git facts and
stored knowledge; it never interprets or advises. No daemon, no scheduler — only
an explicit run.

### Identity / Portfolio
**Module:** `identity`, `portfolio`.
**Why:** `identity` explains one project like a senior engineer (purpose-first).
`portfolio` synthesizes across projects (themes, strengths, effort, profile) and
is where workspace/portfolio questions are answered as synthesis, not as
per-repo dumps.

## Design invariants (do not break)

- Scope is derived from the **objective**, never from keyword matching on the
  question.
- Workspace/portfolio answers must span the portfolio (coverage guard enforces).
- Reality-check questions must **refuse**, never fabricate (P0).
- Follow-ups resolve against the single previous exchange; that exchange is
  reference context, never evidence for the next turn.
- The LLM is confined to Understanding and (optionally) Synthesis. Judgment and
  the EvidenceScope guard are deterministic and LLM-free so they can be tested
  and frozen.

## Frozen vs. expected to evolve

**Frozen (bug fixes only):** `objective` (judgment + EvidenceScope),
`evidence_scope` (coverage/bias/missing), `ask` provider selection, `readme` /
`architecture` extraction logic, the pipeline itself.

**Expected to evolve (knowledge, not reasoning):** `observe` (more signal
sources — terminal, calendar, CI, GitHub), `db` schema for richer facts, README
ingestion quality, relationship inference, identity cards.

**Does not exist yet (future phases, per roadmap):** worker/orchestration
delegation to external models (Claude Code, Codex, Shell, Pytest, …); long-term
cross-session initiative. These depend on trusting the reasoning core first.
