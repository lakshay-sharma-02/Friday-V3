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
    head_sha: Optional[str] = None
    manifest_hash: Optional[str] = None


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
        rpath = Path(r.path)
        if not rpath.exists():
            continue
        meta = collect(Repo(path=rpath))
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
            head_sha=meta.head_sha,
            manifest_hash=_manifest_hash(r.path),
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
            head_sha=snap.head_sha,
            manifest_hash=snap.manifest_hash,
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


# ===========================================================================
# Milestone 9.8 — Observe & Refresh.
#
# NOT a new subsystem. `refresh()` composes the EXISTING knowledge pipeline
# (ingest -> knowledge -> understanding -> initiative -> insight) on top of the
# existing change detection. Every layer's build() is idempotent, so re-running
# over unchanged evidence produces zero new rows — no duplication, no rescan
# storms. The dependency rules (portfolio only if identity/understanding
# changed; insights only if understanding changed) are honoured to skip work.
# ===========================================================================


@dataclass
class RefreshReport:
    """Outcome of a refresh. All counts are DELTAS for this run (created+updated)."""

    repos_scanned: int = 0
    repos_changed: int = 0
    knowledge_updated: int = 0
    understanding_updated: int = 0
    identity_updated: int = 0       # repos whose identity signature changed
    initiatives_changed: int = 0
    insights_changed: int = 0
    portfolio_updated: bool = False  # derived (read layer), not rebuilt
    elapsed_ms: int = 0
    changed_repos: list = None       # human-readable repo names that changed

    def to_text(self, changes: list = None) -> str:
        lines = ["Workspace refreshed\n"]
        lines.append(f"Repositories scanned:   {self.repos_scanned}")
        lines.append(f"Repositories changed:   {self.repos_changed}")
        lines.append(f"Knowledge updated:      {self.knowledge_updated}")
        lines.append(f"Understanding updated:  {self.understanding_updated}")
        lines.append(f"Identity updated:       {self.identity_updated}")
        lines.append(f"Portfolio updated:      {'yes' if self.portfolio_updated else 'no'}")
        lines.append(f"Insights updated:       {self.insights_changed}")
        lines.append(f"Elapsed:               {self.elapsed_ms / 1000:.1f}s")
        return "\n".join(lines) + "\n"


def _identity_signature_for(conn, repo_id: int, path: str = "") -> Optional[str]:
    """Stable identity signature from DISK/ON-INGEST evidence only.

    Uses the on-disk README *content* hash (not the stored `readme_summary`,
    which ingest may re-derive non-deterministically) plus the architecture
    hash and recorded maturity. This makes identity "change" track real edits
    to the repository, so an unchanged repo yields an identical signature
    run-to-run — refresh stays idempotent.
    """
    from .db import get_repositories, get_architecture

    row = next((r for r in get_repositories(conn) if r.id == repo_id), None)
    if row is None:
        return _sha("no-repo")
    arch = get_architecture(conn, repo_id)
    arch_sig = _sha("\n".join(filter(None, (
        arch.architecture, arch.evidence, arch.data_flow,
        arch.known_patterns, arch.complexity,
    ))) if arch else "no-architecture")
    return _sha("␟".join([
        _readme_hash(path) or str(row.readme_summary or ""),
        str(arch_sig),
        str(row.maturity or "Unknown"),
    ]))


_MANIFEST_NAMES = (
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "Pipfile", "package.json", "package-lock.json", "yarn.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "pom.xml", "build.gradle",
    "Gemfile", "composer.json", "poetry.lock",
)


def _manifest_hash(repo_path: str) -> Optional[str]:
    """sha over concatenated dependency-manifest contents (ingest-independent).

    Empty directory (no manifests) hashes a stable sentinel so deletion of the
    last manifest is detected as a change rather than a false-negative."""
    p = Path(repo_path)
    if not p.exists():
        return _sha(f"no-path:{repo_path}")
    chunks: list = []
    for name in _MANIFEST_NAMES:
        f = p / name
        if f.is_file():
            try:
                chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                chunks.append("")
    if not chunks:
        return _sha(f"no-manifest:{repo_path}")
    return _sha("\n".join(chunks))


