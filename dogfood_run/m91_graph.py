"""Milestone 9.1 dogfood transcript — Task Graph Compiler end-to-end.

Drives the REAL engines (no LLM, no mock) through the full live chain:

  Knowledge -> Understanding -> Initiatives -> Insights -> Plans -> Task Graphs

The Planning Engine (M9.0, FROZEN) produces structured Plans; this script then
compiles each Plan into a deterministic, acyclic Task Graph (Friday's execution
IR). Workers will consume ONLY the compiled graph, never the Plan. The lower
layers and Planning are untouched — this is purely additive.

Demonstrates, per goal:
  Plan -> Graph -> Critical Path -> Parallel Tasks -> Export JSON

Run:  PYTHONPATH=. python dogfood_run/m91_graph.py
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import os

from src.friday.db import connect
from src.friday.knowledge.models import (
    Knowledge, KnowledgeConfidence, KnowledgeStatus, KnowledgeType)
from src.friday.knowledge.store import insert_knowledge, get_all_knowledge
from src.friday.understanding import UnderstandingEngine
from src.friday.understanding.models import (
    Understanding, UnderstandingConfidence, UnderstandingStatus, UnderstandingType)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative.models import (
    Initiative, InitiativeConfidence, InitiativeStatus, InitiativeType)
from src.friday.db import insert_initiative
from src.friday.initiative import InitiativeEngine
from src.friday.insight import InsightEngine
from src.friday.planning import PlanEngine, TaskGraphEngine
from src.friday.planning.compiler import _detect_cycle
from src.friday.planning.graph_schema import (
    SCHEMA_VERSION, validate_task_graph, SchemaError)


BASE = "2026-07-15T00:00:00+00:00"
SEEN = set()
N = 0


def k(ldb, subject, stmt, ktype, kconf="medium", evidence_ids=("repo:a",)):
    global N
    real = subject
    while real in SEEN:
        N += 1
        real = f"{subject}_{N}"
    SEEN.add(real)
    N += 1
    insert_knowledge(ldb, [Knowledge(
        type=KnowledgeType.from_str(ktype), subject=real, statement=stmt,
        confidence=KnowledgeConfidence.from_str(kconf), evidence_ids=list(evidence_ids),
        status=KnowledgeStatus.VERIFIED, created_at=BASE, updated_at=BASE, id=None)])
    return real


def u(ldb, subject, stmt, utype, uconf="medium", knowledge_subjects=()):
    kmap = {kk.subject: kk.id for kk in get_all_knowledge(ldb)}
    kids = [kmap[x] for x in knowledge_subjects if x in kmap]
    insert_understanding(ldb, [Understanding(
        type=UnderstandingType.from_str(utype), subject=subject, statement=stmt,
        confidence=UnderstandingConfidence.from_str(uconf),
        status=UnderstandingStatus.OBSERVED, knowledge_ids=kids,
        build_at=BASE, created_at=BASE, updated_at=BASE, id=None).to_row()])


def i(ldb, title, itype="platform", repos=("repo:a",), u_subjects=(), k_subjects=()):
    u_by = {uu.subject: uu.id for uu in UnderstandingEngine(ldb).all_understanding()}
    k_by = {kk.subject: kk.id for kk in get_all_knowledge(ldb)}
    uids = [u_by[s] for s in u_subjects if s in u_by]
    kids = [k_by[s] for s in k_subjects if s in k_by]
    init = Initiative(
        type=InitiativeType.from_str(itype), title=title,
        status=InitiativeStatus.ACTIVE,
        confidence=InitiativeConfidence.from_str("medium"),
        participating_repositories=list(repos), understanding_ids=uids,
        knowledge_ids=kids, build_at=BASE, started_at=BASE, statement="",
        created_at=BASE, updated_at=BASE, id=None)
    insert_initiative(ldb, [init.to_row()])


GOALS = [
    "Implement OAuth",
    "Refactor authentication",
    "Extract shared Rust crates",
    "Build worker system",
    "Improve Vivaha architecture",
]


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = connect(path)

    print("=" * 78)
    print("M9.1 DOGFOOD: Task Graph Compiler end-to-end (real engines, no LLM)")
    print("=" * 78)

    print("\n[1] Knowledge / Understanding / Initiatives / Insights (lower layers)")
    auth_a = k(conn, "auth", "auth solved in api", "recurring_pattern", "medium", ["repo:a"])
    auth_b = k(conn, "auth", "auth solved in web", "recurring_pattern", "medium", ["repo:b"])
    rust1 = k(conn, "rust", "rust infra 1", "engineering_trend", "strong", ["repo:a"])
    rust2 = k(conn, "rust", "rust infra 2", "technology_investment", "strong", ["repo:b"])
    viv = k(conn, "vivaha", "vivaha architecture", "project_architecture", "strong", ["repo:c"])
    wk = k(conn, "worker", "worker scheduling", "engineering_trend", "medium", ["repo:a"])
    u(conn, "auth a", "Auth solved in api.", "engineering_habit", "medium", [auth_a])
    u(conn, "auth b", "Auth solved in web.", "engineering_habit", "medium", [auth_b])
    u(conn, "rust a", "Rust investment rising.", "engineering_direction", "strong", [rust1, rust2])
    u(conn, "rust b", "Rust direction strong.", "engineering_direction", "strong", [rust1, rust2])
    u(conn, "viv", "Vivaha structure.", "engineering_direction", "strong", [viv])
    u(conn, "wk", "Worker scheduling emerging.", "engineering_direction", "medium", [wk])
    i(conn, "Authentication Infrastructure", itype="infrastructure",
      repos=["repo:a", "repo:b"], u_subjects=["auth a", "auth b"])
    i(conn, "Engineering Platform", itype="platform",
      repos=["repo:a", "repo:b"], u_subjects=["rust a", "rust b"])
    i(conn, "Vivaha Platform", itype="platform", repos=["repo:c"], u_subjects=["viv"])
    i(conn, "Worker System", itype="infrastructure", repos=["repo:a"], u_subjects=["wk"])
    InsightEngine(conn).build()

    print("\n[2] Plans (M9.0, FROZEN) -> Task Graphs (M9.1, NEW)")
    peng = PlanEngine(conn)
    geng = TaskGraphEngine(conn)
    for goal in GOALS:
        plan = peng.generate(goal)
        g = geng.generate(goal)
        ids = [t.id for t in g.tasks]
        acyclic = not _detect_cycle(g.edges, ids)
        print(f"\n  Goal: {goal}")
        print(f"    Plan: {plan.plan_type.value} / {plan.confidence.value} "
              f"({len(plan.milestones)} milestones)")
        print(f"    Task Graph: {g.id}")
        print(f"      tasks={len(g.tasks)} edges={len(g.edges)} "
              f"acyclic={acyclic}")
        # Boundary demonstration: the downstream consumer validates the FROZEN
        # JSON contract before trusting it (Planning/compiler untouched).
        export = g.to_json()
        try:
            validate_task_graph(export)
            valid = True
            reason = f"schema_version={SCHEMA_VERSION}, contract OK"
        except SchemaError as exc:
            valid = False
            reason = str(exc)
        print(f"      schema contract: {'VALID' if valid else 'INVALID'} ({reason})")
        print(f"      capabilities: "
              + ", ".join(sorted({c for t in g.tasks
                                  for c in t.required_capabilities})))
        print(f"    Critical path ({len(g.critical_path)}):")
        print("      " + " -> ".join(g._title(p) for p in g.critical_path))
        print(f"    Parallel tasks ({g.parallel_groups} groups):")
        for tid in g.parallel_tasks:
            for t in g.tasks:
                if t.id == tid:
                    print(f"      - {t.title} ({t.task_type})")

    print("\n[3] Graph export (Worker-Engine JSON) — Implement OAuth")
    oauth = geng.graph_by_id("taskgraph:plan:implement oauth")
    export = oauth.to_json()
    print(f"    graph_id={export['graph_id']} tasks={export['task_count']} "
          f"edges={export['edge_count']} acyclic={export['metadata']['acyclic']}")
    # Downstream contract: validate the exported JSON through the frozen schema.
    validate_task_graph(export)
    print(f"    schema contract: VALID (schema_version="
          f"{export['metadata']['schema_version']})")
    print("    first task JSON:")
    print("      " + json.dumps(export["tasks"][0], indent=2).replace("\n", "\n      "))

    print("\n[4] Idempotency (recompile) + append-only history")
    before = len(geng.all_graphs())
    geng.generate("Implement OAuth")  # recompile same goal
    after = len(geng.all_graphs())
    print(f"    graphs before recompile={before} after={after} "
          f"(idempotent on goal: {before == after})")
    print(f"    evolution events for OAuth graph: "
          f"{len(geng.evolution('taskgraph:plan:implement oauth'))}")

    conn.close()
    os.remove(path)
    print("\n" + "=" * 78)
    print("DOGFOOD COMPLETE — Plans compiled to deterministic task DAGs; "
          "no execution performed.")
    print("=" * 78)


if __name__ == "__main__":
    main()
