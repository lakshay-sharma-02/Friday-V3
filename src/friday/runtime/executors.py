"""Built-in execution workers (Milestone 9.7).

Production-ready adapters that perform REAL work. Each implements the frozen
``Worker.execute(task) -> ExecutionResult`` contract (runtime/models.py). They
read the execution instruction from ``task.runtime_payload`` and verify the
outcome before reporting success — no fabricated success, ever.

Workers never plan, schedule, resolve, or review. The runtime is unchanged:
these are just ``Worker`` subclasses the runtime dispatches to. The only new
surface is ``resolve_worker(worker_id, workspace)``, which maps a registry
``worker:<name>`` id to its adapter (the existing ``WorkerResolver`` contract).

Execution model
---------------
``task.runtime_payload`` (a string) carries the operation spec:
  - shell:    raw shell command(s)
  - git:      git args (e.g. ``commit -m "x"``); ``push`` is refused
  - file:     JSON ``{"op":..., "path":..., ...}``
  - python:   python source, OR a pytest invocation (auto-detected)
  - testing:  JSON ``{"cmd":[...]}`` / ``{"path":...}``
  - documentation: JSON ``{"path":..., "content":...}``; if absent, derived
    from the task's own evidence fields

The worker operates in ``workspace`` (default cwd), overridable per call via a
``{"workspace": "..."}`` key in JSON payloads. Every result is objectively
verifiable: exit code, file existence, git working-tree delta, pytest exit.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .models import ExecutionResult, Executor, VerificationResult
from ..db import connect as _resolve_connect


# Honour a global timeout (seconds) for any external process.
_DEFAULT_TIMEOUT = int(os.environ.get("FRIDAY_WORKER_TIMEOUT", "60"))

# Per-task-type timeout overrides (seconds). Testing/verification tasks need
# longer than simple coordination echoes. The executor picks its timeout by
# looking up the task's task_type in this map; falls back to _DEFAULT_TIMEOUT.
_TASK_TYPE_TIMEOUTS = {
    "testing": 300,
    "verification": 300,
    "review": 180,
    "deployment": 300,
    "infrastructure": 180,
}


def _timeout_for(task, fallback: int) -> int:
    """Resolve timeout for a task: explicit task-level override > task-type default > fallback."""
    explicit = getattr(task, "timeout", None) or getattr(task, "estimated_effort", None)
    if explicit:
        try:
            return int(explicit)
        except (ValueError, TypeError):
            pass
    tt = getattr(task, "task_type", "") or ""
    return _TASK_TYPE_TIMEOUTS.get(tt.lower(), fallback)


def _payload(task) -> str:
    return getattr(task, "runtime_payload", "") or ""


def _ws(task, default: str) -> str:
    """Resolve the working directory for an operation.

    Priority: explicit ``workspace`` key in a JSON payload, else the worker's
    configured workspace, else the process cwd.
    """
    p = _payload(task).strip()
    if p.startswith("{"):
        try:
            obj = json.loads(p)
            if isinstance(obj, dict) and obj.get("workspace"):
                return obj["workspace"]
        except (ValueError, TypeError):
            pass
    return default


def _ok(stdout: str, stderr: str, exit_code: int, dur: int,
        artifacts: Optional[List[str]] = None, error: str = "") -> ExecutionResult:
    return ExecutionResult(
        success=True, stdout=stdout, stderr=stderr, exit_code=exit_code,
        duration_ms=dur, artifacts=artifacts or [])


def _filename_from_goal(goal: str, default_title: str, ext: str) -> str:
    """Derive a workspace filename from the user goal.

    When the goal explicitly names a file (e.g. "calculator.py"), use it.
    Otherwise fall back to a slug of the task title.
    """
    # Look for an explicit 'filename.ext' in the goal.
    for word in goal.split():
        word = word.strip(".,;:'\"!?")
        if word.count(".") == 1 and not word.startswith(".") and not word.endswith("."):
            _, suffix = word.rsplit(".", 1)
            if suffix.lower() in ("py", "md", "txt", "sh", "ts", "js", "rs", "go",
                                   "rb", "java", "c", "h", "cpp", "hpp", "rs",
                                   "toml", "json", "yaml", "yml", "sql", "html",
                                   "css", "scss", "less", "tsx", "jsx"):
                return word
    # Fall back to slugged title.
    slug = "".join(c if c.isalnum() else "_" for c in default_title.lower())[:40].strip("_") or "output"
    return f"{slug}{ext}"


def _fail(stdout: str, stderr: str, exit_code: Optional[int], dur: int,
          error: str, artifacts: Optional[List[str]] = None) -> ExecutionResult:
    return ExecutionResult(
        success=False, stdout=stdout, stderr=stderr, exit_code=exit_code,
        duration_ms=dur, error=error, artifacts=artifacts or [])


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

class BuiltinShellExecutor(Executor):
    """Execute shell commands; capture stdout/stderr/exit code; timeout-aware.

    Uses ``shell=True`` because the contract explicitly requires it for shell
    command execution (ponytaic: shell=True is otherwise avoided).
    """

    def __init__(self, worker_id: str = "worker:shell",
                 workspace: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    def execute(self, task) -> ExecutionResult:
        cmd = _payload(task).strip()
        if not cmd:
            # Coordination fallback: a generated task with no explicit command
            # still performs a REAL, verifiable action — gather repo evidence
            # via git, or list the workspace. Never a no-op success.
            ws = _ws(task, self._ws)
            if Path(ws).joinpath(".git").exists() or _is_git(ws):
                cmd = f"git -C {shlex.quote(ws)} log --oneline -10"
            else:
                cmd = f"ls -la {shlex.quote(ws)}"
        ws = _ws(task, self._ws)
        timeout = _timeout_for(task, self._timeout)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=ws, capture_output=True, text=True,
                timeout=timeout)
            dur = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             f"shell command exited {proc.returncode}")
            if not (proc.stdout or proc.stderr).strip():
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             "shell command produced no output")
            return ExecutionResult(
                success=True, stdout=proc.stdout, stderr=proc.stderr,
                exit_code=proc.returncode, duration_ms=dur)
        except subprocess.TimeoutExpired as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail(getattr(e, "stdout", "") or "", "timeout", None, dur,
                         f"shell command timed out after {timeout}s")
        except Exception as e:  # defensive; dispatcher also guards
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")



# backward-compat alias
BuiltinShellWorker = BuiltinShellExecutor

def _is_git(ws: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=ws,
            capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

class GitExecutor(Executor):
    """Version-control operations. Never pushes. Verifies tree changes for
    mutating ops (add/restore/checkout/branch/commit)."""

    _MUTATING = {
        "add", "restore", "checkout", "branch", "commit", "reset", "mv",
        "rm", "tag", "merge", "rebase", "stash", "switch",
    }
    _NEVER = {"push", "push", "upload-pack", "send-pack"}

    def __init__(self, worker_id: str = "worker:git",
                 workspace: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    @staticmethod
    def _porcelain(ws: str) -> str:
        try:
            out = subprocess.run(
                ["git", "status", "--porcelain"], cwd=ws, capture_output=True,
                text=True, timeout=10)
            return out.stdout
        except Exception:
            return ""

    def execute(self, task) -> ExecutionResult:
        raw = _payload(task).strip()
        if not raw:
            # Coordination fallback: report working-tree status (real, verifiable).
            raw = "status --short"
        if raw.split()[0].lower() in self._NEVER:
            return _fail("", f"refused git {raw.split()[0]} (never push)",
                         None, 0, "git push is not permitted by the worker")
        ws = _ws(task, self._ws)
        args = shlex.split(raw)
        sub = args[0].lower() if args else ""
        before = self._porcelain(ws) if sub in self._MUTATING else ""
        timeout = _timeout_for(task, self._timeout)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                ["git", *args], cwd=ws, capture_output=True, text=True,
                timeout=timeout)
            dur = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             f"git {sub} exited {proc.returncode}")
            if sub in self._MUTATING:
                after = self._porcelain(ws)
                if after == before:
                    return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                                 f"git {sub} produced no working-tree change")
            return _ok(proc.stdout, proc.stderr, proc.returncode, dur)
        except subprocess.TimeoutExpired as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", "timeout", None, dur,
                         f"git command timed out after {timeout}s")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")



# backward-compat alias
GitWorker = GitExecutor

# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------

class FileExecutor(Executor):
    """Filesystem operations via a JSON payload:

    {"op":"read",    "path":"..."}
    {"op":"write",   "path":"...", "content":"..."}
    {"op":"append",  "path":"...", "content":"..."}
    {"op":"replace", "path":"...", "old":"...", "new":"..."}
    {"op":"mkdir",   "path":"..."}
    {"op":"delete",  "path":"..."}
    {"op":"copy",    "src":"...", "dst":"..."}
    {"op":"move",    "src":"...", "dst":"..."}
    """

    def __init__(self, worker_id: str = "worker:filesystem",
                 workspace: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    def _path(self, task, p: str) -> Path:
        base = Path(_ws(task, self._ws))
        pp = Path(p)
        # Resolve safely under the workspace; allow absolute overrides only
        # when the path already lives outside (kept simple + explicit).
        if pp.is_absolute():
            return pp
        return (base / pp).resolve()

    def execute(self, task) -> ExecutionResult:
        raw = _payload(task).strip()
        t0 = time.monotonic()
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return _fail("", raw[:200], None, 0,
                         "file worker: payload must be JSON")
        op = (obj.get("op") or "").lower()
        try:
            if op == "read":
                p = self._path(task, obj["path"])
                if not p.exists():
                    return _fail("", "", None, 0, f"file not found: {p}")
                return _ok(p.read_text(encoding="utf-8", errors="replace"),
                           "", 0, 0, [str(p)])
            if op == "write":
                p = self._path(task, obj["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(obj.get("content", ""), encoding="utf-8")
                if not p.exists() or p.stat().st_size == 0:
                    return _fail("", "", None, 0, f"write produced empty/missing: {p}")
                return _ok("", "", 0, 0, [str(p)])
            if op == "append":
                p = self._path(task, obj["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(obj.get("content", ""))
                return _ok("", "", 0, 0, [str(p)])
            if op == "replace":
                p = self._path(task, obj["path"])
                if not p.exists():
                    return _fail("", "", None, 0, f"file not found: {p}")
                text = p.read_text(encoding="utf-8")
                new = text.replace(obj.get("old", ""), obj.get("new", ""), 1)
                if new == text:
                    return _fail("", "", None, 0,
                                 "replace: 'old' not found in file")
                p.write_text(new, encoding="utf-8")
                return _ok("", "", 0, 0, [str(p)])
            if op == "mkdir":
                p = self._path(task, obj["path"])
                p.mkdir(parents=True, exist_ok=True)
                return _ok("", "", 0, 0, [str(p)])
            if op == "delete":
                p = self._path(task, obj["path"])
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                else:
                    return _fail("", "", None, 0, f"path not found: {p}")
                if p.exists():
                    return _fail("", "", None, 0, f"delete failed: {p}")
                return _ok("", "", 0, 0, [str(p)])
            if op in ("copy", "move"):
                src = self._path(task, obj["src"])
                dst = self._path(task, obj["dst"])
                if not src.exists():
                    return _fail("", "", None, 0, f"src not found: {src}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                if op == "copy":
                    if src.is_dir():
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                else:
                    shutil.move(str(src), str(dst))
                if not dst.exists():
                    return _fail("", "", None, 0, f"{op} failed: {dst}")
                return _ok("", "", 0, 0, [str(dst)])
            if op == "noop":
                # Honest no-op: the symbolic op had nothing to do in this
                # workspace (e.g. no matching symbol). Report success with no
                # artifact so verification can fail on evidence if required.
                return _ok("", "", 0, 0, [])
            if op == "replace_all":
                # Rename a symbol across one or more files (Phase 4 symbolic
                # rename). Deterministic and verifiable. Any file with no match
                # is left untouched; at least one replacement must occur.
                symbol = obj.get("symbol", "")
                replacement = obj.get("replacement", "")
                files = obj.get("files") or []
                if not symbol or not files:
                    return _fail("", "", None, 0,
                                 "replace_all needs symbol + files")
                done = 0
                import shutil as _sh
                for f in files:
                    p = Path(f)
                    if not p.exists() or not p.is_file():
                        continue
                    text = p.read_text(encoding="utf-8")
                    if symbol not in text:
                        continue
                    new = text.replace(symbol, replacement)
                    p.write_text(new, encoding="utf-8")
                    done += 1
                if done == 0:
                    return _fail("", "", None, 0,
                                 f"replace_all: no occurrences of {symbol}")
                return _ok("", "", 0, 0, [str(f) for f in files])
            if op == "delete_symbol":
                # Remove every occurrence of a dead-code symbol across files.
                symbol = obj.get("symbol", "")
                files = obj.get("files") or []
                if not symbol or not symbol.strip():
                    return _fail("", "", None, 0,
                                 "delete_symbol needs a non-empty symbol")
                if not files:
                    return _fail("", "", None, 0,
                                 "delete_symbol needs symbol + files")
                done = 0
                for f in files:
                    p = Path(f)
                    if not p.exists() or not p.is_file():
                        continue
                    text = p.read_text(encoding="utf-8")
                    if symbol not in text:
                        continue
                    # Drop the whole statement block: the line(s) containing the
                    # symbol AND the immediately-following indented body, so a
                    # dead `def DEAD_FN():` leaves no dangling `return` body
                    # behind. A blank line or dedented line ends the block.
                    kept = []
                    lines = text.splitlines()
                    i = 0
                    while i < len(lines):
                        ln = lines[i]
                        if symbol in ln:
                            # Remove this line; skip following more-indented
                            # lines (the def/class body) until dedent/blank.
                            base_indent = len(ln) - len(ln.lstrip())
                            i += 1
                            while i < len(lines):
                                nxt = lines[i]
                                if nxt.strip() == "":
                                    i += 1
                                    continue
                                nxt_indent = len(nxt) - len(nxt.lstrip())
                                if nxt_indent > base_indent:
                                    i += 1
                                    continue
                                break
                            continue
                        kept.append(ln)
                        i += 1
                    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    done += 1
                if done == 0:
                    return _fail("", "", None, 0,
                                 f"delete_symbol: no occurrences of {symbol}")
                return _ok("", "", 0, 0, [str(f) for f in files])
            return _fail("", "", None, 0, f"unknown file op: {op}")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")



# backward-compat alias
FileWorker = FileExecutor

# ---------------------------------------------------------------------------
# Python (also pytest-capable, since the resolver routes Testing tasks here)
# ---------------------------------------------------------------------------

class BuiltinPythonExecutor(Executor):
    """Run Python source OR a pytest invocation from the payload.

    Auto-detects pytest: a payload starting with ``pytest`` / ``python -m
    pytest`` / a JSON ``{"pytest":[...]}`` is executed via the test runner so
    the worker fulfils both the Python and Testing responsibilities.
    """

    def __init__(self, worker_id: str = "worker:python",
                 workspace: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    @staticmethod
    def _is_pytest(payload: str) -> bool:
        s = payload.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                return isinstance(obj, dict) and "pytest" in obj
            except (ValueError, TypeError):
                return False
        return s.split()[0] == "pytest" or s.startswith("python -m pytest")

    def execute(self, task) -> ExecutionResult:
        payload = _payload(task).strip()
        if not payload:
            # Coordination fallback: confirm the Python runtime is real and
            # report its version (verifiable, never fabricated).
            payload = 'import sys; print(sys.version)'
        ws = _ws(task, self._ws)
        t0 = time.monotonic()
        try:
            if self._is_pytest(payload):
                if payload.strip().startswith("{"):
                    args = json.loads(payload)["pytest"]
                elif payload.strip().startswith("python -m pytest"):
                    args = shlex.split(payload)[2:]
                else:
                    args = shlex.split(payload)[1:]
                cmd = [sys_exe(), "-m", "pytest", *args]
            else:
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".py", delete=False) as f:
                    f.write(payload)
                    path = f.name
                cmd = [sys_exe(), path]
            timeout = _timeout_for(task, self._timeout)
            proc = subprocess.run(
                cmd, cwd=ws, capture_output=True, text=True,
                timeout=timeout)
            dur = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             f"exit {proc.returncode}: "
                             + _first_failures(proc.stdout, proc.stderr))
            return _ok(proc.stdout, proc.stderr, proc.returncode, dur)
        except subprocess.TimeoutExpired as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", "timeout", None, dur,
                         f"python run timed out after {timeout}s")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")



# backward-compat alias
BuiltinPythonWorker = BuiltinPythonExecutor

def _looks_like_python(exe: str) -> bool:
    """Cheap name-based gate: only probe executables whose name suggests
    CPython. Avoids launching a non-Python host (e.g. an Electron AppImage
    named `ZCode-...AppImage`) which would hang or boot the wrong process."""
    import os
    return "python" in os.path.basename(exe).lower()


def _is_real_python(exe: str) -> bool:
    """True iff `exe` actually runs Python (returns a version on stdout)."""
    if not exe or not _looks_like_python(exe):
        return False
    import subprocess
    try:
        r = subprocess.run(
            [exe, "-c", "import sys; print(sys.version_info[0])"],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip().isdigit()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def sys_exe() -> str:
    """Resolve a genuine Python interpreter.

    Prefers `sys.executable` when it truly runs Python; otherwise probes
    `python3` then `python` on PATH. This keeps workers correct even when the
    host process is not CPython (Electron wrapper, frozen binary, etc.).
    """
    import shutil
    import sys
    if _is_real_python(sys.executable):
        return sys.executable
    for cand in ("python3", "python"):
        path = shutil.which(cand)
        if path and _is_real_python(path):
            return path
    return sys.executable or "python3"


def _first_failures(*texts: str) -> str:
    """Pull pytest failure summary lines for a concise error message."""
    lines: List[str] = []
    for t in texts:
        for line in (t or "").splitlines():
            if "FAILED" in line or "ERROR" in line or "AssertionError" in line:
                lines.append(line.strip())
    return "; ".join(lines[:5])


# ---------------------------------------------------------------------------
# Testing (dedicated worker; exercised directly and via registration)
# ---------------------------------------------------------------------------

class TestingExecutor(Executor):
    """Run the test framework. Reports pass/fail with a failure summary.

    ``__test__ = False`` keeps pytest from collecting this as a test class
    (its name matches the default ``Test*`` collection pattern).

    Payload JSON:
      {"cmd":["pytest","-q"]}        -> run the given command
      {"path":"tests/test_x.py"}     -> run pytest on that file
      {"pytest":["-q","tests/"]}     -> equivalent to cmd form
    """
    __test__ = False

    def __init__(self, worker_id: str = "worker:testing",
                 workspace: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    def execute(self, task) -> ExecutionResult:
        payload = _payload(task).strip()
        if not payload:
            # Coordination fallback: confirm the test runner is installed.
            payload = '{"pytest": ["--version"]}'
        ws = _ws(task, self._ws)
        t0 = time.monotonic()
        try:
            args = self._args(payload)
        except (ValueError, TypeError) as e:
            return _fail("", str(e), None, 0, "testing worker: bad payload")
        if not args:
            return _fail("", "no test target", None, 0,
                         "testing worker: no cmd/path in payload")
        cmd = [sys_exe(), "-m", "pytest", *args]
        timeout = _timeout_for(task, self._timeout)
        try:
            proc = subprocess.run(
                cmd, cwd=ws, capture_output=True, text=True,
                timeout=timeout)
            dur = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             f"tests failed: " + _first_failures(
                                 proc.stdout, proc.stderr))
            return _ok(proc.stdout, proc.stderr, proc.returncode, dur)
        except subprocess.TimeoutExpired:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", "timeout", None, dur,
                         f"test run timed out after {timeout}s")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")

    @staticmethod
    def _args(payload: str) -> List[str]:
        if not payload:
            return []
        if payload.startswith("{"):
            obj = json.loads(payload)
            if "cmd" in obj:
                return list(obj["cmd"])
            if "pytest" in obj:
                return list(obj["pytest"])
            if "path" in obj:
                return [obj["path"]]
            return []
        # Bare path or pytest args.
        if payload.startswith("pytest"):
            return shlex.split(payload)[1:]
        return shlex.split(payload)



# backward-compat alias
TestingWorker = TestingExecutor

# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

class DocumentationExecutor(Executor):
    """Write documentation. Payload JSON ``{"path":..., "content":...}``.

    With no payload, content is derived deterministically from the task's own
    evidence fields (title, description, acceptance criteria) — never invented,
    never an LLM call. Success requires the target file to exist and be
    non-empty.
    """

    DEFAULT_PATH = "README.md"

    def __init__(self, worker_id: str = "worker:documentation",
                 workspace: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    def execute(self, task) -> ExecutionResult:
        raw = _payload(task).strip()
        ws = _ws(task, self._ws)
        t0 = time.monotonic()
        try:
            path, content = self._resolve(raw, task)
            p = Path(path)
            if not p.is_absolute():
                p = (Path(ws) / p).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            dur = int((time.monotonic() - t0) * 1000)
            if not p.exists() or p.stat().st_size == 0:
                return _fail("", "", None, dur,
                             f"documentation write produced empty/missing: {p}")
            return _ok(f"wrote {p}", "", 0, dur, [str(p)])
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")

    def _resolve(self, raw: str, task):
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and obj.get("path"):
                    return obj["path"], obj.get("content", "") or ""
            except (ValueError, TypeError):
                pass
        # Derive from the task's evidence fields (no LLM, no invention).
        title = getattr(task, "title", "") or "Documentation"
        desc = getattr(task, "description", "") or ""
        acs = getattr(task, "acceptance_criteria", []) or []
        lines = [f"# {title}", ""]
        if desc:
            lines.append(desc)
            lines.append("")
        if acs:
            lines.append("## Acceptance criteria")
            for a in acs:
                lines.append(f"- {a}")
            lines.append("")
        return self.DEFAULT_PATH, "\n".join(lines).rstrip() + "\n"



# backward-compat alias
DocumentationWorker = DocumentationExecutor


# ---------------------------------------------------------------------------
# Synthesis — LLM-powered content generation for integration/analysis tasks.
# ---------------------------------------------------------------------------

class SynthesisExecutor(Executor):
    """Generate rich analysis content using the Friday LLM service.

    Designed for integration/analysis tasks where the task description contains
    rich synthesis evidence but the FileExecutor would write template stubs.
    Reads the task's title, description, acceptance criteria, and symbolic
    metadata, builds an LLM prompt to generate proper analysis content, and
    writes the result to the target file.

    Falls back to DocumentationExecutor-style content derivation when the LLM
    is unavailable or fails — the task always produces a file, never crashes.
    """

    DEFAULT_PATH = "analysis.md"

    def __init__(self, worker_id: str = "worker:synthesis",
                 workspace: str = ".", timeout: int = 60) -> None:
        self.worker_id = worker_id
        self._ws = workspace
        self._timeout = timeout

    def execute(self, task) -> ExecutionResult:
        raw = _payload(task).strip()
        ws = _ws(task, self._ws)
        t0 = time.monotonic()

        # Step 1: determine the target path and any pre-existing content hint.
        path, content_hint = self._resolve_path_and_hint(raw, task)

        # Step 2: try LLM synthesis.
        generated = self._try_llm_synthesis(task, content_hint)

        # Step 3: if LLM succeeded, use generated content; otherwise fall back
        # to the DocumentationExecutor approach (derive from task fields).
        if generated:
            content = generated
        else:
            content = self._fallback_content(task, path)

        # Step 4: write the file.
        p = Path(path)
        if not p.is_absolute():
            p = (Path(ws) / p).resolve()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            dur = int((time.monotonic() - t0) * 1000)
            if not p.exists() or p.stat().st_size == 0:
                return _fail("", "", None, dur,
                             f"synthesis write produced empty/missing: {p}")
            return _ok(f"wrote {p} ({len(content)} chars)", "", 0, dur, [str(p)])
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")

    def _resolve_path_and_hint(self, raw: str, task) -> Tuple[str, str]:
        """Extract target path and content hint from payload or task fields."""
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    p = obj.get("path", "") or ""
                    hint = obj.get("content", "") or ""
                    if p:
                        return p, hint
            except (ValueError, TypeError):
                pass
        # Fall back to symbolic metadata.
        sym = getattr(task, "symbolic", None) or {}
        p = sym.get("path", "") or ""
        hint = sym.get("content", "") or ""
        if p:
            return p, hint
        # Last resort: derive from title.
        title = getattr(task, "title", "") or "analysis"
        slug = "".join(c if c.isalnum() else "_" for c in title.lower())[:40].strip("_")
        return f"{slug}.md", hint

    def _read_repo_files(self, sym: dict) -> str:
        """Read key files from repo paths to enrich analysis content.

        Reads README, config files (package.json, pyproject.toml, etc.),
        and a few source files from each repo path. File contents are
        truncated to avoid overflowing the LLM context window.

        Performance: globbing is depth-limited to avoid scanning large
        directories like node_modules/ or .git/. Total output is capped
        at 25K chars to keep the LLM prompt within a reasonable window.

        Returns a formatted string with repo file contents, or empty
        string if no repo paths are available or files can't be read.
        """
        repo_paths = sym.get("repo_paths") or []
        if not repo_paths:
            return ""

        lines: List[str] = []
        _MAX_FILE_SIZE = 5000  # max chars per file
        _MAX_FILES_PER_REPO = 4
        _MAX_TOTAL_CHARS = 25000  # total cap to prevent context overflow

        for repo_idx, rp in enumerate(repo_paths):
            if not rp or not Path(rp).is_dir():
                continue
            repo_name = Path(rp).name
            lines.append(f"\n=== Source Files from {repo_name} ({rp}) ===")

            # Key files to read: config + entry points + README.
            # Only shallow-level config files (no deep recursion needed).
            candidates = [
                "README.md", "readme.md", "Readme.md",
                "package.json", "pyproject.toml", "Cargo.toml",
                "go.mod", "Gemfile", "setup.py", "setup.cfg",
                "requirements.txt", "Makefile", "Dockerfile",
            ]
            # Also check key subdirectories at depth 1 only (avoids scanning
            # node_modules/, .git/, vendor/, etc. which cause performance issues).
            for sub_dir in ("src", "cli", "lib", "app"):
                sub_path = Path(rp) / sub_dir
                if sub_path.is_dir():
                    for f in sorted(sub_path.iterdir())[:_MAX_FILES_PER_REPO]:
                        if f.is_file() and f.suffix in (".py", ".ts", ".js", ".rs", ".go", ".sh"):
                            rel = str(f.relative_to(Path(rp)))
                            if rel not in candidates:
                                candidates.append(rel)

            # Deduplicate and read files.
            seen_files: set = set()
            count = 0
            for candidate in candidates:
                if count >= _MAX_FILES_PER_REPO:
                    break
                # Check total size cap.
                current_total = sum(len(l) for l in lines)
                if current_total >= _MAX_TOTAL_CHARS:
                    break
                fp = Path(rp) / candidate
                resolved = fp.resolve()
                if resolved.exists() and resolved.is_file() and str(resolved) not in seen_files:
                    seen_files.add(str(resolved))
                    try:
                        text = resolved.read_text(
                            encoding="utf-8", errors="replace")
                        if len(text) > _MAX_FILE_SIZE:
                            text = text[:_MAX_FILE_SIZE] + "\n... (truncated)"
                        # Check if adding this file would exceed total cap.
                        if current_total + len(text) > _MAX_TOTAL_CHARS:
                            remaining = _MAX_TOTAL_CHARS - current_total
                            if remaining > 200:
                                text = text[:remaining] + "\n... (truncated)"
                            else:
                                break
                        lines.append(f"\n--- {candidate} ---")
                        lines.append(text)
                        count += 1
                    except (OSError, IOError):
                        pass

        return "\n".join(lines)

    def _try_llm_synthesis(self, task, content_hint: str) -> Optional[str]:
        """Attempt to generate content via the Friday LLM service.

        Builds a structured prompt from the task's title, description,
        acceptance criteria, AND actual repo source files read from disk.
        The content_hint is deliberately NOT passed to the LLM because it
        contains template instructions that confuse the model.

        The task's description field (which carries synthesis evidence) is
        used alongside actual source files read from the repo paths stored
        in the task's symbolic metadata (repo_paths).

        Returns generated markdown or None on failure.
        """
        title = getattr(task, "title", "") or "Analysis"
        desc = getattr(task, "description", "") or ""
        acs = getattr(task, "acceptance_criteria", []) or []

        if not desc:
            return None  # Nothing to synthesize from.

        # Determine document type from the target file path.
        sym = getattr(task, "symbolic", None) or {}
        path = sym.get("path", "") or ""

        # Read actual repo source files for deeper analysis.
        repo_files = self._read_repo_files(sym)

        doc_type_map = {
            "integration-analysis": "architecture comparative analysis",
            "shared-patterns": "shared patterns and divergences documentation",
            "integration-plan": "feasibility assessment and integration plan",
            "adapter-design": "adapter/interface design specification",
        }
        doc_purpose = "analysis document"
        doc_sections = ""
        for key, purpose in doc_type_map.items():
            if key in path:
                doc_purpose = purpose
                if key == "integration-analysis":
                    doc_sections = (
                        "## Project overview for each repo\n"
                        "## Architecture comparison\n"
                        "## Technology stack comparison\n"
                        "## Structural similarities and differences\n"
                        "## Integration opportunities\n"
                        "## Recommendations\n"
                    )
                elif key == "shared-patterns":
                    doc_sections = (
                        "## Shared technologies\n"
                        "## Common architectural patterns\n"
                        "## Shared components and utilities\n"
                        "## Key divergences\n"
                        "## Reuse opportunities\n"
                    )
                elif key == "integration-plan":
                    doc_sections = (
                        "## Feasibility summary\n"
                        "## Integration strategies considered\n"
                        "## Recommended approach\n"
                        "## Effort breakdown by phase\n"
                        "## Risk assessment and mitigations\n"
                        "## Migration roadmap\n"
                        "## Success metrics\n"
                    )
                elif key == "adapter-design":
                    doc_sections = (
                        "## Integration approach and rationale\n"
                        "## Interface specification (APIs, data formats, schemas)\n"
                        "## Data flow between systems\n"
                        "## Error handling and edge cases\n"
                        "## Testing strategy\n"
                        "## Deployment and rollout plan\n"
                    )
                break

        # Check LLM availability before building the prompt.
        try:
            from ..services.llm import _call as _llm_call
        except ImportError:
            return None

        system = (
            "You write thorough, self-contained markdown documents for "
            "software engineering tasks. You will be given:\n"
            "1. A document purpose (what type of document to produce)\n"
            "2. Source evidence (raw findings to work from)\n"
            "3. Required sections (headings to include)\n"
            "4. Acceptance criteria (quality gates)\n\n"
            "CRITICAL RULES:\n"
            "- Write COMPLETE analysis content for EVERY section listed.\n"
            "- Never write instructions like 'TBD', 'Fill this in', "
            "'Document: architecture pattern...', or placeholder text.\n"
            "- Every section must contain specific, concrete analysis with "
            "real technology names, patterns, and architectural details.\n"
            "- Use proper markdown: headings (##), bullet lists, tables, "
            "and code blocks where appropriate.\n"
            "- Output ONLY the markdown content. No commentary, no JSON, "
            "no code fences wrapping the document.\n"
        )

        ac_text = "\n".join(f"- {a}" for a in acs) if acs else "None specified."
        user = (
            f"## Document Purpose\n"
            f"Create a {doc_purpose} for the following task.\n\n"
            f"## Synthesis Evidence\n"
            f"{desc}\n\n"
            f"## Repository Source Files\n"
            f"Here are actual source files read from the repositories on disk. "
            f"Use these to provide specific, concrete analysis with real "
            f"code patterns, dependencies, and architectural details.\n"
            f"{repo_files}\n\n"
            f"## Required Sections\n"
            f"{doc_sections}\n"
            f"## Acceptance Criteria\n"
            f"{ac_text}\n\n"
            f"Generate the complete markdown document now. Every section "
            f"listed above must contain substantive, specific analysis "
            f"content based on the synthesis evidence AND the actual "
            f"source files provided. Write real content, not instructions."
        )

        try:
            result = _llm_call(system, user)
            if result:
                text = result.strip()
                # Strip markdown code fences if the LLM wraps output.
                for fence in ("```markdown", "```markdown\n", "```"):
                    if text.startswith(fence):
                        text = text[len(fence):].strip()
                    if text.endswith("```"):
                        text = text[:-3].strip()
                if len(text) > 100:
                    return text
        except Exception:
            pass
        return None

    def _fallback_content(self, task, path: str) -> str:
        """Fallback content derivation when LLM is unavailable.

        Mirrors DocumentationExecutor._resolve() behaviour: derive content
        from the task's own evidence fields.
        """
        title = getattr(task, "title", "") or "Analysis"
        desc = getattr(task, "description", "") or ""
        acs = getattr(task, "acceptance_criteria", []) or []
        lines = [f"# {title}", ""]
        if desc:
            lines.append(desc)
            lines.append("")
        if acs:
            lines.append("## Acceptance criteria")
            for a in acs:
                lines.append(f"- {a}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        """File-existence and non-empty verification."""
        artifacts = result.artifacts or []
        if artifacts:
            paths_to_check = list(artifacts)
        else:
            sym = getattr(task, "symbolic", None) or {}
            p = sym.get("path", "")
            paths_to_check = [p] if p else []

        for p in paths_to_check:
            pp = Path(p)
            if pp.exists() and pp.stat().st_size > 0:
                return VerificationResult(
                    passed=True,
                    reason=f"artifact {p} exists ({pp.stat().st_size} bytes)")
        return VerificationResult(
            passed=False,
            reason=f"no artifact produced by synthesis executor")


# backward-compat alias
SynthesisWorker = SynthesisExecutor

# ---------------------------------------------------------------------------
# Worker resolution — maps a registry worker_id to its execution adapter.
# ---------------------------------------------------------------------------

def resolve_worker(worker_id: str, workspace: str = ".") -> Optional[Worker]:
    """DEPRECATED: use resolve_executor. Kept for backward compatibility."""
    return resolve_executor(worker_id, workspace)


# ---------------------------------------------------------------------------
# Dynamic worker dispatch — wraps a self-generated worker module.
# ---------------------------------------------------------------------------

class DynamicWorkerExecutor(Executor):
    """Generic adapter for self-generated workers (from the meta-engine).

    Wraps a Python module that exports ``execute(input_data, workspace)``
    returning ``{"success": bool, "output": str, ...}``. The module is
    dynamically imported from ``src.friday.workers.<name>`` and its
    ``execute()`` is called with the task's ``runtime_payload`` as input_data.
    """

    def __init__(self, module, worker_id: str, workspace: str = ".") -> None:
        self.worker_id = worker_id
        self._module = module
        self._ws = workspace

    def execute(self, task) -> ExecutionResult:
        inp = _payload(task).strip()
        inp = normalize_worker_input(inp)
        ws = _ws(task, self._ws)
        t0 = time.monotonic()
        try:
            result = self._module.execute(inp, ws)
            dur = int((time.monotonic() - t0) * 1000)
            if not isinstance(result, dict):
                return _fail("", str(result), None, dur,
                             f"auto worker returned non-dict: {type(result).__name__}")
            success = bool(result.get("success", False))
            if success:
                return _ok(
                    result.get("output", ""),
                    "",
                    0,
                    dur,
                    artifacts=result.get("artifacts", []),
                )
            return _fail(
                result.get("output", ""),
                result.get("error", ""),
                None,
                dur,
                error=result.get("error", "auto worker returned success=False"),
            )
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        """Trust the worker's own success flag (same as base Executor.verify).

        The meta-engine's Stage 3 replay is the authoritative verification
        gate for auto-generated workers. This verify() mirrors the base
        class default — trust success, don't add false-negative checks.
        """
        return VerificationResult(
            passed=result.success,
            reason="auto worker success" if result.success
            else "auto worker failure",
        )


def _find_auto_worker_module(worker_id: str) -> Optional[str]:
    """Extract the module name from an auto-generated worker_id.

    Auto-generated worker_ids follow the pattern ``worker:<name>:<hex>``
    (set by ``deploy._register_worker``). We extract ``<name>`` and look for
    ``src/friday.workers.<name>`` on the Python path (the module the
    meta-engine wrote during deploy).

    Uses ``importlib.import_module`` directly with ``sys.path`` adjusted so
    the Friday project root is importable — same pattern as verification's
    ``_build_invoke_code``. Does NOT check the worker registry; registration
    and module-writing happen atomically during deploy, so the module's
    filesystem presence is a reliable proxy for a completed deploy.

    Returns the loaded module, or None if not found.
    """
    import sys as _sys
    import importlib as _il

    wid = worker_id or ""
    if not wid.startswith("worker:"):
        return None
    rest = wid[len("worker:"):]
    # Strip the hex suffix: worker:name:hex -> name
    parts = rest.split(":")
    if not parts or not parts[0]:
        return None
    name = parts[0]
    fq_name = f"src.friday.workers.{name}"

    # Ensure the project root is on sys.path (same pattern as verification).
    here = Path(__file__).resolve().parents[2]  # src/friday/runtime -> project root
    if str(here) not in _sys.path:
        _sys.path.insert(0, str(here))

    try:
        return _il.import_module(fq_name)
    except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
        return None


def resolve_executor(worker_id: str, workspace: str = ".") -> Optional[Executor]:
    """Return the real execution adapter for a registry ``worker:<name>`` id.

    Covers the native built-in executors AND the M10 external AI adapters
    (Claude/Codex/Gemini/OpenCode/Aider/DeepSeek). The runtime invokes the
    adapter; unavailability is reported by the adapter's own verify() (exit
    code / missing binary), never by fabricating success. Returns None only
    for ids with no execution adapter so the runtime records a clean failure.

    After the hardcoded table, falls back to dynamic import of a module at
    ``src.friday.workers/<name>`` — the convention used by the meta-engine
    when deploying a self-generated worker. This is the dispatch link that
    makes auto-deployed workers invocable by the runtime.
    """
    name = (worker_id or "").lower()
    # Hardcoded built-in executors (unchanged — fast path for known ids).
    if name == "worker:shell":
        return BuiltinShellExecutor(workspace=workspace)
    if name == "worker:git":
        return GitExecutor(workspace=workspace)
    if name == "worker:filesystem":
        return FileExecutor(workspace=workspace)
    if name == "worker:python":
        return BuiltinPythonExecutor(workspace=workspace)
    if name == "worker:testing":
        return TestingExecutor(workspace=workspace)
    if name == "worker:documentation":
        return DocumentationExecutor(workspace=workspace)
    if name == "worker:synthesis":
        return SynthesisExecutor(workspace=workspace)
    if name == "worker:claude":
        return ClaudeCodeWorker(workspace=workspace)
    if name == "worker:codex":
        return CodexWorker(workspace=workspace)
    if name == "worker:gemini":
        return GeminiWorker(workspace=workspace)
    if name == "worker:opencode":
        return OpenCodeWorker(workspace=workspace)
    if name == "worker:aider":
        return AiderWorker(workspace=workspace)
    if name == "worker:deepseek":
        return DeepSeekWorker(workspace=workspace)

    if name == "worker:hyprctl":
        from .hyprland_executor import HyprlandExecutor
        return HyprlandExecutor(workspace=workspace)
    if name == "worker:browser":
        from .browser_executor import BrowserExecutor
        return BrowserExecutor()

    # Formed skill dispatch (Pillar B Stage 4).
    # If the worker_id corresponds to a worker_kind='formed_skill' row,
    # build a ReplayExecutor from the formed_skills payload.
    if name.startswith("worker:"):
        try:
            _rconn = _resolve_connect()
            _row = _rconn.execute(
                "SELECT worker_kind, manifest_ref FROM workers WHERE id = ?",
                (worker_id,)
            ).fetchone()
            if _row is not None and _row["worker_kind"] == "formed_skill":
                ref = _row["manifest_ref"] or ""
                if ref.startswith("formed_skill:"):
                    skill_id = int(ref.split(":")[1])
                    fs_row = _rconn.execute(
                        "SELECT * FROM formed_skills WHERE id = ?", (skill_id,)
                    ).fetchone()
                    if fs_row:
                        import json
                        task_graph = json.loads(fs_row["task_graph"]) if fs_row["task_graph"] else []
                        exemplars = json.loads(fs_row["exemplars"]) if fs_row["exemplars"] else {}
                        _rconn.close()
                        from ..skill_formation import ReplayExecutor
                        return ReplayExecutor(
                            worker_id=worker_id,
                            task_graph=task_graph,
                            exemplars=exemplars,
                            workspace=workspace,
                        )
            _rconn.close()
        except Exception:
            try:
                _rconn.close()
            except Exception:
                pass

    # Dynamic fallback: try to find and load a self-generated worker module.
    module = _find_auto_worker_module(worker_id)
    if module is not None and hasattr(module, "execute"):
        return DynamicWorkerExecutor(
            module, worker_id=worker_id, workspace=workspace)

    return None


# Registry id -> the execution worker_id used for resolution (1:1 here).
BUILTIN_EXECUTION_IDS = (
    "worker:shell", "worker:git", "worker:filesystem", "worker:python",
    "worker:testing", "worker:documentation", "worker:synthesis",
)

# External AI executor ids (non-deterministic; used only as a fallback when no
# deterministic built-in covers the task).
AI_EXECUTOR_IDS = (
    "worker:claude", "worker:codex", "worker:gemini",
    "worker:opencode", "worker:aider", "worker:deepseek",
)

# Configurable AI-executor timeout (seconds). External AI CLIs can hang in
# headless mode; a bounded timeout prevents indefinite waits. Override via the
# FRIDAY_AI_TIMEOUT env var.
AI_TIMEOUT = int(os.environ.get("FRIDAY_AI_TIMEOUT", "30"))

# Deterministic built-in fallback order used when an AI executor fails/hangs.
_DETERMINISTIC_FALLBACKS = (
    "worker:python", "worker:filesystem", "worker:shell", "worker:git",
    "worker:testing", "worker:documentation",
)


def _is_ai_executor_id(worker_id: str) -> bool:
    return (worker_id or "").lower() in AI_EXECUTOR_IDS


def fallback_chain(worker_id: str) -> List[str]:
    """Ordered executor ids to try for a task initially assigned to `worker_id`.

    - Deterministic built-in: only itself (it is the terminal fallback).
    - AI executor: itself, then the other AI executors, then deterministic
      built-ins. One failure never terminates the mission unless every
      candidate fails.
    """
    primary = (worker_id or "").lower()
    if not _is_ai_executor_id(primary):
        return [primary]
    others = [w for w in AI_EXECUTOR_IDS if w != primary]
    return [primary, *others, *_DETERMINISTIC_FALLBACKS]


def execute_with_fallback(task, primary_id: str, workspace: str = ".",
                          worker_resolver=None) -> ExecutionResult:
    """Run `task` via `primary_id`, falling back across candidates on failure.

    Each candidate is invoked; the first success wins. A failed/hung executor
    (timeout, non-zero exit, raised exception) is skipped and the next tried.
    Only if ALL candidates fail (or none exist) is an overall failure returned,
    carrying the last error. Mission execution therefore continues whenever any
    viable alternative exists. Idempotent-friendly: never fabricates success.

    `worker_resolver`, when given, maps a candidate id to its executor and
    overrides the registry lookup (resolve_executor). The runtime always passes
    its own resolver here so injected test mocks are honored on the fallback
    path too — never reaching a live model behind the resolver's back.
    """
    chain = fallback_chain(primary_id)
    last: Optional[ExecutionResult] = None
    last_error = f"no executor for {primary_id}"
    tried: List[str] = []
    for wid in chain:
        exe = worker_resolver(wid) if worker_resolver else resolve_executor(wid, workspace)
        if exe is None:
            last_error = f"{wid} has no execution adapter"
            continue
        # Bound AI executors to AI_TIMEOUT so a headless hang cannot stall the
        # whole mission; deterministic built-ins keep their own timeout.
        if _is_ai_executor_id(wid) and getattr(exe, "_timeout", None):
            try:
                exe._timeout = AI_TIMEOUT
            except Exception:
                pass
        tried.append(wid)
        try:
            result = exe.execute(task)
        except Exception as e:  # defensive: a crashing adapter is just a skip
            last_error = f"{wid} raised: {type(e).__name__}: {e}"
            last = ExecutionResult(
                success=False, stdout="", stderr=str(e), exit_code=None,
                duration_ms=0, error=last_error)
            continue
        # Phase 1.5: run the executor's own contract check (mirrors the
        # dispatcher) so verification_passed is populated on the fallback path
        # too, not just the direct dispatch path.
        try:
            vres = exe.verify(task, result)
            result.verification_passed = vres.passed
            result.metadata = {**result.metadata,
                               "verified": vres.passed,
                               "verify_reason": vres.reason}
        except Exception:
            pass
        if result.success:
            return result
        last = result
        last_error = result.error or f"{wid} failed (exit {result.exit_code})"
    return ExecutionResult(
        success=False,
        stdout=(last.stdout if last else ""),
        stderr=(last.stderr if last else last_error),
        exit_code=(last.exit_code if last else None),
        duration_ms=(last.duration_ms if last else 0),
        error=f"all executors failed [{', '.join(tried)}]: {last_error}",
    )


# ---------------------------------------------------------------------------
# CLIWorker — generic subprocess base
# ---------------------------------------------------------------------------

@dataclass
class Invocation:
    argv: list
    stdin: Optional[str] = None
    cwd: str = "."
    env: dict = field(default_factory=dict)
    timeout: int = _DEFAULT_TIMEOUT
    stream: bool = False


class CLIExecutor(Executor):
    """Base for any executor invoked via a subprocess. Owns ALL subprocess
    mechanics; subclasses implement only build_invocation(task)."""
    def __init__(self, worker_id: Optional[str] = None, workspace: str = ".",
                 timeout: int = _DEFAULT_TIMEOUT) -> None:
        if worker_id is not None:
            self.worker_id = worker_id
        self._workspace = workspace
        self._timeout = timeout

    def execute(self, task) -> ExecutionResult:
        from datetime import datetime, timezone
        inv = self.build_invocation(task)
        t0 = time.monotonic()
        started = datetime.now(timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                inv.argv, input=inv.stdin, cwd=inv.cwd,
                env=inv.env or None, capture_output=True, text=True,
                timeout=inv.timeout)
            dur = int((time.monotonic() - t0) * 1000)
            # CLI workers emit content on stdout; persist it as a workspace
            # artifact so file-producing tasks (implementation/documentation)
            # actually land a file instead of stdout being discarded.
            artifacts = self._persist_stdout(task, proc.stdout)
            return ExecutionResult(
                success=proc.returncode == 0, stdout=proc.stdout,
                stderr=proc.stderr, exit_code=proc.returncode,
                duration_ms=dur, artifacts=artifacts,
                error="" if proc.returncode == 0 else proc.stderr,
                worker_id=self.worker_id, started_at=started,
                ended_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=False, stdout="", stderr=str(e), exit_code=None,
                duration_ms=dur, error=f"{type(e).__name__}: {e}",
                worker_id=self.worker_id, started_at=started,
                ended_at=datetime.now(timezone.utc).isoformat())

    def build_invocation(self, task) -> Invocation:
        raise NotImplementedError

    def _persist_stdout(self, task, stdout: str) -> List[str]:
        """Write CLI stdout to a workspace file for file-producing tasks.

        Only implementation/documentation tasks are expected to yield a file;
        others (analysis/design) keep stdout in-memory. Returns artifact paths.
        """
        if not stdout or not stdout.strip():
            return []
        ttype = (getattr(task, "task_type", "") or "").lower()
        if ttype not in ("implementation", "documentation"):
            return []
        title = (getattr(task, "title", "") or "task").strip()
        # When the task title is generic (e.g. "Implement backend logic"),
        # derive a meaningful filename from the goal (e.g. "calculator.py"
        # from "Create calculator.py containing a simple CLI calculator.").
        goal = (getattr(task, "goal", "") or "").strip()
        ext = ".py" if ttype == "implementation" else ".md"
        filename = _filename_from_goal(goal, title, ext)
        ws = self._workspace or "."
        os.makedirs(ws, exist_ok=True)
        path = os.path.join(ws, filename)
        try:
            with open(path, "w") as f:
                f.write(stdout)
            return [path]
        except OSError:
            return []

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        """Default verification: exit 0 + non-empty stdout (sane for AI CLIs)."""
        passed = result.exit_code == 0 and bool((result.stdout or "").strip())
        return VerificationResult(
            passed=passed,
            reason="exit_code==0 and stdout non-empty" if passed
            else "exit_code!=0 or empty stdout")


# backward-compat alias
CLIWorker = CLIExecutor


# ---------------------------------------------------------------------------
# External AI CLI adapters — auto-detected via PATH
# ---------------------------------------------------------------------------

def _resolve_binary(name: str) -> str:
    """Full PATH to a binary if present, else the bare name (so discovery and
    reporting work without launching a missing tool)."""
    return shutil.which(name) or name


class ClaudeCodeWorker(CLIExecutor):
    worker_id = "worker:claude"

    @staticmethod
    def _dependency_context(task) -> str:
        """Build a compact dependency context block for the prompt.

        Reads task.dependency_summaries (populated by the wave executor
        after each wave completes). Returns empty string when there are
        no completed dependencies.
        """
        summaries = getattr(task, "dependency_summaries", None) or {}
        if not summaries:
            return ""
        lines = ["\n--- Upstream task outputs (completed dependencies) ---"]
        for dep_id, summary in summaries.items():
            lines.append(f"  [{dep_id}] {summary}")
        lines.append("--- End upstream outputs ---\n")
        return "\n".join(lines)

    def build_invocation(self, task):
        # Compose a real prompt from task fields (claude --print requires a
        # non-empty prompt; runtime_payload is usually empty for plan tasks).
        title = getattr(task, "title", "") or "Task"
        desc = getattr(task, "description", "") or ""
        acs = getattr(task, "acceptance_criteria", []) or []
        lines = [f"# {title}", ""]
        if desc:
            lines.append(desc)
            lines.append("")
        if acs:
            lines.append("## Acceptance criteria")
            for a in acs:
                lines.append(f"- {a}")
            lines.append("")
        # Inject dependency summaries if available.
        dep_ctx = self._dependency_context(task)
        if dep_ctx:
            lines.append(dep_ctx)
        prompt = "\n".join(lines).rstrip() + "\n"
        desc = getattr(task, "description", "") or ""
        acs = getattr(task, "acceptance_criteria", []) or []
        lines = [f"# {title}", ""]
        if desc:
            lines.append(desc)
            lines.append("")
        if acs:
            lines.append("## Acceptance criteria")
            for a in acs:
                lines.append(f"- {a}")
            lines.append("")
        prompt = "\n".join(lines).rstrip() + "\n"
        # Claude Code's --print is the documented headless mode: when
        # stdout is not a TTY it skips the workspace-trust dialog.
        # But file/tool actions still block on a permission prompt with
        # no TTY to answer it -> the process hangs.
        # --dangerously-skip-permissions removes that blocker so
        # headless file-writing goals run unattended. Prompt via
        # stdin (multiline argv also hangs). See M10.1 dogfooding
        # regression.
        # --output-format json yields a structured result we verify explicitly
        # (is_error / result) instead of trusting only the exit code.
        return Invocation(
            argv=[_resolve_binary("claude"), "--print",
                   "--output-format", "json",
                   "--dangerously-skip-permissions",
                   "--model", "oc/deepseek-v4-flash-free"],
            stdin=prompt, timeout=AI_TIMEOUT)

    def verify(self, task, result: "ExecutionResult") -> "VerificationResult":
        """Verify a Claude run from its STRUCTURED JSON result, not just exit 0.

        The CLI emits `{ "type": "result", "is_error": bool, "result": "..." }`
        on stdout (--output-format json). A 0 exit with is_error:true is a real
        failure; we never report success on an error payload. Non-JSON output
        (older CLI / streaming) falls back to the parent's exit-code rule so the
        adapter degrades gracefully.
        """
        import json as _json
        if not result.success:
            return VerificationResult(
                passed=False,
                reason=result.error or f"claude exited {result.exit_code}")
        text = (result.stdout or "").strip()
        try:
            obj = _json.loads(text)
        except (ValueError, TypeError):
            # Phase 4 requirement: hard-fail on non-JSON instead of silently degrading.
            sample = text[:200]
            return VerificationResult(
                passed=False,
                reason=f"unexpected output format from claude CLI — expected JSON, got: {sample}"
            )
        if isinstance(obj, dict) and obj.get("is_error"):
            msg = obj.get("result") or obj.get("subtype") or "claude reported error"
            return VerificationResult(passed=False, reason=f"claude is_error: {msg}")
        return VerificationResult(passed=True, reason="claude result OK (is_error=false)")


class CodexWorker(CLIExecutor):
    worker_id = "worker:codex"
    def build_invocation(self, task):
        # `codex` defaults to an interactive TUI that requires a TTY and prompts
        # for approval; `exec` is the headless entry. Bypass approvals so the
        # worker can run unattended (only safe in already-sandboxed envs).
        return Invocation(
            argv=[_resolve_binary("codex"), "exec",
                  "--dangerously-bypass-approvals-and-sandbox", _payload(task)],
            timeout=AI_TIMEOUT)


class GeminiWorker(CLIExecutor):
    worker_id = "worker:gemini"
    def build_invocation(self, task):
        # `-p` = headless/non-interactive; `-y` = auto-approve all actions.
        return Invocation(argv=[_resolve_binary("gemini"), "-p", "-y",
                                 _payload(task)], timeout=AI_TIMEOUT)


class OpenCodeWorker(CLIExecutor):
    worker_id = "worker:opencode"
    def build_invocation(self, task):
        # `run` is the headless entry (no TUI).
        return Invocation(argv=[_resolve_binary("opencode"), "run",
                                 _payload(task)], timeout=AI_TIMEOUT)


class AiderWorker(CLIExecutor):
    worker_id = "worker:aider"
    def build_invocation(self, task):
        return Invocation(argv=[_resolve_binary("aider"), "--message",
                                 _payload(task)], timeout=AI_TIMEOUT)


class DeepSeekWorker(CLIExecutor):
    worker_id = "worker:deepseek"
    def build_invocation(self, task):
        if shutil.which("deepseek"):
            return Invocation(argv=[_resolve_binary("deepseek"), _payload(task)],
                               timeout=AI_TIMEOUT)
        # API mode (HTTP) would override execute(); here we still produce a
        # valid Invocation shape so verification can report unavailability
        # gracefully rather than crashing.
        return Invocation(argv=[_resolve_binary("deepseek")], timeout=AI_TIMEOUT)

    def is_available(self) -> bool:
        return shutil.which("deepseek") is not None or bool(
            os.environ.get("DEEPSEEK_API_KEY"))
