"""Proactive Reply Handler — turns operator replies into actions.

When the Proactive Conversation Engine sends a message like:
  "Hey Lakshay, I found 2 skills degrading — want me to check on them?"

The operator can reply naturally:
  "Yes, check on them" → Friday runs drift detection and reports back
  "Not now" → Friday clears the pending context, tries again later
  "Stop asking me that" → Friday learns a negative preference

Architecture:
  - Thread-safe pending context protected by ``_PENDING_LOCK``
  - IdentityEngine.process() checks for pending before routing normally
  - Intent classification via keyword matching (no LLM needed for this)
  - Action execution per event_type using existing pipelines

Flow:
  1. check_and_proact() sends proactive message → register_pending()
  2. Operator replies via Telegram/Slack/CLI
  3. IdentityEngine.process() detects pending → calls handle_reply()
  4. handle_reply() classifies intent → executes action → returns response
  5. Pending context is cleared

Thread safety:
  - ``_PENDING`` is module-level state shared across IdentityEngine instances
    that may run in separate daemon threads (Telegram poller, Slack RTM, CLI
    handler). Every access is gated by ``_PENDING_LOCK``.
  - ``handle_reply()`` snapshots the pending state under the lock, then releases
    it before executing actions (which may be long-running). This prevents
    lock contention on DB calls.
"""

from __future__ import annotations

import threading
from typing import Optional
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Thread-safe pending proactive context
# ---------------------------------------------------------------------------

_PENDING: Optional[dict] = None
"""Pending proactive context, set by register_pending() and cleared by
handle_reply() or clear_pending(). Structure::

    {
        "event_type": str,
        "channel": str,        # where the proactive message was sent
        "message": str,        # the proactive message text
        "timestamp": str,      # ISO timestamp
    }

All access must be guarded by ``_PENDING_LOCK`` to prevent races when
multiple channel handlers (Telegram, Slack, Discord, CLI) process replies
from different threads.
"""

_PENDING_LOCK = threading.Lock()
"""Lock guarding all reads and writes to ``_PENDING``."""

# How long a pending proactive message stays valid before being cleared
# automatically (e.g. if the operator never replies).
_PENDING_TTL_SECONDS = 3600  # 1 hour


