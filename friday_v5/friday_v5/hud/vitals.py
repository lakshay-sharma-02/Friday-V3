"""Vitals widget — CPU / memory / disk, psutil-backed, 1s timer.

Pure ``format_vitals`` is separated for hermetic testing; the widget
itself reads psutil (optional at runtime — degrades to ``?``).
"""
from __future__ import annotations

from typing import Optional

try:
    import psutil
except Exception:  # pragma: no cover - optional dep
    psutil = None

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


def format_vitals(cpu: Optional[float], mem_gb: Optional[float],
                  disk_pct: Optional[float]) -> str:
    """One-line vitals string, resilient to missing readings."""
    def _pct(v: Optional[float]) -> str:
        return f"{v:.0f}%" if v is not None else "?"

    def _mem(v: Optional[float]) -> str:
        return f"{v:.1f}G" if v is not None else "?"

    return (f"cpu {_pct(cpu)}  mem {_mem(mem_gb)}  "
            f"disk {_pct(disk_pct)}")


def _read() -> tuple:
    if psutil is None:
        return None, None, None
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().used / (1024 ** 3)
        disk = psutil.disk_usage("/").percent
        return cpu, mem, disk
    except Exception:
        return None, None, None


class Vitals(Static):
    """Vitals panel — refreshes every second."""

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        self.update(format_vitals(*_read()))
