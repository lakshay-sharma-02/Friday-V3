"""Runtime models (Milestone 9.5).

The Execution Runtime is the ONLY layer that *performs* work. It consumes a
frozen `ExecutionSchedule` (from M9.4) and executes it. It NEVER plans,
schedules, resolves capabilities, reviews, repairs, retries, or learns.

This module defines:
  - `ExecutionResult` — what a `Worker.execute(task)` returns (the only thing
    the Runtime learns from a worker).
  - The generic `Worker` execution interface + concrete adapters
    (`MockWorker`, `PythonWorker`, `ShellWorker`, ...). The Runtime core calls
    `worker.execute(task)` and knows NOTHING about which provider is behind it.
    There is deliberately no `if provider == ...` anywhere in the Runtime.
  - `RunState` / `SessionState` — the execution state machine.
  - `RuntimeTask`, `RuntimeEvent`, `ExecutionReport` — pure data + (de)serialization.

Contract version (Law 24): every persisted runtime row carries `schema_version`.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# Contract version. Bump only on a breaking change to the runtime shape.
SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Execution state machine (the ONLY states the Runtime uses).
# ---------------------------------------------------------------------------

class RunState(str, Enum):
    """Per-task execution lifecycle. Linear: PENDING -> RUNNING -> terminal."""

    PENDING = "pending"        # queued, not yet started
    RUNNING = "running"        # worker invoked, in flight
    SUCCESS = "success"        # worker reported success
    FAILED = "failed"         # worker reported failure / raised
    CANCELLED = "cancelled"    # ancestor failed; never executed (blocked)

    @classmethod
    def from_str(cls, s: str) -> "RunState":
        s = (s or "").strip().lower()
        for k in cls:
            if k.value == s:
                return k
        raise ValueError(f"{cls.__name__} has no member {s!r}")

    @property
    def terminal(self) -> bool:
        return self in (RunState.SUCCESS, RunState.FAILED, RunState.CANCELLED)


class SessionState(str, Enum):
    """Per-session lifecycle."""

    CREATED = "created"
    RUNNING = "running"
    FINISHED = "finished"

    @classmethod
    def from_str(cls, s: str) -> "SessionState":
        s = (s or "").strip().lower()
        for k in cls:
            if k.value == s:
                return k
        raise ValueError(f"{cls.__name__} has no member {s!r}")


# ---------------------------------------------------------------------------
# Worker execution result.
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """What a Worker returns from `execute(task)`. Opaque to the Runtime.

    `success` drives the state transition (SUCCESS vs FAILED). The Runtime
    stores stdout/stderr/artifacts/exit_code/duration but never interprets them.
    """

    success: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    duration_ms: int = 0
    error: str = ""              # exception message if the worker raised
    worker_id: Optional[str] = None      # provenance
    started_at: Optional[str] = None     # provenance
    ended_at: Optional[str] = None       # provenance
    metadata: dict = field(default_factory=dict)  # provenance
    # The executor's own contract check (after execute()). None until verified.
    # Separates "process exited 0" (success) from "contract satisfied"
    # (verification_passed). Truthful mission reporting keys off this.
    verification_passed: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "artifacts": list(self.artifacts),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "worker_id": self.worker_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "metadata": dict(self.metadata),
            "verification_passed": self.verification_passed,
        }


# ---------------------------------------------------------------------------
# Generic worker interface. THE contract the Runtime depends on.
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Objective correctness verdict from a worker/executor's verify() step."""
    passed: bool
    reason: str = ""
    # Structured evidence behind the verdict (test summary, git diff presence,
    # symbol counts...). Persisted to the journal so the mission is provable.
    evidence: dict = field(default_factory=dict)


class Executor:
    """Generic execution backend. The Runtime calls `execute(task)` and nothing
    else. Concrete adapters subclass this; the Runtime never branches on which
    one it received.

    `task` is the `RuntimeTask` the Runtime hands in (carrying the scheduled
    task's structured fields). The executor decides HOW to run it.
    """

    #: registry id this adapter executes for (e.g. "worker:mock"). The Runtime
    #: matches a scheduled task's `worker_id` to an adapter via this.
    worker_id: str = ""

    def execute(self, task) -> ExecutionResult:
        raise NotImplementedError

    def verify(self, task, result: "ExecutionResult") -> VerificationResult:
        """Default verification: trust the executor's own success flag. Subclasses
        (e.g. CLIWorker) override with objective checks. The dispatcher calls
        this after execute(); review runs later, only if verify passes."""
        return VerificationResult(passed=result.success,
                                  reason="no custom verify; success flag")


Worker = Executor  # backward-compat alias


class MockExecutor(Executor):
    """Deterministic, in-memory executor for tests and dogfooding.

    Honours a `fail` flag (set per-task via `task.runtime_hint`) so failure
    paths can be exercised without real side effects. Never touches the FS
    unless `task` asks for an artifact.
    """

    def __init__(self, worker_id: str = "worker:mock", fail: bool = False) -> None:
        self.worker_id = worker_id
        self._fail = fail

    def execute(self, task) -> ExecutionResult:
        should_fail = self._fail or getattr(task, "runtime_hint", "") == "fail"
        if should_fail:
            return ExecutionResult(
                success=False, stdout="", stderr="mock failure",
                exit_code=1, duration_ms=1, error="mock executor forced failure")
        return ExecutionResult(
            success=True,
            stdout=f"executed {task.task_id}",
            stderr="",
            artifacts=list(getattr(task, "artifacts", []) or []),
            exit_code=0,
            duration_ms=1,
        )


# backward-compat alias
MockWorker = MockExecutor


