"""Memory Engine — Friday's long-term memory system.

Gives Friday the ability to remember facts about the operator, their
preferences, past conversations, and anything else worth recalling.
This transforms Friday from a stateless query engine into a partner
that actually *remembers* things.

Architecture:
  - `knowledge_memory` table in the DB stores facts as key-value pairs
    with metadata (category, source, confidence, context)
  - `MemoryEngine` class provides store/recall/forget/query operations
  - LLM extraction pipeline turns conversation exchanges into memories
  - `build_memory_context()` feeds into context_prompter.py so Friday
    can recall memories in conversations and ask() responses

Categories:
  - personal:       facts about the operator (name, family, location, etc.)
  - preference:     operator preferences (likes, dislikes, preferred tools)
  - fact:           general facts learned from conversation
  - event:          past events, decisions, outcomes
  - project:        facts about the operator's projects

Flow:
  1. IdentityEngine processes a message → `memory.extract_from_conversation()`
  2. LLM analyzes the exchange → extracts structured facts
  3. Facts are stored in `knowledge_memory` table
  4. `build_memory_context()` → `context_prompter.py` → injected into LLM prompts
  5. Friday can now recall: "You mentioned your father's name is Raj last week"
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# DB table setup
# ---------------------------------------------------------------------------

_MEMORY_TABLE = """
CREATE TABLE IF NOT EXISTS knowledge_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    source      TEXT NOT NULL DEFAULT 'conversation',
    channel     TEXT,
    channel_id  TEXT,
    context     TEXT,
    confidence  REAL NOT NULL DEFAULT 1.0,
    recency_score REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memory_key ON knowledge_memory(key);
CREATE INDEX IF NOT EXISTS idx_memory_category ON knowledge_memory(category);
CREATE INDEX IF NOT EXISTS idx_memory_active ON knowledge_memory(is_active);
"""

_WORKING_MEMORY_TABLE = """
CREATE TABLE IF NOT EXISTS working_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    context_key TEXT NOT NULL,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'working',
    source      TEXT NOT NULL DEFAULT 'system',
    context     TEXT,
    priority    INTEGER NOT NULL DEFAULT 0,
    ttl_seconds INTEGER NOT NULL DEFAULT 3600,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_working_memory_key ON working_memory(context_key);
CREATE INDEX IF NOT EXISTS idx_working_memory_expires ON working_memory(expires_at);
"""


def ensure_memory_table(conn) -> None:
    """Create the knowledge_memory table if it doesn't exist.
    Also runs additive migrations for recency_score.
    """
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_memory'"
        ).fetchone()
        if not existing:
            conn.executescript(_MEMORY_TABLE)
            conn.commit()
            return
        # Additive migration: add recency_score column if missing.
        try:
            mem_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge_memory)")}
            if "recency_score" not in mem_cols:
                conn.execute("ALTER TABLE knowledge_memory ADD COLUMN recency_score REAL NOT NULL DEFAULT 1.0")
                conn.commit()
        except Exception:
            pass
    except Exception:
        pass


def ensure_working_memory_table(conn) -> None:
    """Create the working_memory table if it doesn't exist."""
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='working_memory'"
        ).fetchone()
        if existing:
            return
        conn.executescript(_WORKING_MEMORY_TABLE)
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MemoryEngine
# ---------------------------------------------------------------------------


