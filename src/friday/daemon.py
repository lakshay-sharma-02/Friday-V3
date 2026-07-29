"""Friday Daemon — persistent ambient observation loop.

Transforms Friday from a CLI-invoked tool into an always-on operating partner.
The daemon runs as a background process, periodically observing the workspace,
reacting to filesystem changes, and proactively surfacing insights.

Design:
- Classic PID-file daemon pattern (no systemd dependency)
- Polling-based filesystem watcher (zero new dependencies)
- Desktop notifications via notify-send / osascript
- Reuses the existing refresh() pipeline from observe.py
- Each cycle outcome is written to watch_history (same table as ``friday watch``)
- SIGTERM = graceful shutdown, SIGHUP = trigger immediate cycle

Architecture (post-refactor):
  ``CycleContext`` — typed dataclass replacing the mutable ``cycle`` dict.
  ``_run_cycle()`` — orchestrator that delegates to focused stage functions.
  ``_service_identity_poll()`` — single parameterized polling loop used by
     Telegram, Slack, and Discord (kills 3× copy-paste).
  ``_CYCLE_LOCK`` — in-process ``threading.Lock`` replaces filesystem mutex
     as the primary cycle serialisation mechanism.
  Maintenance stages ALWAYS run even when the main pipeline fails, preventing
     DB bloat from compounding failures.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


from .db import connect, now_iso


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FRIDAY_DIR = Path.home() / ".friday"
PID_FILE = FRIDAY_DIR / "daemon.pid"
STATUS_FILE = FRIDAY_DIR / "daemon.status"
LOG_FILE = FRIDAY_DIR / "daemon.log"

# Legacy lock file — kept for backward compat (tests reference it).
# The primary serialization mechanism is now ``_CYCLE_LOCK`` (in-process
# threading.Lock) below. This file is still created during a cycle so that
# external ``friday watch`` commands see it and know a daemon cycle is running,
# but it is no longer the sole arbiter of cycle concurrency.
LOCK_FILE = Path("/tmp") / ".friday-watch.lock"

WATCH_HISTORY_KEYS = (
    "repos_scanned", "repos_changed", "knowledge_updated",
    "understanding_updated", "initiatives_changed", "insights_changed",
)

# Phase A fields persisted in daemon.status so ``friday daemon status`` and
# notifications can surface ambient analysis results without re-querying.
_PHASE_A_FIELDS = ("new_suggestions", "high_severity_suggestions",
                   "new_gaps", "open_gaps",
                   "new_patterns", "top_patterns",
                   "new_intents", "high_conf_intents",
                   "new_skills", "new_correlations",
                   "kill_switch_active",
                   "drifted_skills")


# ---------------------------------------------------------------------------
# Cycle optimisation — interval tuning & cache
# ---------------------------------------------------------------------------

# Fast cycles run at this interval (seconds). They check git + observations
# and skip expensive analysis if nothing changed. Fast cycles complete in
# < 2s when idle.
_FAST_CHECK_INTERVAL = 60

# A full deep analysis cycle runs every N fast cycles (or immediately when
# change is detected). Full cycles run all stages: refresh, observation,
# sequence mining, cross-project correlation, LLM intent labeling, etc.
_FULL_CHECK_EVERY_N = 5  # every 5th fast cycle does a deep pass

# In-process lock for serialising concurrent _run_cycle() calls.
# Replaces the filesystem-based LOCK_FILE as the primary mechanism.
_CYCLE_LOCK = threading.Lock()

# ── Signal globals (module-level, mutated from signal handler thread) ──────
_daemon_shutdown = False
_daemon_cycle_now = False


# ---------------------------------------------------------------------------
# CycleContext — typed cycle state (replaces mutable cycle dict + _CYCLE_CACHE)
# ---------------------------------------------------------------------------


@dataclass
class CycleContext:
    """Typed, immutable-by-convention context for a single daemon cycle.

    Carries all inputs AND outputs of a cycle through the pipeline stages.
    Replaces the mutable ``cycle`` dict (which grew 25+ untyped keys) and
    the module-level ``_CYCLE_CACHE`` dict (which had no thread-safety
    contract).

    Each stage reads from ``ctx`` and writes back through explicit fields.
    The final ``to_result_dict()`` method produces the backward-compatible
    dict that callers (``_do_cycle``, ``notify_cycle_events``) expect.
    """

    # ── Identity ───────────────────────────────────────────────────
    history_id: int = 0
    started_at: str = ""
    cycle_type: str = "full"  # "full" | "fast"

    # ── Input (from cycle-need assessment) ─────────────────────────
    repos_scanned: int = 0
    repos_changed: int = 0
    changed_repo_names: list[str] = field(default_factory=list)

    # ── Outputs (populated by pipeline stages) ─────────────────────
    new_suggestions: int = 0
    high_severity_suggestions: int = 0
    new_gaps: int = 0
    open_gaps: int = 0
    new_patterns: int = 0
    top_patterns: int = 0
    new_intents: int = 0
    high_conf_intents: int = 0
    new_skills: int = 0
    new_correlations: int = 0
    auto_dispatched: int = 0
    drifted_skills: int = 0
    obs_count: int = 0
    actions_derived: int = 0
    knowledge_updated: int = 0
    understanding_updated: int = 0
    initiatives_changed: int = 0
    insights_changed: int = 0
    new_pending_initiatives: int = 0
    pruned: int = 0
    conv_learned: int = 0

    # ── Execution pipeline (M9.2-M9.5) ───────────────────────────
    graphs_resolved: int = 0
    graphs_scheduled: int = 0
    graphs_executed: int = 0
    sessions_executed: int = 0

    # ── Watcher checks ────────────────────────────────────────────────
    watchers_checked: int = 0
    watchers_fired: int = 0

    # ── Worker proposals auto-approved ────────────────────────────
    worker_proposals_approved: int = 0

    # ── Error state (populated on failure) ─────────────────────────
    cycle_outcome: str = "succeeded"
    error_detail: Optional[str] = None
    error_type: Optional[str] = None
    error_action: Optional[str] = None
    error_target: Optional[str] = None
    recovery_hint: Optional[str] = None
    error_eta: Optional[str] = None

    # ── Stage-failure tracking ─────────────────────────────────────
    # Tracks individual stage failures so the cycle runner knows what
    # succeeded and what didn't, without aborting the entire pipeline.
    stage_failures: dict[str, str] = field(default_factory=dict)  # stage_name -> error

    def record_stage_failure(self, stage: str, error: str) -> None:
        """Record a stage failure without aborting the cycle."""
        self.stage_failures[stage] = error[:200]

    @property
    def is_full(self) -> bool:
        return self.cycle_type == "full"

    @property
    def succeeded(self) -> bool:
        return self.cycle_outcome == "succeeded"

    def to_result_dict(self) -> dict[str, Any]:
        """Build backward-compatible result dict for existing callers."""
        d: dict[str, Any] = {
            "cycle_outcome": self.cycle_outcome,
            "cycle_type": self.cycle_type,
            "repos_scanned": self.repos_scanned,
            "repos_changed": self.repos_changed,
            "knowledge_updated": self.knowledge_updated,
            "understanding_updated": self.understanding_updated,
            "new_pending_initiatives": self.new_pending_initiatives,
            "new_suggestions": self.new_suggestions,
            "high_severity_suggestions": self.high_severity_suggestions,
            "new_gaps": self.new_gaps,
            "open_gaps": self.open_gaps,
            "new_patterns": self.new_patterns,
            "top_patterns": self.top_patterns,
            "new_intents": self.new_intents,
            "high_conf_intents": self.high_conf_intents,
            "new_skills": self.new_skills,
            "new_correlations": self.new_correlations,
            "auto_dispatched": self.auto_dispatched,
            "drifted_skills": self.drifted_skills,
            "obs_count": self.obs_count,
            "actions_derived": self.actions_derived,
            "pruned": self.pruned,
            "graphs_resolved": self.graphs_resolved,
            "graphs_scheduled": self.graphs_scheduled,
            "graphs_executed": self.graphs_executed,
            "sessions_executed": self.sessions_executed,
            "watchers_checked": self.watchers_checked,
            "watchers_fired": self.watchers_fired,
            "worker_proposals_approved": self.worker_proposals_approved,
        }
        if self.error_detail:
            d["error_detail"] = self.error_detail
            d["error_type"] = self.error_type
            d["error_action"] = self.error_action
            d["error_target"] = self.error_target
            d["recovery_hint"] = self.recovery_hint
            d["error_eta"] = self.error_eta
        return d


# ---- Cycle-need assessment result (typed) ────────────────────────────────


@dataclass
class _CycleNeed:
    """Result of a fast cycle-need assessment.

    ``has_major_change`` controls whether a full deep analysis runs.
    """
    has_major_change: bool
    changed_repos: list[str] = field(default_factory=list)
    changed_count: int = 0


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
# Cycle-need assessment (fast check, runs every 60s, must complete < 1s)
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


def _check_unprocessed_conversations(conn) -> int:
    """Count conversation_log entries not yet processed by LLM extraction."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_log WHERE processed = 0"
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception:
        return 0


# Module-level cache for cycle-need assessment.
# Kept for backward compat (tests reference it); CycleContext is preferred.
_CYCLE_CACHE: dict[str, Any] = {
    "last_full_cycle_idx": 0,
    "last_repo_signatures": {},
    "last_observation_count": 0,
    "last_repos_changed": 0,
    "last_knowledge_updated": 0,
    "last_unprocessed_convs": 0,
    "fast_cycles_since_full": 0,
}


def _check_cycle_need() -> _CycleNeed:
    """Fast check: detect if anything actually changed since last full cycle.

    Returns a typed ``_CycleNeed`` describing what changed.
    This runs every 60s and must complete in < 1 second. It avoids expensive
    imports (refresh, observation engine) by using lightweight signature checks.

    .. note::

        The ``_CycleNeed`` result replaces the old string-based return
        (``"nothing_changed"`` / ``"major_change"``) with a typed object
        that carries change details, so callers can avoid re-polling in
        the full cycle.
    """
    cache = _CYCLE_CACHE
    try:
        conn = connect()

        # 1. Check repo signatures (fast git poll).
        changed_repos = _poll_repos()
        repos_changed = len(changed_repos)

        # 2. Check observation count (new rows since last check).
        try:
            obs_row = conn.execute("SELECT COUNT(*) AS cnt FROM observations").fetchone()
            obs_count = obs_row["cnt"] if obs_row else 0
        except Exception:
            obs_count = cache.get("last_observation_count", 0)

        # 3. Check unprocessed conversations.
        unprocessed = _check_unprocessed_conversations(conn)

        conn.close()

        # Compare against cached baseline.
        last_obs = cache.get("last_observation_count", 0)
        last_unproc = cache.get("last_unprocessed_convs", 0)

        obs_diff = obs_count - last_obs
        conv_diff = unprocessed - last_unproc

        # Update cache with current values.
        cache["last_repos_changed"] = repos_changed
        cache["last_observation_count"] = obs_count
        cache["last_unprocessed_convs"] = unprocessed

        has_major_change = (
            repos_changed > 0
            or obs_diff > 0
            or conv_diff > 0
        )

        # Force full cycle every N fast cycles even if nothing changed.
        fast_since = cache.get("fast_cycles_since_full", 0)
        if fast_since >= _FULL_CHECK_EVERY_N:
            has_major_change = True

        return _CycleNeed(
            has_major_change=has_major_change,
            changed_repos=changed_repos,
            changed_count=repos_changed,
        )

    except Exception:
        # On any failure, default to full cycle so we don't miss work.
        return _CycleNeed(has_major_change=True)


