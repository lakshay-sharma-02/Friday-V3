"""Security layer tests — Wave 3 (vulnerabilities, secrets, quality gates).

Unit tests that build fixture projects in tmp_path and assert the
built-in (pure-stdlib) scanners find/ignore the right things. External
tools (pip-audit, trufflehog, ruff, mypy) are not required — the
builtin-only classes run with tool discovery stubbed (hermetic), while
TestOptionalTools exercises the optional-tool subprocess JSON parsing
paths with a fake subprocess. Plus the report model and CLI wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday_v6.security import (
    DependencyAuditor,
    QualityGate,
    SecretDetector,
    SecurityReport,
    VulnerabilityScanner,
    is_available,
)
from friday_v6.security.reporter import SEVERITY_ORDER, Finding, severity_color
from friday_v6.security.tooling import find_tool, tool_available


@pytest.fixture
def _hermetic_builtin_only(monkeypatch):
    """Keep the builtin-only tests hermetic.

    find_tool now discovers venv-installed ruff/mypy/pip-audit, so without
    stubbing, `scan()` would run real subprocesses — and worse,
    `scan_environment()` would audit the *actual running venv* via `pip
    list` (machine-dependent; breaks if the venv gains an advisory-matching
    package). TestOptionalTools covers the subprocess paths explicitly and
    is excluded from this fixture.
    """
    import friday_v6.security.deps as deps
    import friday_v6.security.quality as quality
    import friday_v6.security.secrets as secrets

    monkeypatch.setattr(quality, "find_tool", lambda name: None)
    monkeypatch.setattr(quality, "tool_available", lambda name: False)
    monkeypatch.setattr(deps, "tool_available", lambda name: False)
    monkeypatch.setattr(secrets, "find_tool", lambda name: None)
    monkeypatch.setattr(secrets, "tool_available", lambda name: False)
    monkeypatch.setattr(deps.DependencyAuditor, "scan_environment",
                        lambda self: [])


# ==========================================================================
# Report model
# ==========================================================================


class TestFinding:
    def test_severity_rank_orders(self):
        assert Finding(severity="critical").severity_rank == 0
        assert Finding(severity="high").severity_rank == 1
        assert Finding(severity="info").severity_rank == 4

    def test_stable_id(self):
        a = Finding(category="secret", title="x", file="a.py", line=1)
        b = Finding(category="secret", title="x", file="a.py", line=1)
        assert a.id == b.id
        assert len(a.id) == 12

    def test_to_dict(self):
        d = Finding(severity="high", title="t").to_dict()
        assert d["severity"] == "high" and d["title"] == "t"


class TestSecurityReport:
    def _report(self):
        rep = SecurityReport(path=".")
        rep.findings = [
            Finding(category="vulnerability", severity="critical", title="c1"),
            Finding(category="secret", severity="high", title="s1"),
            Finding(category="quality", severity="low", title="q1"),
        ]
        return rep

    def test_counts(self):
        rep = self._report()
        assert rep.counts_by_severity()["critical"] == 1
        assert rep.counts_by_severity()["high"] == 1
        assert rep.counts_by_category()["vulnerability"] == 1
        assert rep.highest_severity() == "critical"

    def test_score_and_grade(self):
        rep = SecurityReport()
        rep.findings = [Finding(severity="high", title="h")]
        assert rep.score() == 90
        assert rep.grade() == "A"

    def test_grade_drops_with_critical(self):
        rep = SecurityReport()
        rep.findings = [Finding(severity="critical", title="c"),
                        Finding(severity="critical", title="c2")]
        assert rep.score() == 60
        assert rep.grade() == "C"

    def test_above_threshold(self):
        rep = self._report()
        assert len(rep.above_threshold("high")) == 2  # critical + high
        assert len(rep.above_threshold("info")) == 3

    def test_sort_findings(self):
        rep = self._report()
        sorted_rep = rep.sort_findings()
        assert sorted_rep.findings[0].severity == "critical"

    def test_json_roundtrip(self):
        rep = self._report()
        data = json.loads(rep.to_json())
        assert data["score"] == 69  # 100 - 20 - 10 - 1
        assert data["grade"] == "C"  # 69 falls in the C band (60-74)
        assert len(data["findings"]) == 3

    def test_summary(self):
        assert "clean" in SecurityReport().summary()
        rep = self._report()
        assert rep.summary().startswith(rep.grade())

    def test_severity_color(self):
        assert severity_color("critical")  # non-empty escape code


# ==========================================================================
# DependencyAuditor (built-in)
# ==========================================================================


@pytest.mark.usefixtures("_hermetic_builtin_only")
class TestDependencyAuditor:
    def _make(self):
        return DependencyAuditor()

    def test_requirements_vulnerable_pin(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.30.0\n")
        findings = self._make().scan_manifest_file(req)
        assert any(f["cve"] == "CVE-2023-32681" for f in findings)
        assert findings[0]["fixed_version"] == "2.31.0"

    def test_requirements_safe_pin(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.31.0\nflask==3.0.0\n")
        findings = self._make().scan_manifest_file(req)
        assert findings == []

    def test_unpinned_flagged(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\nflask==3.0.0\n")
        findings = self._make().scan_manifest_file(req)
        assert any(f["title"].startswith("Unpinned") for f in findings)

    def test_pyproject_deps(self, tmp_path):
        proj = tmp_path / "pyproject.toml"
        proj.write_text(
            "[project]\n"
            "dependencies = [\n"
            '    "jinja2==3.1.2",\n'
            '    "urllib3==2.2.1",\n'
            "]\n"
        )
        findings = self._make().scan_manifest_file(proj)
        assert any(f["cve"] == "CVE-2024-22195" for f in findings)  # jinja2
        assert any(f["cve"] == "CVE-2024-37891" for f in findings)  # urllib3 2.x

    def test_major_line_respect(self, tmp_path):
        """urllib3 1.x advisory must not fire for 2.x and vice versa."""
        req = tmp_path / "requirements.txt"
        req.write_text("urllib3==1.26.18\n")
        findings = self._make().scan_manifest_file(req)
        assert any(f["cve"] == "CVE-2024-37891" for f in findings)

    def test_scan_directory_finds_manifests(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("werkzeug==3.0.2\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "requirements-dev.txt").write_text("requests==2.30.0\n")
        findings, tools = self._make().scan(tmp_path)
        assert any(f["cve"] == "CVE-2024-34069" for f in findings)  # werkzeug
        assert any(f["cve"] == "CVE-2023-32681" for f in findings)  # requests
        assert tools["builtin-deps"] is True

    def test_missing_dir_never_raises(self, tmp_path):
        findings, tools = self._make().scan(tmp_path / "nope")
        assert findings == []
        assert tools["builtin-deps"] is True


# ==========================================================================
# SecretDetector (built-in)
# ==========================================================================


@pytest.mark.usefixtures("_hermetic_builtin_only")
class TestSecretDetector:
    def _make(self):
        return SecretDetector()

    def test_detects_aws_key(self, tmp_path):
        # NB: AKIAIOSFODNN7EXAMPLE (AWS's documented example) is allowlisted
        # as a known non-secret — the detector must still flag real-format
        # keys, so use a different fake value here. The AWS pattern is
        # AKIA + exactly 16 [0-9A-Z] (20 chars total).
        f = tmp_path / "config.py"
        f.write_text("AWS_ACCESS_KEY = 'AKIA1234567890ABCDEF'\n")
        findings = self._make().scan(tmp_path)[0]
        assert any("AWS" in x["title"] for x in findings)

    def test_detects_github_token(self, tmp_path):
        f = tmp_path / "env.json"
        f.write_text('{"token": "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"}\n')
        findings = self._make().scan(tmp_path)[0]
        assert any("GitHub" in x["title"] for x in findings)

    def test_detects_private_key(self, tmp_path):
        # A truncated stub (BEGIN without END) is a doc/test artifact, not an
        # exposed key — real PEM blocks always contain their END marker.
        f = tmp_path / "server.py"
        f.write_text(
            "key = '''-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpQ==\n"
            "-----END RSA PRIVATE KEY-----\n'''\n")
        findings = self._make().scan(tmp_path)[0]
        assert any("Private Key" in x["title"] for x in findings)

    def test_skips_placeholder_generic(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text("password = 'changeme'\napi_key = 'test'\n")
        findings = self._make().scan(tmp_path)[0]
        assert not any(x["title"] == "Exposed secret: Generic Secret Assignment"
                       for x in findings)

    def test_flags_high_entropy_generic(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text("password = 'Kx9mQ2vP7Lz4rWt8cR3nB6hJ1'\n")
        findings = self._make().scan(tmp_path)[0]
        assert any(x["title"] == "Exposed secret: Generic Secret Assignment"
                   for x in findings)

    def test_skips_venv_and_git(self, tmp_path):
        (tmp_path / ".venv").mkdir(parents=True)
        (tmp_path / ".venv" / "dep.py").write_text(
            "token = 'ghp_0123456789abcdefghijklmnopqrstuvwxyzAB'\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            "password = 'Kx9mQ2vP7Lz4rWt8cR3nB6hJ1'\n")
        f = tmp_path / "main.py"
        f.write_text("x = 1\n")
        findings = self._make().scan(tmp_path)[0]
        assert len(findings) == 0

    def test_line_numbers(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\nsecret = 'AKIA1234567890ABCDEF'\n")
        findings = self._make().scan(tmp_path)[0]
        assert findings[0]["line"] == 2

    def test_single_file_scan(self, tmp_path):
        f = tmp_path / "one.py"
        f.write_text("key = 'sk_live_0123456789abcdefghij'\n")
        findings = self._make().scan(f)[0]
        assert any("Stripe" in x["title"] for x in findings)


# ==========================================================================
# QualityGate (built-in)
# ==========================================================================


@pytest.mark.usefixtures("_hermetic_builtin_only")
class TestQualityGate:
    def _make(self):
        return QualityGate()

    def test_syntax_error_flagged(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def broken(:\n    pass\n")
        findings = self._make().scan(tmp_path)[0]
        assert any(x["title"] == "Syntax error" for x in findings)

    def test_unused_import(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("import os\n\ndef f():\n    return 1\n")
        findings = self._make().scan(tmp_path)[0]
        assert any("Unused import: os" in x["title"] for x in findings)

    def test_undefined_name(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def f():\n    return missing_var\n")
        findings = self._make().scan(tmp_path)[0]
        assert any("undefined name" in x["title"].lower() for x in findings)

    def test_bare_except(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("try:\n    pass\nexcept:\n    pass\n")
        findings = self._make().scan(tmp_path)[0]
        assert any(x["title"] == "Bare except clause" for x in findings)

    def test_long_line_and_todo(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("x = " + "1 + " * 40 + "1  # TODO: optimize\n")
        findings = self._make().scan(tmp_path)[0]
        assert any(x["snippet"] == "line-length" for x in findings)
        assert any(x["snippet"] == "todo" for x in findings)

    def test_skips_venv(self, tmp_path):
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "dep.py").write_text("import bad\n")
        f = tmp_path / "main.py"
        f.write_text("x = 1\n")
        findings = self._make().scan(tmp_path)[0]
        assert not any("dep.py" in x["file"] for x in findings)

    def test_clean_file(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("def add(a, b):\n    return a + b\n")
        findings = self._make().scan(tmp_path)[0]
        assert findings == []

    def test_init_py_reexports_not_flagged_unused(self, tmp_path):
        """__init__.py imports ARE the public API (re-exports) — the
        built-in unused-import check must skip them, like ruff's F401."""
        (tmp_path / "__init__.py").write_text(
            "from .mod import Thing\nfrom .sub import helper\n")
        (tmp_path / "mod.py").write_text("class Thing: pass\n")
        (tmp_path / "sub.py").write_text("def helper(): pass\n")
        findings = self._make().scan(tmp_path)[0]
        assert not any("Unused import" in x["title"] for x in findings)

    def test_dunder_names_not_undefined(self, tmp_path):
        """__file__ / __name__ are language-provided, not undefined names."""
        f = tmp_path / "m.py"
        f.write_text("print(__file__)\nprint(__name__)\n")
        findings = self._make().scan(tmp_path)[0]
        assert not any("undefined" in x["title"].lower() for x in findings)

    def test_noqa_suppresses_unused_import(self, tmp_path):
        """Availability-probe imports carry `# noqa: F401` — the builtin
        checker must respect inline suppressions like ruff does."""
        f = tmp_path / "probe.py"
        f.write_text(
            "def probe():\n"
            "    try:\n"
            "        import PIL  # noqa: F401\n"
            "        return True\n"
            "    except ImportError:\n"
            "        return False\n")
        findings = self._make().scan(tmp_path)[0]
        assert not any("Unused import" in x["title"] for x in findings)

    def test_skips_line_length_inside_string_literal(self, tmp_path):
        """Embedded templates (CSS/JS/PowerShell in a triple-quoted string)
        can't be wrapped — lines inside a multi-line string literal must not
        be flagged as too long."""
        f = tmp_path / "tpl.py"
        f.write_text('PAGE = r"""' + "x" * 200 + "\n" + "y" * 200 + ' """\n')
        findings = self._make().scan(tmp_path)[0]
        assert not any(x["snippet"] == "line-length" for x in findings)

    def test_todo_in_string_not_flagged(self, tmp_path):
        """'TODO' inside string data (labels, docstrings, detector source)
        is not a task marker — only a real `#` comment is."""
        f = tmp_path / "m.py"
        f.write_text(
            'LABEL = "TODO: ship this"\n'
            "# TODO: actually fix this\n"
        )
        findings = self._make().scan(tmp_path)[0]
        todos = [x for x in findings if x["snippet"] == "todo"]
        assert len(todos) == 1
        assert "actually fix" in todos[0]["detail"]


