"""NotificationEngine — smart, template-driven notification routing for ambient events.

Replaces the old ``_notify()`` / ``_notify_telegram()`` aggregation in daemon.py
with a rules-based engine that:

1. Reads events from the ambient feed (already pushed by ``_run_cycle()``)
2. Applies routing rules: which events → which channels
3. Uses templates to generate human-readable, varied notification messages
4. Deduplicates: skips events that were already notified recently
5. Ships to channels: desktop (notify-send/osascript), Telegram, Slack
6. Plays sound alerts for important notifications
7. Supports click-to-act for actionable events

Usage::

    from .notification import notify_cycle_events
    notify_cycle_events(conn, cycle, no_notify=effective_no_notify)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# File that stores pending notification actions (for click-to-act).
_ACTION_FILE = Path("/tmp") / "friday_pending_actions.json"


# ---------------------------------------------------------------------------
# Sound alerts
# ---------------------------------------------------------------------------

_SOUND_DIR = Path("/usr/share/sounds/freedesktop/stereo")

# Priority -> sound file mapping
_SOUND_MAP: dict[int, str] = {
    3: "dialog-warning.oga",
    2: "message-new-instant.oga",
    1: "message.oga",
    0: "bell.oga",
}


def _play_sound(priority: int) -> None:
    """Play a notification sound based on priority level.

    Uses ``paplay`` (PulseAudio), ``pw-play`` (PipeWire), or ``aplay`` (ALSA)
    to play a freedesktop sound theme sound file.  Falls back silently if no
    sound player or sound file is available.
    """
    sound_name = _SOUND_MAP.get(priority, "bell.oga")
    sound_path = _SOUND_DIR / sound_name
    if not sound_path.exists():
        # Fallback: try the bell sound if our preferred sound is missing.
        fallback = _SOUND_DIR / "bell.oga"
        if not fallback.exists():
            return
        sound_path = fallback

    import subprocess

    for player in ("paplay", "pw-play", "aplay"):
        try:
            subprocess.run(
                [player, str(sound_path)],
                timeout=2, capture_output=True,
            )
            return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


# ---------------------------------------------------------------------------
# Notification action registry (for click-to-act)
# ---------------------------------------------------------------------------


def _register_action(notif_id: str, label: str, command: str) -> None:
    """Register a pending notification action that can be triggered later.

    Writes to ``_ACTION_FILE`` so the ``friday notif next`` CLI command can
    find and execute the most recent actionable notification.
    """
    try:
        actions: dict[str, dict] = {}
        if _ACTION_FILE.exists():
            raw = _ACTION_FILE.read_text().strip()
            if raw:
                actions = json.loads(raw)
        actions[notif_id] = {
            "label": label,
            "command": command,
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
        _ACTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ACTION_FILE.write_text(
            json.dumps(actions, indent=2, default=str)
        )
    except Exception:
        pass


def _latest_action() -> dict | None:
    """Return the most recently registered pending action, or None."""
    try:
        if not _ACTION_FILE.exists():
            return None
        raw = _ACTION_FILE.read_text().strip()
        if not raw:
            return None
        actions: dict[str, dict] = json.loads(raw)
        if not actions:
            return None
        # Return the action with the latest timestamp.
        sorted_actions = sorted(
            actions.values(),
            key=lambda a: a.get("timestamp", ""),
            reverse=True,
        )
        return sorted_actions[0]
    except Exception:
        return None


def _clear_all_actions() -> None:
    """Remove all pending actions from the registry."""
    try:
        if _ACTION_FILE.exists():
            _ACTION_FILE.unlink()
    except Exception:
        pass


def run_pending_action() -> str:
    """Run the most recent pending notification action.

    Returns a human-readable result string.
    """
    action = _latest_action()
    if action is None:
        return "No pending notification actions."

    cmd = action.get("command", "")
    label = action.get("label", "")
    if not cmd:
        return "Pending action has no command."

    import subprocess

    try:
        result = subprocess.run(
            cmd, shell=True, timeout=30,
            capture_output=True, text=True,
        )
        # Auto-clear the action registry so the same action isn't re-run.
        _clear_all_actions()
        output = result.stdout.strip() or result.stderr.strip() or "(no output)"
        return f"✅ {label}: {cmd}\n{output[:500]}"
    except subprocess.TimeoutExpired:
        return f"⏱️  {label} timed out: {cmd}"
    except Exception as exc:
        return f"❌ {label} failed: {exc}"


# ---------------------------------------------------------------------------
# Notification channels
# ---------------------------------------------------------------------------


def _notify_desktop(
    title: str,
    message: str,
    action_command: str = "",
    action_label: str = "",
) -> None:
    """Desktop notification via notify-send (Linux) or osascript (macOS).

    On Linux, supports click-to-act: if ``action_command`` is provided, adds a
    clickable ``--action=default,Run`` button and registers the action so
    ``friday notif next`` can execute it later.
    """
    import subprocess
    import uuid

    notif_id = str(uuid.uuid4())[:8]

    try:
        if sys.platform == "linux":
            cmd = ["notify-send", title, message]

            # Add action buttons for actionable events.
            if action_command:
                cmd.extend(["--action", f"default,{action_label or 'Run'}"])
                _register_action(notif_id, action_label or action_command, action_command)

            subprocess.run(cmd, timeout=5, capture_output=True)

        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                timeout=5, capture_output=True,
            )
    except Exception:
        pass


def _notify_telegram(title: str, message: str) -> None:
    """Send a rich HTML-formatted message via Telegram.

    Uses HTML parse_mode for bold, code, and emoji formatting.
    Best-effort; silent on failure.
    """
    try:
        from .services.telegram import TelegramConfig, _get_updates, _send_message

        config = TelegramConfig.from_env()
        if not config.configured:
            return

        updates = _get_updates(config, limit=5, timeout=2)
        if not updates:
            return

        seen: list[str] = []
        for u in updates:
            cid = u.get("chat_id")
            if cid and cid not in seen:
                seen.append(cid)
        if not seen:
            return

        chat_id = str(seen[-1])

        # Build HTML-formatted message.
        html_body = _tg_html_message(title, message)
        _send_message(config, chat_id, html_body, parse_mode="HTML")
    except Exception:
        pass


def _notify_slack(title: str, message: str) -> None:
    """Send a rich mrkdwn-formatted message via Slack.

    Best-effort; silent on failure.
    """
    try:
        from .services.slack import SlackConfig, _list_channels, _post_message

        config = SlackConfig.from_env()
        if not config.configured:
            return

        channels = _list_channels(config, limit=1)
        if not channels:
            return

        # Build mrkdwn-formatted message.
        mrkdwn = _slack_mrkdwn_message(title, message)
        for ch in channels:
            ch_id = ch.get("id", "")
            if ch_id:
                _post_message(config, ch_id, mrkdwn)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Message formatters per channel
# ---------------------------------------------------------------------------


def _tg_html_message(title: str, message: str) -> str:
    """Convert a (title, message) pair into an HTML-formatted Telegram message.

    Telegram Bot API HTML mode supports:
      <b>bold</b>, <i>italic</i>, <code>code</code>, <pre>pre</pre>
      <a href="url">link</a>, <u>underline</u>, <s>strike</s>
    """
    # Escape HTML entities in the raw text.
    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    safe_title = _esc(title)
    safe_msg = _esc(message)

    # Wrap friday commands in <code> blocks.
    import re

    safe_msg = re.sub(
        r"`([^`]+)`",
        lambda m: f"<code>{m.group(1)}</code>",
        safe_msg,
    )

    # Bold the title.
    return f"<b>{safe_title}</b>\n\n{safe_msg}"


def _slack_mrkdwn_message(title: str, message: str) -> str:
    """Convert a (title, message) pair into an mrkdwn-formatted Slack message."""
    # Escape Slack mrkdwn special characters.
    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    safe_title = _esc(title)
    safe_msg = _esc(message)

    # Wrap friday commands in `code` backticks.
    import re

    safe_msg = re.sub(
        r"`([^`]+)`",
        lambda m: f"`{m.group(1)}`",
        safe_msg,
    )

    return f"*{safe_title}*\n{safe_msg}"


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------


@dataclass
class NotificationRule:
    """One routing rule: which events produce which notifications."""

    # Event types this rule applies to (empty = all)
    event_types: tuple[str, ...] = ()
    # Minimum priority (0-3) — events below this are skipped
    min_priority: int = 1
    # Which channels to notify
    channels: tuple[str, ...] = ("feed",)
    # Hours to wait before notifying the same event type again
    cooldown_hours: int = 6
    # Whether to play a sound notification
    play_sound: bool = False


# Default rules — highest priority first
DEFAULT_RULES: list[NotificationRule] = [
    # Critical errors → everything + sound
    NotificationRule(
        event_types=("cycle_failed", "kill_switch_activated"),
        min_priority=3,
        channels=("desktop", "telegram", "slack", "feed"),
        cooldown_hours=1,
        play_sound=True,
    ),
    # Important discoveries → desktop + telegram + sound
    NotificationRule(
        event_types=("new_initiative", "high_severity_suggestion",
                      "skill_drift_detected", "capability_gap_detected"),
        min_priority=2,
        channels=("desktop", "telegram", "feed"),
        cooldown_hours=6,
        play_sound=True,
    ),
    # Normal discoveries → desktop only
    NotificationRule(
        event_types=("skill_formed",),
        min_priority=2,
        channels=("desktop", "feed"),
        cooldown_hours=12,
    ),
    # Low-priority → feed only
    NotificationRule(
        event_types=("repo_changed", "knowledge_updated", "new_patterns",
                      "intent_labeled", "cross_project_correlation",
                      "auto_dispatched", "cycle_complete"),
        min_priority=1,
        channels=("feed",),
        cooldown_hours=0,
    ),
]


# ---------------------------------------------------------------------------
# Message templates — one template function per event type
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, Callable[[dict], tuple[str, str]]] = {}


def _template(event_type: str):
    """Decorator to register a template function for an event type."""

    def deco(fn):
        _TEMPLATES[event_type] = fn
        return fn

    return deco


@_template("repo_changed")
def _t_repo_changed(ev: dict) -> tuple[str, str]:
    detail = ev.get("detail", "")
    return (
        "📂 Friday — Repositories Changed",
        detail or "Workspace activity detected.",
    )


@_template("knowledge_updated")
def _t_knowledge(ev: dict) -> tuple[str, str]:
    detail = ev.get("detail", "")
    return (
        "🧠 Friday — Knowledge Updated",
        detail or "Knowledge base updated.",
    )


@_template("new_initiative")
def _t_initiative(ev: dict) -> tuple[str, str]:
    cmd = ev.get("action_command", "")
    msg = ev.get("detail", "A new engineering initiative has emerged.")
    if cmd:
        msg += f"\n→ `{cmd}`"
    return ("💡 Friday — New Initiative", msg)


@_template("high_severity_suggestion")
def _t_suggestion(ev: dict) -> tuple[str, str]:
    cmd = ev.get("action_command", "")
    msg = ev.get("detail", "Friday found an integration opportunity worth reviewing.")
    if cmd:
        msg += f"\n→ `{cmd}`"
    return ("🔗 Friday — Integration Opportunity", msg)


@_template("capability_gap_detected")
def _t_gap(ev: dict) -> tuple[str, str]:
    cmd = ev.get("action_command", "")
    msg = ev.get("detail", "A capability gap has been detected.")
    if cmd:
        msg += f"\n→ `{cmd}`"
    return ("🕳️ Friday — Capability Gap", msg)


@_template("skill_formed")
def _t_skill(ev: dict) -> tuple[str, str]:
    cmd = ev.get("action_command", "")
    msg = ev.get("detail", "A new skill has been formed from workflow patterns.")
    if cmd:
        msg += f"\n→ `{cmd}`"
    return ("⚡ Friday — New Skill", msg)


@_template("skill_drift_detected")
def _t_drift(ev: dict) -> tuple[str, str]:
    cmd = ev.get("action_command", "")
    msg = ev.get("detail", "Some skills are degrading and may need re-formation.")
    if cmd:
        msg += f"\n→ `{cmd}`"
    return ("⚠️ Friday — Skill Drift", msg)


@_template("cross_project_correlation")
def _t_correlation(ev: dict) -> tuple[str, str]:
    cmd = ev.get("action_command", "")
    msg = ev.get("detail", "Structural or semantic overlap found between repositories.")
    if cmd:
        msg += f"\n→ `{cmd}`"
    return ("🔀 Friday — Cross-Project Correlation", msg)


@_template("cycle_failed")
def _t_cycle_failed(ev: dict) -> tuple[str, str]:
    detail = ev.get("detail", "")
    return (
        "🚨 Friday — Cycle Failed",
        f"Observation cycle failed:\n`{detail[:200]}`",
    )


@_template("cycle_complete")
def _t_cycle_complete(ev: dict) -> tuple[str, str]:
    return ("✅ Friday — Cycle Complete", "Workspace observation cycle finished.")


@_template("kill_switch_activated")
def _t_kill_switch(ev: dict) -> tuple[str, str]:
    return (
        "🛑 Friday — Kill Switch Active",
        "All execution is blocked.\n→ `friday autonomy resume`",
    )


@_template("kill_switch_deactivated")
def _t_kill_switch_off(ev: dict) -> tuple[str, str]:
    return (
        "🔓 Friday — Kill Switch Released",
        "Normal execution has been resumed.",
    )


@_template("dispatch_event")
def _t_dispatch(ev: dict) -> tuple[str, str]:
    detail = ev.get("detail", "Skills auto-dispatched.")
    return ("🤖 Friday — Skills Dispatched", detail)


# ---------------------------------------------------------------------------
# Fallback template for unrecognised event types
# ---------------------------------------------------------------------------


def _default_template(ev: dict) -> tuple[str, str]:
    return (
        f"📬 Friday — {ev.get('title', 'Update')}",
        ev.get("detail", ""),
    )


# ---------------------------------------------------------------------------
# Templates that expose action metadata for click-to-act
# ---------------------------------------------------------------------------


def _get_action_info(ev: dict) -> tuple[str, str]:
    """Extract (action_command, action_label) from an event dict.

    Returns empty strings if the event is not actionable.
    """
    cmd = ev.get("action_command", "") or ""
    label = ev.get("action_label", "") or ""
    if not label and cmd:
        # Derive a friendly label from the command.
        if cmd.startswith("friday"):
            label = cmd[6:].strip().capitalize() if len(cmd) > 6 else "Run"
        elif cmd.startswith("http"):
            label = "Open"
        else:
            label = "Run"
    return cmd, label


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class NotificationEngine:
    """Routes ambient events to notification channels based on rules.

    Usage::

        engine = NotificationEngine(rules=DEFAULT_RULES)
        engine.notify_cycle(conn, cycle_data, no_notify=False)
    """

    def __init__(self, rules: list[NotificationRule] | None = None):
        self.rules = rules or list(DEFAULT_RULES)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def notify_cycle(self, conn, cycle: dict, no_notify: bool = False) -> int:
        """Read unread ambient events from the feed and notify according to rules.

        Args:
            conn: Database connection.
            cycle: The cycle result dict (for status/error info).
            no_notify: If True, suppress all desktop/telegram notifications
                       (feed-only mode).

        Returns:
            Number of notifications sent.
        """
        from .ambient import get_feed, get_unread_count, count_recent_of_type

        # If notifications are globally suppressed, still push to feed
        # (feed entries were already created by _run_cycle()).
        if no_notify:
            return 0

        # Check if this was a failed cycle — send immediate notification.
        outcome = cycle.get("cycle_outcome", "failed")
        if outcome == "failed":
            error = cycle.get("error_detail", "Unknown error")
            # Ensure we always have a non-empty error message for the notification.
            if not error or not str(error).strip():
                error = "Unknown error (check daemon log for traceback)"

            # Extract structured error context if available.
            error_type = cycle.get("error_type", "")
            error_action = cycle.get("error_action", "")
            recovery_hint = cycle.get("recovery_hint", "")
            error_eta = cycle.get("error_eta", "")

            # Build a richer notification message from structured fields.
            notif_parts = [str(error)[:500]]
            if error_type:
                notif_parts.insert(0, f"Type: {error_type}")
            if error_action:
                notif_parts.append(f"During: {error_action}")
            if error_eta:
                notif_parts.append(f"ETA: {error_eta}")
            if recovery_hint:
                notif_parts.append(f"→ {recovery_hint}")

            detail_str = "\n".join(notif_parts)

            ev = {
                "event_type": "cycle_failed",
                "title": "Observation cycle failed",
                "detail": detail_str,
                "priority": 3,
                "action_command": "",
                "action_label": "",
            }
            self._send("cycle_failed", ev, channels=("desktop", "telegram", "slack"))
            return 1

        if outcome == "skipped":
            return 0

        # Get unread events from the feed that arrived after the cycle started.
        # We look at events with priority >= 1 (skip routine events for notifications).
        unread = get_feed(conn, limit=20, min_priority=1, include_dismissed=False)
        if not unread:
            return 0

        sent = 0
        for event in unread:
            ev_dict = {
                "id": event.id,
                "event_type": event.event_type,
                "title": event.title,
                "detail": event.detail,
                "priority": event.priority,
                "action_command": event.action_command,
                "action_label": event.action_label,
                "timestamp": event.timestamp,
            }

            # Find the first matching rule.
            rule = self._match_rule(event.event_type, event.priority)
            if rule is None:
                continue

            # Dedup: if this event type was notified recently, skip it
            # (unless it's a critical error).
            if event.priority < 3:
                recent = count_recent_of_type(conn, event.event_type, hours=rule.cooldown_hours)
                # count_recent_of_type includes the current event, so > 1 means
                # we already notified for this type within the cooldown window.
                if recent > 1:
                    continue

            # Build message and send to each channel.
            title, message = self._render(event.event_type, ev_dict)
            self._send(
                event.event_type, ev_dict,
                channels=rule.channels,
                title=title, message=message,
                play_sound=rule.play_sound,
            )
            sent += 1

        return sent

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _match_rule(self, event_type: str, priority: int) -> NotificationRule | None:
        """Find the first rule that applies to this event."""
        for rule in self.rules:
            if rule.event_types and event_type not in rule.event_types:
                continue
            if priority < rule.min_priority:
                continue
            return rule
        return None

    def _render(self, event_type: str, ev: dict) -> tuple[str, str]:
        """Render a (title, message) pair for an event."""
        tpl = _TEMPLATES.get(event_type, _default_template)
        return tpl(ev)

    def _send(
        self,
        event_type: str,
        ev: dict,
        channels: tuple[str, ...],
        title: str = "",
        message: str = "",
        play_sound: bool = False,
        preferred_channel: str | None = None,
    ) -> None:
        """Send a notification to the specified channels.

        When ``preferred_channel`` is set, only send to that channel (plus
        ``feed`` which is always written regardless). This allows the operator
        to choose where they receive notifications (e.g. Telegram only)
        and have it respected from conversation-learned preferences.

        The fallback order is:
          1. Explicit ``preferred_channel`` parameter (from caller)
          2. ``self._preferred_channel`` (set via ``notify_cycle_events()``)

        Supports sound alerts and click-to-act for actionable events.
        """
        if not title or not message:
            title, message = self._render(event_type, ev)

        action_command, action_label = _get_action_info(ev)

        # Resolve preferred_channel: explicit param > engine-level setting.
        pref = preferred_channel or getattr(self, '_preferred_channel', None)

        # Filter channels by preferred_channel when set.
        # "feed" is always included — it's the ambient event log, not a notification.
        if pref:
            active_channels = ["feed"]
            if pref in channels:
                active_channels.append(pref)
            # Fallback: if preferred channel isn't in the rule's channels,
            # use the original set (the rule explicitly excluded it).
            # This prevents losing notifications for misconfigured preferences.
            if len(active_channels) == 1:
                active_channels = list(channels)
            channels = tuple(active_channels)

        for channel in channels:
            if channel == "desktop":
                _notify_desktop(
                    title, message,
                    action_command=action_command,
                    action_label=action_label,
                )
            elif channel == "telegram":
                _notify_telegram(title, message)
            elif channel == "slack":
                _notify_slack(title, message)
            elif channel == "feed":
                pass  # Already in the feed — no-op

        # Play sound for high-priority notifications that reached desktop/telegram.
        if play_sound and ("desktop" in channels or "telegram" in channels):
            priority = ev.get("priority", 1)
            try:
                _play_sound(priority)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Convenience function — use this from daemon.py
# ---------------------------------------------------------------------------

_NOTIFICATION_ENGINE: NotificationEngine | None = None


def notify_cycle_events(conn, cycle: dict, no_notify: bool = False,
                         preferred_channel: str | None = None) -> int:
    """One-shot convenience: notify for a completed daemon cycle.

    Creates a singleton engine and calls ``notify_cycle()``.

    Args:
        conn: Database connection.
        cycle: The cycle result dict from ``_run_cycle()``.
        no_notify: If True, suppress desktop/telegram notifications.
        preferred_channel: If set, only send notifications to this channel
            (plus feed). Respects ``preferred_channel`` learned from conversation.

    Returns:
        Number of notifications sent.
    """
    global _NOTIFICATION_ENGINE
    if _NOTIFICATION_ENGINE is None:
        _NOTIFICATION_ENGINE = NotificationEngine()
    engine = _NOTIFICATION_ENGINE
    # Temporarily set preferred_channel on the engine so _send() uses it.
    # This avoids changing the notify_cycle() signature which is used elsewhere.
    old_pref = getattr(engine, '_preferred_channel', None)
    engine._preferred_channel = preferred_channel
    try:
        return engine.notify_cycle(conn, cycle, no_notify=no_notify)
    finally:
        engine._preferred_channel = old_pref


# ---------------------------------------------------------------------------
# Proactive Conversation Engine — merged from proactive.py
# ---------------------------------------------------------------------------
# The proactive engine turns daemon discoveries into conversations.
# It analyzes cycle results for significant findings, picks the most
# interesting one, and starts a natural conversation about it.
#
# This was originally in ``proactive.py`` with its own template system.
# It's merged here to eliminate duplicate templates for the same event
# types (new_initiative, skill_drift_detected, etc.) and to share the
# same notification infrastructure. The standalone ``proactive.py`` now
# re-exports ``check_and_proact`` from this module.
# ---------------------------------------------------------------------------


#: Minimum priority for an event to be considered "worth starting a conversation".
_MIN_PROACT_PRIORITY = 2

#: Cooldown hours — don't start a conversation about the same event type twice.
_PROACT_COOLDOWN_HOURS = 24

#: Proactive conversation templates — maps event_type to a natural message
#: template function. These are separate from ``_TEMPLATES`` because they
#: produce conversational prose rather than notification title/message pairs.
_PROACTIVE_TEMPLATES: dict[str, Callable[[int, str], str]] = {}


def _proactive_template(event_type: str):
    """Decorator to register a proactive template function."""
    def deco(fn):
        _PROACTIVE_TEMPLATES[event_type] = fn
        return fn
    return deco


@_proactive_template("new_initiative")
def _pt_initiative(value: int, greeting: str) -> str:
    return (
        f"{greeting}I've been analyzing your workspace and noticed something "
        f"interesting — there's a new engineering initiative that emerged. "
        f"Want me to tell you more about it?"
    )


@_proactive_template("high_severity_suggestion")
def _pt_suggestion(value: int, greeting: str) -> str:
    return (
        f"{greeting}I found a promising integration opportunity between your "
        f"projects that's worth looking at. "
        f"Should I walk you through the details?"
    )


@_proactive_template("skill_drift_detected")
def _pt_drift(value: int, greeting: str) -> str:
    return (
        f"{greeting}heads up — {value} of my automated skills are showing signs "
        f"of degradation. They might need re-formation to stay reliable. "
        f"Want me to check on them?"
    )


@_proactive_template("capability_gap_detected")
def _pt_gap(value: int, greeting: str) -> str:
    return (
        f"{greeting}I noticed some capability gaps in my execution pipeline "
        f"that could affect what I can do for you. "
        f"Should I analyze them?"
    )


@_proactive_template("skill_formed")
def _pt_skill(value: int, greeting: str) -> str:
    return (
        f"{greeting}good news — I've learned {value} new workflow pattern(s) "
        f"from your recent activity and formed them into reusable skills. "
        f"They're ready whenever you need them."
    )


@_proactive_template("cross_project_correlation")
def _pt_correlation(value: int, greeting: str) -> str:
    return (
        f"{greeting}interesting — I found structural overlaps between some of "
        f"your projects that I hadn't noticed before. "
        f"Want me to show you?"
    )


def _get_signal_summary(cycle: dict) -> list[dict]:
    """Extract significant signals from the cycle result dict.

    Returns a list of dicts sorted by priority (most significant first),
    each containing the signal type, priority, and detail for messaging.
    """
    signals: list[dict] = []

    # Map cycle result keys to signal metadata.
    signal_map = [
        # (key, event_type, priority_fn, detail_fn)
        ("new_suggestions", "high_severity_suggestion",
         lambda v: min(2 + (v > 0), 3),
         lambda v, high: f"Friday found {v} cross-project integration suggestion(s) ({high} high-severity)"),
        ("high_severity_suggestions", "high_severity_suggestion",
         lambda v: 3 if v > 0 else 0,
         lambda v, _: f"{v} high-severity integration opportunity(s) detected"),
        ("new_gaps", "capability_gap_detected",
         lambda v: min(2 + (v > 0), 3),
         lambda v, _: f"{v} new capability gap(s) detected in the execution pipeline"),
        ("open_gaps", "capability_gap_detected",
         lambda v: 1 if v > 0 else 0,
         lambda v, _: f"{v} open capability gap(s) remain unresolved"),
        ("new_patterns", "new_pattern",
         lambda v: 1 if v > 0 else 0,
         lambda v, _: f"{v} new action pattern(s) mined from your workflow"),
        ("new_intents", "intent_labeled",
         lambda v: 1 if v > 0 else 0,
         lambda v, _: f"{v} new workflow intent(s) recognized and labeled"),
        ("new_skills", "skill_formed",
         lambda v: 2 if v > 0 else 0,
         lambda v, _: f"{v} new skill(s) formed from your workflow patterns"),
        ("drifted_skills", "skill_drift_detected",
         lambda v: 3 if v > 0 else 0,
         lambda v, _: f"{v} skill(s) are degrading and may need attention"),
        ("new_correlations", "cross_project_correlation",
         lambda v: 1 if v > 0 else 0,
         lambda v, _: f"{v} cross-project correlation(s) detected between repositories"),
    ]

    for key, event_type, priority_fn, detail_fn in signal_map:
        value = cycle.get(key, 0)
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        priority = priority_fn(value)
        if priority < _MIN_PROACT_PRIORITY:
            continue
        high_sev = cycle.get("high_severity_suggestions", 0)
        signals.append({
            "event_type": event_type,
            "priority": priority,
            "key": key,
            "value": value,
            "detail": detail_fn(value, high_sev),
        })

    # Sort by priority descending.
    signals.sort(key=lambda s: s["priority"], reverse=True)
    return signals


def _has_proacted_recently(conn, event_type: str) -> bool:
    """Check if we already started a proactive conversation about this type."""
    from datetime import datetime, timezone, timedelta
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=_PROACT_COOLDOWN_HOURS)).isoformat()
        row = conn.execute(
            "SELECT id FROM ambient_feed "
            "WHERE event_type = 'proactive_insight' "
            "AND detail LIKE ? "
            "AND timestamp >= ? "
            "LIMIT 1",
            (f"%{event_type}%", cutoff),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _get_operator_name(conn) -> str:
    """Get the operator's name from their profile, if known."""
    try:
        from .operator import build_operator_profile
        profile = build_operator_profile(conn)
        return profile.explicit_preferences.get("name", "")
    except Exception:
        return ""


