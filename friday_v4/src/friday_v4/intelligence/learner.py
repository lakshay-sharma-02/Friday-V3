"""Continuous Learner — self-improvement from user corrections.

The Wave 4 "self-improving via user correction learning" deliverable.
When the user accepts, dismisses, or corrects a Friday suggestion, the
learner records that feedback as a per-category weight so Friday gets
better at knowing *which kinds* of suggestions land.

Simple and honest: weights are clamped 0-1 (1 = this category is reliably
useful, 0 = always noise). No ML — a moving average of explicit signals,
persisted so learning survives restarts.

Usage:
    learner = ContinuousLearner()
    learner.record_feedback("suggestion.pattern", positive=True)
    learner.record_correction("suggestion.pattern", delta=0.1)
    weight = learner.get_weight("suggestion.pattern")

All data lives in ~/.friday/intelligence/learner.json
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.intelligence.learner")

_INTELLIGENCE_DIR = Path.home() / ".friday" / "intelligence"
_LEARNER_FILE = _INTELLIGENCE_DIR / "learner.json"


class ContinuousLearner:
    """Tracks per-category usefulness weights from user feedback.

    ``record_feedback`` nudges a category's weight toward 1.0 on a
    positive signal and toward 0.0 on a negative one. ``record_correction``
    applies an explicit delta. Weights are clamped to [0, 1] and persisted
    after every update so corrections are never lost.

    Usage:
        learner = ContinuousLearner()
        learner.record_feedback("reminder", positive=True)
        learner.record_feedback("reminder", positive=False)
        learner.get_stats()["weights"]["reminder"]  # ~0.5
    """

    def __init__(self, file: Optional[Path] = None):
        self._lock = threading.Lock()
        self._file = file or _LEARNER_FILE
        self._weights: dict[str, dict] = self._load()

    # ── Storage ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self._file.exists():
                with open(self._file) as f:
                    data = json.load(f)
                    return dict(data.get("weights") or {})
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            logger.debug(f"Could not load learner state: {exc}")
        return {}

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({"weights": self._weights}, f, indent=2)
        except OSError as exc:
            logger.warning(f"Could not save learner state: {exc}")

    # ── Feedback ──────────────────────────────────────────────────────

    def record_feedback(self, category: str, positive: bool) -> float:
        """Record an explicit positive/negative signal for a category.

        Returns the updated weight (0-1).
        """
        with self._lock:
            entry = self._weights.setdefault(category, {
                "weight": 0.5, "positives": 0, "negatives": 0,
                "last_updated": None,
            })
            if positive:
                entry["positives"] += 1
                # Move toward 1.0 by a step proportional to how far off we are.
                entry["weight"] += (1.0 - entry["weight"]) * 0.2
            else:
                entry["negatives"] += 1
                entry["weight"] -= entry["weight"] * 0.2
            entry["weight"] = round(max(0.0, min(1.0, entry["weight"])), 3)
            entry["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return entry["weight"]

    def record_correction(self, category: str, delta: float) -> float:
        """Apply an explicit weight correction (e.g. from a correction UI).

        ``delta`` is added to the current weight (clamped 0-1).
        """
        with self._lock:
            entry = self._weights.setdefault(category, {
                "weight": 0.5, "positives": 0, "negatives": 0,
                "last_updated": None,
            })
            entry["weight"] = round(
                max(0.0, min(1.0, entry["weight"] + delta)), 3)
            entry["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return entry["weight"]

    # ── Queries / reset ───────────────────────────────────────────────

    def get_weight(self, category: str) -> float:
        """The current usefulness weight for a category (default 0.5)."""
        with self._lock:
            entry = self._weights.get(category)
            return entry["weight"] if entry else 0.5

    def get_stats(self) -> dict:
        """Summary of learned weights with feedback counts."""
        with self._lock:
            return {
                "categories_learned": len(self._weights),
                "weights": {k: v["weight"]
                            for k, v in self._weights.items()},
                "details": dict(self._weights),
            }

    def clear_all(self) -> None:
        """Reset all learned weights."""
        with self._lock:
            self._weights = {}
            self._file.unlink(missing_ok=True)
