"""IdentityEngine — persistent Friday persona for conversational interaction.

Gives Friday a name, personality, and bidirectional conversational ability
across all channels (Telegram, Slack, Discord, CLI). The engine:

1. Routes incoming messages through Friday's existing pipelines:
   - ``ask()`` for questions about your workspace
   - ``execute()`` for action commands
   - Direct persona response for chitchat/greetings
2. Maintains per-channel conversation context (last N exchanges)
3. Strikes a balance between helpful AI partner and humble assistant
4. Learns operator identity from conversation (name, preferences) and
   persists them to the operator_profile via ``operator_preferences`` table.
   This unification means chitchat on any channel trains the operator
   profile, which is then visible to ``friday ask``, the dashboard, etc.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from ..db import connect

# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

#: Max exchanges to remember per channel (ring buffer).
_MAX_CONTEXT = 6

#: System prompt fragment for persona-aware responses.
_PERSONA_PROMPT = (
    "You are Friday, an AI operating partner. You are knowledgeable, direct, "
    "and occasionally witty. You help with software engineering work — answering "
    "questions about codebases, executing tasks, and surfacing insights. "
    "You talk like a competent peer, not a servant. Keep responses concise "
    "but thorough when needed. You have access to the user's entire workspace "
    "knowledge and can answer questions about their projects, architecture, "
    "technologies, and engineering patterns."
)

#: Words that indicate chitchat — reply without calling the LLM.
_CHITCHAT = frozenset({
    "hello", "hi", "hey", "sup", "yo", "good morning", "good evening",
    "how are you", "how's it going", "what's up", "whassup",
    "nice", "cool", "thanks", "thank you", "thx", "ty",
    "goodbye", "bye", "see you", "later", "cya",
    "who are you", "what are you", "introduce yourself",
    "good bot", "bad bot", "you're awesome", "you rock",
    # Common elongated/fat-finger variants
    "hii", "hiii", "heyy", "heyyy", "helloo", "hellooo",
    "thanx", "thnks", "thnx",
})


@dataclass
class IdentityConfig:
    """Configuration for Friday's identity persona."""

    name: str = "Friday"
    greeting: str = (
        "I'm Friday, your AI operating partner. I can answer questions about "
        "your projects, execute tasks, and help you stay on top of your "
        "engineering work. What's on your mind?"
    )


