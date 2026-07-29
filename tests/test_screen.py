"""Tests for the Screen/Workspace Awareness module."""

from __future__ import annotations

import json
import time

import pytest

from friday.screen import (
    ScreenContext,
    collect_screen_context,
    _detect_desktop,
    _extract_browser_url,
)


# ---------------------------------------------------------------------------
# ScreenContext model
# ---------------------------------------------------------------------------


class TestScreenContext:
    def test_create_empty(self):
        ctx = ScreenContext()
        assert ctx.active_window_title == ""
        assert ctx.active_window_class == ""
        assert ctx.running_processes == 0
        assert ctx.collected_at == 0.0

    def test_to_dict_roundtrip(self):
        ctx = ScreenContext(
            active_window_title="Refactor auth module — VS Code",
            active_window_class="code",
            active_window_pid=12345,
            active_window_process="Code",
            desktop_environment="hyprland",
            browser_url="https://github.com",
            browser_name="Brave",
            clipboard_text="git commit -m 'fix'",
            running_processes=256,
            top_processes=[{"name": "python3", "cpu_percent": 45.0}],
            collected_at=time.time(),
        )
        d = ctx.to_dict()
        assert d["active_window_title"] == "Refactor auth module — VS Code"
        assert d["active_window_process"] == "Code"
        assert d["browser_url"] == "https://github.com"
        assert d["running_processes"] == 256
        assert len(d["top_processes"]) == 1
        assert d["top_processes"][0]["name"] == "python3"

    def test_format_brief_with_data(self):
        ctx = ScreenContext(
            active_window_process="Code",
            active_window_title="main.py",
            browser_url="https://docs.python.org",
        )
        brief = ctx.format_brief()
        assert "Code" in brief
        assert "main.py" in brief
        assert "docs.python.org" in brief

    def test_format_brief_empty(self):
        ctx = ScreenContext()
        assert ctx.format_brief() == "No screen context available"

    def test_format_block(self):
        ctx = ScreenContext(
            desktop_environment="hyprland",
            active_window_process="Firefox",
            active_window_title="GitHub — Mozilla Firefox",
            browser_url="https://github.com",
            browser_name="Firefox",
            running_processes=300,
        )
        block = ctx.format_block()
        assert "hyprland" in block
        assert "Firefox" in block
        assert "github.com" in block
        assert "300" in block

    def test_format_block_with_clipboard(self):
        ctx = ScreenContext(
            clipboard_text="some copied text",
            clipboard_source="wl-paste",
        )
        block = ctx.format_block()
        assert "Clipboard" in block
        assert "some copied text" in block

    def test_format_block_with_ocr(self):
        ctx = ScreenContext(
            ocr_available=True,
            screen_text="Hello world from the screen",
        )
        block = ctx.format_block()
        assert "OCR" in block
        assert "Hello world" in block


# ---------------------------------------------------------------------------
# Desktop environment detection
# ---------------------------------------------------------------------------


class TestDetectDesktop:
    def test_returns_string(self):
        de = _detect_desktop()
        assert isinstance(de, str)
        assert de in ("hyprland", "wayland", "x11", "unknown")


# ---------------------------------------------------------------------------
# Browser URL extraction
# ---------------------------------------------------------------------------


class TestBrowserUrlExtraction:
    def test_brave_dash(self):
        url, browser = _extract_browser_url("GitHub - Brave", "")
        assert browser == "Brave"
        assert "GitHub" in url

    def test_chrome_dash(self):
        url, browser = _extract_browser_url("Stack Overflow - Google Chrome", "")
        assert browser == "Chrome"

    def test_firefox_emdash(self):
        url, browser = _extract_browser_url("Python.org — Firefox", "")
        assert browser == "Firefox"

    def test_class_based_detection(self):
        url, browser = _extract_browser_url("any title", "Brave-browser")
        assert browser == "Brave"
        assert "any title" in url

    def test_class_chromium(self):
        url, browser = _extract_browser_url("any title", "Chromium-browser")
        assert "Chromium" in browser

    def test_no_match(self):
        url, browser = _extract_browser_url("alacritty", "")
        assert url == ""
        assert browser == ""

    def test_empty_title(self):
        url, browser = _extract_browser_url("", "")
        assert url == ""
        assert browser == ""


# ---------------------------------------------------------------------------
# collect_screen_context — integration-level
# ---------------------------------------------------------------------------