def register_pending(event_type: str, channel: str, message: str) -> None:
    """Register a pending proactive message awaiting operator reply.

    Args:
        event_type: The type of signal that triggered the proactive message
            (e.g. ``"skill_drift_detected"``, ``"new_initiative"``).
        channel: The channel the proactive message was sent to
            (e.g. ``"telegram"``, ``"cli"``, ``"slack"``).
        message: The proactive message text sent to the operator.

    Thread-safe: acquires ``_PENDING_LOCK``.
    """
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = {
            "event_type": event_type,
            "channel": channel,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def clear_pending() -> None:
    """Clear any pending proactive context.

    Thread-safe: acquires ``_PENDING_LOCK``.
    """
    global _PENDING
    with _PENDING_LOCK:
        _PENDING = None


def has_pending() -> bool:
    """Check if there's a pending proactive message awaiting reply.

    Also clears stale entries that have exceeded TTL.

    Thread-safe: acquires ``_PENDING_LOCK``.
    """
    global _PENDING
    with _PENDING_LOCK:
        if _PENDING is None:
            return False

        # Check TTL — clear if too old.
        try:
            ts = datetime.fromisoformat(_PENDING["timestamp"])
            if (datetime.now(timezone.utc) - ts).total_seconds() > _PENDING_TTL_SECONDS:
                _PENDING = None
                return False
        except (ValueError, TypeError):
            pass

        return True


def get_pending_event_type() -> Optional[str]:
    """Get the event_type of the pending proactive message, if any.

    Thread-safe: acquires ``_PENDING_LOCK``.
    """
    with _PENDING_LOCK:
        if not _pending_valid_unlocked():
            return None
        return _PENDING["event_type"]


def get_pending_channel() -> Optional[str]:
    """Get the channel of the pending proactive message, if any.

    Thread-safe: acquires ``_PENDING_LOCK``.
    """
    with _PENDING_LOCK:
        if not _pending_valid_unlocked():
            return None
        return _PENDING["channel"]


def _pending_valid_unlocked() -> bool:
    """Check whether ``_PENDING`` is set and not stale.

    Caller MUST hold ``_PENDING_LOCK``.
    """
    if _PENDING is None:
        return False
    try:
        ts = datetime.fromisoformat(_PENDING["timestamp"])
        if (datetime.now(timezone.utc) - ts).total_seconds() > _PENDING_TTL_SECONDS:
            return False
    except (ValueError, TypeError):
        pass
    return True


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

#: Keywords indicating the operator wants Friday to proceed with the action.
_AFFIRM_KEYWORDS = frozenset({
    "yes", "yeah", "yep", "sure", "okay", "ok", "k", "kk",
    "go ahead", "do it", "tell me", "show me", "walk me through",
    "let's see", "let me see", "i'd like to see", "i want to see",
    "check", "check it", "check them", "look into it", "look into",
    "analyze", "run it", "go for it", "please do", "please",
    "absolutely", "definitely", "of course", "would love to",
    "yes please", "yeah sure", "sure why not",
})

#: Keywords indicating the operator wants to decline for now.
_DECLINE_KEYWORDS = frozenset({
    "no", "nope", "nah", "not now", "later", "maybe later",
    "not yet", "stop", "not right now", "can't now", "busy",
    "some other time", "another time", "skip", "pass",
})

#: Keywords indicating the operator never wants this type of proactive message again.
_DISMISS_KEYWORDS = frozenset({
    "stop asking", "don't ask", "shut up", "never", "stop",
    "don't show me", "don't tell me", "leave me alone",
    "i don't care", "not interested", "that's annoying",
    "stop bothering me", "quit it",
})


def classify_intent(text: str) -> str:
    """Classify the operator's reply intent.

    Uses the LLM first for natural language understanding, falls back to
    keyword matching if LLM is unavailable.

    Args:
        text: The operator's reply text.

    Returns:
        One of ``"affirm"``, ``"decline"``, ``"dismiss"``, or ``"unknown"``.
    """

    lower = text.lower().strip().rstrip("?!.")
    if not lower:
        return "unknown"

    # Try LLM-based classification first.
    try:
        from .services.llm import _call, _enabled
        if _enabled():
            system = (
                "You classify user replies to an AI assistant's proactive suggestions.\n"
                "Return ONLY one word: affirm, decline, dismiss, or unknown.\n\n"
                "- affirm: user wants to proceed (yes, sure, tell me, show me, let's do it, check it out, etc.)\n"
                "- decline: user doesn't want to proceed right now (no, not now, later, maybe later, etc.)\n"
                "- dismiss: user never wants to be asked about this again (stop asking, don't bother me, never, etc.)\n"
                "- unknown: can't determine from the message\n\n"
                "Examples:\n"
                "  'yes please' \u2192 affirm\n"
                "  'sure, show me' \u2192 affirm\n"
                "  'not now' \u2192 decline\n"
                "  'later' \u2192 decline\n"
                "  'stop asking me about this' \u2192 dismiss\n"
                "  'i don't care' \u2192 dismiss\n"
                "  'what's the weather?' \u2192 unknown\n"
                "  'hello' \u2192 unknown"
            )
            result = _call(system, f"User message: {text[:300]}\n\nClassify:")
            if result:
                result = result.strip().lower()
                if result in ("affirm", "decline", "dismiss", "unknown"):
                    return result
    except Exception:
        pass

    # Fallback: keyword matching.
    # Check dismiss first (strongest signal — it includes "stop").
    for kw in _DISMISS_KEYWORDS:
        if kw in lower:
            return "dismiss"

    # Check affirm.
    if lower in _AFFIRM_KEYWORDS:
        return "affirm"
    for kw in _AFFIRM_KEYWORDS:
        if lower.startswith(kw):
            return "affirm"

    # Check decline.
    if lower in _DECLINE_KEYWORDS:
        return "decline"
    for kw in _DECLINE_KEYWORDS:
        if lower.startswith(kw):
            return "decline"

    return "unknown"


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _fetch_initiatives_summary(conn) -> str:
    """Fetch recent initiatives from the DB."""
    try:
        rows = conn.execute(
            "SELECT title, status FROM initiatives "
            "WHERE status != 'resolved' "
            "ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        if not rows:
            return "There are no open initiatives right now."
        lines = ["Here are the open initiatives I've identified:"]
        for i, r in enumerate(rows, 1):
            lines.append(f"  {i}. [{r['status']}] {r['title'][:100]}")
        return "\n".join(lines)
    except Exception:
        return "I couldn't fetch the initiatives list right now."


def _fetch_suggestions_summary(conn) -> str:
    """Fetch recent cross-project suggestions from the generator.

    Suggestions are not persisted in a DB table — they're generated
    on-the-fly by ``generate_suggestions()`` from ``cli_suggest``.
    """
    try:
        from .cli_suggest import generate_suggestions
        result = generate_suggestions(conn)
        if not result.suggestions:
            return "There are no outstanding suggestions right now."
        lines = ["Here are the integration opportunities I've found:"]
        for i, s in enumerate(result.suggestions[:3], 1):
            sev = "\U0001f534" if s.severity == 'high' else "\U0001f7e1"
            lines.append(f"  {i}. {sev} [{s.severity}] {s.title[:100]}")
        return "\n".join(lines)
    except Exception:
        return "I couldn't fetch the suggestions list right now."


def _run_drift_check(conn) -> str:
    """Run drift detection and return a summary."""
    try:
        from .skill_formation import detect_skill_drift, format_drift_reports
        reports = detect_skill_drift(conn)
        if not reports:
            return "I checked all the skills — they're looking healthy. No degradation detected."
        return format_drift_reports(reports)
    except Exception:
        return "I tried to run a drift check but ran into an issue. Try `friday skills drift` manually."


def _run_gap_analysis(conn) -> str:
    """Run capability gap analysis and return a summary."""
    try:
        from .meta.gap_analyzer import analyze
        report = analyze(conn)
        if report.new_gaps == 0 and report.open_gaps == 0:
            return "Good news — I don't see any capability gaps in the pipeline right now."
        return (
            f"I found {report.new_gaps} new gap(s) and {report.open_gaps} open gap(s). "
            f"Run `friday meta analyze` for the full breakdown."
        )
    except Exception:
        return "I tried to analyze capability gaps but hit an error."


def _show_new_skills(conn) -> str:
    """Fetch and summarize recently formed skills."""
    try:
        rows = conn.execute(
            "SELECT fs.id, w.name, w.status FROM formed_skills fs "
            "JOIN workers w ON w.manifest_ref = 'formed_skill:' || CAST(fs.id AS TEXT) "
            "WHERE w.kind = 'formed_skill' "
            "ORDER BY fs.created_at DESC LIMIT 5"
        ).fetchall()
        if not rows:
            return "No skills have been formed yet."
        lines = ["Here are the skills I've formed from your workflow patterns:"]
        for i, r in enumerate(rows, 1):
            status_icon = "\u2705" if r['status'] == 'beta' else "\U0001f7e1"
            lines.append(f"  {i}. {status_icon} {r['name']} ({r['status']})")
        return "\n".join(lines)
    except Exception:
        return "I couldn't fetch the skills list right now."


def _show_correlations(conn) -> str:
    """Fetch and summarize recent cross-project correlations."""
    try:
        rows = conn.execute(
            "SELECT title, confidence FROM insights "
            "WHERE insight_type = 'correlation' "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        if not rows:
            return "No cross-project correlations found yet."
        lines = ["Here are the cross-project correlations I've discovered:"]
        for i, r in enumerate(rows, 1):
            lines.append(f"  {i}. \U0001f517 {r['title'][:100]} (confidence: {r['confidence']})")
        return "\n".join(lines)
    except Exception:
        return "I couldn't fetch correlations right now."


def _execute_action(event_type: str, conn) -> str:
    """Execute the action associated with a proactive event type.

    Args:
        event_type: The type of signal the proactive message was about.
        conn: Database connection.

    Returns:
        A response string with the action result.
    """
    action_map = {
        "new_initiative": _fetch_initiatives_summary,
        "high_severity_suggestion": _fetch_suggestions_summary,
        "skill_drift_detected": _run_drift_check,
        "capability_gap_detected": _run_gap_analysis,
        "skill_formed": _show_new_skills,
        "cross_project_correlation": _show_correlations,
    }

    handler = action_map.get(event_type)
    if handler:
        return handler(conn)

    # Generic fallback.
    return (
        f"I've noted your interest. Run `friday feed` to see what's "
        f"happening, or let me know what you'd like me to look into."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def handle_reply(text: str, conn) -> Optional[str]:
    """Handle a reply to a pending proactive message.

    This is the main entry point, called from ``IdentityEngine.process()``
    when ``has_pending()`` returns True.

    The flow:
      1. Snapshot the pending context under the lock (releases before I/O)
      2. Classify the operator's intent (affirm/decline/dismiss)
      3. Execute the appropriate action (or learn a preference)
      4. Clear the pending context
      5. Return a response to send back through the channel

    Args:
        text: The operator's reply text.
        conn: Database connection.

    Returns:
        A response string to send back, or None if the reply wasn't
        actually a response to a proactive message (false positive).
    """
    global _PENDING

    # Snapshot pending state under the lock, then release before I/O.
    with _PENDING_LOCK:
        if not _pending_valid_unlocked():
            return None

        snapshot = {
            "event_type": _PENDING["event_type"],
            "channel": _PENDING["channel"],
            "message": _PENDING["message"],
        }
        # Clear immediately so duplicate replies don't double-fire.
        _PENDING = None

    event_type = snapshot["event_type"]
    channel = snapshot["channel"]
    message = snapshot["message"]

    # Classify the intent (no lock needed — pure function).
    intent = classify_intent(text)

    if intent == "affirm":
        # Execute the action and respond (I/O, no lock).
        try:
            action_result = _execute_action(event_type, conn)
            response = action_result
        except Exception as exc:
            response = f"I tried to look into that but ran into an error: {exc}"
        return response

    elif intent == "decline":
        response = "No problem. I'll check back later if something else comes up."
        return response

    elif intent == "dismiss":
        # Learn a negative preference for this event type.
        try:
            from .db import set_operator_preference
            set_operator_preference(
                conn,
                key=f"no_proactive_{event_type}",
                value="true",
                source="derived",
            )
            response = (
                f"Got it — I won't ask about this type of thing again. "
                f"If you change your mind, just let me know."
            )
        except Exception:
            response = "Understood. I'll lay low for a while."
        return response

    else:
        # Unknown intent — restore the pending context so it's not lost.
        # This handles cases like the operator replying to a proactive
        # message with a completely different question (e.g. "what's the
        # weather?"). We put it back so they can answer later.
        with _PENDING_LOCK:
            _PENDING = {
                "event_type": event_type,
                "channel": channel,
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        return None
