# Friday Core — FROZEN

**Status: FROZEN as of 2026-07-14.**

The core is the foundation. It is now locked. This document is the contract that
keeps us from rewriting the foundation every few weeks.

## Rule

- **Frozen modules accept bug fixes only.**
- **No new abstractions, no refactors, no API/behavior changes** without an
  explicit architecture review (write-up + sign-off).
- **All new capability lands in the extension layers**, not the core.

If a feature "needs" a core change, that is a signal the design is wrong — solve
it in an extension layer instead.

## Frozen modules

| Area            | Module(s)                                                                 | Why frozen                                                                 |
|-----------------|---------------------------------------------------------------------------|----------------------------------------------------------------------------|
| Brain           | `ask.py` (reasoning pipeline)                                             | The synthesis + retrieval-orchestration path. Stable contract for answers. |
| Retrieval       | `evidence_scope.py` (RetrievalRequirements, EvidenceScope, coverage)      | Defines what evidence is sufficient. Changing it ripples across every ask. |
| Judgment        | `portfolio.py`, `evidence_scope.py` judgment surfaces                     | Engineering-judgment scoring. Frozen so evaluations stay comparable.       |
| Identity         | `identity.py`                                                            | Project identity model. Frozen so downstream consumers stay consistent.    |
| Observation Engine | `observation/` (`engine.py`, `interface.py`, `registry.py`, `git_observer.py`, `model.py`) | Deterministic fact collection. Frozen interface = observers plug in freely. |
| Context         | `context/` (`engine.py`, `session.py`, `correlate.py`, `timeline.py`, `summarize.py`, `models.py`) | Engineering-work derivation above observations. Frozen read/write split.   |

The Observation Engine and Context engines are frozen **including their public
method signatures** (`ObservationEngine.run`, `ContextEngine.build`/`sessions`/
`session`/`timeline`/`summary`/`is_stale`). Adding an observer or a new context
signal happens *inside* those modules' extension points, never by changing the
engine's contract.

## Extension layers (where future work goes)

New work belongs almost entirely here. These are intentionally NOT frozen — they
are where Friday grows.

| Layer        | Path                  | Status      | Purpose                                                        |
|--------------|-----------------------|-------------|----------------------------------------------------------------|
| Observation  | `src/friday/observation/` | exists   | New observers (Terminal, GitHub, Browser, Calendar, Filesystem) register via `ObserverRegistry`. Engine untouched. |
| Knowledge    | `src/friday/knowledge/`   | exists (`knowledge.py`) | Ingestion + knowledge storage. Expand without touching the Brain. |
| Context      | `src/friday/context/`     | exists   | Engineering-work derivation. New correlation signals stay observer-agnostic. |
| Workers      | `src/friday/workers/`     | **not yet created** | Future: background-style units of work that consume Observations/Context. Create when needed; do not bolt onto the core. |

When a new observer or worker appears, it plugs into the existing engine via the
registered interface. The core does not change.

## What "bug fix only" means

Allowed in frozen modules:
- Fix a crash / exception path.
- Fix incorrect output for an existing input (a real bug, not a "would be nicer").
- Fix a security or data-loss issue.
- Trivial, behavior-preserving typo/doc fixes.

NOT allowed without architecture review:
- New public function, class, or parameter on a frozen engine/interface.
- New abstraction layer, helper module, or "manager".
- Changing a frozen method's signature or return type.
- Moving logic between frozen modules.
- Performance rewrites that alter structure (ok only if behavior-identical and reviewed).

## Process for a core change

1. Write a short architecture note (problem, options considered, the minimal
   frozen-surface impact, why it cannot live in an extension layer).
2. Get sign-off.
3. Implement the smallest possible diff.
4. Add/extend regression tests proving the frozen contract is preserved.

## Verification

The freeze is enforced by tests: frozen modules have comprehensive regression
suites (`test_ask.py`, `test_evidence_scope.py`, `test_m6_judgment.py`,
`test_identity_benchmarks.py`, `test_observation*.py`, `test_context*.py`). A
change that breaks those suites is, by definition, a core change and must go
through review.

Current suite: **311 passed** (pre-freeze baseline). Any frozen-module
regression is a review trigger, not a silent edit.
