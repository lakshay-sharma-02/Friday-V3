"""Desktop abstraction tests — Wave 2.

Tests are unit-level with mocked subprocess/hyprctl output (no live
desktop required). Covers the adapter registry, platform adapters,
the WindowManager facade, resolver, tray, hotkeys, and notifier.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ==========================================================================
# SmartWindowResolver
# ==========================================================================


class TestSmartWindowResolver:
    def test_resolve_direct_class(self):
        from friday_v4.desktop.wm_abstraction import (
            SmartWindowResolver,
            WindowInfo,
        )
        windows = [WindowInfo(app_class="firefox", title="Mozilla Firefox")]
        assert SmartWindowResolver.resolve("firefox", windows) == "firefox"

    def test_resolve_semantic(self):
        from friday_v4.desktop.wm_abstraction import (
            SmartWindowResolver,
            WindowInfo,
        )
        windows = [
            WindowInfo(app_class="firefox", title="Mozilla Firefox"),
            WindowInfo(app_class="kitty", title="kitty — main.py"),
            WindowInfo(app_class="Code", title="codebuff — package.json"),
        ]
        assert SmartWindowResolver.resolve("code editor", windows) == "kitty"
        assert SmartWindowResolver.resolve("browser", windows) == "firefox"

    def test_resolve_title_substring(self):
        from friday_v4.desktop.wm_abstraction import (
            SmartWindowResolver,
            WindowInfo,
        )
        windows = [WindowInfo(app_class="obsidian", title="My Vault - Obsidian")]
        assert SmartWindowResolver.resolve("vault", windows) == "obsidian"

    def test_resolve_no_match(self):
        from friday_v4.desktop.wm_abstraction import (
            SmartWindowResolver,
            WindowInfo,
        )
        windows = [WindowInfo(app_class="thunar", title="File Manager")]
        assert SmartWindowResolver.resolve("blender", windows) is None

    def test_suggest_for_window(self):
        from friday_v4.desktop.wm_abstraction import (
            SmartWindowResolver,
            WindowInfo,
        )
        w = WindowInfo(app_class="Code", title="x")
        suggestions = SmartWindowResolver.suggest_for_window(w)
        assert "vs code" in suggestions
        assert "editor" in suggestions


# ==========================================================================
# Desktop environment detection
# ==========================================================================


class TestDetectDesktopEnvironment:
    def test_hyprland(self, monkeypatch):
        from friday_v4.desktop.wm_abstraction import detect_desktop_environment
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc123")
        assert detect_desktop_environment() == "hyprland"

    def test_gnome_wayland(self, monkeypatch):
        from friday_v4.desktop.wm_abstraction import detect_desktop_environment
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        assert detect_desktop_environment() == "gnome"

    def test_kde_x11(self, monkeypatch):
        from friday_v4.desktop.wm_abstraction import detect_desktop_environment
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        assert detect_desktop_environment() == "kde"

    def test_macos(self, monkeypatch):
        from friday_v4.desktop.wm_abstraction import detect_desktop_environment
        monkeypatch.setattr(sys, "platform", "darwin")
        assert detect_desktop_environment() == "macos"

    def test_windows(self, monkeypatch):
        from friday_v4.desktop.wm_abstraction import detect_desktop_environment
        monkeypatch.setattr("os.name", "nt")
        assert detect_desktop_environment() == "windows"

    def test_unknown(self, monkeypatch):
        from friday_v4.desktop.wm_abstraction import detect_desktop_environment
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        assert detect_desktop_environment() == "unknown"


# ==========================================================================
# HyprlandAdapter (mocked hyprctl)
# ==========================================================================


class TestHyprlandAdapter:
    @patch("friday_v4.desktop.hyprland_adapter.HyprlandAdapter._run_hyprctl")
    def test_list_windows(self, mock_run, monkeypatch):
        from friday_v4.desktop.hyprland_adapter import HyprlandAdapter
        mock_run.side_effect = (
            json.dumps([{"address": "0x1", "title": "t", "class": "kitty",
                         "workspace": {"id": 1, "name": "1"}, "at": [0, 0],
                         "size": [100, 100], "monitor": 0, "pid": 1,
                         "floating": False, "fullscreen": 0}]),
            json.dumps({"address": "0x1", "class": "kitty", "title": "t"}),
        )
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
        adapter = HyprlandAdapter()
        windows = adapter.list_windows()
        assert len(windows) == 1
        assert windows[0].app_class == "kitty"
        assert windows[0].is_active is True

    @patch("friday_v4.desktop.hyprland_adapter.HyprlandAdapter._run_hyprctl")
    def test_list_workspaces(self, mock_run):
        from friday_v4.desktop.hyprland_adapter import HyprlandAdapter
        mock_run.side_effect = (
            json.dumps([{"id": 1, "name": "1", "monitor": "eDP-1",
                         "windows": 2, "lastwindowtitle": "kitty"}]),
            json.dumps({"id": 1, "name": "1"}),
        )
        adapter = HyprlandAdapter()
        workspaces = adapter.list_workspaces()
        assert len(workspaces) == 1
        assert workspaces[0].is_active is True
        assert workspaces[0].window_count == 2

    @patch("friday_v4.desktop.hyprland_adapter.HyprlandAdapter._run_hyprctl")
    def test_monitors(self, mock_run):
        from friday_v4.desktop.hyprland_adapter import HyprlandAdapter
        mock_run.return_value = json.dumps(
            [{"name": "eDP-1", "width": 1366, "height": 768,
              "refreshRate": 60.0, "focused": True,
              "activeWorkspace": {"id": 1}, "scale": 1.0,
              "make": "LGD", "model": "eDP"}]
        )
        adapter = HyprlandAdapter()
        monitors = adapter.list_monitors()
        assert len(monitors) == 1
        assert monitors[0].name == "eDP-1"
        assert monitors[0].is_active is True

    @patch("friday_v4.desktop.hyprland_adapter.subprocess.run")
    def test_switch_workspace(self, mock_run, monkeypatch):
        from friday_v4.desktop.hyprland_adapter import HyprlandAdapter
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
        adapter = HyprlandAdapter()
        assert adapter.switch_workspace(3) is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0].endswith("hyprctl")
        assert args[1] == "dispatch"
        assert args[2] == "workspace"
        assert args[3] == "3"

    @patch("friday_v4.desktop.hyprland_adapter.subprocess.run")
    def test_focus(self, mock_run, monkeypatch):
        from friday_v4.desktop.hyprland_adapter import HyprlandAdapter
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
        adapter = HyprlandAdapter()
        assert adapter.focus("firefox", "class") is True
        args = mock_run.call_args[0][0]
        assert args[3] == "class:firefox"

    def test_unavailable_without_session(self, monkeypatch):
        from friday_v4.desktop.hyprland_adapter import HyprlandAdapter
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        adapter = HyprlandAdapter()
        assert adapter.is_available() is False


# ==========================================================================
# WindowManager facade
# ==========================================================================


class TestWindowManagerFacade:
    @patch("friday_v4.desktop.wm_abstraction.create_adapter")
    def test_delegates_to_adapter(self, mock_create):
        from friday_v4.desktop.wm_abstraction import WindowManager
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.desktop_environment = "hyprland"
        mock_create.return_value = adapter

        wm = WindowManager()
        assert wm.is_available is True
        wm.list_windows()
        adapter.list_windows.assert_called_once()
        wm.switch_workspace(2)
        adapter.switch_workspace.assert_called_once_with(2)

    def test_notify_static(self):
        from friday_v4.desktop.wm_abstraction import WindowManager
        # Static method should exist and be callable (platform-dependent result)
        assert callable(WindowManager.notify)


# ==========================================================================
# create_adapter registry
# ==========================================================================


class TestAdapterRegistry:
    @patch("friday_v4.desktop.hyprland_adapter.HyprlandAdapter")
    def test_create_hyprland(self, mock_cls):
        from friday_v4.desktop.wm_abstraction import create_adapter
        create_adapter("hyprland")
        mock_cls.assert_called_once()

    @patch("friday_v4.desktop.gnome_adapter.GNOMEAdapter")
    def test_create_gnome(self, mock_cls):
        from friday_v4.desktop.wm_abstraction import create_adapter
        create_adapter("gnome")
        mock_cls.assert_called_once()

    @patch("friday_v4.desktop.kde_adapter.KDEAdapter")
    def test_create_kde(self, mock_cls):
        from friday_v4.desktop.wm_abstraction import create_adapter
        create_adapter("kde")
        mock_cls.assert_called_once()

    @patch("friday_v4.desktop.macos_adapter.MacOSAdapter")
    def test_create_macos(self, mock_cls):
        from friday_v4.desktop.wm_abstraction import create_adapter
        create_adapter("macos")
        mock_cls.assert_called_once()

    @patch("friday_v4.desktop.windows_adapter.WindowsAdapter")
    def test_create_windows(self, mock_cls):
        from friday_v4.desktop.wm_abstraction import create_adapter
        create_adapter("windows")
        mock_cls.assert_called_once()

    def test_create_unknown_returns_base(self):
        from friday_v4.desktop.wm_abstraction import (
            DesktopAbstraction,
            create_adapter,
        )
        adapter = create_adapter("weird-de")
        assert isinstance(adapter, DesktopAbstraction)
        assert adapter.is_available() is False

    def test_supported_platforms(self):
        from friday_v4.desktop.wm_abstraction import SUPPORTED_PLATFORMS
        assert SUPPORTED_PLATFORMS == ["hyprland", "gnome", "kde", "macos", "windows"]


# ==========================================================================
# GNOME / KDE / macOS / Windows adapters (graceful degradation)
# ==========================================================================


class TestOtherAdapters:
    def test_gnome_unavailable_without_tools(self, monkeypatch):
        from friday_v4.desktop.gnome_adapter import GNOMEAdapter
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with patch.object(GNOMEAdapter, "_run") as mock_run:
            mock_run.return_value = None
            adapter = GNOMEAdapter()
            # Simulate no gdbus / wmctrl / xdotool
            adapter._has_gdbus = False
            adapter._has_wmctrl = False
            adapter._has_xdotool = False
            assert adapter.is_available() is False
            assert adapter.list_windows() == []
            assert adapter.list_workspaces() == []
            assert adapter.get_active_window() is None

    def test_gnome_wmctrl_windows(self, monkeypatch):
        from friday_v4.desktop.gnome_adapter import GNOMEAdapter
        adapter = GNOMEAdapter()
        adapter._is_wayland = False
        adapter._has_wmctrl = True
        adapter._has_xdotool = True
        with patch.object(GNOMEAdapter, "_run") as mock_run:
            mock_run.side_effect = [
                (  # wmctrl -lx
                    "0x04000007 0 host.example.com kitty kitty — main.py\n"
                    "0x04000008 1 host.example.com firefox Mozilla Firefox\n"
                ),
                "0x00000000",  # xdotool getactivewindow
            ]
            windows = adapter.list_windows()
            assert len(windows) == 2
            assert windows[0].app_class == "kitty"
            assert windows[0].workspace_id == 1  # 0-indexed + 1

    def test_kde_unavailable(self):
        from friday_v4.desktop.kde_adapter import KDEAdapter
        adapter = KDEAdapter()
        adapter._has_wmctrl = False
        adapter._has_qdbus = False
        assert adapter.is_available() is False

    def test_macos_unavailable_off_platform(self, monkeypatch):
        from friday_v4.desktop.macos_adapter import MacOSAdapter
        monkeypatch.setattr(sys, "platform", "linux")
        adapter = MacOSAdapter()
        assert adapter.is_available() is False
        assert adapter.switch_workspace(1) is False

    def test_macos_permission_instructions(self):
        from friday_v4.desktop.macos_adapter import MacOSAdapter
        adapter = MacOSAdapter()
        instructions = adapter.setup_instructions()
        assert "Accessibility" in instructions

    def test_windows_unavailable_off_platform(self, monkeypatch):
        from friday_v4.desktop.windows_adapter import WindowsAdapter
        monkeypatch.setattr("os.name", "posix")
        adapter = WindowsAdapter()
        assert adapter.is_available() is False


# ==========================================================================
# SystemTray (graceful when pystray missing)
# ==========================================================================


class TestSystemTray:
    @patch("friday_v4.desktop.tray.SystemTray._check_available", return_value=False)
    def test_unavailable_graceful(self, mock_avail):
        from friday_v4.desktop.tray import SystemTray
        tray = SystemTray()
        assert tray.available is False
        assert tray.start() is False  # doesn't crash
        tray.stop()  # doesn't crash

    def test_repr(self):
        from friday_v4.desktop.tray import SystemTray
        tray = SystemTray(feed_count=5)
        assert "feed_count=5" in repr(tray)


# ==========================================================================
# GlobalHotkeys (graceful when keyboard missing)
# ==========================================================================


class TestGlobalHotkeys:
    @patch("friday_v4.desktop.hotkeys.GlobalHotkeys._check_available", return_value=False)
    def test_unavailable_graceful(self, mock_avail):
        from friday_v4.desktop.hotkeys import GlobalHotkeys
        hotkeys = GlobalHotkeys()
        assert hotkeys.available is False
        assert hotkeys.start() is False
        hotkeys.stop()  # doesn't crash

    @patch("friday_v4.desktop.hotkeys.GlobalHotkeys._check_available", return_value=True)
    def test_register_with_keyboard(self, mock_avail):
        from friday_v4.desktop.hotkeys import GlobalHotkeys
        keyboard_mock = MagicMock()
        hotkeys = GlobalHotkeys(
            on_push_to_talk=lambda: None,
            on_status=lambda: None,
        )
        hotkeys._keyboard = keyboard_mock
        assert hotkeys.start() is True
        assert len(hotkeys.registered) == 2
        hotkeys.stop()
        assert hotkeys.registered == []


# ==========================================================================
# DesktopNotificationChannel (mocked V3 ambient)
# ==========================================================================


class TestNotifier:
    def test_poll_once_notifies_new_events(self, tmp_path):
        from friday_v4.desktop.notifier import DesktopNotificationChannel

        class FakeEvent:
            def __init__(self, eid, priority, title, event_type, detail, project):
                self.id = eid
                self.priority = priority
                self.title = title
                self.event_type = event_type
                self.detail = detail
                self.project = project

        events = [
            FakeEvent(1, 0, "No repos changed", "repo_changed", "", ""),
            FakeEvent(2, 2, "3/8 repos changed", "repo_changed", "detail here", "codebuff"),
            FakeEvent(3, 3, "Cycle failed", "cycle_failed", "boom", ""),
        ]
        feed = list(reversed(events))  # newest first, like get_feed

        ambient_mock = MagicMock()
        ambient_mock.get_feed.return_value = feed
        connect_mock = MagicMock()
        connect_mock.return_value = MagicMock()

        channel = DesktopNotificationChannel(
            min_priority=1,
            state_file=tmp_path / "state.json",
        )
        channel._v3 = {"ambient": ambient_mock, "connect": connect_mock}

        with patch("friday_v4.desktop.notifier.DesktopAbstraction.notify") as notify:
            count = channel.poll_once()

        # Event 2 notified (priority 2 >= 1); event 1 (pri 0) skipped;
        # event 3 (cycle_failed) in _SILENT_TYPES skipped.
        assert count == 1
        notify.assert_called_once()
        # last seen id persisted
        assert channel.last_event_id == 3

    def test_poll_once_without_v3(self, tmp_path):
        from friday_v4.desktop.notifier import DesktopNotificationChannel
        channel = DesktopNotificationChannel(state_file=tmp_path / "s.json")
        channel._v3 = False
        assert channel.poll_once() == 0

    def test_start_stop(self, tmp_path):
        from friday_v4.desktop.notifier import DesktopNotificationChannel
        channel = DesktopNotificationChannel(
            poll_interval=0.01,
            state_file=tmp_path / "s.json",
        )
        # Prevent the loop from touching the real V3 DB during the test
        channel._v3 = False
        assert channel.start() is True
        channel.stop()
        assert channel.running is False
