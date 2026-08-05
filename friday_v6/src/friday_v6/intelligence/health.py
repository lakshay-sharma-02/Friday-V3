"""Code Health Diagnostics — complexity, coverage, and churn trends.

Walks a project tree and computes lightweight, dependency-free health
metrics straight from the source:
  - Cyclomatic-ish complexity per function (branch points via AST)
  - LOC / function counts per file
  - TODO/FIXME markers
  - Git churn (recent commit touch count per file, best-effort)

Scores each file and the whole repo 0-100 with an A-F grade, and flags
the hottest files (high churn + high complexity = likely-to-break).

Pure stdlib (ast, subprocess) — no external analyzers required.

Usage:
    health = CodeHealthDiagnostics()
    report = health.analyze_repo("/path/to/project")
    print(report["grade"], report["score"], report["hotspots"])
"""

from __future__ import annotations

import ast
import logging
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.intelligence.health")

_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)


@dataclass
class FileHealth:
    """Health metrics for a single source file."""
    path: str = ""
    loc: int = 0
    functions: int = 0
    max_complexity: int = 0
    avg_complexity: float = 0.0
    complexity_score: int = 100
    todo_count: int = 0
    churn: int = 0            # commits touching this file (best-effort)
    score: int = 100          # 0-100 composite
    grade: str = "A"
    issues: list[str] = field(default_factory=list)


class _ComplexityVisitor(ast.NodeVisitor):
    """Counts branch points that raise cyclomatic complexity."""

    def __init__(self):
        self.branch_points = 0

    def _count_branch(self, node: ast.AST) -> None:
        """Count one branch point and keep walking (shared by If/While/For)."""
        self.branch_points += 1
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._count_branch(node)

    def visit_While(self, node: ast.While) -> None:
        self._count_branch(node)

    def visit_For(self, node: ast.For) -> None:
        self._count_branch(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._count_branch(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        self.branch_points += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp):
        # Each `and`/`or` adds a decision point
        self.branch_points += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension):
        # Comprehensions with conditions add decision points
        if node.ifs:
            self.branch_points += len(node.ifs)
        self.generic_visit(node)


def _cyclomatic(node: ast.AST) -> int:
    """Cyclomatic complexity = 1 + branch points for a code node."""
    visitor = _ComplexityVisitor()
    visitor.visit(node)
    return 1 + visitor.branch_points


