"""Hermetic tests for Wave 12 polish — benchmarks, docs site, SSH executor."""

from __future__ import annotations

from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────
# tools/benchmarks.py — V4 benchmark harness
# ─────────────────────────────────────────────────────────────────────


class TestBenchmarks:
    def test_v4_benchmarks_run_hermetic(self, tmp_path: Path):
        from tools.benchmarks import v4_benchmarks
        results = v4_benchmarks(tmp_path, iterations=2)
        assert results
        # Every key measured (None = degraded but no crash) — the harness
        # must never raise on a fresh fixture.
        for key in ("v4.db_connect_migrate", "v4.security_scan_fixture",
                    "v4.research_analyze", "v4.reasoning_answer",
                    "v4.collab_merge_20", "v4.ambient_publish"):
            assert key in results

    def test_v3_benchmarks_empty_when_unavailable(self, tmp_path: Path):
        from tools.benchmarks import v3_available, v3_benchmarks
        # V3 is not installed in the hermetic test env — must return {}.
        results = v3_benchmarks(tmp_path, iterations=1)
        if not v3_available():
            assert results == {}
        else:
            assert isinstance(results, dict)

    def test_main_runs(self, tmp_path: Path):
        from tools.benchmarks import main
        assert main(["--iterations", "1"]) == 0


# ─────────────────────────────────────────────────────────────────────
# tools/build_docs_site.py — pure-stdlib markdown → HTML
# ─────────────────────────────────────────────────────────────────────


class TestDocsSite:
    def test_md_to_html_basic(self):
        from tools.build_docs_site import md_to_html
        html = md_to_html("# Title\n\nSome **bold** text with `code`.")
        assert "<h1>Title</h1>" in html
        assert "<strong>bold</strong>" in html
        assert "<code>code</code>" in html

    def test_md_to_html_code_and_list(self):
        from tools.build_docs_site import md_to_html
        html = md_to_html("```python\nx = 1\n```\n\n- a\n- b")
        assert "<pre><code" in html
        assert "<ul>" in html and "<li>a</li>" in html

    def test_md_to_html_escapes_html(self):
        from tools.build_docs_site import md_to_html
        html = md_to_html("<script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_build_site_writes_pages(self, tmp_path: Path):
        from tools.build_docs_site import build_site
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "plan.md").write_text("# Plan\n\nbody")
        out = tmp_path / "site"
        written = build_site(docs, out)
        assert (out / "index.html").exists()
        assert (out / "plan.html").exists()
        assert any(p.name == "index.html" for p in written)
        assert "Plan" in (out / "plan.html").read_text()


# ─────────────────────────────────────────────────────────────────────
# execution/executors.py — SSH executor (Wave 12 network fold-in)
# ─────────────────────────────────────────────────────────────────────


class TestSSHExecutor:
    def test_ssh_registered(self):
        from friday_v6.execution.executors import _EXECUTORS, SSHExecutor
        assert _EXECUTORS["ssh"] is SSHExecutor
        from friday_v6.network import ssh_available
        assert ssh_available() is True

    def test_ssh_gate_confirm_by_default(self):
        from friday_v6.execution.gate import PermissionLevel, PermissionGate
        gate = PermissionGate()
        assert gate.level_for("ssh", "build@10.0.0.5 df -h") \
            == PermissionLevel.CONFIRM
        # Destructive remote commands escalate to NEVER.
        assert gate.level_for("ssh", "root@prod git push origin main") \
            == PermissionLevel.NEVER

    def test_ssh_denied_without_confirmation(self, tmp_path):
        from friday_v6 import db
        from friday_v6.execution import execute
        conn = db.connect(tmp_path / "v4.db")
        try:
            result = execute("ssh", "build@10.0.0.5 df -h",
                             conn=conn, confirm_fn=None)
            assert result.status == "denied"
            assert result.action_id  # audited even when denied
        finally:
            conn.close()

    def test_ssh_undo_none(self):
        from friday_v6.execution.executors import SSHExecutor
        ex = SSHExecutor()
        assert ex._undo_payload("host ls") == {"op": "none"}

    def test_ssh_missing_client_graceful(self, tmp_path, monkeypatch):
        """No ssh binary → structured failure, never an exception."""
        from friday_v6 import db
        from friday_v6.execution import execute
        from friday_v6.execution import executors as exec_mod

        monkeypatch.setattr(exec_mod, "find_tool", lambda name: None)
        conn = db.connect(tmp_path / "v4.db")
        try:
            result = execute("ssh", "host df -h", conn=conn, force=True)
            assert result.status == "failed"
            assert "ssh client not found" in result.output
        finally:
            conn.close()

    def test_ssh_parses_host_and_remote(self, monkeypatch):
        """ssh runs as [ssh, host, 'remote command'] through the sandbox."""
        from friday_v6.execution.executors import SSHExecutor

        captured = {}

        class _FakeSandbox:
            def run(self, args, cwd=None):
                captured["args"] = args
                from friday_v6.execution.sandbox import SandboxResult
                return SandboxResult(result_code=0, stdout="ok")

        monkeypatch.setattr(
            "friday_v6.execution.executors.find_tool", lambda name: "/usr/bin/ssh")
        ex = SSHExecutor(sandbox=_FakeSandbox())
        res = ex.run("user@10.0.0.9 df -h /tmp")
        assert res.result_code == 0
        assert captured["args"][0] == "/usr/bin/ssh"
        assert captured["args"][1] == "user@10.0.0.9"
        assert captured["args"][2] == "df -h /tmp"
