"""Friday Identity — persistent persona that can be chatted with through any channel.

The Identity engine gives Friday a name, personality, and the ability to hold
conversations through Telegram, Slack, Discord, and the CLI. It routes incoming
messages through Friday's existing question-answering and execution pipelines
and sends responses back through the same channel.

Architecture:
  Channel (Telegram/Slack/CLI)
    → IdentityEngine.process_message(text, channel_id, context)
      → ask() for questions
      → execute() for commands (deploy, run, check, etc.)
      → Direct response for chitchat/greetings
    → Response sent back via the same channel
"""

from __future__ import annotations

from .engine import IdentityEngine, IdentityConfig

__all__ = [
    "IdentityEngine",
    "IdentityConfig",
]
