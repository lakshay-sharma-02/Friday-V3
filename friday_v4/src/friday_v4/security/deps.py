"""DependencyAuditor — scan project manifests + installed env for known-vulnerable packages.

Wave 3 (Security & Quality). Two layers:

1. **Built-in (always available, pure stdlib):** parse ``requirements*.txt``,
   ``pyproject.toml`` dependencies, and the installed environment
   (``pip list --format=json``) and compare versions against a small
   **curated advisory database** of real, well-known CVEs with fixed
   versions. Also flags unpinned dependencies as low-severity advisories.

2. **Optional subprocess:** if the ``pip-audit`` CLI is installed, run it
   per-manifest and merge its JSON findings (full OSV coverage). If it is
   missing, the scanner degrades silently to the built-in checks — it never
   fails the scan.

The advisory DB is intentionally small and hand-maintained: it is a
best-effort offline heuristic, NOT a replacement for pip-audit/OSV. It
exists so ``friday4 security scan`` produces real value on a machine
with no security tooling installed (this one).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .tooling import tool_available

logger = logging.getLogger("friday_v4.security.deps")


# ---------------------------------------------------------------------------
# Version comparison (minimal PEP 440-ish, numeric dot segments)
# ---------------------------------------------------------------------------


def _num(seg: str) -> int:
    try:
        return int(seg)
    except ValueError:
        return 0


def version_tuple(version: str) -> tuple[int, ...]:
    """'2.31.0' → (2, 31, 0); tolerate suffixes like '1.0rc1'."""
    parts = re.split(r"[.+-]", str(version).strip())
    return tuple(_num(p) for p in parts if p.isdigit() or p.lstrip("-").isdigit())


def version_lt(a: str, b: str) -> bool:
    return version_tuple(a) < version_tuple(b)


# ---------------------------------------------------------------------------
# Curated advisory database (package → fixed version → advisory)
# ---------------------------------------------------------------------------
# Hand-verified, real CVEs. `major` restricts the advisory to a major line
# (urllib3 1.x and 2.x have separate fixes for the same CVE).

_ADVISORIES: list[dict[str, Any]] = [
    {"package": "requests", "fixed": "2.31.0", "major": None,
     "cve": "CVE-2023-32681", "severity": "medium",
     "title": "requests leaks sensitive headers on redirect",
     "detail": "Authorization and Cookie headers may be sent to a different "
               "host on redirect. Upgrade to >= 2.31.0."},
    {"package": "urllib3", "fixed": "1.26.19", "major": 1,
     "cve": "CVE-2024-37891", "severity": "medium",
     "title": "urllib3 leaks Proxy-Authorization header on redirect",
     "detail": "When a proxy is configured, the Proxy-Authorization header "
               "may be forwarded to the new origin on redirect. "
               "Upgrade to >= 1.26.19."},
    {"package": "urllib3", "fixed": "2.2.2", "major": 2,
     "cve": "CVE-2024-37891", "severity": "medium",
     "title": "urllib3 leaks Proxy-Authorization header on redirect",
     "detail": "When a proxy is configured, the Proxy-Authorization header "
               "may be forwarded to the new origin on redirect. "
               "Upgrade to >= 2.2.2."},
    {"package": "pyyaml", "fixed": "5.4", "major": None,
     "cve": "CVE-2020-14343", "severity": "high",
     "title": "PyYAML unsafe yaml.load arbitrary code execution",
     "detail": "yaml.load() without a safe Loader can execute arbitrary code "
               "from untrusted YAML. Upgrade to >= 5.4."},
    {"package": "jinja2", "fixed": "3.1.3", "major": None,
     "cve": "CVE-2024-22195", "severity": "high",
     "title": "Jinja2 HTML attribute injection",
     "detail": "xmlattr filter does not escape single quotes, enabling XSS "
               "in some contexts. Upgrade to >= 3.1.3."},
    {"package": "pillow", "fixed": "10.2.0", "major": None,
     "cve": "CVE-2023-50447", "severity": "high",
     "title": "Pillow heap buffer overflow in JPEG2000 decoding",
     "detail": "Jpeg2KImagePlugin has an out-of-bounds write on crafted input. "
               "Upgrade to >= 10.2.0."},
    {"package": "aiohttp", "fixed": "3.9.2", "major": None,
     "cve": "CVE-2024-23334", "severity": "high",
     "title": "aiohttp static file directory traversal",
     "detail": "Static file serving can traverse outside the configured "
               "directory. Upgrade to >= 3.9.2."},
    {"package": "starlette", "fixed": "0.36.3", "major": None,
     "cve": "CVE-2024-24762", "severity": "medium",
     "title": "Starlette StaticFiles directory traversal",
     "detail": "StaticFiles allows traversal via encoded separators. "
               "Upgrade to >= 0.36.3."},
    {"package": "setuptools", "fixed": "70.0.0", "major": None,
     "cve": "CVE-2024-6345", "severity": "high",
     "title": "setuptools arbitrary code execution via crafted package",
     "detail": "package_index may execute arbitrary code from a crafted "
               "package. Upgrade to >= 70.0.0."},
    {"package": "werkzeug", "fixed": "3.0.3", "major": None,
     "cve": "CVE-2024-34069", "severity": "high",
     "title": "Werkzeug file disclosure via …/ path traversal",
     "detail": "Sending a crafted …/ path can expose file contents. "
               "Upgrade to >= 3.0.3 (or 2.3.8)."},
    {"package": "gunicorn", "fixed": "20.1.0", "major": None,
     "cve": "CVE-2024-1135", "severity": "medium",
     "title": "gunicorn HTTP request splitting",
     "detail": "Transfer-Encoding smuggling can split requests. "
               "Upgrade to >= 20.1.0."},
]


def _advisory_fixed(package: str, version: str) -> Optional[dict[str, Any]]:
    """Return the matching advisory for an installed version, or None."""
    package = package.lower().replace("_", "-")
    for adv in _ADVISORIES:
        if adv["package"].lower().replace("_", "-") != package:
            continue
        major = adv.get("major")
        if major is not None and version_tuple(version)[:1] != (major,):
            continue
        if version_lt(version, adv["fixed"]):
            return adv
    return None


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _norm_name(name: str) -> str:
    name = re.split(r"[\[<=>!~]", name.strip())[0].strip()
    return name.lower().replace("_", "-")


def _has_version_spec(spec: str) -> bool:
    """True if a dependency spec carries ANY version constraint.

    ``>=7.0``, ``~=3.0``, ``<3`` and ``!=2`` are all real pins — only a
    bare name (``requests`` with no specifier) is truly unpinned.
    """
    return bool(re.search(r"[<>=~!]", spec))


def parse_requirements(text: str) -> list[dict[str, Any]]:
    """Parse requirements.txt content into ``{name, spec, pinned, version}``."""
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "-c", "--")):
            continue
        if line.startswith("-e") or line.startswith("git+") or "://" in line:
            out.append({"name": "", "spec": line, "pinned": False,
                        "version": "", "raw": line})
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", line)
        if not m:
            continue
        name, spec = m.group(1), m.group(2).strip()
        spec = spec.strip("'\"")
        out.append({
            "name": _norm_name(name), "spec": spec,
            "pinned": _has_version_spec(spec),
            "version": spec.replace("==", "").strip() if "==" in spec else "",
            "raw": line,
        })
    return out


def parse_pyproject_toml(path: Path) -> list[dict[str, Any]]:
    """Extract dependency specs from a pyproject.toml (project table)."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - py3.12 target
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        logger.debug(f"pyproject parse failed: {exc}")
        return []
    proj = data.get("project", {})
    if not isinstance(proj, dict):
        return out
    for raw in proj.get("dependencies") or []:
        if not isinstance(raw, str):
            continue
        name, spec = _split_pep508(raw)
        if name:
            out.append({"name": _norm_name(name), "spec": spec,
                        "pinned": _has_version_spec(spec),
                        "version": _pin_version(spec),
                        "raw": raw})
    opt = proj.get("optional-dependencies") or {}
    for group in opt.values() or []:
        for raw in group:
            if not isinstance(raw, str):
                continue
            name, spec = _split_pep508(raw)
            if name:
                out.append({"name": _norm_name(name), "spec": spec,
                            "pinned": _has_version_spec(spec),
                            "version": _pin_version(spec),
                            "raw": raw})
    return out


