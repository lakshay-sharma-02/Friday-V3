"""Anaphora Resolver — cross-query follow-through for conversational context.

Resolves pronouns, implicit subjects, action repetitions, and scope changes
across consecutive questions in the ``ask()`` pipeline. This is the missing
bridge between the deterministic prefix matcher in ``resolve_followup()`` and
full natural language anaphora resolution.

How it fits
-----------
``resolve_followup()`` in ``ask.py`` handles meta-questions (confidence/evidence/
summarize/expand), contrast prefixes ("why not X?"), compare prefixes ("compare
that to X"), and restate/clarify routing. It returns None when the question is
NOT a follow-up — a fresh question that should flow through the normal retrieval
pipeline.

This module runs AFTER ``resolve_followup()`` returns None. It detects patterns
that NEED a fresh retrieval but WITH resolved context injected:

  - **Pronoun substitution**: "tell me more about it" → "describe <prev_subject>"
  - **Same question, different subject**: "and for project X?" → "<prev_question> for X"
  - **Action repetition**: "do that again" → re-run previous ask
  - **Focus narrowing**: "specifically the auth part" → "<prev_question> specifically auth"

When deterministic patterns fail, an LLM-based fallback attempts pronoun
resolution before giving up.

Return value
------------
``resolve_anaphora(question, prev, conn)`` returns ``None`` if no anaphora
is detected (not a follow-through question). Otherwise returns a rewritten
question string that ``ask()`` can flow through the normal retrieval pipeline.
"""

from __future__ import annotations

import re
from typing import Optional

from . import vocabulary as _v
from . import query as q
from .services.llm import _call as _llm_call, _enabled as _llm_enabled

# ---------------------------------------------------------------------------
# Pattern sets (complementary to ask.py's resolve_followup patterns)
# ---------------------------------------------------------------------------

#: Pronouns that need resolution to the previous subject.
_ANAPHORA_PRONOUNS = frozenset({"it", "that", "this", "them"})

#: Action-repeat indicators — user wants the same operation repeated.
_ACTION_REPEAT = frozenset({
    "do that again", "run it again", "repeat that", "repeat it",
    "run that again", "do it again", "again", "one more time",
    "try again", "re-run", "rerun",
})

#: More-like-this — user wants similar items.
_MORE_LIKE_THIS = frozenset({
    "more like this", "similar", "like that", "more of that",
    "any others like it", "what else is similar",
    "show me more", "find more", "anything similar",
})

#: Focus-narrowing prepositions — user wants to zoom into a part.
_FOCUS_PREPOSITIONS = frozenset({
    "specifically", "particularly", "especially", "mainly",
    "focus on", "narrow to", "zoom into", "drill into",
    "specifically the", "particularly the",
})

#: Scope-change indicators — user wants the same question in a different context.
_SCOPE_CHANGES = frozenset({
    "in production", "in detail", "broadly", "overall",
    "at a high level", "in depth", "in practice",
    "from a different angle", "differently",
})

