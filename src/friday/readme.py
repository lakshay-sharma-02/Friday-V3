"""README understanding: deterministic extraction, with optional LLM enrichment.

Deterministic extraction works entirely offline and needs no model. If an LLM
is configured (see llm.py), we prefer its summary but always fall back to the
deterministic one on any failure.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .discovery import Repo
from .llm import summarize as llm_summarize

_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")


@dataclass
class ReadmeResult:
    text: str
    summary: str
    used_llm: bool


def _find_readme(repo: Path) -> Optional[Path]:
    for name in _README_NAMES:
        p = repo / name
        if p.is_file():
            return p
    # Also accept a README with any extension at the root.
    for child in repo.iterdir():
        if child.is_file() and child.name.upper().startswith("README"):
            return child
    return None


def _strip_md(text: str) -> str:
    # Remove code fences and badges; keep readable prose.
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"<[^>]+>", " ", text)  # html
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links -> text
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)  # headings -> text
    return text


def _section(text: str, heading: str) -> Optional[str]:
    """Extract the bullet/paragraph block under a heading (markdown or rst)."""
    lines = text.splitlines()
    pat = re.compile(rf"^[#=\-~]*\s*{re.escape(heading)}\s*[#=\-~]*\s*$", re.IGNORECASE)
    start = None
    for i, line in enumerate(lines):
        if pat.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    # Collect until next heading of equal-or-higher weight (markdown #'s).
    collected = []
    base_hashes = len(lines[start - 1]) - len(lines[start - 1].lstrip("#"))
    for line in lines[start:]:
        if re.match(r"^#{1,6}\s", line):
            cur = len(line) - len(line.lstrip("#"))
            if cur <= base_hashes:
                break
        collected.append(line)
    block = "\n".join(collected).strip()
    return block or None


def _bullets(block: str) -> list[str]:
    return [b.strip("-*+ ").strip() for b in block.splitlines() if re.match(r"\s*[-*+]\s", b)]


def _first_paragraph(text: str) -> str:
    paras = [p.strip() for p in _strip_md(text).split("\n\n") if p.strip()]
    # Skip a leading title-only paragraph.
    for p in paras:
        if len(p.split()) >= 4 and not p.startswith("#"):
            return p
    return paras[0] if paras else ""


def _maturity(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(beta)\b", low):
        return "Beta"
    if re.search(r"\b(alpha)\b", low):
        return "Alpha"
    if re.search(r"\b(wip|work in progress|early|prototype|experimental)\b", low):
        return "WIP"
    if re.search(r"\b(stable|production|released|ga)\b", low):
        return "Stable"
    return "Unknown"


def deterministic_summary(text: str) -> str:
    title = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            title = s.lstrip("# ").strip()
            break
        if s:
            title = s
            break
    purpose = _first_paragraph(text)
    features = _bullets(_section(text, "features") or _section(text, "feature") or "")
    roadmap = _bullets(_section(text, "roadmap") or _section(text, "todo") or "")
    maturity = _maturity(text)

    parts = [f"Purpose:\n{purpose or 'None stated'}", f"\nMaturity:\n{maturity}"]
    if title:
        parts.insert(0, f"Title:\n{title}")
    if features:
        parts.append("\nImportant features:\n" + "\n".join(f"- {f}" for f in features))
    else:
        parts.append("\nImportant features:\nNone stated")
    if roadmap:
        parts.append("\nRoadmap:\n" + "\n".join(f"- {r}" for r in roadmap))
    else:
        parts.append("\nRoadmap:\nNone stated")
    return "\n".join(parts)


def process(repo: Repo) -> Optional[ReadmeResult]:
    path = _find_readme(Path(repo.path))
    if path is None:
        # No README at all — try to recover a purpose from other evidence so we
        # never silently leave identity empty (M4 Part A).
        purpose, source, conf = recover_purpose_fallback(repo.path, Path(repo.path).name)
        if purpose:
            summary = f"Purpose:\n{purpose}\n\nMaturity:\nUnknown"
            return ReadmeResult(text="", summary=summary, used_llm=False)
        return None
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        # README exists but is empty — same recovery path.
        purpose, source, conf = recover_purpose_fallback(repo.path, Path(repo.path).name)
        if purpose:
            summary = f"Purpose:\n{purpose}\n\nMaturity:\nUnknown"
            return ReadmeResult(text="", summary=summary, used_llm=False)
        return None

    summary = llm_summarize(text)
    if summary:
        return ReadmeResult(text=text, summary=summary, used_llm=True)

    det = deterministic_summary(text)
    # If the deterministic summary has no real Purpose line, attempt recovery
    # from manifest/docs/name so the stored identity is still meaningful.
    if "None stated" in det or "Purpose:\n\n" in det:
        purpose, source, conf = recover_purpose_fallback(repo.path, Path(repo.path).name)
        if purpose:
            det = det.replace(
                "Purpose:\nNone stated",
                f"Purpose:\n{purpose}",
            ).replace("Purpose:\n\n", f"Purpose:\n{purpose}\n")
    return ReadmeResult(text=text, summary=det, used_llm=False)


def purpose_only(text: str) -> str:
    """Short prose purpose, used for the per-project Purpose line."""
    return _first_paragraph(text) or "No README summary available."


def manifest_description(repo_path: str | Path) -> Optional[str]:
    """Recover a project description from manifest metadata, else None.

    Used by identity purpose-recovery (audit §6): package.json `description`
    and pyproject.toml `[project].description` are deterministic, evidence-backed
    signals. Returns None when absent so callers fall through rather than invent.
    """
    repo = Path(repo_path)
    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore") or "{}")
        except (json.JSONDecodeError, OSError):
            data = {}
        desc = (data.get("description") or "").strip()
        # Skip placeholder scaffolds ("Add your description here").
        if desc and len(desc.split()) >= 3 and "add your description" not in desc.lower():
            return desc
    pp = repo / "pyproject.toml"
    if pp.is_file():
        txt = pp.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"(?m)^\s*description\s*=\s*['\"]([^'\"]+)['\"]\s*$", txt)
        if m and len(m.group(1).split()) >= 3 and "add your description" not in m.group(1).lower():
            return m.group(1).strip()
    return None


# Doc files that often carry explicit purpose when the README does not.
_PURPOSE_DOC_NAMES = (
    "VISION.md", "PRODUCT.md", "PURPOSE.md", "ABOUT.md", "MISSION.md",
    "ROADMAP.md", "GOALS.md", "DESCRIPTION.md",
)


def _purpose_from_doc(repo_path: str | Path) -> Optional[str]:
    """Recover purpose from a sibling doc file when README is absent/weak.

    Scans known purpose-bearing docs for a '## Purpose' section or the first
    substantive paragraph. Evidence-backed only — returns None otherwise.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return None
    candidates: list[Path] = []
    for name in _PURPOSE_DOC_NAMES:
        p = repo / name
        if p.is_file():
            candidates.append(p)
    # Also any top-level *.md with a Purpose section.
    try:
        children = sorted(repo.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.is_file() and child.suffix.lower() == ".md" and child.name not in _PURPOSE_DOC_NAMES:
            candidates.append(child)

    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.strip():
            continue
        # Explicit '## Purpose' / '## About' section first.
        sec = _section(text, "purpose") or _section(text, "about") or _section(text, "mission")
        if sec:
            first = _first_paragraph(sec)
            if len(first.split()) >= 4:
                return first
        # Otherwise the first real paragraph of the doc.
        para = _first_paragraph(text)
        if len(para.split()) >= 6:
            return para
    return None


def _purpose_from_layout(repo_path: str | Path, repo_name: str) -> Optional[str]:
    """Weak, last-resort purpose hint from directory structure / naming.

    Evidence must exist: a recognizable framework directory or a descriptive
    name. Returns None rather than invent when nothing supports a claim.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return None
    # Framework directories imply a kind of project.
    fw_map = {
        "next": "A Next.js web application.",
        "pages": "A Next.js web application.",
        "src": None,  # too generic
    }
    for marker, hint in fw_map.items():
        if (repo / marker).is_dir() and hint:
            return hint
    # Descriptive multi-word name (e.g. "finance-tracker", "mind-well").
    words = re.split(r"[_\- ]+", repo_name.lower())
    if len(words) >= 2 and all(len(w) > 2 for w in words):
        return f"A {words[0]} {words[1]} project."
    return None


def recover_purpose_fallback(repo_path: str | Path, repo_name: str) -> tuple[Optional[str], str, str]:
    """Deterministic purpose recovery when no usable README summary exists.

    Returns (purpose, source, confidence). Chain of authority:
      manifest description  >  doc-file purpose  >  layout/name hint.
    Each step is evidence-backed; the weakest (layout/name) is flagged Low
    confidence so callers can state it honestly. Never fabricates.
    """
    repo = Path(repo_path)
    desc = manifest_description(repo)
    if desc:
        return desc, "manifest description", "Medium"
    doc = _purpose_from_doc(repo)
    if doc:
        return doc, "project documentation", "Medium"
    layout = _purpose_from_layout(repo, repo_name)
    if layout:
        return layout, "repository name and layout", "Low"
    return None, "", "None"


# Phrases that mark a README as scaffold/boilerplate rather than real docs.
_BOILERPLATE_MARKERS = (
    "this is a template",
    "generated by",
    "created by",
    "a modern ",
    "a starter ",
    "starter template",
    "boilerplate",
    "todo: add",
    "this project is a",
    "Getting Started",
    "welcome to your",
    "your new",
)


def _word_count(text: str) -> int:
    return len(_strip_md(text).split())


def readme_quality(text: Optional[str]) -> str:
    """Classify README quality: none | boilerplate | poor | good."""
    if not text or not text.strip():
        return "none"
    stripped = _strip_md(text)
    words = _word_count(text)
    low = stripped.lower()
    # Boilerplate: a default scaffold template (generic phrases) or essentially empty.
    marker_hits = sum(1 for m in _BOILERPLATE_MARKERS if m in low)
    if words < 10 or marker_hits >= 2:
        return "boilerplate"
    # Poor: present but thin and lacking any structure/features.
    has_features = bool(_section(text, "features") or _section(text, "feature"))
    has_purpose = len(_first_paragraph(text).split()) >= 4
    if words < 100 and not (has_features and has_purpose):
        return "poor"
    return "good"


def readme_completeness(text: Optional[str]) -> str:
    """Classify README completeness: none | partial | complete."""
    if not text or not text.strip():
        return "none"
    has_purpose = len(_first_paragraph(text).split()) >= 4
    has_features = bool(_bullets(_section(text, "features") or _section(text, "feature") or ""))
    if has_purpose and has_features:
        return "complete"
    if has_purpose:
        return "partial"
    return "none"


def maturity_from_summary(summary: Optional[str]) -> Optional[str]:
    """Extract the Maturity field from a stored README summary.

    Handles both inline form (`Maturity: WIP`) and block form
    (`Maturity:\nWIP`) produced by the deterministic summary.
    """
    if not summary:
        return None
    lines = summary.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.lower().startswith("maturity:"):
            val = s.split(":", 1)[1].strip()
            if val:
                return val
            # Block form: value on the next non-empty line.
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return nxt.strip()
    return None