# ==========================================================================
# VulnerabilityScanner (orchestration)
# ==========================================================================


@pytest.mark.usefixtures("_hermetic_builtin_only")
class TestVulnerabilityScanner:
    def _project(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests==2.30.0\n")
        (tmp_path / "app.py").write_text(
            "import os\nSECRET = 'AKIA1234567890ABCDEF'\n"
            "def f():\n    return missing_var\n")
        return tmp_path

    def test_full_scan(self, tmp_path):
        report = VulnerabilityScanner().scan(self._project(tmp_path))
        assert report.scanned_files >= 2
        assert report.counts_by_category().get("vulnerability", 0) >= 1
        assert report.counts_by_category().get("secret", 0) >= 1
        assert report.counts_by_category().get("quality", 0) >= 1
        # sorted most-severe first
        sevs = [f.severity for f in report.findings]
        assert sevs == sorted(sevs, key=SEVERITY_ORDER.index)

    def test_disable_flags(self, tmp_path):
        report = VulnerabilityScanner().scan(
            self._project(tmp_path),
            enable_deps=False, enable_secrets=False, enable_quality=False)
        assert report.findings == []

    def test_scan_quick_threshold(self, tmp_path):
        report = VulnerabilityScanner().scan_quick(self._project(tmp_path),
                                                   threshold="high")
        for f in report.findings:
            assert f.severity in ("critical", "high")

    def test_missing_path(self, tmp_path):
        report = VulnerabilityScanner().scan(tmp_path / "nope")
        assert report.findings == []
        assert report.score() == 100


# ==========================================================================
# Tooling (venv-aware tool discovery)
# ==========================================================================


class TestTooling:
    def test_find_tool_on_path(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ruff")
        assert find_tool("ruff") == "/usr/bin/ruff"

    def test_find_tool_in_venv_bin(self, monkeypatch, tmp_path):
        """Regression: tools in the active venv's bin (not on PATH) must
        still be found — the bug that made `security status` / `doctor`
        report ruff/mypy as missing."""
        import shutil
        import sys

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "ruff"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)  # find_tool requires an executable file
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        assert find_tool("ruff") == str(exe)

    def test_find_tool_script_path(self, monkeypatch, tmp_path):
        """Windows-style Scripts dir is also probed."""
        import shutil
        import sys

        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        exe = scripts / "mypy"
        exe.write_text("x")
        exe.chmod(0o755)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        assert find_tool("mypy") == str(exe)

    def test_find_tool_skips_non_executable(self, monkeypatch, tmp_path):
        """Parity with shutil.which: a non-executable stub in the venv bin
        must NOT be handed back (it would fail at subprocess launch)."""
        import shutil
        import sys

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "bandit").write_text("not executable")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        assert find_tool("bandit") is None

    def test_find_tool_missing(self, monkeypatch):
        import shutil
        import sys

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "prefix", "/nonexistent-prefix")
        assert find_tool("definitely-not-a-tool") is None
        assert tool_available("definitely-not-a-tool") is False

    def test_tool_available_true(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/x/bandit")
        assert tool_available("bandit") is True


# ==========================================================================
# Deps: pure parsing + version helpers
# ==========================================================================


@pytest.mark.usefixtures("_hermetic_builtin_only")
class TestDepsParsing:
    def _auditor(self):
        return DependencyAuditor()

    def test_version_tuple_ordering(self):
        from friday_v6.security.deps import version_lt, version_tuple

        assert version_tuple("2.31.0") == (2, 31, 0)
        assert version_tuple("1.26.19") < version_tuple("2.0.0")
        assert version_lt("2.30.0", "2.31.0") is True
        assert version_lt("2.31.0", "2.30.0") is False
        assert version_lt("2.31.0", "2.31.0") is False
        assert version_lt("3.0.0rc1", "3.0.0") is True

    def test_parse_requirements_comments_and_flags(self):
        from friday_v6.security.deps import parse_requirements

        text = "# comment\n-r other.txt\n--index-url https://x\nrequests==2.31.0\n"
        deps = parse_requirements(text)
        assert len(deps) == 1
        assert deps[0]["name"] == "requests"
        assert deps[0]["pinned"] is True

    def test_range_spec_counts_as_pinned(self, tmp_path):
        """A >= / ~= / < constraint is a real pin — only bare names flag."""
        from friday_v6.security.deps import parse_requirements

        deps = parse_requirements("edge-tts>=7.0\nflask~=3.0\nrequests\n")
        by_name = {d["name"]: d for d in deps}
        assert by_name["edge-tts"]["pinned"] is True
        assert by_name["flask"]["pinned"] is True
        assert by_name["requests"]["pinned"] is False
        # And scanning a manifest with only range pins yields no unpinned finding.
        req = tmp_path / "requirements.txt"
        req.write_text("edge-tts>=7.0\n")
        findings = self._auditor().scan_manifest_file(req)
        assert not any("Unpinned" in f["title"] for f in findings)

    def test_parse_requirements_vcs_url(self):
        from friday_v6.security.deps import parse_requirements

        deps = parse_requirements("-e git+https://github.com/x/y.git#egg=y\n")
        assert deps[0]["name"] == ""
        assert "git+" in deps[0]["spec"]

    def test_parse_requirements_extras_and_ranges(self):
        from friday_v6.security.deps import parse_requirements

        deps = parse_requirements(
            "requests[security]>=2.31,<3\nflask~=3.0\n")
        names = {d["name"] for d in deps}
        assert names == {"requests", "flask"}

    def test_pyproject_optional_dependencies(self, tmp_path):
        proj = tmp_path / "pyproject.toml"
        proj.write_text(
            "[project]\n"
            'dependencies = ["flask==3.0.0"]\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest==8.0.0"]\n'
        )
        findings = self._auditor().scan_manifest_file(proj)
        assert findings == []  # both pinned + safe

    def test_scan_skips_venv_manifests(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests==2.30.0\n")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "requirements.txt").write_text(
            "requests==1.0.0\n")
        findings, _ = self._auditor().scan(tmp_path)
        assert any(f["cve"] == "CVE-2023-32681" for f in findings)


# ==========================================================================
# Optional subprocess integrations (ruff / mypy / trufflehog / pip-audit)
# ==========================================================================


class TestOptionalTools:
    def _fake_run(self, monkeypatch, stdout: str, returncode: int = 0):
        import subprocess
        from types import SimpleNamespace

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=returncode,
                                            stdout=stdout))

    def test_scan_with_ruff_parses_json(self, monkeypatch, tmp_path):
        # ruff's JSON schema puts the path at the TOP level ("filename");
        # location only carries row/column. Regression: the parser used to
        # read location.file, which ruff leaves empty → empty paths.
        payload = json.dumps([{
            "code": "F401",
            "message": "unused import",
            "filename": "src/a.py",
            "location": {"row": 3, "column": 1},
        }])
        self._fake_run(monkeypatch, payload)
        monkeypatch.setattr(
            "friday_v6.security.quality.find_tool", lambda name: "/usr/bin/ruff")
        findings = QualityGate().scan_with_ruff(tmp_path)
        assert findings[0]["severity"] == "high"  # F* code
        assert findings[0]["snippet"] == "F401"
        assert findings[0]["file"] == "src/a.py"  # top-level filename key
        assert findings[0]["line"] == 3

    def test_scan_with_ruff_pins_rule_set(self, monkeypatch, tmp_path):
        """The ruff pass must run with an explicit --select (ruff 0.16's
        default select is a broad style set — noise for a security scan)."""
        import subprocess
        from types import SimpleNamespace

        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="[]")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(
            "friday_v6.security.quality.find_tool", lambda name: "/usr/bin/ruff")
        QualityGate().scan_with_ruff(tmp_path)
        cmd = " ".join(captured["cmd"])
        assert "--select" in cmd
        assert "E4,E7,E9,F,W" in cmd

    def test_scan_with_mypy_parses_json(self, monkeypatch, tmp_path):
        payload = json.dumps([{
            "file": "b.py", "line": 4, "severity": "error",
            "message": "arg-type",
        }])
        self._fake_run(monkeypatch, payload)
        monkeypatch.setattr(
            "friday_v6.security.quality.find_tool", lambda name: "/usr/bin/mypy")
        findings = QualityGate().scan_with_mypy(tmp_path)
        assert findings[0]["severity"] == "high"
        assert findings[0]["file"] == "b.py"

    def test_scan_with_trufflehog_parses_verified(self, monkeypatch, tmp_path):
        line = json.dumps({
            "Verified": True,
            "DetectorName": "AWS",
            "Raw": "AKIA...",
            "SourceMetadata": {"Data": {"Filesystem": {"file": "c.py",
                                                         "line": 7}}},
        })
        self._fake_run(monkeypatch, line + "\n")
        monkeypatch.setattr(
            "friday_v6.security.secrets.find_tool", lambda name: "/usr/bin/trufflehog")
        findings = SecretDetector().scan_with_trufflehog(tmp_path)
        assert findings[0]["severity"] == "critical"
        assert findings[0]["file"] == "c.py"

    def test_scan_with_trufflehog_skips_unverified(self, monkeypatch, tmp_path):
        line = json.dumps({"Verified": False, "DetectorName": "AWS",
                           "Raw": "AKIA..."})
        self._fake_run(monkeypatch, line + "\n")
        monkeypatch.setattr(
            "friday_v6.security.secrets.find_tool", lambda name: "/usr/bin/trufflehog")
        assert SecretDetector().scan_with_trufflehog(tmp_path) == []

    def test_scan_with_pip_audit_parses_json(self, monkeypatch, tmp_path):
        payload = json.dumps({"dependencies": [{
            "name": "requests", "version": "2.30.0",
            "vulns": [{"id": "CVE-2023-32681", "severity": "HIGH",
                        "fix_versions": ["2.31.0"],
                        "description": "leak"}],
        }]})
        self._fake_run(monkeypatch, payload, returncode=1)  # 1 = vulns found
        monkeypatch.setattr(
            "friday_v6.security.deps.tool_available", lambda name: True)
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.30.0\n")
        findings = DependencyAuditor().scan_with_pip_audit(req)
        assert findings[0]["cve"] == "CVE-2023-32681"
        assert findings[0]["detector"] == "pip-audit"

    def test_scan_with_pip_audit_absent_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "friday_v6.security.deps.tool_available", lambda name: False)
        assert DependencyAuditor().scan_with_pip_audit(Path("requirements.txt")) == []


