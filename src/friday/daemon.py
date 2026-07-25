"""Friday Daemon — persistent ambient observation loop.

Transforms Friday from a CLI-invoked tool into an always-on operating partner.
The daemon runs as a background process, periodically observing the workspace,
reacting to filesystem changes, and proactively surfacing insights.

Design:
- Classic PID-file daemon pattern (no systemd dependency)
- Polling-based filesystem watcher (zero new dependencies)
- Desktop notifications via notify-send / osascript
- Reuses the existing refresh() pipeline from observe.py
- Each cycle outcome is written to watch_history (same table as `friday watch`)
- SIGTERM = graceful shutdown, SIGHUP = trigger immediate cycle
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .db import connect, now_iso


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FRIDAY_DIR = Path.home() / ".friday"
PID_FILE = FRIDAY_DIR / "daemon.pid"
STATUS_FILE = FRIDAY_DIR / "daemon.status"
LOG_FILE = FRIDAY_DIR / "daemon.log"
LOCK_FILE = Path("/tmp") / ".friday-watch.lock"
WATCH_HISTORY_KEYS = (
    "repos_scanned", "repos_changed", "knowledge_updated",
    "understanding_updated", "initiatives_changed", "insights_changed",
)

# Phase A fields persisted in daemon.status so `friday daemon status` and
# notifications can surface ambient analysis results without re-querying.
_PHASE_A_FIELDS = ("new_suggestions", "high_severity_suggestions",
                    "new_gaps", "open_gaps",
                    "new_patterns", "top_patterns",
                    "new_intents", "high_conf_intents",
                    "new_skills")


# ---------------------------------------------------------------------------
# Notifications (best-effort, silent on failure)
# ---------------------------------------------------------------------------


def _notify(title: str, message: str) -> None:
    """Send a desktop notification. Best-effort; failures are silent."""
    try:
        if sys.platform == "linux":
            subprocess.run(
                ["notify-send", title, message],
                timeout=5, capture_output=True,
            )
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                timeout=5, capture_output=True,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process management utilities
# ---------------------------------------------------------------------------


def _is_pid_running(pid: int) -> bool:
    """Check if a PID belongs to a running process."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid() -> Optional[int]:
    """Read the daemon PID from the PID file, or None."""
    try:
        raw = PID_FILE.read_text().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _write_pid(pid: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def write_status(**updates) -> dict:
    """Write the daemon status JSON file. Returns the full status dict."""
    status = _read_status()
    status.update(updates)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f, indent=2)
    except OSError:
        pass
    return status


def _read_status() -> dict:
    """Read the current daemon status."""
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "state": "stopped",
            "pid": None,
            "started_at": None,
            "last_cycle_at": None,
            "last_cycle_outcome": None,
            "cycle_count": 0,
            "last_error": None,
            "interval_seconds": 900,
            "watched_repos": 0,
            "new_suggestions": 0,
            "high_severity_suggestions": 0,
            "new_gaps": 0,
            "open_gaps": 0,
        }


def _log(message: str) -> None:
    """Append a timestamped line to the daemon log."""
    timestamp = now_iso()
    line = f"[{timestamp}] {message}\n"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cycle runner (shared with friday watch)
# ---------------------------------------------------------------------------


