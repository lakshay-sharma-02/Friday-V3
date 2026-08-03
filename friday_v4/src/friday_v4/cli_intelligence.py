"""CLI commands for `friday4 intelligence` — advanced intelligence layer.

Usage:
    friday4 intelligence status         # Overview of drift/anomaly/health/predictions
    friday4 intelligence health [path]  # Code health diagnostics for a project
    friday4 intelligence predict        # Predictive insights on tracked metrics
    friday4 intelligence anticipate     # What's likely to break / drift next
    friday4 intelligence drift --metric NAME --value N
    friday4 intelligence anomaly --category NAME --value N
    friday4 intelligence learn --category NAME --feedback positive|negative
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger("friday_v4.cli_intelligence")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Intelligence{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    print()


def _grade_color(grade: str) -> str:
    return {"A": _GREEN, "B": _GREEN, "C": _YELLOW,
            "D": _YELLOW, "F": _RED}.get(grade, _RESET)


def _print_issue(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_intelligence_status(args: argparse.Namespace) -> int:
    """Show an overview of the intelligence layer's state."""
    from .intelligence import is_available

    _print_logo()
    if not is_available():
        _print_issue("Intelligence layer unavailable.")
        return 1

    from .intelligence import (
        AnomalyDetector,
        CodeHealthDiagnostics,
        ContinuousLearner,
        DriftPredictor,
    )

    drift = DriftPredictor()
    anomaly = AnomalyDetector()
    learner = ContinuousLearner()

    # Drift baselines
    drift_stats = drift.get_stats()
    print(f"  {_BOLD}Drift Baselines{_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    if drift_stats["metrics_tracked"]:
        for metric in drift_stats["metrics_tracked"][:6]:
            base = drift_stats["baselines"][metric]
            status = (f"mean {base['mean']}±{base['std']}"
                      if base["has_baseline"] else f"{base['samples']} samples (warming up)")
            print(f"  {_CYAN}◈{_RESET} {metric:<28} {_DIM}{status}{_RESET}")
    else:
        _print_dim("No metrics tracked yet — record samples via `friday4 intelligence drift`.")

    # Anomalies
    anomalies = anomaly.get_recent_anomalies(limit=3)
    print(f"\n  {_BOLD}Anomalies{_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    if anomalies:
        for a in anomalies:
            print(f"  {_YELLOW}⚠{_RESET} {a['category']} — {a['detail']}"
                  f" ({_DIM}z={a['z_score']}{_RESET})")
    else:
        _print_dim("No anomalies flagged yet.")

    # Health grade
    health = CodeHealthDiagnostics()
    report = health.analyze_repo(".")
    grade = report.get("grade", "?")
    color = _grade_color(grade)
    print(f"\n  {_BOLD}Code Health (cwd){_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    print(f"  Grade: {color}{grade}{_RESET}  Score: {report.get('score', 0)}/100"
          f"  Files: {report.get('scanned', 0)}")
    hotspots = report.get("hotspots", [])
    for h in hotspots[:3]:
        print(f"  {_YELLOW}🔥{_RESET} {h['path']} — {h['churn']} changes,"
              f" complexity {h['max_complexity']}")

    # Learner weights
    learner_stats = learner.get_stats()
    print(f"\n  {_BOLD}Learning (corrections){_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    if learner_stats["categories_learned"]:
        for cat, weight in learner_stats["weights"].items():
            bar = "█" * int(weight * 10) + "░" * (10 - int(weight * 10))
            print(f"  {_DIM}{cat:<24}{_RESET} {bar} {weight:.2f}")
    else:
        _print_dim("No correction feedback yet — record via `friday4 intelligence learn`.")

    print()
    return 0


def cmd_intelligence_health(args: argparse.Namespace) -> int:
    """Run code health diagnostics on a project directory."""
    from .intelligence import CodeHealthDiagnostics

    path = args.path or "."
    health = CodeHealthDiagnostics()
    report = health.analyze_repo(path)

    _print_logo()
    print(f"  {_BOLD}Code Health — {path}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")

    if not report.get("ok", False):
        _print_issue(report.get("error", "Analysis failed."))
        return 1

    grade = report.get("grade", "?")
    color = _grade_color(grade)
    print(f"  Overall:  {color}{grade}{_RESET} ({report.get('score', 0)}/100)"
          f" across {report.get('scanned', 0)} files\n")

    # Per-file table (worst first)
    files = sorted(report.get("files", []), key=lambda f: f["score"])
    print(f"  {_BOLD}Files{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    for f in files[:args.top]:
        color = _grade_color(f["grade"])
        detail = (f"churn {f['churn']}, cx {f['max_complexity']}, "
                  f"{f['todo_count']} TODO" if f["max_complexity"] > 1
                  else f"{f['loc']} LOC")
        print(f"  {color}{f['grade']}{_RESET} {f['score']:>3}  "
              f"{f['path'][:60]:<60} {_DIM}{detail}{_RESET}")

    if report.get("issues"):
        print(f"\n  {_BOLD}Issues{_RESET}")
        for issue in report["issues"][:8]:
            print(f"  {_YELLOW}•{_RESET} {issue}")

    print()
    return 0


def cmd_intelligence_predict(args: argparse.Namespace) -> int:
    """Show predictive insights on tracked metrics."""
    from .intelligence import PredictiveAnalytics

    analytics = PredictiveAnalytics()
    stats = analytics.get_stats()

    _print_logo()
    print(f"  {_BOLD}Predictive Insights{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")

    drift_stats = stats.get("drift_metrics", {})
    metrics = drift_stats.get("metrics_tracked", [])
    if not metrics:
        _print_dim("No metrics tracked yet. Record samples via:")
        _print_dim("  friday4 intelligence drift --metric NAME --value N")
        print()
        return 0

    for metric in metrics[:8]:
        forecast = analytics.predict_next(metric)
        predicted = forecast.get("predicted")
        if predicted is None:
            _print_dim(f"  ◈ {metric}: warming up (need more samples)")
            continue
        trend_icon = {"rising": "↗", "falling": "↘", "stable": "→"}.get(
            forecast["trend"], "→")
        color = _YELLOW if forecast["trend"] == "rising" else _CYAN
        print(f"  {color}{trend_icon}{_RESET} {metric:<28}"
              f" next ≈ {predicted} ({forecast['trend']},"
              f" conf {forecast['confidence']:.2f})")

    risks = stats.get("risk_history", {})
    if risks:
        print(f"\n  {_BOLD}Tracked Risk Items{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        for name, entry in list(risks.items())[:5]:
            print(f"  {_DIM}{name}:{_RESET} {entry['risk_level']}"
                  f" ({entry['risk_score']}/100)")

    print()
    return 0


def cmd_intelligence_anticipate(args: argparse.Namespace) -> int:
    """Summarize what's most likely to break or drift next."""
    from .intelligence import (
        AnomalyDetector,
        CodeHealthDiagnostics,
        PredictiveAnalytics,
    )

    analytics = PredictiveAnalytics()
    anomaly = AnomalyDetector()
    health = CodeHealthDiagnostics()

    _print_logo()
    print(f"  {_BOLD}Anticipation — what's likely to break next{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")

    findings = []

    # 1. Health hotspots (churn × complexity)
    report = health.analyze_repo(args.path or ".")
    hotspots = report.get("hotspots", [])
    for h in hotspots[:3]:
        findings.append(f"🔥 {h['path']} — {h['churn']} recent changes"
                        f" and complexity {h['max_complexity']}")
    if not hotspots:
        _print_dim("No health hotspots in cwd.")

    # 2. Rising/falling metric trends
    drift_stats = analytics.get_stats().get("drift_metrics", {})
    for metric in drift_stats.get("metrics_tracked", [])[:5]:
        forecast = analytics.predict_next(metric)
        if forecast.get("predicted") is None:
            continue
        if forecast["trend"] == "rising" and forecast["confidence"] > 0.5:
            findings.append(f"↗ {metric} is trending up (conf {forecast['confidence']:.2f})")
        elif forecast["trend"] == "falling" and forecast["confidence"] > 0.5:
            findings.append(f"↘ {metric} is trending down (conf {forecast['confidence']:.2f})")

    # 3. Recent anomalies
    for a in anomaly.get_recent_anomalies(limit=3):
        findings.append(f"⚠ {a['category']}: {a['detail']}")

    if not findings:
        print(f"  {_GREEN}✓ Nothing unusual — Friday is watching.{_RESET}")
    else:
        for f_ in findings:
            print(f"  {_DIM}•{_RESET} {f_}")

    print()
    return 0


def cmd_intelligence_drift(args: argparse.Namespace) -> int:
    """Record a metric sample and check for drift."""
    from .intelligence import DriftPredictor

    predictor = DriftPredictor()
    # detect() records the sample AND checks it — no separate record call,
    # otherwise the value would be double-counted in the baseline.
    result = predictor.detect(args.metric, args.value)

    _print_logo()
    print(f"  {_BOLD}Drift — {args.metric} = {args.value}{_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")

    if result["drifted"]:
        print(f"  {_YELLOW}⚠ Drift detected{_RESET}"
              f" ({result['direction']}, z={result['z_score']})")
    else:
        color = _GREEN if result["samples"] >= 5 else _DIM
        print(f"  {color}✓ No drift{_RESET}"
              f" (z={result['z_score']}, mean {result['mean']},"
              f" std {result['std']}, {result['samples']} samples)")

    forecast = predictor.predict_next(args.metric)
    if forecast.get("predicted") is not None:
        print(f"  {_DIM}Next: {forecast['predicted']} ({forecast['trend']},"
              f" conf {forecast['confidence']:.2f}){_RESET}")

    print()
    return 0


def cmd_intelligence_anomaly(args: argparse.Namespace) -> int:
    """Record a category sample and check for anomalies."""
    from .intelligence import AnomalyDetector

    detector = AnomalyDetector()
    result = detector.detect(args.category, args.value, detail=args.detail or "")

    _print_logo()
    print(f"  {_BOLD}Anomaly — {args.category} = {args.value}{_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")

    if result["anomalous"]:
        print(f"  {_RED}⚠ Anomalous{_RESET} (z={result['z_score']},"
              f" median {result['median']})")
        print(f"  {_DIM}  {result['detail']}{_RESET}")
    else:
        print(f"  {_GREEN}✓ Normal{_RESET} (median {result['median']},"
              f" z={result['z_score']}, {result['samples']} samples)")
        if result["samples"] < 8:
            _print_dim("  (warming up — need ≥ 8 samples per category)")

    print()
    return 0


def cmd_intelligence_learn(args: argparse.Namespace) -> int:
    """Record user correction feedback for a suggestion category."""
    from .intelligence import ContinuousLearner

    learner = ContinuousLearner()
    if args.feedback in ("positive", "yes", "1", "true"):
        weight = learner.record_feedback(args.category, positive=True)
        label = f"{_GREEN}+ positive{_RESET}"
    else:
        weight = learner.record_feedback(args.category, positive=False)
        label = f"{_RED}− negative{_RESET}"

    _print_logo()
    print(f"  {_BOLD}Feedback{_RESET} {label} for '{args.category}'")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    print(f"  Updated weight: {_GREEN}{weight:.2f}{_RESET} (0=noise, 1=reliable)")
    print()
    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_intelligence_parser(subparsers) -> None:
    """Build the `friday4 intelligence` subparser (used by the integrated CLI)."""
    parser = subparsers.add_parser(
        "intelligence",
        help="Advanced intelligence: drift, anomaly, health, predictions",
        description="Friday's advanced intelligence layer — drift detection, "
                    "anomaly detection, code health diagnostics, predictive "
                    "analytics, and continuous correction learning.",
    )
    intelligence_sub = parser.add_subparsers(dest="intelligence_command")

    # friday4 intelligence status
    p = intelligence_sub.add_parser("status", help="Intelligence layer overview")
    p.set_defaults(func=cmd_intelligence_status)

    # friday4 intelligence health [path]
    p = intelligence_sub.add_parser(
        "health", help="Code health diagnostics for a project")
    p.add_argument("path", nargs="?", default=".",
                   help="Project directory to analyze (default: cwd)")
    p.add_argument("--top", type=int, default=10,
                   help="Show the N worst files (default: 10)")
    p.set_defaults(func=cmd_intelligence_health)

    # friday4 intelligence predict
    p = intelligence_sub.add_parser(
        "predict", help="Predictive insights on tracked metrics")
    p.set_defaults(func=cmd_intelligence_predict)

    # friday4 intelligence anticipate
    p = intelligence_sub.add_parser(
        "anticipate", help="What's likely to break / drift next")
    p.add_argument("--path", type=str, default=".",
                   help="Project directory to analyze (default: cwd)")
    p.set_defaults(func=cmd_intelligence_anticipate)

    # friday4 intelligence drift --metric NAME --value N
    p = intelligence_sub.add_parser(
        "drift", help="Record a metric sample and check for drift")
    p.add_argument("--metric", required=True, help="Metric name (e.g. commits_per_week)")
    p.add_argument("--value", type=float, required=True, help="Sample value")
    p.set_defaults(func=cmd_intelligence_drift)

    # friday4 intelligence anomaly --category NAME --value N
    p = intelligence_sub.add_parser(
        "anomaly", help="Record a category sample and check for anomalies")
    p.add_argument("--category", required=True, help="Category name (e.g. test_failures)")
    p.add_argument("--value", type=float, required=True, help="Sample value")
    p.add_argument("--detail", type=str, default="", help="Optional detail string")
    p.set_defaults(func=cmd_intelligence_anomaly)

    # friday4 intelligence learn --category NAME --feedback positive|negative
    p = intelligence_sub.add_parser(
        "learn", help="Record user correction feedback")
    p.add_argument("--category", required=True, help="Suggestion category")
    p.add_argument("--feedback", required=True,
                   choices=["positive", "negative"],
                   help="Whether the suggestion was useful")
    p.set_defaults(func=cmd_intelligence_learn)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v4.cli_intelligence`."""
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="friday4 intelligence")
    subparsers = parser.add_subparsers(dest="intelligence_command")
    build_intelligence_parser(subparsers)

    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args) or 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
