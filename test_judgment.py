import sqlite3
from friday.db import create_schema
from friday.planning.compiler import TaskGraphCompiler, TaskType
from friday.planning.models import Plan, PlanType
from friday.resolver.engine import CapabilityResolver
from friday.worker.engine import WorkerRegistry
from friday.worker.models import Worker, WorkerKind

def main():
    conn = sqlite3.connect(":memory:")
    # Initialize the tables needed
    create_schema(conn)

    registry = WorkerRegistry(conn)
    registry.register(Worker(
        id="worker:shell",
        name="Shell",
        kind=WorkerKind.EXECUTOR,
        capabilities=["python", "file editing"],
        supported_task_types=["implementation", "refactor"],
        supported_plan_types=["feature"],
    ))
    registry.register(Worker(
        id="worker:claude_code",
        name="Claude Code",
        kind=WorkerKind.EXECUTOR,
        capabilities=["python", "file editing", "research"],
        supported_task_types=["implementation", "refactor"],
        supported_plan_types=["feature"],
    ))
    
    # We need to make claude_code an AI executor so it gets the AI pref logic
    # In resolver.py: is_ai_executor(w) checks if it's in a known list or kind == LLM.
    # Actually wait! is_ai_executor is defined in resolver.models or something?
    # Let me check resolver/models.py to see how is_ai_executor is defined.

