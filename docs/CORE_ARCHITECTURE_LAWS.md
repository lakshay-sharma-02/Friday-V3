# Friday Core Architecture Laws

**Classification:** Constitutional — highest authority in this repository.
**Status:** Frozen. Effective immediately.
**Supersedes:** All READMEs, tutorials, design notes, and milestone reports where they conflict with this document.

---

## Introduction

Friday is intentionally layered. Each layer performs exactly one responsibility and passes structured data downward to the next. This is not a stylistic preference. It is the structural property that has allowed the system to grow from Observation through Understanding, Initiatives, Insights, the Brain, Planning, the Task Graph, and the Worker Registry without collapsing into a single entangled program.

Long-term maintainability depends on preserving these boundaries. Over the next several years, many subsystems will be added: schedulers, runtimes, reviewers, repair loops, learning pipelines. Every one of them will be tempted to reach across a boundary for convenience. Each such reach is a small act of architectural drift. Accumulated drift is how layered systems die.

This document exists to make drift a conscious, reviewable act rather than a silent default. It is the contract between the architecture and the engineers who extend it.

**Authority.** If future code conflicts with these laws, **the code is wrong**. The laws take precedence. A milestone that cannot satisfy these laws must be redesigned, not excused.

Every future milestone — 9.3 and beyond — must satisfy these laws before it is considered complete.

---

## Law 1 — Reality First

**Rule.** Reality is the only source of truth. Observations never invent facts. Inference never replaces evidence.

**Rationale.** A system that confuses inference with fact will propagate confident error downward and render every higher layer untrustworthy.

**Implications.**
- Observation reads the world; it does not hypothesize about it.
- A stored fact must trace to a real observation or a real artifact.
- Probabilistic judgment lives in the Brain; it is labeled as judgment, never stored as fact.

---

## Law 2 — Observation Never Executes

**Rule.** Observation may only observe. It never edits. It never plans. It never executes.

**Rationale.** An observer that writes is indistinguishable from an actor; its outputs can no longer be trusted as a neutral record of state.

**Implications.**
- Observers are read-only against repositories, processes, and external sources.
- Observation produces facts; it produces no mutations outside its own append-only store.

---

## Law 3 — Context Never Infers

**Rule.** Context groups observations. It never creates knowledge.

**Rationale.** Context exists to make observation legible across time (sessions, timelines). If it infers knowledge, it duplicates and competes with the Knowledge layer.

**Implications.**
- Context aggregates and correlates; it does not conclude.
- Knowledge is produced only by the Knowledge layer, from evidence.

---

## Law 4 — Knowledge Is Evidence

**Rule.** Knowledge exists only because evidence exists. Knowledge may weaken. Knowledge may retire. Knowledge is never guessed.

**Rationale.** Knowledge without evidence is rumor. Retiring and weakening are required so the system can correct itself.

**Implications.**
- Every knowledge entry cites evidence identifiers.
- No knowledge is created by free-form generation without an evidence basis.
- Verification and retirement are first-class, evidence-driven transitions.

---

## Law 5 — Understanding Emerges

**Rule.** Understanding is derived. It is never written manually. It never bypasses Knowledge.

**Rationale.** Understanding is the synthesis of knowledge; writing it by hand breaks the chain of evidence from reality to insight.

**Implications.**
- Understanding cites knowledge identifiers.
- No manual Understanding rows are authored outside the derivation engine.

---

## Law 6 — Initiatives Are Long-Lived

**Rule.** Initiatives describe engineering direction. They never execute work.

**Rationale.** Initiatives are strategy. Confusing strategy with execution destroys both the plan and the ability to measure against it.

**Implications.**
- Initiatives reference understanding/knowledge; they do not invoke workers.
- Execution is the responsibility of later layers (Runtime), never Initiatives.

---

## Law 7 — Insights Are Ephemeral

**Rule.** Insights exist only while supported. If evidence disappears, Insights disappear.

**Rationale.** An insight whose support is gone is noise. Ephemerality keeps the feed honest.

**Implications.**
- Insights are re-derived each build from current evidence.
- A missing supporting condition retires the insight; it is not preserved by sentiment.

---

## Law 8 — Brain Never Mutates Reality

**Rule.** Brain may reason. Brain may retrieve. Brain may synthesize. Brain never edits files. Brain never executes commands. Brain never modifies repositories.

**Rationale.** The Brain is the reasoning surface. The moment it writes to the world, it becomes an actor and its outputs can no longer be trusted as analysis.

**Implications.**
- Brain reads layers; it produces answers, not side effects.
- File edits, command execution, and repository mutation are owned by Runtime and Workers, never the Brain.

---

## Law 9 — Planning Is Declarative

**Rule.** Planning answers "What should happen?" Planning never executes.

**Rationale.** A plan is a structured strategy. Execution belongs to later layers. Mixing them couples strategy to a specific runtime.

**Implications.**
- Plans are structured objects (milestones, dependencies, risks, verification, rollback, evidence).
- Planning cites lower-layer identifiers; it does not call workers or run commands.

---

## Law 10 — Task Graph Is The Execution IR

