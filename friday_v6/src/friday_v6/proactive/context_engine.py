"""Deep Context Engine — understands what you're working on.

Combines multiple signals to build a rich understanding of the current
work context:
  - Desktop state: active window, open apps, workspace layout
  - Time context: time of day, day of week, session duration
  - Git activity: recent commits, dirty repos, branches
  - Session memory: what you were doing recently
  - File activity: recently modified files

This is FRIDAY's situational awareness — knowing not just what's on
your screen, but what it means and why it matters.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("friday_v6.proactive.context")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class WorkContext:
    """Rich understanding of the user's current work context."""
    # Desktop
    active_app: str = ""
    active_app_class: str = ""
    active_title: str = ""
    open_apps: list[str] = field(default_factory=list)
    workspace_count: int = 0
    current_workspace: int = 0

    # Time
    time_of_day: str = ""       # "morning", "afternoon", "evening", "night"
    day_of_week: str = ""       # "monday", "tuesday", etc.
    session_minutes: int = 0    # How long since first app opened
    session_count: int = 0      # Sessions today

    # Git
    active_repo: str = ""
    active_branch: str = ""
    dirty_repos: int = 0
    recent_commits_today: int = 0
    recent_commits_week: int = 0

    # Derived
    work_mode: str = ""         # "coding", "reviewing", "writing", "researching"
    focus_level: str = ""       # "deep_focus", "light", "away", "meeting"

    def describe(self) -> str:
        """Natural language description of the work context."""
        parts = []

        # What you're doing
        if self.active_app:
            parts.append(f"in {self.active_app}")
            if self.active_title:
                # Truncate long titles
                short = self.active_title[:50]
                parts.append(f"on '{short}'")

        # Time context
        if self.time_of_day:
            parts.append(f"({self.time_of_day})")

        # Session
        if self.session_minutes > 0:
            parts.append(f"{self.session_minutes} min session")

        # Work mode
        if self.work_mode:
            parts.append(f"mode: {self.work_mode}")

        return " ".join(parts)

    def to_dict(self) -> dict:
        """Serialize to dict for storage."""
        return {
            "active_app": self.active_app,
            "active_app_class": self.active_app_class,
            "active_title": self.active_title,
            "open_apps": self.open_apps,
            "workspace_count": self.workspace_count,
            "current_workspace": self.current_workspace,
            "time_of_day": self.time_of_day,
            "day_of_week": self.day_of_week,
            "session_minutes": self.session_minutes,
            "work_mode": self.work_mode,
            "focus_level": self.focus_level,
        }


# ---------------------------------------------------------------------------
# Deep Context Engine
# ---------------------------------------------------------------------------


