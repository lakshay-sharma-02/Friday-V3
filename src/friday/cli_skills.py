"""CLI command for Pillar B Stage 4 — Skill Formation read/invoke surface.

``friday skills``              — list all formed skills with their worker status.
``friday skills run <name>``   — invoke a formed skill by worker name.
``friday skills drift``        — analyze all formed skills for degradation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time

from .db import connect, get_worker_by_name
from .runtime.executors import resolve_executor


def cmd_skills(args: argparse.Namespace) -> int:
    """Dispatch ``friday skills [list|run|drift]``."""
    action = getattr(args, "action", None)
    if action == "run":
        return _run(args)
    elif action == "drift":
        return _drift(args)
    else:
        return _list()


def _list() -> int:
    """List all formed skills with worker name, status, step count, invocations."""
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT fs.id, fs.workflow_intent_id, fs.task_graph,
                   fs.exemplars, fs.invocation_count, fs.last_invoked_at,
                   fs.created_at,
                   w.id AS worker_id, w.name AS worker_name,
                   w.status, w.confidence
            FROM formed_skills fs
            LEFT JOIN workers w
                ON w.manifest_ref = 'formed_skill:' || CAST(fs.id AS TEXT)
            ORDER BY fs.created_at DESC
        """).fetchall()

        if not rows:
            print("No formed skills yet.")
            print()
            print("Formed skills are created from workflow intents. To seed:")
            print("  friday patterns mine    — mine action patterns")
            print("  friday patterns label   — label workflow intents")
            print("  friday patterns form    — create skills from intents")
            return 0

        print(f"Formed Skills ({len(rows)}):")
        print()
        for r in rows:
            task_graph = json.loads(r["task_graph"]) if r["task_graph"] else []
            step_count = len(task_graph)
            invocations = r["invocation_count"] or 0
            status = r["status"] or "unknown"
            confidence = r["confidence"] or "-"

            worker_label = r["worker_name"] or f"skill #{r['id']}"
            worker_id = r["worker_id"] or "-"
            print(f"  {worker_label}")
            print(f"    Worker ID:  {worker_id}")
            print(f"    Status:     {status}")
            print(f"    Confidence: {confidence}")
            print(f"    Steps:      {step_count}")
            if invocations > 0:
                last_ts = r["last_invoked_at"]
                invoked_str = f"{invocations}x (last: {last_ts[:19]})" if last_ts else f"{invocations}x"
            else:
                invoked_str = "never"
            print(f"    Invoked:    {invoked_str}")

            # Show shadow run status for proposed skills.
            if status == "proposed":
                try:
                    from .db import count_recent_shadow_runs
                    shadow_clean = count_recent_shadow_runs(conn, r["id"], 5)
                    if shadow_clean > 0:
                        from .db import get_shadow_runs_for_skill
                        shadow_rows = get_shadow_runs_for_skill(conn, r["id"], 1)
                        if shadow_rows:
                            last_score = shadow_rows[0]["overall_match_score"]
                            print(f"    Shadow:     {shadow_clean} clean run(s), "
                                  f"last score {last_score:.0%}")
                        else:
                            print(f"    Shadow:     {shadow_clean} clean run(s)")
                    else:
                        print(f"    Shadow:     waiting for first run")
                except Exception:
                    pass

            # Show canary promotion progress for beta skills.
            if status == "beta":
                try:
                    inv = r["invocation_count"] or 0
                    from .skill_formation import _CANARY_PROMOTION_THRESHOLD
                    progress = min(inv, _CANARY_PROMOTION_THRESHOLD)
                    needed = max(0, _CANARY_PROMOTION_THRESHOLD - inv)
                    if needed > 0:
                        print(f"    Promo:      {progress}/{_CANARY_PROMOTION_THRESHOLD} execs")
                    else:
                        print(f"    Promo:      {progress}/{_CANARY_PROMOTION_THRESHOLD} execs ✓")
                except Exception:
                    pass

            print()

        print("Actions:")
        print("  friday skills run <name>    Invoke a formed skill by worker name")
        print("  friday skills drift         Analyze skills for degradation")
    finally:
        conn.close()
    return 0


def _drift(args: argparse.Namespace) -> int:
    """Run drift detection on all formed skills with sufficient replay history."""
    from .skill_formation import detect_skill_drift, format_drift_reports

    conn = connect()
    try:
        reports = detect_skill_drift(conn)
        output = format_drift_reports(reports)
        print(output)
        unhealthy = sum(1 for r in reports if r.overall_health == "unhealthy")
        degrading = sum(1 for r in reports if r.overall_health == "degrading")
        if unhealthy > 0:
            print()
            print(f"🔴 {unhealthy} skill(s) need attention. "
                  f"Run 'friday patterns form --force' to re-form.")
        return 0 if degrading + unhealthy == 0 else 1
    finally:
        conn.close()


