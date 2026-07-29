"""Self-Improvement Planner — generates multi-file capability plans.

Upgraded for the Self-Evolution Engine: replaces the single-worker generator
with a multi-file capability planner that outputs JSON plans covering:

  - Multiple new files
  - Multiple modified files (full content, not diffs)
  - Dependency changes
  - Config/env var changes
  - Test files
  - Verification steps
  - Rollback risk assessment

The old single-worker generator is preserved as generate_worker_code() for
backward compatibility with gap-driven self-improvement.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from typing import Any, Optional

from ..db import get_capability_gap, get_all_workers, now_iso, update_capability_gap
from ..services.llm import _call as llm_call, _enabled as llm_enabled
from .sandbox import Sandbox


# ──────────────────────────────────────────────────────────────────────────
# Phase 2: Multi-file Capability Planner
# ──────────────────────────────────────────────────────────────────────────

CAPABILITY_SYSTEM_PROMPT = textwrap.dedent("""\
You are Friday's self-evolution engine. You upgrade Friday's capabilities.
You receive a capability request and produce a structured plan of changes.

You must output a JSON plan:

```json
{
  "capability_name": "voice_support",
  "description": "Add speech-to-text and text-to-speech to Friday",
  "new_files": [
    {
      "path": "src/friday/services/voice.py",
      "content": "..."
    }
  ],
  "modified_files": [
    {
      "path": "src/friday/cli.py",
      "content": "..." // FULL file content after modification
    }
  ],
  "dependencies": ["edge-tts", "faster-whisper"],
  "config_changes": {
    "env_vars": ["FRIDAY_VOICE_ENABLED", "FRIDAY_TTS_VOICE"],
    "defaults": {"FRIDAY_VOICE_ENABLED": "false"}
  },
  "test_files": [
    {
      "path": "tests/test_voice.py",
      "content": "..."
    }
  ],
  "rollback_risk": "low" | "medium" | "high",
  "verification_steps": [
    "python -m pytest tests/test_voice.py -x",
    "python -m pytest tests/ -x --tb=short"
  ]
}
```

RULES:
- Every modified file must include the COMPLETE file content, not just the diff
- New files must include __init__.py entries if added to a package
- Dependencies must be real pip packages with correct names
- Rollback_risk assessment: low = new files only, no existing code changes; medium = modifies existing files; high = changes core architecture or DB schema
- Verification steps must be runnable commands
- Keep new files under 500 lines unless absolutely necessary
- Follow existing code style (type hints, docstrings, existing patterns)
- For new CLI commands, follow the pattern in cli.py (argparse, cmd_* functions)
- For new executors, follow the pattern in runtime/executors.py
- For new services, follow the pattern in services/ (llm.py, email.py)
- New capabilities must be feature-flagged (disabled by default)
- Output ONLY the JSON. No surrounding text, no markdown unless it wraps the JSON.
""")


def _get_codebase_map(sandbox: Sandbox) -> str:
    """Build a map of Friday's codebase structure for the planner prompt."""
    if not sandbox.sandbox_path:
        return "(sandbox not available)"
    sp = sandbox.sandbox_path
    parts: list[str] = []
    try:
        result = __import__("subprocess").run(
            ["find", "src/friday", "-name", "*.py", "-type", "f"],
            cwd=sp, capture_output=True, text=True, timeout=10,
        )
        files = result.stdout.strip().splitlines()[:50]
        parts.append("Key source files:")
        for f in files:
            parts.append(f"  {f}")
    except Exception:
        parts.append("(could not scan codebase)")
    return "\n".join(parts)


def _get_pyproject_deps(sandbox: Sandbox) -> str:
    """Read the pyproject.toml dependencies from the sandbox."""
    content = sandbox.read_file("pyproject.toml")
    if not content:
        return "(no pyproject.toml found)"
    lines = content.splitlines()
    deps_lines = [l for l in lines if "dependencies" in l.lower() or l.strip().startswith('"') or l.strip().startswith("'")]
    return "\n".join(deps_lines[:20]) if deps_lines else "(no deps section found)"