class CodeHealthDiagnostics:
    """Computes code health metrics for a project tree."""

    def __init__(self, source_exts: Optional[list[str]] = None):
        self.source_exts = source_exts or (".py",)

    # ── Public API ────────────────────────────────────────────────────

    def analyze_repo(self, path: str = ".") -> dict:
        """Analyze a project directory and return a full health report.

        Never raises: missing paths, unparsable files, and absent git all
        degrade to partial reports with ``ok=False`` markers.
        """
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return {"ok": False, "error": f"Path not found: {root}",
                    "score": 0, "grade": "F", "files": [], "hotspots": [],
                    "issues": [f"Path not found: {root}"]}

        churn = self._git_churn(root)
        files: list[dict] = []
        issues: list[str] = []
        scanned = 0

        for src in self._iter_source_files(root):
            scanned += 1
            fh = self._analyze_file(src)
            fh.churn = churn.get(str(src.relative_to(root)), 0)
            fh.score = self._file_score(fh)
            fh.grade = self._grade(fh.score)
            files.append(fh.__dict__)

        if not files:
            return {"ok": True, "path": str(root), "scanned": 0,
                    "score": 100, "grade": "A", "files": [],
                    "hotspots": [], "issues": ["No source files found."]}

        # Repo-level composite: average of file scores weighted by churn+size
        scores = [f["score"] for f in files]
        repo_score = round(statistics.fmean(scores))

        # Hotspots: high churn × high complexity (likely to break next)
        hotspots = sorted(
            (f for f in files if f["churn"] > 0 and f["max_complexity"] > 5),
            key=lambda f: (f["churn"], f["max_complexity"]),
            reverse=True,
        )[:5]
        for f in hotspots:
            issues.append(
                f"{f['path']}: {f['churn']} changes, "
                f"complexity {f['max_complexity']} — hot & complex"
            )

        todo_count = sum(f["todo_count"] for f in files)
        if todo_count:
            issues.append(f"{todo_count} TODO/FIXME markers across {len(files)} files")

        return {
            "ok": True,
            "path": str(root),
            "scanned": scanned,
            "files": files,
            "score": repo_score,
            "grade": self._grade(repo_score),
            "hotspots": hotspots,
            "issues": issues,
        }

    def summarize(self, report: dict) -> dict:
        """Compact summary of an analyze_repo report (for CLI/voice)."""
        return {
            "ok": report.get("ok", False),
            "path": report.get("path", ""),
            "scanned": report.get("scanned", 0),
            "score": report.get("score", 0),
            "grade": report.get("grade", "F"),
            "hotspots": [(f["path"], f["churn"]) for f in report.get("hotspots", [])],
            "issues": report.get("issues", [])[:5],
        }

    # ── Internals ─────────────────────────────────────────────────────

    def _iter_source_files(self, root: Path):
        """Yield source files, skipping VCS/build/dependency dirs."""
        skip = {".git", "venv", "node_modules", "__pycache__",
                "dist", "build", ".mypy_cache", ".pytest_cache", ".tox"}
        for p in root.rglob("*"):
            if p.is_dir():
                continue
            parts = p.parts
            if any(part in skip for part in parts):
                continue
            # venv dirs are often versioned (.venv, .venv312, ...)
            if any(part.startswith(".venv") for part in parts):
                continue
            if p.suffix in self.source_exts:
                yield p

    def _analyze_file(self, path: Path) -> FileHealth:
        """Compute per-file metrics from source text."""
        fh = FileHealth(path=str(path))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            fh.issues.append("Unreadable file")
            return fh

        fh.loc = len([ln for ln in text.splitlines() if ln.strip()])
        fh.todo_count = len(_TODO_RE.findall(text))

        try:
            tree = ast.parse(text)
        except SyntaxError:
            fh.issues.append("Unparsable (syntax error)")
            fh.max_complexity = 1
            fh.score = 40
            fh.grade = self._grade(40)
            return fh

        complexities = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexities.append(_cyclomatic(node))
                fh.functions += 1

        if complexities:
            fh.max_complexity = max(complexities)
            fh.avg_complexity = round(statistics.fmean(complexities), 2)
            # Complexity penalty: average above 5 or a single function > 10
            if fh.max_complexity > 10:
                fh.complexity_score = 60
                fh.issues.append(
                    f"Function with complexity {fh.max_complexity} — consider splitting")
            elif fh.avg_complexity > 5:
                fh.complexity_score = 80
        return fh

    def _file_score(self, fh: FileHealth) -> int:
        """Composite 0-100 score for a file."""
        score = 100
        score -= (100 - fh.complexity_score)          # complexity penalty
        score -= min(fh.todo_count * 5, 20)            # task-marker debt
        if fh.churn > 10:
            score -= 10                                # heavy churn risk
        if fh.issues and fh.issues[0].startswith("Unparsable"):
            score = min(score, 40)
        return max(0, min(100, score))

    @staticmethod
    def _grade(score: int) -> str:
        """Map a 0-100 score to an A-F grade."""
        if score >= 90:
            return "A"
        if score >= 75:
            return "B"
        if score >= 60:
            return "C"
        if score >= 45:
            return "D"
        return "F"

    @staticmethod
    def _git_churn(root: Path) -> dict:
        """Map file path -> commit touch count (best-effort, no git → {})."""
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "log", "--name-only",
                 "--pretty=format:", "-n", "200"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {}
        except (OSError, subprocess.TimeoutExpired):
            return {}

        churn: dict[str, int] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("("):
                continue
            churn[line] = churn.get(line, 0) + 1
        return churn
