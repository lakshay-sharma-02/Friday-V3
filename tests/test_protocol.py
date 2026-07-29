"""Tests for the Named Protocols engine — CRUD and execution."""

from __future__ import annotations

import pytest

from friday.protocol import ProtocolEngine, ProtocolStep, _extract_variables, _resolve_template, format_protocol


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def eng():
    from friday.db import connect
    conn = connect(":memory:")
    engine = ProtocolEngine(conn)
    yield engine
    conn.close()


# ---------------------------------------------------------------------------
# Variable extraction and resolution
# ---------------------------------------------------------------------------


class TestVariables:
    def test_extract_simple(self):
        assert _extract_variables("hello {name}") == ["name"]

    def test_extract_multiple(self):
        result = _extract_variables("{project}:{branch}")
        assert result == ["project", "branch"]

    def test_extract_no_variables(self):
        assert _extract_variables("hello world") == []

    def test_extract_duplicates(self):
        assert _extract_variables("{x} and {x}") == ["x"]

    def test_resolve_basic(self):
        result = _resolve_template("hello {name}", {"name": "world"})
        assert result == "hello world"

    def test_resolve_unresolved_left_as_is(self):
        result = _resolve_template("hello {name}", {})
        assert result == "hello {name}"

    def test_resolve_partial(self):
        result = _resolve_template("{a} and {b}", {"a": "1"})
        assert result == "1 and {b}"


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_create_and_get(self, eng):
        proto = eng.create("deploy", "Deploy the app", steps=[
            ProtocolStep(name="test", payload_template='{"cmd":"pytest"}'),
            ProtocolStep(name="build", payload_template='{"cmd":"docker build"}'),
        ])
        assert proto.id > 0
        assert proto.name == "deploy"
        assert len(proto.steps) == 2
        assert proto.steps[0].name == "test"
        assert proto.steps[0].payload_template == '{"cmd":"pytest"}'

    def test_get_returns_none_for_missing(self, eng):
        proto = eng.get("nonexistent")
        assert proto is None

    def test_get_returns_protocol(self, eng):
        eng.create("test-proto", "Test")
        proto = eng.get("test-proto")
        assert proto is not None
        assert proto.name == "test-proto"
        assert proto.description == "Test"

    def test_list_all_empty(self, eng):
        protos = eng.list_all()
        assert protos == []

    def test_list_all(self, eng):
        eng.create("a", "First")
        eng.create("b", "Second")
        protos = eng.list_all()
        assert len(protos) == 2
        assert protos[0].name == "a"
        assert protos[1].name == "b"

    def test_create_duplicate_raises(self, eng):
        eng.create("dup", "First")
        with pytest.raises(ValueError, match="already exists"):
            eng.create("dup", "Second")

    def test_delete(self, eng):
        eng.create("temp", "Temporary")
        assert eng.get("temp") is not None
        deleted = eng.delete("temp")
        assert deleted is True
        assert eng.get("temp") is None

    def test_delete_nonexistent(self, eng):
        deleted = eng.delete("nonexistent")
        assert deleted is False

    def test_variables_extracted_automatically(self, eng):
        proto = eng.create("vars", steps=[
            ProtocolStep(name="step1", payload_template='{"dir":"{project}","cmd":"{action}"}'),
        ])
        assert "project" in proto.variables
        assert "action" in proto.variables
        assert len(proto.variables) == 2

    def test_no_variables(self, eng):
        proto = eng.create("static", steps=[
            ProtocolStep(name="step1", payload_template='{"cmd":"ls"}'),
        ])
        assert proto.variables == []


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_missing_protocol_raises(self, eng):
        with pytest.raises(ValueError, match="not found"):
            eng.run("nonexistent")

    def test_run_with_variables(self, eng):
        """Test that variables are resolved in payload templates."""
        eng.create("hello-world", steps=[
            ProtocolStep(name="echo", payload_template='{"cmd":"echo {name}"}'),
        ])
        result = eng.run("hello-world", variables={"name": "friday"})
        # The executor may not be available in a test env, but the protocol
        # engine should resolve variables and attempt dispatch.
        assert result["protocol"] == "hello-world"
        assert len(result["steps"]) == 1

    def test_run_missing_variables(self, eng):
        """Missing variables should produce an error step, not crash."""
        eng.create("needs-vars", steps=[
            ProtocolStep(name="step1", payload_template='{"x":"{missing}"}'),
        ])
        result = eng.run("needs-vars", variables={})
        assert result["success"] is False
        assert "unresolved" in result["error"].lower()

    def test_run_on_failure_skip(self, eng):
        """skip mode should continue past failures.

        Verifies that when a step fails (missing variables), the engine
        continues to the next step instead of aborting.
        """
        eng.create("multi-step", steps=[
            ProtocolStep(name="step1", payload_template='{"x":"{bad}"}'),
            ProtocolStep(name="step2", payload_template='{}'),
        ])
        result = eng.run("multi-step", variables={}, on_failure="skip")

        # Both steps should appear in results (skip didn't abort).
        assert len(result["steps"]) == 2
        # Step 1: missing variable -> explicitly failed.
        assert result["steps"][0]["success"] is False
        assert "unresolved" in result["steps"][0].get("error", "").lower()
        # Step 2: empty payload, executor may or may not be available.
        # The key assertion is that it WAS ATTEMPTED (not aborted).
        assert result["steps"][1]["step"] == "step2"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_single(self, eng):
        proto = eng.create("test", "A test protocol", steps=[
            ProtocolStep(name="step1", payload_template='{"cmd":"echo"}'),
        ])
        text = format_protocol(proto)
        assert "test" in text
        assert "A test protocol" in text
        assert "1" in text  # step count

    def test_format_verbose(self, eng):
        proto = eng.create("verbose-test", "Verbose", steps=[
            ProtocolStep(name="step1", worker_id="worker:shell",
                         payload_template='{"cmd":"echo"}'),
        ])
        text = format_protocol(proto, verbose=True)
        assert "worker:shell" in text
        assert "step1" in text