def _split_pep508(raw: str) -> tuple[str, str]:
    """'requests[security]>=2.31,<3' → ('requests', '>=2.31,<3')."""
    raw = raw.strip().strip("'\"")
    m = re.match(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(.*)", raw)
    if not m:
        return "", ""
    name, spec = m.group(1), m.group(2).strip()
    return name, spec


def _pin_version(spec: str) -> str:
    m = re.search(r"==\s*([\w.]+)", spec)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# DependencyAuditor
# ---------------------------------------------------------------------------


class DependencyAuditor:
    """Find known-vulnerable and unpinned dependencies in a project."""

    detector_name = "deps"

    MANIFEST_GLOBS = ("requirements*.txt", "requirements/*.txt",
                      "pyproject.toml", "Pipfile")

    #: Directory parts never descended into when discovering manifests.
    _NOISE: frozenset[str] = frozenset({
        ".git", ".venv", "venv", "node_modules", "site-packages",
        "__pycache__", "dist", "build", ".tox", ".egg-info"})

    def __init__(self, subprocess_timeout: float = 60.0):
        self.subprocess_timeout = subprocess_timeout
        self.tools: dict[str, bool] = {}

    # -- tool discovery --------------------------------------------------

    def _tool_available(self, name: str) -> bool:
        ok = tool_available(name)
        self.tools[name] = ok
        return ok

    # -- built-in scans ---------------------------------------------------

    def scan_manifest_file(self, path: Path) -> list[dict[str, Any]]:
        """Check one manifest file against the curated advisory DB."""
        findings: list[dict[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return findings

        if path.name == "pyproject.toml":
            deps = parse_pyproject_toml(path)
        else:
            deps = parse_requirements(text)

        for dep in deps:
            name = dep.get("name", "")
            spec = dep.get("spec", "")
            pinned = dep.get("pinned", False)
            if not name:
                if "://" in spec or spec.startswith("git+"):
                    findings.append({
                        "severity": "info",
                        "title": "Dependency installed from VCS",
                        "detail": f"'{spec}' is pinned to a repo/URL rather than "
                                  "a released version — audit manually.",
                        "package": spec.split("://")[-1][:40],
                        "detector": "builtin",
                    })
                continue
            if not pinned:
                findings.append({
                    "severity": "low",
                    "title": f"Unpinned dependency: {name}",
                    "detail": f"'{spec}' has no version constraint (pin with "
                              "==, >=, ~= …). Unpinned deps can pull "
                              "breaking or vulnerable versions on install.",
                    "package": name,
                    "detector": "builtin",
                })
                continue
            version = dep.get("version") or _pin_version(spec)
            adv = _advisory_fixed(name, version) if version else None
            if adv:
                findings.append({
                    "severity": adv["severity"],
                    "title": adv["title"],
                    "detail": adv["detail"],
                    "package": name,
                    "installed_version": version,
                    "fixed_version": adv["fixed"],
                    "cve": adv["cve"],
                    "detector": "builtin",
                })
        return findings

    def scan_environment(self) -> list[dict[str, Any]]:
        """Check installed packages (pip list) against the advisory DB."""
        findings: list[dict[str, Any]] = []
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format=json"],
                capture_output=True, text=True, timeout=self.subprocess_timeout)
            if result.returncode != 0:
                return findings
            pkgs = json.loads(result.stdout)
        except Exception as exc:
            logger.debug(f"pip list failed: {exc}")
            return findings
        for pkg in pkgs:
            name = _norm_name(pkg.get("name", ""))
            version = str(pkg.get("version", ""))
            adv = _advisory_fixed(name, version) if version else None
            if adv:
                findings.append({
                    "severity": adv["severity"],
                    "title": adv["title"],
                    "detail": adv["detail"],
                    "package": name,
                    "installed_version": version,
                    "fixed_version": adv["fixed"],
                    "cve": adv["cve"],
                    "detector": "builtin",
                })
        return findings

    # -- optional pip-audit subprocess ------------------------------------

    def scan_with_pip_audit(self, path: Path) -> list[dict[str, Any]]:
        """Run ``pip-audit`` on a manifest and parse its JSON findings."""
        if not self._tool_available("pip-audit"):
            return []
        cmd = ["pip-audit", "--format=json", "--no-deps",
               "-r", str(path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=self.subprocess_timeout)
            if result.returncode not in (0, 1):  # 1 = vulns found (expected)
                return []
            data = json.loads(result.stdout)
        except Exception as exc:
            logger.debug(f"pip-audit failed: {exc}")
            return []
        findings: list[dict[str, Any]] = []
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                sev = str(vuln.get("severity") or "").lower()
                if sev not in ("critical", "high", "medium", "low", "info"):
                    sev = "high"
                fixed = ""
                for fix in vuln.get("fix_versions", []) or []:
                    fixed = fix
                    break
                findings.append({
                    "severity": sev,
                    "title": vuln.get("id") or "pip-audit finding",
                    "detail": vuln.get("description") or "",
                    "package": dep.get("name", ""),
                    "installed_version": dep.get("version", ""),
                    "fixed_version": fixed,
                    "cve": vuln.get("id", ""),
                    "detector": "pip-audit",
                })
        return findings

    # -- public API -------------------------------------------------------

    def scan(self, path: Path) -> tuple[list[dict[str, Any]], dict[str, bool]]:
        """Scan a project directory for dependency issues.

        Returns ``(findings, tools_used)``. Never raises; degrades to the
        built-in checks when pip-audit is unavailable.
        """
        findings: list[dict[str, Any]] = []
        path = Path(path)
        tools: dict[str, bool] = {}
        manifest_count = 0

        if path.is_file():
            roots = [path]
        else:
            roots = []
            for pattern in self.MANIFEST_GLOBS:
                for match in sorted(path.rglob(pattern)):
                    if not match.is_file():
                        continue
                    rel = match.relative_to(path)
                    if any(part in self._NOISE for part in rel.parts[:-1]):
                        continue
                    roots.append(match)
            # Dedup (pyproject.toml may match once; identical files once)
            roots = sorted({str(r): r for r in roots}.values())

        for manifest in roots:
            manifest_count += 1
            findings.extend(self.scan_manifest_file(manifest))
            findings.extend(self.scan_with_pip_audit(manifest))

        # Environment findings are only merged when the project declares no
        # manifests at all — otherwise scanning a project would report every
        # vulnerable package installed in the *running venv* (e.g. V4's own
        # transitive deps), which is noisy and wrong for a project scan.
        if manifest_count == 0:
            findings.extend(self.scan_environment())

        tools.update(self.tools)
        tools["pip-audit"] = tool_available("pip-audit")
        tools["builtin-deps"] = True
        tools["manifests"] = manifest_count > 0
        return findings, tools
