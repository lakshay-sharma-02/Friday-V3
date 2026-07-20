"""Vertical phase progress indicator."""
from __future__ import annotations
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ..models import MissionPhase
from ..style import Color, Style, Icon


_PHASE_ORDER = [
    MissionPhase.PLANNING,
    MissionPhase.DISCOVERY,
    MissionPhase.ANALYSIS,
    MissionPhase.IMPLEMENTATION,
    MissionPhase.VERIFICATION,
    MissionPhase.SUMMARY,
    MissionPhase.COMPLETE,
]


class MissionGraphWidget:
    """Phase-by-phase progress: current highlighted, completed marked, future dim."""

    def render(self, current_phase: MissionPhase) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column("", width=2)
        table.add_column("Phase", width=24)
        current_idx = _PHASE_ORDER.index(current_phase) if current_phase in _PHASE_ORDER else -1

        for i, phase in enumerate(_PHASE_ORDER):
            label = phase.value.replace("_", " ").title()
            if i < current_idx:
                ic = Icon.COMPLETED
                st = Style.SUCCESS
            elif i == current_idx:
                ic = Icon.RUNNING
                st = Style.MISSION_ID
            else:
                ic = Icon.PENDING
                st = Style.DIM
            table.add_row(ic, Text(label, style=st))

        return Panel(table, title="Mission Graph", style=Color.PANEL_BG,
                     border_style=Color.BORDER)