def _build_conversation_message(signal: dict, operator_name: str) -> str:
    """Build a natural conversational message for the signal.

    Uses the LLM if available to make the message sound natural and contextual.
    Falls back to a registered proactive template (or a generic template)
    if the LLM is unavailable.
    """
    event_type = signal["event_type"]
    detail = signal["detail"]
    value = signal["value"]
    greeting = f"Hey {operator_name}, " if operator_name else ""

    # Try LLM for a natural message if available.
    try:
        from .services.llm import _call, _enabled
        if _enabled():
            system = (
                "You are Friday, an AI operating partner. You need to tell the "
                "operator about something interesting you've discovered. Keep it "
                "conversational, natural, and concise (1-3 sentences). Use their "
                f"name ({operator_name}) if you know it. Don't be robotic. "
                "Never say 'I have detected' or 'I have found' — just say it "
                "like a partner would. Do not suggest CLI commands or actions — "
                "just share what you noticed naturally."
            )
            user = (
                f"Tell the operator about this finding naturally:\n"
                f"Type: {event_type}\n"
                f"Detail: {detail}\n"
                f"The operator's name is: {operator_name if operator_name else '(unknown)'}\n\n"
                f"Keep it to 1-3 short sentences. Make it sound like you're "
                f"telling them something interesting, not announcing a system event."
            )
            llm_response = _call(system, user)
            if llm_response and len(llm_response.strip()) > 10:
                return llm_response.strip()
    except Exception:
        pass

    # Fallback: registered proactive template
    tpl = _PROACTIVE_TEMPLATES.get(event_type)
    if tpl:
        return tpl(value, greeting)

    # Generic fallback.
    return (
        f"{greeting}I noticed something worth mentioning: {detail}. "
        f"Want me to look into it?"
    )


