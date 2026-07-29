"""Tests for System Intelligence: telemetry, process monitoring, resource alerts, build watcher."""

from __future__ import annotations

import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock


# ═════════════════════════════════════════════════════════════════════════
# TelemetrySnapshot tests
# ═════════════════════════════════════════════════════════════════════════


class TestTelemetrySnapshot:
    def test_format_brief(self):
        from friday.telemetry import TelemetrySnapshot
        snap = TelemetrySnapshot(cpu_percent=45.5, memory_percent=60.0, disk_percent=70.0)
        result = snap.format_brief()
        assert "CPU 46%" in result
        assert "MEM 60%" in result
        assert "DISK 70%" in result

    def test_format_block_includes_all(self):
        from friday.telemetry import TelemetrySnapshot
        snap = TelemetrySnapshot(
            cpu_percent=45.5, cpu_count=8, cpu_freq_current=2500.0,
            memory_percent=60.0, memory_total=16_000_000_000, memory_available=6_400_000_000,
            disk_percent=70.0, disk_total=500_000_000_000, disk_used=350_000_000_000,
            net_bytes_sent=1000, net_bytes_recv=2000,
            load_avg=(1.5, 1.2, 1.0),
            processes=456,
            health="green",
        )
        result = snap.format_block()
        # Health is displayed as GREEN (via .upper())
        assert "GREEN" in result or "Health: green" in result
        assert "CPU" in result
        assert "MEM" in result
        assert "DISK" in result
        assert "NET" in result
        assert "LOAD" in result
        assert "PROCS" in result
        assert "456" in result

    def test_to_dict(self):
        from friday.telemetry import TelemetrySnapshot
        snap = TelemetrySnapshot(cpu_percent=50.0, memory_percent=60.0, processes=100)
        d = snap.to_dict()
        assert d["cpu_percent"] == 50.0
        assert d["memory_percent"] == 60.0
        assert d["processes"] == 100
        assert "health" in d

    def test_health_assessment_green(self):
        from friday.telemetry import TelemetrySnapshot, _assess_health
        snap = TelemetrySnapshot(cpu_percent=30.0, memory_percent=40.0, disk_percent=50.0)
        health = _assess_health(snap)
        assert health in ("green", "yellow")

    def test_health_assessment_red(self):
        from friday.telemetry import TelemetrySnapshot, _assess_health
        snap = TelemetrySnapshot(cpu_percent=99.0, memory_percent=98.0, disk_percent=97.0)
        health = _assess_health(snap)
        assert health == "red"

    def test_health_assessment_yellow(self):
        from friday.telemetry import TelemetrySnapshot, _assess_health
        snap = TelemetrySnapshot(cpu_percent=85.0, memory_percent=40.0, disk_percent=50.0)
        health = _assess_health(snap)
        assert health == "yellow"

    def test_gpu_formatting(self):
        from friday.telemetry import TelemetrySnapshot
        snap = TelemetrySnapshot(
            gpu={"name": "RTX 4090", "utilization": 65, "memory_used": 8192, "memory_total": 24576, "temperature": 72},
        )
        block = snap.format_block()
        assert "RTX 4090" in block
        assert "65%" in block
        assert "72" in block


# ═════════════════════════════════════════════════════════════════════════
# TelemetryCollector tests
# ═════════════════════════════════════════════════════════════════════════


class TestTelemetryCollector:
    def test_latest_returns_none_before_start(self):
        from friday.telemetry import TelemetryCollector
        collector = TelemetryCollector()
        assert collector.latest() is None

    def test_history_empty_before_start(self):
        from friday.telemetry import TelemetryCollector
        collector = TelemetryCollector()
        assert collector.history() == []

    def test_telemetry_collector_start_stop(self):
        from friday.telemetry import TelemetryCollector
        collector = TelemetryCollector()
        collector.start()
        time.sleep(0.5)
        snap = collector.latest()
        collector.stop()
        assert snap is not None
        assert snap.cpu_percent >= 0
        assert snap.processes > 0

    @pytest.fixture
    def collector(self):
        from friday.telemetry import TelemetryCollector
        c = TelemetryCollector()
        c.start()
        time.sleep(0.5)
        yield c
        c.stop()


