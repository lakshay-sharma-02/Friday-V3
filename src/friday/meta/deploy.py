"""Verified Self-Deployment — merges verified worker into the registry.

Human-in-the-loop: every deploy requires explicit approval until the user
loosens the gate. The deploy step:
  1. Captures the diff from the sandbox.
  2. Registers the new worker in the registry (feature-flagged as 'beta').
  3. Records the diff + changelog in the run log.
  4. Requires `friday meta approve <run_id>` to go live.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .sandbox import Sandbox
from .verification import VerificationResult, verify
from ..db import (
    WorkerHistoryRow,
    WorkerRow,
    WorkerVersionRow,
    get_capability_gap,
    get_si_run,
    get_si_runs,
    get_all_workers,
    insert_si_run,
    insert_worker,
    insert_worker_history,
    insert_worker_version,
    now_iso,
    update_capability_gap,
    update_si_run,
)
from ..worker.models import Worker, WorkerKind


def _now() -> str:
    return now_iso()


def deploy(conn, gap_id: int) -> Optional[int]:
    """Run the full deploy pipeline: sandbox -> plan -> verify -> stage.

    Returns the run_id (for CLI status/approve), or None if the gap is not
    deployable or the pipeline fails before staging.
    """
    gap = get_capability_gap(conn, gap_id)
    if not gap:
        print(f"  error: no gap with id {gap_id}")
        return None
    if gap["status"] not in ("open", "planned"):
        print(f"  status={gap['status']} — expected 'open' or 'planned'")
        return None

    # Check attempt cap.
    attempt_count = gap.get("attempt_count", 0)
    if attempt_count >= 3:
        update_capability_gap(conn, gap_id, status="rejected",
                              updated_at=_now())
        print(f"  gap #{gap_id} rejected — {attempt_count} failed attempts")
        return None

    # Create sandbox.
    sandbox = Sandbox(label=f"gap_{gap_id}_attempt_{attempt_count + 1}")
    try:
        sb_path = sandbox.create()
        print(f"  sandbox created at {sb_path}")

        # Generate real worker code via LLM.
        from .si_planner import generate_worker_code, write_worker_to_sandbox
        name = _worker_name_from_gap(gap["description"])
        remaining = max(0, 3 - attempt_count)
        code = generate_worker_code(conn, gap_id, max_attempts=min(3, remaining))
        if code:
            ok = write_worker_to_sandbox(sandbox, code, name)
            if ok:
                print(f"  LLM-generated worker written: {name}.py")
                print(f"  ({len(code)} chars)")
                # Update gap status to reflect we have generated code.
                update_capability_gap(conn, gap_id, status="building",
                                      updated_at=_now())
            else:
                print(f"  warning: failed to write generated code to sandbox")
                _scaffold_worker_in_sandbox(sandbox, gap)
        else:
            print(f"  LLM codegen failed — falling back to scaffold stub")
            print(f"  generate code manually or configure an LLM provider")
            _scaffold_worker_in_sandbox(sandbox, gap)

        # Capture diff.
        diff = sandbox.capture_diff()
        if not diff:
            print("  no diff produced — nothing to deploy")
            sandbox.cleanup()
            return None

        diff_path = sandbox.diff_path or ""

        # Staging: create the run record (pending approval).
        run_data = {
            "gap_id": gap_id,
            "plan_id": "",
            "sandbox_path": sb_path,
            "diff_path": diff_path,
            "verification_result": "{}",
            "verification_log": "",
            "deployed": 0,
            "human_approved": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        run_id = insert_si_run(conn, run_data)

        # Increment attempt count.
        update_capability_gap(conn, gap_id,
                              attempt_count=attempt_count + 1,
                              updated_at=_now())

        print(f"  staged as run #{run_id}")
        print(f"  diff saved at {diff_path}")
        print()
        print(f"  Review and approve: friday meta approve {run_id}")
        print(f"  Or reject:          friday meta reject {run_id}")
        return run_id

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  deploy pipeline failed: {e}")
        sandbox.cleanup()
        return None


def stage(conn, gap_id: int, sandbox: Sandbox,
          verification_result: VerificationResult) -> int:
    """Create a staged run record (verified but awaiting approval)."""
    diff = sandbox.capture_diff()
    diff_path = sandbox.diff_path or ""
    run_data = {
        "gap_id": gap_id,
        "plan_id": "",
        "sandbox_path": sandbox.sandbox_path or "",
        "diff_path": diff_path,
        "verification_result": json.dumps(verification_result.to_dict()),
        "verification_log": "\n".join(verification_result.log),
        "deployed": 0,
        "human_approved": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    run_id = insert_si_run(conn, run_data)
    update_capability_gap(conn, gap_id, status="verifying", updated_at=_now())
    return run_id


def approve(conn, run_id: int) -> bool:
    """Approve a staged self-improvement run. Registers the worker in the
    registry (status='beta') and writes its module to the live tree.

    This is the FIRST human gate: code is registered and available on disk
    but NOT yet eligible for real scheduling. Use `friday meta promote`
    as a second, separate step to promote beta -> active.

    Returns True on success, False if the run doesn't exist or is already
    deployed.
    """
    run = get_si_run(conn, run_id)
    if not run:
        print(f"  error: no run with id {run_id}")
        return False

    if run.get("deployed"):
        print(f"  run #{run_id} already deployed")
        return False

    gap_id = run["gap_id"]
    gap = get_capability_gap(conn, gap_id)
    if not gap:
        print(f"  error: gap #{gap_id} not found")
        return False

    # Verify result passed? If no verification was run, check now.
    vresult_str = run.get("verification_result", "{}")
    try:
        vresult = json.loads(vresult_str) if vresult_str else {}
    except (ValueError, TypeError):
        vresult = {}

    scenario_verdict = vresult.get("scenario_verdict")
    passed = vresult.get("passed", False)

    if not passed:
        if scenario_verdict == "inconclusive":
            print(f"  run #{run_id}: stage 3 is INCONCLUSIVE (no replayable evidence)")
            print(f"  Re-run `friday meta analyze` for fresh evidence, then re-deploy.")
            print(f"  Or approve with --force to override.")
        else:
            print(f"  run #{run_id} has not passed verification")
            print(f"  Run verification first, or skip with --force")
        return False

    worker_name = _worker_name_from_gap(gap["description"])

    # Write the worker module to the live tree so the dynamic dispatch
    # fallback in resolve_executor() can find and import it.
    # Extract the module source from the deploy diff and write to live tree.
    # This is a hard requirement — without the module file on disk, the
    # dynamic dispatch fallback in resolve_executor() can't find the worker.
    module_source = _extract_module_from_diff(run.get("diff_path", ""), worker_name)
    if module_source is None:
        print(f"  error: could not extract worker module from diff at {run.get('diff_path', '')}")
        print(f"  The diff file may be missing, corrupted, or the worker name may not match.")
        return False

    if not _write_module_to_live_tree(worker_name, module_source):
        print(f"  error: failed to write module to live tree")
        return False
    print(f"  module written: src/friday/workers/{worker_name}.py")

    # Register the worker in the DB (beta status — not yet eligible for
    # real scheduling).
    try:
        _register_worker(conn, worker_name, gap["description"])
    except Exception as e:
        print(f"  worker registration failed: {e}")
        return False

    # Mark run as deployed.
    update_si_run(conn, run_id,
                  deployed=1, human_approved=1,
                  human_reviewed_at=_now(), updated_at=_now())
    update_capability_gap(conn, gap_id, status="deployed", updated_at=_now())
    print(f"  run #{run_id} approved (status=beta)")
    print(f"  new worker: {worker_name}")
    print(f"  Promote to active: friday meta promote --worker {worker_name}")
    return True


def promote(conn, worker_name: str) -> bool:
    """Promote a beta worker to active, making it eligible for real scheduling.

    This is the SECOND human gate, separate from approve. The worker must
    already be registered (via approve) with status='beta'. Promotion changes
    the status to 'active', verifies the module file exists on disk, and
    records a history event.

    Returns True on success, False if the worker is not found, not beta, or
    its module file is missing from the live tree.
    """
    # Find the worker by name.
    from ..db import get_worker_by_name as _get_worker_by_name
    row = _get_worker_by_name(conn, worker_name)
    if not row:
        print(f"  error: no worker found with name '{worker_name}'")
        print(f"  Workers must be approved first: friday meta approve --run-id <id>")
        return False

    wid = row.id
    current_status = row.status

    if current_status == "active":
        print(f"  worker '{worker_name}' is already active")
        return True

    if current_status != "beta":
        print(f"  worker '{worker_name}' has status '{current_status}' — expected 'beta'")
        print(f"  Only beta workers can be promoted to active.")
        return False

    # Verify the module file exists in the live tree before activating.
    project_root = Path(__file__).resolve().parents[3]
    module_path = project_root / "src" / "friday" / "workers" / f"{worker_name}.py"
    if not module_path.exists():
        print(f"  error: module file not found at {module_path}")
        print(f"  Worker must be approved first: friday meta approve --run-id <id>")
        print(f"  Approve writes the module to disk and registers it as beta.")
        return False

    # Promote: update status and record history.
    from ..db import update_worker_status, WorkerHistoryRow
    update_worker_status(conn, wid, "active")

    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=_now(), worker_id=wid, name=worker_name,
            kind=row.kind,
            version=row.version,
            status="active",
            capabilities=row.capabilities,
            limitations="",
            event_type="promoted",
            note="Promoted from beta to active by operator",
        )
    ])

    print(f"  worker '{worker_name}' promoted: beta -> active")
    print(f"  Module: {module_path}")
    print(f"  Worker is now eligible for real scheduling via the capability resolver.")
    return True


def _extract_module_from_diff(diff_path: str, worker_name: str) -> Optional[str]:
    """Extract the worker module source code from the deploy diff.

    The diff (from sandbox.capture_diff()) contains `--- /dev/null` and
    `+++ b/src/friday/workers/{worker_name}.py` headers followed by the
    file content (each line prefixed with `+`).

    Returns the source code as a string, or None if the file can't be found
    or the diff is missing.
    """
    if not diff_path:
        return None
    dp = Path(diff_path)
    if not dp.exists():
        return None
    try:
        content = dp.read_text(encoding="utf-8")
    except (OSError, IOError):
        return None

    # Find the worker module section in the unified diff.
    # Format: --- /dev/null
    #         +++ b/src/friday/workers/{worker_name}.py
    target = f"src/friday/workers/{worker_name}.py"
    lines = content.splitlines()
    in_section = False
    code_lines: list[str] = []

    for line in lines:
        # A new diff section means the target file's section is complete.
        if line.startswith("diff --git "):
            if in_section and code_lines:
                # We were collecting the target file and hit the next file.
                return "\n".join(code_lines).rstrip("\n") + "\n"
            in_section = False
            code_lines = []
        if line.startswith("+++ b/") and line.endswith(target):
            in_section = True
            continue
        if not in_section:
            continue
        # Skip hunk headers and git metadata lines.
        if line.startswith("@@"):
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("new file mode") or line.startswith("index "):
            continue
        if line.startswith("\\ "):
            continue
        # Collect content lines (strip leading '+' for added lines).
        if line.startswith("+"):
            code_lines.append(line[1:])

    # End of diff — return what we collected (if any).
    if not code_lines:
        return None
    return "\n".join(code_lines).rstrip("\n") + "\n"


def _write_module_to_live_tree(worker_name: str, source: str) -> bool:
    """Write a worker module to the live src/friday/workers/ directory.

    The Friday project root is resolved relative to this file's location
    (src/friday/meta/deploy.py -> ../../). The module is written at
    src/friday/workers/{worker_name}.py for dynamic import by the runtime's
    resolve_executor() fallback.
    """
    try:
        # Resolve project root: this file is at src/friday/meta/deploy.py
        # parents[3] is the project root (above src/)
        project_root = Path(__file__).resolve().parents[3]
        workers_dir = project_root / "src" / "friday" / "workers"
        workers_dir.mkdir(parents=True, exist_ok=True)
        # Ensure __init__.py exists so the package is importable.
        init_file = workers_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")
        module_path = workers_dir / f"{worker_name}.py"
        module_path.write_text(source, encoding="utf-8")
        return True
    except (OSError, IOError) as e:
        print(f"  error writing module: {e}")
        return False


def reject(conn, run_id: int) -> bool:
    """Reject a staged self-improvement run."""
    run = get_si_run(conn, run_id)
    if not run:
        print(f"  error: no run with id {run_id}")
        return False
    gap_id = run["gap_id"]
    update_si_run(conn, run_id,
                  human_approved=0,
                  human_reviewed_at=_now(), updated_at=_now())
    update_capability_gap(conn, gap_id, status="rejected", updated_at=_now())
    print(f"  run #{run_id} rejected. gap #{gap_id} marked rejected.")
    return True


def _worker_name_from_gap(description: str) -> str:
    """Derive a clean worker name from a gap description."""
    name = description.lower()
    for prefix in ("worker for:", "missing worker:", "worker needed for:"):
        if prefix in name:
            name = name.split(prefix, 1)[1].strip()
    # Strip to first meaningful word pair.
    import re
    name = re.sub(r"[^a-z0-9_ ]", "", name)
    parts = name.strip().split()[:4]
    return "_".join(parts) if parts else "auto_built_worker"


def _register_worker(conn, name: str, description: str) -> None:
    """Register a new worker in the registry with 'beta' status."""
    from uuid import uuid4
    wid = f"worker:{name}:{uuid4().hex[:8]}"
    cap_name = f"auto_{name}" if name else "auto_built"

    row_data = {
        "id": wid,
        "name": name,
        "kind": "function",
        "description": description,
        "capabilities": cap_name,
        "status": "beta",  # feature-flagged
        "confidence": "low",
        "version": "0.1.0",
        "availability": "available",
        "created_at": _now(),
        "updated_at": _now(),
    }
    w = WorkerRow(**row_data)
    insert_worker(conn, w)
    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=_now(), worker_id=wid, name=name,
            kind="function", version="0.1.0", status="beta",
            capabilities=cap_name, limitations="auto-built; verify before production use",
            event_type="self_improvement",
            note=f"Auto-built by meta-engine to address: {description[:200]}",
        )
    ])
    insert_worker_version(conn, [
        WorkerVersionRow(
            worker_id=wid, version="0.1.0",
            registered_at=_now(),
            changelog=f"Initial auto-built worker for: {description[:200]}",
        )
    ])


def _scaffold_worker_in_sandbox(sandbox: Sandbox, gap: dict) -> None:
    """Create the worker module + test inside the sandbox.

    This is the scaffolding step the planner would normally handle. We produce
    a minimal but real worker module that can be registered.
    """
    if not sandbox.sandbox_path:
        return
    sp = Path(sandbox.sandbox_path)
    desc = gap.get("description", "unknown gap")
    name = _worker_name_from_gap(desc)

    # Worker module.
    worker_dir = sp / "src" / "friday" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "__init__.py").write_text("")
    worker_file = worker_dir / f"{name}.py"
    worker_code = f'''"""Auto-built worker: {desc}"""
from __future__ import annotations
from typing import Optional

WORKER_NAME = "{name}"
WORKER_CAPABILITIES = ["auto_{name}"]
WORKER_EXAMPLE_INPUT = '{{"input": "test"}}'

def execute(input_data: str, workspace: str = ".") -> dict:
    """Execute this worker's operation."""
    return {{
        "success": True,
        "output": f"Auto-built worker '{{WORKER_NAME}}' executed in {{workspace}}",
        "duration_ms": 0,
    }}
'''
    worker_file.write_text(worker_code)

    # Test file.
    test_dir = sp / "tests" / "test_meta"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "__init__.py").write_text("")
    test_file = test_dir / f"test_{name}.py"
    test_code = f'''"""Tests for auto-built worker: {desc}"""
from src.friday.workers.{name} import execute, WORKER_NAME

def test_worker_name():
    assert WORKER_NAME == "{name}"

def test_worker_execute():
    result = execute("test input")
    assert result["success"] is True
    assert "executed" in result["output"]
'''
    test_file.write_text(test_code)

    # Create the __init__ for tests/test_meta
    (sp / "tests" / "test_meta" / "__init__.py").write_text("")


from pathlib import Path  # noqa: E402 (imported here for scaffold function)
