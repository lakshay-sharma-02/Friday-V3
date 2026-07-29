"""Change Impact Analysis — what breaks if I modify this file?

Analyzes the blast radius of a file change across repositories, knowledge,
relationships, correlations, and git history.

Usage::

    from friday.impact import analyze_impact

    report = analyze_impact(conn, "src/myfile.py")
    print(report.to_text())

The analysis collects evidence from:
  - Git log/blame for the file
  - Repository relationship graph (from the relationships table)
  - Cross-project correlations (from correlation_results)
  - Knowledge store (knowledge about the affected repo)
  - Technology / language overlap with other repos
  - Architecture and component data
  - Recent commit activity
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GitBlameEntry:
    """One line of git blame output."""

    author: str
    date: str
    summary: str


@dataclass
class RelatedRepo:
    """A repository related to the affected repo."""

    name: str
    reason: str
    strength: str  # Strong / Medium / Weak
    detail: str = ""


@dataclass
class KnowledgeEntry:
    """A knowledge entry referencing the affected repo."""

    type: str
    statement: str
    confidence: str
    status: str


@dataclass
class ImpactReport:
    """Complete impact analysis report for a single file.

    Every section is optional — if a source has no data, the field is empty.
    """

    # ── Context ──────────────────────────────────────────────────────
    file_path: str
    resolved_repo: Optional[str] = None
    repo_root: Optional[str] = None
    relative_path: Optional[str] = None

    # ── Git history ──────────────────────────────────────────────────
    last_modified: Optional[str] = None
    last_author: Optional[str] = None
    commit_count: int = 0
    recent_commits: list[dict] = field(default_factory=list)
    blame_authors: list[dict] = field(default_factory=list)
    total_authors: int = 0

    # ── Repository health ────────────────────────────────────────────
    repo_commit_count: Optional[int] = None
    repo_is_dirty: bool = False
    repo_last_commit: Optional[str] = None
    repo_primary_author: Optional[str] = None
    repo_languages: dict[str, int] = field(default_factory=dict)

    # ── Related repositories ─────────────────────────────────────────
    related_repos: list[RelatedRepo] = field(default_factory=list)

    # ── Cross-project correlations ───────────────────────────────────
    correlations: list[str] = field(default_factory=list)

    # ── Knowledge ────────────────────────────────────────────────────
    knowledge: list[KnowledgeEntry] = field(default_factory=list)

    # ── Touch-pattern repos ──────────────────────────────────────────
    # Repositories that were committed in the same sessions as this file's repo
    # (proxied via observation frequency).
    co_occurring_repos: list[tuple[str, str]] = field(default_factory=list)

    # ── Architecture ─────────────────────────────────────────────────
    architecture: Optional[str] = None
    known_patterns: Optional[str] = None
    components: list[str] = field(default_factory=list)

    # ── Errors ───────────────────────────────────────────────────────
    errors: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        """Render the report as human-readable text."""
        sections: list[str] = []

        # Header
        sections.append("Impact Analysis")
        sections.append("=" * 60)
        sections.append(f"File:  {self.file_path}")
        if self.resolved_repo:
            sections.append(f"Repo:  {self.resolved_repo}")
        if self.relative_path:
            sections.append(f"Path:  {self.relative_path}")
        sections.append("")

        # Errors
        if self.errors:
            sections.append("⚠ Issues")
            sections.append("-" * 40)
            for err in self.errors:
                sections.append(f"  {err}")
            sections.append("")

        # Git history
        sections.append("Git History")
        sections.append("-" * 40)
        if self.last_modified:
            sections.append(f"  Last modified: {self.last_modified}")
        if self.last_author:
            sections.append(f"  Last author:   {self.last_author}")
        sections.append(f"  Total commits:  {self.commit_count}")
        if self.total_authors:
            sections.append(f"  Total authors:  {self.total_authors}")
        if self.blame_authors:
            sections.append("  Authors:")
            for a in self.blame_authors[:8]:
                pct = a.get("pct", 0)
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                sections.append(f"    {a['author']:24s} {pct:3.0f}% {bar}")
        if self.recent_commits:
            sections.append("  Recent commits:")
            for c in self.recent_commits[:6]:
                sections.append(f"    {c['date'][:10]} {c['author']:20s} {c['summary'][:60]}")
        sections.append("")

        # Repository Health
        sections.append("Repository Health")
        sections.append("-" * 40)
        if self.repo_commit_count is not None:
            sections.append(f"  Total commits (repo): {self.repo_commit_count}")
        sections.append(f"  Dirty:                {'yes' if self.repo_is_dirty else 'no'}")
        if self.repo_last_commit:
            sections.append(f"  Last commit:          {self.repo_last_commit[:19]}")
        if self.repo_primary_author:
            sections.append(f"  Primary author:       {self.repo_primary_author}")
        if self.repo_languages:
            langs = sorted(self.repo_languages.items(), key=lambda x: -x[1])[:6]
            lang_str = ", ".join(f"{l}({c})" for l, c in langs)
            sections.append(f"  Languages:            {lang_str}")
        sections.append("")

        # Related Repositories
        if self.related_repos:
            sections.append("Related Repositories")
            sections.append("-" * 40)
            for r in self.related_repos:
                strength_mark = {"Strong": "●", "Medium": "◉", "Weak": "○"}.get(
                    r.strength, "○"
                )
                sections.append(f"  {strength_mark} {r.name}")
                sections.append(f"     {r.reason}")
                if r.detail:
                    sections.append(f"     {r.detail}")
            sections.append("")

        # Cross-project Correlations
        if self.correlations:
            sections.append("Cross-Project Correlations")
            sections.append("-" * 40)
            for c in self.correlations:
                sections.append(f"  {c}")
            sections.append("")

        # Knowledge
        if self.knowledge:
            sections.append("Knowledge")
            sections.append("-" * 40)
            for k in self.knowledge:
                sections.append(f"  [{k.type}] {k.statement}")
                sections.append(f"    Confidence: {k.confidence}, Status: {k.status}")
            sections.append("")

        # Architecture
        if self.architecture:
            sections.append("Architecture")
            sections.append("-" * 40)
            sections.append(f"  {self.architecture[:200]}")
            if self.known_patterns:
                sections.append(f"  Patterns: {self.known_patterns[:200]}")
            if self.components:
                for c in self.components:
                    sections.append(f"  Component: {c}")
            sections.append("")

        # Co-occurring repositories
        if self.co_occurring_repos:
            sections.append("Co-occurring Repositories")
            sections.append("-" * 40)
            sections.append("  Repos committed alongside this one (recent activity):")
            for name, detail in self.co_occurring_repos[:8]:
                sections.append(f"    {name:<25s} {detail}")
            sections.append("")

        # Footer
        sections.append("=" * 60)

        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(repo_path: str, args: list[str], timeout: int = 15) -> Optional[str]:
    """Run a git command in the given repo. Returns stdout or None on failure."""
    try:
        res = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if res.returncode != 0:
            return None
        return res.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None


def _file_commit_count(repo_path: str, rel_path: str) -> int:
    """Count the number of commits touching a specific file."""
    out = _run_git(repo_path, ["rev-list", "--count", "HEAD", "--", rel_path])
    if out is None:
        return 0
    try:
        return int(out)
    except ValueError:
        return 0


def _recent_commits(repo_path: str, rel_path: str, n: int = 8) -> list[dict]:
    """Get the last N commits touching a file."""
    out = _run_git(
        repo_path,
        ["log", f"-{n}", "--format=%H|%cI|%an|%s", "--", rel_path],
    )
    if not out:
        return []
    commits: list[dict] = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) >= 4:
            commits.append({
                "sha": parts[0][:8],
                "date": parts[1],
                "author": parts[2],
                "summary": parts[3],
            })
    return commits


def _blame_authors(repo_path: str, rel_path: str) -> list[dict]:
    """Get author lines distribution from git blame."""
    out = _run_git(
        repo_path,
        ["blame", "--line-porcelain", rel_path],
        timeout=30,
    )
    if not out:
        return []
    authors: dict[str, int] = {}
    for line in out.splitlines():
        if line.startswith("author "):
            author = line[7:]
            authors[author] = authors.get(author, 0) + 1
    total = sum(authors.values())
    if total == 0:
        return []
    return [
        {"author": a, "count": c, "pct": round(c / total * 100, 1)}
        for a, c in sorted(authors.items(), key=lambda x: -x[1])
    ]


def _last_file_info(repo_path: str, rel_path: str) -> tuple[Optional[str], Optional[str]]:
    """Get (date, author) of the last commit touching the file."""
    out = _run_git(
        repo_path,
        ["log", "-1", "--format=%cI|%an", "--", rel_path],
    )
    if not out:
        return None, None
    parts = out.split("|", 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def _resolve_repos_by_path(file_path: str, repos: list) -> list[dict]:
    """Find all repositories whose path contains the given file.

    This handles the case where a file like 'src/utils.py' lives inside
    a repo with path '/home/user/project'.
    """
    abs_path = str(Path(file_path).resolve())
    matched: list[dict] = []
    for repo in repos:
        repo_root = str(Path(repo.path).resolve()) if hasattr(repo, "path") else str(repo.get("path", ""))
        # Check if the file is inside the repo (path prefix match).
        prefix = repo_root + "/"
        if abs_path.startswith(prefix) or abs_path == repo_root:
            matched.append(repo)
    return matched


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze_impact(
    conn,
    file_path: str,
    max_commits: int = 10,
) -> ImpactReport:
    """Run change impact analysis on a file.

    Args:
        conn: Database connection (use ``friday.db.connect()``).
        file_path: Path to the file to analyze.
        max_commits: Max recent commits to fetch (default 10).

    Returns:
        An ``ImpactReport`` with all available evidence sections populated.

    The report is never None — even when no data is found, empty sections
    are returned so callers can always call ``report.to_text()``.
    """
    report = ImpactReport(file_path=file_path)

    # ── Resolve file to repo ──────────────────────────────────────────
    try:
        from .db import get_repositories
        repos = get_repositories(conn)
    except Exception as exc:
        report.errors.append(f"Could not load repositories: {exc}")
        repos = []

    matched_repos = _resolve_repos_by_path(file_path, repos)

    if not matched_repos:
        report.errors.append(f"Could not resolve file '{file_path}' to any known repository.")
        return report

    # Use the first matched repo for analysis.
    repo = matched_repos[0]
    repo_name = repo.name if hasattr(repo, "name") else repo.get("name", "?")
    repo_path = repo.path if hasattr(repo, "path") else repo.get("path", "")
    repo_id = repo.id if hasattr(repo, "id") else repo.get("id")

    report.resolved_repo = repo_name
    report.repo_root = repo_path

    # Compute relative path within the repo.
    try:
        abs_file = str(Path(file_path).resolve())
        abs_repo = str(Path(repo_path).resolve())
        rel_path = os.path.relpath(abs_file, abs_repo)
        report.relative_path = rel_path
    except (ValueError, OSError):
        rel_path = file_path
        report.relative_path = rel_path

    # ── Git history for the file ──────────────────────────────────────
    if repo_path and os.path.isdir(repo_path):
        report.commit_count = _file_commit_count(repo_path, rel_path)
        report.last_modified, report.last_author = _last_file_info(repo_path, rel_path)
        report.recent_commits = _recent_commits(repo_path, rel_path, max_commits)
        report.blame_authors = _blame_authors(repo_path, rel_path)
        report.total_authors = len(report.blame_authors)

        # Repository-level git metadata.
        try:
            from .gitmeta import collect as collect_gitmeta
            from .discovery import Repo

            dummy_repo = Repo(path=Path(repo_path))
            meta = collect_gitmeta(dummy_repo)
            report.repo_commit_count = meta.commit_count
            report.repo_is_dirty = meta.is_dirty
            report.repo_last_commit = meta.last_commit_date
            report.repo_primary_author = meta.primary_author
            report.repo_languages = meta.languages
        except Exception as exc:
            report.errors.append(f"Could not collect git metadata: {exc}")

    # ── Related repositories (from relationships table) ───────────────
    if repo_id is not None:
        try:
            rows = conn.execute(
                """SELECT r2.name AS other_name, r2.path AS other_path,
                          rel.kind, rel.strength, rel.evidence
                   FROM relationships rel
                   JOIN repositories r1 ON rel.repo_a = r1.id
                   JOIN repositories r2 ON rel.repo_b = r2.id
                   WHERE rel.repo_a = ? OR rel.repo_b = ?
                   ORDER BY rel.priority DESC
                   LIMIT 20""",
                (repo_id, repo_id),
            ).fetchall()
            for row in rows:
                strength = row["strength"] or "Medium"
                report.related_repos.append(RelatedRepo(
                    name=row["other_name"],
                    reason=f"Relationship: {row['kind']}",
                    strength=strength,
                    detail=row["evidence"][:120] if row["evidence"] else "",
                ))
        except Exception as exc:
            report.errors.append(f"Error loading relationships: {exc}")

    # ── Cross-project correlations ───────────────────────────────────
    if repo_id is not None:
        try:
            corr_rows = conn.execute(
                """SELECT * FROM correlation_results
                   WHERE repo_a_id = ? OR repo_b_id = ?
                   ORDER BY structural_score DESC
                   LIMIT 10""",
                (repo_id, repo_id),
            ).fetchall()
            for row in corr_rows:
                a_name = row.get("repo_a_name", "?")
                b_name = row.get("repo_b_name", "?")
                other = b_name if a_name == repo_name else a_name
                score = row.get("semantic_score") or row.get("structural_score", 0)
                reason = row.get("semantic_reason", "") or "Structural similarity"
                report.correlations.append(
                    f"{other} (score={score:.2f}) — {reason}"
                )
        except Exception:
            pass

    # ── Knowledge about the repo ──────────────────────────────────────
    if repo_name:
        try:
            from .knowledge.store import get_knowledge_by_subject
            knowledge_rows = get_knowledge_by_subject(conn, repo_name)
            for k in knowledge_rows[:10]:
                report.knowledge.append(KnowledgeEntry(
                    type=k.type.value if hasattr(k, "type") else str(k.get("type", "?")),
                    statement=k.statement if hasattr(k, "statement") else str(k.get("statement", "")),
                    confidence=k.confidence.value if hasattr(k, "confidence") else str(k.get("confidence", "?")),
                    status=k.status.value if hasattr(k, "status") else str(k.get("status", "?")),
                ))
        except Exception:
            pass

    # ── Architecture / Components ────────────────────────────────────
    if repo_id is not None:
        try:
            arch_row = conn.execute(
                "SELECT architecture, known_patterns, complexity FROM architecture WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            if arch_row:
                report.architecture = arch_row["architecture"]
                report.known_patterns = arch_row.get("known_patterns")
        except Exception:
            pass

        try:
            comp_rows = conn.execute(
                "SELECT name, evidence FROM components WHERE repo_id = ?",
                (repo_id,),
            ).fetchall()
            for r in comp_rows:
                report.components.append(f"{r['name']} — {r['evidence'][:60]}")
        except Exception:
            pass

    # ── Co-occurring repos (from sessions / activity) ─────────────────
    try:
        rows = conn.execute(
            """SELECT DISTINCT other.name AS other_name, other.id AS other_id
               FROM sessions s
               JOIN repositories other ON (
                   ',' || s.repositories || ',' LIKE '%,' || other.name || ',%'
                   OR s.primary_repo = other.name
               )
               WHERE (
                   ',' || s.repositories || ',' LIKE '%,' || ? || ',%'
                   OR s.primary_repo = ?
               )
               AND other.id != ?
               LIMIT 15""",
            (repo_name, repo_name, repo_id),
        ).fetchall()
        for row in rows:
            oname = row["other_name"]
            oid = row["other_id"]
            if not oname:
                continue
            # Check technology overlap.
            shared_langs = ""
            try:
                langs = conn.execute(
                    """SELECT l1.language FROM languages l1
                       JOIN languages l2 ON l1.language = l2.language
                       WHERE l1.repo_id = ? AND l2.repo_id = ?
                       LIMIT 3""",
                    (oid, repo_id),
                ).fetchall()
                if langs:
                    shared_langs = f"Shared language(s): {', '.join(r[0] for r in langs)}"
            except Exception:
                pass
            report.co_occurring_repos.append((oname, shared_langs or "Related by session activity"))
    except Exception:
        pass

    return report


# ── Static import graph analysis (Python ast-based) ───────────────


def _parse_python_imports(file_path: str) -> list[dict]:
    """Parse a Python file and extract all imports using the ``ast`` module.

    Returns a list of dicts:
        {"module": "os", "name": "", "type": "import", "line": 1}
        {"module": "datetime", "name": "datetime", "type": "from", "line": 2}

    Handles ``import X``, ``from X import Y``, aliased imports, and relative imports.
    """
    imports: list[dict] = []
    try:
        import ast
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=file_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({
                        "module": alias.name,
                        "name": alias.asname or "",
                        "type": "import",
                        "line": node.lineno,
                    })
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = "." * node.level + (module or "")
                for alias in node.names:
                    imports.append({
                        "module": module,
                        "name": alias.name,
                        "asname": alias.asname or "",
                        "type": "from",
                        "line": node.lineno,
                    })
        return imports
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []


def _register_imports(conn, repo_id: int, repo_path: str, built_at: str) -> int:
    """Scan all tracked files in a repo and store imports in code_imports.

    Uses ``ast`` for Python files, falls back to regex for JS/TS/Rust/Go/Java.
    Returns the number of import relationships found.
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "ls-files"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return 0
        files = [f.strip() for f in out.stdout.splitlines() if f.strip()]
    except Exception:
        return 0

    total = 0
    for rel_path in files:
        full_path = str(Path(repo_path) / rel_path)
        if not Path(full_path).is_file():
            continue
        ext = Path(rel_path).suffix.lower()
        if ext == ".py":
            imports = _parse_python_imports(full_path)
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            imports = _grep_imports(full_path)
        elif ext in (".rs",):
            imports = _grep_imports(full_path)
        elif ext in (".go",):
            imports = _grep_imports(full_path)
        else:
            continue
        for imp in imports:
            conn.execute(
                "INSERT INTO code_imports "
                "(repo_id, source_file, imported_module, import_type, line_number, built_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo_id, rel_path, imp["module"], imp["type"], imp["line"], built_at),
            )
            total += 1
    conn.commit()
    return total


