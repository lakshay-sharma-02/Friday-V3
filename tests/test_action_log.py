"""Action log tests — Pillar B Stage 1.

Tests the action event model, persistence, and observation diffing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from friday.action_log import (
    ActionEvent,
    diff_observations_to_actions,
    get_recent_actions,
    log_action,
    now_iso,
)
from friday.db import connect


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    try:
        c.close()
    except Exception:
        pass


class TestActionEvent:
    def test_to_row_includes_required_fields(self):
        e = ActionEvent(source="friday", action_type="workspace_switch",
                        target="3")
        row = e.to_row()
        assert row["source"] == "friday"
        assert row["action_type"] == "workspace_switch"
        assert row["target"] == "3"
        assert row["confidence"] == "observed"
        assert row["observed_at"]
        assert row["recorded_at"]

    def test_to_row_auto_fills_timestamps(self):
        e = ActionEvent(source="test", action_type="test_type")
        row = e.to_row()
        assert row["observed_at"]
        assert row["recorded_at"]

    def test_confidence_defaults_to_observed(self):
        e = ActionEvent(source="test", action_type="test")
        assert e.confidence == "observed"

    def test_detail_defaults_to_empty_json(self):
        e = ActionEvent(source="test", action_type="test")
        assert e.detail == "{}"

    def test_to_row_serializes_dict_detail_to_json(self):
        e = ActionEvent(source="test", action_type="test",
                        detail={"key": "val"})
        row = e.to_row()
        assert isinstance(row["detail"], str)
        assert json.loads(row["detail"]) == {"key": "val"}


class TestLogAction:
    def test_log_and_retrieve(self, conn):
        e = ActionEvent(
            source="friday",
            action_type="workspace_switch",
            target="3",
            workspace_id="3",
            confidence="observed",
        )
        row_id = log_action(conn, e)
        assert row_id > 0

        rows = get_recent_actions(conn)
        assert len(rows) >= 1
        last = rows[0]
        assert last["source"] == "friday"
        assert last["action_type"] == "workspace_switch"
        assert last["target"] == "3"

    def test_log_multiple_and_order(self, conn):
        log_action(conn, ActionEvent(source="t1", action_type="a", target="1",
                                     observed_at="2025-01-01T00:00:00"))
        log_action(conn, ActionEvent(source="t2", action_type="b", target="2",
                                     observed_at="2025-01-02T00:00:00"))

        rows = get_recent_actions(conn)
        assert rows[0]["source"] == "t2"  # newest first
        assert rows[1]["source"] == "t1"

    def test_filter_by_source(self, conn):
        log_action(conn, ActionEvent(source="friday", action_type="a",
                                     observed_at="2025-01-01T00:00:00"))
        log_action(conn, ActionEvent(source="user", action_type="b",
                                     observed_at="2025-01-02T00:00:00"))

        rows = get_recent_actions(conn, source="friday")
        assert len(rows) == 1
        assert rows[0]["source"] == "friday"

    def test_limit(self, conn):
        for i in range(5):
            log_action(conn, ActionEvent(
                source="t", action_type=str(i),
                observed_at=f"2025-01-{i+1:02d}T00:00:00"))
        rows = get_recent_actions(conn, limit=2)
        assert len(rows) == 2


class TestDiffObservationsToActions:
    def _obs(self, subject, aspect, value, source="hyprland"):
        return {"subject": subject, "aspect": aspect, "value": value,
                "source": source}

    def test_workspace_switch_detected(self):
        prior = [self._obs("desktop", "active_workspace", "1")]
        current = [self._obs("desktop", "active_workspace", "2")]
        actions = diff_observations_to_actions(prior, current, "2025-01-01T00:00:00")
        assert len(actions) == 1
        assert actions[0].action_type == "workspace_switch"
        assert actions[0].target == "2"

    def test_no_change_no_actions(self):
        prior = [self._obs("desktop", "active_workspace", "1")]
        current = [self._obs("desktop", "active_workspace", "1")]
        actions = diff_observations_to_actions(prior, current, "2025-01-01T00:00:00")
        assert len(actions) == 0

    def test_window_focus_change_detected(self):
        prior = [self._obs("desktop", "active_window_class", "firefox")]
        current = [self._obs("desktop", "active_window_class", "kitty")]
        actions = diff_observations_to_actions(prior, current, "2025-01-01T00:00:00")
        assert len(actions) == 1
        assert actions[0].action_type == "window_focus"
        assert actions[0].target == "kitty"

    def test_app_launch_detected(self):
        prior = [self._obs("desktop", "window_count", "3"),
                 self._obs("desktop", "active_window_class", "")]
        current = [self._obs("desktop", "window_count", "4"),
                   self._obs("desktop", "active_window_class", "firefox")]
        actions = diff_observations_to_actions(prior, current, "2025-01-01T00:00:00")
        app_launches = [a for a in actions if a.action_type == "app_launch"]
        assert len(app_launches) == 1
        assert app_launches[0].target == "firefox"

    def test_app_close_detected(self):
        prior = [self._obs("desktop", "window_count", "4")]
        current = [self._obs("desktop", "window_count", "3")]
        actions = diff_observations_to_actions(prior, current, "2025-01-01T00:00:00")
        closes = [a for a in actions if a.action_type == "app_close"]
        assert len(closes) == 1

    def test_empty_obs_returns_empty(self):
        actions = diff_observations_to_actions([], [], "2025-01-01T00:00:00")
        assert actions == []
