"""Meta-Engine CLI — `friday meta` commands."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import json

from .db import (
    connect,
    get_capability_gaps,
    get_capability_gap,
    get_si_runs,
    get_si_run,
    now_iso,
    update_si_run,
)
from .meta.loop import run_cycle
from .meta.deploy import deploy, approve, reject, promote
from .meta.verification import verify


def cmd_meta(args: argparse.Namespace) -> int:
    """Dispatch friday meta <action>."""
    action = args.action or "status"
    conn = connect()

    try:
        if action == "status":
            return _cmd_status(conn, args)
        elif action == "analyze":
            return _cmd_analyze(conn, args)
        elif action == "plan":
            return _cmd_plan(conn, args)
        elif action == "deploy":
            gap_id = args.gap_id or _prompt_gap_id(conn)
            if gap_id is None:
                print("error: specify --gap-id or have open gaps")
                return 1
            run_id = deploy(conn, gap_id)
            if run_id:
                print(f"Deploy staged as run #{run_id}")
                print(f"  Approve: friday meta approve --run-id {run_id}")
                print(f"  Reject:  friday meta reject --run-id {run_id}")
            return 0 if run_id else 1
        elif action == "approve":
            rid = args.run_id or _prompt_run_id(conn, "approve")
            if rid is None:
                return 1
            if approve(conn, rid):
                return 0
            return 1
        elif action == "reject":
            rid = args.run_id or _prompt_run_id(conn, "reject")
            if rid is None:
                return 1
            if reject(conn, rid):
                return 0
            return 1
        elif action == "promote":
            worker_name = args.worker
            if not worker_name:
                print("error: specify <name> to promote: friday meta promote <worker-name>")
                return 1
            if promote(conn, worker_name):
                return 0
            return 1
        elif action == "verify":
            gap_id = args.gap_id or _prompt_gap_id(conn)
            run_id = args.run_id
            if run_id is None:
                run_id = _prompt_run_id(conn, "verify")
            if gap_id is None or run_id is None:
                return 1
            _cmd_verify(conn, gap_id, run_id, args)
            return 0
        elif action == "run":
            return _cmd_run_cycle(conn, args)
        else:
            print(f"unknown action: {action}", file=sys.stderr)
            print("Available: status, analyze, plan, deploy, approve, reject, verify, run")
            return 2
    finally:
        conn.close()


def _cmd_status(conn, args: argparse.Namespace) -> int:
    """Show current meta-engine status."""
    gaps = get_capability_gaps(conn)
    runs = get_si_runs(conn)

    print("Meta-Engine — Friday's Self-Improvement Loop")
    print()

    # Gap summary.
    open_gaps = [g for g in gaps if g["status"] == "open"]
    planned = [g for g in gaps if g["status"] == "planned"]
    verifying = [g for g in gaps if g["status"] == "verifying"]
    deployed = [g for g in gaps if g["status"] == "deployed"]
    rejected = [g for g in gaps if g["status"] == "rejected"]

    print(f"  Gaps: {len(gaps)} total")
    print(f"  Open:     {len(open_gaps)}")
    print(f"  Planned:  {len(planned)}")
    print(f"  Building: {len(verifying)}")
    print(f"  Deployed: {len(deployed)}")
    print(f"  Rejected: {len(rejected)}")
    print()

    if open_gaps:
        print("  Top open gaps:")
        for g in open_gaps[:5]:
            score = g["score"]
            freq = g["frequency"]
            att = g["attempt_count"]
            print(f"    #{g['id']} [score={score:.1f} freq={freq} attempts={att}]")
            print(f"      {g['description'][:80]}")
        print()

    # Pending approvals.
    pending = [r for r in runs if not r.get("deployed") and r.get("human_approved") == 0]
    if pending:
        print("  Pending approval:")
        for r in pending[:5]:
            g = get_capability_gap(conn, r["gap_id"])
            desc = g["description"][:60] if g else "?"
            print(f"    run #{r['id']} -> gap #{r['gap_id']}: {desc}")
            print(f"      Approve: friday meta approve --run-id {r['id']}")
        print()

    if args.verbose:
        print("  All self-improvement runs:")
        for r in runs[:10]:
            status = "deployed" if r.get("deployed") else (
                "approved" if r.get("human_approved") else "staged")
            print(f"    run #{r['id']} gap=#{r['gap_id']} [{status}] {r.get('created_at', '?')[:19]}")
        print()

    return 0


def _cmd_analyze(conn, args: argparse.Namespace) -> int:
    """Run gap analysis and display results."""
    from .meta.gap_analyzer import analyze
    report = analyze(conn)
    print(report.to_text())
    return 0


def _cmd_plan(conn, args: argparse.Namespace) -> int:
    """Plan for a specific gap."""
    gap_id = args.gap_id or _prompt_gap_id(conn)
    if gap_id is None:
        print("error: specify --gap-id")
        return 1
    from .meta.si_planner import plan_for_gap
    plan_id = plan_for_gap(conn, gap_id)
    if plan_id:
        print(f"Plan generated: {plan_id}")
        return 0
    print("Planning failed or gap not plan-able")
    return 1


def _cmd_verify(conn, gap_id: int, run_id: int, args: argparse.Namespace) -> None:
    """Run verification and display results."""
    from .meta.sandbox import Sandbox
    sandbox = Sandbox(label=f"verify_gap_{gap_id}_run_{run_id}")
    sb_path = sandbox.create()
    print(f"Sandbox at {sb_path}")

    # Apply the deploy's diff so the sandbox has the new worker code.
    # Use a file-level approach: parse the diff and extract new files,
    # since git apply fails when the sandbox base commit differs.
    run = get_si_run(conn, run_id)
    if run:
        diff_path = run.get("diff_path") or ""
        if diff_path:
            from pathlib import Path
            dp = Path(diff_path)
            if dp.exists():
                from .meta.sandbox import _apply_diff_files
                _apply_diff_files(sandbox.sandbox_path, dp.read_text(encoding="utf-8"))
                print(f"  applied files from {diff_path}")
            else:
                print(f"  warning: diff not found at {diff_path}")

    result = verify(conn, gap_id, run_id, sandbox)

    # Persist the verification result back to the run record so that
    # `approve()` and `status` can read it.
    update_si_run(
        conn, run_id,
        verification_result=json.dumps(result.to_dict()),
        verification_log="\n".join(result.log),
        updated_at=now_iso(),
    )

    sandbox.cleanup()
    print(f"Verification: {'PASS' if result.passed else 'FAIL'}")
    if result.failure_reason:
        print(f"  Reason: {result.failure_reason}")
    print("Log:")
    for line in result.log:
        print(f"  {line}")


def _cmd_run_cycle(conn, args: argparse.Namespace) -> int:
    """Run one meta-loop cycle."""
    print("Meta-Engine: running analysis cycle...")
    report = run_cycle(conn, dry_run=args.dry_run, gap_id=args.gap_id)
    print(report.to_text())
    return 0


def _prompt_gap_id(conn) -> Optional[int]:
    """Prompt user to pick a gap from open ones."""
    gaps = get_capability_gaps(conn, status="open")
    if not gaps:
        gaps = get_capability_gaps(conn)
        open_list = [g for g in gaps if g["status"] == "open"]
        if not open_list:
            print("No open gaps found")
            return None
        gaps = open_list
    print("Open gaps:")
    for g in gaps[:10]:
        print(f"  #{g['id']}: [{g['score']}] {g['description'][:80]}")
    try:
        raw = input("Enter gap id: ").strip()
        return int(raw)
    except (ValueError, EOFError):
        return None


def _prompt_run_id(conn, action: str) -> Optional[int]:
    """Prompt user to pick a run."""
    runs = get_si_runs(conn)
    pending = [r for r in runs if not r.get("deployed")]
    if not pending:
        print(f"No pending runs to {action}")
        return None
    print(f"Available runs to {action}:")
    for r in pending[:10]:
        g = get_capability_gap(conn, r["gap_id"])
        desc = g["description"][:60] if g else "?"
        print(f"  #{r['id']}: gap #{r['gap_id']} — {desc}")
    try:
        raw = input(f"Enter run id to {action}: ").strip()
        return int(raw)
    except (ValueError, EOFError):
        return None


def add_subparser(sub) -> None:
    """Add the `meta` subcommand parser."""
    p = sub.add_parser(
        "meta",
        help="Self-improvement loop: detect gaps, plan, build, verify, deploy.",
    )
    p.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "analyze", "plan", "deploy", "approve", "reject", "promote", "verify", "run"],
        help="Action (default: status).",
    )
    p.add_argument("--gap-id", type=int, default=None, help="Capability gap ID.")
    p.add_argument("--run-id", type=int, default=None, help="Self-improvement run ID.")
    p.add_argument("--worker", default=None, help="Worker name for 'promote' action.")
    p.add_argument("--dry-run", action="store_true", help="Analyze only; don't plan.")
    p.add_argument("--verbose", "-v", action="store_true", help="Show detailed output.")
    p.set_defaults(func=cmd_meta)
