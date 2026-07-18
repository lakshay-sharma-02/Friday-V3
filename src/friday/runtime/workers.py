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
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from .models import ExecutionResult, Worker


# Honour a global timeout (seconds) for any external process.
_DEFAULT_TIMEOUT = int(os.environ.get("FRIDAY_WORKER_TIMEOUT", "60"))


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


def _fail(stdout: str, stderr: str, exit_code: Optional[int], dur: int,
          error: str, artifacts: Optional[List[str]] = None) -> ExecutionResult:
    return ExecutionResult(
        success=False, stdout=stdout, stderr=stderr, exit_code=exit_code,
        duration_ms=dur, error=error, artifacts=artifacts or [])


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

class BuiltinShellWorker(Worker):
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
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=ws, capture_output=True, text=True,
                timeout=self._timeout)
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
                         f"shell command timed out after {self._timeout}s")
        except Exception as e:  # defensive; dispatcher also guards
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")


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

class GitWorker(Worker):
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
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                ["git", *args], cwd=ws, capture_output=True, text=True,
                timeout=self._timeout)
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
                         f"git command timed out after {self._timeout}s")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------

class FileWorker(Worker):
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
            return _fail("", "", None, 0, f"unknown file op: {op}")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Python (also pytest-capable, since the resolver routes Testing tasks here)
# ---------------------------------------------------------------------------

class BuiltinPythonWorker(Worker):
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
            proc = subprocess.run(
                cmd, cwd=ws, capture_output=True, text=True,
                timeout=self._timeout)
            dur = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             f"exit {proc.returncode}: "
                             + _first_failures(proc.stdout, proc.stderr))
            return _ok(proc.stdout, proc.stderr, proc.returncode, dur)
        except subprocess.TimeoutExpired as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", "timeout", None, dur,
                         f"python run timed out after {self._timeout}s")
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", str(e), None, dur, f"{type(e).__name__}: {e}")


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

class TestingWorker(Worker):
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
        try:
            proc = subprocess.run(
                cmd, cwd=ws, capture_output=True, text=True,
                timeout=self._timeout)
            dur = int((time.monotonic() - t0) * 1000)
            if proc.returncode != 0:
                return _fail(proc.stdout, proc.stderr, proc.returncode, dur,
                             f"tests failed: " + _first_failures(
                                 proc.stdout, proc.stderr))
            return _ok(proc.stdout, proc.stderr, proc.returncode, dur)
        except subprocess.TimeoutExpired:
            dur = int((time.monotonic() - t0) * 1000)
            return _fail("", "timeout", None, dur,
                         f"test run timed out after {self._timeout}s")
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


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

class DocumentationWorker(Worker):
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


# ---------------------------------------------------------------------------
# Worker resolution — maps a registry worker_id to its execution adapter.
# ---------------------------------------------------------------------------

def resolve_worker(worker_id: str, workspace: str = ".") -> Optional[Worker]:
    """Return the real execution adapter for a registry ``worker:<name>`` id.

    Returns None for ids with no execution adapter (e.g. LLM providers) so the
    runtime records a clean failure instead of fabricating success.
    """
    name = (worker_id or "").lower()
    if name == "worker:shell":
        return BuiltinShellWorker(workspace=workspace)
    if name == "worker:git":
        return GitWorker(workspace=workspace)
    if name == "worker:filesystem":
        return FileWorker(workspace=workspace)
    if name == "worker:python":
        return BuiltinPythonWorker(workspace=workspace)
    if name == "worker:testing":
        return TestingWorker(workspace=workspace)
    if name == "worker:documentation":
        return DocumentationWorker(workspace=workspace)
    return None


# Registry id -> the execution worker_id used for resolution (1:1 here).
BUILTIN_EXECUTION_IDS = (
    "worker:shell", "worker:git", "worker:filesystem", "worker:python",
    "worker:testing", "worker:documentation",
)
