"""Vault — Friday V6's memory as linked markdown (Wave 0, ported from V5).

Layout (all plain files, all human-readable):

- ``raw/``     — append-only capture: every turn, every event. One
                 file per day, lines appended.
- ``wiki/``    — distilled knowledge: linked notes (``[[name]]``).
- ``outputs/`` — artifacts Friday ships (reports, results).
- ``notices/`` — proactive pings (Claude writes them; the HUD reads).

The graph IS the file system: ``[[links]]`` are the edges, grep is
the query, and the FTS index (``vault/index.py``) is a rebuildable
*cache* over it — never the source of truth.

V6 adaptation vs V5: the default vault root lives under the Friday
data dir (``~/.friday/v6_vault``, matching the V4 config/DB
convention) instead of the repo checkout, so operator memory survives
repo moves and installs. Unlike V5 there is no eager module-level
``Vault()`` singleton — constructing one creates directories, and
imports must stay side-effect free (use :func:`default_vault`).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Optional

#: The default vault root — operator memory lives with the rest of
#: Friday's data (~/.friday), not in the repo checkout.
DEFAULT_VAULT = Path.home() / ".friday" / "v6_vault"

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
        lines — the graph query. Ground truth, always works."""
        words = [w for w in re.split(r"\s+", (terms or "").lower())
                 if len(w) >= 2]
        hits: list[tuple[str, str]] = []
        for path in list(self.wiki.glob("*.md")) + \
                sorted(self.raw.glob("*.log"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8",
                                       errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                low = line.lower()
                if any(w in low for w in words):
                    hits.append((path.name, line.strip()[:240]))
        # One line per match, prefixed with its file.
        return [f"{fname}: {line}" for fname, line in hits[:limit]]

    def search(self, terms: str, limit: int = 20) -> list[str]:
        """Vault query — FTS index first, grep fallback (cache, not truth).

        Wave 0 exit criterion: ``vault find`` answers from the index
        when it exists, and from grep when it was deleted. The index is
        refreshed incrementally (mtime scan) before each query; any
        failure degrades to :meth:`query` silently.
        """
        lines, _source = self.search_with_source(terms, limit)
        return lines

    def search_with_source(self, terms: str,
                           limit: int = 20) -> tuple[list[str], str]:
        """Like :meth:`search` but also reports which source answered:
        ``("index", ...)`` when the FTS cache produced hits, else
        ``("grep", ...)``. The ONE search code path for every surface.
        """
        try:
            from .index import VaultIndex
            idx = VaultIndex(self.root)
            if idx.fts_available():
                idx.refresh()
                hits = idx.query(terms, limit)
                if hits:
                    return ([f"{h['path']}: {h['snippet']}".strip()
                             for h in hits], "index")
        except Exception:
            pass  # never crash — grep is the floor
        return (self.query(terms, limit), "grep")

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
        with_mtime: list[tuple[float, Path]] = []
        for p in self.notices.glob("*.md"):
            try:
                with_mtime.append((p.stat().st_mtime, p))
            except OSError:
                continue  # deleted mid-scan — skip
        return [p for _, p in sorted(with_mtime, reverse=True)]

    def latest_notices(self, n: int = 5) -> list[dict]:
        """The ``n`` newest notices as dicts: ``{id, text, path, at}``."""
        out: list[dict] = []
        for p in self.list_notices()[:n]:
            try:
                nid = int(p.stem.split("-")[0])
            except (ValueError, IndexError):
                continue  # non-conforming filename — skip
            try:
                at = p.stat().st_mtime
            except OSError:
                continue  # deleted mid-scan — skip
            body = self.notice_text(p)
            out.append({
                "id": nid,
                "text": body or p.stem,
                "path": str(p),
                "at": at,
            })
        return out


def default_vault() -> Vault:
    """The operator's vault at ``~/.friday/v6_vault`` (lazy — no I/O
    at import time)."""
    return Vault()


__all__ = ["Vault", "default_vault", "DEFAULT_VAULT", "_slug"]
