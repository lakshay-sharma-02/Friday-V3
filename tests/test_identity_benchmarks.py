"""Milestone 3.5 — permanent benchmarks for the human understanding layer.

Each benchmark encodes the audit's forbidden answer (what a senior engineer
would reject) so a reasoning regression cannot silently return. Run WITHOUT an
LLM; assertions target the deterministic evidence and the rendered text.

Sections covered:
  B1 §2/§3  Explain ordering: purpose before architecture, not arch-first.
  B2 §3     Purpose first, never inverted.
  B3 §4     Recommendation needs implementation evidence, not shared concept.
  B4 §5     Human-centric "continue?" combines signals, not commit counts alone.
  B5 §6     Purpose recovery from deterministic evidence + "not enough evidence".
  B6 §7     Entry points separated: Application / Framework root / Utility.
  B7 §8     Weak relationships hidden unless explicitly requested.
  B8 §9     Similarity compares architecture, not a bare dependency list.
  B9 §10    "Which should I continue?" ranks by combined signal.
  B10 §11   Explanation is prose, not a one-line label.
  B11 §1    Identity persists across re-analysis (built from stable facts).
  B12       Before/after: old terse "Next.js project. Pages Router. 80 files." gone.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from friday import query as q
from friday.db import (
    ComponentRow,
    EntryPointRow,
    Repository,
    connect,
    get_all_relationships,
    replace_all_relationships,
    replace_children,
    replace_components,
    replace_entry_points,
    set_repo_quality,
    upsert_repository,
)
from friday.identity import (
    build_identity,
    entry_point_groups,
    explain_project_from_conn,
    recover_purpose,
)
from friday.ask import ask, classify
from friday.summary import build_views, infer_relationship_rows
from friday.readme import manifest_description


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _seed(conn, name, path, *, summary=None, langs=(), techs=(), comps=(),
          eps=(), arch=None, dirty=False, first="2024-01-01", last="2026-07-01",
          commits=100, readme_quality=None, maturity=None, author=None,
          license=None, remote=None):
    rid = upsert_repository(
        conn, name=name, path=path, default_branch="main", is_dirty=dirty,
        first_commit_date=first, last_commit_date=last, remote_url=remote,
        commit_count=commits, readme_summary=summary, license=license,
        primary_author=author,
    )
    replace_children(conn, rid, [__lang(l) for l in langs], [__tech(t) for t in techs])
    if comps:
        replace_components(conn, rid, [
            ComponentRow(repo_id=rid, name=n, evidence="e", strength="Weak") for n in comps
        ])
    if eps:
        replace_entry_points(conn, rid, [
            EntryPointRow(repo_id=rid, kind=k, detail=d, evidence="e") for k, d in eps
        ])
    if arch:
        conn.execute(
            "INSERT OR REPLACE INTO architecture (repo_id, architecture, evidence, confidence) "
            "VALUES (?,?,?,?)", (rid, arch[0], arch[1], arch[2] if len(arch) > 2 else "Unknown"))
    set_repo_quality(conn, rid, maturity=maturity, readme_quality=readme_quality,
                     readme_completeness=readme_quality or "none")
    return rid


def __lang(l):
    from friday.db import LangRow
    return LangRow(language=l, file_count=1)


def __tech(t):
    from friday.db import TechRow
    return TechRow(tech=t, evidence="e")


# --- B1 §2/§3 Explain ordering: purpose before architecture -------------------


def test_b1_explain_purpose_before_architecture(conn):
    _seed(conn, "Vivaha", "/v",
          summary="Purpose:\nVivaha is a premium matrimonial platform.\nMaturity:\nBeta",
          langs=("TypeScript",), techs=("Next.js", "Supabase"),
          arch=("Next.js App Router", "app/ dir", "Likely"),
          readme_quality="good", maturity="Beta")
    ans = ask("Explain Vivaha.", conn, verbose=False)
    assert not ans.used_llm
    # Purpose must appear before the architecture label in the rendered text.
    assert "Vivaha — Vivaha is a premium matrimonial platform." in ans.text
    purpose_idx = ans.text.find("matrimonial platform")
    arch_idx = ans.text.find("Next.js App Router")
    assert purpose_idx != -1 and arch_idx != -1
    assert purpose_idx < arch_idx


# --- B2 §3 Purpose first, never inverted -------------------------------------


def test_b2_purpose_first_not_arch_first(conn):
    _seed(conn, "Aether", "/a",
          summary="Purpose:\nAether is an OS in Rust.\nMaturity:\nUnknown",
          langs=("Rust",), techs=("Rust", "SQLite"),
          arch=("Cargo binary", "src/main.rs", "Verified"),
          readme_quality="good")
    ans = ask("What is Aether?", conn, verbose=False)
    assert not ans.used_llm
    assert "Aether — Aether is an OS in Rust." in ans.text
    # Must not open with a bare architecture label.
    assert not ans.text.startswith("Aether — a Cargo binary project")


# --- B3 §4 Recommendation needs implementation evidence ----------------------


def test_b3_recommendation_not_from_shared_concept(conn):
    _seed(conn, "A", "/a", comps=["Database", "Configuration"], arch=("CLI tool", "x"),
          techs=("Python",))
    _seed(conn, "B", "/b", comps=["Database", "Configuration"], arch=("CLI tool", "x"),
          techs=("Python",))
    ans = ask("Which projects could realistically share code?", conn, verbose=False)
    assert not ans.used_llm
    # Shared concept components (Database/Config) must NOT drive a reuse rec.
    assert "Database" not in ans.text or "Realistic" not in ans.text
    assert "Configuration" not in ans.text or "Realistic" not in ans.text


# --- B4 §5 Human-centric "continue?" combines signals -------------------------


def test_b4_continue_combines_signals(conn):
    # Repo C is dirty + high commit share -> should be the top recommendation,
    # not chosen on commit count alone.
    _seed(conn, "C", "/c", dirty=True, commits=900,
          summary="Purpose:\nC is the flagship product.\nMaturity:\nBeta",
          langs=("Python",), techs=("FastAPI",), readme_quality="good", maturity="Beta")
    _seed(conn, "D", "/d", dirty=False, commits=10,
          summary="Purpose:\nD is a side experiment.\nMaturity:\nWIP",
          langs=("Python",), techs=("Flask",), readme_quality="poor", maturity="WIP")
    ans = ask("Which project should I continue?", conn, verbose=False)
    assert not ans.used_llm
    assert "C" in ans.text
    # Commit count alone is not the framing; signals are cited.
    assert "uncommitted changes" in ans.text or "highest-leverage" in ans.text.lower()


# --- B5 §6 Purpose recovery + honest gap -------------------------------------


def test_b5_purpose_recovery_manifest(conn, tmp_path):
    repo_dir = tmp_path / "proj"
    repo_dir.mkdir()
    (repo_dir / "package.json").write_text('{"name":"proj","description":"A CLI for invoice reconciliation."}')
    (repo_dir / ".git").mkdir()
    rid = _seed(conn, "proj", str(repo_dir),
                langs=("JavaScript",), techs=("Node.js",), arch=("CLI tool", "x"))
    repo = q.all_repositories(conn)[0]
    purpose, sources = recover_purpose(repo, conn)
    assert purpose is not None
    assert "invoice reconciliation" in purpose
    assert "manifest description" in sources


def test_b5b_purpose_missing_is_honest(conn):
    _seed(conn, "Mystery", "/m", langs=(), techs=(), arch=("Unknown", "none"))
    rid = q.repo_by_name(conn, "Mystery").id
    ident = build_identity(conn, rid)
    assert ident.purpose is None
    ans = ask("Explain Mystery.", conn, verbose=False)
    assert "don't have enough evidence" in ans.text or "not enough evidence" in ans.text


# --- B6 §7 Entry points separated --------------------------------------------


def test_b6_entry_point_separation():
    from friday.architecture import EntryPoint
    eps = [
        EntryPointRow(repo_id=1, kind="Next.js app", detail="app/", evidence="e"),
        EntryPointRow(repo_id=1, kind="main()", detail="scripts/fix.py", evidence="e"),
        EntryPointRow(repo_id=1, kind="Utility script", detail="scripts/fix-layouts.sh", evidence="e"),
    ]
    g = entry_point_groups(eps)
    assert any(e.kind == "Next.js app" for e in g.application)
    assert "app/" in g.framework_root
    assert any(e.kind == "Utility script" for e in g.utility)
    # Utility scripts must never leak into the application entry set.
    assert not any(e.kind == "Utility script" for e in g.application)


# --- B7 §8 Weak relationships hidden unless requested ------------------------


def test_b7_weak_relationships_hidden(conn):
    _seed(conn, "A", "/a", author="dev@x.com", techs=("Python",))
    _seed(conn, "B", "/b", author="dev@x.com", techs=("Go",))
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    # Default: weak (shared-author) must NOT be presented as a relationship.
    ans = ask("How is A related to B?", conn, verbose=False)
    assert "no strong or medium relationships" in ans.text.lower()
    assert "omitted" in ans.text.lower()
    # The weak relationship is only surfaced when explicitly requested.
    ans2 = ask("How is A related to B, including weak relationships?", conn, verbose=False)
    assert "shared author" in ans2.text.lower()


# --- B8 §9 Similarity compares architecture, not deps ------------------------


def test_b8_similarity_compares_dimensions(conn):
    _seed(conn, "A", "/a", techs=("FastAPI", "SQLite", "Docker"),
          arch=("FastAPI REST API", "f"), comps=["Database"],
          eps=[("FastAPI app", "main.py")])
    _seed(conn, "B", "/b", techs=("FastAPI", "SQLite", "Docker"),
          arch=("FastAPI REST API", "f"), comps=["Database"],
          eps=[("FastAPI app", "app.py")])
    ans = ask("Which projects could teach each other something?", conn, verbose=False)
    assert not ans.used_llm
    # Dimension-based: architecture + persistence + interface, not "both use X".
    assert "FastAPI REST API" in ans.text
    assert "persistence" in ans.text.lower() or "SQLite" in ans.text


# --- B9 §10 "Which should I continue?" ranks by combined signal -------------


def test_b9_continue_ranks_by_signal(conn):
    _seed(conn, "Top", "/top", dirty=True, commits=700,
          summary="Purpose:\nTop is the core service.\nMaturity:\nBeta",
          langs=("Python",), techs=("FastAPI",), readme_quality="good", maturity="Beta")
    _seed(conn, "Side", "/side", dirty=False, commits=5,
          summary="Purpose:\nSide is experimental.\nMaturity:\nWIP",
          langs=("Python",), techs=("Flask",), readme_quality="poor", maturity="WIP")
    ans = ask("Which project should I continue?", conn, verbose=False)
    # Top should lead the suggestion.
    assert ans.text.startswith("If you want the highest-leverage next step, continue Top.")


# --- B10 §11 Explanation is prose, not a one-liner ---------------------------


def test_b10_explanation_is_prose(conn):
    _seed(conn, "Vivaha", "/v",
          summary="Purpose:\nVivaha is a premium matrimonial platform.\nMaturity:\nBeta",
          langs=("TypeScript",), techs=("Next.js", "Supabase"),
          arch=("Next.js App Router", "app/ dir", "Likely"),
          readme_quality="good", maturity="Beta")
    ans = ask("Explain Vivaha.", conn, verbose=False)
    # Multi-clause prose, not a single label.
    assert ans.text.count(".") >= 3
    assert "Major technologies" in ans.text or "technologies" in ans.text


# --- B11 §1 Identity persists across re-analysis -----------------------------


def test_b11_identity_recomputed_persistently(conn):
    rid = _seed(conn, "Vivaha", "/v",
                summary="Purpose:\nVivaha is a premium matrimonial platform.\nMaturity:\nBeta",
                langs=("TypeScript",), techs=("Next.js", "Supabase"),
                arch=("Next.js App Router", "app/ dir", "Likely"),
                readme_quality="good", maturity="Beta")
    # Recompute (simulating a later `analyze`) — identity must be stable because
    # every input is already persisted.
    ident1 = build_identity(conn, rid)
    ident2 = build_identity(conn, rid)
    assert ident1.purpose == ident2.purpose == "Vivaha is a premium matrimonial platform."


# --- B12 Before/after: old terse form is gone --------------------------------


def test_b12_no_terse_one_liner(conn):
    _seed(conn, "Vivaha", "/v",
          summary="Purpose:\nVivaha is a premium matrimonial platform.\nMaturity:\nBeta",
          langs=("TypeScript",), techs=("Next.js", "Supabase"),
          arch=("Next.js App Router", "app/ dir", "Likely"),
          readme_quality="good", maturity="Beta")
    ans = ask("Explain Vivaha.", conn, verbose=False)
    # The forbidden terse form from the brief must not appear.
    assert ans.text != "Next.js project. Pages Router. 80 files."
    assert "Vivaha is a premium matrimonial platform" in ans.text