class TestCollectScreenContext:
    def test_collect_basic(self):
        """Collect a screen context and verify basic fields."""
        ctx = collect_screen_context(include_clipboard=False)
        assert isinstance(ctx, ScreenContext)
        assert ctx.collected_at > 0
        # Desktop environment should be detected.
        assert ctx.desktop_environment in ("hyprland", "wayland", "x11", "unknown")

    def test_collect_no_clipboard(self):
        """Clipboard should be empty when include_clipboard=False."""
        ctx = collect_screen_context(include_clipboard=False)
        assert ctx.clipboard_text == ""
        assert ctx.clipboard_source == ""

    def test_ocr_disabled_by_default(self):
        """OCR should be False by default."""
        ctx = collect_screen_context()
        assert ctx.screen_text == ""
        # ocr_available may be True if tesseract exists, but screen_text
        # should be empty since we didn't request OCR.
        assert ctx.screen_text == ""

    def test_collect_returns_valid_types(self):
        """Verify type contracts on all fields."""
        ctx = collect_screen_context(include_clipboard=False)
        assert isinstance(ctx.active_window_title, str)
        assert isinstance(ctx.active_window_class, str)
        assert isinstance(ctx.active_window_pid, int)
        assert isinstance(ctx.running_processes, int)
        assert isinstance(ctx.top_processes, list)
        assert isinstance(ctx.desktop_environment, str)


# ---------------------------------------------------------------------------
# Screen change detection
# ---------------------------------------------------------------------------


class TestScreenChangeDetection:
    def test_no_changes_when_identical(self):
        from friday.screen import detect_screen_changes, ScreenChange
        prev = ScreenContext(
            active_window_process="Code",
            browser_url="https://github.com",
            clipboard_text="git push",
        )
        curr = ScreenContext(
            active_window_process="Code",
            browser_url="https://github.com",
            clipboard_text="git push",
        )
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 0

    def test_detects_app_switch(self):
        from friday.screen import detect_screen_changes
        prev = ScreenContext(active_window_process="Code")
        curr = ScreenContext(active_window_process="Brave")
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 1
        assert changes[0].change_type == "app_switch"
        assert changes[0].old_value == "Code"
        assert changes[0].new_value == "Brave"

    def test_detects_first_app(self):
        from friday.screen import detect_screen_changes
        prev = ScreenContext()
        curr = ScreenContext(active_window_process="Code")
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 1
        assert changes[0].change_type == "app_switch"
        assert changes[0].new_value == "Code"

    def test_detects_url_change(self):
        from friday.screen import detect_screen_changes
        prev = ScreenContext(browser_url="https://github.com", browser_name="Brave")
        curr = ScreenContext(browser_url="https://stackoverflow.com", browser_name="Brave")
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 1
        assert changes[0].change_type == "url_change"
        assert changes[0].old_value == "https://github.com"

    def test_detects_clipboard_change(self):
        from friday.screen import detect_screen_changes
        prev = ScreenContext(clipboard_text="old content")
        curr = ScreenContext(clipboard_text="new content 123")
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 1
        assert changes[0].change_type == "clipboard_change"

    def test_ignores_trivial_clipboard_changes(self):
        """Whitespace-only differences should not trigger."""
        from friday.screen import detect_screen_changes
        prev = ScreenContext(clipboard_text="some text")
        curr = ScreenContext(clipboard_text="  some text   ")
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 0

    def test_detects_multiple_changes(self):
        from friday.screen import detect_screen_changes
        prev = ScreenContext(
            active_window_process="Alacritty",
            browser_url="https://docs.python.org",
            clipboard_text="old",
        )
        curr = ScreenContext(
            active_window_process="Brave",
            browser_url="https://github.com",
            clipboard_text="new clipboard value",
        )
        changes = detect_screen_changes(prev, curr)
        assert len(changes) == 3
        types = {c.change_type for c in changes}
        assert "app_switch" in types
        assert "url_change" in types
        assert "clipboard_change" in types

    def test_screen_change_dataclass(self):
        from friday.screen import ScreenChange
        sc = ScreenChange(
            change_type="app_switch",
            old_value="Code",
            new_value="Brave",
            detail="Switched from Code to Brave",
        )
        assert sc.change_type == "app_switch"
        assert sc.old_value == "Code"
        assert sc.new_value == "Brave"
        assert "Switched from" in sc.detail
