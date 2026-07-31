"""DaemonService — the persistent ambient FRIDAY.

Ties the Phase 1/2/4 pieces into one process:

    AnticipationEngine (shared, warm)
      ├── start_observer()      # desktop watcher → pattern learner
      ├── ProactiveSuggestionChannel   (suggestions → desktop notify)
      ├── DesktopNotificationChannel   (V3 ambient → desktop notify)
      └── IntelligenceSampler          (drift/anomaly samples)
      └── VoicePipeline (optional, --voice; hotword idle listening)

Design laws:
- One process, graceful degradation: any component failure logs and
  the rest keep running (never crash the daemon).
- Foreground by default; Ctrl+C / SIGTERM stops cleanly.
- Writes `~/.friday/v4_daemon.status` (JSON) + pid for `status`/`stop`.
- The engine is shared so patterns learned by the observer are warm
  for the suggestion channel.

Usage:
    service = DaemonService(voice=True)
    service.run()            # blocks until stop() (signal handler)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("friday_v4.daemon")

_STATUS_FILE = Path.home() / ".friday" / "v4_daemon.status"
_PID_FILE = Path.home() / ".friday" / "v4_daemon.pid"


@dataclass
class DaemonConfig:
    """Daemon runtime options."""
    voice: bool = False             # start hotword voice pipeline
    notifications: bool = True      # ambient + suggestion channels
    poll_interval: float = 10.0     # ambient feed poll (s)
    suggestion_interval: float = 120.0   # suggestion poll (s)
    sample_interval: float = 300.0  # intelligence sampling (s)
    observer_interval: float = 1.0  # desktop watcher poll (s)
    heartbeat_seconds: float = 30.0 # session heartbeat (s)


class IntelligenceSampler:
    """Periodically records drift/anomaly samples from the intelligence layer.

    The drift baseline needs data before it can detect anything; this
    sampler feeds real metrics while the daemon runs so `friday4
    intelligence status` has a baseline after a few hours. Metrics come
    from the shared context engine (cheap probes, no V3 dependency).
    """

    def __init__(self, interval: float = 300.0,
                 drift=None, anomaly=None, context=None):
        self.interval = interval
        self._drift = drift
        self._anomaly = anomaly
        self._context = context
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._samples = 0

    def _get_engines(self):
        if self._drift is None:
            try:
                from .intelligence.drift import DriftPredictor
                self._drift = DriftPredictor()
            except Exception:
                self._drift = False
        if self._anomaly is None:
            try:
                from .intelligence.anomaly import AnomalyDetector
                self._anomaly = AnomalyDetector()
            except Exception:
                self._anomaly = False
        return self._drift, self._anomaly

    def _metrics(self) -> dict:
        """Real, cheap metrics to sample (empty on any failure)."""
        ctx = self._context
        if ctx is None:
            try:
                from .proactive.context_engine import DeepContextEngine
                ctx = DeepContextEngine()
            except Exception:
                return {}
        try:
            context = ctx.get_context()
            return {
                "window_count": len(context.open_apps),
                "workspace_count": context.workspace_count,
                "session_minutes": context.session_minutes,
                "dirty_repos": context.dirty_repos,
            }
        except Exception:
            return {}

    def sample_once(self) -> int:
        """Record one sample from each engine. Returns metrics sampled."""
        drift, anomaly = self._get_engines()
        metrics = self._metrics()
        count = 0
        for name, value in metrics.items():
            try:
                if drift:
                    drift.record(name, value)
                if anomaly:
                    anomaly.record(name, value)
                count += 1
            except Exception as exc:
                logger.debug(f"Sample {name} failed: {exc}")
        if count:
            self._samples += count
        return count

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-sampler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # Sample once at start so there's an initial baseline.
        try:
            self.sample_once()
        except Exception:
            pass
        while self.running:
            # Short waits with running-check keep stop() responsive.
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.sample_once()
            except Exception:
                logger.debug("Sampler error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


class DaemonService:
    """The ambient FRIDAY process — wires all Phase 1/2/4 components."""

    def __init__(self, config: Optional[DaemonConfig] = None,
                 engine: Optional[Any] = None,
                 voice_pipeline: Optional[Any] = None,
                 notifier: Optional[Any] = None,
                 suggestion_channel: Optional[Any] = None,
                 sampler: Optional[Any] = None):
        self.config = config or DaemonConfig()
        self._engine = engine
        self._voice_pipeline = voice_pipeline
        self._notifier = notifier
        self._suggestion_channel = suggestion_channel
        self._sampler = sampler
        self._owns_engine = engine is None
        self._stop_event = threading.Event()
        self.started_at: Optional[float] = None
        self.notification_count = 0
        self._voice_started = False

    # ── Component construction (lazy, each independently optional) ──

    def _get_engine(self):
        """Shared AnticipationEngine (also drives the observer)."""
        if self._engine is None:
            try:
                from .proactive.anticipation import AnticipationEngine
                self._engine = AnticipationEngine()
                self._owns_engine = True
            except Exception as exc:
                logger.error(f"Proactive engine unavailable: {exc}")
                self._engine = False
        return self._engine

    def _build_components(self) -> None:
        engine = self._get_engine()
        if engine:
            try:
                engine.start_observer(
                    interval_seconds=self.config.observer_interval,
                    heartbeat_seconds=self.config.heartbeat_seconds)
            except Exception as exc:
                logger.warning(f"Observer failed to start: {exc}")

        if self.config.notifications:
            if self._notifier is None:
                try:
                    from .desktop.notifier import DesktopNotificationChannel
                    self._notifier = DesktopNotificationChannel(
                        poll_interval=self.config.poll_interval)
                except Exception as exc:
                    logger.warning(f"Ambient notifier unavailable: {exc}")
                    self._notifier = False
            if self._notifier:
                try:
                    self._notifier.start()
                except Exception as exc:
                    logger.warning(f"Ambient notifier start failed: {exc}")

            if self._suggestion_channel is None:
                try:
                    from .desktop.notifier import ProactiveSuggestionChannel
                    self._suggestion_channel = ProactiveSuggestionChannel(
                        engine=engine if engine is not False else None,
                        poll_interval=self.config.suggestion_interval)
                except Exception as exc:
                    logger.warning(f"Suggestion channel unavailable: {exc}")
                    self._suggestion_channel = False
            if self._suggestion_channel:
                try:
                    self._suggestion_channel.start()
                except Exception as exc:
                    logger.warning(f"Suggestion channel start failed: {exc}")

        if self._sampler is None:
            self._sampler = IntelligenceSampler(
                interval=self.config.sample_interval)
        try:
            self._sampler.start()
        except Exception as exc:
            logger.warning(f"Sampler start failed: {exc}")

        if self.config.voice:
            self._start_voice()

    def _start_voice(self) -> None:
        if self._voice_pipeline is None:
            try:
                from .voice.pipeline import VoicePipeline
                from .voice.router import VoiceRouter
                from .voice.core import config_from_file
                self._voice_pipeline = VoicePipeline(config_from_file())
            except Exception as exc:
                logger.warning(f"Voice pipeline unavailable: {exc}")
                self._voice_pipeline = False
        if not self._voice_pipeline:
            return
        try:
            router = VoiceRouter(self._voice_pipeline, enable_proactive=True)
            self._voice_pipeline.route_function = router.route
            if self._voice_pipeline.start():
                self._voice_started = True
                logger.info("Voice pipeline started (hotword listening)")
            else:
                logger.warning("Voice pipeline failed to start (no audio?)")
        except Exception as exc:
            logger.warning(f"Voice start failed: {exc}")

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict:
        """Current daemon state as a dict (also persisted to status file)."""
        uptime = 0.0
        if self.started_at:
            uptime = time.time() - self.started_at
        comps = {
            "engine": self._engine is not None and self._engine is not False,
            "observer": bool(self._engine and self._engine._observer_thread
                             and self._engine._observer_thread.is_alive())
            if self._engine else False,
            "notifier": bool(self._notifier
                             and getattr(self._notifier, "running", False)),
            "suggestions": bool(self._suggestion_channel
                                and getattr(self._suggestion_channel,
                                            "running", False)),
            "sampler": bool(self._sampler
                            and getattr(self._sampler, "running", False)),
            "voice": self._voice_started,
        }
        return {
            "state": "running" if self.running else "stopped",
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_seconds": round(uptime, 1),
            "notification_count": self.notification_count,
            "components": comps,
        }

    def _write_status(self) -> None:
        try:
            _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATUS_FILE.write_text(
                json.dumps(self.status(), indent=2))
        except OSError as exc:
            logger.debug(f"Status write failed: {exc}")

    def _write_pid(self) -> None:
        try:
            _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PID_FILE.write_text(str(os.getpid()))
        except OSError:
            pass

    @staticmethod
    def clear_state_files() -> None:
        """Remove stale pid/status files (on clean stop)."""
        for p in (_PID_FILE, _STATUS_FILE):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ── Lifecycle ──────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()

    def run(self) -> None:
        """Build components, install signal handlers, block until stop."""
        self.started_at = time.time()
        self._build_components()
        self._write_pid()
        self._write_status()

        # Install signal handlers for clean shutdown. Backgrounded shells
        # (cmd &) inherit SIGINT=SIG_IGN — reset it so Ctrl+C works here.
        def _shutdown(signum, frame):
            logger.info(f"Received signal {signum} — shutting down")
            self.stop()
        signal.signal(signal.SIGINT, _shutdown)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _shutdown)
            except (ValueError, OSError):
                pass  # not in main thread / platform limit

        logger.info("Friday V4 daemon running (pid %s)", os.getpid())
        try:
            while self.running:
                self._write_status()
                # Short wait: the signal handler sets _stop_event, so we
                # exit promptly instead of blocking on a long sleep.
                self._stop_event.wait(5)
        finally:
            self._shutdown_components()

    def stop(self) -> None:
        self._stop_event.set()

    def _shutdown_components(self) -> None:
        """Ordered teardown: channels → observer → engine → voice.

        Bounded: each stop is called but joins are capped (channels can
        block up to poll+1s on a slow poll); the process exits promptly
        even if a channel is mid-poll.
        """
        logger.info("Shutting down Friday V4 daemon…")
        for name, comp in (("suggestion_channel", self._suggestion_channel),
                           ("notifier", self._notifier)):
            if comp and hasattr(comp, "stop"):
                try:
                    comp.stop()
                except Exception as exc:
                    logger.debug(f"{name} stop failed: {exc}")
        if self._sampler and hasattr(self._sampler, "stop"):
            try:
                self._sampler.stop()
            except Exception:
                pass
        if self._engine and self._engine is not False:
            try:
                self._engine.stop_observer()
            except Exception as exc:
                logger.debug(f"Observer stop failed: {exc}")
            if self._owns_engine and hasattr(self._engine, "cleanup"):
                try:
                    self._engine.cleanup()
                except Exception as exc:
                    logger.debug(f"Engine cleanup failed: {exc}")
        if self._voice_pipeline and self._voice_started:
            try:
                self._voice_pipeline.stop()
            except Exception as exc:
                logger.debug(f"Voice stop failed: {exc}")
        # State files removed only after all components stopped — a
        # stopped daemon leaves no trace.
        self.clear_state_files()
        logger.info("Daemon stopped cleanly")


# ── CLI helpers ──────────────────────────────────────────────────────

def read_pid() -> Optional[int]:
    """PID from the pid file, or None."""
    try:
        if _PID_FILE.exists():
            return int(_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pass
    return None


def read_status() -> dict:
    """Last-known daemon status from the status file, or {}."""
    try:
        if _STATUS_FILE.exists():
            return json.loads(_STATUS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def is_running() -> bool:
    """True when a daemon pid file exists AND the process is alive."""
    pid = read_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
