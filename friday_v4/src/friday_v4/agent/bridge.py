"""ClaudeBridge — one persistent Claude Code session behind the chat.

``CLAUDE: <text>`` in the companion PWA forwards the text to a single
long-lived Claude Code session (the Agent SDK spawns the same ``claude``
CLI the operator runs in the terminal — same 9router settings, same
``~/.claude/settings.json``). The session stays alive across prompts
until ``CLAUDE END``, so Claude keeps the whole conversation's context,
just like a real working session.

How it works:

- A background daemon thread runs the asyncio event loop the SDK
  requires. ``send()``/``end()`` (called from HTTP handler threads)
  hand prompts to the loop thread-safe via ``call_soon_threadsafe``
  onto an asyncio queue.
- One ``ClaudeSDKClient`` with a constant ``session_id`` keeps the
  context. ``permission_mode="default"`` + a ``can_use_tool`` callback
  intercept every tool call: the ask becomes a durable Friday
  ``permission_requests`` row (source=``bridge``) surfaced on the
  ambient bus; the operator answers "yes, run it"/"no" from the PWA
  and the SDK future resolves (see :mod:`.permissions`).
- Assistant text and progress stream onto the ambient bus (topic
  ``agent``) so the PWA Live feed shows Claude working in real time.

Design laws:
- **Lazy SDK**: ``claude_agent_sdk`` is imported on first use; when it
  isn't installed, ``available()`` is False and ``send()`` returns a
  neutral message (the never-crash law).
- **One session, explicit end**: context accumulates until ``CLAUDE
  END``; then the client disconnects and the next ``CLAUDE:`` starts
  fresh.
- **Hermetic**: the SDK is mocked in tests; nothing here touches a
  real model under test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("friday_v4.agent.bridge")

#: The chat prefixes that open / close the agent session.
CLAUDE_PREFIX = "CLAUDE:"
CLAUDE_END = "CLAUDE END"

#: The constant session id — one consistent Claude context per bridge.
_SESSION_ID = "friday-bridge"

#: Sentinel that closes the session.
_END = object()


def is_claude_message(text: str) -> bool:
    """``CLAUDE: ...`` (prefix, case-insensitive) → bridge message."""
    return (text or "").strip().upper().startswith(CLAUDE_PREFIX)


def is_claude_end(text: str) -> bool:
    """``CLAUDE END`` (alone) → close the session."""
    return (text or "").strip().upper() == CLAUDE_END


def _agent_model() -> str:
    """The model the bridge uses — same env convention as the one-shot
    ClaudeCodeExecutor (``FRIDAY_V4_CLAUDE_MODEL``, default ``fable`` —
    the gateway model proven in this workspace)."""
    return os.environ.get("FRIDAY_V4_CLAUDE_MODEL", "fable")


class ClaudeBridge:
    """One long-lived Claude Code session, driven from any thread."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._queue: Optional[asyncio.Queue] = None
        self._available: Optional[bool] = None
        self._connected = False
        self._busy = False
        self._lock = threading.Lock()
        #: Set once the worker's loop + queue exist (send() waits on it).
        self._ready = threading.Event()

    # ── availability ─────────────────────────────────────────────────

    def available(self) -> bool:
        """Whether the Agent SDK is installed (lazy probe, cached)."""
        if self._available is None:
            try:
                import claude_agent_sdk  # noqa: F401
                self._available = True
            except Exception:
                self._available = False
        return self._available

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the worker thread + event loop (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.available():
            return False
        self._connected = False
        self._busy = False
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="claude-bridge")
        self._thread.start()
        return True

    def send(self, text: str) -> dict:
        """Forward one prompt to the persistent session (thread-safe).

        Returns a result dict ``{ok, response, ended}`` — the *prompt
        is accepted* (the reply streams to the ambient bus as Claude
        works). When the SDK is unavailable, the response is a neutral
        message, never a raise.
        """
        prompt = text.strip()
        if _is_claude_end(prompt):
            return self.end()
        # The prefix itself is the trigger — strip it so Claude receives
        # the actual instruction ("CLAUDE: list files" → "list files").
        upper = prompt.upper()
        if upper.startswith(CLAUDE_PREFIX):
            prompt = prompt[len(CLAUDE_PREFIX):].strip()
        if not self.available():
            return {
                "ok": False,
                "ended": False,
                "response": ("The Claude bridge isn't available here — "
                             "install claude-agent-sdk in the Friday "
                             "environment to use CLAUDE: messages."),
            }
        if not self.start():
            return {
                "ok": False,
                "ended": False,
                "response": ("Couldn't start the Claude bridge — check "
                             "that the claude CLI + Agent SDK are "
                             "installed."),
            }
        # Wait (briefly) for the worker loop so the very first send
        # right after start() isn't a race.
        self._ready.wait(timeout=10.0)
        if self._loop is None or self._queue is None:
            return {
                "ok": False,
                "ended": False,
                "response": ("The Claude bridge didn't come up — check "
                             "the claude CLI + Agent SDK."),
            }
        with self._lock:
            self._busy = True
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, prompt)
        except Exception as exc:
            logger.warning(f"bridge enqueue failed: {exc}")
            with self._lock:
                self._busy = False
            return {
                "ok": False,
                "ended": False,
                "response": f"Couldn't reach the Claude session: {exc}",
            }
        return {
            "ok": True,
            "ended": False,
            "response": ("Claude Code is on it — follow the Live feed "
                         "for its progress. Say 'CLAUDE END' to close "
                         "the session."),
        }

    def end(self) -> dict:
        """Close the persistent session (fresh context next time)."""
        if self._loop is None or self._queue is None:
            return {"ok": True, "ended": True,
                    "response": "Claude session closed."}
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, _END)
        except Exception as exc:
            logger.warning(f"bridge end failed: {exc}")
        with self._lock:
            self._busy = False
            self._connected = False
        return {"ok": True, "ended": True,
                "response": "Claude session closed — next CLAUDE: starts "
                           "fresh."}

    def status(self) -> dict:
        """Session state for the PWA badge (never raises)."""
        return {
            "available": bool(self.available()),
            "active": bool(self._connected),
            "busy": bool(self._busy),
        }

    # ── worker ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """The bridge's private asyncio loop (daemon thread)."""
        try:
            asyncio.run(self._main())
        except Exception as exc:
            logger.warning(f"claude bridge loop died: {exc}")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready.set()
        client = None
        try:
            from claude_agent_sdk.client import ClaudeSDKClient
            from claude_agent_sdk.types import ClaudeAgentOptions
            from .permissions import make_can_use_tool
        except Exception as exc:
            logger.warning(f"claude bridge import failed: {exc}")
            self._available = False
            return

        can_use_tool = make_can_use_tool(self._db_path, self._loop)
        try:
            options = ClaudeAgentOptions(
                model=_agent_model(),
                permission_mode="default",
                can_use_tool=can_use_tool)
            client = ClaudeSDKClient(options=options)
            # can_use_tool requires the *streaming input* mode — a plain
            # string prompt never triggers the callback. connect with an
            # empty async generator; every query() is also an async
            # generator yielding the user message.
            await client.connect(prompt=self._empty_prompts())
            with self._lock:
                self._connected = True
        except Exception as exc:
            logger.warning(f"claude bridge connect failed: {exc}")
            with self._lock:
                self._connected = False
            return
        self._client = client

        receive = asyncio.create_task(self._drain(client))
        try:
            while True:
                item = await self._queue.get()
                if item is _END:
                    break
                prompt = item
                try:
                    await client.query(
                        prompt=self._user_prompts(prompt),
                        session_id=_SESSION_ID)
                except Exception as exc:
                    logger.warning(f"claude query failed: {exc}")
                    self._publish_agent(f"Claude Code errored: {exc}")
                finally:
                    with self._lock:
                        self._busy = False
        finally:
            receive.cancel()
            try:
                await client.disconnect()
            except Exception as exc:
                logger.debug(f"claude disconnect: {exc}")
            with self._lock:
                self._connected = False
                self._busy = False

    @staticmethod
    async def _empty_prompts():
        """The initial streaming-input generator (no messages yet)."""
        if False:  # pragma: no cover - makes this an async generator
            yield {}

    @staticmethod
    async def _user_prompts(text: str):
        """An async-iterable prompt carrying one user message."""
        yield {"type": "user",
               "message": {"role": "user", "content": text}}

    async def _drain(self, client) -> None:
        """Forward SDK events onto the ambient bus (guard, never raise)."""
        try:
            async for msg in client.receive_messages():
                try:
                    self._handle_message(msg)
                except Exception as exc:
                    logger.debug(f"bridge message handling: {exc}")
        except Exception as exc:
            logger.debug(f"bridge receive ended: {exc}")

    def _handle_message(self, msg) -> None:
        """One SDK event → ambient bus text / state updates."""
        name = type(msg).__name__
        if name == "AssistantMessage":
            # SDK AssistantMessage carries content blocks directly
            # (``content=[TextBlock(text=...)]``), not under .message.
            blocks = getattr(msg, "content", None)
            if not blocks:
                blocks = getattr(getattr(msg, "message", None),
                                 "content", None)
            text = "".join(
                getattr(b, "text", "") for b in blocks or ()
                if getattr(b, "type", "") == "text")
            text = text.strip()
            if text:
                self._publish_agent(text)
        elif name == "SystemMessage":
            pass  # session init noise
        elif name in ("ResultMessage", "StreamEvent", "RateLimitEvent"):
            pass  # progress — the streamed text already told the story

    def _publish_agent(self, text: str) -> None:
        """Push agent output onto the ambient bus (topic ``agent``)."""
        try:
            from .. import db
            from ..ambient import AmbientBus, Event, Priority
            conn = db.connect(self._db_path) if self._db_path \
                else db.connect()
            try:
                AmbientBus(conn).publish(Event(
                    topic="agent",
                    payload=text,
                    priority=Priority.NORMAL,
                    source="agent.bridge"))
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"agent ambient publish failed: {exc}")


#: Module singleton — the mobile server and the autonomy hook share one
#: session (one consistent Claude context per Friday instance).
_bridge: Optional[ClaudeBridge] = None
_bridge_lock = threading.Lock()


def get_bridge(db_path: Optional[str] = None) -> ClaudeBridge:
    """The shared bridge (lazily created, thread-safe)."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = ClaudeBridge(db_path=db_path)
        return _bridge


__all__ = ["ClaudeBridge", "get_bridge", "CLAUDE_PREFIX", "CLAUDE_END",
           "is_claude_message", "is_claude_end"]

# Back-compat aliases used by the routing layer.
_is_claude_message = is_claude_message
_is_claude_end = is_claude_end