def _run_cycle() -> dict:
    """Run a single observation cycle using the existing pipeline.

    Returns a dict with cycle outcome info (same shape as watch_history columns),
    suitable for status updates and watch_history persistence.

    Safe to call concurrently via the LOCK_FILE mechanism.
    """
    # Acquire lock — fail fast if another cycle is running.
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        return {"cycle_outcome": "skipped", "error_detail": "lock held by another cycle"}

    conn = connect()
    started = now_iso()
    cur = conn.execute(
        "INSERT INTO watch_history (started_at, outcome) VALUES (?, 'running')",
        (started,))
    history_id = cur.lastrowid
    conn.commit()

    cycle = {"cycle_outcome": "succeeded", "error_detail": None}
    try:
        from .observe import refresh
        rep = refresh(conn)

        # Harvest high-confidence initiatives (same as cli_watch._harvest_initiatives).
        try:
            from .cli_watch import _harvest_initiatives
            new_pending = _harvest_initiatives(conn, history_id)
        except Exception as exc:
            _log(f"Initiative harvesting failed: {exc}")
            new_pending = 0

        # Run the ObservationEngine with all registered observers (Hyprland,
        # Git, Runtime, etc.) to capture desktop-level, terminal-level, and
        # other ambient state as observations. Persisted observations feed the
        # context engine, the daemon's next analysis, and action-worker
        # verification. Isolated: a failing observer never crashes the cycle.
        try:
            from .observation import ObservationEngine, default_registry
            # Capture PRIOR observation state before the engine writes new ones.
            from .db import latest_observations
            prior_obs = [dict(r) for r in latest_observations(conn)]

            engine_run = ObservationEngine(default_registry(), conn).run()
            obs_count = len(engine_run.all_observations)
            if obs_count > 0:
                _log(f"ObservationEngine: {obs_count} fact(s) from "
                     f"{len(engine_run.observers)} observer(s).")

                # Derive action events from observation diffs (Pillar B Stage 1).
                # This detects user actions like workspace switches, app launches,
                # and window focus changes that happened between observation cycles.
                try:
                    from .action_log import (
                        diff_observations_to_actions, log_action, now_iso)
                    current_obs = [
                        {"source": o.source, "subject": o.subject,
                         "aspect": o.aspect, "value": o.value}
                        for o in engine_run.all_observations
                    ]
                    actions = diff_observations_to_actions(
                        prior_obs, current_obs, now_iso())
                    for action in actions:
                        log_action(conn, action)
                    if actions:
                        _log(f"ActionLogger: derived {len(actions)} action(s) "
                             f"from observation diffs.")
                except Exception as exc:
                    _log(f"ActionLogger diff failed: {exc}")
        except Exception as exc:
            _log(f"ObservationEngine cycle failed: {exc}")

        # Check for failed runtime sessions.
        try:
            from .repair import detect_repair_candidates, propose_repair
            candidates = detect_repair_candidates(conn)
            for c in candidates:
                propose_repair(conn, c)
        except Exception as exc:
            _log(f"Repair detection failed: {exc}")

        # Phase A: Post-cycle ambient analysis — run suggest + gap analyzer
        # automatically so Friday surfaces integration opportunities and
        # capability gaps without being asked.
        # Both hooks are isolated: a failure in either analysis never breaks
        # the cycle, but IS logged so the operator can see when a hook stops
        # working (e.g. after a schema change or DB migration).
        new_suggestions = 0
        high_severity_suggestions = 0
        try:
            from .cli_suggest import generate_suggestions
            sug_result = generate_suggestions(conn)
            if sug_result.suggestions:
                new_suggestions = len(sug_result.suggestions)
                high_severity_suggestions = sum(
                    1 for s in sug_result.suggestions if s.severity == "high")
        except Exception as exc:
            _log(f"Ambient analysis (suggest) failed: {exc}")

        new_gaps = 0
        open_gaps = 0
        try:
            from .meta.gap_analyzer import analyze
            gap_report = analyze(conn)
            new_gaps = gap_report.new_gaps
            open_gaps = gap_report.open_gaps
        except Exception as exc:
            _log(f"Ambient analysis (gaps) failed: {exc}")

        # Pillar B Stage 2: Run sequence mining on accumulated action events.
        # Discovers repeated action patterns (e.g. "open terminal → cd X → run Y")
        # that appear across sessions. Deterministic and fast — pure SQLite scan
        # + local computation. Old patterns are replaced, not appended.
        new_patterns = 0
        top_patterns = 0
        try:
            from .db import clear_mined_patterns, insert_mined_pattern
            from .sequence_miner import mine_sequences

            clear_mined_patterns(conn)
            patterns = mine_sequences(conn)
            for p in patterns:
                insert_mined_pattern(conn, {
                    "sequence_json": json.dumps([[t, tg] for t, tg in p.sequence]),
                    "count": p.count,
                    "distinct_sessions": p.count,
                    "first_seen": p.first_seen,
                    "last_seen": p.last_seen,
                    "common_workspace": p.common_workspace,
                    "common_project": p.common_project,
                    "confidence": "derived",
                    "mined_at": now_iso(),
                })
            new_patterns = len(patterns)
            top_patterns = sum(1 for p in patterns if p.count >= 3)
        except Exception as exc:
            _log(f"Sequence mining failed: {exc}")

        # Pillar B Stage 3: Run LLM intent labeling on mined patterns.
        # Converts raw action-sequence patterns into human-readable workflow
        # descriptions (e.g. "Start dev server", "Open project files").
        # Uses the LLM if available; falls back to deterministic labels.
        # Runs in try/except so a failed LLM call never breaks the cycle.
        new_intents = 0
        high_conf_intents = 0
        try:
            if new_patterns > 0:
                from .db import (
                    clear_workflow_intents, get_mined_patterns,
                    insert_workflow_intent)
                from .intent_labeler import label_intent

                clear_workflow_intents(conn)
                patterns_db = get_mined_patterns(conn)
                for p in patterns_db:
                    seq = json.loads(p["sequence_json"]) if isinstance(p["sequence_json"], str) else p["sequence_json"]
                    intent = label_intent(
                        pattern_sequence=[tuple(item) for item in seq],
                        pattern_count=p["count"],
                        workspace=p.get("common_workspace", ""),
                        project=p.get("common_project", ""),
                    )
                    insert_workflow_intent(conn, {
                        "pattern_id": p["id"],
                        "intent_label": intent.intent_label,
                        "intent_description": intent.intent_description,
                        "steps_text": json.dumps(intent.steps),
                        "confidence": intent.confidence,
                        "pattern_summary": json.dumps([[t, tg] for t, tg in intent.pattern_seq]),
                        "labeled_at": intent.labeled_at,
                    })
                    new_intents += 1
                    if intent.confidence in ("high", "medium"):
                        high_conf_intents += 1
                _log(f"Intent labeling: {new_intents} intent(s) labeled "
                     f"({high_conf_intents} high/medium confidence).")
        except Exception as exc:
            _log(f"Intent labeling failed: {exc}")

        # Pillar B Stage 4: Run skill formation on labeled intents.
        new_skills = 0
        try:
            if new_intents > 0:
                from .skill_formation import form_skills
                formed = form_skills(conn)
                new_skills = len(formed)
                if new_skills:
                    _log(f"Skill formation: {new_skills} skill(s) formed from intents.")
        except Exception as exc:
            _log(f"Skill formation failed: {exc}")

        conn.execute(
            "UPDATE watch_history SET finished_at=?, outcome=?, "
            "repos_scanned=?, repos_changed=?, "
            "knowledge_updated=?, understanding_updated=?, "
            "initiatives_changed=?, insights_changed=?, "
            "new_pending_initiatives=? WHERE id=?",
            (now_iso(), "succeeded",
             rep.repos_scanned, rep.repos_changed,
             rep.knowledge_updated, rep.understanding_updated,
             rep.initiatives_changed, rep.insights_changed,
             new_pending, history_id))
        conn.commit()

        cycle.update({
            "repos_scanned": rep.repos_scanned,
            "repos_changed": rep.repos_changed,
            "knowledge_updated": rep.knowledge_updated,
            "understanding_updated": rep.understanding_updated,
            "new_pending_initiatives": new_pending,
            "new_suggestions": new_suggestions,
            "high_severity_suggestions": high_severity_suggestions,
            "new_gaps": new_gaps,
            "open_gaps": open_gaps,
            "new_patterns": new_patterns,
            "top_patterns": top_patterns,
            "new_intents": new_intents,
            "high_conf_intents": high_conf_intents,
            "new_skills": new_skills,
        })

    except Exception as exc:
        cycle["cycle_outcome"] = "failed"
        cycle["error_detail"] = str(exc)[:500]
        conn.execute(
            "UPDATE watch_history SET finished_at=?, outcome=?, "
            "error_detail=? WHERE id=?",
            (now_iso(), "failed", cycle["error_detail"], history_id))
        conn.commit()
    finally:
        conn.close()
        os.close(lock_fd)
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass

    return cycle


