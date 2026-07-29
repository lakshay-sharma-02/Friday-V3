"""Remote Guidance Mode — step-by-step guided walkthroughs.

Friday can lead the operator through a procedure one step at a time,
waiting for confirmation before proceeding. Works across all channels:
CLI, Telegram, Slack, Discord.

Usage::

    from friday.guide import GuideEngine, create_guide, advance_guide

    # Create a guide from a protocol or from scratch.
    session = create_guide(conn, "Deploy emergency fix", [
        {"instruction": "Run `git pull`", "verification": "git status",
         "timeout_seconds": 120},
        {"instruction": "Run tests", "verification": "pytest -q",
         "timeout_seconds": 300},
    ])

    # Advance to the next step.
    session = advance_guide(conn, session.id, "done")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class GuideStep:
    """One step in a guided walkthrough."""

    index: int
    instruction: str
    verification: str = ""
    timeout_seconds: int = 120
    help_text: str = ""
    completed: bool = False
    failed: bool = False
    started_at: str = ""
    completed_at: str = ""
    output: str = ""
    error: str = ""


@dataclass
class GuideSession:
    """A persisted guided walkthrough session."""

    id: str
    title: str
    current_step: int
    total_steps: int
    status: str  # "running" | "paused" | "completed" | "aborted"
    channel: str
    steps: list[GuideStep]
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Guide Engine
# ---------------------------------------------------------------------------


def _make_id() -> str:
    """Generate a short unique session ID."""
    import hashlib, os
    raw = os.urandom(8)
    return "guide_" + hashlib.sha256(raw).hexdigest()[:12]


def create_guide(
    conn,
    title: str,
    steps: list[dict],
    channel: str = "cli",
) -> GuideSession:
    """Create a new guided walkthrough session.

    Args:
        conn: Database connection.
        title: Human-readable title (e.g. "Deploy emergency fix").
        steps: List of dicts with keys:
            instruction (str, required),
            verification (str, optional — shell command to verify),
            timeout_seconds (int, optional — default 120),
            help_text (str, optional — detailed explanation).
        channel: Delivery channel ("cli", "telegram", "slack", "discord").

    Returns:
        The created ``GuideSession``.
    """
    session_id = _make_id()
    now = now_iso()

    guide_steps: list[GuideStep] = []
    for i, s in enumerate(steps):
        guide_steps.append(GuideStep(
            index=i,
            instruction=s.get("instruction", ""),
            verification=s.get("verification", ""),
            timeout_seconds=s.get("timeout_seconds", 120),
            help_text=s.get("help_text", ""),
        ))

    steps_json = json.dumps([
        {"index": s.index, "instruction": s.instruction,
         "verification": s.verification, "timeout_seconds": s.timeout_seconds,
         "help_text": s.help_text, "completed": s.completed,
         "failed": s.failed, "started_at": s.started_at,
         "completed_at": s.completed_at, "output": s.output, "error": s.error}
        for s in guide_steps
    ])

    try:
        conn.execute(
            "INSERT INTO guide_sessions "
            "(id, protocol_name, title, current_step, total_steps, status, "
            " channel, steps_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, title, title, 0, len(guide_steps), "running",
             channel, steps_json, now, now),
        )
        conn.commit()
    except Exception:
        pass

    return GuideSession(
        id=session_id, title=title, current_step=0,
        total_steps=len(guide_steps), status="running",
        channel=channel, steps=guide_steps,
        created_at=now, updated_at=now,
    )


def get_current_step(session: GuideSession) -> Optional[GuideStep]:
    """Get the current (next uncompleted) step in the guide."""
    for s in session.steps:
        if not s.completed and not s.failed:
            return s
    return None


def advance_guide(
    conn,
    session_id: str,
    action: str = "done",
    output: str = "",
    error: str = "",
) -> Optional[GuideSession]:
    """Advance a guide session by one step.

    Args:
        conn: Database connection.
        session_id: The guide session ID.
        action: "done" to complete current step, "fail" to mark it failed,
                "abort" to cancel the guide, "pause" to pause.
        output: Output from verification (optional).
        error: Error message if verification failed.

    Returns:
        Updated ``GuideSession`` or None if not found.
    """
    session = load_session(conn, session_id)
    if session is None:
        return None

    now = now_iso()

    if action == "abort":
        _update_session(conn, session_id, status="aborted", updated_at=now)
        session.status = "aborted"
        return session

    if action == "pause":
        _update_session(conn, session_id, status="paused", updated_at=now)
        session.status = "paused"
        return session

    if action == "resume":
        _update_session(conn, session_id, status="running", updated_at=now)
        session.status = "running"
        return session

    # Get the current step.
    current = get_current_step(session)
    if current is None:
        # All steps done — mark complete.
        _update_session(conn, session_id, status="completed", updated_at=now)
        session.status = "completed"
        return session

    if action == "fail":
        current.failed = True
        current.error = error
        current.completed_at = now
    elif action == "done":
        current.completed = True
        current.output = output
        current.completed_at = now

    # Update the step index and persist.
    next_step = current.index + 1
    if next_step >= session.total_steps:
        final_status = "completed" if action == "done" else "running"
        _update_session(
            conn, session_id,
            current_step=next_step if action == "done" else session.current_step,
            status=final_status,
            steps=_steps_to_json(session.steps),
            updated_at=now,
        )
        session.status = final_status
    else:
        _update_session(
            conn, session_id,
            current_step=next_step if action == "done" else session.current_step,
            status="running",
            steps=_steps_to_json(session.steps),
            updated_at=now,
        )
        session.current_step = next_step if action == "done" else session.current_step

    session.steps = session.steps  # already mutated in place
    session.updated_at = now
    return session


def load_session(conn, session_id: str) -> Optional[GuideSession]:
    """Load a guide session from the database."""
    try:
        row = conn.execute(
            "SELECT * FROM guide_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None

        steps_json = row["steps_json"] or "[]"
        steps_data = json.loads(steps_json) if steps_json.strip() else []
        steps = [
            GuideStep(
                index=s["index"], instruction=s.get("instruction", ""),
                verification=s.get("verification", ""),
                timeout_seconds=s.get("timeout_seconds", 120),
                help_text=s.get("help_text", ""),
                completed=s.get("completed", False),
                failed=s.get("failed", False),
                started_at=s.get("started_at", ""),
                completed_at=s.get("completed_at", ""),
                output=s.get("output", ""), error=s.get("error", ""),
            )
            for s in steps_data
        ]

        return GuideSession(
            id=row["id"], title=row["title"],
            current_step=row["current_step"],
            total_steps=row["total_steps"],
            status=row["status"], channel=row["channel"],
            steps=steps, created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    except Exception:
        return None


def list_active_sessions(conn) -> list[GuideSession]:
    """List all running or paused guide sessions."""
    try:
        rows = conn.execute(
            "SELECT * FROM guide_sessions WHERE status IN ('running', 'paused') "
            "ORDER BY created_at DESC"
        ).fetchall()
        sessions = []
        for row in rows:
            session = load_session(conn, row["id"])
            if session:
                sessions.append(session)
        return sessions
    except Exception:
        return []


def format_step(session: GuideSession, step: GuideStep) -> str:
    """Format a guide step for CLI display."""
    lines: list[str] = []
    lines.append(f"  Guide: {session.title}")
    lines.append(f"  Step {step.index + 1}/{session.total_steps}")
    lines.append(f"  {'─' * 40}")
    lines.append(f"  {step.instruction}")
    if step.verification:
        lines.append(f"  Verify: {step.verification}")
    if step.timeout_seconds:
        lines.append(f"  Timeout: {step.timeout_seconds}s")
    lines.append("")
    lines.append("  Reply: `done` to proceed, `fail` to report issue, `abort` to cancel")
    return "\n".join(lines)


def _update_session(conn, session_id: str, **kwargs) -> None:
    """Update one or more fields of a guide session."""
    if not kwargs:
        return
    sets = []
    params = []
    for key, val in kwargs.items():
        if key == "steps":
            sets.append("steps_json = ?")
            params.append(val)
        else:
            sets.append(f"{key} = ?")
            params.append(val)
    params.append(session_id)
    try:
        conn.execute(
            f"UPDATE guide_sessions SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    except Exception:
        pass


def _steps_to_json(steps: list[GuideStep]) -> str:
    """Serialize steps to JSON for storage."""
    return json.dumps([
        {"index": s.index, "instruction": s.instruction,
         "verification": s.verification, "timeout_seconds": s.timeout_seconds,
         "help_text": s.help_text, "completed": s.completed,
         "failed": s.failed, "started_at": s.started_at,
         "completed_at": s.completed_at, "output": s.output, "error": s.error}
        for s in steps
    ])
