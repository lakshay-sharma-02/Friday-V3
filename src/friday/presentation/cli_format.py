"""CLI output formatting — clean, professional text for Friday's commands.

Uses pure ANSI codes (no Rich dependency) for terminal coloring, and falls
back to clean ASCII when output is piped or logged. All functions are TTY-aware.

This is the lightweight sibling of the Rich-based mission control renderers:
- ``formatters/`` and ``renderers/`` → Rich panels for the mission control UI
- ``cli_format.py`` → ANSI text for CLI commands and daemon output
"""

from __future__ import annotations

import re
import sys
from typing import Any, Optional


# ---------------------------------------------------------------------------
# ANSI regex for stripping
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Detection: are we in an interactive terminal?
# ---------------------------------------------------------------------------

def _is_tty() -> bool:
    return sys.stdout.isatty()


# ---------------------------------------------------------------------------
# ANSI helpers (no Rich dependency for basic colors)
# ---------------------------------------------------------------------------

class _Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    GRAY = "\033[90m"

    @classmethod
    def text(cls, content: str, *codes: str, condition: bool = True) -> str:
        if not _is_tty() or not condition:
            return content
        return "".join(codes) + content + cls.RESET


bold = lambda t, c=True: _Style.text(t, _Style.BOLD, condition=c)
dim = lambda t, c=True: _Style.text(t, _Style.DIM, condition=c)
green = lambda t, c=True: _Style.text(t, _Style.GREEN, condition=c)
yellow = lambda t, c=True: _Style.text(t, _Style.YELLOW, condition=c)
blue = lambda t, c=True: _Style.text(t, _Style.BLUE, condition=c)
magenta = lambda t, c=True: _Style.text(t, _Style.MAGENTA, condition=c)
cyan = lambda t, c=True: _Style.text(t, _Style.CYAN, condition=c)
red = lambda t, c=True: _Style.text(t, _Style.RED, condition=c)
gray = lambda t, c=True: _Style.text(t, _Style.GRAY, condition=c)


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------

def divider(char: str = "\u2500", length: int = 50) -> str:
    """Print a clean horizontal divider."""
    return gray(char * length)


def header(text: str, sub: Optional[str] = None) -> str:
    """Command header like ``Friday · Workspace Summary``."""
    parts = [bold(cyan("\u25b6")), bold(f" Friday \u00b7 {text}")]
    if sub:
        parts.append(gray(f"  ({sub})"))
    return "  " + " ".join(parts)


def bullet(text: str, indent: int = 0) -> str:
    """Simple bullet point."""
    prefix = "  " * (indent + 1) + "\u00b7 "
    return prefix + text


def label(key: str, value: str, sep: str = ": ") -> str:
    """Key-value pair like ``Language: Python``."""
    return f"  {bold(key)}{sep}{value}"


def status_dot(healthy: bool) -> str:
    """Green filled circle or red X status indicator."""
    return green("\u25cf") if healthy else red("\u2715")


def tag(text: str, color: str = "blue") -> str:
    """Small colored tag/badge like ``[Active]``."""
    c = {"green": green, "yellow": yellow, "red": red, "blue": blue, "gray": gray}
    fn = c.get(color, blue)
    return fn(f"[{text}]")


# ---------------------------------------------------------------------------
# Cards — structured info blocks
# ---------------------------------------------------------------------------

_W = 48  # card width


def card(
    title: str,
    lines: list[str],
    *,
    color: str = "blue",
    indent: int = 0,
    border: bool = True,
) -> str:
    """A structured information card with title and content lines.

    Args:
        title: Card heading.
        lines: Content lines (each line is pre-formatted).
        color: Accent color for the title.
        indent: Left padding.
        border: Show top/bottom border.
    """
    prefix = "  " * indent
    hc = {"green": green, "yellow": yellow, "red": red, "blue": blue, "gray": gray}.get(color, blue)
    title_str = hc(bold(f"  {title}"))

    if not border:
        result = prefix + title_str
        for line in lines:
            result += "\n" + prefix + "  " + str(line)
        return result

    parts = [
        prefix + gray("\u250c" + "\u2500" * _W + "\u2510"),
        prefix + f"\u2502{title_str}{' ' * (_W - len(title) - 3)}\u2502",
        prefix + gray("\u251c" + "\u2500" * _W + "\u2524"),
    ]
    for line in lines:
        content = str(line).rstrip()
        visible = len(_ANSI_RE.sub("", content))
        padding = max(0, _W - visible)
        parts.append(prefix + f"\u2502 {content}{' ' * padding}\u2502")
    parts.append(prefix + gray("\u2514" + "\u2500" * _W + "\u2518"))

    return "\n".join(parts)


def table(headers: list[str], rows: list[list[str]], *, indent: int = 0) -> str:
    """A clean fixed-width table."""
    if not rows:
        return ""

    prefix = "  " * indent
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    sep = gray("  ").join(gray("\u2500" * w) for w in col_widths)
    header_cells = [bold(h.ljust(col_widths[i])) for i, h in enumerate(headers)]
    lines = [prefix + gray("  ").join(header_cells), prefix + sep]

    for row in rows:
        cells = [row[i].ljust(col_widths[i]) if i < len(col_widths) else row[i] for i in range(len(row))]
        lines.append(prefix + gray("  ").join(cells))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status messages
# ---------------------------------------------------------------------------

def error(msg: str) -> str:
    return red(f"\u2715 {msg}")


def warning(msg: str) -> str:
    return yellow(f"\u26a0 {msg}")


def success(msg: str) -> str:
    return green(f"\u2713 {msg}")


def info(msg: str) -> str:
    return blue(f"\u2139 {msg}")
