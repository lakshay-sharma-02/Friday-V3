"""ObservationEngine (Milestone 7).

Generic, deterministic engine. For one run it:

  1. iterates registered observers,
  2. collects their fresh observations,
  3. persists them (append-only, idempotent per fact),
  4. diffs each observer's current facts against its prior run,
  5. emits only the meaningful changes as Change records.

The engine knows nothing about git, terminals, or browsers. A new observer is
registered in the registry and the engine handles it unchanged. There is no
daemon, scheduler, or watcher: `run()` is invoked explicitly (e.g. by
`friday observe` / `friday observers`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..db import (
    ObservationRow,
    insert_observations,
    latest_observations,
    observation_state_as_of,
)
from .interface import Health, Observer, ObserverHealth
from .model import Change, Confidence, Observation, now_iso


@dataclass
class ObserverResult:
    name: str
    health: ObserverHealth
    observations: list[Observation] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


@dataclass
class ObservationRun:
    observed_at: str
    observers: list[ObserverResult] = field(default_factory=list)

    @property
    def all_changes(self) -> list[Change]:
        out: list[Change] = []
        for o in self.observers:
            out.extend(o.changes)
        return out

    @property
    def all_observations(self) -> list[Observation]:
        out: list[Observation] = []
        for o in self.observers:
            out.extend(o.observations)
        return out


class ObservationEngine:
    def __init__(self, registry, conn) -> None:
        self.registry = registry
        self.conn = conn

    def run(self) -> ObservationRun:
        """Execute one observation pass over all registered observers."""
        observed_at = now_iso()
        results: list[ObserverResult] = []

        self.conn.execute("BEGIN TRANSACTION")
        try:
            for observer in self.registry.all():
                health = self._safe_health(observer)
                if not health.healthy:
                    results.append(ObserverResult(observer.name, health))
                    continue
                try:
                    current = observer.collect(self.conn)
                except Exception as exc:  # observer failure must not kill the run
                    results.append(ObserverResult(
                        observer.name,
                        ObserverHealth(False, health.status, health.method,
                                        f"collect failed: {exc}"),
                    ))
                    continue
                prior = observation_state_as_of(
                    self.conn, observer.name, observed_at
                )
                changes = diff_observations(prior, current)
                insert_observations(self.conn, [o.to_row() for o in current])
                results.append(ObserverResult(
                    observer.name, health, current, changes))

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return ObservationRun(observed_at, results)

    def _safe_health(self, observer: Observer) -> ObserverHealth:
        try:
            return observer.health(self.conn)
        except Exception as exc:  # health itself failing is a degraded signal
            return ObserverHealth(False, Health.DEGRADED, "", str(exc))


def diff_observations(
    prior: list[ObservationRow], current: list[Observation]
) -> list[Change]:
    """Diff prior run rows against this run's observations, per (subject, aspect).

    Returns Change records: new facts, removed facts, and changed values.
    Inferred/derived facts carry their cause through. Unchanged facts are not
    reported. Facts present in `prior` but absent now are reported as removed.
    """
    prior_map = {(r.subject, r.aspect): r for r in prior}
    cur_map = {o.key(): o for o in current}
    changes: list[Change] = []

    for key, obs in cur_map.items():
        prev = prior_map.get(key)
        if prev is None:
            changes.append(_new_change(obs))
            continue
        if prev.value != obs.value:
            changes.append(Change(
                subject=obs.subject,
                kind=f"{obs.aspect} changed",
                old=prev.value,
                new=obs.value,
                cause=obs.cause,
                confidence=obs.confidence,
                source=obs.source,
            ))

    for key, prev in prior_map.items():
        if key not in cur_map:
            changes.append(Change(
                subject=prev.subject,
                kind=f"{prev.aspect} removed",
                old=prev.value,
                cause=None,
                confidence=Confidence.from_str(prev.confidence),
                source=prev.source,
            ))

    return changes


def _new_change(obs: Observation) -> Change:
    # A brand-new fact. For booleans/derived counts we surface the value;
    # for inferred facts the value is usually "true" so the aspect name +
    # cause carries the meaning.
    return Change(
        subject=obs.subject,
        kind=f"{obs.aspect} observed",
        new=obs.value,
        cause=obs.cause,
        confidence=obs.confidence,
        source=obs.source,
    )


def format_run(run: ObservationRun) -> str:
    """Render an ObservationRun as a plain-text report for the CLI."""
    lines = [f"Friday Observation Engine — {run.observed_at}", ""]
    for ores in run.observers:
        status = ores.health.status.value
        lines.append(f"[{ores.name}] {status}")
        if not ores.health.healthy:
            if ores.health.detail:
                lines.append(f"    ! {ores.health.detail}")
            if not ores.changes:
                lines.append("    (no changes)")
            continue
        if not ores.changes:
            lines.append("    (no changes)")
        for ch in ores.changes:
            lines.append(f"    • {_render_change(ch)}")
    return "\n".join(lines) + "\n"


def _render_change(ch: Change) -> str:
    if ch.kind.endswith(" observed"):
        aspect = ch.kind[: -len(" observed")]
        return f"{ch.subject} {aspect}: {ch.new}"
    if ch.kind.endswith(" removed"):
        aspect = ch.kind[: -len(" removed")]
        return f"{ch.subject} {aspect} removed (was {ch.old})"
    parts = [f"{ch.subject} {ch.kind}"]
    if ch.old is not None and ch.new is not None:
        parts.append(f"({ch.old} -> {ch.new})")
    elif ch.new is not None:
        parts.append(f"({ch.new})")
    if ch.cause:
        parts.append(f"because {ch.cause}")
    return " ".join(parts) + "."
