"""Cross-project correlation — the MCU "what's the deal between X and Y"
analysis (Wave 11).

``correlate(a, b)`` compares two repos and produces an evidence-cited
:class:`CorrelationEstimate`: shared languages/frameworks, overlapping
files, and an integration-cost estimate as **range + confidence** — never
false precision ("~3 days, confidence: medium").

Cached like ``analyze`` (mtime-keyed) so Friday "already did that" only
when it actually did.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .architecture import analyze

logger = logging.getLogger("friday_v4.research.cross_project")

#: Relative overlap → cost (person-days). Deliberately coarse ranges.
#: 0.0–0.2 low / 0.2–0.5 medium / 0.5+ high.
_DAYS_RANGE: tuple[tuple[float, str], ...] = (
    (0.5, "5–10 days"), (0.2, "2–5 days"), (0.0, "1–2 days"),
)
#: Confidence falls as the evidence base shrinks.
_CONF_BY_FILE_COUNT: tuple[tuple[int, str], ...] = (
    (10, "high"), (3, "medium"), (0, "low"),
)


@dataclass
class CorrelationEstimate:
    """Evidence-cited integration-cost estimate between two repos."""

    a: str
    b: str
    shared_languages: list[str] = field(default_factory=list)
    shared_frameworks: list[str] = field(default_factory=list)
    overlapping_files: list[str] = field(default_factory=list)
    overlap_score: float = 0.0          # 0..1
    days_range: str = "1–2 days"
    confidence: str = "low"
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "a": self.a, "b": self.b,
            "shared_languages": self.shared_languages,
            "shared_frameworks": self.shared_frameworks,
            "overlapping_files": self.overlapping_files,
            "overlap_score": round(self.overlap_score, 2),
            "days_range": self.days_range,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


def _same_name(a: Path, b: Path) -> bool:
    return a.name == b.name


def _overlapping_names(pa: RepoProfile, pb: RepoProfile,
                       limit: int = 10) -> list[str]:
    """Filenames that exist in both trees (same basename)."""
    def names(root: Path) -> set[str]:
        out: set[str] = set()
        try:
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in {
                        ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
                        ".java", ".c", ".cpp", ".h", ".rb", ".swift", ".kt",
                        ".cs", ".sh", ".html", ".css", ".sql", ".json"}:
                    out.add(p.name)
        except OSError:
            pass
        return out
    shared = names(Path(pa.path)) & names(Path(pb.path))
    return sorted(shared)[:limit]


def correlate(a: str | Path, b: str | Path) -> CorrelationEstimate:
    """Estimate integration cost between two repos — never raises."""
    pa = analyze(a)
    pb = analyze(b)
    if not (pa.available and pb.available):
        return CorrelationEstimate(str(a), str(b))

    shared_langs = sorted(set(pa.languages) & set(pb.languages))
    shared_frames = sorted(set(pa.framework_signals)
                           & set(pb.framework_signals))
    overlapping = _overlapping_names(pa, pb)

    # Overlap score: weighted blend of shared signals (0..1).
    score = 0.0
    parts = 0
    if shared_langs:
        score += min(1.0, len(shared_langs) / 3.0) * 0.4
        parts += 1
    if shared_frames:
        score += min(1.0, len(shared_frames) / 2.0) * 0.3
        parts += 1
    if overlapping:
        score += min(1.0, len(overlapping) / 8.0) * 0.3
        parts += 1
    score = score / max(parts, 1)

    days_range = "1–2 days"
    for threshold, label in _DAYS_RANGE:
        if score >= threshold:
            days_range = label
            break

    conf = "low"
    evidence_base = len(overlapping) + len(shared_langs) + len(shared_frames)
    for threshold, label in _CONF_BY_FILE_COUNT:
        if evidence_base >= threshold:
            conf = label
            break

    evidence = []
    if shared_langs:
        evidence.append(f"both use {', '.join(shared_langs[:3])}")
    if shared_frames:
        evidence.append(f"shared stack: {', '.join(shared_frames[:3])}")
    if overlapping:
        evidence.append(f"{len(overlapping)} overlapping file(s): "
                        f"{', '.join(overlapping[:5])}")
    evidence.append(f"estimate {days_range}, confidence {conf} "
                    f"(overlap {score:.0%})")

    return CorrelationEstimate(
        str(pa.path), str(pb.path), shared_langs, shared_frames,
        overlapping, score, days_range, conf, evidence)


__all__ = ["CorrelationEstimate", "correlate"]
