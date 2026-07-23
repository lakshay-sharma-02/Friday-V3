"""friday capability discover|list|info|benchmark|propose|review (M10 + M10.x)."""
from __future__ import annotations
import argparse
import json
import sys
from .db import connect, now_iso
from .worker.engine import WorkerRegistry, _EXTERNAL_MANIFESTS
from .worker.genesis import (
    propose_worker,
    draft_manifest,
    register_approved_proposal,
    CapabilityGapEvent,
    detect_gap,
    reset_gap_tracking,
)
from .worker.models import validate_capabilities
from .runtime.discovery import discover
from .runtime.benchmark import BenchmarkRunner, BenchmarkTask


def _cmd_capability_propose(args: argparse.Namespace, conn) -> int:
    """Run draft_manifest() over pending gaps and write to proposed_workers.

    Reads all pending gaps from the resolver's last resolution run stored in
    resolver_history, or accepts an explicit capability gap to propose for.
    Follows the `friday graph review` UX pattern.
    """
    from .db import get_resolver_history
    reg = WorkerRegistry(conn)

    explicit_gap = getattr(args, "capability", None)

    if explicit_gap:
        # Propose for an explicitly specified capability gap.
        cap = explicit_gap.strip()
        event = CapabilityGapEvent(
            goal=getattr(args, "goal", "manual") or "manual",
            required_capability=cap,
            task_id="",
            graph_id="",
        )
        manifest = draft_manifest(event)
        if manifest is None:
            print(f"Could not draft a manifest for capability: {cap}")
            print("No PATH tool found and no LLM configured (FRIDAY_LLM_MODEL).")
            return 1

        from .db import (
            ProposedWorkerRow, insert_proposed_worker, get_proposed_worker
        )
        safe_cap = cap.replace(" ", "_").lower()
        proposal_id = f"proposal:{safe_cap}:manual"
        existing = get_proposed_worker(conn, proposal_id)
        if existing and existing.status == "pending":
            print(f"Proposal already pending: {proposal_id}")
            return 0

        import json as _json
        row = ProposedWorkerRow(
            id=proposal_id,
            detected_from_goal="manual",
            capability_gap=cap,
            draft_manifest_json=_json.dumps({
                "name": manifest.name,
                "implementation": manifest.implementation,
                "provider": manifest.provider,
                "origin": manifest.origin,
                "capabilities": manifest.capabilities,
                "requirements": list(manifest.requirements) if manifest.requirements else [],
                "supported_task_types": list(manifest.supported_task_types),
                "supported_plan_types": list(manifest.supported_plan_types),
                "supported_languages": list(manifest.supported_languages),
                "description": manifest.description,
                "estimated_speed": manifest.estimated_speed,
                "estimated_cost": manifest.estimated_cost,
                "confidence": manifest.confidence,
            }, indent=2),
            status="pending",
            created_at=now_iso(),
            reviewed_at=None,
        )
        insert_proposed_worker(conn, row)
        print(f"Proposed worker for capability '{cap}': {proposal_id}")
        print(f"Review it: friday capability review")
        return 0

    # Read from resolver_history for goals with UNRESOLVED or missing caps.
    from .db import get_proposed_workers
    history = get_resolver_history(conn)
    pending = get_proposed_workers(conn, status="pending")
    pending_gaps = {p.capability_gap for p in pending}

    # Group history records by graph_id to find goals with gaps.
    gaps_by_graph: dict = {}
    for h in history:
        if h.get("status") != "unresolved":
            continue
        gid = h.get("graph_id", "")
        if not gid:
            continue
        # Load the graph to get the goal.
        from .planning import TaskGraphEngine
        eng = TaskGraphEngine(conn)
        g = eng.graph_by_id(gid)
        goal = g.goal if g else "unknown"
        missing_str = h.get("missing_capabilities", "[]")
        try:
            missing_caps = json.loads(missing_str) if isinstance(missing_str, str) else missing_str
        except (json.JSONDecodeError, TypeError):
            missing_caps = []
        task_id = h.get("task_id", "")
        for cap in missing_caps:
            if cap not in pending_gaps:
                key = (gid, task_id, cap)
                if key not in gaps_by_graph:
                    gaps_by_graph[key] = (goal, task_id, cap, gid)

    if not gaps_by_graph:
        print("No unresolvable capability gaps found in history.")
        print("You can propose one explicitly: friday capability propose --capability <name>")
        return 0

    # Draft manifests for each gap.
    reset_gap_tracking()
    proposed_count = 0
    for (gid, tid, cap), (goal, task_id, cap_name, graph_id) in gaps_by_graph.items():
        created = propose_worker(
            conn, goal=goal, missing_capabilities=[cap_name],
            task_id=tid, graph_id=gid,
        )
        proposed_count += len(created)

    print(f"Proposed {proposed_count} new worker(s) from gap analysis.")
    print(f"Review them: friday capability review")
    return 0


