"""Self-Improvement Planner — generates real worker code for a detected gap.

Uses the configured LLM (via services/llm._call) to write the worker's
execute() function. The generated code goes through these stages:

  1. Design prompt — LLM receives the gap description, evidence, and existing
     worker registry context, and outputs a Python module implementing the
     Worker execute() contract.
  2. Syntax validation — the generated code is parsed (compile()) to catch
     basic errors before it touches the sandbox.
  3. If LLM unavailable or generation fails, falls back to the stub scaffold
     (marking the gap as requiring human implementation).

The generated code is written into the sandbox checkout of Friday's own
repo, then tested and verified before deploy.
"""

from __future__ import annotations

import ast
import textwrap
import traceback
from typing import Optional

from ..db import get_capability_gap, get_all_workers, now_iso, update_capability_gap
from ..services.llm import _call as llm_call, _enabled as llm_enabled
from .sandbox import Sandbox


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
        \"""Execute this worker's operation. Returns dict with keys:
           success: bool
           output: str   (human-readable result)
           artifacts: list[str] (optional, file paths created)
           error: str    (if success=False)
        \"""

RULES:
- Use ONLY the Python standard library. No pip/poetry/conda deps.
- The worker runs inside Friday's own process space. Do NOT import or modify
  any friday.* module (that would be self-modifying in production — the
  Meta-Engine runs the code in a sandbox first, but write it as if it will
  eventually run inline).
- Accept input_data as a JSON string; parse it with json.loads.
  The parsed result may be ANY JSON type (string, object, array, number,
  boolean, or null) depending on the gap. Look at the FAILURE EVIDENCE
  below to see the REAL input shape — design execute() for that shape,
  not an idealized object you imagine.
- workspace is a directory path. All file operations must be relative to it.
- Keep it under 200 lines. Prefer simple implementation over clever.
- Include a __doc__ string describing what the worker does.
- **CRITICAL: return {"success": True, "output": ...} for non-zero exit codes
  from a subprocess you control.** Catch the failure and return the exit code
  and error in the "output" field. For *unexpected exceptions* (file not found,
  permission denied, import error, type error), return
  {"success": False, "error": str(e)}. This distinction matters: a subprocess
  that ran but failed (exit code 2) was handled gracefully = success for the
  worker. An unhandled crash = failure.
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

    Handles both old-format strings (``"task_id:error_message"``) and
    new-format integers (``runtime_results.result_id``). For integers,
    loads the runtime_results row to extract the actual input payload
    and error message, so the LLM can see the real shape of inputs the
    worker will receive.
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
            # Load the actual runtime_results row to get the real input
            # and error, so the LLM sees genuine evidence instead of
            # a placeholder reference.
            row = conn.execute(
                "SELECT worker_id, payload, error, exit_code "
                "FROM runtime_results WHERE result_id = ?", (r,)).fetchone()
            if row:
                wid = row["worker_id"] or "?"
                err = (row["error"] or "")[:200]
                exit_c = row["exit_code"] or "?"
                # Extract the actual input payload.
                payload = row["payload"] or ""
                try:
                    pdata = json.loads(payload) if payload else {}
                    inp = pdata.get("input", "")
                except (ValueError, TypeError):
                    inp = ""
                lines.append(
                    f"  - runtime_result #{r}: worker={wid}, exit_code={exit_c}")
                if inp:
                    lines.append(f"    actual input: {repr(inp)[:300]}")
                if err:
                    lines.append(f"    error: {err}")
            else:
                lines.append(f"  - runtime_result #{r}: (not found in runtime_results)")
        elif isinstance(r, int):
            lines.append(f"  - runtime_result #{r} (see runtime_results table for details)")
        else:
            s = str(r)[:200]
            lines.append(f"  - {s}")
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

    ``max_attempts`` caps LLM round-trips (default 3). The deploy caller
    passes ``min(3, 3 - attempt_count)`` so the inner and outer retry
    budgets share a single pool — never 9 total LLM calls per gap.

    Returns the Python source code as a string, or None if generation failed
    (LLM unavailable or produced invalid code after retries).
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

    # Collect top-level assignments and function defs.
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

    # Required symbols.
    if "WORKER_NAME" not in assigns:
        return False
    if "WORKER_CAPABILITIES" not in assigns:
        return False
    if "WORKER_EXAMPLE_INPUT" not in assigns:
        return False
    if "execute" not in funcs:
        return False

    # execute() must accept at least 1 parameter (input_data).
    fn = funcs["execute"]
    pos_args = [a for a in fn.args.args if a.arg not in ("self", "cls")]
    if len(pos_args) < 1:
        return False

    # WORKER_CAPABILITIES must be a list literal of strings.
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

    # Flag top-level usage of dangerous modules (defence-in-depth).
    # socket is banned for network safety — subprocess, os, shutil are
    # allowed for shell-handling workers (gap #8+, shell exit code fixers).
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
    Derives the smoke test input from the WORKER_EXAMPLE_INPUT constant
    in the generated code via AST extraction.
    Returns True on success.
    """
    sp = sandbox.sandbox_path
    if not sp:
        return False
    from pathlib import Path
    sp_path = Path(sp)

    # Write worker module
    worker_dir = sp_path / "src" / "friday" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "__init__.py").write_text("")
    worker_file = worker_dir / f"{name}.py"
    worker_file.write_text(code)

    # Extract WORKER_EXAMPLE_INPUT from generated code via AST.
    example_input = _extract_example_input(code)

    # Write a smoke test using the example input.
    test_dir = sp_path / "tests" / "test_meta"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "__init__.py").write_text("")
    test_code = _generate_smoke_test(name, example_input)
    test_file = test_dir / f"test_{name}.py"
    test_file.write_text(test_code)

    return True