def _repo_signature(conn, repo_id: int, path: str) -> tuple:
    """Stable, deterministic observable signature of a repo (BEFORE/AFTER).

    Built ONLY from **ingest-independent** evidence so re-running over an
    unchanged repo yields an identical signature (idempotency) — ingest output
    (architecture, knowledge) is explicitly excluded because it can re-derive
    slightly on re-ingest and would create false-positive "changes":

      - HEAD SHA (``collect``) — strongest single git signal; a new commit
        changes the SHA even when the count is unavailable.
      - dirty (live ``collect``) — uncommitted working-tree changes.
      - README hash (on-disk content) — docs edits.
      - manifest hash (on-disk dependency files) — dependency changes.

    Identity change is measured separately via ``_identity_signature_for``.
    """
    meta = collect(Repo(path=Path(path)))
    return (
        meta.head_sha,
        meta.is_dirty,
        _readme_hash(path),
        _manifest_hash(path),
    )


def refresh(conn, repos: Optional[list] = None,
            only_changed: bool = False) -> RefreshReport:
    """Refresh the workspace knowledge stack from current repository state.

    Composes existing pieces:
      1. Re-ingest each target repo (idempotent upserts of architecture/README/
         tech/quality + pairwise relationships).
      2. Rebuild knowledge -> understanding -> initiative -> insight. Each build()
         is idempotent; unchanged evidence yields 0 new rows.
      3. Apply dependency rules: insights rebuild only if understanding changed;
         portfolio is a read-derived view, so it is "updated" iff identity or
         understanding changed.

    `repos` bounds the refresh to a single repository (path or name) or a list.
    `only_changed` restricts to repos whose observable signature changed vs the
    prior stored state (skip expensive re-ingest of untouched repos).

    Returns a RefreshReport with deltas + elapsed time. Never raises for a clean
    miss; an unknown `<repo>` yields a zero-count report.
    """
    import time
    from .db import get_repositories
    from .discovery import Repo, discover_many
    from .ingest import ingest_paths
    from .knowledge import KnowledgeEngine
    from .understanding import UnderstandingEngine
    from .initiative import InitiativeEngine
    from .insight import InsightEngine

    t0 = time.monotonic()
    rep = RefreshReport()

    # Resolve which repositories to refresh.
    all_rows = get_repositories(conn)
    if repos:
        targets = []
        for r in repos:
            # Match by resolved path or by name.
            rp = str(r)
            match = next((row for row in all_rows
                          if row.path == rp or row.name == rp), None)
            if match is None:
                # Maybe an on-disk path not yet ingested — let ingest discover it.
                p = Path(rp).expanduser().resolve()
                if p.exists():
                    targets.append(str(p))
            elif match.path not in targets:
                targets.append(match.path)
        roots = [Path(t) for t in targets]
    else:
        # Refresh the whole known workspace: ingest roots = stored repo roots.
        roots = [Path(r.path) for r in all_rows if r.path]

    rep.repos_scanned = len(roots)
    if not roots:
        rep.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return rep

    # Change baseline = the most recent prior-run signature persisted by
    # take_snapshot (HEAD/dirty/README/manifest). The current run's live disk
    # signature is compared against it. A repo with no prior baseline (first
    # run, or newly discovered) is treated as changed. This is all ingest-
    # independent, so re-running over an unchanged repo compares equal and
    # produces zero work (idempotency).
    baseline_sig = _last_snapshot_signature(conn)

    # Discover the in-scope repos and their CURRENT live disk signatures.
    discovered = discover_many(roots)
    cur_sig: dict = {}
    ident_before: dict = {}
    for d in discovered:
        rid = next((r.id for r in get_repositories(conn)
                    if r.path == str(d.path) and r.id is not None), None)
        cur_sig[str(d.path)] = _repo_signature(conn, rid, str(d.path))
        if rid is not None:
            ident_before[str(d.path)] = _identity_signature_for(conn, rid, str(d.path))

    # `--changed`: only re-ingest repos whose current disk state differs from
    # the prior baseline. Skips untouched repositories entirely.
    if only_changed:
        discovered = [
            d for d in discovered
            if baseline_sig.get(str(d.path)) is None
            or baseline_sig.get(str(d.path)) != cur_sig[str(d.path)]
        ]

    # Record the current disk state for the next run's baseline.
    take_snapshot(conn)

    # Re-ingest (idempotent). Architecture/README/tech/quality + relationships.
    if discovered:
        ingest_paths([d.path for d in discovered], conn)

    rows = get_repositories(conn)
    path_to_id = {r.path: r.id for r in rows if r.id is not None}

    # Only repos discovered in THIS refresh's scope can have changed; the rest
    # are out of scope (whole-workspace refresh = every stored repo's root).
    discovered_paths = {str(d.path) for d in discovered}
    changed_paths: list = []
    changed_names: list = []
    for r in rows:
        if r.path not in discovered_paths:
            continue
        prior = baseline_sig.get(r.path)
        if prior is None or prior != cur_sig.get(r.path):
            changed_paths.append(r.path)
            changed_names.append(r.name)

    rep.repos_changed = len(changed_paths)
    rep.changed_repos = changed_names

    # Identity "updated" = changed repos whose stable identity hash shifted
    # (README content / architecture / maturity) vs pre-re-ingest.
    rep.identity_updated = sum(
        1 for p in changed_paths
        if ident_before.get(p) != _identity_signature_for(
            conn, path_to_id.get(p), p)
    )

    # If nothing's observable state changed, SKIP the expensive rebuilds
    # entirely (spec: "if nothing changed, skip expensive work"). This also
    # keeps refresh idempotent: a no-change re-run produces zero new rows and
    # avoids re-summarization ripple. Rebuild only when evidence moved.
    if rep.repos_changed > 0:
        # --- Knowledge + Understanding (rebuild; report rows touched) --------
        # Report created+updated: a real change may update existing knowledge
        # rather than create new rows, yet the layer still refreshed.
        kres = KnowledgeEngine(conn).build()
        rep.knowledge_updated = kres.created + kres.updated
        ures = UnderstandingEngine(conn).build()
        rep.understanding_updated = ures.created + ures.updated
        ires = InitiativeEngine(conn).build()
        rep.initiatives_changed = ires.created + ires.updated

        # --- Insights: only recompute if understanding gained NEW entries ----
        # (dependency rule: insights depend on understanding).
        if rep.understanding_updated > 0:
            ivres = InsightEngine(conn).build()
            rep.insights_changed = ivres.created + ivres.retired
        else:
            rep.insights_changed = 0

        # --- Portfolio: read-derived; "updated" iff identity/understanding --
        rep.portfolio_updated = (rep.identity_updated > 0
                                 or rep.understanding_updated > 0)
    else:
        rep.knowledge_updated = 0
        rep.understanding_updated = 0
        rep.identity_updated = 0
        rep.initiatives_changed = 0
        rep.insights_changed = 0
        rep.portfolio_updated = False

    rep.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return rep


