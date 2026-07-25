"""HyprlandObserver (Pillar A — Desktop Environment Observation).

Captures the current state of the Hyprland compositor: open windows (clients),
workspaces, monitors, and the active window. These observations serve as the
ground-truth feedback loop for Hyprland Action Workers: after dispatching a
command (switch workspace, focus window, launch app), the observer re-reads
state so the action worker can verify the intended effect actually landed.

Uses `hyprctl` (Hyprland's built-in IPC) exclusively — no fragile pixel
matching, no Wayland-protocol-level hacks. Every fact is directly readable.

No LLM, no daemon. The observer is registered in the default registry and
runs as part of the ordinary ObservationEngine cycle (friday observe / daemon).

Uses shared ``HYPRCTL_PATH`` from ``hyprctl_util`` so the Observer and
Executor always use the same resolved binary path — never the bare name that
would fail in a stripped-PATH subprocess context.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation, now_iso
from ..hyprctl_util import HYPRCTL_PATH, hyprctl as _hyprctl


def _parse_kv(raw: str) -> dict[str, str]:
    """Parse hyprctl's flat key: value output into a dict."""
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def _parse_sections(raw: str) -> list[dict[str, str]]:
    """Parse hyprctl's section-delimited output (each section starts with an
    identifier like 'Window 0x...' or 'Monitor ...'). Returns a list of dicts,
    one per section."""
    sections: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if not line.startswith("\t") and ":" in line and not _is_continuation(line):
            # Start of a new section — save the previous one.
            if current:
                sections.append(current)
                current = {}
            key, _, val = line.partition(":")
            current["_section"] = key.strip()
            current[key.strip()] = val.strip()
        elif line.startswith("\t") and ":" in line:
            k, _, v = line.strip().partition(":")
            current[k.strip()] = v.strip()
    if current:
        sections.append(current)
    return sections


def _is_continuation(line: str) -> bool:
    """True if the line looks like a continuation rather than a new section."""
    return line.startswith(" ") or line.startswith("\t")


def _active_window_info() -> Optional[dict[str, str]]:
    raw = _hyprctl(["activewindow"])
    if not raw:
        return None
    return _parse_kv(raw)


def _window_count() -> int:
    raw = _hyprctl(["clients"])
    if not raw:
        return 0
    sections = _parse_sections(raw)
    # Count only mapped windows (visible ones).
    return sum(1 for s in sections if s.get("mapped") == "1")


def _workspace_list() -> list[dict[str, str]]:
    raw = _hyprctl(["workspaces"])
    if not raw:
        return []
    return _parse_sections(raw)


def _monitor_list() -> list[dict[str, str]]:
    raw = _hyprctl(["monitors"])
    if not raw:
        return []
    return _parse_sections(raw)


class HyprlandObserver(Observer):
    name = "hyprland"

    def collect(self, conn) -> list[Observation]:
        observed_at = now_iso()
        rows: list[Observation] = []

        # Active window (focused).
        aw = _active_window_info()
        if aw:
            rows.append(Observation(
                source=self.name, subject="desktop",
                aspect="active_window_title", value=aw.get("title", ""),
                confidence=Confidence.OBSERVED, observed_at=observed_at,
                scope="hyprland",
                detail=f"class={aw.get('class', '')}, pid={aw.get('pid', '')}",
            ))
            rows.append(Observation(
                source=self.name, subject="desktop",
                aspect="active_window_class", value=aw.get("class", ""),
                confidence=Confidence.OBSERVED, observed_at=observed_at,
                scope="hyprland",
            ))
            rows.append(Observation(
                source=self.name, subject="desktop",
                aspect="active_workspace", value=aw.get("workspace", "0"),
                confidence=Confidence.OBSERVED, observed_at=observed_at,
                scope="hyprland",
            ))

        # Window count.
        rows.append(Observation(
            source=self.name, subject="desktop",
            aspect="window_count", value=str(_window_count()),
            confidence=Confidence.OBSERVED, observed_at=observed_at,
            scope="hyprland",
        ))

        # Workspaces.
        for ws in _workspace_list():
            ws_id = ws.get("workspace ID") or ws.get("id", "?")
            name = ws.get("workspace name") or ws.get("name", "")
            windows = ws.get("windows", "0")
            focused = ws.get("hasfullscreen", "false")
            rows.append(Observation(
                source=self.name, subject=f"workspace:{ws_id}",
                aspect="windows", value=windows,
                confidence=Confidence.OBSERVED, observed_at=observed_at,
                scope="hyprland",
                detail=f"name={name}, fullscreen={focused}",
            ))

        # Monitors.
        for mon in _monitor_list():
            mon_name = mon.get("Monitor", "?")
            res = mon.get("resolution", "")
            ws = mon.get("active workspace", mon.get("workspace", "?"))
            rows.append(Observation(
                source=self.name, subject=f"monitor:{mon_name}",
                aspect="active_workspace", value=ws,
                confidence=Confidence.OBSERVED, observed_at=observed_at,
                scope="hyprland",
                detail=f"resolution={res}",
            ))

        if not rows:
            # Still record a heartbeat even when nothing is measurable.
            rows.append(Observation(
                source=self.name, subject="desktop",
                aspect="heartbeat", value="1",
                confidence=Confidence.DERIVED, observed_at=observed_at,
                scope="hyprland",
                detail="hyprctl responded but no desktop state observed",
            ))

        return rows

    def summarize(self, conn) -> str:
        wc = _window_count()
        aw = _active_window_info()
        ws_list = _workspace_list()
        focus = ""
        if aw:
            focus = f" — focused: {aw.get('class', '?')}"
        ws_count = len(ws_list)
        return (
            f"hyprland: {wc} window(s) across {ws_count} workspace(s)"
            f"{focus}."
        )

    def health(self, conn) -> ObserverHealth:
        # Resolve hyprctl's full PATH via the shared HYPRCTL_PATH from
        # hyprctl_util (set at module load from shutil.which). The daemon
        # fork may strip PATH, making the bare binary name unresolvable in
        # subprocess.run(). We re-check shutil.which here for health status
        # but the shared path is what both observer and executor actually use.
        if not shutil.which("hyprctl"):
            return ObserverHealth(
                False, Health.DOWN, "shutil.which",
                "hyprctl not found on PATH. HyprlandObserver disabled.",
            )
        # Binary exists on PATH — consider the observer healthy. We do NOT run
        # hyprctl --version to verify because hyprctl may require a live
        # Wayland/Hyprland session (socket) for any command, and a non-zero
        # exit from --version in a headless context is a false negative, not a
        # real health issue. The collector will report DEGRADED at observation
        # time if the socket is unreachable.
        return ObserverHealth(True, Health.HEALTHY, HYPRCTL_PATH,
                              f"hyprctl available at {HYPRCTL_PATH}")
