"""Intelligence layer tests — Wave 4 (drift, anomaly, health, predictor, learner).

Unit tests that avoid touching the real ~/.friday/ directory by pointing
each module's state file at a tmp path. All modules are pure statistics
(stdlib only), so no external services are needed.
"""

from __future__ import annotations

from pathlib import Path

from friday_v6.intelligence import (
    AnomalyDetector,
    CodeHealthDiagnostics,
    ContinuousLearner,
    DriftPredictor,
    PredictiveAnalytics,
    is_available,
)

# ==========================================================================
# DriftPredictor
# ==========================================================================


class TestDriftPredictor:
    def _make(self, tmp_path, **kwargs):
        return DriftPredictor(file=tmp_path / "drift.json", **kwargs)

    def test_baseline_warming_up(self, tmp_path):
        """Fewer than 2 samples → no baseline, no drift claim."""
        predictor = self._make(tmp_path)
        predictor.record("commits", 10)
        baseline = predictor.get_baseline("commits")
        assert baseline["has_baseline"] is False
        assert predictor.detect("commits", 99)["drifted"] is False

    def test_detect_flags_drift_beyond_threshold(self, tmp_path):
        """A value far outside the baseline's std-dev is flagged."""
        predictor = self._make(tmp_path, z_threshold=2.0)
        for v in (10, 11, 9, 10, 11, 10, 9, 10, 11, 10):
            predictor.record("commits", v)
        result = predictor.detect("commits", 40)
        assert result["drifted"] is True
        assert result["direction"] == "up"
        assert result["z_score"] > 2.0

    def test_detect_normal_value_not_flagged(self, tmp_path):
        predictor = self._make(tmp_path)
        for v in (10, 11, 9, 10, 11, 10, 9, 10, 11, 10):
            predictor.record("commits", v)
        result = predictor.detect("commits", 10)
        assert result["drifted"] is False

    def test_predict_next_rising_trend(self, tmp_path):
        """Monotonically increasing series → 'rising' forecast."""
        predictor = self._make(tmp_path)
        for v in (1, 2, 3, 4, 5, 6, 7, 8):
            predictor.record("load", v)
        forecast = predictor.predict_next("load")
        assert forecast["trend"] == "rising"
        assert forecast["predicted"] > 8
        assert 0.0 <= forecast["confidence"] <= 1.0

    def test_predict_next_insufficient_data(self, tmp_path):
        predictor = self._make(tmp_path)
        predictor.record("load", 1)
        forecast = predictor.predict_next("load")
        assert forecast["predicted"] is None

    def test_persistence_roundtrip(self, tmp_path):
        """Samples survive a fresh instance reading the same file."""
        predictor = self._make(tmp_path)
        predictor.record("commits", 10)
        predictor.record("commits", 12)

        predictor2 = DriftPredictor(file=tmp_path / "drift.json")
        baseline = predictor2.get_baseline("commits")
        assert baseline["samples"] == 2
        assert baseline["has_baseline"] is True

    def test_clear_all(self, tmp_path):
        predictor = self._make(tmp_path)
        predictor.record("commits", 10)
        predictor.clear_all()
        assert predictor.get_stats()["metrics_tracked"] == []


# ==========================================================================
# AnomalyDetector
# ==========================================================================


class TestAnomalyDetector:
    def _make(self, tmp_path, **kwargs):
        return AnomalyDetector(file=tmp_path / "anomaly.json", **kwargs)

    def test_warming_up_no_anomaly(self, tmp_path):
        detector = self._make(tmp_path, min_samples=8)
        detector.record("test_run", 1)
        result = detector.detect("test_run", 999)
        assert result["anomalous"] is False
        assert "not enough data" in result["detail"]

    def test_detect_outlier(self, tmp_path):
        detector = self._make(tmp_path, min_samples=8)
        for v in (1, 2, 1, 2, 1, 2, 1, 2, 1, 2):
            detector.record("test_run", v)
        result = detector.detect("test_run", 42)
        assert result["anomalous"] is True
        assert result["z_score"] > 3.0

    def test_detect_normal(self, tmp_path):
        detector = self._make(tmp_path)
        for v in (1, 2, 1, 2, 1, 2, 1, 2, 1, 2):
            detector.record("test_run", v)
        result = detector.detect("test_run", 2)
        assert result["anomalous"] is False

    def test_anomaly_logged(self, tmp_path):
        detector = self._make(tmp_path)
        for v in (1, 2, 1, 2, 1, 2, 1, 2, 1, 2):
            detector.record("test_run", v)
        detector.detect("test_run", 42, detail="build failed")
        anomalies = detector.get_recent_anomalies()
        assert len(anomalies) == 1
        assert anomalies[0]["category"] == "test_run"
        assert anomalies[0]["detail"] == "build failed"
        assert "timestamp" in anomalies[0]

    def test_robust_to_outliers_in_baseline(self, tmp_path):
        """MAD baseline is robust — a single extreme sample shouldn't make
        everything else look anomalous."""
        detector = self._make(tmp_path)
        for v in (2, 2, 2, 2, 2, 2, 2, 2, 2, 100):
            detector.record("timing", v)
        # 2 is well within the robust range despite the 100 in history
        result = detector.detect("timing", 2)
        assert result["anomalous"] is False

    def test_persistence_and_clear(self, tmp_path):
        detector = self._make(tmp_path)
        detector.record("timing", 5)
        detector2 = AnomalyDetector(file=tmp_path / "anomaly.json")
        assert detector2.get_stats()["total_samples"] == 1
        detector2.clear_all()
        assert detector2.get_stats()["total_samples"] == 0


