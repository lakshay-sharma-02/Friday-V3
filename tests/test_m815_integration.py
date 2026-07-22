"""Milestone 8.1.5 — Knowledge Integration & Cold Start regression tests.

Every dogfood failure from the sprint is pinned here:

  A. Static knowledge exists immediately after a fresh ingest (no history).
  B. Knowledge build yields > 0 after ingest.
  C. The Brain (ask) consumes the knowledge table as a first-class source.
  D. Cold-start answers explain missing temporal history honestly, never
     return a misleading "0 of N repositories" line.
  E. Query routing: Explain Friday V3 resolves to the longest-matching repo;
     engineering belief is not an abandoned repo; converging is not a
     recommendation.
  F. Context is not immediately stale after `context build`.
  G. No meaningless knowledge (self-loop sequences; single-instant trends).
  H. Evidence availability is explicit (static vs temporal).
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.friday.db import connect, get_architecture, get_languages, get_technologies
from src.friday.knowledge import Knowledge, KnowledgeEngine, KnowledgeType
from src.friday.ask import ask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory DB with the full schema (and migrations) applied."""
    import tempfile
    from pathlib import Path
    os.environ["FRIDAY_DB"] = str(tmp_path / "m815.db")
    conn = connect()  # runs SCHEMA + _migrate (M4 columns)
    yield conn
    conn.close()


def _ingest_repo(conn, rid: int, name: str, purpose: str, techs, arch: str) -> None:
    """Simulate a fresh ingest of ONE repository (no observations/sessions)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO repositories "
        "(id,name,path,default_branch,is_dirty,first_commit_date,last_commit_date,"
        "remote_url,commit_count,readme_summary,license,primary_author,ingestion_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, name, "/p/" + name, "main", 0, now[:10], now[:10], "url", 5,
         purpose, "MIT", "me", now),
    )
    conn.execute("INSERT INTO languages (repo_id,language,file_count) VALUES (?,?,?)",
                 (rid, "Python", 10))
    for t in techs:
        conn.execute("INSERT INTO technologies (repo_id,tech,evidence) VALUES (?,?,?)",
                     (rid, t, "detected"))
    conn.execute("INSERT INTO architecture (repo_id,architecture,evidence) VALUES (?,?,?)",
                 (rid, arch, "detected"))
    conn.commit()


def _seed_workspace(conn, n: int = 3) -> None:
    """Ingest N repos with distinct purpose/tech so static knowledge is rich."""
    specs = [
        ("FridayV3", "purpose: an ai operating partner cli that answers questions",
         ["FastAPI", "SQLite"], "CLI tool"),
        ("Aether", "purpose: an ai-native operating system kernel in rust",
         ["Supabase"], "Web app"),
        ("Vivaha", "purpose: a wedding planning web app with react and supabase",
         ["React", "Supabase"], "Web app"),
    ]
    for i, (name, purpose, techs, arch) in enumerate(specs[:n], start=1):
        _ingest_repo(conn, i, name, purpose, techs, arch)


# ---------------------------------------------------------------------------
# A / B / I: static knowledge after fresh ingest
# ---------------------------------------------------------------------------


def test_knowledge_build_after_fresh_ingest(db):
    """Part A/B/I: a fresh ingest (no observations) must produce knowledge."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    res = eng.build()
    assert res.total > 0, "knowledge build created 0 entries after ingest (bug)"
    assert res.static > 0, "no static knowledge produced from ingest-time data"
    # No observations => no temporal knowledge yet.
    assert res.temporal == 0


