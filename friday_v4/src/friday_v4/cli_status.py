"""Status probes + `friday4 db status` for the V4 CLI.

The unified ``friday4 status`` command lives in ``cli_doctor.cmd_status``
(which already registered the top-level ``status`` parser) and consumes the
subsystem probes defined here — so there is exactly one ``status`` command.
This module contributes the probes plus the ``friday4 db status`` command
for the Wave 9.0 V4 state database.

Design laws: never crash (every probe is guarded), never touch the real
``~/.friday`` in tests, stdlib-only.

Usage:
    friday4 db status [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.cli_status")

# Terminal UI helpers (shared style with cli_talk / cli_daemon).
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"


def _print_logo(title: str):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _row(key: str, value: str, ok: Optional[bool] = True):
    """Print one status row with a colored check/cross/dot."""
    if ok is True:
        icon = f"{_GREEN}✔{_RESET}"
    elif ok is False:
        icon = f"{_RED}✘{_RESET}"
    else:
        icon = f"{_YELLOW}◐{_RESET}"
    print(f"  {icon} {key:<12} {_DIM}{value}{_RESET}")


#: Ordered (name, probe-function-name) pairs for the unified status
#: overview. Names (not bound functions) are stored so tests can monkeypatch
#: ``_probe_*`` and ``print_status_rows`` still picks them up. The
#: `friday4 status` command in cli_doctor delegates here — one source of
#: truth shared by both modules.
STATUS_PROBES: list[tuple[str, str]] = [
    ("daemon", "_probe_daemon"),
    ("voice", "_probe_voice"),
    ("desktop", "_probe_desktop"),
    ("ide", "_probe_ide"),
    ("security", "_probe_security"),
    ("proactive", "_probe_proactive"),
    ("intelligence", "_probe_intelligence"),
    ("web", "_probe_web"),
    ("collab", "_probe_collab"),
    ("db", "_probe_db"),
    ("v3", "_probe_v3"),
    ("mobile", "_probe_mobile"),
]


# ──────────────────────────────────────────────────────────────────────────
# Subsystem probes (each returns (ok, detail) and never raises)
# ──────────────────────────────────────────────────────────────────────────


def _probe_daemon() -> tuple[Optional[bool], str]:
    try:
        from .daemon import is_running, read_status
    except Exception as exc:
        return None, f"daemon module unavailable: {exc}"
    running = is_running()
    status = read_status()
    if running:
        uptime = status.get("uptime_seconds", 0)
        up = (f"{uptime / 60:.1f} min" if uptime >= 60 else f"{uptime:.0f}s")
        comps = status.get("components", {})
        up_count = sum(1 for v in comps.values() if v)
        return True, (f"running (pid {status.get('pid', '?')}, up {up}, "
                      f"{up_count}/{len(comps) or '?'} components up)")
    if status:
        return False, "status file stale — not running"
    return False, "not running (start with `friday4 daemon start`)"


def _probe_voice() -> tuple[Optional[bool], str]:
    # Side-effect-free: never construct TextToSpeech here — its kokoro
    # provider starts a background model download (~2 GB) on construction.
    # A status probe must only import and read config.
    try:
        import friday_v4.voice  # noqa: F401 — import check only
    except Exception as exc:
        return None, f"voice module unavailable: {exc}"
    try:
        from .config import load_config
        provider = load_config().voice.tts_provider or "auto"
    except Exception:
        provider = "auto"
    return True, f"voice stack installed (configured provider: {provider})"


def _probe_desktop() -> tuple[Optional[bool], str]:
    try:
        from .desktop.wm_abstraction import (
            WindowManager,
            detect_desktop_environment,
        )
    except Exception as exc:
        return None, f"desktop module unavailable: {exc}"
    try:
        de = detect_desktop_environment()
        wm = WindowManager()
        available = wm.is_available
    except Exception:
        de = detect_desktop_environment()
        available = False
    if available:
        return True, f"abstraction ready ({de})"
    return False, f"no adapter for environment '{de}'"


def _probe_ide() -> tuple[Optional[bool], str]:
    """Wave 6 IDE integration. A missing editor is informational — the
    always-works AST analyzer still diagnoses code."""
    try:
        from .desktop.ide import detect
    except Exception as exc:
        return None, f"ide module unavailable: {exc}"
    try:
        ide = detect()
    except Exception:
        ide = None
    if ide is not None:
        lsp = "LSP-capable" if ide.lsp_capable else "open/reveal control"
        return True, (f"detected: {ide.name} ({ide.kind}, {lsp})")
    return None, "no editor detected (LSP + AST analysis still available)"


def _probe_security() -> tuple[Optional[bool], str]:
    state_file = Path.home() / ".friday" / "v4_security_last.json"
    try:
        from .security.scanner import VulnerabilityScanner
    except Exception as exc:
        return None, f"security module unavailable: {exc}"
    scans = 0
    grade = "—"
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text())
            scans = int(state.get("scans", 0) or 0)
            report = state.get("report") or {}
            grade = report.get("grade", "—")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(f"security state unreadable: {exc}")
    if scans:
        return True, f"{scans} scan(s) run, last grade: {grade}"
    return True, "ready (no scans yet — `friday4 security scan`)"


def _probe_proactive() -> tuple[Optional[bool], str]:
    try:
        from .proactive.anticipation import AnticipationEngine
    except Exception as exc:
        return None, f"proactive module unavailable: {exc}"
    return True, "anticipation engine ready"


def _probe_intelligence() -> tuple[Optional[bool], str]:
    try:
        from .intelligence.drift import DriftPredictor
        from .intelligence.anomaly import AnomalyDetector
    except Exception as exc:
        return None, f"intelligence module unavailable: {exc}"
    return True, "drift + anomaly detectors ready"


def _probe_web() -> tuple[Optional[bool], str]:
    """Dashboard liveness. Not running is informational, not a failure."""
    try:
        s = socket.create_connection(("127.0.0.1", 8899), timeout=0.4)
        s.close()
        return True, "dashboard running on http://127.0.0.1:8899"
    except OSError:
        return None, "not running (`friday4 web`)"
    except Exception as exc:
        return None, f"probe failed: {exc}"


def _probe_collab() -> tuple[Optional[bool], str]:
    try:
        from .collab import Coordinator
    except Exception as exc:
        return None, f"collab module unavailable: {exc}"
    state_dir = Path.home() / ".friday" / "collab" / "state.json"
    peers = 0
    try:
        if state_dir.exists():
            state = json.loads(state_dir.read_text())
            peers = len(state.get("last_peers", []) or [])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(f"collab state unreadable: {exc}")
    return True, f"ready ({peers} known peer(s))"


def _probe_db() -> tuple[Optional[bool], str]:
    """V4 state DB. Not-yet-created is informational, not a failure
    (the DB is created lazily on first use)."""
    try:
        from . import db
        info = db.db_status()
    except Exception as exc:
        return None, f"db module unavailable: {exc}"
    if not info["exists"]:
        return None, f"{info['path']} — not created yet (created on first use)"
    tables = ", ".join(f"{t}:{c}" for t, c in info["tables"].items())
    return True, (f"schema v{info['schema_version']}, "
                  f"{len(info['tables'])} tables ({tables}), "
                  f"{info['total_rows']} rows")


def _probe_v3() -> tuple[Optional[bool], str]:
    """Legacy V3 data bridge (read-only). Absent V3 is informational,
    not a failure — V4 is fully standalone without it."""
    try:
        from .proactive.v3source import V3DataSource
        src = V3DataSource()
        if src.is_available():
            return True, "V3 bridge connected (~/.friday/friday.db)"
        return None, "no V3 DB found (optional legacy data)"
    except Exception as exc:
        return None, f"v3 module unavailable: {exc}"


def _probe_mobile() -> tuple[Optional[bool], str]:
    """Mobile companion transport (Wave 15). Not running is informational."""
    try:
        from .mobile import PushNotificationService, create_api_server
    except Exception as exc:
        return None, f"mobile module unavailable: {exc}"
    try:
        s = socket.create_connection(("127.0.0.1", 8900), timeout=0.4)
        s.close()
        return True, "companion API running on http://127.0.0.1:8900"
    except OSError:
        return None, "not running (`friday4 mobile serve`)"
    except Exception as exc:
        return None, f"probe failed: {exc}"


# ──────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────


def print_status_rows() -> int:
    """Print every subsystem probe row; returns the overall exit code.

    Shared by ``cli_doctor.cmd_status`` / ``cmd_status`` here so the
    unified overview is built from one source of truth. Probes are looked
    up by name at call time so tests can monkeypatch them. Never raises.
    """
    module = sys.modules[__name__]
    results: list[tuple[str, Optional[bool], str]] = []
    for name, probe_name in STATUS_PROBES:
        try:
            probe = getattr(module, probe_name)
            ok, detail = probe()
        except Exception as exc:  # defensive — probes must never raise
            ok, detail = False, f"probe error: {exc}"
        results.append((name, ok, detail))
        _row(name, detail, ok)

    any_ready = any(ok is True for _, ok, _ in results)
    hard_fail = any(ok is False for _, ok, _ in results)
    info_only = any(ok is None for _, ok, _ in results)

    print()
    if not any_ready:
        print(f"  {_YELLOW}◐{_RESET} No subsystems available — is Friday V4 installed?")
    elif hard_fail:
        print(f"  {_YELLOW}◐{_RESET} Some subsystems unavailable — Friday degrades gracefully.")
    elif info_only:
        print(f"  {_GREEN}✔{_RESET} Core subsystems ready "
              f"(some optional surfaces inactive).")
    else:
        print(f"  {_GREEN}✔{_RESET} All subsystems ready.")
    print()
    return 1 if hard_fail else 0


def cmd_status(args: argparse.Namespace) -> int:
    """Unified V4 layer overview (the `friday4 status` command).

    Registered by ``cli_doctor.build_doctor_parser``; renders every
    subsystem probe (daemon, voice, desktop, security, proactive,
    intelligence, web, collab, db, v3) in one screen.
    """
    _print_logo("Status")
    return print_status_rows()


def cmd_db_status(args: argparse.Namespace) -> int:
    """Show V4 state DB schema + row counts."""
    _print_logo("Database")
    try:
        from . import db
        info = db.db_status(args.db)
    except Exception as exc:
        print(f"  {_RED}✘{_RESET} db module unavailable: {exc}")
        return 1

    _row("path", info["path"])
    if not info["exists"]:
        _row("state", "not created yet (created on first use)", None)
        print()
        return 0

    _row("schema", f"v{info['schema_version']}")
    _row("tables", str(len(info["tables"])))
    _row("total rows", str(info["total_rows"]))
    print(f"\n  {_DIM}Tables{_RESET}")
    for name, count in info["tables"].items():
        print(f"    {name:<20} {count}")
    print()
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Argument parsers
# ──────────────────────────────────────────────────────────────────────────


def build_db_parser(subparsers) -> None:
    """Register `friday4 db` (Wave 9.0 state database management)."""
    parser = subparsers.add_parser(
        "db", help="V4 state database",
        description="Inspect Friday V4's sqlite state database "
                    "(missions, actions, memories, relationships, skills, "
                    "sessions).",
    )
    db_sub = parser.add_subparsers(dest="db_command")

    p = db_sub.add_parser("status", help="Show DB schema + row counts")
    p.add_argument("--db", type=Path, default=None,
                   help="Path to the V4 state DB (default: "
                        "~/.friday/v4.db or $FRIDAY_V4_DB)")
    p.set_defaults(func=cmd_db_status)


if __name__ == "__main__":  # pragma: no cover - standalone entry
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 db")
    sub = parser.add_subparsers(dest="command")
    build_db_parser(sub)
    args = parser.parse_args()
    if hasattr(args, "func"):
        raise SystemExit(args.func(args) or 0)
    parser.print_help()
