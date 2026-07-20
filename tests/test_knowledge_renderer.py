"""Tests for knowledge renderer."""
from __future__ import annotations
from io import StringIO

from rich.console import Console
from friday.presentation.renderers.knowledge import render_knowledge_table


def _render(renderable) -> str:
    buf = StringIO()
    Console(file=buf, width=120).print(renderable)
    return buf.getvalue()


def test_knowledge_table_renders():
    rows = [
        {"type": "trend", "subject": "python adoption",
         "confidence": "strong", "status": "stable"},
    ]
    panel = render_knowledge_table(rows)
    text = _render(panel)
    assert "python adoption" in text
    assert "strong" in text
    assert "trend" in text
