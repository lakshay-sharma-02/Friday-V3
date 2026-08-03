"""Cross-repo code search (Wave 11 §3.1). Pure-stdlib ripgrep-style
recursive search over a repo tree. Returns evidence-cited matches
(file:line + snippet). Never raises.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.research.code_search")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".next", ".cache", ".mypy_cache",
              ".pytest_cache"}
_MAX_BYTES = 200_000


@dataclass
class SearchHit:
    """One code-search result — file:line + snippet (the evidence)."""

    path: str
    line: int
    snippet: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "snippet": self.snippet}


class CodeSearch:
    """Search a repo tree for a pattern (case-insensitive by default)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def search(self, pattern: str, *, max_hits: int = 20) -> list[SearchHit]:
        """Return evidence-cited matches — never raises."""
        if not self.root.is_dir():
            return []
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        hits: list[SearchHit] = []
        try:
            for p in self.root.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in _SKIP_DIRS for part in p.parts):
                    continue
                if p.stat().st_size > _MAX_BYTES:
                    continue
                try:
                    for i, line in enumerate(
                            p.read_text(encoding="utf-8",
                                        errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            hits.append(SearchHit(str(p), i, line.strip()[:120]))
                            if len(hits) >= max_hits:
                                return hits
                except OSError:
                    continue
        except OSError:
            pass
        return hits

    def files_matching(self, pattern: str, *, max_files: int = 20) -> list[str]:
        """Distinct files with at least one hit."""
        seen: dict[str, str] = {}
        for h in self.search(pattern, max_hits=max_files * 4):
            seen.setdefault(h.path, h.snippet)
        return list(seen)[:max_files]


__all__ = ["CodeSearch", "SearchHit"]
