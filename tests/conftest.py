"""Shared pytest fixtures and markers for the Friday test suite.

Live-pipeline guard: tests that drive the *live* AI runtime pipeline (real
Planner -> Resolver -> Executor -> model/CLI) are slow and can hang in a
non-interactive harness, so they are opt-in. A plain ``pytest`` run skips them
fast. Run them deliberately with ``-m live_pipeline`` or by setting
``FRIDAY_RUN_LIVE_TESTS=1`` (e.g. in CI with a backend configured).
"""

from __future__ import annotations

import os

import pytest


def live_tests_enabled() -> bool:
    """True when the user has explicitly opted into live-pipeline tests."""
    return os.environ.get("FRIDAY_RUN_LIVE_TESTS") == "1"


# Apply to any test that requires a live AI backend. Skipped unless opted in,
# so a default ``pytest`` run finishes fast instead of stalling on real models.
skip_unless_live = pytest.mark.skipif(
    not live_tests_enabled(),
    reason="live-pipeline test (opt in with -m live_pipeline or "
           "FRIDAY_RUN_LIVE_TESTS=1)",
)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_pipeline: test drives the live AI runtime pipeline (opt-in)",
    )

