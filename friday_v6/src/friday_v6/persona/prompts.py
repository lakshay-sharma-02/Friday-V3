"""Persona prompts — assemble the identity context block (Wave 10).

The natural-language block that surfaces/consumers use to speak with the
operator's identity in mind. Per the operator's direction there are **no
keywords and no extraction** — the context block quotes what the
operator actually said, verbatim, from the conversation log. Pure string
assembly over an ``IdentityEngine`` profile — no I/O, hermetic.

The wave-10 law: tone adapts via relationship depth (relationship/,
Wave 10 §3.3); until that layer exists, persona stays neutral.
"""

from __future__ import annotations

from typing import Optional


def build_persona_context(profile: Optional[dict] = None) -> str:
    """Render a prompt-ready block from an identity profile.

    Args:
        profile: ``IdentityEngine.profile()`` output. ``None`` or a
            profile with no statements yields ``""`` — never fabricates.

    Returns:
        A natural-language block quoting the operator's own words::

            What the operator has told me (verbatim):
            - "call me Lakshay" (2026-08-01T...)
            - "I prefer Python" (2026-08-01T...)
    """
    if not profile:
        return ""
    statements = profile.get("statements") or []
    tone = profile.get("tone") or "default"
    lines = []
    if tone != "default":
        # Tone adapts via relationship depth (wave-10 §3.3) — never
        # hardcoded. The block tells the consuming surface how to speak.
        lines.append(f"Speak to the operator with a {tone} tone.")
    if not statements:
        return "\n".join(lines)
    lines.append("What the operator has told me (verbatim):")
    for s in statements:
        when = (s.get("when") or "")[:16]
        suffix = f" ({when})" if when else ""
        lines.append(f"- \"{s.get('content', '')}\"{suffix}")
    return "\n".join(lines)


__all__ = ["build_persona_context"]
