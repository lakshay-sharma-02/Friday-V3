"""Pillar B Stage 3 — Intent Labeling.

Takes mined action patterns (from Stage 2) and uses an LLM to infer the
*workflow intent* — the goal the user was trying to accomplish, not just the
literal sequence of actions. This is the jump from "what they did" to "what
they were trying to do."

The labeler is:
- **Grounded**: passes the actual pattern sequence + context (workspace, project,
  frequency) to the LLM. No guessing from summaries.
- **Deterministic fallback**: if LLM is unavailable, returns a label derived
  directly from the action types (e.g. "WorkspaceSwitch -> AppLaunch").
- **Idempotent**: re-labeling the same pattern produces similar labels.
- **Isolated**: a failed LLM call returns the fallback label, never raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .db import now_iso


@dataclass
class WorkflowIntent:
    """A labeled workflow intent derived from a mined action pattern.

    ``intent_label`` is a short human-readable name (e.g. "Start dev server").
    ``intent_description`` is a longer explanation of what the user was doing.
    ``steps`` are natural-language descriptions of each action step.
    ``confidence`` reflects LLM certainty: ``"high"``, ``"medium"``, ``"low"``,
    or ``"fallback"`` when no LLM was available.
    """

    pattern_seq: list[tuple[str, str]] = field(default_factory=list)
    pattern_count: int = 0
    pattern_workspace: str = ""
    pattern_project: str = ""
    intent_label: str = ""
    intent_description: str = ""
    steps: list[str] = field(default_factory=list)
    confidence: str = "fallback"
    labeled_at: str = ""

    def to_dict(self) -> dict:
        return {
            "intent_label": self.intent_label,
            "intent_description": self.intent_description,
            "steps": self.steps,
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# System prompt for the labeling LLM call
# ---------------------------------------------------------------------------

_LABEL_SYSTEM_PROMPT = (
    "You infer the WORKFLOW INTENT behind a sequence of desktop actions. "
    "Output ONLY valid JSON with these keys:\n"
    "- \"intent_label\": a short, memorable name for this workflow "
    "(2-5 words, e.g. 'Start dev server' or 'Open project files')\n"
    "- \"intent_description\": one sentence explaining what the user is trying to accomplish\n"
    "- \"steps\": an array of strings, each describing ONE action in natural language "
    "(e.g. 'Open VS Code terminal', 'Run npm run dev' — not 'Type the command')\n"
    "- \"confidence\": 'high' if the pattern is very clear, 'medium' if plausible, "
    "'low' if you're guessing\n\n"
    "Be specific to the actual actions — don't invent steps that aren't in the sequence. "
    "If the pattern is 'workspace_switch -> workspace_switch', the intent is probably "
    "'Browse workspaces', not something elaborate.\n\n"
    "Respond with ONLY the JSON object, no markdown, no explanation."
)

_USER_TEMPLATE = """Analyze this action pattern and infer the workflow intent:

Pattern:
{pattern_text}

Context:
- Workspace: {workspace}
- Project: {project}
- Occurrences: {count}

Remember: describe what the user is likely TRYING TO DO, not just what the actions literally are.
Output JSON only."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def label_intent(
    pattern_sequence: list[tuple[str, str]],
    pattern_count: int = 1,
    workspace: str = "",
    project: str = "",
) -> WorkflowIntent:
    """Label a mined action pattern with a workflow intent.

    Uses the LLM if available; falls back to a deterministic label derived
    from the action types. The result is always a ``WorkflowIntent`` —
    never None, never raises.

    Args:
        pattern_sequence: List of (action_type, target) tuples.
        pattern_count: How many sessions this pattern appeared in.
        workspace: The most common workspace_id for this pattern.
        project: The most common project name for this pattern.

    Returns:
        A ``WorkflowIntent`` with the LLM label (or fallback).
    """
    pattern_text = _format_pattern(pattern_sequence)

    # Try LLM.
    result_text = _call_llm(pattern_text, pattern_count, workspace, project)

    if result_text:
        parsed = _parse_llm_result(result_text)
        if parsed:
            return WorkflowIntent(
                pattern_seq=pattern_sequence,
                pattern_count=pattern_count,
                pattern_workspace=workspace,
                pattern_project=project,
                intent_label=parsed.get("intent_label", _fallback_label(pattern_sequence)),
                intent_description=parsed.get("intent_description", ""),
                steps=parsed.get("steps", _fallback_steps(pattern_sequence)),
                confidence=parsed.get("confidence", "medium"),
                labeled_at=now_iso(),
            )

    # Fallback: deterministic label from action types.
    return WorkflowIntent(
        pattern_seq=pattern_sequence,
        pattern_count=pattern_count,
        pattern_workspace=workspace,
        pattern_project=project,
        intent_label=_fallback_label(pattern_sequence),
        intent_description=_fallback_description(pattern_sequence),
        steps=_fallback_steps(pattern_sequence),
        confidence="fallback",
        labeled_at=now_iso(),
    )


