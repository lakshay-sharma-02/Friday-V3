"""Status panel widget — daemon state, unread counts, key metrics.

Shown alongside the feed in the dashboard.
"""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..style import Color, Style as TStyle, Icon
from .models import StatusView


class StatusPanel:
    """Quick-status summary — daemon state, unread count, key metrics."""

    def render(self, status: StatusView) -> Panel:
        """Render the status panel as a Rich Panel."""
        table = Table.grid(padding=(0, 1))

        # Daemon state indicator.
        state_mark = (
            f"[{Color.SUCCESS}]{Icon.RUNNING}[/]"
            if status.daemon_state == "running"
            else f"[{Color.ERROR}]{Icon.FAILED}[/]"
            if status.daemon_state == "crashed"
            else f"[{Color.DIM}]{Icon.PENDING}[/]"
        )

        table.add_column(style=TStyle.DIM, width=14)
        table.add_column(style=TStyle.TEXT)

        # Daemon row.
        table.add_row(
            Text("Daemon", style=TStyle.DIM),
            Text.from_markup(f"{state_mark} {status.daemon_state}"),
        )

        # Last cycle.
        if status.last_cycle_at:
            table.add_row(
                Text("Last cycle", style=TStyle.DIM),
                Text(status.last_cycle_at[11:19] if len(status.last_cycle_at) >= 19
                     else status.last_cycle_at),
            )

        # Outcome.
        outcome_color = {
            "succeeded": Color.SUCCESS,
            "failed": Color.ERROR,
            "skipped": Color.WARNING,
            "": Color.DIM,
        }.get(status.last_cycle_outcome, Color.DIM)
        table.add_row(
            Text("Outcome", style=TStyle.DIM),
            Text(status.last_cycle_outcome or "—", style=outcome_color),
        )

        # Cycles.
        table.add_row(
            Text("Cycles", style=TStyle.DIM),
            Text(str(status.cycle_count)),
        )

        # Spacer.
        table.add_row(Text(""), Text(""))

        # Unread events — detailed by priority.
        unread_strs: list[str] = []
        for pri in (3, 2, 1, 0):
            cnt = status.unread_by_priority.get(pri, 0)
            if cnt:
                mark = _PRIORITY_DOT.get(pri, "○")
                color = _PRIORITY_HEX.get(pri, Color.DIM)
                unread_strs.append(f"[{color}]{mark}[/] {cnt}")
        unread_line = "  ".join(unread_strs) if unread_strs else "0"

        table.add_row(
            Text("Unread", style=TStyle.DIM),
            Text.from_markup(unread_line),
        )

        # High-priority.
        if status.high_priority_unread:
            table.add_row(
                Text("  High pri", style=TStyle.DIM),
                Text(str(status.high_priority_unread), style=TStyle.ERROR),
            )

        # Spacer.
        table.add_row(Text(""), Text(""))

        # Repos.
        table.add_row(
            Text("Repos", style=TStyle.DIM),
            Text(str(status.repos_scanned)),
        )

        # Initiatives.
        if status.pending_initiatives:
            table.add_row(
                Text("Initiatives", style=TStyle.DIM),
                Text(str(status.pending_initiatives), style=TStyle.WARNING),
            )

        # Suggestions.
        if status.new_suggestions:
            table.add_row(
                Text("Suggestions", style=TStyle.DIM),
                Text(str(status.new_suggestions)),
            )

        # Skills.
        skill_str = str(status.active_skills) if status.active_skills else "0"
        if status.drifted_skills:
            skill_str += f"  [{Color.WARNING}]⚠ {status.drifted_skills} drift[/]"
        table.add_row(
            Text("Skills", style=TStyle.DIM),
            Text.from_markup(skill_str),
        )

        # Gaps.
        if status.open_gaps:
            table.add_row(
                Text("Gaps", style=TStyle.DIM),
                Text(str(status.open_gaps)),
            )

        # Kill switch.
        if status.kill_switch_active:
            table.add_row(
                Text(""),
                Text(""),
            )
            table.add_row(
                Text("⚠ KILL", style=TStyle.ERROR),
                Text("Active — all exec blocked", style=TStyle.ERROR),
            )

        return Panel(
            table,
            title=f"[{Color.PRIMARY}]⚡ Status[/]",
            border_style=Color.BORDER,
            style=Color.PANEL_BG,
            padding=(0, 1),
        )


_PRIORITY_DOT = {3: "●", 2: "●", 1: "●", 0: "○"}
_PRIORITY_HEX = {3: Color.ERROR, 2: Color.WARNING, 1: Color.PRIMARY, 0: Color.DIM}
