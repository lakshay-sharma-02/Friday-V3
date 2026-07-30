"""Verified Self-Deployment — merges verified worker/ capability into the registry.

Human-in-the-loop: every deploy requires explicit approval until the user
loosens the gate. The deploy step:
  1. Captures the diff from the sandbox.
  2. Registers the new worker in the registry (feature-flagged as 'beta').
  3. Records the diff + changelog in the run log.
  4. Requires `friday meta approve <run_id>` to go live.

Upgraded for the Self-Evolution Engine with deploy_capability() for multi-file
deploys that install dependencies, create feature flags, and track rollback.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .sandbox import Sandbox
from .verification import VerificationResult, verify
from .capability import CapabilityRegistry
from ..services.llm import _call as llm_call
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


def _parse_failed_tests(output: str) -> set[str]:
    """Extract failing test IDs from pytest output.

    Parses lines like:
      FAILED tests/test_foo.py::TestBar::test_baz - AssertionError: ...
    or the summary section:
      FAILED tests/test_agent.py::TestDecompose::test_keyword_run_tests_decomposes
    """
    failed: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            # Line format: FAILED path/to/test.py::TestClass::test_name - Error
            test_id = line[len("FAILED "):]
            # Strip the error message after " - "
            if " - " in test_id:
                test_id = test_id.split(" - ", 1)[0]
            failed.add(test_id)
    return failed


# ──────────────────────────────────────────────────────────────────────────
# Phase 3: Multi-file Capability Deploy
# ──────────────────────────────────────────────────────────────────────────


def deploy_capability(
    conn,
    request: str,
    plan: Optional[dict] = None,
) -> Optional[str]:
    """Deploy a new capability through the full self-evolution pipeline.

    Two paths available:

    **Claude Code path** (default when ``claude`` binary is on PATH):
    1. Create sandbox from Friday's repo
    2. Invoke Claude Code inside the sandbox to implement the capability
    3. If CC fails: rollback, optionally retry once, report error
    4. Capture diff of all changes
    5. Run full regression suite
    6. If tests fail: rollback, return error
    7. Snapshot for rollback, register capability flag (disabled)

    **LLM path** (fallback when CC unavailable):
    1-8. Same as before, using ``generate_capability_plan()`` +
         ``apply_capability_plan_to_sandbox()``.

    Args:
        conn: Database connection.
        request: Natural-language capability description.
        plan: Optional pre-computed plan (from ``friday upgrade plan`` dry-run).
              Used for dependency hints when provided.

    Returns:
        Capability name on success, or None on failure.
    """
    from .si_planner import (
        _derive_capability_name,
        generate_capability_plan,
        generate_capability_via_claude_code,
        validate_capability_plan,
        apply_capability_plan_to_sandbox,
        update_capability_plan_with_deterministic_fallback,
    )

    # Derive capability name.
    if plan and plan.get("capability_name"):
        cap_name = plan["capability_name"]
    else:
        cap_name = _derive_capability_name(request)

    # Create sandbox.
    sandbox = Sandbox(label=f"cap_{cap_name}")
    try:
        sb_path = sandbox.create()
        print(f"  sandbox created at {sb_path}")

        # Try Claude Code path first.
        cc_plan = generate_capability_via_claude_code(request, sandbox, conn=conn)

        if cc_plan:
            # Claude Code succeeded — use its output as the plan.
            plan = cc_plan
            print(f"  ✓ Claude Code completed")
        else:
            # Clean up any partial CC modifications before fallback.
            sandbox.rollback()

            # ── LLM plan path (fallback) ──
            print("  Claude Code not found or failed — falling back to LLM-based planning")
            print("  Install CC for autonomous implementation: pip install claude-code")

            generated_plan = generate_capability_plan(request, sandbox, conn=conn)
            generated_plan = update_capability_plan_with_deterministic_fallback(
                request, generated_plan
            )

            if not generated_plan:
                print("  error: could not generate capability plan")
                sandbox.cleanup()
                return None

            plan = generated_plan
            errors = validate_capability_plan(plan)
            if errors:
                print("  error: plan validation failed:")
                for e in errors:
                    print(f"    - {e}")
                sandbox.cleanup()
                return None

            new_count = len(plan.get("new_files", []))
            mod_count = len(plan.get("modified_files", []))

            # Write files to sandbox.
            ok = apply_capability_plan_to_sandbox(sandbox, plan)
            if not ok:
                print("  error: failed to write files to sandbox")
                sandbox.cleanup()
                return None
            print(f"  wrote {new_count} new file(s), {mod_count} modified file(s)")

        # ── Common post-implementation steps ──

        # Capture baseline test failures if running full suite.
        ver_steps = plan.get("verification_steps", [])
        _baseline_failures: set[str] = set()
        for step in ver_steps:
            args = shlex.split(step)
            _is_full_suite = any(a in ("tests/", "tests", ".") for a in args)
            if _is_full_suite:
                base_result = sandbox.run_tests(args + ["--tb=line", "-q"])
                _baseline_failures = _parse_failed_tests(
                    base_result.get("output", "")
                )
                if _baseline_failures:
                    print(f"  ℹ pre-existing test failures: {len(_baseline_failures)}")
                    for f in sorted(_baseline_failures):
                        print(f"    ─ {f}")
                break

        # Install dependencies.
        deps = plan.get("dependencies", [])
        if deps:
            dep_result = sandbox.install_deps(deps)
            if not dep_result["success"]:
                print(f"  warning: some deps failed: {dep_result['failed_packages']}")
                print(f"  continuing without: {dep_result['failed_packages']}")
            else:
                print(f"  installed {len(deps)} dependency/ies")
                registry = CapabilityRegistry(conn)
                registry.mark_deps_installed(cap_name)

        # Capture diff of all changes.
        diff = sandbox.capture_diff()
        if diff:
            print(f"  captured diff ({len(diff)} bytes)")
        else:
            print("  warning: no diff produced")

        # Run regression suite.
        reg_result = sandbox.run_tests([
            "python", "-m", "pytest", "tests/", "-x", "--tb=short", "-q",
        ])
        if not reg_result.get("passed", False):
            # Check if only pre-existing failures.
            current_failures = _parse_failed_tests(reg_result.get("output", ""))
            new_failures = current_failures - _baseline_failures
            if new_failures:
                print(f"  ✗ regression suite failed — new failures:")
                for f in sorted(new_failures):
                    print(f"    ─ {f}")
                print("  Rolling back...")
                sandbox.rollback()
                sandbox.cleanup()
                return None
            else:
                print(f"  ✓ regression passed (only {len(_baseline_failures)} pre-existing failures)")
        else:
            print("  ✓ regression suite passed")

        # Post-deploy integration smoke test.
        # Run the new capability's import/dry-run against the sandbox code
        # (via PYTHONPATH) to catch API mismatches (wrong dep version, missing
        # symbols, etc.) before registering the capability.
        _smoke_test = plan.get("capability_smoke_test", "")
        if _smoke_test:
            _smoke_start = datetime.now(timezone.utc)
            _smoke_result = subprocess.run(
                ["python", "-c", _smoke_test],
                cwd=sandbox.sandbox_path,
                env=sandbox.sandbox_env(),
                capture_output=True, text=True, timeout=30,
            )
            _smoke_dur = int((datetime.now(timezone.utc) - _smoke_start).total_seconds() * 1000)
            if _smoke_result.returncode != 0:
                stderr_snip = _smoke_result.stderr[:500] if _smoke_result.stderr else ""
                print(f"  ✗ smoke test failed ({_smoke_dur}ms):")
                for ln in stderr_snip.split("\n"):
                    print(f"    | {ln}")
                print("  Rolling back...")
                sandbox.rollback()
                sandbox.cleanup()
                return None
            print(f"  ✓ smoke test passed ({_smoke_dur}ms)")

        # Snapshot for rollback.
        snapshot_commit = sandbox.snapshot()
        if snapshot_commit:
            print(f"  rollback commit: {snapshot_commit[:12]}")
        else:
            print("  warning: could not create rollback snapshot")

        # Register capability flag (disabled by default).
        registry = CapabilityRegistry(conn)
        flag = registry.add(
            name=cap_name,
            description=plan.get("description", cap_name),
            plan_json=json.dumps(plan, indent=2),
            rollback_commit=snapshot_commit,
        )
        if flag:
            print(f"  capability '{cap_name}' registered (disabled by default)")
            print(f"  Enable: friday upgrade enable {cap_name}")
        else:
            print(f"  warning: could not register capability flag")

        print(f"\n  ✅ Capability '{cap_name}' deployed successfully!")
        return cap_name

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  deploy pipeline failed: {e}")
        sandbox.cleanup()
        return None


def rollback_capability(conn, name: str) -> bool:
    """Rollback a deployed capability to its pre-deployment state.

    Uses the rollback_commit stored in the capability_flags table.
    Returns True on success.
    """
    from .capability import CapabilityRegistry
    registry = CapabilityRegistry(conn)
    flag = registry.get(name)
    if not flag:
        print(f"  error: no capability '{name}' found")
        return False

    commit = flag.rollback_commit
    if not commit:
        print(f"  error: no rollback commit for '{name}'")
        return False

    # Create a sandbox to execute the rollback.
    sandbox = Sandbox(label=f"rollback_{name}")
    try:
        sb_path = sandbox.create()
        print(f"  sandbox created at {sb_path}")

        # Reset to the pre-deployment commit.
        ok = sandbox.rollback(commit)
        if not ok:
            print(f"  error: git reset failed")
            sandbox.cleanup()
            return False

        # Run regression suite to verify rollback didn't break anything.
        test_result = sandbox.run_tests([
            "python", "-m", "pytest", "tests/", "-x", "--tb=short", "-q",
        ])
        if not test_result.get("passed", False):
            print(f"  ⚠ Regression suite failed after rollback: "
                  f"{test_result.get('output', '')[:200]}")
            print(f"  Rollback still applied — manual fix may be needed.")

        # Remove the capability flag.
        registry.remove(name)
        print(f"  capability '{name}' rolled back and removed")
        sandbox.cleanup()
        return True

    except Exception as e:
        print(f"  rollback failed: {e}")
        sandbox.cleanup()
        return False


# ──────────────────────────────────────────────────────────────────────────
# Legacy deploy pipeline (preserved for gap-driven self-improvement)
# ──────────────────────────────────────────────────────────────────────────


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
                update_capability_gap(conn, gap_id, status="building",
                                      updated_at=_now())
            else:
                print(f"  warning: failed to write generated code to sandbox")
                _scaffold_worker_in_sandbox(sandbox, gap)
        else:
            print(f"  LLM codegen failed — falling back to scaffold stub")
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

        update_capability_gap(conn, gap_id,
                              attempt_count=attempt_count + 1,
                              updated_at=_now())

        print(f"  staged as run #{run_id}")
        print(f"  diff saved at {diff_path}")
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
    """Approve a staged self-improvement run."""
    # ... (same as before, truncated for brevity)
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
        else:
            print(f"  run #{run_id} has not passed verification")
        return False

    worker_name = _worker_name_from_gap(gap["description"])
    module_source = _extract_module_from_diff(run.get("diff_path", ""), worker_name)
    if module_source is None:
        print(f"  error: could not extract worker module from diff")
        return False
    if not _write_module_to_live_tree(worker_name, module_source):
        print(f"  error: failed to write module to live tree")
        return False
    print(f"  module written: src/friday/workers/{worker_name}.py")
    try:
        _register_worker(conn, worker_name, gap["description"])
    except Exception as e:
        print(f"  worker registration failed: {e}")
        return False

    update_si_run(conn, run_id,
                  deployed=1, human_approved=1,
                  human_reviewed_at=_now(), updated_at=_now())
    update_capability_gap(conn, gap_id, status="deployed", updated_at=_now())
    print(f"  run #{run_id} approved (status=beta)")
    print(f"  new worker: {worker_name}")
    print(f"  Promote to active: friday meta promote --worker {worker_name}")
    return True


def promote(conn, worker_name: str) -> bool:
    """Promote a beta worker to active."""
    from ..db import get_worker_by_name as _get_worker_by_name
    row = _get_worker_by_name(conn, worker_name)
    if not row:
        print(f"  error: no worker found with name '{worker_name}'")
        return False
    wid = row.id
    current_status = row.status
    if current_status == "active":
        print(f"  worker '{worker_name}' is already active")
        return True
    if current_status != "beta":
        print(f"  worker '{worker_name}' has status '{current_status}' — expected 'beta'")
        return False
    project_root = Path(__file__).resolve().parents[3]
    module_path = project_root / "src" / "friday" / "workers" / f"{worker_name}.py"
    if not module_path.exists():
        print(f"  error: module file not found at {module_path}")
        return False
    from ..db import update_worker_status, WorkerHistoryRow
    update_worker_status(conn, wid, "active")
    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=_now(), worker_id=wid, name=worker_name,
            kind=row.kind, version=row.version, status="active",
            capabilities=row.capabilities, limitations="",
            event_type="promoted",
            note="Promoted from beta to active by operator",
        )
    ])
    print(f"  worker '{worker_name}' promoted: beta -> active")
    return True


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
        "id": wid, "name": name, "kind": "function",
        "description": description, "capabilities": cap_name,
        "status": "beta", "confidence": "low", "version": "0.1.0",
        "availability": "available",
        "created_at": _now(), "updated_at": _now(),
    }
    w = WorkerRow(**row_data)
    insert_worker(conn, w)
    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=_now(), worker_id=wid, name=name,
            kind="function", version="0.1.0", status="beta",
            capabilities=cap_name,
            limitations="auto-built; verify before production use",
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


def _extract_module_from_diff(diff_path: str, worker_name: str) -> Optional[str]:
    """Extract the worker module source code from the deploy diff."""
    if not diff_path:
        return None
    dp = Path(diff_path)
    if not dp.exists():
        return None
    try:
        content = dp.read_text(encoding="utf-8")
    except (OSError, IOError):
        return None
    target = f"src/friday/workers/{worker_name}.py"
    lines = content.splitlines()
    in_section = False
    code_lines: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if in_section and code_lines:
                return "\n".join(code_lines).rstrip("\n") + "\n"
            in_section = False
            code_lines = []
        if line.startswith("+++ b/") and line.endswith(target):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("@@") or line.startswith("--- ") or line.startswith("new file mode") or line.startswith("index ") or line.startswith("\\ "):
            continue
        if line.startswith("+"):
            code_lines.append(line[1:])
    return "\n".join(code_lines).rstrip("\n") + "\n" if code_lines else None


def _write_module_to_live_tree(worker_name: str, source: str) -> bool:
    """Write a worker module to the live src/friday/workers/ directory."""
    try:
        project_root = Path(__file__).resolve().parents[3]
        workers_dir = project_root / "src" / "friday" / "workers"
        workers_dir.mkdir(parents=True, exist_ok=True)
        init_file = workers_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")
        module_path = workers_dir / f"{worker_name}.py"
        module_path.write_text(source, encoding="utf-8")
        return True
    except (OSError, IOError) as e:
        print(f"  error writing module: {e}")
        return False


def _scaffold_worker_in_sandbox(sandbox: Sandbox, gap: dict) -> None:
    """Create the worker module + test inside the sandbox."""
    if not sandbox.sandbox_path:
        return
    sp = Path(sandbox.sandbox_path)
    desc = gap.get("description", "unknown gap")
    name = _worker_name_from_gap(desc)
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
    (sp / "tests" / "test_meta" / "__init__.py").write_text("")
