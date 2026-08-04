"""Vault — Friday's memory as linked markdown, no database.

Layout (all plain files, all human-readable):

- ``raw/``     — append-only capture: every turn, every event. One
                 file per day, lines appended.
- ``wiki/``    — distilled knowledge: linked notes (``[[name]]``).
- ``outputs/`` — artifacts Friday ships (reports, results).

The graph IS the file system: ``[[links]]`` are the edges, grep is
the query.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path
from typing import Optional

#: The project's own vault dir — the skills reference ``vault/wiki/...``
#: relative to the repo root, so the default vault must be that one.
DEFAULT_VAULT = Path(__file__).resolve().parent.parent / "vault"

_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)")


def _slug(name: str) -> str:
    """Slugify a note name: keep case, drop unsafe chars."""
    return re.sub(r"[^\w\- ]", "", name).strip().replace(" ", "-")


class Vault:
    """Path helpers + file ops over the vault tree."""

    def __init__(self, root: Optional[Path | str] = None) -> None:
        self.root = Path(root) if root else DEFAULT_VAULT
        self.raw = self.root / "raw"
        self.wiki = self.root / "wiki"
        self.outputs = self.root / "outputs"
        for d in (self.raw, self.wiki, self.outputs):
            d.mkdir(parents=True, exist_ok=True)
        #: Proactive pings — Claude writes these when it notices
        #: something worth surfacing; the HUD + notifier read them.
        self.notices = self.root / "notices"
        self.notices.mkdir(parents=True, exist_ok=True)

    # ── raw (append-only) ────────────────────────────────────────────

    def log(self, role: str, text: str) -> Path:
        """Append one turn to today's raw log. Returns the log file."""
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        day = datetime.date.today().isoformat()
        path = self.raw / f"{day}.log"
        body = (text or "").strip().replace("\n", "\n  ")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {role}\n  {body}\n")
        return path

    # ── wiki ─────────────────────────────────────────────────────────

    def note(self, name: str, content: str) -> Path:
        """Write (or overwrite) a wiki note ``<name>.md``."""
        path = self.wiki / f"{_slug(name) or 'note'}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def note_path(self, name: str) -> Path:
        """Resolve a wiki note path from its ``[[name]]`` form (same
        slug rule as :meth:`note`)."""
        return self.wiki / f"{_slug(name.strip('[]').strip())}.md"

    def list_wiki(self) -> list[Path]:
        """All wiki notes, newest first."""
        return sorted(self.wiki.glob("*.md"),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    # ── query ────────────────────────────────────────────────────────

    def query(self, terms: str, limit: int = 20) -> list[str]:
        """Grep the vault (wiki + raw) for terms. Returns matching
        lines — the graph query."""
        words = [w for w in re.split(r"\s+", (terms or "").lower())
                 if len(w) >= 2]
        hits: list[tuple[str, str]] = []
        for path in list(self.wiki.glob("*.md")) + \
                sorted(self.raw.glob("*.log"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                low = line.lower()
                if any(w in low for w in words):
                    hits.append((path.name, line.strip()[:240]))
        # One line per match, prefixed with its file.
        return [f"{fname}: {line}" for fname, line in hits[:limit]]

    def links_from(self, text: str) -> list[str]:
        """All ``[[links]]`` in text."""
        return _WIKI_LINK.findall(text)

    # ── notices (proactive pings) ────────────────────────────────────

    def notice_text(self, path: Path) -> str:
        """Body of a notice file with its frontmatter lines dropped."""
        try:
            text = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(l for l in text
                         if not l.startswith(("#", "- **"))).strip()

    def list_notices(self) -> list[Path]:
        """All notice files, newest first."""
        return sorted(self.notices.glob("*.md"),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    def latest_notices(self, n: int = 5) -> list[dict]:
        """The ``n`` newest notices as dicts: ``{id, text, path, at}``."""
        out: list[dict] = []
        for p in self.list_notices()[:n]:
            body = self.notice_text(p)
            if not body:
                continue
            out.append({
                "id": int(p.stem.split("-")[0]),
                "text": body,
                "path": str(p),
                "at": p.stat().st_mtime,
            })
        return out


#: Module default vault (path helpers usable without construction).
default = Vault()
