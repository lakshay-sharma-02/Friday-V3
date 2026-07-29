"""Live Telemetry — system metrics collection for Friday.

Collects CPU, memory, disk, network, and optional GPU metrics.

Three subsystems:
  1. **TelemetryCollector** — daemon sidecar thread with ring buffer
  2. **ProcessMonitor** — process baseline learning and anomaly detection
  3. **ResourceAlert** — threshold-based alerting with debounce

Usage::

    # One-shot snapshot
    snap = collect_telemetry()
    print(format_snapshot(snap))

    # Daemon sidecar (continuous collection)
    collector = TelemetryCollector()
    collector.start()
    snapshot = collector.latest()
    history = collector.history(since=time.time() - 3600)
    collector.stop()

    # Process monitoring
    monitor = ProcessMonitor(conn)
    baseline = monitor.get_baseline()
    unknowns = monitor.get_unknown_processes()

    # Resource alerting
    alerter = ResourceAlert(conn)
    alerts = alerter.check(snapshot)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import now_iso


# ═════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════

# Poll intervals (seconds)
_CPU_MEM_DISK_INTERVAL = 10
_GPU_INTERVAL = 30
_NET_INTERVAL = 10

# Ring buffer: keep 60 minutes of data
_RING_BUFFER_SECONDS = 3600
_RING_BUFFER_SAMPLES_FAST = int(_RING_BUFFER_SECONDS / _CPU_MEM_DISK_INTERVAL)  # ~360

# Snapshot persist interval (seconds)
_SNAPSHOT_PERSIST_INTERVAL = 300  # 5 minutes

# Health thresholds
_HEALTH_CPU_WARN = 80.0
_HEALTH_CPU_CRIT = 95.0
_HEALTH_MEM_WARN = 80.0
_HEALTH_MEM_CRIT = 95.0
_HEALTH_DISK_WARN = 85.0
_HEALTH_DISK_CRIT = 95.0
_HEALTH_SWAP_WARN = 50.0

# Process baseline
_BASELINE_DAYS = 7
_BASELINE_MIN_SEEN = 3

# Alert thresholds
_ALERT_CPU = 90.0
_ALERT_CPU_MINUTES = 5
_ALERT_MEM = 90.0
_ALERT_DISK = 90.0
_ALERT_DISK_FREE_CRITICAL = 1_000_000_000  # 1 GB
_ALERT_GPU_TEMP = 85.0
_ALERT_SWAP = 50.0
_ALERT_PROCESS_MULTIPLIER = 2.0
_ALERT_DEBOUNCE_SECONDS = 3600  # 1 hour
_ALERT_DEBOUNCE_CRITICAL = 300   # 5 minutes for critical


# ═════════════════════════════════════════════════════════════════════════
# Data models
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class TelemetrySnapshot:
    """One point-in-time measurement of system resources."""

    timestamp: float = 0.0
    cpu_percent: float = 0.0
    cpu_count: int = 0
    cpu_freq_current: Optional[float] = None
    memory_total: int = 0
    memory_available: int = 0
    memory_percent: float = 0.0
    disk_total: int = 0
    disk_used: int = 0
    disk_percent: float = 0.0
    net_bytes_sent: int = 0
    net_bytes_recv: int = 0
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    swap_total: int = 0
    swap_used: int = 0
    swap_percent: float = 0.0
    processes: int = 0
    gpu: Optional[dict] = None
    # Extended fields
    per_cpu: list[float] = field(default_factory=list)
    per_disk: dict[str, dict] = field(default_factory=dict)
    per_net: dict[str, dict] = field(default_factory=dict)
    disk_io: dict[str, dict] = field(default_factory=dict)
    health: str = "green"  # green / yellow / red
    top_cpu: list[dict] = field(default_factory=list)
    top_mem: list[dict] = field(default_factory=list)
    system_uptime: float = 0.0
    daemon_uptime: float = 0.0

    def to_dict(self) -> dict:
        """Serialize to a plain dict (JSON-compatible)."""
        return {
            "timestamp": self.timestamp,
            "cpu_percent": self.cpu_percent,
            "cpu_count": self.cpu_count,
            "cpu_freq_current": self.cpu_freq_current,
            "memory_total": self.memory_total,
            "memory_available": self.memory_available,
            "memory_percent": round(self.memory_percent, 1),
            "disk_total": self.disk_total,
            "disk_used": self.disk_used,
            "disk_percent": round(self.disk_percent, 1),
            "net_bytes_sent": self.net_bytes_sent,
            "net_bytes_recv": self.net_bytes_recv,
            "load_avg": [round(v, 2) for v in self.load_avg],
            "swap_total": self.swap_total,
            "swap_percent": round(self.swap_percent, 1),
            "processes": self.processes,
            "gpu": self.gpu,
            "health": self.health,
            "per_cpu": self.per_cpu,
            "per_disk": self.per_disk,
            "per_net": self.per_net,
            "system_uptime": self.system_uptime,
            "top_cpu": self.top_cpu,
            "top_mem": self.top_mem,
        }

    def format_brief(self) -> str:
        """One-line summary: CPU, MEM, DISK, health."""
        cpu_s = f"CPU {self.cpu_percent:.0f}%"
        mem_s = f"MEM {self.memory_percent:.0f}%"
        disk_s = f"DISK {self.disk_percent:.0f}%"
        health_s = f"({self.health.upper()})"
        parts = [cpu_s, mem_s, disk_s, health_s]
        if self.gpu:
            gpu_util = self.gpu.get("utilization", 0)
            parts.append(f"GPU {gpu_util}%")
        return "  ".join(parts)

    def format_block(self) -> str:
        """Multi-line formatted snapshot for terminal display."""
        lines: list[str] = []
        lines.append(f"  Health: {self.health.upper()}")
        lines.append(f"  CPU:   {self.cpu_percent:.1f}%  ({self.cpu_count} cores, "
                      f"{self._fmt_freq(self.cpu_freq_current)})")
        if self.per_cpu:
            cores = " ".join(f"{c:.0f}%" for c in self.per_cpu[:8])
            if len(self.per_cpu) > 8:
                cores += f" ... ({len(self.per_cpu)} total)"
            lines.append(f"  Per-core: {cores}")
        lines.append(f"  MEM:   {self._fmt_bytes(self.memory_available)} free / "
                      f"{self._fmt_bytes(self.memory_total)} total "
                      f"({self.memory_percent:.1f}%)")
        if self.swap_total:
            lines.append(f"  SWAP:  {self._fmt_bytes(self.swap_used)} / "
                          f"{self._fmt_bytes(self.swap_total)} "
                          f"({self.swap_percent:.1f}%)")
        lines.append(f"  DISK:  {self._fmt_bytes(self.disk_used)} / "
                      f"{self._fmt_bytes(self.disk_total)} "
                      f"({self.disk_percent:.1f}%)")
        if self.per_disk:
            for mnt, info in sorted(self.per_disk.items()):
                pct = info.get("percent", 0)
                used = info.get("used", 0)
                total = info.get("total", 0)
                lines.append(f"    {mnt}: {self._fmt_bytes(used)}/{self._fmt_bytes(total)} ({pct:.1f}%)")
        if self.per_net:
            for iface, info in sorted(self.per_net.items())[:4]:
                up = info.get("bytes_sent", 0)
                down = info.get("bytes_recv", 0)
                lines.append(f"  NET/{iface}: ↑{self._fmt_bytes(up)} ↓{self._fmt_bytes(down)}")
        else:
            lines.append(f"  NET:   ↑{self._fmt_bytes(self.net_bytes_sent)} "
                          f"↓{self._fmt_bytes(self.net_bytes_recv)}")
        lines.append(f"  LOAD:  {self.load_avg[0]:.2f} {self.load_avg[1]:.2f} "
                      f"{self.load_avg[2]:.2f}")
        lines.append(f"  PROCS: {self.processes}")
        if self.top_cpu:
            top_strs = [f"{p.get('name','?')} {p.get('cpu',0):.1f}%" for p in self.top_cpu[:3]]
            lines.append(f"  Top CPU: {', '.join(top_strs)}")
        if self.top_mem:
            top_strs = [f"{p.get('name','?')} {p.get('mem',0):.1f}%" for p in self.top_mem[:3]]
            lines.append(f"  Top MEM: {', '.join(top_strs)}")
        if self.gpu:
            g = self.gpu
            lines.append(f"  GPU:   {g.get('name', '?')} "
                          f"{g.get('utilization', 0)}% "
                          f"{g.get('memory_used', 0)}/{g.get('memory_total', 0)}MB "
                          f"{g.get('temperature', '?')}°C")
        if self.system_uptime:
            lines.append(f"  Uptime: {_fmt_duration(self.system_uptime)}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_bytes(b: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(b) < 1024:
                return f"{b:.1f}{unit}"
            b /= 1024
        return f"{b:.1f}PB"

    @staticmethod
    def _fmt_freq(freq: Optional[float]) -> str:
        if freq is None:
            return "?"
        if freq >= 1000:
            return f"{freq / 1000:.2f}GHz"
        return f"{freq:.0f}MHz"


# ═════════════════════════════════════════════════════════════════════════
# GPU reader
# ═════════════════════════════════════════════════════════════════════════


def _read_gpu() -> Optional[dict]:
    """Read GPU stats via nvidia-smi. Returns None if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        line = result.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(", ")]
        if len(parts) >= 5:
            return {
                "name": parts[0],
                "utilization": int(parts[1]) if parts[1].isdigit() else 0,
                "memory_used": int(parts[2]) if parts[2].isdigit() else 0,
                "memory_total": int(parts[3]) if parts[3].isdigit() else 0,
                "temperature": int(parts[4]) if parts[4].isdigit() else 0,
            }
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def _read_cpu_temps() -> Optional[float]:
    """Read CPU temperature from thermal zones. Returns max temp in °C or None."""
    try:
        max_temp = 0.0
        base = "/sys/class/thermal"
        for entry in os.listdir(base):
            if entry.startswith("thermal_zone"):
                try:
                    with open(os.path.join(base, entry, "temp")) as f:
                        temp = int(f.read().strip()) / 1000.0
                        max_temp = max(max_temp, temp)
                except (OSError, ValueError):
                    continue
        if max_temp > 0:
            return max_temp
    except Exception:
        pass
    return None


