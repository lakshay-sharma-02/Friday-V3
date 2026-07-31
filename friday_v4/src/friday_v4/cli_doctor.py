"""CLI commands for `friday4 doctor` and `friday4 status`.

doctor — one-command diagnostics across every V4 subsystem.
status  — unified layer overview in one screen.

Exit codes (doctor):
    0  all subsystems healthy
    1  degraded (some subsystems unavailable but core works)
    2  broken (core subsystems failed)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger("friday_v4.cli_doctor")

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


def _line(key: str, value: str, ok: bool = True):
    icon = _GREEN + "✔" if ok else _RED + "✘"
    print(f"  {icon}{_RESET} {key:<22} {_DIM}{value}{_RESET}")


def _section(title: str):
    print(f"\n  {_BOLD}{title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def diag_system() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    try:
        import shutil
        total, used, free = shutil.disk_usage(Path.home())
        disk_gb = free / (1024 ** 3)
        rows.append({"key": "os", "value": f"{platform.system()} "
                     f"{platform.release()}", "ok": True})
        rows.append({"key": "python", "value": platform.python_version(),
                     "ok": True})
        mem = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
        rows.append({"key": "ram", "value": f"{mem:.1f} GB", "ok": True})
        rows.append({"key": "disk free", "value": f"{disk_gb:.1f} GB",
                     "ok": disk_gb > 1})
    except Exception as exc:
        rows.append({"key": "system", "value": f"probe failed: {exc}",
                     "ok": False})
    return all(r["ok"] for r in rows), rows


def diag_v3() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    db = Path.home() / ".friday" / "friday.db"
    if not db.exists():
        rows.append({"key": "db", "value": "~/.friday/friday.db missing",
                     "ok": False})
        return False, rows
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            need = {"observations", "actions", "ambient_feed"}
            ok = need <= tables
            counts = {}
            for t in need:
                try:
                    counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}"
                                             ).fetchone()[0]
                except sqlite3.Error:
                    counts[t] = -1
            rows.append({"key": "tables", "value": ", ".join(
                f"{t}={counts[t]}" for t in sorted(need)), "ok": ok})
        finally:
            conn.close()
    except sqlite3.Error as exc:
        rows.append({"key": "db", "value": f"open failed: {exc}", "ok": False})
        return False, rows
    status = Path.home() / ".friday" / "daemon.status"
    if status.exists():
        try:
            state = json.loads(status.read_text()).get("state", "unknown")
            rows.append({"key": "daemon", "value": f"V3 daemon {state}",
                         "ok": True})
        except (json.JSONDecodeError, OSError):
            rows.append({"key": "daemon", "value": "status unreadable",
                         "ok": False})
    return all(r["ok"] for r in rows), rows


def diag_audio() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    try:
        from friday_v4.voice.audio import list_input_devices, list_output_devices
        ins = list_input_devices()
        outs = list_output_devices()
        rows.append({"key": "input devices", "value": str(len(ins)),
                     "ok": len(ins) > 0})
        rows.append({"key": "output devices", "value": str(len(outs)),
                     "ok": len(outs) > 0})
        if ins:
            rows.append({"key": "default input",
                         "value": ins[0].name if hasattr(ins[0], "name") else str(ins[0]),
                         "ok": True})
        return len(ins) > 0, rows
    except Exception as exc:
        rows.append({"key": "audio", "value": f"probe failed: {exc}",
                     "ok": False})
        return False, rows


def diag_voice() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    try:
        from friday_v4.voice.tts import TextToSpeech, TTSConfig
        tts = TextToSpeech(TTSConfig(primary_provider="auto",
                                     cache_enabled=False))
        for p in tts.list_providers():
            rows.append({"key": f"tts {p['name']}",
                         "value": "available" if p["available"] else "missing",
                         "ok": p["available"]})
        if tts.active_provider_name:
            rows.append({"key": "active provider",
                         "value": tts.active_provider_name, "ok": True})
    except Exception as exc:
        rows.append({"key": "tts", "value": f"probe failed: {exc}",
                     "ok": False})
    # STT availability
    stt_available = False
    try:
        from friday_v4.voice.stt import SpeechToText
        stt = SpeechToText()
        stt_available = bool(stt.is_available)
        rows.append({"key": "stt", "value": "available" if stt_available
                     else "unavailable", "ok": stt_available})
    except Exception as exc:
        rows.append({"key": "stt", "value": f"probe failed: {exc}",
                     "ok": False})
    # Model files
    for name, path in (("piper", Path.home() / ".friday/models/piper/jenny.onnx"),
                       ("kokoro", Path.home() / ".friday/models/kokoro/kokoro-v1.0.onnx")):
        rows.append({"key": f"model {name}",
                     "value": "present" if path.exists() else "not downloaded",
                     "ok": path.exists()})
    # Voice is usable if any TTS provider OR STT is available; missing
    # individual providers are degraded-but-usable, not broken.
    provider_ok = any(r["ok"] for r in rows
                      if r["key"].startswith("tts "))
    usable = provider_ok or stt_available
    return usable, rows


def diag_desktop() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    try:
        from friday_v4.desktop.wm_abstraction import WindowManager
        wm = WindowManager()
        rows.append({"key": "wm", "value": wm.backend_name if
                     hasattr(wm, "backend_name") else str(type(wm).__name__),
                     "ok": wm.is_available})
        if wm.is_available:
            try:
                active = wm.get_active_window()
                rows.append({"key": "active window",
                             "value": f"{active.app_name} — {active.title[:30]}"
                             if active else "none", "ok": True})
            except Exception:
                rows.append({"key": "active window", "value": "probe failed",
                             "ok": False})
    except Exception as exc:
        rows.append({"key": "desktop", "value": f"probe failed: {exc}",
                     "ok": False})
    return bool(rows) and rows[0]["ok"], rows


def diag_proactive() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    sessions = Path.home() / ".friday" / "sessions"
    rows.append({"key": "sessions dir", "value": str(sessions.exists()),
                 "ok": True})
    history = sessions / "history.jsonl"
    count = 0
    if history.exists():
        try:
            count = sum(1 for _ in history.open())
        except OSError:
            pass
    rows.append({"key": "session history", "value": f"{count} sessions",
                 "ok": True})
    try:
        from friday_v4.proactive.pattern_learner import PatternLearner
        stats = PatternLearner().get_stats()
        pairs = stats.get("action_pairs_learned", 0)
        rows.append({"key": "patterns learned", "value": str(pairs),
                     "ok": True})
    except Exception as exc:
        rows.append({"key": "patterns", "value": f"probe failed: {exc}",
                     "ok": False})
    return True, rows


def diag_intelligence() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    try:
        from friday_v4.intelligence.drift import DriftPredictor
        stats = DriftPredictor().get_stats()
        rows.append({"key": "drift metrics", "value": str(stats.get(
            "metric_count", 0)), "ok": True})
    except Exception as exc:
        rows.append({"key": "drift", "value": f"probe failed: {exc}",
                     "ok": False})
    try:
        from friday_v4.intelligence.anomaly import AnomalyDetector
        anoms = AnomalyDetector().get_recent_anomalies(limit=1)
        rows.append({"key": "anomalies", "value": str(len(anoms)),
                     "ok": True})
    except Exception as exc:
        rows.append({"key": "anomalies", "value": f"probe failed: {exc}",
                     "ok": False})
    return True, rows


def diag_daemon() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    try:
        from friday_v4.daemon import is_running, read_status
        running = is_running()
        rows.append({"key": "daemon", "value": "running" if running
                     else "stopped", "ok": True})
        if running:
            status = read_status()
            comps = status.get("components", {})
            for name, up in comps.items():
                rows.append({"key": f"  {name}", "value": "up" if up else "down",
                             "ok": up})
    except Exception as exc:
        rows.append({"key": "daemon", "value": f"probe failed: {exc}",
                     "ok": False})
    return True, rows


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    sections = [
        ("System", diag_system),
        ("Audio", diag_audio),
        ("Voice", diag_voice),
        ("Desktop", diag_desktop),
        ("V3", diag_v3),
        ("Proactive", diag_proactive),
        ("Intelligence", diag_intelligence),
        ("Daemon", diag_daemon),
    ]

    results: dict[str, tuple[bool, list[dict]]] = {}
    for name, fn in sections:
        try:
            results[name] = fn()
        except Exception as exc:
            results[name] = (False, [{"key": "error",
                                      "value": str(exc), "ok": False}])

    if args.json:
        out = {name: {"healthy": ok, "checks": rows}
               for name, (ok, rows) in results.items()}
        print(json.dumps(out, indent=2))
    else:
        _print_logo("Doctor")
        for name, (ok, rows) in results.items():
            _section(name)
            for row in rows:
                _line(row["key"], row["value"], ok=row["ok"])
        print()

    # Exit: broken if core sections failed, degraded if any failed.
    core = ("System", "Desktop", "Voice")
    core_broken = any(not results[n][0] for n in core if n in results)
    any_failed = any(not ok for ok, _ in results.values())
    if core_broken:
        return 2
    if any_failed:
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print_logo("Status")

    # Daemon
    try:
        from friday_v4.daemon import is_running, read_status
        running = is_running()
        _line("daemon", "running" if running else "stopped", ok=True)
        if running:
            status = read_status()
            if status.get("uptime_seconds"):
                _line("uptime", f"{status['uptime_seconds']/60:.1f} min",
                      ok=True)
            _line("notifications",
                  str(status.get("notification_count", 0)), ok=True)
    except Exception:
        _line("daemon", "probe failed", ok=False)

    # Layer statuses (reuse existing CLI commands' data via direct probes).
    _section("Layers")
    try:
        from friday_v4.desktop.wm_abstraction import WindowManager
        wm = WindowManager()
        _line("desktop", wm.backend_name if hasattr(wm, "backend_name")
              else "available", ok=wm.is_available)
    except Exception:
        _line("desktop", "unavailable", ok=False)

    try:
        from friday_v4.voice.tts import TextToSpeech, TTSConfig
        tts = TextToSpeech(TTSConfig(primary_provider="auto",
                                     cache_enabled=False))
        _line("voice", tts.active_provider_name or "no provider",
              ok=bool(tts.active_provider_name))
    except Exception:
        _line("voice", "unavailable", ok=False)

    try:
        from friday_v4.proactive.v3source import V3DataSource
        v3 = V3DataSource()
        _line("v3 data", "connected" if v3.is_available() else "not available",
              ok=v3.is_available())
    except Exception:
        _line("v3 data", "probe failed", ok=False)

    print()
    return 0


def build_doctor_parser(subparsers) -> None:
    """Register `friday4 doctor` and `friday4 status`."""
    p = subparsers.add_parser(
        "doctor", help="Diagnose all V4 subsystems",
        description="One-command health check across audio, voice, desktop, "
                    "V3, proactive, intelligence, and daemon.")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output")
    p.set_defaults(func=cmd_doctor)

    p = subparsers.add_parser(
        "status", help="Unified V4 layer overview",
        description="One screen summary of every V4 layer + daemon state.")
    p.set_defaults(func=cmd_status)
