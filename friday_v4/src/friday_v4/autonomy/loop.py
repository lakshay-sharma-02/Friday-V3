"""AutonomyAgent — Friday's judgment → action loop (Wave: Agent Core).

The always-on presence's *doing* side. On every cycle it gathers
candidates from Friday's own layers (promoted-skill dispatch offers and
active-mission next steps), judges each through the shared permission
gate, and then either executes (AUTO), asks durably (CONFIRM), or
respects NEVER — and it remembers operator overrides so a declined
action is never proposed again.

Flow of one cycle (``cycle_once``):

    candidates = dispatch offers + mission next-steps
    for each candidate:
        if operator override matches → skip (the operator said no/differently)
        level = gate.classify(action_type, command)
        AUTO     → execute silently through gate → sandbox → audit
        CONFIRM  → if no pending ask already → create durable permission
                   request + raise notification (resolved later by the
                   operator's "yes, run it" / `friday4 autonomy approve`)
        NEVER    → skip (never autonomously; operator force only)

Design laws (same as every daemon component):
- Never crashes: any failure logs and is skipped (the daemon law).
- Hermetic: ``db_path``, ``notify``, ``bus``, ``gate``, ``dispatcher``,
  ``missions`` are injectable so tests use a tmp_path DB + fakes.
- The operator's words always win: declines are persisted as overrides
  (``db.record_override``) and block re-proposal until cleared.

Usage:
    agent = AutonomyAgent(interval=300.0, db_path=...)
    agent.start()          # background thread, cycles every interval
    ...
    agent.accept(req_id)   # operator said "yes, run it"
    agent.deny(req_id)     # operator said "no" / "do it differently"
    agent.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.autonomy.loop")

#: A pending permission request older than this expires without an answer
#: (stale asks don't pile up in the web/talk pending list).
_DEFAULT_ASK_TTL_SECONDS = 24 * 3600


def _offer_json(offer: dict) -> str:
    """The noticer offer compactly serialized for the durable ask's goal.

    ``accept()`` re-hydrates the pattern (sequence + example evidence
    trail) from this JSON so forming the skill doesn't depend on the
    audit log having shifted since the offer was made. Never raises.
    """
    import json

    try:
        return json.dumps({
            "sequence": offer.get("sequence") or [],
            "count": offer.get("count", 0),
            "context": offer.get("context") or "",
            "first": offer.get("first") or "",
            "example": offer.get("example") or {},
        }, default=str)
    except Exception:
        return "{}"


def _learn_key(offer: dict) -> str:
    """A stable, neutral dedup key for a learn offer.

    The raw first signature (e.g. ``git:push origin main``) must NOT
    ride in the ask's command — the permission gate would classify it
    NEVER through its dangerous-pattern matcher and the offer would be
    silently skipped. A short hash keeps dedup/override matching exact
    per-pattern while the human text stays in the description and the
    full pattern in the goal JSON. Never raises.
    """
    import hashlib

    first = (offer.get("first") or "").strip()
    try:
        digest = hashlib.sha1(first.encode("utf-8")).hexdigest()[:12]
    except Exception:
        digest = "000000000000"
    return f"learn:{digest}"


def _promote_key(skill) -> str:
    """A stable, neutral dedup key for a promote ask.

    The skill name must NOT ride raw in the command — the gate's
    destructive-pattern matcher would classify a ``deploy-push``-named
    skill's promote ask as NEVER and it would be silently skipped. The
    name/id still live in the description + goal JSON for humans and
    resolution. Never raises.
    """
    import hashlib

    try:
        digest = hashlib.sha1(str(skill.id).encode("utf-8")).hexdigest()[:12]
    except Exception:
        digest = "000000000000"
    return f"promote:{digest}"

@dataclass
class AutonomyOutcome:
    """One candidate's disposition in a cycle."""

    source: str                     # dispatch | mission
    description: str
    action_type: str
    command: str
    disposition: str                # executed | asked | skipped_override | skipped_never | skipped_pending | noop
    action_id: Optional[str] = None
    request_id: Optional[str] = None
    detail: str = ""


