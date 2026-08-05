"""Hermetic tests for the Wave 9.0 V4 state database (friday_v6.db).

Every test uses a tmp_path DB — never the real ~/.friday. Covers:
schema migrations, mission/step CRUD, action audit log, memory
upsert/recall/forget, relationships, skills, sessions/exchanges, the
read-only connection, db_status inspection, and graceful degradation.
"""

from __future__ import annotations

import pytest

from friday_v6 import db
from friday_v6.db import (
    add_mission_step,
    clear_expired_working,
    clear_working,
    connect,
    count_working,
    create_mission,
    create_skill,
    db_status,
    delete_working_context,
    end_session,
    evict_working_contexts,
    finish_action,
    forget_memory,
    get_mission,
    get_relationship,
    get_session,
    get_skill,
    get_working_context,
    list_memories,
    list_missions,
    list_mission_steps,
    list_relationships,
    list_skills,
    list_working_contexts,
    log_exchange,
    migrate,
    now_iso,
    recall_memory,
    recent_actions,
    recent_exchanges,
    record_action,
    record_skill_shadow_match,
    schema_version,
    session_exchanges,
    set_memory_confidence,
    set_working_context,
    start_session,
    store_memory,
    update_mission,
    update_mission_step,
    update_skill,
    upsert_relationship,
)


# ==========================================================================
# Connection & migrations
# ==========================================================================


