"""Mobile companion API — the phone as another surface of the same Friday (Wave 15).

A pure-stdlib ``http.server`` the companion app talks to, exposing the
SAME brain and the SAME durable queue as every other surface:

    GET  /api/status       → transport health + shared-thread summary
    GET  /api/conversation → today's shared session exchanges (one
                             presence — the terminal/web conversation
                             continues on the phone)
    POST /api/talk         → an utterance through the ONE NLU point
                             (nl_router — same brain as talk/voice/web)
    GET  /api/events       → SSE stream over the durable ambient queue
                             (replay since a `since` cursor — the push
                             transport; a reconnecting phone misses
                             nothing)

Design:
- Pure stdlib (ThreadingHTTPServer), local-network by default.
- Every accessor is guarded — a missing DB renders empty/neutral
  payloads, never a 500 (the never-crash law).
- No framework, no external deps — the companion just needs HTTP.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("friday_v4.mobile.api")


class MobileAPI:
    """Read-only + talk accessors for the companion (guarded, never raise)."""

    def __init__(self, db_path=None):
        self._db_path = db_path

    def _conn(self):
        from .. import db
        return db.connect(path=self._db_path)

    # ── status ──────────────────────────────────────────────────────

    def status(self) -> dict:
        """Transport health + shared-thread summary."""
        out: dict = {"available": True, "shared_session": None,
                     "exchanges_today": 0}
        try:
            conn = self._conn()
            try:
                from .. import db
                # Read-only lookup — a status probe must not create the
                # thread (it's born on first actual conversation).
                sid = db.find_shared_session(conn)
                if sid:
                    row = db.get_session(conn, sid)
                    out["shared_session"] = {
                        "id": sid,
                        "surface": (row or {}).get("surface"),
                        "started_at": (row or {}).get("started_at"),
                    }
                    out["exchanges_today"] = len(
                        db.session_exchanges(conn, sid, limit=100000))
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"mobile status failed: {exc}")
            out["available"] = False
            out["error"] = str(exc)
        return out

    # ── conversation (one presence) ─────────────────────────────────

    def conversation(self, limit: int = 40) -> dict:
        """Today's shared-session exchanges, oldest first (Wave 15).

        The phone reads the SAME thread the terminal/web/voice append
        to — a conversation started in the terminal continues here.
        """
        out: dict = {"available": False, "session_id": None, "exchanges": []}
        try:
            conn = self._conn()
            try:
                from .. import db
                # Read-only lookup — reading the conversation must not
                # create it. The thread exists once any surface talks.
                sid = db.find_shared_session(conn)
                out["session_id"] = sid
                rows = db.session_exchanges(conn, sid, limit=limit) if sid else []
                out["exchanges"] = [
                    {"role": r.get("role"), "content": r.get("content"),
                     "intent": r.get("intent", ""),
                     "created_at": r.get("created_at")}
                    for r in rows
                ]
                out["available"] = True
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"mobile conversation failed: {exc}")
        return out

    # ── talk (same brain) ───────────────────────────────────────────

    def talk(self, text: str) -> dict:
        """Route an utterance through the ONE NLU point (nl_router).

        The phone speaks to the same Friday as ``friday4 talk``, voice,
        and the web dashboard — one command language everywhere. The
        exchange lands in the shared session (one presence).
        """
        try:
            from ..nl_router import TextCommandHandler
            conn = self._conn()
            try:
                llm = None
                try:
                    from ..nlu import LLMClient
                    llm = LLMClient()
                except Exception:
                    llm = None
                result = TextCommandHandler(conn, llm=llm).handle(
                    text, force=False)
            finally:
                conn.close()
            return result.to_dict()
        except Exception as exc:
            logger.debug(f"mobile talk failed: {exc}")
            return {"action": "failed", "response": f"Sorry: {exc}"}


class _MobileHandler(BaseHTTPRequestHandler):
    """Serves the companion API (bind the shared MobileAPI + bus)."""

    server_version = "FridayV4-Mobile/1.0"

    #: Shared by all handler threads (bound by create_api_server).
    api: MobileAPI = MobileAPI()

    def log_message(self, fmt, *args):
        logger.debug(fmt, *args)

    # ── helpers ─────────────────────────────────────────────────────

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(),
                   "application/json")

    # ── routing ─────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(self.api.status())
        elif path == "/api/conversation":
            self._json(self.api.conversation())
        elif path == "/api/events":
            self._stream_events()
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/talk":
            text = self._body_text()
            if not text:
                self._json({"action": "failed", "response": "no text sent"},
                           status=400)
                return
            self._json(self.api.talk(text))
        else:
            self._json({"error": "not found"}, status=404)

    def _body_text(self) -> str:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length))
                if isinstance(body, dict) and body.get("text"):
                    return str(body["text"]).strip()
        except (ValueError, json.JSONDecodeError):
            pass
        return ""

    # ── SSE (durable-queue push) ────────────────────────────────────

    def _stream_events(self, poll_interval: float = 1.0) -> None:
        """Server-Sent Events over the durable ambient queue.

        The phone's push transport: every event the daemon/security/
        suggestions publish is streamed with an auto-increment ``id``
        so a reconnecting client replays what it missed via ``since``.
        """
        query = parse_qs(urlparse(self.path).query)
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        import sqlite3
        from .. import db as db_mod
        conn = None
        try:
            conn = db_mod.connect(self.api._db_path, read_only=True) \
                if self.api._db_path else db_mod.connect(read_only=True)
        except (sqlite3.Error, OSError):
            conn = None

        last_id = since
        last_heartbeat = time.monotonic()
        try:
            while True:
                events: list = []
                if conn is not None:
                    try:
                        events = db_mod.ambient_events_since(conn, last_id)
                    except sqlite3.Error:
                        events = []
                for ev in events:
                    rowid = int(ev.get("rowid") or 0)
                    if rowid <= last_id:
                        continue
                    last_id = rowid
                    data = json.dumps({
                        "id": rowid,
                        "topic": ev.get("topic"),
                        "payload": ev.get("payload"),
                        "priority": ev.get("priority"),
                        "source": ev.get("source"),
                        "created_at": ev.get("created_at"),
                    })
                    self.wfile.write(f"id: {rowid}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                if time.monotonic() - last_heartbeat >= 15.0:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(poll_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def create_api_server(host: str = "127.0.0.1", port: int = 8900,
                      db_path=None) -> ThreadingHTTPServer:
    """Build the companion API server (caller runs ``serve_forever``)."""
    _MobileHandler.api = MobileAPI(db_path=db_path)
    server = ThreadingHTTPServer((host, port), _MobileHandler)
    server.daemon_threads = True
    return server


__all__ = ["MobileAPI", "create_api_server"]
