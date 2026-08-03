"""Hermetic tests for the Wave 10 relationship layer (friday_v4.relationship).

Covers:
- depth.py: compute_depth from real interaction signals, monotonicity
  (more interaction → deeper, NEVER suddenly shallower), level_name
- tones.py: tone/verbosity/briefing mapping by depth with morning
  brevity (the MCU "kept the briefing to 3 lines" behavior)
- engine wiring: RelationshipEngine.refresh() persists via the
  relationships table; status() reports depth/level/tone/signals
- persona wiring: IdentityEngine.profile() tone adapts via relationship
  depth, never hardcoded
- db helpers: count_exchanges / list_sessions

Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

from friday_v4 import db
from friday_v4.relationship import (
    RelationshipEngine,
    ToneSelector,
    briefing_length,
    compute_depth,
    level_name,
    tone_for,
    verbosity_for,
)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# depth.py — compute_depth + level_name
# ==========================================================================


class TestComputeDepth:
    def test_zero_signals_is_zero(self):
        assert compute_depth(0, 0, 0, 0) == 0.0

    def test_more_interaction_deeper(self):
        low = compute_depth(10, 2, 0, 1)
        high = compute_depth(500, 40, 8, 20)
        assert high > low

    def test_every_signal_contributes(self):
        base = compute_depth(100, 10, 0, 0)
        with_missions = compute_depth(100, 10, 5, 0)
        with_facts = compute_depth(100, 10, 0, 5)
        assert with_missions > base
        assert with_facts > base

    def test_capped_at_one(self):
        assert compute_depth(10**9, 10**9, 10**9, 10**9) <= 1.0

    def test_never_negative(self):
        assert compute_depth(-5, 0, 0, 0) >= 0.0

    def test_monotonic_in_each_axis(self):
        """More interaction on ANY axis never lowers depth."""
        prev = compute_depth(10, 5, 2, 3)
        assert compute_depth(11, 5, 2, 3) >= prev
        assert compute_depth(10, 6, 2, 3) >= prev
        assert compute_depth(10, 5, 3, 3) >= prev
        assert compute_depth(10, 5, 2, 4) >= prev


class TestLevelName:
    def test_levels_ordered(self):
        # Thresholds (from depth.py): 0.15 acquaintance, 0.40 familiar,
        # 0.65 partner, 0.85 confidant.
        assert level_name(0.0) == "stranger"
        assert level_name(0.30) == "acquaintance"
        assert level_name(0.55) == "familiar"
        assert level_name(0.75) == "partner"
        assert level_name(0.90) == "confidant"
        assert level_name(1.0) == "confidant"

    def test_unknown_depth_bounds(self):
        assert level_name(-1) == "stranger"


# ==========================================================================
# tones.py — tone/verbosity/briefing by depth + morning brevity
# ==========================================================================


class TestTones:
    def test_tone_gradual_by_depth(self):
        assert tone_for(0.0) == "neutral"
        assert tone_for(0.4) == "warm"
        assert tone_for(0.8) == "friendly"
        assert tone_for(1.0) == "close"

    def test_verbosity_bounds(self):
        for d in (0.0, 0.2, 0.5, 0.8, 1.0):
            v = verbosity_for(d)
            assert 1 <= v <= 5
        assert verbosity_for(1.0) == 5
        assert verbosity_for(0.0) == 1

    def test_briefing_length_afternoon(self):
        assert briefing_length(0.0) == "short"
        assert briefing_length(0.5) == "standard"
        assert briefing_length(0.9) == "detailed"

    def test_morning_brevity_shortens(self):
        # MCU: "You're not a morning person — I kept the briefing short."
        assert briefing_length(0.9, hour=9) == "standard"  # detailed → standard
        assert briefing_length(0.5, hour=8) == "short"     # standard → short
        assert briefing_length(0.0, hour=6) == "short"     # already short
        # Afternoon keeps full length.
        assert briefing_length(0.9, hour=14) == "detailed"

    def test_tone_selector_describe(self):
        sel = ToneSelector()
        d = sel.describe(0.8)
        assert d["tone"] == "friendly"
        assert d["verbosity"] == 4
        assert d["briefing"] == "detailed"


# ==========================================================================
# RelationshipEngine — signals, monotonic refresh, persistence
# ==========================================================================


class TestRelationshipEngine:
    def test_signals_from_real_data(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            sid = db.start_session(conn, surface="cli")
            db.log_exchange(conn, sid, "user", "call me Lakshay")
            db.log_exchange(conn, sid, "user", "I prefer Rust")
            db.create_mission(conn, "ship it", status="completed")
            db.store_memory(conn, "operator.name", "Lakshay",
                            source="talk", confidence=0.9)
            s = RelationshipEngine(conn).signals()
            assert s["exchanges"] == 2
            assert s["sessions"] >= 1
            assert s["missions_completed"] == 1
            assert s["facts"] == 1
        finally:
            conn.close()

    def test_refresh_persists_and_is_monotonic(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = RelationshipEngine(conn)
            status1 = engine.refresh()
            assert status1["depth"] >= 0.0
            # More interaction → deeper (or equal), never shallower.
            sid = db.start_session(conn, surface="cli")
            for i in range(60):
                db.log_exchange(conn, sid, "user", f"turn {i}")
            status2 = engine.refresh()
            assert status2["depth"] >= status1["depth"]
            assert status2["signals"]["exchanges"] >= status1["signals"]["exchanges"]
        finally:
            conn.close()

    def test_depth_never_drops_after_data_removal(self, tmp_path):
        """Monotonicity law: even if the log is wiped, depth stays put."""
        conn = _conn(tmp_path)
        try:
            engine = RelationshipEngine(conn)
            sid = db.start_session(conn, surface="cli")
            for i in range(50):
                db.log_exchange(conn, sid, "user", f"turn {i}")
            deep = engine.refresh()["depth"]
            assert deep > 0.0
            # Wipe the conversation log — depth must NOT drop.
            db._execute(conn, "DELETE FROM exchanges")
            status = engine.status()
            assert status["depth"] == deep
        finally:
            conn.close()

    def test_status_shape(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            status = RelationshipEngine(conn).status()
            assert set(status) >= {"peer", "depth", "level", "tone",
                                   "verbosity", "briefing", "signals"}
            assert status["peer"] == "operator"
        finally:
            conn.close()

    def test_refresh_writes_relationship_row(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            RelationshipEngine(conn).refresh()
            row = db.get_relationship(conn, "operator")
            assert row is not None
            assert "depth" in row and "tone" in row
        finally:
            conn.close()


# ==========================================================================
# db helpers (count_exchanges / list_sessions)
# ==========================================================================


class TestDbHelpers:
    def test_count_exchanges_scoped(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            sid = db.start_session(conn)
            db.log_exchange(conn, sid, "user", "hello")
            db.log_exchange(conn, sid, "friday", "hi!")
            assert db.count_exchanges(conn) == 2
            assert db.count_exchanges(conn, role="user") == 1
            assert db.count_exchanges(conn, role="friday") == 1
        finally:
            conn.close()

    def test_list_sessions(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            db.start_session(conn, surface="voice")
            db.start_session(conn, surface="cli")
            sessions = db.list_sessions(conn)
            assert len(sessions) == 2
            assert {s["surface"] for s in sessions} == {"voice", "cli"}
        finally:
            conn.close()


# ==========================================================================
# Persona wiring — tone adapts via relationship depth (never hardcoded)
# ==========================================================================


class TestPersonaWiring:
    def test_identity_profile_tone_from_relationship(self, tmp_path):
        from friday_v4.persona import IdentityEngine
        conn = _conn(tmp_path)
        try:
            # Deep relationship (lots of exchanges) → warm tone, not default.
            sid = db.start_session(conn, surface="cli")
            for i in range(80):
                db.log_exchange(conn, sid, "user", f"turn {i}")
            RelationshipEngine(conn).refresh()
            profile = IdentityEngine(conn).profile()
            assert profile["tone"] != "default"
        finally:
            conn.close()

    def test_identity_profile_default_without_relationship(self, tmp_path):
        from friday_v4.persona import IdentityEngine
        conn = _conn(tmp_path)
        try:
            profile = IdentityEngine(conn).profile()
            assert profile["tone"] in ("default", "neutral")
        finally:
            conn.close()
