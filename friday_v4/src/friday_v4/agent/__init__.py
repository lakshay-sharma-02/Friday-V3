"""Agent bridge — a persistent Claude Code session behind the PWA chat.

The operator types ``CLAUDE: <anything>`` in the companion chat; the
bridge forwards it to ONE long-lived Claude Code session (the Agent SDK
spawns the same ``claude`` CLI the operator runs in the terminal, same
9router settings) and keeps that session alive until ``CLAUDE END`` —
context accumulates across messages like a real conversation.

Tool-permission requests (``can_use_tool``) are woven into Friday's
durable permission system: the ask is recorded in ``permission_requests``
(source=``bridge``) and surfaced on the ambient bus (the phone's SSE
feed sees it), and the operator answers it from any surface with
"yes, run it" / "no" through the normal ``AutonomyAgent.accept/deny``
path — which resolves the SDK's pending tool call instead of executing
a shell command.

Design laws:
- **Never crash** — ``claude_agent_sdk`` is a lazy import; when it's
  not installed, ``available()`` is False and ``send()`` returns a
  neutral message instead of raising.
- **One session** — a single SDK client + constant ``session_id``
  keeps Claude's context across ``CLAUDE:`` prompts until ``CLAUDE END``.
- **Hermetic** — the SDK is mocked in tests; the bridge never touches
  a real model under test.

Status: Wave 22 — CLAUDE: bridge (2026-08).
"""

from __future__ import annotations

from .bridge import (ClaudeBridge, get_bridge, CLAUDE_PREFIX, CLAUDE_END,
                     is_claude_message, is_claude_end)

__all__ = [
    "ClaudeBridge",
    "get_bridge",
    "CLAUDE_PREFIX",
    "CLAUDE_END",
    "is_claude_message",
    "is_claude_end",
]
