"""Named Protocols — user-defined, reusable multi-step macro procedures.

A **Named Protocol** is a parameterized sequence of steps that can be
invoked by name. Unlike auto-derived skills (which are formed from observed
workflow patterns), protocols are **explicitly authored** by the operator.

Example::

    # Create a "deploy" protocol
    friday protocol create deploy \\
        --description "Build, test, and deploy the current project" \\
        --step "shell_exec:worker:shell:{\"command\":\"pytest {project}\"}" \\
        --step "shell_exec:worker:shell:{\"command\":\"docker build -t {project} .\"}" \\
        --step "shell_exec:worker:shell:{\"command\":\"docker push {project}:latest\"}"

    # Run it
    friday protocol run deploy project=myapp

Architecture
------------
- ``Protocol`` dataclass — name, description, steps, variables
- ``ProtocolEngine`` — CRUD + execution through the existing executor pipeline
- ``named_protocols`` DB table
- Steps use ``{variable}`` placeholders resolved at runtime
- Execution reuses ``resolve_executor()`` and ``dispatch()`` from the runtime layer
"""

from __future__ import annotations

import json
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProtocolStep:
    """One step in a named protocol.

    Attributes:
        name:        Human-readable step name (e.g. "run tests").
        action_type: The action type for executor selection
                     (e.g. "shell_exec", "filesystem", "skill_execute").
        worker_id:   The worker to dispatch (e.g. "worker:shell").
        payload_template: JSON string with ``{variable}`` placeholders.
        timeout:     Max seconds for this step (default 120).
    """
    name: str
    action_type: str = "shell_exec"
    worker_id: str = "worker:shell"
    payload_template: str = "{}"
    timeout: int = 120


@dataclass
class Protocol:
    """A named, replayable multi-step macro procedure.

    Attributes:
        id:          DB auto-increment ID (0 when not yet persisted).
        name:        Unique short name used to invoke the protocol.
        description: Human-readable explanation of what the protocol does.
        steps:       Ordered list of ProtocolStep.
        variables:   Parameter names extracted from step payload templates.
        created_at:  ISO timestamp of creation.
        updated_at:  ISO timestamp of last update.
    """
    id: int = 0
    name: str = ""
    description: str = ""
    steps: list[ProtocolStep] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_PROTOCOL_TABLE = """
    CREATE TABLE IF NOT EXISTS named_protocols (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL UNIQUE,
        description     TEXT NOT NULL DEFAULT '',
        steps_json      TEXT NOT NULL DEFAULT '[]',
        variables_json  TEXT NOT NULL DEFAULT '[]',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
"""


def _ensure_table(conn) -> None:
    """Create the named_protocols table if it doesn't exist."""
    try:
        conn.executescript(_PROTOCOL_TABLE)
        conn.commit()
    except Exception:
        conn.rollback()


def _step_to_dict(s: ProtocolStep) -> dict:
    return {
        "name": s.name,
        "action_type": s.action_type,
        "worker_id": s.worker_id,
        "payload_template": s.payload_template,
        "timeout": s.timeout,
    }


def _step_from_dict(d: dict) -> ProtocolStep:
    return ProtocolStep(
        name=d.get("name", ""),
        action_type=d.get("action_type", "shell_exec"),
        worker_id=d.get("worker_id", "worker:shell"),
        payload_template=d.get("payload_template", "{}"),
        timeout=d.get("timeout", 120),
    )


# ---------------------------------------------------------------------------
# Variable extraction
# ---------------------------------------------------------------------------

_VARIABLE_PATTERN = re.compile(r"\{(\w+)\}")


def _extract_variables(text: str) -> list[str]:
    """Extract unique ``{variable}`` names from a template string."""
    return list(dict.fromkeys(_VARIABLE_PATTERN.findall(text)))


def _resolve_template(template: str, variables: dict[str, str]) -> str:
    """Replace ``{variable}`` placeholders with values from the dict.

    Unresolved placeholders are left as-is so the caller can detect them.
    """
    def _replacer(m: re.Match) -> str:
        key = m.group(1)
        return variables.get(key, m.group(0))
    return _VARIABLE_PATTERN.sub(_replacer, template)


# ---------------------------------------------------------------------------
# ProtocolEngine — CRUD + execution
# ---------------------------------------------------------------------------


