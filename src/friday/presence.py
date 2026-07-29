"""PresenceDetector — determines the operator's current state from ambient signals.

Answers one question: **What's the operator's current state?**

State machine:
    AWAY | DESK_IDLE | DESK_ACTIVE | IN_MEETING | DEEP_FOCUS | SLEEPING

Signals (pulled from existing observers):
    - Hyprland heartbeat (last active timestamp) — desk presence
    - Keyboard/mouse idle time — desk activity
    - Calendar observer — current/future events (in_meeting)
    - Git/terminal activity recency — deep focus vs idle
    - Time of day + activity hours over last N days — sleeping vs awake

Design:
    - Deterministic — no LLM for state classification
    - Every signal is optional — graceful degradation if an observer is missing
    - State is a function of the last N minutes, not instant (smoothing to avoid flicker)
    - State changes are persisted as observation facts
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Presence states
# ---------------------------------------------------------------------------


class PresenceState(str, Enum):
    """The operator's current presence state — mutually exclusive."""

    AWAY = "away"
    DESK_IDLE = "desk_idle"
    DESK_ACTIVE = "desk_active"
    IN_MEETING = "in_meeting"
    DEEP_FOCUS = "deep_focus"
    SLEEPING = "sleeping"


# ---------------------------------------------------------------------------
# Presence signals — raw inputs from observers
# ---------------------------------------------------------------------------


