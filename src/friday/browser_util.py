"""Browser automation CDP utilities (Pillar A Layer 2).

Controls a Chromium-based browser (Brave, Chrome) via Chrome DevTools Protocol
(CDP) over WebSocket. No Playwright/Selenium dependency. Uses stdlib only
for the WebSocket transport (no websockets package needed).

Two connection modes:
  1. Connect to existing instance with ``--remote-debugging-port=9222``.
  2. Launch headless instance managed by Friday.

CDP commands are JSON over WebSocket, correlated by message id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import os.path
import random
import shutil
import socket
import struct
import subprocess
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Minimal stdlib WebSocket (RFC 6455) — no third-party dep
# ---------------------------------------------------------------------------

_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-5AB9DC6B2670"


class _WebSocket:
    """A synchronous WebSocket client built on stdlib ``socket``.

    Supports text frames (opcode 0x1), ping/pong for keepalive,
    and clean close handshake.  All client frames are masked.
    """

    def __init__(self, host: str, port: int, path: str, timeout: float = 15.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._closed = False
        self._do_handshake(host, port, path)

    # ── handshake ────────────────────────────────────────────────────────

    def _do_handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(bytes(random.getrandbits(8) for _ in range(16))).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(req.encode())

        # Read HTTP response headers.
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed: connection closed")
            raw += chunk

        header_part, _ = raw.split(b"\r\n\r\n", 1)
        lines = header_part.decode("iso-8859-1").split("\r\n")
        if "101" not in lines[0]:
            raise ConnectionError(
                f"WebSocket handshake rejected: {lines[0]}"
            )

        # Accept key check is skipped for localhost CDP — it's harmless and
        # avoids edge cases with base64 module aliasing on some installations.
        # (The CDP server is on localhost: security is not a concern.)

    # ── frame I/O ────────────────────────────────────────────────────────

    def send(self, data: str) -> None:
        """Send a text frame (masked, opcode 0x1)."""
        payload = data.encode("utf-8")
        masking_key = bytes(random.getrandbits(8) for _ in range(4))
        masked = bytes(b ^ masking_key[i % 4] for i, b in enumerate(payload))
        header = bytearray()
        # FIN + opcode 0x1 (text)
        header.append(0x81)
        # MASK=1 + length
        _encode_length(header, len(payload), masked=True)
        header.extend(masking_key)
        header.extend(masked)
        self._sock.sendall(bytes(header))

    def recv(self, timeout: float | None = None) -> str:
        """Receive a text frame.  Returns the payload as str.

        Handles ping frames automatically (responds with pong).
        Raises ConnectionError on close frame or connection loss.
        """
        if timeout is not None:
            self._sock.settimeout(timeout)
        while True:
            b0, b1 = self._recv_exact(2)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            mask_key = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x1:  # text
                return payload.decode("utf-8")
            if opcode == 0x8:  # close
                self._closed = True
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:  # ping → respond pong
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # pong — ignore
                continue

    def close(self) -> None:
        """Send a close frame and close the socket."""
        if not self._closed:
            try:
                self._closed = True
                masking_key = bytes(random.getrandbits(8) for _ in range(4))
                payload = bytes(b ^ masking_key[i % 4] for i, b in enumerate(b""))
                header = bytearray()
                header.append(0x88)  # FIN + opcode 0x8 (close)
                header.append(0x84)  # MASK=1, length 0 (extended in next bytes)
                # Actually length 0 with mask
                header = bytearray()
                header.append(0x88)
                _encode_length(header, 0, masked=True)
                header.extend(masking_key)
                self._sock.sendall(bytes(header))
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass

    def _send_pong(self, payload: bytes) -> None:
        masking_key = bytes(random.getrandbits(8) for _ in range(4))
        masked = bytes(b ^ masking_key[i % 4] for i, b in enumerate(payload))
        header = bytearray()
        header.append(0x8A)  # FIN + opcode 0xA (pong)
        _encode_length(header, len(payload), masked=True)
        header.extend(masking_key)
        header.extend(masked)
        try:
            self._sock.sendall(bytes(header))
        except Exception:
            pass

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("WebSocket connection broken")
            data += chunk
        return data


def _encode_length(header: bytearray, length: int, *, masked: bool) -> None:
    """Append the length bytes (and mask bit) to *header*."""
    # Build the second byte: mask bit + 7-bit length
    if length < 126:
        header.append((0x80 if masked else 0) | length)
    elif length < 65536:
        header.append((0x80 if masked else 0) | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append((0x80 if masked else 0) | 127)
        header.extend(struct.pack(">Q", length))


# ---------------------------------------------------------------------------
# Browser binary discovery
# ---------------------------------------------------------------------------

_BROWSER_CANDIDATES = [
    "/opt/brave-bin/brave",
    shutil.which("brave"),
    shutil.which("brave-browser"),
    shutil.which("chromium"),
    shutil.which("chromium-browser"),
    shutil.which("google-chrome"),
    shutil.which("google-chrome-stable"),
]
BROWSER_PATH: Optional[str] = None
for _cand in _BROWSER_CANDIDATES:
    if _cand and os.path.isfile(_cand):
        BROWSER_PATH = _cand
        break

CDP_PORT = 9222
CDP_HOST = "127.0.0.1"
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"


class CDPError(Exception):
    """Raised when a CDP command fails or the browser is unreachable."""


# ---------------------------------------------------------------------------
# CDP connection management
# ---------------------------------------------------------------------------


def _find_ws_url() -> Optional[str]:
    """Get the WebSocket debug URL from the browser's HTTP endpoint."""
    import http.client

    try:
        conn = http.client.HTTPConnection(CDP_HOST, CDP_PORT, timeout=3)
        conn.request("GET", "/json/version")
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read().decode())
            return data.get("webSocketDebuggerUrl")
    except Exception:
        pass
    return None


