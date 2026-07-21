"""Milestone 7.3 — Terminal Observer tests.

Deterministic tests for the TerminalObserver: it is a PURE READER of a JSONL
engineering-command activity log and emits engineering observations that plug
into the frozen Observation Engine. No daemon, no PTY, no keylogger, no shell
hooks, no LLM.

Coverage: build/test/git commands, repository switch, failure + success,
duration, unknown tool, privacy (no args/secrets), observer registration, health,
and a real end-to-end run through the Observation Engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday.db import ObservationRow, connect, insert_observations, latest_observations
from friday.observation import (
    Confidence,
    Observation,
    ObservationEngine,
    TerminalObserver,
    categorize,
    default_registry,
)
from friday.observation.terminal_observer import CATEGORIES


def _write_log(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                     encoding="utf-8")


@pytest.fixture
def log_path(tmp_path) -> Path:
    return tmp_path / "activity.jsonl"


# --- Categorization (deterministic, no LLM) --------------------------------


def test_categorize_build_tool():
    assert categorize("cargo") == "Build"
    assert categorize("make") == "Build"
    assert categorize("cmake") == "Build"


def test_categorize_test_tool():
    assert categorize("pytest") == "Test"
    assert categorize("jest") == "Test"


def test_categorize_vcs_tool():
    assert categorize("git") == "Version Control"


def test_categorize_container_and_types():
    assert categorize("docker") == "Container"
    assert categorize("mypy") == "Type Checking"
    assert categorize("npm") == "Dependency Management"


def test_categorize_unknown_tool():
    assert categorize("frobnicate-9000") == "Unknown"
    assert categorize("") == "Unknown"


def test_categorize_is_case_insensitive():
    assert categorize("Pytest") == "Test"
    assert categorize("GIT") == "Version Control"


# --- Build command observations ----------------------------------------------


def test_build_command_emits_facts(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
        "wd": "/code/Aether", "exit": 0, "duration_s": 11.2,
    }])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert ("Aether", "tool") in obs and obs[("Aether", "tool")].value == "cargo"
    assert obs[("Aether", "tool_category")].value == "Build"
    assert obs[("Aether", "exit_status")].value == "0"
    assert obs[("Aether", "success")].value == "true"
    assert obs[("Aether", "duration_s")].value == "11.2"
    assert obs[("Aether", "tool")].confidence is Confidence.OBSERVED
    assert obs[("Aether", "tool_category")].confidence is Confidence.DERIVED


# --- Test command (failure) -------------------------------------------------


def test_test_command_failure(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:10:00+00:00", "tool": "pytest", "repo": "Friday",
        "wd": "/code/Friday", "exit": 1, "duration_s": 4.7,
    }])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert obs[("Friday", "success")].value == "false"
    assert obs[("Friday", "exit_status")].value == "1"
    assert obs[("Friday", "tool_category")].value == "Test"


# --- Git command ------------------------------------------------------------


def test_git_command_with_branch(log_path):
    # No `exit` field recorded => exit_status/success must be omitted.
    _write_log(log_path, [{
        "ts": "2026-07-14T09:20:00+00:00", "tool": "git", "repo": "Vivaha",
        "wd": "/code/Vivaha",
    }])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert obs[("Vivaha", "tool_category")].value == "Version Control"
    assert ("Vivaha", "exit_status") not in obs  # no exit recorded => omitted


# --- Repository switch ------------------------------------------------------


def test_repository_switch_emitted(log_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
         "wd": "/a", "exit": 0},
        {"ts": "2026-07-14T09:30:00+00:00", "tool": "git", "repo": "Vivaha",
         "wd": "/v", "exit": 0},
    ])
    obs = [o for o in TerminalObserver(log_path).collect(None)
           if o.aspect == "repo_switch"]
    assert len(obs) == 1
    assert obs[0].value == "Aether -> Vivaha"
    assert obs[0].confidence is Confidence.DERIVED


def test_tool_switch_emitted(log_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
         "wd": "/a", "exit": 0},
        {"ts": "2026-07-14T09:05:00+00:00", "tool": "pytest", "repo": "Aether",
         "wd": "/a", "exit": 0},
    ])
    obs = [o for o in TerminalObserver(log_path).collect(None)
           if o.aspect == "tool_switch"]
    assert len(obs) == 1
    assert obs[0].value == "cargo -> pytest"


# --- Repeated failures (inferred) -------------------------------------------


def test_repeated_test_failures_inferred(log_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "pytest", "repo": "Friday",
         "wd": "/f", "exit": 1},
        {"ts": "2026-07-14T09:05:00+00:00", "tool": "pytest", "repo": "Friday",
         "wd": "/f", "exit": 1},
    ])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert obs[("Friday", "repeated_test_failures")].value == "true"
    assert obs[("Friday", "repeated_test_failures")].confidence is Confidence.INFERRED
    assert "2" in (obs[("Friday", "repeated_test_failures")].cause or "")


def test_single_failure_not_inferred(log_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "pytest", "repo": "Friday",
         "wd": "/f", "exit": 1},
    ])
    obs = [o for o in TerminalObserver(log_path).collect(None)
           if o.aspect == "repeated_test_failures"]
    assert obs == []


def test_repeated_build_failures_inferred(log_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
         "wd": "/a", "exit": 1},
        {"ts": "2026-07-14T09:05:00+00:00", "tool": "cargo", "repo": "Aether",
         "wd": "/a", "exit": 1},
    ])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert obs[("Aether", "repeated_build_failures")].value == "true"


# --- Long-running build (inferred) ------------------------------------------


def test_long_running_build_inferred(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
        "wd": "/a", "exit": 0, "duration_s": 142.0,
    }])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert obs[("terminal", "long_running_build")].value == "142.0s"
    assert obs[("terminal", "long_running_build")].confidence is Confidence.INFERRED


def test_short_build_not_long(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
        "wd": "/a", "exit": 0, "duration_s": 5.0,
    }])
    obs = [o for o in TerminalObserver(log_path).collect(None)
           if o.aspect == "long_running_build"]
    assert obs == []


# --- Unknown tool -----------------------------------------------------------


def test_unknown_tool_categorized_unknown(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "mystrangecli",
        "repo": "X", "wd": "/x", "exit": 0,
    }])
    obs = {(o.subject, o.aspect): o for o in TerminalObserver(log_path).collect(None)}
    assert obs[("X", "tool_category")].value == "Unknown"
    assert obs[("X", "tool")].value == "mystrangecli"


# --- Privacy guarantees -----------------------------------------------------


def test_no_command_arguments_stored(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "pytest",
        "repo": "Friday", "wd": "/f", "exit": 0,
        # Secrets + raw command must be ignored, never emitted.
        "command": "pytest tests/test_llm.py --api-key=sk-secret123",
        "args": ["--api-key=sk-secret123"],
        "env": {"AWS_SECRET_ACCESS_KEY": "shh", "PATH": "/usr/bin"},
        "stdout": "lots of output", "stderr": "err",
    }])
    obs = TerminalObserver(log_path).collect(None)
    blob = json.dumps([o.__dict__ for o in obs])
    assert "sk-secret123" not in blob
    assert "AWS_SECRET_ACCESS_KEY" not in blob
    assert "api-key" not in blob
    assert "tests/test_llm.py" not in blob
    assert "lots of output" not in blob
    # Only whitelisted metadata fields are present.
    assert all(o.aspect in {
        "tool", "tool_category", "exit_status", "success", "duration_s",
        "repo_switch", "tool_switch", "repeated_test_failures",
        "repeated_build_failures", "long_running_build",
    } for o in obs)


def test_no_env_or_password_fields_emitted(log_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "npm", "repo": "MindWell",
        "wd": "/m", "exit": 0,
        "password": "hunter2", "token": "ghp_xxx",
        "secret": "topsecret",
    }])
    obs = TerminalObserver(log_path).collect(None)
    blob = json.dumps([o.__dict__ for o in obs])
    assert "hunter2" not in blob
    assert "ghp_xxx" not in blob
    assert "topsecret" not in blob


def test_log_without_log_returns_empty(log_path):
    # No log file => healthy, no observations.
    assert TerminalObserver(log_path).collect(None) == []
    assert TerminalObserver(log_path).health(None).healthy is True


# --- Health -----------------------------------------------------------------


def test_health_healthy_when_log_present(log_path):
    _write_log(log_path, [{"ts": "2026-07-14T09:00:00+00:00", "tool": "git",
                           "repo": "V", "exit": 0}])
    h = TerminalObserver(log_path).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


def test_health_down_when_path_is_directory(log_path):
    log_path.mkdir()
    h = TerminalObserver(log_path).health(None)
    assert h.healthy is False
    assert h.status.value == "down"


# --- Observer registration --------------------------------------------------


def test_terminal_registered_in_default_registry():
    assert "terminal" in default_registry()
    assert "git" in default_registry()


def test_register_duplicate_raises():
    from friday.observation import ObserverRegistry
    reg = ObserverRegistry()
    reg.register(TerminalObserver())
    import pytest as _pytest
    with _pytest.raises(ValueError):
        reg.register(TerminalObserver())


# --- Summary ----------------------------------------------------------------


def test_summary_counts_and_success_rate(log_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "pytest", "repo": "Friday",
         "wd": "/f", "exit": 1},
        {"ts": "2026-07-14T09:05:00+00:00", "tool": "pytest", "repo": "Friday",
         "wd": "/f", "exit": 0},
        {"ts": "2026-07-14T09:10:00+00:00", "tool": "git", "repo": "Vivaha",
         "wd": "/v", "exit": 0},
    ])
    summary = TerminalObserver(log_path).summarize(None)
    assert "3 engineering commands" in summary
    assert "Friday" in summary and "Vivaha" in summary
    assert "Failures: 1" in summary
    assert "Success rate: 67%" in summary
    assert "pytest" in summary


def test_summary_empty(log_path):
    assert "0 engineering commands" in TerminalObserver(log_path).summarize(None)


# --- Real end-to-end through the frozen Observation Engine ------------------


def test_end_to_end_through_observation_engine(log_path, tmp_path):
    _write_log(log_path, [
        {"ts": "2026-07-14T09:00:00+00:00", "tool": "cargo", "repo": "Aether",
         "wd": "/a", "exit": 0, "duration_s": 11.2},
        {"ts": "2026-07-14T09:10:00+00:00", "tool": "pytest", "repo": "Friday",
         "wd": "/f", "exit": 1, "duration_s": 4.7},
        {"ts": "2026-07-14T09:20:00+00:00", "tool": "git", "repo": "Vivaha",
         "wd": "/v", "exit": 0},
    ])
    conn = connect(tmp_path / "kb.db")
    # Point the observer at our fixture log via the registry default path.
    import os
    os.environ["FRIDAY_TERMINAL_LOG"] = str(log_path)
    try:
        # Build a registry containing ONLY the terminal observer to isolate it.
        from friday.observation import ObserverRegistry
        reg = ObserverRegistry()
        reg.register(TerminalObserver(log_path))
        run = ObservationEngine(reg, conn).run()
    finally:
        os.environ.pop("FRIDAY_TERMINAL_LOG", None)
    conn.close()
    # One observer ran; terminal facts persisted.
    assert run.observers[0].name == "terminal"
    assert run.observers[0].health.healthy
    # observations_all returns every persisted fact (events have distinct
    # timestamps, so latest_observations would only show the newest batch).
    from friday.db import observations_all
    stored = observations_all(connect(tmp_path / "kb.db"))
    aspects = {(o.subject, o.aspect) for o in stored}
    assert ("Aether", "tool") in aspects
    assert ("Friday", "success") in aspects
    assert ("Vivaha", "tool_category") in aspects
    # Source is "terminal", proving it plugged into the engine unchanged.
    assert all(o.source == "terminal" for o in stored)


def test_observation_ids_are_deterministic_and_idempotent(log_path, tmp_path):
    _write_log(log_path, [{
        "ts": "2026-07-14T09:00:00+00:00", "tool": "pytest", "repo": "Friday",
        "wd": "/f", "exit": 0,
    }])
    from friday.observation import ObserverRegistry
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(TerminalObserver(log_path))
    ObservationEngine(reg, conn).run()
    ids1 = {o.id for o in latest_observations(conn)}
    # Re-run over identical log -> identical ids (no duplicate facts).
    ObservationEngine(reg, conn).run()
    ids2 = {o.id for o in latest_observations(conn)}
    assert ids1 == ids2


def test_malformed_log_lines_skipped(log_path):
    log_path.write_text(
        "not json at all\n"
        '{"ts":"2026-07-14T09:00:00+00:00"}\n'  # no tool -> skipped
        '[broken json\n'
        '{"ts":"2026-07-14T09:05:00+00:00","tool":"git","repo":"V","exit":0}\n',
        encoding="utf-8",
    )
    obs = TerminalObserver(log_path).collect(None)
    # Only the well-formed event with a tool is observed.
    assert len([o for o in obs if o.aspect == "tool"]) == 1
