"""Verbatim statement memory — the Wave 10 persona "learning" (no keywords).

Wave 10 law, operator-direct: *"we don't want keywords or anything like
that."* Persona learning is **not** a keyword/regex extractor — Friday
never parses "call me X" / "I prefer Y" out of speech. Instead, every
utterance flows into the conversation log (the ``sessions``/``exchanges``
tables the reasoning conversation provider already reads), and this
module records/recalls the operator's own words **verbatim**, with
provenance (when it was said).

    record_statement(conn, "call me Lakshay", surface="talk")  # verbatim
    recent_statements(conn, limit=5)   # → [{"content": "...", "when": ...}]

The identity engine answers "who am I" by quoting these statements back
— evidence-cited, never fabricated. No parsing, no extraction rules,
no keyword tables.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("friday_v6.persona.learn")


def record_statement(conn, text: str, surface: str = "talk") -> bool:
    """Log one operator statement verbatim into the conversation log.

    Uses the same ``sessions``/``exchanges`` tables the reasoning
    conversation provider reads — so "what did we talk about" and
    "who am I" both answer from the same ground truth. Wave 15: the
    statement joins the **shared session** (the single presence every
    surface appends to), so statements recorded from any surface are
    part of one conversation. (``surface`` is kept for call-site
    compatibility — the shared session is surface-independent.)
    Guarded: any failure returns False (never raises).
    """
    if conn is None:
        return False
    text = (text or "").strip()
    if not text:
        return False
    try:
        from friday_v6 import db
        sid = db.get_or_create_shared_session(conn)
        if not sid:
            return False
        eid = db.log_exchange(conn, sid, "user", text)
        return bool(eid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"record_statement failed: {exc}")
        return False


def recent_statements(conn, limit: int = 20) -> list[dict]:
    """The operator's most recent statements, verbatim, newest-first.

    Each entry: ``{"content": ..., "when": <iso>, "intent": ...}``.
    Empty on any failure (never raises).
    """
    if conn is None:
        return []
    try:
        from friday_v6 import db
        rows = db.recent_exchanges(conn, limit=limit) or []
        out = []
        for r in rows:
            if (r.get("role") or "") != "user":
                continue
            content = (r.get("content") or "").strip()
            if not content:
                continue
            out.append({
                "content": content,
                "when": r.get("created_at", ""),
                "intent": r.get("intent", ""),
            })
        return out
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"recent_statements failed: {exc}")
        return []


def statement_count(conn) -> int:
    """Number of recorded operator statements (0 on any failure)."""
    return len(recent_statements(conn, limit=100000))


__all__ = ["record_statement", "recent_statements", "statement_count"]