def generate_capability_plan(
    request: str,
    sandbox: Sandbox,
    conn=None,
) -> Optional[dict]:
    """Generate a multi-file capability plan from a natural-language request.

    Args:
        request: Natural-language capability request (e.g. "make yourself capable of speaking")
        sandbox: Sandbox with Friday's repo checkout (for codebase context).
        conn: Optional DB connection for reading existing workers/context.

    Returns:
        Parsed JSON plan dict, or None if LLM unavailable or generation fails.
    """
    if not llm_enabled():
        return None

    codebase_map = _get_codebase_map(sandbox)
    pyproject = _get_pyproject_deps(sandbox)

    # Include existing workers summary if conn is available.
    existing_workers = ""
    if conn:
        try:
            workers = get_all_workers(conn)
            if workers:
                summaries = []
                for w in workers:
                    caps = getattr(w, "capabilities", "") or ""
                    desc = getattr(w, "description", "") or ""
                    summaries.append(f"  - {w.name}: {caps} — {desc[:80]}")
                existing_workers = "EXISTING WORKERS:\n" + "\n".join(summaries)
        except Exception:
            pass

    user_prompt = textwrap.dedent(f"""\
    CAPABILITY REQUEST: {request}

    EXISTING CODEBASE STRUCTURE:
    {codebase_map}

    EXISTING DEPENDENCIES (pyproject.toml):
    {pyproject}

    {existing_workers}

    Generate a plan for this capability upgrade.
    """)

    print(f"  meta: generating capability plan for '{request[:60]}' via LLM...")

    try:
        raw = llm_call(CAPABILITY_SYSTEM_PROMPT, user_prompt)
        if not raw:
            return None

        # Strip markdown fences.
        text = raw.strip()
        if "```json" in text:
            m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
            if m:
                text = m.group(1)
        elif "```" in text:
            m = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
            if m:
                text = m.group(1)

        plan = json.loads(text)
        if not isinstance(plan, dict) or "capability_name" not in plan:
            return None

        # Validate required fields.
        required = ["capability_name", "new_files", "rollback_risk"]
        for r in required:
            if r not in plan:
                plan[r] = "" if r == "capability_name" else [] if r == "new_files" else "medium"

        return plan

    except (json.JSONDecodeError, Exception) as e:
        print(f"  meta: plan generation failed: {e}")
        return None


def validate_capability_plan(plan: dict) -> list[str]:
    """Validate a capability plan structure. Returns list of error messages.

    Empty list = plan is valid.
    """
    errors: list[str] = []

    if not plan.get("capability_name"):
        errors.append("Missing capability_name")
    if not isinstance(plan.get("new_files"), list):
        errors.append("new_files must be a list")
    else:
        for nf in plan["new_files"]:
            if not nf.get("path"):
                errors.append("new_file entry missing 'path'")
            if not nf.get("content"):
                errors.append(f"new_file '{nf.get('path', '?')}' missing 'content'")

    if not isinstance(plan.get("modified_files"), list):
        errors.append("modified_files must be a list")

    if plan.get("rollback_risk") not in ("low", "medium", "high"):
        plan["rollback_risk"] = "medium"

    return errors


def apply_capability_plan_to_sandbox(sandbox: Sandbox, plan: dict) -> bool:
    """Write a capability plan's new files and modified files into the sandbox.

    Returns True on success.
    """
    # Write new files.
    for nf in plan.get("new_files", []):
        path = nf.get("path", "")
        content = nf.get("content", "")
        if path and content:
            ok = sandbox.write_file(path, content)
            if not ok:
                print(f"  warning: failed to write new file {path}")

    # Write modified files (full content replacement).
    for mf in plan.get("modified_files", []):
        path = mf.get("path", "")
        content = mf.get("content", "")
        if path and content:
            ok = sandbox.write_file(path, content)
            if not ok:
                print(f"  warning: failed to write modified file {path}")

    return True