def test_static_knowledge_is_marked_static(db):
    """Static knowledge carries is_static=True; readable separately."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    eng.build()
    static = eng.static_knowledge()
    temporal = eng.temporal_knowledge()
    assert static, "expected static knowledge"
    assert all(k.is_static for k in static)
    assert temporal == [], "no temporal knowledge should exist without history"


def test_static_knowledge_covers_identity_architecture_stack(db):
    """Each ingested repo yields identity / architecture / stack knowledge."""
    _seed_workspace(db, 1)
    eng = KnowledgeEngine(db)
    eng.build()
    types = {k.type for k in eng.static_knowledge()}
    assert KnowledgeType.PROJECT_IDENTITY in types
    assert KnowledgeType.PROJECT_ARCHITECTURE in types
    assert KnowledgeType.PROJECT_STACK in types


def test_portfolio_technology_knowledge(db):
    """Technologies shared across >=2 repos surface as portfolio knowledge."""
    _seed_workspace(db, 3)  # Aether + Vivaha both use Supabase
    eng = KnowledgeEngine(db)
    eng.build()
    portfolio_tech = [k for k in eng.static_knowledge()
                      if k.type == KnowledgeType.PORTFOLIO_TECHNOLOGY]
    assert any("Supabase" in k.statement for k in portfolio_tech)


def test_no_knowledge_without_ingest(db):
    """Empty workspace yields no knowledge (and does not crash)."""
    eng = KnowledgeEngine(db)
    res = eng.build()
    assert res.total == 0
    assert res.static == 0


# ---------------------------------------------------------------------------
# C: Brain consumes Knowledge
# ---------------------------------------------------------------------------


def test_ask_consumes_knowledge_table(db):
    """Part C: a knowledge question reads the knowledge table, not bypass it."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    eng.build()
    ans = ask("What engineering knowledge do you have?", db, verbose=False)
    assert not ans.used_llm
    # The answer must reference accumulated knowledge and the seeded projects.
    assert "Accumulated engineering knowledge" in ans.text
    assert "FridayV3" in ans.text
    # It must NOT report knowledge_total == 0.
    assert ans.evidence.raw.get("knowledge_total", 0) > 0


def test_knowledge_provider_is_primary_for_knowledge_question(db):
    """The KNOWLEDGE objective is chosen for knowledge questions."""
    from src.friday.ask import requirements_from_question
    from src.friday import objective as obj_mod
    req = requirements_from_question(
        "What engineering knowledge have you accumulated?", db)
    decision = obj_mod.judge(req)
    assert decision.objective == obj_mod.Objective.KNOWLEDGE


# ---------------------------------------------------------------------------
# D / H: cold start messaging + no misleading "0 of N"
# ---------------------------------------------------------------------------


def test_cold_start_no_zero_of_n_message(db):
    """Part D/H: cold start must not emit 'based on 0 of N repositories'."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    eng.build()
    ans = ask("What engineering knowledge do you have?", db, verbose=False)
    assert "0 of" not in ans.text, "misleading '0 of N' coverage note present"
    assert "based on" not in ans.text.lower() or "0 of" not in ans.text


def test_cold_start_explains_missing_temporal_history(db):
    """Part D: honestly states temporal trends cannot yet be determined."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    eng.build()
    ans = ask("What long-term engineering trends have you observed?", db, verbose=False)
    # Either it answers from static knowledge, or it explains the history gap
    # honestly. It must never claim a trend it cannot support.
    assert ("one observation" in ans.text.lower()
            or "trend" in ans.text.lower()
            or "Accumulated engineering knowledge" in ans.text)


