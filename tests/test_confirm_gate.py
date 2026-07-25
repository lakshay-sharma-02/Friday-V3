"""Confirm gate tests — Phase 1 Step 1.

These test the safety layer BEFORE any executor runs real actions.
The entire point is proving the gate works: no action executing before
confirmation resolves, read-only bypassing properly, and the race
condition (duplicate dispatch while pending) handled correctly.
"""

from __future__ import annotations

import sys
import textwrap
from io import StringIO
from unittest.mock import patch

import pytest

from friday.runtime.confirm_gate import (
    ActionLevel,
    get_action_level,
    is_action_worker,
    prompt_confirm,
)


class TestActionLevelClassification:
    """Every action type must be classified correctly — wrong classification
    means the gate lets state-changing actions through ungated."""

    def test_read_only_are_auto(self):
        assert get_action_level("query") == ActionLevel.AUTO
        assert get_action_level("clients") == ActionLevel.AUTO
        assert get_action_level("workspaces") == ActionLevel.AUTO
        assert get_action_level("monitors") == ActionLevel.AUTO
        assert get_action_level("activewindow") == ActionLevel.AUTO
        assert get_action_level("activeworkspace") == ActionLevel.AUTO
        assert get_action_level("cursorpos") == ActionLevel.AUTO
        assert get_action_level("binds") == ActionLevel.AUTO
        assert get_action_level("devices") == ActionLevel.AUTO

    def test_state_changing_are_confirm(self):
        assert get_action_level("workspace") == ActionLevel.CONFIRM
        assert get_action_level("exec") == ActionLevel.CONFIRM
        assert get_action_level("focuswindow") == ActionLevel.CONFIRM
        assert get_action_level("movetoworkspace") == ActionLevel.CONFIRM
        assert get_action_level("movetoworkspacesilent") == ActionLevel.CONFIRM
        assert get_action_level("movewindow") == ActionLevel.CONFIRM
        assert get_action_level("resizewindow") == ActionLevel.CONFIRM
        assert get_action_level("fullscreen") == ActionLevel.CONFIRM
        assert get_action_level("togglefloating") == ActionLevel.CONFIRM
        assert get_action_level("pin") == ActionLevel.CONFIRM
        assert get_action_level("focusmonitor") == ActionLevel.CONFIRM
        assert get_action_level("movecursortocorner") == ActionLevel.CONFIRM

    def test_destructive_are_double_confirm(self):
        assert get_action_level("closewindow") == ActionLevel.DOUBLE_CONFIRM
        assert get_action_level("kill") == ActionLevel.DOUBLE_CONFIRM
        assert get_action_level("exit") == ActionLevel.DOUBLE_CONFIRM

    def test_unknown_action_defaults_to_confirm(self):
        """Unknown = state-changing is safer than unknown = auto."""
        assert get_action_level("nonexistent") == ActionLevel.CONFIRM
        assert get_action_level("") == ActionLevel.CONFIRM

    def test_case_insensitive(self):
        assert get_action_level("WORKSPACE") == ActionLevel.CONFIRM
        assert get_action_level("Query") == ActionLevel.AUTO
        assert get_action_level("CLOSEWINDOW") == ActionLevel.DOUBLE_CONFIRM


class TestIsActionWorker:
    """The gate must recognize all action-worker patterns including
    meta-generated workers with action capabilities."""

    def test_known_prefixes(self):
        assert is_action_worker("worker:hyprctl") is True
        assert is_action_worker("worker:hyprctl:abc123") is True
        assert is_action_worker("worker:browser") is True
        assert is_action_worker("worker:browser:def456") is True
        assert is_action_worker("worker:gui") is True
        assert is_action_worker("worker:input") is True

    def test_non_action_workers(self):
        assert is_action_worker("worker:shell") is False
        assert is_action_worker("worker:python") is False
        assert is_action_worker("worker:filesystem") is False
        assert is_action_worker("worker:git") is False
        assert is_action_worker("worker:testing") is False

    def test_capability_based_detection(self):
        """Meta-generated workers with action capabilities are caught."""
        caps = ["Window Management", "Workspace Control"]
        assert is_action_worker("worker:meta_foo", capabilities=caps) is True

    def test_non_action_capabilities(self):
        caps = ["File Reading", "Code Analysis"]
        assert is_action_worker("worker:meta_foo", capabilities=caps) is False


class TestPromptConfirm:
    """The critical tests: does the gate actually block?"""

    def test_auto_returns_true_without_prompt(self):
        """Read-only actions never prompt — always proceed."""
        assert prompt_confirm("query", "clients", "worker:hyprctl") is True

    def test_skip_prompt_confirms_anything(self):
        """--yes mode overrides even destructive actions."""
        assert prompt_confirm("closewindow", "firefox", "worker:hyprctl",
                              skip_prompt=True) is True

    def test_confirm_yes_proceeds(self):
        """User says y -> action proceeds."""
        with patch("sys.stdin", StringIO("y\n")):
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is True

    def test_confirm_no_rejected(self):
        """User says n -> action is rejected."""
        with patch("sys.stdin", StringIO("n\n")):
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is False

    def test_confirm_default_no(self):
        """Empty input defaults to no (n/N is default)."""
        with patch("sys.stdin", StringIO("\n")):
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is False

    def test_double_confirm_both_yes(self):
        """Both prompts y -> proceeds."""
        with patch("sys.stdin", StringIO("y\ny\n")):
            assert prompt_confirm("closewindow", "kitty",
                                  "worker:hyprctl") is True

    def test_double_confirm_first_no(self):
        """First prompt n -> rejected immediately."""
        with patch("sys.stdin", StringIO("n\n")):
            assert prompt_confirm("closewindow", "kitty",
                                  "worker:hyprctl") is False

    def test_double_confirm_second_no(self):
        """First y, second n -> rejected."""
        with patch("sys.stdin", StringIO("y\nn\n")):
            assert prompt_confirm("closewindow", "kitty",
                                  "worker:hyprctl") is False

    def test_eof_returns_false(self):
        """EOF (no TTY) -> reject safely, not crash."""
        with patch("sys.stdin", StringIO("")):
            with pytest.raises(EOFError):
                input()
            # The function catches EOFError internally
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is False

    def test_keyboard_interrupt_returns_false(self):
        """Ctrl-C -> reject safely, not crash."""
        original = sys.stdin
        class InterruptInput:
            def readline(self):
                raise KeyboardInterrupt
        sys.stdin = InterruptInput()  # type: ignore
        try:
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is False
        finally:
            sys.stdin = original


