"""Pure vault→HUD parsers — no Textual, fully testable (Wave 3 port).

Every render/parse function here is a pure function over plain data:
the vault files (schedule/notices/raw log) and the durable ambient
queue. The Textual widgets are thin shells over these — so the entire
HUD's logic is hermetic-testable without the optional dependency.
"""

from __future__ import annotations

import datetime
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
    out: list[str] = []
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


# ── render helpers (pure) ───────────────────────────────────────────


def render_stream(lines: list[str]) -> str:
    """(rendered) stream lines → last ~8."""
    if not lines:
        return "(idle)"
    return "\n".join(lines[-8:])


def render_schedule(items: list[str]) -> str:
    if not items:
        return "nothing scheduled"
    return "\n".join(f"· {i}" for i in items)


def render_notices(notices: list[dict]) -> str:
    if not notices:
        return "no notices"
    return "\n".join(f"· {n.get('text', '')}" for n in notices)


def render_activity(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "(no activity yet today)"


def render_permissions(pending: list[dict]) -> str:
    if not pending:
        return "no pending asks"
    return "\n".join(
        f"[{p.get('id', '?')}] {p.get('description') or p.get('command') or p.get('action_type') or ''}"
        for p in pending)


def _find_terms(text: str) -> str:
    """Search terms from a ``/find`` prompt line, or "".

    ``/find auth`` → ``"auth"``; bare ``/find`` → ``""`` (the caller
    asks "find what?"); anything else (``/findx``) → ``""`` (ordinary
    input, routed to the brain). Pure — no Textual, fully testable.
    """
    stripped = (text or "").strip()
    lower = stripped.lower()
    # Word boundary: ``/find`` alone or ``/find <terms>`` — a bare
    # ``/findx`` or ``/finder`` is NOT a vault search (ordinary input).
    if lower == "/find":
        return ""
    if not lower.startswith("/find "):
        return ""
    return stripped[6:].strip()


def render_commands() -> str:
    return "[ask] type below   [/find term] search vault   [quit] q"


def render_search(hits: list[str], source: str = "") -> str:
    """FTS search results → HUD text (source-named, honest when empty).

    ``source`` is ``"index"`` or ``"grep"`` (the Wave 0 exit criterion:
    cache first, grep floor — never fabricate).
    """
    if not hits:
        return "nothing found"
    tag = "fts" if source == "index" else "grep"
    n = len(hits)
    shown = min(n, 8)
    lines = "\n".join(f"· {h[:120]}" for h in hits[:shown])
    # Honest count: name how many are shown vs how many matched (the
    # panel truncates long result sets).
    if n == 1:
        label = f"({tag})"
    elif shown < n:
        label = f"({tag}, showing {shown} of {n})"
    else:
        label = f"({tag}, {n} hits)"
    return f"{lines}\n{label}"


def format_ambient_event(event) -> str:
    """One durable ambient event → a stream line."""
    topic = getattr(event, "topic", "system")
    payload = getattr(event, "payload", "")
    tag = {"permission": "⛔", "security": "⚠", "mission": "◆",
           "suggestion": "💡", "research": "🔎", "system": "·"}.get(
               topic, "·")
    return f"{tag} [{topic}] {payload}"


def today_raw_path(vault) -> Path:
    """Today's raw log path under a vault (missing → empty tail)."""
    day = datetime.date.today().isoformat()
    return vault.raw / f"{day}.log"


__all__ = [
    "_find_terms", "parse_schedule", "parse_notice_text", "tail_log",
    "render_stream", "render_schedule", "render_notices",
    "render_activity", "render_permissions", "render_commands",
    "render_search", "format_ambient_event", "today_raw_path",
]
