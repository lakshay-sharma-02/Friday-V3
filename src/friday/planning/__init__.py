"""Planning Engine package (Milestone 9.0).

Write-only layer on top of Insights / Initiatives / Understanding / Knowledge.
Produces STRUCTURED, evidence-backed engineering plans. NEVER executes, edits
files, calls workers, or uses an LLM. NEVER reads observations/context/git/
repositories directly. The Brain consumes plans as evidence; it never computes
them. Plans are structured (see models.Plan); text is rendered only at the end.
"""

from .derive import Evidence, plan as derive_plan
from .engine import PlanBuildResult, PlanEngine
from .models import Plan, PlanConfidence, PlanStatus, PlanType

# M9.1: Task Graph Compiler — write-only layer on top of the PlanEngine. NEVER
# executes, edits, or uses an LLM. Compiles a structured Plan into a deterministic
# task DAG that future Workers consume. The PlanEngine below is FROZEN and
# untouched by this addition.
from .compiler import (
    CycleError,
    Task,
    TaskGraph,
    TaskType,
    compile_plan,
)
from .graph_engine import GraphBuildResult, TaskGraphEngine

__all__ = [
    "PlanEngine",
    "PlanBuildResult",
    "Plan",
    "PlanType",
    "PlanStatus",
    "PlanConfidence",
    "Evidence",
    "derive_plan",
    # M9.1 Task Graph Compiler
    "TaskGraphEngine",
    "GraphBuildResult",
    "TaskGraph",
    "Task",
    "TaskType",
    "CycleError",
    "compile_plan",
]