def estimate_plan_changes(plan: dict) -> str:
    """Return a human-readable summary of what a plan would change."""
    lines: list[str] = []
    new_count = len(plan.get("new_files", []))
    mod_count = len(plan.get("modified_files", []))
    dep_count = len(plan.get("dependencies", []))
    test_count = len(plan.get("test_files", []))

    if new_count:
        lines.append(f"Would create: {new_count} new file(s)")
        for nf in plan.get("new_files", [])[:5]:
            path = nf.get("path", "?")
            lines.append(f"    {path}")
    if mod_count:
        lines.append(f"Would modify: {mod_count} file(s)")
        for mf in plan.get("modified_files", [])[:5]:
            path = mf.get("path", "?")
            lines.append(f"    {path}")
    if dep_count:
        lines.append(f"Dependencies: {', '.join(plan.get('dependencies', []))}")
    if test_count:
        lines.append(f"Tests: {test_count} test file(s)")
    risk = plan.get("rollback_risk", "medium")
    lines.append(f"Risk: {risk}")
    lines.append("Rollback is safe (git revert)")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Legacy single-worker generator (preserved for gap-driven self-improvement)
# ──────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""\
You are Friday's self-improvement code generator. You write Python worker
modules that Friday can register and execute.

CONTRACT:
- Output a SINGLE Python file. No explanations, no markdown, no JSON wrapper.
- The file must define:
    WORKER_NAME: str       — short snake_case identifier
    WORKER_CAPABILITIES: list[str]  — capability tags
    WORKER_EXAMPLE_INPUT: str  — JSON string that is a valid minimal input to execute()
    def execute(input_data: str, workspace: str = ".") -> dict:
        \"\"\"Execute this worker's operation. Returns dict with keys:
           success: bool
           output: str   (human-readable result)
           artifacts: list[str] (optional, file paths created)
           error: str    (if success=False)
        \"\"\"

RULES:
- Use ONLY the Python standard library. No pip/poetry/conda deps.
- The worker runs inside Friday's own process space. Do NOT import or modify
  any friday.* module (that would be self-modifying in production — the
  Meta-Engine runs the code in a sandbox first, but write it as if it will
  eventually run inline).
- Accept input_data as a JSON string; parse it with json.loads.
- Keep it under 200 lines. Prefer simple implementation over clever.
- Include a __doc__ string describing what the worker does.
- **CRITICAL: return {"success": True, "output": ...} for non-zero exit codes
  from a subprocess you control.** Catch the failure and return the exit code
  and error in the "output" field.
- Place subprocess stdout/stderr in the "output" field. Only set the "error"
  field for actual exceptions/crashes, not for captured command failures.
- The worker runs in-process. Do NOT import socket (restricted for
  network safety). Other stdlib modules (os, subprocess, shutil) are
  permitted but use them responsibly — prefer pathlib for path operations.
- WORKER_EXAMPLE_INPUT must be a valid JSON string that execute() accepts
  without error. It will be used to generate the worker's smoke test.
  Match it to the real input shape shown in FAILURE EVIDENCE.

