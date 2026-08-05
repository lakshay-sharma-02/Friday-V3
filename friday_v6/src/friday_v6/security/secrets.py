"""SecretDetector — find exposed credentials/keys in a project.

Wave 3 (Security & Quality). Two layers:

1. **Built-in (always available, pure stdlib):** walks the directory tree,
   skips VCS/venv/build noise and binary files, and matches a curated set
   of regex patterns for real credential formats (AWS keys, GitHub tokens,
   Slack tokens, Stripe/Google/OpenAI keys, private key blocks). Generic
   ``key = "..."`` assignments are gated by Shannon entropy to avoid
   flagging placeholder values.

2. **Optional subprocess:** if ``trufflehog`` (or legacy ``truffleHog``)
   is installed, run ``trufflehog filesystem --json`` on the target and
   merge verified findings. Degrades silently when absent.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from collections import Counter
from pathlib import Path

from .tooling import find_tool, tool_available

logger = logging.getLogger("friday_v6.security.secrets")


def _to_int(value) -> int:
    """Best-effort int conversion (0 on failure) — tool JSON is untrusted."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


#: Directories never scanned (VCS, deps, build artifacts, archived code,
#: and test suites — test fixtures deliberately contain FAKE credentials
#: to exercise the detectors, so self-scanning a repo's tests would report
#: the repo's own test data as leaked secrets).
_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", ".egg-info", "site-packages",
    ".idea", ".vscode", "target", ".next", "tests", "archive",
}

#: File extensions scanned (text-ish sources/configs).
_TEXT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".txt", ".md", ".rst", ".xml", ".html", ".htm", ".properties",
    ".lock", ".tf", ".gradle", ".cs", ".php", ".swift", ".kt", ".scala",
}

