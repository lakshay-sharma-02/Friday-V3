# Task Graph JSON Schema (Frozen Public Interface)

**Status:** FROZEN at `schema_version: 1` (Milestone 9.1).
**Owner:** Execution system (M9.2+) consumes this. Planning + Compiler are FROZEN and never change it.

This document is the stable contract between Friday's cognitive/planning stack
(Observation → … → Planning → **Task Graph Compiler**) and every downstream
execution component:

> Worker Registry · Capability Resolver · Scheduler · Runtime · Review ·
> Repair · external integrations

The contract is validated by `src/friday/planning/graph_schema.py`, which imports
**nothing** from the compiler or Planning. Downstream consumers must validate
every graph through `validate_task_graph()` (or `load_task_graph()`) before use.
Never parse or assume the shape — validate it.

---

## Freeze policy

- `SCHEMA_VERSION` is bumped **only** on a breaking change to this JSON shape.
- The vocabulary enums below are the **closed, allowed value sets**. The compiler
  emits only these; consumers must accept only these (reject anything else).
- Adding a capability or task type is a **schema change**: bump the version,
  extend the enum, add a migration note. Do **not** change field shapes without a
  version bump.
- The compiler and Planning never read this file. This module is consumer-side.

---

## Top-level object

`TaskGraph.to_json()` returns exactly:

```json
{
  "graph_id":        "taskgraph:plan:<goal>",
  "goal":            "string",
  "plan_id":         "plan:<goal>",
  "plan_type":       "feature | refactor | infrastructure | …",
  "generated_at":    "ISO-8601 UTC timestamp",
  "task_count":      "integer == len(tasks)",
  "edge_count":      "integer == len(edges)",
  "critical_path_length": "integer == len(critical_path)",
  "critical_path":   ["<task_id>", "…"],
  "parallel_groups": "integer",
  "parallel_tasks":  ["<task_id>", "…"],
  "tasks":           [ <Task>, … ],
  "edges":           [ <Edge>, … ],
  "metadata": {
    "compiler":       "M9.1-task-graph-compiler",
    "acyclic":        true,
    "schema_version": 1
  }
}
```

**Invariants enforced by validation:**
- `task_count == len(tasks)`, `edge_count == len(edges)`,
  `critical_path_length == len(critical_path)`.
- `metadata.schema_version == 1`, `metadata.acyclic == true`.
- The graph is a **DAG** (no cycles in `edges`).
- Every `critical_path` / `parallel_tasks` id references a real task.

---

## Edge

```json
{ "from": "<task_id>", "to": "<task_id>", "kind": "depends_on" }
```

- `from` **depends on** `to` (dependency direction).
- `kind` is always `"depends_on"` (closed set).
- No duplicate edges; both endpoints must reference existing tasks.

---

## Task

```json
{
  "id":                     "taskgraph:plan:<goal>#t<n>",
  "graph_id":               "taskgraph:plan:<goal>",
  "plan_id":                "plan:<goal>",
  "milestone_order":        "integer",
  "title":                  "string",
  "description":            "string",
  "task_type":              "<TASK_TYPE>",
  "required_capabilities":  ["<CAPABILITY>", "…"],
  "complexity":             "<COMPLEXITY>",
  "priority":               "<PRIORITY>",
  "estimated_effort":       "<EFFORT>",
  "dependencies":           ["<task_id>", "…"],
  "inputs":                 ["string", "…"],
  "outputs":                ["string", "…"],
  "acceptance_criteria":    ["string", "…"],   // never empty
  "verification":           [ {"method": "string", "detail": "string"}, … ],
  "rollback":               [ {"strategy": "string", "detail": "string"}, … ],
  "evidence":               ["<lower-layer id>", "…"],
  "status":                 "<TASK_STATUS>",
  "confidence":             "<CONFIDENCE>",
  "sequence":               "integer"
}
```

- `graph_id`/`plan_id` must match the enclosing graph.
- `dependencies` reference only **earlier** task ids (preventing cycles).
- `acceptance_criteria` is **never empty**; `verification`/`rollback` are never empty.

---

## Closed vocabularies

### `task_type` (frozen, never LLM-generated)
```
analysis, design, implementation, testing, documentation, migration, review,
refactor, infrastructure, research, verification, deployment, configuration,
cleanup, planning
```

### `required_capabilities` (capabilities ONLY — no worker names)
```
rust, python, typescript, sql, architecture, testing, documentation,
frontend, backend, infrastructure, research, configuration
```

### `priority`
```
low, medium, high, critical
```

### `complexity`
```
tiny, small, medium, large, very_large
```

### `estimated_effort`
```
low, medium, high
```

### `status` (task lifecycle; compiler emits `pending`, downstream mutates)
```
pending, in_progress, blocked, completed, failed, skipped, cancelled
```

### `confidence`
```
weak, medium, strong
```

### `kind` (edge)
```
depends_on
```

---

## Consumer contract

```python
from src.friday.planning.graph_schema import validate_task_graph, load_task_graph

obj = json.loads(worker_engine_receive())   # untrusted / external input
validate_task_graph(obj)                     # raises SchemaError if invalid
graph = load_task_graph(obj)                 # typed, consumer-safe view
for task in graph.tasks:
    resolver.assign(task, capabilities=task.required_capabilities)
```

If validation fails, the graph is **rejected wholesale** — never partially
trusted. This boundary lets the execution system evolve independently while the
cognitive stack and Planning remain stable.
