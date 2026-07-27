"""Integration tests: executor → action_log + autonomy wiring.

Verifies that every return path in HyprlandExecutor.execute() and
BrowserExecutor.execute() calls both log_action() and record_action_outcome().
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from friday.runtime.hyprland_executor import HyprlandExecutor
from friday.runtime.browser_executor import BrowserExecutor


class MockTask:
    """Minimal task stub with a runtime_payload field."""
    def __init__(self, runtime_payload: str = ""):
        self.runtime_payload = runtime_payload


HYPRCTL_PATH = "friday.runtime.hyprland_executor._hyprctl"


@pytest.fixture(autouse=True)
def test_env():
    """Isolate each test with a temp DB and auto-confirm for prompt_confirm.

    Overrides FRIDAY_DB env var so all connect() calls (including those in
    autonomy.py, action_log.py, and the executors) point to the same temp DB.
    Also patches prompt_confirm at the executor import sites so tests don't
    hang on stdin (the executors import via ``from .confirm_gate import ...``
    which creates a local reference, so patching the definition site won't
    affect the already-imported reference).
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db = os.environ.get("FRIDAY_DB")
    os.environ["FRIDAY_DB"] = tmp.name

    with patch("friday.runtime.hyprland_executor.prompt_confirm", return_value=True), \
         patch("friday.runtime.browser_executor.prompt_confirm", return_value=True):
        yield

    os.unlink(tmp.name)
    if old_db is not None:
        os.environ["FRIDAY_DB"] = old_db
    else:
        del os.environ["FRIDAY_DB"]


# ──────────────────────────────────────────────────────────────────────
# HyprlandExecutor integration
# ──────────────────────────────────────────────────────────────────────


class TestHyprlandExecutorAutonomy:
    """Every return path in HyprlandExecutor.execute() must record outcomes."""

    def test_empty_payload_logs_failure(self):
        exc = HyprlandExecutor()
        task = MockTask("")
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("hyprctl_parse")
        assert perm.consecutive_failures >= 1

    def test_bad_json_logs_failure(self):
        exc = HyprlandExecutor()
        task = MockTask("not json")
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("hyprctl_parse")
        assert perm.consecutive_failures >= 1

    def test_missing_action_field_logs_failure(self):
        exc = HyprlandExecutor()
        task = MockTask(json.dumps({"target": "3"}))
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("hyprctl_parse")
        assert perm.consecutive_failures >= 1

    def test_query_failure_logs_outcome(self):
        with patch(HYPRCTL_PATH, return_value=None):
            exc = HyprlandExecutor()
            task = MockTask(json.dumps({"action": "query", "target": "clients"}))
            result = exc.execute(task)
            assert result.success is False

            from friday.autonomy import get_action_permission
            perm = get_action_permission("query")
            assert perm.consecutive_failures >= 1

    def test_query_success_logs_outcome(self):
        with patch(HYPRCTL_PATH, return_value="output: ok"):
            exc = HyprlandExecutor()
            task = MockTask(json.dumps({"action": "query", "target": "clients"}))
            result = exc.execute(task)
            assert result.success is True

            from friday.autonomy import get_action_permission
            perm = get_action_permission("query")
            assert perm.consecutive_successes >= 1


# ──────────────────────────────────────────────────────────────────────
# BrowserExecutor integration
# ──────────────────────────────────────────────────────────────────────


class TestBrowserExecutorAutonomy:
    """Every return path in BrowserExecutor.execute() must record outcomes."""

    def test_empty_payload_logs_failure(self):
        exc = BrowserExecutor()
        task = MockTask("")
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("browser_parse")
        assert perm.consecutive_failures >= 1

    def test_bad_json_logs_failure(self):
        exc = BrowserExecutor()
        task = MockTask("not json")
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("browser_parse")
        assert perm.consecutive_failures >= 1

    def test_missing_action_field_logs_failure(self):
        exc = BrowserExecutor()
        task = MockTask(json.dumps({"target": "https://example.com"}))
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("browser_parse")
        assert perm.consecutive_failures >= 1

    @patch.object(BrowserExecutor, "_ensure_connected", return_value=False)
    def test_connection_failure_logs_outcome(self, mock_connect):
        exc = BrowserExecutor()
        task = MockTask(json.dumps(
            {"action": "navigate", "target": "https://example.com"}))
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("browser_navigate")
        assert perm.consecutive_failures >= 1

    @patch.object(BrowserExecutor, "_ensure_connected", return_value=True)
    @patch.object(BrowserExecutor, "_dispatch_action",
                  return_value={"success": True, "output": "ok"})
    def test_action_success_logs_outcome(self, mock_dispatch, mock_connect):
        exc = BrowserExecutor()
        task = MockTask(json.dumps({"action": "read", "target": "css:.content"}))
        result = exc.execute(task)
        assert result.success is True

        from friday.autonomy import get_action_permission
        perm = get_action_permission("browser_read")
        assert perm.consecutive_successes >= 1

    @patch.object(BrowserExecutor, "_ensure_connected", return_value=True)
    @patch.object(BrowserExecutor, "_dispatch_action",
                  return_value={"success": False, "error": "mock failure"})
    def test_action_failure_logs_outcome(self, mock_dispatch, mock_connect):
        exc = BrowserExecutor()
        task = MockTask(json.dumps({"action": "read", "target": "css:.content"}))
        result = exc.execute(task)
        assert result.success is False

        from friday.autonomy import get_action_permission
        perm = get_action_permission("browser_read")
        assert perm.consecutive_failures >= 1