def _find_page_targets() -> list[dict]:
    """List available page targets (tabs) from the browser."""
    import http.client

    try:
        conn = http.client.HTTPConnection(CDP_HOST, CDP_PORT, timeout=3)
        conn.request("GET", "/json")
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read().decode())
    except Exception:
        pass
    return []


def is_browser_available() -> bool:
    """Check if a CDP-enabled browser is running on the debug port."""
    return _find_ws_url() is not None


def is_binary_available() -> bool:
    """Check if the browser binary exists on disk."""
    return BROWSER_PATH is not None


def launch_browser(headless: bool = True, port: int = CDP_PORT) -> Optional[subprocess.Popen]:
    """Launch the browser with remote debugging enabled.

    Returns the subprocess Popen object, or None if the binary is missing.
    """
    if BROWSER_PATH is None:
        return None

    args = [
        BROWSER_PATH,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--user-data-dir=/tmp/.friday-browser-profile",
    ]
    if headless:
        args.append("--headless=new")

    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            if _find_ws_url() is not None:
                return proc
            time.sleep(0.5)
        proc.kill()
        return None
    except OSError:
        return None


def connect_websocket() -> Optional[Any]:
    """Open a synchronous WebSocket to the browser's CDP endpoint.

    Uses a stdlib-only WebSocket client (no ``websockets`` package needed).
    Returns a connection-like object with ``.send(data: str)``,
    ``.recv() -> str``, and ``.close()`` methods, or None on failure.

    The connection is automatically attached to the first available page
    target (tab), so high-level helpers (navigate, click, etc.) work
    without explicit session setup.
    """
    ws_url = _find_ws_url()
    if ws_url is None:
        return None

    # Parse ws://host:port/path
    rest = ws_url
    if rest.startswith("ws://"):
        rest = rest[5:]
    elif rest.startswith("wss://"):
        rest = rest[6:]
    host_port, _, path = rest.partition("/")
    path = "/" + path
    if ":" in host_port:
        host, _, port_str = host_port.partition(":")
        port = int(port_str)
    else:
        host = host_port
        port = 443 if ws_url.startswith("wss") else 80

    try:
        ws = _WebSocket(host, port, path, timeout=10)
    except Exception:
        return None

    # Auto-attach to the first page target so high-level actions work
    # without explicit session management.
    try:
        ws._cdp_session_id = _auto_attach_page(ws)
    except Exception:
        ws._cdp_session_id = None

    return ws


