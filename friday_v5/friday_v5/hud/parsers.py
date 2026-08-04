"""Pure vault→HUD parsers — no Textual, fully testable."""
from __future__ import annotations

import re
from pathlib import Path

_SCHEDULE_ITEM = re.compile(r"^-\s+(?!\[x\]\s)(.+)$")
_NOTICE_META = re.compile(r"^(?:# .*|- \*\*(at|id)\*\*:.*)$", re.MULTILINE)


def parse_schedule(path: Path) -> list[str]:
    """Upcoming schedule lines from a wiki/schedule.md (skips done)."""
    try:
        lines = path.read_text(encoding="utf-8",
                               errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith("---"):
            continue
        m = _SCHEDULE_ITEM.match(line)
        if m and "[x]" not in line:
            out.append(m.group(1))
    return out


def parse_notice_text(path: Path) -> str:
    """Notice body without its meta block."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _NOTICE_META.sub("", text).strip()


def tail_log(path: Path, n: int = 6) -> list[str]:
    """Last ``n`` non-empty lines of a raw log."""
    try:
        lines = [l for l in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines()
                 if l.strip()]
    except OSError:
        return []
    return lines[-n:]