@dataclass
class PresenceSignals:
    """Raw signals collected from all available observers.

    Every field is Optional — each signal source can be missing without
    breaking presence detection. The PresenceDetector fuses available
    signals deterministically.
    """

    # Hyprland/desktop: when was the last user interaction?
    last_active_at: Optional[str] = None  # ISO timestamp
    idle_seconds: Optional[int] = None  # keyboard/mouse idle time

    # Calendar: is there a current or upcoming event?
    current_event_title: Optional[str] = None  # title of current/upcoming event
    current_event_minutes_remaining: Optional[int] = None
    has_upcoming_event: bool = False
    next_event_minutes_away: Optional[int] = None

    # Git/terminal activity: is the operator actively coding?
    last_git_commit_at: Optional[str] = None
    git_activity_minutes_ago: Optional[int] = None
    terminal_active_seconds_ago: Optional[int] = None

    # Time context
    local_hour: int = 0  # 0-23
    is_weekend: bool = False

    # Activity history (from relationship metrics or observation)
    typical_wake_hour: int = 7  # when the operator typically starts work
    typical_sleep_hour: int = 23  # when the operator typically stops

    # Learned focus windows — times when the operator is most productive
    # (from operator profile or relationship metrics)
    learned_focus_windows: list[tuple[int, int]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PresenceDetector — deterministic state machine
# ---------------------------------------------------------------------------


class PresenceDetector:
    """Deterministic presence state machine.

    Collects available signals and fuses them into a single presence state.
    Uses smoothing to avoid flicker between states on brief transitions.

    Usage::

        detector = PresenceDetector()
        signals = detector.collect_signals(conn)
        state = detector.determine_state(signals)
        # → state is a PresenceState enum value
    """

    # Thresholds (seconds)
    AWAY_TIMEOUT = 900  # 15 min of no heartbeat → AWAY
    DESK_IDLE_TIMEOUT = 300  # 5 min of no input → DESK_IDLE
    DEEP_FOCUS_IDLE_MAX = 120  # max idle seconds while still considered "deep focus"
    SLEEPING_HOUR_THRESHOLD = 6  # if it's past typical sleep hour + this, assume sleeping
    IN_MEETING_IDLE_MAX = 1800  # max idle before we stop assuming "in meeting"

    # Smoothing: minimum time in a state before we report a change (seconds)
    MIN_STATE_DURATION = 60  # 1 minute minimum to avoid flicker

    # Currently-reported state (for smoothing)
    _current_state: PresenceState = PresenceState.AWAY
    _state_changed_at: Optional[datetime] = None

    def collect_signals(self, conn=None) -> PresenceSignals:
        """Collect raw signals from all available sources.

        Every signal source is best-effort — missing sources produce None
        without raising. This ensures graceful degradation on any platform.
        """
        signals = PresenceSignals(
            local_hour=datetime.now(timezone.utc).hour,
            is_weekend=datetime.now(timezone.utc).weekday() >= 5,
        )

        # 1. Hyprland/desktop presence via screen module
        try:
            from .screen import collect_screen_context

            ctx = collect_screen_context(include_ocr=False, include_clipboard=False)
            if ctx:
                # If we have screen context, the user is at the desk
                signals.last_active_at = (
                    ctx.captured_at if hasattr(ctx, "captured_at") else None
                )
                # Idle time is approximated by process inactiveness
                if not ctx.active_window_process:
                    signals.idle_seconds = self.AWAY_TIMEOUT
                elif ctx.active_window_process:
                    signals.idle_seconds = 0
        except Exception:
            pass

        # 2. Active app detection via working memory (written by daemon's screen_aware stage)
        if conn is not None:
            try:
                from .memory import WorkingMemory

                wm = WorkingMemory(conn)
                active = wm.get_context("active_app")
                if active:
                    signals.last_active_at = datetime.now(timezone.utc).isoformat()
                    signals.idle_seconds = 0

                # Check git activity timeline entries
                clipboard = wm.get_context("clipboard")
                if clipboard:
                    # Recent clipboard activity = user is active
                    if signals.idle_seconds is None or signals.idle_seconds > 30:
                        signals.idle_seconds = min(
                            signals.idle_seconds or 60, 30
                        )
            except Exception:
                pass

        # 3. Calendar observer — check working memory for current event context
        # (posted by the calendar observer during daemon cycles)
        if conn is not None:
            try:
                from .memory import WorkingMemory
                wm = WorkingMemory(conn)
                cal_ctx = wm.get_context("calendar_current_event")
                if cal_ctx:
                    signals.current_event_title = str(cal_ctx)[:80]
                    signals.has_upcoming_event = True

                # Also check observations table for recent calendar entries
                rows = conn.execute(
                    "SELECT value FROM observations WHERE source = 'calendar_observer' "
                    "AND aspect = 'title' ORDER BY observed_at DESC LIMIT 1"
                ).fetchall()
                if rows:
                    val = rows[0]["value"]
                    if val:
                        signals.current_event_title = val[:80]
                        signals.has_upcoming_event = True
            except Exception:
                pass

        # 4. Git activity from last commit timestamps
        if conn is not None:
            try:
                last_commit = conn.execute(
                    "SELECT MAX(last_commit_date) AS last FROM repositories "
                    "WHERE last_commit_date IS NOT NULL"
                ).fetchone()
                if last_commit and last_commit["last"]:
                    last = last_commit["last"]
                    signals.last_git_commit_at = last
                    try:
                        commit_dt = dt.datetime.fromisoformat(last[:19])
                        now_utc = datetime.now(timezone.utc)
                        mins_ago = int((now_utc - commit_dt).total_seconds() / 60)
                        signals.git_activity_minutes_ago = max(0, mins_ago)
                    except Exception:
                        pass
            except Exception:
                pass

        # 5. Learned focus windows from operator profile
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT value FROM operator_preferences WHERE key = 'focus_windows'"
                ).fetchall()
                if rows:
                    import json

                    windows = json.loads(rows[0]["value"])
                    if isinstance(windows, list):
                        parsed = []
                        for w in windows:
                            if isinstance(w, (list, tuple)) and len(w) == 2:
                                parsed.append((int(w[0]), int(w[1])))
                        if parsed:
                            signals.learned_focus_windows = parsed
            except Exception:
                pass

            try:
                row = conn.execute(
                    "SELECT value FROM operator_preferences WHERE key = 'wake_hour'"
                ).fetchone()
                if row:
                    signals.typical_wake_hour = int(row["value"])
            except Exception:
                pass

            try:
                row = conn.execute(
                    "SELECT value FROM operator_preferences WHERE key = 'sleep_hour'"
                ).fetchone()
                if row:
                    signals.typical_sleep_hour = int(row["value"])
            except Exception:
                pass

        return signals

    def determine_state(self, signals: PresenceSignals) -> PresenceState:
        """Fuse raw signals into a deterministic presence state.

        Priority order (first match wins):
            1. SLEEPING — if it's the middle of the night and no recent activity
            2. AWAY — if no heartbeat for 15+ minutes
            3. IN_MEETING — if a calendar event is active
            4. DEEP_FOCUS — if the user is actively coding with recent git/terminal activity
            5. DESK_ACTIVE — if at desk with recent input
            6. DESK_IDLE — if at desk but idle for 5+ minutes
            7. AWAY — fallback
        """
        now_utc = datetime.now(timezone.utc)
        hour = signals.local_hour or now_utc.hour

        # ── 1. SLEEPING check ──────────────────────────────────────
        # If it's past typical sleep hour by 2+ hours, and we haven't
        # seen activity in the last 30 min, assume sleeping.
        sleep_hour = signals.typical_sleep_hour
        wake_hour = signals.typical_wake_hour

        if sleep_hour > 0:
            # Handle overnight: sleep_hour=23, wake_hour=7
            is_late_night = hour >= sleep_hour or hour < wake_hour
            no_recent_activity = (signals.idle_seconds is not None
                                  and signals.idle_seconds > 1800)
            if is_late_night and no_recent_activity:
                return PresenceState.SLEEPING
            if is_late_night and signals.last_active_at is None:
                # Midnight check — if it's 2am and no activity seen, likely sleeping
                if hour >= 1 and hour < wake_hour:
                    return PresenceState.SLEEPING

        # ── 2. AWAY check ──────────────────────────────────────────
        idle = signals.idle_seconds
        if idle is not None and idle >= self.AWAY_TIMEOUT:
            return PresenceState.AWAY

        # No signals at all → AWAY
        if (idle is None
                and signals.last_active_at is None
                and signals.current_event_title is None):
            return PresenceState.AWAY

        # ── 3. IN_MEETING check ────────────────────────────────────
        meeting = signals.current_event_title
        # If a calendar event is active, we're "in meeting" even during
        # brief idle periods (you might be listening).
        if meeting:
            # Only override if idle isn't excessive (still at desk)
            if idle is None or idle < self.IN_MEETING_IDLE_MAX:
                return PresenceState.IN_MEETING

        # ── 4. DEEP_FOCUS check ────────────────────────────────────
        # Heavy coding = git commits + low idle time + recent terminal
        is_recently_coding = (
            (signals.git_activity_minutes_ago is not None
             and signals.git_activity_minutes_ago < 60)
            and (idle is not None and idle < self.DEEP_FOCUS_IDLE_MAX)
        )
        if is_recently_coding:
            # Double-check: are we in a learned focus window?
            if signals.learned_focus_windows:
                in_focus_window = any(
                    start <= hour <= end
                    for start, end in signals.learned_focus_windows
                )
                if in_focus_window:
                    return PresenceState.DEEP_FOCUS
                # If not in a focus window, still deep focus if actively coding
                # (focus windows are bonus strengthening, not a hard gate)
                return PresenceState.DEEP_FOCUS
            return PresenceState.DEEP_FOCUS

        # ── 5. DESK_ACTIVE check ───────────────────────────────────
        if idle is not None and idle < self.DESK_IDLE_TIMEOUT:
            return PresenceState.DESK_ACTIVE

        if signals.last_active_at is not None and idle is None:
            return PresenceState.DESK_ACTIVE

        # ── 6. DESK_IDLE check ─────────────────────────────────────
        if idle is not None and idle >= self.DESK_IDLE_TIMEOUT:
            return PresenceState.DESK_IDLE

        # ── 7. Fallback ────────────────────────────────────────────
        if idle is not None:
            return PresenceState.DESK_IDLE
        return PresenceState.AWAY

    def smooth_state(self, new_state: PresenceState) -> PresenceState:
        """Apply smoothing to prevent flicker on brief transitions.

        Only transitions to a new state if we've been in the current state
        for at least MIN_STATE_DURATION. This prevents rapid back-and-forth
        when signals are ambiguous.
        """
        now = datetime.now(timezone.utc)

        if new_state == self._current_state:
            return self._current_state

        # If we haven't set a change time yet, set it now
        if self._state_changed_at is None:
            self._state_changed_at = now
            return self._current_state

        # If we're considering a new state, check elapsed time
        elapsed = (now - self._state_changed_at).total_seconds()
        if elapsed < self.MIN_STATE_DURATION:
            # Too soon to change — stay in current state
            return self._current_state

        # Stable enough — transition
        self._current_state = new_state
        self._state_changed_at = now
        return new_state

    def detect(self, conn=None) -> PresenceState:
        """One-shot: collect signals, determine state, apply smoothing.

        Returns the operator's current presence state.

        This is the main entry point. Call it from the daemon cycle to
        get the current state.
        """
        signals = self.collect_signals(conn)
        raw_state = self.determine_state(signals)
        return self.smooth_state(raw_state)


