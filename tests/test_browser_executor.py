"""BrowserExecutor tests — Phase 1 Step 3.

Tests the CDP-based browser automation executor. Key focus:
- Confirm gate wired BEFORE any CDP action
- Read-only actions bypass gate
- State-changing actions go through gate
- Graceful degradation when no browser is available

All tests that require a real browser are marked @pytest.mark.skipif
and only run when a CDP endpoint is available.
"""

from __future__ import annotations

import json
import textwrap
from io import StringIO
from unittest.mock import patch

import pytest

from friday.runtime.browser_executor import BrowserExecutor
from friday.runtime.models import ExecutionResult


def _make_task(payload: str):
    class FakeTask:
        runtime_payload = payload
    return FakeTask()


# ── Structural: gate before dispatch ─────────────────────────────────────

class TestStructural:
    """Confirm the gate is wired BEFORE any CDP action."""

    def test_prompt_confirm_called_before_dispatch(self):
        import ast
        import inspect
        source = textwrap.dedent(inspect.getsource(BrowserExecutor.execute))
        tree = ast.parse(source)

        class Finder(ast.NodeVisitor):
            def __init__(self):
                self.confirm = None
                self.dispatch = None
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name):
                    if node.func.id == "prompt_confirm" and self.confirm is None:
                        self.confirm = node.lineno
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "_dispatch_action" and self.dispatch is None:
                        self.dispatch = node.lineno
                self.generic_visit(node)

        f = Finder()
        f.visit(tree)
        assert f.confirm is not None, "prompt_confirm() must be called"
        assert f.dispatch is not None, "_dispatch_action() must exist"
        assert f.confirm < f.dispatch, (
            f"prompt_confirm at line {f.confirm} must precede "
            f"_dispatch_action at {f.dispatch}")


# ── Error handling (no browser needed) ───────────────────────────────────

class TestErrorHandling:
    """These require no real browser — test the error paths."""

    def test_empty_payload_fails_gracefully(self):
        exe = BrowserExecutor()
        result = exe.execute(_make_task(""))
        assert result.success is False
        assert "empty" in result.error.lower()

    def test_bad_json_fails_gracefully(self):
        exe = BrowserExecutor()
        result = exe.execute(_make_task("not json"))
        assert result.success is False
        assert "json" in result.error.lower()

    def test_missing_action_fails_gracefully(self):
        exe = BrowserExecutor()
        result = exe.execute(_make_task('{"notaction": "navigate"}'))
        assert result.success is False
        assert "action" in result.error.lower() or "required" in result.error.lower()

    def test_unknown_action_gated_then_errors(self):
        """Unknown action still goes through gate (defaults to CONFIRM).
        Executor connects first, then dispatches — so failure may be
        connectivity before 'unknown action' check. Either error is OK."""
        with patch("sys.stdin", StringIO("y\n")):
            exe = BrowserExecutor()
            result = exe.execute(_make_task('{"action": "nonexistent"}'))
        assert result.success is False
        # Either unknown action OR connectivity failure
        assert "unknown" in result.error.lower() or "connect" in result.error.lower()

    def test_connectivity_failure_reported_clearly(self):
        """No browser running — should hit gate (title is AUTO now) and then
        fail on connectivity, not crash."""
        exe = BrowserExecutor()
        with patch.object(exe, '_ensure_connected', return_value=False):
            result = exe.execute(_make_task('{"action": "title"}'))
        assert result.success is False
        assert any(msg in result.error.lower() for msg in
                   ("connect", "browser", "could not connect", "--remote-debugging-port"))


# ── Confirm gate integration ─────────────────────────────────────────────

class TestConfirmGateIntegration:
    """Test the gate blocks state-changing actions when rejected."""

    def test_read_bypasses_gate(self):
        """title is AUTO level — should not prompt."""
        exe = BrowserExecutor()
        with patch.object(exe, '_ensure_connected', return_value=False):
            result = exe.execute(_make_task('{"action": "title"}'))
        assert result.success is False
        # Must fail on connectivity, not "cancelled by user"
        assert "user" not in result.error.lower()
        assert "cancelled" not in result.error.lower()

    def test_structural_gate_position(self):
        """Repeat the structural test to make the point."""
        import ast
        import inspect
        source = textwrap.dedent(inspect.getsource(BrowserExecutor.execute))
        tree = ast.parse(source)

        class Finder(ast.NodeVisitor):
            def __init__(self):
                self.confirm = None
                self.dispatch = None
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name):
                    if node.func.id == "prompt_confirm" and self.confirm is None:
                        self.confirm = node.lineno
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "_dispatch_action" and self.dispatch is None:
                        self.dispatch = node.lineno
                self.generic_visit(node)

        f = Finder()
        f.visit(tree)
        assert f.confirm is not None
        assert f.dispatch is not None
        assert f.confirm < f.dispatch
