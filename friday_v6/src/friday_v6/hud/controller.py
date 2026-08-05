"""HUD controller — the HUD's brain, pure and hermetic (Wave 3).

The HUD is *another surface of the same Friday*: its input box routes
through the same :class:`~friday_v6.nl_router.TextCommandHandler`
every other surface uses, its stream mirrors the durable ambient bus,
and its permission buttons resolve the same durable asks the autonomy
loop raises. This controller is what the Textual widgets drive — it
has NO Textual import, so the entire wiring is hermetic-testable and
the HUD degrades (never crashes) when Textual is missing.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from ..vault import Vault
from . import parsers
from .vitals import _read

logger = logging.getLogger("friday_v6.hud.controller")


class HudController:
    """One object the widgets poll — handler + vault + stream + asks.

    Never raises: every accessor degrades to an honest empty value
    (missing DB, vault, or ambient table → graceful response).
    """

    def __init__(self, conn=None, vault_root: Optional[str] = None,
                 desktop_handler=None, llm=None) -> None:
        self._conn = conn
        self._vault_root = vault_root
        self._desktop_handler = desktop_handler
        self._llm = llm
        self._lock = threading.Lock()
        #: In-memory stream tail (local turns + replayed ambient events).
        self._stream: list[str] = []
        self._vault = Vault(root=vault_root) if vault_root else Vault()
        self._handler = None  # built lazily — never at import/init time

    # ── brain (the ONE handler, shared by every surface) ────────────

    @property
    def handler(self):
        """The TextCommandHandler — built lazily on first use."""
        if self._handler is None:
            from ..nl_router import TextCommandHandler
            self._handler = TextCommandHandler(
                conn=self._conn,
                desktop_handler=self._desktop_handler,
                llm=self._llm,
                vault_root=self._vault_root)
        return self._handler

    def handle(self, text: str) -> str:
        """Route one utterance through the brain; returns Friday's reply.

        The reply is pushed to the HUD's stream like the phone/web
        chat (one presence — the same exchange log, the same brain).
        Never raises: any failure replies honestly.
        """
        text = (text or "").strip()
        if not text:
            return ""
        try:
            result = self.handler.handle(text)
            reply = getattr(result, "response", "") or ""
        except Exception as exc:
            logger.warning(f"hud handle failed: {exc}")
            reply = f"Sorry, I ran into an error: {exc}"
        self.push(f"you: {text}")
        if reply:
            self.push(f"friday: {reply}")
        return reply

    # ── stream (durable ambient replay + local turns) ───────────────

    def push(self, line: str) -> None:
        with self._lock:
            self._stream.append(line)
            self._stream = self._stream[-200:]

    def stream_lines(self, limit: int = 8) -> list[str]:
        """Recent stream lines: local turns + replayed ambient events.

        The durable ambient queue is replayed like the phone's SSE
        feed (a late surface catches what it missed). Newly replayed
        events are merged INTO the in-memory tail (dedup by message)
        so a later poll never re-appends the same line.
        """
        lines = list(self._stream)
        try:
            from ..ambient import AmbientBus
            for ev in AmbientBus(self._conn).replay(limit=25):
                line = parsers.format_ambient_event(ev)
                if line in lines:
                    continue
                lines.append(line)
                with self._lock:
                    if line not in self._stream:
                        self._stream.append(line)
                        self._stream = self._stream[-200:]
        except Exception as exc:
            logger.debug(f"hud ambient replay skipped: {exc}")
        return lines[-limit:]

    # ── vault panels ────────────────────────────────────────────────

    def schedule(self) -> list[str]:
        try:
            return parsers.parse_schedule(self._vault.wiki / "schedule.md")
        except Exception as exc:
            logger.debug(f"hud schedule failed: {exc}")
            return []

    def notices(self, n: int = 5) -> list[dict]:
        try:
            return self._vault.latest_notices(n)
        except Exception as exc:
            logger.debug(f"hud notices failed: {exc}")
            return []

    def activity(self, n: int = 6) -> list[str]:
        try:
            return parsers.tail_log(parsers.today_raw_path(self._vault), n)
        except Exception as exc:
            logger.debug(f"hud activity failed: {exc}")
            return []

    # ── permission asks (the SAME durable asks as phone/web/CLI) ────

    def pending_asks(self) -> list[dict]:
        """Open durable permission asks (AutonomyAgent.pending)."""
        try:
            from ..autonomy import AutonomyAgent
            return AutonomyAgent(conn=self._conn).pending(limit=10) or []
        except Exception as exc:
            logger.debug(f"hud pending asks failed: {exc}")
            return []

    def allow(self, request_id: str) -> Optional[dict]:
        """The operator approved an ask — run it through the real gate."""
        try:
            from ..autonomy import AutonomyAgent
            outcome = AutonomyAgent(conn=self._conn).accept(request_id)
            if outcome:
                self.push(f"⛔ allowed: {request_id} → "
                          f"{outcome.get('status', '?')}")
            return outcome
        except Exception as exc:
            logger.warning(f"hud allow failed: {exc}")
            self.push(f"⛔ allow failed: {exc}")
            return None

    def deny(self, request_id: str) -> bool:
        """The operator declined — resolve the ask + record an override."""
        try:
            from ..autonomy import AutonomyAgent
            ok = AutonomyAgent(conn=self._conn).deny(
                request_id, reason="operator declined (HUD)")
            if ok:
                self.push(f"⛔ denied: {request_id}")
            return ok
        except Exception as exc:
            logger.warning(f"hud deny failed: {exc}")
            return False

    # ── FTS search (Wave 6) ─────────────────────────────────────────

    def search(self, terms: str, limit: int = 20) -> tuple[list[str], str]:
        """Vault search — FTS index first, grep fallback (cache, not truth).

        The same ONE search code path as ``friday6 vault find``, surfaced
        in the HUD: ``("index", hits)`` when the FTS cache answered,
        ``("grep", hits)`` otherwise. Never raises — a broken index or
        vault degrades to an honest empty result.
        """
        try:
            hits, source = self._vault.search_with_source(terms, limit)
            return hits or [], source or "grep"
        except Exception as exc:
            logger.debug(f"hud search failed: {exc}")
            return [], "grep"

    # ── vitals (pure; psutil optional) ──────────────────────────────

    def vitals(self) -> str:
        from .vitals import format_vitals
        return format_vitals(*_read())


__all__ = ["HudController"]