# ---------------------------------------------------------------------------
# Attention levels — maps presence state to interrupt permeability
# ---------------------------------------------------------------------------


class AttentionLevel(int, Enum):
    """How permeable the operator is to interruptions.

    Higher number = more interruptions pass through.
    """

    NONE = 0  # SLEEPING — nothing passes
    MINIMAL = 1  # AWAY, DEEP_FOCUS — only urgent
    LOW = 2  # IN_MEETING — urgent + queued for later
    MODERATE = 3  # DESK_IDLE — urgent + important
    HIGH = 4  # DESK_ACTIVE — everything


#: Mapping from presence state to attention level.
#: Urgent (pri 3) always passes. These levels gate priority 2 and below.
_ATTENTION_MAP: dict[PresenceState, AttentionLevel] = {
    PresenceState.SLEEPING: AttentionLevel.NONE,
    PresenceState.AWAY: AttentionLevel.MINIMAL,
    PresenceState.DEEP_FOCUS: AttentionLevel.MINIMAL,
    PresenceState.IN_MEETING: AttentionLevel.LOW,
    PresenceState.DESK_IDLE: AttentionLevel.MODERATE,
    PresenceState.DESK_ACTIVE: AttentionLevel.HIGH,
}


def attention_for_state(state: PresenceState) -> AttentionLevel:
    """Return the attention level for a given presence state."""
    return _ATTENTION_MAP.get(state, AttentionLevel.MODERATE)