def _last_snapshot_signature(conn) -> dict:
    """Map repo_path -> ingest-independent signature from the most recent
    ``snapshots`` row (written by ``take_snapshot``):
    (head_sha, dirty, readme_hash, manifest_hash).

    This is the SAME 4-tuple ``_repo_signature`` computes live from disk, so the
    ``--changed`` baseline compares apples-to-apples. Before these columns
    existed (pre-M9.8 DBs) the row may be NULL; we fall back to
    (commit_count, dirty, readme_hash, architecture_hash) which is a close but
    non-identical proxy — acceptable for legacy databases only.
    """
    rows = conn.execute(
        "SELECT repo_path, commit_count, is_dirty, readme_hash, "
        "architecture_hash, head_sha, manifest_hash FROM snapshots "
        "WHERE observed_at = (SELECT MAX(observed_at) FROM snapshots)"
    ).fetchall()
    out: dict = {}
    for r in rows:
        if r["head_sha"] is not None or r["manifest_hash"] is not None:
            out[r["repo_path"]] = (r["head_sha"], bool(r["is_dirty"]),
                                    r["readme_hash"], r["manifest_hash"])
        else:
            out[r["repo_path"]] = (r["commit_count"], bool(r["is_dirty"]),
                                    r["readme_hash"], r["architecture_hash"])
    return out

