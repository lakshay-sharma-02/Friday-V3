"""Reusable Table builders for list-style commands."""
from __future__ import annotations
from rich.table import Table
from rich.text import Text
from ..style import Color, Style


def data_table(title: str, columns: list[str], rows: list[list[str]]) -> Table:
    """Build a formatted table from column names and row data."""
    table = Table(title=title, border_style=Color.BORDER, style=Color.PANEL_BG)
    for col in columns:
        table.add_column(col, style=Style.TEXT)
    for row in rows:
        table.add_row(*row)
    return table


def key_value_table(pairs: list[tuple[str, str]], title: str = "") -> Table:
    """Two-column table for key-value data (e.g. explain output)."""
    table = Table(title=title, border_style=Color.BORDER, style=Color.PANEL_BG)
    table.add_column("Key", style=Style.DIM)
    table.add_column("Value", style=Style.TEXT)
    for k, v in pairs:
        table.add_row(k, v)
    return table
