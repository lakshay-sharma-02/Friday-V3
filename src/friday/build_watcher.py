"""BuildWatcher — build/test pipeline monitoring for System Intelligence.

Watches build commands, parses output for errors/warnings/slow tests,
and tracks build history over time.

Usage::

    watcher = BuildWatcher(conn)
    result = watcher.run_build("cargo build")
    print(result.to_dict())

    history = watcher.get_history(limit=20)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BuildResult:
    """Result of a single build/test run."""

    command: str = ""
    project: str = ""
    success: bool = False
    exit_code: Optional[int] = None
    duration_ms: int = 0
    error_count: int = 0
    warning_count: int = 0
    slow_test_count: int = 0
    output_text: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timestamp: str = ""

    @classmethod
    def from_output(cls, command: str, returncode: int, stdout: str,
                    stderr: str, duration_ms: int, project: str = "") -> "BuildResult":
        """Parse build output and classify errors/warnings."""
        combined = stdout + "\n" + stderr
        result = cls(
            command=command,
            project=project,
            success=returncode == 0,
            exit_code=returncode,
            duration_ms=duration_ms,
            output_text=combined[-5000:],  # keep last 5K chars
            timestamp=now_iso(),
        )

        # Parse errors (lines matching common compiler error patterns)
        errors = []
        for line in combined.splitlines():
            line_lower = line.lower()
            if any(p in line_lower for p in ("error:", "error[", "fatal:", "panic:")):
                if "warning as error" not in line_lower:
                    errors.append(line[:200])
        result.errors = errors[:50]
        result.error_count = len(errors)

        # Parse warnings
        warnings = []
        for line in combined.splitlines():
            line_lower = line.lower()
            if "warning:" in line_lower or "warning[" in line_lower:
                warnings.append(line[:200])
        result.warnings = warnings[:50]
        result.warning_count = len(warnings)

        # Parse slow tests (lines indicating test duration > 1s)
        slow = 0
        for line in combined.splitlines():
            # Match patterns like "test ... ... ms" or "PASS ... (X.XXs)"
            match = re.search(r"(\d+)ms|([\d.]+)s", line)
            if match:
                val = match.group(1) or match.group(2)
                try:
                    duration = float(val)
                    if "." in (match.group(2) or ""):
                        duration *= 1000  # seconds to ms
                    if duration > 1000:
                        slow += 1
                except ValueError:
                    pass
        result.slow_test_count = slow

        return result

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "project": self.project,
            "success": self.success,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "slow_test_count": self.slow_test_count,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# BuildWatcher
# ---------------------------------------------------------------------------

_BUILD_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS build_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    project         TEXT NOT NULL DEFAULT '',
    command         TEXT NOT NULL DEFAULT '',
    success         INTEGER NOT NULL DEFAULT 0,
    exit_code       INTEGER,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    warning_count   INTEGER NOT NULL DEFAULT 0,
    slow_test_count INTEGER NOT NULL DEFAULT 0,
    output_text     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_build_history_timestamp
    ON build_history(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_build_history_project
    ON build_history(project);
CREATE INDEX IF NOT EXISTS idx_build_history_success
    ON build_history(success);
"""