def _cmd_capability_review(args: argparse.Namespace, conn) -> int:
    """List pending proposed workers and let the user approve/reject by id.

    Follows the `friday graph review` UX pattern exactly:
    - `friday capability review` lists pending proposals.
    - `friday capability review <id>` shows full detail.
    - `friday capability review approve <id>` approves and registers.
    - `friday capability review reject <id>` rejects.
    """
    from .db import (
        get_proposed_workers, get_proposed_worker,
        update_proposed_worker_status,
    )
    reg = WorkerRegistry(conn)

    action = getattr(args, "review_action", None)
    target = getattr(args, "review_target", None)

    if action == "approve" and target:
        pid = _resolve_proposal_id(target, conn)
        if pid is None:
            print(f"error: no pending proposal found matching '{target}'", file=sys.stderr)
            return 2
        success = register_approved_proposal(conn, pid, reg)
        if success:
            print(f"Approved and registered: {pid}")
            row = get_proposed_worker(conn, pid)
            if row:
                print(f"  Worker now available in the registry.")
        else:
            print(f"error: failed to register proposal '{pid}' — capabilities likely invalid",
                  file=sys.stderr)
            return 2
        return 0

    if action == "reject" and target:
        pid = _resolve_proposal_id(target, conn)
        if pid is None:
            print(f"error: no pending proposal found matching '{target}'", file=sys.stderr)
            return 2
        update_proposed_worker_status(conn, pid, "rejected")
        print(f"Rejected: {pid}")
        return 0

    if action and action not in ("approve", "reject"):
        # Treat as a target ID to show detail.
        target = action

    if target:
        return _show_proposal_detail(conn, reg, target)

    # List all pending proposals.
    pending = get_proposed_workers(conn, status="pending")
    if not pending:
        print("No pending worker proposals.")
        print("Proposals are auto-created when the resolver finds an unresolvable capability gap.")
        print("You can also propose one explicitly: friday capability propose --capability <name>")
        return 0

    # Read operator profile for passive annotation (Phase 2 — informational only).
    from .operator import build_operator_profile
    profile = build_operator_profile(conn)

    print(f"Pending worker proposals — {len(pending)}\n")
    for p in pending:
        short_id = p.id.split(":")[-1] if ":" in p.id else p.id
        try:
            manifest = json.loads(p.draft_manifest_json)
        except (json.JSONDecodeError, TypeError):
            manifest = {}
        name = manifest.get("name", "???")
        caps = ", ".join(manifest.get("capabilities", [])) or "-"
        print(f"  {p.capability_gap}")
        print(f"      id={short_id} | name={name} | caps: {caps}")
        print(f"      goal: {p.detected_from_goal}")
        # Phase 2: passive approval-rate annotation (informational only).
        # Phase 2: passive approval-rate annotation (informational only).
        # Only shown when the user has actually approved at least one proposal,
        # so an empty profile (no review history) produces byte-identical output.
        cap_rate = profile.capability_approval_rate
        if cap_rate and cap_rate["approved"] > 0:
            rate_pct = round(cap_rate["rate"] * 100)
            print(f"      profile: you've approved {cap_rate['approved']}/{cap_rate['total']} "
                  f"proposals ({rate_pct}%)")
        print(f"      -> friday capability review {short_id} for details")
        print()

    print("Actions:")
    print("  friday capability review <id>             Show full detail")
    print("  friday capability review approve <id>     Approve and register worker")
    print("  friday capability review reject <id>      Reject proposal")
    return 0


def _resolve_proposal_id(ref: str, conn) -> str:
    """Resolve a short reference to a full proposal id."""
    from .db import get_proposed_workers
    pending = get_proposed_workers(conn, status="pending")
    # Exact match on full id.
    for p in pending:
        if p.id == ref:
            return p.id
    # Match on short id (last segment).
    for p in pending:
        short = p.id.split(":")[-1] if ":" in p.id else p.id
        if short == ref:
            return p.id
    # Also check rejected proposals.
    rejected = get_proposed_workers(conn, status="rejected")
    for p in rejected:
        if p.id == ref:
            return p.id
        short = p.id.split(":")[-1] if ":" in p.id else p.id
        if short == ref:
            return p.id
    return None