# ==========================================================================
# Package availability + CLI wiring
# ==========================================================================


class TestSecurityPackage:
    def test_is_available_true(self):
        assert is_available() is True

    def test_all_classes_exposed(self):
        for cls in (VulnerabilityScanner, SecretDetector, DependencyAuditor,
                    QualityGate, SecurityReport, Finding):
            assert callable(cls)


class TestSecurityCLI:
    def test_parser_registers_subcommands(self):
        import argparse

        from friday_v6.cli_security import build_security_parser

        parser = argparse.ArgumentParser(prog="friday6")
        subparsers = parser.add_subparsers(dest="command")
        build_security_parser(subparsers)
        args = parser.parse_args(["security", "scan", "--threshold", "high"])
        assert args.threshold == "high"
        args = parser.parse_args(["security", "scan", "--json", "--no-quality"])
        assert args.json is True and args.no_quality is True
        args = parser.parse_args(["security", "status"])
        assert args.security_command == "status"

    def test_integrated_friday6_parser_includes_security(self):
        """The `friday6` entry point exposes `friday6 security`."""
        import argparse

        from friday_v6.cli_daemon import build_daemon_parser
        from friday_v6.cli_desktop import build_desktop_parser
        from friday_v6.cli_doctor import build_doctor_parser
        from friday_v6.cli_intelligence import build_intelligence_parser
        from friday_v6.cli_proactive import build_proactive_parser
        from friday_v6.cli_security import build_security_parser
        from friday_v6.cli_talk import build_talk_parser, build_voice_parser
        parser = argparse.ArgumentParser(prog="friday6")
        subparsers = parser.add_subparsers(dest="command")
        build_talk_parser(subparsers)
        build_voice_parser(subparsers)
        build_daemon_parser(subparsers)
        build_doctor_parser(subparsers)
        build_desktop_parser(subparsers)
        build_proactive_parser(subparsers)
        build_intelligence_parser(subparsers)
        build_security_parser(subparsers)

        args = parser.parse_args(["security", "scan", "."])
        assert args.security_command == "scan"
