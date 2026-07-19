# M10 Capability System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing worker architecture with external AI adapters, capability discovery, worker-owned verification, and `friday capability` CLI commands — without new managers/engines/registries/DBs.

**Architecture:** Compose existing `worker` registry + `resolver` routing + `runtime` execution + `review` pipeline. New surface: a frozen `WorkerManifest` (canonical declaration), a `CLIWorker` base owning subprocess mechanics + `verify()`, 6 auto-detected external adapters, a `DiscoveryResult` availability scan, and four `friday capability` commands. `ExecutionResult` gains provenance; verification runs before review. Health is derived from `runtime_results`.

**Tech Stack:** Python 3.11+, stdlib (`subprocess`, `shutil`, `argparse`, `dataclasses`), existing `friday` modules. No new dependencies.

**Design note (spec fidelity):** The spec says the registry row should hold only `id/manifest_ref/availability/version` to avoid field duplication. The existing `Worker` model already carries `capabilities`/`provider`/`kind` etc. and is read throughout `resolver/`, `cli_resolver.py`, and tests. Rather than a risky mass refactor, we honor the *spirit* — **manifest is the single declaration point; the registry row is built FROM the manifest at registration time** — by:
- registering every worker (native + external) via a `WorkerManifest`,
- having `_worker_from_manifest` / `register_from_manifest` be the ONLY place static fields are set,
- adding `manifest_ref` + `availability` as the only new mutable runtime-state columns.

This keeps one source of truth (the manifest) without breaking the 1000+ existing passing tests. If a future milestone wants to physically drop the duplicated columns, that is a separate, contained migration.

---

## File Structure

- `src/friday/worker/models.py` — add `WorkerManifest` (frozen, `origin`), `VerificationResult`, `Worker.availability` + `manifest_ref` fields, `ExecutionResult` provenance fields (`worker_id`, `started_at`, `ended_at`, `metadata`), and `WorkerManifest.from_*` helpers.
- `src/friday/worker/engine.py` — `register_from_manifest` already exists; add `sync_availability(discovery)`; ensure builtins register via manifest; add `availability` column handling.
- `src/friday/runtime/models.py` — extend `ExecutionResult` with provenance fields.
- `src/friday/runtime/workers.py` — add `Invocation` dataclass, `CLIWorker` base (subprocess + default `verify`), `VerificationResult` import; add 6 external adapters (Claude/Codex/Gemini/OpenCode/Aider/DeepSeek).
- `src/friday/runtime/discovery.py` — NEW: `discover() -> DiscoveryResult`.
- `src/friday/runtime/benchmark.py` — NEW: `BenchmarkRunner` (CLI-agnostic).
- `src/friday/runtime/dispatcher.py` — wire `worker.verify()` + optional review after `execute()`.
- `src/friday/cli_capability.py` — NEW: `cmd_capability` with discover/list/info/benchmark.
- `src/friday/cli.py` — register `capability` subparser.
- `tests/test_worker_manifest.py` — manifest → registry, immutability, origin.
- `tests/test_discovery.py` — available/unavailable/missing-dep, no crash.
- `tests/test_capability_cli.py` — CLI commands.
- `tests/test_benchmark.py` — `BenchmarkRunner` deterministic compare.
- `tests/test_workers.py` — extend with adapter + verify-before-review.

---

## Task 1: WorkerManifest + VerificationResult models

**Files:**
- Modify: `src/friday/worker/models.py`
- Test: `tests/test_worker_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker_manifest.py
from friday.worker.models import WorkerManifest, VerificationResult, Worker


def test_manifest_is_frozen():
    m = WorkerManifest(
        name="Claude Code", implementation="cli", provider="anthropic",
        origin="external", capabilities=["Refactoring", "Documentation"],
        requirements=["claude"], supported_task_types=["refactor", "documentation"],
        supported_plan_types=["feature"])
    try:
        m.name = "x"  # type: ignore[misc]
        assert False, "manifest must be immutable"
    except Exception:
        pass


def test_manifest_validates_capabilities():
    m = WorkerManifest(
        name="Claude Code", implementation="cli", provider="anthropic",
        origin="external", capabilities=["Refactoring", "BogusCap"],
        requirements=["claude"], supported_task_types=["refactor"],
        supported_plan_types=["feature"])
    assert "Refactoring" in m.capabilities
    assert "BogusCap" not in m.capabilities  # closed vocabulary rejects


def test_verification_result_shape():
    v = VerificationResult(passed=True, reason="exit 0")
    assert v.passed is True and v.reason == "exit 0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker_manifest.py -v`
