"""Ingest orchestration: discover -> metadata -> tech -> readme -> store."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .discovery import Repo, discover_many
from .db import (
    LangRow,
    RelationshipRow,
    TechRow,
    replace_all_relationships,
    replace_children,
    set_repo_quality,
    upsert_repository,
)
from .architecture import analyze_and_store
from .gitmeta import collect
from .readme import maturity_from_summary, process, readme_completeness, readme_quality
from .summary import RepoView, build_views, infer_relationship_rows
from .tech import Detection, detect


@dataclass
class IngestReport:
    repos_found: int
    repos_stored: int
    llm_summaries: int


def _readme_text(repo: Repo):
    """Return the raw README text if present, else None (for quality scoring)."""
    from .readme import _find_readme

    path = _find_readme(repo.path)
    if path is None:
        return None
    return path.read_text(encoding="utf-8", errors="ignore").strip()


# ---------------------------------------------------------------------------
# Friday self-awareness (Phase 6, Task 3 — Option C).
#
# Keep Friday's own repo in the workspace so it can understand itself as a
# project (source code, Python stack, CLI architecture). But its README has
# been trimmed to just "Document", which feeds an empty identity into the
# portfolio/theme pipeline. We override the README summary with the project's
# own pyproject.toml description for a meaningful self-description, while
# still ingesting everything else (language stats, tech detection, architecture
# analysis) normally — those are accurate for real source code.
# ---------------------------------------------------------------------------

_FRIDAY_ROOT: str = ""


def _friday_root() -> Path:
    """Return the resolved project root for the Friday package itself.

    ingest.py lives at src/friday/ingest.py, so parents[0]=src/friday,
    parents[1]=src, parents[2]=project root (the git repo root).
    The project root is the directory containing pyproject.toml.
    """
    global _FRIDAY_ROOT
    if not _FRIDAY_ROOT:
        try:
            _FRIDAY_ROOT = str(Path(__file__).resolve().parents[2])
        except (IndexError, Exception):
            _FRIDAY_ROOT = ""
    return Path(_FRIDAY_ROOT) if _FRIDAY_ROOT else Path()


def _is_friday_repo(repo: Repo) -> bool:
    """Check if this discovered repo is Friday's own project directory."""
    root = _friday_root()
    if not root or not root.exists():
        return False
    try:
        return repo.path.resolve() == root.resolve()
    except (OSError, ValueError):
        return False


def _friday_readme_summary(repo: Repo) -> Optional[str]:
    """Read pyproject.toml description as a meaningful README summary for Friday.

    Returns a Purpose/Maturity summary derived from the project's own metadata
    instead of its trimmed README.md. Returns None if pyproject.toml is missing
    or unreadable (falls through to normal README processing).
    """
    try:
        pyproj_path = repo.path / "pyproject.toml"
        if not pyproj_path.is_file():
            return None
        text = pyproj_path.read_text(encoding="utf-8", errors="ignore")
        # Extract [project] description field.
        import re as _re
        m = _re.search(r'^description\s*=\s*["\'](.+?)["\']', text, _re.MULTILINE)
        if not m:
            return None
        desc = m.group(1).strip()
        if not desc:
            return None
        # Format as a minimal README summary (same format readme.py produces).
        return f"Purpose:\n{desc}\n\nMaturity:\nActive"
    except (OSError, IOError):
        return None


def ingest_paths(paths: list[Path], conn: sqlite3.Connection) -> IngestReport:
    repos: list[Repo] = discover_many(paths)
    report = IngestReport(repos_found=len(repos), repos_stored=0, llm_summaries=0)

    for repo in repos:
        meta = collect(repo)
        detections: list[Detection] = detect(repo)
        readme = process(repo)
        readme_text = _readme_text(repo)

        # Phase 6, Option C: for Friday's own repo, use pyproject.toml's
        # description as the README summary (the working README.md is just
        # "Document"). Everything else — language stats, tech detection,
        # architecture analysis — is accurate real signal and stays.
        if _is_friday_repo(repo):
            override = _friday_readme_summary(repo)
            if override:
                summary_text = override
            else:
                summary_text = readme.summary if readme else None
        else:
            summary_text = readme.summary if readme else None

        if readme and readme.used_llm:
            report.llm_summaries += 1

        repo_id = upsert_repository(
            conn,
            name=meta.name,
            path=meta.path,
            default_branch=meta.default_branch,
            is_dirty=meta.is_dirty,
            first_commit_date=meta.first_commit_date,
            last_commit_date=meta.last_commit_date,
            remote_url=meta.remote_url,
            commit_count=meta.commit_count,
            readme_summary=summary_text,
            license=meta.license,
            primary_author=meta.primary_author,
        )
        languages = [LangRow(language=l, file_count=c) for l, c in meta.languages.items()]
        technologies = [TechRow(tech=d.tech, evidence=d.evidence) for d in detections]
        replace_children(conn, repo_id, languages, technologies)

        # Identity-card fields: README quality / completeness / maturity.
        set_repo_quality(
            conn,
            repo_id,
            maturity=maturity_from_summary(summary_text),
            readme_quality=readme_quality(readme_text),
            readme_completeness=readme_completeness(readme_text),
        )

        # Milestone 3: persistent architectural knowledge.
        analyze_and_store(conn, repo)

        report.repos_stored += 1

    # Relationships are computed across all repos at once (pairwise).
    _store_relationships(conn)
    return report


def _store_relationships(conn: sqlite3.Connection) -> None:
    """Recompute and persist pairwise relationships for every repository."""
    views = build_views(conn)
    all_rows: list[RelationshipRow] = infer_relationship_rows(views)
    replace_all_relationships(conn, all_rows)