def _run(args: argparse.Namespace) -> int:
    """Invoke a formed skill by worker name."""
    name = getattr(args, "name", None)
    if not name:
        print("error: skill name required (friday skills run <name>)", file=sys.stderr)
        return 2

    on_failure = getattr(args, "on_failure", None)
    if on_failure is not None and on_failure not in ("abort", "skip", "retry_alt"):
        print(f"error: invalid --on-failure '{on_failure}' "
              f"(choose from: abort, skip, retry_alt)", file=sys.stderr)
        return 2

    conn = connect()
    try:
        worker = get_worker_by_name(conn, name)
        if worker is None:
            print(f"error: no worker found with name '{name}'", file=sys.stderr)
            print("Run `friday skills` to see available skills.", file=sys.stderr)
            return 2

        if worker.kind != "formed_skill" and worker.worker_kind != "formed_skill":
            print(f"error: '{name}' is not a formed skill (kind={worker.kind})", file=sys.stderr)
            return 2

        wid = worker.id
        manifest_ref = worker.manifest_ref or ""

        # Proposed skills should warn that they're in shadow mode.
        if worker.status == "proposed":
            print(f"⚠  Skill '{name}' has status 'proposed' and is running in shadow mode.")
            print("   Shadow mode simulates execution without side effects.")
            print("   Run `friday skills` to check shadow run progress.")
            print()

        # Confirm gate: show skill details and ask to proceed.
        print(f"Skill:    {name}")
        print(f"Worker:   {wid}")
        print(f"Status:   {worker.status}")
        print(f"On fail:  {on_failure or 'abort (auto)'}")
        if manifest_ref and manifest_ref.startswith("formed_skill:"):
            try:
                skill_id = int(manifest_ref.split(":")[1])
                fs = conn.execute(
                    "SELECT task_graph, exemplars FROM formed_skills WHERE id = ?",
                    (skill_id,)
                ).fetchone()
                if fs:
                    tg = json.loads(fs["task_graph"]) if fs["task_graph"] else []
                    print(f"Steps:    {len(tg)}")
                    for i, (action_type, target) in enumerate(tg, 1):
                        print(f"  {i}. {action_type} ({target})")
            except (ValueError, IndexError, json.JSONDecodeError):
                pass
        print()

        try:
            response = input("Proceed with skill execution? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            response = "n"
        if response != "y":
            print("Execution cancelled.")
            return 0

        # Build the executor via resolve_executor (handles ReplayExecutor construction).
        executor = resolve_executor(wid)
        if executor is None:
            print(f"error: could not resolve executor for {wid}", file=sys.stderr)
            return 2

        # If executor is a ReplayExecutor, set its strategy.
        if hasattr(executor, "_on_failure"):
            if on_failure is None:
                executor._on_failure = "abort"
                executor._auto_downgrade = True
            else:
                executor._on_failure = on_failure
                executor._auto_downgrade = False

        from .skill_formation import _MiniTask

        task = _MiniTask(payload="", ref=manifest_ref)

        print("Executing...")
        t0 = _time.monotonic()
        result = executor.execute(task)
        dur = int((_time.monotonic() - t0) * 1000)

        if result.success:
            print(f"✓ Skill completed in {dur}ms")
            if result.stdout:
                try:
                    output = json.loads(result.stdout)
                    step_results = output.get("results", [])
                    for sr in step_results:
                        mark = "✓" if sr.get("success") else "✗" if sr.get("skipped") else "—"
                        action = sr.get("action", "?")
                        target = sr.get("target", "")
                        info = ""
                        if sr.get("error"):
                            info = f" ({sr['error']})"
                        elif sr.get("reason"):
                            info = f" ({sr['reason']})"
                        print(f"  {mark} {action} -> {target}{info}")
                except (json.JSONDecodeError, TypeError):
                    output_preview = result.stdout[:200]
                    print(f"  Output: {output_preview}")
        else:
            print(f"✗ Skill failed in {dur}ms")
            if result.error:
                print(f"  Error: {result.error}")
            if result.stdout:
                print(f"  Output: {result.stdout[:300]}")

        return 0 if result.success else 1
    finally:
        conn.close()