class DeepContextEngine:
    """Builds a rich understanding of the user's current work context.

    Usage:
        engine = DeepContextEngine()
        context = engine.get_context()
        print(context.describe())
    """

    def __init__(self, session_store=None):
        self._session_store = session_store
        self._session_start = datetime.now(timezone.utc)
        self._wm = None  # WindowManager — lazy loaded
        # pid → (cached_at, repo_name, branch); avoids spawning git
        # subprocesses on every observer cycle for the same window.
        self._repo_cache: dict[int, tuple[float, str, str]] = {}

    @property
    def _window_manager(self):
        """Lazy-loaded WindowManager."""
        if self._wm is None:
            try:
                from ..desktop.wm_abstraction import WindowManager
                self._wm = WindowManager()
            except ImportError:
                self._wm = None
        return self._wm

    def get_context(self) -> WorkContext:
        """Get the current work context by combining all signals."""
        ctx = WorkContext()

        # 1. Desktop state
        self._enrich_desktop(ctx)

        # 2. Time context
        self._enrich_time(ctx)

        # 3. Git activity
        self._enrich_git(ctx)

        # 4. Session memory
        self._enrich_session(ctx)

        # 5. Derive work mode and focus
        self._derive_work_mode(ctx)

        return ctx

    def get_active_app(self) -> tuple[str, str]:
        """Cheap desktop-only probe: (app_class, title) of the active window.

        Unlike get_context(), this never spawns git subprocesses or walks
        sessions, so the background PatternLearner observer can call it
        every few seconds without paying for git queries.
        """
        wm = self._window_manager
        if not wm or not wm.is_available:
            return ("", "")
        try:
            active = wm.get_active_window()
            if active:
                return (active.app_class or "", active.title or "")
        except Exception as exc:
            logger.debug(f"Active window probe failed: {exc}")
        return ("", "")

    def get_active_repo(self, ttl_seconds: float = 60.0) -> tuple[str, str]:
        """Resolve (repo_name, branch) for the active window's process.

        Uses the active window's PID to find its working directory (via
        ``/proc/<pid>/cwd`` on Linux) and asks git for the repo there — so
        patterns tie to the project the user is *actually in*, not the
        daemon's own CWD. Falls back to ``os.getcwd()`` when the PID is
        unavailable, and returns ("", "") when there's no desktop or repo.

        Cached per-PID for ``ttl_seconds`` so the background observer
        doesn't re-run git subprocesses on every cycle.
        """
        wm = self._window_manager
        if not wm or not wm.is_available:
            return ("", "")
        try:
            active = wm.get_active_window()
            if not active:
                return ("", "")
            pid = int(getattr(active, "pid", 0) or 0)
            now = time.time()
            cached = self._repo_cache.get(pid)
            if cached and now - cached[0] < ttl_seconds:
                return cached[1], cached[2]
            cwd = self._cwd_for_window(pid)
            repo, branch = self._resolve_repo(cwd)
            self._repo_cache[pid] = (now, repo, branch)
            return repo, branch
        except Exception as exc:
            logger.debug(f"Active repo probe failed: {exc}")
            return ("", "")

    @staticmethod
    def _cwd_for_window(pid: int) -> str:
        """Best-effort working directory of a window's process."""
        if pid > 0 and platform.system() == "Linux":
            try:
                return os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
        return os.getcwd()

    @staticmethod
    def _resolve_repo(cwd: str) -> tuple[str, str]:
        """Return (repo_name, branch) for a directory, or ('', '')."""
        try:
            top = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=3,
            )
            if top.returncode != 0:
                return ("", "")
            repo_name = Path(top.stdout.strip()).name
            branch = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
            branch_name = branch.stdout.strip() if branch.returncode == 0 else ""
            return repo_name, branch_name
        except Exception as exc:
            logger.debug(f"Repo resolution failed for {cwd}: {exc}")
            return ("", "")

    def _enrich_desktop(self, ctx: WorkContext):
        """Add desktop state: active window, open apps, workspaces."""
        wm = self._window_manager
        if not wm or not wm.is_available:
            return

        try:
            active = wm.get_active_window()
            if active:
                ctx.active_app = active.app_name
                ctx.active_app_class = active.app_class
                ctx.active_title = active.title or ""

            windows = wm.list_windows()
            ctx.open_apps = list(set(w.app_name for w in windows if w.app_name))

            workspaces = wm.list_workspaces()
            ctx.workspace_count = len(workspaces)

            active_ws = wm.get_active_workspace()
            if active_ws:
                ctx.current_workspace = active_ws.id
        except Exception as exc:
            logger.debug(f"Desktop enrichment failed: {exc}")

    def _enrich_time(self, ctx: WorkContext):
        """Add time context: time of day, day of week, session duration."""
        now = datetime.now()

        hour = now.hour
        if 5 <= hour < 12:
            ctx.time_of_day = "morning"
        elif 12 <= hour < 17:
            ctx.time_of_day = "afternoon"
        elif 17 <= hour < 21:
            ctx.time_of_day = "evening"
        else:
            ctx.time_of_day = "night"

        ctx.day_of_week = now.strftime("%A").lower()

        # Session duration. ``_session_start`` is stored as aware UTC
        # (``datetime.now(timezone.utc)`` at engine construction), so the
        # elapsed time must be computed against *aware UTC now* — naively
        # labelling local wall-clock as UTC (``now.replace(tzinfo=utc)``)
        # added the whole timezone offset as fake session minutes (a fresh
        # engine looked like a 5.5-hour session on UTC+5:30 machines),
        # which made the proactive git suggestions fire unconditionally.
        if self._session_start:
            elapsed = datetime.now(timezone.utc) - self._session_start
            ctx.session_minutes = int(elapsed.total_seconds() / 60)

    def _enrich_git(self, ctx: WorkContext):
        """Add git context: active repo, branch, dirty status, commits."""
        try:
            # Check if we're in a git repo
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=3,
                cwd=os.getcwd(),
            )
            if result.returncode == 0:
                repo_path = result.stdout.strip()
                ctx.active_repo = Path(repo_path).name

                # Get current branch
                branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=3,
                    cwd=repo_path,
                )
                if branch.returncode == 0:
                    ctx.active_branch = branch.stdout.strip()

                # Check dirty status
                dirty = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True, text=True, timeout=3,
                    cwd=repo_path,
                )
                if dirty.returncode == 0:
                    ctx.dirty_repos = 1 if dirty.stdout.strip() else 0

                # Recent commits today
                today = datetime.now().strftime("%Y-%m-%d")
                commits_today = subprocess.run(
                    ["git", "log", f"--after={today}T00:00:00", "--oneline"],
                    capture_output=True, text=True, timeout=3,
                    cwd=repo_path,
                )
                if commits_today.returncode == 0:
                    ctx.recent_commits_today = len(
                        [ln for ln in commits_today.stdout.split("\n") if ln.strip()]
                    )

                # Recent commits this week
                from datetime import timedelta
                week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                commits_week = subprocess.run(
                    ["git", "log", f"--after={week_ago}T00:00:00", "--oneline"],
                    capture_output=True, text=True, timeout=3,
                    cwd=repo_path,
                )
                if commits_week.returncode == 0:
                    ctx.recent_commits_week = len(
                        [ln for ln in commits_week.stdout.split("\n") if ln.strip()]
                    )

        except Exception as exc:
            logger.debug(f"Git enrichment failed: {exc}")

    def _enrich_session(self, ctx: WorkContext):
        """Add session context from session store."""
        if self._session_store:
            stats = self._session_store.get_today_stats()
            ctx.session_count = stats.get("session_count", 0)

    def _derive_work_mode(self, ctx: WorkContext):
        """Derive work mode and focus level from combined signals."""
        app = ctx.active_app_class.lower()

        # Work mode
        if any(editor in app for editor in ["code", "kitty", "vim", "nvim",
                                              "sublime", "jetbrains", "zcode"]):
            ctx.work_mode = "coding"
        elif any(browser in app for browser in ["firefox", "chromium", "brave",
                                                  "google-chrome", "zen"]):
            # Could be researching or browsing — check title
            title_lower = ctx.active_title.lower()
            if any(doc in title_lower for doc in ["docs", "documentation",
                                                    "mdn", "stackoverflow", "github"]):
                ctx.work_mode = "researching"
            elif any(code in title_lower for code in ["pr", "pull request",
                                                       "review", "diff"]):
                ctx.work_mode = "reviewing"
            else:
                ctx.work_mode = "browsing"
        elif any(chat in app for chat in ["slack", "discord", "telegram"]):
            ctx.work_mode = "communicating"
        elif any(note in app for note in ["obsidian", "logseq", "notion"]):
            ctx.work_mode = "writing"
        else:
            ctx.work_mode = "idle"

        # Focus level
        if ctx.session_minutes > 0:
            # Deep focus: coding for 30+ min without switching apps
            if ctx.work_mode == "coding" and ctx.session_minutes > 30:
                ctx.focus_level = "deep_focus"
            elif ctx.session_minutes > 15:
                ctx.focus_level = "light"
            else:
                ctx.focus_level = "active"
        else:
            ctx.focus_level = "idle"

    def get_context_summary(self) -> str:
        """Get a brief spoken summary of the current context."""
        ctx = self.get_context()

        if not ctx.active_app:
            return "I can't see your desktop right now."

        parts = []
        parts.append(f"You're in {ctx.active_app}")

        if ctx.active_repo:
            parts.append(f"working on {ctx.active_repo}")
            if ctx.active_branch:
                parts.append(f"on branch {ctx.active_branch}")

        if ctx.dirty_repos > 0:
            parts.append("with uncommitted changes")

        parts.append(f"({ctx.session_minutes} min session)")

        return ". ".join(parts) + "."

    def get_proactive_suggestions(self) -> list[str]:
        """Get contextually relevant suggestions based on current state."""
        ctx = self.get_context()
        suggestions = []

        # Git suggestions
        if ctx.dirty_repos > 0 and ctx.session_minutes > 20:
            suggestions.append(
                "You have uncommitted changes. Would you like me to review them?"
            )

        # Coding suggestions
        if ctx.work_mode == "coding" and ctx.session_minutes > 45:
            suggestions.append(
                "You've been coding for a while. Tests running in the background?"
            )

        # Commit frequency patterns
        if ctx.recent_commits_week > 0 and ctx.dirty_repos > 0:
            suggestions.append(
                "I noticed you commit frequently. Ready to stage and commit?"
            )

        # Time-based suggestions
        if ctx.time_of_day == "morning" and ctx.session_minutes < 5:
            suggestions.append(
                "Good morning! I'm caught up on everything. "
                "Would you like a briefing on what changed since yesterday?"
            )

        return suggestions

    def cleanup(self):
        """Release resources."""
        self._wm = None
        self._repo_cache = {}
