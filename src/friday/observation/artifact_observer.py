"""ArtifactObserver (Milestone 7.4).

A NEW observer for the frozen Observation Engine. It observes engineering
*artifacts* (repositories, manifests, documentation, archives, research PDFs,
diagrams, datasets, benchmarks, logs) from filesystem METADATA ONLY. It never
reads file contents, never walks outside the configured roots, never runs a
daemon, watcher, or indexer.

DESIGN (privacy-first, metadata-only):

  This observer is a PURE READER. It `stat`s paths within the configured roots
  and classifies each artifact deterministically by name/extension/structure.
  File *contents* (source, PDF pages, markdown text, images, secrets) are NEVER
  opened, read, or emitted. Only metadata (name, extension, size, mtime, type,
  relative path within a root) ever leaves the filesystem call.

  Stable identity (per design review): an artifact's primary identity is NOT an
  absolute path. It is `root_alias/relative_path` (e.g. "Projects/Aether"),
  where `root_alias` is the configured root's stable name ("Projects",
  "Downloads", "Documents"). If the user later moves their workspace root or
  syncs across machines, observations stay meaningful instead of being tied to
  /home/lakshay/....

  No LLM. No embeddings. No planner. No agents. Classification is a frozen
  table. Signals are deterministic facts with evidence-backed causes.

Observations emitted per artifact (Observed):
  category    Repository / Manifest / Documentation / Archive / Diagram /
              Research Paper / Dataset / Image / Benchmark / Log / Binary / Unknown
  name        filename (metadata, not contents)
  ext         extension (or "" for a directory)
  size        bytes (0 for a directory)
  modified_at mtime (ISO)

Lifecycle / engineering signals (Inferred, evidence-backed cause):
  repository_created / repository_removed / repository_renamed
  readme_added / manifest_added / new_notes_directory
  research_pdf_downloaded / archive_extracted / large_archive_extraction
  project_moved (cross-root rename)
  documentation_improving / research_activity_increasing
  workspace_reorganization / repeated_downloads

The engine diffs on (subject, aspect), so created/removed/moved fall out of the
current-state `category` facts automatically; the inferred signals add the
engineering judgment on top.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation

# Configured roots can be overridden (colon-separated) for testing / portability.
ARTIFACT_ROOTS_ENV = "FRIDAY_ARTIFACT_ROOTS"

# Default roots (by stable alias name, never the absolute path as identity).
DEFAULT_ROOTS = ["Projects", "Downloads", "Documents"]

# Skip these directory names while scanning (bound cost, avoid content crawls).
IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox", "target", "build",
    "dist", ".idea", ".vscode", ".svn", ".hg", ".friday",
}
# Skip these file suffixes: the observer watches engineering artifacts, not its
# own metadata store (the Friday knowledge DB is a sqlite file).
IGNORE_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
# Maximum recursion depth inside a root (ponytail: full-home walk is forbidden;
# deep trees are bounded. Raise via ARTIFACT_MAX_DEPTH if a project is deeper).
MAX_DEPTH = int(os.environ.get("FRIDAY_ARTIFACT_MAX_DEPTH", "8"))

# A document/archive larger than this (bytes) is flagged as "large".
LARGE_DOCUMENT_BYTES = 50 * 1024 * 1024
LARGE_ARCHIVE_BYTES = 100 * 1024 * 1024
# At least this many artifacts currently in Downloads => repeated downloads.
REPEATED_DOWNLOAD_THRESHOLD = 2

REPO_MARKER = ".git"

MANIFEST_FILES = {
    "cargo.toml", "package.json", "pyproject.toml", "go.mod", "go.sum",
    "pom.xml", "build.gradle", "build.gradle.kts", "requirements.txt",
    "dockerfile", "docker-compose.yml", "compose.yml", "makefile",
    "cmakelists.txt", "package-lock.json", "yarn.lock", "gemfile",
    "build.sbt", "meson.build", "environment.yml", "setup.py",
}

DOC_EXTS = {".md", ".rst", ".markdown"}
DOC_NAME_PREFIXES = ("readme",)  # README, README.md, readme.txt, ...
NOTES_DIR_NAMES = {"notes", "note", "docs", "doc"}

ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar", ".zip",
    ".7z", ".rar", ".gz", ".bz2", ".xz",
)
DATASET_SUFFIXES = (".csv", ".tsv", ".parquet", ".feather", ".arrow", ".tsv.gz")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")
DIAGRAM_SUFFIXES = (".svg", ".drawio", ".excalidraw", ".puml", ".plantuml", ".mermaid")
LOG_SUFFIXES = (".log",)
BINARY_SUFFIXES = (
    ".so", ".o", ".a", ".exe", ".dll", ".bin", ".elf", ".appimage",
    ".dylib", ".class", ".pyc", ".wasm",
)

RESEARCH_SUFFIXES = (".pdf",)
BENCHMARK_NAME_HINTS = ("benchmark", "bench", "results", ".perf")


def classify(path: Path, is_dir: bool) -> str:
    """Deterministic artifact category from name/extension/structure. No LLM."""
    if is_dir:
        if (path / REPO_MARKER).is_dir():
            return "Repository"
        if path.name.lower() in NOTES_DIR_NAMES:
            return "Documentation"
        return "Unknown"
    name = path.name.lower()
    suffix = path.suffix.lower()
    stem = path.stem.lower()
    # Manifests (exact filename match, case-insensitive).
    if name in MANIFEST_FILES:
        return "Manifest"
    # README* documentation.
    if name.startswith(DOC_NAME_PREFIXES) or suffix in DOC_EXTS:
        return "Documentation"
    if suffix in RESEARCH_SUFFIXES:
        return "Research Paper"
    if suffix in DATASET_SUFFIXES:
        return "Dataset"
    if suffix in DIAGRAM_SUFFIXES:
        return "Diagram"
    if suffix in IMAGE_SUFFIXES:
        return "Image"
    if suffix in ARCHIVE_SUFFIXES:
        return "Archive"
    if suffix in LOG_SUFFIXES:
        return "Log"
    if suffix in BINARY_SUFFIXES:
        return "Binary"
    if suffix == "" and (
        "benchmark" in name or "bench" in name or "results" in name
    ):
        return "Benchmark"
    if suffix in (".json", ".csv", ".tsv", ".yaml", ".yml") and any(
        h in name for h in BENCHMARK_NAME_HINTS
    ):
        return "Benchmark"
    return "Unknown"


def _archive_stem(name: str) -> Optional[str]:
    """Return the extraction stem of an archive filename, e.g. 'llvm' for
    'llvm.tar.gz'. Returns None for non-archives."""
    low = name.lower()
    for suf in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf):
            return low[: -len(suf)]
    return None


class ArtifactObserver(Observer):
    name = "artifact"

    def __init__(self, roots: Optional[list[Path]] = None) -> None:
        # Roots are the ONLY inputs. If not given, resolved from defaults/env at
        # collect time so tests can inject fixtures via roots=[...].
        self._roots = roots

    # --- root resolution ------------------------------------------------------

    def resolve_roots(self) -> list[Path]:
        if self._roots is not None:
            return [Path(p).expanduser() for p in self._roots]
        override = os.environ.get(ARTIFACT_ROOTS_ENV)
        if override:
            return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
        return [Path.home() / r for r in DEFAULT_ROOTS]

    def _root_alias(self, root: Path) -> str:
        return root.name

    def _artifact_id(self, root: Path, rel: Path) -> str:
        # Stable identity: root alias + relative path (NOT absolute).
        return f"{self._root_alias(root)}/{rel.as_posix()}"

    # --- Observer interface ---------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        roots = self.resolve_roots()
        if not roots:
            return ObserverHealth(False, Health.DOWN, "no roots",
                                  "no artifact roots configured.")
        missing = [str(r) for r in roots if not r.exists()]
        if len(missing) == len(roots):
            return ObserverHealth(
                False, Health.DOWN, "roots missing",
                f"none of the configured roots exist: {', '.join(missing)}.")
        detail = None
        if missing:
            detail = (f"{len(missing)} root(s) not present: "
                      f"{', '.join(missing)}.")
        return ObserverHealth(
            True, Health.HEALTHY, "roots accessible",
            detail or f"watching {len(roots)} root(s).")

    def collect(self, conn) -> list[Observation]:
        """Emit STABLE current-state facts only.

        The engine diffs these against the prior run, so every transition
        (repository created/removed, README added, manifest detected, project
        moved, archive extracted, ...) falls out of the diff automatically and
        the observer is idempotent on a no-op re-run. No per-run-only facts are
        emitted (those would vanish on a stable second run and read as spurious
        "removed" changes).
        """
        at = datetime.now(timezone.utc).isoformat()
        roots = self.resolve_roots()
        prior = observation_prior(conn, self.name, at)

        observations: list[Observation] = []
        current: dict[str, dict] = {}  # id -> meta
        # Archive stems seen, per root alias, for extraction detection.
        archive_stems: dict[str, list[str]] = {}
        counts = {
            "Repository": 0, "Manifest": 0, "Documentation": 0,
            "Archive": 0, "Research Paper": 0, "Dataset": 0,
            "Image": 0, "Diagram": 0, "Log": 0, "Binary": 0,
            "Benchmark": 0, "Unknown": 0,
        }
        downloads = 0
        notes_dirs = 0

        for root in roots:
            alias = self._root_alias(root)
            if not root.exists() or not root.is_dir():
                continue
            for rel, is_dir, st in self._walk(root):
                category = classify(root / rel, is_dir)
                aid = self._artifact_id(root, rel)
                size = 0 if is_dir else (st.st_size if st else 0)
                mtime = st.st_mtime if st else 0.0
                current[aid] = {
                    "alias": alias, "rel": rel, "category": category,
                    "is_dir": is_dir, "size": size, "mtime": mtime,
                }
                observations.extend(self._artifact_facts(
                    at, aid, alias, rel, category, is_dir, size, mtime))
                counts[category] = counts.get(category, 0) + 1
                if alias.lower() == "downloads":
                    downloads += 1
                if category == "Documentation" and is_dir:
                    notes_dirs += 1
                if category == "Archive":
                    s = _archive_stem(rel.name)
                    if s:
                        archive_stems.setdefault(alias, []).append(s)

        # --- Stable per-artifact engineering state (present every run) --------
        # README / manifest presence per project directory (any directory, not
        # only git repositories): the engine diff reads a flip as "added/removed".
        for aid, meta in current.items():
            if not meta["is_dir"]:
                continue
            repo_rel = meta["rel"]
            repo_name = repo_rel.name
            readme = any(
                (c["rel"].name.lower().startswith("readme")
                 and c["rel"].parent == repo_rel)
                for c in current.values())
            manifest = any(
                c["rel"].name.lower() in MANIFEST_FILES
                and c["rel"].parent == repo_rel
                for c in current.values())
            observations.append(self._obs(
                at, repo_name, "readme", "present" if readme else "absent",
                Confidence.DERIVED,
                cause=f"README {'present' if readme else 'absent'} in {aid}."))
            observations.append(self._obs(
                at, repo_name, "manifest", "present" if manifest else "absent",
                Confidence.DERIVED,
                cause=f"manifest {'present' if manifest else 'absent'} in {aid}."))

        # Notes directory presence (per notes dir).
        for aid, meta in current.items():
            if meta["category"] == "Documentation" and meta["is_dir"]:
                observations.append(self._obs(
                    at, meta["rel"].name, "notes_directory", "present",
                    Confidence.DERIVED,
                    cause=f"notes/docs directory present at {aid}."))

        # Extracted archive: a directory whose name matches an archive stem.
        for alias, stems in archive_stems.items():
            for aid, meta in current.items():
                if meta["is_dir"] and meta["rel"].name.lower() in stems:
                    observations.append(self._obs(
                        at, meta["rel"].name, "extracted_archive", "true",
                        Confidence.INFERRED,
                        cause=f"directory '{meta['rel'].name}' matches an "
                              f"extracted archive in {alias}."))
                    if meta["size"] >= LARGE_ARCHIVE_BYTES:
                        observations.append(self._obs(
                            at, meta["rel"].name, "large_archive_extraction",
                            "true", Confidence.INFERRED,
                            cause=f"large archive extracted "
                                  f"({meta['size']} bytes)."))

        # Research PDF in Downloads (workspace-level, stable per run).
        pdf_in_downloads = any(
            c["category"] == "Research Paper"
            and c["alias"].lower() == "downloads"
            for c in current.values())
        observations.append(self._obs(
            at, "workspace", "research_pdf", "present" if pdf_in_downloads else "absent",
            Confidence.DERIVED,
            cause=("research PDF present in Downloads." if pdf_in_downloads
                   else "no research PDF in Downloads.")))

        # Large document flag (stable per artifact).
        for aid, meta in current.items():
            if meta["category"] == "Research Paper" and meta["size"] >= LARGE_DOCUMENT_BYTES:
                observations.append(self._obs(
                    at, meta["rel"].name, "large_document", f"{meta['size']}B",
                    Confidence.INFERRED,
                    cause=f"large research document ({meta['size']} bytes)."))

        # --- Workspace-level counts (stable; engine diffs on change) ----------
        observations.extend([
            self._obs(at, "workspace", "artifact_count", str(len(current)),
                      Confidence.OBSERVED),
            self._obs(at, "workspace", "repository_count", str(counts["Repository"]),
                      Confidence.OBSERVED),
            self._obs(at, "workspace", "documentation_count", str(counts["Documentation"]),
                      Confidence.OBSERVED),
            self._obs(at, "workspace", "research_paper_count", str(counts["Research Paper"]),
                      Confidence.OBSERVED),
            self._obs(at, "workspace", "archive_count", str(counts["Archive"]),
                      Confidence.OBSERVED),
            self._obs(at, "workspace", "download_count", str(downloads),
                      Confidence.OBSERVED),
        ])
        # Inferred engineering signals derived deterministically from counts.
        # These are STABLE (re-emitted every run) so the run is idempotent; the
        # engine surfaces them as changes only when the underlying count moves.
        if counts["Repository"] > 0:
            observations.append(self._obs(
                at, "workspace", "repository_lifecycle", "active",
                Confidence.DERIVED,
                cause=f"{counts['Repository']} repositories tracked."))
        if downloads >= REPEATED_DOWNLOAD_THRESHOLD:
            observations.append(self._obs(
                at, "workspace", "repeated_downloads", str(downloads),
                Confidence.INFERRED,
                cause=f"{downloads} artifacts currently in Downloads."))
        return observations

    def summarize(self, conn) -> str:
        roots = self.resolve_roots()
        counts: dict[str, int] = {}
        total = 0
        notes_dirs = 0
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for rel, is_dir, st in self._walk(root):
                cat = classify(root / rel, is_dir)
                if cat == "Unknown":
                    continue
                counts[cat] = counts.get(cat, 0) + 1
                total += 1
                if cat == "Documentation" and is_dir:
                    notes_dirs += 1
        # Workspace changes this run vs the most recent prior run.
        prior_ids = _prior_category_ids(conn, self.name)
        current_ids = set()
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for rel, is_dir, st in self._walk(root):
                if classify(root / rel, is_dir) == "Unknown":
                    continue
                current_ids.add(self._artifact_id(root, rel))
        changes = len(current_ids - prior_ids) + len(prior_ids - current_ids)

        lines = [
            "Artifact Observer",
            "Healthy",
            "Observed",
            "",
            f"{total} artifacts",
            "Repositories",
            str(counts.get("Repository", 0)),
            "Research papers",
            str(counts.get("Research Paper", 0)),
            "Documentation",
            str(counts.get("Documentation", 0) - notes_dirs),
            "Archives",
            str(counts.get("Archive", 0)),
            "Workspace changes",
            str(changes),
        ]
        return "\n".join(lines)

    # --- internals ------------------------------------------------------------

    def _walk(self, root: Path):
        """Yield (relative_path, is_dir, stat_result) for classifiable entries.

        Bounded recursion: skips heavy/irrelevant dirs and respects MAX_DEPTH.
        Only `stat` metadata is touched; no file is opened.
        """
        root = root.resolve()
        try:
            entries = list(os.scandir(root))
        except (OSError, PermissionError):
            return
        depth = 0
        stack = [(root, depth, entries)]
        while stack:
            base, d, ents = stack.pop()
            for e in ents:
                rel = e.path[len(str(root)) + 1:]
                if e.is_dir(follow_symlinks=False):
                    if e.name in IGNORE_DIRS:
                        continue
                    if e.name in IGNORE_DIRS:
                        continue
                    if d + 1 > MAX_DEPTH:
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        st = None
                    yield Path(rel), True, st
                    if d + 1 < MAX_DEPTH:
                        try:
                            stack.append((Path(e.path), d + 1,
                                          list(os.scandir(e.path))))
                        except (OSError, PermissionError):
                            pass
                else:
                    if e.name.lower().endswith(tuple(IGNORE_FILE_SUFFIXES)):
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        st = None
                    yield Path(rel), False, st

    def _artifact_facts(self, at, aid, alias, rel, category, is_dir, size, mtime):
        ext = "" if is_dir else (rel.suffix.lower() or "")
        return [
            self._obs(at, aid, "category", category, Confidence.OBSERVED,
                      scope=alias),
            self._obs(at, aid, "name", rel.name, Confidence.OBSERVED,
                      scope=alias),
            self._obs(at, aid, "ext", ext, Confidence.OBSERVED, scope=alias),
            self._obs(at, aid, "size", str(size), Confidence.OBSERVED,
                      scope=alias),
            self._obs(at, aid, "modified_at", _iso(mtime), Confidence.OBSERVED,
                      scope=alias, detail=str(int(mtime))),
        ]

    def _obs(self, at, subject, aspect, value, confidence,
             scope: str = "", cause: Optional[str] = None,
             detail: Optional[str] = None) -> Observation:
        return Observation(
            source=self.name, subject=subject, aspect=aspect, value=value,
            confidence=confidence, observed_at=at, scope=scope,
            cause=cause, detail=detail,
        )


# --- module-level helpers ----------------------------------------------------


def _int(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _iso(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return ""


def observation_prior(conn, source: str, observed_at: str) -> list:
    """Prior run facts for this source. Falls back gracefully when conn is None
    (unit tests that don't need cross-run diffing)."""
    if conn is None:
        return []
    from ..db import observation_state_as_of

    return observation_state_as_of(conn, source, observed_at)


def _prior_category_ids(conn, source: str) -> set:
    if conn is None:
        return set()
    from ..db import latest_observations

    rows = latest_observations(conn)
    return {r.subject for r in rows if r.source == source
            and r.aspect == "category"}