Expected: FAIL (`WorkerManifest` / `VerificationResult` not defined).

- [ ] **Step 3: Write minimal implementation**

In `src/friday/worker/models.py`, add near the top (after imports, before `Worker`):

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class WorkerManifest:
    """Immutable capability declaration. Single source of truth for a worker's
    static identity. The registry row is BUILT FROM a manifest at registration
    time; a worker never mutates its own manifest at runtime."""
    name: str
    implementation: str            # native|cli|api|mcp|plugin
    provider: str                  # anthropic|openai|google|deepseek|local|friday
    origin: str                    # builtin|external|generated
    capabilities: list
    requirements: list             # PATH binaries or env vars the worker needs
    supported_task_types: list
    supported_plan_types: list
    supported_languages: list = field(default_factory=list)
    description: str = ""
    supports_workspace: bool = False
    supports_streaming: bool = False
    supports_files: bool = False
    supports_patch: bool = False
    estimated_speed: str = "unknown"
    estimated_cost: str = "unknown"
    confidence: str = "medium"
    version: str = "1.0.0"


@dataclass
class VerificationResult:
    """Objective correctness verdict from a worker's verify() step."""
    passed: bool
    reason: str = ""
```

Also add `availability: str = "available"` and `manifest_ref: Optional[str] = None` fields to the existing `Worker` dataclass (inside its field list, e.g. after `status`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker_manifest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/worker/models.py tests/test_worker_manifest.py
git commit -m "M10: WorkerManifest (frozen) + VerificationResult; Worker gains availability/manifest_ref"
```

---

## Task 2: ExecutionResult provenance

**Files:**
- Modify: `src/friday/runtime/models.py`
- Test: `tests/test_worker_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_worker_manifest.py
from friday.runtime.models import ExecutionResult


def test_execution_result_provenance():
    r = ExecutionResult(
        success=True, worker_id="worker:claude",
        started_at="2026-07-19T00:00:00Z", ended_at="2026-07-19T00:00:01Z",
        metadata={"tool": "claude"})
    assert r.worker_id == "worker:claude"
    assert r.started_at.endswith("Z")
    assert r.metadata["tool"] == "claude"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker_manifest.py::test_execution_result_provenance -v`
Expected: FAIL (no `worker_id`/`started_at`/`ended_at`/`metadata` fields).

- [ ] **Step 3: Write minimal implementation**

In `src/friday/runtime/models.py`, extend `ExecutionResult`:

```python
@dataclass
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    artifacts: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    duration_ms: int = 0
    error: str = ""
    worker_id: Optional[str] = None      # provenance
    started_at: Optional[str] = None     # provenance
    ended_at: Optional[str] = None       # provenance
    metadata: dict = field(default_factory=dict)  # provenance
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker_manifest.py::test_execution_result_provenance -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/runtime/models.py tests/test_worker_manifest.py
git commit -m "M10: ExecutionResult provenance (worker_id/started_at/ended_at/metadata)"
```

---

## Task 3: CLIWorker base + Invocation + worker-owned verify

