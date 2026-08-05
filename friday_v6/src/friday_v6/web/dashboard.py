"""Dashboard data — JSON payloads for the `friday6 web` UI.

Every accessor is guarded (any failure returns an empty/neutral payload)
so the dashboard renders even when a subsystem is missing — the same
graceful-degradation law the daemon follows. No V3 writes, ever: V4
stays the product, V3 stays a read-only data source.

Payloads are built from the same sources the CLIs read:
- daemon:      ``~/.friday/v4_daemon.status`` + pid liveness
- security:    ``~/.friday/v4_security_last.json`` (written by the
               daemon's SecurityScanner)
- intelligence: DriftPredictor / AnomalyDetector stores
- proactive:   PatternLearner stats + session history
- v3:          V3DataSource (read-only bridge, gracefully absent)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("friday_v6.web.dashboard")

_HOME = Path.home()
_SECURITY_STATE = _HOME / ".friday" / "v4_security_last.json"
_SESSION_HISTORY = _HOME / ".friday" / "sessions" / "history.jsonl"


def daemon_state() -> dict:
    """Daemon status file + whether the process is currently alive."""
    try:
        from friday_v6.daemon import is_running, read_status
        status = read_status()
        status["running"] = is_running()
        return status
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"daemon probe failed: {exc}")
        return {"running": False, "error": str(exc)}


def security_state() -> dict:
    """Last security scan persisted by the daemon's SecurityScanner."""
    try:
        if _SECURITY_STATE.exists():
            return json.loads(_SECURITY_STATE.read_text())
    except (json.JSONDecodeError, OSError) as exc:  # pragma: no cover
        logger.debug(f"security state unreadable: {exc}")
    return {"scans": 0, "last_error": None, "report": None}


def intelligence_state() -> dict:
    """Drift + anomaly statistics from the Wave 4 stores."""
    out: dict = {"available": False}
    try:
        from friday_v6.intelligence import AnomalyDetector, DriftPredictor
        out.update({
            "available": True,
            "drift": DriftPredictor().get_stats(),
            "anomaly": AnomalyDetector().get_stats(),
        })
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"intelligence probe failed: {exc}")
        out["error"] = str(exc)
    return out


def proactive_state() -> dict:
    """Pattern-learner stats + session history count."""
    out: dict = {"sessions": 0, "available": False}
    try:
        if _SESSION_HISTORY.exists():
            with _SESSION_HISTORY.open() as fh:
                out["sessions"] = sum(1 for _ in fh)
    except OSError:  # pragma: no cover
        pass
    try:
        from friday_v6.proactive.pattern_learner import PatternLearner
        out.update({
            "available": True,
            "patterns": PatternLearner().get_stats(),
        })
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"pattern probe failed: {exc}")
    return out


def v3_state() -> dict:
    """Read-only V3 bridge status + ambient feed (empty when V3 absent)."""
    out: dict = {"available": False}
    try:
        from friday_v6.proactive.v3source import V3DataSource
        src = V3DataSource()
        out["available"] = src.is_available()
        if out["available"]:
            out["counts"] = src.observation_counts(hours=24)
            out["daemon_state"] = src.daemon_state().get("state", "unknown")
            out["ambient_recent"] = src.recent_ambient_events(hours=24,
                                                              limit=12)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"v3 probe failed: {exc}")
    return out


def voice_state() -> dict:
    """Configured voice providers (cheap probe — never loads models)."""
    out: dict = {"available": False}
    try:
        from friday_v6.voice.core import config_from_file
        cfg = config_from_file()
        out.update({
            "available": True,
            "tts_provider": cfg.tts_provider,
            "hotword": cfg.hotword or "disabled",
        })
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"voice probe failed: {exc}")
    return out


#: Marker files that make a directory look like a project.
_PROJECT_MARKERS = (".git", "pyproject.toml", "requirements.txt",
                    "package.json", "Cargo.toml", "go.mod", "Pipfile")


def project_candidates(limit: int = 40) -> list[str]:
    """Directories that look like projects, for the scan-path picker.

    Looks at home-dir children and the current working directory for
    directories containing a git repo or a common manifest file. Cheap,
    guarded, and bounded — these are suggestions, not a filesystem index.
    """
    found: set[str] = set()

    def _is_project(d: Path) -> bool:
        try:
            return any((d / m).exists() for m in _PROJECT_MARKERS)
        except OSError:  # pragma: no cover - defensive
            return False

    roots: list[Path] = [Path.home()]
    try:
        roots.append(Path.cwd())
    except OSError:  # pragma: no cover - cwd deleted
        pass
    for root in roots:
        try:
            if _is_project(root):
                found.add(str(root))
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    if _is_project(child):
                        found.add(str(child))
        except OSError:  # pragma: no cover - defensive
            continue
    return sorted(found)[:limit]