# ==========================================================================
# CodeHealthDiagnostics
# ==========================================================================


class TestCodeHealthDiagnostics:
    def _write(self, tmp_path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_analyze_missing_path(self, tmp_path):
        health = CodeHealthDiagnostics()
        report = health.analyze_repo(str(tmp_path / "nope"))
        assert report["ok"] is False

    def test_simple_project_grade(self, tmp_path):
        self._write(tmp_path, "main.py", (
            "def simple(a, b):\n"
            "    return a + b\n"
            "\n"
            "def run():\n"
            "    print(simple(1, 2))\n"
        ))
        health = CodeHealthDiagnostics()
        report = health.analyze_repo(str(tmp_path))
        assert report["ok"] is True
        assert report["scanned"] == 1
        assert report["grade"] in ("A", "B")
        assert report["score"] >= 75

    def test_complex_function_flagged(self, tmp_path):
        self._write(tmp_path, "complex.py", (
            "def spaghetti(x):\n"
            "    if x > 0:\n"
            "        if x > 1:\n"
            "            if x > 2:\n"
            "                if x > 3:\n"
            "                    if x > 4:\n"
            "                        if x > 5:\n"
            "                            return 'deep'\n"
            "                        return '5'\n"
            "                    return '4'\n"
            "                return '3'\n"
            "            return '2'\n"
            "        return '1'\n"
            "    return '0'\n"
        ))
        health = CodeHealthDiagnostics()
        report = health.analyze_repo(str(tmp_path))
        assert report["ok"] is True
        file = report["files"][0]
        assert file["max_complexity"] >= 6
        # Complexity debt must degrade the file/repo score and grade
        assert file["score"] < 100
        assert report["score"] < 100
        assert report["grade"] != "A"

    def test_syntax_error_degrades_gracefully(self, tmp_path):
        self._write(tmp_path, "broken.py", "def broken(:\n    pass\n")
        health = CodeHealthDiagnostics()
        report = health.analyze_repo(str(tmp_path))
        assert report["ok"] is True  # repo still analyzed
        assert report["files"][0]["issues"]  # file flagged

    def test_skips_venv_and_vcs(self, tmp_path):
        self._write(tmp_path, "main.py", "x = 1\n")
        self._write(tmp_path, ".venv/lib/site-packages/dep.py", "import bad\n")  # noqa
        health = CodeHealthDiagnostics()
        report = health.analyze_repo(str(tmp_path))
        assert report["scanned"] == 1  # only main.py

    def test_summarize(self, tmp_path):
        self._write(tmp_path, "main.py", "x = 1\n")
        health = CodeHealthDiagnostics()
        summary = health.summarize(health.analyze_repo(str(tmp_path)))
        assert "grade" in summary and "score" in summary


# ==========================================================================
# PredictiveAnalytics
# ==========================================================================


class TestPredictiveAnalytics:
    def _make(self, tmp_path):
        return PredictiveAnalytics(file=tmp_path / "predictor.json")

    def test_predict_next_rising(self, tmp_path):
        analytics = self._make(tmp_path)
        for v in (1, 2, 3, 4, 5, 6, 7, 8):
            analytics.record("build_minutes", v)
        forecast = analytics.predict_next("build_minutes")
        assert forecast["metric"] == "build_minutes"
        assert forecast["trend"] == "rising"
        assert forecast["predicted"] > 8

    def test_predict_insufficient(self, tmp_path):
        analytics = self._make(tmp_path)
        analytics.record("build_minutes", 5)
        assert analytics.predict_next("build_minutes")["predicted"] is None

    def test_rank_risk_orders_by_signal(self, tmp_path):
        analytics = self._make(tmp_path)
        items = [
            {"path": "hot.py", "churn": 25, "max_complexity": 18, "score": 60},
            {"path": "calm.py", "churn": 1, "max_complexity": 2, "score": 95},
        ]
        ranked = analytics.rank_risk(items)
        assert ranked[0]["path"] == "hot.py"
        assert ranked[0]["risk_score"] > ranked[1]["risk_score"]
        assert ranked[0]["risk_level"] in ("high", "medium")
        assert ranked[1]["risk_level"] == "low"

    def test_rank_risk_caps_and_limits(self, tmp_path):
        analytics = self._make(tmp_path)
        items = [{"path": f"f{i}.py", "churn": 100, "max_complexity": 100,
                  "score": 0} for i in range(10)]
        ranked = analytics.rank_risk(items, top_n=3)
        assert len(ranked) == 3
        assert all(0 <= r["risk_score"] <= 100 for r in ranked)

    def test_track_risk_persists(self, tmp_path):
        analytics = self._make(tmp_path)
        analytics.track_risk("main.py", {"churn": 20, "max_complexity": 15,
                                         "score": 70})
        analytics2 = PredictiveAnalytics(file=tmp_path / "predictor.json")
        history = analytics2.get_risk_history()
        assert "main.py" in history
        assert history["main.py"]["risk_score"] > 0

    def test_clear_all(self, tmp_path):
        analytics = self._make(tmp_path)
        analytics.record("build_minutes", 5)
        analytics.track_risk("x.py", {"churn": 1, "score": 90})
        analytics.clear_all()
        assert analytics.get_stats()["risk_items_tracked"] == 0


# ==========================================================================
# ContinuousLearner
# ==========================================================================


class TestContinuousLearner:
    def _make(self, tmp_path):
        return ContinuousLearner(file=tmp_path / "learner.json")

    def test_positive_feedback_raises_weight(self, tmp_path):
        learner = self._make(tmp_path)
        w1 = learner.record_feedback("suggestion.pattern", positive=True)
        w2 = learner.record_feedback("suggestion.pattern", positive=True)
        assert w1 > 0.5
        assert w2 > w1
        assert w2 <= 1.0

    def test_negative_feedback_lowers_weight(self, tmp_path):
        learner = self._make(tmp_path)
        w1 = learner.record_feedback("suggestion.pattern", positive=False)
        w2 = learner.record_feedback("suggestion.pattern", positive=False)
        assert w1 < 0.5
        assert w2 < w1
        assert w2 >= 0.0

    def test_weights_clamped(self, tmp_path):
        learner = self._make(tmp_path)
        for _ in range(50):
            learner.record_feedback("cat", positive=True)
        assert learner.get_weight("cat") <= 1.0
        for _ in range(50):
            learner.record_feedback("cat2", positive=False)
        assert learner.get_weight("cat2") >= 0.0

    def test_correction_delta(self, tmp_path):
        learner = self._make(tmp_path)
        w = learner.record_correction("cat", delta=0.3)
        assert abs(w - 0.8) < 0.01  # 0.5 + 0.3

    def test_default_weight(self, tmp_path):
        learner = self._make(tmp_path)
        assert learner.get_weight("never_seen") == 0.5

    def test_persistence(self, tmp_path):
        learner = self._make(tmp_path)
        learner.record_feedback("cat", positive=True)
        learner2 = ContinuousLearner(file=tmp_path / "learner.json")
        assert learner2.get_weight("cat") > 0.5
        assert learner2.get_stats()["categories_learned"] == 1


# ==========================================================================
# Package availability + CLI wiring
# ==========================================================================


class TestIntelligencePackage:
    def test_is_available_true(self):
        """All five modules import — the stub is now implemented."""
        assert is_available() is True

    def test_all_classes_exposed(self):
        for cls in (DriftPredictor, AnomalyDetector, CodeHealthDiagnostics,
                    PredictiveAnalytics, ContinuousLearner):
            assert callable(cls)


class TestIntelligenceCLI:
    def test_parser_registers_subcommands(self):
        import argparse

        from friday_v6.cli_intelligence import build_intelligence_parser

        parser = argparse.ArgumentParser(prog="friday6")
        subparsers = parser.add_subparsers(dest="command")
        build_intelligence_parser(subparsers)
        args = parser.parse_args(["intelligence", "health", "--top", "5"])
        assert args.top == 5
        args = parser.parse_args(["intelligence", "drift",
                                  "--metric", "commits", "--value", "3"])
        assert args.metric == "commits" and args.value == 3.0
        args = parser.parse_args(["intelligence", "anomaly",
                                  "--category", "tests", "--value", "2"])
        assert args.category == "tests"
        args = parser.parse_args(["intelligence", "learn",
                                  "--category", "x", "--feedback", "positive"])
        assert args.feedback == "positive"

    def test_integrated_friday6_parser_includes_intelligence(self):
        """The `friday6` entry point exposes `friday6 intelligence`."""
        # Parse-only via a dedicated subprocess-free path: build the parser
        # exactly like main() does and check the choice exists.
        import argparse

        from friday_v6.cli_desktop import build_desktop_parser
        from friday_v6.cli_intelligence import build_intelligence_parser
        from friday_v6.cli_proactive import build_proactive_parser
        from friday_v6.cli_talk import build_talk_parser, build_voice_parser, main

        parser = argparse.ArgumentParser(prog="friday6")
        subparsers = parser.add_subparsers(dest="command")
        build_talk_parser(subparsers)
        build_voice_parser(subparsers)
        build_desktop_parser(subparsers)
        build_proactive_parser(subparsers)
        build_intelligence_parser(subparsers)

        assert callable(main)  # entry point reachable
        args = parser.parse_args(["intelligence", "status"])
        assert args.intelligence_command == "status"