class MemoryEngine:
    """Friday's long-term memory — stores and recalls facts.

    Usage::

        engine = MemoryEngine(conn)
        engine.store("operator_father_name", "Raj", "personal", "conversation")
        facts = engine.recall("father")
        # → [MemoryFact(key="operator_father_name", value="Raj", ...)]

        context = engine.build_memory_context()
        # → "Things I know about the operator: father's name is Raj..."
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        ensure_memory_table(conn)

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def store(
        self,
        key: str,
        value: str,
        category: str = "general",
        source: str = "conversation",
        context: str = "",
        confidence: float = 1.0,
        channel: str = "",
        channel_id: str = "",
    ) -> int:
        """Store a fact in memory. Updates existing fact if key already exists.

        Args:
            key: Unique identifier for the fact (e.g. ``"operator_father_name"``).
            value: The fact value (e.g. ``"Raj"``).
            category: One of ``personal``, ``preference``, ``fact``, ``event``, ``project``.
            source: How this was learned (``"conversation"``, ``"explicit"``, ``"inference"``).
            context: The original exchange or situation that produced this memory.
            confidence: How confident we are in this fact (0.0 to 1.0).
            channel: The channel the exchange happened on.
            channel_id: The channel-specific identifier.

        Returns:
            The row id of the stored/fact memory.
        """
        now = datetime.now(timezone.utc).isoformat()
        key = key.strip().lower()
        key = re.sub(r"[^a-z0-9_]", "_", key)

        # Check if key already exists.
        existing = self._conn.execute(
            "SELECT id FROM knowledge_memory WHERE key = ? AND is_active = 1",
            (key,),
        ).fetchone()

        if existing:
            # Update existing — merge confidence and context,
            # boost recency_score on every reaffirmation (capped at 1.0).
            old_row = self._conn.execute(
                "SELECT confidence, recency_score FROM knowledge_memory WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            merged_conf = max(old_row["confidence"] if old_row else 0, confidence)
            old_recency = old_row["recency_score"] if old_row else 0.5
            new_recency = min(1.0, old_recency + 0.15)
            self._conn.execute(
                "UPDATE knowledge_memory SET value = ?, confidence = ?, "
                "recency_score = ?, context = ?, updated_at = ?, source = ? WHERE id = ?",
                (value, merged_conf, new_recency, context[:500], now, source, existing["id"]),
            )
            self._conn.commit()
            return existing["id"]

        # New fact — starts with recency_score = 1.0 (fresh).
        cur = self._conn.execute(
            "INSERT INTO knowledge_memory "
            "(key, value, category, source, channel, channel_id, context, confidence, "
            " recency_score, created_at, updated_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, 1)",
            (key, value, category, source, channel, channel_id,
             context[:500], confidence, now, now),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def recall(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        key: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Recall active memories matching the given criteria.

        Args:
            query: Free-text query — searches key and value fields.
            category: Filter by category (``personal``, ``preference``, etc.).
            key: Exact key lookup.
            limit: Maximum results.

        Returns:
            List of memory dicts with keys: id, key, value, category,
            source, context, confidence, created_at, updated_at.
        """
        clauses: list[str] = ["is_active = 1"]
        params: list = []

        if key:
            clauses.append("key = ?")
            params.append(key.strip().lower())

        if category:
            clauses.append("category = ?")
            params.append(category)

        if query:
            clauses.append("(key LIKE ? OR value LIKE ?)")
            like = f"%{query.strip().lower()}%"
            params.append(like)
            params.append(like)

        # Order by recency-weighted score: facts that are confident AND
        # recently reaffirmed rank highest. Fresh-but-low-confidence facts
        # rank above stale-but-high-confidence ones, which is the correct
        # ordering for a partner that should remember recent interactions
        # even when confidence in those facts hasn't fully accumulated yet.
        sql = (
            "SELECT id, key, value, category, source, context, confidence, "
            "recency_score, (confidence * recency_score) AS recency_weighted_score, "
            "created_at, updated_at FROM knowledge_memory "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY recency_weighted_score DESC, updated_at DESC LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def recall_by_key(self, key: str) -> Optional[dict]:
        """Recall a specific memory by its exact key name."""
        results = self.recall(key=key, limit=1)
        return results[0] if results else None

    def forget(self, key: str) -> bool:
        """Deactivate a memory (soft delete).

        Args:
            key: The memory key to deactivate.

        Returns:
            True if a memory was deactivated, False otherwise.
        """
        key = key.strip().lower()
        cur = self._conn.execute(
            "UPDATE knowledge_memory SET is_active = 0, updated_at = ? WHERE key = ? AND is_active = 1",
            (datetime.now(timezone.utc).isoformat(), key),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def count(self, category: Optional[str] = None) -> int:
        """Count active memories, optionally filtered by category."""
        if category:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_memory WHERE is_active = 1 AND category = ?",
                (category,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_memory WHERE is_active = 1"
            ).fetchone()
        return row["cnt"] if row else 0

    def clear(self) -> int:
        """Deactivate ALL memories. Returns count of deactivated rows."""
        cur = self._conn.execute(
            "UPDATE knowledge_memory SET is_active = 0, updated_at = ? WHERE is_active = 1",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Recency decay
    # ------------------------------------------------------------------

    def decay_memories(self, days_threshold: int = 7, decay_rate: float = 0.2) -> int:
        """Decay recency_score for memories that haven't been reaffirmed recently.

        Facts older than ``days_threshold`` without an update have their
        ``recency_score`` reduced by ``decay_rate`` (minimum 0.0). This ensures
        old or unverified facts naturally sink below fresh ones in recall order.

        Args:
            days_threshold: Age in days beyond which decay is applied.
            decay_rate: Amount subtracted from recency_score per decay pass.

        Returns:
            Number of memories that were decayed.
        """
        now = datetime.now(timezone.utc)
        threshold_iso = datetime.fromtimestamp(
            now.timestamp() - days_threshold * 86400, tz=timezone.utc
        ).isoformat()
        try:
            cur = self._conn.execute(
                "UPDATE knowledge_memory SET recency_score = MAX(0.0, recency_score - ?), "
                "updated_at = ? "
                "WHERE is_active = 1 AND updated_at < ? AND recency_score > 0.0",
                (decay_rate, now.isoformat(), threshold_iso),
            )
            self._conn.commit()
            return cur.rowcount
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # LLM extraction from conversation
    # ------------------------------------------------------------------

    def extract_from_conversation(
        self,
        user_message: str,
        friday_reply: str,
        channel: str = "",
        channel_id: str = "",
    ) -> list[dict]:
        """Use the LLM to extract facts from a conversation exchange.

        Analyzes what the user said and how Friday replied, then identifies
        any facts worth remembering (personal info, preferences, decisions).

        Args:
            user_message: What the operator said.
            friday_reply: How Friday responded.
            channel: The channel the exchange happened on.
            channel_id: The channel-specific identifier.

        Returns:
            List of extracted fact dicts with keys: key, value, category,
            confidence. Empty list if no facts were found or LLM is unavailable.
        """
        try:
            from .services.llm import _call, _enabled
            if not _enabled():
                return self._extract_deterministic(user_message, channel, channel_id)
        except Exception:
            return self._extract_deterministic(user_message, channel, channel_id)

        system = (
            "You are Friday's memory extraction system. Analyze a conversation "
            "exchange and extract any facts worth remembering about the user.\n\n"
            "Return ONLY a JSON array of objects, each with:\n"
            "  - key: a short snake_case identifier (e.g. 'operator_father_name', 'user_lives_in')\n"
            "  - value: the fact value (e.g. 'Raj', 'New York')\n"
            "  - category: one of 'personal', 'preference', 'fact', 'event', 'project'\n"
            "  - confidence: float 0.0-1.0 how sure you are this is a real fact\n\n"
            "Rules:\n"
            "  - ONLY extract concrete facts the user stated or clearly implied\n"
            "  - Do NOT extract opinions, vague statements, or chitchat\n"
            "  - Do NOT extract facts from Friday's replies — only what the user said\n"
            "  - Do NOT extract data already stored as operator preferences\n"
            "  - If no facts worth remembering, return []\n"
            "  - Return valid JSON only, no markdown, no explanation"
        )

        user = (
            f"User message: {user_message[:500]}\n"
            f"Friday reply: {friday_reply[:500]}\n\n"
            "Extract any facts worth remembering:"
        )

        try:
            from .services.llm import _call_structured, _parse_json_response

            raw = _call(system, user)
            if not raw:
                return self._extract_deterministic(user_message, channel, channel_id)

            # Parse JSON from the response using the shared helper.
            facts = _parse_json_response(raw)
            if not isinstance(facts, list):
                return []

            # Validate and store.
            stored: list[dict] = []
            for f in facts:
                key = f.get("key", "").strip()
                value = f.get("value", "").strip()
                category = f.get("category", "general")
                confidence = float(f.get("confidence", 0.5))

                if not key or not value:
                    continue
                if category not in ("personal", "preference", "fact", "event", "project"):
                    category = "general"
                if confidence < 0.3:
                    continue

                self.store(
                    key=key,
                    value=value,
                    category=category,
                    source="conversation",
                    context=f"User: {user_message[:300]}",
                    confidence=confidence,
                    channel=channel,
                    channel_id=channel_id,
                )
                stored.append(f)

            return stored

        except Exception:
            return self._extract_deterministic(user_message, channel, channel_id)

    def _extract_deterministic(
        self,
        user_message: str,
        channel: str = "",
        channel_id: str = "",
    ) -> list[dict]:
        """Fallback: extract facts using regex patterns when LLM is unavailable.

        Covers common self-disclosure patterns like:
        - \"my name is X\" / \"I'm X\" / \"call me X\"
        - \"my father's name is X\" / \"my dad's name is X\"
        - \"I live in X\" / \"I'm from X\"
        - \"I work at X\" / \"I work for X\"
        - \"I like X\" / \"I love X\" / \"I prefer X\"
        - \"I use X\" / \"I have X\"
        """
        lower = user_message.lower().strip()
        facts: list[dict] = []

        # Self-introduction patterns (name).
        name_pats = [
            (r"my name is (\w+)", "operator_name"),
            (r"my name's (\w+)", "operator_name"),
            (r"call me (\w+)", "operator_name"),
            (r"i'm (\w+)", "operator_name"),
            (r"i am (\w+)", "operator_name"),
            (r"name's (\w+)", "operator_name"),
        ]
        for pat, key in name_pats:
            m = re.search(pat, lower)
            if m:
                name = m.group(1).strip().capitalize()
                if len(name) >= 2 and name.isalpha():
                    facts.append({
                        "key": key,
                        "value": name,
                        "category": "personal",
                        "confidence": 0.9,
                    })
                    break

        # Family patterns.
        family_pats = [
            (r"my father's name is (\w+)", "operator_father_name"),
            (r"my dad's name is (\w+)", "operator_father_name"),
            (r"my mother's name is (\w+)", "operator_mother_name"),
            (r"my mom's name is (\w+)", "operator_mother_name"),
        ]
        for pat, key in family_pats:
            m = re.search(pat, lower)
            if m:
                name = m.group(1).strip().capitalize()
                if len(name) >= 2 and name.isalpha():
                    facts.append({
                        "key": key,
                        "value": name,
                        "category": "personal",
                        "confidence": 0.85,
                    })

        # Location patterns.
        loc_pats = [
            (r"i live in (\w+)", "operator_location"),
            (r"i'm from (\w+)", "operator_location"),
            (r"i stay in (\w+)", "operator_location"),
        ]
        for pat, key in loc_pats:
            m = re.search(pat, lower)
            if m:
                loc = m.group(1).strip().capitalize()
                facts.append({
                    "key": key,
                    "value": loc,
                    "category": "personal",
                    "confidence": 0.8,
                })

        # Work patterns.
        work_pats = [
            (r"i work at (\w+)", "operator_workplace"),
            (r"i work for (\w+)", "operator_workplace"),
            (r"i work as an? (\w+)", "operator_role"),
            (r"i'm an? (\w+)", "operator_role"),
        ]
        for pat, key in work_pats:
            m = re.search(pat, lower)
            if m:
                val = m.group(1).strip().capitalize()
                false_positive = ("engineer", "developer", "student", "here", "not",
                                  "trying", "just", "working")
                if val.lower() not in false_positive:
                    facts.append({
                        "key": key,
                        "value": val,
                        "category": "personal",
                        "confidence": 0.75,
                    })

        # Store extracted facts.
        for f in facts:
            self.store(
                key=f["key"],
                value=f["value"],
                category=f["category"],
                source="conversation",
                context=f"User: {user_message[:300]}",
                confidence=f["confidence"],
                channel=channel,
                channel_id=channel_id,
            )

        return facts

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def build_memory_context(self, max_facts: int = 15) -> str:
        """Build a natural-language block of remembered facts for LLM prompts.

        Returns a string like::

            Things I remember about the operator:
            - Their name is Lakshay
            - Their father's name is Raj
            - They prefer Python (learned from conversation)
            - They live in New York

        Or empty string if no memories exist.
        """
        try:
            rows = self._conn.execute(
                "SELECT key, value, category, source, confidence, "
                "(confidence * recency_score) AS recency_weighted_score, created_at "
                "FROM knowledge_memory WHERE is_active = 1 "
                "ORDER BY recency_weighted_score DESC, updated_at DESC LIMIT ?",
                (max_facts,),
            ).fetchall()
        except Exception:
            return ""

        if not rows:
            return ""

        lines: list[str] = ["Things I remember about the operator:"]
        for r in rows:
            key = r["key"].replace("_", " ").strip()
            value = r["value"]
            source = "(learned)" if r["source"] == "conversation" else ""
            category_icon = {
                "personal": "👤",
                "preference": "⭐",
                "fact": "📌",
                "event": "📅",
                "project": "📁",
            }.get(r["category"], "•")
            lines.append(
                f"  {category_icon} {key}: {value} {source}".strip()
            )

        return "\n".join(lines)

    def format_for_prompt(self, query_context: str = "") -> str:
        """Build a memory context section for injection into LLM prompts.

        Uses relevance scoring to selectively inject only memories relevant
        to the current query context. Each memory is scored by keyword overlap
        with the query, and only memories with relevance score > 0.5 are
        included. A relevance signal is included with each injected memory.

        Args:
            query_context: Optional text to find relevant memories for.
                If provided, only memories relevant to this context are included.
                The relevance score = (matching words) / (total unique words in memory).

        Returns:
            A string to inject into the LLM prompt, or empty string.
        """
        if query_context:
            # Relevance scoring: compute keyword overlap ratio.
            query_words = set(w.strip(".,!?;:'\"").lower()
                              for w in query_context.split() if len(w) > 2)
            all_memories = self.recall(limit=50)
            scored: list[tuple[float, dict]] = []
            for m in all_memories:
                mem_text = f"{m['key']} {m['value']} {m.get('category', '')}".lower()
                mem_words = set(w.strip(".,!?;:'\"").lower()
                                for w in mem_text.split() if len(w) > 2)
                if not mem_words:
                    continue
                overlap = len(query_words & mem_words)
                # Relevance = overlap / total unique in memory
                relevance = overlap / max(len(mem_words), 1)
                if relevance > 0.5:
                    scored.append((relevance, m))
                elif overlap >= 2 and len(query_words) >= 3:
                    # Even if ratio is low, 2+ keyword matches is meaningful.
                    scored.append((relevance, m))

            # Sort by relevance score descending.
            scored.sort(key=lambda x: -x[0])
            if not scored:
                return ""
            memories = [m for _, m in scored[:10]]
            relevance_map = {m['key']: round(rel, 2) for rel, m in scored[:10]}
        else:
            memories = self.recall(limit=10)
            relevance_map = {}

        if not memories:
            return ""

        lines = ["\n\n--- MEMORY (things I remember about the user) ---"]
        for m in memories:
            key_display = m["key"].replace("_", " ").strip()
            source_tag = "(learned)" if m["source"] == "conversation" else ""
            relevance_tag = ""
            if relevance_map:
                rel_score = relevance_map.get(m['key'], 0)
                relevance_tag = f" [relevance: {rel_score}]"
            lines.append(
                f"- {key_display}: {m['value']} {source_tag}{relevance_tag}".strip()
            )
        lines.append("--- END MEMORY ---")
        lines.append(
            "Instruction: The MEMORY section contains facts I've learned about the "
            "user. I may reference them naturally when relevant, but I should never "
            "fabricate specifics."
        )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# WorkingMemory — ephemeral "what am I doing right now" context
# ---------------------------------------------------------------------------


class WorkingMemory:
    """Ephemeral working memory for "what am I doing right now" context.

    Unlike ``MemoryEngine`` which stores durable facts, ``WorkingMemory``
    tracks short-term context like:
    - "I'm in the middle of refactoring the auth module"
    - "The daemon just finished its cycle at 14:32"
    - "I'm waiting for the operator's decision on deployment"

    Every entry has a TTL (default 1 hour). Expired entries are automatically
    pruned on every read/write. This is the separation between working memory
    (ephemeral, mid-task) and semantic memory (durable facts) that the
    FRIDAY architecture calls for.

    Usage::

        wm = WorkingMemory(conn)
        wm.set_context("current_task", "Refactoring auth module", priority=3)
        ctx = wm.get_current_context()
        # → "Current task: Refactoring auth module"
        wm.clear_expired()
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        ensure_working_memory_table(conn)

    #: Maximum number of active working memory entries before priority-based
    #: eviction kicks in. When exceeded, the lowest-priority entries beyond
    #: the limit are deleted (starting with expired ones, then lowest priority).
    MAX_ENTRIES = 50

    def set_context(
        self,
        context_key: str,
        value: str,
        category: str = "working",
        source: str = "system",
        context: str = "",
        priority: int = 0,
        ttl_seconds: int = 3600,
    ) -> int:
        """Set a working memory context entry. Replaces existing by key.

        Automatically evicts the lowest-priority entries when the number of
        active entries exceeds ``MAX_ENTRIES`` (50). Expired entries are
        evicted first, followed by the lowest-priority non-expired entries.

        Args:
            context_key: Unique key for this context (e.g. ``"current_task"``).
            value: The context value.
            category: Category label (``working``, ``status``, ``pending``, etc.).
            source: Who or what set this context.
            context: Optional detail string.
            priority: Higher values = more important (surfaced first).
            ttl_seconds: Seconds until this entry auto-expires.

        Returns:
            The row id of the stored context entry.
        """
        self.clear_expired()
        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + ttl_seconds,
            tz=timezone.utc,
        ).isoformat()

        context_key = context_key.strip().lower()
        context_key = re.sub(r"[^a-z0-9_]", "_", context_key)

        # Upsert: replace existing entry with same key.
        existing = self._conn.execute(
            "SELECT id FROM working_memory WHERE context_key = ?",
            (context_key,),
        ).fetchone()

        if existing:
            self._conn.execute(
                "UPDATE working_memory SET value = ?, category = ?, source = ?, "
                "context = ?, priority = ?, ttl_seconds = ?, created_at = ?, "
                "expires_at = ? WHERE id = ?",
                (value, category, source, context[:500], priority,
                 ttl_seconds, now, expires, existing["id"]),
            )
            self._conn.commit()
        else:
            cur = self._conn.execute(
                "INSERT INTO working_memory "
                "(context_key, value, category, source, context, priority, "
                " ttl_seconds, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (context_key, value, category, source, context[:500],
                 priority, ttl_seconds, now, expires),
            )
            self._conn.commit()
            existing = {"id": cur.lastrowid or 0}

        # Priority-based eviction: if over MAX_ENTRIES, remove lowest-priority
        # entries (expired first, then lowest priority) until under the limit.
        self._evict_if_needed()

        return existing["id"]

    def _evict_if_needed(self) -> int:
        """Evict lowest-priority entries if active count exceeds MAX_ENTRIES.

        First removes expired entries (safety net beyond clear_expired), then
        removes the lowest-priority non-expired entries until under the limit.

        Returns:
            Number of entries evicted.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM working_memory"
            ).fetchone()
            current_count = count_row["cnt"] if count_row else 0

            if current_count <= self.MAX_ENTRIES:
                return 0

            overage = current_count - self.MAX_ENTRIES
            evicted = 0

            # 1. Evict expired entries first (belt-and-suspenders).
            cur = self._conn.execute(
                "DELETE FROM working_memory WHERE expires_at < ?",
                (now,),
            )
            evicted += cur.rowcount
            current_count -= cur.rowcount

            # 2. If still over, evict lowest-priority non-expired entries.
            if current_count > self.MAX_ENTRIES:
                remaining_overage = current_count - self.MAX_ENTRIES
                # Delete the lowest-priority entries that haven't expired.
                cur = self._conn.execute(
                    """DELETE FROM working_memory WHERE id IN (
                        SELECT id FROM working_memory
                        WHERE expires_at >= ?
                        ORDER BY priority ASC, created_at ASC
                        LIMIT ?
                    )""",
                    (now, remaining_overage),
                )
                evicted += cur.rowcount

            if evicted:
                self._conn.commit()

            return evicted

        except Exception:
            return 0

    def get_context(self, context_key: str) -> Optional[dict]:
        """Get a specific working memory entry by key."""
        self.clear_expired()
        row = self._conn.execute(
            "SELECT id, context_key, value, category, source, context, "
            "priority, ttl_seconds, created_at, expires_at "
            "FROM working_memory WHERE context_key = ?",
            (context_key.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None

    def get_contexts_by_category(self, category: str, limit: int = 10) -> list[dict]:
        """Get working memory entries filtered by category.

        Args:
            category: Category to filter by (``working``, ``status``, ``pending``, etc.).
            limit: Maximum results.

        Returns:
            List of working memory dicts, ordered by priority DESC then created_at DESC.
        """
        self.clear_expired()
        try:
            rows = self._conn.execute(
                "SELECT context_key, value, category, source, context, priority, "
                "ttl_seconds, created_at, expires_at "
                "FROM working_memory WHERE category = ? "
                "ORDER BY priority DESC, created_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_contexts_by_source(self, source: str, limit: int = 10) -> list[dict]:
        """Get working memory entries filtered by source.

        Args:
            source: Source filter (``system``, ``daemon``, ``planner``, etc.).
            limit: Maximum results.

        Returns:
            List of working memory dicts, ordered by priority DESC then created_at DESC.
        """
        self.clear_expired()
        try:
            rows = self._conn.execute(
                "SELECT context_key, value, category, source, context, priority, "
                "ttl_seconds, created_at, expires_at "
                "FROM working_memory WHERE source = ? "
                "ORDER BY priority DESC, created_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_current_context(self, limit: int = 10,
                            min_priority: int = 0) -> str:
        """Build a natural-language summary of current working context.

        Args:
            limit: Maximum entries to include.
            min_priority: Minimum priority threshold (0 = all, 3 = high+ only).

        Returns a string like::

            Current working context:
            - Current task: Refactoring auth module (medium priority)
            - Last daemon cycle: succeeded at 14:32
            - Waiting on: operator decision for deployment

        Or empty string if no context is set.
        """
        self.clear_expired()
        try:
            if min_priority > 0:
                rows = self._conn.execute(
                    "SELECT context_key, value, category, source, context, priority "
                    "FROM working_memory WHERE priority >= ? "
                    "ORDER BY priority DESC, created_at DESC LIMIT ?",
                    (min_priority, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT context_key, value, category, source, context, priority "
                    "FROM working_memory "
                    "ORDER BY priority DESC, created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception:
            return ""

        if not rows:
            return ""

        lines = ["Current working context:"]
        for r in rows:
            key_display = r["context_key"].replace("_", " ").strip()
            priority_label = {0: "low", 1: "normal", 2: "medium",
                              3: "high", 4: "critical", 5: "blocking"}.get(
                r["priority"], f"priority={r['priority']}")
            source_tag = f"({r['source']})" if r["source"] else ""
            lines.append(
                f"  - {key_display}: {r['value']} ({priority_label}) {source_tag}".strip()
            )

        return "\n".join(lines)

    def clear_expired(self) -> int:
        """Delete all expired working memory entries.

        Returns:
            Number of expired entries deleted.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            cur = self._conn.execute(
                "DELETE FROM working_memory WHERE expires_at < ?",
                (now,),
            )
            self._conn.commit()
            return cur.rowcount
        except Exception:
            return 0

    def clear_all(self) -> int:
        """Delete ALL working memory entries (e.g. on daemon restart).

        Returns:
            Number of entries deleted.
        """
        try:
            cur = self._conn.execute("DELETE FROM working_memory")
            self._conn.commit()
            return cur.rowcount
        except Exception:
            return 0

    def count(self) -> int:
        """Count the number of active (non-expired) working memory entries."""
        self.clear_expired()
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM working_memory"
            ).fetchone()
            return row["cnt"] if row else 0
        except Exception:
            return 0

    def build_working_context(self, max_entries: int = 8) -> str:
        """Build a working-context block for LLM prompts.

        Returns a string like::

            --- WORKING CONTEXT (what I'm doing right now) ---
            Current task: Refactoring auth module
            Waiting on: Operator decision
            --- END WORKING CONTEXT ---

        Or empty string if no context is set.
        """
        ctx = self.get_current_context(limit=max_entries)
        if not ctx:
            return ""
        lines = [
            "\n\n--- WORKING CONTEXT (what I'm doing right now) ---",
            ctx,
            "--- END WORKING CONTEXT ---",
        ]
        return "\n".join(lines)
