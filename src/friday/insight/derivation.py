"""Insight derivation rules (Milestone 8.5) — LLM-grounded rewrite.

Transforms accumulated UNDERSTANDING (plus Initiatives and Knowledge) into
rare, high-value ENGINEERING INSIGHTS via LLM synthesis over real evidence.
Replaces the old rule-engine system that produced 6 of 7 generic boilerplate
paragraphs.

Design (mirrors synthesis.py / understanding/derivation.py):
  - Gather all understanding, initiative, and knowledge evidence.
  - Call the LLM with the full workspace context and ask it to identify what's
    specifically notable — or null if nothing stands out.
  - The LLM determines the InsightType from what it finds, not from a fixed
    rule table.
  - Deterministic fallback: when no LLM is available, emit zero insights
    instead of running the rule engine. An empty result is more honest than
    a generic paragraph.
  - Ephemerality preserved: the insight engine still retires insights whose
    triggering conditions no longer fire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from ..services.llm import _call as llm_call
from ..services.llm import _enabled as llm_enabled
from ..understanding.models import UnderstandingType
from .models import InsightType


# ---------------------------------------------------------------------------
# Candidate — a tentative insight before confidence aggregation + lifecycle.
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    type: InsightType
    title: str
    statement: str
    understanding_ids: List[str] = field(default_factory=list)
    initiative_ids: List[str] = field(default_factory=list)
    knowledge_ids: List[str] = field(default_factory=list)
    repos: List[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.type, self.title)

    def evidence_count(self) -> int:
        return (len(self.understanding_ids) + len(self.initiative_ids)
                + len(self.knowledge_ids))


# ---------------------------------------------------------------------------
# Data structures for building the evidence prompt
# ---------------------------------------------------------------------------


def _build_evidence_text(
    understanding: List,
    initiatives: List,
    knowledge: List,
) -> str:
    """Build a structured evidence summary for the LLM prompt.

    Only includes entries that have actual statements (not template holdovers).
    Grouped by type for clarity.
    """
    lines: List[str] = []

    # Understanding
    if understanding:
        lines.append("=== ENGINEERING UNDERSTANDING ===")
        # Group by type
        by_type = {}
        for u in understanding:
            ut = getattr(u, "type", None)
            utype = getattr(ut, "value", "unknown") if ut is not None else "unknown"
            by_type.setdefault(utype, []).append(u)
        for utype, items in sorted(by_type.items()):
            label = utype.replace("_", " ").title()
            lines.append(f"\n{label} ({len(items)}):")
            for u in items:
                subject = getattr(u, "subject", "?")
                stmt = getattr(u, "statement", "")
                conf = getattr(u, "confidence", None)
                conf_str = getattr(conf, "value", "?") if conf is not None else "?"
                if stmt:
                    lines.append(f"  - [{conf_str}] {subject}: {stmt}")

    # Initiatives
    if initiatives:
        lines.append("\n=== ENGINEERING INITIATIVES ===")
        for i in initiatives:
            title = getattr(i, "title", "?")
            stmt = getattr(i, "statement", "")
            conf = getattr(i, "confidence", None)
            conf_str = getattr(conf, "value", "?") if conf is not None else "?"
            status = getattr(i, "status", None)
            status_str = getattr(status, "value", "?") if status is not None else "?"
            repos = getattr(i, "participating_repositories", []) or []
            repo_str = f" [repos: {', '.join(repos[:5])}]" if repos else ""
            if stmt:
                lines.append(f"  - [{conf_str}] ({status_str}) {title}: {stmt}{repo_str}")

    # Knowledge (summarized, no more than 3 of each type)
    if knowledge:
        lines.append("\n=== ENGINEERING KNOWLEDGE ===")
        by_type = {}
        for k in knowledge:
            kt = getattr(k, "type", None)
            ktype = getattr(kt, "value", "unknown") if kt is not None else "unknown"
            by_type.setdefault(ktype, []).append(k)
        for ktype, items in sorted(by_type.items()):
            label = ktype.replace("_", " ").title()
            n = len(items)
            lines.append(f"\n{label} ({n}):")
            for k in items[:3]:
                subject = getattr(k, "subject", "?")
                stmt = getattr(k, "statement", "")
                conf = getattr(k, "confidence", None)
                conf_str = getattr(conf, "value", "?") if conf is not None else "?"
                if stmt:
                    lines.append(f"  - [{conf_str}] {subject}: {stmt}")
            if n > 3:
                lines.append(f"  ... and {n - 3} more")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM system prompt for insight generation
# ---------------------------------------------------------------------------

_INSIGHT_SYSTEM = (
    "You are Friday's insight layer. Your job is to examine a workspace's "
    "engineering understanding, initiatives, and knowledge, and identify "
    "what is genuinely notable — findings that deserve human attention.\n\n"
    "Rules:\n"
    "1. Base your analysis ONLY on the evidence provided below.\n"
    "2. Look for genuinely non-obvious patterns: repeated problems solved "
    "independently, converging/diverging directions, emerging risks that "
    "are reinforced by multiple signals, breakthrough moments where "
    "expertise is crossing a threshold.\n"
    "3. A valid insight must be SURPRISING or ACTIONABLE — something the "
    "engineer would not have noticed themselves. Obvious facts (\"you have "
    "many Python projects\") are not insights.\n"
    "4. Do NOT force a finding. If nothing genuinely notable emerges, "
    "return an empty findings list — that is a valid, honest result.\n"
    "5. Each insight needs a semantic title (never a repo name) and a "
    "1-3 sentence explanation.\n"
    "6. Confidence reflects how much evidence supports the finding. "
    "\"Strong\" means >=3 independent evidence items, \"Medium\" means 2, "
    "\"Weak\" means 1.\n\n"
    "Return valid JSON only:\n"
    '{"findings": [{"title": str, "type": str, "statement": str, '
    '"confidence": str}], "workspace_note": str|null}\n\n'
    '  type: one of "engineering_opportunity", "engineering_risk", '
    '"engineering_recommendation", "engineering_convergence", '
    '"engineering_divergence", "engineering_bottleneck", '
    '"engineering_blind_spot", "engineering_debt", "engineering_reuse", '
    '"engineering_momentum", "engineering_drift", "engineering_investment", '
    '"engineering_warning", "engineering_breakthrough", '
    '"engineering_efficiency", "engineering_focus"\n'
    '  confidence: "Strong" | "Medium" | "Weak"\n'
    '  workspace_note: optional one-sentence summary of the workspace state\n\n'
    'When findings is empty, set workspace_note to explain why.'
)

_USER_TEMPLATE = """Below is the evidence from your engineering workspace:

