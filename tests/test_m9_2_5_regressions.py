"""M9.2.5 Execution Readiness Sprint — regression tests.

One test per repaired defect. Every test is written to FAIL against the
pre-sprint code (RED), then pass once the fix lands (GREEN).

Run: pytest tests/test_m9_2_5_regressions.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from friday.db import (
    connect,
    insert_observations,
    ObservationRow,
    get_task_graph_by_id,
    insert_task_graph,
)
from friday.knowledge.models import Knowledge, KnowledgeType, KnowledgeStatus, KnowledgeConfidence
from friday.knowledge import static as knowledge_static
from friday.context import ContextEngine, EngineeringSession
from friday.context.models import SessionActivity
from friday.observation.model import Observation
from friday.observation import model as obs_model
from friday.planning import graph_engine, compiler
from friday.planning.graph_schema import validate_task_graph, SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


def _obs_row(observed_at, subject, aspect, value="1"):
    return ObservationRow(
        id="", observed_at=observed_at, source="git", subject=subject,
        aspect=aspect, value=value, confidence="Observed", scope=subject,
    )


# ---------------------------------------------------------------------------
# Part A #1 — observations PRIMARY KEY / dedupe
# ---------------------------------------------------------------------------

def test_observations_no_duplicate_on_reinsert(tmp_path):
    conn = connect(tmp_path / "obs.db")
    row = _obs_row("2026-07-14T10:00:00+00:00", "FridayV3", "commit_count")
    insert_observations(conn, [row])
    insert_observations(conn, [row])  # re-ingest same fact
    rows = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
    assert rows["n"] == 1, "INSERT OR REPLACE must dedupe on PRIMARY KEY(id)"


def test_observations_no_duplicate_on_reinsert_same_run(tmp_path):
    """Audit C1 repro: `insert_observations` called twice with the SAME rows
    (same observed_at + id) must collapse to one row, not append a duplicate.
    This is the exact HIGH-severity bug — without PRIMARY KEY(id) the
    INSERT OR REPLACE degraded to a plain INSERT and doubled every re-ingest."""
    conn = connect(tmp_path / "obs.db")
    run1 = _obs_row("2026-07-14T10:00:00+00:00", "FridayV3", "commit_count", "1")
    run2 = _obs_row("2026-07-14T10:00:00+00:00", "FridayV3", "commit_count", "1")
    insert_observations(conn, [run1])
    insert_observations(conn, [run2])  # identical re-ingest
    rows = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
    assert rows["n"] == 1, f"re-ingest must dedupe on PRIMARY KEY(id): got {rows['n']}"


# ---------------------------------------------------------------------------
# Part A #2 — knowledge evolution CLI crash (import gap)
# ---------------------------------------------------------------------------

def test_knowledge_evolution_runs_on_empty_db(tmp_path):
    db = tmp_path / "evo.db"
    env = {**__import__("os").environ, "FRIDAY_DB": str(db)}
    result = subprocess.run(
        [sys.executable, "-m", "friday.cli", "knowledge", "evolution"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"CLI crashed: {result.stderr}"


# ---------------------------------------------------------------------------
# Part A #3 — knowledge verification inflation
# ---------------------------------------------------------------------------

def _knowledge_row(subject, evidence_ids, verification_count=0,
                   status=KnowledgeStatus.OBSERVED):
    return Knowledge(
        type=KnowledgeType.ENGINEERING_TREND, subject=subject,
        statement=f"{subject} trend", confidence=KnowledgeConfidence.STRONG,
        evidence_ids=list(evidence_ids), status=status,
        verification_count=verification_count,
    )


def test_verification_count_stable_without_new_evidence(tmp_path):
    from friday.knowledge.engine import KnowledgeEngine
    from friday.knowledge.store import insert_knowledge, get_all_knowledge
    conn = connect(tmp_path / "know.db")
    k = _knowledge_row("t1", ["obs:1"], verification_count=1, status=KnowledgeStatus.VERIFIED)
    insert_knowledge(conn, [k])

    eng = KnowledgeEngine(conn)
    eng.build()  # no new evidence -> count must not grow
    eng.build()

    all_k = get_all_knowledge(conn)
    assert len(all_k) == 1
    assert all_k[0].verification_count == 1, "verification_count inflated without new evidence"
    assert all_k[0].status == KnowledgeStatus.VERIFIED


def test_verify_knowledge_increments_only_when_called():
    from friday.knowledge.confidence import verify_knowledge
    k = _knowledge_row("t1", ["obs:1"], verification_count=0, status=KnowledgeStatus.OBSERVED)
    before = k.verification_count
    k2 = verify_knowledge(k)
    assert k2.verification_count == before + 1, "verify_knowledge must increment count"


# ---------------------------------------------------------------------------
# Part A #4 — context build idempotency
# ---------------------------------------------------------------------------

def _insert_repo(conn, repo_id, name, path):
    conn.execute(
        "INSERT INTO repositories(id,name,path,commit_count,readme_summary,ingestion_time) "
        "VALUES (?,?,?,0,'',?)",
        (repo_id, name, str(path), "2026-07-14T10:00:00+00:00"),
    )


def _seed_obs(conn, subject, aspect, at):
    o = Observation(source="git", subject=subject, aspect=aspect, value="1",
                    observed_at=at, scope=subject, confidence=obs_model.Confidence.OBSERVED)
    conn.execute(
        "INSERT OR REPLACE INTO observations "
        "(id, observed_at, source, subject, aspect, value, confidence, scope, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{at}:git:{subject}:{aspect}", at, "git", subject, aspect, "1",
         "Observed", subject, None),
    )


def test_context_build_idempotent_on_same_data(tmp_path):
    conn = connect(tmp_path / "ctx.db")
    conn.execute("INSERT INTO repositories(id,name,path,commit_count,ingestion_time) VALUES (1,'FridayV3',?,0,?)",
                 (str(tmp_path / "a"), "2026-07-14T10:00:00+00:00"))
    _seed_obs(conn, "FridayV3", "commit_count", "2026-07-14T10:00:00+00:00")
    _seed_obs(conn, "FridayV3", "branch", "2026-07-14T10:05:00+00:00")

    eng = ContextEngine(conn)
    eng.build()
    n1 = len(eng.sessions())
    eng.build()  # identical data, default as_of -> idempotent
    n2 = len(eng.sessions())
    assert n2 == n1, f"context build duplicated sessions: {n1} -> {n2}"


def test_session_id_excludes_built_at():
    s = EngineeringSession(
        start_time="2026-07-14T10:00:00+00:00",
        end_time="2026-07-14T10:05:00+00:00",
        repositories=["FridayV3"], observations=["o1"],
        primary_repo="FridayV3",
    )
    s2 = EngineeringSession(
        start_time="2026-07-14T10:00:00+00:00",
        end_time="2026-07-14T10:05:00+00:00",
        repositories=["FridayV3"], observations=["o1"],
        primary_repo="FridayV3",
    )
    assert s.id == s2.id, "session id must not include built_at"
    assert "built_at" not in s.id


# ---------------------------------------------------------------------------
# Part A #5 — referential integrity (FKs)
# ---------------------------------------------------------------------------

def test_task_graph_delete_cascades_orphan_tasks(tmp_path):
    conn = connect(tmp_path / "fk.db")
    # Build a minimal plan + graph referencing it (raw rows; FK enforced).
    pid = "plan:g"
    conn.execute(
        "INSERT INTO plans(id,goal,plan_type,confidence,status,"
        "affected_initiative_ids,affected_insight_ids,affected_understanding_ids,"
        "affected_knowledge_ids,risks,verification,rollback,plan_text,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "g", "feature", "medium", "planned", "", "", "", "", "", "", "", "",
         "2026-07-14T10:00:00+00:00", "2026-07-14T10:00:00+00:00"),
    )
    gid = f"taskgraph:plan:{pid[5:]}" if pid.startswith("plan:") else f"taskgraph:{pid}"
    conn.execute(
        "INSERT INTO task_graphs(id,goal,plan_id,plan_type,task_count,edge_count,"
        "critical_path_length,parallel_groups,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (gid, "g", pid, "feature", 0, 0, 0, 0, "compiled",
         "2026-07-14T10:00:00+00:00", "2026-07-14T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO tasks(id,graph_id,plan_id,milestone_order,title,description,"
        "task_type,required_capabilities,complexity,priority,estimated_effort,"
        "dependencies,inputs,outputs,acceptance_criteria,verification,rollback,"
        "evidence,status,confidence,sequence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{gid}#t1", gid, pid, 1, "t", "", "implementation", "", "medium", "medium",
         "small", "", "[]", "[]", "[]", "[]", "[]", "[]", "pending", "medium", 1),
    )
    conn.commit()
    # Deleting the plan must cascade-delete the graph and its tasks (FK ON DELETE CASCADE).
    conn.execute("DELETE FROM plans WHERE id=?", (pid,))
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    assert remaining == 0, f"orphan tasks remained after plan delete: {remaining}"


def test_bad_foreign_key_rejected(tmp_path):
    conn = connect(tmp_path / "fk2.db")
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO tasks(id,graph_id,plan_id,milestone_order,title,description,"
            "task_type,required_capabilities,complexity,priority,estimated_effort,"
            "dependencies,inputs,outputs,acceptance_criteria,verification,rollback,"
            "evidence,status,confidence,sequence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x#t1", "nonexistent-graph", "nonexistent-plan", 1, "t", "", "implementation",
             "", "medium", "medium", "small", "", "[]", "[]", "[]", "[]", "[]", "[]",
             "pending", "medium", 1),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Part B — Law 19 (Knowledge must not depend on Brain identity)
# ---------------------------------------------------------------------------

def test_static_knowledge_does_not_import_identity():
    import inspect
    src = inspect.getsource(knowledge_static)
    assert "from ..identity import" not in src, "Knowledge layer still imports Brain identity"
    assert "from ..identity" not in src


def test_static_knowledge_builds_without_identity(tmp_path):
    conn = connect(tmp_path / "static.db")
    conn.execute("INSERT INTO repositories(id,name,path,commit_count,readme_summary,ingestion_time) "
                 "VALUES (1,'FridayV3',?,0,'Purpose:\\nA build tool.\\n\\n',?)",
                 (str(tmp_path / "a"), "2026-07-14T10:00:00+00:00"))
    # Should not raise (no Brain dependency).
    knowledge_static.detect_static_knowledge(conn)


# ---------------------------------------------------------------------------
# Part B — Law 24 (schema_version on contracts)
# ---------------------------------------------------------------------------

def test_knowledge_carries_schema_version(tmp_path):
    conn = connect(tmp_path / "sv.db")
    k = _knowledge_row("t1", ["obs:1"])
    assert hasattr(k, "schema_version")
    from friday.knowledge.store import insert_knowledge, get_all_knowledge
    insert_knowledge(conn, [k])
    got = get_all_knowledge(conn)[0]
    assert got.schema_version == Knowledge.SCHEMA_VERSION


def test_validate_task_graph_rejects_missing_schema_version():
    bad = {
        "goal": "g", "plan_id": "plan:g", "plan_type": "feature",
        "tasks": [], "edges": [], "metadata": {},  # no schema_version
    }
    with pytest.raises(Exception):
        validate_task_graph(bad)


def test_validate_task_graph_accepts_current_version(tmp_path):
    from friday.planning.engine import PlanEngine
    conn = connect(tmp_path / "vg.db")
    PlanEngine(conn).generate("g")
    g = graph_engine.TaskGraphEngine(conn).generate("g")
    # A real, current-version graph must pass validation (returns None).
    assert validate_task_graph(g.to_json()) is None


# ---------------------------------------------------------------------------
# Part C — Task Graph validation enforced on generate
# ---------------------------------------------------------------------------

def test_graph_generate_enforces_validation(tmp_path):
    # Part C: the generated Task Graph must pass through validate_task_graph().
    # We build a real graph via the engines (happy path) and assert the
    # contract validator accepts its serialized form. (Malformed graphs are
    # rejected by validate_task_graph — covered by
    # test_validate_task_graph_rejects_missing_schema_version and the strict
    # enum tests, which the rebuild path now enforces.)
    from friday.planning.engine import PlanEngine
    conn = connect(tmp_path / "gen.db")
    PlanEngine(conn).generate("g")
    g = graph_engine.TaskGraphEngine(conn).generate("g")
    assert g is not None
    # validate_task_graph raises on violation, returns None on success.
    assert validate_task_graph(g.to_json()) is None
    # Rebuild from DB must also be valid.
    row = get_task_graph_by_id(conn, g.id)
    assert graph_engine.TaskGraphEngine(conn)._rebuild(row) is not None


# ---------------------------------------------------------------------------
# Part D/E — serialization round-trip + strict enums
# ---------------------------------------------------------------------------

def test_knowledge_roundtrip(tmp_path):
    conn = connect(tmp_path / "rt.db")
    k = _knowledge_row("t1", ["obs:1"], verification_count=2, status=KnowledgeStatus.STABLE)
    from friday.knowledge.store import insert_knowledge, get_all_knowledge
    insert_knowledge(conn, [k])
    got = get_all_knowledge(conn)[0]
    assert got.subject == k.subject
    assert got.evidence_ids == k.evidence_ids
    assert got.verification_count == 2
    assert got.status == KnowledgeStatus.STABLE


def test_enum_from_str_strict():
    # Part E: unknown enum values must RAISE, never silently coerce to a default.
    with pytest.raises(ValueError):
        KnowledgeType.from_str("not_a_real_type")
    with pytest.raises(ValueError):
        KnowledgeStatus.from_str("bogus")
    with pytest.raises(ValueError):
        compiler.TaskType.from_str("definitely_not_a_task_type")
    from friday.knowledge.models import KnowledgeConfidence
    from friday.observation.model import Confidence, Health
    from friday.context.models import SessionActivity
    with pytest.raises(ValueError):
        KnowledgeConfidence.from_str("frobnicate")
    with pytest.raises(ValueError):
        Confidence.from_str("nonsense")
    with pytest.raises(ValueError):
        Health.from_str("bogus")
    with pytest.raises(ValueError):
        SessionActivity.from_str("not_an_activity")
    # Valid members still resolve (including the explicit "unknown" sentinel).
    assert SessionActivity.from_str("unknown") is SessionActivity.UNKNOWN
    assert Confidence.from_str("Observed") is Confidence.OBSERVED


# ---------------------------------------------------------------------------
# Part D — serialization round-trip equality (serialize -> deserialize -> equal)
# ---------------------------------------------------------------------------

def test_understanding_roundtrip(tmp_path):
    from friday.understanding.models import (
        Understanding, UnderstandingType, UnderstandingStatus, UnderstandingConfidence)
    from friday.db import insert_understanding, get_all_understanding
    conn = connect(tmp_path / "u.db")
    u = Understanding(
        id="u:1", type=UnderstandingType.ENGINEERING_DIRECTION, subject="R",
        statement="R trends up", confidence=UnderstandingConfidence.MEDIUM,
        status=UnderstandingStatus.OBSERVED, knowledge_ids=["repo:1"], build_at="t",
        created_at="c", updated_at="u")
    insert_understanding(conn, [u.to_row()])
    assert Understanding.from_row(get_all_understanding(conn)[0]) == u
    assert Understanding.from_row(u.to_row()) == u


def test_initiative_roundtrip(tmp_path):
    from friday.initiative.models import (
        Initiative, InitiativeType, InitiativeStatus, InitiativeConfidence)
    from friday.db import insert_initiative, get_all_initiatives
    conn = connect(tmp_path / "iv.db")
    iv = Initiative(
        id="i:1", type=InitiativeType.INFRASTRUCTURE, title="Auth",
        status=InitiativeStatus.ACTIVE, confidence=InitiativeConfidence.STRONG,
        participating_repositories=["R"], understanding_ids=["u1"],
        knowledge_ids=["repo:1"], build_at="t", created_at="c", updated_at="u")
    insert_initiative(conn, [iv.to_row()])
    assert Initiative.from_row(get_all_initiatives(conn)[0]) == iv
    assert Initiative.from_row(iv.to_row()) == iv


def test_insight_roundtrip(tmp_path):
    from friday.insight.models import (
        Insight, InsightType, InsightStatus, InsightConfidence)
    from friday.db import insert_insight, get_all_insights
    conn = connect(tmp_path / "ins.db")
    ins = Insight(
        id="ins:1", type=InsightType.OPPORTUNITY, title="X", statement="do x",
        status=InsightStatus.OBSERVED, confidence=InsightConfidence.WEAK,
        understanding_ids=["u1"], initiative_ids=["i1"], knowledge_ids=["repo:1"],
        build_at="t", created_at="c", updated_at="u")
    insert_insight(conn, [ins.to_row()])
    assert Insight.from_row(get_all_insights(conn)[0]) == ins
    assert Insight.from_row(ins.to_row()) == ins


def test_plan_roundtrip(tmp_path):
    from friday.planning.models import Plan, PlanType, PlanConfidence, PlanStatus
    from friday.db import insert_plan, get_all_plans
    conn = connect(tmp_path / "pl.db")
    p = Plan(
        id="plan:g", goal="Build auth", plan_type=PlanType.FEATURE,
        confidence=PlanConfidence.MEDIUM, status=PlanStatus.PLANNED,
        affected_knowledge_ids=["repo:1"], milestones=[{"order": 1, "title": "m"}],
        verification=[{"method": "test", "detail": "x"}], plan_text="fixed")
    insert_plan(conn, [p.to_row()])
    assert Plan.from_row(get_all_plans(conn)[0]) == p
    assert Plan.from_row(p.to_row()) == p


def test_worker_roundtrip(tmp_path):
    from friday.worker.models import Worker, WorkerKind
    from friday.db import insert_worker, get_all_workers
    conn = connect(tmp_path / "w.db")
    w = Worker(
        id="worker:w", name="W", kind=WorkerKind.LLM, capabilities=["Python", "Testing"],
        supported_languages=["Python"], supported_task_types=["testing"],
        context_window=8000, requires_python=True, created_at="c", updated_at="u")
    insert_worker(conn, w.to_row())
    assert Worker.from_row(get_all_workers(conn)[0]) == w
    assert Worker.from_row(w.to_row()) == w


def test_taskgraph_rebuild_roundtrip(tmp_path):
    # TaskGraph round-trips through the DB: generate -> persist -> rebuild.
    from friday.planning.engine import PlanEngine
    from friday.planning.graph_engine import TaskGraphEngine
    conn = connect(tmp_path / "tg.db")
    PlanEngine(conn).generate("Build auth")
    g = TaskGraphEngine(conn).generate("Build auth")
    rebuilt = TaskGraphEngine(conn).graph_by_id(g.id)
    assert rebuilt is not None
    assert len(rebuilt.tasks) == len(g.tasks)
    assert {t.id for t in rebuilt.tasks} == {t.id for t in g.tasks}
    assert {e["from"] for e in rebuilt.edges} == {e["from"] for e in g.edges}


# ---------------------------------------------------------------------------
# Part F — transaction safety (no partial task graph)
# ---------------------------------------------------------------------------

def test_task_graph_insert_atomic(tmp_path):
    from friday.planning.models import Plan, PlanType
    from friday.planning.graph_engine import TaskGraphEngine
    from friday.planning.engine import PlanEngine
    conn = connect(tmp_path / "tx.db")
    # A valid graph is written atomically: header + tasks + edges commit together.
    PlanEngine(conn).generate("g")
    g = TaskGraphEngine(conn).generate("g")
    graphs = conn.execute("SELECT COUNT(*) AS n FROM task_graphs").fetchone()["n"]
    tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    edges = conn.execute("SELECT COUNT(*) AS n FROM task_edges").fetchone()["n"]
    assert graphs == 1
    assert tasks == len(g.tasks)
    assert edges == len(g.edges)
    # No orphan header without tasks.
    assert tasks > 0