#: Words that connect a follow-up to the previous question ("and X", "also X").
_CONTINUATION_WORDS = frozenset({
    "also", "and", "plus", "what about", "how about",
})


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_anaphora(
    question: str,
    prev_question: str,
    prev_subject: Optional[str],
    prev_needs: list[str],
    conn,
) -> Optional[str]:
    """Resolve anaphoric references in **question** against the previous turn.

    Args:
        question: The current user question (potential anaphora).
        prev_question: The previous turn's question text.
        prev_subject: The subject (repo name) the previous answer was about,
            or None for workspace-wide answers.
        prev_needs: The evidence needs from the previous turn's
            RetrievalRequirements.
        conn: Database connection (for repo name lookup).

    Returns:
        A rewritten question string with anaphora resolved, or None if no
        anaphoric pattern was detected (caller should treat as a fresh
        question).
    """
    qlow = question.lower().strip()

    # 1. Action repetition — "again", "do that again", etc.
    for phrase in _ACTION_REPEAT:
        if phrase == qlow or qlow.startswith(phrase):
            # Re-run the previous question exactly.
            return prev_question

    # 2. More like this — "similar", "find more like it", etc.
    for phrase in _MORE_LIKE_THIS:
        if qlow == phrase or phrase in qlow:
            if prev_subject:
                return f"What projects are similar to {prev_subject}?"
            return "similarity"

    # 3. Focus narrowing — "specifically the X part"
    for prep in _FOCUS_PREPOSITIONS:
        if qlow.startswith(prep):
            tail = qlow[len(prep):].strip().strip(":,;.")
            if prev_subject:
                return f"What can you tell me about {tail} in {prev_subject}?"
            # No subject to anchor — rewrite as a general question about the topic.
            return f"What can you tell me about {tail}?"
        if prep in qlow and prep in ("focus on", "narrow to"):
            idx = qlow.index(prep) + len(prep)
            tail = qlow[idx:].strip().strip(":,;.")
            if prev_subject:
                return f"What can you tell me about {tail} in {prev_subject}?"
            return f"What can you tell me about {tail}?"

    # 4. Scope change — "in production", "broadly", etc.
    for scope in _SCOPE_CHANGES:
        if scope in qlow:
            # Remove the scope qualifier and apply to the previous question.
            cleaned = _remove_phrase(qlow, scope)
            if not cleaned or cleaned in _SCOPE_CHANGES:
                # Pure scope modifier — attach it to the previous question.
                non_scope = _remove_all_phrases(qlow, _SCOPE_CHANGES)
                if non_scope and non_scope not in _SCOPE_CHANGES:
                    return _inject_scope(prev_question if prev_question else non_scope, scope)
                return f"{prev_question} {scope}" if prev_question else cleaned
            return cleaned

    # 5. Continuation + pronoun — "and it?", "what about that one?", "also this?"
    #    Resolve the pronoun to the previous subject.
    for cw in _CONTINUATION_WORDS:
        if qlow.startswith(cw) or qlow == cw:
            tail = qlow[len(cw):].strip().strip(" ?.,;:")
            if not tail:
                # Pure continuation "and?" / "also?" — just re-ask previous.
                if prev_subject:
                    return f"Tell me more about {prev_subject}"
                return prev_question
            # Check if tail starts with a pronoun.
            tokens = tail.split()
            first_word = tokens[0] if tokens else ""
            if first_word in _ANAPHORA_PRONOUNS:
                # "and it?" / "what about that?"
                rest = " ".join(tokens[1:]).strip()
                if prev_subject:
                    if rest:
                        return f"What about {prev_subject} {rest}"
                    return f"Tell me more about {prev_subject}"
                return prev_question

    # 6. Pronoun as SUBJECT — "it", "that", "this" appearing as primary topic.
    #    Check if the question has a pronoun where a repo name would be.
    if _is_pronoun_based(qlow):
        resolved = _resolve_pronoun_question(qlow, prev_subject)
        if resolved:
            return resolved

    # 7. Focus question without explicit subject — "what about auth?", "how about testing?"
    #    If previous had a subject, resolve the question against it.
    if prev_subject and _is_implicit_subject_switch(qlow, prev_subject, conn):
        # The question mentions a DIFFERENT repo than the previous subject.
        # Same question type, different target.
        mentioned = _named_repo_in(qlow, conn)
        if mentioned and mentioned.lower() != prev_subject.lower():
            return _lift_question_type(prev_question, prev_subject, mentioned)

    # 8. LLM fallback — only if the question looks like it might contain
    #    an unresolved pronoun that we couldn't catch above.
    if _probably_anaphoric(qlow):
        llm_resolved = _llm_resolve_pronoun(question, prev_question, prev_subject)
        if llm_resolved:
            return llm_resolved

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_pronoun_based(qlow: str) -> bool:
    """Check if the question starts with or is dominated by a pronoun."""
    tokens = qlow.strip(" ?.,;:!").split()
    if not tokens:
        return False

    # "it", "that", "this" as the first word (the subject of the sentence)
    first = tokens[0]
    if first in _ANAPHORA_PRONOUNS:
        return True

    # "tell me about it", "describe it", "show me that" — pronoun near the end
    # as the object.
    if len(tokens) >= 2 and tokens[-1] in _ANAPHORA_PRONOUNS:
        return True

    # "what about it?", "how about that?"
    if len(tokens) >= 2 and tokens[-1] in _ANAPHORA_PRONOUNS and tokens[0] in ("what", "how"):
        return True

    return False


def _resolve_pronoun_question(qlow: str, prev_subject: Optional[str]) -> Optional[str]:
    """Replace pronouns in a question with the previous subject."""
    if not prev_subject:
        return None

    tokens = qlow.strip(" ?.,;:!").split()
    if not tokens:
        return None

    # Build a resolved version by replacing pronouns.
    resolved_tokens = []
    replaced = False
    for tok in tokens:
        if tok in _ANAPHORA_PRONOUNS:
            resolved_tokens.append(prev_subject)
            replaced = True
        else:
            resolved_tokens.append(tok)

    if not replaced:
        return None

    result = " ".join(resolved_tokens)
    # Add the question mark back if original had one.
    if qlow.rstrip().endswith("?"):
        result += "?"
    return result.capitalize()