# ---------------------------------------------------------------------------
# Interrupt priority gating
# ---------------------------------------------------------------------------


def should_interrupt(
    state: PresenceState,
    event_priority: int,
) -> bool:
    """Determine whether an event of a given priority should interrupt now.

    Args:
        state: Current presence state.
        event_priority: Priority of the event (0-3).
            3 = urgent (kill switch, build failure, security)
            2 = important (drift, capability gaps, high-value suggestions)
            1 = normal (new patterns, insights, learnings)
            0 = routine (cycle complete, knowledge updated)

    Returns:
        True if the event should be delivered immediately.
        False if it should be deferred.
    """
    attention = attention_for_state(state)

    # Urgent (priority 3) always passes regardless of state.
    if event_priority >= 3:
        return True

    # Priority 2 passes only when attention >= MODERATE
    if event_priority == 2:
        return attention >= AttentionLevel.MODERATE

    # Priority 1 passes only when attention >= HIGH
    if event_priority == 1:
        return attention >= AttentionLevel.HIGH

    # Priority 0 (routine) never interrupts
    return False


# ---------------------------------------------------------------------------
# Deferred interrupt queue
# ---------------------------------------------------------------------------


def enqueue_deferred_interrupt(
    conn,
    event_type: str,
    message: str,
    priority: int,
    state: PresenceState,
) -> int:
    """Store an event in the deferred_interrupts table for later delivery.

    Returns the row id, or 0 on failure.
    """
    from .db import now_iso

    try:
        cur = conn.execute(
            """INSERT INTO deferred_interrupts
               (event_type, priority, message, state_at_creation, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, priority, message, state.value, now_iso()),
        )
        conn.commit()
        return cur.lastrowid or 0
    except Exception:
        return 0


def deliver_pending_interrupts(
    conn,
    current_state: PresenceState,
    max_per_cycle: int = 3,
) -> list[dict]:
    """Deliver pending deferred interrupts that are now appropriate.

    When presence transitions to a more permissive state, check the
    deferred_interrupts table and deliver the highest-priority pending item.

    Args:
        conn: Database connection.
        current_state: Current presence state.
        max_per_cycle: Maximum number of interrupts to deliver in one call.

    Returns:
        List of delivered interrupt dicts with event_type, message, priority.
    """
    delivered: list[dict] = []

    try:
        # Get pending interrupts ordered by priority desc, then oldest first
        rows = conn.execute(
            "SELECT id, event_type, priority, message FROM deferred_interrupts "
            "WHERE delivered_at IS NULL ORDER BY priority DESC, created_at ASC "
            f"LIMIT {max_per_cycle}"
        ).fetchall()

        for row in rows:
            if not should_interrupt(current_state, row["priority"]):
                continue  # Still shouldn't interrupt — skip

            # Mark as delivered
            from .db import now_iso

            conn.execute(
                "UPDATE deferred_interrupts SET delivered_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )

            delivered.append({
                "event_type": row["event_type"],
                "message": row["message"],
                "priority": row["priority"],
                "id": row["id"],
            })

        if delivered:
            conn.commit()

    except Exception:
        pass

    return delivered


def get_pending_interrupts_count(conn) -> int:
    """Count how many deferred interrupts are waiting."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM deferred_interrupts WHERE delivered_at IS NULL"
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception:
        return 0


def format_state(state: PresenceState) -> str:
    """Return a human-readable label for a presence state."""
    labels = {
        PresenceState.AWAY: "Away",
        PresenceState.DESK_IDLE: "At desk (idle)",
        PresenceState.DESK_ACTIVE: "At desk (active)",
        PresenceState.IN_MEETING: "In a meeting",
        PresenceState.DEEP_FOCUS: "Deep focus",
        PresenceState.SLEEPING: "Sleeping",
    }
    return labels.get(state, state.value.replace("_", " ").title())


def detect_and_persist(conn) -> tuple[PresenceState, bool]:
    """Collect signals, detect presence state, persist to DB, return (state, changed).

    Persists the current state as an operator preference so the CLI and
    dashboard can read it. Also persists to working memory for fast access.

    Returns:
        Tuple of (current_state, changed) where changed is True if the
        state has changed since the last cycle.
    """
    detector = PresenceDetector()
    state = detector.detect(conn)
    changed = False

    try:
        # Read last known state
        from .db import now_iso

        last_state_row = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = 'presence_state'"
        ).fetchone()
        last_state = last_state_row["value"] if last_state_row else ""

        changed = last_state != state.value

        if changed:
            # Persist new state
            conn.execute(
                "INSERT OR REPLACE INTO operator_preferences (key, value, source, updated_at) "
                "VALUES ('presence_state', ?, 'system', ?)",
                (state.value, now_iso()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO operator_preferences (key, value, source, updated_at) "
                "VALUES ('presence_state_changed_at', ?, 'system', ?)",
                (now_iso(), now_iso()),
            )
            conn.commit()

            # Store in working memory for fast CLI access
            try:
                from .memory import WorkingMemory

                wm = WorkingMemory(conn)
                wm.set_context(
                    "presence_state",
                    state.value,
                    category="presence",
                    source="system",
                    priority=3,
                    ttl_seconds=3600,
                    context=f"Presence: {format_state(state)}",
                )
            except Exception:
                pass

    except Exception:
        pass

    return state, changed


# ---------------------------------------------------------------------------
# Focus mode (manual override)
# ---------------------------------------------------------------------------


def set_focus_mode(conn, duration_minutes: int) -> str:
    """Enable manual focus mode for N minutes.

    During focus mode, only urgent (priority 3) interrupts pass through.
    After the timer expires, normal presence detection resumes.

    Returns a human-readable confirmation message.
    """
    from .db import now_iso

    try:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        ).isoformat()

        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, source, updated_at) "
            "VALUES ('focus_mode', 'true', 'explicit', ?)",
            (now_iso(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, source, updated_at) "
            "VALUES ('focus_expires_at', ?, 'explicit', ?)",
            (expires_at, now_iso()),
        )
        conn.commit()

        # Persist state as deep_focus while in focus mode
        detect_and_persist(conn)

        return (
            f"🔇 Focus mode enabled for {duration_minutes} minutes. "
            f"Only urgent items will interrupt. Auto-cancels at "
            f"{(datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).strftime('%H:%M')} UTC."
        )
    except Exception as exc:
        return f"Could not enable focus mode: {exc}"


