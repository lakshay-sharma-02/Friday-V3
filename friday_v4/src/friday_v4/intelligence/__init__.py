"""Advanced Intelligence — Drift detection, anomaly detection, predictive analytics.

Extends V3's learning pipeline with ML-based intelligence capabilities. Where
V3 detects drift by comparing exemplar distributions, V4 predicts drift before
it happens. Where V3 records action outcomes, V4 detects anomalous patterns.

Capabilities:
    - Predictive drift detection (time-series on execution patterns)
    - Anomaly detection in skill and execution behavior
    - Code health diagnostics (complexity, coverage, churn trends)
    - Predictive analytics (what's likely to break next)
    - Continuous learning from user corrections

**Status:** Wave 4 — implemented. All modules are pure-stdlib statistics
(no ML dependencies): drift uses a moving-average + std-dev baseline with
z-score drift flags; anomaly uses robust median/MAD thresholding; health
uses AST complexity + git churn; predictor wraps drift trends with a
churn×complexity risk ranker; learner tracks correction feedback weights.
"""

from __future__ import annotations

try:
    from .drift import DriftPredictor
    from .anomaly import AnomalyDetector
    from .health import CodeHealthDiagnostics
    from .predictor import PredictiveAnalytics
    from .learner import ContinuousLearner
    _INTELLIGENCE_AVAILABLE = True
except ImportError:  # pragma: no cover - Wave 4 stub
    DriftPredictor = None  # type: ignore
    AnomalyDetector = None  # type: ignore
    CodeHealthDiagnostics = None  # type: ignore
    PredictiveAnalytics = None  # type: ignore
    ContinuousLearner = None  # type: ignore
    _INTELLIGENCE_AVAILABLE = False


def is_available() -> bool:
    """Whether the advanced intelligence layer is implemented yet."""
    return _INTELLIGENCE_AVAILABLE


__all__ = [
    "DriftPredictor",
    "AnomalyDetector",
    "CodeHealthDiagnostics",
    "PredictiveAnalytics",
    "ContinuousLearner",
    "is_available",
    "_INTELLIGENCE_AVAILABLE",
]
