"""Built-in Workers regression tests (Milestone 9.7).

Covers: registration (idempotent), worker lookup, dispatch, each of the six
workers (shell/git/file/python/documentation/testing), runtime execution via
the real worker resolver, and worker failure handling.

Workers execute REAL work and never fabricate success: every result is
verified (exit code / file existence / git delta / pytest exit).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from friday.db import connect
from friday.runtime import dispatch
from friday.runtime.models import RuntimeTask, RunState, ExecutionResult
from friday.runtime.workers import (
    BuiltinPythonWorker,
    BuiltinShellWorker,
    DocumentationWorker,
    FileWorker,
    GitWorker,
    TestingWorker,
    resolve_worker,
)
from friday.worker.engine import WorkerRegistry, BUILTIN_WORKERS


from friday.runtime.workers import CLIWorker, Invocation, VerificationResult
from friday.runtime.workers import (
    ClaudeCodeWorker, CodexWorker, GeminiWorker, OpenCodeWorker,
    AiderWorker, DeepSeekWorker)

from friday.runtime.dispatcher import dispatch
from friday.runtime.models import RuntimeTask


def test_external_adapters_build_invocation():
    for W in (ClaudeCodeWorker, CodexWorker, GeminiWorker, OpenCodeWorker,
              AiderWorker, DeepSeekWorker):
        w = W()
        inv = w.build_invocation(_task("do the thing"))
        assert isinstance(inv, Invocation)
        assert inv.argv  # non-empty command


def test_deepseek_is_available_no_crash(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    w = DeepSeekWorker()
    # is_available() must be callable and return a bool without raising
    assert isinstance(w.is_available(), bool)


class _EchoWorker(CLIWorker):
    worker_id = "worker:echo"
    def build_invocation(self, task):
        return Invocation(argv=["printf", "%s", (task.runtime_payload or "")])


def test_cliworker_runs_invocation():
    t = _task("hi")
    res = _EchoWorker().execute(t)
    assert res.success is True
    assert res.stdout == "hi"


def test_cliworker_default_verify():
    ok = ExecutionResult(success=True, exit_code=0, stdout="x")
    bad = ExecutionResult(success=True, exit_code=0, stdout="")
    t = _task("hi")
    assert CLIWorker().verify(t, ok).passed is True
    assert CLIWorker().verify(t, bad).passed is False


def _task(payload: str = "", **kw) -> RuntimeTask:
    return RuntimeTask(
        execution_id="e1", session_id="s1", schedule_id="g1",
        task_id=kw.get("task_id", "t1"), worker_id="worker:x",
        wave=1,
        runtime_payload=payload,
        **{k: v for k, v in kw.items() if k != "task_id"},
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "workers_test.db")
    yield c
    c.close()


# ===================================================================
# Registration
# ===================================================================

def test_register_builtins_idempotent(conn):
    reg = WorkerRegistry(conn)
    r1 = reg.register_builtins()
    # Documentation + Testing are among the built-ins.
    names = {w.name for w in reg.all_workers()}
    assert "Documentation" in names
    assert "Testing" in names
    # Second registration must not duplicate.
    r2 = reg.register_builtins()
    assert reg.count() == r1.created + r1.updated  # stable count
    assert r2.created == 0  # everything already existed


def test_register_builtin_has_six_execution_workers(conn):
    reg = WorkerRegistry(conn)
    reg.register_builtins()
    ids = {w.id for w in reg.all_workers()}
    for wid in ("worker:shell", "worker:git", "worker:filesystem",
                "worker:python", "worker:documentation", "worker:testing"):
        assert wid in ids


# ===================================================================
# Lookup / resolution
# ===================================================================

def test_resolve_worker_known():
    assert isinstance(resolve_worker("worker:shell"), BuiltinShellWorker)
    assert isinstance(resolve_worker("worker:git"), GitWorker)
    assert isinstance(resolve_worker("worker:filesystem"), FileWorker)
    assert isinstance(resolve_worker("worker:python"), BuiltinPythonWorker)
    assert isinstance(resolve_worker("worker:documentation"), DocumentationWorker)
    assert isinstance(resolve_worker("worker:testing"), TestingWorker)


def test_resolve_worker_unknown_returns_none():
    # LLM provider ids have no execution adapter.
    assert resolve_worker("worker:claude") is None
    assert resolve_worker("worker:codex") is None


# ===================================================================
# Dispatch
# ===================================================================

def test_dispatch_missing_worker_fails():
    from friday.runtime.models import Worker
    res = dispatch(_task("echo hi"), None)
    assert res.success is False
    assert "no worker" in res.error


def test_dispatch_exception_becomes_failure():
    from friday.runtime.models import Worker
    class Boom(Worker):
        worker_id = "worker:boom"
        def execute(self, task):
            raise RuntimeError("kaboom")
    res = dispatch(_task("x"), Boom())
    assert res.success is False
    assert "kaboom" in res.error


# ===================================================================
# Shell worker
# ===================================================================

def test_shell_worker_runs_command(tmp_path):
    w = BuiltinShellWorker(workspace=str(tmp_path))
    res = w.execute(_task("echo hello"))
    assert res.success is True
    assert "hello" in res.stdout
    assert res.exit_code == 0


def test_shell_worker_nonzero_exit_fails(tmp_path):
    w = BuiltinShellWorker(workspace=str(tmp_path))
    res = w.execute(_task("exit 3"))
    assert res.success is False
    assert res.exit_code == 3


def test_shell_worker_empty_payload_gathers_evidence(tmp_path):
    # No command -> real git-log evidence gathering in a git workspace.
    repo = tmp_path / "r"
    repo.mkdir()
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} commit -q --allow-empty -m init")
    w = BuiltinShellWorker(workspace=str(repo))
    res = w.execute(_task(""))
    assert res.success is True  # git log of a seeded repo has a header


def test_shell_worker_timeout(tmp_path):
    w = BuiltinShellWorker(workspace=str(tmp_path), timeout=1)
    res = w.execute(_task("sleep 5"))
    assert res.success is False
    assert "timed out" in res.error


# ===================================================================
# Git worker
# ===================================================================

def test_git_worker_status(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    os.system(f"git -C {repo} init -q")
    w = GitWorker(workspace=str(repo))
    res = w.execute(_task("status --short"))
    assert res.success is True


def test_git_worker_commit_changes_tree(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email t@t.co")
    os.system(f"git -C {repo} config user.name t")
    (repo / "a.txt").write_text("hi")
    w = GitWorker(workspace=str(repo))
    res = w.execute(_task('add a.txt'))
    assert res.success is True
    res2 = w.execute(_task('commit -m "add a"'))
    assert res2.success is True
    # Working tree changed (staged -> committed): `git status --short` is now
    # empty, which is what proves the mutation landed.
    assert GitWorker._porcelain(str(repo)).strip() == ""


def test_git_worker_refuses_push(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    os.system(f"git -C {repo} init -q")
    w = GitWorker(workspace=str(repo))
    res = w.execute(_task("push origin main"))
    assert res.success is False
    assert "never push" in res.stderr


def test_git_worker_noop_commit_fails(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email t@t.co")
    os.system(f"git -C {repo} config user.name t")
    w = GitWorker(workspace=str(repo))
    res = w.execute(_task('commit -m "nothing"'))
    # Nothing staged -> no working-tree change -> reject.
    assert res.success is False


# ===================================================================
# File worker
# ===================================================================

def test_file_worker_write_read(tmp_path):
    w = FileWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps(
        {"op": "write", "path": "out.txt", "content": "data"})))
    assert res.success is True
    assert (tmp_path / "out.txt").read_text() == "data"


def test_file_worker_write_empty_fails(tmp_path):
    w = FileWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps(
        {"op": "write", "path": "empty.txt", "content": ""})))
    assert res.success is False


def test_file_worker_append(tmp_path):
    w = FileWorker(workspace=str(tmp_path))
    w.execute(_task(json.dumps({"op": "write", "path": "f", "content": "a"})))
    w.execute(_task(json.dumps({"op": "append", "path": "f", "content": "b"})))
    assert (tmp_path / "f").read_text() == "ab"


def test_file_worker_replace(tmp_path):
    w = FileWorker(workspace=str(tmp_path))
    w.execute(_task(json.dumps({"op": "write", "path": "f", "content": "foo bar"})))
    res = w.execute(_task(json.dumps(
        {"op": "replace", "path": "f", "old": "bar", "new": "baz"})))
    assert res.success is True
    assert (tmp_path / "f").read_text() == "foo baz"


def test_file_worker_copy_move_delete(tmp_path):
    w = FileWorker(workspace=str(tmp_path))
    w.execute(_task(json.dumps({"op": "write", "path": "a", "content": "x"})))
    w.execute(_task(json.dumps({"op": "copy", "src": "a", "dst": "b"})))
    assert (tmp_path / "b").exists()
    w.execute(_task(json.dumps({"op": "move", "src": "b", "dst": "c"})))
    assert (tmp_path / "c").exists() and not (tmp_path / "b").exists()
    w.execute(_task(json.dumps({"op": "delete", "path": "c"})))
    assert not (tmp_path / "c").exists()


def test_file_worker_bad_payload_fails(tmp_path):
    w = FileWorker(workspace=str(tmp_path))
    res = w.execute(_task("not json"))
    assert res.success is False


# ===================================================================
# Python worker
# ===================================================================

def test_python_worker_runs_source(tmp_path):
    w = BuiltinPythonWorker(workspace=str(tmp_path))
    res = w.execute(_task("print('hello from py')"))
    assert res.success is True
    assert "hello from py" in res.stdout


def test_python_worker_raises_fails(tmp_path):
    w = BuiltinPythonWorker(workspace=str(tmp_path))
    res = w.execute(_task("raise ValueError('boom')"))
    assert res.success is False
    assert "boom" in (res.stderr + res.error)


def test_python_worker_runs_pytest(tmp_path):
    (tmp_path / "test_x.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n")
    w = BuiltinPythonWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps({"pytest": ["test_x.py"]})))
    assert res.success is True


def test_python_worker_pytest_failure_reported(tmp_path):
    (tmp_path / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n")
    w = BuiltinPythonWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps({"pytest": ["test_bad.py"]})))
    assert res.success is False
    assert "test_bad" in (res.stdout + res.stderr)


# ===================================================================
# Testing worker
# ===================================================================

def test_testing_worker_runs_pytest(tmp_path):
    (tmp_path / "test_t.py").write_text(
        "def test_pass():\n    assert True\n")
    w = TestingWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps({"path": "test_t.py"})))
    assert res.success is True


def test_testing_worker_failure_summary(tmp_path):
    (tmp_path / "test_fail.py").write_text(
        "def test_fail():\n    assert 1 == 2\n")
    w = TestingWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps({"path": "test_fail.py"})))
    assert res.success is False
    assert "FAILED" in (res.stdout + res.stderr) or "test_fail" in (res.stdout + res.stderr)


def test_testing_worker_bad_payload_fails(tmp_path):
    w = TestingWorker(workspace=str(tmp_path))
    res = w.execute(_task("not json"))
    assert res.success is False


# ===================================================================
# Documentation worker
# ===================================================================

def test_documentation_worker_writes_explicit(tmp_path):
    w = DocumentationWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps(
        {"path": "README.md", "content": "# My Project\n\nDocs.\n"})))
    assert res.success is True
    assert (tmp_path / "README.md").read_text().startswith("# My Project")


def test_documentation_worker_derives_from_task(tmp_path):
    t = _task("", task_id="doc1")
    t.title = "Add README documentation"
    t.description = "Project overview."
    t.acceptance_criteria = ["explains purpose"]
    w = DocumentationWorker(workspace=str(tmp_path))
    res = w.execute(t)
    assert res.success is True
    text = (tmp_path / "README.md").read_text()
    assert "Add README documentation" in text
    assert "explains purpose" in text


def test_documentation_worker_empty_fails(tmp_path):
    w = DocumentationWorker(workspace=str(tmp_path))
    res = w.execute(_task(json.dumps({"path": "x.md", "content": ""})))
    assert res.success is False


# ===================================================================
# Runtime execution via the real worker resolver
# ===================================================================

def test_runtime_uses_real_worker_resolver(conn, tmp_path):
    """End-to-end: schedule a graph and run it with the real worker resolver;
    the documentation task writes a README in the workspace."""
    from friday.planning import PlanEngine, TaskGraphEngine
    from friday.resolver import CapabilityResolver
    from friday.scheduler import TaskScheduler
    from friday.runtime import RuntimeEngine

    repo = tmp_path / "ws"
    repo.mkdir()
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email t@t.co")
    os.system(f"git -C {repo} config user.name t")
    os.system(f"git -C {repo} commit -q --allow-empty -m init")

    reg = WorkerRegistry(conn)
    reg.register_builtins()
    PlanEngine(conn).generate("Add README documentation")
    g = TaskGraphEngine(conn).generate("Add README documentation")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id)

    # Mirror production (cli_runtime._resolve_any): known execution ids map to
    # their adapter; unknown ids (e.g. LLM-only worker:search) delegate to the
    # shell worker, which performs real, verifiable repo evidence-gathering. No
    # fabricated success for unassigned tasks.
    from friday.runtime.workers import BuiltinShellWorker
    def _resolve_any(wid):
        w = resolve_worker(wid or "worker:mock", str(repo))
        return w if w is not None else BuiltinShellWorker(workspace=str(repo))
    eng = RuntimeEngine(conn, worker_resolver=_resolve_any)
    report = eng.run(sched.schedule)
    # The documentation task must have executed and written the README.
    assert (repo / "README.md").exists()
    assert report.failed == 0


class _EchoVerifyWorker(CLIWorker):
    worker_id = "worker:echov"
    def build_invocation(self, task):
        return Invocation(argv=["printf", "%s", (task.runtime_payload or "")])


def test_dispatch_runs_verify_and_records_metadata():
    t = _task("ok")
    res = dispatch(t, _EchoVerifyWorker())
    assert res.success is True
    # verification ran and its outcome is recorded in metadata
    assert "verified" in res.metadata
    assert res.metadata["verified"] is True
