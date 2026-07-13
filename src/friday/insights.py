"""Deterministic workspace insights, derived purely from stored metadata + SQL.

No LLM involvement. These power both the `summary` cross-project section and the
`ask` "insights" intent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .db import Repository
from .query import (
    _parse_date,
    abandoned_repos,
    all_repositories,
    duplicate_tech,
    most_active,
    newest_repos,
)


@dataclass
class Insight:
    text: str


def generate_insights(conn, today: Optional[dt.date] = None) -> list[Insight]:
    today = today or dt.date.today()
    repos = _repos(conn)
    out: list[Insight] = []

    # Newest repositories.
    newest = newest_repos(conn, 3)
    if newest:
        names = ", ".join(r.name for r in newest)
        out.append(Insight(text=f"The {len(newest)} newest repositories are: {names}."))
        if repos:
            out.append(
                Insight(text=f"{newest[0].name} is your newest project "
                          f"(first commit {newest[0].first_commit_date[:10]}).")
            )

    # Repository carrying the majority of workspace commits (lifetime share,
    # not recent velocity — most_active scores commits/day over the repo's age).
    active = most_active(conn, today, 3)
    if active and repos:
        top_repo, _ = active[0]
        total_commits = sum(r.commit_count or 0 for r in repos)
        if top_repo.commit_count and total_commits:
            share = top_repo.commit_count / total_commits
            if share >= 0.4:
                out.append(
                    Insight(text=f"{top_repo.name} has received the majority of commits "
                              f"({share:.0%} of all commits across the workspace).")
                )

    # Shared technologies (languages excluded — sharing a language is not a
    # "configuration to duplicate"; that wording was a bug, audit W8).
    dups = duplicate_tech(conn)
    non_lang = {
        t: names for t, names in dups.items()
        if t not in _code_langs_set(conn)
    }
    for tech, names in sorted(non_lang.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(
            Insight(text=f"Several projects use {tech} ({'/'.join(names)}).")
        )

    # Similar language layouts (same primary language ecosystem across many).
    lang_counts: dict[str, list[str]] = {}
    for r in repos:
        for lang in _code_langs(conn, r):
            lang_counts.setdefault(lang, []).append(r.name)
    for lang, names in sorted(lang_counts.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(names) >= 3:
            out.append(
                Insight(text=f"Multiple repositories ({len(names)}) share a {lang} layout.")
            )

    # Poor README quality.
    poor = [r.name for r in repos if r.readme_quality in ("poor", "boilerplate")]
    if len(poor) >= 1:
        out.append(
            Insight(text=f"{len(poor)} project(s) have poor README quality: "
                      f"{', '.join(poor)}.")
        )

    # Abandoned repositories.
    aban = abandoned_repos(conn, today)
    for r in aban:
        out.append(
            Insight(text=f"{r.name} looks abandoned (no commit in "
                      f"{(today - _parse_date(r.last_commit_date)).days} days).")
        )

    return out


def _repos(conn) -> list[Repository]:
    return all_repositories(conn)


def _code_langs(conn, repo: Repository) -> list[str]:
    if repo.id is None:
        return []
    from .db import get_languages
    from .summary import NON_CODE_LANGS

    rows = get_languages(conn, repo.id)
    return [r.language for r in rows if r.language not in NON_CODE_LANGS]


def _code_langs_set(conn) -> set[str]:
    """All code languages used across the workspace (for insight filtering)."""
    out: set[str] = set()
    for r in _repos(conn):
        out.update(_code_langs(conn, r))
    return out
