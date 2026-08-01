"""Pattern Learner — learns your workflow patterns by watching.

FRIDAY learns repeated behavior sequences by observing what you do and
building frequency distributions. No ML, no neural networks — just
statistical pattern matching on action pairs.

What it learns:
  - Action pairs: "After editing main.py, you open test_main.py 80% of the time"
  - Timing patterns: "You always run tests before pushing"
  - App sequences: "You open browser after terminal 60% of the time"
  - Session patterns: "You work deepest in the morning on Mondays"

All data is stored in ~/.friday/patterns/ as JSON files.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.proactive.patterns")

_PATTERNS_DIR = Path.home() / ".friday" / "patterns"
_ACTION_PAIRS_FILE = _PATTERNS_DIR / "action_pairs.json"
_APP_SEQUENCES_FILE = _PATTERNS_DIR / "app_sequences.json"
_TIMING_PATTERNS_FILE = _PATTERNS_DIR / "timing_patterns.json"


# ---------------------------------------------------------------------------
# Pattern Learner
# ---------------------------------------------------------------------------


class PatternLearner:
    """Learns and recalls behavioral patterns from observed actions.

    Tracks three types of patterns:
      1. Action pairs: (context → action) probabilities
      2. App sequences: (app A → app B) transition probabilities
      3. Timing patterns: when certain actions typically occur

    Usage:
        learner = PatternLearner()
        learner.observe_action("edit_file", {"file": "main.py"})
        learner.observe_app_transition("kitty", "firefox")

        suggestions = learner.get_suggestions(
            context={"active_app": "kitty", "file": "main.py"}
        )
    """

    def __init__(self):
        self._lock = threading.Lock()
        _PATTERNS_DIR.mkdir(parents=True, exist_ok=True)

        self._action_pairs: dict = self._load_json(_ACTION_PAIRS_FILE)
        self._app_sequences: dict = self._load_json(_APP_SEQUENCES_FILE)
        self._timing_patterns: dict = self._load_json(_TIMING_PATTERNS_FILE)
        self._recent_actions: list[dict] = []  # Last 50 actions
        # Per-project recent-action buffers, so pairs can be scored *within*
        # a repo ("in repo X you usually run tests after editing").
        self._recent_actions_by_repo: dict[str, list[dict]] = {}
        self._last_app: Optional[str] = None

    def _load_json(self, path: Path) -> dict:
        """Load JSON data from a file, returning empty dict on failure."""
        try:
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug(f"Could not load {path}: {exc}")
        return {}

    def _save_json(self, path: Path, data: dict):
        """Save data to a JSON file."""
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as exc:
            logger.warning(f"Could not save {path}: {exc}")

    @staticmethod
    def _learn_pair(root: dict, from_action: str, to_action: str) -> None:
        """Increment the (from_action → to_action) pair count in a table root.

        ``root`` is either the global table or a repo-scoped sub-table; both
        use the same ``{"pairs": ..., "totals": ...}`` shape.
        """
        pair_key = f"{from_action}→{to_action}"
        pairs = root.setdefault("pairs", {})
        pairs[pair_key] = pairs.get(pair_key, 0) + 1
        totals = root.setdefault("totals", {})
        totals[from_action] = totals.get(from_action, 0) + 1

    def _pattern_root(self, table: dict, repo: Optional[str]) -> dict:
        """Pick the repo-scoped sub-table when it has signal, else global."""
        if repo:
            repo_root = table.get("repos", {}).get(repo)
            if repo_root and repo_root.get("totals", {}):
                return repo_root
        return table

    # ── Action Pairs ──────────────────────────────────────────────────

    def observe_action(self, action_type: str, context: Optional[dict] = None):
        """Observe an action and learn from it.

        Builds frequency distributions: given a certain context,
        what action is likely to follow?

        When the context includes a ``repo``, the pair is also learned in a
        repo-scoped table so suggestions can tie patterns to specific
        projects ("in repo X you usually run tests after editing").

        Args:
            action_type: The action type (e.g., "edit_file", "run_tests")
            context: Optional context dict with keys like "app", "file", "repo"
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            context = context or {}
            repo = context.get("repo") or ""

            observation = {
                "action": action_type,
                "context": context,
                "timestamp": now.isoformat(),
            }

            # Store the recent action
            self._recent_actions.append(observation)
            if len(self._recent_actions) > 50:
                self._recent_actions.pop(0)

            learned = False

            # Learn action pairs: if we have a previous action, learn the
            # (previous → current) transition
            if len(self._recent_actions) >= 2:
                prev = self._recent_actions[-2]
                self._learn_pair(self._action_pairs, prev["action"], action_type)
                learned = True

            # Repo-scoped learning: pair the action with the previous action
            # observed *in the same repo* (per-project recent buffer).
            if repo:
                recent_repo = self._recent_actions_by_repo.setdefault(repo, [])
                recent_repo.append(observation)
                if len(recent_repo) > 50:
                    recent_repo.pop(0)
                if len(recent_repo) >= 2:
                    prev = recent_repo[-2]
                    repo_root = self._action_pairs.setdefault(
                        "repos", {}).setdefault(repo, {})
                    self._learn_pair(repo_root, prev["action"], action_type)
                    learned = True

            if learned:
                self._save_json(_ACTION_PAIRS_FILE, self._action_pairs)

    def get_action_probability(self, from_action: str, to_action: str,
                               repo: Optional[str] = None) -> float:
        """Get the probability of `to_action` following `from_action`.

        Prefers the repo-scoped table when ``repo`` has learned signal;
        otherwise falls back to the global distribution.

        Returns a value between 0.0 and 1.0.
        """
        root = self._pattern_root(self._action_pairs, repo)
        pairs = root.get("pairs", {})
        totals = root.get("totals", {})

        if totals.get(from_action, 0) == 0:
            if repo:
                return self.get_action_probability(
                    from_action, to_action, repo=None)
            return 0.0

        pair_key = f"{from_action}→{to_action}"
        count = pairs.get(pair_key, 0)
        total = totals.get(from_action, 0)

        return count / total

    def get_most_likely_next_action(self, from_action: str,
                                    repo: Optional[str] = None) -> Optional[str]:
        """Get the most likely action to follow from_action.

        When ``repo`` is given and has enough signal for ``from_action``,
        prediction is repo-scoped; otherwise it falls back to the global
        distribution.
        """
        root = self._pattern_root(self._action_pairs, repo)
        pairs = root.get("pairs", {})
        totals = root.get("totals", {})

        if totals.get(from_action, 0) == 0:
            if repo:
                return self.get_most_likely_next_action(from_action, repo=None)
            return None

        prefix = f"{from_action}→"
        candidates = []
        for key, count in pairs.items():
            if key.startswith(prefix):
                to_action = key[len(prefix):]
                prob = count / totals.get(from_action, 1)
                if prob > 0.2:  # Minimum threshold
                    candidates.append((prob, to_action))

        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def get_last_action(self) -> Optional[str]:
        """Get the most recently observed action type, or None if none yet.

        Feeds the AnticipationEngine so pattern-based suggestions use the
        user's *real* last action instead of a hardcoded placeholder.
        """
        with self._lock:
            if not self._recent_actions:
                return None
            return self._recent_actions[-1].get("action")

    # ── App Sequences ─────────────────────────────────────────────────

    def observe_app_transition(self, from_app: str, to_app: str,
                               repo: Optional[str] = None):
        """Observe a transition from one app to another.

        Builds a transition matrix: {(app A → app B): count}. When ``repo``
        is given, the transition is also learned repo-scoped so app-switch
        suggestions can be tied to the project being worked on.
        """
        with self._lock:
            transitions = self._app_sequences.setdefault("transitions", {})

            # Key: "appA→appB"
            key = f"{from_app.lower()}→{to_app.lower()}"
            transitions[key] = transitions.get(key, 0) + 1

            # Track total transitions from each app
            totals = self._app_sequences.setdefault("totals", {})
            totals[from_app.lower()] = totals.get(from_app.lower(), 0) + 1

            if repo:
                repo_root = self._app_sequences.setdefault(
                    "repos", {}).setdefault(repo, {})
                r_transitions = repo_root.setdefault("transitions", {})
                r_transitions[key] = r_transitions.get(key, 0) + 1
                r_totals = repo_root.setdefault("totals", {})
                r_totals[from_app.lower()] = r_totals.get(from_app.lower(), 0) + 1

            self._save_json(_APP_SEQUENCES_FILE, self._app_sequences)

    def get_most_likely_next_app(self, current_app: str,
                                 repo: Optional[str] = None) -> Optional[str]:
        """Given the current app, what app is the user likely to switch to?

        Prefers the repo-scoped transition table when ``repo`` has signal;
        otherwise falls back to the global distribution.
        """
        root = self._pattern_root(self._app_sequences, repo)
        transitions = root.get("transitions", {})
        totals = root.get("totals", {})

        if totals.get(current_app.lower(), 0) == 0:
            if repo:
                return self.get_most_likely_next_app(current_app, repo=None)
            return None

        prefix = f"{current_app.lower()}→"

        candidates = []
        for key, count in transitions.items():
            if key.startswith(prefix):
                next_app = key[len(prefix):]
                total = totals.get(current_app.lower(), 1)
                prob = count / total
                if prob > 0.15:
                    candidates.append((prob, next_app))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        return candidates[0][1]

    # ── Timing Patterns ───────────────────────────────────────────────

    def observe_timing(self, action_type: str):
        """Observe when an action typically occurs.

        Builds hourly distributions per action type.
        """
        with self._lock:
            now = datetime.now()
            hour = now.hour
            day = now.strftime("%A").lower()

            hourly = self._timing_patterns.setdefault("hourly", {})
            hourly.setdefault(action_type, {})
            hourly[action_type][str(hour)] = hourly[action_type].get(str(hour), 0) + 1

            daily = self._timing_patterns.setdefault("daily", {})
            # Use lists for JSON compatibility (JSON has no set type)
            days_list = daily.setdefault(action_type, [])
            if day not in days_list:
                days_list.append(day)

            self._save_json(_TIMING_PATTERNS_FILE, self._timing_patterns)

    def get_peak_hour(self, action_type: str) -> Optional[int]:
        """Get the hour when this action is most commonly performed."""
        hourly = self._timing_patterns.get("hourly", {})
        hours = hourly.get(action_type, {})
        if not hours:
            return None

        peak_hour = max(hours, key=hours.get)  # type: ignore
        return int(peak_hour)

    def get_days_for_action(self, action_type: str) -> list[str]:
        """Get the days of week this action is performed on."""
        daily = self._timing_patterns.get("daily", {})
        return list(daily.get(action_type, set()))

    # ── Suggestion Generation ─────────────────────────────────────────

    def get_suggestions(self, context: Optional[dict] = None) -> list[str]:
        """Generate proactive suggestions based on learned patterns.

        Args:
            context: Current context dict (active_app, recent_actions, etc.)

        Returns:
            List of natural language suggestions.
        """
        suggestions = []

        if context is None:
            context = {}

        current_app = context.get("active_app_class", "").lower()
        last_action = context.get("last_action", "")
        repo = context.get("active_repo") or ""

        # 1. Predict next app (repo-scoped when the project is known)
        if current_app:
            next_app = self.get_most_likely_next_app(current_app, repo=repo or None)
            if next_app and current_app.lower() != next_app.lower():
                if repo:
                    suggestions.append(
                        f"In {repo}, whenever you're in {current_app.title()}, "
                        f"you often switch to {next_app.title()}. "
                        f"Open it for you?"
                    )
                else:
                    suggestions.append(
                        f"Whenever you're in {current_app.title()}, "
                        f"you often switch to {next_app.title()}. "
                        f"Open it for you?"
                    )

        # 2. Predict next action (repo-scoped when the project is known)
        if last_action:
            next_action = self.get_most_likely_next_action(
                last_action, repo=repo or None)
            if next_action:
                action_labels = {
                    "edit_file": "Run tests",
                    "run_tests": "Check test report",
                    "git_commit": "Push to remote",
                    "git_push": "Check CI status",
                    "open_pr": "Review PR diff",
                    "review_pr": "Merge PR",
                    "coding": "Keep coding",
                    "browsing": "Browse the web",
                    "communicating": "Check your messages",
                    "writing": "Write",
                    "focus": "Stay focused",
                }
                label = action_labels.get(next_action, next_action.replace("_", " ").title())
                repo_part = f" in {repo}" if repo else ""
                suggestions.append(
                    f"After {last_action.replace('_', ' ')}{repo_part}, "
                    f"you usually {label.lower()}. Want me to? "
                )

        # 3. Timing-based suggestions
        if last_action:
            peak = self.get_peak_hour(last_action)
            if peak is not None:
                current_hour = datetime.now().hour
                # Suggest at the peak hour when the user is most active
                if abs(current_hour - peak) <= 1:
                    # Already happening at peak time — no need to suggest
                    pass

        return suggestions[:3]  # Max 3 suggestions

    # ── Stats ─────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Get summary statistics about learned patterns."""
        pairs = self._action_pairs.get("pairs", {})
        totals = self._action_pairs.get("totals", {})
        transitions = self._app_sequences.get("transitions", {})
        hourly = self._timing_patterns.get("hourly", {})

        # Find the strongest pattern
        strongest_pair = max(pairs, key=pairs.get) if pairs else "none"
        strongest_transition = max(transitions, key=transitions.get) if transitions else "none"

        return {
            "action_pairs_learned": len(pairs),
            "total_actions_observed": sum(totals.values()),
            "app_transitions_learned": len(transitions),
            "timing_patterns_tracked": len(hourly),
            "strongest_action_pattern": strongest_pair,
            "strongest_app_transition": strongest_transition,
            "repos_observed": len(set(self._action_pairs.get("repos", {}))
                                 | set(self._app_sequences.get("repos", {}))),
            "repo_transitions_tracked": len(self._app_sequences.get("repos", {})),
        }

    def clear_all(self):
        """Clear all learned patterns."""
        with self._lock:
            self._action_pairs = {}
            self._app_sequences = {}
            self._timing_patterns = {}
            self._recent_actions = []
            self._recent_actions_by_repo = {}

            for path in [_ACTION_PAIRS_FILE, _APP_SEQUENCES_FILE, _TIMING_PATTERNS_FILE]:
                path.unlink(missing_ok=True)
