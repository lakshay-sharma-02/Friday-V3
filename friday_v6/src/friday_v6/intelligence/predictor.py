"""Predictive Analytics — what's likely to break next.

Combines the drift baseline with per-metric trend lines to answer
"what will happen next" and "which file is most at risk". Built purely on
statistics (least-squares trends + churn/complexity ranking) so it works
offline with zero ML dependencies, per the Wave 4 plan.

Usage:
    analytics = PredictiveAnalytics()
    analytics.record("test_failures", 2)
    forecast = analytics.predict_next("test_failures")
    risks = analytics.rank_risk([{"path": "main.py", "churn": 12,
                                  "complexity": 18, "score": 70}])
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from .drift import DriftPredictor

logger = logging.getLogger("friday_v6.intelligence.predictor")

_INTELLIGENCE_DIR = Path.home() / ".friday" / "intelligence"
_RISK_FILE = _INTELLIGENCE_DIR / "predictor.json"


class PredictiveAnalytics:
    """Forecasts metric trends and ranks risk for the CLI/anticipation layer.

    ``predict_next`` wraps DriftPredictor's least-squares trend, exposing a
    plain "next value + direction + confidence" answer. ``rank_risk``
    scores arbitrary items (files, repos) by churn × complexity — the
    signal the plan calls "likely to break next" — normalized 0-100.
    """

    def __init__(self, file: Optional[Path] = None):
        self._lock = threading.Lock()
        self._file = file or _RISK_FILE
        # Keep the drift baseline next to our own state so callers passing a
        # tmp path (tests) don't accidentally read/write the real ~/.friday.
        drift_file = self._file.with_name("drift.json")
        self._drift = DriftPredictor(file=drift_file)
        self._risk_history: dict[str, dict] = self._load()

    # ── Storage ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self._file.exists():
                with open(self._file) as f:
                    return dict(json.load(f).get("risk_history") or {})
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            logger.debug(f"Could not load predictor state: {exc}")
        return {}

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({"risk_history": self._risk_history}, f, indent=2)
        except OSError as exc:
            logger.warning(f"Could not save predictor state: {exc}")

    # ── Time-series forecasting ───────────────────────────────────────

    def record(self, metric: str, value: float) -> None:
        """Record a sample for a metric (delegates to the drift baseline)."""
        self._drift.record(metric, value)

    def predict_next(self, metric: str) -> dict:
        """Forecast the next value of a metric.

        Returns a dict with ``metric``, ``predicted`` (or None when there's
        not enough data), ``trend`` ("rising"/"falling"/"stable"), ``slope``
        and ``confidence`` (0-1).
        """
        forecast = self._drift.predict_next(metric)
        forecast["metric"] = metric
        return forecast

    # ── Risk ranking ──────────────────────────────────────────────────

    def rank_risk(self, items: list[dict], top_n: int = 5) -> list[dict]:
        """Rank items (files/repos) by likelihood of breaking next.

        Each item may carry ``churn`` (recent commits), ``max_complexity``
        or ``complexity``, and ``score`` (a health score, 0-100, where
        lower = worse). Items without signal are ranked last.

        Returns a new list of items, each with a ``risk_score`` (0-100,
        higher = riskier) and ``risk_level`` ("high"/"medium"/"low").
        """
        ranked = []
        for item in items:
            churn = int(item.get("churn") or 0)
            complexity = int(item.get("max_complexity")
                             or item.get("complexity") or 0)
            health = int(item.get("score") or 100)

            # Risk grows with churn + complexity; falls with good health.
            risk = (min(churn, 30) * 1.5
                    + min(complexity, 20) * 1.0
                    + max(0, 100 - health) * 0.3)
            risk_score = int(max(0, min(100, risk)))
            level = ("high" if risk_score >= 60 else
                     "medium" if risk_score >= 30 else "low")
            ranked.append({**item, "risk_score": risk_score,
                           "risk_level": level})

        ranked.sort(key=lambda i: i["risk_score"], reverse=True)
        return ranked[:top_n]

    def track_risk(self, name: str, metrics: dict) -> dict:
        """Persist a named item's risk so history can be trended.

        Stores the computed risk score under ``name`` and returns it.
        """
        ranked = self.rank_risk([metrics], top_n=1)
        entry = ranked[0] if ranked else dict(metrics)
        with self._lock:
            self._risk_history[name] = {
                "risk_score": entry.get("risk_score", 0),
                "risk_level": entry.get("risk_level", "low"),
            }
            self._save()
        return entry

    def get_risk_history(self) -> dict:
        """Previously tracked risk scores by name."""
        with self._lock:
            return dict(self._risk_history)

    def get_stats(self) -> dict:
        """Summary of what the predictor is tracking."""
        with self._lock:
            return {
                "drift_metrics": self._drift.get_stats(),
                "risk_items_tracked": len(self._risk_history),
                "risk_history": dict(self._risk_history),
            }

    def clear_all(self) -> None:
        """Reset drift baselines and risk history."""
        with self._lock:
            self._drift.clear_all()
            self._risk_history = {}
            self._file.unlink(missing_ok=True)