def check_and_proact(conn, cycle: dict) -> dict:
    """Analyze a daemon cycle and proactively start a conversation if warranted.

    This is the main entry point, called from ``_do_cycle()`` after notifications.

    Args:
        conn: Database connection.
        cycle: The cycle result dict from ``_run_cycle()``.

    Returns:
        A dict with proactive result info (for logging):
            signaled: whether anything worth being proactive about was found
            event_type: the type of signal that triggered (or empty)
            message: the message sent (or empty)
    """
    result: dict = {
        "signaled": False,
        "event_type": "",
        "message": "",
    }

    # 1. Find significant signals from this cycle.
    signals = _get_signal_summary(cycle)
    if not signals:
        return result

    # 2. Pick the most significant one.
    top = signals[0]

    # 3. Skip if we already proacted about this type recently.
    if _has_proacted_recently(conn, top["event_type"]):
        return result

    # 4. Presence gate: don't interrupt if the operator is in a restrictive state.
    try:
        from .presence import should_interrupt, get_current_state, enqueue_deferred_interrupt
        state, focus_active = get_current_state(conn)

        if not should_interrupt(state, top["priority"]):
            # Defer the interrupt instead of dropping it
            enqueue_deferred_interrupt(
                conn, top["event_type"],
                f"{top['detail']} — {top.get('value', '')}",
                top["priority"],
                state,
            )
            return result
    except Exception:
        pass

    # 5. Build a conversational message.
    operator_name = _get_operator_name(conn)
    message = _build_conversation_message(top, operator_name)
    result["signaled"] = True
    result["event_type"] = top["event_type"]
    result["message"] = message

    # 5. Push to ambient feed as a proactive insight event.
    try:
        from .ambient import push_event, AmbientEvent
        from datetime import datetime, timezone
        ev = AmbientEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="proactive_insight",
            title=message[:80],
            detail=message,
            source="daemon",
            priority=top["priority"],
            category="intelligence",
        )
        push_event(conn, ev, dedup_hours=_PROACT_COOLDOWN_HOURS)
    except Exception:
        pass

    # 6. Log to conversation_log as a Friday-initiated message.
    try:
        from .db import log_exchange
        log_exchange(
            conn,
            channel="proactive",
            channel_id="proactive:daemon",
            user_message="",
            friday_reply=message,
            routing="proactive",
        )
    except Exception:
        pass

    # 7. Register as pending proactive — the operator can reply naturally
    # via Telegram/Slack/CLI and their response will be routed through the
    # ProactiveReplyHandler in IdentityEngine.process().
    try:
        from .proactive_reply import register_pending
        channel = cycle.get("_preferred_channel", "proactive")
        register_pending(top["event_type"], channel, message)
    except Exception:
        pass

    return result
