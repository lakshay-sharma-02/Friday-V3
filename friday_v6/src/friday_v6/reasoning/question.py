"""Question classification — the first pass of the reasoning layer.

Deterministic keyword rules map an ASK utterance to a
:class:`QuestionType` so the provider registry knows which evidence to
gather. Mirrors the ``understanding/`` philosophy: rules first,
never guess — anything unmatched is :attr:`QuestionType.UNKNOWN`.

Hermetic: pure string logic, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class QuestionType(str, Enum):
    """The kind of question Friday is being asked."""

    IDENTITY = "identity"    # who/what Friday is — self-knowledge
    STATUS = "status"        # what's the state of projects / things
    ACTIVITY = "activity"    # what happened / what did I do recently
    CONVERSATION = "conversation"  # what did we talk about
    MISSION = "mission"      # progress on a goal / mission
    MEMORY = "memory"        # what does Friday remember / know
    SKILLS = "skills"        # what has Friday learned / what skills
    COLLAB = "collab"        # what's the team/peers working on
    STYLE = "style"          # why does Friday talk the way she does
    CAPABILITY = "capability"  # what can Friday do (the capability registry)
    CODE = "code"            # what's wrong with a file — IDE diagnostics (Wave 6)
    UNKNOWN = "unknown"      # no rule matched — ask or say don't know


#: Ordered rules: (question_type, trigger words/phrases). First match
#: wins. Trigger matching is word-boundary; phrases match as substrings.
_RULES: tuple[tuple[QuestionType, tuple[str, ...]], ...] = (
    # CAPABILITY + STYLE go BEFORE IDENTITY: "what are your capabilities"
    # contains identity's "what are you" phrase as a substring, so the
    # specific capability question must win the ordering (Wave 16/17).
    (QuestionType.CAPABILITY, (
        "what can you do", "what are you capable", "what are your "
        "capabilities", "capabilities", "what can you help with",
        "what do you do", "what are you able", "list your abilities",
        "what can you handle", "how can you help",
    )),
    (QuestionType.STYLE, (
        "why do you talk", "why do you speak", "why are you so",
        "why are you being", "why so formal", "why so casual",
        "why so friendly", "why do you sound", "why are you like this",
        "your tone", "why that tone", "why do you act",
    )),
    (QuestionType.IDENTITY, (
        "who are you", "what are you", "who made you", "who built you",
        "what's your name", "what is your name", "are you friday",
        "introduce yourself",
        # Operator identity — "who am I" style questions answered from
        # stored facts by the persona-aware identity_provider.
        "who am i", "what do you know about me", "tell me about myself",
        "what do you remember about me", "do you know my name",
        "what's my name", "what is my name", "remember my name",
        "tell me about yourself",
    )),
    (QuestionType.STATUS, (
        "status", "state of", "how are things", "how's everything",
        "how is everything", "what's going on", "what is going on",
        "what's happening", "what is happening", "overview",
    )),
    # CONVERSATION is checked BEFORE ACTIVITY so time-windowed recall
    # stays a conversation question: "what did we talk about yesterday"
    # contains ACTIVITY's bare "yesterday" trigger but asks about the
    # *conversation* — Wave 15 (one presence). "what did we do
    # yesterday" has no conversation trigger and still classifies
    # ACTIVITY.
    (QuestionType.CONVERSATION, (
        "what did we talk about", "what have we talked about",
        "what did we discuss", "what have we discussed",
        "what did we go over", "what have we been talking about",
        "recap our conversation", "recap our chat", "what did i ask",
    )),
    (QuestionType.ACTIVITY, (
        "what did i do", "what did we do", "what happened",
        "what changed", "what has changed", "recent activity",
        "what have you been doing", "what've you been doing",
        "what did you do", "yesterday", "last week", "today",
    )),
    (QuestionType.MISSION, (
        "mission", "progress", "how is it going", "how are we doing",
        "how's it going", "what's next", "what is next", "next step",
        "steps", "goal", "going",
    )),
    (QuestionType.MEMORY, (
        "what do you know", "what do you remember", "do you remember",
        "remember", "tell me about", "what do you have on",
    )),
    (QuestionType.SKILLS, (
        "what did you learn", "what have you learned", "what've you learned",
        "what did i teach you", "what skills", "skills do you have",
        "show me your skills", "list your skills", "what can you do "
        "automatically", "learned any skills", "what workflows do you know",
    )),
    (QuestionType.COLLAB, (
        "team", "peer", "colleague", "collaborat", "workspace members",
        "what are others", "what is everyone", "anyone else", "others doing",
        "what's my team", "what is my team", "team working on",
        "peers working on", "someone else", "other instance",
    )),
    # CODE (Wave 6) — an ASK backstop for "what's wrong with X" phrased
    # as a question (the NL layer's IDE intent normally catches these;
    # the LLM can still classify them as ask). Answered from the real
    # IDE/static analyzer, never a guess.
    (QuestionType.CODE, (
        "what's wrong with", "what is wrong with", "why won't this compile",
        "why won't it compile", "syntax error", "compile error",
        "errors in", "error in", "issues in", "problems in",
        "is my code clean", "is this code clean", "check my code",
        "why does this file", "why is this file", "what are the errors",
        "what is the error", "lint", "does this compile",
    )),
)


def _hit(text: str, word: str) -> bool:
    return bool(re.search(rf"\b{re.escape(word)}\b", text))


def classify(text: str) -> QuestionType:
    """Classify an ASK utterance into a :class:`QuestionType`.

    Ordered rules; the first match wins. "going" is a MISSION signal
    ("how is the auth refactor going?"), but STATUS is checked first
    so "how are things going" stays a status question.
    """
    lower = (text or "").strip().lower()
    for qtype, triggers in _RULES:
        for trigger in triggers:
            if " " in trigger:
                if trigger in lower:
                    return qtype
            elif _hit(lower, trigger):
                return qtype
    return QuestionType.UNKNOWN


@dataclass
class Question:
    """One parsed question: its type + a best-effort target entity."""

    text: str
    type: QuestionType
    target: Optional[str] = None  # e.g. a repo/project name when mentioned


def parse(text: str) -> Question:
    """Parse an ASK utterance: type + best-effort target (never raises)."""
    qtype = classify(text)
    target = _extract_target(text)
    return Question(text=(text or "").strip(), type=qtype, target=target)


#: Targets that are just filler — never a real entity. "how's it
#: going" extracts "it" after the "how's" marker; filtering it keeps
#: the question target-less so providers fall back to the active/latest
#: subject instead of matching nothing (Wave 19 shepherding fix).
_SKIP_TARGETS = frozenset({
    "it", "this", "that", "there", "things", "things going", "you",
    "your", "yours", "my", "mine", "we", "us", "our", "everyone",
    "everything", "them", "those", "these", "the", "a", "an", "now",
})


def _extract_target(text: str) -> Optional[str]:
    """Best-effort entity after prepositions ('status of X' → X).

    Trailing question verbs are stripped so "how's the auth refactor
    going" yields the target "the auth refactor", not "the auth
    refactor going"; a remaining filler target ("it" from "how's it
    going") is dropped so the question stays target-less rather than
    filtering everything out.
    """
    lower = text.lower()
    for marker in ("status of", "state of", "what's up with", "what is up with",
                   "what's wrong with", "what is wrong with", "what about",
                   "how is", "how's", "tell me about"):
        idx = lower.find(marker)
        if idx >= 0:
            rest = text[idx + len(marker):].strip(" :'\".,!?")
            for verb in (" going", " doing", " today", " now"):
                if rest.lower().endswith(verb):
                    rest = rest[: -len(verb)].strip()
            if rest and rest.lower() not in _SKIP_TARGETS:
                return rest
    return None


__all__ = ["Question", "QuestionType", "classify", "parse"]