# ---------------------------------------------------------------------------
# Pipeline stage functions (each is a focused, testable step)
# ---------------------------------------------------------------------------

# ---- Stage: Refresh (git + knowledge pipeline) ---------------------------

def _stage_refresh(ctx: CycleContext, conn) -> None:
    """Run the git refresh and knowledge pipeline."""
    try:
        from .observe import refresh
        from .cli_watch import _harvest_initiatives

        rep = refresh(conn)
        ctx.repos_scanned = rep.repos_scanned
        ctx.repos_changed = rep.repos_changed
        ctx.knowledge_updated = rep.knowledge_updated
        ctx.understanding_updated = rep.understanding_updated
        ctx.initiatives_changed = rep.initiatives_changed
        ctx.insights_changed = rep.insights_changed
        ctx.changed_repo_names = rep.changed_repos or []

        try:
            new_pending = _harvest_initiatives(conn, ctx.history_id)
            ctx.new_pending_initiatives = new_pending
        except Exception as exc:
            _log(f"Initiative harvesting failed: {exc}")

        try:
            from .repair import detect_repair_candidates, propose_repair
            candidates = detect_repair_candidates(conn)
            for c in candidates:
                propose_repair(conn, c)
        except Exception as exc:
            _log(f"Repair detection failed: {exc}")

    except Exception as exc:
        _log(f"Refresh stage failed: {exc}")
        ctx.record_stage_failure("refresh", str(exc))
        raise  # re-raise — caller decides whether to abort or continue


# ---- Stage: Observation Engine + action logging ---------------------------

def _stage_observation(conn) -> dict:
    """Run the ObservationEngine and action logging.

    Returns a dict with obs_count and actions_derived for the cycle context.
    Does NOT take ``ctx`` — failure recording is handled by the caller
    after collecting results, making the signature consistent with all
    other parallel stage functions.
    """
    result: dict = {"obs_count": 0, "actions_derived": 0}
    try:
        from .observation import ObservationEngine, default_registry
        from .db import latest_observations
        from .ambient import push_observations_to_feed
        from .action_log import diff_observations_to_actions, log_action

        prior_obs = [dict(r) for r in latest_observations(conn)]
        engine_run = ObservationEngine(default_registry(), conn).run()
        obs_count = len(engine_run.all_observations)
        result["obs_count"] = obs_count

        if obs_count > 0:
            pushed = push_observations_to_feed(conn, engine_run)
            if pushed:
                _log(f"Observation-to-feed bridge: {pushed} event(s).")

            current_obs = [
                {"source": o.source, "subject": o.subject,
                 "aspect": o.aspect, "value": o.value}
                for o in engine_run.all_observations
            ]
            actions = diff_observations_to_actions(prior_obs, current_obs, now_iso())
            for action in actions:
                log_action(conn, action)
            result["actions_derived"] = len(actions)
            if actions:
                _log(f"ActionLogger: derived {len(actions)} action(s).")
    except Exception as exc:
        _log(f"Observation engine stage failed: {exc}")
    return result


# ---- Parallel pipeline stages (each opens its own connection for SQLite) ---


def _stage_suggest_analysis(conn) -> dict:
    """Run cross-project suggestion analysis. Returns counts dict."""
    result: dict = {"new_suggestions": 0, "high_severity_suggestions": 0}
    try:
        from .cli_suggest import generate_suggestions
        sug_result = generate_suggestions(conn)
        if sug_result.suggestions:
            result["new_suggestions"] = len(sug_result.suggestions)
            result["high_severity_suggestions"] = sum(
                1 for s in sug_result.suggestions if s.severity == "high")
    except Exception as exc:
        _log(f"Suggest analysis failed: {exc}")
    return result


def _stage_gap_analysis(conn) -> dict:
    """Run capability gap analysis. Returns counts dict."""
    result: dict = {"new_gaps": 0, "open_gaps": 0}
    try:
        from .meta.gap_analyzer import analyze
        gap_report = analyze(conn)
        result["new_gaps"] = gap_report.new_gaps
        result["open_gaps"] = gap_report.open_gaps
    except Exception as exc:
        _log(f"Gap analysis failed: {exc}")
    return result


def _stage_correlation(conn) -> dict:
    """Run cross-project correlation. Returns counts dict."""
    result: dict = {"new_correlations": 0}
    try:
        from .cross_project import run_correlation
        cors = run_correlation(conn)
        result["new_correlations"] = len(cors)
        if cors:
            _log(f"Cross-project: {len(cors)} correlation(s) detected.")
    except Exception as exc:
        _log(f"Cross-project correlation failed: {exc}")
    return result


def _stage_sequence_mining(conn) -> dict:
    """Run sequence mining on action events. Returns counts dict."""
    result: dict = {"new_patterns": 0, "top_patterns": 0}
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
        result["new_patterns"] = len(patterns)
        result["top_patterns"] = sum(1 for p in patterns if p.count >= 3)
    except Exception as exc:
        _log(f"Sequence mining failed: {exc}")
    return result


def _stage_conversation_learning(conn) -> dict:
    """Run conversation learning — memory + preference extraction."""
    result: dict = {"conv_learned": 0, "conv_extracted": {}, "conv_memories_stored": 0}
    try:
        from .conversation_learner import process_conversations, extract_memories_from_conversations

        mem_result = extract_memories_from_conversations(conn)
        result["conv_memories_stored"] = mem_result.get("memories_stored", 0)
        if result["conv_memories_stored"]:
            mem_keys = mem_result.get("memories", [])
            _log(f"Memory extraction: stored {result['conv_memories_stored']} fact(s)")

        conv_result = process_conversations(conn)
        result["conv_learned"] = conv_result.get("processed", 0)
        result["conv_extracted"] = conv_result.get("extracted", {})
        if result["conv_learned"]:
            keys = list(result["conv_extracted"].keys())
            _log(f"Conversation learner: processed {result['conv_learned']} exchange(s), "
                 f"extracted {len(keys)} field(s): {', '.join(keys)}")
    except Exception as exc:
        _log(f"Conversation learning failed: {exc}")
    return result


# ---- Sequential pipeline stages (use primary connection) ------------------


def _stage_intent_labeling(ctx: CycleContext, conn) -> dict:
    """Run LLM intent labeling on mined patterns. Returns counts dict.

    Uses batched LLM calls: all patterns are sent in a single request
    instead of N sequential requests, reducing API costs from O(N) to
    O(1). Falls back to individual calls if the batch fails.
    """
    result: dict = {"new_intents": 0, "high_conf_intents": 0}
    if ctx.new_patterns == 0:
        return result
    try:
        from .db import clear_workflow_intents, get_mined_patterns, insert_workflow_intent
        from .intent_labeler import label_intents_batch
        clear_workflow_intents(conn)
        patterns_db = get_mined_patterns(conn)

        # Batch label all patterns in a single LLM call.
        intents = label_intents_batch(patterns_db)

        for p, intent in zip(patterns_db, intents):
            insert_workflow_intent(conn, {
                "pattern_id": p["id"],
                "intent_label": intent.intent_label,
                "intent_description": intent.intent_description,
                "steps_text": json.dumps(intent.steps),
                "confidence": intent.confidence,
                "pattern_summary": json.dumps([[t, tg] for t, tg in intent.pattern_seq]),
                "labeled_at": intent.labeled_at,
            })
            result["new_intents"] += 1
            if intent.confidence in ("high", "medium"):
                result["high_conf_intents"] += 1
        _log(f"Intent labeling: {result['new_intents']} intent(s) labeled "
             f"({result['high_conf_intents']} high/medium confidence).")
    except Exception as exc:
        _log(f"Intent labeling failed: {exc}")
    return result


def _stage_skill_formation(ctx: CycleContext, conn, intent_result: dict) -> dict:
    """Run skill formation on labeled intents. Returns counts dict."""
    result: dict = {"new_skills": 0}
    new_intents = intent_result.get("new_intents", 0)
    if new_intents == 0:
        return result
    try:
        from .skill_formation import form_skills
        formed = form_skills(conn)
        result["new_skills"] = len(formed)
        if formed:
            _log(f"Skill formation: {len(formed)} skill(s) formed from intents.")
    except Exception as exc:
        _log(f"Skill formation failed: {exc}")
    return result


def _stage_auto_dispatch(ctx: CycleContext, conn) -> int:
    """Run auto-dispatch of formed skills. Returns count dispatched."""
    try:
        from .skill_formation import auto_dispatch_skills
        results = auto_dispatch_skills(conn)
        dispatched = len(results)
        if dispatched:
            ok = sum(1 for r in results if r.get("succeeded"))
            _log(f"Auto-dispatch: {dispatched} skill(s) triggered "
                 f"({ok} succeeded).")
        return dispatched
    except Exception as exc:
        _log(f"Auto-dispatch failed: {exc}")
        return 0


def _stage_drift_detection(ctx: CycleContext, conn) -> int:
    """Run drift detection on formed skills. Returns count of drifted skills."""
    try:
        from .skill_formation import detect_skill_drift
        drift_reports = detect_skill_drift(conn)
        if drift_reports:
            unhealthy = sum(1 for r in drift_reports if r.overall_health == "unhealthy")
            degrading = sum(1 for r in drift_reports if r.overall_health == "degrading")
            drifted = unhealthy + degrading
            if drifted:
                _log(f"Drift detection: {unhealthy} unhealthy, {degrading} degrading "
                     f"skill(s) found.")
            return drifted
    except Exception as exc:
        _log(f"Drift detection failed: {exc}")
    return 0