def _is_implicit_subject_switch(qlow: str, prev_subject: str, conn) -> bool:
    """Check if the question names a DIFFERENT repo than the previous subject,
    without explicitly asking about the previous one. This covers:
    "what about project-x?", "and project-y?", "how about zed?"
    """
    mentioned = _named_repo_in(qlow, conn)
    if not mentioned:
        return False
    if mentioned.lower() == prev_subject.lower():
        return False
    # The question doesn't mention the previous subject at all — it's a switch.
    return prev_subject.lower() not in qlow


def _lift_question_type(prev_question: str, prev_subject: str, new_subject: str) -> str:
    """Reapply the previous question type to a new subject.

    E.g. "Describe foo" followed by "describe bar" → "Describe bar".
    Handles rewriting the question to swap the subject.
    """
    prev_lower = prev_question.lower()
    prev_subj_lower = prev_subject.lower()

    # If the previous question literally contained the subject, swap it.
    if prev_subj_lower in prev_lower:
        # Case-insensitive replacement (but preserve case of the question).
        # Build by replacing the FIRST occurrence of the subject.
        idx = prev_lower.index(prev_subj_lower)
        return prev_question[:idx] + new_subject + prev_question[idx + len(prev_subject):]

    # No direct subject mention — just ask the same type about the new subject.
    # Prepend the previous question type.
    return f"{prev_question} — especially for {new_subject}"


def _named_repo_in(text: str, conn) -> Optional[str]:
    """Return the first repository name found in **text**, or None."""
    for r in q.all_repositories(conn):
        if r.name.lower() in text.lower():
            return r.name
    return None


def _remove_phrase(text: str, phrase: str) -> str:
    """Remove **phrase** from **text**, cleaning up extra whitespace."""
    result = text.replace(phrase, "")
    return " ".join(result.split())


def _remove_all_phrases(text: str, phrases: frozenset) -> str:
    """Remove all phrases from **text** in one pass."""
    result = text
    for p in phrases:
        result = result.replace(p, "")
    return " ".join(result.split())


def _inject_scope(question: str, scope_phrase: str) -> str:
    """Inject a scope qualifier into a question naturally."""
    # Simple: append the scope.
    return f"{question.rstrip(' ?')} {scope_phrase}"


def _probably_anaphoric(qlow: str) -> bool:
    """Heuristic: does the question LOOK like it might have an unresolved
    anaphoric reference? We check for pronoun presence (the most reliable
    indicator) and avoid short-question heuristics that trigger false
    positives for legitimate standalone questions like "why?" or "how?"."""
    for p in _ANAPHORA_PRONOUNS:
        if p in qlow.split():
            return True
    return False


def _llm_resolve_pronoun(
    question: str,
    prev_question: str,
    prev_subject: Optional[str],
) -> Optional[str]:
    """Use the LLM to resolve pronouns when deterministic patterns fail.

    This is a last resort — only fires when no deterministic pattern matched
    AND the question looks anaphoric. The LLM is asked to rewrite the question
    with resolved references, or return EMPTY if it's not anaphoric.
    """
    try:
        if not _llm_enabled():
            return None

        system = (
            "You rewrite follow-up questions to resolve pronouns and implicit references. "
            "Given the previous question and its subject, rewrite the new question so it "
            "is self-contained and has NO pronouns or references to the previous turn. "
            "If the new question is NOT a follow-up (has no pronouns, no implicit references), "
            "return EMPTY.\n\n"
            "Rules:\n"
            "- Replace 'it', 'this', 'that', 'them' with the previous subject when applicable.\n"
            "- Answer ONLY the rewritten question text, or EMPTY if not a follow-up.\n"
            "- Do NOT include any explanation, prefix, or surrounding text.\n"
            "- Keep the rewritten question natural — capitalize properly, add question marks.\n"
            "- If the question is ambiguous (e.g. 'it' could refer to multiple things), "
            "use the previous subject as the resolution."
        )

        subject_line = f"Previous subject: {prev_subject}" if prev_subject else "Previous subject: (none — workspace question)"
        user = (
            f"Previous question: {prev_question}\n"
            f"{subject_line}\n"
            f"Follow-up question: {question}\n\n"
            f"Rewritten question (or EMPTY):"
        )

        result = _llm_call(system, user, timeout=10)
        if result is None:
            return None

        result = result.strip()
        if result.upper() == "EMPTY" or not result or len(result) < 5:
            return None

        return result
    except Exception:
        return None
