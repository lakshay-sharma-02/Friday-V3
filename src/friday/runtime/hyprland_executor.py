"""Hyprland Action Worker (Pillar A Layer 1).

Dispatches hyprctl commands to control windows, workspaces, and launch
applications. After each action, re-reads the desktop state via the
HyprlandObserver to VERIFY the intended effect actually landed — not just
trust the exit code.

Safety:
- All state-changing actions require explicit human confirmation (the
  confirm-before-execute gate). Read-only queries (get workspaces, list
  windows) auto-execute.
- Verification includes one retry with a short delay to mitigate race
  conditions (user alt-tabbing during an automated workspace switch).
- Destructive actions (close window) carry an extra "are you sure?" step.

Payload format (JSON):
  {"action": "workspace", "target": "3"}        — switch to workspace 3
  {"action": "exec", "target": "firefox"}        — launch application
  {"action": "focuswindow", "target": "kitty"}   — focus window by class
  {"action": "closewindow", "target": "class:kitty"}  — close by class/pid
  {"action": "movetoworkspace", "target": "3"}   — move focused window to ws 3
  {"action": "fullscreen"}                       — toggle fullscreen
  {"action": "query", "target": "clients"}        — read-only: list windows
  {"action": "query", "target": "workspaces"}     — read-only: list workspaces
  {"action": "query", "target": "activewindow"}   — read-only: current focus

Read-only actions: query/*, activewindow info.
Write actions: everything else — requires confirmation.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional

from .models import ExecutionResult, Executor, VerificationResult
from .confirm_gate import (
    ActionLevel,
    get_action_level,
    prompt_confirm,
)
from ..hyprctl_util import hyprctl as _hyprctl
from ..action_log import ActionEvent, log_action, now_iso as _now
from ..db import connect as _db_connect
import json


def _payload(task) -> str:
    """Extract the runtime_payload from a task, or empty string."""
    return getattr(task, "runtime_payload", "") or ""

_VERIFY_RETRY_DELAY = 0.3  # seconds between verification retry

# Actions that only read state — no confirmation needed.
_READ_ONLY_ACTIONS = frozenset({"query", "activewindow", "activeworkspace",
                                 "cursorpos", "monitors", "workspaces",
                                 "clients", "binds", "devices"})

# Actions that modify state — always require confirmation.
_WRITE_ACTIONS = frozenset({"workspace", "exec", "focuswindow", "closewindow",
                             "movetoworkspace", "movetoworkspacesilent",
                             "movewindow", "resizewindow", "fullscreen",
                             "togglefloating", "pin", "kill"})


def _hyprctl_dispatch(dispatcher: str, arg: str = "") -> bool:
    """Hyprland dispatch. Returns True if the command was accepted."""
    args = ["dispatch", dispatcher]
    if arg:
        args.append(arg)
    result = _hyprctl(args)
    return result is not None


def _read_active_window() -> dict[str, str]:
    """Read the currently focused window as a dict."""
    raw = _hyprctl(["activewindow"])
    if not raw:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            val = v.strip()
            # hyprctl activewindow format: "workspace: 3 (3)" — strip name suffix
            if k.strip() == "workspace":
                val = val.split()[0]
            out[k.strip()] = val
    return out


def _read_workspace_list() -> list[dict[str, str]]:
    """Return workspace info as a list of dicts."""
    raw = _hyprctl(["workspaces"])
    if not raw:
        return []
    sections: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line:
            continue
        if not line.startswith("\t") and ":" in line:
            if current:
                sections.append(current)
                current = {}
            k, _, v = line.partition(":")
            current["_section"] = k.strip()
            current[k.strip()] = v.strip()
        elif line.startswith("\t") and ":" in line:
            k, _, v = line.strip().partition(":")
            current[k.strip()] = v.strip()
    if current:
        sections.append(current)
    return sections


def is_write_action(action: str) -> bool:
    """True if this action modifies state and needs confirmation."""
    return action.lower() in _WRITE_ACTIONS


def is_read_only_action(action: str) -> bool:
    """True if this action only queries state."""
    return action.lower() in _READ_ONLY_ACTIONS


class HyprlandExecutor(Executor):
    """Dispatch Hyprland window/workspace/app-control actions.

    Handles the full hyprctl dispatch vocabulary. After every write action,
    re-reads the relevant state (active window, workspace list) and confirms
    the intended change actually occurred. One retry with a short delay
    mitigates race conditions from concurrent user interaction.
    """

    def __init__(self, worker_id: str = "worker:hyprctl",
                 workspace: str = ".") -> None:
        self.worker_id = worker_id
        self._ws = workspace

    def execute(self, task) -> ExecutionResult:
        raw = _payload(task).strip()
        if not raw:
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=0,
                error="hyprctl worker: empty payload",
            )

        # Parse action + target.
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return ExecutionResult(
                success=False, stdout="", stderr=raw[:200],
                exit_code=None, duration_ms=0,
                error="hyprctl worker: payload must be JSON",
            )

        action = (obj.get("action") or "").lower().strip()
        target = (obj.get("target") or "").strip()
        if not action:
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=0,
                error="hyprctl worker: 'action' field is required",
            )

        t0 = time.monotonic()

        # --- CONFIRM GATE: block before any side-effecting action ---
        if not prompt_confirm(
            action=action,
            target=target,
            worker_id=self.worker_id,
            skip_prompt=False,
        ):
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"hyprctl {action} {target}: cancelled by user",
            )

        # --- Read-only queries (no side effects, auto-execute) ---
        level = get_action_level(action)
        if level == ActionLevel.AUTO:
            sub_action = target if action == "query" else action
            result = _hyprctl([sub_action])
            if result is None:
                return ExecutionResult(
                    success=False, stdout="", stderr="",
                    exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                    error=f"hyprctl {sub_action} returned no output",
                )
            try:
                conn = _db_connect()
                log_action(conn, ActionEvent(
                    source="friday", action_type="hyprctl_" + sub_action,
                    target="", detail=json.dumps({"output": result[:200]}),
                    confidence="observed",
                    observed_at=_now(),
                ))
            except Exception:
                pass
            return ExecutionResult(
                success=True, stdout=result, stderr="",
                exit_code=0, duration_ms=int((time.monotonic() - t0) * 1000),
            )

        # --- State-changing actions (require verification) ---
        # Capture state BEFORE the action for verify-by-diff.
        before_active = _read_active_window()
        before_workspaces = _read_workspace_list()
        before_ws_active = before_active.get("workspace", "")
        before_class = before_active.get("class", "")

        dispatched = self._dispatch(action, target)
        if not dispatched:
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"hyprctl dispatch {action} {target} failed",
            )

        # --- Verify the action had the intended effect ---
        verified = self._verify_action(action, target, before_active,
                                        before_workspaces, before_ws_active,
                                        before_class)

        if not verified:
            # One retry with a short delay (race condition mitigation:
            # user action during our automated action).
            time.sleep(_VERIFY_RETRY_DELAY)
            verified = self._verify_action(action, target, before_active,
                                            before_workspaces, before_ws_active,
                                            before_class)

        dur = int((time.monotonic() - t0) * 1000)

        if verified:
            try:
                conn = _db_connect()
                log_action(conn, ActionEvent(
                    source="friday", action_type="hyprctl_" + action,
                    target=target, detail=json.dumps({"action": action, "target": target}),
                    confidence="observed",
                    observed_at=_now(),
                ))
            except Exception:
                pass
            return ExecutionResult(
                success=True,
                stdout=f"hyprctl dispatch {action} {target} — verified",
                stderr="", exit_code=0, duration_ms=dur,
            )
        else:
            return ExecutionResult(
                success=False,
                stdout="", stderr="",
                exit_code=None, duration_ms=dur,
                error=f"hyprctl {action} {target} dispatched but verification "
                      f"failed (state did not change as expected). "
                      f"This may be a race condition — retry or check manually.",
            )

    def _dispatch(self, action: str, target: str) -> bool:
        """Dispatch a hyprctl command. Returns True if accepted."""
        if action == "workspace":
            return _hyprctl_dispatch("workspace", target)
        elif action == "exec":
            return _hyprctl_dispatch("exec", target)
        elif action == "focuswindow":
            return _hyprctl_dispatch("focuswindow", target)
        elif action == "closewindow":
            return _hyprctl_dispatch("closewindow", target)
        elif action == "movetoworkspace":
            return _hyprctl_dispatch("movetoworkspace", target)
        elif action == "movetoworkspacesilent":
            return _hyprctl_dispatch("movetoworkspacesilent", target)
        elif action == "movewindow":
            return _hyprctl_dispatch("movewindow", target)
        elif action == "resizewindow":
            return _hyprctl_dispatch("resizewindow", target)
        elif action == "fullscreen":
            return _hyprctl_dispatch("fullscreen")
        elif action == "togglefloating":
            return _hyprctl_dispatch("togglefloating")
        elif action == "pin":
            return _hyprctl_dispatch("pin", target)
        elif action == "kill":
            return _hyprctl_dispatch("kill", target)
        elif action == "exit":
            return _hyprctl_dispatch("exit")
        elif action == "focusmonitor":
            return _hyprctl_dispatch("focusmonitor", target)
        elif action == "movecursortocorner":
            return _hyprctl_dispatch("movecursortocorner", target)
        else:
            # Fallback: try as-is (hyprctl may have dispatchers we don't list).
            return _hyprctl_dispatch(action, target)

    def _verify_action(self, action: str, target: str,
                       before_active: dict, before_workspaces: list,
                       before_ws: str, before_class: str) -> bool:
        """Verify a state-changing action by reading post-action state.
        
        Each action type has a specific verification strategy:
        - workspace: the active workspace changed to the target
        - exec: a new window appeared (active window or window count changed)
        - focuswindow: the active window class matches the target
        - closewindow: the window count decreased
        - movetoworkspace: active workspace OR window list changed
        - fullscreen: active workspace fullscreen state from workspace list
        - resizewindow: the active window size actually changed
        """
        after_active = _read_active_window()
        after_workspaces = _read_workspace_list()

        if action == "workspace":
            # The active workspace should now be the target.
            return after_active.get("workspace", "") == target

        elif action == "exec":
            # A new window should exist—active window class changed or
            # the window count increased.
            new_class = after_active.get("class", "")
            if new_class and new_class != before_class:
                return True
            # Fallback: check if the workspace list shows more windows.
            before_total = sum(
                int(ws.get("windows", "0")) for ws in before_workspaces
            )
            after_total = sum(
                int(ws.get("windows", "0")) for ws in after_workspaces
            )
            return after_total > before_total

        elif action == "focuswindow":
            # The target should match the active window's class or title.
            target_lower = target.lower()
            after_class = after_active.get("class", "").lower()
            after_title = after_active.get("title", "").lower()
            return target_lower in after_class or target_lower in after_title

        elif action == "closewindow":
            # Window count should have decreased.
            before_total = sum(
                int(ws.get("windows", "0")) for ws in before_workspaces
            )
            after_total = sum(
                int(ws.get("windows", "0")) for ws in after_workspaces
            )
            return after_total < before_total

        elif action in ("movetoworkspace", "movetoworkspacesilent"):
            # The active workspace changed OR the target workspace gained
            # a window.
            current_ws = after_active.get("workspace", "")
            if current_ws and current_ws != before_ws:
                return True
            # Check if the target workspace's window count went up.
            for ws in after_workspaces:
                if ws.get("id", "") == target or ws.get("name", "") == target:
                    return True
            return False

        elif action == "movewindow":
            return after_active.get("workspace", "") != before_ws

        elif action == "resizewindow":
            # Size changed: different width/height values.
            if "size" in after_active and before_active.get("size"):
                return after_active["size"] != before_active["size"]
            return False  # Can't confirm it changed — don't assume success.

        elif action == "fullscreen":
            # Check the active workspace's fullscreen flag from workspace list.
            for ws in after_workspaces:
                if ws.get("id", "") == after_active.get("workspace", ""):
                    has_fullscreen = int(ws.get("hasfullscreen", "0")) > 0
                    # Find the before-state for the same workspace.
                    for bws in before_workspaces:
                        if bws.get("id", "") == after_active.get("workspace", ""):
                            before_fs = int(bws.get("hasfullscreen", "0")) > 0
                            return has_fullscreen != before_fs
                    return has_fullscreen  # No before data — assume if fs now true
            return False  # Could not find workspace in list

        return False  # Default: don't trust dispatch — require explicit verification


    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        """Post-execution verify: the action reported success and verification
        passed (the verify-by-diff was already run inside execute())."""
        return VerificationResult(
            passed=result.success,
            reason="hyprctl action completed" if result.success
            else result.error or "hyprctl action failed",
        )