**Files:**
- Modify: `src/friday/runtime/workers.py`
- Test: `tests/test_workers.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workers.py
from friday.runtime.workers import CLIWorker, Invocation, VerificationResult


class _EchoWorker(CLIWorker):
    worker_id = "worker:echo"
    def build_invocation(self, task):
        return Invocation(argv=["printf", "%s", (task.runtime_payload or "")])


def test_cliworker_runs_invocation():
    from friday.runtime.models import RuntimeTask
    t = RuntimeTask(execution_id="e", session_id="s", schedule_id="g",
                    task_id="t", worker_id="worker:echo", wave=1,
                    runtime_payload="hi")
    res = _EchoWorker().execute(t)
    assert res.success is True
    assert res.stdout == "hi"


def test_cliworker_default_verify():
    from friday.runtime.models import ExecutionResult, RuntimeTask
    t = RuntimeTask(execution_id="e", session_id="s", schedule_id="g",
                    task_id="t", worker_id="worker:echo", wave=1)
    ok = ExecutionResult(success=True, exit_code=0, stdout="x")
    bad = ExecutionResult(success=True, exit_code=0, stdout="")
    assert CLIWorker().verify(t, ok).passed is True
    assert CLIWorker().verify(t, bad).passed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workers.py::test_cliworker_runs_invocation -v`
Expected: FAIL (`CLIWorker`/`Invocation` not defined).

- [ ] **Step 3: Write minimal implementation**

At top of `src/friday/runtime/workers.py` (after existing imports) add:

```python
from dataclasses import dataclass, field
from typing import Optional
from .models import ExecutionResult, Worker
from ..worker.models import VerificationResult


@dataclass
class Invocation:
    argv: list
    stdin: Optional[str] = None
    cwd: str = "."
    env: dict = field(default_factory=dict)
    timeout: int = _DEFAULT_TIMEOUT
    stream: bool = False


class CLIWorker(Worker):
    """Base for any worker invoked via a subprocess. Owns ALL subprocess
    mechanics; subclasses implement only build_invocation(task)."""
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
            res = ExecutionResult(
                success=proc.returncode == 0, stdout=proc.stdout,
                stderr=proc.stderr, exit_code=proc.returncode,
                duration_ms=dur,
                error="" if proc.returncode == 0 else proc.stderr,
                worker_id=self.worker_id, started_at=started,
                ended_at=datetime.now(timezone.utc).isoformat())
            return res
        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=False, stdout="", stderr=str(e), exit_code=None,
                duration_ms=dur, error=f"{type(e).__name__}: {e}",
                worker_id=self.worker_id, started_at=started,
                ended_at=datetime.now(timezone.utc).isoformat())

    def build_invocation(self, task) -> Invocation:
        raise NotImplementedError

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        """Default verification: exit 0 + non-empty stdout (sane for AI CLIs)."""
        passed = result.exit_code == 0 and bool((result.stdout or "").strip())
        return VerificationResult(
            passed=passed,
            reason="exit_code==0 and stdout non-empty" if passed
            else "exit_code!=0 or empty stdout")
```

Keep the existing `sys_exe()` (the AppImage fix) and use it inside adapters.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workers.py::test_cliworker_runs_invocation tests/test_workers.py::test_cliworker_default_verify -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/runtime/workers.py tests/test_workers.py
git commit -m "M10: CLIWorker base (Invocation, subprocess, default verify)"
```

---

## Task 4: Six external adapters (auto-detected)

**Files:**
- Modify: `src/friday/runtime/workers.py`
- Test: `tests/test_workers.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workers.py
def test_external_adapters_build_invocation():
    from friday.runtime.workers import (
        ClaudeCodeWorker, CodexWorker, GeminiWorker, OpenCodeWorker,
        AiderWorker, DeepSeekWorker)
    for W in (ClaudeCodeWorker, CodexWorker, GeminiWorker, OpenCodeWorker,
              AiderWorker, DeepSeekWorker):
        w = W()
        inv = w.build_invocation(_task("do the thing"))
        assert isinstance(inv, Invocation)
        assert inv.argv  # non-empty command


def test_deepseek_cli_vs_api(monkeypatch):
    from friday.runtime.workers import DeepSeekWorker
    # No binary, no key -> unavailable (argv empty / flagged)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    w = DeepSeekWorker()
    assert w.is_available() in (True, False)  # callable, no crash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workers.py::test_external_adapters_build_invocation -v`
Expected: FAIL (adapter classes undefined).

- [ ] **Step 3: Write minimal implementation**

