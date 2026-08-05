"""Mobile companion tray — one icon, Friday reachable from anywhere (Wave 22).

Like 9router's tray: a persistent icon shows the companion server is up,
the menu opens the dashboard / prints the remote URLs / generates a
pairing code / stops the server. Runs inside the ``friday4 mobile serve
--tray`` process (and via the autostart entry, on every login).

Reuses ``desktop.tray.SystemTray`` (pystray) — the icon, the background
thread, and the graceful "pystray missing → unavailable, never crash"
behavior are all inherited; only the menu is mobile-specific.

Usage:
    tray = MobileTray(base_url=\"http://0.0.0.0:8900\", db_path=...,
                      on_stop=lambda: server.shutdown())
    tray.start()          # background thread, returns immediately
    tray.update_urls(tailscale=\"100.64.0.5\", tunnel=\"https://…\")
    tray.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.mobile.tray")

_RESET = "\033[0m"
_CYAN = "\033[96m"


class MobileTray:
    """The companion server's system tray (mobile menu, SystemTray engine)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8900",
        db_path: Optional[str] = None,
        on_stop: Optional[Callable[[], None]] = None,
    ):
        self._base_url = base_url
        self._db_path = db_path
        self._on_stop = on_stop
        self._tray = None
        self._tailscale_url: Optional[str] = None
        self._tunnel_url: Optional[str] = None

    # ── availability ─────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        from ..desktop.tray import SystemTray
        probe = SystemTray()
        return probe.available

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, daemon: bool = True) -> bool:
        """Show the tray icon (reuses SystemTray's engine + thread)."""
        from ..desktop.tray import SystemTray

        tray = SystemTray(
            title="Friday Companion",
            daemon_state="running",
            menu_items=self._menu_items(),
            on_quit=self._on_stop,
        )
        if not tray.available:
            return False
        self._tray = tray
        return tray.start(daemon=daemon)

    def stop(self) -> None:
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception as exc:
                logger.debug(f"mobile tray stop failed: {exc}")
            self._tray = None

    def update_urls(self, tailscale_url: Optional[str] = None,
                    tunnel_url: Optional[str] = None) -> None:
        """Refresh the reachable-URL info shown in the tooltip."""
        self._tailscale_url = tailscale_url
        self._tunnel_url = tunnel_url
        if self._tray is not None:
            state = self._reach_label()
            try:
                self._tray.update_daemon_state(state)
            except Exception:
                pass

    # ── menu ─────────────────────────────────────────────────────────

    def _menu_items(self) -> list:
        # ASCII labels only — pystray's X11 backend encodes menus as
        # latin-1 and crashes on emoji/em-dashes (the never-crash law).
        return [
            ("Open dashboard", self._open_dashboard),
            ("Show remote URLs", self._show_urls),
            ("Pair a device", self._pair),
            ("Status", self._status),
        ]

    def _reach_label(self) -> str:
        if self._tunnel_url:
            return "exposed · tunnel"
        if self._tailscale_url:
            return "reachable anywhere"
        return "running · LAN"

    def _open_dashboard(self) -> None:
        import webbrowser
        try:
            webbrowser.open(self._base_url)
        except Exception as exc:
            logger.debug(f"tray open dashboard failed: {exc}")

    def _show_urls(self) -> None:
        try:
            from .cli_mobile import cmd_mobile_remote
            import argparse
            args = argparse.Namespace(token=None, db=self._db_path)
            cmd_mobile_remote(args)
        except Exception as exc:
            logger.debug(f"tray show urls failed: {exc}")

    def _pair(self) -> None:
        try:
            from .pairing import PairingService
            service = PairingService(db_path=self._db_path)
            code = service.generate()
            print(f"\n  {_CYAN}◆ Your pairing code:{_RESET}  {code}\n"
                  f"  Valid 10 min, single use — enter it in the phone "
                  f"app's Device tab.\n")
        except Exception as exc:
            logger.debug(f"tray pair failed: {exc}")

    def _status(self) -> None:
        try:
            from .api import MobileAPI
            api = MobileAPI(db_path=self._db_path)
            st = api.status()
            ok = bool(st.get("available"))
            print(f"\n  Companion server: {'online' if ok else 'offline'}"
                  f" · {self._reach_label()}"
                  f" · exchanges today: {st.get('exchanges_today', 0)}\n")
        except Exception as exc:
            logger.debug(f"tray status failed: {exc}")
