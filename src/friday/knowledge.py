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


def ingest_paths(paths: list[Path], conn: sqlite3.Connection) -> IngestReport:
    repos: list[Repo] = discover_many(paths)
    report = IngestReport(repos_found=len(repos), repos_stored=0, llm_summaries=0)

    for repo in repos:
        meta = collect(repo)
        detections: list[Detection] = detect(repo)
        readme = process(repo)
        readme_text = _readme_text(repo)

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
        report.repos_stored += 1

    # Relationships are computed across all repos at once (pairwise).
    _store_relationships(conn)
    return report


def _store_relationships(conn: sqlite3.Connection) -> None:
    """Recompute and persist pairwise relationships for every repository."""
    views = build_views(conn)
    all_rows: list[RelationshipRow] = infer_relationship_rows(views)
    replace_all_relationships(conn, all_rows)
