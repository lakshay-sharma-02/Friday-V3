# System Intelligence — Prompt for Claude Code

## Intent
FRIDAY in the MCU reads suit vitals in real-time — power levels, thruster efficiency, damage reports. She's *inside* the machine. Your Friday has observers that poll every 60s+ but no live telemetry stream. It can't answer "How's my system doing?" with current, real-time data. Also missing: awareness of what processes are running, build/test pipeline watching, and proactive resource alerts.

## What to build

### Phase 1: Live Telemetry Stream

Create `src/friday/telemetry.py` with a `TelemetryCollector` that runs as a daemon sidecar thread (not a cycle hook — continuous).

**What it collects (all local, no deps beyond stdlib + /proc):**
- **CPU**: per-core usage, load average, temperature (via `sensors` or `thermal_zone`), process count
- **Memory**: total, used, free, swap, cached, available
- **Disk**: per-mount usage, IO stats, inode usage
- **Network**: bytes up/down, errors, drops, per-interface
- **GPU**: if NVIDIA (`nvidia-smi`), if AMD (`rocm-smi`), utilization, memory, temp
- **Processes**: top 5 by CPU, top 5 by memory, total count
- **Uptime**: system uptime, Friday daemon uptime

**Collection design:**
- Poll interval: CPU/mem/disk every 10s, GPU every 30s, everything else every 60s
- Ring buffer in memory: last 60 minutes of data (360 samples for fast metrics)
- Persist snapshots to `telemetry_snapshots` table every 5 minutes (for historical queries)
- On request (`friday telemetry`), collect a FRESH snapshot immediately
- All collection is best-effort — a failed sensor never crashes the daemon

**Output:**
- `friday telemetry` → formatted table with current vitals
- `friday telemetry --live` → streaming view (like `htop` in terminal)
- `friday telemetry cpu` → CPU-specific view with per-core breakdown
- `friday telemetry --since 1h` → historical trend for last hour

**Key design:**
- Zero new dependencies — everything from `/proc`, `/sys`, and subprocess calls to existing tools
- Telemetry thread communicates via a shared `telemetry_cache` dict with a lock — never blocks the main cycle
- GPU data is optional (graceful fallback to "GPU metrics unavailable")
- Include a `telemetry_health` field: "green" (all nominal), "yellow" (one warning), "red" (critical threshold exceeded)

### Phase 2: Process Monitoring

Build into `telemetry.py` (same module, `ProcessMonitor` class).

**What it does:**
- Maintains a **known process baseline**: processes seen across the last N cycles that are normal for your environment (your shell, editor, browser, etc.)
- On each snapshot, identifies UNKNOWN processes — ones not in the baseline
- Flags: new background process, crypto miner (CPU + high network), unexpected daemon, fork bomb (rapid process count growth)
- Stores process sightings in `process_baseline` table: `(name, cmdline, first_seen, last_seen, count, known: bool)`
- `friday processes` → show current processes, highlight unknowns
- `friday processes --unknown` → only unknown/new processes
- `friday process <name>` → details about a specific process (cpu, mem, runtime, command line)

**Baseline learning:**
- First 7 days are learning mode — every process is marked as potentially known
- After 7 days, processes seen > 3 times are marked as known
- User can explicitly mark: `friday process tag <pid> --known "my dev server"`
- User can explicitly flag: `friday process tag <pid> --unknown "investigate this"`

### Phase 3: Resource Alerting

Build into `telemetry.py` (`ResourceAlert` class) and `daemon.py`.

**Thresholds (configurable, sensible defaults):**
- CPU: > 90% for 5+ minutes → alert
- Memory: > 90% used → alert
- Disk: any mount > 90% → alert
- Disk: any mount < 1GB free → critical alert
- GPU temp: > 85°C → alert
- Swap: > 50% used → alert
- Process count: > 2x baseline → alert
- Network: interface down → critical alert

**Delivery:**
- Resource alerts are pushed as ambient feed events with appropriate priority:
  - GREEN → no event
  - YELLOW → info event (priority 1), folded into next briefing
  - RED → important event (priority 2), routed through presence-gated interrupt
  - CRITICAL → urgent event (priority 3), immediate notification regardless of presence
- Recovery: when a metric returns to normal, push a "resolved" event
- Debounce: don't re-alert for the same metric within 60 minutes unless it's critical

### Phase 4: Build/Test Watcher (upgrade)

Your existing `watcher.py` has `shell_exit_code` which can check build status. But it's generic. Add a purpose-built `BuildWatcher` that understands build output:

- `friday watch build --cmd "cargo build"` → watches the build, not just exit code
- Parses build output for: compilation errors, warnings, test failures, slow tests
- On first success after previous failure: "Build is green again" event
- On first failure after previous success: "Build is red" urgent event
- Stores build history in `build_history` table: `(timestamp, success, error_count, warning_count, duration_ms)`
- `friday build history` → show build trend over time
- Tracks build duration — if it suddenly doubles, flag it

## Files to touch
- `src/friday/telemetry.py` (new) — TelemetryCollector, ProcessMonitor, ResourceAlert
- `src/friday/daemon.py` — telemetry sidecar thread, process baseline hook
- `src/friday/db.py` — add `telemetry_snapshots`, `process_baseline`, `build_history` tables
- `src/friday/cli.py` — add `friday telemetry`, `friday processes`, `friday process`, `friday build history` commands
- `src/friday/ambient.py` — add `resource_alert`, `build_status_changed`, `process_anomaly` event types
- `src/friday/proactive.py` — wire resource alerts through the interrupt queue
- `src/friday/presence.py` — (if presence module exists) resource alerts can use presence state for escalation
- `tests/test_telemetry.py` (new)
- `tests/test_process_monitor.py` (new)

## Acceptance criteria
1. `friday telemetry` shows CPU, memory, disk, network, GPU (if available), uptime in a formatted table
2. `friday telemetry --live` shows a streaming htop-style view
3. `friday processes` lists running processes with CPU/mem, highlights unknown ones
4. Unknown process detected → `process_anomaly` event in ambient feed
5. CPU > 90% for 5 minutes → `resource_alert` event with priority 2
6. When alert condition clears → "resolved" event pushed
7. `friday watch build --cmd "cargo build"` monitors build, pushes status change events
8. `friday build history` shows build trend (last N builds, pass/fail, duration, warnings)
9. All telemetry collection gracefully handles missing data (no GPU, no /proc access, etc.)
10. Zero new required dependencies — everything uses stdlib or existing system tools