# ---------------------------------------------------------------------------
# LLM integration
# ---------------------------------------------------------------------------


def _call_llm(pattern_text: str, count: int, workspace: str, project: str) -> Optional[str]:
    """Call the LLM to label a pattern. Returns raw JSON text or None."""
    try:
        from .services.llm import _call as _llm_call

        user = _USER_TEMPLATE.format(
            pattern_text=pattern_text,
            workspace=workspace or "(unknown)",
            project=project or "(unknown)",
            count=count,
        )
        return _llm_call(_LABEL_SYSTEM_PROMPT, user)
    except Exception:
        return None


def _parse_llm_result(raw: str) -> Optional[dict]:
    """Parse the LLM's JSON response."""
    raw = raw.strip()
    # Strip markdown fences if present.
    if raw.startswith("```"):
        # Find the first { and last }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]
    try:
        obj = json.loads(raw)
        if "intent_label" in obj:
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Deterministic fallback strategies
# ---------------------------------------------------------------------------


def _fallback_label(sequence: list[tuple[str, str]]) -> str:
    """Generate a deterministic label from action types alone."""
    if not sequence:
        return "Empty pattern"
    # Group by action type and count.
    from collections import Counter
    types = [a for a, _ in sequence]
    counts = Counter(types)
    primary = counts.most_common(1)[0][0]
    return f"{primary} × {len(sequence)}"


def _fallback_description(sequence: list[tuple[str, str]]) -> str:
    """Generate a deterministic description from action types."""
    if not sequence:
        return ""
    step_names = " → ".join(t for t, _ in sequence)
    return f"Repeated desktop workflow: {step_names}"


def _fallback_steps(sequence: list[tuple[str, str]]) -> list[str]:
    """Generate deterministic step descriptions from action types."""
    steps = []
    for i, (action_type, target) in enumerate(sequence, 1):
        if target and target != action_type.split("_")[0]:
            steps.append(f"Action {i}: {action_type} ({target})")
        else:
            steps.append(f"Action {i}: {action_type}")
    return steps


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_pattern(sequence: list[tuple[str, str]]) -> str:
    """Format a pattern sequence for the LLM prompt."""
    lines = []
    for i, (action_type, target) in enumerate(sequence, 1):
        if target and target != f"<{action_type.split('_')[0]}>":
            lines.append(f"  {i}. {action_type} → {target}")
        else:
            lines.append(f"  {i}. {action_type}")
    return "\n".join(lines)


def format_intents(intents: list[WorkflowIntent]) -> str:
    """Render labeled intents as a human-readable report."""
    if not intents:
        return "No workflow intents labeled yet."

    lines = ["Workflow Intents", "=" * 40, ""]
    for i, intent in enumerate(intents, 1):
        confidence_mark = {
            "high": "✓", "medium": "~", "low": "?", "fallback": "⚙",
        }.get(intent.confidence, "?")
        lines.append(f"{i}. [{confidence_mark}] {intent.intent_label}")
        if intent.intent_description:
            lines.append(f"   {intent.intent_description}")
        if intent.steps:
            for step in intent.steps:
                lines.append(f"   • {step}")
        context_parts = []
        if intent.pattern_workspace:
            context_parts.append(f"ws={intent.pattern_workspace}")
        if intent.pattern_project:
            context_parts.append(f"project={intent.pattern_project}")
        if intent.pattern_count > 1:
            context_parts.append(f"{intent.pattern_count}x")
        if context_parts:
            lines.append(f"   [{', '.join(context_parts)}]")
        lines.append("")
    return "\n".join(lines)