class ProtocolEngine:
    """Create, read, update, delete, and execute named protocols.

    Usage::

        eng = ProtocolEngine(conn)
        proto = eng.create("deploy", "Run tests and deploy", steps=[
            ProtocolStep(name="test", payload_template='{"command":"pytest"}'),
        ])
        eng.run("deploy", {"project": "myapp"})
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        _ensure_table(conn)

    # ── CRUD ──────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        description: str = "",
        steps: Optional[list[ProtocolStep]] = None,
    ) -> Protocol:
        """Create a new named protocol.

        Args:
            name:        Unique protocol name.
            description: Optional explanation.
            steps:       Ordered list of ProtocolStep.

        Returns:
            The persisted Protocol (with id set).

        Raises:
            ValueError: If a protocol with this name already exists.
        """
        steps = steps or []
        now = now_iso()

        # Extract variables from all step templates.
        all_templates = " ".join(s.payload_template for s in steps)
        variables = _extract_variables(all_templates)

        steps_json = json.dumps([_step_to_dict(s) for s in steps])
        variables_json = json.dumps(variables)

        try:
            self._conn.execute(
                """INSERT INTO named_protocols
                   (name, description, steps_json, variables_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, description, steps_json, variables_json, now, now),
            )
            self._conn.commit()
            proto_id = self._conn.execute(
                "SELECT id FROM named_protocols WHERE name = ?", (name,)
            ).fetchone()["id"]
        except Exception as exc:
            self._conn.rollback()
            if "UNIQUE" in str(exc):
                raise ValueError(f"Protocol '{name}' already exists") from exc
            raise

        return Protocol(
            id=proto_id,
            name=name,
            description=description,
            steps=steps,
            variables=variables,
            created_at=now,
            updated_at=now,
        )

    def get(self, name: str) -> Optional[Protocol]:
        """Look up a protocol by name. Returns None if not found."""
        row = self._conn.execute(
            "SELECT * FROM named_protocols WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_protocol(row)

    def list_all(self) -> list[Protocol]:
        """Return all protocols, ordered by name."""
        rows = self._conn.execute(
            "SELECT * FROM named_protocols ORDER BY name"
        ).fetchall()
        return [self._row_to_protocol(r) for r in rows]

    def delete(self, name: str) -> bool:
        """Delete a protocol by name. Returns True if deleted, False if not found."""
        row = self._conn.execute(
            "SELECT id FROM named_protocols WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM named_protocols WHERE id = ?", (row["id"],))
        self._conn.commit()
        return True

    def _row_to_protocol(self, row) -> Protocol:
        steps_raw = row["steps_json"] or "[]"
        variables_raw = row["variables_json"] or "[]"
        try:
            steps_data = json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
        except (json.JSONDecodeError, TypeError):
            steps_data = []
        try:
            variables_data = json.loads(variables_raw) if isinstance(variables_raw, str) else variables_raw
        except (json.JSONDecodeError, TypeError):
            variables_data = []

        return Protocol(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            steps=[_step_from_dict(s) for s in steps_data],
            variables=list(variables_data) if isinstance(variables_data, (list, tuple)) else [],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Execution ─────────────────────────────────────────────────────

    def run(
        self,
        name: str,
        variables: Optional[dict[str, str]] = None,
        on_failure: str = "abort",
    ) -> dict[str, Any]:
        """Execute a named protocol step by step.

        Args:
            name:       The protocol name to run.
            variables:  Dict of ``{variable: value}`` to substitute into
                        step payload templates.
            on_failure: Step failure strategy:
                        - ``"abort"`` — stop on first failure
                        - ``"skip"`` — log failure and continue

        Returns:
            A result dict with keys:
              - ``success``: bool
              - ``protocol``: str (name)
              - ``steps``: list of per-step result dicts
              - ``total_duration_ms``: int
              - ``error``: str (if overall failure)

        Raises:
            ValueError: If the protocol is not found.
        """
        proto = self.get(name)
        if proto is None:
            raise ValueError(f"Protocol '{name}' not found")

        variables = variables or {}
        t0 = _time.monotonic()

        step_results: list[dict] = []
        overall_success = True

        for step in proto.steps:
            # Resolve variable placeholders in the payload template.
            resolved_payload = _resolve_template(step.payload_template, variables)

            # Check for unresolved placeholders.
            unresolved = _VARIABLE_PATTERN.findall(resolved_payload)
            if unresolved:
                step_results.append({
                    "step": step.name,
                    "success": False,
                    "error": f"Unresolved variables: {', '.join(unresolved)}. "
                             f"Provide values: {' '.join(f'{v}=<value>' for v in unresolved)}",
                    "duration_ms": 0,
                })
                if on_failure == "abort":
                    overall_success = False
                    break
                continue

            # Dispatch the step through the executor pipeline.
            step_t0 = _time.monotonic()
            try:
                from .runtime.executors import resolve_executor
                from .runtime.dispatcher import dispatch
                from .runtime.models import Executor

                worker = resolve_executor(step.worker_id)
                if worker is None:
                    step_results.append({
                        "step": step.name,
                        "success": False,
                        "error": f"No executor available for '{step.worker_id}'",
                        "duration_ms": int((_time.monotonic() - step_t0) * 1000),
                    })
                    if on_failure == "abort":
                        overall_success = False
                        break
                    continue

                # Build a minimal task for dispatch.
                task = _MiniTask(
                    task_id=f"{name}:{step.name}",
                    worker_id=step.worker_id,
                    runtime_payload=resolved_payload,
                    execution_id=f"proto:{name}",
                    title=step.name,
                    task_type=step.action_type,
                    timeout=step.timeout,
                    dependencies=[],
                    outputs=[],
                    acceptance_criteria=[],
                    verification=[],
                    symbolic={},
                    dependency_summaries={},
                    goal=proto.description,
                    session_id="",
                    schedule_id=f"proto:{name}",
                    wave=1,
                )

                result = dispatch(task, worker)
                step_dur = int((_time.monotonic() - step_t0) * 1000)
                step_results.append({
                    "step": step.name,
                    "success": result.success,
                    "stdout": (result.stdout or "")[:500],
                    "stderr": (result.stderr or "")[:500],
                    "error": result.error or "",
                    "exit_code": result.exit_code,
                    "duration_ms": step_dur,
                })

                if not result.success and on_failure == "abort":
                    overall_success = False
                    break

            except Exception as exc:
                step_dur = int((_time.monotonic() - step_t0) * 1000)
                step_results.append({
                    "step": step.name,
                    "success": False,
                    "error": str(exc)[:500],
                    "duration_ms": step_dur,
                })
                if on_failure == "abort":
                    overall_success = False
                    break

        total_dur = int((_time.monotonic() - t0) * 1000)

        failed = [s for s in step_results if not s["success"]]
        error_msg = ""
        if not overall_success and failed:
            error_msg = failed[0].get("error", "Unknown failure")

        # Log the execution to the action log.
        try:
            from .action_log import ActionEvent, log_action
            log_action(self._conn, ActionEvent(
                source="friday",
                action_type="protocol_run",
                target=json.dumps({
                    "protocol": name,
                    "steps_total": len(proto.steps),
                    "steps_succeeded": len([s for s in step_results if s.get("success")]),
                    "steps_failed": len(failed),
                }),
                detail=json.dumps({
                    "steps": step_results,
                    "variables": variables,
                    "on_failure": on_failure,
                }),
                confidence="observed",
                observed_at=now_iso(),
            ))
        except Exception:
            pass

        return {
            "success": overall_success,
            "protocol": name,
            "steps": step_results,
            "total_duration_ms": total_dur,
            "error": error_msg,
        }


# ---------------------------------------------------------------------------
# Mini-task (reused from autonomous_planner's pattern)
# ---------------------------------------------------------------------------

from collections import namedtuple

_MiniTask = namedtuple("_MiniTask", [
    "task_id", "worker_id", "runtime_payload", "execution_id",
    "title", "task_type", "timeout", "dependencies", "outputs",
    "acceptance_criteria", "verification", "symbolic",
    "dependency_summaries", "goal", "session_id", "schedule_id", "wave",
])


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_protocol(proto: Protocol, verbose: bool = False) -> str:
    """Render a protocol as human-readable text."""
    lines = [
        f"Protocol: {proto.name}",
        f"  Description: {proto.description or '(none)'}",
        f"  Variables: {', '.join(proto.variables) if proto.variables else '(none)'}",
        f"  Steps: {len(proto.steps)}",
    ]
    if verbose:
        for i, step in enumerate(proto.steps, 1):
            lines.append(f"    {i}. {step.name}")
            lines.append(f"       Worker: {step.worker_id}")
            lines.append(f"       Payload: {step.payload_template[:120]}")
            lines.append(f"       Timeout: {step.timeout}s")
    return "\n".join(lines)


def format_protocols(protos: list[Protocol]) -> str:
    """Render a list of protocols as a table."""
    if not protos:
        return "  No protocols defined yet.\n  Use: friday protocol create <name> ..."
    lines = [f"{'Name':<20} {'Steps':<6} {'Variables':<20} {'Description'}", "-" * 80]
    for p in protos:
        vars_str = ", ".join(p.variables) if p.variables else "-"
        desc = p.description[:40] if p.description else ""
        lines.append(f"{p.name:<20} {len(p.steps):<6} {vars_str:<20} {desc}")
    return "\n".join(lines)