def _stage_worker_auto_approve(ctx: CycleContext, conn) -> int:
    """Auto-approve pending deterministic worker proposals.

    Runs after skill formation so newly-formed skills don't interfere.
    Only auto-approves PATH-based (Tier 1) proposals — LLM-generated
    proposals still require manual review.
    Returns count of proposals that were approved."""
    try:
        from .worker.genesis import auto_approve_proposals
        from .worker.engine import WorkerRegistry

        registry = WorkerRegistry(conn)
        registry.register_builtins()
        approved = auto_approve_proposals(conn, registry)
        if approved:
            _log(f"Worker auto-approve: {approved} proposal(s) approved "
                 f"and registered.")
        return approved
    except Exception as exc:
        _log(f"Worker auto-approve failed: {exc}")
        return 0


# ---- Maintenance stage (ALWAYS runs, even on pipeline failure) ------------


def _stage_mission_progress(ctx: CycleContext, conn) -> None:
    """Advance all active persistent missions by one step each cycle.

    Calls ``MissionEngine.advance_missions()`` which executes the next
    pending step for each active mission and reports progress. Best-effort:
    never fails the cycle — failures in individual mission steps are
    recorded on the mission and trigger adaptive plan revision.

    Runs in the ALWAYS section after spontaneous review.
    """
    try:
        from .mission import stage_mission_progress
        updates = stage_mission_progress(conn)
        for u in updates:
            _log(f"Mission: {u}")
    except Exception as exc:
        _log(f"Mission progress stage failed: {exc}")


