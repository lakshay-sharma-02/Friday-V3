"""Phase B: Conversation Learner — LLM-based identity and preference extraction.

Replaces the brittle regex-based preference learning in IdentityEngine with
a proper LLM extraction step that runs as a daemon post-cycle hook.

How it works:
  1. The daemon's ``_run_cycle()`` calls ``process_conversations(conn)``
  2. We scan ``conversation_log`` for entries where ``processed = 0``
  3. We batch them into the LLM with a structured extraction prompt
  4. The LLM returns a JSON object with extracted name, preferences, and insights
  5. We persist each extracted field to ``operator_preferences`` (source='derived')
  6. We mark all processed entries as ``processed = 1``
  7. If the LLM is unavailable, we skip gracefully — entries stay unprocessed

Key design decisions:
  - **Batch processing**: Multiple exchanges are sent in one LLM call so the model
    can see conversational context and cross-reference statements.
  - **Derived source**: Extracted preferences are stored with ``source='derived'``
    so explicit ``friday profile set`` commands always take priority.
  - **Never overwrite explicit**: If an explicit preference exists for a key, the
    LLM extraction is skipped for that key (but still logged for other keys).
  - **Best-effort**: Fully isolated in try/except — a failure never breaks the
    daemon cycle.
"""

from __future__ import annotations

import json
from typing import Optional

from .db import set_operator_preference

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_EXCHANGES_PER_BATCH = 50

_SYSTEM_PROMPT = (
    "You are an identity and preference extractor for an AI operating partner "
    "called Friday. Your job is to read conversation exchanges between a user "
    "(the operator) and Friday, and extract information about the operator's "
    "identity and preferences.\n\n"
    "Extract ONLY information that is explicitly stated or very strongly implied "
    "by the user's messages. Do not guess or fabricate.\n\n"
    "Return a JSON object with any of these fields that you can confidently "
    "extract. Omit fields you're not sure about — never set them to null:\n\n"
    '{\n'
    '  "name": "the operator\'s name if mentioned (e.g. \\"Lakshay\\"), else omit",\n'
    '  "preferences": {\n'
    '    "preferred_technology": "programming language or framework (e.g. \\"Python\\", \\"Rust\\"), else omit",\n'
    '    "preferred_worker_types": "comma-separated worker types like \\"shell\\", \\"browser\\", \\"github\\", else omit",\n'
    '    "preferred_channel": "e.g. \\"telegram\\", \\"slack\\", \\"email\\", else omit",\n'
    '    "no_notifications": "true or false as a string if the user states a notification preference, else omit"\n'
    '  },\n'
    '  "insights": [\n'
    '    "any notable facts about the operator\'s work, tools, style, or habits"\n'
    '  ]\n'
    '}\n\n'
    "Rules:\n"
    "- Only include a field if you have direct evidence from the conversation\n"
    "- For name extraction: look for self-introductions (\\\"my name is X\\\", "
    "\\\"I'm X\\\", \\\"call me X\\\")\n"
    "- For preferences: look for statements like \\\"I prefer X\\\", \\\"I like Y\\\", "
    "\\\"I use Z\\\", \\\"I want email\\\"\n"
    "- preferred_worker_types should be a single comma-separated string like "
    "\\\"shell, browser\\\" or omitted entirely\n"
    "- insights should capture useful facts that don't fit specific preference keys\n"
    "- If you cannot confidently extract anything, return an empty JSON object: {}"
)

# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


def _format_exchanges(rows: list) -> str:
    """Format conversation_log rows into a readable prompt for the LLM.

    Each exchange is shown with channel, timestamp, and routing label so the
    LLM has full context for extraction.
    """
    parts: list[str] = []
    for r in rows:
        channel = r.channel
        conv_at = r.conversation_at[11:19] if len(r.conversation_at) >= 19 else r.conversation_at
        routing = f" ({r.routing})" if r.routing else ""
        parts.append(
            f"[{channel}{routing} @ {conv_at}]\n"
            f"  User:   {r.user_message}\n"
            f"  Friday: {r.friday_reply}\n"
        )
    return "\n".join(parts)


