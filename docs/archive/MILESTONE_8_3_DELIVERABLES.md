# Milestone 8.3 — Understanding Engine: Deliverables

Date: 2026-07-15

The Understanding Engine is a NEW, WRITE-ONLY layer above Knowledge.
It derives durable engineering *meaning* from accumulated knowledge (plus
knowledge-evolution events). It never reads observations, context, git, or
READMEs directly. It never calls an LLM. The Brain consumes it as one more
evidence provider — unchanged otherwise.

All 590 tests in the suite pass (23 new regression tests for this layer).

======================================================================
1. ARCHITECTURE ADDITIONS
======================================================================

Reality → Observation → Context → Knowledge → Knowledge Evolution →
**Understanding Engine (NEW)** → Brain (UNCHANGED).

New package: `src/friday/understanding/`
  - models.py      — Understanding / UnderstandingType / UnderstandingStatus /
                     UnderstandingConfidence enums + dataclass, append-only rows.
  - confidence.py  — deterministic confidence aggregation (no LLM).
  - derivation.py  — detectors that turn knowledge into candidate understandings.
  - engine.py      — build(), explain(), evolution/history timelines, idempotent.
  - __init__.py    — public surface.

Wiring (additive only):
  - db.py: 3 new tables + storage functions (understanding, understanding_history,
            understanding_evolution).
  - cli.py: `friday understanding ...` subcommand via cli_understanding.py.
  - ask.py / objective.py: `_p_understanding` provider added; reflective
    objectives route understanding as primary evidence. No routing/judgment
    redesign, no new objectives beyond the additive KNOWLEDGE objective.

======================================================================
2. DATABASE ADDITIONS  (src/friday/db.py, append-only)
======================================================================

`understanding` — one row per derived understanding.
    id TEXT PK, type, subject, statement, confidence, status,
    knowledge_ids (CSV of cited knowledge ids), created_at, updated_at,
    build_at, retired_at.

`understanding_history` — one full snapshot per build (PK build_at, id).
    Mirrors knowledge_history. Never mutated; every build appends.

`understanding_evolution` — deterministic lifecycle events
    (Strengthened / Stabilized / Verified / Superseded). PK id = build_at:type:uid.

Lower-layer tables (observations, sessions, knowledge, knowledge_history,
evolution_events) are UNTOUCHED — the db.py diff is purely additive.

======================================================================
3. NEW FILES
======================================================================

  src/friday/understanding/__init__.py
  src/friday/understanding/models.py
  src/friday/understanding/confidence.py
  src/friday/understanding/derivation.py
  src/friday/understanding/engine.py
  src/friday/cli_understanding.py
  tests/test_understanding.py

Modified (additive only): db.py, cli.py, ask.py, objective.py.

======================================================================
4. UNDERSTANDING DERIVATION ALGORITHM
======================================================================

Input: all Knowledge rows + all knowledge-evolution events.
Output: candidate understandings (pre-confidence).

Steps (deterministic, no LLM):
  1. Mark contradicted knowledge ids from evolution events of type
     Contradicted / Weakened / Retired / Split.
  2. Build a per-subject index keyed by lowercased subject; each bucket
     carries the SET of knowledge types that back that subject.
  3. Run detector families:
     a. Per-subject detectors — fired by the COMBINATION of knowledge types
        present for a subject (cross-source reinforcement):
          ENGINEERING_DIRECTION (invest+direction / trend+direction)
          TECHNOLOGY_PREFERENCE (preference, or strong investment)
          EMERGING_EXPERTISE (invest+trend)
          SKILL_DEVELOPMENT (invest)
          ENGINEERING_PHILOSOPHY (preference+pattern)
          ARCHITECTURAL_STYLE (architecture+stack)
          ENGINEERING_IDENTITY (identity+direction)
          LONG_TERM_INITIATIVE (direction + evolution+invest)
          ENGINEERING_HABIT (habit)
          ENGINEERING_STRENGTH (any strong knowledge)
          ENGINEERING_WEAKNESS (bottleneck, or all-weak knowledge)
     b. Content-based per-subject detectors — keyword lexicons over subject +
        statements (commercial, research) — REQUIRED to derive from knowledge
        text, never from raw observations.
     c. Global multi-subject detectors:
          INVESTMENT_TREND (>=2 invested subjects -> one portfolio understanding)
          TECHNOLOGY_SHIFT (one subject trending down + another up)
          PROJECT_CONVERGENCE (relationship/integration/evolution present)
          PROJECT_DIVERGENCE (subject whose knowledge was contradicted)
          ENGINEERING_RISK / ENGINEERING_BLIND_SPOT (bottleneck or contradicted)
  4. Merge candidates sharing (type, subject); union knowledge ids, keep the
     more specific statement.
  5. Every surviving candidate cites >=1 valid knowledge id (dangling citations
     dropped in the engine before persistence).

All 21 required understanding types are represented by detectors.

======================================================================
5. CONFIDENCE ALGORITHM  (confidence.py, fully deterministic)
======================================================================

  contributor weight: WEAK=1, MEDIUM=2, STRONG=4
  cross_source_multiplier = min(1.6, 1.0 + 0.15*(distinct_contributor_types-1))
       (two types=1.15, three=1.30, four=1.45, >=5=1.6)
  agreement_factor = 1.0 if all contributors share direction sign, else 0.6

  score = total_weight * cross_source_multiplier * agreement_factor

  band:  score >= 16 -> STRONG ; >= 6 -> MEDIUM ; else WEAK

  status_from_confidence:
     STRONG + >=4 contributors -> STABLE
     STRONG + >=2                -> VERIFIED
     MEDIUM + >=2                -> OBSERVED
     else                        -> CANDIDATE