@dataclass
class ConversationContext:
    """Holds the last N exchanges for one channel conversation."""

    exchanges: list[tuple[str, str]] = field(default_factory=list)  # (user_msg, friday_reply)

    def add(self, user: str, friday: str) -> None:
        self.exchanges.append((user, friday))
        if len(self.exchanges) > _MAX_CONTEXT:
            self.exchanges = self.exchanges[-_MAX_CONTEXT:]

    def format(self) -> str:
        """Format recent exchanges as context for the next LLM call."""
        if not self.exchanges:
            return ""
        lines = ["Recent conversation context (most recent last):"]
        for i, (user, friday) in enumerate(self.exchanges[-4:], 1):
            lines.append(f"  [{i}] User: {user[:120]}")
            lines.append(f"      Friday: {friday[:120]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# IdentityEngine
# ---------------------------------------------------------------------------


class IdentityEngine:
    """Core identity engine — routes messages, maintains context, returns responses.

    Usage::
        engine = IdentityEngine()
        reply = engine.process("what's the architecture of project X?", "telegram:12345")
        # → routes to ask(), returns evidence-backed answer

        reply = engine.process("deploy the staging server", "telegram:12345")
        # → routes to execute, returns execution result
    """

    def __init__(self, config: Optional[IdentityConfig] = None,
                 conn=None) -> None:
        self.config = config or IdentityConfig()
        self._conn = conn  # Optional DB connection for operator profile persistence.
        # Per-channel conversation contexts (key = channel_id, e.g. "telegram:8656925628")
        self.contexts: dict[str, ConversationContext] = {}
        # Cache the operator's name for efficient response personalization.
        self._operator_name: Optional[str] = None
        self._load_operator_name()
        # Cache MemoryEngine to avoid redundant table-exists checks on every message.
        self._memory_engine: Optional[object] = None
        self._init_memory_engine()

    def _load_operator_name(self) -> None:
        """Load the operator's name from the operator profile, if available."""
        if self._conn is None:
            return
        try:
            from ..operator import build_operator_profile
            profile = build_operator_profile(self._conn)
            raw = profile.explicit_preferences.get("name", "")
            if raw:
                self._operator_name = raw
        except Exception:
            pass

    def _set_preference(self, key: str, value: str) -> None:
        """Persist an operator preference via the DB connection, if available."""
        if self._conn is None:
            return
        try:
            from ..db import set_operator_preference
            set_operator_preference(self._conn, key=key, value=value, source="explicit")
        except Exception:
            pass

    def _log_exchange(self, channel: str, channel_id: str,
                       user_message: str, friday_reply: str,
                       routing: str = "") -> None:
        """Append one exchange to the conversation_log table.

        This is the foundation of Phase A — every exchange across all channels
        is persisted so the daemon's downstream LLM extraction (Phase B) can
        learn operator identity and preferences without brittle regex patterns.

        Best-effort: silently fails if the DB connection is not available.
        """
        if self._conn is None:
            return
        try:
            from ..db import log_exchange
            log_exchange(
                self._conn,
                channel=channel,
                channel_id=channel_id,
                user_message=user_message,
                friday_reply=friday_reply,
                routing=routing,
            )
        except Exception:
            pass

    def get_context(self, channel_id: str) -> ConversationContext:
        """Get or create a conversation context for a channel."""
        if channel_id not in self.contexts:
            self.contexts[channel_id] = ConversationContext()
        return self.contexts[channel_id]

    # ------------------------------------------------------------------
    # Name extraction (trains the operator profile)
    # ------------------------------------------------------------------

    _NAME_PATTERNS = [
        re.compile(r"my name is (\w+)", re.IGNORECASE),
        re.compile(r"call me (\w+)", re.IGNORECASE),
        re.compile(r"i'm (\w+)", re.IGNORECASE),
        re.compile(r"i am (\w+)", re.IGNORECASE),
        re.compile(r"you can call me (\w+)", re.IGNORECASE),
        re.compile(r"my name's (\w+)", re.IGNORECASE),
        re.compile(r"name's (\w+)", re.IGNORECASE),
    ]

    _NAME_FALSE_POSITIVES = frozenset({
        "done", "back", "here", "new", "just", "working", "trying",
        "going", "coming", "looking", "starting", "beginning",
        "done", "ready", "set", "good", "fine", "okay", "ok",
        "tired", "busy", "free", "hungry", "bored",
    })

    def _extract_name(self, text: str) -> Optional[str]:
        """Extract a mentioned name from self-introduction patterns.

        Returns the extracted name (capitalized) or None if no pattern matches.
        Also filters out common words that could be false positives.
        """
        for pat in self._NAME_PATTERNS:
            m = pat.search(text)
            if m:
                name = m.group(1).strip().capitalize()
                if name.lower() in self._NAME_FALSE_POSITIVES:
                    continue
                if len(name) >= 2 and name.isalpha():
                    return name
        return None

    # ------------------------------------------------------------------
    # Preference extraction (trains the operator profile)
    # ------------------------------------------------------------------

    # Regex matches "I prefer <topic>", "I like <topic>", "I want <topic>", "I use <topic>"
    _PREFERENCE_REGEX = re.compile(
        r"\bi (?:prefer|like|love|enjoy|favor|want|use) (\w+)", re.IGNORECASE
    )

    # Subject word → (preference_key, formatted_value, confirmation_template)
    # ``lambda v: v`` uses the matched word as-is; others use the canonical value.
    _PREFERENCE_TOPICS: dict[str, tuple[str, str, str]] = {
        # Technologies
        "python":  ("preferred_technology", "Python",        "Got it — I'll remember you prefer {value}."),
        "rust":    ("preferred_technology", "Rust",          "Got it — I'll remember you prefer {value}."),
        "go":      ("preferred_technology", "Go",            "Got it — I'll remember you prefer {value}."),
        "golang":  ("preferred_technology", "Go",            "Got it — I'll remember you prefer {value}."),
        "typescript": ("preferred_technology", "TypeScript", "Got it — I'll remember you prefer {value}."),
        "javascript": ("preferred_technology", "JavaScript", "Got it — I'll remember you prefer {value}."),
        "react":   ("preferred_technology", "React",         "Got it — I'll remember you prefer {value}."),
        "vue":     ("preferred_technology", "Vue",           "Got it — I'll remember you prefer {value}."),
        "svelte":  ("preferred_technology", "Svelte",        "Got it — I'll remember you prefer {value}."),
        "vue.js":  ("preferred_technology", "Vue",           "Got it — I'll remember you prefer {value}."),
        "cpp":     ("preferred_technology", "C++",           "Got it — I'll remember you prefer {value}."),
        "c++":     ("preferred_technology", "C++",           "Got it — I'll remember you prefer {value}."),
        "csharp":  ("preferred_technology", "C#",            "Got it — I'll remember you prefer {value}."),

        # Worker types
        "shell":   ("preferred_worker_types", '["worker:shell"]',
                    "Noted — I'll use shell workers when possible."),
        "bash":    ("preferred_worker_types", '["worker:shell"]',
                    "Noted — I'll use shell workers when possible."),
        "zsh":     ("preferred_worker_types", '["worker:shell"]',
                    "Noted — I'll use shell workers when possible."),
        "browser": ("preferred_worker_types", '["worker:browser"]',
                    "Noted — I'll use browser workers when possible."),
        "cli":     ("preferred_worker_types", '["worker:shell"]',
                    "Noted — I'll use CLI workers when possible."),
        "git":     ("preferred_worker_types", '["worker:github"]',
                    "Noted — I'll use git workers when possible."),
        "github":  ("preferred_worker_types", '["worker:github"]',
                    "Noted — I'll use git workers when possible."),
        "hyprland":("preferred_worker_types", '["worker:hyprctl"]',
                    "Noted — I'll use Hyprland workers when possible."),

        # Communication channels
        "email":   ("preferred_channel", "email",
                    "Got it — I'll use email for important updates."),
        "telegram":("preferred_channel", "telegram",
                    "Got it — I'll use Telegram for updates."),
        "slack":   ("preferred_channel", "slack",
                    "Got it — I'll use Slack for updates."),
        "discord": ("preferred_channel", "discord",
                    "Got it — I'll use Discord for updates."),

        # Notification preferences
        "notifications": ("no_notifications", "false",
                          "I'll keep notifications on for you."),
        "alerts":        ("no_notifications", "false",
                          "I'll keep notifications on for you."),
    }

    _PREFERENCE_FALSE_POSITIVES = frozenset({
        "this", "that", "these", "those", "it", "them", "its",
        "working", "using", "doing", "making", "building",
        "talking", "seeing", "hearing", "going", "coming",
        "the", "a", "an", "my", "your", "our", "their",
        "new", "old", "big", "small", "fast", "slow",
        "very", "really", "quite", "just", "always", "never",
        "things", "stuff", "projects", "code", "work",
        "more", "less", "some", "any", "all", "each",
        "with", "without", "from", "over", "under",
    })

    def _extract_preference(self, text: str) -> Optional[tuple[str, str, str]]:
        """Extract an operator preference from statements like ``I prefer Python``.

        Returns ``(preference_key, value, confirmation_template)`` when a known
        preference subject is detected, or ``None`` if no match.
        """
        m = self._PREFERENCE_REGEX.search(text)
        if not m:
            return None

        subject = m.group(1).lower().strip()

        # Filter common non-preference words.
        if subject in self._PREFERENCE_FALSE_POSITIVES:
            return None
        if len(subject) < 2:
            return None

        # Look up in known preference topics.
        entry = self._PREFERENCE_TOPICS.get(subject)
        if entry:
            key, value, template = entry
            return (key, value, template)

        return None

    def _learn_operator_info(self, text: str) -> Optional[str]:
        """Check if the message contains operator-info we can learn from.

        Detects in order:
        1. Self-introduction ("my name is X") — persists name
        2. Preference statements ("I prefer Python") — persists preference

        Returns a response string if something was learned, or None if nothing
        was detected.
        """
        # 1. Name extraction
        name = self._extract_name(text)
        if name and name != self._operator_name:
            self._operator_name = name
            self._set_preference("name", name)
            return name

        # 2. Preference extraction
        pref = self._extract_preference(text)
        if pref:
            key, value, template = pref
            self._set_preference(key, value)
            return template.format(value=value)

        return None

    def _init_memory_engine(self) -> None:
        """Initialize the cached MemoryEngine, if a DB connection is available."""
        if self._conn is None:
            return
        try:
            from ..memory import MemoryEngine
            self._memory_engine = MemoryEngine(self._conn)
        except Exception:
            self._memory_engine = None

    def _extract_memories(self, text: str, reply: str, channel_id: str) -> None:
        """Extract and store memories from a conversation exchange.

        Uses the cached MemoryEngine (or LLM with deterministic fallback)
        to identify personal facts, preferences, and other information worth
        remembering from what the operator said. Best-effort — never raises.
        """
        if self._memory_engine is None:
            return
        try:
            channel = channel_id.split(":")[0] if ":" in channel_id else channel_id
            self._memory_engine.extract_from_conversation(
                user_message=text,
                friday_reply=reply,
                channel=channel,
                channel_id=channel_id,
            )
        except Exception:
            pass

    def _build_persona_context(self) -> str:
        """Build the LEARNED CONTEXT section with operator info and memories.

        Includes relationship depth and tone modulation when DB is available.
        Returns a string to inject into the LLM prompt, or empty string.
        """
        parts: list[str] = []

        # Operator name
        if self._operator_name:
            parts.append(f"The person you're talking to is named {self._operator_name}.")

        # Relationship depth + tone modulation (Relationship & Personalization)
        if self._conn is not None:
            try:
                from ..operator.depth import compute_relationship_depth, get_tone_params
                depth = compute_relationship_depth(self._conn)
                if depth.level > 0:
                    parts.append(
                        f"Your relationship with them: Level {depth.level} "
                        f"— {depth.label}. {depth.description}"
                    )
                    tone = get_tone_params(depth.level)
                    tone_fragment = tone.to_prompt_fragment()
                    if tone_fragment:
                        parts.append(f"Tone: {tone_fragment}")
            except Exception:
                pass

        # Preferences from operator_preferences
        if self._conn is not None:
            try:
                rows = self._conn.execute(
                    "SELECT key, value, source FROM operator_preferences "
                    "WHERE key != 'name' ORDER BY key LIMIT 10"
                ).fetchall()
                if rows:
                    pref_strs = []
                    for r in rows:
                        marker = " (learned)" if r["source"] == "derived" else ""
                        pref_strs.append(f"{r['key']}={r['value']}{marker}")
                    parts.append(f"Their preferences: {'; '.join(pref_strs)}")
            except Exception:
                pass

        # Memories from knowledge_memory (uses relevance scoring)
        if self._memory_engine is not None:
            try:
                ctx = self._memory_engine.build_memory_context(max_facts=8)
                if ctx:
                    parts.append(f"Things I remember about them:\n{ctx}")
            except Exception:
                pass

        # Working memory — what Friday is doing right now.
        try:
            from ..memory import WorkingMemory
            if self._conn is not None:
                wm = WorkingMemory(self._conn)
                working_ctx = wm.get_current_context(limit=6, min_priority=1)
                if working_ctx:
                    parts.append(f"\nWhat I'm doing right now:\n{working_ctx}")
        except Exception:
            pass

        if not parts:
            return ""

        return (
            "\n\n--- WHAT YOU KNOW ABOUT THIS PERSON ---\n"
            + "\n".join(parts)
            + "\n--- END WHAT YOU KNOW ---\n"
            "You MAY reference this knowledge naturally when relevant. "
            "For example: 'I remember you prefer Python' or 'As we discussed earlier.' "
            "But never fabricate — only reference what's actually shown above."
        )

    def _llm_chat(self, text: str, context: Optional[ConversationContext] = None) -> Optional[str]:
        """Send a message to the LLM with Friday's personality and context.

        This is the PRIMARY path for ALL conversation. The LLM receives:
        1. Friday's personality system prompt
        2. Learned context (operator name, preferences, memories)
        3. Recent conversation history (from context)
        4. The user's message

        Returns the LLM response, or None if LLM is unavailable (caller
        falls back to hardcoded patterns).
        """
        try:
            from ..services.llm import _call, _enabled
            if not _enabled():
                return None
        except Exception:
            return None

        try:
            from .prompts import FRIDAY_PERSONA
        except Exception:
            return None

        # Build the full system prompt.
        persona_context = self._build_persona_context()

        # Conversation history.
        history = ""
        if context is not None:
            hist = context.format()
            if hist:
                history = f"\n\nRecent conversation:\n{hist}\n"

        system = FRIDAY_PERSONA + persona_context + history

        user = f"The person says: {text}"

        try:
            reply = _call(system, user)
            if reply and len(reply.strip()) > 5:
                return reply.strip()
        except Exception:
            pass

        return None

    def process(self, text: str, channel_id: str = "cli") -> str:
        """Process an incoming message and return a response.

        PRIMARY PATH: LLM with personality (if available).
        FALLBACK: hardcoded routing through ask/execute/chitchat.

        Args:
            text: The message text from the user.
            channel_id: Unique identifier for the conversation channel
                (e.g. ``"telegram:8656925628"``, ``"slack:C12345"``, ``"cli"``).

        Returns:
            The response text to send back through the channel.
        """
        text = (text or "").strip()
        if not text:
            return ""

        # --- Pre-route: Proactive reply handling ------------------------------
        # If there's a pending proactive message awaiting operator response,
        # check if this message is a reply to it.
        try:
            from ..proactive_reply import has_pending, handle_reply
            if has_pending():
                reply = handle_reply(text, self._conn)
                if reply is not None:
                    context = self.get_context(channel_id)
                    context.add(text, reply)
                    self._log_exchange(
                        channel_id.split(":")[0], channel_id, text, reply,
                        routing="proactive_reply",
                    )
                    self._extract_memories(text, reply, channel_id)
                    return reply
        except Exception:
            pass

        context = self.get_context(channel_id)

        # --- PRIMARY PATH: LLM with personality ------------------------------
        # Try the LLM first with Friday's full personality + context.
        # This handles chitchat, identity questions, general conversation,
        # and anything else naturally. Falls through to hardcoded routing
        # if LLM is unavailable.
        llm_reply = self._llm_chat(text, context)
        if llm_reply is not None:
            context.add(text, llm_reply)
            self._log_exchange(
                channel_id.split(":")[0], channel_id, text, llm_reply,
                routing="persona",
            )
            self._extract_memories(text, llm_reply, channel_id)
            return llm_reply

        # --- FALLBACK: Hardcoded routing (LLM unavailable) ------------------
        # Learn operator info from conversation.
        learned_info = self._learn_operator_info(text)
        if learned_info:
            if self._operator_name and learned_info == self._operator_name:
                reply = self._greeting_with_name(learned_info)
            else:
                reply = learned_info
            context.add(text, reply)
            self._log_exchange(channel_id.split(":")[0], channel_id, text, reply, routing="learn")
            self._extract_memories(text, reply, channel_id)
            return reply

        # Identity questions.
        if self._is_identity_question(text):
            reply = self._personalized_greeting()
            context.add(text, reply)
            self._log_exchange(channel_id.split(":")[0], channel_id, text, reply, routing="identity")
            self._extract_memories(text, reply, channel_id)
            return reply

        # Chitchat / Greetings.
        if self._is_chitchat(text):
            reply = self._handle_chitchat(text)
            context.add(text, reply)
            self._log_exchange(channel_id.split(":")[0], channel_id, text, reply, routing="chitchat")
            self._extract_memories(text, reply, channel_id)
            return reply

        # Ambiguous — could be either a command or a question.
        if self._is_ambiguous(text):
            reply = self._handle_ambiguous(text, channel_id)
            context.add(text, reply)
            self._log_exchange(
                channel_id.split(":")[0], channel_id, text, reply,
                routing="ambiguous",
            )
            self._extract_memories(text, reply, channel_id)
            return reply

        # Action commands.
        if self._is_command(text):
            reply = self._handle_command(text, channel_id)
            context.add(text, reply)
            self._log_exchange(channel_id.split(":")[0], channel_id, text, reply, routing="command")
            self._extract_memories(text, reply, channel_id)
            return reply

        # Questions (ask pipeline).
        if self._is_question(text):
            reply = self._handle_question(text, channel_id, context)
            context.add(text, reply)
            self._log_exchange(channel_id.split(":")[0], channel_id, text, reply, routing="question")
            self._extract_memories(text, reply, channel_id)
            return reply

        # Final fallback: treat as question.
        reply = self._handle_question(text, channel_id, context)
        context.add(text, reply)
        self._log_exchange(channel_id.split(":")[0], channel_id, text, reply, routing="question_fallback")
        self._extract_memories(text, reply, channel_id)
        return reply

    def _personalized_greeting(self) -> str:
        """Return the persona greeting, personalized with operator name if known."""
        if self._operator_name:
            return (
                f"I'm Friday, your AI operating partner. "
                f"Nice to see you again, {self._operator_name}! "
                "I can answer questions about your projects, execute tasks, "
                "and help you stay on top of your engineering work. "
                "What's on your mind?"
            )
        return self.config.greeting

    def _greeting_with_name(self, name: str) -> str:
        """Return a welcome response after learning the operator's name."""
        return (
            f"Nice to meet you, {name}! I'll remember that. "
            "I'm Friday, your AI operating partner. "
            "What can I help you with?"
        )

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    # Expanded action keywords — from the Agentic Action Layer specification.
    # Includes both prefix-based and full-word action markers.
    _COMMAND_PREFIXES = (
        "deploy", "run ", "execute", "check ", "status of",
        "start ", "stop ", "restart ", "install ", "update ",
        "create ", "delete ", "fix ", "refactor ", "test ",
        "push ", "pull ", "merge ", "commit ",
        "build ", "compile ", "migrate ",
        "copy ", "paste ", "move ", "rename ",
        "open ", "close ", "launch ", "kill ",
        "send ", "fetch ", "download ", "upload ",
        "add ", "remove ", "edit ", "modify ",
        "enable ", "disable ", "set ", "unset ",
        "plan ", "graph ", "review ", "repair ",
        "observe ", "refresh ", "scan ", "mine ",
        "label ", "form ", "correlate ",
    )

    # Full-word action markers — any occurrence, not just at start.
    _ACTION_KEYWORDS = frozenset({
        "clipboard", "deploy", "execute", "refactor", "compile",
        "migrate", "install", "merge", "commit", "push", "pull",
        "kill", "reboot", "restart", "launch", "dispatch",
    })

    # Short imperative action words — used by Tier 3 of _is_command() to
    # catch brief commands like "test auth" or "deploy now" without falsely
    # catching noun phrases like "the project structure". Only matches when
    # the first word is literally one of these.
    _SHORT_ACTION_WORDS = frozenset({
        "test", "deploy", "build", "run", "fix", "check", "push",
        "pull", "merge", "commit", "start", "stop", "show", "list",
        "find", "open", "close", "create", "make", "add", "remove",
        "copy", "move", "edit", "set", "get", "go", "do", "kill",
    })

    # Question/open-ended keywords — indicate Q&A, not action.
    _QUESTION_PREFIXES = frozenset({
        "what", "why", "how", "who", "when", "where",
        "explain", "describe", "tell me", "show me",
        "can you", "could you", "would you", "do you",
        "does ", "is there", "are there", "what's",
        "what is", "how does", "how do", "who is",
        "list ", "what are",
    })

    def _is_command(self, text: str) -> bool:
        """Check if the message looks like a command/action request.

        Uses three tiers:
        1. Prefix match against ``_COMMAND_PREFIXES`` (startswith).
        2. Full-word match against ``_ACTION_KEYWORDS`` (anywhere in text).
        3. Empty / single-word fallback: if the first word is imperative-like
           (no question words), assume action.

        Returns True if the text is likely an action request.
        """
        lower = text.lower().strip()
        # Tier 1: Prefix match.
        if any(lower.startswith(p) for p in self._COMMAND_PREFIXES):
            return True
        # Tier 2: Full-word action keyword present anywhere.
        words = set(lower.split())
        if words & self._ACTION_KEYWORDS:
            return True
        # Tier 3: Short imperative commands whose first word is a known
        # action word. "test auth", "deploy now", "fix bug" — but NOT
        # noun phrases like "the project structure" or "my code".
        first_word = lower.split()[0] if lower.split() else ""
        if len(lower.split()) <= 3 and first_word in self._SHORT_ACTION_WORDS:
            return True
        return False

    def _is_question(self, text: str) -> bool:
        """Check if the message looks like a Q&A question.

        Matches question prefixes (what, why, how, explain, etc.)
        and multi-word question starters (tell me, show me, can you, etc.)
        """
        lower = text.lower().strip()
        # Check prefix-based question starters.
        if any(lower.startswith(q) for q in self._QUESTION_PREFIXES):
            return True
        # Check ends with a question mark (strong indicator).
        if lower.endswith("?"):
            return True
        return False

    def _is_ambiguous(self, text: str) -> bool:
        """Check if the message could be either a command or a question.

        Returns True when the intent is genuinely unclear:
        - Contains both action and question keywords.
        - Starts with an action keyword but ends with "?".
        - Just a noun phrase or single word not matching any known category.
        - Uses passive constructions that could be either.
        """
        lower = text.lower().strip()
        words = set(lower.split())

        # Contains both action and question markers.
        has_action = any(lower.startswith(p) for p in self._COMMAND_PREFIXES)
        has_action = has_action or bool(words & self._ACTION_KEYWORDS)
        has_question = any(lower.startswith(q) for q in self._QUESTION_PREFIXES)
        has_question = has_question or lower.endswith("?")

        if has_action and has_question:
            return True

        # Single word or very short text that doesn't match anything.
        # Could be a noun ("deployment?") or an imperative ("test").
        if len(lower.split()) <= 2 and not has_action and not has_question:
            if not self._is_chitchat(text):
                return True

        return False

    def _is_chitchat(self, text: str) -> bool:
        """Check if the message is chitchat/greeting (no evidence needed).

        Matches exact chitchat words as well as common elongated variants
        like ``hiii``, ``heyyy``, ``hellooo`` that users often type.
        """
        lower = text.lower().strip().rstrip("?!.")
        if lower in _CHITCHAT:
            return True
        first = lower.split()[0] if lower.split() else ""
        if first in _CHITCHAT:
            return True
        # Elongated greeting variants: hiiii, heyyy, etc.
        # Check if the first word starts with a short chitchat word and is
        # only a few chars longer (accounts for "hiiii" but not "himalayas").
        # Only "hi" and "hey" need this — other variants are already in _CHITCHAT.
        for chit in ("hi", "hey"):
            if first.startswith(chit) and len(first) <= len(chit) + 3:
                return True
        return False

    def _is_identity_question(self, text: str) -> bool:
        """Check if the user is asking who Friday is."""
        lower = text.lower().strip()
        # Also match Telegram commands like /start and /help.
        return any(q in lower for q in (
            "who are you", "what are you", "introduce yourself",
            "tell me about yourself",
        )) or lower in ("/start", "/help")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_chitchat(self, text: str) -> str:
        """Handle chitchat/greetings with persona-appropriate responses."""
        lower = text.lower().strip().rstrip("?!.")

        # Normalize for elongated greeting variants: "hiii" → "hi", "heyyy" → "hey"
        _normalized = lower
        for root in ("hello", "hi", "hey", "thanks"):
            if _normalized.startswith(root) and len(_normalized) <= len(root) + 3:
                _normalized = root
                break

        if _normalized in ("hello", "hi", "hey", "sup", "yo", "good morning", "good evening"):
            if self._operator_name:
                return f"Hey {self._operator_name}! What can I help you with?"
            return "Hey! What can I help you with?"

        if _normalized in ("how are you", "how's it going", "what's up"):
            name_part = f" {self._operator_name}" if self._operator_name else ""
            return (f"I'm good{name_part} — your workspace is quiet right now. "
                    "Anything you want me to look into?")

        if _normalized in ("thanks", "thank you", "thx", "ty"):
            return "Anytime."

        if lower in ("who are you", "what are you"):
            return self.config.greeting

        if lower in ("goodbye", "bye", "see you", "later", "cya"):
            return "Later! I'll be here."

        if lower in ("good bot", "you're awesome", "you rock", "nice", "cool"):
            return "Thanks! Happy to help."

        return "Got it. What else?"

    def _handle_question(self, text: str, channel_id: str,
                         context: ConversationContext) -> str:
        """Route a question through Friday's ask() pipeline."""
        try:
            from ..ask import Exchange, ask, Answer as AskAnswer
            from ..ask import Evidence as AskEvidence
            from ..ask import RetrievalRequirements

            conn = connect()
            prev: Optional["Exchange"] = None
            if context.exchanges:
                last_q, last_a = context.exchanges[-1]
                # context stores (str, str) tuples, but Exchange expects
                # an Answer object (with .evidence). Wrap the string reply
                # in a proper Answer so follow-up reference resolution in
                # ask() doesn't crash with 'str' object has no attribute 'evidence'.
                if isinstance(last_a, str):
                    prev = Exchange(
                        last_q,
                        AskAnswer(
                            text=last_a,
                            evidence=AskEvidence(
                                requirements=RetrievalRequirements(),
                                blocks=[last_a] if last_a else [],
                            ),
                            used_llm=False,
                        ),
                    )
                else:
                    prev = Exchange(last_q, last_a)

            answer = ask(text, conn, prev=prev, verbose=False)
            conn.close()

            if answer.text:
                return answer.text.strip()
            return "I'm not sure I have enough context to answer that yet."
        except Exception as exc:
            return f"Sorry, I ran into an error thinking about that: {exc}"

    def _handle_ambiguous(self, text: str, channel_id: str) -> str:
        """Handle a message that could be either a command or a question.

        Returns a clarifying question asking the user to disambiguate.
        This is the "unsure? → ask" path from the Agentic Action Layer spec.

        When the LLM is available, it can decide based on personality +
        context. When the LLM is unavailable, returns a hardcoded prompt.
        """
        # Try LLM with personality to resolve ambiguity naturally.
        try:
            context = self.get_context(channel_id)
            llm_reply = self._llm_chat(text, context)
            if llm_reply is not None:
                return llm_reply
        except Exception:
            pass

        # Hardcoded fallback: ask to clarify.
        name_part = f", {self._operator_name}" if self._operator_name else ""
        return (
            f"I'm not sure if you're asking me a question or telling me to do something{name_part}. "
            "Do you want me to **answer** that or **do** it? "
            "For example: 'friday ask ...' or 'friday do ...'"
        )

    def _handle_command(self, text: str, channel_id: str) -> str:
        """Route a command through Friday's execution pipeline.

        PRIMARY PATH: AgenticExecutor (decomposes task into steps across
        all tools — shell, git, filesystem, clipboard, etc.).
        FALLBACK: Legacy cmd_execute for simple single-goal commands.
        """
        # Fast predicate: if it looks like a simple command, use legacy path.
        lower = text.lower().strip()
        _SIMPLE_CMDS = frozenset({
            "deploy", "run ", "execute ", "start ", "stop ", "restart ",
            "check ", "status of",
        })
        is_simple = any(lower.startswith(p) for p in _SIMPLE_CMDS)

        if not is_simple:
            # Use the AgenticExecutor for complex multi-step tasks.
            try:
                from ..agent import run_agent, format_session
                agent_conn = connect()
                try:
                    session = run_agent(text, workspace=".", persist=True, conn=agent_conn)
                    if session.status == "succeeded":
                        return format_session(session)
                finally:
                    agent_conn.close()
                # Agent failed — fall through to legacy path.
            except Exception:
                pass

        # Legacy path: single command via cmd_execute.
        try:
            conn = connect()
            from ..cli_execute import cmd_execute
            import argparse

            args = argparse.Namespace()
            args.goal = [text]
            args.workspace = "."
            args.yes = True  # Auto-confirm for channel commands
            args.dry_run = False

            # Capture stdout
            import io
            import sys
            old_stdout = sys.stdout
            sys.stdout = captured = io.StringIO()
            try:
                rc = cmd_execute(args)
                output = captured.getvalue()
            finally:
                sys.stdout = old_stdout
            conn.close()

            if rc == 0:
                return f"Done. {output.strip()[:500]}"
            return f"Ran into an issue: {output.strip()[:500]}"
        except ImportError:
            return self._handle_question(
                f"Can you help me with this task: {text}",
                channel_id, ConversationContext()
            )
        except Exception as exc:
            return f"Couldn't execute that: {exc}"