def _extract_example_input(code: str) -> str:
    """Extract WORKER_EXAMPLE_INPUT from generated Python code via AST.

    Returns the JSON string as-is (it is a JSON literal already — the
    LLM writes it as '{"file_path": "test.py"}'), or '{}' if not found.
    """
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
    """Validate a gap is plan-able and generate a plan.

    Runs the LLM code generation to confirm a worker can be built for this
    gap. On success, marks the gap status as 'planned' and returns a plan
    identifier. The deploy step re-generates the code — this is a
    validation pass that confirms plan-ability without persisting the code
    across lifecycle phases.

    Returns a plan ID string, or None if the gap is not plan-able or code
    generation fails.
    """
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
    """Generate a smoke test for the auto-built worker using its declared
    example input. This ensures the test actually exercises the worker with
    a payload it expects, rather than a hand-rolled guess.

    ``example_input`` is the WORKER_EXAMPLE_INPUT JSON string from the
    generated worker module. It is embedded in the test as a Python string
    literal (wrapped in triple-single-quotes for safety against internal
    double quotes in the JSON).

    Falls back to a minimal structural test if example_input is empty or
    would clearly cause the worker to fail (e.g. sends test.json when the
    worker requires file_path).
    """
    import json    # Only use example_input if it's non-empty and wouldn't obviously fail
    # (i.e. it contains at least one key with a value).
    use_example = False
    try:
        parsed = json.loads(example_input) if example_input else {}
        if parsed and isinstance(parsed, dict) and any(parsed.values()):
            use_example = True
    except (ValueError, TypeError):
        pass

    if use_example:
        # Embed the example input in triple-single-quotes to avoid clashes
        # with JSON's internal double quotes. JSON never contains triple
        # single quotes, so this is safe.
        # 8-space body indent: 4 spaces for the function body + 4 more
        # to survive textwrap.dedent() which removes the common 4-space
        # leading indent from the f-string template below.
        example_escaped = example_input.replace("'''", "\\'\\'\\'")
        success_test_lines = [
            "def test_execute_with_example_input():",
            f"        result = execute('''{example_escaped}''')",
            "        assert isinstance(result, dict)",
            '        assert result.get("success") is True, f"expected success, got {result}"',
        ]
        success_test = "\n".join(success_test_lines)
    else:
        # 8-space body indent: 4 spaces for the function body + 4 more
        # to survive textwrap.dedent() which removes the common 4-space
        # leading indent from the f-string template below.
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
