"""Relationship inference and cross-project observations — from stored-like views."""

from __future__ import annotations

from datetime import date, timedelta

from friday.db import LangRow, Repository, TechRow
from friday.summary import RepoView, cross_project_observations, infer_relationships


def _view(name, *, langs=(), techs=(), first=None, last=None, commits=None, author=None, remote=None, dirty=False):
    return RepoView(
        repo=Repository(
            id=1,
            name=name,
            path=f"/x/{name}",
            default_branch="main",
            is_dirty=dirty,
            first_commit_date=first,
            last_commit_date=last,
            remote_url=remote,
            commit_count=commits,
            readme_summary=None,
            license=None,
            primary_author=author,
            ingestion_time="now",
        ),
        languages=[LangRow(language=l, file_count=1) for l in langs],
        technologies=[TechRow(tech=t, evidence="e") for t in techs],
    )


def test_shared_language_and_tech():
    a = _view("A", langs=("Python",), techs=("FastAPI", "SQLite"))
    b = _view("B", langs=("Python",), techs=("Flask", "SQLite"))
    rels = infer_relationships([a, b])
    kinds = {(r.kind, r.a, r.b) for r in rels}
    assert ("shared-language", "A", "B") in kinds
    # SQLite is a database tech -> surfaced as shared-db (higher priority than shared-tech).
    assert ("shared-db", "A", "B") in kinds
    # shared-db must outrank the generic shared-tech fallback.
    assert all(r.kind != "shared-tech" for r in rels if "SQLite" in r.evidence)


def test_shared_org():
    a = _view("A", remote="https://github.com/acme/proj1")
    b = _view("B", remote="https://github.com/acme/proj2")
    rels = infer_relationships([a, b])
    assert any(r.kind == "shared-org" for r in rels)


def test_independent_repos_no_relationship():
    a = _view("A", langs=("Rust",), techs=("Cargo",))
    b = _view("B", langs=("Go",), techs=("Docker",))
    assert infer_relationships([a, b]) == []


def test_duplicate_tech_observation():
    a = _view("A", techs=("SQLite",))
    b = _view("B", techs=("SQLite",))
    c = _view("C", techs=("SQLite",))
    obs = cross_project_observations([a, b, c], infer_relationships([a, b, c]), date.today())
    # Wording fixed: sharing a tech is "use", not "duplicate configuration".
    assert any("3 repositories use SQLite" in o.text for o in obs)


def test_stale_repo_observation():
    old = _view("Old", last=(date.today() - timedelta(days=200)).isoformat())
    obs = cross_project_observations([old], [], date.today())
    assert any("has not been modified recently" in o.text for o in obs)


def test_highest_commit_frequency():
    a = _view("A", first=(date.today() - timedelta(days=10)).isoformat(),
              last=date.today().isoformat(), commits=100)
    b = _view("B", first=(date.today() - timedelta(days=10)).isoformat(),
              last=date.today().isoformat(), commits=20)
    obs = cross_project_observations([a, b], [], date.today())
    assert any("highest commit frequency" in o.text and "A has" in o.text for o in obs)


def test_largest_project():
    a = _view("A", langs=("Python",), )  # file_count default 1 each
    big = RepoView(
        repo=_view("Big").repo,
        languages=[LangRow(language="Go", file_count=500)],
        technologies=[],
    )
    obs = cross_project_observations([a, big], [], date.today())
    assert any("largest project" in o.text and "Big is" in o.text for o in obs)
