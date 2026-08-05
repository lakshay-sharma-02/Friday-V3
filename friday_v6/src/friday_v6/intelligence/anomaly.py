"""Anomaly Detection — statistical thresholding on execution patterns.

Flags unusual observations that deviate from a category's established
distribution. Uses a robust median + median-absolute-deviation (MAD)
baseline so a handful of outliers don't skew the "normal" range, and
requires a minimum sample count before judging anything anomalous
(Friday never cries wolf on cold-start data).

Usage:
    detector = AnomalyDetector()
    detector.record("test_run", 3)     # failures
    detector.record("test_run", 2)
    result = detector.detect("test_run", 27)
    if result["anomalous"]:
        print(f"Unusual: z={result['z_score']:.1f} — {result['detail']}")

All data lives in ~/.friday/intelligence/anomaly.json
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.intelligence.anomaly")

_INTELLIGENCE_DIR = Path.home() / ".friday" / "intelligence"
_ANOMALY_FILE = _INTELLIGENCE_DIR / "anomaly.json"

_DEFAULT_MIN_SAMPLES = 8
_DEFAULT_Z_THRESHOLD = 3.0
_MAX_HISTORY = 200          # per-category sample cap
_MAX_LOG = 50               # anomaly event log cap


class AnomalyDetector:
    """Detects anomalous values against a robust per-category baseline.

    For each named category, keeps a bounded window of recent values and a
    robust baseline (median + MAD). A new value whose normalized deviation
    exceeds ``z_threshold`` is reported as anomalous and logged with a
    timestamp so the CLI can surface "Friday noticed something unusual".

    Usage:
        detector = AnomalyDetector()
        detector.record("test_failures", 0)
        detector.record("test_failures", 1)
        anomaly = detector.detect("test_failures", 42)
    """

    def __init__(self, min_samples: int = _DEFAULT_MIN_SAMPLES,
                 z_threshold: float = _DEFAULT_Z_THRESHOLD,
                 file: Optional[Path] = None):
        self._lock = threading.Lock()
        self.min_samples = min_samples
        self.z_threshold = z_threshold
        self._file = file or _ANOMALY_FILE
        self._samples: dict[str, list[float]] = {}
        self._anomalies: list[dict] = []
        self._load()

    # ── Storage ───────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._file.exists():
                with open(self._file) as f:
                    data = json.load(f)
                    self._samples = {
                        k: [float(v) for v in vals]
                        for k, vals in (data.get("samples") or {}).items()
                    }
                    self._anomalies = list(data.get("anomalies") or [])
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            logger.debug(f"Could not load anomaly state: {exc}")

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({"samples": self._samples,
                           "anomalies": self._anomalies}, f, indent=2)
        except OSError as exc:
            logger.warning(f"Could not save anomaly state: {exc}")

    # ── Recording ─────────────────────────────────────────────────────

    def record(self, category: str, value: float) -> None:
        """Append a value to a category's sample history (bounded)."""
        with self._lock:
            self._record_locked(category, value)

    def _record_locked(self, category: str, value: float) -> None:
        """Append logic. Caller must hold ``self._lock``.

        Extracted so ``detect`` (which already holds the non-reentrant
        lock) can record without re-acquiring it and deadlocking.
        """
        series = self._samples.setdefault(category, [])
        series.append(float(value))
        if len(series) > _MAX_HISTORY:
            del series[:len(series) - _MAX_HISTORY]
        self._save()

    # ── Baseline ──────────────────────────────────────────────────────

    def get_baseline(self, category: str) -> dict:
        """Robust baseline for a category: median + MAD.

        Returns ``has_baseline=False`` until ``min_samples`` are present.
        """
        with self._lock:
            return self._get_baseline_locked(category)

    def _get_baseline_locked(self, category: str) -> dict:
        """Baseline computation. Caller must hold ``self._lock``.

        Extracted so ``detect`` (which already holds the non-reentrant
        lock) can compute a baseline without re-acquiring it.
        """
        series = self._samples.get(category) or []
        if len(series) < self.min_samples:
            return {"median": 0.0, "mad": 0.0, "samples": len(series),
                    "has_baseline": False}

        median = statistics.median(series)
        deviations = [abs(v - median) for v in series]
        mad = statistics.median(deviations) or 0.0
        return {"median": round(median, 3), "mad": round(mad, 3),
                "samples": len(series), "has_baseline": True}

    # ── Detection ─────────────────────────────────────────────────────

    def detect(self, category: str, value: float,
               detail: str = "") -> dict:
        """Check a value against the category's robust baseline.

        Also records the value into the category's history (so the baseline
        learns) and appends an anomaly log entry when flagged.

        Returns:
            dict with ``anomalous``, ``z_score``, ``median``, ``mad``,
            ``samples`` and a human ``detail``.
        """
        with self._lock:
            baseline = self._get_baseline_locked(category)
            self._record_locked(category, value)

            if not baseline["has_baseline"] or baseline["mad"] == 0:
                return {"anomalous": False, "z_score": 0.0,
                        "median": baseline["median"], "mad": baseline["mad"],
                        "samples": baseline["samples"],
                        "detail": "not enough data yet"}

            # Normalized deviation: (x - median) / (1.4826 * MAD)
            z = (float(value) - baseline["median"]) / (1.4826 * baseline["mad"])
            anomalous = abs(z) >= self.z_threshold

            result = {
                "anomalous": anomalous,
                "z_score": round(z, 3),
                "median": baseline["median"],
                "mad": baseline["mad"],
                "samples": baseline["samples"],
                "detail": detail or (
                    f"value {value} deviates from typical {baseline['median']}"
                ),
            }

            if anomalous:
                self._anomalies.append({
                    "category": category,
                    "value": float(value),
                    "z_score": round(z, 3),
                    "detail": result["detail"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                if len(self._anomalies) > _MAX_LOG:
                    del self._anomalies[:len(self._anomalies) - _MAX_LOG]
                self._save()
                logger.info(f"Anomaly detected in {category}: z={z:.2f}")

            return result

    # ── Queries / reset ───────────────────────────────────────────────

    def get_recent_anomalies(self, limit: int = 20) -> list[dict]:
        """Most recent anomaly log entries (newest first)."""
        with self._lock:
            return list(reversed(self._anomalies[-limit:]))

    def get_stats(self) -> dict:
        """Summary of tracked categories + recent anomaly count."""
        with self._lock:
            return {
                "categories_tracked": list(self._samples.keys()),
                "total_samples": sum(len(v) for v in self._samples.values()),
                "anomalies_logged": len(self._anomalies),
                "recent_anomalies": list(reversed(self._anomalies[-5:])),
            }

    def clear_all(self) -> None:
        """Drop all samples and anomaly history."""
        with self._lock:
            self._samples = {}
            self._anomalies = []
            self._file.unlink(missing_ok=True)
