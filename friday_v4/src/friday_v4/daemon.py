"""DaemonService — the persistent ambient FRIDAY.

Ties the Phase 1/2/3/4 pieces into one process:

    AnticipationEngine (shared, warm)
      ├── start_observer()      # desktop watcher → pattern learner
      ├── ProactiveSuggestionChannel   (suggestions → desktop notify)
      ├── DesktopNotificationChannel   (V3 ambient → desktop notify)
      ├── IntelligenceSampler          (drift/anomaly samples)
      ├── SecurityScanner              (periodic Wave 3 security scan)
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("friday_v4.daemon")

_STATUS_FILE = Path.home() / ".friday" / "v4_daemon.status"
_PID_FILE = Path.home() / ".friday" / "v4_daemon.pid"

#: Cap on persisted notified-finding ids (bounds the state file size;
#: ids beyond this are dropped oldest-first).
_MAX_NOTIFIED_IDS = 5000


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
    security_scan: bool = True      # periodic Wave 3 security scan
    security_interval: float = 3600.0   # seconds between security scans
    security_threshold: str = "medium" # findings at/above this are kept
    security_notify_threshold: str = "high"  # only notify at/above this
    security_path: str = "."       # project path to scan (default: cwd)
    memory_sweep: bool = True       # periodic Wave 10 memory decay sweep
    memory_interval: float = 3600.0 # seconds between decay sweeps
    skill_learn: bool = True        # periodic Wave 10 skill learning sweep
    skill_interval: float = 3600.0  # seconds between skill sweeps
    relationship_refresh: bool = True  # periodic depth recompute
    relationship_interval: float = 3600.0  # seconds between refreshes
    dispatch_offer: bool = True     # periodic Wave 14 dispatch offers
    dispatch_interval: float = 3600.0  # seconds between dispatch offer checks
    mobile_push: bool = True        # periodic Wave 15 mobile push drain
    mobile_push_interval: float = 60.0  # seconds between push passes
    mobile_push_priority: int = 0   # only push events at/above this (0 = all)
    mobile_push_hook: Optional[str] = None  # operator hook: notification JSON
                                  # piped to this shell command's stdin
    mobile_push_file: Optional[str] = None  # JSONL outbox path (alt. hook)
    autonomy: bool = True           # the agent loop — Friday acts by itself
    autonomy_interval: float = 300.0   # seconds between judgment cycles
    autonomy_asks: int = 3          # max permission requests per cycle
    autonomy_idle_seconds: float = 300.0  # busy gate: don't ask CONFIRM work
                              # while the operator has been active recently
    autonomy_learn: bool = True     # self-learn: offer repeated patterns as
                              # skills + missions (the "I noticed you…" ask)
    autonomy_promote: bool = True   # self-develop: offer verified-skill
                              # promotion as a normal durable ask


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


class MemorySweeper:
    """Periodically runs the Wave 10 memory decay sweep.

    The ``memory/`` layer's ``MemoryStore.decay()`` fades unused facts
    and forgets stale ones below the confidence floor — but it only
    helps if something *runs* it. This daemon component owns that
    schedule so long-term memory stays bounded without operator
    intervention (wave-10 doc: "decay_memories runs on a schedule").

    Same never-crash contract as the other daemon components: any
    failure logs and the sweep is skipped; ``db.connect`` uses the
    caller-provided path (default: the V4 state DB) so tests stay
    hermetic with a tmp_path DB.
    """

    def __init__(self, interval: float = 3600.0,
                 db_path=None,
                 decay_kwargs=None):
        self.interval = interval
        self._db_path = db_path
        self._decay_kwargs = decay_kwargs or {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._sweeps = 0
        self.last_report: Optional[dict] = None
        self.last_error: Optional[str] = None

    def sweep_once(self) -> int:
        """Run one decay pass; returns facts decayed+removed (0 on failure)."""
        try:
            from . import db
            from .memory import MemoryStore
            conn = db.connect(path=self._db_path)
            try:
                report = MemoryStore(conn).decay(**self._decay_kwargs)
                self._sweeps += 1
                self.last_report = {
                    "decayed": report.decayed,
                    "removed": report.removed,
                    "total": report.total,
                }
                self.last_error = None
                return report.decayed + report.removed
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"Memory sweep failed: {exc}")
            self.last_error = str(exc)
            return 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-memory", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First sweep shortly after start so memory is bounded fast.
        try:
            self.sweep_once()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.sweep_once()
            except Exception:
                logger.debug("Memory sweep error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


class SkillLearner:
    """Periodically runs the Wave 10 skills shadow-first sweep.

    The ``skills/`` layer's ``ReplayExecutor.learn()`` only forms shadow
    skills when something *runs* it, and ``ShadowExecutor.sweep()`` only
    accumulates shadow matches when it observes real activity. This daemon
    component owns that schedule: every interval it replays the audit log
    for repeated patterns and sweeps shadow matches — so skills form and
    verify without operator intervention.

    Safety law (wave-10 §3.4): the sweep NEVER executes anything. It only
    reads the audit log and bumps shadow-match counters. Promotion still
    requires the operator's explicit approval (``friday4 skills promote``).

    Same never-crash contract as the other daemon components: any failure
    logs and is skipped; ``db_path`` is injectable so tests stay hermetic.
    """

    def __init__(self, interval: float = 3600.0,
                 db_path=None,
                 min_occurrences: int = 2,
                 learn_prefix: str = "learned"):
        self.interval = interval
        self._db_path = db_path
        self._min_occurrences = min_occurrences
        self._learn_prefix = learn_prefix
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._sweeps = 0
        self.last_report: Optional[dict] = None
        self.last_error: Optional[str] = None

    def sweep_once(self) -> int:
        """One pass: learn + shadow-sweep + surface 'noticed' offers.

        Returns the number of shadow matches recorded this pass (0 on any
        failure). Never executes anything. Wave 14: the RepetitionNoticer
        runs here too and its offers are surfaced in ``last_report`` so
        the daemon's status/briefing can say "I noticed you do this every
        time…" before the operator asks.
        """
        try:
            from . import db
            from .skills import ReplayExecutor, ShadowExecutor
            from .skills.noticer import RepetitionNoticer
            conn = db.connect(path=self._db_path)
            try:
                # Noticer first: offers reflect patterns not yet covered
                # by a skill, BEFORE this pass forms them (Wave 14 — the
                # "I noticed you do this every time" moment is surfaced,
                # not silently pre-empted by learn()).
                offers = RepetitionNoticer(
                    conn, min_occurrences=self._min_occurrences,
                ).notice(limit=3)
                formed = ReplayExecutor(
                    conn, min_occurrences=self._min_occurrences,
                ).learn(prefix=self._learn_prefix)
                matches = ShadowExecutor(conn).sweep()
                self._sweeps += 1
                self.last_report = {
                    "formed": len(formed),
                    "shadow_matches": len(matches),
                    "names": [s.name for s in formed],
                    "offers": len(offers),
                    "offer_lines": [o.get("offer", "") for o in offers],
                }
                self.last_error = None
                return len(matches)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"Skill sweep failed: {exc}")
            self.last_error = str(exc)
            return 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-skills", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First sweep shortly after start so skills form fast.
        try:
            self.sweep_once()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.sweep_once()
            except Exception:
                logger.debug("Skill sweep error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


class DispatchOfferer:
    """Periodically offers dispatch suggestions when context matches (Wave 14).

    The skills layer's ``SkillDispatcher`` computes next-step suggestions
    on demand; this daemon component owns the *offer* schedule — every
    interval it re-checks the current context and, when a promoted skill
    matches and the offer hasn't been surfaced recently, raises a
    desktop notification and publishes a durable ``dispatch`` ambient
    event. The operator's "yes, run it" then flows back through the ONE
    NLU point (``Intent.ACCEPT``) → the gate → execution (and, for
    multi-step skills, a supervised mission).

    Safety law: this component NEVER executes anything. It only reads
    the audit trail for a context match and raises an offer.

    Same never-crash contract as the other daemon components: any
    failure logs and is skipped; ``db_path``, ``notify`` and ``bus`` are
    injectable so tests stay hermetic.
    """

    def __init__(self, interval: float = 3600.0,
                 db_path=None,
                 notify=None,
                 bus=None,
                 offer_cap: int = 32):
        self.interval = interval
        self._db_path = db_path
        self._notify = notify  # injectable notifier (tests); default desktop
        self._bus = bus        # shared ambient bus (wired by the daemon)
        self._offer_cap = offer_cap
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._checks = 0
        # Bounded dedupe of offered keys (in-memory, daemon-lifetime). A
        # daemon restart may re-offer a skill once — unlike the security
        # scanner's persisted notified-ids, context usually moves on, so a
        # single repeat is acceptable and keeps this component stateless.
        self._offered: list[str] = []
        self.last_report: Optional[dict] = None
        self.last_error: Optional[str] = None

    def offer_once(self) -> int:
        """One offer pass; returns offers raised (0 on none/failure).

        Reads the audit trail via ``SkillDispatcher`` — never executes.
        A suggestion already offered recently (same skill + first
        command) is not re-surfaced until the context moves on.
        """
        try:
            from . import db
            from .skills import SkillDispatcher
            conn = db.connect(path=self._db_path)
            try:
                dispatcher = SkillDispatcher(conn)
                suggestions = dispatcher.suggest(limit=1)
                self._checks += 1
                if not suggestions:
                    self.last_report = {"offers": 0, "checked": True}
                    self.last_error = None
                    return 0
                s = suggestions[0]
                first = (s.get("next_steps") or [{}])[0]
                key = f"{s.get('skill_id')}:{first.get('command')}"
                if key in self._offered:
                    self.last_report = {"offers": 0, "deduped": True}
                    return 0
                self._offered.append(key)
                if len(self._offered) > self._offer_cap:
                    self._offered = self._offered[-self._offer_cap:]
                offer = dispatcher.prompt(s)
                self._raise_offer(offer)
                self.last_report = {
                    "offers": 1,
                    "skill": s.get("skill_name"),
                    "next": first.get("command", ""),
                    "offer": offer,
                }
                self.last_error = None
                return 1
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"Dispatch offer failed: {exc}")
            self.last_error = str(exc)
            return 0

    def _raise_offer(self, offer: str) -> None:
        """Notify + publish the offer (both guarded, never raise)."""
        try:
            notifier = self._notify or self._default_notify
            notifier("Friday · Suggestion", f"Skill offer: {offer}",
                     urgency="normal", timeout_ms=15000)
        except Exception as exc:
            logger.debug(f"Dispatch notification failed: {exc}")
        if self._bus is None:
            return
        try:
            from .ambient import Event, Priority
            self._bus.publish(Event(
                topic="dispatch",
                payload=offer,
                priority=Priority.IMPORTANT,
                source="daemon.dispatch"))
        except Exception as exc:
            logger.debug(f"Ambient dispatch publish failed: {exc}")

    def _default_notify(self, title: str, message: str,
                        urgency: str = "normal",
                        timeout_ms: Optional[int] = None) -> bool:
        try:
            from .desktop.wm_abstraction import DesktopAbstraction
            return DesktopAbstraction.notify(title, message, urgency=urgency,
                                             timeout_ms=timeout_ms)
        except Exception as exc:
            logger.debug(f"Dispatch notification failed: {exc}")
            return False

    @staticmethod
    def _default_notify_static(title: str, message: str,
                               urgency: str = "normal",
                               timeout_ms: Optional[int] = None) -> bool:
        """Static desktop notify (usable before the daemon is built)."""
        try:
            from .desktop.wm_abstraction import DesktopAbstraction
            return DesktopAbstraction.notify(title, message, urgency=urgency,
                                             timeout_ms=timeout_ms)
        except Exception as exc:
            logger.debug(f"Desktop notification failed: {exc}")
            return False

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-dispatch", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First check shortly after start so an offer surfaces fast.
        try:
            self.offer_once()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.offer_once()
            except Exception:
                logger.debug("Dispatch offer error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


class MobilePushWorker:
    """Periodically drains the durable ambient queue to the phone (Wave 15).

    The mobile transport (``mobile/push.PushNotificationService``)
    replays durable ambient events to a transporter (a companion app's
    push endpoint) — but it only helps if something *runs* it. This
    daemon component owns that schedule, so the phone gets pushed
    without manual ``friday4 mobile push``.

    Design:
    - Runs ``PushNotificationService.poll_once()`` on ``interval``; the
      service's persisted rowid cursor means a restart delivers exactly
      what the phone missed (miss nothing, re-deliver nothing).
    - ``transporter`` / ``min_priority`` / ``state_file`` / ``db_path``
      are injectable so tests stay hermetic (defaults: the service's own
      log transporter, all priorities, the shared push state file).
    - Same never-crash contract as the other daemon components: any
      failure logs and the pass is skipped; ``last_report``/
      ``last_error`` surface the outcome for status.
    """

    def __init__(self, interval: float = 60.0,
                 db_path=None,
                 transporter=None,
                 min_priority: int = 0,
                 state_file: Optional[Path] = None,
                 hook: Optional[str] = None,
                 file_path: Optional[str] = None):
        self.interval = interval
        self._db_path = db_path
        self._transporter = transporter
        self.min_priority = min_priority
        self._state_file = state_file
        #: Operator-configurable destination (Wave 15): an explicit
        #: ``transporter`` wins; else ``hook`` (shell command, JSON on
        #: stdin); else ``file_path`` (JSONL outbox); else the service's
        #: default logger transporter.
        self._hook = hook
        self._file_path = file_path
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._passes = 0
        self.last_report: Optional[dict] = None
        self.last_error: Optional[str] = None

    def poll_once(self) -> int:
        """One drain pass; returns events delivered (0 on none/failure)."""
        try:
            from .mobile import (PushNotificationService, command_transporter,
                                 file_transporter)
            transporter = self._transporter
            if transporter is None and self._hook:
                transporter = command_transporter(self._hook)
            if transporter is None and self._file_path:
                transporter = file_transporter(Path(self._file_path))
            service = PushNotificationService(
                db_path=self._db_path,
                transporter=transporter,
                min_priority=self.min_priority,
                state_file=self._state_file,
            )
            delivered = service.poll_once()
            self._passes += 1
            self.last_report = {
                "delivered": delivered,
                "cursor": service.cursor,
                "delivered_total": service.delivered_total,
            }
            self.last_error = None
            return delivered
        except Exception as exc:
            logger.debug(f"Mobile push failed: {exc}")
            self.last_error = str(exc)
            return 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-mobile", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First pass shortly after start so the phone gets caught up fast.
        try:
            self.poll_once()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.poll_once()
            except Exception:
                logger.debug("Mobile push error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


class RelationshipRefresher:
    """Periodically recomputes relationship depth from real data (Wave 10 §3.3).

    Depth is derived from cumulative interaction signals (exchanges,
    sessions, missions, facts) — a schedule keeps the stored depth fresh
    as the operator works with Friday across surfaces. Monotonic by
    design: recompute never drops the depth.
    """

    def __init__(self, interval: float = 3600.0, db_path=None):
        self.interval = interval
        self._db_path = db_path
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._refreshes = 0
        self.last_report: Optional[dict] = None
        self.last_error: Optional[str] = None

    def refresh_once(self) -> Optional[dict]:
        """One depth recompute + persist; returns the status (None on fail)."""
        try:
            from . import db
            from .relationship import RelationshipEngine
            conn = db.connect(path=self._db_path)
            try:
                status = RelationshipEngine(conn).refresh()
                self._refreshes += 1
                self.last_report = status
                self.last_error = None
                return status
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"Relationship refresh failed: {exc}")
            self.last_error = str(exc)
            return None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-relationship", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            self.refresh_once()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.refresh_once()
            except Exception:
                logger.debug("Relationship refresh error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval + 1, 3))
            self._thread = None


class AmbientWorker:
    """Wave 11 — connects the daemon to the ambient bus (push replaces poll).

    Owns the bus (a per-daemon AmbientBus with the V4 DB connection) so
    daemon components (security scanner, suggestion channels) publish
    durable events without owning a connection. A light sweep publishes
    the daily briefing topic so the morning/evening surface has real
    state behind it (Wave 11 §3.3 — briefings from real V4 state).
    """

    def __init__(self, db_path=None, briefing_interval: float = 3600.0):
        self.db_path = db_path
        self.briefing_interval = briefing_interval
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._bus = None
        self._conn = None
        self.last_error: Optional[str] = None

    def bus(self):
        """The shared AmbientBus (lazily built — never raises)."""
        if self._bus is None:
            try:
                from . import db
                from .ambient import AmbientBus
                self._conn = db.connect(path=self.db_path)
                self._bus = AmbientBus(self._conn)
            except Exception as exc:
                logger.debug(f"ambient bus unavailable: {exc}")
                self._bus = False
        return self._bus if self._bus is not False else None

    def wire_channels(self, speak_fn=None, notify_fn=None) -> None:
        """Subscribe live surface channels to the shared bus (Wave 15).

        Push reaches every surface: when the voice pipeline is running
        and/or a desktop notifier is available, CRITICAL events are
        *spoken* and IMPORTANT+ events pop a banner — the same events
        the web SSE stream and the mobile push transport deliver.
        Wildcard subscribe ("*") so surfaces need not enumerate topics.
        Never raises — a missing bus/channel degrades silently.
        """
        bus = self.bus()
        if bus is None:
            return
        try:
            from .ambient.channels import desktop_channel, speak_channel
            from .ambient.bus import Priority
            if speak_fn is not None:
                bus.subscribe("*", speak_channel(speak_fn, Priority.CRITICAL))
            if notify_fn is not None:
                bus.subscribe("*", desktop_channel(notify_fn,
                                                    Priority.IMPORTANT))
        except Exception as exc:
            logger.debug(f"ambient channel wiring failed: {exc}")

    def publish_briefing(self) -> None:
        """Publish a morning/evening briefing as a durable ambient event."""
        bus = self.bus()
        if bus is None:
            return
        try:
            from .briefing import build_briefing
            kind = "evening" if _now_hour() >= 17 else "morning"
            conn = self._conn
            b = build_briefing(conn, kind=kind)
            from .ambient import Event, Priority
            bus.publish(Event(topic="briefing", payload=b.text,
                              priority=Priority.ROUTINE, source="daemon"))
        except Exception as exc:
            logger.debug(f"ambient briefing failed: {exc}")
            self.last_error = str(exc)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-ambient", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            self.publish_briefing()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.briefing_interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.publish_briefing()
            except Exception:
                logger.debug("Ambient briefing error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.briefing_interval + 1, 3))
            self._thread = None
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _now_hour() -> int:
    """Local hour 0-23 (briefing kind selection)."""
    try:
        from datetime import datetime
        return datetime.now().hour
    except Exception:
        return 8


class SecurityScanner:
    """Periodically runs the Wave 3 security scan on a project path.

    Uses ``VulnerabilityScanner.scan_quick`` (built-in checks + any
    installed tools) and raises a desktop notification for each finding
    at or above ``notify_threshold`` — deduplicated by finding id so a
    finding is surfaced once, and the ids are persisted in the state
    file so a daemon restart does not re-notify previously surfaced
    criticals.

    State is persisted to ``~/.friday/v4_security_last.json`` so
    ``friday4 doctor`` / ``friday4 status`` can report the last scan
    even after the daemon stops. Never raises: any failure logs and is
    skipped (never crash the daemon).
    """

    def __init__(self, interval: float = 3600.0, path: str = ".",
                 threshold: str = "medium",
                 notify_threshold: str = "high",
                 notify=None, state_file: Optional[Path] = None,
                 notify_timeout_ms: int = 15000,
                 bus=None):
        self.interval = interval
        self.path = path
        self.threshold = threshold
        self.notify_threshold = notify_threshold
        self.notify_timeout_ms = notify_timeout_ms
        self._notify = notify  # injectable notifier (tests); default desktop
        self._state_file = state_file or Path.home() / ".friday" / "v4_security_last.json"
        #: Shared Wave 11 ambient bus (wired by the daemon). When set,
        #: findings publish durably through it; a standalone scanner
        #: (web /api/scan) falls back to an in-memory bus.
        self._bus = bus
        self.running = False
        self._thread: Optional[threading.Thread] = None
        persisted = self._load_state()
        try:
            self._scans = int(persisted.get("scans", 0))
        except (TypeError, ValueError):
            self._scans = 0  # guard a valid-JSON but malformed value
        # Ordered list (oldest first): restored from the state file so a
        # restart doesn't re-notify already-surfaced findings; trimmed
        # on load to mirror the save-side cap.
        self._notified_ids: list[str] = list(
            persisted.get("notified_ids", []))[-_MAX_NOTIFIED_IDS:]
        self.last_report: Optional[dict] = None
        self.last_error: Optional[str] = None

    # ── Notification ───────────────────────────────────────────────

    def _publish_ambient(self, finding) -> None:
        """Publish the finding onto the Wave 11 ambient bus (durable).

        Uses the daemon-wired shared bus when available (durable queue + 
        in-process subscribers); a standalone scanner falls back to an
        in-memory bus. Never raises — a missing bus degrades to a log.
        """
        try:
            from .ambient import AmbientBus, Event, Priority
            bus = self._bus if self._bus is not None else AmbientBus()
            loc = finding.file or finding.package or "?"
            bus.publish(Event(
                topic="security",
                payload=f"{finding.severity.upper()} — {loc} · "
                        f"{(finding.detail or '')[:120]}",
                priority=Priority.IMPORTANT,
                source="daemon.security"))
        except Exception as exc:
            logger.debug(f"ambient publish failed: {exc}")

    def _default_notify(self, title: str, message: str,
                        urgency: str = "normal",
                        timeout_ms: Optional[int] = None) -> bool:
        try:
            from .desktop.wm_abstraction import DesktopAbstraction
            return DesktopAbstraction.notify(title, message, urgency=urgency,
                                             timeout_ms=timeout_ms)
        except Exception as exc:
            logger.debug(f"Security notification failed: {exc}")
            return False

    def _raise_notification(self, finding) -> None:
        title = f"Friday · Security: {finding.title}"
        if finding.cve:
            title += f" [{finding.cve}]"
        loc = finding.file or finding.package
        message = (f"{finding.severity.upper()} — {loc} · "
                   f"{finding.detail[:120]}")
        notifier = self._notify or self._default_notify
        try:
            # Normal urgency + explicit timeout so the banner auto-dismisses
            # (critical banners are persistent on GNOME and never fade). The
            # severity is already carried in the message text.
            notifier(title, message, urgency="normal",
                     timeout_ms=self.notify_timeout_ms)
        except Exception as exc:
            logger.debug(f"Security notification failed: {exc}")

    # ── Scan ───────────────────────────────────────────────────────

    def scan_once(self) -> int:
        """Run one security scan; notify new high-severity findings.

        Returns the number of findings newly notified this run (0 when
        everything was already seen or on any failure).
        """
        try:
            from .security.reporter import SEVERITY_ORDER
            from .security.scanner import VulnerabilityScanner
            report = VulnerabilityScanner().scan_quick(self.path,
                                                       threshold=self.threshold)
            if self.notify_threshold not in SEVERITY_ORDER:
                self.notify_threshold = "high"  # defensive fallback
            actionable = [f for f in report.findings
                          if f.severity_rank <= SEVERITY_ORDER.index(
                              self.notify_threshold)
                          and f.id not in self._notified_ids]
            for finding in actionable:
                self._notified_ids.append(finding.id)
                self._raise_notification(finding)
                self._publish_ambient(finding)
            if len(self._notified_ids) > _MAX_NOTIFIED_IDS:
                self._notified_ids = self._notified_ids[-_MAX_NOTIFIED_IDS:]
            self._scans += 1
            self.last_report = report.to_dict()
            self.last_error = None
            self._save_state()
            return len(actionable)
        except Exception as exc:
            logger.debug(f"Security scan failed: {exc}")
            self.last_error = str(exc)
            return 0

    # ── State persistence ──────────────────────────────────────────

    def _load_state(self) -> dict:
        """Read previously persisted scanner state ({} on any problem)."""
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({
                "scans": self._scans,
                "last_error": self.last_error,
                "report": self.last_report,
                "notified_ids": self._notified_ids,
            }, indent=2, default=str))
        except OSError as exc:
            logger.debug(f"Security state write failed: {exc}")

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="friday-security", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First scan shortly after start so results appear fast.
        try:
            self.scan_once()
        except Exception:
            pass
        while self.running:
            for _ in range(max(int(self.interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
            try:
                self.scan_once()
            except Exception:
                logger.debug("Security scan error", exc_info=True)

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            # Bounded: a scan_quick pass can be slow (pip-audit/secrets
            # on a big repo) and is uninterruptible, so don't block
            # shutdown on it — cap the join and let the daemon exit
            # (the thread is daemon=True).
            self._thread.join(timeout=5)
            self._thread = None


class DaemonService:
    """The ambient FRIDAY process — wires all Phase 1/2/3/4 components."""

    def __init__(self, config: Optional[DaemonConfig] = None,
                 engine: Optional[Any] = None,
                 voice_pipeline: Optional[Any] = None,
                 notifier: Optional[Any] = None,
                 suggestion_channel: Optional[Any] = None,
                 sampler: Optional[Any] = None,
                 security_scanner: Optional[Any] = None,
                 memory_sweeper: Optional[Any] = None,
                 skill_learner: Optional[Any] = None,
                 relationship_refresher: Optional[Any] = None,
                 ambient_worker: Optional[Any] = None,
                 dispatch_offerer: Optional[Any] = None,
                 mobile_push_worker: Optional[Any] = None,
                 autonomy_agent: Optional[Any] = None,
                 db_path=None):
        self.config = config or DaemonConfig()
        self._db_path = db_path
        self._engine = engine
        self._voice_pipeline = voice_pipeline
        self._notifier = notifier
        self._suggestion_channel = suggestion_channel
        self._sampler = sampler
        self._security_scanner = security_scanner
        self._memory_sweeper = memory_sweeper
        self._skill_learner = skill_learner
        self._relationship_refresher = relationship_refresher
        self._ambient_worker = ambient_worker
        self._dispatch_offerer = dispatch_offerer
        self._mobile_push_worker = mobile_push_worker
        self._autonomy_agent = autonomy_agent
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
                # The engine gets the state DB so its desktop observer
                # persists app-switch events — "watch me" can then
                # capture app opens as skill steps, with no CLI involved
                # (the always-on presence records everything itself).
                # ``db_path`` defaults to the V4 state DB path (None =
                # caller default) so production records into
                # ~/.friday/v4.db and tests stay hermetic.
                engine_db = self._db_path
                if engine_db is None:
                    try:
                        from . import db as _db
                        engine_db = _db.default_db_path()
                    except Exception:
                        engine_db = None
                self._engine = AnticipationEngine(db_path=engine_db)
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

        # Ambient worker first: its shared bus is the push transport every
        # publisher below (suggestions, security findings) uses (Wave 11 —
        # push replaces polling).
        if self._ambient_worker is None:
            self._ambient_worker = AmbientWorker()
        try:
            self._ambient_worker.start()
        except Exception as exc:
            logger.warning(f"Ambient worker start failed: {exc}")
        shared_bus = None
        try:
            if self._ambient_worker:
                shared_bus = self._ambient_worker.bus()
        except Exception:
            shared_bus = None

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
                        poll_interval=self.config.suggestion_interval,
                        bus=shared_bus,
                    )
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

        if self.config.security_scan:
            if self._security_scanner is None:
                try:
                    self._security_scanner = SecurityScanner(
                        interval=self.config.security_interval,
                        path=self.config.security_path,
                        threshold=self.config.security_threshold,
                        notify_threshold=self.config.security_notify_threshold,
                        bus=shared_bus,
                    )
                except Exception as exc:
                    logger.warning(f"Security scanner unavailable: {exc}")
                    self._security_scanner = False
            if self._security_scanner:
                try:
                    self._security_scanner.start()
                except Exception as exc:
                    logger.warning(f"Security scanner start failed: {exc}")

        if self.config.memory_sweep:
            if self._memory_sweeper is None:
                try:
                    self._memory_sweeper = MemorySweeper(
                        interval=self.config.memory_interval)
                except Exception as exc:
                    logger.warning(f"Memory sweeper unavailable: {exc}")
                    self._memory_sweeper = False
            if self._memory_sweeper:
                try:
                    self._memory_sweeper.start()
                except Exception as exc:
                    logger.warning(f"Memory sweeper start failed: {exc}")

        if self.config.skill_learn:
            if self._skill_learner is None:
                try:
                    self._skill_learner = SkillLearner(
                        interval=self.config.skill_interval)
                except Exception as exc:
                    logger.warning(f"Skill learner unavailable: {exc}")
                    self._skill_learner = False
            if self._skill_learner:
                try:
                    self._skill_learner.start()
                except Exception as exc:
                    logger.warning(f"Skill learner start failed: {exc}")

        if self.config.relationship_refresh:
            if self._relationship_refresher is None:
                try:
                    self._relationship_refresher = RelationshipRefresher(
                        interval=self.config.relationship_interval)
                except Exception as exc:
                    logger.warning(f"Relationship refresher unavailable: {exc}")
                    self._relationship_refresher = False
            if self._relationship_refresher:
                try:
                    self._relationship_refresher.start()
                except Exception as exc:
                    logger.warning(f"Relationship refresher start failed: {exc}")

        if self.config.dispatch_offer:
            if self._dispatch_offerer is None:
                try:
                    self._dispatch_offerer = DispatchOfferer(
                        interval=self.config.dispatch_interval,
                        bus=shared_bus,
                    )
                except Exception as exc:
                    logger.warning(f"Dispatch offerer unavailable: {exc}")
                    self._dispatch_offerer = False
            if self._dispatch_offerer:
                try:
                    self._dispatch_offerer.start()
                except Exception as exc:
                    logger.warning(f"Dispatch offerer start failed: {exc}")

        if self.config.mobile_push:
            if self._mobile_push_worker is None:
                try:
                    self._mobile_push_worker = MobilePushWorker(
                        interval=self.config.mobile_push_interval,
                        min_priority=self.config.mobile_push_priority,
                        db_path=self._db_path,
                        hook=self.config.mobile_push_hook,
                        file_path=self.config.mobile_push_file,
                    )
                except Exception as exc:
                    logger.warning(f"Mobile push worker unavailable: {exc}")
                    self._mobile_push_worker = False
            if self._mobile_push_worker:
                try:
                    self._mobile_push_worker.start()
                except Exception as exc:
                    logger.warning(f"Mobile push worker start failed: {exc}")

        if self.config.autonomy:
            if self._autonomy_agent is None:
                try:
                    from .autonomy import AutonomyAgent
                    self._autonomy_agent = AutonomyAgent(
                        interval=self.config.autonomy_interval,
                        max_asks=self.config.autonomy_asks,
                        idle_seconds=self.config.autonomy_idle_seconds,
                        learn=self.config.autonomy_learn,
                        promote=self.config.autonomy_promote,
                        db_path=self._db_path,
                        bus=shared_bus,
                    )
                except Exception as exc:
                    logger.warning(f"Autonomy agent unavailable: {exc}")
                    self._autonomy_agent = False
            if self._autonomy_agent:
                try:
                    self._autonomy_agent.start()
                except Exception as exc:
                    logger.warning(f"Autonomy agent start failed: {exc}")

        if self.config.voice:
            self._start_voice()

        # Wave 15 — push reaches every surface: subscribe the live voice
        # pipeline (speaks CRITICAL events) and the desktop notifier
        # (banners for IMPORTANT+) to the shared bus. Both optional;
        # the web SSE stream and mobile push already read the durable
        # queue directly.
        try:
            if self._ambient_worker is not None:
                speak_fn = None
                if (self._voice_pipeline and self._voice_started
                        and hasattr(self._voice_pipeline, "speak")):
                    speak_fn = self._voice_pipeline.speak
                notify_fn = None
                if self.config.notifications and self._notifier is not False:
                    notify_fn = DaemonService._default_notify_static
                self._ambient_worker.wire_channels(speak_fn=speak_fn,
                                                   notify_fn=notify_fn)
        except Exception as exc:
            logger.debug(f"surface channel wiring failed: {exc}")

    def _start_voice(self) -> None:
        if self._voice_pipeline is None:
            try:
                from .voice.core import config_from_file
                from .voice.pipeline import VoicePipeline
                from .voice.router import VoiceRouter
                self._voice_pipeline = VoicePipeline(config_from_file())
            except Exception as exc:
                logger.warning(f"Voice pipeline unavailable: {exc}")
                self._voice_pipeline = False
        if not self._voice_pipeline:
            return
        try:
            # Wire the conversation log so spoken utterances persist
            # (Wiring Law: voice is a first-class entrypoint into the
            # brain — persona identity + conversation providers read it).
            voice_conn = None
            try:
                from . import db
                voice_conn = db.connect()
            except Exception:
                voice_conn = None
            self._voice_conn = voice_conn
            router = VoiceRouter(self._voice_pipeline, enable_proactive=True,
                                 conn=voice_conn)
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
            "security": bool(self._security_scanner
                             and getattr(self._security_scanner, "running", False)),
            "memory": bool(self._memory_sweeper
                           and getattr(self._memory_sweeper, "running", False)),
            "skills": bool(self._skill_learner
                           and getattr(self._skill_learner, "running", False)),
            "relationship": bool(self._relationship_refresher
                                 and getattr(self._relationship_refresher,
                                             "running", False)),
            "dispatch": bool(self._dispatch_offerer
                             and getattr(self._dispatch_offerer, "running", False)),
            "mobile": bool(self._mobile_push_worker
                           and getattr(self._mobile_push_worker, "running", False)),
            "autonomy": bool(self._autonomy_agent
                             and getattr(self._autonomy_agent, "running", False)),
            "ambient": bool(self._ambient_worker
                            and getattr(self._ambient_worker, "running", False)),
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
        if self._security_scanner and hasattr(self._security_scanner, "stop"):
            try:
                self._security_scanner.stop()
            except Exception:
                pass
        if self._memory_sweeper and hasattr(self._memory_sweeper, "stop"):
            try:
                self._memory_sweeper.stop()
            except Exception:
                pass
        if self._skill_learner and hasattr(self._skill_learner, "stop"):
            try:
                self._skill_learner.stop()
            except Exception:
                pass
        if self._relationship_refresher and hasattr(
                self._relationship_refresher, "stop"):
            try:
                self._relationship_refresher.stop()
            except Exception:
                pass
        if self._dispatch_offerer and hasattr(self._dispatch_offerer, "stop"):
            try:
                self._dispatch_offerer.stop()
            except Exception:
                pass
        if self._mobile_push_worker and hasattr(self._mobile_push_worker, "stop"):
            try:
                self._mobile_push_worker.stop()
            except Exception:
                pass
        if self._autonomy_agent and hasattr(self._autonomy_agent, "stop"):
            try:
                self._autonomy_agent.stop()
            except Exception:
                pass
        if self._ambient_worker and hasattr(self._ambient_worker, "stop"):
            try:
                self._ambient_worker.stop()
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
        if getattr(self, "_voice_conn", None) is not None:
            try:
                self._voice_conn.close()
            except Exception:
                pass
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
