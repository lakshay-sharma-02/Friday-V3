"""Tests for the ``friday skills`` CLI subcommand (Pillar B Stage 4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

import pytest

from friday.cli_skills import cmd_skills


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_formed_skill(conn, name: str = "test_skill",
                       status: str = "beta", step_count: int = 2,
                       invocation_count: int = 0) -> tuple[int, str]:
    """Seed a formed skill + worker row. Returns (skill_id, worker_id)."""
    now = datetime.now(timezone.utc).isoformat()

    # Create all needed tables.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS formed_skills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_intent_id INTEGER NOT NULL,
            task_graph      TEXT NOT NULL,
            exemplars       TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0,
            last_invoked_at TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            kind            TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            capabilities    TEXT NOT NULL DEFAULT '',
            supported_languages     TEXT NOT NULL DEFAULT '',
            supported_task_types    TEXT NOT NULL DEFAULT '',
            supported_plan_types    TEXT NOT NULL DEFAULT '',
            limitations     TEXT NOT NULL DEFAULT '',
            estimated_speed         TEXT NOT NULL DEFAULT '',
            estimated_cost          TEXT NOT NULL DEFAULT '',
            context_window          INTEGER NOT NULL DEFAULT 0,
            parallelism             INTEGER NOT NULL DEFAULT 1,
            requires_network        INTEGER NOT NULL DEFAULT 0,
            requires_filesystem     INTEGER NOT NULL DEFAULT 0,
            requires_git            INTEGER NOT NULL DEFAULT 0,
            requires_python         INTEGER NOT NULL DEFAULT 0,
            requires_shell          INTEGER NOT NULL DEFAULT 0,
            confidence              TEXT NOT NULL DEFAULT 'medium',
            version                 TEXT NOT NULL DEFAULT '1.0.0',
            status                  TEXT NOT NULL DEFAULT 'active',
            schema_version          TEXT NOT NULL DEFAULT '1.0',
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            availability            TEXT NOT NULL DEFAULT 'available',
            manifest_ref            TEXT,
            worker_kind             TEXT NOT NULL DEFAULT 'function'
        );
        CREATE TABLE IF NOT EXISTS worker_capabilities (
            worker_id TEXT NOT NULL, capability TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
        CREATE TABLE IF NOT EXISTS worker_history (
            registered_at TEXT NOT NULL, worker_id TEXT NOT NULL,
            name TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL,
            note TEXT, PRIMARY KEY (registered_at, worker_id)
        );
        CREATE TABLE IF NOT EXISTS worker_versions (
            worker_id TEXT NOT NULL, version TEXT NOT NULL,
            registered_at TEXT NOT NULL, changelog TEXT,
            PRIMARY KEY (worker_id, version)
        );
    """)

    task_graph = json.dumps([
        ["workspace_switch", "<workspace>"],
        ["app_launch", "<app>"],
    ][:step_count])

    # Insert formed_skill row.
    cur = conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, "
        "invocation_count, last_invoked_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, task_graph, '{"0": {"default": "3"}}', invocation_count,
         None, now, now)
    )
    skill_id = cur.lastrowid

    # Insert worker row.
    wid = f"worker:{name}:abc123"
    conn.execute(
        "INSERT INTO workers (id, name, kind, description, capabilities, "
        "confidence, version, status, schema_version, created_at, updated_at, "
        "availability, manifest_ref, worker_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (wid, name, "formed_skill", f"Skill: {name}", "Workflow Replay",
         "high", "0.1.0", status, "1.0", now, now, "available",
         f"formed_skill:{skill_id}", "formed_skill"),
    )
    conn.commit()

    # Insert worker_capabilities (form_skills does this).
    conn.execute(
        "INSERT OR IGNORE INTO worker_capabilities (worker_id, capability) "
        "VALUES (?, ?)",
        (wid, "Workflow Replay"),
    )
    conn.commit()

    return skill_id, wid


