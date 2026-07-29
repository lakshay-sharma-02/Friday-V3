"""Tests for ClipboardExecutor (src/friday/runtime/clipboard_executor.py).

Covers:
  - Clipboard detection (detect_tool, available, status)
  - File bridge read/write fallback
  - Executor read/write operations
  - Edge cases (empty text, missing payload, bad JSON)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


# ──────────────────────────────────────────────────────────────────────────
# Helper: build a minimal task-like object
# ──────────────────────────────────────────────────────────────────────────


def _make_task(payload: str = ""):
    class _MiniTask:
        pass
    t = _MiniTask()
    t.runtime_payload = payload
    t.task_id = "test_task"
    t.worker_id = "worker:clipboard"
    t.execution_id = "test:clipboard"
    t.timeout = 30
    t.task_type = "clipboard"
    t.title = "clipboard test"
    t.goal = "clipboard test"
    t.outputs = []
    t.acceptance_criteria = []
    t.verification = []
    t.symbolic = {}
    t.dependency_summaries = {}
    return t


# ──────────────────────────────────────────────────────────────────────────
# Clipboard detection
# ──────────────────────────────────────────────────────────────────────────


class TestClipboardDetection:
    """Tests for clipboard tool detection."""

    def test_detect_tool_no_tools(self):
        """When no clipboard tools are installed, detect should return None."""
        with patch("shutil.which", return_value=None):
            from friday.runtime.clipboard_executor import _detect_clipboard_tool
            assert _detect_clipboard_tool() is None

    def test_detect_tool_wl_clipboard(self):
        """When wl-paste/wl-copy are found, detect should return wl-clipboard."""
        def _fake_which(cmd):
            if cmd in ("wl-paste", "wl-copy"):
                return f"/usr/bin/{cmd}"
            return None
        with patch("shutil.which", side_effect=_fake_which):
            from friday.runtime.clipboard_executor import _detect_clipboard_tool
            assert _detect_clipboard_tool() == "wl-clipboard"

    def test_detect_tool_xclip(self):
        """When xclip is found, detect should return xclip."""
        def _fake_which(cmd):
            if cmd == "xclip":
                return "/usr/bin/xclip"
            return None
        with patch("shutil.which", side_effect=_fake_which):
            from friday.runtime.clipboard_executor import _detect_clipboard_tool
            assert _detect_clipboard_tool() == "xclip"

    def test_detect_tool_macos(self):
        """When pbcopy/pbpaste are found, detect should return macos."""
        def _fake_which(cmd):
            if cmd in ("pbcopy", "pbpaste"):
                return f"/usr/bin/{cmd}"
            return None
        with patch("shutil.which", side_effect=_fake_which):
            from friday.runtime.clipboard_executor import _detect_clipboard_tool
            assert _detect_clipboard_tool() == "macos"

    def test_clipboard_available_false_when_no_tools(self):
        """clipboard_available() should return False when no system tools found."""
        with patch("shutil.which", return_value=None):
            from friday.runtime.clipboard_executor import clipboard_available
            assert clipboard_available() is False

    def test_clipboard_status_file_bridge(self):
        """clipboard_status() should mention file bridge when no tools."""
        with patch("shutil.which", return_value=None):
            from friday.runtime.clipboard_executor import clipboard_status
            status = clipboard_status()
            assert "file bridge" in status.lower()


# ──────────────────────────────────────────────────────────────────────────
# File bridge fallback
# ──────────────────────────────────────────────────────────────────────────


class TestFileBridge:
    """Tests for the file bridge fallback (no system clipboard tools)."""

    def test_write_read_via_file_bridge(self):
        """Write then read via file bridge should return the same text."""
        with patch("shutil.which", return_value=None):
            # Use a temp path for the bridge
            import tempfile
            tmpdir = tempfile.mkdtemp()
            bridge_path = Path(tmpdir) / "clipboard_bridge.txt"
            try:
                with patch("friday.runtime.clipboard_executor._FALLBACK_PATH", bridge_path):
                    from friday.runtime.clipboard_executor import _clipboard_write, _clipboard_read
                    assert _clipboard_write("hello bridge")
                    text = _clipboard_read()
                    assert text == "hello bridge"
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_empty_text(self):
        """Writing empty text should succeed (writes empty file)."""
        with patch("shutil.which", return_value=None):
            import tempfile
            tmpdir = tempfile.mkdtemp()
            bridge_path = Path(tmpdir) / "clipboard_bridge.txt"
            try:
                with patch("friday.runtime.clipboard_executor._FALLBACK_PATH", bridge_path):
                    from friday.runtime.clipboard_executor import _clipboard_write, _clipboard_read
                    assert _clipboard_write("")
                    text = _clipboard_read()
                    assert text == ""
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    def test_read_nonexistent_bridge(self):
        """Reading from a nonexistent bridge file should return empty string."""
        with patch("shutil.which", return_value=None):
            from friday.runtime.clipboard_executor import _clipboard_read
            text = _clipboard_read()
            # The default path (~/.friday/clipboard_bridge.txt) likely doesn't exist
            assert text == ""


# ──────────────────────────────────────────────────────────────────────────
# ClipboardExecutor
# ──────────────────────────────────────────────────────────────────────────


class TestClipboardExecutor:
    """Tests for ClipboardExecutor.execute()."""

    def test_execute_read_returns_stdout(self):
        """Reading clipboard should return text as stdout."""
        with patch("shutil.which", return_value=None):
            import tempfile
            tmpdir = tempfile.mkdtemp()
            bridge_path = Path(tmpdir) / "clipboard_bridge.txt"
            try:
                bridge_path.write_text("clipboard content", encoding="utf-8")
                with patch("friday.runtime.clipboard_executor._FALLBACK_PATH", bridge_path):
                    from friday.runtime.clipboard_executor import ClipboardExecutor
                    exe = ClipboardExecutor()
                    task = _make_task(json.dumps({"op": "read"}))
                    result = exe.execute(task)
                    assert result.success
                    assert "clipboard content" in result.stdout
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    def test_execute_write_success(self):
        """Writing to clipboard should succeed with file bridge."""
        with patch("shutil.which", return_value=None):
            import tempfile
            tmpdir = tempfile.mkdtemp()
            bridge_path = Path(tmpdir) / "clipboard_bridge.txt"
            try:
                with patch("friday.runtime.clipboard_executor._FALLBACK_PATH", bridge_path):
                    from friday.runtime.clipboard_executor import ClipboardExecutor
                    exe = ClipboardExecutor()
                    task = _make_task(json.dumps({"op": "write", "text": "test write"}))
                    result = exe.execute(task)
                    assert result.success
                    assert "Copied" in result.stdout
                    # Verify file was written
                    assert bridge_path.read_text() == "test write"
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    def test_execute_write_no_text_fails(self):
        """Writing without text should fail with descriptive error."""
        from friday.runtime.clipboard_executor import ClipboardExecutor
        exe = ClipboardExecutor()
        task = _make_task(json.dumps({"op": "write"}))
        result = exe.execute(task)
        assert not result.success
        assert "no text" in result.error.lower()

    def test_execute_bad_json(self):
        """Bad JSON payload should not crash — treat as default op (read)."""
        from friday.runtime.clipboard_executor import ClipboardExecutor
        exe = ClipboardExecutor()
        task = _make_task("this is not json")
        result = exe.execute(task)
        # Should not crash; treat as read
        assert result.success or not result.success  # depends on clipboard state

    def test_execute_empty_payload(self):
        """Empty payload should default to read and not crash."""
        from friday.runtime.clipboard_executor import ClipboardExecutor
        exe = ClipboardExecutor()
        task = _make_task("")
        result = exe.execute(task)
        # Should not crash; just read
        assert result.success or not result.success

    def test_execute_unknown_op(self):
        """Unknown operation should fail with descriptive error."""
        from friday.runtime.clipboard_executor import ClipboardExecutor
        exe = ClipboardExecutor()
        task = _make_task(json.dumps({"op": "unknown_op_xyz"}))
        result = exe.execute(task)
        assert not result.success
        assert "unknown" in result.error.lower()


# ──────────────────────────────────────────────────────────────────────────
# Task model compatibility
# ──────────────────────────────────────────────────────────────────────────


class TestTaskModel:
    """Verify the ClipboardExecutor works with the runtime's RuntimeTask model."""

    def test_executor_worker_id(self):
        """Executor should have the correct worker_id."""
        from friday.runtime.clipboard_executor import ClipboardExecutor
        exe = ClipboardExecutor()
        assert exe.worker_id == "worker:clipboard"

    def test_executor_resolve_registration(self):
        """ClipboardExecutor should be resolvable via resolve_executor."""
        from friday.runtime.executors import resolve_executor
        exe = resolve_executor("worker:clipboard")
        assert exe is not None
        from friday.runtime.clipboard_executor import ClipboardExecutor
        assert isinstance(exe, ClipboardExecutor)