def memory_state() -> dict:
    """Long-term facts + working memory for the dashboard card (Wave 10).

    Reads the typed memory layer (FactMemory + WorkingMemory) over the
    V4 state DB. Guarded like every accessor: a missing DB or subsystem
    yields an empty neutral payload, never a crash.
    """
    out: dict = {"available": False, "facts": [], "working": ""}
    try:
        from friday_v6 import db
        from friday_v6.memory import FactMemory, WorkingMemory
        # Read-only probe: the dashboard must never create the DB or write
        # to it (same contract as every other dashboard accessor). A
        # missing DB fails the connect → guarded → unavailable payload.
        conn = db.connect(read_only=True)
        try:
            facts = FactMemory(conn).recall(limit=10)
            working = WorkingMemory(conn).current_context()
            out.update({
                "available": True,
                "facts": [{
                    "key": f.key, "value": f.value, "source": f.source,
                    "confidence": round(f.confidence, 3),
                } for f in facts],
                "working": working,
            })
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"memory probe failed: {exc}")
    return out


def relationship_state() -> dict:
    """Relationship depth + tone for the dashboard card (Wave 10 §3.3).

    Read-only probe over the V4 state DB: depth (monotonic — recompute
    never drops it), level, tone, verbosity, briefing length, and the
    real interaction signals behind them. Guarded like every accessor:
    a missing DB yields an empty neutral payload, never a crash.
    """
    out: dict = {"available": False}
    try:
        from friday_v6 import db
        from friday_v6.relationship import RelationshipEngine
        conn = db.connect(read_only=True)
        try:
            status = RelationshipEngine(conn).status()
            out.update({"available": True, **status})
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"relationship probe failed: {exc}")
    return out


def skills_state() -> dict:
    """Skills summary by verification state for the dashboard card (Wave 10 §3.4).

    Read-only probe over the V4 state DB. Guarded like every accessor:
    a missing DB yields an empty payload, never a crash.
    """
    out: dict = {"available": False}
    try:
        from friday_v6 import db
        from friday_v6.skills import SkillRegistry
        conn = db.connect(read_only=True)
        try:
            skills = SkillRegistry(conn).list(limit=100000)
            counts: dict[str, int] = {}
            for s in skills:
                counts[s.verification_state] = \
                    counts.get(s.verification_state, 0) + 1
            out.update({
                "available": True,
                "total": len(skills),
                "shadow": counts.get("shadow", 0),
                "verified": counts.get("verified", 0),
                "promoted": counts.get("promoted", 0),
                "demoted": counts.get("demoted", 0),
                "recent": [{
                    "name": s.name,
                    "state": s.verification_state,
                    "confidence": round(s.confidence, 3),
                    "shadow_matches": s.shadow_matches,
                } for s in skills[:6]],
            })
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"skills probe failed: {exc}")
    return out


def ambient_state() -> dict:
    """Recent ambient events (Wave 11) for the dashboard feed.

    Read-only probe over the V4 state DB: the durable queue's latest
    events (security, briefing, system) plus a research/briefing digest.
    Guarded like every accessor — a missing DB yields an empty payload.
    """
    out: dict = {"available": False, "events": []}
    try:
        from friday_v6 import db
        conn = db.connect(read_only=True)
        try:
            events = db.recent_ambient_events(conn, limit=12)
            out.update({
                "available": True,
                "events": [{
                    "topic": e.get("topic"),
                    "payload": e.get("payload"),
                    "priority": e.get("priority"),
                    "when": e.get("created_at"),
                } for e in events],
            })
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"ambient probe failed: {exc}")
    return out


def briefing_state() -> dict:
    """Today's briefing (Wave 11) — real state, tone-adapted."""
    out: dict = {"available": False}
    try:
        from friday_v6 import db
        conn = db.connect(read_only=True)
        try:
            from friday_v6.briefing import build_briefing
            b = build_briefing(conn, kind="morning")
            out.update({"available": True,
                        "text": b.text, "tone": b.tone,
                        "sections": b.sections})
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"briefing probe failed: {exc}")
    return out


def conversation_state(limit: int = 40) -> dict:
    """Today's shared-session exchanges — the browser resumes the thread.

    Wave 15 (One Presence): the dashboard chat is a first-class surface
    of the SAME Friday, so it hydrates from the shared session
    (``surface='shared'``, one per UTC day — the thread the terminal,
    voice, and web chat all append to). A conversation started in the
    terminal continues visibly in the browser. Read-only probe;
    guarded like every accessor — a missing DB yields an empty neutral
    payload, never a crash.
    """
    out: dict = {"available": False, "session_id": None, "exchanges": []}
    try:
        from friday_v6 import db
        conn = db.connect(read_only=True)
        try:
            # Read-only probe: look up today's shared thread WITHOUT
            # creating it (a mode=ro connection cannot INSERT anyway —
            # get_or_create would fail and return a phantom id).
            sid = db.find_shared_session(conn)
            out["session_id"] = sid
            rows = db.session_exchanges(conn, sid, limit=limit) if sid else []
            out["exchanges"] = [
                {"role": r.get("role"), "content": r.get("content"),
                 "intent": r.get("intent", ""),
                 "created_at": r.get("created_at")}
                for r in rows
            ]
            out["available"] = True
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"conversation probe failed: {exc}")
    return out


