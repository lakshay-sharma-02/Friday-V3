"""Tool-permission weave — Claude's asks become Friday's durable asks.

When the bridged Claude Code session wants to use a tool (Bash, Edit,
Write…), the SDK's ``can_use_tool`` callback fires. This module:

1. Records the ask durably (``permission_requests``, source=``bridge``)
   so it survives restarts and is resolvable from any surface —
   "yes, run it" / "no" through the same ``AutonomyAgent`` path the
   daemon's asks use.
2. Publishes an IMPORTANT ambient event so the PWA's Live feed shows
   "May I Bash: rm -f /tmp/x?" before the operator answers.
3. Blocks the SDK's pending tool call on an asyncio.Future until the
   operator resolves the ask; ``resolve()`` (called from
   ``AutonomyAgent.accept/deny``) completes it.

Never crashes: a missing DB or bus degrades the ask to an immediate
deny, never a raise (the SDK tool call just doesn't run).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

logger = logging.getLogger("friday_v6.agent.permissions")

#: The permission_requests source tag that marks a bridge ask. The
#: AutonomyAgent.accept/deny hook keys on this to resolve the SDK
#: future instead of running ``execute`` (the bridge ask is not a shell
#: command — it is permission for a Claude Code tool call).
SOURCE = "bridge"

#: How long the SDK waits for the operator's decision before the tool
#: call is denied (fail-closed; Claude blocks meanwhile).
ASK_TIMEOUT_SECONDS = 3600.0


class BridgePermission:
    """One pending tool ask: durable request row ↔ SDK future."""

    __slots__ = ("request_id", "description", "future", "loop", "resolved")

    def __init__(self, request_id: str, description: str,
                 future, loop) -> None:
        self.request_id = request_id
        self.description = description
        self.future = future
        self.loop = loop
        self.resolved = False


class PermissionRegistry:
    """Thread-safe registry of pending bridge asks (request_id → ask).

    ``request_id`` is the durable ``permission_requests`` id — the same
    id the operator's "yes, run it" resolves through AutonomyAgent. The
    registry is a module singleton so the mobile server (which creates
    the ask) and the autonomy hook (which resolves it) share it.
    """

    def __init__(self) -> None:
        self._asks: dict[str, BridgePermission] = {}
        self._lock = threading.Lock()

    def register(self, ask: BridgePermission) -> None:
        with self._lock:
            self._asks[ask.request_id] = ask

    def get(self, request_id: str) -> Optional[BridgePermission]:
        with self._lock:
            return self._asks.get(request_id)

    def resolve(self, request_id: str, allow: bool,
                reason: str = "") -> bool:
        """Complete the pending SDK tool call (thread-safe).

        Runs from the HTTP/CLI thread (AutonomyAgent.accept/deny) while
        the SDK awaits inside the bridge's event loop — ``call_soon``
        across the loop boundary is the only safe way to touch the
        future. Returns False when the ask isn't pending here.
        """
        with self._lock:
            ask = self._asks.get(request_id)
            if ask is None or ask.resolved:
                return False
            ask.resolved = True
            del self._asks[request_id]
        try:
            ask.loop.call_soon_threadsafe(
                ask.future.set_result,
                (allow, reason))
        except Exception as exc:
            logger.warning(f"bridge permission resolve failed: {exc}")
            return False
        return True


#: Module singleton — the bridge and the autonomy hook share it.
registry = PermissionRegistry()


def _summarize(tool_input: dict, limit: int = 160) -> str:
    """A short operator-facing summary of a tool call's input."""
    try:
        text = json.dumps(tool_input, default=str)
    except Exception:
        text = str(tool_input)
    return text[:limit]


def make_can_use_tool(db_path: Optional[str], loop, timeout: float =
                      ASK_TIMEOUT_SECONDS):
    """Build the SDK's ``can_use_tool`` callback for one bridge session.

    The returned coroutine is what the Claude Agent SDK awaits before
    running a tool: it records a durable bridge ask, publishes the
    ambient event, and blocks on the operator's decision. Returns a
    ``PermissionResultAllow`` / ``PermissionResultDeny`` dict exactly
    as the SDK expects (``{"behavior": "allow"}`` / ``{"behavior":
    "deny", "message": ...}``) — plain dicts keep the SDK a lazy dep.
    """
    from .. import db

    async def can_use_tool(tool_name: str, tool_input: dict, ctx) -> dict:
        # Wave 5 — the kill switch: while the operator has armed it
        # (``friday6 abort``), EVERY tool call is denied immediately —
        # no ask is recorded, no future is created, the session stops
        # mid-turn. The operator's override wins over everything.
        try:
            from ..abort import kill_switch
            if kill_switch().is_armed():
                return {"behavior": "deny",
                        "message": "The operator aborted this session."}
        except Exception:
            pass  # a broken switch never blocks the permission path
        description = f"{tool_name}: {_summarize(tool_input)}"
        conn = None
        try:
            conn = db.connect(db_path) if db_path else db.connect()
            rid = db.create_permission_request(
                conn,
                description=f"Claude Code wants to {description}",
                action_type=f"claude_tool:{tool_name}",
                command=json.dumps(tool_input, default=str),
                cwd=str(getattr(ctx, "cwd", "") or ""),
                goal=description,
                source=SOURCE)
            if not rid:
                logger.debug("bridge ask: no request id → deny")
                return {"behavior": "deny", "message": "permission system unavailable"}
        except Exception as exc:
            logger.warning(f"bridge ask record failed: {exc}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return {"behavior": "deny", "message": "permission system unavailable"}
        try:
            # Publish so the PWA Live feed shows the ask immediately.
            from ..ambient import AmbientBus, Event, Priority
            try:
                AmbientBus(conn).publish(Event(
                    topic="permission",
                    payload=f"Claude Code wants to {description}",
                    priority=Priority.IMPORTANT,
                    source="agent.bridge"))
            except Exception as exc:
                logger.debug(f"bridge ask push failed: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        import asyncio
        future = loop.create_future()
        registry.register(BridgePermission(rid, description, future, loop))
        try:
            allow, _reason = await asyncio.wait_for(
                future, timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            allow = False
        if allow:
            return {"behavior": "allow"}
        return {"behavior": "deny",
                "message": "The operator declined this tool call."}

    return can_use_tool


__all__ = ["registry", "make_can_use_tool", "SOURCE"]
