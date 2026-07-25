"""Pillar B Stage 4 — Skill Formation.

Takes labeled workflow intents (Stage 3) and forms them into deployable,
replayable skills registered in the shared workers registry.
"""

from __future__ import annotations

import json
from typing import Optional
from uuid import uuid4

from .db import (
    get_mined_patterns,
    get_workflow_intents,
    get_formed_skill_by_intent,
    insert_formed_skill,
    insert_worker,
    insert_worker_history,
    insert_worker_version,
    now_iso,
)
from .db import WorkerRow, WorkerHistoryRow, WorkerVersionRow


_CONSENSUS_THRESHOLD = 0.8


def form_skills(conn) -> list[dict]:
    """Run the skill formation pipeline.

    For each high/medium confidence workflow intent that doesn't already
    have a formed skill, creates the skill record and registers a worker.

    Returns list of formed skill dicts that were created (empty if none).
    """
    intents = get_workflow_intents(conn)
    if not intents:
        return []

    created: list[dict] = []

    for intent in intents:
        confidence = (intent.get("confidence") or "low").lower()
        if confidence not in ("high", "medium"):
            continue

        intent_id = intent["id"]

        # Skip if already formed.
        existing = get_formed_skill_by_intent(conn, intent_id)
        if existing:
            continue

        skill = _form_one(conn, intent)
        if skill:
            created.append(skill)

    return created


def _form_one(conn, intent: dict) -> Optional[dict]:
    """Form one skill from a workflow intent."""
    now = now_iso()
    intent_id = intent["id"]

    # Build task_graph from pattern_summary.
    pattern_summary = intent.get("pattern_summary", "")
    try:
        task_graph = json.loads(pattern_summary) if pattern_summary else []
    except (json.JSONDecodeError, TypeError):
        task_graph = []

    if not task_graph:
        return None

    # Get the source mined_pattern for exemplars.
    pattern_id = intent.get("pattern_id")
    exemplar_data: dict = {}
    if pattern_id:
        patterns = get_mined_patterns(conn, limit=9999)
        for p in patterns:
            if p["id"] == pattern_id:
                try:
                    raw = p.get("exemplars", "{}")
                    exemplar_data = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError):
                    exemplar_data = {}
                break

    # Resolve exemplar values with consensus check.
    resolved_exemplars: dict[str, dict] = {}
    has_low_consensus = False
    for pos_key, dist in exemplar_data.items():
        if not isinstance(dist, dict):
            continue
        total = sum(dist.values())
        if total == 0:
            continue
        best_val = max(dist, key=dist.get)
        best_count = dist[best_val]
        consensus = best_count / total
        is_stable = consensus >= _CONSENSUS_THRESHOLD
        if not is_stable:
            has_low_consensus = True
        resolved_exemplars[pos_key] = {
            "default": best_val,
            "distribution": dist,
            "consensus": round(consensus, 3),
            "stable": is_stable,
        }

    # Determine worker status based on confidence + consensus cap.
    intent_conf = (intent.get("confidence") or "low").lower()

    # Apply distribution cap: low-consensus step caps at "medium".
    if has_low_consensus and intent_conf == "high":
        effective_conf = "medium"
    else:
        effective_conf = intent_conf

    # Map to worker status: high -> beta, medium -> proposed.
    status_map = {"high": "beta", "medium": "proposed"}
    worker_status = status_map.get(effective_conf, "proposed")

    # Derive worker name from intent label.
    label = intent.get("intent_label", "unnamed_workflow")
    worker_name = _sanitize_name(label)

    # Insert formed_skills row.
    fs_data = {
        "workflow_intent_id": intent_id,
        "task_graph": json.dumps(task_graph),
        "exemplars": json.dumps(resolved_exemplars),
        "invocation_count": 0,
        "last_invoked_at": None,
        "created_at": now,
        "updated_at": now,
    }
    skill_id = insert_formed_skill(conn, fs_data)

    # Register worker in workers table with kind='formed_skill'.
    impl_ref = f"formed_skill:{skill_id}"
    wid = f"worker:{worker_name}:{uuid4().hex[:8]}"

    description = intent.get("intent_description", label) or label

    w = WorkerRow(
        id=wid,
        name=worker_name,
        kind="formed_skill",
        description=description[:500],
        capabilities="Workflow Replay",
        confidence=effective_conf,
        version="0.1.0",
        status=worker_status,
        schema_version="1.0",
        created_at=now,
        updated_at=now,
        availability="available",
        manifest_ref=impl_ref,
        worker_kind="formed_skill",
    )
    insert_worker(conn, w)

    # Record history.
    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=now,
            worker_id=wid,
            name=worker_name,
            kind="formed_skill",
            version="0.1.0",
            status=worker_status,
            capabilities="Workflow Replay",
            limitations="auto-formed from observed workflow; verify before use",
            event_type="skill_formation",
            note=f"Formed from workflow intent #{intent_id}: {label[:200]}",
        )
    ])
    insert_worker_version(conn, [
        WorkerVersionRow(
            worker_id=wid,
            version="0.1.0",
            registered_at=now,
            changelog=f"Initial skill formed from workflow intent #{intent_id}: {label[:200]}",
        )
    ])

    return {
        "skill_id": skill_id,
        "worker_id": wid,
        "worker_name": worker_name,
        "status": worker_status,
        "confidence": effective_conf,
        "step_count": len(task_graph),
    }


def _sanitize_name(label: str) -> str:
    """Derive a clean kebab-case name from an intent label."""
    import re
    name = label.lower().strip()
    name = re.sub(r"[^a-z0-9_ ]", "", name)
    parts = name.strip().split()[:4]
    return "_".join(parts) if parts else "formed_workflow"


def format_formed_skills(skills: list[dict]) -> str:
    """Render formed skills as human-readable report."""
    if not skills:
        return "No formed skills yet."
    lines = ["Formed Skills", "=" * 40, ""]
    for i, s in enumerate(skills, 1):
        lines.append(f"{i}. {s.get('worker_name', '?')}")
        lines.append(f"   Worker ID: {s.get('worker_id', '?')}")
        lines.append(f"   Status: {s.get('status', '?')}")
        lines.append(f"   Steps: {s.get('step_count', 0)}")
        context_parts = []
        if s.get("skill_id"):
            context_parts.append(f"skill_id={s['skill_id']}")
        if s.get("confidence"):
            context_parts.append(f"confidence={s['confidence']}")
        if context_parts:
            lines.append(f"   [{', '.join(context_parts)}]")
        lines.append("")
    return "\n".join(lines)
