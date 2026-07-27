"""Browser Action Worker (Pillar A Layer 2).

Controls a Chromium-based browser (Brave, Chrome) via Chrome DevTools Protocol
(CDP) over websockets. Handles page navigation, clicking, typing, reading text,
and waiting for elements — all verified by reading the DOM back after each action.

Uses raw CDP via websockets (no Playwright/Selenium dependency) and connects to
either an existing browser instance with ``--remote-debugging-port=9222``, or
launches a headless instance managed by Friday.

Payload format (JSON):
  {"action": "navigate", "target": "https://example.com"}     — navigate to URL
  {"action": "click",    "target": "css:button.submit"}        — click element
  {"action": "type",     "target": "css:input#search", "value":"query"} — type text
  {"action": "read",     "target": "css:.result"}              — read text content
  {"action": "wait",     "target": "css:.loaded"}              — wait for element
  {"action": "title"}                                          — get page title
  {"action": "url"}                                             — get current URL
  {"action": "screenshot"}                                      — capture screenshot

Read-only actions: read, title, url, screenshot.
Write actions: navigate, click, type, wait — require confirmation.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from .models import ExecutionResult, Executor, VerificationResult
from .confirm_gate import ActionLevel, get_action_level, prompt_confirm
from ..action_log import ActionEvent, log_action, now_iso as _now
from ..autonomy import record_action_outcome
from ..db import connect as _db_connect


def _payload(task) -> str:
    return getattr(task, "runtime_payload", "") or ""


# Actions that only read state — no confirmation needed.
_READ_ONLY_ACTIONS = frozenset({"read", "title", "url", "screenshot"})

# Actions that modify state — always require confirmation.
_WRITE_ACTIONS = frozenset({"navigate", "click", "type", "wait"})


class BrowserExecutor(Executor):
    """Dispatch browser automation actions via CDP.

    Connects to a running browser with remote debugging, or launches a
    headless instance. Each action is verified by reading the DOM after
    execution to confirm the expected effect.
    """

    def __init__(self, worker_id: str = "worker:browser",
                 launch_headless: bool = True) -> None:
        self.worker_id = worker_id
        self._launch_headless = launch_headless
        self._ws = None  # websocket connection, opened lazily

    def _ensure_connected(self) -> bool:
        """Ensure we have an active CDP connection. Launches browser if needed."""
        from ..browser_util import (
            is_browser_available, launch_browser, connect_websocket, send_command,
        )

        if self._ws is not None:
            try:
                # Quick liveness check: send a trivial CDP command directly.
                # We use send_command (not get_page_url or similar wrappers)
                # because those swallow exceptions — we need the raw raise
                # to know the connection is really dead.
                send_command(self._ws, "Runtime.evaluate",
                             {"expression": "1+1"})
                return True
            except Exception:
                self._ws = None

        # Try connecting to existing instance.
        ws = connect_websocket()
        if ws is not None:
            self._ws = ws
            return True

        # Launch headless instance.
        if self._launch_headless:
            proc = launch_browser(headless=True)
            if proc is not None:
                ws = connect_websocket()
                if ws is not None:
                    self._ws = ws
                    return True

        return False

    def _disconnect(self) -> None:
        """Close the websocket connection."""
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def execute(self, task) -> ExecutionResult:
        raw = _payload(task).strip()
        if not raw:
            self._autonomy_record("browser_parse", "", False, "empty payload")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=0,
                error="browser worker: empty payload",
            )

        # Parse action + target.
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            self._autonomy_record("browser_parse", "", False, "bad JSON")
            return ExecutionResult(
                success=False, stdout="", stderr=raw[:200],
                exit_code=None, duration_ms=0,
                error="browser worker: payload must be JSON",
            )

        action = (obj.get("action") or "").lower().strip()
        target = (obj.get("target") or "").strip()
        value = obj.get("value", "")
        if not action:
            self._autonomy_record("browser_parse", target or "", False, "no action field")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=0,
                error="browser worker: 'action' field is required",
            )

        t0 = time.monotonic()

        # Helper: log action outcome + record for autonomy escalation.
        def _record(success: bool, err: str = "") -> None:
            try:
                conn = _db_connect()
                status = "success" if success else "failure"
                log_action(conn, ActionEvent(
                    source="friday",
                    action_type="browser_" + action,
                    target=target,
                    detail=json.dumps({"action": action, "target": target,
                                       "value": str(value)[:200] if value else "",
                                       "status": status, "error": err}),
                    confidence="observed",
                    observed_at=_now(),
                ))
                record_action_outcome("browser_" + action, success, conn=conn)
                conn.close()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

        # --- CONFIRM GATE: block before any side-effecting action ---
        if not prompt_confirm(
            action=action,
            target=target,
            worker_id=self.worker_id,
            skip_prompt=False,
        ):
            _record(False, "cancelled by user")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"browser {action} {target}: cancelled by user",
            )

        # --- Connect to browser ---
        if not self._ensure_connected():
            _record(False, "could not connect to browser")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                error="browser worker: could not connect to browser (is Brave/Chrome "
                      "running with --remote-debugging-port=9222?)",
            )

        from ..browser_util import (
            navigate, click_element, type_text, read_text,
            wait_for_selector, get_page_title, get_page_url,
            take_screenshot, CDPError,
        )

        try:
            result = self._dispatch_action(
                action, target, value,
                navigate, click_element, type_text, read_text,
                wait_for_selector, get_page_title, get_page_url,
                take_screenshot,
            )
        except CDPError as e:
            _record(False, f"CDP error: {e}")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"browser CDP error: {e}",
            )
        except Exception as e:
            _record(False, f"{type(e).__name__}: {e}")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"browser worker error: {type(e).__name__}: {e}",
            )

        dur = int((time.monotonic() - t0) * 1000)

        if result["success"]:
            _record(True)
            stdout_parts = [f"browser {action}"]
            if result.get("output"):
                stdout_parts.append(f": {result['output'][:200]}")
            return ExecutionResult(
                success=True, stdout=" ".join(stdout_parts), stderr="",
                exit_code=0, duration_ms=dur,
            )
        else:
            err = result.get("error", f"browser {action} failed")
            _record(False, err)
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=dur,
                error=err,
            )

    def _dispatch_action(self, action, target, value,
                         navigate_fn, click_fn, type_fn, read_fn,
                         wait_fn, title_fn, url_fn, screenshot_fn) -> dict:
        """Dispatch a browser action. Returns dict with success + optional output."""
        ws = self._ws

        if action == "navigate":
            if not target:
                return {"success": False, "error": "browser navigate: target URL is required"}
            navigate_fn(ws, target)
            # Verify by reading the page title.
            page_title = title_fn(ws)
            return {"success": True, "output": f"Navigated to {target}, title: {page_title}"}

        elif action == "click":
            if not target:
                return {"success": False, "error": "browser click: target selector is required"}
            sel = _strip_css_prefix(target)
            if click_fn(ws, sel):
                return {"success": True, "output": f"Clicked {target}"}
            return {"success": False, "error": f"browser click: element '{target}' not found"}

        elif action == "type":
            if not target:
                return {"success": False, "error": "browser type: target selector is required"}
            sel = _strip_css_prefix(target)
            text = str(value) if value else ""
            if type_fn(ws, sel, text):
                return {"success": True, "output": f"Typed '{text[:50]}' into {target}"}
            return {"success": False, "error": f"browser type: element '{target}' not found"}

        elif action == "read":
            if not target:
                return {"success": False, "error": "browser read: target selector is required"}
            sel = _strip_css_prefix(target)
            text = read_fn(ws, sel)
            if text is not None:
                return {"success": True, "output": text[:500]}
            return {"success": False, "error": f"browser read: element '{target}' not found"}

        elif action == "wait":
            if not target:
                return {"success": False, "error": "browser wait: target selector is required"}
            sel = _strip_css_prefix(target)
            found = wait_fn(ws, sel)
            if found:
                return {"success": True, "output": f"Element '{target}' appeared"}
            return {"success": False, "error": f"browser wait: element '{target}' did not appear"}

        elif action == "title":
            page_title = title_fn(ws)
            return {"success": True, "output": page_title}

        elif action == "url":
            page_url = url_fn(ws)
            return {"success": True, "output": page_url}

        elif action == "screenshot":
            data = screenshot_fn(ws)
            if data:
                return {"success": True, "output": f"screenshot: {len(data)} bytes (base64)"}
            return {"success": False, "error": "browser screenshot failed"}

        else:
            return {"success": False, "error": f"browser worker: unknown action '{action}'"}

    @staticmethod
    def _autonomy_record(action_type: str, target: str, success: bool,
                         detail: str = "") -> None:
        """Standalone helper for logging outcomes at early return points
        before the `_record` closure is defined (parse errors, etc.)."""
        try:
            conn = _db_connect()
            status = "success" if success else "failure"
            log_action(conn, ActionEvent(
                source="friday",
                action_type=action_type,
                target=target,
                detail=json.dumps({"action": action_type, "target": target,
                                   "status": status, "error": detail}),
                confidence="observed",
                observed_at=_now(),
            ))
            record_action_outcome(action_type, success, conn=conn)
            conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        return VerificationResult(
            passed=result.success,
            reason="browser action completed" if result.success
            else result.error or "browser action failed",
        )

    def __del__(self):
        self._disconnect()


def _strip_css_prefix(selector: str) -> str:
    """Strip a 'css:' prefix from a selector if present."""
    s = selector.strip()
    for prefix in ("css:", "css ", "CSS:", "CSS "):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s