OUTPUT ONLY THE PYTHON FILE. NO SURROUNDING TEXT.
""")


def _evidence_summary(evidence_refs: str, conn=None) -> str:
    """Condense JSON evidence refs into a short text for the prompt.
    Handles both old-format strings and new-format integers.
    """
    import json
    try:
        refs = json.loads(evidence_refs) if evidence_refs else []
    except (ValueError, TypeError):
        refs = []
    if not refs:
        return "(no specific failure evidence available)"
    lines = []
    for r in refs[:5]:
        if isinstance(r, int) and conn is not None:
            row = conn.execute(
                "SELECT worker_id, payload, error, exit_code "
                "FROM runtime_results WHERE result_id = ?", (r,)).fetchone()
            if row:
                wid = row["worker_id"] or "?"
                err = (row["error"] or "")[:200]
                exit_c = row["exit_code"] or "?"
                payload = row["payload"] or ""
                try:
                    pdata = json.loads(payload) if payload else {}
                    inp = pdata.get("input", "")
                except (ValueError, TypeError):
                    inp = ""
                lines.append(f"  - runtime_result #{r}: worker={wid}, exit_code={exit_c}")
                if inp:
                    lines.append(f"    actual input: {repr(inp)[:300]}")
                if err:
                    lines.append(f"    error: {err}")
            else:
                lines.append(f"  - runtime_result #{r}: (not found)")
        else:
            lines.append(f"  - {str(r)[:200]}")
    return "\n".join(lines)


def _existing_capabilities_summary(conn) -> str:
    """Summarise existing workers so the LLM doesn't duplicate."""
    workers = get_all_workers(conn)
    if not workers:
        return "(no existing workers registered)"
    lines = []
    for w in workers:
        caps = getattr(w, "capabilities", "") or ""
        desc = getattr(w, "description", "") or ""
        lines.append(f"  - {w.name}: {caps} — {desc[:80]}")
    return "\n".join(lines)


def generate_worker_code(conn, gap_id: int,
                         max_attempts: int = 3) -> Optional[str]:
    """Generate a working Python worker module for this gap using the LLM.

    Preserved for backward compatibility with gap-driven self-improvement.
    """
    gap = get_capability_gap(conn, gap_id)
    if not gap:
        return None

    description = gap["description"]
    evidence = _evidence_summary(gap.get("evidence_refs", "[]"), conn=conn)
    existing = _existing_capabilities_summary(conn)

    if not llm_enabled():
        print(f"  meta: LLM not configured — cannot generate code for gap #{gap_id}")
        return None

    user_prompt = textwrap.dedent(f"""\
    GAP DESCRIPTION:
    {description}

    FAILURE EVIDENCE (from runtime_results):
    {evidence}

    EXISTING WORKERS (don't duplicate):
    {existing}

    IMPORTANT: The "actual input" lines in FAILURE EVIDENCE show the REAL
    value input_data will contain. Design execute() to handle THAT exact
    shape. If the evidence shows a raw command string, design for a string.
    Match WORKER_EXAMPLE_INPUT to the real input shape from the evidence.

    Generate a Python worker module that addresses this gap. Remember: only
    the Python code, no markdown fences, no explanation.
    """)

    print(f"  meta: generating worker code for gap #{gap_id} via LLM...")

    for attempt in range(max_attempts):
        raw = llm_call(SYSTEM_PROMPT, user_prompt)
        if not raw:
            print(f"  meta: LLM returned empty on attempt {attempt + 1}")
            continue

        code = _clean_code(raw)
        if not code:
            continue

        ok = _validate_code(code)
        if ok:
            print(f"  meta: valid code generated on attempt {attempt + 1}")
            return code
        print(f"  meta: generated code failed validation on attempt {attempt + 1}")

    print(f"  meta: all {max_attempts} attempts failed to generate valid code for "
          f"gap #{gap_id}")
    return None


def _clean_code(raw: str) -> Optional[str]:
    """Strip markdown fences, leading/trailing whitespace, return clean Python."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("python"):
            raw = raw[6:]
        raw = raw.strip()
    return raw if raw else None


def _validate_code(code: str) -> bool:
    """Check that the code is syntactically valid Python and defines the
    required interface. Uses AST parsing — never exec() the generated code.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    assigns: dict[str, ast.AST] = {}
    funcs: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigns[target.id] = node
        elif isinstance(node, ast.FunctionDef):
            funcs[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigns[node.target.id] = node

    if "WORKER_NAME" not in assigns:
        return False
    if "WORKER_CAPABILITIES" not in assigns:
        return False
    if "WORKER_EXAMPLE_INPUT" not in assigns:
        return False
    if "execute" not in funcs:
        return False

    fn = funcs["execute"]
    pos_args = [a for a in fn.args.args if a.arg not in ("self", "cls")]
    if len(pos_args) < 1:
        return False

    cap_node = assigns["WORKER_CAPABILITIES"]
    if isinstance(cap_node, ast.Assign) and len(cap_node.targets) == 1:
        val = cap_node.value
        if isinstance(val, ast.List):
            if not val.elts:
                return False
            if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in val.elts):
                return False
        elif isinstance(val, ast.Constant) and val.value is None:
            return False

    banned = frozenset({"socket"})
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                if name in banned:
                    imported.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                name = node.module.split(".")[0]
                if name in banned:
                    imported.add(name)
    if imported:
        return False

    return True