class PythonExecutor(Executor):
    """Executes a Python snippet/file described by the task, locally.

    Reads the snippet from `task.runtime_payload` (a string of Python source).
    Never imports or evaluates caller code paths — it runs in a subprocess with
    the snippet written to a temp file. The Runtime does not know this detail.
    """

    def __init__(self, worker_id: str = "worker:python") -> None:
        self.worker_id = worker_id

    def execute(self, task) -> ExecutionResult:
        payload = getattr(task, "runtime_payload", "") or ""
        if not payload.strip():
            return ExecutionResult(
                success=True, stdout="(no payload)", exit_code=0, duration_ms=0)
        t0 = time.monotonic()
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(payload)
            path = f.name
        try:
            proc = subprocess.run(
                ["python3", path], capture_output=True, text=True, timeout=60)
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=proc.stdout, stderr=proc.stderr,
                exit_code=proc.returncode, duration_ms=dur,
                error="" if proc.returncode == 0 else proc.stderr)
        except Exception as e:  # subprocess timeout / not found
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=False, stdout="", stderr=str(e),
                exit_code=None, duration_ms=dur, error=str(e))


# backward-compat alias
PythonWorker = PythonExecutor


class ShellExecutor(Executor):
    """Executes a shell command described by the task, locally."""

    def __init__(self, worker_id: str = "worker:shell") -> None:
        self.worker_id = worker_id

    def execute(self, task) -> ExecutionResult:
        payload = getattr(task, "runtime_payload", "") or ""
        if not payload.strip():
            return ExecutionResult(
                success=True, stdout="(no payload)", exit_code=0, duration_ms=0)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                payload, shell=True, capture_output=True, text=True, timeout=60)
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=proc.stdout, stderr=proc.stderr,
                exit_code=proc.returncode, duration_ms=dur,
                error="" if proc.returncode == 0 else proc.stderr)
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=False, stdout="", stderr=str(e),
                exit_code=None, duration_ms=dur, error=str(e))


# backward-compat alias
ShellWorker = ShellExecutor


# NOTE: ClaudeWorker / GeminiWorker / CodexWorker / FutureWorker are intentionally
# NOT defined here. They are external execution backends that would implement the
# same `Worker.execute(task) -> ExecutionResult` interface. The Runtime never
# references them by name — it receives a `Worker` and calls `execute`. Adapter
# discovery (mapping a registry `worker_id` to an adapter instance) is injected
# into the Runtime via a `worker_resolver` callable, keeping the core generic.


# ---------------------------------------------------------------------------
# RuntimeTask — the unit the Runtime hands to a Worker.
# ---------------------------------------------------------------------------

@dataclass
class RuntimeTask:
    """A scheduled task made executable. Carries the scheduler's structured
    fields plus optional execution hints the worker may read.

    The Runtime builds one of these from a `ScheduledTask`; it does not mutate
    the scheduler's data.
    """

    execution_id: str
    session_id: str
    schedule_id: str
    task_id: str
    worker_id: str
    wave: int
    dependencies: List[str] = field(default_factory=list)
    # Optional execution inputs (worker-specific); empty for pure mock runs.
    runtime_payload: str = ""
    artifacts: List[str] = field(default_factory=list)
    runtime_hint: str = ""      # e.g. "fail" to force a mock failure
    task_type: str = ""          # copied from the planning task for workers
    title: str = ""               # copied from the planning task for workers
    goal: str = ""                 # original user goal (copied from graph)
    # Phase 1.5 execution contract (carried from the planning Task so the
    # runtime verifies an explicit contract instead of guessing from prose).
    outputs: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    verification: List[dict] = field(default_factory=list)
    # Phase 3/4: symbolic engineering intent (op + target). The runtime
    # translates this into a concrete executor payload at execution time.
    symbolic: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "schedule_id": self.schedule_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "wave": self.wave,
            "dependencies": list(self.dependencies),
            "task_type": self.task_type,
            "title": self.title,
            "goal": self.goal,
            "outputs": list(self.outputs),
            "acceptance_criteria": list(self.acceptance_criteria),
            "verification": list(self.verification),
            "symbolic": dict(self.symbolic),
        }


# ---------------------------------------------------------------------------
# RuntimeEvent — append-only execution event.
# ---------------------------------------------------------------------------

@dataclass
class RuntimeEvent:
    event_id: str
    session_id: str
    kind: str               # session_started | task_started | task_finished |
                            # task_failed | session_finished
    task_id: str = ""
    worker_id: str = ""
    detail: str = ""
    at: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "detail": self.detail,
            "at": self.at,
        }


# ---------------------------------------------------------------------------
# ExecutionReport — the Runtime's sole output.
# ---------------------------------------------------------------------------

@dataclass
class ExecutionReport:
    """What `run()` returns. No analysis, no recommendations — just outcomes."""

    session_id: str
    schedule_id: str
    state: str
    started_at: str
    finished_at: str
    wave_count: int = 0
    duration_ms: int = 0
    verification_time_ms: int = 0
    stopped_at: Optional[str] = None     # task_id where a blocking failure ended
    stop_reason: Optional[str] = None    # truthful "why the mission stopped"
    executed: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0         # blocked descendants of a failure
    tasks: List[dict] = field(default_factory=list)
    workers_used: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "schedule_id": self.schedule_id,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wave_count": self.wave_count,
            "duration_ms": self.duration_ms,
            "verification_time_ms": self.verification_time_ms,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "executed": self.executed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "workers_used": list(self.workers_used),
            "artifacts": list(self.artifacts),
            "tasks": list(self.tasks),
            "schema_version": SCHEMA_VERSION,
        }
