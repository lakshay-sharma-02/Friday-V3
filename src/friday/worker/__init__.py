"""Worker Registry (Milestone 9.2).

WRITE-ONLY layer on TOP of the Task Graph Compiler. It describes WHO is capable
of executing tasks — it NEVER executes, schedules, selects, runs, or decides
anything. The registry is a catalog of capability profiles. The future
Capability Resolver (M9.3) reads this catalog to match task requirements against
worker capabilities; this layer only answers "what capabilities exist?".

Provider-agnostic by design (the one architectural improvement called out in the
spec): every worker is a GENERIC capability profile keyed by `kind`
(llm/cli/function/agent/tool/service). Claude, Codex, Gemini, a local CLI tool,
a Python function, or a future remote agent all fit the same schema — none are
special-cased. The Capability Resolver therefore never needs provider logic.

Determinism guarantees (no LLM, no embeddings, no vectors, no randomness):
- Capabilities come from a CLOSED deterministic vocabulary — never free-form.
- Languages/task-types/plan-types are validated against frozen sets.
- Registration is deterministic; re-registering the same id REPLACES the row,
  records the prior version in worker_history (append-only), and logs the
  version. No engine modification required to add a future worker.
"""

from .engine import WorkerRegistry
from .models import (
    KIND_LLM,
    KIND_CLI,
    KIND_FUNCTION,
    KIND_AGENT,
    KIND_TOOL,
    KIND_SERVICE,
    Worker,
    WorkerKind,
    all_capabilities,
    is_valid_capability,
    is_valid_language,
    is_valid_task_type,
    is_valid_plan_type,
)

__all__ = [
    "WorkerRegistry",
    "Worker",
    "WorkerKind",
    "KIND_LLM",
    "KIND_CLI",
    "KIND_FUNCTION",
    "KIND_AGENT",
    "KIND_TOOL",
    "KIND_SERVICE",
    "all_capabilities",
    "is_valid_capability",
    "is_valid_language",
    "is_valid_task_type",
    "is_valid_plan_type",
]
