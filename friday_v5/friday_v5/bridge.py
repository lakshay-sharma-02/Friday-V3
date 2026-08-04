"""ClaudeBridge — one persistent Claude Code session behind the engine.

V5 port of V4's ``agent/bridge.py``, de-DB'd: the V4 version pushed
assistant text onto a SQLite ambient bus; this one hands it to an
``on_output`` callback instead. Everything else — the worker-thread
asyncio loop, the constant ``session_id`` so context accumulates, the
lazy SDK import — is unchanged.

Design laws:
- **Lazy SDK**: ``claude_agent_sdk`` is imported on first use; when it
  isn't installed, ``available()`` is False and ``send()`` returns a
  neutral message (the never-crash law).
- **One session, explicit end**: context accumulates until ``end()``;
  then the client disconnects and the next prompt starts fresh.
- **Hermetic**: the SDK is mocked in tests; nothing here touches a
  real model under test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("friday_v5.bridge")

#: The constant session id — one consistent Claude context per bridge.
_SESSION_ID = "friday-v5"

#: Sentinel that closes the session.
_END = object()

#: Tools granted without a prompt — the vault IS the memory, so its
#: I/O is always on. ``permission_mode="acceptEdits"`` makes Write/Edit
#: flow anyway; the callback additionally auto-allows the vault readers.
#: A *list* — the SDK shadows whole-tool entries before the callback.
_ALLOWED_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]

#: Hook event keys — PreToolUse fires for every tool call before it
#: runs, the reliable gate (can_use_tool only fires on "ask").
_HOOK_EVENT = "PreToolUse"


def _pre_tool_hooks(vault_root: Path, loop):
    """Build the PreToolUse hook matcher for the SDK options.

    An in-process ``HookCallback`` (runs on the bridge's event loop)
    that gates every tool call. Vault tools pass instantly; gated
    tools block on the operator's file-based decision.

    The SDK's converter reads attributes (``matcher.hooks``), so this
    returns a real ``HookMatcher`` — a plain dict serializes as empty
    hooks and is silently dropped.
    """
    from .permissions import make_pre_tool_hook
    from claude_agent_sdk.types import HookMatcher
    return {
        _HOOK_EVENT: [
            HookMatcher(matcher="*",
                        hooks=[make_pre_tool_hook(vault_root, loop)],
                        timeout=3600.0)
        ]
    }

#: One assistant text chunk (streamed) OR the final answer. ``final``
#: is True exactly for the terminal ``ResultMessage.result``.
OnOutput = Callable[[str, bool], None]


def _agent_model() -> str:
    """The model the bridge uses — same env convention as V4
    (``FRIDAY_V4_CLAUDE_MODEL``, default ``fable``)."""
    return os.environ.get("FRIDAY_V5_CLAUDE_MODEL", os.environ.get(
        "FRIDAY_V4_CLAUDE_MODEL", "fable"))


class ClaudeBridge:
    """One long-lived Claude Code session, driven from any thread."""

    def __init__(self, on_output: Optional[OnOutput] = None,
                 vault_root: Optional[Path] = None) -> None:
        self.on_output = on_output
        self._vault_root = vault_root
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

        Returns a result dict ``{ok, ended}`` — the prompt is accepted;
        output arrives on the ``on_output`` callback as Claude works.
        When the SDK is unavailable, returns a neutral failure, never
        a raise.
        """
        prompt = (text or "").strip()
        if not prompt:
            return {"ok": False, "ended": False}
        if not self.available():
            self._emit("The Claude bridge isn't available — install "
                       "claude-agent-sdk in the Friday environment.",
                       final=True)
            return {"ok": False, "ended": False}
        if not self.start():
            self._emit("Couldn't start the Claude bridge — check that "
                       "the claude CLI + Agent SDK are installed.",
                       final=True)
            return {"ok": False, "ended": False}
        # Wait (briefly) for the worker loop so the very first send
        # right after start() isn't a race.
        self._ready.wait(timeout=10.0)
        if self._loop is None or self._queue is None:
            self._emit("The Claude bridge didn't come up — check the "
                       "claude CLI + Agent SDK.", final=True)
            return {"ok": False, "ended": False}
        with self._lock:
            self._busy = True
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, prompt)
        except Exception as exc:
            logger.warning(f"bridge enqueue failed: {exc}")
            with self._lock:
                self._busy = False
            self._emit(f"Couldn't reach the Claude session: {exc}",
                       final=True)
            return {"ok": False, "ended": False}
        return {"ok": True, "ended": False}

    def end(self) -> dict:
        """Close the persistent session (fresh context next time)."""
        if self._loop is None or self._queue is None:
            return {"ok": True, "ended": True}
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, _END)
        except Exception as exc:
            logger.warning(f"bridge end failed: {exc}")
        with self._lock:
            self._busy = False
            self._connected = False
        return {"ok": True, "ended": True}

    def status(self) -> dict:
        """Session state (never raises)."""
        return {
            "available": bool(self.available()),
            "active": bool(self._connected),
            "busy": bool(self._busy),
        }

    def _emit(self, text: str, final: bool = False) -> None:
        if self.on_output is not None:
            try:
                self.on_output(text, final)
            except Exception as exc:
                logger.debug(f"bridge on_output failed: {exc}")

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
        except Exception as exc:
            logger.warning(f"claude bridge import failed: {exc}")
            self._available = False
            return

        # File-based permission gate. Two layers, same contract:
        # 1. can_use_tool callback — fires when the CLI evaluates a
        #    tool to "ask" (belt).
        # 2. PreToolUse hook — fires for EVERY tool, the reliable
        #    headless gate (suspenders). The hook blocks on the
        #    operator's allow/deny sidecar.
        can_use_tool = None
        hooks = None
        if self._vault_root is not None:
            from .permissions import make_can_use_tool
            can_use_tool = make_can_use_tool(self._vault_root, self._loop)
            hooks = _pre_tool_hooks(self._vault_root, self._loop)

        try:
            options = ClaudeAgentOptions(
                model=_agent_model(),
                permission_mode="acceptEdits",
                allowed_tools=_ALLOWED_TOOLS,
                can_use_tool=can_use_tool,
                hooks=hooks)
            client = ClaudeSDKClient(options=options)
            # Streaming input mode — connect with an empty async
            # generator; every query() is also an async generator
            # yielding the user message.
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
                try:
                    await client.query(
                        prompt=self._user_prompts(item),
                        session_id=_SESSION_ID)
                except Exception as exc:
                    logger.warning(f"claude query failed: {exc}")
                    self._emit("Claude Code errored: %s" % exc,
                               final=True)
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
        """The initial streaming-input generator (no messages yet).

        Never ends: connect() closes stdin as soon as the prompt
        generator finishes, which kills the CLI subprocess — later
        query() writes then fail with "ProcessTransport is not ready
        for writing". Idling forever keeps the session alive; real
        messages come via query().
        """
        if False:  # pragma: no cover - makes this an async generator
            yield {}
        while True:
            await asyncio.sleep(3600)

    @staticmethod
    async def _user_prompts(text: str):
        """An async-iterable prompt carrying one user message."""
        yield {"type": "user",
               "message": {"role": "user", "content": text}}

    async def _drain(self, client) -> None:
        """Forward SDK events to the on_output callback (guard, never
        raise)."""
        try:
            async for msg in client.receive_messages():
                try:
                    self._handle_message(msg)
                except Exception as exc:
                    logger.debug(f"bridge message handling: {exc}")
        except Exception as exc:
            logger.debug(f"bridge receive ended: {exc}")

    def _handle_message(self, msg) -> None:
        """One SDK event → on_output text / state updates."""
        name = type(msg).__name__
        if name == "ResultMessage":
            # The terminal message — final answer, error, or result text.
            result = getattr(msg, "result", None)
            if result:
                self._emit(result.strip(), final=True)
            errors = getattr(msg, "errors", None)
            if errors:
                self._emit("; ".join(str(e) for e in errors), final=True)
        elif name == "AssistantMessage":
            # Streamed assistant text during the turn.
            blocks = getattr(msg, "content", None)
            if not blocks:
                blocks = getattr(getattr(msg, "message", None),
                                 "content", None)
            text = "".join(
                getattr(b, "text", "") for b in blocks or ()
                if getattr(b, "type", "") == "text")
            text = text.strip()
            if text:
                self._emit(text)
        elif name == "SystemMessage":
            pass  # session init noise


__all__ = ["ClaudeBridge", "OnOutput"]
