"""Design tokens — single source of truth for visual identity."""
from __future__ import annotations


class Color:
    """GitHub-dark-inspired palette."""
    PRIMARY = "#58a6ff"       # Blue
    SUCCESS = "#3fb950"       # Green
    WARNING = "#d29922"       # Yellow
    ERROR = "#f85149"         # Red
    TEXT = "#e6edf3"          # Light gray
    DIM = "#8b949e"           # Muted gray
    BORDER = "#30363d"        # Subtle border
    PANEL_BG = "#0d1117"      # Panel background
    HEADER_BG = "#161b22"     # Header background


class Icon:
    MISSION = "◆"
    WORKER = "▲"
    COMPLETED = "✓"
    FAILED = "✗"
    RUNNING = "●"
    PENDING = "○"
    WAITING = "◷"
    PHASE = "▶"


class Style:
    HEADER = f"bold white on {Color.HEADER_BG}"
    MISSION_ID = f"bold {Color.PRIMARY}"
    SUCCESS = f"bold {Color.SUCCESS}"
    ERROR = f"bold {Color.ERROR}"
    WARNING = f"{Color.WARNING}"
    DIM = f"{Color.DIM}"
    TEXT = f"{Color.TEXT}"
    PANEL_BORDER = f"{Color.BORDER}"