# ---------------------------------------------------------------------------
# Filesystem polling
# ---------------------------------------------------------------------------


def _poll_repos() -> list[str]:
    """Check ingested repos for filesystem changes.

    Uses the same signature-comparison logic as observe.refresh():
    compares (head_sha, is_dirty, readme_hash, manifest_hash) from the
    last snapshot against the live disk state.

    Returns a list of repo paths that changed.
    """
    changed: list[str] = []
    try:
        conn = connect()
        from .db import get_repositories
        from .observe import _last_snapshot_signature, _repo_signature

        baseline = _last_snapshot_signature(conn)
        repos = get_repositories(conn)

        for r in repos:
            path = r.path
            if not path:
                continue
            p = Path(path)
            if not p.exists():
                continue
            rid = r.id
            current = _repo_signature(conn, rid, path)
            prior = baseline.get(path)
            if prior is None or prior != current:
                changed.append(path)

        conn.close()
    except Exception as exc:
        _log(f"Repo polling error: {exc}")

    return changed


def _last_cycle_duration() -> Optional[float]:
    """Return the duration of the most recent watch_history cycle in seconds."""
    try:
        conn = connect()
        row = conn.execute(
            "SELECT started_at, finished_at FROM watch_history "
            "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row["started_at"] and row["finished_at"]:
            from datetime import datetime
            s = datetime.fromisoformat(row["started_at"])
            f = datetime.fromisoformat(row["finished_at"])
            return (f - s).total_seconds()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Daemon main loop
# ---------------------------------------------------------------------------


def run_daemon(interval_seconds: int = 900, no_notify: bool = False) -> None:
    """Main daemon loop. Called from the child process after fork.

    Args:
        interval_seconds: Time between scheduled cycles (default 15 min).
        no_notify: If True, suppress desktop notifications.
    """
    _log(f"Daemon started (PID {os.getpid()}, interval={interval_seconds}s).")

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGHUP, _handle_sighup)

    # Register the handler reference on the module so signal handlers can see it.
    global _daemon_shutdown, _daemon_cycle_now
    _daemon_shutdown = False
    _daemon_cycle_now = False

    # Read operator profile for notification preference.
    try:
        from .operator.engine import should_notify as _profile_should_notify
        conn = connect()
        _effective_no_notify = no_notify or not _profile_should_notify(conn)
        conn.close()
    except Exception:
        _effective_no_notify = no_notify

    cycle_count = 0
    write_status(
        state="running",
        pid=os.getpid(),
        started_at=now_iso(),
        last_cycle_at=None,
        last_cycle_outcome=None,
        cycle_count=0,
        interval_seconds=interval_seconds,
        watched_repos=0,
    )

    # Track repo signatures for filesystem polling.
    poll_interval = 60  # seconds between filesystem polls
    last_poll = 0.0

    # Run first cycle immediately.
    _do_cycle(cycle_count, _effective_no_notify)
    cycle_count += 1

    while not _daemon_shutdown:
        now = time.monotonic()

        # Poll for filesystem changes if enough time has passed.
        if now - last_poll >= poll_interval:
            last_poll = now
            changed = _poll_repos()
            if changed:
                _log(f"Filesystem change detected in {len(changed)} repo(s). "
                     f"Triggering immediate cycle.")
                _do_cycle(cycle_count, _effective_no_notify)
                cycle_count += 1
                # Reset the timer so we don't double-run immediately.
                last_poll = time.monotonic()

        # Check for SIGHUP-triggered cycle.
        if _daemon_cycle_now:
            _daemon_cycle_now = False
            _log("SIGHUP triggered immediate cycle.")
            _do_cycle(cycle_count, _effective_no_notify)
            cycle_count += 1
            last_poll = time.monotonic()

        # Sleep in 1-second increments so we can respond to signals.
        for _ in range(interval_seconds):
            if _daemon_shutdown or _daemon_cycle_now:
                break
            time.sleep(1)

        # Scheduled cycle (only if not already triggered by poll/SIGHUP above).
        if not _daemon_shutdown and not _daemon_cycle_now:
            _do_cycle(cycle_count, _effective_no_notify)
            cycle_count += 1

    # Graceful shutdown.
    write_status(state="stopped")
    _log("Daemon stopped gracefully.")
    _remove_pid()


