"""IdentityEngine — who the operator is, from what they actually said.

Wave 10 law, operator-direct: *"we don't want keywords or anything like
that."* Friday does not extract names/preferences with regex or keyword
tables. ``IdentityEngine`` is a **verbatim view over the conversation
log**: everything the operator said is stored word-for-word (with
provenance), and identity answers quote those statements back — cited,
never fabricated, never parsed.

    engine = IdentityEngine(conn)
    engine.remember("call me Lakshay")     # stored verbatim, no parsing
    engine.remember("I prefer Python")     # stored verbatim, no parsing
    profile = engine.profile()             # {"statements": [...], "tone": ...}
    data = engine.identity_answer()        # facts: verbatim quotes

Design laws (wave-10 doc §3.2, operator-amended):
- **No keywords**: nothing is extracted from speech — the exact words
  are the memory.
- **Identity is a view over the conversation log**, never a separate
  hidden store (the same exchanges the reasoning conversation provider
  reads).
- **Never fabricates**: no statements → honest "I don't know you yet";
  an empty profile is reported as empty.
"""

from __future__ import annotations

import logging
from typing import Optional

from .learn import recent_statements, record_statement

logger = logging.getLogger("friday_v4.persona.engine")


class IdentityEngine:
    """Answers identity questions by quoting the operator's own words."""

    def __init__(self, conn=None) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Recording (verbatim — no parsing)
    # ------------------------------------------------------------------

    def remember(self, text: str, surface: str = "talk") -> Optional[str]:
        """Record the operator's statement verbatim; returns an ack.

        The exact words are stored in the conversation log with
        provenance — nothing is parsed or interpreted. Returns the
        confirmation to speak back, or None when nothing could be
        recorded (never raises).
        """
        if not (text or "").strip():
            return None
        ok = record_statement(self._conn, text, surface=surface)
        if not ok:
            return None
        return ("Noted — I'll remember what you said.")

    # ------------------------------------------------------------------
    # Profile (verbatim view over the conversation log)
    # ------------------------------------------------------------------

    def profile(self) -> dict:
        """The operator's identity profile — a verbatim view over what
        they told Friday. ``{"statements": [...], "tone": ...}`` —
        empty statements when nothing has been said yet.

        Tone is NOT hardcoded (wave-10 §3.3): it adapts via the
        relationship layer's depth when that layer is available, else
        stays ``"default"``. No keywords, no extraction — the statements
        remain the operator's own words, verbatim.
        """
        tone = "default"
        try:
            from ..relationship import RelationshipEngine
            status = RelationshipEngine(self._conn).status()
            # Tone adapts via depth, but only once a relationship actually
            # exists (depth > 0). A fresh DB must stay "default" — the
            # "neutral" band at depth 0 is a meaningful signal we only
            # adopt once there is real interaction. EXCEPTION (Wave 17):
            # an explicit tone-direction ("be more casual") wins even on
            # a fresh relationship — the operator asked, so it applies
            # from the first interaction.
            if ((status.get("depth") or 0.0) > 0.0
                    or (status.get("tone_direction") or {}).get("tone")):
                tone = status.get("tone") or "default"
        except Exception:
            pass  # relationship layer unavailable → neutral default
        return {
            "statements": recent_statements(self._conn, limit=20),
            "tone": tone,
        }

    def identity_answer(self) -> dict:
        """The 'who am I' answer data, or an empty dict when unknown.

        ``facts`` are the operator's own words, verbatim, newest-first
        — ready for the reasoning ``identity_provider`` to render as
        evidence (``v4.exchanges``). Includes the relationship ``tone``
        (depth-derived, never hardcoded) so every surface can speak with
        the right register.
        """
        statements = recent_statements(self._conn, limit=8)
        facts = [
            f"you told me: \"{s['content']}\"" for s in statements
        ]
        tone = self.profile().get("tone", "default")
        return {"facts": facts, "statements": statements, "tone": tone}

    def greeting(self) -> str:
        """A greeting — neutral when the operator is unknown.

        No keyword extraction means no name slot to personalize with;
        Friday greets honestly and invites disclosure naturally.
        """
        return ("I'm Friday, your AI operating partner. I can answer "
                "questions about your projects, run tasks, track missions, "
                "and remember what matters to you. What's on your mind?")


__all__ = ["IdentityEngine"]