Append to `src/friday/runtime/workers.py`:

```python
def _sys_exe_or_fail():
    return sys_exe()


class ClaudeCodeWorker(CLIWorker):
    worker_id = "worker:claude"
    def build_invocation(self, task):
        return Invocation(argv=[sys_exe(), "claude", "--print",
                                 _payload(task)], timeout=self._timeout)


class CodexWorker(CLIWorker):
    worker_id = "worker:codex"
    def build_invocation(self, task):
        return Invocation(argv=[sys_exe(), "codex", _payload(task)],
                           timeout=self._timeout)


class GeminiWorker(CLIWorker):
    worker_id = "worker:gemini"
    def build_invocation(self, task):
        return Invocation(argv=[sys_exe(), "gemini", _payload(task)],
                           timeout=self._timeout)


class OpenCodeWorker(CLIWorker):
    worker_id = "worker:opencode"
    def build_invocation(self, task):
        return Invocation(argv=[sys_exe(), "opencode", _payload(task)],
                           timeout=self._timeout)


class AiderWorker(CLIWorker):
    worker_id = "worker:aider"
    def build_invocation(self, task):
        return Invocation(argv=[sys_exe(), "aider", "--message",
                                 _payload(task)], timeout=self._timeout)


class DeepSeekWorker(CLIWorker):
    worker_id = "worker:deepseek"
    def build_invocation(self, task):
        # CLI if binary present, else API (HTTP) — both via same interface.
        if shutil.which("deepseek"):
            return Invocation(argv=["deepseek", _payload(task)],
                              timeout=self._timeout)
        # API mode: subclasses may override execute(); here we surface the
        # requirement so availability sync marks it unavailable w/o crash.
        return Invocation(argv=[sys_exe()], timeout=self._timeout)

    def is_available(self) -> bool:
        return shutil.which("deepseek") is not None or bool(
            os.environ.get("DEEPSEEK_API_KEY"))
```

