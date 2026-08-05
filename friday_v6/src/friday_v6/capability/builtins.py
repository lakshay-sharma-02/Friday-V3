"""Built-in capabilities — the registry's first entries (Wave 16, Law 7).

Registers everything Friday can do out of the box:

- **Executors** — the gated execution layer's action types (shell / git /
  file / python / testing / ssh), reached via the ``execute`` intent.
- **Reasoning providers** — each question type the answer engine can
  answer (status / activity / mission / memory / conversation / skills /
  collab / style / capability), reached via ``ask``.
- **Intents** — every NL intent Friday understands (the ONE NLU point).
- **Surfaces** — every entry point (talk / voice / web / desktop /
  security / memory / skills / research / collab).

Learned skills are added dynamically by the registry (self-extension);
this module owns only the static built-ins. Every registration is pure
data — never raises.
"""

from __future__ import annotations

import logging

from .registry import Capability

logger = logging.getLogger("friday_v6.capability.builtins")


def _intent_capabilities() -> list[Capability]:
    """One capability per NL intent (the ONE NLU point's surface)."""
    from ..nlu import Intent
    descriptions = {
        Intent.EXECUTE: "run commands, tests, git, and file operations through the gated sandbox",
        Intent.ASK: "answer questions with evidence-cited reasoning",
        Intent.PLAN: "create and track missions from natural-language goals",
        Intent.DESKTOP: "control windows, workspaces, and apps on your desktop",
        Intent.GREETING: "greet you and introduce myself",
        Intent.HELP: "explain what I can do",
        Intent.RESEARCH: "analyze repos, correlate projects, and brief you",
        Intent.SKILL: "watch a demonstration and learn a skill from it",
        Intent.ACCEPT: "approve a suggested next step or permission ask",
        Intent.DENY: "decline a pending ask or redirect an action",
        Intent.SECURITY: "scan projects for vulnerabilities and secrets",
        Intent.MEMORY: "store and recall facts with your explicit consent",
        Intent.STYLE: "adapt how I talk (be more casual, formal, brief…)",
        Intent.IDE: "diagnose and analyze code with your IDE (LSP + static analysis)",
    }
    caps: list[Capability] = []
    for intent in Intent:
        if intent == Intent.UNKNOWN:
            continue
        caps.append(Capability(
            id=f"intent:{intent.value}",
            name=intent.value,
            description=descriptions.get(intent, f"understand the {intent.value} intent"),
            intents=(intent.value,),
            layer="intent",
            permission_level="auto",
        ))
    return caps


def _executor_capabilities() -> list[Capability]:
    """The gated execution layer's action types."""
    executors = (
        ("shell", "run a shell command in the sandbox", "confirm"),
        ("git", "run git operations (read-only auto, state-changing confirm)", "confirm"),
        ("file", "read/write/append/delete/move files with undo", "confirm"),
        ("python", "run a python snippet or script in the sandbox", "confirm"),
        ("testing", "run the test suite (pytest)", "confirm"),
        ("ssh", "run a command on a remote host", "never"),
        ("claude", "delegate a complex task to the Claude Code CLI (reads, edits, runs bash itself)", "confirm"),
    )
    return [
        Capability(
            id=f"executor:{action_type}",
            name=action_type,
            description=description,
            intents=("execute", action_type),
            layer="executor",
            permission_level=level,
        )
        for action_type, description, level in executors
    ]


def _provider_capabilities() -> list[Capability]:
    """One capability per reasoning question type (the evidence engine)."""
    from ..reasoning import QuestionType
    descriptions = {
        QuestionType.IDENTITY: "who you are and who I am",
        QuestionType.STATUS: "the state of your projects, missions, and actions",
        QuestionType.ACTIVITY: "what you did recently",
        QuestionType.CONVERSATION: "what we talked about",
        QuestionType.MISSION: "mission progress and next steps",
        QuestionType.MEMORY: "what I remember",
        QuestionType.SKILLS: "what I've learned",
        QuestionType.COLLAB: "what your team is working on",
        QuestionType.STYLE: "why I talk the way I do",
        QuestionType.CAPABILITY: "what I can do",
        QuestionType.CODE: "what's wrong with a file — IDE/LSP diagnostics",
    }
    caps: list[Capability] = []
    for qtype in QuestionType:
        if qtype == QuestionType.UNKNOWN:
            continue
        caps.append(Capability(
            id=f"provider:{qtype.value}",
            name=qtype.value,
            description=descriptions.get(qtype, f"answer {qtype.value} questions"),
            intents=("ask",),
            layer="provider",
            permission_level="auto",
            source="provider",
        ))
    return caps


def _surface_capabilities() -> list[Capability]:
    """Every entry point — the same Friday on every surface (Law 10)."""
    surfaces = (
        ("talk", "the natural-language command surface (friday6 talk / web chat)"),
        ("voice", "the voice interface (speak to Friday)"),
        ("web", "the web dashboard"),
        ("desktop", "desktop awareness and control"),
        ("security", "security scanning"),
        ("memory", "the memory layer"),
        ("skills", "the skill learning layer"),
        ("research", "research, synthesis, and briefing"),
        ("collab", "multi-instance collaboration"),
        ("ide", "the IDE integration (LSP analysis and editor control)"),
    )
    return [
        Capability(
            id=f"surface:{name}",
            name=name,
            description=description,
            intents=(name,),
            layer="surface",
            permission_level="auto",
        )
        for name, description in surfaces
    ]


#: The static built-in set (executors + providers + intents + surfaces).
BUILTIN_CAPABILITIES: tuple[Capability, ...] = (
    tuple(_executor_capabilities())
    + tuple(_provider_capabilities())
    + tuple(_intent_capabilities())
    + tuple(_surface_capabilities())
)


def register_builtins(registry) -> None:
    """Register every built-in capability into a CapabilityRegistry.

    Idempotent (the registry keys by id — re-registering replaces).
    Never raises: a missing subsystem (e.g. no reasoning layer) skips
    its group via the guard in each builder.
    """
    for cap in BUILTIN_CAPABILITIES:
        registry.register(cap)


__all__ = ["BUILTIN_CAPABILITIES", "register_builtins"]
