"""Symbolic task -> concrete executor payload (Phase 4, execution layer).

The planner emits SYMBOLIC tasks (engineering intent: rename a symbol, create
a module, run the formatter...). The planner deliberately knows nothing about
file paths (Phase 3 decision: Planner = intent, Resolver = repo knowledge,
Executor = work). This module is the Executor-side half: given a task's
`symbolic` op and the concrete workspace, it locates the affected files and
builds the exact payload the assigned executor understands (FileExecutor JSON,
GitExecutor args, TestingExecutor pytest, ShellExecutor command).

Pure-ish: it READS the workspace (grep) and returns a payload string. It never
mutates the repo itself — the executor does. No LLM, no planning, no new
subsystem. This is execution glue only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .models import ExecutionResult, RuntimeTask
from .verification import VerificationResult


# ---------------------------------------------------------------------------
# Workspace search (read-only grep).
# ---------------------------------------------------------------------------

def _grep(workspace: str, pattern: str, glob: str = "*.py") -> List[str]:
    """Return absolute paths of files under `workspace` containing `pattern`.

    SAFETY: an empty pattern would match EVERY file (grep -e '' matches all),
    which downstream would wipe the whole repo. Refuse it.
    """
    if not pattern or not pattern.strip():
        return []
    try:
        out = subprocess.run(
            ["grep", "-rIl", f"--include={glob}", "-e", pattern, workspace],
            capture_output=True, text=True, timeout=30)
        return [p.strip() for p in out.stdout.splitlines() if p.strip()]
    except Exception:
        return []


def _count_occurrences(path: str, symbol: str) -> int:
    try:
        out = subprocess.run(
            ["grep", "-c", "-e", symbol, path],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return 0
        try:
            return sum(int(line) for line in out.stdout.splitlines() if line.strip().isdigit())
        except ValueError:
            return 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Payload builders per symbolic op.
# ---------------------------------------------------------------------------

def _rename_payload(symbol: str, replacement: str, files: List[str],
                    worker_id: str) -> str:
    """Rename a symbol across files, in the format the ASSIGNED worker expects.
    Few files -> per-file op; many files -> shell sed pass."""
    if not files:
        return json.dumps({"op": "noop", "reason": f"no files contain {symbol}"})
    if _is_shell(worker_id):
        return f"sed -i 's/{symbol}/{replacement}/g' " + " ".join(
            f'"{f}"' for f in files)
    if _is_python(worker_id):
        lines = [
            "import io, pathlib",
            f"sym={symbol!r}; rep={replacement!r}; files={files!r}",
            "for f in files:",
            "    p = pathlib.Path(f)",
            "    if p.exists() and sym in p.read_text():",
            "        p.write_text(p.read_text().replace(sym, rep))",
        ]
        return "\n".join(lines)
    # Default (filesystem / git): FileExecutor replace_all.
    return json.dumps({
        "op": "replace_all", "symbol": symbol, "replacement": replacement,
        "files": files,
    })


def _create_module_payload(mod: str, workspace: str, worker_id: str) -> str:
    path = str((Path(workspace) / f"{mod}.py").resolve())
    if _is_shell(worker_id):
        return f"mkdir -p {Path(path).parent} && : > '{path}'"
    if _is_python(worker_id):
        return "\n".join([
            "import pathlib",
            f"path=pathlib.Path({path!r})",
            "path.parent.mkdir(parents=True, exist_ok=True)",
            "path.write_text('')",
        ])
    return json.dumps({"op": "write", "path": path, "content": ""})


def _remove_payload(symbol: str, files: List[str], worker_id: str) -> str:
    if not symbol or not symbol.strip():
        # Safety: a dead-code removal with no concrete symbol must NOT wipe
        # the repository. The executor would drop every line of every file.
        return json.dumps({"op": "noop",
                           "reason": "no dead-code symbol specified; refusing "
                                     "blanket removal"})
    if not files:
        return json.dumps({"op": "noop", "reason": f"no files contain {symbol}"})
    if _is_shell(worker_id):
        return "sed -i '/" + symbol + "/d' " + " ".join(
            f'"{f}"' for f in files)
    if _is_python(worker_id):
        # Emitted as executable Python source (the resolver may assign
        # worker:python to cleanup). Uses the `ast` module to remove whole
        # function/class nodes whose source contains the dead symbol, so a dead
        # `def DEAD_FN():` (and any enclosing empty def) is gone without leaving
        # dangling bodies. Always yields valid Python.
        return "\n".join([
            "import ast, pathlib",
            f"sym={symbol!r}; files={files!r}",
            "for _f in files:",
            "    p = pathlib.Path(_f)",
            "    if not p.exists() or not p.is_file():",
            "        continue",
            "    src = p.read_text(encoding='utf-8')",
            "    try:",
            "        tree = ast.parse(src)",
            "    except SyntaxError:",
            "        continue",
            "    def _src(node):",
            "        return ast.get_source_segment(src, node) or ''",
            "    def _filter(body):",
            "        kept = []",
            "        for n in body:",
            "            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):",
            "                if sym in _src(n):",
            "                    continue",
            "            kept.append(n)",
            "        return kept",
            "    tree.body = _filter(tree.body)",
            "    for _node in ast.walk(tree):",
            "        if isinstance(_node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):",
            "            _node.body = _filter(_node.body)",
            "    p.write_text(ast.unparse(tree) + '\\n', encoding='utf-8')",
        ])
    return json.dumps({"op": "delete_symbol", "symbol": symbol, "files": files})


def _is_shell(worker_id: str) -> bool:
    return bool(worker_id) and ("shell" in worker_id or "git" in worker_id)


def _is_python(worker_id: str) -> bool:
    return bool(worker_id) and "python" in worker_id


def _create_file_payload(path: str, content: str, worker_id: str) -> str:
    """Build a file-creation payload appropriate for the assigned worker.

    - FileExecutor (filesystem): JSON {"op":"write",...}
    - Python: python snippet writing the file
    - Shell/git: shell heredoc/redirect command
    - Anything else: JSON (the fallback resolver route)
    """
    if _is_shell(worker_id):
        escaped = content.replace("'", "'\\''")
        return f"mkdir -p $(dirname '{path}') && echo '{escaped}' > '{path}'"
    if _is_python(worker_id):
        return "\n".join([
            "import pathlib",
            f"path = pathlib.Path({path!r})",
            f"path.parent.mkdir(parents=True, exist_ok=True)",
            f"path.write_text({content!r}, encoding='utf-8')",
        ])
    # Default: FileExecutor JSON format.
    return json.dumps({"op": "write", "path": path, "content": content})


def build_payload(task: RuntimeTask, workspace: str = ".") -> str:
    """Translate a task's symbolic intent into a concrete executor payload.

    Returns the existing `runtime_payload` unchanged for non-symbolic tasks.
    For symbolic tasks, greps the workspace and emits the payload the ASSIGNED
    worker understands (filesystem -> FileExecutor JSON, python -> python
    snippet, shell/git -> shell command). If the workspace yields nothing,
    returns a safe no-op payload (the executor reports it; verification then
    fails on evidence).
    """
    sym = getattr(task, "symbolic", None) or {}
    if not sym:
        return getattr(task, "runtime_payload", "") or ""

    op = sym.get("op", "")
    worker_id = getattr(task, "worker_id", "") or ""
    symbol = sym.get("symbol") or sym.get("target") or sym.get("module") or ""
    workspace = str(workspace)

    # --- rename family -----------------------------------------------------
    if op in ("rename_declaration", "rename_imports", "update_references"):
        replacement = sym.get("replacement", "")
        files = _grep(workspace, symbol)
        return _rename_payload(symbol, replacement, files, worker_id)

    # --- extract / refactor: create a new module --------------------------
    if op == "create_module":
        mod = sym.get("module") or "extracted_module"
        return _create_module_payload(mod, workspace, worker_id)

    # --- extract / refactor: move code / update imports -------------------
    if op in ("move_code", "update_imports"):
        # Concrete relocation is performed by the file-edit tasks; this step
        # ensures the target module exists so later edits succeed.
        mod = sym.get("module") or "extracted_module"
        return _create_module_payload(mod, workspace, worker_id)

    # --- maintenance: remove dead code ------------------------------------
    if op == "remove_safely":
        files = _grep(workspace, symbol)
        return _remove_payload(symbol, files, worker_id)

    # --- formatter ---------------------------------------------------------
    if op == "run_formatter":
        return "ruff format . || black . || true"

    # --- tests / verification --------------------------------------------
    if op in ("run_tests", "run_regression_tests", "verify_fix"):
        return json.dumps({"pytest": ["-q"]})

    # --- Phase 1: create_file (from trivial/LLM planner) ------------------
    if op == "create_file":
        path = sym.get("path", "")
        content = sym.get("content", "")
        payload = _create_file_payload(path, content, worker_id)
        # Derive test command: explicit command from LLM, or auto-detect
        # from content patterns (def test_, class Test, unittest, import pytest).
        cmd = _derive_test_command(sym, task)
        if cmd:
            if _is_shell(worker_id):
                payload = f"{payload} && {cmd}"
            else:
                # For Python executor: write file then run the command.
                payload += f"\nimport subprocess\nsubprocess.run({cmd!r}, shell=True, check=True)"
        return payload

    # --- Phase 1: run_command (from trivial/LLM planner) ------------------
    if op == "run_command":
        cmd = sym.get("command", "")
        return cmd or "echo no-command"

    # Unknown symbolic op: leave payload as-is (executor will no-op safely).
    return getattr(task, "runtime_payload", "") or ""


# ---------------------------------------------------------------------------
# Evidence-based verification for symbolic tasks.
# ---------------------------------------------------------------------------

def _derive_test_command(sym: dict, task) -> str:
    """Derive a test command from a task's symbolic, if auto-detectable.

    Checks the explicit ``command`` field first, then auto-detects from
    content patterns (def test_, class Test, unittest, import pytest).
    Returns '' if no command can be derived."""
    cmd = sym.get("command", "")
    if cmd:
        return cmd
    content = sym.get("content", "")
    path = sym.get("path", "")
    _test_patterns = ("def test_", "class Test", "unittest", "import pytest")
    _is_test = (getattr(task, "task_type", "") or "").lower() == "testing"
    if _is_test or any(p in (content or "") for p in _test_patterns):
        if path.endswith(".py"):
            return f"python -m pytest {path} -v"
    return ""


def verify_symbolic(task: RuntimeTask, result: ExecutionResult,
                    workspace: str = ".") -> Optional[VerificationResult]:
    """Evidence-based verification for rename/refactor and Phase 1 ops.

    For a rename, the proof is: the OLD symbol count is 0 across the workspace
    and the NEW symbol appears at least once. For create_file, the proof is
    the file actually existing. Returns None when the task has no
    symbolic op we can evidence-check (caller falls back to artifact checks).
    """
    sym = getattr(task, "symbolic", None) or {}
    op = sym.get("op", "")

    # --- Phase 1: create_file verification ---
    if op == "create_file":
        path = sym.get("path", "")
        # Resolve the path against the workspace — symbolic paths from the
        # planner are relative (e.g. "hello.py") but the executor writes them
        # to the workspace directory. Checking CWD (Path(path).exists()) would
        # miss the file and falsely fail verification.
        if path:
            full_path = str((Path(workspace).resolve() / path).resolve())
        else:
            full_path = ""
        if full_path and Path(full_path).exists():
            content = Path(full_path).read_text(encoding="utf-8", errors="replace")
            expected = sym.get("content", "")
            if expected and expected not in content:
                return VerificationResult(
                    passed=False,
                    reason=f"file {path} exists but content mismatch")
            # If the LLM specified a command (e.g. "pytest test_git_branch.py"),
            # execute it NOW as part of verification. Without this, a create_file
            # task that wrote the file but never ran the test would pass
            # verification. Re-running the command independently ensures the
            # test actually passes, not just that the file exists.
            cmd = _derive_test_command(sym, task)
            if cmd:
                try:
                    import subprocess as _sp
                    _r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
                except _sp.TimeoutExpired:
                    return VerificationResult(
                        passed=False, reason=f"test command timed out: {cmd}",
                        evidence={"command": cmd, "stdout": "(timeout)"})
                except Exception as _e:
                    return VerificationResult(
                        passed=False, reason=f"test command error: {_e}",
                        evidence={"command": cmd, "error": str(_e)})
                if _r.returncode != 0:
                    _detail = (_r.stderr[:300] or _r.stdout[:300] or "(no output)").strip()
                    return VerificationResult(
                        passed=False,
                        reason=f"test command failed (exit {_r.returncode}): {_detail}",
                        evidence={"command": cmd, "stdout": _r.stdout[:1000],
                                   "stderr": _r.stderr[:1000], "exit_code": _r.returncode})
                return VerificationResult(
                    passed=True,
                    reason=f"file {path} exists with correct content, test passed",
                    evidence={"path": full_path, "command": cmd, "exit_code": _r.returncode})
            return VerificationResult(
                passed=True,
                reason=f"file {path} exists with correct content",
                evidence={"path": full_path})
        if path:
            return VerificationResult(
                passed=False, reason=f"expected file {full_path} not found")
        return None

    # --- Phase 1: run_command verification ---
    if op == "run_command":
        # Trust exit code — the executor already checked this.
        if result.success:
            return VerificationResult(
                passed=True,
                reason="command executed successfully",
                evidence={"exit_code": result.exit_code})
        return VerificationResult(
            passed=False, reason=f"command failed: {result.error}")

    if op not in ("rename_declaration", "rename_imports", "update_references"):
        return None

    symbol = sym.get("symbol")
    replacement = sym.get("replacement")
    if not symbol:
        return None

    workspace = str(workspace)
    old_files = _grep(workspace, symbol)
    old_count = sum(_count_occurrences(f, symbol) for f in old_files)
    if old_count != 0:
        return VerificationResult(
            passed=False,
            reason=(f"rename incomplete: {old_count} occurrence(s) of "
                    f"'{symbol}' still present after execution"))
    # Old symbol gone. If a replacement was expected, confirm it now exists.
    if replacement:
        new_files = _grep(workspace, replacement)
        new_count = sum(_count_occurrences(f, replacement) for f in new_files)
        if new_count == 0:
            return VerificationResult(
                passed=False,
                reason=(f"rename incomplete: '{replacement}' not found "
                        f"anywhere after removing '{symbol}'"))
        return VerificationResult(
            passed=True,
            reason=(f"rename verified: '{symbol}' count=0, "
                    f"'{replacement}' count={new_count}"))
    return VerificationResult(
        passed=True, reason=f"symbol '{symbol}' removed (count=0)")