def _do_cycle(cycle_num: int, no_notify: bool) -> None:
    """Run one observation cycle and update status.

    Notifications are gated by both the --no-notify CLI flag AND the operator
    profile's should_notify() preference. If the operator has set
    'no_notifications=true' via 'friday profile set', notifications are
    suppressed even when --no-notify is not passed.
    """
    _log(f"Cycle #{cycle_num + 1} starting...")

    # Refresh notification gate from profile — operator may have changed
    # preference since daemon start.
    effective_no_notify = no_notify
    try:
        from .operator.engine import should_notify
        conn = connect()
        if not should_notify(conn):
            effective_no_notify = True
        conn.close()
    except Exception:
        pass

    cycle = _run_cycle()
    duration = _last_cycle_duration()
    if duration is not None:
        _log(f"Cycle #{cycle_num + 1} took {duration:.1f}s.")

    outcome = cycle.pop("cycle_outcome", "failed")
    error_detail = cycle.pop("error_detail", None)

    status_updates = {
        "last_cycle_at": now_iso(),
        "last_cycle_outcome": outcome,
        "cycle_count": cycle_num + 1,
        "last_error": error_detail,
    }
    # Copy watch-history-like fields into status.
    for key in ("repos_scanned", "repos_changed", "knowledge_updated",
                "new_pending_initiatives"):
        if key in cycle:
            mapped = "watched_repos" if key == "repos_scanned" else key
            status_updates[mapped] = cycle[key]

    # Copy Phase A ambient analysis fields into status so `friday daemon
    # status` surfaces suggestions and gaps without re-querying.
    for key in _PHASE_A_FIELDS:
        if key in cycle:
            status_updates[key] = cycle[key]

    write_status(**status_updates)

    if outcome == "failed":
        _log(f"Cycle #{cycle_num + 1} FAILED: {error_detail}")
        if not effective_no_notify:
            _notify("Friday — Cycle Failed", f"Observation cycle #{cycle_num + 1} failed: {error_detail[:200]}")
    elif outcome == "skipped":
        _log(f"Cycle #{cycle_num + 1} skipped (lock held).")
    else:
        changed = cycle.get("repos_changed", 0)
        scanned = cycle.get("repos_scanned", 0)
        knowledge = cycle.get("knowledge_updated", 0)
        pending = cycle.get("new_pending_initiatives", 0)
        new_suggestions = cycle.get("new_suggestions", 0)
        high_sev_sug = cycle.get("high_severity_suggestions", 0)
        new_gaps = cycle.get("new_gaps", 0)
        open_gaps = cycle.get("open_gaps", 0)
        new_patterns = cycle.get("new_patterns", 0)
        top_patterns = cycle.get("top_patterns", 0)
        new_intents = cycle.get("new_intents", 0)
        high_conf_intents = cycle.get("high_conf_intents", 0)

        _log(f"Cycle #{cycle_num + 1} complete: {changed}/{scanned} repos changed, "
             f"{knowledge} knowledge updates, {pending} new initiatives, "
             f"{new_suggestions} suggestions, {new_gaps} new gaps, "
             f"{new_patterns} patterns, {new_intents} intents.")

        # Build notification parts for ambient findings.
        notify_parts = []
        if changed:
            notify_parts.append(f"{changed}/{scanned} repo(s) changed")
        if knowledge:
            notify_parts.append(f"{knowledge} knowledge updates")
        if pending:
            notify_parts.append(f"{pending} new initiative(s)")
        if high_sev_sug:
            notify_parts.append(f"{high_sev_sug} high-severity integration suggestion(s)")
        elif new_suggestions:
            notify_parts.append(f"{new_suggestions} integration suggestion(s)")
        if new_gaps:
            notify_parts.append(f"{new_gaps} new capability gap(s) detected")
        elif open_gaps:
            notify_parts.append(f"{open_gaps} open gap(s) pending")
        if high_conf_intents:
            notify_parts.append(f"{high_conf_intents} workflow(s) recognized")
        elif new_intents:
            notify_parts.append(f"{new_intents} workflow(s) labeled")
        elif top_patterns:
            notify_parts.append(f"{top_patterns} frequent action pattern(s)")
        elif new_patterns:
            notify_parts.append(f"{new_patterns} action pattern(s) mined")
        new_skills = cycle.get("new_skills", 0)
        if new_skills:
            notify_parts.append(f"{new_skills} new skill(s) formed")

        if notify_parts and not effective_no_notify:
            _notify(
                "Friday — Workspace Update",
                ". ".join(notify_parts) + ".",
            )

        # Log high-severity suggestions in detail so they're searchable.
        if new_suggestions:
            _log(f"  {new_suggestions} cross-project suggestion(s) available "
                 f"(run `friday suggest` to view)")
        if new_gaps:
            _log(f"  {new_gaps} new capability gap(s) detected "
                 f"(run `friday meta analyze` to view details)")
        if new_patterns:
            _log(f"  {new_patterns} action pattern(s) mined "
                 f"(run `friday patterns` to view)")
        if new_intents:
            _log(f"  {new_intents} workflow intent(s) labeled "
                 f"(run `friday patterns label` to view)")
        if new_skills:
            _log(f"  {new_skills} skill(s) formed from workflow intents "
                 f"(run `friday patterns form` to review)")


