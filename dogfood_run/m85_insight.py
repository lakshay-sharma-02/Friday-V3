"""Milestone 8.5 dogfood transcript — Insight Engine end-to-end.

Drives the REAL engines (no LLM, no mock) through the full live chain:

  Knowledge -> Understanding -> Initiatives -> Insights -> Brain (ask)

The Insight Engine consumes the *outputs* of the Knowledge, Understanding, and
Initiative engines — exactly what those engines would have produced from real
observations. We seed those lower-layer outputs here (the same contract the
Insight engine reads in production), then run the new write-only Insight layer
and the Brain's unchanged insight provider. The point: prove the new layer
derives rare, high-value insights from accumulated understanding, that every
answer references insight ids, and that the Brain needs no redesign.

Lower layers are NOT modified; we only feed them their normal output rows.

Run:  PYTHONPATH=. python dogfood_run/m85_insight.py
"""

from __future__ import annotations

import sqlite3
import tempfile, os

from src.friday.db import connect
from src.friday.knowledge.models import (
    Knowledge, KnowledgeConfidence, KnowledgeStatus, KnowledgeType)
from src.friday.knowledge.store import insert_knowledge, get_all_knowledge
from src.friday.understanding import UnderstandingEngine
from src.friday.understanding.models import (
    Understanding, UnderstandingConfidence, UnderstandingStatus, UnderstandingType)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative.models import (
    Initiative, InitiativeConfidence, InitiativeStatus, InitiativeType)
from src.friday.db import insert_initiative
from src.friday.initiative import InitiativeEngine
from src.friday.insight import InsightEngine
from src.friday.ask import _p_insight


BASE = "2026-07-15T00:00:00+00:00"
SEEN = set()
N = 0


def k(ldb, subject, stmt, ktype, kconf="medium", evidence_ids=("repo:a",)):
    global N
    real = subject
    while real in SEEN:
        N += 1
        real = f"{subject}_{N}"
    SEEN.add(real)
    N += 1
    insert_knowledge(ldb, [Knowledge(
        type=KnowledgeType.from_str(ktype), subject=real, statement=stmt,
        confidence=KnowledgeConfidence.from_str(kconf),
        evidence_ids=list(evidence_ids), status=KnowledgeStatus.VERIFIED,
        created_at=BASE, updated_at=BASE, id=None)])
    return real


def u(conn, subject, stmt, utype, uconf="medium", knowledge_subjects=()):
    kmap = {kk.subject: kk.id for kk in get_all_knowledge(conn)}
    kids = [kmap[x] for x in knowledge_subjects if x in kmap]
    insert_understanding(conn, [Understanding(
        type=UnderstandingType.from_str(utype), subject=subject, statement=stmt,
        confidence=UnderstandingConfidence.from_str(uconf),
        status=UnderstandingStatus.OBSERVED, knowledge_ids=kids,
        build_at=BASE, created_at=BASE, updated_at=BASE, id=None).to_row()])


def i(conn, title, itype="platform", repos=("repo:a",), u_subjects=(), k_subjects=()):
    u_by = {uu.subject: uu.id for uu in UnderstandingEngine(conn).all_understanding()}
    k_by = {kk.subject: kk.id for kk in get_all_knowledge(conn)}
    uids = [u_by[s] for s in u_subjects if s in u_by]
    kids = [k_by[s] for s in k_subjects if s in k_by]
    init = Initiative(
        type=InitiativeType.from_str(itype), title=title,
        status=InitiativeStatus.ACTIVE,
        confidence=InitiativeConfidence.from_str("medium"),
        participating_repositories=list(repos), understanding_ids=uids,
        knowledge_ids=kids, build_at=BASE, started_at=BASE, statement="",
        created_at=BASE, updated_at=BASE, id=None)
    insert_initiative(conn, [init.to_row()])


