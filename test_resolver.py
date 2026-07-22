import sys
import sqlite3
from friday.db import init_db
from friday.planning.compiler import TaskGraphCompiler
from friday.planning.models import Plan, PlanType
from friday.resolver.engine import CapabilityResolver
from friday.worker.engine import WorkerRegistry
from friday.worker.models import Worker, WorkerKind

def main():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    registry = WorkerRegistry(conn)
    registry.register(Worker(
        id="worker:shell",
        name="Shell",
        kind=WorkerKind.EXECUTOR,
        capabilities=["shell commands"],
        supported_task_types=["implementation", "configuration"],
        supported_plan_types=["trivial", "feature"],
    ))
    registry.register(Worker(
        id="worker:claude_code",
        name="Claude Code",
        kind=WorkerKind.EXECUTOR,
        capabilities=["python", "shell commands", "file editing", "research", "architecture", "testing", "documentation", "frontend", "backend", "infrastructure", "configuration"],
        supported_task_types=["implementation", "refactor", "design", "review", "analysis", "research", "testing"],
        supported_plan_types=["trivial", "feature", "refactor"],
    ))

    plan = Plan(
        goal="Test goal",
        plan_type=PlanType.FEATURE,
        milestones=[
            {"title": "mechanical_task", "task_type": "implementation", "symbolic": {"op": "run_command"}},
            {"title": "judgment_task", "task_type": "implementation"}
        ]
    )
    
    compiler = TaskGraphCompiler()
    graph = compiler.compile(plan)
    
    # We must insert the graph into DB so resolver can find it
    from friday.planning.engine import TaskGraphEngine
    g_engine = TaskGraphEngine(conn)
    g_engine.save_graph(graph)

    resolver = CapabilityResolver(conn)
    res = resolver.resolve_graph(graph.id)
    
    print("Resolved:")
    for a in res.assignments:
        task = next((t for t in graph.tasks if t.id == a.task_id), None)
        print(f"Task: {task.title} (type: {task.task_type}, symbolic: {task.symbolic}) -> Worker: {a.worker_id}")

if __name__ == '__main__':
    main()
