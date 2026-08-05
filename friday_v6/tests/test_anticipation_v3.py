"""Tests for AnticipationEngine's V3 data-source integration."""

from __future__ import annotations

from friday_v6.proactive.anticipation import AnticipationEngine


class _FakeSource:
    """V3DataSource stand-in returning a fixed digest."""

    def __init__(self, digest: str = "5 observations (git_observer: 5)."):
        self._digest = digest

    def workspace_digest(self, hours: float = 24.0) -> str:
        return self._digest


class TestAnticipationV3:
    def test_v3_digest_in_summary(self):
        engine = AnticipationEngine(data_source=_FakeSource())
        summary = engine.get_context_summary()
        assert "5 observations" in summary

    def test_v3_digest_empty_without_source(self):
        engine = AnticipationEngine(data_source=False)
        summary = engine.get_context_summary()
        assert "observations" not in summary

    def test_v3_digest_cached(self):
        source = _FakeSource()
        engine = AnticipationEngine(data_source=source)
        first = engine.v3_digest()
        second = engine.v3_digest()
        assert first == second == "5 observations (git_observer: 5)."

    def test_cleanup_with_source(self):
        engine = AnticipationEngine(data_source=_FakeSource())
        engine.cleanup()  # must not raise
