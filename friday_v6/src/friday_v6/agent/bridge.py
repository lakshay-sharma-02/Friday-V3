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
import time
from typing import Optional

logger = logging.getLogger("friday_v6.agent.bridge")

#: The chat prefixes that open / close the agent session.
CLAUDE_PREFIX = "CLAUDE:"
CLAUDE_END = "CLAUDE END"

#: The constant session id — one consistent Claude context per bridge.
_SESSION_ID = "friday-bridge"

#: Sentinel that closes the session.
_END = object()

#: Sentinel that (re)runs the SDK connect handshake. send() queues it
#: and waits on ``_connected_event`` so a worker whose connect() died
#: is reported as a failure instead of a silent acceptance.
_CONNECT = object()


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
        #: In-flight turns awaiting the worker (wait_idle waits on this,
        #: not just _busy — a turn that failed must still be counted
        #: so the CLI doesn't exit and kill the session mid-flight).
        self._pending_turns = 0
        #: Set once the worker's loop + queue exist (send() waits on it).
        self._ready = threading.Event()
        #: Set once the SDK session handshake finished (send() waits on
        #: it before claiming acceptance — a dead connect is reported
        #: as failure, never as "Claude Code is on it").
        self._connected_event: Optional[threading.Event] = None
        self._connect_error: Optional[str] = None

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
        self._connected_event = threading.Event()
        self._connect_error = None
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
        # Wave 5 — kill switch: while armed (``friday6 abort``), new
        # prompts are refused until the operator clears it. The in-flight
        # turn is already stopped by the tool hook denying every call;
        # this refuses anything queued behind it.
        try:
            from ..abort import kill_switch
            if kill_switch().is_armed():
                return {
                    "ok": False,
                    "ended": False,
                    "response": ("I've stopped — the operator aborted this "
                                 "session. Clear it with 'friday6 abort "
                                 "--clear' to resume."),
                }
        except Exception:
            pass
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
        # right after start() isn't a race. Then wait for the *session
        # connect* (the SDK handshake + auth) — a worker that dies in
        # connect() must never be reported as accepting work.
        self._ready.wait(timeout=10.0)
        if self._loop is None or self._queue is None:
            return {
                "ok": False,
                "ended": False,
                "response": ("The Claude bridge didn't come up — check "
                             "the claude CLI + Agent SDK."),
            }
        self._connected_event = threading.Event()
        self._connect_error = None
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, _CONNECT)
        self._connected_event.wait(timeout=30.0)
        with self._lock:
            if not self._connected:
                return {
                    "ok": False,
                    "ended": False,
                    "response": self._connect_error or (
                        "The Claude session didn't connect — check that "
                        "the claude CLI is authenticated and "
                        "FRIDAY_V4_CLAUDE_MODEL names a reachable "
                        "model."),
                }
        with self._lock:
            self._busy = True
            self._pending_turns += 1
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, prompt)
        except Exception as exc:
            logger.warning(f"bridge enqueue failed: {exc}")
            with self._lock:
                self._busy = False
                self._pending_turns -= 1
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
            self._pending_turns = 0
        return {"ok": True, "ended": True,
                "response": "Claude session closed — next CLAUDE: starts "
                           "fresh."}

    def status(self) -> dict:
        """Session state for the PWA badge (never raises)."""
        aborted = False
        try:
            from ..abort import kill_switch
            aborted = kill_switch().is_armed()
        except Exception:
            pass
        return {
            "available": bool(self.available()),
            "active": bool(self._connected),
            "busy": bool(self._busy),
            "aborted": aborted,
        }

    def wait_idle(self, timeout: float = 300.0,
                  ask_callback=None) -> None:
        """Block until the current turn's ``_busy`` flag clears.

        One-shot CLI surfaces call this after ``send()``: the worker
        runs on a daemon thread, so without it the process would exit
        and kill Claude mid-turn. Returns after the turn (the streamed
        replies are already on the ambient bus) or the timeout —
        never raises.

        ``ask_callback(request_id, description)`` is invoked once per
        pending durable permission ask the turn raises — one-shot
        surfaces use it to prompt the operator instead of blocking
        silently until ``ASK_TIMEOUT_SECONDS`` (1h).
        """
        if ask_callback is not None:
            from .permissions import registry
            seen: set[str] = set()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with self._lock:
                    idle = (not self._busy
                            and self._pending_turns == 0)
                if idle:
                    return
                # Surface new pending asks exactly once each.
                try:
                    for rid, ask in list(registry._asks.items()):
                        if rid not in seen:
                            seen.add(rid)
                            ask_callback(rid, ask.description)
                except Exception:
                    pass
                time.sleep(0.25)
            logger.warning("bridge wait_idle timed out")
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._busy and self._pending_turns == 0:
                    return
            time.sleep(0.25)
        logger.warning("bridge wait_idle timed out")

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
            # string prompt never triggers the callback. connect with
            # ``None``: the SDK substitutes its own empty stream that
            # keeps the connection open. (A never-yielding custom
            # generator stalls the initialize handshake — the transport
            # stays ``not ready for writing`` and the first query dies.)
            await client.connect(prompt=None)
            with self._lock:
                self._connected = True
        except Exception as exc:
            logger.warning(f"claude bridge connect failed: {exc}")
            self._connect_error = f"The Claude session failed to connect: {exc}"
            with self._lock:
                self._connected = False
            ev = self._connected_event
            if ev is not None:
                ev.set()
            return
        self._client = client

        # Drain ONLY while a query is running. The SDK's
        # receive_messages() shares the transport with query(); a
        # background drain started before any query races the write
        # path and every query then dies with "ProcessTransport is not
        # ready for writing". The proven order: query() first, then
        # receive. So the drain task is (re)created per turn and
        # cancelled when the turn's ResultMessage arrives.
        try:
            while True:
                item = await self._queue.get()
                if item is _END:
                    break
                if item is _CONNECT:
                    with self._lock:
                        ok = self._connected
                        err = self._connect_error
                    self._connected_event.set()
                    if not ok:
                        # The worker's connect failed (or never ran) —
                        # report the real reason to the waiting send().
                        self._connect_error = err or (
                            "The Claude session didn't connect — check "
                            "the claude CLI + Agent SDK.")
                    continue
                prompt = item
                drain = asyncio.create_task(self._drain_until_result(client))
                try:
                    await self._query_with_retry(client, prompt)
                    # Drain completes on its own when the ResultMessage
                    # arrives; wait for it so streamed text is published
                    # before wait_idle sees the turn end.
                    await asyncio.wait_for(
                        asyncio.shield(drain), timeout=300.0)
                except Exception as exc:
                    logger.warning(f"bridge turn failed: {exc}")
                finally:
                    drain.cancel()
                    with self._lock:
                        self._busy = False
                        self._pending_turns -= 1
        finally:
            try:
                await client.disconnect()
            except Exception as exc:
                logger.debug(f"claude disconnect: {exc}")
            with self._lock:
                self._connected = False
                self._busy = False
                self._pending_turns = 0

    @staticmethod
    async def _query_with_retry(client, prompt: str, attempts: int = 4,
                                delay: float = 1.0) -> None:
        """One query, retrying the transport-not-ready race.

        The SDK's connect() spawns the subprocess and returns before
        the CLI finishes its init handshake; the first query can raise
        ``CLIConnectionError("ProcessTransport is not ready for
        writing")``. Retry with backoff so the first real turn isn't
        lost to the race. Any other error is fatal (published, not
        retried).
        """
        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                await client.query(
                    prompt=ClaudeBridge._user_prompts(prompt),
                    session_id=_SESSION_ID)
                return
            except Exception as exc:
                last_exc = exc
                if "not ready for writing" not in str(exc) or i == attempts - 1:
                    break
                await asyncio.sleep(delay * (i + 1))
        logger.warning(f"claude query failed: {last_exc}")
        try:
            ClaudeBridge._publish_agent_static(
                f"Claude Code errored: {last_exc}")
        except Exception:
            pass

    @staticmethod
    def _publish_agent_static(text: str) -> None:
        """Static shim for :meth:`_publish_agent` (no self needed)."""
        from .. import db
        from ..ambient import AmbientBus, Event, Priority
        conn = db.connect()
        try:
            AmbientBus(conn).publish(Event(
                topic="agent", payload=text, priority=Priority.INFO,
                source="agent.bridge"))
        finally:
            conn.close()

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

    async def _drain_until_result(self, client) -> None:
        """Forward SDK events onto the ambient bus; stop at ResultMessage.

        Runs per-turn (see the _main loop): receive_messages() must be
        consumed only while a query is live, and the task ends when the
        turn's ResultMessage arrives so the caller's wait_for returns.
        Guard, never raise.
        """
        try:
            async for msg in client.receive_messages():
                try:
                    self._handle_message(msg)
                except Exception as exc:
                    logger.debug(f"bridge message handling: {exc}")
                if type(msg).__name__ == "ResultMessage":
                    return
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
            # A block is speech-worthy when it carries text. TextBlock in
            # the SDK has no ``type`` field (0.2.x dropped it — the type is
            # implicit in the class name), so match on the class name:
            # accept ``TextBlock`` / ``*Text`` and, defensively, any block
            # whose ``type`` is "text". ThinkingBlock stays silence.
            text = "".join(
                getattr(b, "text", "") for b in blocks or ()
                if type(b).__name__.endswith(("Text", "TextBlock"))
                or getattr(b, "type", "") == "text")
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
                    priority=Priority.ROUTINE,
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