@dataclass
class AutonomyResult:
    """One full cycle's report (also used for status)."""

    cycle: int = 0
    outcomes: list[AutonomyOutcome] = field(default_factory=list)
    executed: int = 0
    asked: int = 0
    skipped: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle,
            "executed": self.executed,
            "asked": self.asked,
            "skipped": self.skipped,
            "last_error": self.last_error,
            "outcomes": [
                {
                    "source": o.source,
                    "description": o.description,
                    "action_type": o.action_type,
                    "command": o.command,
                    "disposition": o.disposition,
                    "action_id": o.action_id,
                    "request_id": o.request_id,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }


class AutonomyAgent:
    """Periodic judgment → action loop, wired into the daemon."""

    def __init__(self, interval: float = 300.0,
                 db_path=None,
                 conn=None,
                 notify: Optional[Callable[..., bool]] = None,
                 bus=None,
                 gate=None,
                 dispatcher=None,
                 missions=None,
                 ask_ttl_seconds: float = _DEFAULT_ASK_TTL_SECONDS,
                 max_asks: int = 3,
                 idle_seconds: float = 300.0,
                 learn: bool = True,
                 promote: bool = True) -> None:
        self.interval = interval
        self._db_path = db_path
        self._conn = conn              # injected connection (talk/web/voice/CLI)
        self._notify = notify          # injectable notifier (tests); default desktop
        self._bus = bus                # shared ambient bus (durable events)
        self._gate = gate              # PermissionGate (shared, pure)
        self._dispatcher = dispatcher  # SkillDispatcher (injectable)
        self._missions = missions      # MissionEngine (injectable)
        self._ask_ttl_seconds = ask_ttl_seconds
        self._max_asks = max_asks
        #: The busy gate: CONFIRM asks are only raised when the operator
        #: has been idle for at least this long (or no activity is
        #: recorded yet). AUTO work still runs — it is read-only and
        #: never interrupts anything.
        self._idle_seconds = idle_seconds
        #: Self-learn (offer repeated patterns as skills+missions) and
        #: self-develop (offer verified-skill promotion) toggles — the
        #: daemon exposes them as autonomy_learn / autonomy_promote.
        self._learn_enabled = learn
        self._promote_enabled = promote
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[AutonomyResult] = None
        self._cycles = 0

    # ── Gate / layers (lazy, injectable) ──────────────────────────────

    def _get_gate(self):
        if self._gate is None:
            try:
                from ..execution.gate import PermissionGate
                self._gate = PermissionGate()
            except Exception:
                self._gate = False
        return self._gate

    def _get_conn(self):
        """The connection to use: an injected one wins (talk/web/voice/
        CLI surfaces pass theirs so resolution hits the same DB the ask
        was created on); otherwise open our own by path."""
        if self._conn is not None:
            return self._conn
        try:
            from .. import db
            return db.connect(path=self._db_path)
        except Exception as exc:
            logger.debug(f"autonomy db unavailable: {exc}")
            return None

    def _close_conn(self, conn) -> None:
        """Close only connections we opened — injected ones belong to the
        caller (the nl_router / CLI / voice surface owns its DB)."""
        if conn is None or conn is self._conn:
            return
        try:
            conn.close()
        except Exception:
            pass

    # ── Candidate gathering (Friday's own judgment inputs) ────────────

    def _dispatch_candidates(self, conn) -> list[dict]:
        """Promoted-skill next steps on context match (read-only)."""
        try:
            if self._dispatcher is None:
                from ..skills import SkillDispatcher
                self._dispatcher = SkillDispatcher(conn)
            suggestions = self._dispatcher.suggest(limit=self._max_asks)
        except Exception as exc:
            logger.debug(f"autonomy dispatch gather failed: {exc}")
            return []
        out: list[dict] = []
        for s in suggestions:
            steps = s.get("next_steps") or []
            if not steps:
                continue
            first = steps[0]
            atype = (first.get("action_type") or "").strip()
            if not atype:
                continue
            out.append({
                "source": "dispatch",
                "description": (f"{s.get('skill_name')}: "
                                f"{first.get('command') or atype}"),
                "action_type": atype,
                "command": (first.get("command") or "").strip(),
                "goal": f"skill '{s.get('skill_name')}' next step",
            })
        return out

    def _mission_candidates(self, conn) -> list[dict]:
        """Active missions' next executable step (read-only)."""
        try:
            if self._missions is None:
                from ..missions import MissionEngine
                self._missions = MissionEngine(conn)
            missions = self._missions.list(status="active", limit=5)
        except Exception as exc:
            logger.debug(f"autonomy mission gather failed: {exc}")
            return []
        out: list[dict] = []
        for mission in missions:
            try:
                step = self._missions.next_step(mission.id)
            except Exception:
                step = None
            if step is None or not getattr(step, "is_executable", False):
                continue
            atype = (step.action_type or "").strip()
            if not atype:
                continue
            out.append({
                "source": "mission",
                "description": (f"mission '{mission.title}': "
                                f"{step.command or atype}"),
                "action_type": atype,
                "command": (step.command or "").strip(),
                "goal": f"mission '{mission.title}' step",
                "mission_id": mission.id,
                "step_id": step.id,
            })
        return out

    def _learn_candidates(self, conn) -> list[dict]:
        """Repeated patterns → "I noticed you keep doing X" offers.

        The self-learn input: the RepetitionNoticer reads the audit log
        for ordered sequences the operator repeats, and each NEW pattern
        (not already a skill) becomes a CONFIRM-level candidate — the
        operator's "yes" forms a shadow skill AND a mission from the
        pattern, so Friday turns its own observations into reusable work.
        """
        if not self._learn_enabled:
            return []
        try:
            from ..skills import RepetitionNoticer
            noticer = RepetitionNoticer(conn)
            offers = noticer.notice(limit=self._max_asks)
        except Exception as exc:
            logger.debug(f"autonomy learn gather failed: {exc}")
            return []
        out: list[dict] = []
        for offer in offers:
            seq = offer.get("sequence") or []
            if not seq:
                continue
            first = offer.get("first") or seq[0]
            # The neutral key (``learn:<hash>``) dedups/overrides; the raw
            # signature never rides in ``command`` so the gate can't
            # mis-classify the offer as NEVER (destructive-pattern
            # matcher). The full offer (sequence + example evidence
            # trail) rides in ``goal`` so accept() can form a rich skill
            # without re-reading the log.
            out.append({
                "source": "learn",
                "description": offer.get("offer") or
                               (f"I noticed a repeated pattern "
                                f"({first}) — form a skill for it?"),
                "action_type": "skill",
                "command": _learn_key(offer),
                "goal": _offer_json(offer),
                "cwd": (offer.get("example") or {}).get("cwd") or "",
                "offer": offer,
            })
        return out

    def _promotion_candidates(self, conn) -> list[dict]:
        """Verified skills → ask to promote (self-develop input).

        A skill reaches ``verified`` automatically once it has enough
        shadow matches (the shadow-first law) — but promotion is always
        operator-approved. This candidate surfaces that approval as a
        normal durable ask instead of a CLI-only step, closing the loop:
        learn → verify → promote → dispatch.
        """
        if not self._promote_enabled:
            return []
        try:
            from ..skills import SkillRegistry
            reg = SkillRegistry(conn)
            verified = reg.list(verification_state="verified",
                                limit=self._max_asks)
        except Exception as exc:
            logger.debug(f"autonomy promote gather failed: {exc}")
            return []
        import json

        out: list[dict] = []
        for skill in verified:
            # The command is a neutral hash (same rationale as
            # ``_learn_key``): a user-named skill like ``deploy-push``
            # must not let the gate's destructive-pattern matcher
            # classify the *promote* ask as NEVER. The skill id rides in
            # the goal JSON — ``_ask_permission`` only persists schema
            # columns, so it survives resolution through ``goal``.
            out.append({
                "source": "promote",
                "description": (f"Skill '{skill.name}' has "
                                f"{skill.shadow_matches} shadow matches — "
                                f"promote it so I can offer it proactively?"),
                "action_type": "skill",
                "command": _promote_key(skill),
                "goal": json.dumps({"skill_id": skill.id,
                                     "skill_name": skill.name}),
            })
        return out

    # ── The busy gate (idle detection) ────────────────────────────────

    def _is_operator_busy(self, conn) -> bool:
        """True when the operator has been active recently.

        CONFIRM asks never interrupt active work — Friday waits for an
        idle moment (or the operator's own ``what's pending``/``yes``).
        ``None`` (no activity recorded) is treated as idle: there is
        nothing to interrupt. Never raises.
        """
        try:
            from datetime import datetime, timezone
            from .. import db
            last = db.last_activity_at(conn)
            if not last:
                return False
            last_dt = datetime.fromisoformat(last)
            now = datetime.now(timezone.utc)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            return (now - last_dt).total_seconds() < self._idle_seconds
        except Exception as exc:
            logger.debug(f"autonomy idle probe failed: {exc}")
            return False

    # ── The cycle ─────────────────────────────────────────────────────

    def cycle_once(self) -> AutonomyResult:
        """One judgment pass; returns the outcome report (never raises)."""
        result = AutonomyResult(cycle=self._cycles)
        self._cycles += 1
        gate = self._get_gate()
        if gate is False or not gate:
            result.last_error = "permission gate unavailable"
            self.last_result = result
            return result

        conn = self._get_conn()
        if conn is None:
            result.last_error = "state DB unavailable"
            self.last_result = result
            return result
        try:
            try:
                from .. import db
                candidates = [
                    *self._dispatch_candidates(conn),
                    *self._mission_candidates(conn),
                    *self._learn_candidates(conn),
                    *self._promotion_candidates(conn),
                ]
            except Exception as exc:
                result.last_error = str(exc)
                self.last_result = result
                return result

            # Expire stale asks first so the pending list stays honest.
            try:
                from datetime import datetime, timedelta, timezone
                cutoff = (datetime.now(timezone.utc)
                          - timedelta(seconds=self._ask_ttl_seconds)
                          ).isoformat(timespec="microseconds")
                db.expire_permission_requests(conn, cutoff)
            except Exception as exc:
                logger.debug(f"autonomy expiry failed: {exc}")

            seen_asks = 0
            for cand in candidates:
                outcome = self._judge_candidate(conn, gate, cand)
                result.outcomes.append(outcome)
                if outcome.disposition == "executed":
                    result.executed += 1
                elif outcome.disposition == "asked":
                    result.asked += 1
                    seen_asks += 1
                    if seen_asks >= self._max_asks:
                        break
                elif outcome.disposition in ("skipped_override",
                                             "skipped_never",
                                             "skipped_pending",
                                             "noop"):
                    result.skipped += 1
            self.last_result = result
            return result
        finally:
            self._close_conn(conn)

    def _judge_candidate(self, conn, gate, cand: dict) -> AutonomyOutcome:
        """Decide + act on one candidate (execute / ask / skip)."""
        from .. import db
        atype = cand.get("action_type") or ""
        cmd = cand.get("command") or ""

        # 1. Operator overrides win — the operator said no / differently.
        try:
            if db.is_overridden(conn, atype, cmd):
                return AutonomyOutcome(
                    source=cand.get("source", "?"),
                    description=cand.get("description", ""),
                    action_type=atype, command=cmd,
                    disposition="skipped_override",
                    detail="operator override recorded")
        except Exception:
            pass

        # 2. Judge through the gate (the permission system).
        try:
            level = gate.classify(atype, cmd)
        except Exception as exc:
            return AutonomyOutcome(
                source=cand.get("source", "?"),
                description=cand.get("description", ""),
                action_type=atype, command=cmd,
                disposition="noop", detail=f"gate error: {exc}")

        if level.value == "auto":
            return self._execute_silently(conn, cand)
        if level.value == "never":
            # NEVER work is never autonomous — operator force only.
            return AutonomyOutcome(
                source=cand.get("source", "?"),
                description=cand.get("description", ""),
                action_type=atype, command=cmd,
                disposition="skipped_never",
                detail="never-level action — operator override only")
        # CONFIRM → durable permission request (deduped while pending).
        try:
            for req in db.pending_permission_requests(conn, limit=50):
                if (req.get("action_type") == atype
                        and req.get("command") == cmd):
                    return AutonomyOutcome(
                        source=cand.get("source", "?"),
                        description=cand.get("description", ""),
                        action_type=atype, command=cmd,
                        disposition="skipped_pending",
                        request_id=req.get("id"),
                        detail="already asked — waiting on the operator")
        except Exception:
            pass
        # The busy gate: CONFIRM asks never interrupt active work — the
        # operator is typing/coding, so the ask waits for an idle moment
        # (or their own "what's pending" / "yes, run it"). AUTO work
        # already ran above; it is read-only and never interrupts.
        if self._is_operator_busy(conn):
            return AutonomyOutcome(
                source=cand.get("source", "?"),
                description=cand.get("description", ""),
                action_type=atype, command=cmd,
                disposition="skipped_busy",
                detail="operator is active — waiting for an idle moment")
        return self._ask_permission(conn, cand)

    def _execute_silently(self, conn, cand: dict) -> AutonomyOutcome:
        """AUTO-level work runs by itself — gated, sandboxed, audited."""
        atype = cand.get("action_type") or ""
        cmd = cand.get("command") or ""
        try:
            from ..execution import execute
            result = execute(atype, cmd, cwd=cand.get("cwd") or None,
                             conn=conn, confirm_fn=None, force=False,
                             goal=cand.get("goal") or cand.get("description"))
        except Exception as exc:
            return AutonomyOutcome(
                source=cand.get("source", "?"),
                description=cand.get("description", ""),
                action_type=atype, command=cmd,
                disposition="noop", detail=f"execution failed: {exc}")
        status = getattr(result, "status", "failed")
        return AutonomyOutcome(
            source=cand.get("source", "?"),
            description=cand.get("description", ""),
            action_type=atype, command=cmd,
            disposition="executed",
            action_id=getattr(result, "action_id", None),
            detail=f"{status}: {getattr(result, 'output', '')[:80]}")

    def _ask_permission(self, conn, cand: dict) -> AutonomyOutcome:
        """CONFIRM work becomes a durable permission request + a notify."""
        from .. import db
        atype = cand.get("action_type") or ""
        cmd = cand.get("command") or ""
        description = cand.get("description") or f"{atype}: {cmd}"
        try:
            rid = db.create_permission_request(
                conn, description, atype, command=cmd,
                cwd=cand.get("cwd") or "",
                goal=cand.get("goal") or description,
                source=cand.get("source", "autonomy"),
                mission_id=cand.get("mission_id"),
                step_id=cand.get("step_id"))
        except Exception as exc:
            return AutonomyOutcome(
                source=cand.get("source", "?"),
                description=description, action_type=atype, command=cmd,
                disposition="noop", detail=f"ask failed: {exc}")
        if not rid:
            return AutonomyOutcome(
                source=cand.get("source", "?"),
                description=description, action_type=atype, command=cmd,
                disposition="noop", detail="could not create permission ask")
        self._raise_ask(description)
        return AutonomyOutcome(
            source=cand.get("source", "?"),
            description=description, action_type=atype, command=cmd,
            disposition="asked", request_id=rid,
            detail="permission requested — say 'yes, run it'")

    # ── Self-learn / self-develop resolution ─────────────────────────

    def _accept_learn(self, conn, req: dict) -> dict:
        """The operator said yes to "I noticed you keep doing X".

        Forms a shadow skill from the pattern (self-learn) AND creates a
        mission from its steps (self-develop — the learned pattern
        becomes work the loop drives). Never executes anything. Returns
        an outcome dict with skill_id/mission_id; never raises.
        """
        import json

        try:
            payload = json.loads(req.get("goal") or "{}")
        except Exception:
            payload = {}
        try:
            from ..skills import ReplayExecutor
            replay = ReplayExecutor(conn)
            skill = replay.learn_one(payload) if payload else None
            skill_id = skill.id if skill else None
        except Exception as exc:
            logger.debug(f"autonomy learn form failed: {exc}")
            skill_id = None
        mission_id = None
        try:
            seq = payload.get("sequence") or []
            if seq:
                from ..missions import MissionEngine
                from ..missions.planner import StepPlan
                example = payload.get("example") or {}
                cwd = (example.get("cwd") or "").strip() or \
                      (req.get("cwd") or "").strip()
                steps: list[StepPlan] = []
                for sig in seq:
                    atype, _, cmd = sig.partition(":")
                    atype = (atype or "").strip()
                    if not atype:
                        continue
                    steps.append(StepPlan(
                        title=cmd.strip() or atype,
                        action_type=atype,
                        command=cmd.strip(),
                        cwd=cwd or None))
                if steps:
                    engine = MissionEngine(conn)
                    mission = engine.create(
                        title=(f"learned: {payload.get('first') or seq[0]}"),
                        goal=("auto-created from a repeated pattern you "
                              "approved me to learn"),
                        plan=steps, cwd=cwd or None, schedule=True)
                    if mission:
                        engine.start(mission.id)
                        mission_id = mission.id
        except Exception as exc:
            logger.debug(f"autonomy learn mission failed: {exc}")
        if skill_id:
            detail = (f"formed shadow skill '{payload.get('first') or skill_id}'"
                      + (f" + mission" if mission_id else ""))
        else:
            detail = (f"mission started from the learned pattern"
                      if mission_id else "nothing to learn from that pattern")
        return {"status": "succeeded" if (skill_id or mission_id) else "failed",
                "output": detail, "skill_id": skill_id,
                "mission_id": mission_id}

    def _accept_promote(self, conn, req: dict) -> dict:
        """The operator approved promoting a verified skill.

        Promotion is the operator-approval step of the shadow-first
        lifecycle (verified → promoted) — the CLI's ``promote`` path
        reached through a normal durable ask. Never executes anything.
        The skill id is read from the ask's goal JSON (encoded by
        ``_promotion_candidates``) with a direct ``skill_id`` fallback
        for injected callers.
        """
        import json

        try:
            from ..skills import SkillRegistry
            reg = SkillRegistry(conn)
            skill_id = req.get("skill_id") or ""
            if not skill_id:
                try:
                    payload = json.loads(req.get("goal") or "{}")
                    skill_id = (payload or {}).get("skill_id") or ""
                except Exception:
                    skill_id = ""
            if not skill_id:
                return {"status": "failed", "error": "no skill id"}
            ok = reg.promote(skill_id)
            if not ok:
                return {"status": "failed",
                        "error": "skill is not verified / promotable"}
            skill = reg.get_by_id(skill_id)
            return {"status": "succeeded",
                    "output": f"promoted skill '{skill.name if skill else skill_id}'",
                    "skill_id": skill_id}
        except Exception as exc:
            logger.debug(f"autonomy promote failed: {exc}")
            return {"status": "failed", "error": str(exc)}

    # ── Mission auto-advance ─────────────────────────────────────────

    def _advance_mission(self, conn, req: dict, outcome: dict) -> None:
        """Complete an approved mission step and evaluate the next one.

        The operator's "yes" approved one step. Friday completes it in
        the mission engine and immediately looks at the next step in the
        same cycle: AUTO-level next steps run silently right away;
        CONFIRM-level next steps become the next durable permission ask;
        NEVER-level steps are left for the operator's explicit override.
        Never raises — a missing mission/step just ends the advance
        quietly (the next cycle re-evaluates).
        """
        try:
            from .. import db as _db
            from ..missions import MissionEngine
            engine = MissionEngine(conn)
            mission = engine.get(req.get("mission_id"))
            if not mission:
                return
            # Mark the approved step completed (idempotent).
            try:
                if req.get("step_id"):
                    _db.update_mission_step(
                        conn, req["step_id"],
                        status="completed",
                        result=(outcome.get("output") or "")[:500])
            except Exception as exc:
                logger.debug(f"autonomy step complete failed: {exc}")
            # Evaluate the next step in the same cycle.
            try:
                nxt = engine.next_step(mission.id)
            except Exception:
                nxt = None
            if nxt is None or not getattr(nxt, "is_executable", False):
                engine._maybe_finish(mission.id)
                return
            atype = (nxt.action_type or "").strip()
            if not atype:
                engine._maybe_finish(mission.id)
                return
            try:
                gate = self._get_gate()
                level = gate.classify(atype, nxt.command or "") if gate else None
            except Exception:
                level = None
            if level is not None and getattr(level, "value", "") == "auto":
                # AUTO next step → run silently in the same cycle. The
                # step is only marked completed when it actually
                # succeeded — a failed sandbox run must not be recorded
                # as done (it stays pending for the next cycle).
                try:
                    from ..execution import execute
                    res = execute(atype, nxt.command or "",
                                  cwd=nxt.cwd or None,
                                  conn=conn, confirm_fn=None, force=False,
                                  goal=f"mission '{mission.title}' step")
                    status = getattr(res, "status", "failed")
                    if status == "succeeded":
                        try:
                            _db.update_mission_step(
                                conn, nxt.id, status="completed",
                                result="auto-advanced by autonomy")
                        except Exception:
                            pass
                        engine._maybe_finish(mission.id)
                except Exception as exc:
                    logger.debug(f"autonomy auto-advance failed: {exc}")
            elif level is not None and getattr(level, "value", "") != "never":
                # CONFIRM next step → the next durable ask (the operator
                # approves it like the previous one). The busy gate is
                # intentionally NOT consulted here: the operator just
                # approved interactively, so they are present — asking
                # about the immediate next step is natural, not an
                # interruption.
                try:
                    _db.create_permission_request(
                        conn,
                        f"mission '{mission.title}': {nxt.command or atype}",
                        atype, command=nxt.command or "",
                        cwd=nxt.cwd or "",
                        goal=f"mission '{mission.title}' step",
                        source="mission",
                        mission_id=mission.id, step_id=nxt.id)
                except Exception as exc:
                    logger.debug(f"autonomy next-ask failed: {exc}")
            # NEVER-level next steps are never proposed — the loop's
            # judge already skips them on the next cycle.
        except Exception as exc:
            logger.debug(f"autonomy mission advance failed: {exc}")

    # ── Resolution (the operator answered) ────────────────────────────

    def accept(self, request_id: str, force: bool = False) -> Optional[dict]:
        """The operator approved an ask — run it through the real gate.

        The operator's "yes" IS the CONFIRM approval (passed as a
        pre-approved confirm_fn). ``force`` stays the caller's: NEVER
        steps (push/deploy) remain blocked without an explicit override.
        Returns the execution outcome dict, or None when unresolvable.
        """
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            from .. import db
            req = db.get_permission_request(conn, request_id)
            if not req or req.get("status") != "pending":
                return None
            atype = req.get("action_type") or ""
            cmd = req.get("command") or ""
            # The gate is the permission system here too: a NEVER-level
            # action is never run by a bare "yes" — only by an explicit
            # force override. Check it BEFORE calling execute so the
            # safety law holds even if a caller substitutes its own
            # execution layer (as tests do).
            try:
                gate = self._get_gate()
                level = gate.classify(atype, cmd) if gate else None
            except Exception:
                level = None
            if level is not None and getattr(level, "value", "") == "never" \
                    and not force:
                db.resolve_permission_request(conn, request_id, "denied")
                return {"status": "denied", "request_id": request_id,
                        "error": "never-level action needs an explicit "
                                  "operator override (force)"}
            # Self-learn / self-develop asks are resolved inside Friday's
            # own layers (skills + missions) — the "yes" isn't an
            # execution command, it's approval to form/promote. These
            # never touch the executor.
            if req.get("source") == "learn":
                out = self._accept_learn(conn, req)
                db.resolve_permission_request(
                    conn, request_id,
                    "approved" if out.get("status") == "succeeded"
                    else "denied")
                out["request_id"] = request_id
                return out
            if req.get("source") == "promote":
                out = self._accept_promote(conn, req)
                db.resolve_permission_request(
                    conn, request_id,
                    "approved" if out.get("status") == "succeeded"
                    else "denied")
                out["request_id"] = request_id
                return out
            try:
                from ..execution import execute
                result = execute(
                    atype, cmd, cwd=req.get("cwd") or None, conn=conn,
                    confirm_fn=lambda _d: True, force=force,
                    goal=req.get("goal") or req.get("description"))
            except Exception as exc:
                logger.warning(f"autonomy accept execute failed: {exc}")
                db.resolve_permission_request(conn, request_id, "denied")
                return {"status": "failed", "error": str(exc),
                        "request_id": request_id}
            status = getattr(result, "status", "failed")
            db.resolve_permission_request(
                conn, request_id,
                "approved" if status == "succeeded" else "denied")
            out = {
                "status": status,
                "action_id": getattr(result, "action_id", None),
                "output": getattr(result, "output", ""),
                "request_id": request_id,
            }
            # Mission auto-advance: the approved step of a mission is
            # completed and the next step is evaluated in the same cycle
            # (AUTO next steps run immediately; CONFIRM next steps become
            # the next durable ask). The mission progresses on its own
            # after the operator says yes once.
            if status == "succeeded" and (req.get("mission_id")
                                          or req.get("step_id")):
                self._advance_mission(conn, req, out)
            return out
        finally:
            self._close_conn(conn)

    def deny(self, request_id: str, reason: str = "operator declined") -> bool:
        """The operator declined — resolve the ask and record an override.

        Recording the override is what makes Friday *learn* from the
        operator: the same action_type (or command) is never proposed
        again until the operator clears it.
        """
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            from .. import db
            req = db.get_permission_request(conn, request_id)
            if not req or req.get("status") != "pending":
                return False
            db.resolve_permission_request(conn, request_id, "denied")
            db.record_override(conn, req.get("action_type") or "",
                               req.get("command") or "",
                               reason=reason, source="autonomy.deny")
            return True
        except Exception as exc:
            logger.warning(f"autonomy deny failed: {exc}")
            return False
        finally:
            self._close_conn(conn)

    def pending(self, limit: int = 20) -> list[dict]:
        """Open permission asks (web/talk/CLI surface)."""
        conn = self._get_conn()
        if conn is None:
            return []
        try:
            from .. import db
            return db.pending_permission_requests(conn, limit=limit)
        except Exception:
            return []
        finally:
            self._close_conn(conn)

    # ── Notification ──────────────────────────────────────────────────

    def _raise_ask(self, description: str) -> None:
        """Notify + publish the ask (both guarded, never raise)."""
        try:
            notifier = self._notify or self._default_notify
            notifier("Friday · Asking permission",
                     f"May I {description}?  (say 'yes, run it' or "
                     f"'friday4 autonomy approve' to allow)",
                     urgency="normal", timeout_ms=15000)
        except Exception as exc:
            logger.debug(f"autonomy ask notification failed: {exc}")
        if self._bus is None:
            return
        try:
            from ..ambient import Event, Priority
            self._bus.publish(Event(
                topic="permission",
                payload=f"May I {description}?",
                priority=Priority.IMPORTANT,
                source="daemon.autonomy"))
        except Exception as exc:
            logger.debug(f"autonomy ambient publish failed: {exc}")

    def _default_notify(self, title: str, message: str,
                        urgency: str = "normal",
                        timeout_ms: Optional[int] = None) -> bool:
        try:
            from ..desktop.wm_abstraction import DesktopAbstraction
            return DesktopAbstraction.notify(title, message, urgency=urgency,
                                             timeout_ms=timeout_ms)
        except Exception as exc:
            logger.debug(f"autonomy notification failed: {exc}")
            return False

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-autonomy", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First cycle shortly after start so the loop is live fast.
        try:
            self.cycle_once()
        except Exception:
            logger.debug("autonomy first cycle failed", exc_info=True)
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.cycle_once()
            except Exception:
                logger.debug("autonomy cycle error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


__all__ = ["AutonomyAgent", "AutonomyResult", "AutonomyOutcome",
           "_DEFAULT_ASK_TTL_SECONDS"]
