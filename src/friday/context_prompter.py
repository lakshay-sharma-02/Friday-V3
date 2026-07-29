"""Pillar C — Learned Personality + Phase D — Conversation Context Injection.

Enriches LLM prompts with context from what Friday has learned about the user's
workflows, active projects, recurring patterns, AND conversation history +
learned operator preferences. This is NOT templated personalization — it's real
accumulated state surfaced naturally so the LLM can reference it in conversation
when relevant.

The enrichment is injected as a "LEARNED CONTEXT" section in the system prompt,
separate from the Evidence block. The LLM is instructed to use it only when
naturally relevant, never to fabricate familiarity.

Sources:
- workflow_intents (Pillar B Stage 3): labeled workflows like "Start dev server"
- mined_patterns (Pillar B Stage 2): raw repeated action sequences
- sessions: recent engineering sessions
- repositories: active project list
- conversation_log (Phase A): recent exchanges across all channels
- operator_preferences (Phase B+C): learned identity and preferences
"""

from __future__ import annotations

import json
from typing import Optional


_MAX_CONVERSATION_EXCHANGES = 6
_MAX_PREFERENCES = 10
_MAX_MEMORY_FACTS = 10


def build_context_prompt(conn) -> str:
    """Build a natural-language enrichment block for the LLM system prompt.

    Returns a string that can be appended to the system prompt, or an empty
    string if no learned context exists yet. Never raises.

    Phase D adds conversation history and learned operator preferences
    so the LLM can reference what you've discussed and what it knows
    about you naturally in its answers.
    """
    parts: list[str] = []

    _add_workflows(conn, parts)
    _add_recent_sessions(conn, parts)
    _add_project_overview(conn, parts)
    _add_operator_preferences(conn, parts)
    _add_conversation_context(conn, parts)
    _add_memory_context(conn, parts)

    if not parts:
        return ""

    return (
        "\n\n--- LEARNED CONTEXT (background knowledge about the user) ---\n"
        + "\n".join(parts)
        + "\n--- END LEARNED CONTEXT ---\n"
        "\nInstruction: The LEARNED CONTEXT above is background knowledge about "
        "the user's typical workflows, active projects, identity, preferences, "
        "and recent conversation history. You MAY reference it naturally when "
        "it is relevant to the question (e.g. 'I noticed you've been working "
        "on X recently', 'You mentioned you prefer Python', 'As we discussed '"
        "earlier'). But NEVER fabricate specifics — only reference what is "
        "actually shown. The Evidence block above is the ONLY source of facts "
        "for answering the question. If no learned context is listed, say "
        "nothing about it."
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


def _add_operator_preferences(conn, parts: list[str]) -> None:
    """Append learned operator identity and preferences (Phase B+C).

    Reads from operator_preferences table (both explicit and derived)
    and formats everything Friday knows about the operator.
    """
    try:
        rows = conn.execute(
            "SELECT key, value, source FROM operator_preferences ORDER BY key LIMIT ?",
            (_MAX_PREFERENCES,),
        ).fetchall()
    except Exception:
        return

    if not rows:
        return

    # Separate name from other preferences for cleaner display.
    name = None
    other_prefs: list[str] = []

    for r in rows:
        key = r["key"]
        value = r["value"]
        source = r["source"]
        if key == "name" and value:
            name = value
        elif key in ("preferred_technology", "preferred_channel", "no_notifications",
                     "preferred_worker_types"):
            marker = "" if source == "explicit" else " (learned)"
            other_prefs.append(f"{key}={value}{marker}")
        else:
            # Other preferences (future keys) shown as-is.
            marker = "" if source == "explicit" else " (learned)"
            other_prefs.append(f"{key}={value}{marker}")

    lines: list[str] = []
    if name:
        lines.append(f"\nOperator name: {name}")
    if other_prefs:
        lines.append("Operator preferences: " + "; ".join(other_prefs))

    if lines:
        parts.append("\n".join(lines))


def _add_memory_context(conn, parts: list[str]) -> None:
    """Append remembered facts from the memory engine (Memory Layer).

    Reads active facts from the knowledge_memory table and appends
    them to the learned context so Friday can recall what it knows
    about the operator across conversations.
    """
    try:
        from .memory import MemoryEngine
        engine = MemoryEngine(conn)
        context = engine.build_memory_context(max_facts=_MAX_MEMORY_FACTS)
        if context:
            parts.append("\n" + context)
    except Exception:
        pass


def _add_conversation_context(conn, parts: list[str]) -> None:
    """Append recent conversation history (Phase A+D).

    Reads the last N exchanges from the conversation_log across all
    channels, showing what the operator and Friday have discussed.
    This lets the LLM reference previous conversations naturally.
    """
    try:
        rows = conn.execute(
            """SELECT channel, channel_id, routing, user_message, friday_reply,
                      conversation_at
               FROM conversation_log
               ORDER BY conversation_at DESC
               LIMIT ?""",
            (_MAX_CONVERSATION_EXCHANGES,),
        ).fetchall()
    except Exception:
        return

    if not rows:
        return

    lines = ["\nRecent conversation history (most recent first):"]
    for r in reversed(rows):  # Reverse so most recent is last (reads naturally)
        channel = r["channel"] or "?"
        when = r["conversation_at"][11:19] if r["conversation_at"] and len(r["conversation_at"]) >= 19 else "?"
        user_msg = (r["user_message"] or "")[:150]
        friday_reply = (r["friday_reply"] or "")[:150]
        lines.append(f"  [{channel} @ {when}] You: {user_msg}")
        lines.append(f"  [{channel} @ {when}] Friday: {friday_reply}")

    parts.append("\n".join(lines))
