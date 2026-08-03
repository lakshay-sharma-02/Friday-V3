"""Execution Layer — gated, sandboxed, audited actions (Wave 9).

The moment V4 reaches — *actually doing things*, safely. Every action
flows through the same pipeline:

    classify (gate) → record (audit) → confirm? → sandbox.run → finish (audit)

Three permission levels (``execution/gate.py``):

    AUTO     — read-only (status, diff): execute silently
    CONFIRM  — state-changing (writes, test runs): require y/n
    NEVER    — prod/deploy/push: operator override only (force=True)

The sandbox (``execution/sandbox.py``) enforces path allowlists,
timeouts, and secret-free child environments. The audit trail
(``execution/audit.py``) records every attempt — including denials —
into the V4 ``actions`` table. Reversible actions carry undo payloads
(``execution/undo.py``) so Friday can take back what it did.

**Status:** Wave 9 — built (2026-08). Pure stdlib; hermetic tests. The
imports below stay guarded so importing this package never crashes the
rest of Friday V4.

Usage:
    from friday_v4.execution import execute
    result = execute("testing", "tests/", cwd=Path("."), conn=conn,
                     confirm_fn=confirm)
    if result.ok:
        print(result.output)
"""

from __future__ import annotations

try:
    from .audit import AuditLogger
    from .executors import (
        BaseExecutor,
        ClaudeCodeExecutor,
        ExecutionResult,
        FileExecutor,
        GitExecutor,
        PythonExecutor,
        ShellExecutor,
        TestingExecutor,
        execute,
        register_executor,
    )
    from .gate import ConfirmFn, PermissionGate, PermissionLevel
    from .sandbox import Sandbox, SandboxResult, SandboxViolation
    from .undo import UndoManager, UndoResult
    _EXECUTION_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    AuditLogger = None  # type: ignore
    BaseExecutor = None  # type: ignore
    ClaudeCodeExecutor = None  # type: ignore
    ExecutionResult = None  # type: ignore
    FileExecutor = None  # type: ignore
    GitExecutor = None  # type: ignore
    PythonExecutor = None  # type: ignore
    ShellExecutor = None  # type: ignore
    TestingExecutor = None  # type: ignore
    execute = None  # type: ignore
    register_executor = None  # type: ignore
    ConfirmFn = None  # type: ignore
    PermissionGate = None  # type: ignore
    PermissionLevel = None  # type: ignore
    Sandbox = None  # type: ignore
    SandboxResult = None  # type: ignore
    SandboxViolation = None  # type: ignore
    UndoManager = None  # type: ignore
    UndoResult = None  # type: ignore
    _EXECUTION_AVAILABLE = False


def is_available() -> bool:
    """Whether the execution layer is implemented yet."""
    return _EXECUTION_AVAILABLE


__all__ = [
    "AuditLogger",
    "BaseExecutor",
    "ClaudeCodeExecutor",
    "ExecutionResult",
    "FileExecutor",
    "GitExecutor",
    "PythonExecutor",
    "ShellExecutor",
    "TestingExecutor",
    "execute",
    "register_executor",
    "ConfirmFn",
    "PermissionGate",
    "PermissionLevel",
    "Sandbox",
    "SandboxResult",
    "SandboxViolation",
    "UndoManager",
    "UndoResult",
    "is_available",
    "_EXECUTION_AVAILABLE",
]