**Rule.** Task Graph is Friday's Intermediate Representation. Everything after Task Graph consumes it. Everything before Task Graph is unaware of execution. Task Graph schema is versioned. Backward compatibility must be preserved.

**Rationale.** A single, versioned IR decouples "what to do" from "who does it" and "when". Without it, execution logic leaks into planning.

**Implications.**
- The Task Graph is the contract between Planning and all downstream execution layers.
- Schema changes are versioned; older graphs must remain interpretable.
- Planning/Understanding/Knowledge never reference execution specifics.

---

## Law 11 — Workers Never Think

**Rule.** Workers receive Tasks. Workers execute Tasks. Workers never create Tasks. Workers never rewrite Plans. Workers never mutate the Task Graph.

**Rationale.** Workers are capability profiles + executors. Authoring work inverts the control flow and destroys the single-writer property of each layer.

**Implications.**
- Workers consume the Task Graph; they do not author it.
- A worker's registry entry is metadata only — it describes, it does not decide.
- Workers never edit Plans or the Task Graph.

---

## Law 12 — Capability Resolution Owns Assignment

**Rule.** Workers never choose themselves. Schedulers never choose workers. Capability Resolver performs assignment. Only one layer owns this responsibility.

**Rationale.** If assignment is duplicated across layers, behavior becomes inconsistent and untestable. Single ownership is required.

**Implications.**
- Assignment of tasks to workers happens in exactly one place: the Capability Resolver.
- Scheduler orders; it does not assign. Runtime invokes; it does not assign.

---

## Law 13 — Scheduler Owns Time

**Rule.** Scheduler determines ordering. Scheduler never changes work.

**Rationale.** Ordering and content are independent concerns. A scheduler that rewrites work conflates "when" with "what".

**Implications.**
- Scheduler reads the Task Graph and assigns sequence; it does not alter tasks.
- Work content is fixed by the Task Graph; time is the scheduler's only lever.

---

## Law 14 — Runtime Owns Execution

**Rule.** Runtime invokes workers. Runtime never changes Plans. Runtime never changes Task Graphs.

**Rationale.** The Runtime is the boundary to the real world. It must be a faithful executor, not an author.

**Implications.**
- Runtime maps assignments to worker invocations.
- Runtime does not edit the plan or graph; deviations are reported, not silently applied.

---

## Law 15 — Review Owns Truth

**Rule.** Workers never declare success. Review determines success. Verification is objective: tests, builds, exit codes, static analysis, evidence — not opinions.

**Rationale.** Self-reported success is unverifiable. Truth must come from an independent, evidence-based check.

**Implications.**
- A worker's completion is a claim; Review is the verdict.
- Review emits an objective Review Record; it does not trust worker output at face value.

---

## Law 16 — Repair Is Evidence Driven

**Rule.** Failures produce Repairs. Repairs originate from Review. Workers never invent repairs.

**Rationale.** Repair without root-cause evidence repeats failure and hides it.

**Implications.**
- Repair is triggered by a Review verdict, not by a worker's own judgment.
- Repairs are traceable to the failing evidence and the Review Record.

---

## Law 17 — Learning Is Observational

**Rule.** Execution becomes Observation. Observation becomes Knowledge. Learning never bypasses Observation.

**Rationale.** Learning that skips Observation writes conclusions without evidence, violating Law 4.

**Implications.**
- Post-execution learning flows through the Observation layer back into Knowledge.
- No layer writes knowledge directly from execution output.

---

## Law 18 — Single Responsibility

**Rule.** Every layer owns exactly one responsibility. Responsibilities never overlap.

**Rationale.** Overlap creates ambiguous ownership and makes change dangerous.

**Implications.**
- If two layers can do the same thing, one of them is mis-scoped.
- New capability goes in the layer whose single responsibility covers it — or a new layer is added.

---

## Law 19 — Downward Dependencies Only

**Rule.** Layers may only depend on lower layers. Never upward. Never circular.

**Rationale.** Upward or circular dependencies make the system impossible to reason about or test in isolation.

**Implications.**
- A lower layer (e.g. Knowledge) has no knowledge of a higher layer (e.g. Planning).
- The Task Graph depends on Planning; it does not depend on the Runtime.

---

## Law 20 — Append-Only History

**Rule.** History is preserved for Knowledge, Understanding, Initiatives, Insights, Plans, Task Graphs, Workers, Execution. History is never rewritten.

**Rationale.** Rewritable history destroys the ability to audit, replay, and trust evolution.

**Implications.**
- Live rows may transition (status, version); history tables accumulate, never mutate.
- No `UPDATE` or `DELETE` against a history table.

---

## Law 21 — Determinism First

**Rule.** Every deterministic algorithm is preferred over LLM reasoning. LLMs are optional. Evidence is mandatory.

**Rationale.** Determinism is testable, replayable, and cheap. LLMs are nondeterministic, unverifiable, and costly. Use them only where evidence-bearing determinism cannot do the job.

**Implications.**
- Classification, routing, validation, and schema work are deterministic.
- LLM output is never treated as fact (Law 4, Law 8).

