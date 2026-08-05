"""Change-impact analysis (Wave 11 §3.1). Given a file in a repo,
estimate which other files reference it (imports/requires). Evidence is
the referencing file:line pairs; the blast radius is a count + the
top references. Never raises.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .code_search import CodeSearch

logger = logging.getLogger("friday_v6.research.impact")

#: Stem → identifier forms ("auth.py" → "auth", "import auth", "from .auth").
_IMPORT_RX = re.compile(
    r"(?:import|from|require\(|from\s+[\"']).{0,40}?"
    r"([\w.-]*?)(?:\.py)?[\"']?",
    re.IGNORECASE)


@dataclass
class ImpactReport:
    """How many files reference ``target`` and where."""

    repo: str
    target: str
    referencing_files: list[str] = field(default_factory=list)
    reference_count: int = 0
    top_references: list[str] = field(default_factory=list)
    severity: str = "low"   # low | medium | high — by reference count

    def to_dict(self) -> dict:
        return {
            "repo": self.repo, "target": self.target,
            "referencing_files": self.referencing_files,
            "reference_count": self.reference_count,
            "top_references": self.top_references,
            "severity": self.severity,
        }


def impact(repo: str | Path, target: str, *, max_files: int = 15) -> ImpactReport:
    """Estimate the blast radius of changing ``target`` in ``repo``."""
    repo_path = Path(repo).expanduser().resolve()
    search = CodeSearch(repo_path)

    stem = Path(target).stem  # "auth.py" → "auth"
    # Match the stem as an import identifier: "auth" or "from x import auth".
    pattern = rf"\b{re.escape(stem)}\b"
    hits = search.search(pattern, max_hits=max_files * 6)

    files: dict[str, str] = {}
    for h in hits:
        # Only count hits that look like an import/require reference.
        if _IMPORT_RX.search(h.snippet) or re.search(
                rf"\bfrom\s+[\"'].*{re.escape(stem)}", h.snippet, re.I):
            files.setdefault(h.path, h.snippet)
    ref_files = list(files)[:max_files]

    if len(ref_files) >= 10:
        severity = "high"
    elif len(ref_files) >= 3:
        severity = "medium"
    else:
        severity = "low"

    return ImpactReport(
        repo=str(repo_path), target=str(target),
        referencing_files=ref_files, reference_count=len(ref_files),
        top_references=[f"{p}: {s}" for p, s in
                        list(files.items())[:5]],
        severity=severity)


__all__ = ["ImpactReport", "impact"]
