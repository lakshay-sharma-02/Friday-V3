"""Clipboard Executor — read/write the system clipboard with graceful fallback.

Provides clipboard access for the AgenticExecutor. Supports:
  - Linux: wl-clipboard (wl-copy/wl-paste) or xclip
  - macOS: pbcopy/pbpaste
  - Fallback: ``~/.friday/clipboard_bridge.txt`` when no system tool is found

The fallback path is documented in the output so the user knows a real clipboard
tool should be installed for seamless operation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .models import ExecutionResult, Executor


_CLIPBOARD_DIR = Path.home() / ".friday"
_FALLBACK_PATH = _CLIPBOARD_DIR / "clipboard_bridge.txt"


def _detect_clipboard_tool() -> Optional[str]:
    """Detect which clipboard tool is available on this system.

    Returns ``"wl-clipboard"``, ``"xclip"``, ``"macos"``, or ``None``.
    """
    if shutil.which("wl-paste") and shutil.which("wl-copy"):
        return "wl-clipboard"
    if shutil.which("xclip"):
        return "xclip"
    if shutil.which("pbcopy") and shutil.which("pbpaste"):
        return "macos"
    return None


def _clipboard_read() -> str:
    """Read the current clipboard contents.

    Returns empty string on failure (never raises).
    """
    tool = _detect_clipboard_tool()
    if tool == "wl-clipboard":
        try:
            proc = subprocess.run(
                ["wl-paste"], capture_output=True, text=True, timeout=5)
            return proc.stdout if proc.returncode == 0 else ""
        except Exception:
            return ""
    if tool == "xclip":
        try:
            proc = subprocess.run(
                ["xclip", "-o", "-selection", "clipboard"],
                capture_output=True, text=True, timeout=5)
            return proc.stdout if proc.returncode == 0 else ""
        except Exception:
            return ""
    if tool == "macos":
        try:
            proc = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=5)
            return proc.stdout if proc.returncode == 0 else ""
        except Exception:
            return ""
    # Fallback: read from file bridge.
    try:
        if _FALLBACK_PATH.exists():
            return _FALLBACK_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _clipboard_write(text: str) -> bool:
    """Write text to the system clipboard.

    Returns True on success, False on failure.
    """
    tool = _detect_clipboard_tool()
    if tool == "wl-clipboard":
        try:
            proc = subprocess.run(
                ["wl-copy"], input=text, capture_output=True, text=True, timeout=5)
            return proc.returncode == 0
        except Exception:
            return False
    if tool == "xclip":
        try:
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text, capture_output=True, text=True, timeout=5)
            return proc.returncode == 0
        except Exception:
            return False
    if tool == "macos":
        try:
            proc = subprocess.run(
                ["pbcopy"], input=text, capture_output=True, text=True, timeout=5)
            return proc.returncode == 0
        except Exception:
            return False
    # Fallback: write to file bridge.
    try:
        _CLIPBOARD_DIR.mkdir(parents=True, exist_ok=True)
        _FALLBACK_PATH.write_text(text, encoding="utf-8")
        return True
    except Exception:
        return False


def clipboard_available() -> bool:
    """Check if a real clipboard tool is available (not just the file bridge)."""
    return _detect_clipboard_tool() is not None


def clipboard_status() -> str:
    """Return a human-readable clipboard tool status string."""
    tool = _detect_clipboard_tool()
    if tool:
        return f"Clipboard: {tool}"
    return "Clipboard: file bridge (~/.friday/clipboard_bridge.txt)"


class ClipboardExecutor(Executor):
    """Read/write the system clipboard.

    Payload JSON:
      {"op": "read"}              -> read clipboard contents
      {"op": "write", "text": "..."} -> write text to clipboard

    Returns the clipboard text on read, or success confirmation on write.
    Uses wl-clipboard, xclip, pbcopy/pbpaste, or falls back to a file bridge.
    """

    def __init__(self, worker_id: str = "worker:clipboard") -> None:
        self.worker_id = worker_id

    def execute(self, task) -> ExecutionResult:
        raw = getattr(task, "runtime_payload", "") or ""
        t0 = time.monotonic()

        try:
            obj = json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError):
            obj = {}
        op = (obj.get("op") or "read").lower()

        try:
            if op == "read":
                text = _clipboard_read()
                dur = int((time.monotonic() - t0) * 1000)
                if not text and not clipboard_available():
                    return ExecutionResult(
                        success=True,
                        stdout="(empty clipboard)",
                        stderr="",
                        exit_code=0,
                        duration_ms=dur,
                        metadata={
                            "clipboard_tool": "file_bridge",
                            "note": "No system clipboard tool found — used file bridge",
                        },
                    )
                tool_note = ""
                if not clipboard_available():
                    tool_note = "\n⚠ No system clipboard tool found — used file bridge"
                return ExecutionResult(
                    success=True,
                    stdout=text + tool_note,
                    stderr="",
                    exit_code=0,
                    duration_ms=dur,
                    metadata={"clipboard_tool": _detect_clipboard_tool() or "file_bridge"},
                )

            if op == "write":
                text = obj.get("text", "")
                if not text:
                    dur = int((time.monotonic() - t0) * 1000)
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=dur,
                        error="clipboard write: no text provided",
                    )
                ok = _clipboard_write(text)
                dur = int((time.monotonic() - t0) * 1000)
                if ok:
                    tool_note = ""
                    if not clipboard_available():
                        tool_note = " (file bridge — no system clipboard tool found)"
                    return ExecutionResult(
                        success=True,
                        stdout=f"Copied {len(text)} chars to clipboard{tool_note}",
                        stderr="",
                        exit_code=0,
                        duration_ms=dur,
                        metadata={
                            "clipboard_tool": _detect_clipboard_tool() or "file_bridge",
                            "char_count": len(text),
                        },
                    )
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=1,
                    duration_ms=dur,
                    error="clipboard write failed",
                )

            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=dur,
                error=f"unknown clipboard op: {op}",
            )

        except Exception as e:
            dur = int((time.monotonic() - t0) * 1000)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=None,
                duration_ms=dur,
                error=f"clipboard executor: {type(e).__name__}: {e}",
            )