def _extract_from_llm_response(raw: str) -> dict:
    """Parse the LLM's JSON response into a flat extraction dict.

    Returns a dict like:
        {"name": "Lakshay", "preferred_technology": "Python", ...}

    Empty dict on any parse failure.
    """
    # Try to find a JSON block in the response.
    content = raw.strip()

    # Strip markdown code fences if present.
    if content.startswith("```"):
        # Find the opening and closing fences
        start = content.find("\n")
        if start != -1:
            content = content[start:].strip()
        if content.endswith("```"):
            content = content[:-3].strip()
        elif "```" in content:
            idx = content.rfind("```")
            content = content[:idx].strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Try to find a JSON object within the text.
        brace_start = content.find("{")
        brace_end = content.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                data = json.loads(content[brace_start : brace_end + 1])
            except json.JSONDecodeError:
                return {}
        else:
            return {}

    if not isinstance(data, dict):
        return {}

    result: dict = {}

    # Extract name.
    name = data.get("name")
    if isinstance(name, str) and len(name.strip()) >= 2:
        result["name"] = name.strip().capitalize()

    # Extract preferences.
    prefs = data.get("preferences")
    if isinstance(prefs, dict):
        # preferred_technology
        tech = prefs.get("preferred_technology")
        if isinstance(tech, str) and len(tech.strip()) >= 2:
            result["preferred_technology"] = tech.strip().capitalize()

        # preferred_worker_types — handles both comma-separated string
        # and JSON array (LLMs often prefer array in a JSON context).
        worker_raw = prefs.get("preferred_worker_types")
        workers: list[str] = []
        if isinstance(worker_raw, str) and worker_raw.strip():
            workers = [w.strip() for w in worker_raw.split(",") if w.strip()]
        elif isinstance(worker_raw, list):
            workers = [str(w).strip() for w in worker_raw if isinstance(w, (str,)) and str(w).strip()]
        if workers:
            normalized = []
            for w in workers:
                if not w.startswith("worker:"):
                    normalized.append(f"worker:{w.lower()}")
                else:
                    normalized.append(w.lower())
            result["preferred_worker_types"] = json.dumps(normalized)

        # preferred_channel
        channel = prefs.get("preferred_channel")
        if isinstance(channel, str) and len(channel.strip()) >= 2:
            result["preferred_channel"] = channel.strip().lower()

        # no_notifications
        no_notif = prefs.get("no_notifications")
        if isinstance(no_notif, str) and no_notif.lower() in ("true", "false"):
            result["no_notifications"] = no_notif.lower()
        elif isinstance(no_notif, bool):
            result["no_notifications"] = "true" if no_notif else "false"

    # Extract insights (stored as a JSON array in the preferences or logged).
    insights = data.get("insights")
    if isinstance(insights, list) and insights:
        # Store as a single joined string for simplicity.
        result["_extracted_insights"] = json.dumps(insights)

    return result


