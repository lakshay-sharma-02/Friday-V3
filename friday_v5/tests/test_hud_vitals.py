"""Vitals widget test — pure formatting, no Textual, no psutil."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.hud.vitals import format_vitals


def test_format_vitals_renders_readings():
    out = format_vitals(cpu=12.0, mem_gb=3.2, disk_pct=61.0)
    assert "cpu 12%" in out
    assert "mem 3.2G" in out
    assert "disk 61%" in out


def test_format_vitals_handles_missing():
    out = format_vitals(cpu=None, mem_gb=None, disk_pct=None)
    assert "cpu ?" in out