# ---------------------------------------------------------------------------
# Signal handlers (module-level globals for communication with the loop)
# ---------------------------------------------------------------------------

_daemon_shutdown = False
_daemon_cycle_now = False


def _handle_sigterm(signum, frame) -> None:
    global _daemon_shutdown
    _daemon_shutdown = True
    _log("Received SIGTERM, shutting down gracefully.")


def _handle_sighup(signum, frame) -> None:
    global _daemon_cycle_now
    _daemon_cycle_now = True
    _log("Received SIGHUP, scheduling immediate cycle.")


# ---------------------------------------------------------------------------
# Daemon lifecycle commands (called from cli_daemon.py)
# ---------------------------------------------------------------------------


def is_running() -> bool:
    """Check if the daemon is currently running."""
    pid = _read_pid()
    if pid is None:
        return False
    return _is_pid_running(pid)


def get_status() -> dict:
    """Return the current daemon status dict."""
    return _read_status()


def start(interval_seconds: int = 900, no_notify: bool = False) -> int:
    """Start the daemon as a background process.

    Returns 0 on success, 1 if already running, 2 on fork failure.
    """
    if is_running():
        pid = _read_pid()
        print(f"Daemon already running (PID {pid}).", file=sys.stderr)
        print("Use 'friday daemon restart' or 'friday daemon stop' first.")
        return 1

    # Fork the daemon process.
    try:
        pid = os.fork()
    except OSError as exc:
        print(f"Fork failed: {exc}", file=sys.stderr)
        return 2

    if pid > 0:
        # Parent process: write PID and return.
        _write_pid(pid)
        print(f"Daemon started (PID {pid}).")
        print(f"Log: {LOG_FILE}")
        print(f"Status: {STATUS_FILE}")
        return 0

    # Child process: become the daemon.
    # Detach from parent's stdio.
    try:
        sys.stdin.close()
        sys.stdout.flush()
        sys.stderr.flush()
        # Redirect stdio to /dev/null.
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
    except Exception:
        pass

    # Ensure PID file exists for our new PID (parent may have been killed).
    _write_pid(os.getpid())

    try:
        run_daemon(interval_seconds=interval_seconds, no_notify=no_notify)
    except Exception as exc:
        _log(f"Daemon crashed: {exc}")
        write_status(state="crashed", last_error=str(exc)[:500])
    finally:
        _remove_pid()

    # Exit the child process.
    os._exit(0)


