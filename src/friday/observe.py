"""Milestone 5/7 — continuous observation.

Milestone 5 snapshot machinery (append-only `snapshots` table, `diff_snapshots`,
`format_report`, `observe`) is preserved in full so existing benchmarks keep
passing. Milestone 7 routes the live `friday observe` command through the
generic Observation Engine: `GitObserver` supplies deterministically-read git
facts, the engine persists and diffs them, and `observe_via_engine` translates
the engine's Change records into the same engineering-language vocabulary.

No LLM, no daemon, no planner. Observation is pull-based: `friday observe` is
the sole trigger.
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
from .observation import (
    Change as EngineChange,
    ObservationEngine,
    default_registry,
    format_run,
)


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


@dataclass
class Change:
    """A single meaningful workspace change, in engineering language.

    `kind` is the engineering concept (never internal vocab like "identity").
    `cause` is the evidence-backed reason, or None when not derivable.
    """

    repo: str
    kind: str          # e.g. "purpose", "architecture", "README", "became dirty"
    old: Optional[object] = None
    new: Optional[object] = None
    cause: Optional[str] = None


def diff_snapshots(prev, cur) -> list[Change]:
    """Produce only meaningful changes as structured Change records: added,
    removed, renamed, then per-repo field changes. Unchanged repositories are
    intentionally NOT reported. Internal vocabulary (e.g. "identity") is mapped
    to the specific engineering concept that changed, with a cause when known."""
    prev_by = _by_path(prev)
    cur_by = _by_path(cur)
    changes: list[Change] = []

    for path, c in cur_by.items():
        p = prev_by.get(path)
        if p is None:
            changes.append(Change(repo=c.repo_name, kind="new repository"))
            continue
        if p.repo_name != c.repo_name:
            changes.append(Change(
                repo=c.repo_name, kind="renamed", old=p.repo_name, new=c.repo_name,
                cause=f"path {path} now reports the name {c.repo_name}.",
            ))
        if p.is_dirty and not c.is_dirty:
            changes.append(Change(
                repo=c.repo_name, kind="became clean",
                cause="uncommitted changes were committed or reverted.",
            ))
        elif not p.is_dirty and c.is_dirty:
            changes.append(Change(
                repo=c.repo_name, kind="became dirty",
                cause="you have uncommitted changes in the working tree.",
            ))
        if p.commit_count is not None and c.commit_count is not None:
            delta = c.commit_count - p.commit_count
            if delta > 0:
                changes.append(Change(
                    repo=c.repo_name, kind="commits gained", new=delta,
                    cause=f"you added {delta} commit(s) since the last observation.",
                ))
            elif delta < 0:
                changes.append(Change(
                    repo=c.repo_name, kind="commits lost", new=-delta,
                    cause=f"the history lost {-delta} commit(s) (e.g. rebase/reset).",
                ))
        if p.default_branch != c.default_branch:
            changes.append(Change(
                repo=c.repo_name, kind="branch changed", old=p.default_branch,
                new=c.default_branch,
                cause=f"the checked-out branch moved from {p.default_branch} to {c.default_branch}.",
            ))
        if p.readme_hash != c.readme_hash:
            # README changed. If the identity purpose is unchanged it was a docs
            # edit; if purpose changed too, the README drove the purpose change.
            cause = "the README summary changed."
            if p.identity_hash != c.identity_hash:
                cause = "the README summary changed, which updated the project's purpose."
            changes.append(Change(
                repo=c.repo_name, kind="README changed", cause=cause))
        if p.architecture_hash != c.architecture_hash:
            changes.append(Change(
                repo=c.repo_name, kind="architecture changed",
                cause="new framework or implementation evidence appeared.",
            ))
        if p.identity_hash != c.identity_hash:
            # Decompose identity into the specific engineering concept.
            _identity_changes(c.repo_name, p, c, changes)

    for path, p in prev_by.items():
        if path not in cur_by:
            changes.append(Change(repo=p.repo_name, kind="repository removed"))

    if not changes:
        return [Change(repo="", kind="no changes")]
    return changes


def _identity_changes(name: str, p, c, out: list[Change]) -> None:
    """Emit the specific engineering-concept change(s) behind an identity hash
    delta. We do not have the raw fields in the snapshot, so we attribute by
    elimination against the other hashes:
      - README changed but architecture same -> purpose/maturity changed via docs.
      - architecture changed -> technology stack / architecture changed.
      - neither -> project maturity / business focus changed.
    Each carries a cause so the report never says bare 'identity changed'."""
    if p.readme_hash != c.readme_hash:
        out.append(Change(
            repo=name, kind="purpose changed",
            cause="the README summary changed."))
    if p.architecture_hash != c.architecture_hash:
        out.append(Change(
            repo=name, kind="technology stack changed",
            cause="the detected architecture/framework evidence changed."))
    if p.readme_hash == c.readme_hash and p.architecture_hash == c.architecture_hash:
        out.append(Change(
            repo=name, kind="project maturity changed",
            cause="the recorded maturity or business focus changed."))


def observe(conn) -> tuple[Optional[str], list[Change]]:
    """Run one observation: fetch previous, store current, return
    (previous observed_at, Change records)."""
    prev = latest_observation(conn)
    prev_time = prev[0].observed_at if prev else None
    _, cur = take_snapshot(conn)
    changes = diff_snapshots(prev, cur)
    return prev_time, changes


_ENGINEERING_LABEL = {
    "new repository": "New repository detected",
    "renamed": "was renamed",
    "became clean": "is now clean (no uncommitted changes)",
    "became dirty": "now has uncommitted changes",
    "commits gained": "gained commits",
    "commits lost": "lost commits",
    "branch changed": "moved branch",
    "README changed": "README changed",
    "architecture changed": "architecture changed",
    "purpose changed": "purpose changed",
    "technology stack changed": "technology stack changed",
    "project maturity changed": "project maturity changed",
    "repository removed": "Repository removed",
    "no changes": None,
}


def _render_change(ch: Change) -> str:
    label = _ENGINEERING_LABEL.get(ch.kind)
    if label is None and ch.kind == "no changes":
        return "No significant workspace changes detected."
    if ch.kind == "new repository":
        return f"New repository detected: {ch.repo}."
    if ch.kind == "renamed":
        return f"{ch.old} was renamed to {ch.repo}."
    if ch.kind == "branch changed":
        return f"{ch.repo} moved from {ch.old} to {ch.new}."
    if ch.kind == "commits gained":
        return f"{ch.repo} gained {ch.new} commits."
    if ch.kind == "commits lost":
        return f"{ch.repo} lost {ch.new} commits."
    if ch.kind == "repository removed":
        return f"Repository removed: {ch.repo}."
    # Generic: "<repo> <label>." + cause when known.
    line = f"{ch.repo} {label}."
    if ch.cause:
        line = f"{ch.repo} {label} because {ch.cause}"
    return line


def format_report(prev_time: Optional[str], changes: list[Change]) -> str:
    since = prev_time if prev_time else "(no prior observation — baseline recorded)"
    header = "Friday Observation\nSince " + since + "\n"
    if prev_time is None:
        return header + "\n• Baseline observation recorded.\n"
    bullets = "\n".join(f"• {_render_change(c)}" for c in changes)
    return header + "\n" + bullets + "\n"


# ---------------------------------------------------------------------------
# Milestone 7 — Observation Engine route for `friday observe`.
# ---------------------------------------------------------------------------


def observe_via_engine(conn) -> tuple[Optional[str], list[Change]]:
    """Run the live observation through the generic Observation Engine.

    Returns (previous observed_at, M5-compatible Change records) so the CLI can
    share `format_report`. GitObserver supplies the facts; the engine persists
    and diffs them. We translate the engine's Change records into the same
    vocabulary `format_report` understands (dirty, commits gained/lost, branch
    changed/switch, dormant repo, repeated reverts, merge events).
    """
    run = ObservationEngine(default_registry(), conn).run()
    prev = _prior_observed_at(conn)
    changes = _translate_run(run)
    return prev, changes


def _prior_observed_at(conn) -> Optional[str]:
    from .db import latest_observations
    rows = latest_observations(conn)
    return rows[0].observed_at if rows else None


# Map engine Change.kind -> M5-compatible Change record. Only *meaningful*
# changes are surfaced; first-sighting facts ("... observed") and "... removed"
# are baseline noise for the engineering report and are dropped.
_ASPECT_LABEL = {
    "dirty changed": "became dirty",
    "branch changed": "branch changed",
    "branch_switch": "branch changed",
    "commit_count changed": "commits gained",
    "dormant": "became dormant",
    "repeated_reverts": "repeated reverts",
    "merge_events": "merge activity",
}


def _translate_run(run) -> list[Change]:
    out: list[Change] = []
    for ores in run.observers:
        for ec in ores.changes:
            # Skip first-sighting and removal noise; only map meaningful diffs.
            if ec.kind.endswith(" observed") or ec.kind.endswith(" removed"):
                continue
            kind = _ASPECT_LABEL.get(ec.kind)
            if kind is None:
                continue
            if kind == "became dirty":
                if ec.new == "true":
                    out.append(Change(
                        repo=ec.subject, kind="became dirty",
                        cause=ec.cause or "uncommitted changes in the working tree."))
                else:
                    out.append(Change(
                        repo=ec.subject, kind="became clean",
                        cause=ec.cause or "uncommitted changes were committed."))
            elif kind == "branch changed":
                if ec.old and ec.new:
                    out.append(Change(
                        repo=ec.subject, kind="branch changed", old=ec.old,
                        new=ec.new,
                        cause=ec.cause or f"branch moved from {ec.old} to {ec.new}."))
            elif kind == "commits gained":
                try:
                    delta = int(ec.new) - int(ec.old or 0)
                except (TypeError, ValueError):
                    delta = 0
                if delta > 0:
                    out.append(Change(
                        repo=ec.subject, kind="commits gained", new=delta,
                        cause=ec.cause or f"you added {delta} commit(s)."))
                elif delta < 0:
                    out.append(Change(
                        repo=ec.subject, kind="commits lost", new=-delta,
                        cause=ec.cause or f"history lost {-delta} commit(s)."))
            elif kind == "became dormant":
                out.append(Change(
                    repo=ec.subject, kind="became dormant",
                    cause=ec.cause or "repository idle for 30+ days."))
            elif kind == "repeated reverts":
                out.append(Change(
                    repo=ec.subject, kind="repeated reverts",
                    cause=ec.cause))
            elif kind == "merge activity":
                if ec.new and ec.new != "0":
                    out.append(Change(
                        repo=ec.subject, kind="merge activity", new=ec.new,
                        cause=ec.cause))
    if not out:
        return [Change(repo="", kind="no changes")]
    return out


# Re-export engine formatter for the new CLI commands.
format_engine_report = format_run
