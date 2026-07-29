"""Tests for the Self-Evolution Engine — sandbox, capability, planner, deploy."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# =============================================================================
# Helpers
# =============================================================================


def _cap_conn():
    """Create a fresh in-memory SQLite connection with all migrations applied."""
    from friday.db import connect
    return connect(":memory:")


# =============================================================================
# Capability Registry
# =============================================================================


class TestCapabilityRegistry:
    def test_add_and_get(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry, CapabilityFlag

        registry = CapabilityRegistry(conn)
        flag = registry.add("voice_support", "Add voice capabilities", "{}")
        assert flag is not None
        assert flag.name == "voice_support"
        assert flag.enabled is False
        assert flag.installed is True

        fetched = registry.get("voice_support")
        assert fetched is not None
        assert fetched.name == "voice_support"

    def test_get_nonexistent(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        assert registry.get("nonexistent") is None

    def test_enable_disable(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("test_cap", "Test")
        assert registry.enable("test_cap") is True
        flag = registry.get("test_cap")
        assert flag.enabled is True
        assert registry.disable("test_cap") is True
        flag = registry.get("test_cap")
        assert flag.enabled is False

    def test_list_empty(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        assert registry.list_all() == []

    def test_list_multiple(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("voice", "Voice support")
        registry.add("discord", "Discord bot")
        flags = registry.list_all()
        assert len(flags) == 2

    def test_status_label(self):
        from friday.meta.capability import CapabilityFlag

        pending = CapabilityFlag(name="v", installed=False)
        assert pending.status_label == "pending"

        disabled = CapabilityFlag(name="v", installed=True, enabled=False)
        assert disabled.status_label == "disabled"

        enabled = CapabilityFlag(name="v", installed=True, enabled=True)
        assert enabled.status_label == "enabled"

    def test_rollback_commit(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("test", "Test", rollback_commit="abc123")
        assert registry.set_rollback_commit("test", "def456") is True
        flag = registry.get("test")
        assert flag.rollback_commit == "def456"

    def test_mark_deps_installed(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("test", "Test")
        assert registry.mark_deps_installed("test") is True
        flag = registry.get("test")
        assert flag.deps_installed is True

    def test_remove(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("test", "Test")
        assert registry.remove("test") is True
        assert registry.get("test") is None

    def test_touch(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("test", "Test")
        assert registry.touch("test") is True
        flag = registry.get("test")
        assert flag.last_used_at is not None

    def test_get_last_deployed(self):
        conn = _cap_conn()
        from friday.meta.capability import CapabilityRegistry

        registry = CapabilityRegistry(conn)
        registry.add("cap1", "First")
        registry.add("cap2", "Second")
        last = registry.get_last_deployed()
        assert last is not None
        assert last.name == "cap2"


# =============================================================================
# Sandbox upgrades
# =============================================================================


class TestSandboxMethods:
    def test_read_file_nonexistent(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        # No sandbox created — should return empty string.
        content = sandbox.read_file("nonexistent.py")
        assert content == ""

    def test_file_exists_nonexistent(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        assert sandbox.file_exists("nonexistent.py") is False

    def test_dry_run_empty(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        result = sandbox.dry_run([])
        assert result["created"] == []
        assert result["deps"] == []

    def test_dry_run_with_changes(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        changes = [
            {"type": "new_file", "path": "src/friday/services/voice.py", "content_summary": "320 lines"},
            {"type": "modified_file", "path": "src/friday/cli.py", "content_summary": "+15 lines"},
            {"type": "dependency", "name": "edge-tts"},
            {"type": "config_change", "name": "FRIDAY_VOICE_ENABLED"},
        ]
        result = sandbox.dry_run(changes)
        assert len(result["created"]) == 1
        assert len(result["modified"]) == 1
        assert len(result["deps"]) == 1
        assert len(result["config_changes"]) == 1
        assert "voice.py" in result["created"][0]
        assert "edge-tts" in result["deps"]
        assert "FRIDAY_VOICE_ENABLED" in result["config_changes"][0]

    def test_snapshot_no_sandbox(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        assert sandbox.snapshot() is None

    def test_rollback_no_sandbox(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        assert sandbox.rollback("abc123") is False

    def test_snapshot_rollback_property(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        assert sandbox.base_commit is None
        assert sandbox.sandbox_path is None
        assert sandbox.diff_path is None

    def test_write_file_no_sandbox(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        assert sandbox.write_file("test.py", "content") is False

    def test_install_deps_no_sandbox(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        result = sandbox.install_deps(["some-package"])
        assert result["success"] is False
        assert "Sandbox not created" in result["output"]

    def test_install_deps_empty(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        result = sandbox.install_deps([])
        assert result["success"] is True
        assert "No deps" in result["output"]

    def test_test_file_no_sandbox(self):
        from friday.meta.sandbox import Sandbox
        sandbox = Sandbox()
        with pytest.raises(RuntimeError, match="Sandbox not created"):
            sandbox.test_file("tests/test_foo.py")


# =============================================================================
# Capability Planner
# =============================================================================


class TestCapabilityPlan:
    def test_validate_capability_plan_minimal(self):
        from friday.meta.si_planner import validate_capability_plan

        plan = {
            "capability_name": "test_cap",
            "new_files": [{"path": "test.py", "content": "print('hello')"}],
            "modified_files": [],
            "dependencies": [],
            "test_files": [],
            "rollback_risk": "low",
            "verification_steps": [],
        }
        errors = validate_capability_plan(plan)
        assert errors == []

    def test_validate_missing_name(self):
        from friday.meta.si_planner import validate_capability_plan

        plan = {
            "new_files": [],
            "modified_files": [],
            "rollback_risk": "medium",
        }
        errors = validate_capability_plan(plan)
        assert any("capability_name" in e for e in errors)

    def test_validate_missing_new_files_content(self):
        from friday.meta.si_planner import validate_capability_plan

        plan = {
            "capability_name": "test",
            "new_files": [{"path": "test.py"}],  # missing content
            "modified_files": [],
            "rollback_risk": "low",
        }
        errors = validate_capability_plan(plan)
        assert any("missing 'content'" in e for e in errors)

    def test_validate_defaults_rollback_risk(self):
        from friday.meta.si_planner import validate_capability_plan

        plan = {
            "capability_name": "test",
            "new_files": [],
            "modified_files": [],
            "rollback_risk": "invalid",
        }
        errors = validate_capability_plan(plan)
        assert plan["rollback_risk"] == "medium"

    def test_estimate_plan_changes(self):
        from friday.meta.si_planner import estimate_plan_changes

        plan = {
            "capability_name": "voice_support",
            "new_files": [{"path": "src/friday/services/voice.py"}],
            "modified_files": [{"path": "src/friday/cli.py"}],
            "dependencies": ["edge-tts"],
            "test_files": [{"path": "tests/test_voice.py"}],
            "rollback_risk": "medium",
        }
        summary = estimate_plan_changes(plan)
        assert "voice.py" in summary
        assert "cli.py" in summary
        assert "edge-tts" in summary
        assert "Risk: medium" in summary

    def test_apply_capability_plan_to_sandbox_no_sandbox(self):
        from friday.meta.si_planner import apply_capability_plan_to_sandbox
        from friday.meta.sandbox import Sandbox

        sandbox = Sandbox()  # no sandbox created
        plan = {
            "new_files": [{"path": "test.py", "content": "x = 1"}],
            "modified_files": [],
        }
        # Should not raise.
        apply_capability_plan_to_sandbox(sandbox, plan)


# =============================================================================
# Deploy module imports (smoke tests)
# =============================================================================


class TestDeployImports:
    def test_deploy_functions_exist(self):
        from friday.meta.deploy import (
            deploy_capability,
            rollback_capability,
            deploy,
            approve,
            promote,
            reject,
            stage,
        )
        assert callable(deploy_capability)
        assert callable(rollback_capability)

    def test_rollback_no_capability(self):
        conn = _cap_conn()
        from friday.meta.deploy import rollback_capability
        # Non-existent capability — should return False, not crash.
        ok = rollback_capability(conn, "nonexistent")
        assert ok is False


# =============================================================================
# CLI module imports (smoke tests)
# =============================================================================


class TestCLIImports:
    def test_cli_meta_imports(self):
        from friday.cli_meta import cmd_upgrade, add_subparser
        assert callable(cmd_upgrade)
        assert callable(add_subparser)
