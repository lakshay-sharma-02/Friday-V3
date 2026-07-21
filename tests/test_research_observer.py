"""Milestone 7.6 — Research Observer tests.

Deterministic tests for ResearchObserver: it reads engineering research
*resource metadata* through an offline FixtureProvider and emits engineering
observations that plug into the frozen Observation Engine. No browser, no
network, no telemetry, no LLM.

Coverage: documentation, RFC, paper, API reference, blog, unknown classification,
privacy (no contents/query/cookies/personal), registration, health, summary,
engine integration, offline fixtures, and derived engineering signals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday.db import connect, observations_all
from friday.observation import (
    Category,
    Confidence,
    ObservationEngine,
    ObserverRegistry,
    ResearchObserver,
    ResearchResource,
    classify_research,
    default_registry,
    topic_of,
)
from friday.observation.research_observer import FixtureProvider


def _res(url, **over):
    base = dict(url=url, title="", timestamp="2026-07-12T10:00:00+00:00",
                duration_s=None, language=None, bookmarked=False,
                read_completion=None, repeated_visits=1, category=None)
    base.update(over)
    return base


def _observer(resources):
    return ResearchObserver(FixtureProvider(resources))


# --- Classification (deterministic, no LLM) --------------------------------


def test_classify_documentation_by_host():
    assert classify_research("docs.rs") == Category.DOCUMENTATION
    assert classify_research("doc.rust-lang.org") == Category.DOCUMENTATION
    assert classify_research("docs.python.org") == Category.DOCUMENTATION
    assert classify_research("www.kernel.org") == Category.DOCUMENTATION


def test_classify_rfc_by_host():
    assert classify_research("datatracker.ietf.org") == Category.RFC
    assert classify_research("rfc-editor.org") == Category.RFC


def test_classify_research_paper_by_host():
    assert classify_research("arxiv.org") == Category.RESEARCH_PAPER
    assert classify_research("dl.acm.org") == Category.RESEARCH_PAPER


def test_classify_api_reference_by_host():
    assert classify_research("platform.openai.com") == Category.API_REFERENCE
    assert classify_research("api.stripe.com") == Category.API_REFERENCE
    assert classify_research("supabase.com") == Category.API_REFERENCE


def test_classify_unknown():
    assert classify_research("example.com") == Category.UNKNOWN
    assert classify_research("") == Category.UNKNOWN


def test_classify_explicit_category_overrides():
    assert classify_research("example.com", Category.BLOG) == Category.BLOG


def test_classify_video_host():
    assert classify_research("youtube.com") == Category.VIDEO
    assert classify_research("youtu.be") == Category.VIDEO


# --- Documentation resource ------------------------------------------------


def test_documentation_resource_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_res("https://docs.rs/tokio")]).collect(None)}
    subj = "https://docs.rs/tokio"
    assert obs[(subj, "host")].value == "docs.rs"
    assert obs[(subj, "category")].value == Category.DOCUMENTATION
    assert obs[(subj, "category")].confidence is Confidence.OBSERVED
    assert obs[(subj, "visited_at")].value == "2026-07-12T10:00:00+00:00"


# --- RFC -------------------------------------------------------------------


def test_rfc_resource_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_res("https://datatracker.ietf.org/doc/rfc2616")]).collect(None)}
    subj = "https://datatracker.ietf.org/doc/rfc2616"
    assert obs[(subj, "category")].value == Category.RFC
    assert obs[(subj, "host")].value == "datatracker.ietf.org"


# --- Research paper --------------------------------------------------------


def test_paper_resource_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_res("https://arxiv.org/abs/2401.12345")]).collect(None)}
    subj = "https://arxiv.org/abs/2401.12345"
    assert obs[(subj, "category")].value == Category.RESEARCH_PAPER


# --- API reference ---------------------------------------------------------


def test_api_reference_resource_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_res("https://platform.openai.com/docs/api-reference")]).collect(None)}
    subj = "https://platform.openai.com/docs/api-reference"
    assert obs[(subj, "category")].value == Category.API_REFERENCE


# --- Blog ------------------------------------------------------------------


def test_blog_resource_explicit_category():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_res("https://blog.rust-lang.org/2026", category=Category.BLOG)]).collect(None)}
    subj = "https://blog.rust-lang.org/2026"
    assert obs[(subj, "category")].value == Category.BLOG


# --- Unknown ---------------------------------------------------------------


def test_unknown_resource_classified_unknown():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_res("https://example.com/page")]).collect(None)}
    subj = "https://example.com/page"
    assert obs[(subj, "category")].value == Category.UNKNOWN


# --- Metadata fields --------------------------------------------------------


def test_optional_metadata_observed():
    obs = {(o.subject, o.aspect): o for o in _observer([_res(
        "https://docs.rs/tokio", title="Tokio docs", duration_s=42.5,
        language="rust", bookmarked=True, read_completion=0.8,
        repeated_visits=3)]).collect(None)}
    subj = "https://docs.rs/tokio"
    assert obs[(subj, "title")].value == "Tokio docs"
    assert obs[(subj, "visit_duration_s")].value == "42.5"
    assert obs[(subj, "language")].value == "rust"
    assert obs[(subj, "bookmarked")].value == "true"
    assert obs[(subj, "read_completion")].value == "0.80"
    assert obs[(subj, "repeated_visits")].value == "3"


# --- Health ----------------------------------------------------------------


def test_health_healthy_with_resources():
    h = _observer([_res("https://docs.rs/tokio")]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


def test_health_healthy_when_empty():
    h = _observer([]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


# --- Registration ----------------------------------------------------------


def test_research_registered_in_default_registry():
    assert "research" in default_registry()
    assert "github" in default_registry()


def test_register_duplicate_raises():
    reg = ObserverRegistry()
    reg.register(ResearchObserver(FixtureProvider([])))
    with pytest.raises(ValueError):
        reg.register(ResearchObserver(FixtureProvider([])))


# --- Engine integration ----------------------------------------------------


def test_end_to_end_through_observation_engine(tmp_path):
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(_observer([_res("https://docs.rs/tokio"),
                            _res("https://datatracker.ietf.org/doc/rfc2616")]))
    run = ObservationEngine(reg, conn).run()
    conn.close()
    assert run.observers[0].name == "research"
    assert run.observers[0].health.healthy
    stored = observations_all(connect(tmp_path / "kb.db"))
    aspects = {(o.subject, o.aspect) for o in stored}
    assert ("https://docs.rs/tokio", "category") in aspects
    assert ("https://datatracker.ietf.org/doc/rfc2616", "category") in aspects
    assert all(o.source == "research" for o in stored)


def test_observation_ids_deterministic_and_idempotent(tmp_path):
    obs = _observer([_res("https://docs.rs/tokio")])
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(obs)
    ObservationEngine(reg, conn).run()
    ids1 = {o.id for o in observations_all(conn)}
    ObservationEngine(reg, conn).run()
    ids2 = {o.id for o in observations_all(conn)}
    assert ids1 == ids2


# --- Offline fixtures ------------------------------------------------------


def test_offline_fixture_file(tmp_path):
    snap = tmp_path / "research.json"
    snap.write_text(json.dumps([_res("https://docs.rs/serde")]), encoding="utf-8")
    from friday.observation.research_observer import FixtureProvider as FP
    obs = {(o.subject, o.aspect): o for o in
           ResearchObserver(FP(snap)).collect(None)}
    assert obs[("https://docs.rs/serde", "host")].value == "docs.rs"


def test_offline_snapshot_env_used(tmp_path):
    snap = tmp_path / "research.json"
    snap.write_text(json.dumps([_res("https://platform.openai.com/x")]),
                    encoding="utf-8")
    import os
    os.environ["FRIDAY_RESEARCH_EXPORT"] = str(snap)
    try:
        from friday.observation.research_observer import default_provider
        obs = {(o.subject, o.aspect): o for o in
               ResearchObserver(default_provider()).collect(None)}
    finally:
        os.environ.pop("FRIDAY_RESEARCH_EXPORT", None)
    assert obs[("https://platform.openai.com/x", "host")].value == "platform.openai.com"


def test_empty_fixture_yields_only_resource_count():
    obs = {(o.subject, o.aspect): o for o in _observer([]).collect(None)}
    assert list(obs.keys()) == [("research", "resources")]
    assert obs[("research", "resources")].value == "0"


# --- Privacy guarantees ----------------------------------------------------


def test_no_page_contents_or_query_or_cookies_emitted():
    res = _res(
        "https://docs.rs/tokio?token=abc#frag", title="Tokio",
        # These fields must be ignored entirely.
        cookies={"session": "secret"}, query="token=abc",
        content="<html>full page body</html>", form_data={"password": "hunter2"},
        html="<body>secret</body>")
    obs = ResearchObserver(FixtureProvider([res])).collect(None)
    blob = json.dumps([o.__dict__ for o in obs])
    # URL query string is stripped before any observation is made.
    assert "https://docs.rs/tokio" in {o.subject for o in obs}
    assert "secret" not in blob
    assert "hunter2" not in blob
    assert "full page body" not in blob
    assert "token=abc" not in blob
    ALLOWED = {
        "host", "title", "category", "language", "visited_at",
        "repeated_visits", "bookmarked", "visit_duration_s",
        "read_completion", "resources",
        "researching_operating_systems", "researching_databases",
        "researching_authentication", "researching_networking",
        "researching_compiler", "researching_filesystem",
        "researching_kernel", "researching_standards",
        "researching_research_papers", "researching_ai_infrastructure",
        "repeated_rust_learning", "repeated_systems_programming",
    }
    assert all(o.aspect in ALLOWED for o in obs)


def test_export_drops_personal_and_social_domains(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps([
        _res("https://mail.google.com/mail"),
        _res("https://facebook.com/feed"),
        _res("https://docs.rs/tokio"),
        _res("https://www.kernel.org/doc"),
    ]), encoding="utf-8")
    from friday.observation.research_observer import ExportProvider
    out = ExportProvider(export).fetch()
    hosts = {ResearchResource.from_dict(d).host for d in out}
    assert "mail.google.com" not in hosts
    assert "facebook.com" not in hosts
    assert "docs.rs" in hosts
    assert "www.kernel.org" in hosts


def test_export_accepts_explicit_engineering_category(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps([
        _res("https://blog.example.com/post", category=Category.BLOG),
    ]), encoding="utf-8")
    from friday.observation.research_observer import ExportProvider
    out = ExportProvider(export).fetch()
    assert len(out) == 1  # explicit engineering category accepted


def test_export_rejects_unknown_host_without_category(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps([
        _res("https://random-shopping-site.example/items"),
    ]), encoding="utf-8")
    from friday.observation.research_observer import ExportProvider
    assert ExportProvider(export).fetch() == []


# --- Topic signals (derived / inferred) ------------------------------------


def test_researching_operating_systems_inferred():
    res = [_res(f"https://www.kernel.org/doc/{i}") for i in range(3)]
    obs = {(o.subject, o.aspect): o for o in _observer(res).collect(None)}
    assert obs[("research", "researching_operating_systems")].value == "true"
    assert obs[("research", "researching_operating_systems")].confidence is Confidence.INFERRED


def test_repeated_rust_learning_inferred():
    res = [_res(f"https://docs.rs/crate{i}", language="rust") for i in range(3)]
    obs = {(o.subject, o.aspect): o for o in _observer(res).collect(None)}
    assert obs[("research", "repeated_rust_learning")].value == "true"


def test_topic_below_threshold_not_inferred():
    res = [_res("https://www.kernel.org/doc/1"),
           _res("https://www.kernel.org/doc/2")]
    obs = [o for o in _observer(res).collect(None)
           if o.aspect.startswith("researching_")]
    assert obs == []


def test_heavy_database_research_inferred():
    res = [_res(f"https://supabase.com/docs/{i}") for i in range(3)]
    obs = {(o.subject, o.aspect): o for o in _observer(res).collect(None)}
    assert obs[("research", "researching_databases")].value == "true"


def test_topic_of_helper():
    assert topic_of("www.kernel.org", Category.DOCUMENTATION, None) == "operating_systems"
    assert topic_of("docs.rs", Category.DOCUMENTATION, "rust") == "rust_learning"
    assert topic_of("example.com", Category.UNKNOWN, None) is None


# --- Resource model --------------------------------------------------------


def test_resource_from_dict_normalizes():
    r = ResearchResource.from_dict({"url": "https://docs.rs/x", "category": Category.RFC})
    assert r.host == "docs.rs"
    # Explicit known category is preserved (host hint would only fill UNKNOWN).
    assert r.category == Category.RFC


def test_resource_handles_missing_url():
    r = ResearchResource.from_dict({})
    assert r.host == ""
    assert r.category == Category.UNKNOWN


# --- Summary ---------------------------------------------------------------


def test_summary_counts_and_top_domains(tmp_path):
    conn = connect(tmp_path / "kb.db")
    res = [
        _res("https://docs.rs/tokio"), _res("https://docs.rs/serde"),
        _res("https://datatracker.ietf.org/doc/rfc1"),
        _res("https://platform.openai.com/x"),
        _res("https://platform.openai.com/y"),
        _res("https://arxiv.org/abs/1"), _res("https://www.kernel.org/doc"),
    ]
    summary = _observer(res).summarize(conn)
    conn.close()
    assert "Engineering resources\n7" in summary
    assert "Documentation\n3" in summary
    assert "RFCs\n1" in summary
    assert "API references\n2" in summary
    assert "docs.rs" in summary
    assert "openai.com" in summary
    # Top 4 domains by frequency; kernel.org is 5th here so not guaranteed shown.
    assert "datatracker.ietf.org" in summary


def test_summary_healthy_header():
    assert ResearchObserver(FixtureProvider([])).summarize(None).startswith(
        "Research Observer\nHealthy")
