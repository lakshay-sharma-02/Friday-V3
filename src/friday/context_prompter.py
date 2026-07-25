"""Pillar C — Learned Personality.

Enriches LLM prompts with context from what Friday has learned about the user's
workflows, active projects, and recurring patterns. This is NOT templated
personalization — it's real accumulated state surfaced naturally so the LLM can
reference it in conversation when relevant.

The enrichment is injected as a "LEARNED CONTEXT" section in the system prompt,
separate from the Evidence block. The LLM is instructed to use it only when
naturally relevant, never to fabricate familiarity.

Sources:
- workflow_intents (Pillar B Stage 3): labeled workflows like "Start dev server"
- mined_patterns (Pillar B Stage 2): raw repeated action sequences
- sessions: recent engineering sessions
- repositories: active project list
"""

from __future__ import annotations

import json
from typing import Optional


def build_context_prompt(conn) -> str:
    """Build a natural-language enrichment block for the LLM system prompt.

    Returns a string that can be appended to the system prompt, or an empty
    string if no learned context exists yet. Never raises.
    """
    parts: list[str] = []

    _add_workflows(conn, parts)
    _add_recent_sessions(conn, parts)
    _add_project_overview(conn, parts)

    if not parts:
        return ""

    return (
        "\n\n--- LEARNED CONTEXT (background knowledge about the user) ---\n"
        + "\n".join(parts)
        + "\n--- END LEARNED CONTEXT ---\n"
        "\nInstruction: The LEARNED CONTEXT above is background knowledge about "
        "the user's typical workflows and active projects. You MAY reference it "
        "naturally when it is relevant to the question (e.g. 'I noticed you've "
        "been working on X recently'), but NEVER fabricate specifics. The "
        "Evidence block above is the ONLY source of facts for answering the "
        "question. If no learned context is listed, say nothing about it."
    )


def _add_workflows(conn, parts: list[str]) -> None:
    """Append known workflow intents to the context block."""
    try:
        rows = conn.execute(
            "SELECT intent_label, intent_description, confidence, "
            "pattern_summary FROM workflow_intents "
            "WHERE confidence IN ('high', 'medium') "
            "ORDER BY labeled_at DESC LIMIT 5"
        ).fetchall()
    except Exception:
        return

    if not rows:
        return

    lines = ["\nWorkflows I've learned:"]
    for r in rows:
        label = r["intent_label"]
        desc = r["intent_description"] or ""
        conf = r["confidence"]
        if desc:
            lines.append(f"- {label}: {desc} ({conf} confidence)")
        else:
            lines.append(f"- {label} ({conf} confidence)")

    parts.append("\n".join(lines))


def _add_recent_sessions(conn, parts: list[str]) -> None:
    """Append recent engineering session activity."""
    try:
        rows = conn.execute(
            "SELECT start_time, primary_repo, summary FROM sessions "
            "ORDER BY start_time DESC LIMIT 3"
        ).fetchall()
    except Exception:
        return

    if not rows:
        return

    lines = ["\nRecent activity:"]
    for r in rows:
        when = r["start_time"][:16] if r["start_time"] else "?"
        repo = r["primary_repo"] or "multiple repos"
        summary = r["summary"] or ""
        if summary:
            lines.append(f"- {when}: {repo} — {summary[:120]}")
        else:
            lines.append(f"- {when}: {repo}")

    parts.append("\n".join(lines))


def _add_project_overview(conn, parts: list[str]) -> None:
    """Append an overview of active projects."""
    try:
        rows = conn.execute(
            "SELECT name, maturity, commit_count FROM repositories "
            "WHERE commit_count > 0 ORDER BY last_commit_date DESC LIMIT 8"
        ).fetchall()
    except Exception:
        return

    if not rows:
        return

    # Pick the most active ones.
    active = [r for r in rows if r["commit_count"] and r["commit_count"] > 5]
    if not active:
        active = rows[:3]

    lines = ["\nActive projects:"]
    for r in active:
        name = r["name"]
        mat = r["maturity"] or "unknown"
        commits = r["commit_count"] or 0
        lines.append(f"- {name} ({mat}, {commits} commits)")

    parts.append("\n".join(lines))
