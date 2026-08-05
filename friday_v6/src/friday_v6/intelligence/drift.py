"""Predictive Drift Detection — time-series on execution patterns.

Where V3 detects drift by comparing exemplar distributions after the fact,
V4's DriftPredictor predicts drift *before* it compounds: it keeps a
rolling baseline (moving average + std-dev) of a metric and flags when a
new sample deviates beyond a z-score threshold, then forecasts the trend
with a simple linear fit.

No ML, no external deps — pure statistics, per the Wave 4 plan.

Usage:
    predictor = DriftPredictor()
    predictor.record("commits_per_week", 12)
    if predictor.detect("commits_per_week", 3)["drifted"]:
        print("Work pattern has changed — investigate!")
    forecast = predictor.predict_next("commits_per_week")

All data lives in ~/.friday/intelligence/drift.json
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.intelligence.drift")

_INTELLIGENCE_DIR = Path.home() / ".friday" / "intelligence"
_DRIFT_FILE = _INTELLIGENCE_DIR / "drift.json"

# Default rolling window for the baseline (in samples).
_DEFAULT_WINDOW = 20


class DriftPredictor:
    """Detects statistically-significant drift in a metric's time series.

    For each named metric it keeps a bounded window of recent samples and
    derives a baseline (mean + std-dev). ``detect`` computes the z-score of
    a new sample against that baseline; an absolute z-score above the
    threshold (default 2.0 ≈ 95th percentile) is reported as drift.

    ``predict_next`` fits a simple least-squares line over the window and
    extrapolates one step ahead, so Friday can say *"commit frequency is
    trending down"* before the change becomes a problem.

    Usage:
        predictor = DriftPredictor()
        predictor.record("actions_per_session", 40)
        predictor.record("actions_per_session", 42)
        report = predictor.detect("actions_per_session", 18)
        report == {"drifted": True, "z_score": ..., "mean": ..., "std": ...}
    """

    def __init__(self, window: int = _DEFAULT_WINDOW,
                 z_threshold: float = 2.0,
                 file: Optional[Path] = None):
        self._lock = threading.Lock()
        self.window = max(window, 5)
        self.z_threshold = z_threshold
        self._file = file or _DRIFT_FILE
        self._series: dict[str, list[float]] = self._load()

    # ── Storage ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self._file.exists():
                with open(self._file) as f:
                    data = json.load(f)
                    return {k: [float(v) for v in vals]
                            for k, vals in (data.get("series") or {}).items()}
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            logger.debug(f"Could not load drift state: {exc}")
        return {}

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({"series": self._series}, f, indent=2)
        except OSError as exc:
            logger.warning(f"Could not save drift state: {exc}")

    # ── Recording ─────────────────────────────────────────────────────

    def record(self, metric: str, value: float) -> None:
        """Append a sample to the metric's bounded time series.

        The window is kept at ``self.window`` samples (oldest dropped) so
        the baseline reflects recent behavior, not ancient history.
        """
        with self._lock:
            series = self._series.setdefault(metric, [])
            series.append(float(value))
            if len(series) > self.window:
                del series[:len(series) - self.window]
            self._save()

    # ── Baseline ──────────────────────────────────────────────────────

    def get_baseline(self, metric: str) -> dict:
        """Return the baseline statistics for a metric.

        Returns a dict with ``mean``, ``std``, ``samples``, and ``has_baseline``
        (True once there are enough samples to be meaningful).
        """
        with self._lock:
            return self._get_baseline_locked(metric)

    def _get_baseline_locked(self, metric: str) -> dict:
        """Baseline computation. Caller must hold ``self._lock``.

        Extracted so ``detect``/``get_stats`` (which already hold the
        non-reentrant lock) can compute a baseline without re-acquiring
        it and deadlocking.
        """
        series = self._series.get(metric) or []
        if len(series) < 2:
            return {"mean": 0.0, "std": 0.0, "samples": len(series),
                    "has_baseline": False}
        mean = statistics.fmean(series)
        std = statistics.stdev(series) if len(series) > 1 else 0.0
        return {"mean": round(mean, 3), "std": round(std, 3),
                "samples": len(series), "has_baseline": True}

    # ── Drift detection ───────────────────────────────────────────────

    def detect(self, metric: str, value: float) -> dict:
        """Check whether a new sample deviates significantly from baseline.

        Returns:
            dict with keys:
              - ``drifted``: True when |z| >= threshold (and a baseline exists)
              - ``z_score``: signed z-score of the sample vs the baseline
              - ``mean`` / ``std``: baseline statistics
              - ``samples``: baseline sample count
              - ``direction``: "up" | "down" | "none"
        """
        with self._lock:
            # Record the sample FIRST (even during warm-up) so a metric
            # accumulates history through detect() alone — otherwise a fresh
            # metric would early-return below and never reach a baseline.
            series = self._series.setdefault(metric, [])
            series.append(float(value))
            if len(series) > self.window:
                del series[:len(series) - self.window]

            baseline = self._get_baseline_locked(metric)

            if not baseline["has_baseline"] or baseline["std"] == 0:
                self._save()
                return {"drifted": False, "z_score": 0.0,
                        "mean": baseline["mean"], "std": baseline["std"],
                        "samples": baseline["samples"], "direction": "none"}

            # z-score of the new value against the baseline distribution
            z = (float(value) - baseline["mean"]) / baseline["std"]
            drifted = abs(z) >= self.z_threshold
            direction = ("up" if z > 0 else "down") if drifted else "none"
            self._save()

            return {"drifted": drifted, "z_score": round(z, 3),
                    "mean": baseline["mean"], "std": baseline["std"],
                    "samples": baseline["samples"], "direction": direction}

    # ── Forecasting ───────────────────────────────────────────────────

    def predict_next(self, metric: str) -> dict:
        """Forecast the next sample with a least-squares linear trend.

        Returns:
            dict with ``predicted``, ``trend`` ("rising" | "falling" |
            "stable"), ``slope``, and ``confidence`` (0-1 based on how
            strongly the window follows a line).
        """
        with self._lock:
            series = self._series.get(metric) or []
            n = len(series)
            if n < 2:
                return {"predicted": None, "trend": "stable", "slope": 0.0,
                        "confidence": 0.0}

            # Least-squares slope over index vs value
            xs = list(range(n))
            x_mean = statistics.fmean(xs)
            y_mean = statistics.fmean(series)
            num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, series))
            den = sum((x - x_mean) ** 2 for x in xs)
            slope = num / den if den else 0.0
            intercept = y_mean - slope * x_mean

            predicted = intercept + slope * n  # one step ahead
            trend = ("rising" if slope > 0.01 else
                     "falling" if slope < -0.01 else "stable")

            # Confidence: R² of the linear fit (0-1)
            if y_mean:
                ss_tot = sum((y - y_mean) ** 2 for y in series)
                ss_res = sum(
                    (y - (intercept + slope * x)) ** 2
                    for x, y in zip(xs, series)
                )
                confidence = 1.0 - (ss_res / ss_tot) if ss_tot else 0.0
            else:
                confidence = 0.0

            return {"predicted": round(predicted, 3), "trend": trend,
                    "slope": round(slope, 4),
                    "confidence": round(max(0.0, min(1.0, confidence)), 3)}

    # ── Stats / reset ─────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Summary of all tracked metrics with their baselines."""
        with self._lock:
            return {
                "metrics_tracked": list(self._series.keys()),
                "total_samples": sum(len(v) for v in self._series.values()),
                "baselines": {
                    m: self._get_baseline_locked(m) for m in self._series
                },
            }

    def clear_all(self) -> None:
        """Drop all tracked series."""
        with self._lock:
            self._series = {}
            self._file.unlink(missing_ok=True)
