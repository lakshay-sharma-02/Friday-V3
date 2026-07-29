"""Immutable view models for the ambient dashboard.

Renderers consume these, never domain objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class FeedEventView:
    """One event in the dashboard feed display."""

    id: int
    timestamp: str
    event_type: str
    title: str
    detail: str
    priority: int
    category: str
    project: str
    payload: str
    confidence: float
    salience: float
    actionable: bool
    action_label: str
    action_command: str
    dismissed: bool


@dataclass(frozen=True)
class StatusView:
    """Daemon + workspace status snapshot for the dashboard."""

    daemon_state: str  # "running" | "stopped" | "crashed"
    last_cycle_at: str
    last_cycle_outcome: str
    cycle_count: int
    repos_scanned: int
    unread_events: int
    unread_by_priority: dict[int, int]
    high_priority_unread: int
    pending_initiatives: int
    open_gaps: int
    active_skills: int
    drifted_skills: int
    new_suggestions: int
    kill_switch_active: bool


@dataclass(frozen=True)
class FooterView:
    """Status line shown in the dashboard footer."""

    text: str
    event_count: int
    unread_count: int


@dataclass(frozen=True)
class DashboardView:
    """Complete snapshot of everything the dashboard displays."""

    events: list[FeedEventView]
    status: StatusView
    footer: FooterView
    scroll_offset: int = 0
    selected_index: int = -1
