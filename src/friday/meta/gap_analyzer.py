"""Gap Analyzer — detect + score capability gaps from runtime execution data.

Reads runtime_results for repeated failures, timeouts, and "no worker available"
events. Cross-references against the worker capability registry. Scores gaps by:
frequency, blast radius, and estimated build cost.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from ..db import (
    get_capability_gaps,
    insert_capability_gap,
    update_capability_gap,
    get_runtime_tasks,
    get_runtime_results,
    get_runtime_evolution,
    get_all_workers,
    now_iso,
)


@dataclass
class GapReport:
    """Result of a gap analysis pass."""
    total_gaps: int = 0
    new_gaps: int = 0
    open_gaps: int = 0
    gaps: list[dict] = field(default_factory=list)

    def to_text(self) -> str:
        lines = ["Meta-Engine — Capability Gap Analysis", ""]
        if not self.gaps:
            lines.append("No capability gaps detected.")
            lines.append("(Friday's existing workers cover all observed task requirements.)")
        else:
            lines.append(f"Open gaps:     {self.open_gaps}")
            lines.append(f"New this pass: {self.new_gaps}")
            lines.append(f"Total tracked: {self.total_gaps}")
            lines.append("")
            for g in sorted(self.gaps, key=lambda x: x.get("score", 0), reverse=True):
                status = g.get("status", "?")
                freq = g.get("frequency", 0)
                score = g.get("score", 0.0)
                lines.append(f"  [{status}] score={score:.1f} freq={freq} — {g.get('description', '?')}")
        return "\n".join(lines) + "\n"


# ponytail: scoring weights are constants now; move to operator_preferences when
# the operator profile supports numeric preference values.
_FREQ_WEIGHT = 0.4
_BLAST_WEIGHT = 0.3
_COST_PENALTY = 0.3
_MAX_ATTEMPTS_PER_GAP = 3


def _now() -> str:
    return now_iso()


def _load_refs(refs_str: str) -> list:
    try:
        return json.loads(refs_str) if refs_str else []
    except (ValueError, TypeError):
        return []


def analyze(conn) -> GapReport:
    """Run one gap analysis pass. Reads runtime data, scores gaps, persists new
    ones, updates existing ones. Idempotent — re-running on the same data
    produces the same gaps."""
    # Gather raw evidence.
    failed_tasks = _get_failed_tasks(conn)
    missing_cap_events = _get_no_worker_events(conn)
    worker_caps = _get_worker_capabilities(conn)

    # Group by capability hint → gap description.
    hints: dict[str, dict] = {}
    for ft in failed_tasks:
        # The reason column or error column carries the failure description.
        reason = (ft.get("error") or ft.get("reason") or "").strip()
        worker_id = ft.get("worker_id") or "unknown"
        cap = _extract_capability_hint(reason, ft, worker_id=worker_id)
        # Group by worker_id first so different workers produce different gaps.
        # Uses a composite key — triple-colon separator is unlikely in descriptions.
        key = f"{worker_id}:::{cap}"
        if key not in hints:
            hints[key] = {"description": cap, "evidence": [], "frequency": 0}
        hints[key]["evidence"].append(ft.get("result_id"))
        hints[key]["frequency"] += 1

    for ev in missing_cap_events:
        reason = ev.get("reason", "")
        cap = _detect_missing_capability(reason)
        if cap not in hints:
            hints[cap] = {"description": cap, "evidence": [], "frequency": 0}
        hints[cap]["evidence"].append(reason[:120])
        hints[cap]["frequency"] += 1

    # Exclude gaps already covered by existing workers.
    for h in list(hints.keys()):
        if _worker_covers(h, worker_caps):
            del hints[h]

    # Score each hint and merge with stored gaps.
    existing = {g["description"]: g for g in get_capability_gaps(conn)}
    report = GapReport()

    for desc, info in hints.items():
        # Use the human-readable description (cap), not the composite key
        # (worker_id:::cap), for matching with existing gaps and storage.
        desc_key = info["description"]
        burst = _blast_radius(desc_key, info["evidence"])
        cost = _build_cost(desc_key, worker_caps)
        score = (info["frequency"] * _FREQ_WEIGHT) + (burst * _BLAST_WEIGHT) - (cost * _COST_PENALTY)
        score = max(0.0, round(score, 1))

        if desc_key in existing:
            eg = existing[desc_key]
            if eg["status"] in ("deployed", "rejected"):
                continue
            update_capability_gap(conn, eg["id"],
                                  frequency=info["frequency"],
                                  score=score,
                                  evidence_refs=json.dumps(info["evidence"][:20]),
                                  updated_at=_now())
            eg.update(frequency=info["frequency"], score=score,
                      evidence_refs=json.dumps(info["evidence"][:20]))
            report.gaps.append(eg)
        else:
            gid = insert_capability_gap(conn, {
                "description": desc_key,
                "evidence_refs": json.dumps(info["evidence"][:20]),
                "frequency": info["frequency"],
                "score": score,
                "status": "open",
                "created_at": _now(),
                "updated_at": _now(),
            })
            report.new_gaps += 1
            report.gaps.append({
                "id": gid, "description": desc_key, "score": score,
                "frequency": info["frequency"], "status": "open",
            })

    # Count.
    all_gaps = get_capability_gaps(conn)
    report.total_gaps = len(all_gaps)
    report.open_gaps = sum(1 for g in all_gaps if g["status"] == "open")
    return report


def _get_failed_tasks(conn) -> list[dict]:
    """Return tasks that failed execution, across all sessions."""
    sessions = conn.execute(
        "SELECT DISTINCT session_id FROM runtime_sessions ORDER BY created_at DESC"
    ).fetchall()
    failed = []
    for s in sessions:
        sid = s["session_id"]
        results = get_runtime_results(conn, sid)
        for r in results:
            if not r.get("success", True):
                failed.append(r)
    return failed


def _get_no_worker_events(conn) -> list[dict]:
    """Return evolution events where no worker was available."""
    rows = get_runtime_evolution(conn)
    return [r for r in rows
            if "no worker" in (r.get("reason", "")).lower()
            or "unavailable" in (r.get("reason", "")).lower()]


def _get_worker_capabilities(conn) -> set[str]:
    workers = get_all_workers(conn)
    caps: set[str] = set()
    for w in workers:
        # WorkerRow is a dataclass, not dict
        raw = w.capabilities or ""
        for c in raw.split(","):
            c = c.strip().lower()
            if c:
                caps.add(c)
    return caps


def _extract_capability_hint(reason: str, task: dict, worker_id: str = "") -> str:
    """Derive a human-readable capability gap description from failure evidence.

    Groups primarily by ``worker_id`` (set by ``analyze()``), then applies
    keyword hints on the error reason. The fallback builds a description from
    structured fields (worker_id, exit_code, input payload) rather than
    returning a static generic string — so the LLM codegen gets concrete
    evidence about what shape of input the worker will receive.
    """
    rlow = reason.lower()
    if "no worker" in rlow or "no such worker" in rlow:
        return _detect_missing_capability(reason)
    if "timeout" in rlow:
        return "Slow execution — worker may need optimization or a faster alternative"
    if "not found" in rlow or "no such file" in rlow:
        return "File-system operations require more robust path resolution"
    if "syntax" in rlow or "parse" in rlow or "compile" in rlow:
        return "Code generation quality — syntax/parse failures suggest weak code-writing capability"
    if "import" in rlow or "module" in rlow:
        return "Dependency resolution — missing or incorrect import handling"
    # Try task-level capability tags.
    caps = task.get("matched_capabilities", task.get("required_capabilities", ""))
    if caps:
        return f"Worker needed for: {caps}"
    # Fallback: build from structured fields (worker_id, exit_code, input)
    # instead of a static string. This gives the LLM concrete evidence about
    # the actual input shape.
    exit_code = task.get("exit_code") or "?"
    command = ""
    payload = task.get("payload") or ""
    if payload:
        try:
            pdata = json.loads(payload) if isinstance(payload, str) else {}
            inp = pdata.get("input", "")
            if inp:
                command = f" on '{inp[:80]}'"
        except (ValueError, TypeError):
            pass
    return f"{worker_id} exit code {exit_code}{command}"


def _detect_missing_capability(reason: str) -> str:
    """Try to extract what capability was missing from the reason."""
    rlow = reason.lower()
    for prefix in ("no worker", "no such worker", "no worker available"):
        if prefix in rlow:
            idx = rlow.find(prefix) + len(prefix)
            rest = reason[idx:].strip().strip(":;,. ")
            if rest and len(rest) < 60:
                return f"Missing worker: {rest}"
    return "Missing worker — unregistered capability needed"


def _blast_radius(desc: str, evidence: list) -> int:
    """Estimate how many pending tasks are blocked by this gap. 1-10 scale."""
    # More evidence entries = wider blast radius.
    return min(10, len(evidence) + 1)


def _build_cost(desc: str, existing_caps: set) -> int:
    """Estimate build difficulty: 1 (easy) to 10 (hard).

    A gap that already has partial capability coverage in existing workers is
    cheaper to fill than one requiring entirely new infrastructure."""
    dlow = desc.lower()
    if any(w in dlow for w in ("syntax", "parser", "compiler", "ast")):
        return 6
    if any(w in dlow for w in ("worker: claude", "worker: gemini", "llm", "ai")):
        return 3  # wrapping an existing API is cheap
    if any(w in dlow for w in ("shell", "cli", "command")):
        return 2
    if any(w in dlow for w in ("file", "path", "git")):
        return 3
    if any(w in dlow for w in ("test", "pytest")):
        return 4
    # Check for partial capability overlap.
    for cap in existing_caps:
        if any(w in cap for w in dlow.split()):
            return 3
    return 5


def _worker_covers(desc: str, existing_caps: set) -> bool:
    """Return True if an existing worker already handles this capability."""
    dlow = desc.lower()
    for cap in existing_caps:
        if dlow in cap or cap in dlow:
            return True
    return False