# ═════════════════════════════════════════════════════════════════════════
# Formatting
# ═════════════════════════════════════════════════════════════════════════


class TestFormatting:
    def test_format_json(self):
        from friday.telemetry import TelemetrySnapshot, format_json
        snap = TelemetrySnapshot(cpu_percent=50.0)
        result = json.loads(format_json(snap))
        assert result["cpu_percent"] == 50.0

    def test_format_snapshot_brief(self):
        from friday.telemetry import TelemetrySnapshot, format_snapshot
        snap = TelemetrySnapshot(cpu_percent=45.5, memory_percent=60.0, disk_percent=70.0, health="green")
        result = format_snapshot(snap, brief=True)
        assert "GREEN" in result or "green" in result.lower()


# ═════════════════════════════════════════════════════════════════════════
# ProcessMonitor tests
# ═════════════════════════════════════════════════════════════════════════


class TestProcessMonitor:
    @pytest.fixture
    def conn(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        # Create process_baseline table
        c.execute("""
            CREATE TABLE IF NOT EXISTS process_baseline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                cmdline TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                known INTEGER NOT NULL DEFAULT 0,
                user_label TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_process_baseline_name ON process_baseline(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_process_baseline_known ON process_baseline(known)")
        yield c
        c.close()

    def test_tag_process(self, conn):
        from friday.telemetry import ProcessMonitor
        monitor = ProcessMonitor(conn)
        # Insert a process manually
        from friday.db import now_iso
        conn.execute(
            "INSERT INTO process_baseline (name, cmdline, first_seen, last_seen, seen_count, known) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            ("python", "python test.py", now_iso(), now_iso(), 1),
        )
        conn.commit()

        result = monitor.tag_process("python", known=True, label="my dev server")
        assert result is True

        row = conn.execute("SELECT known, user_label FROM process_baseline WHERE name = ?", ("python",)).fetchone()
        assert row["known"] == 1
        assert row["user_label"] == "my dev server"

    def test_get_baseline_empty(self, conn):
        from friday.telemetry import ProcessMonitor
        monitor = ProcessMonitor(conn)
        baseline = monitor.get_baseline()
        assert baseline == []

    def test_get_unknown_processes_empty(self, conn):
        from friday.telemetry import ProcessMonitor
        monitor = ProcessMonitor(conn)
        unknowns = monitor.get_unknown_processes()
        assert unknowns == []


# ═════════════════════════════════════════════════════════════════════════
# ResourceAlert tests
# ═════════════════════════════════════════════════════════════════════════


class TestResourceAlert:
    @pytest.fixture
    def conn(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        yield c
        c.close()

    def test_check_returns_alerts_for_high_cpu(self, conn):
        from friday.telemetry import TelemetrySnapshot, ResourceAlert
        from friday.db import now_iso
        alert = ResourceAlert(conn)
        snap = TelemetrySnapshot(cpu_percent=95.0, memory_percent=40.0, disk_percent=50.0, health="red")
        results = alert.check(snap)
        cpu_alerts = [a for a in results if a["metric"] == "cpu"]
        assert len(cpu_alerts) >= 1
        assert cpu_alerts[0]["level"] == "red"

    def test_check_no_alerts_for_normal(self, conn):
        from friday.telemetry import TelemetrySnapshot, ResourceAlert
        alert = ResourceAlert(conn)
        # Set disk_total high enough to avoid the disk_free critical alert
        snap = TelemetrySnapshot(
            cpu_percent=30.0, memory_percent=40.0,
            disk_percent=50.0, disk_total=500_000_000_000, disk_used=250_000_000_000,
            health="green",
        )
        results = alert.check(snap)
        non_resolved = [a for a in results if not a["is_resolved"]]
        assert len(non_resolved) == 0

    def test_check_swap_alert(self, conn):
        from friday.telemetry import TelemetrySnapshot, ResourceAlert
        alert = ResourceAlert(conn)
        snap = TelemetrySnapshot(
            cpu_percent=30.0, memory_percent=40.0, disk_percent=50.0,
            swap_total=8_000_000_000, swap_used=5_000_000_000, swap_percent=62.5,
            health="yellow",
        )
        results = alert.check(snap)
        swap_alerts = [a for a in results if a["metric"] == "swap" and not a["is_resolved"]]
        assert len(swap_alerts) >= 1
        assert swap_alerts[0]["level"] == "yellow"


# ═════════════════════════════════════════════════════════════════════════
# BuildWatcher tests
# ═════════════════════════════════════════════════════════════════════════


class TestBuildWatcher:
    @pytest.fixture
    def conn(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("""
            CREATE TABLE IF NOT EXISTS build_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                command TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                exit_code INTEGER,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                slow_test_count INTEGER NOT NULL DEFAULT 0,
                output_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        yield c
        c.close()

    def test_run_success(self, conn):
        from friday.build_watcher import BuildWatcher
        watcher = BuildWatcher(conn)
        result = watcher.run("echo 'hello'", project="test")
        assert result.success is True
        assert result.exit_code == 0

    def test_run_failure(self, conn):
        from friday.build_watcher import BuildWatcher
        watcher = BuildWatcher(conn)
        result = watcher.run("false", project="test")
        assert result.success is False
        assert result.exit_code == 1

    def test_get_history(self, conn):
        from friday.build_watcher import BuildWatcher
        watcher = BuildWatcher(conn)
        watcher.run("echo 'hello'", project="test")
        watcher.run("echo 'world'", project="test")
        history = watcher.get_history()
        assert len(history) == 2

    def test_get_stats(self, conn):
        from friday.build_watcher import BuildWatcher
        watcher = BuildWatcher(conn)
        watcher.run("echo 'hello'", project="test")
        watcher.run("false", project="test")
        stats = watcher.get_stats()
        assert stats["total_builds"] == 2
        assert stats["successes"] == 1
        assert stats["failures"] == 1
        assert stats["pass_rate"] == 50.0

    def test_parse_errors(self, conn):
        from friday.build_watcher import BuildResult
        result = BuildResult.from_output(
            command="cargo build",
            returncode=1,
            stdout="",
            stderr="error[E0308]: mismatched types\n  --> src/main.rs:10:5\n",
            duration_ms=500,
        )
        assert result.error_count >= 1
        assert any("error[E0308]" in e for e in result.errors)

    def test_parse_warnings(self, conn):
        from friday.build_watcher import BuildResult
        result = BuildResult.from_output(
            command="cargo build",
            returncode=0,
            stdout="warning: unused variable\nwarning: dead code\nBuilding...",
            stderr="",
            duration_ms=300,
        )
        assert result.warning_count >= 2


# ═════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ═════════════════════════════════════════════════════════════════════════


class TestBuildFormatting:
    def test_format_build_history_empty(self):
        from friday.build_watcher import format_build_history
        result = format_build_history([])
        assert "No build history found" in result

    def test_format_build_stats(self):
        from friday.build_watcher import format_build_stats
        stats = {"total_builds": 10, "successes": 8, "failures": 2, "pass_rate": 80.0, "avg_duration_ms": 1500}
        result = format_build_stats(stats)
        assert "80.0" in result
        assert "10" in result
        assert "1500" in result


# ═════════════════════════════════════════════════════════════════════════
# CLI module tests (smoke tests — verify imports and dispatch)
# ═════════════════════════════════════════════════════════════════════════


class TestCliTelemetry:
    def test_add_subparser(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        from friday.cli_telemetry import add_subparser
        add_subparser(sub)
        # Verify subcommand names
        for name in ("telemetry", "processes", "process", "build"):
            subparser = None
            for action in parser._actions:
                if hasattr(action, "choices") and action.choices:
                    if name in action.choices:
                        subparser = action.choices[name]
                        break
            assert subparser is not None, f"Subparser '{name}' not found"