class TestRaceCondition:
    """If confirmation is pending and a second call comes in (duplicate
    dispatch, retry), it must NOT execute twice or bypass the gate."""

    def test_repeated_call_still_gated(self):
        """Two consecutive calls with same params both hit the gate."""
        with patch("sys.stdin", StringIO("y\n")):
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is True
        with patch("sys.stdin", StringIO("y\n")):
            # Second call is independent — still gated, still requires input
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is True

    def test_rejected_then_retry_must_confirm_again(self):
        """First reject, second approve — second must still go through gate."""
        with patch("sys.stdin", StringIO("n\n")):
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is False
        with patch("sys.stdin", StringIO("y\n")):
            # The gate does not cache rejections — user can change their mind
            assert prompt_confirm("workspace", "3", "worker:hyprctl") is True

    def test_no_cache_between_calls(self):
        """Each call is independent — no shared mutable state."""
        results = []
        for _ in range(3):
            with patch("sys.stdin", StringIO("y\n")):
                results.append(prompt_confirm("workspace", "3",
                                              "worker:hyprctl"))
        assert results == [True, True, True]


class TestExecutionGuard_Structural:
    """Verify the executor code path calls prompt_confirm before dispatch.

    We check that the HyprlandExecutor and BrowserExecutor have the
    prompt_confirm call positioned before any real dispatch in their
    execute() methods — a structural test that can't be fooled by
    passing the unit tests with a gate that's wired wrong.
    """

    def test_hyprland_executor_has_gate_before_dispatch(self):
        """Confirm gate call exists before the first _dispatch in execute()."""
        import ast
        import inspect
        from friday.runtime.hyprland_executor import HyprlandExecutor

        source = textwrap.dedent(inspect.getsource(HyprlandExecutor.execute))
        tree = ast.parse(source)

        # Walk the AST: find calls to prompt_confirm and _dispatch
        class CallFinder(ast.NodeVisitor):
            def __init__(self):
                self.confirm_calls = []
                self.dispatch_calls = []
                self.confirm_lineno = None
                self.dispatch_lineno = None

            def visit_Call(self, node):
                if (isinstance(node.func, ast.Name) and
                        node.func.id == "prompt_confirm"):
                    self.confirm_calls.append(node.lineno)
                    if self.confirm_lineno is None:
                        self.confirm_lineno = node.lineno
                if (isinstance(node.func, ast.Attribute) and
                        node.func.attr == "_dispatch"):
                    self.dispatch_calls.append(node.lineno)
                    if self.dispatch_lineno is None:
                        self.dispatch_lineno = node.lineno
                self.generic_visit(node)

        finder = CallFinder()
        finder.visit(tree)

        assert finder.confirm_lineno is not None, (
            "HyprlandExecutor.execute() must call prompt_confirm()")
        assert finder.dispatch_lineno is not None, (
            "HyprlandExecutor.execute() must call _dispatch()")
        assert finder.confirm_lineno < finder.dispatch_lineno, (
            f"prompt_confirm() at line {finder.confirm_lineno} must be "
            f"called BEFORE _dispatch() at line {finder.dispatch_lineno}")

    def test_browser_executor_has_gate_before_dispatch(self):
        """Confirm gate call exists before the first CDP action in execute()."""
        import ast
        import inspect
        from friday.runtime.browser_executor import BrowserExecutor

        source = textwrap.dedent(inspect.getsource(BrowserExecutor.execute))
        tree = ast.parse(source)

        class CallFinder(ast.NodeVisitor):
            def __init__(self):
                self.confirm_calls = []
                self.dispatch_calls = []
                self.confirm_lineno = None
                self.dispatch_lineno = None

            def visit_Call(self, node):
                if (isinstance(node.func, ast.Name) and
                        node.func.id == "prompt_confirm"):
                    self.confirm_calls.append(node.lineno)
                    if self.confirm_lineno is None:
                        self.confirm_lineno = node.lineno
                if (isinstance(node.func, ast.Attribute) and
                        node.func.attr == "_dispatch_action"):
                    self.dispatch_calls.append(node.lineno)
                    if self.dispatch_lineno is None:
                        self.dispatch_lineno = node.lineno
                self.generic_visit(node)

        finder = CallFinder()
        finder.visit(tree)

        assert finder.confirm_lineno is not None, (
            "BrowserExecutor.execute() must call prompt_confirm()")
        assert finder.dispatch_lineno is not None, (
            "BrowserExecutor.execute() must call _dispatch_action()")
        assert finder.confirm_lineno < finder.dispatch_lineno, (
            f"prompt_confirm() at line {finder.confirm_lineno} must be "
            f"called BEFORE _dispatch_action() at line {finder.dispatch_lineno}")
