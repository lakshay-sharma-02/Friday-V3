"""Resolver — the ONE NLU point (Wave 13a).

``resolve(text)`` is the single entry point every surface calls (voice
router, ``friday6 talk``, web chat). It is **LLM-first**: intent +
entities + confidence come from the LLM through ``classify()``; the
deterministic rules run only when the LLM is absent/offline. The result
is a canonical :class:`ResolvedAction` that execution/missions/reasoning
consume — the same command language everywhere.

Never raises — unknown input comes back with
``needs_clarification=True`` instead of an error.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .confidence import Assessment, assess
from .entities import Entity, EntityType, extract, find_type
from .intent import Intent, IntentResult, classify, is_agentic_goal, _ide_target
from .llm import LLMClient

#: A git clone URL (https/git@ssh), matched case-insensitively. The
#: URL IS the concrete command — "clone it <url>" / "clone <url>" must
#: route to the claude executor (it clones + sets up), never a dead
#: "git" guess with an empty command.
_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://|git://)[^\s]+",
    re.IGNORECASE)

logger = logging.getLogger("friday_v6.nlu.resolver")

#: action_type values MUST match friday_v6.execution's executors.
_EXECUTION_TYPES = frozenset({"shell", "git", "file", "python", "testing",
                              "claude"})


@dataclass
class ResolvedAction:
    """Canonical, surface-independent interpretation of one utterance."""

    text: str
    intent: Intent
    action_type: Optional[str] = None
    command: str = ""
    target: Optional[str] = None
    goal: Optional[str] = None
    entities: list[Entity] = field(default_factory=list)
    assessment: Optional[Assessment] = None

    @property
    def needs_clarification(self) -> bool:
        return bool(self.assessment and self.assessment.needs_clarification)

    @property
    def clarification(self) -> Optional[str]:
        return self.assessment.clarification if self.assessment else None

    @property
    def can_execute(self) -> bool:
        return (self.intent == Intent.EXECUTE
                and self.action_type in _EXECUTION_TYPES
                and not self.needs_clarification)

    def to_execution(self) -> Optional[dict]:
        if not self.can_execute:
            return None
        return {
            "action_type": self.action_type,
            "command": self.command,
            "goal": self.text,
        }

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "intent": self.intent.value,
            "action_type": self.action_type,
            "command": self.command,
            "target": self.target,
            "goal": self.goal,
            "entities": [{"type": e.type.value, "value": e.value}
                         for e in self.entities],
            "needs_clarification": self.needs_clarification,
            "clarification": self.clarification,
            "can_execute": self.can_execute,
        }


def resolve(text: str, llm: Optional[LLMClient] = None) -> ResolvedAction:
    """Interpret ``text`` — LLM first, deterministic fallback. Never raises.

    Args:
        text: The utterance.
        llm: The single NLU point's LLM client. ``None`` (or the
            package default when unset) → deterministic fallback.

    Returns:
        A canonical :class:`ResolvedAction`.
    """
    raw = (text or "").strip()
    result: IntentResult = classify(raw, llm=llm)

    # LLM path: entities + clarification come from the model.
    llm_clarification = None
    if llm is not None and result.needs_clarification and result.clarification:
        llm_clarification = result.clarification
    entity_values = result.entity_values if result.entity_values else None
    entities = extract(raw, entity_values)
    decision = assess(result, llm_clarification=llm_clarification)

    action_type: Optional[str] = None
    command = ""
    target: Optional[str] = None
    goal: Optional[str] = None

    if result.intent == Intent.EXECUTE:
        action_type = result.action_type
        command = _resolve_execute_command(result, entities)
        # Wave 18 hands: a complex agentic goal with no concrete
        # command ("figure out why the build fails and fix it")
        # delegates to the Claude Code CLI — the task text IS the
        # command. Explicit "claude" from the LLM wins; the fallback
        # catches goal-shaped utterances whose resolved command is
        # empty/no executor.
        if action_type == "claude":
            command = raw
        elif (not command and not result.needs_clarification
                and is_agentic_goal(raw)):
            action_type = "claude"
            command = raw
        # A URL in the utterance IS the command: "clone it <url>" /
        # "clone <url>" / "pull <url>" must route to the claude
        # executor as an explicit task ("clone <url> into a sensible
        # project dir"), never a dead "git" guess with an empty
        # command that fails at the gate or asks "what would you like
        # me to run?".
        if (action_type != "claude" and _URL_RE.search(raw)):
            action_type = "claude"
            command = raw
        if action_type == "claude" and not llm_clarification:
            # assess() above flagged EXECUTE-without-action_type as
            # needing clarification — but we've just routed it to the
            # claude executor, so that generic "what would you like me
            # to run?" no longer applies. The task text IS the command.
            # (An LLM-requested clarification is still respected.)
            decision = Assessment()
        target = command or None
    elif result.intent == Intent.DESKTOP:
        target = result.target
    elif result.intent == Intent.IDE:
        # Wave 6: the file being diagnosed. LLM target wins; entity
        # extraction and the deterministic trigger regex back it up so
        # every surface lands on a real path.
        target = result.target
        if not target:
            ent = (find_type(entities, EntityType.FILE)
                   or find_type(entities, EntityType.PATH))
            if ent:
                target = ent.value
        if not target:
            target = _ide_target(raw)
    elif result.intent in (Intent.ASK, Intent.RESEARCH, Intent.SKILL,
                           Intent.ACCEPT, Intent.DENY, Intent.SECURITY,
                           Intent.MEMORY, Intent.STYLE):
        target = result.target or None
        if result.intent == Intent.MEMORY and not goal:
            goal = result.goal or result.target or None
        if result.intent == Intent.STYLE and not goal:
            goal = result.goal or raw
    elif result.intent == Intent.PLAN:
        goal = result.goal or result.target or raw

    return ResolvedAction(
        text=raw,
        intent=result.intent,
        action_type=action_type,
        command=command,
        target=target,
        goal=goal,
        entities=entities,
        assessment=decision,
    )


def _resolve_execute_command(result: IntentResult,
                             entities: list[Entity]) -> str:
    """Best command string for an EXECUTE action (LLM or fallback slots)."""
    action_type = result.action_type
    if action_type == "git":
        return result.command or ""
    if action_type in ("testing", "file"):
        ent = (find_type(entities, EntityType.PATH)
               or find_type(entities, EntityType.FILE))
        if ent:
            return ent.value
    return result.command or ""


__all__ = ["ResolvedAction", "resolve", "_EXECUTION_TYPES"]