---

## Law 22 — Execution Is Replayable

**Rule.** Every execution can be reconstructed: Plan → Task Graph → Assignments → Execution → Review → Observation. Everything required for replay must be serializable.

**Rationale.** Without replay, failures cannot be diagnosed and learning cannot occur.

**Implications.**
- Assignments, execution records, and review records are persisted as structured data.
- No execution state lives only in memory or in an undisclosed worker.

---

## Law 23 — Stable Interfaces

**Rule.** Every major layer communicates only through explicit contracts. Never through hidden state. Never through globals. Never through implicit coupling.

**Rationale.** Hidden coupling is the most common cause of architectural drift and regressions.

**Implications.**
- Layers exchange structured objects or versioned schemas, not mutable shared singletons.
- Cross-layer calls go through defined functions/tables, not module-level side effects.

---

## Law 24 — Versioned Contracts

**Rule.** Every public contract — Task Graph, Worker Manifest, Execution Record, Review Record — must be versioned. Schema evolution must never silently break older data.

**Rationale.** Downstream consumers depend on contracts. Silent breakage corrupts the execution pipeline.

**Implications.**
- Each contract carries a schema version.
- Readers handle prior versions; migrations are explicit and reversible where possible.

---

## Law 25 — Architectural Freeze

**Rule.** Once a layer reaches production, future milestones extend it. They do not redesign it. Architecture evolves upward, not sideways.

**Rationale.** Redesigning a frozen layer invalidates everything built on it and reopens settled decisions.

**Implications.**
- A production layer is extended by adding above it, not by rewriting it.
- "It would be cleaner if we restructured X" is not sufficient cause to touch a frozen layer.

---

## Architectural Dependency Diagram

```
Reality
   │  (observed)
   ▼
Observation          Law 2: observe only     [append-only facts]
   │
   ▼
Context              Law 3: group only       [sessions, timeline]
   │
   ▼
Knowledge            Law 4: evidence only    [retire / weaken]
   │
   ▼
Understanding        Law 5: derived only     [cites knowledge]
   │
   ▼
Initiatives          Law 6: direction only  [long-lived, no exec]
   │
   ▼
Insights            Law 7: ephemeral        [evidence-backed]
   │
   ▼
Brain                Law 8: reason only      [no world mutation]
   │
   ▼
Planning             Law 9: declarative      [what should happen]
   │
   ▼
Task Graph           Law 10: Execution IR    [versioned schema]
   │
   ▼
Worker Registry      (M9.2) metadata only    [who is capable]
   │
   ▼
Capability Resolver  Law 12: assignment only [matches caps → workers]
   │
   ▼
Scheduler            Law 13: time only       [ordering]
   │
   ▼
Runtime              Law 14: invoke only     [no plan/graph edits]
   │
   ▼
Workers              Law 11: execute only    [no authoring]
   │
   ▼
Review               Law 15: truth only      [objective verification]
   │                                  │
   │                                  └─► Repair (Law 16: evidence-driven)
   ▼
Observation  ◄────── Learning (Law 17: execution → observation → knowledge)
```

Dependencies flow downward only (Law 19). History accumulates at every stage (Law 20). All classification and routing is deterministic (Law 21). Contracts are versioned (Law 24).

---

## Architectural Invariants

These must never be violated. A violation is a defect regardless of intent.

1. No layer edits a repository, file, or external system except the Runtime (and the Workers it invokes).
2. No layer above Knowledge creates Knowledge. No layer above Understanding creates Understanding.
3. The Task Graph is the sole execution IR; nothing above it knows execution specifics, nothing below it knows assignment or scheduling.
4. Assignment of tasks to workers occurs in exactly one place.
5. A worker never declares its own success; Review does.
6. History tables are append-only; no row is updated or deleted in them.
7. Every public contract carries a schema version and remains readable by prior-version consumers.
8. No upward or circular dependencies between layers.
9. No hidden state, globals, or implicit coupling across layer boundaries.
10. Deterministic algorithms are used wherever evidence-bearing determinism is possible; LLM output is never stored as fact.
11. A frozen production layer is extended, never redesigned.
12. Execution is fully reconstructable from persisted, serializable records.

---

## Engineering Review Checklist

Every future milestone must satisfy all of the following before it is considered complete.

- □ No frozen layer modified.
- □ No upward dependency introduced.
- □ No circular dependency introduced.
- □ No responsibility moved between layers.
- □ No hidden state introduced.
- □ All new contracts versioned.
- □ Append-only history preserved.
- □ Deterministic behavior maintained.
- □ Existing CLI remains compatible.
- □ Existing tests continue to pass.
- □ New layer owns exactly one responsibility (Law 18).
- □ New layer depends only on lower layers (Law 19).
- □ New public contract carries a schema version (Law 24).
- □ Execution path remains replayable (Law 22).
- □ No layer edits reality outside the Runtime boundary (Law 8, Law 14).
- □ Review, not the worker, owns success (Law 15).

If any item cannot be checked, the milestone is incomplete. Resolve the conflict by redesigning the code — not by relaxing the law.