def _grep_imports(file_path: str) -> list[dict]:
    """Extract imports from non-Python files using regex patterns."""
    imports: list[dict] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return imports
    patterns = [
        (r'import\s+["\']([^"\']+)["\']', "go_import"),
        (r'import\s+(\w+(?:\.\w+)*)\s+from\s+', "ts_import"),
        (r'from\s+["\']([^"\']+)["\']\s+import\s+', "ts_import"),
        (r'const\s+\w+\s*=\s*require\s*\(["\']([^"\']+)["\']', "require"),
        (r'use\s+(\w+(?:::\w+)*)\s*;', "rust_use"),
        (r'^\s*import\s+(?:static\s+)?(\w+(?:\.\w+)*)\s*;', "java_import"),
    ]
    for i, line in enumerate(content.splitlines(), 1):
        for pattern, lang in patterns:
            for m in re.finditer(pattern, line):
                imports.append({"module": m.group(1), "name": "", "type": lang, "line": i})
    return imports


def build_import_graph(conn, repo_name_or_path: str) -> int:
    """Build (or rebuild) the static import graph for a repository.

    Clears existing data, scans all tracked files, and stores imports in the
    ``code_imports`` table. Returns the number of import relationships found.

    Args:
        conn: Database connection.
        repo_name_or_path: Repository name or path to scan.
    """
    from .db import get_repositories, now_iso
    built_at = now_iso()
    repos = get_repositories(conn)
    target = None
    for r in repos:
        rn = r.name if hasattr(r, "name") else r.get("name", "")
        rp = r.path if hasattr(r, "path") else r.get("path", "")
        if rn == repo_name_or_path or rp == repo_name_or_path:
            target = r
            break
    if target is None:
        return 0
    rid = target.id if hasattr(target, "id") else target.get("id")
    rp = target.path if hasattr(target, "path") else target.get("path", "")
    if not rid or not rp:
        return 0
    conn.execute("DELETE FROM code_imports WHERE repo_id = ?", (rid,))
    conn.execute("DELETE FROM code_dependencies WHERE repo_id = ?", (rid,))
    return _register_imports(conn, rid, rp, built_at)