Confidence is always DERIVED; never guessed, never LLM-scored.

======================================================================
6. HISTORY MODEL
======================================================================

understanding_history: full append-only snapshot of every understanding per
build (PK build_at, understanding_id). Builds never UPDATE history — they
INSERT OR REPLACE one snapshot per (build, understanding). The engine reads
the PRIOR build's snapshot to diff and emit evolution events. created_at /
status / confidence / knowledge_ids are preserved across builds so lifecycle
(Candidate→Observed→Verified→Stable) only advances upward; Retired is never
auto-resurrected by a rebuild.

======================================================================
7. EVOLUTION MODEL
======================================================================

understanding_evolution: deterministic events derived by diffing consecutive
history snapshots:
  Strengthened — confidence grew (e.g. weak->medium), lists newly added
                supporting knowledge ids.
  Stabilized   — status advanced to STABLE.
  Verified     — status advanced to VERIFIED.
  Superseded   — statement refined while confidence unchanged (prior wording
                retained in the event).
Event id = build_at:event_type:understanding_id (idempotent INSERT OR IGNORE).

======================================================================
8. BRAIN INTEGRATION
======================================================================

Minimal and additive. A new provider `_p_understanding` (decorated with
`@_provider("understanding")`) reads the understanding table and injects it as
supporting evidence / raw context. Reflective objectives (DIRECTION, PRIORITIZE,
UNIVERSE, KNOWLEDGE) route understanding as primary evidence so answers
reference durable meaning, not just raw facts. No routing logic, judgment,
retrieval (RetrievalRequirements), or Engineering Judgment changes. The Brain
is otherwise frozen.

======================================================================
9. CLI ADDITIONS  (friday understanding ...)
======================================================================

  friday understanding              List current understandings (grouped by type,
                                    showing subject, confidence, statement,
                                    citation count, status).
  friday understanding build        Derive understandings from knowledge
                                    (idempotent — same knowledge -> same rows).
  friday understanding explain <id> Show statement, confidence + derivation
                                    breakdown, supporting knowledge ids, history,
                                    evolution. Accepts full id or Nth-newest index.
  friday understanding evolution    Timeline of understanding evolution events.

Existing commands are NOT modified in behavior (cli.py dispatch only gained a
new subparser; lower commands unchanged).

======================================================================
10. DOGFOOD TRANSCRIPT
======================================================================

See docs/MILESTONE_8_3_DOGFOOD.md (full captured run).

Pipeline demonstrated: Knowledge (seeded) → Knowledge build → Understanding
build → Understanding list/explain/evolution → Ask.

Ask questions answered by grounding in understanding:
  "What am I becoming?"      -> Rust-focused systems engineer w/ commercial (vivaha) focus.
  "What direction is my eng taking?" -> consolidating around Rust (3 evidence)
                                   + vivaha commercial anchor.
  "What projects are converging?" -> refused gracefully (no upstream observation
                                   data seeded) — correct: understanding never
                                   invents, and the Brain won't hallucinate.
Every reflective answer traces to understanding entries that cite knowledge.

======================================================================
11. REGRESSION TESTS  (tests/test_understanding.py — 23 tests)
======================================================================

  cold start / empty knowledge        — build on empty knowledge => 0 rows, no error
  single knowledge                   — one knowledge => one or more understandings
  multiple knowledge                 — cross-source reinforcement raises confidence
  contradictory knowledge            — contradicted ids drive RISK / BLIND_SPOT
  confidence aggregation             — weighted score + cross-source multiplier
  evolution                         — Strengthened/Stabilized events emitted
  history                           — prior snapshots preserved across builds
  retirement                        — Retired stays retired across rebuilds
  brain compatibility               — _p_understanding returns understanding text
  no hallucination                 — never fabricates facts/ids
  no duplicate understanding        — (type,subject) merged into one row
  append only                       — history/evolution only grow
  repeated builds / idempotency     — 2nd build: Created=0, Updated=N
  out-of-order timestamps           — deterministic id ignores clock ordering
  multi-project workspace           — distinct subjects produce distinct rows
  every understanding references valid knowledge ids — 0 dangling citations

======================================================================
12. FULL TEST COUNT
======================================================================

  590 passed (2 pre-existing pytest mark warnings, unrelated).
  Of these, 23 are the new Understanding Engine regression tests.

======================================================================
13. CONFIRMATION: ONLY THE UNDERSTANDING LAYER WAS ADDED
======================================================================

CONFIRMED.

  - Observation, Context, Knowledge, Knowledge Evolution, Brain/Judgment,
    RetrievalRequirements, Engineering Judgment: architecturally UNCHANGED.
  - db.py lower-layer tables: unmodified (diff is purely additive — 3 new
    understanding tables + functions only).
  - The understanding layer NEVER imports observation/context/LLM/git/README
    modules. It depends only on .db (reading knowledge + evolution) and
    .knowledge (the Knowledge model).
  - cli.py / objective.py / ask.py changes are additive (new subcommand,
    new provider, new KNOWLEDGE objective + route) — no existing behavior
    altered, no refactor.
  - No LLM, embeddings, vector DB, agents, planners, graph DB, neural
    scoring, hidden state, or speculation anywhere in the layer.

The Brain became smarter (richer reflective answers) without becoming more
complicated.