def _read_uptime() -> float:
    """Read system uptime from /proc/uptime. Returns seconds."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


# ═════════════════════════════════════════════════════════════════════════
# One-shot telemetry collection
# ═════════════════════════════════════════════════════════════════════════


def collect_telemetry(
    include_gpu: bool = False,
    include_net: bool = True,
    include_processes: bool = True,
    daemon_start_time: Optional[float] = None,
) -> TelemetrySnapshot:
    """Collect a point-in-time snapshot of system metrics.

    Args:
        include_gpu: If True, attempt to read GPU stats via nvidia-smi.
        include_net: If True, include network I/O counters.
        include_processes: If True, include top CPU/mem processes.
        daemon_start_time: Optional daemon start timestamp for daemon uptime.

    Returns:
        A ``TelemetrySnapshot`` with all available metrics.
        Never raises — individual metric failures return defaults.
    """
    import psutil

    snap = TelemetrySnapshot(timestamp=time.time())
    snap.system_uptime = _read_uptime()
    if daemon_start_time:
        snap.daemon_uptime = time.time() - daemon_start_time

    # ── CPU ───────────────────────────────────────────────────────────
    try:
        snap.cpu_percent = psutil.cpu_percent(interval=0.1)
        snap.cpu_count = psutil.cpu_count(logical=True) or 0
        freq = psutil.cpu_freq()
        snap.cpu_freq_current = freq.current if freq else None
        snap.load_avg = psutil.getloadavg()
        snap.per_cpu = psutil.cpu_percent(interval=0.05, percpu=True)
    except Exception:
        pass

    # ── Memory ────────────────────────────────────────────────────────
    try:
        mem = psutil.virtual_memory()
        snap.memory_total = mem.total
        snap.memory_available = mem.available
        snap.memory_percent = mem.percent
    except Exception:
        pass

    # ── Swap ──────────────────────────────────────────────────────────
    try:
        swap = psutil.swap_memory()
        snap.swap_total = swap.total
        snap.swap_used = swap.used
        snap.swap_percent = swap.percent
    except Exception:
        pass

    # ── Disk ──────────────────────────────────────────────────────────
    # Root partition
    try:
        disk = psutil.disk_usage("/")
        snap.disk_total = disk.total
        snap.disk_used = disk.used
        snap.disk_percent = disk.percent
    except Exception:
        pass

    # Per-mount disk usage
    try:
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                snap.per_disk[part.mountpoint] = {
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent,
                    "fstype": part.fstype,
                }
            except (PermissionError, OSError):
                continue
    except Exception:
        pass

    # Disk I/O
    try:
        io = psutil.disk_io_counters(perdisk=True)
        for name, counters in io.items():
            snap.disk_io[name] = {
                "read_bytes": counters.read_bytes,
                "write_bytes": counters.write_bytes,
                "read_count": counters.read_count,
                "write_count": counters.write_count,
            }
    except Exception:
        pass

    # ── Network ───────────────────────────────────────────────────────
    if include_net:
        try:
            net = psutil.net_io_counters()
            snap.net_bytes_sent = net.bytes_sent
            snap.net_bytes_recv = net.bytes_recv
        except Exception:
            pass
        # Per-interface
        try:
            io_counters = psutil.net_io_counters(pernic=True)
            for iface, counters in io_counters.items():
                snap.per_net[iface] = {
                    "bytes_sent": counters.bytes_sent,
                    "bytes_recv": counters.bytes_recv,
                    "packets_sent": counters.packets_sent,
                    "packets_recv": counters.packets_recv,
                    "errin": counters.errin,
                    "errout": counters.errout,
                    "dropin": counters.dropin,
                    "dropout": counters.dropout,
                }
        except Exception:
            pass

    # ── Process count + top processes ────────────────────────────────
    try:
        snap.processes = len(psutil.pids())
    except Exception:
        pass

    if include_processes:
        snap.top_cpu, snap.top_mem = _get_top_processes()

    # ── GPU (optional) ────────────────────────────────────────────────
    if include_gpu:
        snap.gpu = _read_gpu()

    # ── Health assessment ────────────────────────────────────────────
    snap.health = _assess_health(snap)

    return snap


def _get_top_processes() -> tuple[list[dict], list[dict]]:
    """Get top 5 CPU and top 5 memory processes. Graceful degradation."""
    import psutil

    top_cpu: list[dict] = []
    top_mem: list[dict] = []
    try:
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "create_time"]):
            try:
                info = proc.info
                if info["cpu_percent"] is not None and info["name"]:
                    processes.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes.sort(key=lambda p: p.get("cpu_percent", 0) or 0, reverse=True)
        for p in processes[:5]:
            top_cpu.append({
                "pid": p["pid"],
                "name": p["name"] or "?",
                "cpu": p.get("cpu_percent", 0) or 0,
                "mem": p.get("memory_percent", 0) or 0,
            })

        processes.sort(key=lambda p: p.get("memory_percent", 0) or 0, reverse=True)
        for p in processes[:5]:
            top_mem.append({
                "pid": p["pid"],
                "name": p["name"] or "?",
                "cpu": p.get("cpu_percent", 0) or 0,
                "mem": p.get("memory_percent", 0) or 0,
            })
    except Exception:
        pass
    return top_cpu, top_mem


def _assess_health(snap: TelemetrySnapshot) -> str:
    """Assess system health from a snapshot.

    Returns:
        'green' — all nominal
        'yellow' — one warning threshold exceeded
        'red' — critical threshold exceeded
    """
    issues: list[str] = []
    if snap.cpu_percent >= _HEALTH_CPU_CRIT:
        issues.append("red")
    elif snap.cpu_percent >= _HEALTH_CPU_WARN:
        issues.append("yellow")

    if snap.memory_percent >= _HEALTH_MEM_CRIT:
        issues.append("red")
    elif snap.memory_percent >= _HEALTH_MEM_WARN:
        issues.append("yellow")

    if snap.disk_percent >= _HEALTH_DISK_CRIT:
        issues.append("red")
    elif snap.disk_percent >= _HEALTH_DISK_WARN:
        issues.append("yellow")

    if snap.swap_percent >= _HEALTH_SWAP_WARN:
        issues.append("yellow")

    if "red" in issues:
        return "red"
    if "yellow" in issues:
        return "yellow"
    return "green"


# ═════════════════════════════════════════════════════════════════════════
# Formatting
# ═════════════════════════════════════════════════════════════════════════


def format_json(snap: TelemetrySnapshot) -> str:
    """Return the snapshot as a formatted JSON string."""
    return json.dumps(snap.to_dict(), indent=2)


def format_snapshot(snap: TelemetrySnapshot, brief: bool = False) -> str:
    """Render a snapshot to a human-readable string."""
    if brief:
        return snap.format_brief()
    return snap.format_block()


def _fmt_duration(seconds: float) -> str:
    """Format seconds to human-readable duration."""
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# ═════════════════════════════════════════════════════════════════════════
# TelemetryCollector — daemon sidecar thread
# ═════════════════════════════════════════════════════════════════════════


class TelemetryCollector:
    """Continuous telemetry collector that runs as a daemon thread.

    Collects metrics on a staggered schedule:
      - CPU/mem/disk: every 10s
      - Network: every 10s
      - GPU: every 30s
      - Full snapshot persisted to DB: every 5min

    Maintains an in-memory ring buffer of the last ~60 minutes of data.
    Thread-safe: uses a lock around the ring buffer.
    """

    def __init__(self, conn=None, daemon_start_time: Optional[float] = None):
        self._conn = conn
        self._daemon_start_time = daemon_start_time
        self._lock = threading.Lock()
        self._ring: deque = deque(maxlen=_RING_BUFFER_SAMPLES_FAST)
        self._latest: Optional[TelemetrySnapshot] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gpu_counter = 0
        self._last_persist_time: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the collector thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="telemetry-collector",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the collector to stop and wait for it."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ── Accessors ────────────────────────────────────────────────────

    def latest(self) -> Optional[TelemetrySnapshot]:
        """Get the most recent snapshot (thread-safe)."""
        with self._lock:
            return self._latest

    def history(
        self,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> list[TelemetrySnapshot]:
        """Get historical snapshots from the ring buffer (thread-safe).

        Args:
            since: Unix timestamp; only return snapshots newer than this.
            limit: Maximum number of snapshots to return.

        Returns:
            List of snapshots sorted by timestamp (oldest first).
        """
        with self._lock:
            snaps = list(self._ring)
        if since:
            snaps = [s for s in snaps if s.timestamp >= since]
        return snaps[-limit:]

    def _run(self) -> None:
        """Main collection loop."""
        _ensure_tables(self._conn)
        while not self._stop_event.is_set():
            try:
                # GPU: only every ~30s (3 cycles of 10s)
                self._gpu_counter += 1
                include_gpu = self._gpu_counter % 3 == 0

                snap = collect_telemetry(
                    include_gpu=include_gpu,
                    daemon_start_time=self._daemon_start_time,
                )

                with self._lock:
                    self._latest = snap
                    self._ring.append(snap)

                # Persist full snapshot every 5 minutes
                if snap.timestamp - self._last_persist_time >= _SNAPSHOT_PERSIST_INTERVAL:
                    _persist_snapshot(self._conn, snap)
                    self._last_persist_time = snap.timestamp

            except Exception:
                pass  # Never crash the thread

            self._stop_event.wait(_CPU_MEM_DISK_INTERVAL)


# ═════════════════════════════════════════════════════════════════════════
# DB helpers
# ═════════════════════════════════════════════════════════════════════════


_TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    cpu_percent     REAL NOT NULL DEFAULT 0,
    cpu_count       INTEGER NOT NULL DEFAULT 0,
    memory_percent  REAL NOT NULL DEFAULT 0,
    memory_total    INTEGER NOT NULL DEFAULT 0,
    disk_percent    REAL NOT NULL DEFAULT 0,
    disk_total      INTEGER NOT NULL DEFAULT 0,
    swap_percent    REAL NOT NULL DEFAULT 0,
    processes       INTEGER NOT NULL DEFAULT 0,
    load_1m         REAL NOT NULL DEFAULT 0,
    health          TEXT NOT NULL DEFAULT 'green',
    per_cpu_json    TEXT NOT NULL DEFAULT '[]',
    per_disk_json   TEXT NOT NULL DEFAULT '{}',
    per_net_json    TEXT NOT NULL DEFAULT '{}',
    top_cpu_json    TEXT NOT NULL DEFAULT '[]',
    top_mem_json    TEXT NOT NULL DEFAULT '[]',
    gpu_json        TEXT,
    system_uptime   REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
    ON telemetry_snapshots(timestamp DESC);

CREATE TABLE IF NOT EXISTS process_baseline (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    cmdline         TEXT NOT NULL DEFAULT '',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    known           INTEGER NOT NULL DEFAULT 0,
    user_label      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_process_baseline_name
    ON process_baseline(name);

CREATE INDEX IF NOT EXISTS idx_process_baseline_known
    ON process_baseline(known);
"""