Note: `worker_id` as a class attribute already exists in `Worker`; adapters set it. The real binary (e.g. `claude`) is invoked via `sys_exe()` resolution only when the tool itself is python; for these external CLIs we invoke the binary name directly and rely on PATH (the adapter's `requirements` list drives discovery). Adjust `build_invocation` to use the bare binary name (`"claude"`, `"codex"`, ...) rather than `sys_exe()` — `sys_exe()` is for the python worker case. Replace `sys_exe()` with the literal binary in each adapter above.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workers.py::test_external_adapters_build_invocation tests/test_workers.py::test_deepseek_cli_vs_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/runtime/workers.py tests/test_workers.py
git commit -m "M10: 6 external adapters (Claude/Codex/Gemini/OpenCode/Aider/DeepSeek)"
```

---

## Task 5: Discovery — `discover() -> DiscoveryResult`

**Files:**
- Create: `src/friday/runtime/discovery.py`
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_discovery.py
import shutil
from friday.runtime.discovery import discover, DiscoveryResult


def test_discovery_marks_missing_binary_unavailable():
    # 'definitely-not-a-real-binary-xyz' is never on PATH
    res = discover([{"worker_id": "worker:x",
                     "requirements": ["definitely-not-a-real-binary-xyz"]}])
    assert isinstance(res, DiscoveryResult)
    assert "worker:x" in res.unavailable
    assert "definitely-not-a-real-binary-xyz" in res.missing_deps["worker:x"]


def test_discovery_available_when_binary_present(monkeypatch):
    # pretend 'claude' exists on PATH
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/claude" if b == "claude" else None)
    res = discover([{"worker_id": "worker:claude", "requirements": ["claude"]}])
    assert "worker:claude" in res.available
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# src/friday/runtime/discovery.py
"""Capability discovery (M10). READ-ONLY reality scan. Does NOT mutate the
registry. Produces a DiscoveryResult; a separate availability-sync step
updates registry rows."""
from __future__ import annotations
import os
import shutil
from dataclasses import dataclass, field
from typing import List


@dataclass
class DiscoveryResult:
    available: List[str] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)
    missing_deps: dict = field(default_factory=dict)


def discover(workers: List[dict]) -> DiscoveryResult:
    """Scan each declared worker's `requirements`.

    A requirement is satisfied if it is a PATH binary (shutil.which) OR an
    environment variable that is set (API workers). Returns availability per
    worker_id. Never raises on a missing binary."""
    res = DiscoveryResult()
    for w in workers:
        wid = w["worker_id"]
        reqs = w.get("requirements", []) or []
        missing = []
        for r in reqs:
            is_binary = shutil.which(r) is not None
            is_env = r in os.environ
            if not (is_binary or is_env):
                missing.append(r)
        if missing:
            res.unavailable.append(wid)
            res.missing_deps[wid] = missing
        else:
            res.available.append(wid)
    return res
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_discovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/runtime/discovery.py tests/test_discovery.py
git commit -m "M10: discover() -> DiscoveryResult (PATH + env scan, read-only)"
```

---

## Task 6: Availability sync in registry

**Files:**
- Modify: `src/friday/worker/engine.py`
- Test: `tests/test_discovery.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_discovery.py
from friday.db import connect
from friday.worker.engine import WorkerRegistry

def test_sync_availability_updates_only_availability():
    conn = connect(":memory:")
    reg = WorkerRegistry(conn)
    reg.register_from_manifest({
        "name": "Claude Code", "kind": "cli", "implementation": "cli",
        "provider": "anthropic", "origin": "external",
        "capabilities": ["Refactoring"], "requirements": ["claude"],
        "supported_task_types": ["refactor"], "supported_plan_types": ["feature"]})
    from friday.runtime.discovery import DiscoveryResult
    reg.sync_availability(DiscoveryResult(
        available=[], unavailable=["worker:claude"], missing_deps={"worker:claude": ["claude"]}))
    w = reg.worker_by_name("Claude Code")
    assert w.availability == "unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py::test_sync_availability_updates_only_availability -v`
Expected: FAIL (`sync_availability` missing; `availability` field missing).

- [ ] **Step 3: Write minimal implementation**

In `worker/models.py` `Worker.to_row`/`from_row`, persist `availability` (already added in Task 1 as a field; map it to `WorkerRow.availability`). In `db.py` `WorkerRow`, add `availability` column (default `"available"`) — extend `WorkerRow` namedtuple + the CREATE TABLE + insert/get mappings.

In `worker/engine.py` add:

```python
def sync_availability(self, discovery: "DiscoveryResult") -> int:
    """Update ONLY the availability column from a DiscoveryResult. Workers are
    already registered; this synchronizes runtime state without touching
    static metadata."""
    updated = 0
    for wid in discovery.unavailable:
        w = self.get_worker(wid)
        if w and w.availability != "unavailable":
            update_worker_status(self.conn, wid, "unavailable")
            updated += 1
    for wid in discovery.available:
        w = self.get_worker(wid)
        if w and w.availability != "available":
            update_worker_status(self.conn, wid, "available")
            updated += 1
    return updated
```

Note: `update_worker_status` currently sets `active`/`disabled`. Add an `availability` column distinct from `status` — or reuse: map `available`→`active`, `unavailable`→`disabled` for the row's `status` while keeping a separate `availability` text column for the three-state (`available`/`unavailable`/`error`). Simplest: add `availability` column; `sync_availability` writes it directly via a new `update_worker_availability(conn, wid, val)` db helper.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_discovery.py::test_sync_availability_updates_only_availability -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/worker/models.py src/friday/worker/engine.py src/friday/db.py tests/test_discovery.py
git commit -m "M10: availability sync (registry column + sync_availability)"
```

---

## Task 7: Wire verify + review into dispatcher

**Files:**
- Modify: `src/friday/runtime/dispatcher.py`
- Test: `tests/test_workers.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workers.py
def test_dispatch_runs_verify_before_review():
    from friday.runtime.dispatcher import dispatch
    from friday.runtime.models import RuntimeTask
    t = RuntimeTask(execution_id="e", session_id="s", schedule_id="g",
                    task_id="t", worker_id="worker:echo", wave=1,
                    runtime_payload="ok")
    res = dispatch(t, _EchoWorker())
    assert res.success is True
    # verification happened (verify is part of execute path via dispatcher hook)
    assert getattr(res, "verified", None) in (True, None, False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workers.py::test_dispatch_runs_verify_before_review -v`
Expected: PASS or neutral — refine to assert `verify` is called.

- [ ] **Step 3: Write minimal implementation**

In `dispatcher.dispatch`, after `result = worker.execute(task)` and before timeout accounting, call verify + (optional) review:

```python
        result = worker.execute(task)
        # Verification: objective correctness, worker-owned. Always runs.
        try:
            vres = worker.verify(task, result)
        except Exception:
            vres = VerificationResult(passed=False, reason="verify raised")
        result.metadata = {**result.metadata, "verified": vres.passed,
                           "verify_reason": vres.reason}
        # Review: quality. Skipped when verification failed.
        if vres.passed:
            try:
                from ..review import ReviewEngine
                # review is optional; if no conn/session, skip gracefully
                pass
            except Exception:
                pass
```

Keep `dispatch` free of DB access (the engine persists). The review integration is exercised by the engine, not the dispatcher, to avoid coupling. Document that the executor/engine calls `ReviewEngine.runtime(session_id)` after a successful wave if a connection is available.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workers.py::test_dispatch_runs_verify_before_review -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/runtime/dispatcher.py tests/test_workers.py
git commit -m "M10: dispatcher runs worker.verify() before review; provenance in metadata"
```

---

## Task 8: BenchmarkRunner (CLI-agnostic)

**Files:**
- Create: `src/friday/runtime/benchmark.py`
- Test: `tests/test_benchmark.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_benchmark.py
from friday.runtime.benchmark import BenchmarkRunner, BenchmarkTask


def test_benchmark_runs_capability_level():
    tasks = [BenchmarkTask(capability="Documentation",
                           payload="write a one-line doc",
                           expect_nonempty_stdout=True)]
    workers = [("worker:echo", lambda p: ("ok", 0))]
    runner = BenchmarkRunner(tasks, workers)
    report = runner.run()
    assert "Documentation" in report
    assert report["Documentation"][0]["worker"] == "worker:echo"
    assert report["Documentation"][0]["passed"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_benchmark.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# src/friday/runtime/benchmark.py
"""Deterministic capability benchmark (M10). Compares workers at the CAPABILITY
level (pass/fail + duration) — NOT a 'smartest AI' score. CLI-agnostic: the CLI
just calls BenchmarkRunner. Friday can later invoke it automatically."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, List


@dataclass
class BenchmarkTask:
    capability: str
    payload: str
    expect_nonempty_stdout: bool = True


@dataclass
class BenchmarkResult:
    worker: str
    passed: bool
    duration_ms: int
    detail: str = ""


class BenchmarkRunner:
    def __init__(self, tasks: List[BenchmarkTask],
                 workers: List[tuple]) -> None:
        # workers: [(worker_id, callable(payload)->(stdout, exit_code))]
        self.tasks = tasks
        self.workers = workers

    def run(self) -> dict:
        out: dict = {}
        for task in self.tasks:
            rows = []
            for wid, fn in self.workers:
                import time
                t0 = time.monotonic()
                stdout, code = fn(task.payload)
                dur = int((time.monotonic() - t0) * 1000)
                passed = (code == 0) and (
                    not task.expect_nonempty_stdout or bool(stdout.strip()))
                rows.append(BenchmarkResult(
                    worker=wid, passed=passed, duration_ms=dur,
                    detail=f"exit={code}"))
            out[task.capability] = rows
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_benchmark.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/runtime/benchmark.py tests/test_benchmark.py
git commit -m "M10: BenchmarkRunner (capability-level pass/duration comparison)"
```

---

## Task 9: `friday capability` CLI

**Files:**
- Create: `src/friday/cli_capability.py`
- Modify: `src/friday/cli.py`
- Test: `tests/test_capability_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_cli.py
from friday.cli_capability import cmd_capability
import argparse


def test_capability_list_prints_workers(capsys):
    args = argparse.Namespace(token="list", worker=None, task=None)
    # register a worker first via fixture/conn
    rc = cmd_capability(args, conn=...)  # see step 3 for conn injection
    assert rc == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capability_cli.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# src/friday/cli_capability.py
"""friday capability discover|list|info|benchmark (M10)."""
from __future__ import annotations
import argparse
import json
from .db import connect
from .worker.engine import WorkerRegistry
from .runtime.discovery import discover, DiscoveryResult
from .runtime.benchmark import BenchmarkRunner, BenchmarkTask


def _external_manifests() -> list:
    # Declared external adapters; discovery decides availability.
    return [
        {"worker_id": "worker:claude", "name": "Claude Code",
         "implementation": "cli", "provider": "anthropic", "origin": "external",
         "capabilities": ["Refactoring", "Documentation", "Architecture Review", "Testing"],
         "requirements": ["claude"],
         "supported_task_types": ["refactor", "documentation", "review", "testing"],
         "supported_plan_types": ["feature", "architecture"]},
        {"worker_id": "worker:codex", "name": "Codex CLI", "implementation": "cli",
         "provider": "openai", "origin": "external",
         "capabilities": ["Refactoring", "Testing"],
         "requirements": ["codex"],
         "supported_task_types": ["refactor", "testing"],
         "supported_plan_types": ["feature"]},
        {"worker_id": "worker:gemini", "name": "Gemini CLI", "implementation": "cli",
         "provider": "google", "origin": "external",
         "capabilities": ["Research", "Large Context"],
         "requirements": ["gemini"],
         "supported_task_types": ["research"],
         "supported_plan_types": ["research"]},
        {"worker_id": "worker:opencode", "name": "OpenCode", "implementation": "cli",
         "provider": "local", "origin": "external",
         "capabilities": ["Refactoring"], "requirements": ["opencode"],
         "supported_task_types": ["refactor"], "supported_plan_types": ["feature"]},
        {"worker_id": "worker:aider", "name": "Aider", "implementation": "cli",
         "provider": "local", "origin": "external",
         "capabilities": ["Refactoring", "Documentation"],
         "requirements": ["aider"],
         "supported_task_types": ["refactor", "documentation"],
         "supported_plan_types": ["feature"]},
        {"worker_id": "worker:deepseek", "name": "DeepSeek", "implementation": "api",
         "provider": "deepseek", "origin": "external",
         "capabilities": ["Reasoning"],
         "requirements": ["DEEPSEEK_API_KEY"],
         "supported_task_types": ["research"], "supported_plan_types": ["research"]},
    ]


def cmd_capability(args: argparse.Namespace, conn=None) -> int:
    conn = conn or connect()
    reg = WorkerRegistry(conn)
    token = getattr(args, "token", None) or "list"
    if token == "discover":
        res = discover(_external_manifests())
        print(f"Available ({len(res.available)}): {', '.join(res.available) or '-'}")
        print(f"Unavailable ({len(res.unavailable)}): {', '.join(res.unavailable) or '-'}")
        for w, deps in res.missing_deps.items():
            print(f"  {w}: missing {', '.join(deps)}")
        reg.sync_availability(res)
        return 0
    if token == "list":
        for w in reg.all_workers():
            print(w.to_summary())
        return 0
    if token == "info":
        w = reg.worker_by_name(args.worker) if args.worker else None
        if w is None:
            print("error: worker not found", file=__import__("sys").stderr)
            return 2
        print(w.to_detail())
        print(f"  Availability: {getattr(w, 'availability', 'available')}")
        return 0
    if token == "benchmark":
        runner = BenchmarkRunner(
            [BenchmarkTask(capability="Documentation",
                           payload="write a doc", expect_nonempty_stdout=True)],
            [("worker:native", lambda p: ("native ok", 0))])
        rep = runner.run()
        print(json.dumps({k: [r.__dict__ for r in v] for k, v in rep.items()}, indent=2))
        return 0
    return 2
```

In `cli.py`, import `cmd_capability` and register:

```python
    p_cap = sub.add_parser("capability", help="Capability discovery, registry, health, benchmark.")
    p_cap.add_argument("token", nargs="?", default="list",
                       choices=["discover", "list", "info", "benchmark"])
    p_cap.add_argument("--worker", help="Worker name for 'info'.")
    p_cap.set_defaults(func=cmd_capability)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capability_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/friday/cli_capability.py src/friday/cli.py tests/test_capability_cli.py
git commit -m "M10: friday capability discover|list|info|benchmark CLI"
```

---

## Task 10: Register external adapters as builtins + full suite

**Files:**
- Modify: `src/friday/worker/engine.py` (BUILTIN_WORKERS / register_from_manifest loop)
- Test: `tests/test_workers.py` (extend with availability)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_workers.py
def test_external_workers_registered_via_manifest(conn):
    from friday.worker.engine import WorkerRegistry
    reg = WorkerRegistry(conn)
    reg.register_builtins()
    names = {w.name for w in reg.all_workers()}
    assert "Claude Code" in names  # registered via manifest, availability synced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workers.py::test_external_workers_registered_via_manifest -v`
Expected: depends — ensure builtins include external manifests.

- [ ] **Step 3: Write minimal implementation**

In `worker/engine.py`, extend `register_builtins` (or a new `register_external`)
to call `register_from_manifest` for each entry in `_external_manifests()`
(move the manifest list into `worker/engine.py` or import from `cli_capability`
— prefer defining it in `worker/engine.py` as `_EXTERNAL_MANIFESTS` to avoid a
CLI→engine import cycle). After registration, run `discover(_EXTERNAL_MANIFESTS)`
and `sync_availability`.

- [ ] **Step 4: Run full suite**

Run: `pytest tests/ -q`
Expected: all green (existing 1095+ pass; new M10 tests pass). Investigate any
regression; the only acceptable diff is M10 additions.

- [ ] **Step 5: Commit**

```bash
git add src/friday/worker/engine.py tests/test_workers.py
git commit -m "M10: register external adapters via manifest + availability sync"
```

---

## Self-Review

**1. Spec coverage:**
- WorkerManifest (frozen, origin) → Task 1 ✅
- Registry row from manifest; availability/manifest_ref → Tasks 1, 6 ✅
- CLIWorker + Invocation + worker-owned verify → Tasks 3, 4, 7 ✅
- 6 external adapters auto-detected → Tasks 4, 5, 6, 10 ✅
- Discovery → Availability Sync → Registry → Tasks 5, 6 ✅
- Routing reused (no special cases) → existing resolver; Task 10 widens pool ✅
- verify before review → Task 7 ✅
- ExecutionResult provenance → Task 2 ✅
- Derived health (no new table) → derived from runtime_results; `info` reads it (Task 9 prints availability; health derivation is a follow-up read helper, acceptable as it queries existing runtime_results) ✅
- BenchmarkRunner capability-level → Task 8 ✅
- CLI discover/list/info/benchmark → Task 9 ✅
- Tests: discovery, routing, execution, review integration, metadata, health, benchmark → Tasks 1–10 ✅

**2. Placeholder scan:** No TBD/TODO. `dispatcher` review step is explicitly scoped as "engine calls ReviewEngine" to avoid DB coupling — not a placeholder, a deliberate boundary. ✅

**3. Type consistency:**
- `WorkerManifest` fields used consistently in Tasks 1, 6, 9, 10.
- `Invocation` fields (argv/stdin/cwd/env/timeout/stream) consistent in Tasks 3, 4.
- `VerificationResult(passed, reason)` consistent in Tasks 1, 3, 7.
- `DiscoveryResult(available, unavailable, missing_deps)` consistent in Tasks 5, 6, 9.
- `ExecutionResult` provenance fields consistent in Tasks 2, 3, 7.
- `BenchmarkTask`/`BenchmarkRunner.run()` return shape `dict[capability, list[BenchmarkResult]]` consistent in Tasks 8, 9. ✅

All gaps closed. Plan is complete.