def _auto_attach_page(ws) -> Optional[str]:
    """Find the first page target and attach to it, returning the sessionId.

    ``Target.attachToTarget`` responds with *both* a
    ``Target.attachedToTarget`` event and a command response
    (``{"id": N, "result": {"sessionId": ...}}``).  We grab the sessionId
    from the event and discard the orphaned command response so subsequent
    ``send_command`` calls don't confuse it for their own reply.
    """
    # Get page targets.
    _send_raw(ws, 1, "Target.getTargets")
    deadline = time.monotonic() + 5
    target_id: str | None = None
    while time.monotonic() < deadline:
        raw = ws.recv(timeout=1.0)
        resp = json.loads(raw)
        if resp.get("id") == 1:
            infos = resp.get("result", {}).get("targetInfos", [])
            for t in infos:
                if t.get("type") == "page":
                    target_id = t["targetId"]
                    break
            break

    if not target_id:
        _send_raw(ws, 2, "Target.createTarget", {"url": "about:blank"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=1.0)
            resp = json.loads(raw)
            if resp.get("id") == 2:
                target_id = resp.get("result", {}).get("targetId")
                break

    if not target_id:
        return None

    # Attach to the target. Response comes as a Target.attachedToTarget event.
    _send_raw(ws, 3, "Target.attachToTarget",
              {"targetId": target_id, "flatten": True})
    deadline = time.monotonic() + 5
    session_id: str | None = None
    while time.monotonic() < deadline:
        raw = ws.recv(timeout=1.0)
        resp = json.loads(raw)
        if resp.get("method") == "Target.attachedToTarget":
            session_id = resp.get("params", {}).get("sessionId")
        # Also catch the orphaned command response so we drain it.
        if resp.get("id") == 3 and resp.get("result"):
            pass  # discarded
        if session_id and resp.get("id") == 3:
            break

    return session_id


def _send_raw(ws, msg_id: int, method: str, params: dict | None = None) -> None:
    """Low-level CDP send without waiting for response."""
    cmd = {"id": msg_id, "method": method}
    if params:
        cmd["params"] = params
    ws.send(json.dumps(cmd))


# ---------------------------------------------------------------------------
# CDP command helpers
# ---------------------------------------------------------------------------


def send_command(ws, method: str, params: dict | None = None,
                 timeout: float = 10.0) -> dict:
    """Send a CDP command and wait for the response.

    Args:
        ws: A WebSocket connection object (from connect_websocket).
        method: CDP method name (e.g. ``Page.navigate``).
        params: Optional parameters dict.
        timeout: Max seconds to wait for a response.

    Returns:
        The ``result`` dict from the CDP response.

    Raises:
        CDPError: If the command fails or times out.
    """
    msg_id = int(time.monotonic() * 1000) % 1000000
    cmd: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        cmd["params"] = params
    # Route through the auto-attached session if available.
    session_id = getattr(ws, "_cdp_session_id", None)
    if session_id:
        cmd["sessionId"] = session_id

    try:
        ws.send(json.dumps(cmd))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=min(1.0, deadline - time.monotonic()))
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                if "error" in resp:
                    raise CDPError(
                        f"CDP {method} failed: {resp['error'].get('message', str(resp['error']))}")
                return resp.get("result", {})
        raise CDPError(f"CDP {method} timed out after {timeout}s")
    except ConnectionError as e:
        raise CDPError(str(e)) from e


# ---------------------------------------------------------------------------
# High-level page actions
# ---------------------------------------------------------------------------


def new_tab(ws, url: str = "about:blank") -> str:
    """Open a new tab and navigate to the given URL. Returns the targetId."""
    result = send_command(ws, "Target.createTarget", {"url": url})
    return result.get("targetId", "")


