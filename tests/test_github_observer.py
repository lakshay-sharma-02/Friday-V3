"""Milestone 7.5 — GitHub Observer tests.

Deterministic tests for GitHubObserver: it reads repository *metadata* through
an offline FixtureProvider and emits engineering observations that plug into
the frozen Observation Engine. No network, no GitHub client, no webhook, no LLM.

Coverage: repository metadata, issue, PR, merge, CI success, CI failure,
release, review, health, registration, engine integration, offline fixtures,
privacy (no bodies/diffs/comments), and derived engineering signals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday.db import connect, observations_all
from friday.observation import (
    Confidence,
    GitHubObserver,
    ObservationEngine,
    ObserverRegistry,
    RepositorySnapshot,
    default_registry,
)
from friday.observation.github_observer import FixtureProvider


def _friday(slug="Friday/Vivaha", **over):
    base = dict(
        full_name=slug, default_branch="main", stars=120, forks=14,
        open_issue_count=3, closed_issue_count=27, open_pr_count=1,
        merged_pr_count=5, archived=False, private=False, visibility="public",
        branch_protection=True, draft_pr_count=0,
        recent_commits=[{"date": "2026-07-12T10:00:00+00:00"}],
        recent_releases=[{"tag": "1.2.0", "published_at": "2026-07-10T09:00:00+00:00"}],
        workflows=[{"name": "Tests", "status": "completed", "conclusion": "success"}],
        pull_requests=[{"number": 42, "state": "open", "draft": False,
                        "review_state": "requested", "created_at": "2026-07-11T09:00:00+00:00"}],
        issues=[{"number": 7, "state": "open", "assigned": True,
                 "created_at": "2026-07-09T09:00:00+00:00"}],
    )
    base.update(over)
    return base


def _observer(snaps):
    return GitHubObserver(FixtureProvider(snaps))


# --- Repository metadata ----------------------------------------------------


def test_repository_metadata_observed():
    obs = {(o.subject, o.aspect): o for o in _observer([_friday()]).collect(None)}
    assert obs[("Friday/Vivaha", "default_branch")].value == "main"
    assert obs[("Friday/Vivaha", "stars")].value == "120"
    assert obs[("Friday/Vivaha", "forks")].value == "14"
    assert obs[("Friday/Vivaha", "open_issues")].value == "3"
    assert obs[("Friday/Vivaha", "closed_issues")].value == "27"
    assert obs[("Friday/Vivaha", "open_prs")].value == "1"
    assert obs[("Friday/Vivaha", "merged_prs")].value == "5"
    assert obs[("Friday/Vivaha", "archived")].value == "false"
    assert obs[("Friday/Vivaha", "visibility")].value == "public"
    assert obs[("Friday/Vivaha", "branch_protection")].value == "true"
    assert obs[("Friday/Vivaha", "draft_prs")].value == "0"
    assert obs[("Friday/Vivaha", "recent_commits")].value == "1"
    for k in ("stars", "forks", "open_issues", "default_branch",
              "archived", "visibility", "branch_protection"):
        assert obs[("Friday/Vivaha", k)].confidence is Confidence.OBSERVED


# --- Issue ------------------------------------------------------------------


def test_issue_assigned_observed():
    obs = {(o.subject, o.aspect): o for o in _observer([_friday()]).collect(None)}
    assert obs[("Friday/Vivaha#7", "issue_assigned")].value == "true"
    assert obs[("Friday/Vivaha#7", "issue_assigned")].confidence is Confidence.OBSERVED
    # Unassigned issue.
    snap = _friday(issues=[{"number": 8, "state": "open", "assigned": False,
                            "created_at": "2026-07-09T09:00:00+00:00"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha#8", "issue_assigned")].value == "false"


# --- PR ---------------------------------------------------------------------


def test_pr_opened_observed():
    obs = {(o.subject, o.aspect): o for o in _observer([_friday()]).collect(None)}
    assert obs[("Friday/Vivaha#42", "pr_state")].value == "open"
    assert obs[("Friday/Vivaha#42", "pr_draft")].value == "false"
    assert obs[("Friday/Vivaha#42", "review_state")].value == "requested"
    assert obs[("Friday/Vivaha#42", "pr_state")].confidence is Confidence.OBSERVED


def test_draft_pr_observed():
    snap = _friday(pull_requests=[{"number": 9, "state": "open", "draft": True,
                                   "review_state": "none",
                                   "created_at": "2026-07-11T09:00:00+00:00"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha#9", "pr_draft")].value == "true"


# --- Merge ------------------------------------------------------------------


def test_pr_merged_observed():
    snap = _friday(merged_pr_count=1,
                   pull_requests=[{"number": 10, "state": "merged", "merged": True,
                                   "draft": False, "review_state": "approved",
                                   "created_at": "2026-07-01T09:00:00+00:00",
                                   "merged_at": "2026-07-13T09:00:00+00:00"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha#10", "pr_state")].value == "merged"
    assert obs[("Friday/Vivaha#10", "review_state")].value == "approved"
    assert obs[("Friday/Vivaha", "merged_prs")].value == "1"


# --- CI success / failure ---------------------------------------------------


def test_ci_success_observed():
    obs = {(o.subject, o.aspect): o for o in _observer([_friday()]).collect(None)}
    assert obs[("Friday/Vivaha/Tests", "ci_status")].value == "success"
    assert obs[("Friday/Vivaha", "workflow_success")].value == "1"
    assert obs[("Friday/Vivaha", "workflow_failures")].value == "0"


def test_ci_failure_observed():
    snap = _friday(workflows=[{"name": "Tests", "status": "completed",
                              "conclusion": "failure"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha/Tests", "ci_status")].value == "failure"
    assert obs[("Friday/Vivaha", "workflow_failures")].value == "1"
    assert obs[("Friday/Vivaha", "workflow_success")].value == "0"


def test_cancelled_workflow_is_failure():
    snap = _friday(workflows=[{"name": "Tests", "status": "completed",
                              "conclusion": "cancelled"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha/Tests", "ci_status")].value == "failure"


# --- Release ----------------------------------------------------------------


def test_release_published_observed():
    obs = {(o.subject, o.aspect): o for o in _observer([_friday()]).collect(None)}
    assert obs[("Friday/Vivaha@1.2.0", "release")].value == "published"
    assert obs[("Friday/Vivaha@1.2.0", "release")].confidence is Confidence.OBSERVED
    assert "1.2.0" in (obs[("Friday/Vivaha@1.2.0", "release")].cause or "")


# --- Review -----------------------------------------------------------------


def test_review_counts_observed():
    snap = _friday(pull_requests=[
        {"number": 1, "state": "open", "draft": False, "review_state": "requested",
         "created_at": "2026-07-11T09:00:00+00:00"},
        {"number": 2, "state": "open", "draft": False, "review_state": "approved",
         "created_at": "2026-07-11T09:00:00+00:00"},
        {"number": 3, "state": "open", "draft": False, "review_state": "changes_requested",
         "created_at": "2026-07-11T09:00:00+00:00"},
    ])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha", "review_requested")].value == "1"
    assert obs[("Friday/Vivaha", "review_approved")].value == "1"
    assert obs[("Friday/Vivaha", "review_changes_requested")].value == "1"


# --- Health -----------------------------------------------------------------


def test_health_healthy_with_repos():
    h = _observer([_friday()]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


def test_health_healthy_when_empty():
    # No repos configured => still healthy ("nothing to observe").
    h = _observer([]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


# --- Registration -----------------------------------------------------------


def test_github_registered_in_default_registry():
    assert "github" in default_registry()
    assert "git" in default_registry()
    assert "terminal" in default_registry()


def test_register_duplicate_raises():
    reg = ObserverRegistry()
    reg.register(GitHubObserver(FixtureProvider([])))
    with pytest.raises(ValueError):
        reg.register(GitHubObserver(FixtureProvider([])))


# --- Engine integration -----------------------------------------------------


def test_end_to_end_through_observation_engine(tmp_path):
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(_observer([_friday()]))
    run = ObservationEngine(reg, conn).run()
    conn.close()
    assert run.observers[0].name == "github"
    assert run.observers[0].health.healthy
    stored = observations_all(connect(tmp_path / "kb.db"))
    aspects = {(o.subject, o.aspect) for o in stored}
    assert ("Friday/Vivaha", "stars") in aspects
    assert ("Friday/Vivaha#42", "pr_state") in aspects
    assert ("Friday/Vivaha@1.2.0", "release") in aspects
    assert all(o.source == "github" for o in stored)


def test_observation_ids_deterministic_and_idempotent(tmp_path):
    obs = _observer([_friday()])
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(obs)
    ObservationEngine(reg, conn).run()
    ids1 = {o.id for o in observations_all(conn)}
    ObservationEngine(reg, conn).run()
    ids2 = {o.id for o in observations_all(conn)}
    assert ids1 == ids2


def test_engine_emits_pr_opened_as_change(tmp_path):
    # First run: nothing. Second run: a PR appears -> engine diffs as change.
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(_observer([_friday(pull_requests=[])]))
    ObservationEngine(reg, conn).run()
    reg2 = ObserverRegistry()
    reg2.register(_observer([_friday()]))
    run = ObservationEngine(reg2, conn).run()
    conn.close()
    changes = run.observers[0].changes
    kinds = [(c.subject, c.kind, c.new) for c in changes]
    assert ("Friday/Vivaha#42", "pr_state observed", "open") in kinds


# --- Offline fixtures -------------------------------------------------------


def test_offline_fixture_file(tmp_path):
    snap = tmp_path / "github.json"
    snap.write_text(json.dumps([_friday("Aether/Core")]), encoding="utf-8")
    from friday.observation.github_observer import FixtureProvider as FP
    obs = {(o.subject, o.aspect): o for o in
           GitHubObserver(FP(snap)).collect(None)}
    assert obs[("Aether/Core", "stars")].value == "120"


def test_offline_snapshot_env_used(tmp_path):
    snap = tmp_path / "github.json"
    snap.write_text(json.dumps([_friday("Nova/Edge")]), encoding="utf-8")
    import os
    os.environ["FRIDAY_GITHUB_SNAPSHOT"] = str(snap)
    try:
        from friday.observation.github_observer import default_provider
        obs = {(o.subject, o.aspect): o for o in
               GitHubObserver(default_provider()).collect(None)}
    finally:
        os.environ.pop("FRIDAY_GITHUB_SNAPSHOT", None)
    assert obs[("Nova/Edge", "stars")].value == "120"


def test_empty_fixture_yields_only_workspace_count():
    obs = {(o.subject, o.aspect): o for o in _observer([]).collect(None)}
    # Only the workspace repository count is emitted; no per-repo facts.
    assert obs == {("github", "repositories"): obs[("github", "repositories")]}
    assert obs[("github", "repositories")].value == "0"


# --- Privacy guarantees -----------------------------------------------------


def test_no_pr_or_issue_bodies_emitted():
    snap = _friday(
        pull_requests=[{"number": 42, "state": "open", "draft": False,
                        "review_state": "requested",
                        "created_at": "2026-07-11T09:00:00+00:00",
                        "title": "Add frobnication",
                        "body": "SECRET PR BODY with diffs",
                        "diff": "--- a/file.py\n-removed",
                        "comments": ["internal comment"]}],
        issues=[{"number": 7, "state": "open", "assigned": True,
                 "created_at": "2026-07-09T09:00:00+00:00",
                 "body": "SECRET ISSUE BODY",
                 "comments": ["private message"]}],
    )
    obs = GitHubObserver(FixtureProvider([snap])).collect(None)
    blob = json.dumps([o.__dict__ for o in obs])
    assert "SECRET PR BODY" not in blob
    assert "SECRET ISSUE BODY" not in blob
    assert "internal comment" not in blob
    assert "private message" not in blob
    assert "removed" not in blob  # no diff leak
    # Only whitelisted aspects emitted.
    ALLOWED = {
        "default_branch", "stars", "forks", "open_issues", "closed_issues",
        "open_prs", "merged_prs", "archived", "visibility", "branch_protection",
        "draft_prs", "recent_commits", "workflow_failures", "workflow_success",
        "review_requested", "review_approved", "review_changes_requested",
        "pr_state", "pr_draft", "review_state", "issue_assigned",
        "release", "ci_status", "repositories",
        "repository_inactive", "repeated_ci_failures", "high_review_activity",
        "release_cadence", "merge_frequency", "long_lived_pr", "stale_issue",
    }
    assert all(o.aspect in ALLOWED for o in obs)


def test_no_token_or_secret_fields_emitted():
    snap = _friday(pull_requests=[{"number": 1, "state": "open", "draft": False,
                                   "review_state": "none",
                                   "created_at": "2026-07-11T09:00:00+00:00",
                                   "token": "ghp_secret", "password": "hunter2"}])
    obs = GitHubObserver(FixtureProvider([snap])).collect(None)
    blob = json.dumps([o.__dict__ for o in obs])
    assert "ghp_secret" not in blob
    assert "hunter2" not in blob


# --- Engineering signals (derived / inferred) -------------------------------


def test_repeated_ci_failures_inferred():
    snap = _friday(workflows=[
        {"name": "Tests", "status": "completed", "conclusion": "failure"},
        {"name": "Lint", "status": "completed", "conclusion": "failure"},
    ])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha", "repeated_ci_failures")].value == "true"
    assert obs[("Friday/Vivaha", "repeated_ci_failures")].confidence is Confidence.INFERRED
    assert "2" in (obs[("Friday/Vivaha", "repeated_ci_failures")].cause or "")


def test_single_ci_failure_not_inferred():
    snap = _friday(workflows=[{"name": "Tests", "status": "completed",
                              "conclusion": "failure"}])
    obs = [o for o in _observer([snap]).collect(None)
           if o.aspect == "repeated_ci_failures"]
    assert obs == []


def test_repository_inactive_inferred():
    snap = _friday(recent_commits=[{"date": "2026-05-01T10:00:00+00:00"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha", "repository_inactive")].value == "true"
    assert obs[("Friday/Vivaha", "repository_inactive")].confidence is Confidence.INFERRED


def test_high_review_activity_derived():
    prs = [{"number": i, "state": "open", "draft": False,
            "review_state": "approved",
            "created_at": "2026-07-11T09:00:00+00:00"}
           for i in range(1, 7)]  # 6 reviews >= threshold 5
    obs = {(o.subject, o.aspect): o for o in _observer([_friday(pull_requests=prs)]).collect(None)}
    assert obs[("Friday/Vivaha", "high_review_activity")].value == "true"
    assert obs[("Friday/Vivaha", "high_review_activity")].confidence is Confidence.DERIVED


def test_release_cadence_derived():
    snap = _friday(recent_releases=[
        {"tag": "1.2.0", "published_at": "2026-07-10T09:00:00+00:00"},
        {"tag": "1.1.0", "published_at": "2026-07-05T09:00:00+00:00"},
    ])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha", "release_cadence")].value == "2"


def test_long_lived_pr_inferred():
    snap = _friday(pull_requests=[{"number": 3, "state": "open", "draft": False,
                                   "review_state": "none",
                                   "created_at": "2026-06-01T09:00:00+00:00"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha#3", "long_lived_pr")].value == "true"
    assert obs[("Friday/Vivaha#3", "long_lived_pr")].confidence is Confidence.INFERRED


def test_stale_issue_inferred():
    snap = _friday(issues=[{"number": 5, "state": "open", "assigned": False,
                            "created_at": "2026-03-01T09:00:00+00:00"}])
    obs = {(o.subject, o.aspect): o for o in _observer([snap]).collect(None)}
    assert obs[("Friday/Vivaha#5", "stale_issue")].value == "true"


# --- Summary ----------------------------------------------------------------


def test_summary_counts(tmp_path):
    conn = connect(tmp_path / "kb.db")
    summary = _observer([_friday(), _friday("Vivaha/CLI")]).summarize(conn)
    conn.close()
    assert "Repositories\n2" in summary
    assert "Open PRs\n2" in summary
    assert "Merged today\n0" in summary
    assert "CI failures\n0" in summary
    assert "Recent releases\n2" in summary


def test_summary_healthy_header():
    assert GitHubObserver(FixtureProvider([])).summarize(None).startswith(
        "GitHub Observer\nHealthy")


# --- Snapshot model ---------------------------------------------------------


def test_repository_snapshot_from_dict_normalizes():
    s = RepositorySnapshot.from_dict({"full_name": "A/B", "stars": "12"})
    assert s.full_name == "A/B"
    assert s.stars == 12
    assert s.visibility == "public"
    assert s.recent_commits == []
