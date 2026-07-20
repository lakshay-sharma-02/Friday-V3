"""Shared helpers for renderers — status formatting, duration, etc."""
from __future__ import annotations


def format_duration(seconds: int) -> str:
    """Human-readable duration from seconds."""
    mins, secs = divmod(seconds, 60)
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h}h {m}m {secs}s"
    return f"{m}m {secs}s"