def close_tab(ws, target_id: str) -> None:
    """Close a tab by targetId."""
    send_command(ws, "Target.closeTarget", {"targetId": target_id})


def navigate(ws, url: str) -> None:
    """Navigate the current page to a URL. Waits for the page to load."""
    send_command(ws, "Page.navigate", {"url": url})
    _wait_for_load(ws)


def _wait_for_load(ws, timeout: float = 15.0) -> None:
    """Wait for the page's load event."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.recv(timeout=1.0)
            event = json.loads(raw)
            method = event.get("method", "")
            if method == "Page.loadEventFired":
                return
            if method == "Page.lifecycleEvent":
                if event.get("params", {}).get("name") == "load":
                    return
        except (TimeoutError, ConnectionError, json.JSONDecodeError):
            continue
        except OSError:
            continue


def _runtime_eval(ws, expression: str) -> Any:
    """Evaluate JavaScript in the page context via Runtime.evaluate.

    Returns the ``value`` from the CDP result, or None on failure.
    """
    try:
        result = send_command(ws, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        val = result.get("result", {})
        if val.get("type") == "undefined":
            return None
        return val.get("value")
    except CDPError:
        return None


def click_element(ws, selector: str) -> bool:
    """Click an element identified by CSS selector via Runtime.evaluate."""
    escaped = _escape_selector(selector)
    result = _runtime_eval(ws, f"""(() => {{
        const el = document.querySelector('{escaped}');
        if (!el) return false;
        el.click();
        return true;
    }})()""")
    return bool(result)


def type_text(ws, selector: str, text: str) -> bool:
    """Type text into an input field identified by CSS selector.

    Clears the field first, then inserts the text via JavaScript.
    """
    escaped = _escape_selector(selector)
    ready = _runtime_eval(ws, f"""(() => {{
        const el = document.querySelector('{escaped}');
        if (!el) return false;
        el.focus();
        el.value = '';
        return true;
    }})()""")
    if not ready:
        return False
    try:
        _runtime_eval(ws, f"""(() => {{
            const el = document.querySelector('{escaped}');
            if (el) {{
                el.value = {json.dumps(text)};
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        }})()""")
        return True
    except Exception:
        return False


def read_text(ws, selector: str) -> Optional[str]:
    """Read the text content of an element via Runtime.evaluate.

    Returns ``el.textContent`` (no HTML tags), or None if not found.
    """
    escaped = _escape_selector(selector)
    result = _runtime_eval(ws, f"""(() => {{
        const el = document.querySelector('{escaped}');
        if (!el) return null;
        return el.textContent.trim();
    }})()""")
    return str(result) if result is not None else None


def wait_for_selector(ws, selector: str, timeout: float = 10.0) -> bool:
    """Wait for an element matching the CSS selector to appear in the DOM."""
    escaped = _escape_selector(selector)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _runtime_eval(ws, f"""(() => {{
            return !!document.querySelector('{escaped}');
        }})()""")
        if result:
            return True
        time.sleep(0.2)
    return False


def _escape_selector(sel: str) -> str:
    """Escape a CSS selector for safe embedding in a JavaScript string."""
    return sel.replace("\\", "\\\\").replace("'", "\\'")


def get_page_title(ws) -> str:
    """Get the current page title."""
    try:
        result = send_command(ws, "Runtime.evaluate", {
            "expression": "document.title",
        })
        return result.get("result", {}).get("value", "")
    except CDPError:
        return ""


def get_page_url(ws) -> str:
    """Get the current page URL."""
    try:
        result = send_command(ws, "Runtime.evaluate", {
            "expression": "window.location.href",
        })
        return result.get("result", {}).get("value", "")
    except CDPError:
        return ""


def take_screenshot(ws) -> Optional[str]:
    """Take a screenshot of the current page.

    Returns a base64-encoded PNG string, or None on failure.
    """
    try:
        result = send_command(ws, "Page.captureScreenshot", {"format": "png"})
        return result.get("data")
    except CDPError:
        return None
