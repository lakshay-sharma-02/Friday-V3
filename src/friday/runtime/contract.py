"""Execution contracts (Phase 1.5).

Every executable task declares WHAT success means. The runtime verifies that
contract against observed reality instead of guessing from natural-language
goals. This module is the contract's single source of truth: a pure,
deterministic projection of a Task's structured fields into a `TaskContract`.

No LLM, no network, no I/O beyond parsing. The planner already populates
`outputs` (expected artifacts), `verification` (steps), and `acceptance_criteria`
(success conditions) on the Task — this module reads them and turns the
artifact descriptions into concrete, checkable file paths.

The contract is METADATA. It does not change how any executor runs; it only
tells verification what must be true afterward.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Extensions that unambiguously name a real file (vs a bare word).
_FILE_EXT = re.compile(
    r"\.(py|md|txt|sh|ts|tsx|jsx|js|rs|go|rb|java|c|h|cpp|hpp|json|"
    r"yaml|yml|sql|html|css|scss|toml|cfg|ini|lock|env|pdf|csv|xml)$",
    re.IGNORECASE,
)

# Path-ish token: contains a slash and ends in a file ext, or is absolute.
_PATHISH = re.compile(r"(?:/|\./|\.\./|~)[\w./\-~]+?\.\w{1,6}\b")


@dataclass
class TaskContract:
    """What must be true after a task executes, for the runtime to call it done.

    - expected_artifacts: concrete file paths the task must produce.
    - verification_steps: structured steps the planner attached (method+detail).
    - success_conditions: human-readable conditions (acceptance criteria).
    """

    expected_artifacts: List[str] = field(default_factory=list)
    verification_steps: List[dict] = field(default_factory=list)
    success_conditions: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "expected_artifacts": list(self.expected_artifacts),
            "verification_steps": list(self.verification_steps),
            "success_conditions": list(self.success_conditions),
        }


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
    seen = set()
    uniq: List[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def contract_for_task(task, create_type: Optional[str] = None) -> TaskContract:
    """Project a Task (or RuntimeTask) into its execution contract.

    `expected_artifacts` is derived deterministically:
      1. Any structured `outputs` entries that are file paths (e.g. "hello.py",
         "src/main.py") — the planner's explicit contract.
      2. File paths named in `title`/`description` (the planner's prose often
         names the very file). This is the *fallback* so an older planner that
         did not stamp `outputs` still yields a checkable contract.
    `verification_steps` comes from `verification`; `success_conditions` from
    `acceptance_criteria`. No LLM, no guessing beyond explicit tokens.
    """
    outputs = _as_list(getattr(task, "outputs", None))
    verification = _as_list(getattr(task, "verification", None))
    conditions = _as_list(getattr(task, "acceptance_criteria", None))

    # 1) Explicit artifact paths from the structured `outputs` contract.
    artifacts: List[str] = []
    seen = set()
    for o in outputs:
        if not isinstance(o, str):
            continue
        o = o.strip()
        if not o:
            continue
        if _looks_like_path(o):
            if o not in seen:
                seen.add(o)
                artifacts.append(o)

    # 2) Fallback: file tokens named in title/description/goal.
    if not artifacts:
        for attr in ("title", "description", "goal"):
            v = getattr(task, attr, "") or ""
            if isinstance(v, str):
                for p in _scan_paths(v):
                    if p not in seen:
                        seen.add(p)
                        artifacts.append(p)

    return TaskContract(
        expected_artifacts=artifacts,
        verification_steps=[v for v in verification if isinstance(v, dict)],
        success_conditions=[c for c in conditions if isinstance(c, str)],
    )


def _as_list(v) -> List:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [v]
        except (ValueError, TypeError):
            return [v]
    return [v]


def _looks_like_path(s: str) -> bool:
    """True for a token that unambiguously names a file to produce."""
    if s.startswith("/") or s.startswith("./") or s.startswith("../") or "~" in s:
        return bool(_FILE_EXT.search(s))
    return bool(_FILE_EXT.search(s))


def resolve_artifact_paths(contract: TaskContract, workspace: str = "."
                           ) -> List[str]:
    """Resolve the contract's expected artifacts to absolute paths."""
    base = Path(workspace).resolve()
    out: List[str] = []
    for p in contract.expected_artifacts:
        pp = Path(p)
        if not pp.is_absolute():
            pp = base / pp
        out.append(str(pp.resolve()))
    return out
