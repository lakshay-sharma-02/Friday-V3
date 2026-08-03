"""Relationship depth — how close Friday and the operator are (Wave 10 §3.3).

Depth is computed from *real interaction data*, never guessed:

- conversation volume (user exchanges logged in ``exchanges``)
- session count (how often the operator comes back)
- mission completions (trust: the operator lets Friday track work)
- disclosed facts (memory facts = trust through disclosure)

Monotonicity law: *more interaction → deeper, never suddenly shallower*.
The computed depth is clamped to the stored value, so a data hiccup or
an empty query can never fake a falling-out; depth only ever rises as
the operator keeps working with Friday. Tone/verbosity selection lives
in :mod:`friday_v4.relationship.tones`.

Usage::

    engine = RelationshipEngine(conn)
    status = engine.refresh()      # recompute from real data + persist
    status = engine.status()       # read-only view (depth/level/tone)

Hermetic: takes a ``conn``; no real ``~/.friday`` access in tests.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from .. import db
from .tones import ToneDirection, ToneSelector

logger = logging.getLogger("friday_v4.relationship.depth")

#: Default peer — the single-operator relationship.
DEFAULT_PEER = "operator"

#: Depth levels — a vocabulary for the operator-facing status/CLI.
_DEPTH_LEVELS: list[tuple[float, str]] = [
    (0.00, "stranger"),
    (0.15, "acquaintance"),
    (0.40, "familiar"),
    (0.65, "partner"),
    (0.85, "confidant"),
]


def level_name(depth: float) -> str:
    """A human level label for a depth score (0..1)."""
    label = "stranger"
    for threshold, name in _DEPTH_LEVELS:
        if depth >= threshold:
            label = name
    return label


def compute_depth(exchanges: int, sessions: int,
                  missions_completed: int, facts: int) -> float:
    """Depth from raw interaction signals, asymptotic toward 1.0.

    Each signal grows logistically (never a cliff): conversation volume
    matters most (0.45 weight), mission trust next (0.30), disclosed
    facts last (0.25). All inputs are cumulative counters, so the score
    is naturally non-decreasing as the relationship accumulates.
    """
    conv = 1.0 - math.exp(-max(0, exchanges) / 200.0)
    trust = 1.0 - math.exp(-max(0, missions_completed) / 10.0)
    disclosure = 1.0 - math.exp(-max(0, facts) / 15.0)
    raw = 0.45 * conv + 0.30 * trust + 0.25 * disclosure
    return round(min(1.0, raw), 3)


class RelationshipEngine:
    """Computes + persists the operator relationship from real data.

    Signals come from the V4 state DB (exchanges, sessions, missions,
    memories) via the typed ``db`` helpers — every read is guarded, so a
    missing table or DB degrades to zero signals, never a crash.
    """

    def __init__(self, conn, peer: str = DEFAULT_PEER) -> None:
        self._conn = conn
        self._peer = peer
        self._tones = ToneSelector()

    # ------------------------------------------------------------------
    # Signals (real interaction data)
    # ------------------------------------------------------------------

    def signals(self) -> dict:
        """Raw cumulative interaction counters from the V4 DB."""
        exchanges = 0
        sessions = 0
        missions_completed = 0
        facts = 0
        try:
            exchanges = db.count_exchanges(self._conn, role="user")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"exchange count failed: {exc}")
        try:
            sessions = len(db.list_sessions(self._conn, limit=100000) or [])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"session count failed: {exc}")
        try:
            missions = db.list_missions(self._conn, limit=100000) or []
            missions_completed = sum(
                1 for m in missions if m.get("status") == "completed")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"mission count failed: {exc}")
        try:
            facts = len(db.list_memories(self._conn, limit=100000) or [])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"fact count failed: {exc}")
        return {
            "exchanges": exchanges,
            "sessions": sessions,
            "missions_completed": missions_completed,
            "facts": facts,
        }

    # ------------------------------------------------------------------
    # Depth
    # ------------------------------------------------------------------

    def _stored_depth(self) -> float:
        try:
            row = db.get_relationship(self._conn, self._peer)
            return float(row.get("depth", 0.0)) if row else 0.0
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"stored depth read failed: {exc}")
            return 0.0

    def compute(self, signals: Optional[dict] = None) -> float:
        """Depth from current signals, clamped to never drop (monotonic)."""
        s = signals or self.signals()
        computed = compute_depth(
            s.get("exchanges", 0), s.get("sessions", 0),
            s.get("missions_completed", 0), s.get("facts", 0))
        # Monotonicity law: never suddenly shallower. The stored depth
        # only ever rises; a zero-signal read cannot reset the
        # relationship.
        return round(max(computed, self._stored_depth()), 3)

    def refresh(self) -> dict:
        """Recompute depth from real data and persist (returns status).

        Preserves any explicit tone-direction (Wave 17): the depth-derived
        tone column is refreshed, but the stored direction stays intact
        so "be more casual" survives daemon sweeps until the operator
        says otherwise.
        """
        signals = self.signals()
        depth = self.compute(signals)
        tone = self._tones.tone_for(depth)
        try:
            db.upsert_relationship(
                self._conn, self._peer, depth=depth, tone=tone,
                interaction_delta=0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"relationship persist failed: {exc}")
        # NB: the ``tone`` arg is deliberately NOT passed to status() —
        # status() computes the *effective* tone (explicit direction
        # wins over depth), and passing the depth tone here would clobber
        # a stored direction on every daemon refresh.
        return self.status(signals=signals, depth=depth)

    # ------------------------------------------------------------------
    # Tone direction (Wave 17 — adaptive identity)
    # ------------------------------------------------------------------

    def direction(self) -> Optional[ToneDirection]:
        """The stored explicit tone-direction, or None."""
        try:
            raw = db.get_tone_direction(self._conn, self._peer)
            return ToneDirection.from_dict(raw)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"tone direction read failed: {exc}")
            return None

    def set_direction(self, tone: Optional[str] = None,
                      verbosity: Optional[int] = None,
                      request: str = "") -> dict:
        """Persist an explicit tone-direction; returns the new status.

        ``tone`` must be one of :data:`DIRECTION_TONES` (or None to keep
        the depth default), ``verbosity`` 1..5. ``request`` is the
        operator's exact words, kept verbatim so Friday can explain why.
        Never raises — a failure returns the current status unchanged.
        """
        try:
            db.set_tone_direction(self._conn, self._peer, tone=tone,
                                  verbosity=verbosity, request=request)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"tone direction persist failed: {exc}")
        return self.status()

    def clear_direction(self) -> dict:
        """Remove the explicit direction ("be yourself again")."""
        try:
            db.clear_tone_direction(self._conn, self._peer)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"tone direction clear failed: {exc}")
        return self.status()

    def explain_tone(self) -> str:
        """Why Friday talks the way she does — the explainable identity.

        Wave 17 MCU test #4: "be more casual" must be explainable.
        With a direction: cites the operator's request + date. Without:
        honest — tone adapts to relationship depth (never a hidden
        hardcoded personality).
        """
        direction = self.direction()
        status = self.status()
        tone = status.get("tone", "neutral")
        if direction is not None and direction.request:
            when = direction.set_at[:10] if direction.set_at else "recently"
            return (f"I talk {tone} because you asked me to on {when}: "
                    f"\"{direction.request}\"")
        if direction is not None:
            return (f"I talk {tone} because you asked me to.")
        depth = status.get("depth", 0.0)
        return (f"My tone adapts to how close we've become "
                f"(relationship depth {depth:.2f}). "
                f"Say 'be more casual' and I'll shift it.")

    # ------------------------------------------------------------------
    # View
    # ------------------------------------------------------------------

    def status(self, signals: Optional[dict] = None,
               depth: Optional[float] = None,
               tone: Optional[str] = None) -> dict:
        """The operator-facing relationship view (never raises).

        Wave 17: when an explicit tone-direction is stored, the reported
        ``tone``/``verbosity`` are the *effective* values (direction wins
        over depth) and the raw direction is included so every surface
        can show + explain it.
        """
        s = signals or self.signals()
        d = depth if depth is not None else self.compute(s)
        direction = self.direction()
        t = tone or self._tones.effective_tone(d, direction)
        verbosity = self._tones.effective_verbosity(d, direction)
        return {
            "peer": self._peer,
            "depth": d,
            "level": level_name(d),
            "tone": t,
            "verbosity": verbosity,
            "briefing": self._tones.briefing_length(d),
            "tone_direction": None if direction is None else {
                "tone": direction.tone,
                "verbosity": direction.verbosity,
                "request": direction.request,
                "set_at": direction.set_at,
            },
            "signals": s,
        }


__all__ = ["DEFAULT_PEER", "RelationshipEngine", "compute_depth",
           "level_name"]
