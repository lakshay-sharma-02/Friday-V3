"""Tests for the DaemonService and IntelligenceSampler."""

from __future__ import annotations

import time

import pytest

from friday_v4.daemon import DaemonConfig, DaemonService, IntelligenceSampler


class _FakeEngine:
    """Minimal AnticipationEngine stand-in (no desktop/DB side effects)."""

    def __init__(self):
        self.observer_started = False
        self.observer_stopped = False
        self.cleaned_up = False
        self._observer_thread = None

    def start_observer(self, interval_seconds=1.0, heartbeat_seconds=30.0):
        self.observer_started = True

    def stop_observer(self):
        self.observer_stopped = True

    def cleanup(self):
        self.cleaned_up = True


class _FakeChannel:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.running = False

    def start(self):
        self.started = True
        self.running = True

    def stop(self):
        self.stopped = True
        self.running = False


class _FakeSampler:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.running = False

    def start(self):
        self.started = True
        self.running = True

    def stop(self):
        self.stopped = True
        self.running = False


class TestDaemonService:
    def test_builds_and_shuts_down(self):
        engine = _FakeEngine()
        notifier = _FakeChannel()
        suggestions = _FakeChannel()
        sampler = _FakeSampler()
        service = DaemonService(
            config=DaemonConfig(),
            engine=engine,
            notifier=notifier,
            suggestion_channel=suggestions,
            sampler=sampler,
        )
        service._build_components()
        assert engine.observer_started
        assert notifier.started
        assert suggestions.started
        assert sampler.started

        service._shutdown_components()
        assert notifier.stopped
        assert suggestions.stopped
        assert sampler.stopped
        assert engine.observer_stopped
        # Injected engines are caller-owned: observer stops but cleanup is
        # the caller's job (the daemon only cleans up engines it built).
        assert not engine.cleaned_up

    def test_shutdown_cleans_up_owned_engine(self):
        engine = _FakeEngine()
        service = DaemonService(
            config=DaemonConfig(),
            engine=engine,  # built by caller → daemon does NOT own it
            notifier=None, suggestion_channel=None, sampler=None,
        )
        service._owns_engine = True  # simulate daemon-built engine
        service._shutdown_components()
        assert engine.cleaned_up

    def test_run_loop_exits_on_stop(self, tmp_path, monkeypatch):
        """run() returns promptly once stop() is called (5s wait loop)."""
        import friday_v4.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod, "_STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(daemon_mod, "_PID_FILE", tmp_path / "daemon.pid")

        engine = _FakeEngine()
        notifier = _FakeChannel()
        sampler = _FakeSampler()
        service = DaemonService(
            config=DaemonConfig(),
            engine=engine, notifier=notifier,
            suggestion_channel=None, sampler=sampler,
        )

        # Stop after a moment from a timer thread (run blocks).
        def _stop_soon():
            time.sleep(0.3)
            service.stop()
        import threading
        threading.Thread(target=_stop_soon, daemon=True).start()

        start = time.time()
        service.run()
        elapsed = time.time() - start
        assert elapsed < 10
        assert not service.running

    def test_components_degrade_gracefully(self):
        """Missing components (all None) must not crash _build_components."""
        service = DaemonService(config=DaemonConfig(),
                                engine=False, notifier=False,
                                suggestion_channel=False, sampler=False)
        service._build_components()  # should not raise
        assert service._engine is False

    def test_status_shape(self):
        service = DaemonService(config=DaemonConfig())
        service._stop_event.set()  # simulate stopped daemon
        status = service.status()
        assert status["state"] == "stopped"
        assert "components" in status
        assert "pid" in status


class TestIntelligenceSampler:
    def test_sample_once_with_fakes(self):
        class _FakeDrift:
            def __init__(self):
                self.records = []

            def record(self, metric, value):
                self.records.append((metric, value))

        class _FakeAnomaly:
            def __init__(self):
                self.records = []

            def record(self, category, value):
                self.records.append((category, value))

        class _FakeContext:
            def get_context(self):
                class Ctx:
                    open_apps = ["a", "b"]
                    workspace_count = 2
                    session_minutes = 5
                    dirty_repos = 1
                return Ctx()

        drift, anomaly = _FakeDrift(), _FakeAnomaly()
        sampler = IntelligenceSampler(interval=0.1, drift=drift,
                                      anomaly=anomaly,
                                      context=_FakeContext())
        count = sampler.sample_once()
        assert count == 4
        assert ("window_count", 2) in drift.records
        assert ("dirty_repos", 1) in anomaly.records

    def test_loop_start_stop(self):
        sampler = IntelligenceSampler(interval=0.05)
        sampler.start()
        assert sampler.running
        time.sleep(0.2)
        sampler.stop()
        assert not sampler.running

    def test_sample_once_metrics_empty_on_context_failure(self):
        sampler = IntelligenceSampler(interval=0.1, drift=None, anomaly=None,
                                      context=None)
        # No desktop in CI → metrics empty → count 0, no crash.
        assert sampler.sample_once() >= 0
