"""IdentityEngine — persistent Friday persona for conversational interaction.

Gives Friday a name, personality, and bidirectional conversational ability
across all channels (Telegram, Slack, Discord, CLI). The engine:

1. Routes incoming messages through Friday's existing pipelines:
   - ``ask()`` for questions about your workspace
   - ``execute()`` for action commands
   - Direct persona response for chitchat/greetings
2. Maintains per-channel conversation context (last N exchanges)
3. Strikes a balance between helpful AI partner and humble assistant
"""

from __future__ import annotations

import datetime as dt
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

    def __init__(self, config: Optional[IdentityConfig] = None) -> None:
        self.config = config or IdentityConfig()
        # Per-channel conversation contexts (key = channel_id, e.g. "telegram:8656925628")
        self.contexts: dict[str, ConversationContext] = {}

    def get_context(self, channel_id: str) -> ConversationContext:
        """Get or create a conversation context for a channel."""
        if channel_id not in self.contexts:
            self.contexts[channel_id] = ConversationContext()
        return self.contexts[channel_id]

    def process(self, text: str, channel_id: str = "cli") -> str:
        """Process an incoming message and return a response.

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

        context = self.get_context(channel_id)

        # --- Route: Identity questions (checked BEFORE chitchat so
        # "who are you" / "introduce yourself" get the persona greeting) ---
        if self._is_identity_question(text):
            reply = self.config.greeting
            context.add(text, reply)
            return reply

        # --- Route: Chitchat / Greetings ---
        if self._is_chitchat(text):
            reply = self._handle_chitchat(text)
            context.add(text, reply)
            return reply

        # --- Route: Action commands ---
        if self._is_command(text):
            reply = self._handle_command(text, channel_id)
            context.add(text, reply)
            return reply

        # --- Route: Questions (ask pipeline) ---
        reply = self._handle_question(text, channel_id, context)
        context.add(text, reply)
        return reply

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    _COMMAND_PREFIXES = (
        "deploy", "run ", "execute", "check ", "status of",
        "start ", "stop ", "restart ", "install ", "update ",
        "create ", "delete ", "fix ", "refactor ", "test ",
        "deploy ", "push ", "pull ", "merge ", "commit ",
        "build ", "compile ", "migrate ",
    )

    def _is_command(self, text: str) -> bool:
        """Check if the message looks like a command/action request."""
        lower = text.lower().strip()
        return any(lower.startswith(p) for p in self._COMMAND_PREFIXES)

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
            return "Hey! What can I help you with?"

        if _normalized in ("how are you", "how's it going", "what's up"):
            return ("I'm good — your workspace is quiet right now. "
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

    def _handle_command(self, text: str, channel_id: str) -> str:
        """Route a command through Friday's execution pipeline."""
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
            # Fallback: route through ask if execute module not available
            return self._handle_question(
                f"Can you help me with this task: {text}",
                channel_id, ConversationContext()
            )
        except Exception as exc:
            return f"Couldn't execute that: {exc}"
