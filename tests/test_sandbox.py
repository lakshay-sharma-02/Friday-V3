"""Tests for the What-If Sandbox — dry-run actions in an isolated temp directory."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from friday.sandbox import SandboxEngine, SandboxResult


# ---------------------------------------------------------------------------
# SandboxResult model
# ---------------------------------------------------------------------------


class TestSandboxResult:
    def test_empty_result_format(self):
        result = SandboxResult(action="echo hi", action_type="shell")
        text = result.format()
        assert "What-If Sandbox Simulation" in text
        assert "echo hi" in text
        assert "nothing was actually changed" in text

    def test_to_dict(self):
        result = SandboxResult(action="test", action_type="shell",
                               stdout="hello", success=True)
        d = result.to_dict()
        assert d["action"] == "test"
        assert d["stdout"] == "hello"
        assert d["success"] is True

    def test_cleanup_removes_sandbox(self, tmp_path):
        sandbox_dir = str(tmp_path / "sandbox")
        os.makedirs(sandbox_dir)
        result = SandboxResult(action="test", action_type="shell",
                               sandbox_path=sandbox_dir)
        result.cleanup()
        assert not os.path.exists(sandbox_dir)

    def test_cleanup_idempotent(self):
        result = SandboxResult(action="test", action_type="shell",
                               sandbox_path="")
        # Should not raise.
        result.cleanup()

    def test_result_with_files(self):
        result = SandboxResult(
            action="touch x", action_type="shell",
            files_created=["test.txt", "src/main.py"],
            files_modified=["README.md"],
            stdout="done",
        )
        text = result.format()
        assert "Created: 2" in text
        assert "test.txt" in text
        assert "src/main.py" in text
        assert "README.md" in text
        assert "done" in text


# ---------------------------------------------------------------------------
# SandboxEngine — shell simulation
# ---------------------------------------------------------------------------


class TestShellSimulation:
    def test_simple_command(self):
        """Simulate a simple echo command."""
        engine = SandboxEngine()
        result = engine.simulate("echo hello world")
        assert result.success is True
        assert "hello world" in result.stdout

    def test_command_failure(self):
        """Simulate a command that exits non-zero."""
        engine = SandboxEngine(keep_sandbox=True)
        result = engine.simulate("exit 42")
        assert result.success is False
        assert result.exit_code == 42

    def test_command_timeout(self):
        """Simulate a command that times out."""
        engine = SandboxEngine()
        result = engine.simulate("sleep 10", timeout=1)
        assert result.success is False
        assert len(result.errors) > 0

    def test_with_repo_path(self, tmp_path):
        """Simulate a command inside a repo copy."""
        # Create a mini repo.
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Hello\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo,
                       capture_output=True)

        engine = SandboxEngine()
        result = engine.simulate("echo hi", repo_path=str(repo))
        assert result.repo == "my-repo"
        # The sandbox should have been cleaned up.
        assert result._cleaned or not result.sandbox_path


# ---------------------------------------------------------------------------
# SandboxEngine — file simulation
# ---------------------------------------------------------------------------


class TestFileSimulation:
    def test_write_file(self, tmp_path):
        """Simulate writing a file in a repo copy."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "original.txt").write_text("original content\n")

        engine = SandboxEngine(keep_sandbox=False)
        result = engine.simulate_file(
            {"op": "write", "path": "new.txt", "content": "new file content"},
            repo_path=str(repo),
        )
        assert result.success is True
        # The sandbox should be cleaned up.
        assert result._cleaned or not result.sandbox_path
        # The real repo should NOT have the new file.
        assert not (repo / "new.txt").exists()

    def test_delete_file(self, tmp_path):
        """Simulate deleting a file."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "delete_me.txt").write_text("bye\n")

        engine = SandboxEngine()
        result = engine.simulate_file(
            {"op": "delete", "path": "delete_me.txt"},
            repo_path=str(repo),
        )
        assert result.success is True
        # Real file should still exist.
        assert (repo / "delete_me.txt").exists()

    def test_replace_in_file(self, tmp_path):
        """Simulate a text replacement."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "config.txt").write_text("VERSION=old\nNAME=test\n")

        engine = SandboxEngine()
        result = engine.simulate_file(
            {"op": "replace", "path": "config.txt",
             "old": "old", "new": "new"},
            repo_path=str(repo),
        )
        assert result.success is True
        # Real file should be unchanged.
        assert (repo / "config.txt").read_text() == "VERSION=old\nNAME=test\n"

    def test_mkdir(self, tmp_path):
        """Simulate creating a directory."""
        repo = tmp_path / "my-repo"
        repo.mkdir()

        engine = SandboxEngine()
        result = engine.simulate_file(
            {"op": "mkdir", "path": "new-dir/subdir"},
            repo_path=str(repo),
        )
        assert result.success is True
        # Real directory should not exist.
        assert not (repo / "new-dir").exists()

    def test_unknown_op(self):
        """Unknown file operation returns error."""
        engine = SandboxEngine()
        result = engine.simulate_file({"op": "unknown"})
        assert result.success is False
        assert len(result.errors) > 0

    def test_file_not_found(self, tmp_path):
        """Reading a non-existent file returns error."""
        repo = tmp_path / "my-repo"
        repo.mkdir()

        engine = SandboxEngine()
        result = engine.simulate_file(
            {"op": "read", "path": "nonexistent.txt"},
            repo_path=str(repo),
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# SandboxEngine — file change detection
# ---------------------------------------------------------------------------


class TestFileChanges:
    def test_detect_created_file(self, tmp_path):
        """Sandbox detects files created by a command."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Hello\n")

        engine = SandboxEngine(keep_sandbox=True)
        result = engine.simulate(
            "touch new_file.txt && mkdir sub && touch sub/data.txt",
            repo_path=str(repo),
        )
        assert len(result.files_created) >= 2
        assert "new_file.txt" in result.files_created

    def test_detect_deleted_file(self, tmp_path):
        """Sandbox detects files deleted by a command."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Hello\n")
        (repo / "old.txt").write_text("bye\n")

        engine = SandboxEngine(keep_sandbox=True)
        result = engine.simulate("rm old.txt", repo_path=str(repo))
        assert "old.txt" in result.files_deleted

    def test_detect_modified_file(self, tmp_path):
        """Sandbox detects files modified by a command."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "data.txt").write_text("original\n")

        engine = SandboxEngine(keep_sandbox=True)
        result = engine.simulate(
            "echo 'modified content' > data.txt",
            repo_path=str(repo),
        )
        assert any("data.txt" in f for f in result.files_modified), (
            f"Expected data.txt in files_modified: {result.files_modified}"
        )


# ---------------------------------------------------------------------------
# Requested action — real-world patterns
# ---------------------------------------------------------------------------


class TestRealWorldPatterns:
    def test_simulate_file_write_undo(self, tmp_path):
        """Writing a file in sandbox does NOT touch the real file."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "important.py").write_text("SECRET_KEY = 'real'\n")

        engine = SandboxEngine()
        result = engine.simulate_file(
            {"op": "write", "path": "important.py",
             "content": "SECRET_KEY = 'hacked'\n"},
            repo_path=str(repo),
        )
        assert result.success is True
        # Real file must be unchanged.
        assert (repo / "important.py").read_text() == "SECRET_KEY = 'real'\n"

    def test_simulate_rm_no_side_effects(self, tmp_path):
        """Running rm in sandbox doesn't delete the real file."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "data.csv").write_text("a,b,c\n")

        engine = SandboxEngine()
        result = engine.simulate("rm data.csv", repo_path=str(repo))
        # The sandbox would delete it, but the real file stays.
        assert (repo / "data.csv").exists()
        assert "data.csv" in result.files_deleted

    def test_git_diff_capture(self, tmp_path):
        """Sandbox captures a git diff for changes."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Hello\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo,
                       capture_output=True)

        engine = SandboxEngine(keep_sandbox=True)
        result = engine.simulate(
            "echo '# Modified' > README.md",
            repo_path=str(repo),
        )
        assert result.diff
        # There should be a diff showing the README change.
        assert "README" in result.diff or "+# Modified" in result.diff

    def test_no_side_effects_from_first_class(self, tmp_path):
        """The sandbox earns its name — NO side effects ever."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / "secret.txt").write_text("don't touch this\n")

        # Take a snapshot before.
        original_content = (repo / "secret.txt").read_text()

        engine = SandboxEngine()
        engine.simulate(
            "rm secret.txt && echo 'malicious' > secret.txt",
            repo_path=str(repo),
        )

        # After simulation, real file is untouched.
        assert (repo / "secret.txt").exists()
        assert (repo / "secret.txt").read_text() == original_content