def _show_proposal_detail(conn, reg, ref: str) -> int:
    """Show one proposal's full detail."""
    from .db import get_proposed_workers
    pending = get_proposed_workers(conn, status="pending")
    matched = None
    for p in pending:
        short = p.id.split(":")[-1] if ":" in p.id else p.id
        if p.id == ref or short == ref:
            matched = p
            break
    if matched is None:
        print(f"error: no pending proposal found: {ref}", file=sys.stderr)
        return 2

    print(f"Proposal: {matched.id}\n")
    print(f"Gap:           {matched.capability_gap}")
    print(f"Detected from: {matched.detected_from_goal}")
    print(f"Status:        {matched.status}")
    print(f"Created:       {matched.created_at}")
    print()

    try:
        manifest = json.loads(matched.draft_manifest_json)
    except (json.JSONDecodeError, TypeError):
        print("(invalid manifest JSON)")
        return 2

    print("Draft WorkerManifest:")
    print(f"  Name:             {manifest.get('name', '-')}")
    print(f"  Implementation:   {manifest.get('implementation', '-')}")
    print(f"  Provider:         {manifest.get('provider', '-')}")
    print(f"  Origin:           {manifest.get('origin', '-')}")
    print(f"  Capabilities:     {', '.join(manifest.get('capabilities', [])) or '-'}")
    print(f"  Requirements:     {', '.join(manifest.get('requirements', [])) or '-'}")
    print(f"  Task types:       {', '.join(manifest.get('supported_task_types', [])) or '-'}")
    print(f"  Plan types:       {', '.join(manifest.get('supported_plan_types', [])) or '-'}")
    print(f"  Languages:        {', '.join(manifest.get('supported_languages', [])) or '-'}")
    print(f"  Description:      {manifest.get('description', '-')}")
    print(f"  Speed:            {manifest.get('estimated_speed', '-')}")
    print(f"  Cost:             {manifest.get('estimated_cost', '-')}")
    print(f"  Confidence:       {manifest.get('confidence', '-')}")
    print()

    short_id = matched.id.split(":")[-1] if ":" in matched.id else matched.id
    print("Actions:")
    print(f"  friday capability review approve {short_id}")
    print(f"  friday capability review reject {short_id}")
    return 0


def cmd_capability(args: argparse.Namespace, conn=None) -> int:
    conn = conn or connect()
    reg = WorkerRegistry(conn)
    token = getattr(args, "token", None) or "list"

    # Map argparse positional args to review-specific attributes
    # (same pattern as cmd_graph dispatches action→review_action).
    raw_action = getattr(args, "action", None)
    raw_target = getattr(args, "target", None)
    args.review_action = raw_action
    args.review_target = raw_target
    # Also map --capability and --goal for the propose subcommand.
    cap = getattr(args, "capability", None)
    if cap and not getattr(args, "goal", None):
        args.goal = "manual"
    if token == "discover":
        res = discover(_EXTERNAL_MANIFESTS)
        print(f"Available ({len(res.available)}): {', '.join(res.available) or '-'}")
        print(f"Unavailable ({len(res.unavailable)}): {', '.join(res.unavailable) or '-'}")
        for w, deps in res.missing_deps.items():
            print(f"  {w}: missing {', '.join(deps)}")
        reg.sync_availability(res)
        return 0
    if token == "list":
        for w in reg.all_workers():
            print(w.to_summary())
        return 0
    if token == "info":
        name = getattr(args, "worker", None)
        w = reg.worker_by_name(name) if name else None
        if w is None:
            print("error: worker not found", file=__import__("sys").stderr)
            return 2
        print(w.to_detail())
        print(f"  Availability: {getattr(w, 'availability', 'available')}")
        return 0
    if token == "benchmark":
        runner = BenchmarkRunner(
            [BenchmarkTask(capability="Documentation", payload="write a doc",
                           expect_nonempty_stdout=True)],
            [("worker:native", lambda p: ("native ok", 0))])
        rep = runner.run()
        print(json.dumps({k: [r.__dict__ for r in v] for k, v in rep.items()}, indent=2))
        return 0
    if token == "propose":
        return _cmd_capability_propose(args, conn)
    if token == "review":
        return _cmd_capability_review(args, conn)
    return 2
