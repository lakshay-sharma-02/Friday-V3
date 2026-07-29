"""Scrollable feed widget — the main event list for the ambient dashboard.

Displays ambient events in reverse chronological order with:
- Priority coloring (red=critical, yellow=important, blue=noteworthy, gray=info)
- Category badge on each event
- Selected/highlighted item for keyboard interaction
- Actionable items marked with an action indicator
"""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style as RichStyle

from ..style import Color, Style as TStyle
from .models import FeedEventView


# Priority → display color
_PRIORITY_STYLE = {
    3: TStyle.ERROR,       # critical → red
    2: TStyle.WARNING,     # important → yellow
    1: f"bold {Color.PRIMARY}",  # noteworthy → blue
    0: TStyle.DIM,         # info → gray
}

_PRIORITY_MARK = {
    3: "●",
    2: "●",
    1: "●",
    0: "○",
}

# Category → color
_CATEGORY_COLORS = {
    "workspace": Color.SUCCESS,
    "intelligence": Color.PRIMARY,
    "quality": Color.WARNING,
    "execution": Color.PRIMARY,
    "system": Color.DIM,
}

# Project badge color (hash-based so same project always gets same color).
_PROJECT_COLORS = (
    "bright_cyan",
    "bright_magenta",
    "bright_yellow",
    "bright_green",
    "bright_blue",
    "bright_red",
    "cyan",
    "magenta",
    "yellow",
    "green",
)


def _project_color(project: str) -> str:
    """Deterministic color for a project name."""
    return _PROJECT_COLORS[hash(project) % len(_PROJECT_COLORS)]


def _time_str(ts: str) -> str:
    """Extract time portion from ISO timestamp."""
    if len(ts) >= 19:
        return ts[11:19]
    return ts[:8] if len(ts) >= 8 else ts


class FeedWidget:
    """Scrollable list of ambient events with priority coloring.

    Supports keyboard-driven scrolling and selection.
    """

    def __init__(self, max_visible: int = 20):
        self.max_visible = max_visible

    def render(
        self,
        events: list[FeedEventView],
        scroll_offset: int = 0,
        selected_index: int = -1,
    ) -> Panel:
        """Render the feed widget as a Rich Panel.

        Args:
            events: All events to display.
            scroll_offset: How many events to skip from the top.
            selected_index: Index of the currently selected (highlighted) event,
                            or -1 for none.

        Returns:
            A Rich Panel with the feed content.
        """
        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True)  # priority marker
        table.add_column(no_wrap=True)  # category badge
        table.add_column(no_wrap=True)  # project badge
        table.add_column(no_wrap=True)  # timestamp
        table.add_column(no_wrap=True)  # title + detail

        visible = events[scroll_offset: scroll_offset + self.max_visible]
        if not visible:
            table.add_row(
                Text("  ", style=TStyle.DIM),
                Text(""),
                Text(""),
                Text(""),
                Text("  No events yet. Run a daemon cycle to populate the feed.",
                     style=TStyle.DIM),
            )
        else:
            for i, ev in enumerate(visible):
                abs_index = scroll_offset + i
                is_selected = abs_index == selected_index

                # Build the priority marker.
                mark = _PRIORITY_MARK.get(ev.priority, "○")
                pri_style = _PRIORITY_STYLE.get(ev.priority, TStyle.DIM)
                marker = Text(f"{mark} ", style=pri_style)

                # Category badge (shortened).
                cat = ev.category[:6].ljust(6)
                cat_color = _CATEGORY_COLORS.get(ev.category, Color.DIM)
                badge = Text(f"[{cat}]", style=f"{cat_color}")

                # Project badge (only when project is set).
                if ev.project and ev.project.strip():
                    proj = ev.project[:12].ljust(12)
                    proj_color = _project_color(ev.project)
                    project_badge = Text(f"{proj}", style=f"bold {proj_color}")
                else:
                    project_badge = Text(" " * 12, style=TStyle.DIM)

                # Timestamp.
                ts = Text(_time_str(ev.timestamp), style=TStyle.DIM)

                # Title.
                title_style = _PRIORITY_STYLE.get(ev.priority, TStyle.TEXT)
                title_text = Text(ev.title, style=title_style)

                # Salience indicator (compact bar for high-salience events).
                if ev.salience >= 4.0:
                    salience_indicator = Text(" ⚡", style=TStyle.ERROR)
                elif ev.salience >= 2.5:
                    salience_indicator = Text(" ◈", style=TStyle.WARNING)
                else:
                    salience_indicator = Text("")

                # Detail + action on second line.
                detail_parts: list[tuple[str, str]] = []
                if ev.detail:
                    detail_parts.append((ev.detail[:80], TStyle.DIM))
                if ev.actionable and ev.action_command:
                    detail_parts.append((f" → {ev.action_command}", TStyle.WARNING))

                if detail_parts:
                    detail_line = Text.assemble(*detail_parts)
                    combined = Text.assemble(title_text, salience_indicator, Text("\n"), detail_line)
                else:
                    combined = Text.assemble(title_text, salience_indicator)

                # Selection highlight.
                if is_selected:
                    combined = Text.assemble(
                        Text("▸ ", style="bold white"),
                        combined,
                    )
                    row_style = f"on {Color.HEADER_BG}"
                else:
                    row_style = ""

                table.add_row(marker, badge, project_badge, ts, combined, style=row_style)

        # Count how many are off-screen.
        total = len(events)
        hidden_before = scroll_offset
        hidden_after = max(0, total - scroll_offset - self.max_visible)

        subtitle = f"{total} event(s)"
        if hidden_before:
            subtitle += f"  ↑{hidden_before} more"
        if hidden_after:
            subtitle += f"  ↓{hidden_after} more"

        return Panel(
            table,
            title=f"[{Color.PRIMARY}]📡 Live Feed[/]",
            subtitle=subtitle,
            border_style=Color.BORDER,
            style=Color.PANEL_BG,
        )
