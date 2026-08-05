"""Repo purpose recovery from READMEs (Wave 11 §3.1).

``readme_purpose(repo)`` finds README.md (or rst/txt) and returns the
first meaningful paragraph — the "what is this repo" evidence. Never
raises; empty repo → no purpose.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.research.readme")

_CANDIDATES = ("README.md", "README.rst", "README.txt", "Readme.md")

#: First paragraph = first 2-5 non-empty lines that aren't badges/titles.
_SKIP_LINE = re.compile(r"^(!\[|\[!|#+\s|!\[alt|<div|$)", re.IGNORECASE)


@dataclass
class ReadmePurpose:
    """The extracted purpose + the evidence line."""

    repo: str
    purpose: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {"repo": self.repo, "purpose": self.purpose,
                "source": self.source}


def readme_purpose(repo: str | Path) -> ReadmePurpose:
    repo_path = Path(repo).expanduser().resolve()
    for name in _CANDIDATES:
        f = repo_path / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = [ln.strip() for ln in text.splitlines()[:60]]
        kept: list[str] = []
        for ln in lines:
            if _SKIP_LINE.match(ln) and not kept:
                continue
            if ln and ln not in kept:
                kept.append(ln)
            if len(kept) >= 3:
                break
        purpose = " ".join(kept)[:300] if kept else ""
        if purpose:
            return ReadmePurpose(str(repo_path), purpose, str(f))
    return ReadmePurpose(str(repo_path))


__all__ = ["ReadmePurpose", "readme_purpose"]