def disable_focus_mode(conn) -> str:
    """Disable manual focus mode and resume normal presence detection."""
    from .db import now_iso

    try:
        conn.execute(
            "DELETE FROM operator_preferences WHERE key = 'focus_mode'"
        )
        conn.execute(
            "DELETE FROM operator_preferences WHERE key = 'focus_expires_at'"
        )
        conn.commit()

        # Re-evaluate presence state without focus override
        detect_and_persist(conn)

        return "🔊 Focus mode disabled. Normal interrupt behavior restored."
    except Exception as exc:
        return f"Could not disable focus mode: {exc}"


def is_focus_mode(conn) -> bool:
    """Check if focus mode is currently active (not expired)."""
    try:
        row = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = 'focus_mode'"
        ).fetchone()
        if not row or row["value"].lower() != "true":
            return False

        # Check expiration
        expires = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = 'focus_expires_at'"
        ).fetchone()
        if expires:
            try:
                expires_dt = datetime.fromisoformat(expires["value"])
                if datetime.now(timezone.utc) > expires_dt:
                    # Expired — clean up
                    conn.execute(
                        "DELETE FROM operator_preferences WHERE key = 'focus_mode'"
                    )
                    conn.execute(
                        "DELETE FROM operator_preferences WHERE key = 'focus_expires_at'"
                    )
                    conn.commit()
                    return False
            except Exception:
                pass

        return True
    except Exception:
        return False


def get_current_state(conn) -> tuple[PresenceState, bool]:
    """Get the current presence state and whether focus mode is active.

    Returns:
        Tuple of (state, focus_active).
    """
    focus_active = is_focus_mode(conn)
    if focus_active:
        return PresenceState.DEEP_FOCUS, True

    try:
        detector = PresenceDetector()
        state = detector.detect(conn)
        return state, False
    except Exception:
        return PresenceState.AWAY, False
