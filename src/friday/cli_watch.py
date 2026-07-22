"""friday watch — ambient workspace observation loop.

Installs as a systemd user timer. Each cycle runs the existing deterministic
pipeline (refresh → knowledge → understanding → initiative → insight), writes
outcomes to watch_history, and surfaces high-confidence initiatives to
pending_initiatives for human review.

Design choice: systemd user timers over cron.
  - CachyOS/Hyprland daily driver: systemd is native, no mailspool, integrated
    journald logging (journalctl --user -u friday-watch), proper dependency
    ordering, and user-level without root.
  - `systemctl --user` is reliable on all modern Linux distributions.
  - Cron would require MAILTO setup and has no native "do not overlap" — the
    lock file would need to be added anyway.
  - systemd's OnCalendar also handles skipped cycles when the machine is off
    (no catch-up firestorm on resume).

Non-negotiables carried forward:
  - Fail loud: every cycle outcome (succeeded/skipped/failed + why) is written
    to watch_history and visible via `friday doctor`.
  - Never destructive: read-only observation + analysis only. No file writes
    to the user's project, no auto-execution of task graphs.
  - Safe to interrupt: every layer's build() is idempotent and wrapped in
    atomic transactions. An interrupted cycle picks up cleanly next time.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .db import connect, now_iso


FRIDAY_DIR = Path.home() / ".friday"
SERVICE_NAME = "friday-watch"
SERVICE_UNIT = (
    "[Unit]\n"
    "Description=Friday ambient watch cycle\n"
    "\n"
    "[Service]\n"
    "Type=oneshot\n"
    "ExecStart={EXEC} watch --run-once\n"
    "Environment=FRIDAY_WATCH_INTERVAL={INTERVAL}\n"
    "LockPersonality=yes\n"
    "PrivateTmp=yes\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target\n"
)
TIMER_UNIT = (
    "[Unit]\n"
    "Description=Friday ambient watch timer\n"
    "\n"
    "[Timer]\n"
    "OnCalendar={ON_CALENDAR}\n"
    "Persistent=true\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target\n"
)

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LOCKFILE = Path("/tmp") / ".friday-watch.lock"


def _default_interval_seconds() -> int:
    raw = os.environ.get("FRIDAY_WATCH_INTERVAL", "")
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 2700  # 45 min


def _interval_to_oncalendar(interval_sec: int) -> str:
    """Map interval seconds to a systemd OnCalendar expression.

    Uses *-*-* *:0/N syntax for repeating minute intervals. Sub-minute
    not supported by OnCalendar. The FRIDAY_WATCH_INTERVAL env var in
    the service unit provides the actual interval hint; the timer cadence
    is the minimum polling frequency.
    """
    mins = max(1, interval_sec // 60)
    return f"*-*-* *:0/{mins}"


def _watch_dir() -> Path:
    p = FRIDAY_DIR / "watch"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------


def _install(args: argparse.Namespace) -> int:
    """Install systemd user service + timer."""
    interval_sec = _default_interval_seconds()
    on_cal = _interval_to_oncalendar(interval_sec)

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    exe = sys.executable or "python3"
    svc = SERVICE_UNIT.replace("{INTERVAL}", str(interval_sec)).replace(
        "{EXEC}", f"{exe} -m friday.cli")
    (SYSTEMD_USER_DIR / f"{SERVICE_NAME}.service").write_text(svc)

    tmr = TIMER_UNIT.replace("{ON_CALENDAR}", on_cal)
    (SYSTEMD_USER_DIR / f"{SERVICE_NAME}.timer").write_text(tmr)

    # Reload, enable, start.
    _quiet("systemctl", "--user", "daemon-reload")
    _quiet("systemctl", "--user", "enable", f"{SERVICE_NAME}.timer")
    _quiet("systemctl", "--user", "start", f"{SERVICE_NAME}.timer")
    print(f"friday-watch installed (interval ~{interval_sec // 60}m, "
          f"OnCalendar={on_cal})")
    print("Check status: friday watch --status")
    print("Logs: journalctl --user -u friday-watch -f")
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    """Stop and remove systemd service + timer."""
    _quiet("systemctl", "--user", "stop", f"{SERVICE_NAME}.timer", exit_ok=True)
    _quiet("systemctl", "--user", "disable", f"{SERVICE_NAME}.timer", exit_ok=True)
    for f in (f"{SERVICE_NAME}.service", f"{SERVICE_NAME}.timer"):
        p = SYSTEMD_USER_DIR / f
        if p.exists():
            p.unlink()
    _quiet("systemctl", "--user", "daemon-reload")
    print("friday-watch uninstalled.")
    return 0


def _status(args: argparse.Namespace) -> int:
    """Show watch loop status: timer enabled/active, recent cycles."""
    import subprocess

    conn = connect()

    # Timer enabled?
    enabled = _check_output(
        "systemctl", "--user", "is-enabled", f"{SERVICE_NAME}.timer")
    active = _check_output(
        "systemctl", "--user", "is-active", f"{SERVICE_NAME}.timer")
    last_trigger = _check_output(
        "systemctl", "--user", "show", f"{SERVICE_NAME}.timer",
        "--property", "LastTriggerUSec") if active == "active" else ""

    print("Watch loop status\n")
    if enabled == "enabled":
        print(f"Timer enabled:        yes")
        print(f"Timer active:         {active}")
        if last_trigger and "=" in last_trigger:
            print(f"Last trigger:         {last_trigger.rsplit('=', 1)[-1]}")
    else:
        print("Not installed.")
        print("Run: friday watch --install\n")

    # Recent cycles from DB.
    rows = conn.execute(
        "SELECT id, started_at, finished_at, outcome, repos_scanned, "
        "repos_changed, error_detail "
        "FROM watch_history ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()

    if rows:
        print()
        print("Recent cycles:\n")
        for r in rows:
            dur = ""
            if r["finished_at"] and r["started_at"]:
                try:
                    from datetime import datetime
                    s = datetime.fromisoformat(r["started_at"])
                    f = datetime.fromisoformat(r["finished_at"])
                    dur = f" ({(f - s).total_seconds():.0f}s)"
                except (ValueError, TypeError):
                    pass
            mark = {"succeeded": "✓", "skipped": "~", "running": "…",
                    "failed": "✗"}.get(r["outcome"], "?")
            print(f"  [{mark}] #{r['id']} {r['outcome']}{dur}")
            print(f"         scanned={r['repos_scanned']} "
                  f"changed={r['repos_changed']}")
            if r["error_detail"] and r["outcome"] == "failed":
                print(f"         error: {r['error_detail'][:120]}")
        print()

    return 0


# ---------------------------------------------------------------------------
# Run once (called by timer, or manually via --run-once)
# ---------------------------------------------------------------------------


def _run_once(args: argparse.Namespace) -> int:
    """Run a single watch cycle.

    Safe to call concurrently (lock file). Writes outcome to watch_history.
    """
    # Acquire lock — fail fast if another cycle is running.
    try:
        lock_fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        print("Watch cycle already running (lockfile /tmp/.friday-watch.lock).",
              file=sys.stderr)
        return 1

    conn = connect()
    started = now_iso()
    cur = conn.execute(
        "INSERT INTO watch_history (started_at, outcome) VALUES (?, 'running')",
        (started,))
    history_id = cur.lastrowid
    conn.commit()

    outcome = "succeeded"
    error_detail = None
    try:
        try:
            # The full pipeline: refresh → knowledge → understanding →
            # initiative → insight. refresh() already composes all layers.
            from .observe import refresh
            rep = refresh(conn)

            # Check for high-confidence initiatives worth surfacing.
            new_pending = _harvest_initiatives(conn, history_id)

            # Write outcome.
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

            if not args.quiet:
                print(f"[friday-watch] Cycle #{history_id} complete. "
                      f"{rep.repos_changed}/{rep.repos_scanned} repos changed, "
                      f"{new_pending} new pending initiatives.")
        except Exception as exc:
            outcome = "failed"
            error_detail = str(exc)
            conn.execute(
                "UPDATE watch_history SET finished_at=?, outcome=?, "
                "error_detail=? WHERE id=?",
                (now_iso(), "failed", error_detail[:1000], history_id))
            conn.commit()
            if not args.quiet:
                print(f"[friday-watch] Cycle #{history_id} FAILED: {exc}",
                      file=sys.stderr)
            return 1
    finally:
        conn.close()
        os.close(lock_fd)
        try:
            LOCKFILE.unlink()
        except OSError:
            pass

    return 0


def _harvest_initiatives(conn, watch_run_id: int) -> int:
    """Harvest high-confidence initiatives into pending_initiatives.

    Every cycle, re-synthesize statements for ALL high-confidence initiatives
    (not just new ones) so pending entries stay in sync when evidence changes.
    Reviewed or dismissed entries are skipped; their statement is frozen.

    Returns count of initiatives touched.
    """
    rows = conn.execute(
        "SELECT id, title, statement, initiative_type, confidence, "
        "understanding_ids, knowledge_ids "
        "FROM initiatives WHERE confidence IN ('medium', 'strong')"
    ).fetchall()

    if not rows:
        return 0

    # Pre-fetch pending state to skip reviewed/dismissed.
    pending_state = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, reviewed, dismissed_at FROM pending_initiatives"
        ).fetchall()
    }

    count = 0
    for r in rows:
        pid = r["id"]
        pstate = pending_state.get(pid)

        # Skip reviewed/dismissed entries — their statement is frozen.
        if pstate is not None and (pstate["reviewed"] or pstate["dismissed_at"] is not None):
            continue

        # Synthesize statement from actual evidence (fixes template filler).
        statement = _synthesize_initiative_statement(
            conn, pid, r["title"], r["initiative_type"],
            r["understanding_ids"], r["knowledge_ids"])
        try:
            conn.execute(
                "INSERT INTO pending_initiatives "
                "(id, title, statement, initiative_type, confidence, "
                "understanding_ids, knowledge_ids, detected_at, watch_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "statement=excluded.statement, "
                "understanding_ids=excluded.understanding_ids, "
                "knowledge_ids=excluded.knowledge_ids",
                (pid, r["title"], statement, r["initiative_type"],
                 r["confidence"], r["understanding_ids"] or "",
                 r["knowledge_ids"] or "",
                 now_iso(), watch_run_id))
            # Also backfill the initiatives table with the real statement.
            conn.execute(
                "UPDATE initiatives SET statement=? WHERE id=? AND statement != ?",
                (statement, pid, statement))
            count += 1
            conn.commit()
        except Exception:
            pass
    return count


def _synthesize_initiative_statement(conn, iid, title, itype, und_ids, k_ids) -> str:
    """Synthesize a meaningful statement from actual evidence.

    Replaces template filler with evidence-grounded synthesis by fetching
    the actual understanding/knowledge statements and extracting key concepts.

    NOTE: keep in sync with InitiativeEngine._synthesize_statement() in
    engine.py (same logic, different layer to avoid circular imports).
    """
    if not und_ids and not k_ids:
        return f"{title}: a {itype} effort indicated by 0 understanding(s) and 0 knowledge."

    # Fetch understanding and knowledge records
    und_ids_list = [u.strip() for u in (und_ids or "").split(",") if u.strip()]
    k_ids_list = [k.strip() for k in (k_ids or "").split(",") if k.strip()]

    statements = []
    for uid in und_ids_list:
        u = conn.execute(
            "SELECT statement FROM understanding WHERE id = ?", (uid,)
        ).fetchone()
        if u and u["statement"]:
            statements.append(u["statement"])

    for kid in k_ids_list:
        k = conn.execute(
            "SELECT statement FROM knowledge WHERE id = ?", (kid,)
        ).fetchone()
        if k and k["statement"]:
            statements.append(k["statement"])

    if not statements:
        return f"{title}: a {itype} effort indicated by {len(und_ids_list)} understanding(s) and {len(k_ids_list)} knowledge."

    # Deduplicate.
    unique = list(dict.fromkeys(statements))

    # For small evidence sets (≤5 statements), join raw evidence directly.
    # Concept extraction is a lossy compression that strips the subject (tech
    # name) from each statement, making all maintenance-type initiatives with
    # identical understanding structures produce identical concept output.
    if len(unique) <= 5:
        cleaned = [s.strip().rstrip(". ") for s in unique]
        stmt = ", ".join(cleaned)
        return f"{title}: {stmt}"

    # Extract key concepts from statements
    concepts = _extract_concepts_from_statements(statements)

    if len(concepts) == 0:
        cleaned = [s.strip().rstrip(". ") for s in statements[:3]]
        stmt = " and ".join(cleaned)
    elif len(concepts) == 1:
        stmt = concepts[0]
    else:
        if len(concepts) <= 3:
            stmt = " and ".join(concepts)
        else:
            stmt = ", ".join(concepts[:3]) + f" (and {len(concepts)-3} more aspects)"

    return f"{title}: {stmt}"


def _extract_concepts_from_statements(statements: list) -> list:
    """Extract key concepts from statements for synthesis."""
    concepts = []
    seen = set()

    # NOTE: keep in sync with engine.py _extract_concepts concept_keywords list.
    concept_keywords = [
        ("architecture", "architectural evolution"),
        ("stabilizing", "stabilizing architecture"),
        ("purpose", "purpose evolution"),
        ("fit", "integration fit"),
        ("integrate", "integration opportunity"),
        ("platform", "platform convergence"),
        ("frontend", "frontend experience"),
        ("authentication", "authentication infrastructure"),
        ("auth", "authentication infrastructure"),
        ("session", "session management"),
        ("jwt", "JWT handling"),
        ("credential", "credential management"),
        ("oauth", "OAuth integration"),
        ("router", "AI routing"),
        ("llm", "LLM integration"),
        ("agent", "agent coordination"),
        ("knowledge", "knowledge evolution"),
        ("memory", "memory systems"),
        ("rust", "systems infrastructure"),
        ("kernel", "kernel development"),
        ("filesystem", "filesystem operations"),
        ("runtime", "runtime optimization"),
        ("compiler", "compiler development"),
        ("migration", "technology migration"),
        ("documentation", "documentation"),
        ("test", "test coverage"),
        ("ci/cd", "CI/CD pipeline"),
        ("docker", "container deployment"),
        ("database", "data layer"),
        ("api", "API design"),
        ("backend", "backend services"),
        ("project", "project evolution"),
        ("project convergence", "project convergence"),
        ("project divergence", "project divergence"),
        ("weakness", "emerging weakness"),
        ("direction", "technology direction"),
        ("engineering identity", "engineering identity"),
        ("converging", "converging efforts"),
        ("diverging", "diverging direction"),
        ("recurring", "recurring patterns"),
        ("blind spot", "blind spot detected"),
        ("risk", "engineering risk"),
    ]

    for stmt in statements:
        stmt_lower = stmt.lower()
        found = False
        for keyword, concept in concept_keywords:
            if keyword in stmt_lower and concept not in seen:
                concepts.append(concept)
                seen.add(concept)
                found = True
                break
        if not found:
            cleaned = stmt.strip().rstrip(". ")
            for prefix in ["a ", "the ", "an "]:
                if cleaned.lower().startswith(prefix):
                    cleaned = cleaned[len(prefix):]
            words = cleaned.split()
            if len(words) >= 3 and cleaned not in seen:
                concepts.append(cleaned)
                seen.add(cleaned)

    return concepts


# ---------------------------------------------------------------------------
# Run foreground (interactive)
# ---------------------------------------------------------------------------


def cmd_watch(args: argparse.Namespace) -> int:
    """Dispatch friday watch subcommands."""
    if args.install:
        return _install(args)
    if args.uninstall:
        return _uninstall(args)
    if args.status:
        return _status(args)
    if args.run_once:
        return _run_once(args)
    # Default: run one cycle in foreground.
    args.quiet = False  # always show output in interactive mode
    return _run_once(args)


def _quiet(*cmd: str, exit_ok: bool = False) -> str:
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not exit_ok:
        print(f"error: {' '.join(cmd)}: {r.stderr.strip()}", file=sys.stderr)
    return r.stdout.strip()


def _check_output(*cmd: str) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""