def _ensure_tables(conn) -> None:
    """Ensure telemetry tables exist. No-op if conn is None."""
    if conn is None:
        return
    try:
        conn.executescript(_TELEMETRY_SCHEMA)
        conn.commit()
    except Exception:
        conn.rollback()


def _persist_snapshot(conn, snap: TelemetrySnapshot) -> None:
    """Persist a snapshot to the telemetry_snapshots table."""
    if conn is None:
        return
    try:
        conn.execute(
            """INSERT INTO telemetry_snapshots
               (timestamp, cpu_percent, cpu_count, memory_percent, memory_total,
                disk_percent, disk_total, swap_percent, processes, load_1m,
                health, per_cpu_json, per_disk_json, per_net_json,
                top_cpu_json, top_mem_json, gpu_json, system_uptime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.fromtimestamp(snap.timestamp, tz=timezone.utc).isoformat(),
                round(snap.cpu_percent, 1),
                snap.cpu_count,
                round(snap.memory_percent, 1),
                snap.memory_total,
                round(snap.disk_percent, 1),
                snap.disk_total,
                round(snap.swap_percent, 1),
                snap.processes,
                round(snap.load_avg[0], 2),
                snap.health,
                json.dumps(snap.per_cpu),
                json.dumps(snap.per_disk),
                json.dumps(snap.per_net),
                json.dumps(snap.top_cpu),
                json.dumps(snap.top_mem),
                json.dumps(snap.gpu) if snap.gpu else None,
                snap.system_uptime,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()


def get_telemetry_history(conn, since: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Query historical telemetry snapshots from the DB.

    Args:
        conn: DB connection.
        since: ISO timestamp filter (return only rows after this time).
        limit: Max rows to return.

    Returns:
        List of dicts with snapshot data (newest first).
    """
    if since:
        rows = conn.execute(
            "SELECT * FROM telemetry_snapshots WHERE timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM telemetry_snapshots ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════
# ProcessMonitor — process baseline learning & anomaly detection
# ═════════════════════════════════════════════════════════════════════════


class ProcessMonitor:
    """Monitor running processes against a learned baseline.

    Maintains a ``process_baseline`` table in the DB that records every
    process seen, how many times, and whether it's considered "known".

    **Baseline learning:**
    - First 7 days are learning mode — every process is potentially known.
    - After 7 days, processes seen > 3 times are marked known.
    - User can explicitly tag processes with ``tag_process()``.
    """

    def __init__(self, conn):
        self._conn = conn
        _ensure_tables(conn)
        self._is_learning = self._check_learning_mode()

    # ── Collection ───────────────────────────────────────────────────

    def collect(self) -> list[dict]:
        """Collect current process info and update the baseline.

        Returns:
            List of unknown process dicts (name, pid, cpu, mem, cmdline).
        """
        import psutil

        unknowns: list[dict] = []
        now = now_iso()

        for proc in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_percent", "create_time"]):
            try:
                info = proc.info
                name = info["name"] or "?"
                cmdline = " ".join(info["cmdline"] or [])[:200]
                if not name or name in ("", "?"):
                    continue

                # Update baseline
                self._record_sighting(name, cmdline, now)

                # Check if unknown
                if not self._is_known(name, cmdline):
                    unknowns.append({
                        "pid": info["pid"],
                        "name": name,
                        "cpu": info.get("cpu_percent", 0) or 0,
                        "mem": info.get("memory_percent", 0) or 0,
                        "cmdline": cmdline,
                        "runtime": (time.time() - (info.get("create_time", time.time()) or time.time())),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # After collecting, re-evaluate known/unknown if not learning
        if not self._is_learning:
            self._auto_label_known()

        return unknowns

    def _record_sighting(self, name: str, cmdline: str, now: str) -> None:
        """Record a process sighting in the baseline table."""
        try:
            existing = self._conn.execute(
                "SELECT id, seen_count FROM process_baseline WHERE name = ? AND cmdline = ?",
                (name, cmdline),
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE process_baseline SET last_seen = ?, seen_count = seen_count + 1 WHERE id = ?",
                    (now, existing["id"]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO process_baseline (name, cmdline, first_seen, last_seen, seen_count, known) "
                    "VALUES (?, ?, ?, ?, 1, 0)",
                    (name, cmdline, now, now),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()

    def _is_known(self, name: str, cmdline: str) -> bool:
        """Check if a process is in the known baseline."""
        try:
            row = self._conn.execute(
                "SELECT known, seen_count FROM process_baseline WHERE name = ? AND cmdline = ?",
                (name, cmdline),
            ).fetchone()
            if row is None:
                return self._is_learning  # Unknown in learning mode = known
            if row["known"]:
                return True
            if not self._is_learning and row["seen_count"] >= _BASELINE_MIN_SEEN:
                return True
            return self._is_learning
        except Exception:
            return True  # Default to known on error

    def _check_learning_mode(self) -> bool:
        """Check if the system is still in the learning period."""
        try:
            row = self._conn.execute(
                "SELECT MIN(first_seen) AS earliest FROM process_baseline"
            ).fetchone()
            if row is None or not row["earliest"]:
                return True
            earliest = datetime.fromisoformat(row["earliest"])
            days_elapsed = (datetime.now(timezone.utc) - earliest).days
            return days_elapsed < _BASELINE_DAYS
        except Exception:
            return True

    def _auto_label_known(self) -> None:
        """Auto-label processes seen > 3 times as known."""
        try:
            self._conn.execute(
                "UPDATE process_baseline SET known = 1 WHERE seen_count >= ? AND known = 0",
                (_BASELINE_MIN_SEEN,),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()

    # ── Queries ──────────────────────────────────────────────────────

    def get_baseline(self, known_only: bool = False, limit: int = 100) -> list[dict]:
        """Get the process baseline.

        Args:
            known_only: If True, only return known processes.
            limit: Max rows.

        Returns:
            List of dicts with process baseline info.
        """
        if known_only:
            rows = self._conn.execute(
                "SELECT * FROM process_baseline WHERE known = 1 "
                "ORDER BY seen_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM process_baseline ORDER BY seen_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_unknown_processes(self, limit: int = 50) -> list[dict]:
        """Get processes NOT in the known baseline.

        Returns:
            List of process dicts from the baseline that are unknown
            (sorted by most recently seen).
        """
        rows = self._conn.execute(
            "SELECT * FROM process_baseline WHERE known = 0 "
            "ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_process_detail(self, name: str) -> Optional[dict]:
        """Get detailed baseline info for a specific process name."""
        row = self._conn.execute(
            "SELECT * FROM process_baseline WHERE name = ? ORDER BY seen_count DESC LIMIT 1",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    # ── User tagging ─────────────────────────────────────────────────

    def tag_process(self, name: str, known: bool, label: str = "") -> bool:
        """Explicitly tag a process as known or unknown.

        Args:
            name: Process name to tag (matches all cmdline variants).
            known: True to mark as known, False to mark as unknown.
            label: Optional user label (e.g. "my dev server").

        Returns:
            True if at least one row was updated.
        """
        try:
            cur = self._conn.execute(
                "UPDATE process_baseline SET known = ?, user_label = ? WHERE name = ?",
                (1 if known else 0, label, name),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except Exception:
            self._conn.rollback()
            return False

    def get_all_current_processes(self) -> list[dict]:
        """Get all currently running processes with CPU/mem info.

        Returns:
            List of process dicts with pid, name, cpu, mem, cmdline, runtime.
        """
        import psutil

        result: list[dict] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_percent", "create_time"]):
            try:
                info = proc.info
                name = info["name"] or "?"
                cmdline = " ".join(info["cmdline"] or [])[:200]
                if not name or name in ("", "?"):
                    continue

                known = self._is_known(name, cmdline)
                result.append({
                    "pid": info["pid"],
                    "name": name,
                    "cpu": info.get("cpu_percent", 0) or 0,
                    "mem": info.get("memory_percent", 0) or 0,
                    "cmdline": cmdline,
                    "runtime": (time.time() - (info.get("create_time", time.time()) or time.time())),
                    "known": known,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        result.sort(key=lambda p: -p["cpu"])
        return result

    def get_all_current_processes_grouped(self) -> dict[str, list[dict]]:
        """Get all currently running processes, grouped by known/unknown."""
        all_procs = self.get_all_current_processes()
        known = [p for p in all_procs if p["known"]]
        unknown = [p for p in all_procs if not p["known"]]
        return {"known": known, "unknown": unknown}


# ═════════════════════════════════════════════════════════════════════════
# ResourceAlert — threshold-based alerting with debounce
# ═════════════════════════════════════════════════════════════════════════


class ResourceAlert:
    """Threshold-based resource alerting with debounce and recovery detection.

    Checks a snapshot against configured thresholds and returns alert events.
    Tracks the last alert time per metric to debounce (don't re-alert for the
    same metric within the debounce window unless critical).

    Usage::

        alerter = ResourceAlert(conn)
        alerts = alerter.check(snapshot)
        for alert in alerts:
            push_event(conn, alert)
    """

    def __init__(self, conn):
        self._conn = conn
        # Track last alert time per metric key (iso timestamp)
        self._last_alert: dict[str, float] = {}
        # Track previous snapshot health for recovery detection
        self._prev_health: Optional[dict[str, bool]] = None
        self._prev_snap: Optional[TelemetrySnapshot] = None

    # ── Main check ───────────────────────────────────────────────────

    def check(self, snap: TelemetrySnapshot) -> list[dict]:
        """Check a snapshot against all thresholds.

        Args:
            snap: The current telemetry snapshot.

        Returns:
            List of alert dicts, each with keys:
              - metric: name of the metric (e.g. "cpu", "memory", "disk")
              - level: "yellow", "red", or "critical"
              - current_value: the current value
              - threshold: the threshold that was exceeded
              - message: human-readable alert message
              - is_resolved: True if this is a recovery event
              - timestamp: ISO timestamp
        """
        alerts: list[dict] = []
        now = time.time()

        # ── CPU > 90% ──────────────────────────────────────────────
        if snap.cpu_percent >= _ALERT_CPU:
            if self._should_alert("cpu", now):
                alerts.append(self._build_alert(
                    metric="cpu", level="red",
                    current=snap.cpu_percent, threshold=_ALERT_CPU,
                    msg=f"CPU at {snap.cpu_percent:.0f}% (threshold: {_ALERT_CPU:.0f}%)",
                ))
        else:
            # Recovery
            if self._prev_health and self._prev_health.get("cpu_alerted"):
                alerts.append(self._build_resolved("cpu", "CPU back to normal"))

        # ── Memory > 90% ───────────────────────────────────────────
        if snap.memory_percent >= _ALERT_MEM:
            if self._should_alert("memory", now):
                alerts.append(self._build_alert(
                    metric="memory", level="red",
                    current=snap.memory_percent, threshold=_ALERT_MEM,
                    msg=f"Memory at {snap.memory_percent:.0f}% (threshold: {_ALERT_MEM:.0f}%)",
                ))
        else:
            if self._prev_health and self._prev_health.get("mem_alerted"):
                alerts.append(self._build_resolved("memory", "Memory back to normal"))

        # ── Disk > 90% ─────────────────────────────────────────────
        if snap.disk_percent >= _ALERT_DISK:
            if self._should_alert("disk", now):
                alerts.append(self._build_alert(
                    metric="disk", level="red",
                    current=snap.disk_percent, threshold=_ALERT_DISK,
                    msg=f"Disk at {snap.disk_percent:.0f}% (threshold: {_ALERT_DISK:.0f}%)",
                ))
        else:
            if self._prev_health and self._prev_health.get("disk_alerted"):
                alerts.append(self._build_resolved("disk", "Disk space back to normal"))

        # ── Disk free < 1GB (critical) ────────────────────────────
        free_bytes = snap.disk_total - snap.disk_used
        if free_bytes < _ALERT_DISK_FREE_CRITICAL:
            if self._should_alert("disk_free", now, critical=True):
                free_mb = free_bytes / (1024 * 1024)
                alerts.append(self._build_alert(
                    metric="disk_free", level="critical",
                    current=free_mb, threshold=_ALERT_DISK_FREE_CRITICAL / (1024 * 1024),
                    msg=f"Only {free_mb:.0f} MB free on disk (critical: < 1 GB)",
                ))
        else:
            if self._prev_health and self._prev_health.get("disk_free_alerted"):
                alerts.append(self._build_resolved("disk_free", "Disk free space recovered"))

        # ── Swap > 50% ─────────────────────────────────────────────
        if snap.swap_total > 0 and snap.swap_percent >= _ALERT_SWAP:
            if self._should_alert("swap", now):
                alerts.append(self._build_alert(
                    metric="swap", level="yellow",
                    current=snap.swap_percent, threshold=_ALERT_SWAP,
                    msg=f"Swap at {snap.swap_percent:.0f}% (threshold: {_ALERT_SWAP:.0f}%)",
                ))
        else:
            if self._prev_health and self._prev_health.get("swap_alerted"):
                alerts.append(self._build_resolved("swap", "Swap usage back to normal"))

        # ── GPU temp > 85°C ────────────────────────────────────────
        if snap.gpu and snap.gpu.get("temperature", 0) >= _ALERT_GPU_TEMP:
            if self._should_alert("gpu_temp", now):
                temp = snap.gpu["temperature"]
                alerts.append(self._build_alert(
                    metric="gpu_temp", level="red",
                    current=temp, threshold=_ALERT_GPU_TEMP,
                    msg=f"GPU temperature at {temp}°C (threshold: {_ALERT_GPU_TEMP}°C)",
                ))
        else:
            if self._prev_health and self._prev_health.get("gpu_temp_alerted"):
                alerts.append(self._build_resolved("gpu_temp", "GPU temperature back to normal"))

        # ── Network interface down detection ──────────────────────
        if snap.per_net:
            for iface, info in snap.per_net.items():
                # An interface with zero sent AND zero received might be down.
                # Skip loopback.
                if iface == "lo":
                    continue
                sent = info.get("bytes_sent", 0)
                recv = info.get("bytes_recv", 0)
                if sent == 0 and recv == 0:
                    if self._should_alert(f"net_down_{iface}", now, critical=True):
                        alerts.append(self._build_alert(
                            metric=f"net_down_{iface}", level="critical",
                            current=0, threshold=1,
                            msg=f"Network interface '{iface}' appears down (0 bytes sent/recv)",
                        ))
                else:
                    if self._prev_snap and self._prev_snap.per_net and self._prev_snap.per_net.get(iface, {}).get("bytes_sent", 0) == 0 and self._prev_snap.per_net.get(iface, {}).get("bytes_recv", 0) == 0:
                        if sent > 0 or recv > 0:
                            alerts.append(self._build_resolved(
                                f"net_down_{iface}",
                                f"Network interface '{iface}' is back up",
                            ))

        # ── Process count spike ────────────────────────────────────
        baseline_count = self._get_baseline_process_count()
        if baseline_count > 0 and snap.processes > baseline_count * _ALERT_PROCESS_MULTIPLIER:
            if self._should_alert("process_spike", now):
                alerts.append(self._build_alert(
                    metric="process_spike", level="red",
                    current=snap.processes, threshold=int(baseline_count * _ALERT_PROCESS_MULTIPLIER),
                    msg=f"Process count at {snap.processes} (2x baseline: {baseline_count})",
                ))

        # Track health for recovery detection
        self._prev_health = {
            "cpu_alerted": snap.cpu_percent >= _ALERT_CPU,
            "mem_alerted": snap.memory_percent >= _ALERT_MEM,
            "disk_alerted": snap.disk_percent >= _ALERT_DISK,
            "disk_free_alerted": free_bytes < _ALERT_DISK_FREE_CRITICAL,
            "swap_alerted": snap.swap_percent >= _ALERT_SWAP if snap.swap_total > 0 else False,
            "gpu_temp_alerted": snap.gpu and snap.gpu.get("temperature", 0) >= _ALERT_GPU_TEMP,
        }
        self._prev_snap = snap

        return alerts

    # ── Helpers ──────────────────────────────────────────────────────

    def _should_alert(self, metric: str, now: float, critical: bool = False) -> bool:
        """Check if we should alert for this metric based on debounce.

        Args:
            metric: Metric key.
            now: Current timestamp.
            critical: If True, use shorter debounce window.

        Returns:
            True if the alert should fire.
        """
        debounce = _ALERT_DEBOUNCE_CRITICAL if critical else _ALERT_DEBOUNCE_SECONDS
        last = self._last_alert.get(metric, 0)
        if now - last >= debounce:
            self._last_alert[metric] = now
            return True
        return False

    def _build_alert(
        self, metric: str, level: str,
        current: float, threshold: float, msg: str,
    ) -> dict:
        """Build an alert dict for an active alert."""
        return {
            "metric": metric,
            "level": level,
            "current_value": current,
            "threshold": threshold,
            "message": msg,
            "is_resolved": False,
            "timestamp": now_iso(),
        }

    def _build_resolved(self, metric: str, msg: str) -> dict:
        """Build a resolution/recovery alert dict."""
        return {
            "metric": metric,
            "level": "green",
            "current_value": 0,
            "threshold": 0,
            "message": msg,
            "is_resolved": True,
            "timestamp": now_iso(),
        }

    def _get_baseline_process_count(self) -> int:
        """Get the average process count from the baseline (past hour)."""
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            row = self._conn.execute(
                "SELECT AVG(processes) AS avg_count FROM telemetry_snapshots "
                "WHERE timestamp >= ?",
                (cutoff,),
            ).fetchone()
            if row and row["avg_count"]:
                return int(row["avg_count"])
        except Exception:
            pass
        return 0

    def push_alerts_to_feed(self, alerts: list[dict]) -> int:
        """Push alert results to the ambient feed.

        Args:
            alerts: Alert dicts from ``check()``.

        Returns:
            Number of events pushed.
        """
        pushed = 0
        for alert in alerts:
            try:
                from .ambient import AmbientEvent, push_event, now_iso as ambient_ts

                if alert["is_resolved"]:
                    ev = AmbientEvent(
                        timestamp=alert["timestamp"],
                        event_type="resource_alert",
                        title=f"✅ Resolved: {alert['metric']}",
                        detail=alert["message"],
                        source="telemetry",
                        priority=1,
                        category="system",
                    )
                else:
                    pri = {"yellow": 1, "red": 2, "critical": 3}.get(alert.get("level", "red"), 2)
                    ev = AmbientEvent(
                        timestamp=alert["timestamp"],
                        event_type="resource_alert",
                        title=f"⚠️ {alert['metric'].upper()} alert: {alert['level'].upper()}",
                        detail=alert["message"],
                        source="telemetry",
                        priority=pri,
                        category="system",
                    )
                push_event(self._conn, ev)
                pushed += 1
            except Exception:
                continue
        return pushed

    def push_process_anomaly_events(self, unknowns: list[dict], limit: int = 5) -> int:
        """Push process anomaly events to the ambient feed.

        Args:
            unknowns: List of unknown process dicts from ``ProcessMonitor.collect()``.
            limit: Max events to push.

        Returns:
            Number of events pushed.
        """
        pushed = 0
        for proc in unknowns[:limit]:
            try:
                from .ambient import AmbientEvent, push_event

                name = proc.get("name", "?")
                pid = proc.get("pid", "?")
                cpu = proc.get("cpu", 0)
                mem = proc.get("mem", 0)
                cmdline = proc.get("cmdline", "")[:60]
                ev = AmbientEvent(
                    timestamp=now_iso(),
                    event_type="process_anomaly",
                    title=f"Unknown process: {name} (PID {pid})",
                    detail=f"CPU: {cpu:.1f}%  MEM: {mem:.1f}%  Cmd: {cmdline}",
                    source="telemetry",
                    priority=2,
                    category="system",
                )
                push_event(self._conn, ev)
                pushed += 1
            except Exception:
                continue
        return pushed
