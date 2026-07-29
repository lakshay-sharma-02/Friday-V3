"""System Intelligence CLI — telemetry, processes, build commands.

Usage::

    friday telemetry                       # One-shot snapshot
    friday telemetry --live                # Live-updating dashboard
    friday telemetry cpu                   # CPU-specific view
    friday telemetry memory                # Memory-specific view
    friday telemetry disk                  # Disk-specific view
    friday telemetry net                   # Network-specific view
    friday telemetry --since 1h            # Historical trend
    friday processes                       # List processes
    friday processes --unknown             # Unknown processes only
    friday process <name>                  # Process details
    friday process tag <pid> --known       # Tag as known
    friday build history                   # Build history
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
import os
from typing import Optional

from .db import connect


# ═════════════════════════════════════════════════════════════════════════
# Main dispatcher
# ═════════════════════════════════════════════════════════════════════════


def cmd_telemetry(args: argparse.Namespace) -> int:
    """Dispatch `friday telemetry`."""
    view = getattr(args, "view", None)
    live_mode = getattr(args, "live", False)
    json_mode = getattr(args, "json", False)
    brief = getattr(args, "brief", False)
    include_gpu = getattr(args, "gpu", False)
    count = getattr(args, "count", 5)
    interval = getattr(args, "interval", 2)
    since = getattr(args, "since", None)

    if since:
        return _show_history(since=since)
    if live_mode:
        return _run_live(include_gpu=include_gpu, count=count, interval=interval)
    if json_mode:
        return _show_json(include_gpu=include_gpu)
    if view == "cpu":
        return _show_cpu()
    if view == "memory" or view == "mem":
        return _show_memory()
    if view == "disk":
        return _show_disk()
    if view == "net" or view == "network":
        return _show_network()
    return _show_snapshot(include_gpu=include_gpu, brief=brief)


def cmd_processes(args: argparse.Namespace) -> int:
    """Dispatch `friday processes`."""
    unknown_only = getattr(args, "unknown", False)
    return _list_processes(unknown_only=unknown_only)


def cmd_process(args: argparse.Namespace) -> int:
    """Dispatch `friday process`."""
    action = getattr(args, "action", None)
    name = getattr(args, "name", None)
    pid = getattr(args, "pid", None)
    known = getattr(args, "known", False)
    label = getattr(args, "label", "")

    if action == "tag":
        pid_val = pid or name
        if not pid_val:
            print("error: specify a process name or PID to tag", file=sys.stderr)
            return 1
        return _tag_process(pid_val, known=known, label=label)

    # Default: show process details
    if not name:
        print("error: specify a process name", file=sys.stderr)
        return 1
    return _show_process(name)


def cmd_build(args: argparse.Namespace) -> int:
    """Dispatch `friday build`."""
    action = getattr(args, "action", "history")
    project = getattr(args, "project", "")
    limit = getattr(args, "limit", 20)

    if action == "history":
        return _show_build_history(project=project, limit=limit)
    if action == "run":
        cmd = getattr(args, "command", "")
        if not cmd:
            print("error: specify a command to run (e.g. --cmd 'cargo build')",
                  file=sys.stderr)
            return 1
        return _run_build(cmd, project=project)
    return 1


# ═════════════════════════════════════════════════════════════════════════
# Telemetry snapshot commands
# ═════════════════════════════════════════════════════════════════════════


def _show_snapshot(include_gpu: bool = False, brief: bool = False) -> int:
    """Collect and display a single snapshot."""
    try:
        from .telemetry import collect_telemetry, format_snapshot
        snap = collect_telemetry(include_gpu=include_gpu)
        print(format_snapshot(snap, brief=brief))
        return 0
    except ImportError as exc:
        print(f"error: telemetry requires psutil: pip install psutil ({exc})",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: telemetry collection failed: {exc}", file=sys.stderr)
        return 1


def _show_json(include_gpu: bool = False) -> int:
    try:
        from .telemetry import collect_telemetry, format_json
        snap = collect_telemetry(include_gpu=include_gpu)
        print(format_json(snap))
        return 0
    except ImportError as exc:
        print(f"error: telemetry requires psutil: pip install psutil ({exc})",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _show_cpu() -> int:
    """Show CPU-specific view with per-core breakdown."""
    try:
        import psutil
        print("── CPU ──────────────────────────────")
        print(f"  Overall: {psutil.cpu_percent(interval=0.2):.1f}%")
        print(f"  Cores:   {psutil.cpu_count(logical=True)} logical, "
              f"{psutil.cpu_count(logical=False)} physical")
        freq = psutil.cpu_freq()
        if freq:
            print(f"  Freq:    {freq.current:.0f}MHz (min {freq.min:.0f}, max {freq.max:.0f})")
        load = psutil.getloadavg()
        print(f"  Load:    {load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}")
        per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)
        print(f"  Per-core:")
        cols = 4
        for i in range(0, len(per_cpu), cols):
            row = per_cpu[i:i+cols]
            parts = "  ".join(f"Core {i+j}: {c:.1f}%".ljust(16) for j, c in enumerate(row))
            print(f"    {parts}")
        temp = _get_cpu_temp()
        if temp:
            print(f"  Temp:    {temp:.1f}°C")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _show_memory() -> int:
    """Show memory-specific view."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        print("── MEMORY ────────────────────────────")
        print(f"  Total:     {_fmt_bytes(mem.total)}")
        print(f"  Available: {_fmt_bytes(mem.available)} ({mem.percent:.1f}% used)")
        print(f"  Used:      {_fmt_bytes(mem.used)}")
        print(f"  Free:      {_fmt_bytes(mem.free)}")
        if hasattr(mem, 'cached') and mem.cached:
            print(f"  Cached:    {_fmt_bytes(mem.cached)}")
        if hasattr(mem, 'buffers') and mem.buffers:
            print(f"  Buffers:   {_fmt_bytes(mem.buffers)}")
        if swap.total:
            print(f"── SWAP ─────────────────────────────")
            print(f"  Total:     {_fmt_bytes(swap.total)}")
            print(f"  Used:      {_fmt_bytes(swap.used)} ({swap.percent:.1f}%)")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _show_disk() -> int:
    """Show disk-specific view with per-mount breakdown."""
    try:
        import psutil
        print("── DISK ──────────────────────────────")
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                bar = _bar(usage.percent, 100)
                print(f"  {part.mountpoint:<20} {_fmt_bytes(usage.used)}/{_fmt_bytes(usage.total)} "
                      f"({usage.percent:.1f}%) {bar}")
                print(f"  {'':<20} {_fmt_bytes(usage.free)} free  [{part.fstype}]")
            except (PermissionError, OSError):
                continue
        # Disk I/O
        try:
            io = psutil.disk_io_counters()
            print(f"── DISK I/O ─────────────────────────")
            print(f"  Read:  {_fmt_bytes(io.read_bytes)}  ({io.read_count} ops)")
            print(f"  Write: {_fmt_bytes(io.write_bytes)}  ({io.write_count} ops)")
        except Exception:
            pass
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _show_network() -> int:
    """Show network-specific view with per-interface breakdown."""
    try:
        import psutil
        print("── NETWORK ───────────────────────────")
        io = psutil.net_io_counters()
        print(f"  Total:  ↑ {_fmt_bytes(io.bytes_sent)}  ↓ {_fmt_bytes(io.bytes_recv)}")
        print(f"  Errors: ↑ {io.errout}  ↓ {io.errin}")
        print(f"  Drops:  ↑ {io.dropout}  ↓ {io.dropin}")
        print(f"── Per-Interface ─────────────────────")
        per_nic = psutil.net_io_counters(pernic=True)
        for iface, nic in sorted(per_nic.items()):
            print(f"  {iface:<12} ↑ {_fmt_bytes(nic.bytes_sent):>10}  ↓ {_fmt_bytes(nic.bytes_recv):>10}  "
                  f"err {nic.errin}/{nic.errout}  drop {nic.dropin}/{nic.dropout}")
        # Network interfaces/addresses
        if_addrs = psutil.net_if_addrs()
        print(f"── Addresses ─────────────────────────")
        for iface, addrs in sorted(if_addrs.items()):
            for addr in addrs:
                if addr.family.name == "AF_INET":
                    print(f"  {iface:<12} {addr.address}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _show_history(since: str = "1h") -> int:
    """Show historical telemetry trend."""
    try:
        from .telemetry import get_telemetry_history
        conn = connect()

        # Parse since
        if since.endswith("h"):
            hours = int(since[:-1])
            since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        elif since.endswith("m"):
            minutes = int(since[:-1])
            since_iso = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        else:
            since_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        rows = get_telemetry_history(conn, since=since_iso, limit=100)
        conn.close()

        if not rows:
            print(f"  No telemetry data since {since}.")
            return 0

        print(f"── Telemetry History (since {since}) ──────────────────")
        print(f"  {'Time':<20} {'CPU':<7} {'MEM':<7} {'DISK':<7} {'SWAP':<7} {'Procs':<7} {'Health':<8}")
        print(f"  {'-'*56}")
        for r in rows:
            ts = (r["timestamp"] or "?")[11:19]
            cpu = f"{r['cpu_percent']:.1f}%"
            mem = f"{r['memory_percent']:.1f}%"
            disk = f"{r['disk_percent']:.1f}%"
            swap = f"{r['swap_percent']:.1f}%"
            procs = str(r["processes"])
            health = r["health"]
            print(f"  {ts:<20} {cpu:<7} {mem:<7} {disk:<7} {swap:<7} {procs:<7} {health:<8}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ═════════════════════════════════════════════════════════════════════════
# Live dashboard
# ═════════════════════════════════════════════════════════════════════════


def _run_live(
    include_gpu: bool = False,
    count: int = 30,
    interval: int = 2,
) -> int:
    """Run a live-updating telemetry dashboard using rich."""
    try:
        from .telemetry import collect_telemetry
        from rich.console import Console
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich.layout import Layout
    except ImportError as exc:
        print(f"error: live mode requires rich library: {exc}", file=sys.stderr)
        return 1

    console = Console()
    collect_telemetry(include_gpu=include_gpu)  # warm up

    def _build_layout(snap, iteration: int) -> Layout:
        layout = Layout()
        header = Text.assemble(
            ("╔═══ FRIDAY TELEMETRY ═══╗", "bold cyan"),
            Text(f"  snapshot #{iteration}  health: {snap.health.upper()}", "dim"),
        )
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
        )
        layout["header"].update(Panel(header, style="cyan"))

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()

        # CPU
        freq_s = f"{snap.cpu_freq_current:.0f}MHz" if snap.cpu_freq_current else "?"
        cpu_bar = _bar(snap.cpu_percent, 100)
        grid.add_row("CPU", f"{snap.cpu_percent:.1f}%  {cpu_bar}")
        grid.add_row("     ", f"{snap.cpu_count} cores @ {freq_s}")

        # Memory
        mem_bar = _bar(snap.memory_percent, 100)
        mem_free = _fmt_bytes(snap.memory_available)
        mem_total = _fmt_bytes(snap.memory_total)
        grid.add_row("MEM", f"{snap.memory_percent:.1f}%  {mem_bar}")
        grid.add_row("     ", f"{mem_free} free / {mem_total}")

        if snap.swap_total:
            swap_bar = _bar(snap.swap_percent, 100)
            swap_used = _fmt_bytes(snap.swap_used)
            swap_total = _fmt_bytes(snap.swap_total)
            grid.add_row("SWAP", f"{snap.swap_percent:.1f}%  {swap_bar}")
            grid.add_row("     ", f"{swap_used} / {swap_total}")

        # Disk
        disk_bar = _bar(snap.disk_percent, 100)
        disk_used = _fmt_bytes(snap.disk_used)
        disk_total = _fmt_bytes(snap.disk_total)
        grid.add_row("DISK", f"{snap.disk_percent:.1f}%  {disk_bar}")
        grid.add_row("     ", f"{disk_used} / {disk_total}")

        # Network
        net_up = _fmt_bytes(snap.net_bytes_sent)
        net_down = _fmt_bytes(snap.net_bytes_recv)
        grid.add_row("NET", f"↑ {net_up}  ↓ {net_down}")

        # Load & Processes
        grid.add_row("LOAD", f"{snap.load_avg[0]:.2f} {snap.load_avg[1]:.2f} {snap.load_avg[2]:.2f}")
        grid.add_row("PROCS", str(snap.processes))

        # Top CPU
        if snap.top_cpu:
            top_strs = [f"{p.get('name','?')} {p.get('cpu',0):.0f}%" for p in snap.top_cpu[:3]]
            grid.add_row("TOP CPU", ", ".join(top_strs))

        # Top MEM
        if snap.top_mem:
            top_strs = [f"{p.get('name','?')} {p.get('mem',0):.0f}%" for p in snap.top_mem[:3]]
            grid.add_row("TOP MEM", ", ".join(top_strs))

        # GPU
        if snap.gpu:
            g = snap.gpu
            gpu_bar = _bar(g.get("utilization", 0), 100)
            gpu_mem = f"{g.get('memory_used', 0)}/{g.get('memory_total', 0)}MB"
            grid.add_row("GPU", f"{g.get('utilization', 0)}%  {gpu_bar}")
            grid.add_row("     ", f"{g.get('name', '?')}  {gpu_mem}  {g.get('temperature', '?')}°C")

        # Uptime
        if snap.system_uptime:
            grid.add_row("UPTIME", _fmt_duration(snap.system_uptime))

        # Health
        health_color = {"green": "green", "yellow": "yellow", "red": "red"}.get(snap.health, "white")
        grid.add_row("HEALTH", f"[{health_color}]{snap.health.upper()}[/{health_color}]")

        layout["body"].update(Panel(grid, title="[bold]System Resources[/bold]", border_style="green"))
        return layout

    try:
        with Live(auto_refresh=False, console=console, screen=False) as live:
            for i in range(count):
                snap = collect_telemetry(include_gpu=include_gpu)
                layout = _build_layout(snap, i + 1)
                live.update(layout, refresh=True)
                if i < count - 1:
                    time.sleep(interval)
    except KeyboardInterrupt:
        pass
    return 0


# ═════════════════════════════════════════════════════════════════════════
# Process commands
# ═════════════════════════════════════════════════════════════════════════


def _list_processes(unknown_only: bool = False) -> int:
    """List running processes, highlighting unknowns."""
    try:
        conn = connect()
        from .telemetry import ProcessMonitor
        monitor = ProcessMonitor(conn)

        grouped = monitor.get_all_current_processes_grouped()

        if unknown_only:
            procs = grouped["unknown"]
            if not procs:
                print("  No unknown processes found.")
                return 0
            print(f"── Unknown Processes ({len(procs)}) ────────────────────")
            print(f"  {'PID':<8} {'CPU%':<7} {'MEM%':<7} {'Runtime':<12} {'Name':<20} Cmdline")
            print(f"  {'-'*70}")
            for p in procs[:30]:
                pid = str(p.get("pid", "?"))
                cpu = f"{p.get('cpu', 0):.1f}"
                mem = f"{p.get('mem', 0):.1f}"
                rt = _fmt_duration(p.get("runtime", 0))
                name = (p.get("name", "?") or "?")[:20]
                cmdline = (p.get("cmdline", "") or "")[:50]
                print(f"  {pid:<8} {cpu:<7} {mem:<7} {rt:<12} {name:<20} {cmdline}")
        else:
            known = grouped["known"]
            unknown = grouped["unknown"]
            print(f"── Processes ({len(known)} known, {len(unknown)} unknown) ──")
            print(f"  {'PID':<8} {'CPU%':<7} {'MEM%':<7} {'Runtime':<12} {'Name':<20} {'':>6} Cmdline")
            print(f"  {'-'*80}")

            # Print unknowns first (highlighted)
            for p in unknown[:10]:
                pid = str(p.get("pid", "?"))
                cpu = f"{p.get('cpu', 0):.1f}"
                mem = f"{p.get('mem', 0):.1f}"
                rt = _fmt_duration(p.get("runtime", 0))
                name = (p.get("name", "?") or "?")[:20]
                cmdline = (p.get("cmdline", "") or "")[:50]
                print(f"  {pid:<8} {cpu:<7} {mem:<7} {rt:<12} {name:<20} ⚠️  {cmdline}")
            if len(unknown) > 10:
                print(f"  ... and {len(unknown) - 10} more unknown processes")

            # Print top known by CPU
            known_sorted = sorted(known, key=lambda p: -p.get("cpu", 0))
            for p in known_sorted[:15]:
                pid = str(p.get("pid", "?"))
                cpu = f"{p.get('cpu', 0):.1f}"
                mem = f"{p.get('mem', 0):.1f}"
                rt = _fmt_duration(p.get("runtime", 0))
                name = (p.get("name", "?") or "?")[:20]
                cmdline = (p.get("cmdline", "") or "")[:50]
                print(f"  {pid:<8} {cpu:<7} {mem:<7} {rt:<12} {name:<20} {'':>6} {cmdline}")

        conn.close()
        return 0
    except ImportError as exc:
        print(f"error: requires psutil: pip install psutil ({exc})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _show_process(name: str) -> int:
    """Show details for a specific process."""
    try:
        conn = connect()
        from .telemetry import ProcessMonitor
        monitor = ProcessMonitor(conn)

        # Get all current processes and filter by name
        procs = monitor.get_all_current_processes()
        matches = [p for p in procs if name.lower() in p.get("name", "").lower()]

        if not matches:
            # Check baseline
            baseline = monitor.get_process_detail(name)
            if baseline:
                print(f"  Process '{name}' is not currently running.")
                print(f"  Last seen: {baseline.get('last_seen', '?')[:19]}")
                print(f"  Seen {baseline.get('seen_count', 0)} times")
                print(f"  Known: {bool(baseline.get('known'))}")
                if baseline.get("user_label"):
                    print(f"  Label: {baseline['user_label']}")
                conn.close()
                return 0
            print(f"  No process found matching '{name}'.")
            conn.close()
            return 1

        for p in matches[:10]:
            pid = p.get("pid", "?")
            cpu = p.get("cpu", 0)
            mem = p.get("mem", 0)
            rt = _fmt_duration(p.get("runtime", 0))
            cmdline = p.get("cmdline", "") or "(none)"
            known = "known" if p.get("known") else "⚠️ unknown"
            print(f"  PID:      {pid}")
            print(f"  Name:     {p.get('name', '?')}  ({known})")
            print(f"  CPU:      {cpu:.1f}%")
            print(f"  Memory:   {mem:.1f}%")
            print(f"  Runtime:  {rt}")
            print(f"  Cmdline:  {cmdline}")
            print()

        conn.close()
        return 0
    except ImportError as exc:
        print(f"error: requires psutil: pip install psutil ({exc})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _tag_process(pid_or_name, known: bool, label: str) -> int:
    """Tag a process as known or unknown."""
    try:
        conn = connect()
        from .telemetry import ProcessMonitor
        monitor = ProcessMonitor(conn)

        if pid_or_name.isdigit():
            pid = int(pid_or_name)
            import psutil
            try:
                proc = psutil.Process(pid)
                name = proc.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                print(f"error: process {pid} not found", file=sys.stderr)
                conn.close()
                return 1
        else:
            name = pid_or_name

        ok = monitor.tag_process(name, known=known, label=label)
        if ok:
            status = "known" if known else "unknown"
            label_str = f" ({label})" if label else ""
            print(f"  Tagged '{name}' as {status}{label_str}.")
        else:
            print(f"  Process '{name}' not found in baseline.", file=sys.stderr)
            conn.close()
            return 1

        conn.close()
        return 0
    except ImportError as exc:
        print(f"error: requires psutil: pip install psutil ({exc})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ═════════════════════════════════════════════════════════════════════════
# Build commands
# ═════════════════════════════════════════════════════════════════════════


def _show_build_history(project: str = "", limit: int = 20) -> int:
    """Show build history."""
    try:
        conn = connect()
        from .build_watcher import BuildWatcher, format_build_history, format_build_stats
        watcher = BuildWatcher(conn)

        if project:
            stats = watcher.get_stats(project=project)
        else:
            stats = watcher.get_stats()

        rows = watcher.get_history(project=project, limit=limit)
        print(format_build_stats(stats))
        print()
        print(format_build_history(rows))
        conn.close()
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_build(command: str, project: str = "") -> int:
    """Run a build command and show the result."""
    try:
        conn = connect()
        from .build_watcher import BuildWatcher
        watcher = BuildWatcher(conn)

        print(f"  Running: {command}")
        print()
        result = watcher.run(command, project=project)

        status = "✅ SUCCESS" if result.success else "❌ FAILED"
        print(f"  Status:   {status}")
        print(f"  Duration: {result.duration_ms}ms")
        print(f"  Errors:   {result.error_count}")
        print(f"  Warnings: {result.warning_count}")
        print(f"  Slow tests: {result.slow_test_count}")

        if result.errors:
            print(f"\n  Errors ({len(result.errors)}):")
            for e in result.errors[:10]:
                print(f"    {e}")
        if result.warnings:
            print(f"\n  Warnings ({len(result.warnings)}):")
            for w in result.warnings[:10]:
                print(f"    {w}")
        conn.close()
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}PB"


def _fmt_duration(seconds: float) -> str:
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


def _bar(value: float, maximum: float, width: int = 12) -> str:
    filled = int((value / max(maximum, 1)) * width)
    filled = max(0, min(filled, width))
    empty = width - filled
    return "█" * filled + "░" * empty


def _get_cpu_temp() -> Optional[float]:
    try:
        base = "/sys/class/thermal"
        max_temp = 0.0
        for entry in os.listdir(base):
            if entry.startswith("thermal_zone"):
                try:
                    with open(os.path.join(base, entry, "temp")) as f:
                        temp = int(f.read().strip()) / 1000.0
                        max_temp = max(max_temp, temp)
                except (OSError, ValueError):
                    continue
        return max_temp if max_temp > 0 else None
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════
# Subparser registration
# ═════════════════════════════════════════════════════════════════════════


def add_subparser(sub) -> None:
    """Add telemetry, processes, process, and build subcommands."""

    # ── telemetry ───────────────────────────────────────────────────
    p = sub.add_parser(
        "telemetry",
        help="System metrics: CPU, memory, disk, network, GPU.",
        description="Collect and display real-time system telemetry.",
    )
    p.add_argument(
        "view", nargs="?", default=None,
        choices=["cpu", "memory", "mem", "disk", "net", "network"],
        help="View a specific subsystem (cpu, memory, disk, net).",
    )
    p.add_argument("--live", "-l", action="store_true",
                    help="Live-updating dashboard (requires rich library).")
    p.add_argument("--json", "-j", action="store_true",
                    help="Output as JSON for scripting.")
    p.add_argument("--gpu", "-g", action="store_true",
                    help="Include GPU stats (requires nvidia-smi).")
    p.add_argument("--brief", "-b", action="store_true",
                    help="One-line summary instead of full block.")
    p.add_argument("--count", "-n", type=int, default=30,
                    help="Number of snapshots for live mode (default: 30).")
    p.add_argument("--interval", "-i", type=int, default=2,
                    help="Seconds between live snapshots (default: 2).")
    p.add_argument("--since", type=str, default=None,
                    help="Show historical trend (e.g. '1h', '30m').")
    p.set_defaults(func=cmd_telemetry)

    # ── processes ───────────────────────────────────────────────────
    p_procs = sub.add_parser(
        "processes",
        help="List running processes with CPU/mem, highlight unknowns.",
    )
    p_procs.add_argument("--unknown", "-u", action="store_true",
                          help="Show only unknown processes.")
    p_procs.set_defaults(func=cmd_processes)

    # ── process ─────────────────────────────────────────────────────
    p_proc = sub.add_parser(
        "process",
        help="Show or tag a specific process.",
    )
    p_proc.add_argument("action", nargs="?", default="show",
                         choices=["show", "tag"])
    p_proc.add_argument("name", nargs="?", default=None,
                         help="Process name or PID.")
    p_proc.add_argument("--pid", type=str, default=None,
                         help="PID to tag (alternative to name).")
    p_proc.add_argument("--known", action="store_true",
                         help="Tag as known process.")
    p_proc.add_argument("--unknown", dest="known", action="store_false",
                         help="Tag as unknown process.")
    p_proc.add_argument("--label", type=str, default="",
                         help="User label for the process (e.g. 'my dev server').")
    p_proc.set_defaults(func=cmd_process)

    # ── build ───────────────────────────────────────────────────────
    p_build = sub.add_parser(
        "build",
        help="Build history and monitoring.",
    )
    p_build.add_argument("action", nargs="?", default="history",
                          choices=["history", "run"])
    p_build.add_argument("--project", "-p", type=str, default="",
                          help="Project name filter.")
    p_build.add_argument("--limit", "-n", type=int, default=20,
                          help="Number of history entries (default: 20).")
    p_build.add_argument("--cmd", type=str, default=None,
                          help="Command to run (for 'run' action).")
    p_build.set_defaults(func=cmd_build)
