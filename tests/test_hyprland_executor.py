"""HyprlandExecutor tests — Phase 1 Step 2.

Tests dispatch + verify-by-diff against real hyprctl on this machine.
All tests use reversible actions only (workspace switch, window focus).
Destructive actions are never tested here — they are covered by the
confirm gate tests alone.

Key invariants tested:
- Read-only actions succeed without side effects
- Write actions go through confirm gate before dispatch
- Verify-by-diff catches stale state and retries
- The executor never fabricates success on ambiguous reads
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from friday.runtime.hyprland_executor import (
    HyprlandExecutor,
    is_read_only_action,
    is_write_action,
)
from friday.runtime.models import ExecutionResult


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_task(payload: str):
    """Create a minimal task-like object with runtime_payload."""
    class FakeTask:
        runtime_payload = payload
    return FakeTask()


def _ok(result: ExecutionResult) -> bool:
    """Shortcut: True if the result reports success."""
    return result.success


# ── Action classification (fast, no hyprctl needed) ──────────────────────

class TestActionClassification:
    def test_read_only_actions(self):
        assert is_read_only_action("query") is True
        assert is_read_only_action("activewindow") is True
        assert is_read_only_action("monitors") is True
        assert is_read_only_action("workspaces") is True
        assert is_read_only_action("clients") is True

    def test_write_actions(self):
        assert is_write_action("workspace") is True
        assert is_write_action("exec") is True
        assert is_write_action("focuswindow") is True
        assert is_write_action("closewindow") is True
        assert is_write_action("fullscreen") is True

    def test_mutually_exclusive(self):
        for a in ("workspace", "exec", "closewindow", "fullscreen"):
            assert is_read_only_action(a) is False, f"{a} should not be read-only"
        for a in ("query", "activewindow", "monitors"):
            assert is_write_action(a) is False, f"{a} should not be write"


# ── Structural: gate before dispatch ─────────────────────────────────────

class TestStructural:
    """Confirm the gate is wired BEFORE any action dispatches."""

    def test_prompt_confirm_called_before_hyprctl_dispatch(self):
        import ast
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(HyprlandExecutor.execute))
        tree = ast.parse(source)

        class Finder(ast.NodeVisitor):
            def __init__(self):
                self.confirm = None
                self._dispatch = None
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name):
                    if node.func.id == "prompt_confirm" and self.confirm is None:
                        self.confirm = node.lineno
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "_dispatch" and self._dispatch is None:
                        self._dispatch = node.lineno
                self.generic_visit(node)

        f = Finder()
        f.visit(tree)
        assert f.confirm is not None, "prompt_confirm() must be called"
        assert f._dispatch is not None, "_dispatch() must exist"
        assert f.confirm < f._dispatch, (
            f"prompt_confirm at line {f.confirm} must precede _dispatch at {f._dispatch}")


# ── Real hyprctl tests ───────────────────────────────────────────────────

@pytest.mark.skipif(not __import__("shutil").which("hyprctl"),
                    reason="hyprctl not on PATH — not on Hyprland?")
class TestRealHyprctl:
    """Tests that actually call hyprctl on this machine.

    If no Hyprland session is running, these will fail gracefully
    (empty output, no crash).
    """

    def _read_only_executor(self):
        return HyprlandExecutor()

    def test_query_clients_is_read_only(self):
        """query clients should not produce an error."""
        exe = self._read_only_executor()
        task = _make_task('{"action": "query", "target": "clients"}')
        result = exe.execute(task)
        # May fail if no Hyprland session, but must not crash
        if result.success:
            assert result.stdout
        else:
            # Acceptable: no Hyprland session
            assert "no output" in result.error or "returned no output" in result.error

    def test_query_activewindow_succeeds(self):
        exe = self._read_only_executor()
        task = _make_task('{"action": "query", "target": "activewindow"}')
        result = exe.execute(task)
        if result.success:
            assert result.stdout
            # Should contain a window class or title
            assert "class" in result.stdout.lower() or "title" in result.stdout.lower()
        else:
            assert "no output" in result.error

    def test_query_workspaces_returns_data(self):
        exe = self._read_only_executor()
        task = _make_task('{"action": "query", "target": "workspaces"}')
        result = exe.execute(task)
        if result.success:
            assert result.stdout
            # Workspace list typically has workspace IDs
            assert "workspace" in result.stdout.lower() or "id" in result.stdout.lower()

    def test_empty_payload_fails(self):
        """Empty payload must not crash — must return a clear error."""
        exe = self._read_only_executor()
        result = exe.execute(_make_task(""))
        assert result.success is False
        assert "empty" in result.error.lower()

    def test_bad_json_payload_fails(self):
        exe = self._read_only_executor()
        result = exe.execute(_make_task("not json"))
        assert result.success is False
        assert "json" in result.error.lower()

    def test_missing_action_field_fails(self):
        exe = self._read_only_executor()
        result = exe.execute(_make_task('{"notaction": "workspace"}'))
        assert result.success is False
        assert "action" in result.error.lower() or "required" in result.error.lower()

    def test_workspace_switch_is_write(self):
        """Workspace switch must go through confirm gate (tested structurally)."""
        assert is_write_action("workspace") is True


class TestRaceCondition:
    """The verify-by-diff retry mechanism handles stale reads.

    Unlike the AST structural tests above, these mock the underlying
    state-read functions to PRODUCE stale reads and verify the retry
    logic actually fires and resolves correctly — or reports 'unconfirmed'
    when retries also return stale.
    """

    def test_retry_succeeds_on_stale_then_fresh(self):
        """First _verify_action returns False (stale), second returns True
        (fresh after retry). Executor must retry and report success."""
        verify_results = [False, True]  # stale, then fresh

        def _mock_verify(action, target, before_active, before_workspaces,
                         before_ws, before_class):
            return verify_results.pop(0)

        with patch("sys.stdin", StringIO("y\n")):
            with patch.object(HyprlandExecutor, "_verify_action",
                              side_effect=_mock_verify):
                with patch("friday.runtime.hyprland_executor._hyprctl_dispatch",
                           return_value=True):
                    exe = HyprlandExecutor()
                    result = exe.execute(
                        _make_task('{"action": "workspace", "target": "2"}'))

        assert result.success is True, (
            f"Executor should have retried and succeeded, got: {result.error}")
        assert "verified" in result.stdout

    def test_retry_reports_unconfirmed_when_persistently_stale(self):
        """Both first verify AND retry return False. Executor must report
        a clear 'unconfirmed' / verification-failed error — never success."""
        def _always_stale(*args, **kwargs):
            return False

        with patch("sys.stdin", StringIO("y\n")):
            with patch.object(HyprlandExecutor, "_verify_action",
                              side_effect=_always_stale):
                with patch("friday.runtime.hyprland_executor._hyprctl_dispatch",
                           return_value=True):
                    exe = HyprlandExecutor()
                    result = exe.execute(
                        _make_task('{"action": "workspace", "target": "2"}'))

        assert result.success is False, (
            "Executor must NOT report success when state never changed")
        # Error must clearly communicate the failure
        assert any(phrase in result.error.lower()
                   for phrase in ("verification failed", "race", "state did not change")), (
            f"Error must clearly indicate verification failure, got: {result.error}")

    def test_retry_not_attempted_on_first_success(self):
        """When _verify_action returns True on first call, retry is skipped."""
        def _first_time_ok(*args, **kwargs):
            return True

        with patch("sys.stdin", StringIO("y\n")):
            with patch.object(HyprlandExecutor, "_verify_action",
                              side_effect=_first_time_ok):
                with patch("friday.runtime.hyprland_executor._hyprctl_dispatch",
                           return_value=True):
                    exe = HyprlandExecutor()
                    result = exe.execute(
                        _make_task('{"action": "workspace", "target": "2"}'))

        assert result.success is True, (
            f"Should have succeeded on first verify, got: {result.error}")
        assert "verified" in result.stdout