def stop() -> int:
    """Stop the daemon gracefully via SIGTERM.

    Returns 0 on success, 1 if not running, 2 if signal failed.
    """
    pid = _read_pid()
    if pid is None:
        print("Daemon is not running.", file=sys.stderr)
        return 1

    if not _is_pid_running(pid):
        print(f"Daemon PID {pid} is not running. Cleaning up PID file.")
        _remove_pid()
        write_status(state="stopped")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"Failed to send SIGTERM to PID {pid}: {exc}", file=sys.stderr)
        return 2

    # Wait for the daemon to exit.
    for _ in range(30):
        if not _is_pid_running(pid):
            break
        time.sleep(0.5)

    if _is_pid_running(pid):
        print(f"Daemon PID {pid} did not exit after 15s. Forcing with SIGKILL.")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    _remove_pid()
    write_status(state="stopped")
    print("Daemon stopped.")
    return 0


def restart(interval_seconds: int = 900, no_notify: bool = False) -> int:
    """Restart the daemon."""
    stop()
    return start(interval_seconds=interval_seconds, no_notify=no_notify)


def logs(tail: int = 50) -> Optional[list[str]]:
    """Return the last N lines of the daemon log."""
    try:
        lines = LOG_FILE.read_text().splitlines()
        return lines[-tail:]
    except (OSError, FileNotFoundError):
        return None