class BuildWatcher:
    """Watches build/test commands and tracks history.

    Usage::

        watcher = BuildWatcher(conn)
        result = watcher.run("cargo build")
        history = watcher.get_history()
    """

    def __init__(self, conn):
        self._conn = conn
        self._ensure_table()
        self._prev_success: Optional[bool] = None

    # ── Table management ─────────────────────────────────────────────

    def _ensure_table(self) -> None:
        try:
            self._conn.executescript(_BUILD_HISTORY_TABLE)
            self._conn.commit()
        except Exception:
            self._conn.rollback()

    # ── Running builds ───────────────────────────────────────────────

    def run(self, command: str, project: str = "",
            timeout: int = 300, cwd: Optional[str] = None) -> BuildResult:
        """Run a build command and record the result.

        Args:
            command: Shell command to run.
            project: Optional project name.
            timeout: Timeout in seconds.
            cwd: Working directory for the command.

        Returns:
            A BuildResult with parsed output.
        """
        start = time.time()
        try:
            proc = subprocess.run(
                command, shell=True, timeout=timeout,
                capture_output=True, text=True, cwd=cwd,
            )
            duration_ms = int((time.time() - start) * 1000)
            result = BuildResult.from_output(
                command=command,
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                duration_ms=duration_ms,
                project=project,
            )
        except subprocess.TimeoutExpired:
            duration_ms = int((time.time() - start) * 1000)
            result = BuildResult(
                command=command, project=project,
                success=False, exit_code=-1,
                duration_ms=duration_ms,
                output_text=f"[TIMEOUT after {timeout}s]",
                timestamp=now_iso(),
            )
        except FileNotFoundError:
            result = BuildResult(
                command=command, project=project,
                success=False, exit_code=-1,
                output_text=f"[COMMAND NOT FOUND: {command}]",
                timestamp=now_iso(),
            )
        except Exception as exc:
            result = BuildResult(
                command=command, project=project,
                success=False, exit_code=-1,
                output_text=f"[ERROR: {exc}]",
                timestamp=now_iso(),
            )

        # Persist
        self._persist(result)

        # Detect transition
        transition = self._detect_transition(result)

        # Push event on status change
        if transition:
            self._push_status_event(result, transition)

        self._prev_success = result.success
        return result

    def _persist(self, result: BuildResult) -> None:
        """Save a build result to the DB."""
        try:
            self._conn.execute(
                """INSERT INTO build_history
                   (timestamp, project, command, success, exit_code,
                    duration_ms, error_count, warning_count,
                    slow_test_count, output_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result.timestamp, result.project, result.command,
                 int(result.success), result.exit_code,
                 result.duration_ms, result.error_count,
                 result.warning_count, result.slow_test_count,
                 result.output_text, result.timestamp),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()

    # ── Transition detection ─────────────────────────────────────────

    def _detect_transition(self, result: BuildResult) -> Optional[str]:
        """Detect if the build status changed from previous run.

        Returns:
            'green' — first success after previous failure
            'red' — first failure after previous success
            'slower' — duration doubled compared to previous successful build
            None — no meaningful change
        """
        if self._prev_success is None:
            return None

        if result.success and not self._prev_success:
            return "green"
        if not result.success and self._prev_success:
            return "red"

        # Check for sudden slowdown
        if result.success:
            prev = self._get_previous_success_duration()
            if prev and result.duration_ms > prev * 2:
                return "slower"

        return None

    def _get_previous_success_duration(self) -> Optional[int]:
        """Get the duration of the most recent successful build."""
        try:
            row = self._conn.execute(
                "SELECT duration_ms FROM build_history "
                "WHERE success = 1 ORDER BY id DESC LIMIT 1 OFFSET 1"
            ).fetchone()
            return row["duration_ms"] if row else None
        except Exception:
            return None

    # ── Event pushing ─────────────────────────────────────────────────

    def _push_status_event(self, result: BuildResult, transition: str) -> None:
        """Push a build_status_changed event to the ambient feed."""
        try:
            from .ambient import AmbientEvent, push_event

            if transition == "green":
                title = f"✅ Build green: {result.project or result.command[:40]}"
                detail = (f"Build succeeded after previous failure. "
                          f"({result.duration_ms}ms, {result.warning_count} warnings)")
                pri = 2
            elif transition == "red":
                title = f"❌ Build red: {result.project or result.command[:40]}"
                detail = (f"Build failed after previous success. "
                          f"{result.error_count} errors, {result.warning_count} warnings")
                pri = 3
            elif transition == "slower":
                title = f"🐢 Build slower: {result.project or result.command[:40]}"
                detail = (f"Build duration doubled ({result.duration_ms}ms vs previous). "
                          f"{result.warning_count} warnings")
                pri = 2
            else:
                return

            ev = AmbientEvent(
                timestamp=result.timestamp,
                event_type="build_status_changed",
                title=title,
                detail=detail,
                source="build_watcher",
                priority=pri,
                category="system",
            )
            push_event(self._conn, ev)
        except Exception:
            pass

    # ── Queries ───────────────────────────────────────────────────────

    def get_history(self, project: str = "", limit: int = 50) -> list[dict]:
        """Get build history, newest first."""
        if project:
            rows = self._conn.execute(
                "SELECT * FROM build_history WHERE project = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM build_history ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self, project: str = "") -> dict:
        """Get build statistics."""
        if project:
            total = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM build_history WHERE project = ?",
                (project,),
            ).fetchone()["cnt"]
            successes = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM build_history "
                "WHERE project = ? AND success = 1", (project,),
            ).fetchone()["cnt"]
            avg_duration = self._conn.execute(
                "SELECT AVG(duration_ms) AS avg_dur FROM build_history "
                "WHERE project = ? AND success = 1", (project,),
            ).fetchone()["avg_dur"]
        else:
            total = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM build_history",
            ).fetchone()["cnt"]
            successes = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM build_history WHERE success = 1",
            ).fetchone()["cnt"]
            avg_duration = self._conn.execute(
                "SELECT AVG(duration_ms) AS avg_dur FROM build_history WHERE success = 1",
            ).fetchone()["avg_dur"]

        pass_rate = (successes / total * 100) if total > 0 else 0.0
        return {
            "total_builds": total,
            "successes": successes,
            "failures": total - successes,
            "pass_rate": round(pass_rate, 1),
            "avg_duration_ms": int(avg_duration) if avg_duration else 0,
        }


def format_build_history(rows: list[dict], verbose: bool = False) -> str:
    """Format build history for terminal display."""
    if not rows:
        return "  No build history found."
    lines = [
        f"{'Time':<22} {'Status':<10} {'Duration':<12} {'Errors':<8} {'Warnings':<10} {'Command':<30}",
        "-" * 90,
    ]
    for r in rows:
        ts = (r["timestamp"] or "?")[:19]
        status = "✅ PASS" if r["success"] else "❌ FAIL"
        dur = f"{r['duration_ms']}ms" if r["duration_ms"] else "?"
        err = str(r["error_count"] or 0)
        warn = str(r["warning_count"] or 0)
        cmd = (r["command"] or "?")[:30]
        lines.append(f"{ts:<22} {status:<10} {dur:<12} {err:<8} {warn:<10} {cmd:<30}")
    return "\n".join(lines)


def format_build_stats(stats: dict) -> str:
    """Format build statistics for terminal display."""
    return (
        f"  Total builds: {stats['total_builds']}\n"
        f"  Pass rate:    {stats['pass_rate']}%\n"
        f"  Successes:    {stats['successes']}\n"
        f"  Failures:     {stats['failures']}\n"
        f"  Avg duration: {stats['avg_duration_ms']}ms"
    )
