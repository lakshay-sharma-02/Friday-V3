"""CLI commands for the Planning Engine (Milestone 9.0)."""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .planning import PlanEngine, PlanStatus
from .planning.models import PlanConfidence


def cmd_plan_generate(args: argparse.Namespace) -> int:
    """WRITE: derive a plan for a goal and persist it."""
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print("error: a goal is required: friday plan \"<goal>\"",
              file=sys.stderr)
        return 2
    conn = connect()
    eng = PlanEngine(conn)
    p = eng.generate(goal)
    conn.close()
    print(p.render_text())
    return 0


def cmd_plans_list(args: argparse.Namespace) -> int:
    """READ: list active plans (non-superseded)."""
    conn = connect()
    eng = PlanEngine(conn)
    items = eng.active_plans()
    conn.close()
    if not items:
        print("No plans derived yet.\n")
        print("Run:\n")
        print('  friday plan "Implement OAuth"\n')
        return 0
    items = sorted(items, key=lambda p: p.updated_at, reverse=True)
    for p in items:
        mark = {
            PlanStatus.APPROVED: "*",
            PlanStatus.REFINED: "#",
            PlanStatus.PLANNED: "?",
            PlanStatus.SUPERSEDED: "x",
        }.get(p.status, "·")
        conf = p.confidence.value[0].upper()
        ev = (p.initiative_count + p.insight_count + p.understanding_count
              + p.knowledge_count)
        print(f"  [{mark}] {p.goal} ({p.plan_type.value}, {conf}, "
              f"evidence={ev})")
        print(f"      milestones={len(p.milestones)} risks={len(p.risks)} "
              f"complexity={p.estimated_complexity} effort={p.estimated_effort}")
    print(f"\nActive: {len(items)}")
    return 0


def resolve_plan_id(pid: str, eng: PlanEngine) -> "tuple[str | None, int | None]":
    """Resolve a reference: full deterministic id, or INTEGER = Nth newest."""
    if pid.isdigit():
        n = int(pid)
        ordered = sorted(eng.all_plans(), key=lambda p: p.created_at, reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    return pid, None


def cmd_plan_explain(args: argparse.Namespace) -> int:
    """READ: explain one plan with milestones/dependencies/risks/verification/
    rollback/confidence/supporting evidence."""
    pid = getattr(args, "id", None) or getattr(args, "plan_id", None)
    if not pid:
        print("error: plan ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2
    conn = connect()
    eng = PlanEngine(conn)
    resolved, err = resolve_plan_id(pid, eng)
    if err is not None:
        count = len(eng.all_plans())
        print(f"error: plan index {pid} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err
    p = eng.plan_by_id(resolved)
    conn.close()
    if p is None:
        print(f"error: plan not found: {pid}", file=sys.stderr)
        return 2

    print(f"Plan: {p.id}\n")
    print(f"Goal:         {p.goal}")
    print(f"Type:         {p.plan_type.value}")
    print(f"Status:       {p.status.value}")
    print(f"Confidence:   {p.confidence.value}")
    print(f"Complexity:   {p.estimated_complexity}")
    print(f"Effort:       {p.estimated_effort}")
    print(f"Created:      {p.created_at}")
    print(f"Updated:      {p.updated_at}")

    print("\nSupporting evidence:")
    print(f"  Initiatives:    {', '.join(p.affected_initiative_ids) or '(none)'}")
    print(f"  Insights:       {', '.join(p.affected_insight_ids) or '(none)'}")
    print(f"  Understanding:  {', '.join(p.affected_understanding_ids) or '(none)'}")
    print(f"  Knowledge:      {', '.join(p.affected_knowledge_ids) or '(none)'}")

    print("\nMilestones:")
    for m in p.milestones:
        print(f"  {m.get('order')}. {m.get('title')}"
              + (f" — {m.get('detail')}" if m.get('detail') else ""))

    print("\nDependencies:")
    if not p.dependencies:
        print("  (none identified)")
    for d in p.dependencies:
        print(f"  - {d.get('kind')}: {d.get('target')}"
              + (f" ({d.get('reason')})" if d.get('reason') else ""))

    print("\nRisks:")
    if not p.risks:
        print("  (none identified)")
    for r in p.risks:
        print(f"  - [{r.get('severity','medium')}] {r.get('kind')}: "
              + r.get("detail", ""))

    print("\nVerification:")
    for v in p.verification:
        print(f"  - {v.get('method')}: {v.get('detail','')}")

    print("\nRollback:")
    for rb in p.rollback:
        print(f"  - {rb.get('strategy')}: {rb.get('detail','')}")

    from .db import plan_history_for
    conn2 = connect()
    hist = plan_history_for(conn2, p.id or "")
    conn2.close()
    print("\nHistory:")
    if not hist:
        print("  (no prior snapshots)")
    for h in hist:
        print(f"  {h.generated_at[:19]}  {h.confidence:6}  {h.status:8}  "
              f"{h.plan_type}")
    return 0


def cmd_plan_history(args: argparse.Namespace) -> int:
    """READ: chronological timeline of plan evolution events."""
    conn = connect()
    eng = PlanEngine(conn)
    events = eng.evolution()
    conn.close()
    if not events:
        print("No plan evolution yet. Run `friday plan \"<goal>\"`.")
        return 0
    print("Plan Evolution Events\n")
    for e in events:
        print(f"{e.timestamp[:19]}  {e.event_type:12}  {e.plan_id}")
        print(f"    {e.reason}")
    print(f"\nTotal events: {len(events)}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Dispatch friday plan subcommands."""
    goal = getattr(args, "goal", None)
    action = getattr(args, "action", None)
    plan_id = getattr(args, "plan_id", None)
    # `plan explain <id>` -> goal token is "explain", action holds the id.
    # `plan explain --id <id>` -> goal token is "explain", --id holds the id.
    if goal in ("explain", "history", "list"):
        real_id = action or plan_id or getattr(args, "id", None)
        if goal == "explain":
            args.plan_id = real_id
            return cmd_plan_explain(args)
        elif goal == "history":
            return cmd_plan_history(args)
        else:
            return cmd_plans_list(args)
    if action == "explain":
        return cmd_plan_explain(args)
    elif action == "history":
        return cmd_plan_history(args)
    elif action == "list":
        return cmd_plans_list(args)
    else:
        # No action with a goal present -> generate; bare `friday plan` -> list.
        if goal:
            return cmd_plan_generate(args)
        return cmd_plans_list(args)
