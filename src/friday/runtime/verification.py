"""Truthful mission verification (Phase 3 of Runtime Stabilization).

An executor exiting 0 does NOT mean the mission succeeded. This module makes
FRIDAY verify *evidence*: for any task whose text (goal / title / acceptance
criteria / payload) references a file path, the referenced artifact must
actually exist on disk after execution (or the worker must have reported an
artifact). If a task claims success but produced no expected file, it is
FAILED truthfully — the mission never reports success with no file present.

Pure, deterministic, no I/O beyond stating paths. No LLM, no network.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .models import VerificationResult
from .contract import contract_for_task, resolve_artifact_paths

# Task types whose whole purpose is to *produce* an artifact on disk. For these,
# a referenced file path is a hard expectation. Testing/verification are CHECK
# tasks (their evidence is the runner's output, not a file) so they are NOT
# here — they fall through to verify_task_artifacts, which captures the test
# summary as evidence.
_CREATION_TASK_TYPES = frozenset({
    "implementation", "documentation", "configuration",
    "cleanup", "migration", "infrastructure", "deployment",
})

# Extensions that unambiguously name a real file (vs a bare word).
_FILE_EXT = re.compile(
    r"\.(py|md|txt|sh|ts|tsx|jsx|js|rs|go|rb|java|c|h|cpp|hpp|json|"
    r"yaml|yml|sql|html|css|scss|toml|cfg|ini|lock|env|pdf|csv|xml)$",
    re.IGNORECASE,
)

# Absolute or ./relative path token: contains a slash and ends in a file ext,
# or is an absolute path. Avoids matching ordinary prose words.
_PATHISH = re.compile(r"(?:/|\./|\.\./|~)[\w./\-~]+?\.\w{1,6}\b")


def _scan_paths(text: str) -> List[str]:
    """Return candidate file-path tokens found in free text."""
    if not text:
        return []
    out: List[str] = []
    # JSON payloads: {"path": "...", "op":"write"} etc.
    if text.strip().startswith("{"):
        try:
            obj = json.loads(text)
            for key in ("path", "file", "filename", "out"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
        except (ValueError, TypeError):
            pass
    # Bare "name.ext" tokens (e.g. "hello.py", "README.md").
    for m in re.finditer(r"\b[\w\-]+\.\w{1,6}\b", text):
        tok = m.group(0)
        if _FILE_EXT.search(tok):
            out.append(tok)
    # Path-ish tokens with a slash.
    for m in _PATHISH.finditer(text):
        out.append(m.group(0).rstrip(".,;:'\"!?)"))
    # De-dup, preserve order.
    seen = set()
    uniq: List[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def expected_paths(task, workspace: str = ".") -> List[str]:
    """Resolve the file paths a task is expected to produce.

    Phase 1.5: prefer the task's EXPLICIT contract. The planner stamps concrete
    file paths into `task.outputs`; those are authoritative. Only when the
    contract is empty do we fall back to scanning goal/title/payload prose (the
    old heuristic) so legacy tasks without a stamped contract still verify.

    Returns absolute paths resolved against the workspace. Empty when the task
    references no file.
    """
    # 1) Explicit contract: structured artifact paths from `outputs`.
    contract_paths = resolve_artifact_paths(
        contract_for_task(task), workspace)
    if contract_paths:
        return contract_paths

    # 2) Fallback: derive from free text (goal/title/payload/acceptance).
    texts: List[str] = []
    for attr in ("goal", "title", "runtime_payload"):
        v = getattr(task, attr, "") or ""
        if isinstance(v, str):
            texts.append(v)
    ac = getattr(task, "acceptance_criteria", None)
    if isinstance(ac, (list, tuple)):
        texts.extend(str(x) for x in ac)
    elif isinstance(ac, str):
        texts.append(ac)

    base = Path(workspace).resolve()
    resolved: List[str] = []
    seen = set()
    for t in texts:
        for p in _scan_paths(t):
            pp = Path(p)
            if not pp.is_absolute():
                pp = base / pp
            ap = str(pp.resolve())
            if ap not in seen:
                seen.add(ap)
                resolved.append(ap)
    return resolved


def _extract_test_summary(text: str) -> Optional[dict]:
    """Pull a pytest-style pass/fail summary from worker stdout/stderr.

    Returns e.g. {"passed": 3, "failed": 1, "summary": "2 failed, 1 passed"}
    or None when no summary line is present. Evidence for testing tasks.
    """
    if not text:
        return None
    summary = None
    passed = failed = 0
    for line in text.splitlines():
        low = line.lower()
        # "===== 2 failed, 1 passed in 0.12s ====="  /  "3 passed in 0.10s"
        if "passed" in low or "failed" in low:
            # Only count the canonical summary line (contains 'in <time>s' or
            # is the final tally), not inline "FAILED test_x" collector lines.
            if "in " in low and ("s" in low.split("in")[-1] or "passed" in low):
                summary = line.strip().strip("=").strip()
                import re as _re
                for tok in ("passed", "failed", "error", "skipped"):
                    m = _re.search(rf"(\d+)\s+{tok}", low)
                    if m:
                        if tok == "passed":
                            passed = int(m.group(1))
                        elif tok == "failed":
                            failed = int(m.group(1))
    if summary is None:
        return None
    return {"passed": passed, "failed": failed, "summary": summary}


def _git_diff_present(workspace: str) -> bool:
    """True if the workspace has an uncommitted working-tree change (evidence
    that a git/commit task actually did work)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except Exception:
        return False