#: (name, regex, severity) — ordered; more specific patterns first.
#: The Generic pattern captures the assigned value (group 1) so it can be
#: entropy-gated (see ``_scan_file``).
_PATTERNS: list[tuple[str, str, str]] = [
    ("AWS Access Key ID", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b", "critical"),
    ("GitHub Personal Access Token", r"\bgh[pousr]_[0-9A-Za-z]{36,255}\b", "critical"),
    ("GitHub Fine-grained Token", r"\bgithub_pat_[0-9A-Za-z_]{20,}\b", "critical"),
    ("Stripe Secret Key", r"\bsk_live_[0-9a-zA-Z]{16,}\b", "critical"),
    ("OpenAI API Key", r"\bsk-[A-Za-z0-9]{20,}\b", "high"),
    ("Google API Key", r"\bAIza[0-9A-Za-z\-_]{35}\b", "high"),
    ("Slack Token", r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b", "high"),
    ("Private Key Block",
     r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
     "critical"),
    ("Generic Secret Assignment",
     r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret)\b"
     r"\s*[:=]\s*[\"']([A-Za-z0-9_\-./+]{8,})[\"']",
     "medium"),
]

#: Values that are obviously placeholders (case-insensitive substring check).
_PLACEHOLDERS = (
    "changeme", "change_me", "your_api_key", "your_token", "your_key",
    "your-secret", "example", "sample", "placeholder", "xxxx", "test",
    "dummy", "foobar", "lorem", "secret123", "password123", "123456",
)

#: Exact values that are WELL-KNOWN documented examples, not real secrets.
#: ``AKIAIOSFODNN7EXAMPLE`` is the canonical example key from AWS's own
#: documentation — flagging it is a false positive every time.
_EXAMPLE_VALUES = frozenset({
    "AKIAIOSFODNN7EXAMPLE",          # AWS docs example key id
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # AWS docs example secret
})

_MIN_ENTROPY = 3.0  # generic assignments below this entropy are skipped


def _shannon_entropy(value: str) -> float:
    """Shannon entropy in bits/char — higher = more random-looking."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(p in low for p in _PLACEHOLDERS)


class SecretDetector:
    """Find exposed credentials in a project directory."""

    detector_name = "secrets"

    def __init__(self, subprocess_timeout: float = 120.0,
                 max_file_size: int = 512 * 1024):
        self.subprocess_timeout = subprocess_timeout
        self.max_file_size = max_file_size
        self._compiled = [(name, re.compile(pat), sev)
                          for name, pat, sev in _PATTERNS]

    # -- built-in scanning ------------------------------------------------

    @staticmethod
    def _is_text(path: Path) -> bool:
        """Skip binaries via a null-byte sniff on the head of the file."""
        if path.suffix.lower() not in _TEXT_EXTS:
            return False
        try:
            with path.open("rb") as fh:
                head = fh.read(4096)
            return b"\x00" not in head
        except OSError:
            return False

    def _iter_files(self, root: Path):
        """Yield text files under root, skipping noise dirs and big files."""
        root = Path(root)
        if root.is_file():
            if self._is_text(root):
                yield root
            return
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
            if path.stat().st_size > self.max_file_size:
                continue
            if self._is_text(path):
                yield path

    def scan_path(self, root: Path) -> list[dict]:
        """Scan one file path (or whole tree) for secrets."""
        findings: list[dict] = []
        root = Path(root)
        if root.is_file():
            self._scan_file(root, findings)
            return findings
        for path in self._iter_files(root):
            try:
                self._scan_file(path, findings)
            except Exception as exc:
                logger.debug(f"Secret scan failed for {path}: {exc}")
        return findings

    def _scan_file(self, path: Path, findings: list[dict]) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line_no, line in enumerate(text.splitlines(), start=1):
            for name, regex, severity in self._compiled:
                for m in regex.finditer(line):
                    matched = m.group(0)
                    if name == "Generic Secret Assignment":
                        value = m.group(1)
                        if not value or _is_placeholder(value):
                            continue
                        if _shannon_entropy(value) < _MIN_ENTROPY:
                            continue
                    elif name == "Private Key Block":
                        # A bare BEGIN marker with no END is a truncated
                        # stub (docs, tests) — not an exposed key. Real
                        # PEM blocks always contain their END marker.
                        if "-----END" not in text:
                            continue
                    else:
                        if matched in _EXAMPLE_VALUES:
                            continue  # documented example, not a leak
                        # High-entropy guard for compact token formats.
                        if len(matched) < 12:
                            continue
                    findings.append({
                        "severity": severity,
                        "title": f"Exposed secret: {name}",
                        "detail": f"{name} detected. Revoke the credential and "
                                  "remove it from history; add a .gitignore / "
                                  "secret-scan to prevent recurrence.",
                        "file": str(path),
                        "line": line_no,
                        "snippet": matched[:80],
                        "detector": "builtin",
                    })
                    break  # one finding per line per secret type

    # -- optional trufflehog subprocess ------------------------------------

    def scan_with_trufflehog(self, root: Path) -> list[dict]:
        """Run trufflehog (filesystem mode) and parse its JSON stream."""
        binary = find_tool("trufflehog") or find_tool("truffleHog")
        if not binary:
            return []
        cmd = [binary, "filesystem", "--json", "--no-update", str(root)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=self.subprocess_timeout)
        except Exception as exc:
            logger.debug(f"trufflehog failed: {exc}")
            return []
        findings: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not item.get("Verified") and item.get("DetectorName") != "Private Key":
                continue
            meta = item.get("SourceMetadata", {}).get("Data", {})
            findings.append({
                "severity": "critical" if item.get("Verified") else "medium",
                "title": f"trufflehog: {item.get('DetectorName', 'secret')}",
                "detail": (item.get("VerificationError") or item.get("Raw")
                           or "Secret detected by trufflehog.")[:200],
                "file": meta.get("Filesystem", {}).get("file", ""),
                "line": _to_int(meta.get("Filesystem", {}).get("line")),
                "snippet": (item.get("Raw") or "")[:80],
                "detector": "trufflehog",
            })
        return findings

    # -- public API -------------------------------------------------------

    def scan(self, root: Path) -> tuple[list[dict], dict]:
        """Scan a directory; returns (findings, tools_used). Never raises."""
        root = Path(root)
        findings = self.scan_path(root)
        findings.extend(self.scan_with_trufflehog(root))
        tools = {
            "trufflehog": tool_available("trufflehog"),
            "builtin-secrets": True,
        }
        return findings, tools
