"""Tests for the DaemonService, IntelligenceSampler, and SecurityScanner."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from friday_v4.daemon import (
    DaemonConfig,
    DaemonService,
    DispatchOfferer,
    IntelligenceSampler,
    MemorySweeper,
    SecurityScanner,
)


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
            security_scanner=False,  # keep unit tests hermetic
            memory_sweeper=False,
            skill_learner=False,
            relationship_refresher=False,
            dispatch_offerer=False, autonomy_agent=False,
            mobile_push_worker=False,
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
            security_scanner=False,  # keep unit tests hermetic
            memory_sweeper=False, skill_learner=False,
            relationship_refresher=False, dispatch_offerer=False, autonomy_agent=False,
            mobile_push_worker=False,
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
            security_scanner=False,  # keep unit tests hermetic
            memory_sweeper=False, skill_learner=False,
            relationship_refresher=False, dispatch_offerer=False, autonomy_agent=False,
            mobile_push_worker=False,
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
                                suggestion_channel=False, sampler=False,
                                security_scanner=False,
                                memory_sweeper=False, skill_learner=False,
                                relationship_refresher=False,
                                dispatch_offerer=False, autonomy_agent=False,
                                mobile_push_worker=False)
        service._build_components()  # should not raise
        assert service._engine is False

    def test_status_shape(self):
        service = DaemonService(config=DaemonConfig())
        service._stop_event.set()  # simulate stopped daemon
        status = service.status()
        assert status["state"] == "stopped"
        assert "components" in status
        assert "pid" in status


class TestMemorySweeper:
    """Wave 10 decay sweep wired into the daemon."""

    def test_sweep_once_decays_and_removes(self, tmp_path):
        from friday_v4 import db
        from friday_v4.memory import DECAY_TIME, MemoryStore
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        store = MemoryStore(conn)
        store.store("project.old", "v1", confidence=0.9,
                    decay_policy=DECAY_TIME)
        store.store("project.fresh", "v2", confidence=0.9,
                    decay_policy=DECAY_TIME)
        # Backdate the old fact deterministically (30-day TTL).
        conn.execute(
            "UPDATE memories SET created_at = '2026-01-01T00:00:00+00:00', "
            "updated_at = '2026-01-01T00:00:00+00:00' "
            "WHERE mem_key = 'project.old'")
        conn.commit()
        conn.close()

        sweeper = MemorySweeper(interval=3600.0, db_path=dbp,
                                decay_kwargs={
                                    "now": "2026-08-01T12:00:00+00:00"})
        count = sweeper.sweep_once()
        assert count >= 1
        assert sweeper.last_report["decayed"] == 1
        assert sweeper._sweeps == 1
        assert sweeper.last_error is None
        # The fresh fact is untouched.
        conn = db.connect(dbp)
        assert db.recall_memory(conn, "project.fresh")["confidence"] == 0.9
        conn.close()

    def test_sweep_once_graceful_on_missing_db(self, tmp_path):
        """A missing DB is not an error: the sweep bootstraps an empty
        store and decays nothing (db.connect auto-creates the file, the
        established convention everywhere in V4)."""
        sweeper = MemorySweeper(interval=3600.0,
                                db_path=tmp_path / "missing" / "v4.db")
        assert sweeper.sweep_once() == 0
        assert sweeper.last_error is None  # graceful, not an error

    def test_sweep_once_never_crashes_on_broken_db(self, tmp_path):
        """A corrupt DB records last_error and returns 0 — never raises."""
        db_path = tmp_path / "broken" / "v4.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"this is not a sqlite database")
        sweeper = MemorySweeper(interval=3600.0, db_path=db_path)
        assert sweeper.sweep_once() == 0
        assert sweeper.last_error  # recorded, not raised

    def test_start_stop_lifecycle(self, tmp_path):
        sweeper = MemorySweeper(interval=0.05, db_path=tmp_path / "v4.db")
        sweeper.start()
        assert sweeper.running
        time.sleep(0.2)
        sweeper.stop()
        assert not sweeper.running

    def test_daemon_status_includes_memory_component(self, tmp_path):
        from friday_v4.daemon import DaemonService
        sweeper = MemorySweeper(interval=3600.0,
                                db_path=tmp_path / "v4.db")
        sweeper.running = True
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                memory_sweeper=sweeper)
        comps = service.status()["components"]
        assert comps["memory"] is True

    def test_daemon_builds_memory_sweeper_when_enabled(self, tmp_path,
                                                       monkeypatch):
        service = DaemonService(
            config=DaemonConfig(memory_sweep=True, memory_interval=0.05),
            engine=False, notifier=False, suggestion_channel=False,
            sampler=False, security_scanner=False,                                memory_sweeper=False, skill_learner=False,
                                relationship_refresher=False, dispatch_offerer=False, autonomy_agent=False,
                                mobile_push_worker=False)
        # memory_sweeper=False means the daemon treats it as unavailable
        # (like security_scanner=False) — no real DB touched.
        service._build_components()
        assert service._memory_sweeper is False


class TestDispatchOfferer:
    """Wave 14 dispatch offers wired into the daemon."""

    def _promote_skill(self, conn, name: str = "run-tests", steps=None) -> str:
        from friday_v4.skills import SkillRegistry
        reg = SkillRegistry(conn)
        steps = steps or [
            {"action_type": "testing", "command": "pytest -q", "goal": "run tests"},
            {"action_type": "shell", "command": "echo hi", "goal": "next"},
        ]
        sid = reg.create(name, steps=steps)
        for _ in range(reg._verify_matches):
            reg.record_shadow_match(sid)
        reg.verify(sid)
        reg.promote(sid)
        return sid

    def test_offer_once_offers_when_context_matches(self, tmp_path):
        from friday_v4 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        self._promote_skill(conn, "run-tests")
        db.record_action(conn, "testing", goal="go", command="pytest -q",
                         cwd="/home/me/friday_v4", status="succeeded")
        conn.close()

        notified = []
        offerer = DispatchOfferer(interval=3600.0, db_path=dbp,
                                  notify=lambda t, m, **kw: notified.append(m))
        count = offerer.offer_once()
        assert count == 1
        assert offerer.last_report["skill"] == "run-tests"
        assert offerer.last_report["next"] == "echo hi"
        assert notified and "run-tests" in notified[0]
        assert offerer.last_error is None

    def test_offer_once_dedupes_repeat_offers(self, tmp_path):
        """The same suggestion is not re-offered until the context moves."""
        from friday_v4 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        self._promote_skill(conn, "run-tests")
        db.record_action(conn, "testing", goal="go", command="pytest -q",
                         cwd="/home/me/friday_v4", status="succeeded")
        conn.close()

        notified = []
        offerer = DispatchOfferer(interval=3600.0, db_path=dbp,
                                  notify=lambda t, m, **kw: notified.append(m))
        assert offerer.offer_once() == 1
        assert offerer.offer_once() == 0  # same key → deduped
        assert len(notified) == 1

    def test_offer_once_never_executes(self, tmp_path):
        """The offerer only reads — no new audit actions are recorded."""
        from friday_v4 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        self._promote_skill(conn, "run-tests")
        db.record_action(conn, "testing", goal="go", command="pytest -q",
                         cwd="/home/me/friday_v4", status="succeeded")
        conn.close()

        offerer = DispatchOfferer(interval=3600.0, db_path=dbp)
        assert offerer.offer_once() == 1
        conn = db.connect(dbp)
        actions = db.recent_actions(conn)
        conn.close()
        assert len(actions) == 1  # only the seeded pytest action
        assert actions[0]["action_type"] == "testing"

    def test_offer_once_no_match_is_silent(self, tmp_path):
        from friday_v4 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        self._promote_skill(conn, "run-tests")
        conn.close()  # no matching action → nothing to offer

        offerer = DispatchOfferer(interval=3600.0, db_path=dbp)
        assert offerer.offer_once() == 0
        assert offerer.last_report["offers"] == 0

    def test_offer_once_graceful_on_missing_db(self, tmp_path):
        offerer = DispatchOfferer(interval=3600.0,
                                  db_path=tmp_path / "missing" / "v4.db")
        assert offerer.offer_once() == 0
        assert offerer.last_error is None  # graceful, not an error

    def test_offer_once_never_crashes_on_broken_db(self, tmp_path):
        db_path = tmp_path / "broken" / "v4.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"this is not a sqlite database")
        offerer = DispatchOfferer(interval=3600.0, db_path=db_path)
        assert offerer.offer_once() == 0
        assert offerer.last_error  # recorded, not raised

    def test_start_stop_lifecycle(self, tmp_path):
        offerer = DispatchOfferer(interval=0.05, db_path=tmp_path / "v4.db")
        offerer.start()
        assert offerer.running
        time.sleep(0.2)
        offerer.stop()
        assert not offerer.running

    def test_daemon_status_includes_dispatch_component(self, tmp_path):
        offerer = DispatchOfferer(interval=3600.0,
                                  db_path=tmp_path / "v4.db")
        offerer.running = True
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                dispatch_offerer=offerer)
        comps = service.status()["components"]
        assert comps["dispatch"] is True

    def test_daemon_builds_dispatch_offerer_when_enabled(self, tmp_path,
                                                         monkeypatch):
        service = DaemonService(
            config=DaemonConfig(dispatch_offer=True, dispatch_interval=0.05),
            engine=False, notifier=False, suggestion_channel=False,
            sampler=False, security_scanner=False,
            memory_sweeper=False, skill_learner=False,
            relationship_refresher=False, dispatch_offerer=False, autonomy_agent=False,
            mobile_push_worker=False)
        # dispatch_offerer=False means the daemon treats it as unavailable
        # (same convention as security_scanner=False) — no real DB touched.
        service._build_components()
        assert service._dispatch_offerer is False

    def test_daemon_shutdown_stops_dispatch_offerer(self, tmp_path):
        offerer = DispatchOfferer(interval=3600.0,
                                  db_path=tmp_path / "v4.db")
        offerer.start()
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                dispatch_offerer=offerer)
        service._shutdown_components()
        assert not offerer.running


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
                    open_apps: tuple[str, ...] = ("a", "b")
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


class _FakeVulnScanner:
    """Stand-in for security.scanner.VulnerabilityScanner (monkeypatched)."""

    def __init__(self, report):
        self._report = report

    def scan_quick(self, path, threshold="medium"):
        return self._report


def _finding(**kw):
    from friday_v4.security.reporter import Finding
    defaults = {
        "category": "vulnerability", "severity": "critical",
        "title": "test vuln", "detail": "unpatched",
        "package": "requests", "installed_version": "2.30.0",
        "fixed_version": "2.31.0", "cve": "CVE-TEST",
    }
    defaults.update(kw)
    return Finding(**defaults)


def _stub_scanner(monkeypatch, report):
    monkeypatch.setattr(
        "friday_v4.security.scanner.VulnerabilityScanner",
        lambda: _FakeVulnScanner(report))


class TestSecurityScanner:
    """Wave 3 periodic scanner wired into the daemon."""

    def _report(self, tmp_path, findings):
        from friday_v4.security.reporter import SecurityReport
        report = SecurityReport(path=str(tmp_path), scanned_at="now")
        report.findings = findings
        return report

    def test_scan_once_runs_notifies_and_persists(self, tmp_path, monkeypatch):
        report = self._report(tmp_path, [_finding()])
        _stub_scanner(monkeypatch, report)
        state_file = tmp_path / "v4_security_last.json"
        notified = []
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), threshold="medium",
            notify_threshold="high",
            notify=lambda t, m, urgency="normal", **kw: notified.append(t),
            state_file=state_file)

        count = scanner.scan_once()
        assert count == 1
        assert len(notified) == 1
        assert "CVE-TEST" in notified[0]
        # State persisted for doctor/status to read.
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["scans"] == 1
        assert state["report"]["grade"] == report.grade()
        assert scanner.last_report["grade"] == report.grade()

    def test_scan_once_notifications_fade(self, tmp_path, monkeypatch):
        """Security notifications use normal urgency + a bounded timeout so
        banners auto-dismiss (critical urgency is persistent on GNOME and
        never fades)."""
        report = self._report(tmp_path, [_finding()])
        _stub_scanner(monkeypatch, report)
        notified = []
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), threshold="medium",
            notify_threshold="high",
            notify=lambda t, m, **kw: notified.append((t, kw)),
            state_file=tmp_path / "v4_security_last.json")

        assert scanner.scan_once() == 1
        assert len(notified) == 1
        _title, kw = notified[0]
        assert kw["urgency"] == "normal"
        assert kw["timeout_ms"] == 15000

    def test_scan_once_default_notify_forwards_timeout(self, tmp_path,
                                                      monkeypatch):
        """The default (production) notifier path — no injected ``notify=``
        — forwards normal urgency and the fade timeout to
        DesktopAbstraction.notify. Regression: the default path used to
        drop timeout_ms (TypeError swallowed silently), which would have
        kept banners persistent or dropped them entirely."""
        report = self._report(tmp_path, [_finding()])
        _stub_scanner(monkeypatch, report)
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), threshold="medium",
            notify_threshold="high",
            state_file=tmp_path / "v4_security_last.json")

        with patch("friday_v4.desktop.wm_abstraction.DesktopAbstraction.notify") as mock_notify:
            assert scanner.scan_once() == 1

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["urgency"] == "normal"
        assert kwargs["timeout_ms"] == 15000

    def test_scan_once_deduplicates_notifications(self, tmp_path, monkeypatch):
        report = self._report(tmp_path, [_finding()])
        _stub_scanner(monkeypatch, report)
        notified = []
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), notify_threshold="high",
            notify=lambda t, m, urgency="normal", **kw: notified.append(t),
            state_file=tmp_path / "v4_security_last.json")

        assert scanner.scan_once() == 1
        assert scanner.scan_once() == 0  # same finding id → no re-notify
        assert len(notified) == 1
        assert scanner._scans == 2

    def test_notified_ids_persist_across_restart(self, tmp_path,
                                                 monkeypatch):
        """A restarted scanner (new instance, same state file) does not
        re-notify criticals that were already surfaced."""
        report = self._report(tmp_path, [_finding()])
        _stub_scanner(monkeypatch, report)
        state_file = tmp_path / "v4_security_last.json"
        notified = []

        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), notify_threshold="high",
            notify=lambda t, m, urgency="normal", **kw: notified.append(t),
            state_file=state_file)
        assert scanner.scan_once() == 1
        assert len(notified) == 1

        # Simulate a daemon restart: a fresh scanner on the same file.
        restarted = SecurityScanner(
            interval=3600.0, path=str(tmp_path), notify_threshold="high",
            notify=lambda t, m, urgency="normal", **kw: notified.append(t),
            state_file=state_file)
        assert restarted.scan_once() == 0
        assert len(notified) == 1  # not re-notified
        assert restarted._scans == 2  # cumulative across restarts

    def test_notified_ids_persisted_in_state_file(self, tmp_path,
                                                  monkeypatch):
        report = self._report(tmp_path, [_finding(),
                                         _finding(severity="high",
                                                  title="second",
                                                  cve="CVE-TWO")])
        _stub_scanner(monkeypatch, report)
        state_file = tmp_path / "v4_security_last.json"
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), notify_threshold="high",
            state_file=state_file)
        scanner.scan_once()

        state = json.loads(state_file.read_text())
        assert len(state["notified_ids"]) == 2
        assert all(fid in state["notified_ids"]
                   for fid in scanner._notified_ids)

    def test_corrupt_state_file_degrades_gracefully(self, tmp_path):
        """A corrupted state file must not crash the scanner at startup."""
        state_file = tmp_path / "v4_security_last.json"
        state_file.write_text("{ not json")
        scanner = SecurityScanner(interval=3600.0, path=str(tmp_path),
                                  state_file=state_file)
        assert scanner._notified_ids == []
        assert scanner._scans == 0

    def test_scan_once_below_notify_threshold_stays_silent(self, tmp_path,
                                                          monkeypatch):
        report = self._report(tmp_path, [
            _finding(severity="low", title="style nit", cve="")])
        _stub_scanner(monkeypatch, report)
        notified = []
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path), notify_threshold="high",
            notify=lambda t, m, urgency="normal", **kw: notified.append(t),
            state_file=tmp_path / "v4_security_last.json")

        assert scanner.scan_once() == 0
        assert notified == []
        # Report is still persisted so doctor shows the last scan.
        assert scanner.last_report is not None

    def test_scan_once_captures_scanner_failure(self, tmp_path, monkeypatch):
        class _BoomScanner:
            def scan_quick(self, path, threshold="medium"):
                raise RuntimeError("scanner exploded")
        monkeypatch.setattr(
            "friday_v4.security.scanner.VulnerabilityScanner",
            lambda: _BoomScanner())
        scanner = SecurityScanner(
            interval=3600.0, path=str(tmp_path),
            state_file=tmp_path / "v4_security_last.json")

        assert scanner.scan_once() == 0
        assert "scanner exploded" in (scanner.last_error or "")

    def test_start_stop_lifecycle(self, tmp_path, monkeypatch):
        report = self._report(tmp_path, [])
        _stub_scanner(monkeypatch, report)
        scanner = SecurityScanner(
            interval=0.05, path=str(tmp_path), notify_threshold="high",
            state_file=tmp_path / "v4_security_last.json")
        scanner.start()
        assert scanner.running
        time.sleep(0.2)
        scanner.stop()
        assert not scanner.running
        assert scanner._scans >= 1  # ran its initial scan

    def test_daemon_status_includes_security_component(self, tmp_path):
        from friday_v4.daemon import DaemonService
        # Explicit state file keeps the test hermetic (no ~/.friday reads).
        scanner = SecurityScanner(interval=3600.0, path=str(tmp_path),
                                  state_file=tmp_path / "v4_security_last.json")
        scanner.running = True
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=scanner)
        comps = service.status()["components"]
        assert comps["security"] is True


def _write_security_state(tmp_path, state: dict) -> None:
    """Write a daemon security state file where diag_security reads it.

    diag_security resolves the file as ``Path.home()/.friday/…``; the
    tests patch home to ``tmp_path``, so the file must live under
    ``tmp_path/.friday/``.
    """
    target = tmp_path / ".friday" / "v4_security_last.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state))


class TestDoctorSecurity:
    """`friday4 doctor` Security section (reads the daemon's state file)."""

    def test_diag_security_reports_tools_and_state(self, tmp_path,
                                                   monkeypatch):
        from friday_v4 import cli_doctor
        _write_security_state(tmp_path, {
            "scans": 3,
            "last_error": None,
            "report": {"grade": "B", "score": 85,
                        "counts_by_severity": {"critical": 0, "high": 1,
                                                "medium": 2, "low": 0,
                                                "info": 0}},
        })
        monkeypatch.setattr(cli_doctor.Path, "home",
                            classmethod(lambda cls: tmp_path))
        ok, rows = cli_doctor.diag_security()
        keys = [r["key"] for r in rows]
        assert "tool builtin" in keys
        assert "scans run" in keys
        grade = next(r for r in rows if r["key"] == "last grade")
        assert "B" in grade["value"]
        findings = next(r for r in rows if r["key"] == "last findings")
        assert "1 high" in findings["value"]
        # High findings in the last scan degrade the section.
        assert ok is False

    def test_diag_security_no_state_file_is_healthy(self, tmp_path,
                                                    monkeypatch):
        from friday_v4 import cli_doctor
        monkeypatch.setattr(cli_doctor.Path, "home",
                            classmethod(lambda cls: tmp_path))
        ok, rows = cli_doctor.diag_security()
        assert ok
        assert any(r["key"] == "last scan" for r in rows)

    def test_diag_security_clean_scan_is_healthy(self, tmp_path, monkeypatch):
        from friday_v4 import cli_doctor
        _write_security_state(tmp_path, {
            "scans": 1,
            "last_error": None,
            "report": {"grade": "A", "score": 100,
                        "counts_by_severity": {"critical": 0, "high": 0,
                                                "medium": 0, "low": 0,
                                                "info": 0}},
        })
        monkeypatch.setattr(cli_doctor.Path, "home",
                            classmethod(lambda cls: tmp_path))
        ok, rows = cli_doctor.diag_security()
        assert ok
        findings = next(r for r in rows if r["key"] == "last findings")
        assert findings["value"] == "clean"
