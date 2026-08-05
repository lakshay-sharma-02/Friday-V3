"""Screen controller — capture, OCR, and input simulation (Wave 23).

Friday *sees* the screen (screenshot → tesseract OCR with positions)
and *touches* it (click / type / scroll / keys through the input
simulator). Pure-stdlib: capture uses ``grim`` (Wayland) /
``gnome-screenshot`` / ``import``; input uses ``ydotool`` (Wayland
mouse) / ``wtype`` (Wayland text) / ``xdotool`` (X11 + keys); OCR uses
``tesseract``.

Design laws (the never-crash + honesty laws):

- Every method returns an :class:`ActionResult` with ``ok`` and
  ``message`` — never raises.
- A missing tool degrades to an honest message ("tesseract isn't
  installed") — never a crash, never a fabricated success.
- All subprocess work goes through the injectable ``runner`` so tests
  substitute a fake and stay 100% hermetic (no display needed).
- Input is REAL input to the operator's desktop: the NL layer
  confirms clicks/types, and the CLI defaults to asking — the
  operator's override is always the last word.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .parsers import OCRWord, parse_ocr_tsv

logger = logging.getLogger("friday_v6.screen")

#: ``runner(cmd: list[str], timeout: int) -> (returncode, stdout, stderr)``
Runner = Callable[[list[str], int], tuple[int, str, str]]


def _default_runner(cmd: list[str], timeout: int = 15
                    ) -> tuple[int, str, str]:
    """Run a subprocess; (rc, stdout, stderr) — never raises."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
        return result.returncode, result.stdout or "", result.stderr or ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug(f"screen subprocess failed ({cmd[0]}): {exc}")
        return -1, "", str(exc)


@dataclass
class ActionResult:
    """One screen operation's outcome — honest, never raises."""

    ok: bool
    message: str
    words: list[OCRWord] | None = None
    image_path: str | None = None
    position: tuple[int, int] | None = None


#: Key names → xdotool / wtype key syntax (the "+" separator is what
#: xdotool understands; wtype uses the same names).
_KEY_MAP = {
    "enter": "Return", "return": "Return", "escape": "Escape",
    "esc": "Escape", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
    "page up": "Prior", "page down": "Next",
    "super": "Super_L", "windows": "Super_L",
    "alt+tab": "alt+Tab",
    "ctrl+c": "ctrl+c", "ctrl-v": "ctrl+v", "ctrl-a": "ctrl+a",
    "ctrl-s": "ctrl+s", "ctrl-z": "ctrl+z",
    "ctrl+shift+s": "ctrl+shift+s", "ctrl+shift+p": "ctrl+shift+p",
    "ctrl+shift+c": "ctrl+shift+c",
}