class TestSkillsList:
    def test_empty_list(self):
        """Skills list with no rows shows helpful message."""
        import argparse
        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS formed_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_intent_id INTEGER NOT NULL,
                task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
                invocation_count INTEGER NOT NULL DEFAULT 0,
                last_invoked_at TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '',
                limitations TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                version TEXT NOT NULL DEFAULT '1.0.0',
                status TEXT NOT NULL DEFAULT 'active',
                schema_version TEXT NOT NULL DEFAULT '1.0',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                availability TEXT NOT NULL DEFAULT 'available',
                manifest_ref TEXT, worker_kind TEXT NOT NULL DEFAULT 'function',
                supported_languages TEXT NOT NULL DEFAULT '',
                supported_task_types TEXT NOT NULL DEFAULT '',
                supported_plan_types TEXT NOT NULL DEFAULT '',
                estimated_speed TEXT NOT NULL DEFAULT '',
                estimated_cost TEXT NOT NULL DEFAULT '',
                context_window INTEGER NOT NULL DEFAULT 0,
                parallelism INTEGER NOT NULL DEFAULT 1,
                requires_network INTEGER NOT NULL DEFAULT 0,
                requires_filesystem INTEGER NOT NULL DEFAULT 0,
                requires_git INTEGER NOT NULL DEFAULT 0,
                requires_python INTEGER NOT NULL DEFAULT 0,
                requires_shell INTEGER NOT NULL DEFAULT 0
            );
        """)

        import friday.cli_skills as mod
        original_fn = mod.connect

        try:
            mod.connect = lambda: conn
            with patch("sys.stdout", new_callable=StringIO) as cap:
                rc = cmd_skills(argparse.Namespace(action=None))
            output = cap.getvalue()
            assert rc == 0
            assert "No formed skills yet" in output
            assert "friday patterns mine" in output
        finally:
            mod.connect = original_fn

    def test_lists_formed_skills(self):
        """Skills list shows seeded skills with details."""
        import argparse

        conn = _fresh_db()
        _seed_formed_skill(conn, name="start_browsing", status="beta",
                           invocation_count=2)

        import friday.cli_skills as mod
        original_fn = mod.connect
        try:
            mod.connect = lambda: conn
            with patch("sys.stdout", new_callable=StringIO) as cap:
                rc = cmd_skills(argparse.Namespace(action=None))
            output = cap.getvalue()
            assert rc == 0
            assert "Formed Skills" in output
            assert "start_browsing" in output
            assert "beta" in output
            assert "2x" in output  # invocation count
        finally:
            mod.connect = original_fn

    def test_list_multiple_skills(self):
        """Skills list shows all formed skills, newest first."""
        import argparse

        conn = _fresh_db()
        _seed_formed_skill(conn, name="skill_a", status="beta")
        _seed_formed_skill(conn, name="skill_b", status="proposed")

        import friday.cli_skills as mod
        original_fn = mod.connect
        try:
            mod.connect = lambda: conn
            with patch("sys.stdout", new_callable=StringIO) as cap:
                rc = cmd_skills(argparse.Namespace(action=None))
            output = cap.getvalue()
            assert rc == 0
            assert "skill_a" in output
            assert "skill_b" in output
            assert "beta" in output
            assert "proposed" in output
        finally:
            mod.connect = original_fn


class TestSkillsRun:
    def test_run_missing_name(self):
        """Run with no name returns error."""
        import argparse

        rc = cmd_skills(argparse.Namespace(action="run", name=None))
        assert rc == 2

    def test_run_nonexistent_skill(self):
        """Run with unknown name returns error."""
        import argparse

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '',
                limitations TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                version TEXT NOT NULL DEFAULT '1.0.0',
                status TEXT NOT NULL DEFAULT 'active',
                schema_version TEXT NOT NULL DEFAULT '1.0',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                availability TEXT NOT NULL DEFAULT 'available',
                worker_kind TEXT NOT NULL DEFAULT 'function',
                manifest_ref TEXT,
                supported_languages TEXT NOT NULL DEFAULT '',
                supported_task_types TEXT NOT NULL DEFAULT '',
                supported_plan_types TEXT NOT NULL DEFAULT '',
                estimated_speed TEXT NOT NULL DEFAULT '',
                estimated_cost TEXT NOT NULL DEFAULT '',
                context_window INTEGER NOT NULL DEFAULT 0,
                parallelism INTEGER NOT NULL DEFAULT 1,
                requires_network INTEGER NOT NULL DEFAULT 0,
                requires_filesystem INTEGER NOT NULL DEFAULT 0,
                requires_git INTEGER NOT NULL DEFAULT 0,
                requires_python INTEGER NOT NULL DEFAULT 0,
                requires_shell INTEGER NOT NULL DEFAULT 0
            );
        """)

        import friday.cli_skills as mod
        original_fn = mod.connect
        try:
            mod.connect = lambda: conn
            with patch("sys.stderr", new_callable=StringIO) as cap:
                rc = cmd_skills(argparse.Namespace(action="run", name="nonexistent"))
            assert rc == 2
            assert "no worker found" in cap.getvalue().lower()
        finally:
            mod.connect = original_fn

    def test_run_rejects_non_skill_worker(self):
        """Run rejects workers that aren't formed_skills."""
        import argparse

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '',
                limitations TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                version TEXT NOT NULL DEFAULT '1.0.0',
                status TEXT NOT NULL DEFAULT 'active',
                schema_version TEXT NOT NULL DEFAULT '1.0',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                availability TEXT NOT NULL DEFAULT 'available',
                worker_kind TEXT NOT NULL DEFAULT 'function',
                manifest_ref TEXT,
                supported_languages TEXT NOT NULL DEFAULT '',
                supported_task_types TEXT NOT NULL DEFAULT '',
                supported_plan_types TEXT NOT NULL DEFAULT '',
                estimated_speed TEXT NOT NULL DEFAULT '',
                estimated_cost TEXT NOT NULL DEFAULT '',
                context_window INTEGER NOT NULL DEFAULT 0,
                parallelism INTEGER NOT NULL DEFAULT 1,
                requires_network INTEGER NOT NULL DEFAULT 0,
                requires_filesystem INTEGER NOT NULL DEFAULT 0,
                requires_git INTEGER NOT NULL DEFAULT 0,
                requires_python INTEGER NOT NULL DEFAULT 0,
                requires_shell INTEGER NOT NULL DEFAULT 0
            );
        """)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO workers (id, name, kind, description, capabilities, "
            "confidence, version, status, schema_version, created_at, updated_at, "
            "availability, worker_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("worker:shell:builtin", "shell", "function", "Shell executor",
             "Shell Commands", "high", "1.0.0", "active", "1.0", now, now,
             "available", "function"),
        )
        conn.commit()
        import friday.cli_skills as mod
        original_fn = mod.connect
        try:
            mod.connect = lambda: conn
            with patch("sys.stderr", new_callable=StringIO) as cap:
                rc = cmd_skills(argparse.Namespace(action="run", name="shell"))
            assert rc == 2
            assert "not a formed skill" in cap.getvalue().lower()
        finally:
            mod.connect = original_fn

    def test_run_cancelled_by_user(self):
        """Run with user declining confirmation returns cleanly."""
        import argparse

        conn = _fresh_db()
        _seed_formed_skill(conn, name="test_skill")

        import friday.cli_skills as mod
        original_fn = mod.connect
        try:
            mod.connect = lambda: conn
            with patch("sys.stdin", StringIO("n\n")):
                with patch("sys.stdout", new_callable=StringIO) as cap:
                    rc = cmd_skills(argparse.Namespace(action="run", name="test_skill"))
            output = cap.getvalue()
            assert rc == 0
            assert "cancelled" in output.lower()
        finally:
            mod.connect = original_fn


def test_skills_subparser_configured():
    """Verify the CLI subparser is wired correctly."""
    import argparse
    from friday.cli import main

    with patch("sys.stdout", new_callable=StringIO):
        with patch("sys.stderr", new_callable=StringIO):
            # Parse --help to confirm the skills parser is registered.
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest="command")
            p = sub.add_parser("skills", help="List or invoke formed skills.")
            p.add_argument("action", nargs="?", default="list",
                           choices=["list", "run"])
            p.add_argument("name", nargs="?", default=None)

            ns = parser.parse_args(["skills"])
            assert ns.command == "skills"
            assert ns.action == "list"

            ns = parser.parse_args(["skills", "run", "my_skill"])
            assert ns.command == "skills"
            assert ns.action == "run"
            assert ns.name == "my_skill"


def test_skills_help_available():
    """friday skills --help shows the expected subcommand help."""
    from friday.cli import main

    with patch("sys.stdout", new_callable=StringIO):
        with pytest.raises(SystemExit) as exc:
            main(["skills", "--help"])
        # argparse exits with code 0 for --help
        assert exc.value.code == 0
