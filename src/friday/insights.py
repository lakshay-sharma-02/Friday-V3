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
    get_technologies,
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

    # --- Milestone 6 (F2/F7): surprising engineering observations FIRST ------
    # A senior engineer surfaces the non-obvious before the obvious. These are
    # evidence-cited and silent when unsupported; they must not be buried under
    # raw facts like "newest repository".
    out.extend(_engineering_insights(conn, today))

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


def _engineering_insights(conn, today: dt.date) -> list[Insight]:
    """Non-obvious engineering observations derived from stored evidence.

    Only emitted when supported by Medium/Strong evidence. This is the layer
    that answers 'tell me something I haven't noticed' — it must avoid obvious
    facts (newest repo, shared language) and never fabricate."""
    from .db import get_all_relationships, get_architecture
    from . import judgment

    out: list[Insight] = []

    # Repeated solution: two repos sharing a Medium/Strong *implementation-level*
    # relationship (not a coincidence) suggests a problem solved twice.
    rels = get_all_relationships(conn)
    seen_pairs: set[tuple[str, str]] = set()
    name_by_id = {r.id: r.name for r in all_repositories(conn)}
    for rel in rels:
        if rel.strength == "Weak":
            continue
        if rel.kind in ("duplicated-functionality", "shared-abstraction",
                        "shared-implementation"):
            key = tuple(sorted((rel.repo_a, rel.repo_b)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            an, bn = name_by_id.get(rel.repo_a), name_by_id.get(rel.repo_b)
            if an and bn:
                out.append(Insight(
                    text=f"{an} and {bn} appear to solve a similar problem "
                         f"({rel.evidence}) — you may have built the same "
                         f"capability twice."))

    # Converging trend: a technology whose repo-count grew between the last two
    # observations (requires observation history from M5).
    trend = _converging_tech_trend(conn)
    if trend:
        tech, before, after = trend
        out.append(Insight(
            text=f"Your work is converging on {tech}: it now appears in {after} "
                 f"repositories, up from {before} at the previous observation."))

    # Commercial shift: a commercial/product-themed repo carrying the majority
    # of commits — effort is tilting toward commercial work.
    shift = _commercial_shift(conn, today)
    if shift:
        out.append(Insight(text=shift))

    return out


def _converging_tech_trend(conn) -> Optional[tuple[str, int, int]]:
    """Best tech whose repo-count rose between the two most recent observations.
    Returns (tech, prev_count, cur_count) or None (no history / no growth)."""
    from .db import latest_observation
    from . import query as q

    snaps = latest_observation(conn)
    if not snaps:
        return None
    latest_time = snaps[0].observed_at
    rows = conn.execute(
        "SELECT observed_at, repo_path, repo_name FROM snapshots "
        "ORDER BY observed_at DESC"
    ).fetchall()
    times = sorted({r["observed_at"] for r in rows}, reverse=True)
    if len(times) < 2:
        return None
    prev_time, cur_time = times[1], times[0]
    prev_paths = {r["repo_path"] for r in rows if r["observed_at"] == prev_time}
    cur_paths = {r["repo_path"] for r in rows if r["observed_at"] == cur_time}

    prev_tech: dict[str, set[str]] = {}
    cur_tech: dict[str, set[str]] = {}
    for r in q.all_repositories(conn):
        techs = {t.tech for t in get_technologies(conn, r.id)}
        if r.path in prev_paths:
            prev_tech.setdefault(r.path, set()).update(techs)
        if r.path in cur_paths:
            cur_tech.setdefault(r.path, set()).update(techs)

    best = None
    for tech in set().union(*cur_tech.values()) if cur_tech else set():
        before = sum(1 for s in prev_tech.values() if tech in s)
        after = sum(1 for s in cur_tech.values() if tech in s)
        if after > before and (best is None or after - before > best[2] - best[1]):
            best = (tech, before, after)
    return best


def _commercial_shift(conn, today: dt.date) -> Optional[str]:
    """Return an insight string if a commercial/product repo leads workspace
    commit share, else None."""
    from .portfolio import detect_themes
    from .query import most_active

    repos = all_repositories(conn)
    if not repos:
        return None
    comm = [t for t in detect_themes(conn, today)
            if t.theme in ("Products", "Commercial applications")]
    if not comm:
        return None
    names = {n for t in comm for n in t.repos}
    active = {r.id: s for r, s in most_active(conn, today, len(repos))}
    facts = {r.id: r for r in repos}
    for rid, share_score in sorted(active.items(), key=lambda kv: -kv[1]):
        r = facts.get(rid)
        if r and r.name in names:
            total = sum(x.commit_count or 0 for x in repos)
            if r.commit_count and total and r.commit_count / total >= 0.4:
                return (f"Commercial work is becoming your dominant engineering "
                        f"effort: {r.name} carries the majority of workspace commits.")
    return None


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