class ScreenController:
    """Capture + OCR — Friday's eyes. Never raises."""

    def __init__(self, runner: Runner | None = None,
                 output_dir: str | None = None) -> None:
        self._runner = runner or _default_runner
        self._output_dir = output_dir or str(
            Path.home() / ".friday" / "screen")
        Path(self._output_dir).mkdir(parents=True, exist_ok=True)

    # ── capability (honest) ────────────────────────────────────────

    def capabilities(self) -> dict:
        """Which tools exist — for status/CLI (never claims success)."""
        return {
            "capture": (shutil.which("grim") or shutil.which("gnome-screenshot")
                        or shutil.which("import")) is not None,
            "ocr": shutil.which("tesseract") is not None,
            "mouse": (shutil.which("ydotool") or shutil.which("xdotool"))
                     is not None,
            "type": (shutil.which("wtype") or shutil.which("xdotool"))
                    is not None,
            "keys": (shutil.which("wtype") or shutil.which("xdotool"))
                    is not None,
        }

    def is_available(self) -> bool:
        caps = self.capabilities()
        return bool(caps["capture"] or caps["ocr"])

    # ── capture ────────────────────────────────────────────────────

    def capture(self, output_path: str | None = None) -> ActionResult:
        """Screenshot the whole screen; returns the image path."""
        if output_path is None:
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(Path(self._output_dir) / f"screen_{ts}.png")
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            # Wayland: grim. X11: gnome-screenshot/import fallback.
            if shutil.which("grim"):
                rc, _, err = self._runner(["grim", output_path], 15)
                if rc == 0 and Path(output_path).exists():
                    return ActionResult(True, "captured",
                                        image_path=output_path)
                return ActionResult(False, f"grim failed: {err.strip() or rc}")
            for tool in ("gnome-screenshot", "import"):
                if shutil.which(tool):
                    cmd = [tool, "-f", output_path] if tool == "gnome-screenshot" \
                        else [tool, output_path]
                    rc, _, err = self._runner(cmd, 15)
                    if rc == 0 and Path(output_path).exists():
                        return ActionResult(True, "captured",
                                            image_path=output_path)
                    return ActionResult(
                        False, f"{tool} failed: {err.strip() or rc}")
        except Exception as exc:
            logger.debug(f"capture failed: {exc}")
            return ActionResult(False, f"capture failed: {exc}")
        return ActionResult(
            False, "no screen capture tool (need grim, gnome-screenshot "
                   "or import)")

    # ── OCR ────────────────────────────────────────────────────────

    def ocr(self, image_path: str | None = None) -> ActionResult:
        """OCR an image (or a fresh capture) → OCRWord list."""
        if not shutil.which("tesseract"):
            return ActionResult(False, "tesseract isn't installed — I can't "
                                       "read the screen.")
        if image_path is None:
            cap = self.capture()
            if not cap.ok:
                return cap
            image_path = cap.image_path
        try:
            # TSV: word-level rows with bounding boxes + confidence.
            rc, stdout, err = self._runner(
                ["tesseract", image_path, "stdout", "tsv"], 30)
            if rc != 0:
                return ActionResult(
                    False, f"tesseract failed: {err.strip() or rc}")
            words = parse_ocr_tsv(stdout)
            if not words:
                return ActionResult(True, "I can't read any text on the "
                                          "screen.", words=[])
            return ActionResult(
                True, f"read {len(words)} word(s)", words=words,
                image_path=image_path)
        except Exception as exc:
            logger.debug(f"ocr failed: {exc}")
            return ActionResult(False, f"ocr failed: {exc}")

    def find(self, target: str) -> ActionResult:
        """Find an on-screen word/phrase for a target; returns its center."""
        res = self.ocr()
        if not res.ok:
            return res
        words = res.words or []
        from .parsers import find_click_target, find_phrase_region
        region = find_phrase_region(words, target)
        if region:
            left = min(w.left for w in region)
            top = min(w.top for w in region)
            right = max(w.left + w.width for w in region)
            bottom = max(w.top + w.height for w in region)
            return ActionResult(
                True, "found", words=words,
                position=((left + right) // 2, (top + bottom) // 2))
        word = find_click_target(words, target)
        if word:
            return ActionResult(True, "found", words=words,
                                position=word.center)
        return ActionResult(False, f"I can't see '{target}' on the screen.",
                            words=words)


class InputController:
    """Click / type / scroll / keys — Friday's hands. Never raises."""

    def __init__(self, runner: Runner | None = None,
                 screen_size: tuple[int, int] | None = None) -> None:
        self._runner = runner or _default_runner
        #: (width, height) for ydotool's *relative* mouse coordinates
        #: (0..1 fraction of the display). Injected/auto-detected.
        self._screen_size = screen_size

    # ── internals ──────────────────────────────────────────────────

    def _screen_size_detect(self) -> tuple[int, int] | None:
        """Best-effort display size (grim -o '' does not; use hyprctl)."""
        if self._screen_size:
            return self._screen_size
        if shutil.which("hyprctl"):
            try:
                import json
                rc, out, _ = self._runner(["hyprctl", "monitors", "-j"], 5)
                if rc == 0 and out.strip():
                    data = json.loads(out)
                    if data:
                        return int(data[0]["width"]), int(data[0]["height"])
            except Exception:
                pass
        return None

    def _ydotool_relative(self, x: int, y: int) -> tuple[float, float] | None:
        """ydotool mousemove uses a 0..0xFFFF *relative* coordinate space."""
        size = self._screen_size_detect()
        if not size:
            return None
        w, h = size
        if w <= 0 or h <= 0:
            return None
        return (min(max(x / w, 0.0), 1.0) * 65535,
                min(max(y / h, 0.0), 1.0) * 65535)

    # ── click ──────────────────────────────────────────────────────

    def click(self, x: int, y: int, button: str = "left") -> ActionResult:
        """Click at absolute screen coordinates (left/right/middle)."""
        btn = {"left": "1", "right": "3", "middle": "2"}.get(button, "1")
        # Wayland: ydotool (uinput) — move + click, relative coords.
        if shutil.which("ydotool"):
            rel = self._ydotool_relative(x, y)
            if rel:
                rx, ry = int(rel[0]), int(rel[1])
                self._runner(["ydotool", "mousemove", "--absolute",
                              str(rx), str(ry)], 5)
                rc, _, err = self._runner(["ydotool", "click", btn], 5)
                if rc == 0:
                    return ActionResult(True, f"clicked at ({x}, {y})",
                                        position=(x, y))
                return ActionResult(False, f"ydotool click failed: "
                                           f"{err.strip() or rc}")
        # X11: xdotool — absolute coords + click.
        if shutil.which("xdotool"):
            rc, _, err = self._runner(["xdotool", "mousemove",
                                       str(x), str(y)], 5)
            if rc == 0:
                rc2, _, err2 = self._runner(["xdotool", "click", btn], 5)
                if rc2 == 0:
                    return ActionResult(True, f"clicked at ({x}, {y})",
                                        position=(x, y))
                return ActionResult(False, f"xdotool click failed: "
                                           f"{err2.strip() or rc2}")
            return ActionResult(False, f"xdotool mousemove failed: "
                                       f"{err.strip() or rc}")
        return ActionResult(False, "no input tool (need ydotool or xdotool) "
                                   "to click.")

    # ── type ───────────────────────────────────────────────────────

    def type_text(self, text: str) -> ActionResult:
        """Type text into the focused field (no selection needed)."""
        if not text:
            return ActionResult(False, "nothing to type.")
        if shutil.which("wtype"):
            rc, _, err = self._runner(["wtype", text], 10)
            if rc == 0:
                return ActionResult(True, f"typed '{text[:40]}'")
            return ActionResult(False, f"wtype failed: {err.strip() or rc}")
        if shutil.which("xdotool"):
            rc, _, err = self._runner(["xdotool", "type", "--delay", "20",
                                       text], 10)
            if rc == 0:
                return ActionResult(True, f"typed '{text[:40]}'")
            return ActionResult(False, f"xdotool type failed: "
                                       f"{err.strip() or rc}")
        return ActionResult(False, "no typing tool (need wtype or xdotool).")

    # ── scroll ─────────────────────────────────────────────────────

    def scroll(self, direction: str = "down", amount: int = 3
               ) -> ActionResult:
        """Scroll the focused window (buttons 4/5 = up/down)."""
        button = "4" if direction == "up" else "5"
        if shutil.which("xdotool"):
            ok = True
            for _ in range(max(1, min(amount, 10))):
                rc, _, err = self._runner(["xdotool", "click", button], 5)
                if rc != 0:
                    return ActionResult(
                        False, f"xdotool scroll failed: {err.strip() or rc}")
            return ActionResult(True, f"scrolled {direction}")
        if shutil.which("ydotool"):
            # ydotool scroll takes a signed magnitude (negative = up).
            mag = -amount if direction == "up" else amount
            rc, _, err = self._runner(["ydotool", "scroll", str(mag)], 5)
            if rc == 0:
                return ActionResult(True, f"scrolled {direction}")
            return ActionResult(False, f"ydotool scroll failed: "
                                       f"{err.strip() or rc}")
        return ActionResult(False, "no input tool to scroll.")

    # ── keys ───────────────────────────────────────────────────────

    def press(self, key: str) -> ActionResult:
        """Press a key / shortcut (enter, tab, ctrl+c, …)."""
        mapped = _KEY_MAP.get((key or "").lower())
        if not mapped:
            return ActionResult(False, f"I don't know the key '{key}'.")
        if shutil.which("wtype"):
            rc, _, err = self._runner(["wtype", "-k", mapped], 5)
            if rc == 0:
                return ActionResult(True, f"pressed {key}")
            return ActionResult(False, f"wtype key failed: {err.strip() or rc}")
        if shutil.which("xdotool"):
            rc, _, err = self._runner(["xdotool", "key", mapped], 5)
            if rc == 0:
                return ActionResult(True, f"pressed {key}")
            return ActionResult(False, f"xdotool key failed: "
                                       f"{err.strip() or rc}")
        return ActionResult(False, "no input tool to press keys.")


__all__ = [
    "_KEY_MAP",
    "ActionResult",
    "InputController",
    "Runner",
    "ScreenController",
]