def _check_auto_rollback(conn) -> None:
    """Check if the daemon crashed since the last capability deployment and auto-rollback.

    If a capability was deployed but the daemon did NOT exit cleanly afterward,
    this function rolls back the last deployed capability to prevent broken state.
    On success, pushes a notification to the ambient feed.

    Best-effort: never fails or raises.
    """
    try:
        from .meta.capability import CapabilityRegistry
        from .meta.deploy import rollback_capability
        from .ambient import push_event, AmbientEvent
        from .db import now_iso

        registry = CapabilityRegistry(conn)
        last = registry.get_last_deployed()
        if not last:
            return  # No capabilities deployed — nothing to check.

        # Get the daemon's last exit status from the status file.
        # If the last cycle was completed successfully, no rollback needed.
        try:
            status = _read_status()
            last_outcome = status.get("last_cycle_outcome")
        except Exception:
            last_outcome = None

        # If the last cycle outcome was 'failed' or the status shows a crash
        # (no successful completion since the capability was deployed), rollback.
        if last_outcome == "failed" or last_outcome is None:
            # Check if the capability was deployed AFTER the last successful cycle.
            deployed_at = last.added_at
            last_success_cycle = conn.execute(
                "SELECT finished_at FROM watch_history "
                "WHERE outcome = 'succeeded' ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if last_success_cycle:
                success_time = last_success_cycle["finished_at"] or ""
                if success_time > deployed_at:
                    # A successful cycle happened after deployment — no crash.
                    return

            # Deploy either has no successful cycle after it, or the last cycle
            # failed. Auto-rollback.
            _log(f"Auto-rollback: daemon may have crashed after '{last.name}' deployment — rolling back...")
            ok = rollback_capability(conn, last.name)
            if ok:
                push_event(conn, AmbientEvent(
                    timestamp=now_iso(),
                    event_type="auto_rollback",
                    title=f"⚠ Auto-rolled back '{last.name}' — caused daemon issues",
                    detail="The daemon detected an unstable state after deployment and reverted the change.",
                    priority=3,
                    category="system",
                ))
                _log(f"Auto-rollback: successfully rolled back '{last.name}'")
            else:
                _log(f"Auto-rollback: failed to roll back '{last.name}'")

    except Exception as exc:
        _log(f"Auto-rollback check failed: {exc}")


def _stage_daily_briefing(conn) -> None:
    """Generate a daily briefing on the first daemon cycle after 8:00 AM.

    Runs once per calendar day in the ALWAYS maintenance section. Checks
    whether a briefing has already been delivered today (via ``briefing_log``)
    and skips if so. Best-effort: never fails the cycle.

    The briefing is pushed to the ambient feed as a ``briefing_available``
    event so the operator sees it in the dashboard and notifications.
    """
    try:
        from datetime import datetime, timezone
        from .db import now_iso
        from .briefing import (
            build_briefing,
            build_evening_briefing,
            _compute_headline,
            _cache_briefing,
            has_briefing_been_delivered,
            mark_briefing_delivered,
            format_briefing_summary,
        )
        from .ambient import push_event, AmbientEvent, EventType

        now = datetime.now(timezone.utc)
        hour = now.hour
        is_evening = hour >= 20  # 8 PM UTC = evening cutoff
        briefing_type = "evening" if is_evening else "morning"

        # Check if already delivered today.
        if has_briefing_been_delivered(conn, briefing_type):
            return

        # Build the briefing.
        if is_evening:
            report = build_evening_briefing(conn)
        else:
            report = build_briefing(conn, hours=24)
            report.headline = _compute_headline(report)
            if not report.headline:
                report.headline = "All quiet — no significant changes"
            _cache_briefing(conn, report)

        # Mark as delivered so it doesn't re-fire this cycle.
        mark_briefing_delivered(conn, briefing_type)

        # Push to ambient feed.
        summary = format_briefing_summary(report)
        push_event(conn, AmbientEvent(
            timestamp=now_iso(),
            event_type=EventType.BRIEFING_AVAILABLE,
            title=f"{'Evening wrap-up' if is_evening else 'Morning briefing'} available",
            detail=summary or "View the full briefing: `friday briefing`",
            priority=2,
            category="system",
            actionable=True,
            action_label="View briefing",
            action_command=f"friday briefing {'--evening' if is_evening else ''}",
        ), dedup_hours=12)

        _log(f"Daily briefing ({briefing_type}): {summary}")

    except Exception as exc:
        _log(f"Daily briefing stage failed: {exc}")


def _stage_spontaneous_review(ctx: CycleContext, conn) -> None:
    """Run spontaneous code review — proactively identify code worth reviewing.

    Checks five trigger sources each cycle (dirty repos, PR signals, CI
    failures, skill drift, blast radius) and pushes findings to the pending
    review queue + ambient feed. Best-effort: never fails the cycle.

    Runs in the ALWAYS section after watcher checks so review findings appear
    in the same batch of ambient events.
    """
    try:
        from .spontaneous_review import SpontaneousReviewEngine

        engine = SpontaneousReviewEngine(conn)
        notes = engine.run()
        if not notes:
            return

        # Push high/medium severity to ambient feed.
        pushed = engine.push_to_feed(notes)
        if pushed:
            _log(f"Spontaneous review: {pushed} event(s) pushed to feed.")

        # Insert all notes into pending queue.
        inserted = engine.push_to_pending(notes)
        if inserted:
            _log(f"Spontaneous review: {inserted} new review note(s) "
                 f"added to pending queue (from {len(notes)} total findings).")
    except Exception as exc:
        _log(f"Spontaneous review failed: {exc}")


def _stage_watcher_check(ctx: CycleContext, conn) -> None:
    """Run persistent watcher checks — conditions the operator is waiting
    to be notified about ("tell me when this test passes").

    Runs after autonomous planning so that watcher-triggered events appear
    in the same cycle's notification batch. Best-effort: never fails the cycle.
    """
    ctx.watchers_checked = 0
    ctx.watchers_fired = 0
    try:
        from .watcher import WatcherEngine

        eng = WatcherEngine(conn)
        results = eng.check_all()
        ctx.watchers_checked = len(results)
        ctx.watchers_fired = sum(1 for r in results if r.get("triggered"))
        if ctx.watchers_fired:
            _log(f"Watcher check: {ctx.watchers_fired}/{ctx.watchers_checked} "
                 f"watcher(s) triggered.")
    except Exception as exc:
        _log(f"Watcher check failed: {exc}")


def _stage_telemetry(ctx: CycleContext, conn) -> None:
    """Collect system telemetry and store in working memory.

    Every cycle, captures CPU, memory, disk, and network metrics via psutil
    and writes them to WorkingMemory so the dashboard, briefing, and ambient
    feed can query them. Best-effort: never fails or raises.

    If any resource exceeds 90%, pushes a priority-3 alert event to the
    ambient feed. GPU metrics are included when nvidia-smi is available.
    """
    try:
        from .telemetry import collect_telemetry
        from .memory import WorkingMemory
        from .ambient import push_event, AmbientEvent

        snap = collect_telemetry(include_gpu=True)
        wm = WorkingMemory(conn)

        # CPU
        cpu_alert = snap.cpu_percent > 90
        wm.set_context(
            "telemetry_cpu", f"{snap.cpu_percent:.1f}%",
            category="telemetry", source="daemon",
            priority=3 if cpu_alert else 1,
            ttl_seconds=300,
            context=f"CPU: {snap.cpu_percent:.1f}% on {snap.cpu_count} cores",
        )
        # Memory
        mem_alert = snap.memory_percent > 90
        wm.set_context(
            "telemetry_memory", f"{snap.memory_percent:.1f}%",
            category="telemetry", source="daemon",
            priority=3 if mem_alert else 1,
            ttl_seconds=300,
            context=f"MEM: {snap.memory_percent:.1f}% of {snap._fmt_bytes(snap.memory_total)}",
        )
        # Disk
        disk_alert = snap.disk_percent > 90
        wm.set_context(
            "telemetry_disk", f"{snap.disk_percent:.1f}%",
            category="telemetry", source="daemon",
            priority=3 if disk_alert else 1,
            ttl_seconds=300,
            context=f"DISK: {snap.disk_percent:.1f}% used",
        )
        # Brief summary
        wm.set_context(
            "telemetry_summary", snap.format_brief(),
            category="telemetry", source="daemon",
            priority=1,
            ttl_seconds=300,
            context=snap.format_brief(),
        )

        # Alert on critical resource usage.
        if cpu_alert or mem_alert or disk_alert:
            push_event(conn, AmbientEvent(
                timestamp=now_iso(),
                event_type="resource_alert",
                title="⚠ High resource usage detected",
                detail=snap.format_brief(),
                priority=3,
                category="system",
            ))

        # Log summary on significant changes (log-friendly).
        if ctx.is_full:
            _log(f"Telemetry: {snap.format_brief()}")

    except Exception as exc:
        _log(f"Telemetry stage failed: {exc}")


def _stage_presence(ctx: CycleContext, conn) -> None:
    """Detect operator presence state and manage interrupt queue.

    Every cycle, collects ambient signals (desktop activity, calendar,
    git activity) and determines the operator's presence state. When
    the state changes, pushes an ambient event. Also delivers any
    pending deferred interrupts that are now appropriate for the
    current state.

    Best-effort: never fails or raises.
    """
    try:
        from .presence import (
            detect_and_persist,
            deliver_pending_interrupts,
            format_state,
        )
        from .ambient import push_event, presence_changed_event

        state, changed = detect_and_persist(conn)

        if changed:
            push_event(conn, presence_changed_event(
                old_state="unknown",
                new_state=state.value,
            ), dedup_hours=1)
            _log(f"Presence: {format_state(state)}")

        # Deliver any pending deferred interrupts that are now appropriate.
        delivered = deliver_pending_interrupts(conn, state, max_per_cycle=2)
        if delivered:
            for d in delivered:
                _log(f"Deferred interrupt delivered: {d['event_type']} — {d['message'][:60]}")

    except Exception as exc:
        _log(f"Presence detection stage failed: {exc}")


# Module-level cache for previous screen context (change detection).
# Persisted across cycles so we can compare against the last snapshot.
_LAST_SCREEN_CTX: dict = {
    "active_app": "",
    "active_window": "",
    "browser_url": "",
    "clipboard_text": "",
}


def _stage_screen_aware(ctx: CycleContext, conn) -> None:
    """Collect workspace screen context and store in working memory.

    Every cycle, detects the active window, running app, clipboard
    content, and (opt-in) screen text via OCR. Stores in WorkingMemory
    so the dashboard, briefing, and ambient feed can query what you're
    working on. Best-effort: never fails or raises.

    Privacy: clipboard is always read; OCR is opt-in only.

    Also detects changes since the last cycle (app switch, URL change,
    clipboard change) and stores them in ``ctx`` for the auto-watcher
    stage to consume.
    """
    try:
        from .screen import collect_screen_context, detect_screen_changes, ScreenContext, ScreenChange
        from .memory import WorkingMemory

        ctx_screen = collect_screen_context(
            include_ocr=False,
            include_clipboard=True,
        )
        wm = WorkingMemory(conn)

        # Store current context in working memory.
        if ctx_screen.active_window_process:
            wm.set_context(
                "active_app", ctx_screen.active_window_process,
                category="workspace", source="screen",
                priority=2, ttl_seconds=300,
                context=f"Active: {ctx_screen.active_window_process}",
            )
        if ctx_screen.active_window_title:
            wm.set_context(
                "active_window", ctx_screen.active_window_title[:80],
                category="workspace", source="screen",
                priority=1, ttl_seconds=300,
            )
        if ctx_screen.browser_url:
            wm.set_context(
                "browser_url",
                f"{ctx_screen.browser_name}: {ctx_screen.browser_url[:80]}",
                category="workspace", source="screen",
                priority=1, ttl_seconds=120,
            )
        if ctx_screen.clipboard_text:
            wm.set_context(
                "clipboard", ctx_screen.clipboard_text[:200],
                category="workspace", source="screen",
                priority=1, ttl_seconds=120,
                context=f"Clipboard ({ctx_screen.clipboard_source})",
            )
        brief = ctx_screen.format_brief()
        wm.set_context(
            "workspace_summary", brief,
            category="workspace", source="screen",
            priority=1, ttl_seconds=300, context=brief,
        )
        if ctx.is_full and ctx_screen.active_window_process:
            _log(f"Screen: {brief}")

        # Detect changes from previous snapshot and store for auto-watchers.
        global _LAST_SCREEN_CTX
        prev_ctx = ScreenContext(
            active_window_process=_LAST_SCREEN_CTX.get("active_app", ""),
            active_window_title=_LAST_SCREEN_CTX.get("active_window", ""),
            browser_url=_LAST_SCREEN_CTX.get("browser_url", ""),
            clipboard_text=_LAST_SCREEN_CTX.get("clipboard_text", ""),
        )
        changes: list[ScreenChange] = detect_screen_changes(prev_ctx, ctx_screen)

        # Store changes in module-level cache for the auto-watcher stage.
        _LAST_SCREEN_CTX["screen_changes"] = changes
        _LAST_SCREEN_CTX["active_app"] = ctx_screen.active_window_process
        _LAST_SCREEN_CTX["active_window"] = ctx_screen.active_window_title
        _LAST_SCREEN_CTX["browser_url"] = ctx_screen.browser_url
        _LAST_SCREEN_CTX["clipboard_text"] = ctx_screen.clipboard_text

        # Log detected changes and store in working memory timeline.
        if changes:
            for c in changes:
                _log(f"Screen change detected: {c.change_type} — {c.detail}")
                # Store each change as a timeline entry in working memory
                # (3-hour TTL so the timeline shows recent history).
                timeline_key = f"screen_{c.change_type}_{int(time.time())}"
                wm.set_context(
                    timeline_key,
                    c.detail[:200],
                    category="timeline",
                    source="screen",
                    priority=2,
                    ttl_seconds=10800,  # 3 hours
                    context=f"{c.change_type}: {c.old_value[:40]} → {c.new_value[:40]}",
                )

    except Exception as exc:
        _log(f"Screen awareness stage failed: {exc}")


def _stage_auto_watchers(ctx: CycleContext, conn) -> None:
    """Auto-create screen-context watchers from detected changes.

    When Friday detects an app switch, URL change, or clipboard change,
    this stage creates temporary persistent watchers that monitor the
    new context. These watchers auto-expire after 30 minutes.

    Respects the operator's tuning rules from ``friday wait context --tune``:
    - ``friday wait context --tune add --app brave --action ignore``
      → ignores all Brave switches
    - ``friday wait context --tune add --app slack --action watch``
      → watches Slack even though it's not a known browser/IDE
    - ``friday wait context --tune defaults`` → restores default behavior

    Best-effort: never fails the cycle.
    """
    try:
        from .watcher import (
            WatcherEngine,
            should_create_auto_watcher,
            DEFAULT_BROWSER_KEYWORDS,
            DEFAULT_IDE_KEYWORDS,
        )

        global _LAST_SCREEN_CTX
        changes = _LAST_SCREEN_CTX.get("screen_changes", [])
        if not changes:
            # Still prune stale auto-watchers even without changes.
            eng = WatcherEngine(conn)
            pruned = eng.prune_auto_watchers()
            if pruned:
                _log(f"Auto-watchers: pruned {pruned} stale watcher(s).")
            return

        eng = WatcherEngine(conn)

        for change in changes:
            ctype = change.change_type
            old_val = change.old_value
            new_val = change.new_value

            if ctype == "app_switch":
                new_app = new_val.lower()

                # Check the operator's tuning rules before creating a watcher.
                if not should_create_auto_watcher(conn, new_app):
                    _log(f"Auto-watchers: skipping '{new_val}' (tuned off or unknown app).")
                    continue

                # Use default category detection (already validated by
                # should_create_auto_watcher above; just determine label).
                if any(k in new_app for k in _DEFAULT_BROWSER_KEYWORDS):
                    eng.create_auto_watcher(
                        f"browser: {new_val[:20]} active",
                        "active_app",
                        {"app": new_val},
                        ttl_minutes=30,
                    )
                elif any(k in new_app for k in _DEFAULT_IDE_KEYWORDS):
                    eng.create_auto_watcher(
                        f"ide: {new_val[:20]} active",
                        "active_app",
                        {"app": new_val},
                        ttl_minutes=30,
                    )
                else:
                    # User-tuned app that's not in default categories
                    # (e.g. "slack" added via `--tune add --app slack --action watch`).
                    eng.create_auto_watcher(
                        f"app: {new_val[:20]} active",
                        "active_app",
                        {"app": new_val},
                        ttl_minutes=30,
                    )

            elif ctype == "url_change":
                eng.create_auto_watcher(
                    f"url: {new_val[:30]}...",
                    "window_title",
                    {"contains": new_val[:40]},
                    ttl_minutes=20,
                )

            elif ctype == "clipboard_change":
                short_val = new_val.strip()[:60]
                if "http" in short_val.lower() or ".com" in short_val.lower():
                    eng.create_auto_watcher(
                        "clipboard: URL detected",
                        "clipboard_content",
                        {"contains": short_val[:30]},
                        ttl_minutes=15,
                    )
                elif len(short_val) > 20:
                    eng.create_auto_watcher(
                        "clipboard: content changed",
                        "clipboard_content",
                        {"min_length": 10},
                        ttl_minutes=15,
                    )

        # Prune stale auto-watchers after creating new ones.
        pruned = eng.prune_auto_watchers()
        if pruned:
            _log(f"Auto-watchers: pruned {pruned} stale watcher(s).")

    except Exception as exc:
        _log(f"Auto-watcher stage failed: {exc}")


def _stage_maintenance(ctx: CycleContext, conn) -> None:
    """Run always-needed maintenance: feed retention, memory decay, etc.

    This stage is invoked even when the main pipeline has failed, so that
    DB bloat never compounds across failed cycles.
    """
    pruned = 0
    try:
        from .ambient import prune_feed
        pruned = prune_feed(conn)
        if pruned:
            _log(f"Feed retention: pruned {pruned} old event(s).")
    except Exception as exc:
        _log(f"Feed retention pruner failed: {exc}")

    try:
        from .memory import WorkingMemory, MemoryEngine

        wm = WorkingMemory(conn)
        expired = wm.clear_expired()
        if expired:
            _log(f"Working memory: cleared {expired} expired entry/ies.")

        now_ts = now_iso()
        wm.set_context(
            "daemon_status",
            "running",
            category="status",
            source="system",
            priority=2,
            ttl_seconds=7200,
            context="Daemon is running.",
        )
        wm.set_context(
            "last_cycle_at",
            ctx.started_at[:19] if ctx.started_at else now_ts[:19],
            category="status",
            source="system",
            priority=1,
            ttl_seconds=7200,
        )

        # Kill switch state (critical — always visible).
        try:
            from .autonomy import is_kill_switch_active
            ks_active = is_kill_switch_active()
            wm.set_context(
                "kill_switch",
                "ACTIVE" if ks_active else "inactive",
                category="autonomy",
                source="system",
                priority=5 if ks_active else 1,
                ttl_seconds=3600,
                context="Emergency stop for all autonomous actions." if ks_active else "",
            )
        except Exception:
            pass

        # Pending autonomous plans count.
        try:
            pending_plans = conn.execute(
                "SELECT COUNT(*) AS cnt FROM autonomous_actions WHERE status='pending'"
            ).fetchone()
            pcount = pending_plans["cnt"] if pending_plans else 0
            if pcount:
                wm.set_context(
                    "pending_plans",
                    f"{pcount} plan(s) awaiting confirmation",
                    category="autonomy",
                    source="planner",
                    priority=3,
                    ttl_seconds=3600,
                )
        except Exception:
            pass

        mem = MemoryEngine(conn)
        decayed = mem.decay_memories(days_threshold=7, decay_rate=0.2)
        if decayed:
            _log(f"Memory decay: reduced recency_score for {decayed} fact(s).")
    except Exception as exc:
        _log(f"Working memory / memory decay failed: {exc}")

    try:
        from .autonomy import reconcile_escalation
        escalations = reconcile_escalation(conn)
        for msg in escalations:
            _log(f"Autonomy escalation: {msg}")
    except Exception as exc:
        _log(f"Autonomy reconciliation failed: {exc}")

    ctx.pruned = pruned


# ---- Ambient event pushing (ALWAYS runs) ---------------------------------


def _stage_push_events(ctx: CycleContext, conn,
                       has_rep: bool,
                       conv_extracted: dict,
                       conv_learned: int,
                       post_repair_drifts: list | None = None) -> None:
    """Push ambient feed events for cycle results.

    Always runs — even when the main pipeline partially failed, we still
    surface whatever results we have.

    ``post_repair_drifts`` comes from ``_stage_autonomous_planning()`` and
    contains skill repair outcomes (skill_name, strategy, pre_health,
    post_health) that get pushed as ``SKILL_AUTO_REPAIRED`` events.
    """
    try:
        from .ambient import (
            push_event,
            repo_change_event,
            knowledge_event,
            initiative_event,
            suggestion_event,
            gap_event,
            pattern_event,
            intent_event,
            skill_event,
            drift_event,
            correlation_event,
            dispatch_event,
            cycle_complete_event,
            briefing_event,
            worker_approved_event,
            skill_repaired_event,
        )

        if ctx.is_full and has_rep:
            push_event(conn, repo_change_event(
                ctx.repos_changed,
                ctx.repos_scanned,
                names=ctx.changed_repo_names or None,
            ), dedup_hours=6)
            push_event(conn, knowledge_event(ctx.knowledge_updated), dedup_hours=6)
            push_event(conn, initiative_event(ctx.new_pending_initiatives))
            sug = suggestion_event(ctx.new_suggestions, ctx.high_severity_suggestions)
            if sug:
                push_event(conn, sug)
            push_event(conn, gap_event(ctx.new_gaps, ctx.open_gaps))
            push_event(conn, pattern_event(ctx.new_patterns, ctx.top_patterns), dedup_hours=2)
            push_event(conn, intent_event(ctx.new_intents, ctx.high_conf_intents), dedup_hours=2)
            push_event(conn, skill_event(ctx.new_skills), dedup_hours=6)
            push_event(conn, drift_event(ctx.drifted_skills))
            push_event(conn, correlation_event(ctx.new_correlations), dedup_hours=2)
            push_event(conn, dispatch_event(ctx.auto_dispatched), dedup_hours=6)
        push_event(conn, cycle_complete_event(), dedup_hours=6)

        # Compute and store relationship metrics (post-cycle, full cycles only).
        if ctx.is_full:
            try:
                from .relationship import compute_all_metrics
                compute_all_metrics(conn, window_days=7)
            except Exception:
                pass

        # Push briefing-available event (only after full cycles with data).
        if ctx.is_full:
            parts: list[str] = []
            if ctx.repos_changed:
                parts.append(f"Repos: {ctx.repos_changed} changed")
            if ctx.new_pending_initiatives:
                parts.append(f"Initiatives: {ctx.new_pending_initiatives}")
            if ctx.drifted_skills:
                parts.append(f"Degraded: {ctx.drifted_skills}")
            if ctx.watchers_fired:
                parts.append(f"Watchers: {ctx.watchers_fired}")
            if ctx.cycle_outcome == "failed":
                parts.append("⚠ Cycle had errors")
            summary = " · ".join(parts) if parts else "No new activity"
            push_event(conn, briefing_event(summary), dedup_hours=6)

        # Push self-healing events (worker approvals + skill repairs).
        if ctx.is_full and ctx.worker_proposals_approved:
            push_event(conn, worker_approved_event(ctx.worker_proposals_approved),
                      dedup_hours=6)

        if post_repair_drifts:
            for d in post_repair_drifts:
                push_event(conn, skill_repaired_event(
                    name=d.get("skill_name", "?"),
                    strategy=d.get("strategy", "?"),
                    pre_health=d.get("pre_health", "?"),
                    post_health=d.get("post_health", "?"),
                ), dedup_hours=6)

        # Push conversation learning event (only if something was learned).
        if conv_learned:
            key_count = len(conv_extracted)
            title = f"Learned {key_count} operator preference(s) from conversation"
            if key_count > 0:
                detail = "; ".join(f"{k}={v}" for k, v in conv_extracted.items()
                                 if not k.startswith("_"))
                push_event(conn, {
                    "event_type": "conversation_learned",
                    "title": title,
                    "detail": detail,
                    "source": "daemon",
                    "priority": 2,
                    "category": "intelligence",
                    "actionable": False,
                }, dedup_hours=24)

    except Exception as exc:
        _log(f"Ambient event feed failed: {exc}")


# ---- Execution pipeline stage (M9.2-M9.5: plan → resolve → schedule → run) ---


def _stage_execution_pipeline(ctx: CycleContext, conn) -> dict:
    """Run the planning→scheduler→resolver→runtime pipeline.

    Finds task graphs in ``compiled`` or ``approved`` status that have NOT
    yet been scheduled (no row in scheduler_runs), then runs each through:

      1. CapabilityResolver.resolve_graph()  —  assign workers to tasks
      2. TaskScheduler.schedule_graph()       —  compute waves + schedule
      3. RuntimeEngine.run()                  —  execute the schedule

    This is the bridge between "Friday can plan" and "Friday executes".
    Before this stage, plans accumulated in DB tables but were never run.

    Stage failures are isolated: one graph failing does not abort others.
    Returns a dict with execution counts for the cycle context.
    """
    result: dict = {
        "graphs_resolved": 0,
        "graphs_scheduled": 0,
        "graphs_executed": 0,
        "sessions": [],
    }

    try:
        # 1. Find compiled/approved graphs that haven't been scheduled yet.
        graphs = conn.execute(
            "SELECT id, goal, status FROM task_graphs "
            "WHERE status IN ('compiled', 'approved') "
            "AND id NOT IN ("
            "  SELECT graph_id FROM scheduler_runs WHERE status = 'scheduled'"
            "  UNION "
            "  SELECT graph_id FROM scheduler_runs WHERE status = 'running'"
            "  UNION "
            "  SELECT graph_id FROM scheduler_runs WHERE status = 'completed'"
            ")"
            "ORDER BY created_at DESC"
        ).fetchall()

        if not graphs:
            return result

        from .resolver.engine import CapabilityResolver
        from .scheduler.engine import TaskScheduler
        from .runtime.engine import RuntimeEngine

        for g in graphs:
            graph_id: str = g["id"]
            goal: str = g.get("goal", "") or ""

            try:
                # 2. Resolve capabilities if not already assigned.
                resolver = CapabilityResolver(conn)
                existing = resolver.assignments(graph_id)
                if not existing:
                    resolve_result = resolver.resolve_graph(
                        graph_id, workspace=".")
                    if resolve_result.unresolved > 0:
                        _log(f"Execution pipeline: '{goal[:60]}' has "
                             f"{resolve_result.unresolved} unresolved task(s)"
                             f" — skipping execution.")
                        continue
                    result["graphs_resolved"] += 1
                    _log(f"Execution pipeline: resolved '{goal[:60]}'"
                         f" ({resolve_result.assigned} assigned tasks).")

                # 3. Schedule the graph (compute waves, priorities).
                scheduler = TaskScheduler(conn)
                schedule_result = scheduler.schedule_graph(graph_id)
                result["graphs_scheduled"] += 1
                _log(f"Execution pipeline: scheduled '{goal[:60]}'"
                     f" ({schedule_result.schedule.wave_count} waves,"
                     f" {len(schedule_result.schedule.tasks)} tasks).")

                # 4. Execute via RuntimeEngine.
                engine = RuntimeEngine(conn, workspace=".")  # project root (daemon runs from cwd)
                report = engine.run(schedule_result.schedule)
                result["graphs_executed"] += 1
                result["sessions"].append(report.session_id)

                _log(f"Execution pipeline: executed '{goal[:60]}'"
                     f" — session {report.session_id[:12]}...,"
                     f" {report.succeeded} succeeded,"
                     f" {report.failed} failed,"
                     f" {report.cancelled} cancelled,"
                     f" {report.duration_ms}ms.")

            except Exception as exc:
                _log(f"Execution pipeline: graph '{goal[:60]}' "
                     f"({graph_id[:24]}...) failed: {exc}")
                ctx.record_stage_failure(
                    f"exec:{graph_id[:24]}", str(exc)[:200])

    except Exception as exc:
        _log(f"Execution pipeline stage failed: {exc}")
        ctx.record_stage_failure("exec_pipeline", str(exc)[:200])

    return result


# ---- Autonomous planner stage ---------------------------------------------


def _stage_autonomous_planning(ctx: CycleContext, conn) -> dict:
    """Run autonomous action planning after analysis completes."""
    auto_result: dict = {}
    try:
        from .autonomous_planner import plan_and_dispatch
        auto_result = plan_and_dispatch(conn, ctx.to_result_dict())
        if auto_result.get("planned", 0):
            _log(f"Autonomous planner: {auto_result['planned']} plan(s) created, "
                 f"{auto_result['dispatched']} dispatched, "
                 f"{auto_result['pending_confirm']} pending confirmation.")

        # Post-repair drift re-check.
        post_drifts = auto_result.get("post_repair_drifts", [])
        if post_drifts:
            repairs_gone = sum(
                1 for d in post_drifts
                if d.get("post_health") in ("healthy", "deleted")
            )
            if repairs_gone:
                drifted_before = ctx.drifted_skills
                ctx.drifted_skills = max(0, ctx.drifted_skills - repairs_gone)
                _log(f"Post-repair drift re-check: {repairs_gone} skill(s) "
                     f"now healthy/deleted (was {drifted_before}, "
                     f"now {ctx.drifted_skills} drifted).")
            for d in post_drifts:
                sn = d.get("skill_name", "?")
                pre = d.get("pre_health", "?")
                post = d.get("post_health", "?")
                strat = d.get("strategy", "?")
                _log(f"  Repair '{sn}': {pre} -> {post} (strategy: {strat})")
    except Exception as exc:
        _log(f"Autonomous planner failed: {exc}")
    return auto_result


# ---- Watch-history persistence --------------------------------------------


def _persist_watch_history(ctx: CycleContext, conn) -> None:
    """Write cycle outcome to watch_history table."""
    try:
        if ctx.is_full:
            conn.execute(
                "UPDATE watch_history SET finished_at=?, outcome=?, "
                "repos_scanned=?, repos_changed=?, "
                "knowledge_updated=?, understanding_updated=?, "
                "initiatives_changed=?, insights_changed=?, "
                "new_pending_initiatives=? WHERE id=?",
                (now_iso(), ctx.cycle_outcome,
                 ctx.repos_scanned, ctx.repos_changed,
                 ctx.knowledge_updated, ctx.understanding_updated,
                 ctx.initiatives_changed, ctx.insights_changed,
                 ctx.new_pending_initiatives, ctx.history_id))
        else:
            conn.execute(
                "UPDATE watch_history SET finished_at=?, outcome=? WHERE id=?",
                (now_iso(), ctx.cycle_outcome, ctx.history_id))
        conn.commit()
    except Exception as exc:
        _log(f"Watch history update failed: {exc}")


# ---------------------------------------------------------------------------
# _run_cycle — orchestrator (refactored into stage calls)
# ---------------------------------------------------------------------------


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


def _run_cycle() -> dict:
    """Run a single observation cycle using the refactored stage pipeline.

    Architecture:
      1. Acquire cycle lock (in-process threading.Lock + legacy lock file).
      2. Assess cycle need via ``_check_cycle_need()`` (fast git/obs poll).
      3. If full cycle needed, run the core pipeline:
         a. Refresh (git + knowledge pipeline) — sequential, primary connection.
         b. Parallel stages via ThreadPoolExecutor — each on its own connection.
         c. Sequential post-processing (intent labeling → skill formation →
            auto-dispatch → drift detection).
      4. ALWAYS: maintenance (feed retention, working memory, memory decay).
      5. ALWAYS: ambient event pushing.
      6. ALWAYS: autonomous planning.
      7. ALWAYS: watch_history persistence.

    Stage failures are isolated — one failed stage does not abort the
    pipeline. Maintenance and event pushing ALWAYS run, even on failure.

    Returns a dict (backward-compatible with old ``_run_cycle`` callers).
    """
    # Acquire the in-process lock (primary mechanism).
    if not _CYCLE_LOCK.acquire(blocking=False):
        return {"cycle_outcome": "skipped", "error_detail": "in-process lock held"}

    # Also acquire the legacy lock file (for external tools).
    lock_fd: Optional[int] = None
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        _CYCLE_LOCK.release()
        return {"cycle_outcome": "skipped", "error_detail": "lock file held by another process"}

    conn = connect()
    started = now_iso()
    cur = conn.execute(
        "INSERT INTO watch_history (started_at, outcome) VALUES (?, 'running')",
        (started,))
    history_id = cur.lastrowid

    # Build the typed cycle context.
    ctx = CycleContext(history_id=history_id or 0, started_at=started)
    has_rep = False
    conv_extracted: dict = {}
    conv_learned: int = 0

    try:
        # ── Phase 0: Determine cycle depth ──
        need = _check_cycle_need()
        ctx.cycle_type = "full" if need.has_major_change else "fast"
        ctx.repos_changed = need.changed_count
        ctx.changed_repo_names = need.changed_repos

        cache = _CYCLE_CACHE
        if need.has_major_change:
            cache["fast_cycles_since_full"] = 0
            cache["last_full_cycle_idx"] = history_id
        else:
            cache["fast_cycles_since_full"] = cache.get("fast_cycles_since_full", 0) + 1

        if need.has_major_change:
            # ════════════════════════════════════════════════════════════
            # FULL CYCLE — run the complete pipeline
            # ════════════════════════════════════════════════════════════

            # Stage 1: Refresh (sequential, primary connection).
            try:
                _stage_refresh(ctx, conn)
                has_rep = True
            except Exception:
                # Refresh failed — continue with what we have.
                pass

            # Stage 2: Parallel pipeline (each stage on its own connection).
            # Individual stage failures are caught per-stage rather than
            # aborting the entire ThreadPoolExecutor. A stage that fails
            # returns empty defaults; other stages complete independently.
            # All stage functions use the same ``(conn)`` signature so they
            # can be submitted uniformly via ``pool.submit(fn, connect())``.
            llm_results: dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cycle") as pool:
                futures: dict = {}

                def _safe_submit(fn, key: str) -> None:
                    """Submit a stage, catching setup errors per stage."""
                    try:
                        future = pool.submit(fn, connect())
                        futures[future] = key
                    except Exception as exc:
                        _log(f"Parallel stage '{key}' failed to submit: {exc}")
                        ctx.record_stage_failure(key, str(exc))

                _safe_submit(_stage_observation, "obs_engine")
                _safe_submit(_stage_suggest_analysis, "suggest")
                _safe_submit(_stage_gap_analysis, "gaps")
                _safe_submit(_stage_correlation, "correlation")
                _safe_submit(_stage_sequence_mining, "mining")
                _safe_submit(_stage_conversation_learning, "conv_learn")

                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        result = future.result(timeout=120)
                    except Exception as exc:
                        _log(f"Parallel stage '{key}' raised: {exc}")
                        ctx.record_stage_failure(key, str(exc))
                        result = {}
                    llm_results[key] = result

            # Collect parallel results into context.
            obs_result = llm_results.get("obs_engine", {})
            ctx.obs_count = obs_result.get("obs_count", 0)
            ctx.actions_derived = obs_result.get("actions_derived", 0)
            ctx.new_suggestions = llm_results.get("suggest", {}).get("new_suggestions", 0)
            ctx.high_severity_suggestions = llm_results.get("suggest", {}).get("high_severity_suggestions", 0)
            ctx.new_gaps = llm_results.get("gaps", {}).get("new_gaps", 0)
            ctx.open_gaps = llm_results.get("gaps", {}).get("open_gaps", 0)
            ctx.new_correlations = llm_results.get("correlation", {}).get("new_correlations", 0)
            ctx.new_patterns = llm_results.get("mining", {}).get("new_patterns", 0)
            ctx.top_patterns = llm_results.get("mining", {}).get("top_patterns", 0)
            conv_result = llm_results.get("conv_learn", {})
            conv_learned = conv_result.get("conv_learned", 0)
            conv_extracted = conv_result.get("conv_extracted", {})

            # Stage 3: Sequential post-processing (primary connection).
            intent_result = _stage_intent_labeling(ctx, conn)
            ctx.new_intents = intent_result.get("new_intents", 0)
            ctx.high_conf_intents = intent_result.get("high_conf_intents", 0)

            skill_result = _stage_skill_formation(ctx, conn, intent_result)
            ctx.new_skills = skill_result.get("new_skills", 0)

            ctx.auto_dispatched = _stage_auto_dispatch(ctx, conn)
            ctx.drifted_skills = _stage_drift_detection(ctx, conn)
            # Bridge: gap analysis → worker proposals. New open gaps
            # auto-create proposals, which are then auto-approved above.
            try:
                from .worker.genesis import propose_from_gaps
                auto_proposed = propose_from_gaps(conn)
                if auto_proposed:
                    _log(f"Gap→proposal bridge: {auto_proposed} proposal(s) created "
                         f"from open capability gaps.")
            except Exception as exc:
                _log(f"Gap-to-proposal bridge failed: {exc}")

            ctx.worker_proposals_approved = _stage_worker_auto_approve(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Maintenance (feed retention, working memory, decay)
        # ════════════════════════════════════════════════════════════════
        _stage_maintenance(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Telemetry (system resource snapshot)
        # ════════════════════════════════════════════════════════════════
        _stage_telemetry(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Screen awareness (active window, clipboard, apps)
        # ════════════════════════════════════════════════════════════════
        _stage_screen_aware(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Auto-create screen-context watchers
        # ════════════════════════════════════════════════════════════════
        _stage_auto_watchers(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Presence detection (operator state + interrupt gating)
        # ════════════════════════════════════════════════════════════════
        _stage_presence(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Ambient event pushing
        # ════════════════════════════════════════════════════════════════
        post_repair_drifts = auto_result.get("post_repair_drifts", [])
        _stage_push_events(ctx, conn, has_rep, conv_extracted, conv_learned,
                          post_repair_drifts)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Autonomous planning
        # ════════════════════════════════════════════════════════════════
        auto_result = _stage_autonomous_planning(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Persistent watcher checks
        # ════════════════════════════════════════════════════════════════
        _stage_watcher_check(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Spontaneous code review
        # ════════════════════════════════════════════════════════════════
        _stage_spontaneous_review(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Daily briefing check (first cycle after 8am)
        # ════════════════════════════════════════════════════════════════
        _stage_daily_briefing(conn)

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: PR review check (new/updated pull requests)
        # ════════════════════════════════════════════════════════════════
        try:
            from .pr_review import run_pr_review
            n = run_pr_review(conn)
            if n:
                _log(f"PR review: {n} new review(s) generated.")
        except Exception as exc:
            _log(f"PR review stage failed: {exc}")

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Auto-rollback check (self-evolution safety)
        # ════════════════════════════════════════════════════════════════
        try:
            _check_auto_rollback(conn)
        except Exception as exc:
            _log(f"Auto-rollback check failed: {exc}")

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Persistent mission progress
        # ════════════════════════════════════════════════════════════════
        _stage_mission_progress(ctx, conn)

        # ════════════════════════════════════════════════════════════════
        # FULL ONLY: Execution pipeline (plan → resolve → schedule → run)
        # ════════════════════════════════════════════════════════════════
        if ctx.is_full:
            exec_result = _stage_execution_pipeline(ctx, conn)
            ctx.graphs_resolved = exec_result.get("graphs_resolved", 0)
            ctx.graphs_scheduled = exec_result.get("graphs_scheduled", 0)
            ctx.graphs_executed = exec_result.get("graphs_executed", 0)
            ctx.sessions_executed = len(exec_result.get("sessions", []))

        # ════════════════════════════════════════════════════════════════
        # ALWAYS: Persist watch history
        # ════════════════════════════════════════════════════════════════
        _persist_watch_history(ctx, conn)

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        _log(f"Cycle failed:\n{tb[:2000]}")

        # Wrap in a structured FridayError envelope.
        from .errors import error_from_exception, FridayError, ErrorType
        if isinstance(exc, FridayError):
            friday_err = exc
        else:
            friday_err = error_from_exception(
                exc,
                action="daemon_cycle",
                target=f"cycle #{history_id}",
                recovery_hint="Check the daemon log for the full traceback: "
                              "`friday daemon logs`",
            )
        ctx.cycle_outcome = "failed"
        ctx.error_detail = str(friday_err)[:500]
        ctx.error_type = friday_err.error_type.value
        ctx.error_action = friday_err.action
        ctx.error_target = friday_err.target
        ctx.recovery_hint = friday_err.recovery_hint
        ctx.error_eta = friday_err.eta_hint

        # Maintenance ALWAYS runs — even on failure.
        try:
            _stage_maintenance(ctx, conn)
        except Exception as maint_exc:
            _log(f"Post-failure maintenance also failed: {maint_exc}")

        # Push failure event to feed.
        try:
            from .ambient import push_event, cycle_failed_event
            push_event(conn, cycle_failed_event(ctx.error_detail or "Unknown error"))
        except Exception:
            pass

        # Persist failure to watch_history.
        try:
            conn.execute(
                "UPDATE watch_history SET finished_at=?, outcome=?, "
                "error_detail=? WHERE id=?",
                (now_iso(), "failed", ctx.error_detail, history_id))
            conn.commit()
        except Exception:
            pass

    finally:
        conn.close()
        _CYCLE_LOCK.release()
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass

    return ctx.to_result_dict()


# ---------------------------------------------------------------------------
# Unified service identity polling (replaces 3× copy-paste)
# ---------------------------------------------------------------------------


def _service_identity_poll(
    shutdown_flag: Callable[[], bool],
    config: object,
    poll_interval: float,
    service_name: str,
    fetch_messages: Callable[..., list[dict]],
    send_ack: Callable[..., tuple[bool, Any]],
    edit_message: Callable[..., tuple[bool, Any]],
    send_fallback: Callable[..., tuple[bool, Any]],
    get_sender_id: Callable[[dict], str],
    get_chat_id: Callable[[dict], str],
    get_message_text: Callable[[dict], str],
    get_update_channel_id: Callable[[dict], str],
    fetch_kwargs: Optional[dict] = None,
) -> None:
    """Unified polling loop for any chat service.

    Parameters
    ----------
    shutdown_flag:
        Callable returning True when the daemon is shutting down.
    config:
        Service config object (``TelegramConfig``, ``SlackConfig``, etc.).
    poll_interval:
        Seconds between polls.
    service_name:
        Human-readable name for logging (``"telegram"``, ``"slack"``, etc.).
    fetch_messages:
        ``fn(config, **fetch_kwargs) -> list[dict]`` — fetches new messages.
    send_ack:
        ``fn(config, chat_id, "On it.") -> (ok, message_id)``.
    edit_message:
        ``fn(config, chat_id, message_id, reply_text) -> (ok, err)``.
    send_fallback:
        ``fn(config, chat_id, reply_text) -> (ok, err)`` — used when edit fails.
    get_sender_id:
        ``fn(msg_dict) -> str`` — returns the bot's own identifier.
    get_chat_id:
        ``fn(msg_dict) -> str`` — returns the chat/thread identifier.
    get_message_text:
        ``fn(msg_dict) -> str`` — returns the message text.
    get_update_channel_id:
        ``fn(msg_dict) -> str`` — returns the channel-ID string (e.g. "telegram:12345").
    fetch_kwargs:
        Extra kwargs passed to ``fetch_messages`` (e.g. ``limit``, ``timeout``, ``offset``).

    Runs until ``shutdown_flag()`` is True. Best-effort: failures are logged
    and the loop continues.
    """
    from .persona import IdentityEngine
    from .db import connect

    tconn = connect()
    engine = IdentityEngine(conn=tconn)
    kw = fetch_kwargs or {}

    # Get bot's own username/ID at startup so we skip our own messages.
    own_id: Optional[str] = None
    try:
        ourselves = fetch_messages(config, **{**kw, "limit": 1})
        if ourselves:
            own_id = get_sender_id(ourselves[0])
    except Exception:
        pass

    consecutive_errors = 0

    while not shutdown_flag():
        try:
            if not _is_config_configured(config):
                for _ in range(int(poll_interval)):
                    if shutdown_flag():
                        break
                    time.sleep(1)
                continue

            # Backoff on consecutive errors.
            if consecutive_errors >= 5:
                _log(f"{service_name} identity: {consecutive_errors} consecutive errors, "
                     f"backing off 60s.")
                for _ in range(60):
                    if shutdown_flag():
                        break
                    time.sleep(1)
                consecutive_errors = 0
                continue

            updates = fetch_messages(config, **kw)
            if updates:
                processed = 0
                for u in updates:
                    chat_id = get_chat_id(u)
                    text = get_message_text(u)
                    if not text or not chat_id:
                        continue

                    # Skip own messages (prevents echo loops).
                    sender_id = get_sender_id(u)
                    if own_id and sender_id == own_id:
                        continue

                    # Send instant acknowledgment.
                    ack_ok, ack_msg_id = send_ack(config, str(chat_id), "On it.")
                    if not ack_ok:
                        _log(f"{service_name}: ack failed: {ack_msg_id}")
                        continue

                    channel_id = get_update_channel_id(u)
                    reply = engine.process(text, channel_id=channel_id)

                    # Edit ack with real response.
                    if reply and ack_msg_id:
                        edit_ok, edit_err = edit_message(
                            config, str(chat_id), ack_msg_id, reply)
                        if edit_ok:
                            processed += 1
                        else:
                            # Fallback: send as new message.
                            send_fallback(config, str(chat_id), reply)
                            if ack_msg_id:
                                processed += 1

                if processed:
                    _log(f"{service_name} identity: responded to {processed} message(s)")

            consecutive_errors = 0

        except Exception as exc:
            consecutive_errors += 1
            _log(f"{service_name} identity poll error #{consecutive_errors}: {exc}")

        # Sleep in small increments for responsive shutdown.
        for _ in range(int(poll_interval)):
            if shutdown_flag():
                break
            time.sleep(1)


def _is_config_configured(config: object) -> bool:
    """Check if a service config has the ``configured`` attribute set."""
    try:
        return bool(getattr(config, "configured", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Legacy poll functions (thin wrappers for backward compat)
# ---------------------------------------------------------------------------

# These functions are preserved with their original signatures so that any
# code referencing ``_telegram_identity_poll``, ``_slack_identity_poll``, or
# ``_discord_identity_poll`` (e.g. tests, monkey-patching) continues to work.
# Each is a thin wrapper around ``_service_identity_poll()``.


def _telegram_identity_poll(interval: float = 5.0) -> None:
    """Poll Telegram for new messages and respond via IdentityEngine.

    Legacy wrapper. Uses the unified ``_service_identity_poll()`` internally.
    """
    from .services.telegram import TelegramConfig, _get_me, _get_updates, _send_message, _edit_message

    config = TelegramConfig.from_env()

    def _fetch(conf, **kw) -> list[dict]:
        return _get_updates(conf, **kw)

    def _ack(conf, chat_id: str, text: str):
        return _send_message(conf, chat_id, text)

    def _edit(conf, chat_id: str, msg_id: Any, text: str):
        return _edit_message(conf, chat_id, int(msg_id), text)

    def _fallback(conf, chat_id: str, text: str):
        return _send_message(conf, chat_id, text)

    def _sender_id(msg: dict) -> str:
        return msg.get("from_user", "")

    def _chat_id(msg: dict) -> str:
        return msg.get("chat_id", "")

    def _msg_text(msg: dict) -> str:
        return msg.get("text", "")

    def _channel_id(msg: dict) -> str:
        return f"telegram:{msg.get('chat_id', '')}"

    offset_file = Path("/tmp/friday_telegram_identity_offset.txt")

    def _fetch_with_offset(conf, **kw) -> list[dict]:
        offset: Optional[int] = None
        try:
            raw = offset_file.read_text().strip()
            if raw:
                offset = int(raw)
        except (OSError, ValueError):
            pass
        updates = _get_updates(conf, limit=kw.get("limit", 10),
                               timeout=kw.get("timeout", 10), offset=offset)
        if updates:
            max_id = max(u.get("update_id", 0) for u in updates if u.get("update_id"))
            try:
                offset_file.write_text(str(max_id + 1))
            except OSError:
                pass
        return updates

    _service_identity_poll(
        shutdown_flag=lambda: _daemon_shutdown,
        config=config,
        poll_interval=interval,
        service_name="telegram",
        fetch_messages=_fetch_with_offset,
        send_ack=_ack,
        edit_message=_edit,
        send_fallback=_fallback,
        get_sender_id=_sender_id,
        get_chat_id=_chat_id,
        get_message_text=_msg_text,
        get_update_channel_id=_channel_id,
    )


def _slack_identity_poll(interval: float = 10.0) -> None:
    """Poll Slack for new messages and respond via IdentityEngine.

    Legacy wrapper. Uses the unified ``_service_identity_poll()`` internally.
    """
    from .services.slack import (
        SlackConfig, _list_channels, _fetch_channel_messages,
        _post_message, _edit_message)

    config = SlackConfig.from_env()

    # Track last-seen timestamp per channel.
    seen: dict[str, str] = {}
    bot_user_id: Optional[str] = None

    try:
        from .services.slack import _get_client
        client = _get_client(config)
        if client is not None:
            auth = client.auth_test()
            bot_user_id = auth.get("user_id") if auth else None
    except Exception:
        pass

    def _fetch_all(conf, **kw) -> list[dict]:
        results: list[dict] = []
        channels = _list_channels(conf, limit=5)
        for ch in channels:
            ch_id = ch.get("id", "")
            if not ch_id:
                continue
            last_ts = seen.get(ch_id)
            msgs = _fetch_channel_messages(conf, ch_id, limit=5)
            for msg in reversed(msgs):
                ts = msg.get("ts", "")
                if not ts:
                    continue
                if last_ts and ts <= last_ts:
                    continue
                if bot_user_id and msg.get("user") == bot_user_id:
                    continue
                text = msg.get("text", "").strip()
                if not text:
                    continue
                results.append({
                    "chat_id": ch_id,
                    "text": text,
                    "ts": ts,
                    "from_user": msg.get("user", ""),
                    "channel_name": ch.get("name", "?"),
                })
            if msgs:
                seen[ch_id] = max(m["ts"] for m in msgs if m.get("ts"))
        return results

    def _ack(conf, chat_id: str, text: str):
        return _post_message(conf, chat_id, text)

    def _edit(conf, chat_id: str, msg_ts: Any, text: str):
        return _edit_message(conf, chat_id, msg_ts, text)

    def _fallback(conf, chat_id: str, text: str):
        return _post_message(conf, chat_id, text)

    def _sender_id(msg: dict) -> str:
        return msg.get("from_user", "")

    def _chat_id(msg: dict) -> str:
        return msg.get("chat_id", "")

    def _msg_text(msg: dict) -> str:
        return msg.get("text", "")

    def _channel_id(msg: dict) -> str:
        return f"slack:{msg.get('chat_id', '')}"

    _service_identity_poll(
        shutdown_flag=lambda: _daemon_shutdown,
        config=config,
        poll_interval=interval,
        service_name="slack",
        fetch_messages=_fetch_all,
        send_ack=_ack,
        edit_message=_edit,
        send_fallback=_fallback,
        get_sender_id=_sender_id,
        get_chat_id=_chat_id,
        get_message_text=_msg_text,
        get_update_channel_id=_channel_id,
    )


def _discord_identity_poll(interval: float = 10.0) -> None:
    """Poll Discord for new messages and respond via IdentityEngine.

    Legacy wrapper. Uses the unified ``_service_identity_poll()`` internally.
    """
    from .services.discord import (
        DiscordConfig, _list_guilds, _list_channels, _fetch_messages,
        _post_message, _edit_message)

    config = DiscordConfig.from_env()

    seen: dict[str, str] = {}
    bot_username: Optional[str] = None

    try:
        from .services.discord import _api_get
        me = _api_get(config, "/users/@me")
        if me:
            bot_username = me.get("username")
    except Exception:
        pass

    def _fetch_all(conf, **kw) -> list[dict]:
        results: list[dict] = []
        guilds = _list_guilds(conf, limit=1)
        for guild in guilds:
            gid = guild.get("id", "")
            if not gid:
                continue
            channels = _list_channels(conf, gid)
            for ch in channels[:5]:
                ch_id = ch.get("id", "")
                if not ch_id:
                    continue
                last_id = seen.get(ch_id)
                msgs = _fetch_messages(conf, ch_id, limit=5)
                for msg in reversed(msgs):
                    mid = msg.get("id", "")
                    if not mid:
                        continue
                    if last_id and mid <= last_id:
                        continue
                    if bot_username and msg.get("author") == bot_username:
                        continue
                    text = msg.get("content", "").strip()
                    if not text:
                        continue
                    results.append({
                        "chat_id": ch_id,
                        "text": text,
                        "id": mid,
                        "from_user": msg.get("author", ""),
                        "channel_name": ch.get("name", "?"),
                        "guild_name": guild.get("name", "?"),
                    })
                if msgs:
                    seen[ch_id] = max(m["id"] for m in msgs if m.get("id"))
        return results

    def _ack(conf, chat_id: str, text: str):
        return _post_message(conf, chat_id, text)

    def _edit(conf, chat_id: str, msg_id: Any, text: str):
        return _edit_message(conf, chat_id, msg_id, text)

    def _fallback(conf, chat_id: str, text: str):
        return _post_message(conf, chat_id, text)

    def _sender_id(msg: dict) -> str:
        return msg.get("from_user", "")

    def _chat_id(msg: dict) -> str:
        return msg.get("chat_id", "")

    def _msg_text(msg: dict) -> str:
        return msg.get("text", "")

    def _channel_id(msg: dict) -> str:
        return f"discord:{msg.get('chat_id', '')}"

    _service_identity_poll(
        shutdown_flag=lambda: _daemon_shutdown,
        config=config,
        poll_interval=interval,
        service_name="discord",
        fetch_messages=_fetch_all,
        send_ack=_ack,
        edit_message=_edit,
        send_fallback=_fallback,
        get_sender_id=_sender_id,
        get_chat_id=_chat_id,
        get_message_text=_msg_text,
        get_update_channel_id=_channel_id,
    )


# ---------------------------------------------------------------------------
# Daemon main loop
# ---------------------------------------------------------------------------


def run_daemon(interval_seconds: int = 900, no_notify: bool = False) -> None:
    """Main daemon loop. Called from the child process after fork.

    Optimized for 60-second fast-polling: each cycle detects whether
    anything changed via ``_check_cycle_need()`` and runs a full deep
    analysis only when needed. Idle fast cycles complete in < 2s.

    Args:
        interval_seconds: Configured interval. The loop always polls every
            ``_FAST_CHECK_INTERVAL`` (60s). ``interval_seconds`` is stored in
            status for compatibility; the actual full-cycle depth is
            controlled by ``_check_cycle_need()`` which forces a full cycle
            every N fast cycles even when nothing changed.
        no_notify: If True, suppress desktop notifications.
    """
    _log(f"Daemon started (PID {os.getpid()}, fast_poll={_FAST_CHECK_INTERVAL}s,"
         f" config_interval={interval_seconds}s).")

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGHUP, _handle_sighup)

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

    # Start Telegram identity polling sub-thread (fast-poll for interactive chat).
    _telegram_thread = threading.Thread(
        target=_telegram_identity_poll,
        args=(5.0,),
        daemon=True,
        name="telegram-identity",
    )
    _telegram_thread.start()
    _log("Telegram identity polling started (5s interval).")

    # Start Slack identity polling sub-thread.
    _slack_thread = threading.Thread(
        target=_slack_identity_poll,
        args=(10.0,),
        daemon=True,
        name="slack-identity",
    )
    _slack_thread.start()
    _log("Slack identity polling started (10s interval).")

    # Start Discord identity polling sub-thread.
    _discord_thread = threading.Thread(
        target=_discord_identity_poll,
        args=(10.0,),
        daemon=True,
        name="discord-identity",
    )
    _discord_thread.start()
    _log("Discord identity polling started (10s interval).")

    # Start TelemetryCollector sidecar thread (continuous metrics collection).
    try:
        from .telemetry import TelemetryCollector
        _telemetry_collector = TelemetryCollector(daemon_start_time=time.time())
        _telemetry_collector.start()
        _log("Telemetry collector started (10s interval).")
    except Exception as exc:
        _log(f"Telemetry collector failed to start: {exc}")

    # Run first cycle immediately (forced full cycle on startup).
    _CYCLE_CACHE["fast_cycles_since_full"] = _FULL_CHECK_EVERY_N + 1
    _do_cycle(cycle_count, _effective_no_notify)
    cycle_count += 1

    # Main loop: poll every _FAST_CHECK_INTERVAL seconds.
    while not _daemon_shutdown:
        # Check for SIGHUP-triggered cycle.
        if _daemon_cycle_now:
            _daemon_cycle_now = False
            _log("SIGHUP triggered immediate cycle.")
            _CYCLE_CACHE["fast_cycles_since_full"] = _FULL_CHECK_EVERY_N + 1
            _do_cycle(cycle_count, _effective_no_notify)
            cycle_count += 1
            continue

        # Sleep in 1-second increments so we can respond to signals.
        for _ in range(_FAST_CHECK_INTERVAL):
            if _daemon_shutdown or _daemon_cycle_now:
                break
            time.sleep(1)

        if _daemon_shutdown:
            break

        _do_cycle(cycle_count, _effective_no_notify)
        cycle_count += 1

    # Graceful shutdown.
    write_status(state="stopped")
    _log("Daemon stopped gracefully.")
    _remove_pid()


def _do_cycle(cycle_num: int, no_notify: bool) -> None:
    """Run one observation cycle and update status.

    Notifications are gated by both the ``--no-notify`` CLI flag AND the operator
    profile's ``should_notify()`` preference. If the operator has set
    'no_notifications=true' via 'friday profile set', notifications are
    suppressed even when ``--no-notify`` is not passed.

    Kill switch: if the emergency stop is active, the cycle is skipped
    immediately without running any observers, analyzers, or dispatchers.
    The daemon continues running so it can detect when the kill switch
    is released — it just doesn't execute any work.
    """
    # Check emergency kill switch before running any work.
    try:
        from .autonomy import is_kill_switch_active
        if is_kill_switch_active():
            _log(f"Cycle #{cycle_num + 1} SKIPPED (kill switch active).")
            write_status(
                last_cycle_at=now_iso(),
                last_cycle_outcome="skipped",
                cycle_count=cycle_num + 1,
                kill_switch_active=True,
            )
            return
    except Exception:
        pass

    _log(f"Cycle #{cycle_num + 1} starting...")

    # Refresh notification gate from profile.
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

    outcome = cycle.get("cycle_outcome", "failed")
    error_detail = cycle.get("error_detail")

    status_updates: dict = {
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

    # Copy Phase A ambient analysis fields into status.
    for key in _PHASE_A_FIELDS:
        if key in cycle:
            status_updates[key] = cycle[key]

    write_status(**status_updates)

    # Route notifications through the NotificationEngine.
    try:
        from .notification import notify_cycle_events
        from .operator.engine import get_preferred_channel
        nconn = connect()
        pref_channel = get_preferred_channel(nconn)
        notify_cycle_events(nconn, cycle, no_notify=effective_no_notify,
                            preferred_channel=pref_channel)
        nconn.close()
    except Exception as exc:
        _log(f"NotificationEngine failed: {exc}")

    # Proactive conversation engine.
    try:
        nconn2 = connect()
        from .proactive import check_and_proact
        cycle["_preferred_channel"] = pref_channel or "proactive"
        proact_result = check_and_proact(nconn2, cycle)
        if proact_result.get("signaled"):
            _log(f"Proactive: {proact_result['event_type']} — {proact_result['message'][:80]}")
        nconn2.close()
    except Exception as exc:
        _log(f"Proactive engine failed: {exc}")

    # Log summary.
    changed = cycle.get("repos_changed", 0)
    scanned = cycle.get("repos_scanned", 0)
    knowledge = cycle.get("knowledge_updated", 0)
    pending = cycle.get("new_pending_initiatives", 0)
    new_suggestions = cycle.get("new_suggestions", 0)
    new_gaps = cycle.get("new_gaps", 0)
    new_patterns = cycle.get("new_patterns", 0)
    new_intents = cycle.get("new_intents", 0)
    new_correlations = cycle.get("new_correlations", 0)
    new_skills = cycle.get("new_skills", 0)
    drifted_skills = cycle.get("drifted_skills", 0)

    outcome_label = outcome if outcome != "skipped" else "skipped (lock held)"
    _log(f"Cycle #{cycle_num + 1} {outcome_label}: {changed}/{scanned} repos changed, "
         f"{knowledge} knowledge updates, {pending} new initiatives, "
         f"{new_suggestions} suggestions, {new_gaps} new gaps, "
         f"{new_patterns} patterns, {new_intents} intents, "
         f"{new_correlations} correlations, {new_skills} skills, "
         f"{drifted_skills} drifted.")

    if outcome == "failed" and error_detail:
        _log(f"  Error: {error_detail[:300]}")


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


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
    try:
        sys.stdin.close()
        sys.stdout.flush()
        sys.stderr.flush()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
    except Exception:
        pass

    _write_pid(os.getpid())

    try:
        run_daemon(interval_seconds=interval_seconds, no_notify=no_notify)
    except Exception as exc:
        _log(f"Daemon crashed: {exc}")
        write_status(state="crashed", last_error=str(exc)[:500])
    finally:
        _remove_pid()

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
