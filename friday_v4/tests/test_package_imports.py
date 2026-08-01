"""Package import-safety tests.

Every ``friday_v4`` subpackage must be importable without side effects —
including the future-wave stubs (mobile, collab, security, intelligence,
network) whose internal modules aren't built yet. This guards against the
``ide/`` landmine class of bug where a stub package crashed on import.
"""

from __future__ import annotations

import importlib

import pytest


class TestPackageImports:
    @pytest.mark.parametrize(
        "pkg",
        [
            "friday_v4",
            "friday_v4.voice",
            "friday_v4.desktop",
            "friday_v4.desktop.ide",
            "friday_v4.proactive",
            "friday_v4.mobile",
            "friday_v4.collab",
            "friday_v4.security",
            "friday_v4.intelligence",
            "friday_v4.network",
        ],
    )
    def test_package_imports_clean(self, pkg: str):
        module = importlib.import_module(pkg)
        assert module is not None
        # Stub packages should advertise their planned contents
        assert hasattr(module, "__all__")

    def test_stub_packages_graceful(self):
        """Stubs construct cleanly and report planned-but-not-built state.

        Wave 4 (intelligence) has shipped, so it now reports available;
        the remaining waves (3/5/7/network) are still planned-but-not-built
        and must report ``is_available() is False``.
        """
        for pkg in (
            "friday_v4.mobile",
            "friday_v4.collab",
            "friday_v4.security",
            "friday_v4.network",
        ):
            module = importlib.import_module(pkg)
            assert callable(getattr(module, "is_available"))
            assert module.is_available() is False

    def test_intelligence_available_after_wave4(self):
        """Wave 4 shipped — the intelligence layer is importable and reports
        itself as available (stub-state marker flipped)."""
        module = importlib.import_module("friday_v4.intelligence")
        assert module.is_available() is True

    def test_desktop_exports(self):
        """The desktop package exports the Wave 2 surface."""
        from friday_v4.desktop import (
            DesktopAbstraction,
            WindowManager,
            DesktopNotificationChannel,
            GlobalHotkeys,
            SystemTray,
        )
        assert WindowManager is not None
        assert DesktopAbstraction is not None
        assert SystemTray is not None
        assert GlobalHotkeys is not None
        assert DesktopNotificationChannel is not None