class TestConnectAndMigrations:
    def test_connect_creates_db_and_applies_migrations(self, tmp_path):
        path = tmp_path / "v4.db"
        conn = connect(path)
        try:
            assert path.exists()
            assert schema_version(conn) == 10
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()}
            assert {"missions", "mission_steps", "actions", "memories",
                    "relationships", "skills", "sessions", "exchanges",
                    "working_memory", "watches", "desktop_events",
                    "permission_requests", "operator_overrides",
                    "mobile_devices"} <= tables
        finally:
            conn.close()

    def test_migrate_is_idempotent(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            migrate(conn)
            migrate(conn)
            assert schema_version(conn) == 10
        finally:
            conn.close()

    def test_migration_3_adds_skill_shadow_columns(self, tmp_path):
        """Migration 3 adds the shadow-first workflow columns to skills."""
        conn = connect(tmp_path / "v4.db")
        try:
            sid = create_skill(conn, "run-tests-after-edit", steps=[{"op": "run"}])
            assert sid
            skill = get_skill(conn, "run-tests-after-edit")
            assert skill["shadow_matches"] == 0
            assert skill["version"] == 1
            assert record_skill_shadow_match(conn, sid)
            assert get_skill(conn, "run-tests-after-edit")["shadow_matches"] == 1
            assert update_skill(conn, sid, version=2, shadow_matches=5)
            assert get_skill(conn, "run-tests-after-edit")["version"] == 2
            assert get_skill(conn, "run-tests-after-edit")["shadow_matches"] == 5
        finally:
            conn.close()

    def test_read_only_connect_does_not_create_file(self, tmp_path):
        path = tmp_path / "v4.db"
        with pytest.raises(Exception):
            connect(path, read_only=True)  # file missing → sqlite error
        assert not path.exists()

    def test_read_only_connect_reads_existing_db(self, tmp_path):
        path = tmp_path / "v4.db"
        conn = connect(path)
        create_mission(conn, "existing mission")
        conn.close()

        ro = connect(path, read_only=True)
        try:
            rows = list_missions(ro)
            assert rows and rows[0]["title"] == "existing mission"
            # Writes on a read-only connection fail gracefully (guarded).
            assert create_mission(ro, "nope") is None
        finally:
            ro.close()

    def test_connect_uses_default_path_without_arg(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setattr(db, "_DEFAULT_DB", tmp_path / "v4.db")
        conn = connect()
        try:
            assert (tmp_path / "v4.db").exists()
        finally:
            conn.close()

    def test_default_db_path_respects_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_DB", str(tmp_path / "env.db"))
        assert db.default_db_path() == tmp_path / "env.db"


# ==========================================================================
# Missions & steps
# ==========================================================================


class TestMissions:
    def test_create_and_get(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            mid = create_mission(conn, "ship auth refactor",
                                 description="migrate session handling",
                                 priority="high")
            assert mid
            mission = get_mission(conn, mid)
            assert mission["title"] == "ship auth refactor"
            assert mission["status"] == "planned"
            assert mission["priority"] == "high"
        finally:
            conn.close()

    def test_list_and_status_filter(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            create_mission(conn, "a", status="active")
            create_mission(conn, "b", status="completed")
            active = list_missions(conn, status="active")
            assert [m["title"] for m in active] == ["a"]
            assert len(list_missions(conn)) == 2
        finally:
            conn.close()

    def test_update_mission_fields(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            mid = create_mission(conn, "a")
            assert update_mission(conn, mid, status="active", priority="high")
            m = get_mission(conn, mid)
            assert m["status"] == "active"
            assert m["priority"] == "high"
            assert not update_mission(conn, mid)  # no fields → False
        finally:
            conn.close()

    def test_add_and_update_steps(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            mid = create_mission(conn, "a")
            s1 = add_mission_step(conn, mid, "first")
            s2 = add_mission_step(conn, mid, "second")
            steps = list_mission_steps(conn, mid)
            assert [s["title"] for s in steps] == ["first", "second"]
            assert steps[0]["position"] < steps[1]["position"]
            assert update_mission_step(conn, s1, status="completed",
                                       result="done")
            assert list_mission_steps(conn, mid)[0]["status"] == "completed"
        finally:
            conn.close()

    def test_steps_deleted_with_mission(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            mid = create_mission(conn, "a")
            add_mission_step(conn, mid, "step")
            conn.execute("DELETE FROM missions WHERE id = ?", (mid,))
            conn.commit()
            assert list_mission_steps(conn, mid) == []
        finally:
            conn.close()


# ==========================================================================
# Action audit log
# ==========================================================================


class TestActions:
    def test_record_and_finish(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            aid = record_action(conn, "git", goal="stage changes",
                                status="pending", permission_level="confirm",
                                command="git add -A")
            assert aid
            assert finish_action(conn, aid, status="succeeded",
                                 result_code=0, output="ok",
                                 undo_payload={"op": "reset"})
            actions = recent_actions(conn)
            assert len(actions) == 1
            a = actions[0]
            assert a["status"] == "succeeded"
            assert a["result_code"] == 0
            assert a["output"] == "ok"
            assert a["permission_level"] == "confirm"
        finally:
            conn.close()

    def test_recent_actions_filter_and_limit(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            for i in range(5):
                record_action(conn, "git", goal=f"g{i}")
            record_action(conn, "testing", goal="run tests")
            gits = recent_actions(conn, action_type="git")
            assert len(gits) == 5
            assert len(recent_actions(conn, limit=2)) == 2
        finally:
            conn.close()


# ==========================================================================
# Memories
# ==========================================================================


class TestMemories:
    def test_store_recall_upsert(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            mid = store_memory(conn, "operator.name", "Lakshay",
                               source="voice:2026-08-01", confidence=0.95)
            assert mid
            mem = recall_memory(conn, "operator.name")
            assert mem["value"] == "Lakshay"
            assert mem["source"] == "voice:2026-08-01"

            # Upsert keeps the same key (single row), updates value.
            store_memory(conn, "operator.name", "Lakshay S.",
                           source="voice:2026-08-02", confidence=0.98)
            mems = list_memories(conn)
            assert len(mems) == 1
            assert mems[0]["value"] == "Lakshay S."
        finally:
            conn.close()

    def test_upsert_carries_decay_policy(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            store_memory(conn, "pref.lang", "rust", decay_policy="usage")
            store_memory(conn, "pref.lang", "rust", decay_policy="time")
            mem = recall_memory(conn, "pref.lang")
            assert mem["decay_policy"] == "time"  # update clause carries it
        finally:
            conn.close()

    def test_set_memory_confidence_no_usage_touch(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            store_memory(conn, "k", "v", confidence=0.9)
            # Read the raw row — recall_memory() itself touches updated_at
            # as a usage-decay signal, so it can't be used to observe
            # whether the fade touched the row.
            def _updated_at():
                return conn.execute(
                    "SELECT updated_at FROM memories WHERE mem_key = ?",
                    ("k",)).fetchone()[0]
            before = _updated_at()
            assert set_memory_confidence(conn, "k", 0.4)
            # A decay fade must not look like a "use" — updated_at unchanged.
            assert _updated_at() == before
            row = recall_memory(conn, "k")
            assert row["confidence"] == 0.4
            # recall's usage-touch DID change updated_at (the signal works).
            assert _updated_at() != before
            assert set_memory_confidence(conn, "nope", 0.1) is False
        finally:
            conn.close()

    def test_forget_and_prefix_list(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            store_memory(conn, "pref.language", "rust")
            store_memory(conn, "pref.tooling", "python")
            store_memory(conn, "other.x", "1")
            prefs = list_memories(conn, mem_key_prefix="pref")
            assert len(prefs) == 2
            assert forget_memory(conn, "pref.language")
            assert recall_memory(conn, "pref.language") is None
        finally:
            conn.close()


# ==========================================================================
# Relationships
# ==========================================================================


class TestRelationships:
    def test_upsert_creates_and_increments(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            rid = upsert_relationship(conn, "Lakshay", depth=1.0,
                                      tone="warm", preferences={"verbose": True})
            assert rid
            rel = get_relationship(conn, "Lakshay")
            assert rel["depth"] == 1.0
            assert rel["tone"] == "warm"
            assert rel["interaction_count"] == 1

            upsert_relationship(conn, "Lakshay", depth=2.0)
            rel = get_relationship(conn, "Lakshay")
            assert rel["depth"] == 2.0
            assert rel["interaction_count"] == 2
        finally:
            conn.close()

    def test_list_ordered_by_depth(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            upsert_relationship(conn, "b", depth=0.5)
            upsert_relationship(conn, "a", depth=3.0)
            rels = list_relationships(conn)
            assert rels[0]["peer"] == "a"
        finally:
            conn.close()


# ==========================================================================
# Skills
# ==========================================================================


class TestSkills:
    def test_create_get_list(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            sid = create_skill(conn, "run-tests-after-edit",
                               steps=[{"op": "run", "cmd": "pytest"}],
                               confidence=0.4, verification_state="shadow")
            assert sid
            skill = get_skill(conn, "run-tests-after-edit")
            assert skill["verification_state"] == "shadow"
            assert skill["failure_count"] == 0

            assert update_skill(conn, sid, verification_state="verified",
                                confidence=0.9, failure_count=1)
            skill = get_skill(conn, "run-tests-after-edit")
            assert skill["verification_state"] == "verified"
            assert skill["confidence"] == 0.9
            assert skill["failure_count"] == 1
        finally:
            conn.close()

    def test_list_filter_by_state(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            create_skill(conn, "s1", verification_state="shadow")
            create_skill(conn, "s2", verification_state="promoted")
            shadow = list_skills(conn, verification_state="shadow")
            assert [s["name"] for s in shadow] == ["s1"]
        finally:
            conn.close()


# ==========================================================================
# Sessions & exchanges
# ==========================================================================


class TestSessions:
    def test_session_lifecycle_and_exchanges(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            sid = start_session(conn, surface="voice")
            assert sid
            assert get_session(conn, sid)["surface"] == "voice"
            log_exchange(conn, sid, "user", "what's new?", intent="status")
            log_exchange(conn, sid, "friday", "3 repos changed")
            ex = session_exchanges(conn, sid)
            assert [e["role"] for e in ex] == ["user", "friday"]
            assert ex[0]["intent"] == "status"
            assert end_session(conn, sid)
            assert get_session(conn, sid)["ended_at"] is not None
        finally:
            conn.close()

    def test_recent_exchanges_spans_sessions_newest_first(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            sid1 = start_session(conn, surface="voice")
            sid2 = start_session(conn, surface="cli")
            log_exchange(conn, sid1, "user", "first")
            log_exchange(conn, sid2, "user", "second")
            rows = recent_exchanges(conn)
            assert [r["content"] for r in rows] == ["second", "first"]
            assert len(recent_exchanges(conn, limit=1)) == 1
        finally:
            conn.close()


# ==========================================================================
# Inspection & degradation
# ==========================================================================


class TestDbStatus:
    def test_db_status_missing_file(self, tmp_path):
        info = db_status(tmp_path / "nope.db")
        assert info["exists"] is False
        assert info["schema_version"] == 0
        assert info["tables"] == {}
        assert info["total_rows"] == 0

    def test_db_status_counts(self, tmp_path):
        path = tmp_path / "v4.db"
        conn = connect(path)
        create_mission(conn, "a")
        record_action(conn, "git")
        store_memory(conn, "k", "v")
        conn.close()

        info = db_status(path)
        assert info["exists"] is True
        assert info["schema_version"] == 10
        assert info["tables"]["missions"] == 1
        assert info["tables"]["actions"] == 1
        assert info["tables"]["memories"] == 1
        assert info["tables"]["watches"] == 0
        assert info["tables"]["desktop_events"] == 0
        assert info["tables"]["permission_requests"] == 0
        assert info["tables"]["operator_overrides"] == 0
        assert info["tables"]["mobile_devices"] == 0
        assert info["total_rows"] >= 3

    def test_db_status_corrupt_file(self, tmp_path):
        path = tmp_path / "v4.db"
        path.write_text("not a database")
        info = db_status(path)
        assert info["exists"] is True  # file exists but unreadable
        # Should not raise; reports whatever it could (empty counts ok).
        assert "tables" in info

    def test_helpers_degrade_on_missing_table(self, tmp_path):
        """A dropped table must degrade gracefully, never crash."""
        path = tmp_path / "v4.db"
        conn = connect(path)
        conn.execute("DROP TABLE IF EXISTS missions")
        conn.commit()
        try:
            assert list_missions(conn) == []          # guarded read
            assert create_mission(conn, "x") is None  # guarded write → None
        finally:
            conn.close()

    def test_helpers_recover_after_migrate(self, tmp_path):
        """Migrate re-applies a dropped schema when the version marker is
        rewound (migrate skips already-applied versions by design), then
        helpers work again."""
        path = tmp_path / "v4.db"
        conn = connect(path)
        conn.execute("DROP TABLE IF EXISTS missions")
        conn.execute("PRAGMA user_version = 0")  # rewind → v1 reapplies
        conn.commit()
        migrate(conn)
        mid = create_mission(conn, "recovered")
        assert mid is not None
        assert get_mission(conn, mid)["title"] == "recovered"
        conn.close()


class TestWorkingMemory:
    def test_set_get_upsert(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            wid = set_working_context(conn, "current_task", "Refactoring auth",
                                      priority=3, source="planner")
            assert wid
            row = get_working_context(conn, "current_task")
            assert row["value"] == "Refactoring auth"
            assert row["priority"] == 3
            assert row["source"] == "planner"
            assert row["category"] == "working"
            assert row["ttl_seconds"] == 3600

            # Upsert replaces value, keeps single row.
            set_working_context(conn, "current_task", "Refactoring auth v2",
                                priority=4)
            rows = list_working_contexts(conn)
            assert len(rows) == 1
            assert rows[0]["value"] == "Refactoring auth v2"
            assert rows[0]["priority"] == 4
        finally:
            conn.close()

    def test_expiry_with_injected_now(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            set_working_context(conn, "task", "x", ttl_seconds=3600,
                                now="2026-08-01T10:00:00+00:00")
            assert count_working(conn) == 1
            # Before expiry → nothing removed.
            assert clear_expired_working(conn,
                                         now="2026-08-01T10:30:00+00:00") == 0
            assert count_working(conn) == 1
            # After expiry → removed.
            assert clear_expired_working(conn,
                                         now="2026-08-01T12:00:00+00:00") == 1
            assert count_working(conn) == 0
        finally:
            conn.close()

    def test_priority_ordering(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            set_working_context(conn, "low", "1", priority=0)
            set_working_context(conn, "high", "2", priority=4)
            set_working_context(conn, "mid", "3", priority=2)
            rows = list_working_contexts(conn)
            assert [r["context_key"] for r in rows] == ["high", "mid", "low"]
        finally:
            conn.close()

    def test_evict_and_clear(self, tmp_path):
        conn = connect(tmp_path / "v4.db")
        try:
            for i in range(6):
                set_working_context(conn, f"k{i}", str(i), priority=i)
            # Evict down to 3 → removes the 3 lowest-priority entries.
            assert evict_working_contexts(conn, max_entries=3) == 3
            assert count_working(conn) == 3
            remaining = [r["context_key"] for r in list_working_contexts(conn)]
            assert "k5" in remaining and "k0" not in remaining

            assert delete_working_context(conn, "k5")
            assert count_working(conn) == 2
            assert clear_working(conn) == 2
            assert count_working(conn) == 0
        finally:
            conn.close()


class TestNowIso:
    def test_iso_format(self):
        ts = now_iso()
        assert "T" in ts and ts.endswith("Z") or "+00:00" in ts
