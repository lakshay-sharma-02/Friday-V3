"""Confirm-before-execute safety gate (Pillar A).

Action-layer workers (Hyprland, browser automation, native GUI) are the first
workers whose *mistake* has a real-world side effect outside Friday's sandbox:
sending a wrong message, closing the wrong window, launching the wrong app is not
the same failure class as a broken shell worker.

This gate enforces graduated autonomy based on TWO axes:
  - **Reversibility**: can this action be undone?
  - **Blast radius**: how much surface does this touch?

The combination determines the required confirmation level:
  +-----------------+--------+--------+-------+----------+
  |                 | NARROW | MEDIUM | WIDE  | CRITICAL |
  +-----------------+--------+--------+-------+----------+
  | REVERSIBLE      | AUTO   | NOTIFY | CONFIRM | DOUBLE |
  | IRREVERSIBLE    | NOTIFY | CONFIRM | DOUBLE | DOUBLE |
  +-----------------+--------+--------+-------+----------+

Four confirmation levels:
  - AUTO:            Read-only; execute silently, no notification.
  - NOTIFY:          Execute immediately, then notify the operator of what
                     was done ("execute-and-notify").
  - CONFIRM:         State-changing; requires y/n before execution.
  - DOUBLE_CONFIRM:  Destructive; requires two y/n prompts.

The gate is called by the executor dispatch path (resolve_executor) and by any
CLI command that invokes an action worker. It checks the worker's capabilities
and the specific action to determine the required confirmation level.

Usage:
  from .confirm_gate import confirm_action, ActionLevel
  
  level = confirm_action(worker_id, action_type)
  if level == ActionLevel.CONFIRM:
      # Prompt user for y/n
  elif level == ActionLevel.DOUBLE_CONFIRM:
      # Prompt twice
  # ActionLevel.AUTO or ActionLevel.NOTIFY: no prompt needed
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ActionLevel(Enum):
    """How much confirmation an action needs before executing.

    Four tiers ordered from least to most restrictive:
      AUTO   → execute silently, no notification
      NOTIFY → execute immediately, notify operator
      CONFIRM → prompt for y/n
      DOUBLE → prompt twice for destructive actions
    """

    AUTO = "auto"                # Read-only: no confirmation needed
    NOTIFY = "notify"            # Execute-and-notify: do it, tell the operator
    CONFIRM = "confirm"          # State-changing: requires y/n
    DOUBLE_CONFIRM = "double"    # Destructive: requires two y/n prompts


class Reversibility(Enum):
    """Whether an action can be undone after execution."""

    REVERSIBLE = "reversible"      # Can be undone (switch workspace back)
    IRREVERSIBLE = "irreversible"  # Cannot be undone (close window, kill)


class BlastRadius(Enum):
    """How much surface an action touches when executed.

    NARROW   → one element (focus a window, query state)
    MEDIUM   → one subsystem (switch workspace, launch app)
    WIDE     → multiple subsystems (kill, restart)
    CRITICAL → everything (exit compositor, deploy to prod)
    """

    NARROW = "narrow"
    MEDIUM = "medium"
    WIDE = "wide"
    CRITICAL = "critical"


class ObservationConfidence(Enum):
    """How much observed evidence Friday has about an action's reliability.

    This is the third axis in the autonomy classifier:
      - Severity (reversibility × blast_radius): how bad would failure be?
      - Probability (observation_confidence): how likely is failure?

    Combined: Risk = Severity × Probability → ActionLevel

    HIGH   → 10+ successful observations of this action type with similar target
             pattern. The action has proven reliable in practice.
    MEDIUM → 3-9 successful observations, or the action type is known but the
             specific target is unfamiliar.
    LOW    → < 3 observations. Novel action or target — treat with caution.
    NONE   → No observed execution history at all. Conservatively treated as LOW.
    """

    HIGH = "high"        # 10+ successful observations
    MEDIUM = "medium"    # 3-9 successful observations
    LOW = "low"          # < 3 observations
    NONE = "none"        # No observation history (treated as LOW)


# Mapping from (reversibility, blast_radius) → ActionLevel based on the
# graduated autonomy matrix described in the module docstring.
# This is the BASE level before observation confidence is factored in.
_AXIS_TO_LEVEL: dict[tuple[Reversibility, BlastRadius], ActionLevel] = {
    (Reversibility.REVERSIBLE, BlastRadius.NARROW):   ActionLevel.AUTO,
    (Reversibility.REVERSIBLE, BlastRadius.MEDIUM):   ActionLevel.NOTIFY,
    (Reversibility.REVERSIBLE, BlastRadius.WIDE):     ActionLevel.CONFIRM,
    (Reversibility.REVERSIBLE, BlastRadius.CRITICAL): ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.NARROW):   ActionLevel.CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM):   ActionLevel.CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.WIDE):     ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.CRITICAL): ActionLevel.DOUBLE_CONFIRM,
}

# Mapping from (reversibility, blast_radius, observation_confidence) → ActionLevel.
# The third axis modulates the base level:
#   - HIGH observation confidence: actions proven reliable → level may drop 1 tier
#     (e.g. NOTIFY → AUTO for well-known safe actions)
#   - MEDIUM observation confidence: base level unchanged
#   - LOW/NONE observation confidence: novel/unfamiliar → level bumps up 1 tier
#     (e.g. AUTO → NOTIFY, NOTIFY → CONFIRM, CONFIRM → DOUBLE)
#
# This implements the risk formula: Risk = Severity × Probability
# where severity is the base (reversibility × blast_radius) and probability
# is the observation confidence.
_AXIS_TO_LEVEL_3D: dict[
    tuple[Reversibility, BlastRadius, ObservationConfidence],
    ActionLevel,
] = {
    # ── HIGH observation confidence: actions proven reliable ──
    # Level may drop 1 tier from base (or stay if already at AUTO).
    (Reversibility.REVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.HIGH):   ActionLevel.AUTO,
    (Reversibility.REVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.HIGH):   ActionLevel.AUTO,    # dropped from NOTIFY
    (Reversibility.REVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.HIGH):   ActionLevel.NOTIFY,  # dropped from CONFIRM
    (Reversibility.REVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.HIGH):   ActionLevel.CONFIRM, # dropped from DOUBLE
    (Reversibility.IRREVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.HIGH): ActionLevel.NOTIFY,  # dropped from CONFIRM
    (Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.HIGH): ActionLevel.CONFIRM, # dropped from CONFIRM (stays)
    (Reversibility.IRREVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.HIGH): ActionLevel.DOUBLE_CONFIRM, # stays
    (Reversibility.IRREVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.HIGH): ActionLevel.DOUBLE_CONFIRM, # stays

    # ── MEDIUM observation confidence: base level unchanged ──
    (Reversibility.REVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.MEDIUM): ActionLevel.AUTO,
    (Reversibility.REVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.MEDIUM): ActionLevel.NOTIFY,
    (Reversibility.REVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.MEDIUM): ActionLevel.CONFIRM,
    (Reversibility.REVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.MEDIUM): ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.MEDIUM): ActionLevel.CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.MEDIUM): ActionLevel.CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.MEDIUM): ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.MEDIUM): ActionLevel.DOUBLE_CONFIRM,

    # ── LOW or NONE observation confidence: novel actions → cautious ──
    # Level bumps up 1 tier from base (or stays at DOUBLE).
    (Reversibility.REVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.LOW):    ActionLevel.NOTIFY,   # bumped from AUTO
    (Reversibility.REVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.LOW):    ActionLevel.CONFIRM,   # bumped from NOTIFY
    (Reversibility.REVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.LOW):    ActionLevel.DOUBLE_CONFIRM, # bumped from CONFIRM
    (Reversibility.REVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.LOW):    ActionLevel.DOUBLE_CONFIRM, # stays
    (Reversibility.IRREVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.LOW):  ActionLevel.DOUBLE_CONFIRM, # bumped from CONFIRM
    (Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.LOW):  ActionLevel.DOUBLE_CONFIRM, # bumped from CONFIRM
    (Reversibility.IRREVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.LOW):  ActionLevel.DOUBLE_CONFIRM, # stays
    (Reversibility.IRREVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.LOW):  ActionLevel.DOUBLE_CONFIRM, # stays

    # NONE confidence is treated identically to LOW.
    (Reversibility.REVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.NONE):   ActionLevel.NOTIFY,
    (Reversibility.REVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.NONE):   ActionLevel.CONFIRM,
    (Reversibility.REVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.NONE):   ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.REVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.NONE):   ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.NARROW,   ObservationConfidence.NONE): ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM,   ObservationConfidence.NONE): ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.WIDE,     ObservationConfidence.NONE): ActionLevel.DOUBLE_CONFIRM,
    (Reversibility.IRREVERSIBLE, BlastRadius.CRITICAL, ObservationConfidence.NONE): ActionLevel.DOUBLE_CONFIRM,
}


@dataclass(frozen=True)
class ActionClassification:
    """Three-axis classification of an action type.

    Every known action type gets a (reversibility, blast_radius) pair that
    determines its default confirmation level. The third axis — observation
    confidence — is resolved dynamically from the action log at classification
    time via ``lookup_observation_confidence()``.

    Unknown actions default to ``(IRREVERSIBLE, MEDIUM)`` — the safest
    conservative guess for unexpected action types.
    """

    reversibility: Reversibility
    blast_radius: BlastRadius
    observation_confidence: ObservationConfidence = ObservationConfidence.NONE

    def resolve_level(self) -> ActionLevel:
        """Resolve this classification to an ActionLevel.

        Uses the three-axis matrix (reversibility × blast_radius ×
        observation_confidence). Falls back to the two-axis base level
        if the specific 3D combination isn't defined (shouldn't happen
        since the matrix is exhaustive).
        """
        three_d = _AXIS_TO_LEVEL_3D.get(
            (self.reversibility, self.blast_radius, self.observation_confidence),
        )
        if three_d is not None:
            return three_d
        # Fallback to two-axis base level (no observation modulation).
        return _AXIS_TO_LEVEL.get(
            (self.reversibility, self.blast_radius),
            ActionLevel.CONFIRM,
        )

    def with_observation(self, obs: ObservationConfidence) -> "ActionClassification":
        """Return a new classification with the given observation confidence."""
        return ActionClassification(
            reversibility=self.reversibility,
            blast_radius=self.blast_radius,
            observation_confidence=obs,
        )


# Worker capabilities that indicate this is an action worker (not a shell/python
# worker that operates inside Friday's own workspace).
_ACTION_CAPABILITIES = frozenset({
    "Window Management",
    "Workspace Control",
    "Application Launcher",
})


# Action types classified by (reversibility, blast_radius).
#
# The two-axis classification determines the default confirmation level
# via the matrix in _AXIS_TO_LEVEL. Actions not listed here get the
# conservative default of (IRREVERSIBLE, MEDIUM) → CONFIRM.
#
# When adding a new action type, classify it here by asking:
#   1. Can this be undone? (reversibility)
#   2. How much does it touch? (blast_radius)
# The level is derived automatically — no need to pick a level directly.
_ACTION_CLASSIFICATIONS: dict[str, ActionClassification] = {
    # ── Read-only queries (reversible + narrow) → AUTO ──
    "query":              ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "clients":           ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "workspaces":        ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "monitors":          ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "activewindow":      ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "activeworkspace":   ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "cursorpos":         ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "binds":             ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "devices":           ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),

    # ── Browser read-only (reversible + narrow) → AUTO ──
    "read":               ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "title":              ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "url":                ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "screenshot":         ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),

    # ── State-changing, reversible, medium blast → NOTIFY ──
    "workspace":          ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "exec":               ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "focuswindow":        ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "movetoworkspace":    ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "movetoworkspacesilent": ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "movewindow":         ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "resizewindow":       ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "fullscreen":         ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "togglefloating":     ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "pin":                ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "focusmonitor":       ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),
    "movecursortocorner": ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),

    # ── Irreversible, narrow blast → CONFIRM (confirm-first) ──
    "closewindow":        ActionClassification(Reversibility.IRREVERSIBLE, BlastRadius.NARROW),

    # ── Irreversible, wide blast → DOUBLE ──
    "kill":               ActionClassification(Reversibility.IRREVERSIBLE, BlastRadius.WIDE),

    # ── Irreversible, critical blast → DOUBLE ──
    "exit":               ActionClassification(Reversibility.IRREVERSIBLE, BlastRadius.CRITICAL),

    # ── Shell execution (state-changing, medium blast) → NOTIFY ──
    # Shell commands can modify files, launch processes, etc. Reversible
    # when the command can be undone (e.g. mkdir, touch). Default to NOTIFY
    # so Friday executes immediately but informs the operator.
    "shell_exec":         ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),

    # ── Git operations (reversible via reflog, medium blast) → NOTIFY ──
    # Git changes are recoverable via reflog/reset. The executor refuses
    # push operations at the worker level. Default to NOTIFY.
    "git_ops":            ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),

    # ── Filesystem operations (reversible when backed up, medium blast) → NOTIFY ──
    # File writes, moves, copies are recoverable. Deletions are sent to
    # a higher level by the delete operation below.
    "filesystem":         ActionClassification(Reversibility.REVERSIBLE, BlastRadius.MEDIUM),

    # ── File delete (irreversible, medium blast) → CONFIRM ──
    # Deleting files cannot be undone (unless git-tracked). Separate
    # classification so the same executor handles both.
    "filesystem_delete":  ActionClassification(Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM),

    # ── Python execution (reversible, narrow blast) → AUTO ──
    # Python scripts run in the workspace; their effects are contained
    # and verifiable. Pytest/test execution is also here.
    "python_exec":        ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),
    "testing":            ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),

    # ── Documentation writes (reversible, narrow) → AUTO ──
    # Writing READMEs or analysis docs has minimal blast radius.
    "documentation":      ActionClassification(Reversibility.REVERSIBLE, BlastRadius.NARROW),

    # ── Skill execution (reversible but wide blast) → CONFIRM ──
    # Formed skills may touch multiple subsystems. Require confirmation
    # until the skill has proven reliability.
    "skill_execute":      ActionClassification(Reversibility.REVERSIBLE, BlastRadius.WIDE),
}

# Action types (from the action field of the worker payload) mapped to their
# confirmation level. The canonical source of default levels is now
# ``_ACTION_CLASSIFICATIONS`` — this dict is DERIVED from it for backwards
# compatibility with code that reads ``_ACTION_LEVELS`` directly.
# ── Observation confidence lookup ──────────────────────────────────
# Thresholds for counting successful observations from the action log.
# "Successful" means an action of this type was logged with no error detail
# or with confidence='observed' in the actions table.
_OBS_CONFIDENCE_HIGH_THRESHOLD = 10    # 10+ successful → HIGH
_OBS_CONFIDENCE_MEDIUM_THRESHOLD = 3   # 3-9 successful → MEDIUM
_OBS_CONFIDENCE_LOOKBACK_HOURS = 168   # look back 7 days (168 hours)


def lookup_observation_confidence(action: str,
                                   target: str = "",
                                   conn=None) -> ObservationConfidence:
    """Determine observation confidence for an action+target combo.

    Queries the ``actions`` table for successful executions of this action
    type within the observation lookback window. Counts exact matches
    (same action_type) and approximate target matches.

    Args:
        action: The action type to look up (e.g. 'shell_exec', 'git_ops').
        target: Optional target pattern for finer-grained matching
                (e.g. 'ls -la' vs 'rm -rf /tmp/build').
        conn: Optional DB connection for querying action history.

    Returns:
        ObservationConfidence.HIGH if 10+ successful observations.
        ObservationConfidence.MEDIUM if 3-9 successful observations.
        ObservationConfidence.LOW if 1-2 successful observations.
        ObservationConfidence.NONE if no observations found.
    """
    if conn is None:
        return ObservationConfidence.NONE

    action_lower = (action or "").lower().strip()
    if not action_lower:
        return ObservationConfidence.NONE

    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(
            hours=_OBS_CONFIDENCE_LOOKBACK_HOURS)).isoformat()

        # Query successful executions of this action type.
        # We count rows in the actions table where:
        #   - action_type matches (exact or a LIKE pattern)
        #   - observed_at is within the lookback window
        #   - confidence indicates success ('observed', 'derived', or not 'failed')
        rows = conn.execute(
            """SELECT COUNT(*) AS cnt FROM actions
               WHERE action_type = ?
               AND observed_at >= ?
               AND confidence IN ('observed', 'derived', 'confirmed')""",
            (action_lower, cutoff),
        ).fetchone()
        count = rows["cnt"] if rows else 0

        if count >= _OBS_CONFIDENCE_HIGH_THRESHOLD:
            return ObservationConfidence.HIGH
        if count >= _OBS_CONFIDENCE_MEDIUM_THRESHOLD:
            return ObservationConfidence.MEDIUM
        if count >= 1:
            return ObservationConfidence.LOW

        return ObservationConfidence.NONE

    except Exception:
        return ObservationConfidence.NONE


_ACTION_LEVELS: dict[str, ActionLevel] = {
    action: _AXIS_TO_LEVEL.get(
        (cls.reversibility, cls.blast_radius),
        ActionLevel.CONFIRM,
    )
    for action, cls in _ACTION_CLASSIFICATIONS.items()
}

# Default classification for unrecognized action types (safest conservative guess).
_DEFAULT_CLASSIFICATION = ActionClassification(
    Reversibility.IRREVERSIBLE, BlastRadius.MEDIUM
)


def is_action_worker(worker_id: str,
                    capabilities: Optional[list[str]] = None) -> bool:
    """True if the worker has action capabilities (desktop control).

    Checks two signals:
    1. Worker ID follows the action-worker naming pattern
       (worker:hyprctl, worker:browser, worker:gui, worker:input).
    2. Worker declares any of the known action capabilities
       (Window Management, Workspace Control, Application Launcher).

    Either signal alone is sufficient — this ensures meta-generated workers
    with action capabilities but non-standard ids are also caught.
    """
    wid = (worker_id or "").lower()
    # Check worker_id prefix pattern.
    if any(wid.startswith(p) for p in ("worker:hyprctl", "worker:browser",
                                        "worker:gui", "worker:input")):
        return True
    # Check capabilities (catches meta-generated workers with action caps).
    if capabilities:
        caps_lower = {c.lower().strip() for c in capabilities}
        if caps_lower & {c.lower() for c in _ACTION_CAPABILITIES}:
            return True
    return False


def get_action_level(action: str, conn=None) -> ActionLevel:
    """Return the confirmation level for a given action type.

    Uses the three-axis classifier (reversibility × blast_radius ×
    observation_confidence). The observation confidence is resolved
    dynamically from the action log when a DB connection is provided.

    When ``conn`` is ``None``, falls back to the base two-axis level
    (no observation modulation) to preserve backwards compatibility
    with callers that don't have DB access.

    Unknown action types get the conservative default
    ``(IRREVERSIBLE, MEDIUM) → CONFIRM``.
    """
    action_lower = (action or "").lower().strip()
    cls = _ACTION_CLASSIFICATIONS.get(action_lower)
    if cls is not None:
        if conn is not None:
            # Three-axis: resolve observation confidence from action history.
            obs = lookup_observation_confidence(action_lower, conn=conn)
            return cls.with_observation(obs).resolve_level()
        # No conn: use base two-axis level directly (no observation
        # modulation). This preserves backwards compatibility with
        # callers that don't have DB access.
        return _AXIS_TO_LEVEL.get(
            (cls.reversibility, cls.blast_radius),
            ActionLevel.CONFIRM,
        )
    # For unknown actions, use the default two-axis base level directly.
    # The ``_DEFAULT_CLASSIFICATION`` is already conservative (IRREVERSIBLE,
    # MEDIUM) — no need to apply observation modulation here since unknown
    # action types are inherently unfamiliar and the base level already
    # reflects that caution.
    return _AXIS_TO_LEVEL.get(
        (_DEFAULT_CLASSIFICATION.reversibility, _DEFAULT_CLASSIFICATION.blast_radius),
        ActionLevel.CONFIRM,
    )


def classify_action(action: str, conn=None) -> ActionClassification:
    """Return the three-axis classification for an action type.

    Returns the registered classification with dynamic observation
    confidence resolved from the action log (if conn is provided),
    otherwise the conservative default ``(IRREVERSIBLE, MEDIUM)``.

    When ``conn`` is ``None``, the observation confidence is ``NONE``
    (no modulation) to preserve backwards compatibility.

    This is useful for operators inspecting why a particular action has
    its permission level (whether via the autonomous_planner or CLI).
    """
    action_lower = (action or "").lower().strip()
    base = _ACTION_CLASSIFICATIONS.get(action_lower, _DEFAULT_CLASSIFICATION)
    if conn is not None:
        # Three-axis: resolve observation confidence from action history.
        obs = lookup_observation_confidence(action_lower, conn=conn)
        return base.with_observation(obs)
    # No conn: return base classification unchanged (no observation
    # modulation). The NONE observation confidence is the default in
    # the frozen dataclass, so the caller sees the base level.
    return base


def prompt_confirm(action: str, target: str, worker_id: str,
                   skip_prompt: bool = False,
                   conn=None) -> bool:
    """Prompt the user to confirm an action. Returns True if confirmed.

    Uses the three-axis classifier (reversibility × blast_radius ×
    observation_confidence) to determine the required level. The
    observation confidence is resolved dynamically from the action
    log via the conn parameter.

    Args:
        action: The hyprctl action type (e.g. 'workspace', 'exec', 'closewindow').
        target: The target of the action (e.g. '3', 'firefox', 'class:kitty').
        worker_id: The worker that will execute the action (for display).
        skip_prompt: If True, auto-confirm (for scripted/--yes mode).
        conn: Optional DB connection for autonomy checks + observation lookup.

    Returns:
        True if the action should proceed, False to cancel.
    """
    # Graduated autonomy: check kill switch FIRST and foremost.
    # The kill switch is the nuclear option — it blocks ALL executors
    # regardless of confirmation level or skip_prompt flag.
    # Check in-memory cache first (fast path), fall back to DB.
    from ..autonomy import is_autonomy_enabled, is_kill_switch_active
    if is_kill_switch_active(conn):
        print(f"\n🛑 [KILL SWITCH ACTIVE] {worker_id} blocked: {action} {target}")
        print("  Run 'friday autonomy resume' to release the kill switch.")
        return False

    # When autonomy is disabled, ALL action workers are blocked.
    if not is_autonomy_enabled(conn):
        print(f"\n🔒 [AUTONOMY DISABLED] {worker_id} blocked: {action} {target}")
        print("  Use 'friday autonomy enable' to re-enable autonomous actions.")
        return False

    # Graduated autonomy: check per-action-type override + auto-downgrade.
    # The effective level is resolved by precedence:
    #   user override > auto-downgrade > hardcoded default
    from ..autonomy import get_action_permission
    auto_perm = get_action_permission(action, conn)
    effective_override = auto_perm.effective_level

    if skip_prompt:
        # Even with --yes, respect the kill switch (checked above).
        return True

    # Determine the confirmation level using the three-axis classifier:
    # reversibility × blast_radius × observation_confidence.
    # Falls back to the graduated autonomy effective level if overridden.
    level = get_action_level(action, conn=conn)
    if effective_override != level.value:
        level = ActionLevel(effective_override)

    if level == ActionLevel.AUTO:
        return True

    action_desc = f"{action} {target}" if target else action
    source = worker_id or "action worker"

    if level == ActionLevel.NOTIFY:
        # Execute-and-notify: do it, then tell the operator.
        # Only print the notification when not in skip_prompt mode.
        if not skip_prompt:
            print(f"\nℹ️  [{source}] {action_desc}")
        return True

    if level == ActionLevel.CONFIRM:
        print(f"\n[ACTION REQUIRED] {source} wants to: {action_desc}")
        try:
            response = input("  Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return response in ("y", "yes")

    if level == ActionLevel.DOUBLE_CONFIRM:
        print(f"\n⚠️  [DESTRUCTIVE ACTION] {source} wants to: {action_desc}")
        print("  This action CANNOT be undone.")
        try:
            first = input("  Are you sure? [y/N] ").strip().lower()
            if first not in ("y", "yes"):
                return False
            second = input("  REALLY sure? This will close/terminate. [y/N] ").strip().lower()
            return second in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    return False
