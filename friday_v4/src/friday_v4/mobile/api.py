"""Mobile companion API — the phone as another surface of the same Friday (Wave 15/7).

A pure-stdlib ``http.server`` that also serves the companion PWA (the
phone app itself, ``app/``) and exposes the SAME brain and the SAME
durable queue as every other surface:

    GET  /                  → the companion PWA (app shell + manifest +
                               service worker + icons — installable)
    GET  /api/status        → transport health + shared-thread summary
    GET  /api/conversation  → today's shared session exchanges (one
                               presence — the terminal/web conversation
                               continues on the phone)
    POST /api/talk          → an utterance through the ONE NLU point
                               (nl_router — same brain as talk/voice/web)
    GET  /api/events        → SSE stream over the durable ambient queue
                               (replay since a `since` cursor — the push
                               transport; a reconnecting phone misses
                               nothing)
    POST /api/devices/register · touch · DELETE /api/devices/<id>
                           → pairing + liveness

Design:
- Pure stdlib (ThreadingHTTPServer), local-network by default.
- Every accessor is guarded — a missing DB renders empty/neutral
  payloads, never a 500 (the never-crash law).
- Static app files are served from a fixed allowlist (no path
  traversal), and a missing ``app/`` dir degrades to an API-only
  server — the API never depends on the UI.
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

#: The companion PWA lives next to this module (``app/``).
_APP_DIR = Path(__file__).resolve().parent / "app"

#: Fixed allowlist of servable app files → content type. Only these
#: names are ever read from disk — no traversal, no surprises.
_APP_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "application/javascript; charset=utf-8"),
    "manifest.json": ("manifest.json", "application/manifest+json; charset=utf-8"),
    "service-worker.js": ("service-worker.js", "application/javascript; charset=utf-8"),
    "icon-192.png": ("icon-192.png", "image/png"),
    "icon-512.png": ("icon-512.png", "image/png"),
    "apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "favicon.ico": ("icon-192.png", "image/png"),
}


class MobileAPI:
    """Read-only + talk accessors for the companion (guarded, never raise)."""

    def __init__(self, db_path=None, token: Optional[str] = None):
        self._db_path = db_path
        #: Optional bearer token for the API. When set, every ``/api/*``
        #: route requires ``Authorization: Bearer <token>`` (or
        #: ``?token=<token>`` — the SSE stream can't set headers via
        #: EventSource). This is what makes exposing Friday over a
        #: public tunnel safe: the PWA is just a shell, the API is the
        #: power, and the token gates the power. Never set → open on
        #: the LAN (the default, unchanged behavior).
        self._token = token or None

    def _conn(self):
        from .. import db
        return db.connect(path=self._db_path)

    def authorized(self, headers: dict, query: dict = None) -> bool:
        """Whether a request may touch the API (token gate).

        With no token configured the API is open (LAN default). With a
        token, either ``Authorization: Bearer <token>`` or the
        ``token`` query param (for EventSource) must match.
        """
        if not self._token:
            return True
        auth = str(headers.get("Authorization") or "")
        if auth == f"Bearer {self._token}":
            return True
        query = query or {}
        if (query.get("token") or [""])[0] == self._token:
            return True
        return False

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

    # ── devices (Wave 7 pairing) ────────────────────────────────────

    def register_device(self, code: str, token: str, *,
                        platform: str = "unknown",
                        name: str = "") -> dict:
        """Pair a phone: verify the one-time code, bind the push token."""
        try:
            from .pairing import PairingService
            service = PairingService(db_path=self._db_path)
            device_id = service.register(code, token, platform=platform,
                                         name=name)
        except Exception as exc:
            logger.debug(f"device register failed: {exc}")
            return {"ok": False, "error": "pairing unavailable"}
        if not device_id:
            return {"ok": False,
                    "error": "invalid or expired pairing code"}
        return {"ok": True, "device_id": device_id}

    def devices(self) -> dict:
        """Paired devices — token omitted (the API never leaks it)."""
        try:
            from .pairing import PairingService
            service = PairingService(db_path=self._db_path)
            rows = service.devices()
        except Exception as exc:
            logger.debug(f"device list failed: {exc}")
            return {"devices": []}
        clean = [{"id": r["id"], "platform": r["platform"],
                  "name": r["name"], "created_at": r["created_at"],
                  "last_seen": r["last_seen"]}
                 for r in rows]
        return {"devices": clean}

    def remove_device(self, device_id: str) -> dict:
        """Unpair a phone by device id."""
        try:
            from .pairing import PairingService
            service = PairingService(db_path=self._db_path)
            removed = service.remove(device_id)
        except Exception as exc:
            logger.debug(f"device remove failed: {exc}")
            return {"ok": False, "error": "pairing unavailable"}
        return {"ok": removed, "removed": removed}

    def touch_device(self, device_id: str) -> dict:
        """Record that a paired phone is alive (updates ``last_seen``)."""
        try:
            from .. import db
            conn = self._conn()
            try:
                # touch_device is best-effort (None); be honest by
                # reporting whether the device actually exists.
                exists = db.get_device(conn, device_id) is not None
                if exists:
                    db.touch_device(conn, device_id)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"device touch failed: {exc}")
            return {"ok": False, "error": "device registry unavailable"}
        return {"ok": exists, "touched": exists}

    # ── talk (same brain) ───────────────────────────────────────────

    def talk(self, text: str) -> dict:
        """Route an utterance through the ONE NLU point (nl_router).

        The phone speaks to the same Friday as ``friday4 talk``, voice,
        and the web dashboard — one command language everywhere. The
        exchange lands in the shared session (one presence).

        Desktop control rides along too: the companion server runs ON
        the operator's PC, so "open brave" from the phone's Chat tab
        focuses/launches Brave here (the same ``desktop_text_command``
        the CLI and web dashboard use). Never raises — an unavailable
        desktop degrades to an honest message.

        Wave 22 — CLAUDE: bridge: a message starting ``CLAUDE:`` is
        forwarded to one persistent Claude Code session instead of the
        NL router (the session keeps context until ``CLAUDE END``);
        tool-permission asks surface in the PWA via the durable
        permission flow. The rest of the utterance space is unchanged.
        """
        stripped = (text or "").strip()
        if stripped.upper().startswith("CLAUDE:") or \
                stripped.upper() == "CLAUDE END":
            return self._agent_talk(stripped)
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
                # Same desktop handler as friday4 talk / the web chat —
                # the phone controls the PC it is paired to.
                desktop_handler = None
                try:
                    from ..desktop.wm_abstraction import \
                        desktop_text_command
                    desktop_handler = desktop_text_command
                except Exception:
                    desktop_handler = None
                # durable_ask=True: the phone can't prompt interactively
                # (no terminal y/N), so a CONFIRM action becomes a
                # DURABLE permission ask the operator answers from the
                # PWA's inline Yes/No buttons ("yes, run it" / "no").
                result = TextCommandHandler(conn, llm=llm,
                                            desktop_handler=desktop_handler
                                            ).handle(text, force=False,
                                                     durable_ask=True)
            finally:
                conn.close()
            return result.to_dict()
        except Exception as exc:
            logger.debug(f"mobile talk failed: {exc}")
            return {"action": "failed", "response": f"Sorry: {exc}"}

    def _agent_talk(self, text: str) -> dict:
        """CLAUDE: bridge routing (never raises).

        ``CLAUDE: <prompt>`` forwards to the persistent session;
        ``CLAUDE END`` closes it. The bridge publishes progress onto
        the ambient bus (the PWA Live feed shows Claude working); the
        reply here is the acknowledgment + the durable request_id when
        a tool ask is pending.
        """
        try:
            from ..agent import get_bridge, is_claude_message, is_claude_end
            bridge = get_bridge(db_path=self._db_path)
            if is_claude_end(text):
                result = bridge.end()
            else:
                result = bridge.send(text)
            out = {"action": "agent", "intent": "agent",
                   "response": result.get("response", ""),
                   "status": "succeeded" if result.get("ok") else "failed"}
            return out
        except Exception as exc:
            logger.debug(f"agent talk failed: {exc}")
            return {"action": "failed", "intent": "agent",
                    "response": f"The Claude bridge errored: {exc}",
                    "status": "failed"}

    def agent_status(self) -> dict:
        """Bridge session state for the PWA badge (never raises)."""
        try:
            from ..agent import get_bridge
            return get_bridge(db_path=self._db_path).status()
        except Exception as exc:
            logger.debug(f"agent status failed: {exc}")
            return {"available": False, "active": False, "busy": False}


class _MobileHandler(BaseHTTPRequestHandler):
    """Serves the companion API (bind the shared MobileAPI + bus)."""

    server_version = "FridayV4-Mobile/1.0"

    #: Shared by all handler threads (bound by create_api_server).
    api: MobileAPI = MobileAPI()

    #: Where the companion PWA lives (bound by create_api_server).
    app_dir: Path = _APP_DIR

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

    def _require_api_auth(self, query=None) -> bool:
        """Gate one /api/* request; writes the 401 itself on failure.

        Static app files are NOT gated (the PWA shell must load so the
        operator can enter the token) — only the API is the power.
        """
        if self.api.authorized(self.headers, query):
            return True
        self._json({"error": "unauthorized", "hint": "set the "
                    "friday companion token on the Status tab"},
                   status=401)
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/status":
            if not self._require_api_auth(query):
                return
            self._json(self.api.status())
        elif path == "/api/conversation":
            if not self._require_api_auth(query):
                return
            self._json(self.api.conversation())
        elif path == "/api/devices":
            if not self._require_api_auth(query):
                return
            self._json(self.api.devices())
        elif path == "/api/agent/status":
            if not self._require_api_auth(query):
                return
            self._json(self.api.agent_status())
        elif path == "/api/events":
            if not self._require_api_auth(query):
                return
            self._stream_events(query)
        else:
            # Everything else is a candidate static app file (the PWA).
            name = path.lstrip("/")
            if name in _APP_FILES:
                self._serve_app_file(name)
            else:
                self._json({"error": "not found"}, status=404)

    def _serve_app_file(self, name: str) -> None:
        """Serve one fixed PWA file from the app dir (guarded).

        ``name`` is a key of ``_APP_FILES`` (allowlisted — a request
        can never name an arbitrary path on disk). A missing app dir
        degrades to a 404, never a crash (the never-crash law): the
        API keeps working even if the UI files were removed.
        """
        filename, ctype = _APP_FILES[name]
        try:
            body = (self.app_dir / filename).read_bytes()
        except OSError as exc:
            logger.debug(f"app file {filename} missing: {exc}")
            self._json({"error": "app unavailable"}, status=404)
            return
        self._send(200, body, ctype)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/talk":
            if not self._require_api_auth(query):
                return
            text = self._body_text()
            if not text:
                self._json({"action": "failed", "response": "no text sent"},
                           status=400)
                return
            self._json(self.api.talk(text))
        elif path == "/api/devices/register":
            if not self._require_api_auth(query):
                return
            body = self._body_json()
            result = self.api.register_device(
                str(body.get("code") or ""),
                str(body.get("token") or ""),
                platform=str(body.get("platform") or "unknown"),
                name=str(body.get("name") or ""))
            self._json(result, status=200 if result.get("ok") else 401)
        elif path == "/api/devices/touch":
            if not self._require_api_auth(query):
                return
            body = self._body_json()
            self._json(self.api.touch_device(str(body.get("device_id") or "")))
        else:
            self._json({"error": "not found"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        prefix = "/api/devices/"
        if path.startswith(prefix):
            if not self._require_api_auth(query):
                return
            device_id = path[len(prefix):].strip("/")
            if not device_id:
                self._json({"error": "device id required"}, status=400)
                return
            self._json(self.api.remove_device(device_id))
        else:
            self._json({"error": "not found"}, status=404)

    def _body_text(self) -> str:
        try:
            body = self._body_json()
            if body.get("text"):
                return str(body["text"]).strip()
        except (ValueError, json.JSONDecodeError):
            pass
        return ""

    def _body_json(self) -> dict:
        """Parse the request body as a JSON object ({} on failure)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length))
                if isinstance(body, dict):
                    return body
        except (ValueError, json.JSONDecodeError):
            pass
        return {}

    # ── SSE (durable-queue push) ────────────────────────────────────

    def _stream_events(self, query: dict,
                       poll_interval: float = 1.0) -> None:
        """Server-Sent Events over the durable ambient queue.

        The phone's push transport: every event the daemon/security/
        suggestions publish is streamed with an auto-increment ``id``
        so a reconnecting client replays what it missed via ``since``.
        """
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
                      db_path=None,
                      app_dir: Optional[Path] = None,
                      token: Optional[str] = None) -> ThreadingHTTPServer:
    """Build the companion server: the PWA + the API (caller runs
    ``serve_forever``). Pass ``app_dir`` to override where the app is
    served from (tests inject a tmp dir; the default is the packaged
    ``app/`` next to this module). Pass ``token`` to gate every
    ``/api/*`` route behind ``Authorization: Bearer <token>`` (or the
    ``?token=`` query param) — required before exposing Friday over a
    public tunnel; the static PWA shell stays public so the operator
    can enter the token."""
    _MobileHandler.api = MobileAPI(db_path=db_path, token=token)
    _MobileHandler.app_dir = Path(app_dir) if app_dir else _APP_DIR
    server = ThreadingHTTPServer((host, port), _MobileHandler)
    server.daemon_threads = True
    return server


__all__ = ["MobileAPI", "create_api_server"]
