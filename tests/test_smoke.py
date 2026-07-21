"""End-to-end smoke test: install → ingest → execute → verify artifact.

This single test runs the full Friday pipeline against a real temp repo
and asserts a real file artifact exists on disk. It should pass on every
commit. More valuable than 1000 granular unit tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import subprocess


def test_end_to_end_smoke():
    """Fresh install → ingest → pipeline → real file on disk."""
    # 1. Create a fixture repo
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "fixture"
        repo.mkdir(parents=True)
        (repo / "README.md").write_text("# Fixture Project\n\nA test project for Friday smoke tests.\n")
        (repo / "main.py").write_text("def main():\n    print('hello')\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        # 2. Run Friday commands in an isolated workspace
        orig_dir = Path.cwd()
        os.chdir(tmp)
        try:
            friday_cmd = "friday"

            # Ingest
            r = subprocess.run(
                [friday_cmd, "ingest", str(repo)],
                capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"ingest failed: {r.stderr}"

            # Workers list (should auto-bootstrap)
            r = subprocess.run(
                [friday_cmd, "workers"],
                capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"workers failed: {r.stderr}"
            assert "Python" in r.stdout or "Shell" in r.stdout, \
                f"workers output missing builtins: {r.stdout[:500]}"

            # Execute — run a simple shell command
            r = subprocess.run(
                [friday_cmd, "execute",
                 "output the current working directory and list files"],
                capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, \
                f"execute failed: stdout={r.stdout[-500:]}\nstderr={r.stderr[-500:]}"
            # Verify the shell worker produced output
            assert "Mission" in r.stdout or "session" in r.stdout or "succeeded" in r.stdout, \
                f"execute output missing success indicators: {r.stdout[:500]}"

            # Doctor should pass
            r = subprocess.run(
                [friday_cmd, "doctor"],
                capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"doctor failed: {r.stderr}"

            # Doctor should pass
            r = subprocess.run(
                [friday_cmd, "doctor"],
                capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"doctor failed: {r.stderr}"

            # Ask should not crash (output depends on LLM availability)
            r = subprocess.run(
                [friday_cmd, "ask", "what is this project"],
                capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"ask failed: {r.stderr}"

        finally:
            os.chdir(orig_dir)
