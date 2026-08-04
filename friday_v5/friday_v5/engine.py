"""Engine — Friday V5's brain: a persistent Claude Code session that
routes each request to the right skill and writes everything to the
vault.

    Engine().ask("standup at 9am tomorrow")
        → embeds the skill table + vault context
        → forwards to the persistent ClaudeBridge session
        → streams assistant text to ``on_output``
        → appends the turn to ``vault/raw/``

The system prompt is deliberately thin: it points Claude at the vault
and the skills. Routing, skill use, and note-writing are Claude's job,
not code's.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from .bridge import ClaudeBridge
from .skills import render_all
from .vault import Vault

#: One CLI-swallowable call; (text, is_final_answer).
OnOutput = Callable[[str, bool], None]


class Engine:
    """Wires a persistent Claude session to a vault + skills dir."""

    def __init__(self, cwd: Optional[Path | str] = None,
                 vault: Optional[Vault] = None,
                 on_output: Optional[OnOutput] = None) -> None:
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.vault = vault or Vault()
        self.on_output = on_output
        self._bridge = ClaudeBridge(
            on_output=self._route_output,
            vault_root=self.vault.root)
        self._last_final: list[str] = []
        self._done = threading.Event()

    # ── output plumbing ──────────────────────────────────────────────

    def _route_output(self, text: str, final: bool) -> None:
        if final:
            self._last_final.append(text)
            # Log the assistant's answer to raw (worker thread — file
            # append is the only write, safe enough).
            try:
                self.vault.log("friday", text)
            except Exception:
                pass
            self._done.set()
        if self.on_output is not None:
            try:
                self.on_output(text, final)
            except Exception:
                pass

    def wait(self, timeout: float = 120.0) -> str:
        """Block until the final answer arrives; return it (or '')."""
        self._done.wait(timeout=timeout)
        return self._last_final[-1] if self._last_final else ""

    def ask_sync(self, text: str, timeout: float = 120.0) -> str:
        """One request through the engine, blocking until the final
        answer. The voice path calls this (it needs the reply to speak
        it). Returns the reply (or '' on failure)."""
        self.ask(text)
        return self.wait(timeout=timeout)

    # ── the ask ──────────────────────────────────────────────────────

    def _system_context(self, user_text: str) -> str:
        """The persistent context: vault layout, skill table, recent
        raw — kept compact so Claude reads it cheaply."""
        recent = []
        logs = sorted(self.vault.raw.glob("*.log"), reverse=True)[:2]
        for log in logs:
            lines = log.read_text(encoding="utf-8").splitlines()
            recent.extend(lines[-6:])
        tail = "\n".join(recent) if recent else "(empty)"
        return (
            "You are Friday, the operator's ambient AI partner. Memory "
            "is the vault (linked markdown, no database):\n"
            f"- raw: {self.vault.raw} (append-only turn log)\n"
            f"- wiki: {self.vault.wiki} (distilled notes, [[links]])\n"
            f"- outputs: {self.vault.outputs}\n\n"
            "Available skills (read the matching SKILL.md when the "
            "moment needs it):\n"
            f"{render_all(self.cwd)}\n\n"
            "Recent raw:\n"
            f"{tail}\n\n"
            "The operator says:\n"
            f"{user_text}"
        )

    def ask(self, text: str) -> dict:
        """One request through the engine. Returns ``{ok, reply}``."""
        prompt = self._system_context(text)
        self._last_final.clear()
        self._done.clear()
        result = self._bridge.send(prompt)
        self.vault.log("user", text)
        return {"ok": result.get("ok", False)}

    # ── permission surface ───────────────────────────────────────────

    def _decide(self, request_id: str, allow: bool, reason: str,
                verb: str) -> bool:
        """Shared decision path: resolve in-process registry, write the
        sidecar the hook polls, archive the pending ask. Returns True
        if the ask was found (resolved or sidecar written)."""
        from .permissions import VaultPermissions
        store = VaultPermissions(self.vault.root)
        ok = False
        try:
            from .permissions import registry
            ok = registry.resolve(request_id, allow, reason)
        except Exception:
            pass
        sidecar = store.pending / f"{request_id}.decision"
        sidecar.write_text("allow" if allow else "deny", encoding="utf-8")
        store.archive(request_id, allow, reason or verb)
        self.vault.log("operator", f"{verb} {request_id}: {reason or verb}")
        return ok or sidecar.exists()

    def allow(self, request_id: str, reason: str = "") -> bool:
        """Approve a pending Claude tool ask."""
        return self._decide(request_id, True, reason, "allowed")

    def deny(self, request_id: str, reason: str = "no") -> bool:
        """Deny a pending Claude tool ask."""
        return self._decide(request_id, False, reason, "denied")

    # ── surface access ───────────────────────────────────────────────

    @property
    def bridge(self) -> ClaudeBridge:
        return self._bridge