{evidence}

Analyze this evidence and return any genuinely notable insights. Be honest:
if nothing stands out, return an empty findings list.
"""


# ---------------------------------------------------------------------------
# Deterministic fallback — emit zero insights when the LLM is unavailable.
# This is the honest choice: the old rule engine produced 6 boilerplate
# paragraphs and 1 real finding out of 7. Zero is better than 6/7 filler.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def detect(
    understanding: List,
    initiatives: List,
    knowledge: List,
) -> List[Candidate]:
    """Run insight detection.

    Calls the LLM with the full workspace evidence. When the LLM is
    unavailable or returns empty, emits zero insights — an honest result
    that avoids generic boilerplate.

    Returns candidates for the insight engine to process (confidence
    aggregation, lifecycle, persistence).
    """
    if not llm_enabled():
        # No LLM: emit zero insights. The old rule engine is removed
        # because it produced 6/7 generic paragraphs.
        return []

    evidence = _build_evidence_text(understanding, initiatives, knowledge)

    # If there's nothing to analyze, don't bother calling the LLM.
    if not evidence.strip():
        return []

    user = _USER_TEMPLATE.format(evidence=evidence[:12000])

    content = llm_call(_INSIGHT_SYSTEM, user)
    if not content:
        return []

    # Parse JSON
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip().strip("`").strip()

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return []

    raw_findings = data.get("findings", []) if isinstance(data, dict) else []
    if not raw_findings:
        return []

    # Resolve types
    out: List[Candidate] = []
    for f in raw_findings:
        title = f.get("title", "")
        type_str = f.get("type", "")
        statement = f.get("statement", "")
        conf_str = f.get("confidence", "Weak")

        # Map confidence string to our enum
        if conf_str.lower() not in ("strong", "medium", "weak"):
            conf_str = "Weak"

        try:
            itype = InsightType.from_str(type_str)
        except ValueError:
            continue

        if not title or not statement:
            continue

        # Collect evidence ids that support this finding
        uids = [u.id for u in understanding if u.id]
        iids = [i.id for i in initiatives if i.id]
        kids = [k.id for k in knowledge if k.id]

        # For repos, collect from initiatives
        repos = []
        for i in initiatives:
            r = getattr(i, "participating_repositories", []) or []
            repos.extend(r)
        repos = sorted(set(repos))

        out.append(Candidate(
            type=itype,
            title=title,
            statement=statement,
            understanding_ids=uids,
            initiative_ids=iids,
            knowledge_ids=kids,
            repos=repos,
        ))

    return out
