"""Web dashboard — `friday6 web` local UI over the V4 subsystems.

Serves a self-contained dark dashboard (pure-stdlib ``http.server``, no
FastAPI/Flask — consistent with the Wave 3/4 "pure-stdlib, always works"
philosophy) that visualizes daemon, security, intelligence, proactive,
V3-bridge, and voice status in the browser.

Data comes from the same guarded accessors the CLIs use (daemon status
file, security scanner state file, intelligence stores, read-only V3
bridge), so the dashboard renders even when a subsystem is missing.
"""

from __future__ import annotations

from . import dashboard, server

__all__ = ["dashboard", "server"]