def capability_state() -> dict:
    """The capability registry summary (Wave 16, Law 7).

    Read-only probe: what Friday can do today — builtins (executors,
    providers, intents, surfaces) + learned skills. Guarded like every
    accessor — a missing DB yields an empty neutral payload.
    """
    out: dict = {"available": False, "total": 0, "by_layer": {},
                 "skills": 0}
    try:
        from friday_v6 import db
        conn = None
        try:
            conn = db.connect(read_only=True)
            from friday_v6.capability import CapabilityRegistry
            summary = CapabilityRegistry(conn).summary()
            out.update({
                "available": True,
                "total": summary.get("total", 0),
                "by_layer": summary.get("by_layer", {}),
                "skills": (summary.get("by_layer") or {}).get("skill", 0),
                "recent_skills": [
                    c.to_dict() for c in
                    CapabilityRegistry(conn).by_layer("skill")[:6]
                ],
            })
        finally:
            if conn is not None:
                conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"capability probe failed: {exc}")
    return out


def autonomy_state() -> dict:
    """The autonomy loop's surface for the dashboard (read-only probe).

    Pending permission asks + the last judgment-cycle report + override
    count, so the operator can see (and approve/deny) what Friday is
    waiting on without the CLI. Guarded like every accessor — a missing
    DB yields an empty neutral payload, never a crash.
    """
    out: dict = {"available": False, "pending": [], "overrides": 0}
    try:
        from friday_v6 import db
        conn = db.connect(read_only=True)
        try:
            pending = db.pending_permission_requests(conn, limit=20)
            out.update({
                "available": True,
                "pending": pending,
                "overrides": len(db.list_overrides(conn)),
            })
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"autonomy probe failed: {exc}")
    return out


def autonomy_approve(request_id: str) -> dict:
    """Approve a pending permission ask (the web "yes")."""
    try:
        from friday_v6.autonomy import AutonomyAgent
        outcome = AutonomyAgent().accept(request_id)
        if not outcome:
            return {"ok": False, "response": "That request is no longer "
                                              "pending."}
        if outcome.get("status") == "succeeded":
            return {"ok": True,
                    "response": f"Done. (audit {outcome.get('action_id')})"}
        if outcome.get("status") == "denied":
            return {"ok": False,
                    "response": "That action needs an explicit override — "
                                "a bare approval isn't enough."}
        return {"ok": False,
                "response": f"That didn't work — {outcome.get('status')}."}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"autonomy approve failed: {exc}")
        return {"ok": False, "response": f"Sorry: {exc}"}


def autonomy_deny(request_id: str) -> dict:
    """Deny a pending permission ask + record an override (the web "no")."""
    try:
        from friday_v6.autonomy import AutonomyAgent
        if AutonomyAgent().deny(request_id, reason="web dashboard"):
            return {"ok": True, "response": "Declined — I won't suggest "
                                             "that again."}
        return {"ok": False, "response": "That request is no longer pending."}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"autonomy deny failed: {exc}")
        return {"ok": False, "response": f"Sorry: {exc}"}


def talk(text: str) -> dict:
    """Route a chat utterance through the Wave 9 NLU brain.

    The web dashboard is a first-class Friday surface, so it gets the
    same command language as voice and ``friday6 talk``: ``resolve`` →
    execute / missions / reasoning. Returns the TalkResult dict, or an
    error payload on any failure (never raises).
    """
    try:
        from friday_v6 import db
        from friday_v6.nl_router import TextCommandHandler
        conn = db.connect()
        try:
            llm = None
            try:
                from friday_v6.nlu import LLMClient
                llm = LLMClient()
            except Exception:
                llm = None
            # §2 hardening: the web chat is a first-class surface — desktop
            # NL works here too (never raises; degrades to an honest msg).
            desktop_handler = None
            try:
                from friday_v6.desktop.wm_abstraction import (
                    desktop_text_command)
                desktop_handler = desktop_text_command
            except Exception:
                desktop_handler = None
            result = TextCommandHandler(conn, llm=llm,
                                        desktop_handler=desktop_handler).handle(
                text, force=False)
        finally:
            conn.close()
        return result.to_dict()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"dashboard talk failed: {exc}")
        return {"action": "failed", "response": f"Sorry: {exc}"}


def overview() -> dict:
    """Aggregated payload for the dashboard's top-level view."""
    return {
        "daemon": daemon_state(),
        "autonomy": autonomy_state(),
        "security": security_state(),
        "intelligence": intelligence_state(),
        "proactive": proactive_state(),
        "memory": memory_state(),
        "relationship": relationship_state(),
        "skills": skills_state(),
        "capability": capability_state(),
        "ambient": ambient_state(),
        "briefing": briefing_state(),
        "v3": v3_state(),
        "voice": voice_state(),
    }
