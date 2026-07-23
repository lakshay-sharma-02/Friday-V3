"""Worker Genesis — detect capability gaps and draft worker proposals (M10.x).

When the Capability Resolver hits a capability requirement that no registered
worker satisfies, this module detects the gap, drafts candidate WorkerManifest
objects, and stores them as pending proposals. Nothing is trusted and nothing
is registered until a human runs `friday capability review` and approves.

This is the single missing piece: an assistant that becomes more capable of
your work over time, by noticing the gap between what you asked and what it
can do, and proposing a new tool to fill it.

Capabilities are always validated against the closed vocabulary. No free-form
or hallucinated capabilities reach the registry.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from ..db import (
    ProposedWorkerRow,
    insert_proposed_worker,
    get_proposed_workers,
    get_proposed_worker,
    update_proposed_worker_status,
    now_iso,
)
from .models import (
    WorkerManifest,
    validate_capabilities,
    is_valid_capability,
    all_capabilities,
)


@dataclass
class CapabilityGapEvent:
    """Record of a capability gap detected during resolution.

    Emitted when a goal requires a capability that no active worker can
    satisfy (missing from the resolved assignment). Detected deterministically
    at the resolver's failure point.
    """
    goal: str
    required_capability: str
    task_id: str
    graph_id: str = ""
    detected_at: str = field(default_factory=lambda: now_iso())

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "required_capability": self.required_capability,
            "task_id": self.task_id,
            "graph_id": self.graph_id,
            "detected_at": self.detected_at,
        }


# Track detected gaps to avoid duplicate events for repeated failures
# of the same gap in the same resolution run.
_seen_gaps: set = set()


def detect_gap(
    goal: str,
    missing_capabilities: List[str],
    task_id: str = "",
    graph_id: str = "",
) -> List[CapabilityGapEvent]:
    """Detect capability gaps from a list of missing capabilities.

    Returns one CapabilityGapEvent per missing capability that has not already
    been emitted for this (goal, gap) pair in the current run. Duplicate
    detection is reset per `reset_gap_tracking()` call.
    """
    events: List[CapabilityGapEvent] = []
    for cap in missing_capabilities:
        if not cap.strip():
            continue
        key = (goal, cap.strip())
        if key in _seen_gaps:
            continue
        _seen_gaps.add(key)
        events.append(CapabilityGapEvent(
            goal=goal,
            required_capability=cap.strip(),
            task_id=task_id,
            graph_id=graph_id,
        ))
    return events


def reset_gap_tracking() -> None:
    """Reset duplicate gap detection (called at start of each resolution run)."""
    _seen_gaps.clear()


def _tool_name_from_capability(cap: str) -> Optional[str]:
    """Derive a plausible CLI tool name from a capability string.

    Deterministic mapping based on the closed capability vocabulary:
    - Known tool-capability pairs are mapped directly.
    - Unknown capabilities return None (no deterministic match).

    This uses only the closed vocabulary — no free-form strings.
    """
    cap_lower = cap.strip().lower()
    # Known capability -> CLI tool name mappings.
    known_tools = {
        "rust": "cargo",
        "python": "python3",
        "typescript": "npx tsc",
        "javascript": "node",
        "go": "go",
        "java": "java",
        "c": "gcc",
        "c++": "g++",
        "ruby": "ruby",
        "sql": "sqlite3",
        "shell commands": "bash",
        "git operations": "git",
        "testing": "pytest",
    }
    # Also check if the capability looks like a tool name (e.g., "Blender", "Docker").
    if cap_lower in known_tools:
        return known_tools[cap_lower]
    # Check if any canonical capability alias matches a known tool name.
    _CAP_TO_TOOL = {c.lower(): c for c in all_capabilities()}
    if cap_lower in _CAP_TO_TOOL:
        # For generic capabilities (Architecture, Frontend, etc.), no tool.
        return None
    return None


def _check_path_for_tool(tool_name: str) -> bool:
    """Check if a tool binary exists on PATH."""
    if not tool_name:
        return False
    # Extract the base command from a compound string (e.g., "npx tsc" -> "npx").
    base = tool_name.split()[0]
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, base)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


def _llm_draft_manifest(
    gap: CapabilityGapEvent,
) -> Optional[WorkerManifest]:
    """Fallback: use the LLM to draft a WorkerManifest for a capability gap.

    Only invoked when no deterministic PATH match is found AND when the
    FRIDAY_LLM_MODEL environment variable is configured. The LLM output is
    validated against the closed capability vocabulary before use — any
    capability outside the vocabulary is rejected. This ensures the LLM
    is never the sole source of truth for a capability grant.

    Uses the same tier pattern as planning/derive.py: deterministic first,
    LLM only as enrichment, never sole source of truth.
    """
    llm_model = os.environ.get("FRIDAY_LLM_MODEL", "")
    if not llm_model:
        return None

    try:
        from ..services.llm import _call
        _SYSTEM = (
            "You generate WorkerManifest JSON for new worker capability profiles. "
            "Output ONLY valid JSON. Each manifest describes: name, implementation "
            "(cli|api|native), provider, origin, capabilities (from the closed "
            "vocabulary), requirements, supported_task_types, supported_plan_types, "
            "supported_languages, description, estimated_speed, estimated_cost, "
            "and confidence. Be concise. Never include capabilities outside the "
            "provided vocabulary."
        )
        _USER = (
            f"A capability gap was detected for: '{gap.required_capability}'\n"
            f"from the goal: '{gap.goal}'\n\n"
            f"Propose a WorkerManifest (JSON only, no markdown) with these fields:\n"
            f"  name: a short name for the worker\n"
            f"  implementation: one of 'cli' | 'api' | 'native'\n"
            f"  provider: 'local' or the tool vendor\n"
            f"  origin: 'generated'\n"
            f"  capabilities: list of capabilities from the closed vocabulary:\n"
            f"    {', '.join(all_capabilities())}\n"
            f"  requirements: list of PATH binaries or env vars the worker needs\n"
            f"  supported_task_types: list of task type keywords\n"
            f"  supported_plan_types: list of plan type keywords\n"
            f"  description: short description of what this worker does\n"
            f"  estimated_speed: 'fast' | 'medium' | 'slow'\n"
            f"  estimated_cost: 'low' | 'medium' | 'high'\n"
            f"  confidence: 'high' | 'medium' | 'low'\n\n"
            f"Return ONLY valid JSON, no other text.\n"
        )
        result = _call(_SYSTEM, _USER)
        if not result or not result.strip():
            return None

        # Try to extract JSON from the response.
        text = result.strip()
        # Remove markdown code fences if present.
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines.
            start = 0
            for i, line in enumerate(lines):
                if line.strip().startswith("```"):
                    start = i + 1
                    break
            end = len(lines)
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip().startswith("```"):
                    end = i
                    break
            text = "\n".join(lines[start:end]).strip()

        manifest_data = json.loads(text)
    except Exception:
        return None

    # Validate: capabilities must be in the closed vocabulary.
    raw_caps = manifest_data.get("capabilities", [])
    valid_caps = validate_capabilities(raw_caps)
    if not valid_caps:
        return None  # No valid capabilities = reject the proposal.

    name = (manifest_data.get("name") or "").strip()
    if not name:
        return None

    return WorkerManifest(
        name=name,
        implementation=manifest_data.get("implementation", "cli"),
        provider=manifest_data.get("provider", "local"),
        origin="generated",
        capabilities=valid_caps,
        requirements=manifest_data.get("requirements", []),
        supported_task_types=manifest_data.get("supported_task_types", []),
        supported_plan_types=manifest_data.get("supported_plan_types", []),
        supported_languages=manifest_data.get("supported_languages", []),
        description=manifest_data.get("description", ""),
        estimated_speed=manifest_data.get("estimated_speed", "unknown"),
        estimated_cost=manifest_data.get("estimated_cost", "unknown"),
        confidence=manifest_data.get("confidence", "medium"),
    )


def draft_manifest(gap: CapabilityGapEvent) -> Optional[WorkerManifest]:
    """Draft a WorkerManifest for a capability gap.

    Uses a 3-tier approach:
    1. Deterministic: check PATH for a CLI tool whose name is derived from
       the capability/task type vocabulary.
    2. LLM fallback: only if FRIDAY_LLM_MODEL is configured, ask the LLM to
       draft a manifest. LLM output is validated against the closed capability
       vocabulary — capabilities outside the vocabulary are rejected.
    3. If neither works, return None (no proposal can be drafted).

    The returned manifest is NEVER auto-registered. It must be written to the
    proposed_workers table with status=pending and approved by a human.
    """
    tool_name = _tool_name_from_capability(gap.required_capability)

    # Tier 1: Deterministic PATH check.
    if tool_name and _check_path_for_tool(tool_name):
        # Found a matching tool on PATH. Build a minimal deterministic manifest.
        cap = gap.required_capability
        # Map capability to plausible task/plan types.
        cap_lower = cap.strip().lower()
        task_types = ["implementation"]
        plan_types = ["feature"]
        if cap_lower in ("testing",):
            task_types = ["testing", "verification"]
            plan_types = ["testing"]
        elif cap_lower in ("documentation",):
            task_types = ["documentation"]
            plan_types = ["documentation"]
        elif cap_lower in ("infrastructure", "shell commands", "git operations"):
            task_types = ["infrastructure", "configuration"]
            plan_types = ["infrastructure"]

        return WorkerManifest(
            name=cap.capitalize(),
            implementation="cli",
            provider="local",
            origin="generated",
            capabilities=validate_capabilities([cap]),
            requirements=[tool_name.split()[0]],
            supported_task_types=task_types,
            supported_plan_types=plan_types,
            description=f"Local {cap} tool found on PATH ({tool_name})",
            estimated_speed="fast",
            estimated_cost="low",
            confidence="medium",
        )

    # Tier 2: LLM fallback for enrichment.
    llm_draft = _llm_draft_manifest(gap)
    if llm_draft is not None:
        return llm_draft

    # Neither tier worked — no proposal can be drafted.
    return None


def _manifest_to_json(manifest: WorkerManifest) -> str:
    """Serialize a WorkerManifest to JSON string."""
    return json.dumps({
        "name": manifest.name,
        "implementation": manifest.implementation,
        "provider": manifest.provider,
        "origin": manifest.origin,
        "capabilities": manifest.capabilities,
        "requirements": list(manifest.requirements) if manifest.requirements else [],
        "supported_task_types": list(manifest.supported_task_types),
        "supported_plan_types": list(manifest.supported_plan_types),
        "supported_languages": list(manifest.supported_languages),
        "description": manifest.description,
        "estimated_speed": manifest.estimated_speed,
        "estimated_cost": manifest.estimated_cost,
        "confidence": manifest.confidence,
    }, indent=2)


def propose_worker(
    conn,
    goal: str,
    missing_capabilities: List[str],
    task_id: str = "",
    graph_id: str = "",
) -> List[str]:
    """Detect a gap, draft a manifest, and write it to proposed_workers.

    Returns the list of proposal IDs that were created (empty if no
    deterministic or LLM draft could be produced for any gap).
    """
    events = detect_gap(goal, missing_capabilities, task_id=task_id, graph_id=graph_id)
    created: List[str] = []
    for event in events:
        manifest = draft_manifest(event)
        if manifest is None:
            continue
        # Build a deterministic, unique proposal ID.
        safe_cap = event.required_capability.replace(" ", "_").lower()
        proposal_id = f"proposal:{safe_cap}:{event.graph_id or 'adhoc'}:{event.task_id or 'unknown'}"

        # Only insert if not already pending.
        existing = get_proposed_worker(conn, proposal_id)
        if existing and existing.status == "pending":
            continue

        row = ProposedWorkerRow(
            id=proposal_id,
            detected_from_goal=goal,
            capability_gap=event.required_capability,
            draft_manifest_json=_manifest_to_json(manifest),
            status="pending",
            created_at=now_iso(),
            reviewed_at=None,
        )
        insert_proposed_worker(conn, row)
        created.append(proposal_id)
    return created


def register_approved_proposal(
    conn,
    proposal_id: str,
    registry,
) -> bool:
    """Register an approved proposal into the live WorkerRegistry.

    Validates the manifest's capabilities against the closed vocabulary.
    If validation fails (e.g., overbroad or fabricated capabilities), the
    proposal is rejected instead of registered. Returns True on success.
    """
    row = get_proposed_worker(conn, proposal_id)
    if row is None:
        return False
    if row.status != "pending":
        return False

    try:
        manifest_data = json.loads(row.draft_manifest_json)
    except (json.JSONDecodeError, TypeError):
        return False

    # Validate capabilities against the closed vocabulary.
    raw_caps = manifest_data.get("capabilities", [])
    valid_caps = validate_capabilities(raw_caps)

    # Reject if ANY declared capability is outside the vocabulary.
    # Per spec: "a proposed manifest with a fabricated/overbroad capability
    # claim is rejected at review, not silently trusted." This prevents
    # LLM hallucinations or fabricated claims from partially polluting the
    # registry even when some capabilities happen to be valid.
    #
    # We check each raw capability individually via is_valid_capability()
    # (which handles both canonical forms and aliases), rather than comparing
    # lengths, because validate_capabilities deduplicates — a manifest with
    # ["Python", "python", "Rust"] would produce valid_caps with fewer items
    # than raw_caps even though all capabilities are valid.
    from .models import (
        is_valid_capability as _is_valid_cap,
        is_valid_language as _is_valid_lang,
        is_valid_task_type as _is_valid_tt,
        is_valid_plan_type as _is_valid_pt,
    )
    for cap in raw_caps:
        c = (cap or "").strip()
        if c and not _is_valid_cap(c):
            update_proposed_worker_status(conn, proposal_id, "rejected")
            return False

    # Also validate languages, task types, plan types.
    raw_langs = manifest_data.get("supported_languages", [])
    for lang in raw_langs:
        l = (lang or "").strip()
        if l and not _is_valid_lang(l):
            update_proposed_worker_status(conn, proposal_id, "rejected")
            return False
    raw_tt = manifest_data.get("supported_task_types", [])
    for tt in raw_tt:
        t = (tt or "").strip()
        if t and not _is_valid_tt(t):
            update_proposed_worker_status(conn, proposal_id, "rejected")
            return False
    raw_pt = manifest_data.get("supported_plan_types", [])
    for pt in raw_pt:
        p = (pt or "").strip()
        if p and not _is_valid_pt(p):
            update_proposed_worker_status(conn, proposal_id, "rejected")
            return False

    from .models import (
        validate_languages,
        validate_task_types,
        validate_plan_types,
    )
    valid_langs = validate_languages(raw_langs)
    valid_task_types = validate_task_types(raw_tt)
    valid_plan_types = validate_plan_types(raw_pt)

    # Build a Worker from the manifest.
    from .engine import _worker_from_manifest
    try:
        worker = _worker_from_manifest({
            **manifest_data,
            "capabilities": valid_caps,
            "supported_languages": valid_langs,
            "supported_task_types": valid_task_types,
            "supported_plan_types": valid_plan_types,
        })
    except Exception:
        update_proposed_worker_status(conn, proposal_id, "rejected")
        return False

    # Register into the live WorkerRegistry.
    result = registry.register(worker)
    update_proposed_worker_status(conn, proposal_id, "approved")
    return True