def _git_diff_summary(workspace: str) -> str:
    """One-line evidence of working-tree change (file count)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10)
        files = [ln for ln in out.stdout.splitlines() if ln.strip()]
        return f"{len(files)} file(s) changed" if files else "no changes"
    except Exception:
        return "unknown"


def verify_task_artifacts(
    task, result, workspace: str = ".",
) -> VerificationResult:
    """Evidence-based verification for one executed task.

    Rules:
      - No file referenced by the task -> PASS (nothing to verify; trust the
        executor). We never fail a task we cannot evidence-check.
      - File referenced AND exists (or worker reported an artifact) -> PASS.
      - File referenced BUT missing and no artifact produced -> FAIL with a
        reason naming expected vs observed. This is the "no file exists" guard.

    Evidence is captured per task type: testing tasks record the pytest summary;
    git/deployment tasks record a working-tree diff.
    """
    ttype = (getattr(task, "task_type", "") or "").lower()
    evidence: dict = {}

    # Testing evidence: the test runner's pass/fail summary is the proof.
    if ttype in ("testing", "verification"):
        summary = _extract_test_summary(f"{result.stdout}\n{result.stderr}")
        if summary is not None:
            evidence["test_summary"] = summary
            if not result.success and summary.get("failed"):
                return VerificationResult(
                    passed=False,
                    reason=f"tests failed: {summary['summary']}",
                    evidence=evidence)

    # Git/deployment evidence: a committed/changed working tree is the proof.
    if ttype in ("git", "deployment", "infrastructure") and result.success:
        if _git_diff_present(workspace):
            evidence["git"] = _git_diff_summary(workspace)

    paths = expected_paths(task, workspace)
    if not paths:
        if evidence:
            return VerificationResult(
                passed=result.success,
                reason=result.error or "evidence captured",
                evidence=evidence)
        return VerificationResult(passed=True, reason="no expected artifact")

    produced = set(result.artifacts or [])
    existing = [p for p in paths if Path(p).exists()]
    if existing or produced:
        found = sorted(set(existing) | produced)
        if evidence:
            return VerificationResult(
                passed=True,
                reason=f"artifact(s) present: {', '.join(Path(f).name for f in found)}",
                evidence=evidence)
        return VerificationResult(
            passed=True,
            reason=f"artifact(s) present: {', '.join(Path(f).name for f in found)}")

    missing = [Path(p).name for p in paths]
    reason = (f"expected artifact(s) not found: {', '.join(missing)} "
              f"(checked {len(paths)} referenced path(s))")
    return VerificationResult(passed=False, reason=reason, evidence=evidence)


def verify_creation_task(task, result, workspace: str = ".") -> VerificationResult:
    """Strict check for creation-type tasks: the contracted file must exist.

    A creation task's whole purpose is to produce a named artifact on disk. If
    the planner stamped a concrete path (hello.py), only THAT file satisfies the
    contract — a worker that writes a different file (goodbye.py) must FAIL
    truthfully. A creation task with no named file falls back to the lenient
    artifact check, since the planner gave us nothing to verify against.
    """
    ttype = (getattr(task, "task_type", "") or "").lower()
    if ttype not in _CREATION_TASK_TYPES:
        return verify_task_artifacts(task, result, workspace)
    paths = expected_paths(task, workspace)
    if not paths:
        # Creation task but planner named no file -> cannot evidence-check.
        return VerificationResult(passed=True, reason="creation task, no named artifact")
    # Only the contracted path(s) satisfy a creation task. An unrelated artifact
    # (e.g. goodbye.py when hello.py was expected) is NOT sufficient.
    wanted = {Path(p).resolve() for p in paths}
    produced_paths = {Path(workspace).resolve() / a for a in (result.artifacts or [])}
    existing = [p for p in paths if Path(p).exists()]
    produced_match = [a for a in (result.artifacts or [])
                      if (Path(workspace).resolve() / a).resolve() in wanted]
    if existing or produced_match:
        found = sorted({Path(p).name for p in existing} | set(produced_match))
        return VerificationResult(
            passed=True,
            reason=f"artifact present: {', '.join(found)}",
            evidence={"artifacts": found})
    missing = [Path(p).name for p in paths]
    wrote = [Path(a).name for a in (result.artifacts or [])] or ["(none)"]
    return VerificationResult(
        passed=False,
        reason=(f"creation task produced wrong artifact: expected "
                f"{', '.join(missing)}, worker wrote {', '.join(wrote)}"),
        evidence={"expected": missing, "produced": wrote})