def ask(conn, query):
    ev = type("E", (), {"blocks": [], "raw": {}})()
    req = type("R", (), {"query": query, "needs": set(), "subject": None})()
    _p_insight.fn(req, conn, ev, None)
    items = ev.raw.get("insights", [])
    ids = [it.get("id") for it in items]
    print(f"\n>>> {query}")
    print(f"    insight_total = {ev.raw.get('insight_total', 0)}")
    for it in items:
        print(f"    - [{it.get('type')}] {it.get('title')}  ({it.get('confidence')})")
        print(f"        id={it.get('id')}")
    return ids


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = connect(path)

    print("=" * 78)
    print("M8.5 DOGFOOD: Insight Engine end-to-end (real engines, no LLM)")
    print("=" * 78)

    print("\n[1] Knowledge (lower-layer output, seeded from observation chain)")
    # Two repos, repeated auth, heavy rust, commercial up / research down,
    # emerging expertise, recurring patterns, recurring bottlenecks.
    auth_a = k(conn, "auth", "auth solved in api (repo:a)", "recurring_pattern",
               "medium", ["repo:a"])
    auth_b = k(conn, "auth", "auth solved in web (repo:b)", "recurring_pattern",
               "medium", ["repo:b"])
    rust1 = k(conn, "rust", "rust infra 1", "engineering_trend", "strong", ["repo:a"])
    rust2 = k(conn, "rust", "rust infra 2", "technology_investment", "strong", ["repo:b"])
    comm = k(conn, "comm", "commercial rising", "engineering_trend", "medium", ["repo:a"])
    res = k(conn, "res", "research present", "engineering_trend", "medium", ["repo:b"])
    exp_a = k(conn, "exp a", "kernel expertise emerging", "engineering_trend",
              "strong", ["repo:a"])
    exp_b = k(conn, "exp b", "compilers expertise emerging", "engineering_trend",
              "strong", ["repo:b"])
    risk_k = k(conn, "risk x", "a risk is brewing", "engineering_trend", "medium", ["repo:a"])
    weak_k = k(conn, "weak y", "a weakness exists", "engineering_trend", "medium", ["repo:a"])
    pat1 = k(conn, "p1", "build-script pattern", "recurring_pattern", "medium", ["repo:a"])
    pat2 = k(conn, "p2", "build-script pattern", "recurring_pattern", "medium", ["repo:b"])
    pat3 = k(conn, "p3", "config pattern", "recurring_pattern", "medium", ["repo:c"])
    bn1 = k(conn, "b1", "review queue bottleneck", "recurring_bottleneck", "medium", ["repo:a"])
    bn2 = k(conn, "b2", "review queue bottleneck", "recurring_bottleneck", "medium", ["repo:b"])
    bn3 = k(conn, "b3", "ci flake bottleneck", "recurring_bottleneck", "medium", ["repo:a"])
    print(f"    knowledge rows: {len(get_all_knowledge(conn))}")

    print("\n[2] Understanding (lower-layer output)")
    u(conn, "auth a", "Auth solved in api.", "engineering_habit", "medium", [auth_a])
    u(conn, "auth b", "Auth solved in web.", "engineering_habit", "medium", [auth_b])
    u(conn, "rust a", "Rust investment rising.", "engineering_direction", "strong",
      [rust1, rust2])
    u(conn, "rust b", "Rust direction strong.", "engineering_direction", "strong",
      [rust1, rust2])
    u(conn, "comm inc", "Commercial increasing.", "commercial_direction", "medium", [comm])
    u(conn, "res dec", "Research decreasing.", "research_direction", "medium", [res])
    u(conn, "inv a", "Investment increasing in systems.", "investment_trend",
      "strong", [rust1])
    u(conn, "inv b", "Investment increasing in kernels.", "investment_trend",
      "strong", [rust2])
    u(conn, "exp a", "Kernel expertise emerging.", "emerging_expertise", "strong", [exp_a])
    u(conn, "exp b", "Compilers expertise emerging.", "emerging_expertise", "strong", [exp_b])
    u(conn, "risk", "A risk is emerging.", "engineering_risk", "medium", [risk_k])
    u(conn, "weak", "A weakness exists.", "engineering_weakness", "medium", [weak_k])
    # run understanding build for proper lifecycle/confidence
    ur = UnderstandingEngine(conn).build()
    print(f"    understanding: total={ur.total} created={ur.created}")

    print("\n[3] Initiatives (lower-layer output)")
    i(conn, "Engineering Platform", itype="platform",
      repos=["repo:a", "repo:b"], u_subjects=["rust a", "rust b"])
    ir = InitiativeEngine(conn).build()
    print(f"    initiatives: total={ir.total} created={ir.created}")

    print("\n[4] Insights (build)  <-- M8.5 NEW LAYER")
    res = InsightEngine(conn).build()
    print(res.to_text().strip())

    eng = InsightEngine(conn)
    active = eng.active_insights()
    print("    active insights:")
    for ins in active:
        print(f"      - {ins.type.value:20} {ins.title}  [{ins.confidence.value}]")

    print("\n[5] Brain (ask) — every answer must reference insight ids")
    for q in (
        "What opportunities am I missing?",
        "What engineering debt is growing?",
        "What should I build next?",
        "What keeps repeating?",
        "What reusable component should exist?",
        "What is my biggest blind spot?",
        "What engineering investment is paying off?",
    ):
        ids = ask(conn, q)
        assert ids, f"expected insight ids in answer to: {q!r}"

    print("\n[6] Explain every insight (confidence + supporting evidence)")
    for ins in active:
        obj, br, u_ids, i_ids, k_ids, evo = eng.explain(ins.id)
        print(f"\n    {ins.id}")
        print(f"      type={ins.type.value} confidence={ins.confidence.value} "
              f"status={ins.status.value}")
        print(f"      statement: {ins.statement}")
        print(f"      confidence breakdown: {br}")
        print(f"      understanding={len(u_ids)} initiative={len(i_ids)} "
              f"knowledge={len(k_ids)}")
        assert br, "every insight explains its derived confidence"
        assert (u_ids or i_ids or k_ids), "every insight cites evidence"

    print("\n[7] Insight Evolution timeline")
    for e in eng.evolution():
        print(f"      {e.timestamp[:19]}  {e.event_type:12}  {e.reason}")

    conn.close()
    os.remove(path)
    print("\n" + "=" * 78)
    print(f"DOGFOOD COMPLETE — {len(active)} active insights; Brain consumed "
          "them without redesign.")
    print("=" * 78)


if __name__ == "__main__":
    main()