def write_worker_to_sandbox(sandbox: Sandbox, code: str, name: str) -> bool:
    """Write the generated worker code into the sandbox checkout.
    Creates the module at src/friday/workers/{name}.py and a matching test.
    """
    sp = sandbox.sandbox_path
    if not sp:
        return False
    from pathlib import Path
    sp_path = Path(sp)

    worker_dir = sp_path / "src" / "friday" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "__init__.py").write_text("")
    worker_file = worker_dir / f"{name}.py"
    worker_file.write_text(code)

    example_input = _extract_example_input(code)

    test_dir = sp_path / "tests" / "test_meta"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "__init__.py").write_text("")
    test_code = _generate_smoke_test(name, example_input)
    test_file = test_dir / f"test_{name}.py"
    test_file.write_text(test_code)

    return True


def _extract_example_input(code: str) -> str:
    """Extract WORKER_EXAMPLE_INPUT from generated Python code via AST."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "{}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "WORKER_EXAMPLE_INPUT":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return "{}"


def plan_for_gap(conn, gap_id: int) -> Optional[str]:
    """Validate a gap is plan-able and generate a plan."""
    gap = get_capability_gap(conn, gap_id)
    if not gap:
        print(f"  error: no gap with id {gap_id}")
        return None
    if gap["status"] not in ("open", "planned"):
        print(f"  status={gap['status']} — expected 'open' or 'planned'")
        return None

    print(f"  planning for gap #{gap_id}: {gap['description'][:80]}")

    code = generate_worker_code(conn, gap_id)
    if not code:
        print(f"  plan failed — could not generate worker code")
        return None

    update_capability_gap(conn, gap_id, status="planned", updated_at=now_iso())

    import time as _time
    plan_id = f"plan_{gap_id}_{int(_time.time())}"
    print(f"  plan {plan_id} ready — worker code generated ({len(code)} chars)")
    return plan_id


def _generate_smoke_test(name: str, example_input: str = "{}") -> str:
    """Generate a smoke test for the auto-built worker."""
    import json
    use_example = False
    try:
        parsed = json.loads(example_input) if example_input else {}
        if parsed and isinstance(parsed, dict) and any(parsed.values()):
            use_example = True
    except (ValueError, TypeError):
        pass

    if use_example:
        example_escaped = example_input.replace("'''", "\\'\\'\\'")
        success_test_lines = [
            "def test_execute_with_example_input():",
            f"        result = execute('''{example_escaped}''')",
            "        assert isinstance(result, dict)",
            '        assert result.get("success") is True, f"expected success, got {result}"',
        ]
        success_test = "\n".join(success_test_lines)
    else:
        success_test = (
            "def test_execute_returns_dict():\n"
            "        result = execute('{}')\n"
            "        assert isinstance(result, dict)\n"
            '        assert "success" in result\n'
        )

    return textwrap.dedent(f"""\
    \"\"\"Smoke tests for auto-built worker: {name}.\"\"\"
    import json
    from src.friday.workers.{name} import execute, WORKER_NAME, WORKER_CAPABILITIES

    def test_worker_constants():
        assert isinstance(WORKER_NAME, str) and len(WORKER_NAME) > 0
        assert isinstance(WORKER_CAPABILITIES, list) and len(WORKER_CAPABILITIES) > 0

    {success_test}

    def test_execute_handles_empty_input():
        result = execute("")
        assert isinstance(result, dict)
        assert "success" in result
    """)
