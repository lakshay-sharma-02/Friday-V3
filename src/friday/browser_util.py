"""Browser automation CDP utilities (Pillar A Layer 2).

Provides a CDP (Chrome DevTools Protocol) connection to a Chromium-based
browser (Brave, Chrome, Edge). Uses raw websocket CDP — no Playwright or
Selenium dependency.

Two connection modes:
  1. **Connect to existing instance** — if Brave/Chrome is already running
     with ``--remote-debugging-port=9222``, connect via websocket.
  2. **Launch headless instance** — launch Brave in headless mode with
     remote debugging on port 9222.

CDP commands are sent as JSON over a websocket and responses are correlated
by message id. Common actions (navigate, click, type, read) are exposed as
high-level helpers.

Uses ``/opt/brave-bin/brave`` on this machine (resolved once at module load).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from typing import Any, Optional

import requests

# Resolved browser binary path. Brave is the primary target on this machine.
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


def _find_ws_url() -> Optional[str]:
    """Get the websocket debug URL from the browser's HTTP endpoint.

    Queries ``http://localhost:9222/json/version`` and returns the
    ``webSocketDebuggerUrl``. Returns None if the browser is not listening.
    """
    try:
        resp = requests.get(f"{CDP_URL}/json/version", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("webSocketDebuggerUrl")
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        pass
    return None


def _find_page_targets() -> list[dict]:
    """List available page targets (tabs) from the browser.

    Returns a list of target dicts with ``id``, ``title``, ``url``, and
    ``webSocketDebuggerUrl``.
    """
    try:
        resp = requests.get(f"{CDP_URL}/json", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return []


def is_browser_available() -> bool:
    """Check if a CDP-enabled browser is running on the debug port."""
    if BROWSER_PATH is None:
        return False
    url = _find_ws_url()
    return url is not None


def is_binary_available() -> bool:
    """Check if the browser binary exists on disk."""
    return BROWSER_PATH is not None


def launch_browser(headless: bool = True, port: int = CDP_PORT) -> Optional[subprocess.Popen]:
    """Launch the browser with remote debugging enabled.

    Args:
        headless: If True, launch in headless mode (no visible window).
        port: The CDP port to listen on.

    Returns:
        The subprocess Popen object, or None if the binary couldn't be found.
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
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for the browser to start listening.
        for _ in range(20):
            if _find_ws_url() is not None:
                return proc
            time.sleep(0.5)
        # Browser started but CDP not ready — kill and report failure.
        proc.kill()
        return None
    except OSError:
        return None


def connect_websocket() -> Optional[Any]:
    """Open a websocket connection to the browser's CDP endpoint.

    Returns a websocket connection object, or None if unavailable.
    Uses the browser's websocket debug URL.

    The caller is responsible for closing the connection.
    """
    import websockets

    ws_url = _find_ws_url()
    if ws_url is None:
        return None

    try:
        return websockets.sync.connect(ws_url, timeout=10)
    except Exception:
        return None


def send_command(ws, method: str, params: dict | None = None,
                 timeout: float = 10.0) -> dict:
    """Send a CDP command and wait for the response.

    Args:
        ws: An open websocket connection.
        method: CDP method name (e.g. ``Page.navigate``).
        params: Optional parameters dict.
        timeout: Max seconds to wait for a response.

    Returns:
        The ``result`` dict from the CDP response.

    Raises:
        CDPError: If the command fails or times out.
    """
    import websockets

    msg_id = int(time.monotonic() * 1000) % 1000000
    cmd = {"id": msg_id, "method": method}
    if params:
        cmd["params"] = params

    try:
        ws.send(json.dumps(cmd))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=deadline - time.monotonic())
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                if "error" in resp:
                    raise CDPError(
                        f"CDP {method} failed: {resp['error'].get('message', str(resp['error']))}")
                return resp.get("result", {})
        raise CDPError(f"CDP {method} timed out after {timeout}s")
    except websockets.exceptions.WebSocketException as e:
        raise CDPError(f"WebSocket error: {e}")


# ---------------------------------------------------------------------------
# High-level page actions
# ---------------------------------------------------------------------------

def new_tab(ws, url: str = "about:blank") -> str:
    """Open a new tab and navigate to the given URL.

    Returns the targetId of the new tab.
    """
    result = send_command(ws, "Target.createTarget", {"url": url})
    return result.get("targetId", "")


def close_tab(ws, target_id: str) -> None:
    """Close a tab by targetId."""
    send_command(ws, "Target.closeTarget", {"targetId": target_id})


def navigate(ws, url: str) -> None:
    """Navigate the current page to a URL. Waits for the page to load."""
    send_command(ws, "Page.navigate", {"url": url})
    # Wait for the page to finish loading.
    _wait_for_load(ws)


def _wait_for_load(ws, timeout: float = 15.0) -> None:
    """Wait for the page's ``Page.loadEventFired`` or ``lifecycleEvent/load``."""
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
        except TimeoutError:
            continue
        except (json.JSONDecodeError, OSError):
            continue


def _runtime_eval(ws, expression: str) -> Any:
    """Evaluate a JavaScript expression in the page context via Runtime.evaluate.

    Returns the ``value`` from the CDP result, or None on failure.
    This is the preferred way to interact with the DOM — avoids fragile
    nodeId management and works across navigations.
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
    """Click an element identified by CSS selector via Runtime.evaluate.

    Uses ``document.querySelector(sel).click()`` to trigger a native click
    event — no fragile box-model coordinate math needed. Returns True if
    the element was found and clicked.
    """
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

    Clears the field first by setting ``value = ''``, then types the text
    using ``Input.insertText`` (inserts at cursor, no per-character dispatch).
    Returns True if successful.
    """
    escaped = _escape_selector(selector)
    # Focus the element and clear its value.
    ready = _runtime_eval(ws, f"""(() => {{
        const el = document.querySelector('{escaped}');
        if (!el) return false;
        el.focus();
        el.value = '';
        return true;
    }})()""")
    if not ready:
        return False
    # Insert text directly (single CDP command, no per-character dispatch).
    try:
        # Use Runtime.evaluate to set the value directly — faster and more
        # reliable than per-character key dispatch. json.dumps handles escaping.
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

    Uses ``el.textContent`` which returns clean text without HTML tags,
    script/style content, or hidden elements. Returns None if not found.
    """
    escaped = _escape_selector(selector)
    result = _runtime_eval(ws, f"""(() => {{
        const el = document.querySelector('{escaped}');
        if (!el) return null;
        return el.textContent.trim();
    }})()""")
    return str(result) if result is not None else None


def wait_for_selector(ws, selector: str, timeout: float = 10.0) -> bool:
    """Wait for an element matching the CSS selector to appear in the DOM.

    Uses ``Runtime.evaluate`` to poll every 200ms. Returns True if found,
    False on timeout.
    """
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
    """Escape a CSS selector for safe embedding in a JavaScript string.

    Escapes single quotes and backslashes so the selector can be safely
    placed inside a single-quoted JavaScript string.
    """
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