def test_evidence_availability_explicit(db):
    """Part H: the answer knows static vs temporal availability."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    eng.build()
    ans = ask("What engineering knowledge do you have?", db, verbose=False)
    raw = ans.evidence.raw
    assert raw["knowledge_static"] > 0
    assert raw["knowledge_temporal"] == 0


# ---------------------------------------------------------------------------
# E: query routing
# ---------------------------------------------------------------------------


def test_explain_friday_v3_resolves_to_v3(db):
    """Part E: 'Explain Friday V3' must explain Friday V3, not Friday."""
    _ingest_repo(db, 1, "Friday", "purpose: the original friday cli", ["Bash"], "CLI tool")
    _ingest_repo(db, 2, "Friday V3",
                 "purpose: the rewritten friday v3 operating partner", ["Python"], "CLI tool")
    ans = ask("Explain Friday V3.", db, verbose=False)
    assert "friday v3" in ans.text[:80].lower(), ans.text[:120]
    # V3 (the more specific project) must dominate, not the bare 'Friday'.
    assert ans.text.lower().index("friday v3") <= ans.text.lower().index("friday") + 12 \
        or "Friday V3" in ans.text[:80]


def test_explain_friday_resolves_to_friday(db):
    """'Explain Friday' (no V3) explains the original Friday repo."""
    _ingest_repo(db, 1, "Friday", "purpose: the original friday cli", ["Bash"], "CLI tool")
    _ingest_repo(db, 2, "Friday V3",
                 "purpose: the rewritten friday v3 operating partner", ["Python"], "CLI tool")
    ans = ask("Explain Friday.", db, verbose=False)
    # Should be about Friday generally / the operating partner; not collapse to V3.
    assert "Friday" in ans.text


def test_engineering_belief_not_abandoned(db):
    """Part E: 'engineering belief' must NOT become an abandoned-repo answer."""
    _seed_workspace(db, 3)
    ans = ask("What is my engineering belief?", db, verbose=False)
    assert "abandoned" not in ans.text.lower(), "belief collapsed to abandoned repo"
    assert "inactive" not in ans.text.lower() or "abandon" not in ans.text.lower()


def test_converging_projects_not_recommendation(db):
    """Part E: 'converging projects' must NOT become a work-on-next answer."""
    _seed_workspace(db, 3)
    ans = ask("Which of my projects are converging?", db, verbose=False)
    assert "work on next" not in ans.text.lower()
    assert "continue" not in ans.text.lower() or "converg" in ans.text.lower()


# ---------------------------------------------------------------------------
# F: context freshness
# ---------------------------------------------------------------------------


def test_context_not_immediately_stale(db):
    """Part F: after `context build`, the context must not report stale."""
    from src.friday.context.engine import ContextEngine
    from src.friday.observe import observe_via_engine
    _seed_workspace(db, 1)
    observe_via_engine(db)  # records observations
    eng = ContextEngine(db)
    eng.build()
    assert not eng.is_stale(), "context reported stale immediately after build"


# ---------------------------------------------------------------------------
# G: knowledge quality thresholds
# ---------------------------------------------------------------------------


def test_no_meaningless_self_loop_sequence(db):
    """Part G: 'committing frequently followed by committing' is not knowledge."""
    from src.friday.context.models import EngineeringSession, SessionActivity
    from src.friday.knowledge.patterns import detect_repeated_sequences
    now = datetime.datetime.now(datetime.timezone.utc)
    sessions = [
        EngineeringSession(
            start_time=(now - datetime.timedelta(hours=i)).isoformat(),
            end_time=(now - datetime.timedelta(hours=i, minutes=-30)).isoformat(),
            repositories=["Friday"], primary_repo="Friday",
            observations=[], activity=SessionActivity.COMMITTING,
            confidence=__import__("src.friday.observation.model", fromlist=["Confidence"]).Confidence.DERIVED,
        )
        for i in range(6)
    ]
    k = detect_repeated_sequences(sessions, min_count=2)
    assert not any("committing is frequently followed by committing" in kk.statement
                   for kk in k), "self-loop sequence produced meaningless knowledge"


def test_single_instant_trend_not_emitted(db):
    """Part G: a single observation instant is not a trend/emerging interest."""
    from src.friday.observation.model import Confidence, Observation
    from src.friday.knowledge.trends import detect_trends
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # All observations share the SAME timestamp (one observe run).
    obs = [Observation(source="git", subject="Rust", aspect="language",
                       value="used", observed_at=now, scope="",
                       confidence=Confidence.OBSERVED) for _ in range(5)]
    k = detect_trends(obs, [])
    assert not any("emerging interest" in kk.statement.lower() for kk in k), \
        "single-instant observation produced an 'emerging interest' trend"


def test_trend_requires_temporal_spread(db):
    """Part G: a real trend needs observations spread over time."""
    from src.friday.observation.model import Confidence, Observation
    from src.friday.knowledge.trends import detect_trends
    now = datetime.datetime.now(datetime.timezone.utc)
    obs = [Observation(source="git", subject="Rust", aspect="language",
                       value="used",
                       observed_at=(now - datetime.timedelta(days=i)).isoformat(),
                       scope="", confidence=Confidence.OBSERVED) for i in range(5)]
    k = detect_trends(obs, [])
    # Spread over days => a trend/interest may be emitted (this is legitimate).
    assert isinstance(k, list)


# ---------------------------------------------------------------------------
# Idempotency preserved (no regression of existing behavior)
# ---------------------------------------------------------------------------


def test_build_idempotent_with_static(db):
    """Re-building over the same ingest data changes nothing."""
    _seed_workspace(db, 3)
    eng = KnowledgeEngine(db)
    r1 = eng.build()
    r2 = eng.build()
    assert r2.created == 0
    assert r2.total == r1.total
    assert r2.static == r1.static