def trace_symbol(conn, symbol: str, repo_name: str = "") -> dict:
    """Trace all references to a symbol across the codebase.

    Queries ``code_dependencies`` and categorizes results into DIRECT,
    TRANSITIVE, TEST, and CONFIG buckets.

    Args:
        conn: Database connection.
        symbol: The symbol/function/class name to trace.
        repo_name: Optional repo to scope the search.

    Returns:
        Dict with keys: direct, transitive, test, config, total.
    """
    result: dict[str, list[dict]] = {"direct": [], "transitive": [], "test": [], "config": []}
    query = "SELECT file_path, line_number, dep_type, resolved_path, resolved_repo FROM code_dependencies WHERE symbol = ?"
    params: list = [symbol]
    if repo_name:
        query += " AND resolved_repo = ?"
        params.append(repo_name)
    query += " ORDER BY file_path, line_number"
    try:
        rows = conn.execute(query, params).fetchall()
    except Exception:
        return result
    for row in rows:
        fp = row["file_path"]
        entry = {"file_path": fp, "line_number": row["line_number"], "dep_type": row["dep_type"]}
        if fp.startswith(("test/", "tests/")) or "/test_" in fp:
            result["test"].append(entry)
        elif fp.endswith((".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".conf")):
            result["config"].append(entry)
        elif row["dep_type"] in ("from", "import", "require"):
            result["direct"].append(entry)
        else:
            result["transitive"].append(entry)
    result["total"] = sum(len(v) for v in result.values())
    return result


def format_symbol_impact(symbol: str, trace: dict) -> str:
    """Format a symbol trace into a human-readable impact report.

    Output::

        Impact of 'verify_auth':
          DIRECT (5 files)
            src/auth.py:15 — import
          TEST (2 files)
            tests/test_auth.py:10 — import
          TRANSITIVE (0 files)
          CONFIG (0 files)
    """
    lines: list[str] = [f"Impact of '{symbol}':"]
    for label, key in [("DIRECT", "direct"), ("TRANSITIVE", "transitive"),
                       ("TEST", "test"), ("CONFIG", "config")]:
        items = trace.get(key, [])
        if items:
            lines.append(f"  {label} ({len(items)} file(s))")
            for item in items[:12]:
                lines.append(f"    {item['file_path']}:{item['line_number']} \u2014 {item['dep_type']}")
            if len(items) > 12:
                lines.append(f"    ... and {len(items) - 12} more")
        else:
            lines.append(f"  {label} (0 files)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dirty-repo pattern scanning (used by SpontaneousReviewEngine)
# ---------------------------------------------------------------------------


def scan_dirty_patterns(repo_path: str) -> list[dict]:
    """Scan a repo's dirty (uncommitted) files for review-worthy patterns.

    Analyzes each modified, staged, or untracked file via ``git status``
    for common code-quality issues that warrant attention before commit:

    - **Leftover TODOs / FIXMEs / HACKs / XXXs** — unfinished work.
    - **Debug print/console.log statements** — left in production code.
    - **Merge conflict markers** (``<<<<<<<``, ``=======``, ``>>>>>>>``).
    - **Large single hunks** (>50 added lines) — risky changes.
    - **Missing tests** — new source files without corresponding test files.

    Args:
        repo_path: Path to the git repository root.

    Returns:
        A list of finding dicts, each with:
            ``label`` (str), ``severity`` ("high" | "medium" | "low"),
            ``file`` (str), ``detail`` (str), ``line`` (int|None).
    """
    findings: list[dict] = []
    path_obj = Path(repo_path)
    if not path_obj.exists():
        return findings

    try:
        # Get dirty files (staged + unstaged + untracked).
        status = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if not status.stdout.strip():
            return findings

        dirty_files: list[str] = []
        for line in status.stdout.splitlines():
            # Status porcelain format: XY filename
            # X = staging area, Y = working tree, ?? = untracked
            if len(line) < 3:
                continue
            x, y = line[0], line[1]
            if x in ("?", "M", "A", "D", "R", "C") or y in ("M", "A", "D", "R", "C"):
                fname = line[3:].strip()
                # Handle renamed files ("R  old -> new")
                if "->" in fname:
                    fname = fname.split("->")[-1].strip()
                if fname:
                    dirty_files.append(fname)
    except Exception:
        return findings

    # Dedup (a file can appear in both staged and unstaged).
    dirty_files = list(dict.fromkeys(dirty_files))

    # Diff stats for large hunk detection.
    diff_stats: dict[str, int] = {}
    try:
        diff = subprocess.run(
            ["git", "-C", repo_path, "diff", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        for dline in diff.stdout.splitlines():
            if "|" in dline:
                parts = dline.rsplit("|", 1)
                fname = parts[0].strip()
                try:
                    count = int(parts[1].strip().split()[0])
                    diff_stats[fname] = count
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass

    # Staged diff too.
    try:
        staged = subprocess.run(
            ["git", "-C", repo_path, "diff", "--cached", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        for dline in staged.stdout.splitlines():
            if "|" in dline:
                parts = dline.rsplit("|", 1)
                fname = parts[0].strip()
                try:
                    count = int(parts[1].strip().split()[0])
                    diff_stats[fname] = max(diff_stats.get(fname, 0), count)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass

    PATTERNS = [
        # (pattern, label, severity, is_merge_marker)
        (r"<<<<<<<", "Merge conflict marker", "high", True),
        (r"=======", "Merge conflict marker", "high", True),
        (r">>>>>>>", "Merge conflict marker", "high", True),
        (r"\bTODO\b", "Unresolved TODO", "medium", False),
        (r"\bFIXME\b", "Unresolved FIXME", "medium", False),
        (r"\bHACK\b", "Unresolved HACK", "medium", False),
        (r"\bXXX\b", "Unresolved XXX", "medium", False),
        (r"print\(|console\.log\(|console\.warn\(", "Debug print statement", "medium", False),
        (r"import pdb;|ipdb\.set_trace|breakpoint\(\)", "Debugger left in code", "high", False),
    ]

    for filename in dirty_files:
        full_path = path_obj / filename
        if not full_path.exists() or not full_path.is_file():
            continue

        # Skip binary files.
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        lines = content.splitlines()

        # Pattern scan — group merge conflict markers into one finding per file.
        has_merge_conflict = False
        for pat_regex, label, severity, is_merge in PATTERNS:
            for i, line in enumerate(lines, 1):
                if re.search(pat_regex, line):
                    if is_merge:
                        has_merge_conflict = True
                    else:
                        findings.append({
                            "label": label,
                            "severity": severity,
                            "file": filename,
                            "detail": f"Line {i}: {line.strip()[:80]}",
                            "line": i,
                        })
                    break  # one finding per (file, pattern) per non-merge group

        if has_merge_conflict:
            findings.append({
                "label": "Merge conflict markers found",
                "severity": "high",
                "file": filename,
                "detail": f"File contains merge conflict markers (<<<<<<<, =======, >>>>>>>).",
                "line": None,
            })

        # Large hunk detection.
        add_count = diff_stats.get(filename, 0)
        if add_count >= 50:
            findings.append({
                "label": "Large change",
                "severity": "medium",
                "file": filename,
                "detail": f"{add_count} lines changed — consider splitting into smaller commits.",
                "line": None,
            })
        elif add_count >= 200:
            findings.append({
                "label": "Very large change",
                "severity": "high",
                "file": filename,
                "detail": f"{add_count} lines changed in one file — risks reviewability.",
                "line": None,
            })

        # Missing test detection.
        if _is_source_file(filename):
            test_file = _corresponding_test_file(filename)
            if test_file and not (path_obj / test_file).exists():
                findings.append({
                    "label": "Missing tests",
                    "severity": "medium",
                    "file": filename,
                    "detail": f"Source file changed but no corresponding test found ({test_file}).",
                    "line": None,
                })

    return findings


def _is_source_file(filename: str) -> bool:
    """Check if a filename looks like a source file (not test, config, or docs)."""
    src_exts = (".py", ".rs", ".go", ".js", ".ts", ".tsx", ".jsx", ".java",
                ".cpp", ".c", ".h", ".hpp", ".rb", ".swift", ".kt")
    test_prefixes = ("test_", "_test", ".spec.", ".test.", "__init__")
    name = Path(filename).name
    ext = Path(filename).suffix
    if ext not in src_exts:
        return False
    if any(name.startswith(p) or p in name for p in test_prefixes):
        return False
    return True


def _corresponding_test_file(filename: str) -> Optional[str]:
    """Get the corresponding test file path for a source file, or None."""
    p = Path(filename)
    stem = p.stem
    ext = p.suffix

    candidates = [
        f"test_{stem}{ext}",
        f"{stem}_test.py" if ext == ".py" else None,
        str(p.parent / f"test_{stem}{ext}"),
        str(p.parent / f"{stem}_test{ext}"),
    ]
    # Also check in a tests/ directory.
    if p.parent.name not in ("tests", "test"):
        candidates.append(str(p.parent / "tests" / f"test_{stem}{ext}"))
        candidates.append(str(p.parent.parent / "tests" / f"test_{stem}{ext}"))

    for c in candidates:
        if c:
            return c
    return None  # no matching test file found


def format_impact_summary(report: ImpactReport) -> str:
    """Return a one-line summary of the impact analysis for CLI display."""
    parts: list[str] = []
    if report.resolved_repo:
        parts.append(f"Repo: {report.resolved_repo}")
    if report.commit_count:
        parts.append(f"Commits: {report.commit_count}")
    if report.total_authors:
        parts.append(f"Authors: {report.total_authors}")
    rel = len(report.related_repos)
    if rel:
        parts.append(f"Relationships: {rel}")
    corr = len(report.correlations)
    if corr:
        parts.append(f"Correlations: {corr}")
    know = len(report.knowledge)
    if know:
        parts.append(f"Knowledge: {know}")
    if not parts:
        parts.append("No impact data available")
    return " | ".join(parts)
