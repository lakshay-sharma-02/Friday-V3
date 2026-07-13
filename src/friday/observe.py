"""Milestone 5 — continuous observation.

Friday records the current engineering workspace as an append-only snapshot and
reports only the *meaningful differences* since the previous observation. This
is observation only: it reads git facts and stored knowledge, stores the
snapshot, and diffs — it never interprets, advises, or re-analyzes. No LLM, no
architecture/relationship mutation. Running `friday observe` is the sole
trigger (no daemon, scheduler, or watcher).

Storage lives in db.py (snapshots table, insert_snapshot, latest_observation).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import identity
from .db import SnapshotRow, insert_snapshot, latest_observation, now_iso
from .discovery import Repo
from .gitmeta import collect
from .readme import _find_readme


@dataclass
class ObservationSnapshot:
    observed_at: str
    repo_path: str
    repo_name: Optional[str]
    default_branch: Optional[str]
    commit_count: Optional[int]
    last_commit_date: Optional[str]
    is_dirty: bool
    readme_hash: Optional[str]
    architecture_hash: Optional[str]
    identity_hash: Optional[str]


def _sha(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _readme_hash(repo_path: str) -> Optional[str]:
    """sha of the on-disk README if present; else sha of a stable sentinel
    (so a missing README does not false-positive as 'changed')."""
    p = _find_readme(Path(repo_path))
    if p is None:
        return _sha(f"no-readme:{repo_path}")
    try:
        return _sha(p.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return _sha(f"no-readme:{repo_path}")


def _architecture_hash(conn, repo_id: int) -> Optional[str]:
    from .db import get_architecture

    arch = get_architecture(conn, repo_id)
    if arch is None:
        return _sha("no-architecture")
    return _sha("\n".join(filter(None, (
        arch.architecture, arch.evidence, arch.data_flow,
        arch.known_patterns, arch.complexity,
    ))))


def _identity_signature(ident: Optional[identity.ProjectIdentity]) -> Optional[str]:
    if ident is None:
        return _sha("no-identity")
    parts = [
        ident.purpose or "",
        ident.maturity,
        ident.activity,
        ident.phase or "",
        ident.importance or "",
        ident.business_value or "",
        ",".join(sorted(ident.technologies)),
        ",".join(sorted(ident.related_projects)),
        ",".join(sorted(ident.blockers)),
        ",".join(sorted(ident.evidence_sources)),
    ]
    return _sha("␟".join(parts))


def take_snapshot(conn) -> tuple[str, list[ObservationSnapshot]]:
    """Capture the current workspace. Returns (observed_at, per-repo snapshots).

    Git facts are re-collected live (dirty state, commit count, last commit,
    branch) so change is detected even without a full re-ingest. The three
    hashes (README / architecture / identity) are derived from current on-disk
    and stored state. Writes all rows with one shared observed_at to db.
    """
    from .db import get_repositories

    observed_at = now_iso()
    snaps: list[ObservationSnapshot] = []
    for r in get_repositories(conn):
        if r.id is None:
            continue
        meta = collect(Repo(path=Path(r.path)))
        ident = identity.build_identity(conn, r.id)
        snap = ObservationSnapshot(
            observed_at=observed_at,
            repo_path=r.path,
            repo_name=r.name,
            default_branch=meta.default_branch,
            commit_count=meta.commit_count,
            last_commit_date=meta.last_commit_date,
            is_dirty=meta.is_dirty,
            readme_hash=_readme_hash(r.path),
            architecture_hash=_architecture_hash(conn, r.id),
            identity_hash=_identity_signature(ident),
        )
        snaps.append(snap)
        insert_snapshot(conn, SnapshotRow(
            observed_at=snap.observed_at,
            repo_path=snap.repo_path,
            repo_name=snap.repo_name,
            default_branch=snap.default_branch,
            commit_count=snap.commit_count,
            last_commit_date=snap.last_commit_date,
            is_dirty=snap.is_dirty,
            readme_hash=snap.readme_hash,
            architecture_hash=snap.architecture_hash,
            identity_hash=snap.identity_hash,
        ))
    return observed_at, snaps


def _by_path(rows) -> dict[str, object]:
    return {r.repo_path: r for r in rows}


def diff_snapshots(prev, cur) -> list[str]:
    """Produce only meaningful changes: added, removed, renamed, then per-repo
    field changes. Unchanged repositories are intentionally NOT reported."""
    prev_by = _by_path(prev)
    cur_by = _by_path(cur)
    changes: list[str] = []

    # Added / renamed (same path, different name) and per-repo changes.
    for path, c in cur_by.items():
        p = prev_by.get(path)
        if p is None:
            changes.append(f"New repository detected: {c.repo_name}.")
            continue
        if p.repo_name != c.repo_name:
            changes.append(f"{p.repo_name} was renamed to {c.repo_name}.")
        if p.is_dirty and not c.is_dirty:
            changes.append(f"{c.repo_name} is now clean (no uncommitted changes).")
        elif not p.is_dirty and c.is_dirty:
            changes.append(f"{c.repo_name} now has uncommitted changes.")
        if p.commit_count is not None and c.commit_count is not None:
            delta = c.commit_count - p.commit_count
            if delta > 0:
                changes.append(f"{c.repo_name} gained {delta} commits.")
            elif delta < 0:
                changes.append(f"{c.repo_name} lost {-delta} commits.")
        if p.default_branch != c.default_branch:
            changes.append(
                f"{c.repo_name} moved from {p.default_branch} to {c.default_branch}."
            )
        if p.readme_hash != c.readme_hash:
            changes.append(f"{c.repo_name} README changed.")
        if p.architecture_hash != c.architecture_hash:
            changes.append(f"{c.repo_name} architecture changed.")
        if p.identity_hash != c.identity_hash:
            changes.append(f"{c.repo_name} identity changed.")

    # Removed.
    for path, p in prev_by.items():
        if path not in cur_by:
            changes.append(f"Repository removed: {p.repo_name}.")

    if not changes:
        return ["No significant workspace changes detected."]
    return changes


def observe(conn) -> tuple[Optional[str], list[str]]:
    """Run one observation: fetch previous, store current, return
    (previous observed_at, diff bullet lines)."""
    prev = latest_observation(conn)
    prev_time = prev[0].observed_at if prev else None
    _, cur = take_snapshot(conn)
    changes = diff_snapshots(prev, cur)
    return prev_time, changes


def format_report(prev_time: Optional[str], changes: list[str]) -> str:
    since = prev_time if prev_time else "(no prior observation — baseline recorded)"
    header = "Friday Observation\nSince " + since + "\n"
    if prev_time is None:
        return header + "\n• Baseline observation recorded.\n"
    bullets = "\n".join(f"• {c}" for c in changes)
    return header + "\n" + bullets + "\n"
