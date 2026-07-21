"""Regression tests for verified dogfooding defects (DOGFOOD_REPORT.md).

Each test pins one reported defect so it cannot silently return.

Defects covered:
  1. knowledge explain closes DB before history query  (HIGH)
  2. runtime_show ignores session id                  (HIGH)
  3. runtime_export prints sessions instead of JSON    (HIGH)
  4. worker capability normalization rejects valid caps(MEDIUM)
  5. quoted schedule ids leak quotes into plan id      (MEDIUM)
  6. ask maturity question returns no evidence         (MEDIUM)
  7. duplicate relationship lines in presentation      (LOW)
  8. plan evidence:0 despite existing knowledge        (LOW)
  9. strategy platform repeats a repo name 3x          (LOW)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from friday import cli as cli_mod
from friday.cli_knowledge import cmd_knowledge_explain
from friday.db import connect, upsert_repository
from friday.worker.models import validate_capabilities, is_valid_capability
from friday.planning.derive import plan, Evidence
from friday.ask import ask
from friday.strategy import strategy_platform

from tests.conftest import skip_unless_live
from friday.knowledge.store import get_all_knowledge


# ---------------------------------------------------------------------------
# Shared seeding
# ---------------------------------------------------------------------------


def _seed_repo(conn, name, *, readme, commit_count=10):
    """Maturity is derived from the README 'Maturity:' line by the ingest
    pipeline; we pass it inline so build_identity/reports see it."""
    return upsert_repository(
        conn, name=name, path=f"/{name.lower()}", default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-01-01",
        remote_url=None, commit_count=commit_count, readme_summary=readme,
        license=None, primary_author=None,
    )


def _build_knowledge(conn):
    from friday.knowledge import KnowledgeEngine, evolve
    from friday.db import atomic
    with atomic(conn):
        KnowledgeEngine(conn).build()
        evolve(conn)


# --- Defect 1: knowledge explain closed-DB crash ---------------------------


def test_knowledge_explain_keeps_connection_until_history(tmp_path, monkeypatch, capsys):
    """The connection must stay open through the history query (no traceback)."""
    db = tmp_path / "kb.db"
    monkeypatch.setenv("FRIDAY_DB", str(db))

    conn = connect(db)
    _seed_repo(conn, "Aether",
               readme="Purpose:\nAether is an AI operating system.\nMaturity: Stable",
               commit_count=20)
    _build_knowledge(conn)
    conn.close()

    conn = connect(db)
    k = get_all_knowledge(conn)[0]
    conn.close()
    args = type("A", (), {"id": k.id, "knowledge_id": None, "verbose": False})()
    rc = cmd_knowledge_explain(args)
    assert rc == 0
    assert "History:" in capsys.readouterr().out


# --- Defects 2 & 3: runtime_show / runtime_export ---------------------------


def _run_runtime(tmp_path, monkeypatch, capsys):
    """Ingest a repo and run the full runtime pipeline to get a real session."""
    monkeypatch.setenv("FRIDAY_DB", str(tmp_path / "kb.db"))
    conn = connect(tmp_path / "kb.db")
    _seed_repo(conn, "Aether",
               readme="Purpose:\nAether is an AI operating system.\nMaturity: Stable",
               commit_count=20)
    conn.close()
    capsys.readouterr()  # drain
    rc = cli_mod.cmd_runtime(type("A", (), {"goal": ["Add logout to Aether"]})())
    assert rc == 0
    out = capsys.readouterr().out
    sid = out.split("Runtime session:")[1].split()[0].strip()
    return sid


@skip_unless_live
def test_runtime_show_filters_by_session_id(tmp_path, monkeypatch, capsys):
    sid1 = _run_runtime(tmp_path, monkeypatch, capsys)
    # A second real session to prove the filter excludes the other one.
    capsys.readouterr()
    rc = cli_mod.cmd_runtime(type("A", (), {"goal": ["Refactor auth in Aether"]})())
    assert rc == 0
    out = capsys.readouterr().out
    sid2 = out.split("Runtime session:")[1].split()[0].strip()
    assert sid1 != sid2

    capsys.readouterr()
    rc = cli_mod.cmd_runtime_show(type("A", (), {"session_id": sid1, "id": None})())
    assert rc == 0
    out = capsys.readouterr().out
    assert sid1 in out
    assert sid2 not in out


@skip_unless_live
def test_runtime_show_unknown_id_errors(tmp_path, monkeypatch, capsys):
    _run_runtime(tmp_path, monkeypatch, capsys)
    capsys.readouterr()
    rc = cli_mod.cmd_runtime_show(
        type("A", (), {"session_id": "sess:missing", "id": None})())
    assert rc == 2


@skip_unless_live
def test_runtime_export_is_json(tmp_path, monkeypatch, capsys):
    _run_runtime(tmp_path, monkeypatch, capsys)
    capsys.readouterr()
    rc = cli_mod.cmd_runtime_export(type("A", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip().startswith("{")  # JSON, not the session list
    assert "session_count" in out


# --- Defect 4: capability normalization -------------------------------------


def test_capability_aliases_survive():
    caps = validate_capabilities(["Shell", "Git", "File System"])
    assert "Shell Commands" in caps
    assert "Git Operations" in caps
    assert "File Editing" in caps
    assert is_valid_capability("Shell")
    assert is_valid_capability("git")
    assert is_valid_capability("File System")
    # Unknown caps still rejected.
    assert validate_capabilities(["Telepathy"]) == []


# --- Defect 5: quoted goal id normalization ---------------------------------


def test_plan_id_strips_surrounding_quotes():
    from friday.planning.models import PlanType
    p = plan('"add logout button to mindwell"', Evidence())
    assert p._generate_id() == "plan:add logout button to mindwell"
    p2 = plan("add logout button to mindwell", Evidence())
    assert p._generate_id() == p2._generate_id()


# --- Defect 6: ask maturity surfaces evidence -------------------------------


def test_ask_most_mature_uses_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FRIDAY_DB", str(tmp_path / "kb.db"))
    monkeypatch.setenv("FRIDAY_LLM_MODEL", "")
    monkeypatch.setenv("FRIDAY_LLM_API_KEY", "")
    conn = connect(tmp_path / "kb.db")
    _seed_repo(conn, "Aether",
               readme="Purpose:\nAether is an AI operating system.\nMaturity: Stable",
               commit_count=30)
    _seed_repo(conn, "Vivaha",
               readme="Purpose:\nVivaha is a wedding app.\nMaturity: WIP",
               commit_count=5)
    conn.close()
    conn = connect(tmp_path / "kb.db")
    ans = ask("Which project is most mature?", conn, verbose=False)
    conn.close()
    assert not ans.used_llm
    # The answer must cite the stored maturity evidence, not refuse.
    assert "maturity" in ans.text.lower()
    assert "Aether" in ans.text


# --- Defect 7: relationship line dedup in presentation -----------------------


def test_related_presentation_dedups_pairs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FRIDAY_DB", str(tmp_path / "kb.db"))
    conn = connect(tmp_path / "kb.db")
    ra = _seed_repo(conn, "Aether",
                    readme="Purpose:\nAether is an AI OS.\nMaturity: Stable")
    rb = _seed_repo(conn, "Vivaha",
                    readme="Purpose:\nVivaha is a wedding app.\nMaturity: WIP")
    # Two identical relationship rows for the same pair+kind (as can accumulate).
    for _ in range(2):
        conn.execute(
            "INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength) "
            "VALUES (?,?,?,?,?,?)", (ra, rb, "shared-architecture", "Both are systems", 5, "Medium"))
    conn.commit()
    conn.close()
    conn = connect(tmp_path / "kb.db")
    ans = ask("Which projects are related?", conn, verbose=False)
    conn.close()
    pair_lines = [l for l in ans.text.splitlines()
                  if "Aether" in l and "Vivaha" in l and "shared" in l.lower()]
    assert len(pair_lines) <= 1


# --- Defect 8: plan connects to project-named evidence ----------------------


def test_plan_surfaces_project_knowledge(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FRIDAY_DB", str(tmp_path / "kb.db"))
    conn = connect(tmp_path / "kb.db")
    _seed_repo(conn, "MindWell",
               readme="Purpose:\nMindWell is a mental health platform.\nMaturity: Beta",
               commit_count=15)
    _build_knowledge(conn)
    conn.close()

    conn = connect(tmp_path / "kb.db")
    ev = Evidence(knowledge=get_all_knowledge(conn), initiatives=[],
                 insights=[], understanding=[])
    p = plan("Add logout button to MindWell", ev)
    conn.close()
    # The plan must reference the existing MindWell knowledge (not evidence: 0).
    assert p.affected_knowledge_ids, "plan should reference MindWell knowledge"
    assert p.evidence_count() >= 1


# --- Defect 9: strategy platform no repeated repo name ----------------------


def test_strategy_platform_no_repeated_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FRIDAY_DB", str(tmp_path / "kb.db"))
    monkeypatch.setenv("FRIDAY_LLM_MODEL", "")
    monkeypatch.setenv("FRIDAY_LLM_API_KEY", "")
    conn = connect(tmp_path / "kb.db")
    ra = _seed_repo(conn, "Aether",
                    readme="Purpose:\nAether is an AI OS.\nMaturity: Stable")
    rb = _seed_repo(conn, "Vivaha",
                    readme="Purpose:\nVivaha is a wedding app.\nMaturity: WIP")
    _seed_repo(conn, "Friday",
               readme="Purpose:\nFriday is an operating partner.\nMaturity: Beta")
    # Several shared-* relationships all involving Vivaha (would inflate its count).
    for _ in range(3):
        conn.execute(
            "INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength) "
            "VALUES (?,?,?,?,?,?)", (ra, rb, "shared-architecture", "Both systems", 5, "Medium"))
    conn.commit()
    conn.close()
    conn = connect(tmp_path / "kb.db")
    out = strategy_platform(conn)[0]
    conn.close()
    # The old defect printed "Vivaha" 3x: once as the lead and twice mislabeled
    # under "Other repos contributing reusable capability:" (a component-count
    # dict polluted with repo names). After the fix repo names and component
    # names are separate, so Vivaha appears at most twice (lead + reuse note).
    assert "Vivaha (3)" not in out  # the old mislabeled count artifact
    assert out.count("Vivaha") <= 2
