"""Pillar B Stage 1 — Action Log (Pillar B — Passive Action Logging).

Logs every action Friday observes or takes: app opened, workspace switched,
command run, file edited, message sent. Each action is a single event row
with context (time, workspace, project) for sequence mining in Stage 2.

Two sources of actions:
  1. **Friday's own actions** — logged at execution time by executors
     (HyprlandExecutor, etc.) via ``log_action()``.
  2. **User actions inferred from observation diffs** — derived by
     ``diff_observations_to_actions()`` which compares consecutive observation
     runs and detects changes (window focused, workspace switched, app launched).

The actions table is append-only — every row is one event, never mutated.
Sequence mining (Stage 2) reads from this table to find repeated patterns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ActionEvent:
    """One action event: what happened, when, and in what context."""

    source: str           # "hyprland", "terminal", "friday", "browser"
    action_type: str      # "workspace_switch", "app_launch", "window_focus", "command_run"
    target: str = ""      # what was acted upon (workspace "3", app "firefox", command string)
    detail: str = "{}"    # JSON with additional context (previous workspace, pid, etc.)
    workspace_id: Optional[str] = None   # which workspace was active
    project: Optional[str] = None        # which project was in focus (derived from cwd)
    session_id: Optional[str] = None     # links to daemon/execution session
    confidence: str = "observed"         # observed, derived, inferred
    observed_at: str = ""                # when the action happened
    recorded_at: str = ""                # when Friday persisted it

    def to_row(self) -> dict:
        return {
            "source": self.source,
            "action_type": self.action_type,
            "target": self.target or "",
            "detail": self.detail if isinstance(self.detail, str) else json.dumps(self.detail),
            "workspace_id": self.workspace_id,
            "project": self.project,
            "session_id": self.session_id,
            "confidence": self.confidence,
            "observed_at": self.observed_at or now_iso(),
            "recorded_at": self.recorded_at or now_iso(),
        }


def log_action(conn, event: ActionEvent) -> int:
    """Persist one action event to the actions table. Returns the row id."""
    row = event.to_row()
    cur = conn.execute(
        """INSERT INTO actions
           (source, action_type, target, detail, workspace_id, project,
            session_id, confidence, observed_at, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["source"], row["action_type"], row["target"], row["detail"],
         row["workspace_id"], row["project"], row["session_id"],
         row["confidence"], row["observed_at"], row["recorded_at"]),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_actions(conn, limit: int = 50,
                       source: Optional[str] = None) -> list[dict]:
    """Return the most recent action events, optionally filtered by source."""
    if source:
        rows = conn.execute(
            "SELECT * FROM actions WHERE source = ? "
            "ORDER BY observed_at DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def diff_observations_to_actions(
    prior_obs: list[dict],
    current_obs: list[dict],
    observed_at: str,
) -> list[ActionEvent]:
    """Derive action events from differences between two observation snapshots.

    Compares prior observations vs current observations on (source, subject,
    aspect) keys and detects changes that represent actions:
      - active_window_class changed → "window_focus"
      - active_workspace changed → "workspace_switch"
      - window_count increased → "app_launch"
      - window_count decreased → "app_close"

    Only desktop/hyprland observations are currently diffed. Other sources
    (terminal, browser) will be added as those observers come online.

    Returns a list of ActionEvent objects (empty if no meaningful changes).
    """
    prior_map = _obs_map(prior_obs)
    current_map = _obs_map(current_obs)
    actions: list[ActionEvent] = []

    # Detect workspace switches (active_workspace value changed).
    prior_ws = prior_map.get(("desktop", "active_workspace", "hyprland"))
    current_ws = current_map.get(("desktop", "active_workspace", "hyprland"))
    if prior_ws is not None and current_ws is not None:
        if prior_ws.get("value") != current_ws.get("value"):
            actions.append(ActionEvent(
                source="hyprland",
                action_type="workspace_switch",
                target=current_ws.get("value", ""),
                detail=json.dumps({"from": prior_ws.get("value", ""),
                                   "to": current_ws.get("value", "")}),
                workspace_id=current_ws.get("value"),
                confidence="derived",
                observed_at=observed_at,
            ))

    # Detect window focus changes (active_window_class changed).
    prior_class = prior_map.get(("desktop", "active_window_class", "hyprland"))
    current_class = current_map.get(("desktop", "active_window_class", "hyprland"))
    if prior_class is not None and current_class is not None:
        prior_val = prior_class.get("value", "")
        current_val = current_class.get("value", "")
        if prior_val != current_val and current_val:
            actions.append(ActionEvent(
                source="hyprland",
                action_type="window_focus",
                target=current_val,
                detail=json.dumps({"from_class": prior_val,
                                   "to_class": current_val}),
                confidence="derived",
                observed_at=observed_at,
            ))

    # Detect app launches (window_count increased).
    prior_wc = prior_map.get(("desktop", "window_count", "hyprland"))
    current_wc = current_map.get(("desktop", "window_count", "hyprland"))
    if prior_wc is not None and current_wc is not None:
        try:
            prior_n = int(prior_wc.get("value", "0"))
            current_n = int(current_wc.get("value", "0"))
            if current_n > prior_n:
                # New windows appeared — derive the new class from current obs.
                new_class = current_class.get("value", "") if current_class else ""
                actions.append(ActionEvent(
                    source="hyprland",
                    action_type="app_launch",
                    target=new_class,
                    detail=json.dumps({"before_count": prior_n,
                                       "after_count": current_n}),
                    confidence="derived",
                    observed_at=observed_at,
                ))
            elif current_n < prior_n:
                actions.append(ActionEvent(
                    source="hyprland",
                    action_type="app_close",
                    target="",
                    detail=json.dumps({"before_count": prior_n,
                                       "after_count": current_n}),
                    confidence="derived",
                    observed_at=observed_at,
                ))
        except (ValueError, TypeError):
            pass

    return actions


def _obs_map(
    observations: list[dict],
) -> dict[tuple[str, str, str], dict]:
    """Index observations by (subject, aspect, source) for fast diffing."""
    out: dict[tuple[str, str, str], dict] = {}
    for obs in observations:
        subject = obs.get("subject", "")
        aspect = obs.get("aspect", "")
        source = obs.get("source", "")
        out[(subject, aspect, source)] = obs
    return out
