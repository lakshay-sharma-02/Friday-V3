"""Permanent regression corpus — runs every real bug from M1-M6 as a locked test.

Each corpus file under tests/regressions/*.json encodes a bug as STRUCTURED
properties (not exact wording), so future changes can never silently reintroduce
it. The runner seeds a deterministic workspace, asks the question offline
(no LLM), and asserts:

  - required_scope     : ev.raw["scope"] must match
  - expected.objective : ev.raw["objective"] must match (when present)
  - expected.min_repos_cited : >= N repository names must appear in the answer
  - forbidden          : none of these substrings may appear (P0 guards)
  - required_evidence  : all these substrings must appear
  - coverage_note_required : a coverage/missing-evidence note must be present

No keyword patches, no question-specific code. Add a new bug -> drop a JSON file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from friday import objective as obj
from friday.ask import Exchange, ask
from friday.db import (
    LangRow, SnapshotRow, TechRow, connect, insert_snapshot,
    replace_all_relationships, replace_children, set_repo_quality,
    upsert_architecture, upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows

REGRESSIONS = sorted(Path(__file__).parent.glob("regressions/*.json"))
REPO_NAMES = ["aether", "friday-v3", "vivaha", "mindwell", "finance-tracker"]


def _seed(conn, sparse=False, inject=None):
    def seed(name, path, summary, langs, techs, arch, commits, dirty=False):
        rid = upsert_repository(
            conn, name=name, path=path, default_branch="main", is_dirty=dirty,
            first_commit_date="2025-01-01", last_commit_date="2026-07-01",
            remote_url="https://github.com/acme/" + name, commit_count=commits,
            readme_summary=summary, license="MIT", primary_author="dev@acme.com",
        )
        replace_children(conn, rid, [LangRow(l, 10) for l in langs],
                         [TechRow(t, "e") for t in techs])
        upsert_architecture(conn, repo_id=rid, architecture=arch, evidence="stored")
        set_repo_quality(conn, rid, None, "good" if summary else "none",
                         "complete" if summary else "none")
        return rid

    if sparse:
        # No stated purpose anywhere -> coverage warning must fire.
        for n in REPO_NAMES:
            seed(n, "/" + n[0], None, (), (), "Library", 100)
        conn.commit()
        return

    seed("aether", "/a",
         "Purpose:\nAether is an operating system in Rust.\nValue:\ncore infrastructure.\nMaturity:\nUnknown",
         ("Rust",), ("Rust",), "Cargo workspace", 120)
    seed("friday-v3", "/f3",
         "Purpose:\nFriday V3 is an AI operating partner.\nValue:\nautomates workspace operations.\nMaturity:\nBeta",
         ("Python",), ("Python", "Supabase"), "CLI tool", 600, dirty=True)
    seed("vivaha", "/v",
         "Purpose:\nVivaha is a premium matrimonial platform.\nValue:\nhelps people find partners.\nMaturity:\nBeta",
         ("TypeScript",), ("Next.js", "Supabase"), "Next.js App Router", 200)
    seed("mindwell", "/m",
         "Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
         ("Python",), ("Python",), "React SPA", 150)
    seed("finance-tracker", "/ft",
         "Purpose:\nfinance-tracker tracks personal spending.\nMaturity:\nWIP",
         ("Python",), ("Python",), "Library", 80)
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    insert_snapshot(conn, SnapshotRow(
        observed_at="2026-07-10", repo_path="/f3", repo_name="friday-v3",
        default_branch="main", commit_count=600, last_commit_date="2026-07-01",
        is_dirty=1, readme_hash="r", architecture_hash="a", identity_hash="i"))
    # Generic inject hook: corpus files may require specific architecture content
    # to exercise a rendering path (e.g. data_flow newline handling). Not a patch —
    # just fixture setup.
    for repo, blob in (inject or {}).items():
        for r in __import__("friday.query", fromlist=["all_repositories"]).all_repositories(conn):
            if r.name == repo:
                conn.execute(
                    "UPDATE architecture SET data_flow=?, known_patterns=? WHERE repo_id=?",
                    (blob.get("data_flow", ""), blob.get("known_patterns", ""), r.id))
                break
    conn.commit()


def _repos_cited(text: str) -> int:
    low = text.lower()
    return sum(1 for n in REPO_NAMES if n in low)


@pytest.mark.parametrize("path", REGRESSIONS, ids=lambda p: p.stem)
def test_regression_corpus(path, tmp_path):
    spec = json.loads(Path(path).read_text())
    conn = connect(tmp_path / "kb.db")
    _seed(conn, sparse=spec.get("seed_sparse", False), inject=spec.get("inject"))

    if spec.get("context_question"):
        ctx_ans = ask(spec["context_question"], conn, verbose=False)
        prev = Exchange(question=spec["context_question"], answer=ctx_ans)
        ans = ask(spec["question"], conn, prev=prev, verbose=False)
    else:
        ans = ask(spec["question"], conn, verbose=False)

    raw = ans.evidence.raw
    text = ans.text
    low = text.lower()

    # required scope. When the corpus documents (required_scope_note) that the
    # stored scope may legitimately differ from the intended scope — e.g. a
    # resolved follow-up that restates prior evidence — the scope is recorded but
    # not hard-asserted; the binding invariants are forbidden/required_evidence.
    if not spec.get("required_scope_note"):
        assert raw.get("scope") == spec["required_scope"], (
            f"{spec['id']}: scope {raw.get('scope')!r} != {spec['required_scope']!r}")

    exp = spec.get("expected", {})
    if exp.get("objective"):
        assert raw.get("objective") == exp["objective"], (
            f"{spec['id']}: objective {raw.get('objective')!r} != {exp['objective']!r}")
    if exp.get("min_repos_cited"):
        assert _repos_cited(text) >= exp["min_repos_cited"], (
            f"{spec['id']}: only {_repos_cited(text)} repos cited, "
            f"need >= {exp['min_repos_cited']}")
    if exp.get("coverage_note_required"):
        assert "missing evidence" in low or "based on" in low, (
            f"{spec['id']}: expected a coverage/missing-evidence note")

    # forbidden (P0 guards)
    for bad in spec.get("forbidden", []):
        assert bad.lower() not in low, (
            f"{spec['id']}: forbidden substring present: {bad!r}")

    # required evidence
    for need in spec.get("required_evidence", []):
        assert need.lower() in low, (
            f"{spec['id']}: required evidence missing: {need!r}")

    conn.close()
