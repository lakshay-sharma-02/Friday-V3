"""TerminalObserver (Milestone 7.3).

A NEW observer for the frozen Observation Engine. It converts terminal
engineering activity into deterministic engineering observations that plug into
the existing engine — no engine, context, or brain changes.

DESIGN (privacy-first, no capture):
  This observer is a PURE READER. It does not watch the shell, attach a PTY,
  hook readline, parse shell history, or run a daemon. It reads a JSONL
  *engineering-command activity log* at a known path. Each line is a pre-sanitized
  engineering event written by some external mechanism OUTSIDE Friday:

      {"ts": "<ISO>", "tool": "pytest", "repo": "Friday",
       "wd": "/abs/path", "exit": 1, "duration_s": 4.7}

  Only the whitelisted metadata fields above are ever read. Command arguments,
  environment variables, secrets, stdout/stderr, and interactive input are
  NEVER read and NEVER emitted. The observer structurally cannot leak them: it
  maps only `ts/tool/repo/wd/exit/duration_s` to observations and ignores
  everything else.

Observations emitted per event:
  tool            (Observed)   the binary name, not its arguments
  tool_category   (Derived)    Build/Test/Version Control/...
  exit_status     (Observed)   process exit code
  success         (Derived)    exit == 0
  duration_s      (Observed)   execution seconds

Run-level engineering signals (evidence-backed, inferred where judgment is
involved):
  repeated_test_failures / repeated_build_failures  (Inferred, cause)
  long_running_build                                (Inferred, cause)
  repo_switch / tool_switch                          (Derived)

Categorization is a frozen, deterministic table — no LLM.

Confidence follows the Observation Engine vocabulary (Observed/Derived/Inferred).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation

# Execution longer than this (seconds) is flagged as a long-running build.
LONG_BUILD_S = 60.0
# At least this many failed runs of the same (repo, category) to infer "repeated".
REPEATED_FAILURE_THRESHOLD = 2


class ToolCategory(str):
    """Frozen engineering tool categories (deterministic, no LLM)."""


CATEGORIES = {
    # Build
    "cargo": "Build", "cmake": "Build", "make": "Build", "bazel": "Build",
    "gradle": "Build", "mvn": "Build", "go": "Build", "rustc": "Build",
    "gcc": "Build", "g++": "Build", "clang": "Build", "javac": "Build",
    "msbuild": "Build", "ninja": "Build", "mix": "Build",
    # Test
    "pytest": "Test", "tox": "Test", "jest": "Test", "vitest": "Test",
    "mocha": "Test", "rspec": "Test", "unittest": "Test", "ctest": "Test",
    "cargo-test": "Test", "go-test": "Test", "dotnet-test": "Test",
    # Version Control
    "git": "Version Control", "hg": "Version Control", "svn": "Version Control",
    "fossil": "Version Control",
    # Formatting
    "black": "Formatting", "isort": "Formatting", "ruff-format": "Formatting",
    "prettier": "Formatting", "gofmt": "Formatting", "clang-format": "Formatting",
    "stylua": "Formatting",
    # Linting
    "ruff": "Linting", "flake8": "Linting", "pylint": "Linting",
    "eslint": "Linting", "clang-tidy": "Linting", "shellcheck": "Linting",
    "hadolint": "Linting",
    # Type Checking
    "mypy": "Type Checking", "pyright": "Type Checking", "tsc": "Type Checking",
    "flow": "Type Checking", "ocamlc": "Type Checking",
    # Documentation
    "sphinx": "Documentation", "mkdocs": "Documentation", "doxygen": "Documentation",
    "pdoc": "Documentation", "typst": "Documentation",
    # Dependency Management
    "uv": "Dependency Management", "pip": "Dependency Management",
    "poetry": "Dependency Management", "conda": "Dependency Management",
    "npm": "Dependency Management", "pnpm": "Dependency Management",
    "yarn": "Dependency Management", "cargo-add": "Dependency Management",
    "bundler": "Dependency Management", "mix-deps": "Dependency Management",
    # Package Management
    "npm-publish": "Package Management", "twine": "Package Management",
    "cargo-publish": "Package Management", "podman-build": "Package Management",
    # Deployment
    "kubectl": "Deployment", "helm": "Deployment", "terraform": "Deployment",
    "ansible": "Deployment", "serverless": "Deployment",
    # Container
    "docker": "Container", "podman": "Container", "buildah": "Container",
    "nerdctl": "Container",
    # Database
    "psql": "Database", "mysql": "Database", "sqlite3": "Database",
    "mongo": "Database", "redis-cli": "Database", "psql": "Database",
    # Search
    "grep": "Search", "rg": "Search", "ag": "Search", "fd": "Search",
    "find": "Search", "awk": "Search",
    # Benchmark
    "hyperfine": "Benchmark", "criterion": "Benchmark", "pytest-bench": "Benchmark",
    # Generic interpreters (category by primary use: script execution)
    "python": "Build", "node": "Build", "ruby": "Build", "lua": "Build",
    "perl": "Build", "php": "Build", "java": "Build",
}


def categorize(tool: str) -> str:
    """Deterministic tool → category. Unknown tools map to 'Unknown'."""
    return CATEGORIES.get((tool or "").strip().lower(), "Unknown")


def _default_log_path() -> Path:
    override = os.environ.get("FRIDAY_TERMINAL_LOG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".friday" / "terminal_activity.jsonl"


def _safe_parse(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


class TerminalObserver(Observer):
    name = "terminal"

    def __init__(self, log_path: Optional[Path] = None) -> None:
        # Log path is the ONLY input; the observer never creates or writes it.
        self.log_path = log_path or _default_log_path()

    # --- Observer interface --------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        p = self.log_path
        if p.exists():
            if p.is_dir():
                return ObserverHealth(
                    False, Health.DOWN, "log path is a directory",
                    f"{p} is a directory, not a JSONL log.")
            return ObserverHealth(True, Health.HEALTHY, "log readable",
                                  f"log: {p}")
        # No log yet is healthy: there is simply no activity to observe.
        return ObserverHealth(True, Health.HEALTHY, "no log yet",
                              f"no activity log at {p} (none recorded).")

    def collect(self, conn) -> list[Observation]:
        """Read the activity log and emit engineering observations.

        Pure reader: only whitelisted metadata fields are mapped to facts.
        Returns [] when the log is absent or empty.
        """
        events = self._read_events()
        observations: list[Observation] = []
        prev_repo: Optional[str] = None
        prev_tool: Optional[str] = None
        failures: dict[tuple[str, str], int] = {}
        max_build_dur: float = 0.0

        for ev in events:
            obs = self._event_observations(ev)
            if not obs:
                continue
            observations.extend(obs)
            repo = ev.get("repo")
            tool = (ev.get("tool") or "").strip().lower()
            cat = categorize(tool)
            # Track run-level signals.
            exit_v = ev.get("exit")
            if isinstance(exit_v, int) and exit_v != 0 and cat in ("Test", "Build"):
                failures[(repo or "terminal", cat)] = \
                    failures.get((repo or "terminal", cat), 0) + 1
            dur = ev.get("duration_s")
            if isinstance(dur, (int, float)) and cat == "Build":
                max_build_dur = max(max_build_dur, float(dur))
            if prev_repo is not None and repo is not None and repo != prev_repo:
                observations.append(self._obs(
                    ev, repo, "repo_switch", f"{prev_repo} -> {repo}",
                    Confidence.DERIVED,
                    cause=f"terminal activity moved from {prev_repo} to {repo}."))
            if prev_tool is not None and tool and tool != prev_tool:
                observations.append(self._obs(
                    ev, repo or "terminal", "tool_switch", f"{prev_tool} -> {tool}",
                    Confidence.DERIVED,
                    cause=f"tool changed from {prev_tool} to {tool}."))
            if repo:
                prev_repo = repo
            if tool:
                prev_tool = tool

        # Run-level inferred signals.
        for (repo, cat), count in failures.items():
            if count >= REPEATED_FAILURE_THRESHOLD:
                kind = "repeated_test_failures" if cat == "Test" \
                    else "repeated_build_failures"
                observations.append(self._obs_timestamped(
                    ev_ts=events[-1].get("ts") if events else None,
                    subject=repo, aspect=kind, value="true",
                    confidence=Confidence.INFERRED,
                    cause=f"{count} {cat.lower()} command(s) failed in this window."))
        if max_build_dur >= LONG_BUILD_S:
            observations.append(self._obs_timestamped(
                ev_ts=events[-1].get("ts") if events else None,
                subject="terminal", aspect="long_running_build",
                value=f"{max_build_dur:.1f}s",
                confidence=Confidence.INFERRED,
                cause=f"a build ran for {max_build_dur:.1f}s (>= {LONG_BUILD_S:.0f}s)."))

        return observations

    def summarize(self, conn) -> str:
        events = self._read_events()
        if not events:
            return ("Terminal Observer\n"
                    "Healthy\n"
                    "Observed\n"
                    "0 engineering commands\n"
                    "Repositories: (none)\n"
                    "Top tools: (none)\n"
                    "Failures: 0\n"
                    "Success rate: n/a")
        repos: list[str] = []
        tools: dict[str, int] = {}
        failures = 0
        successes = 0
        for ev in events:
            repo = ev.get("repo")
            if repo and repo not in repos:
                repos.append(repo)
            tool = (ev.get("tool") or "").strip().lower()
            if tool:
                tools[tool] = tools.get(tool, 0) + 1
            exit_v = ev.get("exit")
            if isinstance(exit_v, int):
                if exit_v == 0:
                    successes += 1
                else:
                    failures += 1
        total = successes + failures
        rate = f"{(successes / total * 100):.0f}%" if total else "n/a"
        top = ", ".join(t for t, _ in sorted(tools.items(),
                                             key=lambda kv: kv[1], reverse=True)[:3])
        return ("Terminal Observer\n"
                "Healthy\n"
                "Observed\n"
                f"{len(events)} engineering commands\n"
                f"Repositories:\n" + "".join(f"  {r}\n" for r in repos) +
                f"Top tools: {top or '(none)'}\n"
                f"Failures: {failures}\n"
                f"Success rate: {rate}")

    # --- internals ----------------------------------------------------------

    def _read_events(self) -> list[dict]:
        p = self.log_path
        if not p.exists() or p.is_dir():
            return []
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        events: list[dict] = []
        for line in text.splitlines():
            obj = _safe_parse(line)
            if obj and obj.get("tool"):
                events.append(obj)
        return events

    def _obs(self, ev: dict, subject: str, aspect: str, value: str,
             confidence: Confidence, cause: Optional[str] = None) -> Observation:
        ts = ev.get("ts") or _now()
        scope = ev.get("wd") or ""
        return Observation(
            source=self.name, subject=subject or "terminal", aspect=aspect,
            value=value, confidence=confidence, observed_at=ts, scope=scope,
            cause=cause,
        )

    def _obs_timestamped(self, ev_ts: Optional[str], subject: str, aspect: str,
                         value: str, confidence: Confidence,
                         cause: Optional[str] = None) -> Observation:
        return Observation(
            source=self.name, subject=subject or "terminal", aspect=aspect,
            value=value, confidence=confidence, observed_at=ev_ts or _now(),
            cause=cause,
        )

    def _event_observations(self, ev: dict) -> list[Observation]:
        tool = (ev.get("tool") or "").strip().lower()
        if not tool:
            return []
        repo = ev.get("repo") or "terminal"
        cat = categorize(tool)
        out = [
            self._obs(ev, repo, "tool", tool, Confidence.OBSERVED),
            self._obs(ev, repo, "tool_category", cat, Confidence.DERIVED),
        ]
        exit_v = ev.get("exit")
        if isinstance(exit_v, int):
            out.append(self._obs(ev, repo, "exit_status", str(exit_v),
                                 Confidence.OBSERVED))
            out.append(self._obs(
                ev, repo, "success",
                "true" if exit_v == 0 else "false", Confidence.DERIVED,
                cause=f"exit code {exit_v}."))
        dur = ev.get("duration_s")
        if isinstance(dur, (int, float)):
            out.append(self._obs(ev, repo, "duration_s", f"{float(dur):.1f}",
                                 Confidence.OBSERVED))
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