def _has_explicit_preference(conn, key: str) -> bool:
    """Check if an explicit preference exists (should not be overwritten).

    Uses a direct SQL query instead of fetching all rows.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM operator_preferences WHERE key = ? AND source = 'explicit' LIMIT 1",
            (key,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _persist_extraction(conn, extracted: dict) -> list[str]:
    """Persist extracted fields to operator_preferences.

    Only writes fields where:
      1. No explicit preference already exists
      2. The extracted value is non-empty

    Returns a list of keys that were persisted (for logging).
    """
    persisted: list[str] = []

    for key, value in extracted.items():
        # Skip internal keys (prefixed with _).
        if key.startswith("_"):
            continue

        # Skip if an explicit preference already exists.
        if _has_explicit_preference(conn, key):
            continue

        try:
            set_operator_preference(
                conn, key=key, value=str(value), source="derived"
            )
            persisted.append(key)
        except Exception:
            pass

    return persisted


def process_conversations(conn, dry_run: bool = False) -> dict:
    """Scan and process unprocessed conversation_log entries via the LLM.

    This is the main entry point, called from the daemon's ``_run_cycle()``.

    Args:
        conn: Database connection.
        dry_run: If True, log what would be extracted without persisting
                 or marking as processed.

    Returns:
        A dict with summary info:
            scanned: number of unprocessed entries found
            processed: number actually processed (may be 0 if LLM unavailable)
            extracted: dict of extracted fields
            persisted: list of keys that were saved
    """
    from .db import get_unprocessed_conversations, mark_conversation_processed

    result: dict = {
        "scanned": 0,
        "processed": 0,
        "extracted": {},
        "persisted": [],
    }

    # 1. Fetch unprocessed entries.
    rows = get_unprocessed_conversations(conn, limit=_MAX_EXCHANGES_PER_BATCH)
    if not rows:
        return result

    result["scanned"] = len(rows)

    # 2. Format exchanges for the LLM.
    exchanges_text = _format_exchanges(rows)

    # 3. Call the LLM with structured output.
    from .services.llm import _call_structured, _enabled

    if not _enabled():
        result["processed"] = 0
        return result

    user_prompt = (
        f"Extract operator identity and preferences from these conversation "
        f"exchanges. Return a JSON object with any extracted fields.\n\n"
        f"{exchanges_text}"
    )

    data = _call_structured(_SYSTEM_PROMPT, user_prompt)

    # 4. Extract whatever we can from the LLM response (or empty if it failed).
    extracted: dict = {}
    if data and isinstance(data, dict):
        extracted = _extract_from_llm_response(json.dumps(data))
    result["extracted"] = extracted

    if not dry_run:
        # 5. Persist extracted fields (only if no explicit preference exists).
        persisted_keys = _persist_extraction(conn, extracted)
        result["persisted"] = persisted_keys

        # 6. ALWAYS mark entries as processed — regardless of whether the
        #    LLM extraction succeeded or not. Without this, failed extractions
        #    cause the SAME entries to be re-fetched and re-processed every
        #    single daemon cycle, wasting LLM calls indefinitely.
        #
        #    Note: ``processed`` counts *attempted* entries, not necessarily
        #    *successful* ones. A zero-key extraction still marks entries so
        #    they aren't re-processed next cycle. The caller can use
        #    ``extracted`` to distinguish empty results from failures.
        for r in rows:
            try:
                mark_conversation_processed(conn, r.id)
            except Exception:
                pass
        result["processed"] = len(rows)

    return result


# ---------------------------------------------------------------------------
# Memory extraction (runs alongside preference extraction)
# ---------------------------------------------------------------------------


def extract_memories_from_conversations(conn, dry_run: bool = False) -> dict:
    """Scan unprocessed conversation_log entries and extract memories.

    This is a parallel step to ``process_conversations()``. While that function
    extracts preferences via the LLM, this one extracts general facts and
    personal information using the ``MemoryEngine`` (which also uses the LLM
    with a deterministic fallback).

    Uses the same unprocessed entries — the ``processed`` flag is shared, so
    whether this runs before or after ``process_conversations()``, each entry
    is processed exactly once.

    Args:
        conn: Database connection.
        dry_run: If True, log what would be extracted without persisting.

    Returns:
        A dict with summary info:
            scanned: number of unprocessed entries found
            processed: number actually processed
            memories_stored: number of facts stored
            memories: list of extracted fact keys
    """
    from .db import get_unprocessed_conversations

    result: dict = {
        "scanned": 0,
        "processed": 0,
        "memories_stored": 0,
        "memories": [],
    }

    # 1. Fetch unprocessed entries.
    rows = get_unprocessed_conversations(conn, limit=_MAX_EXCHANGES_PER_BATCH)
    if not rows:
        return result

    result["scanned"] = len(rows)

    # 2. Process each entry through the MemoryEngine.
    try:
        from .memory import MemoryEngine
        engine = MemoryEngine(conn)

        total_stored = 0
        all_keys: list[str] = []

        for r in rows:
            user_msg = r.user_message
            friday_reply = r.friday_reply
            channel = r.channel
            channel_id = r.channel_id

            if not user_msg:
                continue

            if dry_run:
                facts = engine._extract_deterministic(
                    user_message=user_msg,
                    channel=channel,
                    channel_id=channel_id,
                )
            else:
                facts = engine.extract_from_conversation(
                    user_message=user_msg,
                    friday_reply=friday_reply,
                    channel=channel,
                    channel_id=channel_id,
                )

            total_stored += len(facts)
            for f in facts:
                if f.get("key") not in all_keys:
                    all_keys.append(f["key"])

        result["memories_stored"] = total_stored
        result["memories"] = all_keys

        # NOTE: We do NOT mark entries as processed here. The caller
        # (``process_conversations()`` in ``daemon.py``) handles marking
        # after both preference extraction and memory extraction complete.
        # This ensures both steps run on the same entries.
        result["processed"] = 0

    except Exception:
        pass

    return result
