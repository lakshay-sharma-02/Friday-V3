"""Natural-language screen interpreter — Friday sees and touches (Wave 23).

``screen_text_command(text)`` is the ONE screen surface — the same
``TextCommandHandler`` every surface routes through (voice / CLI / web
/ phone / HUD) calls it pre-dispatch for screen phrases. It:

- **reads**: "what's on my screen" → screenshot + OCR → the text Friday
  actually sees (honest "I can't read anything" when the screen is
  empty or tesseract is missing);
- **touches**: "click the login button" (OCR → find the phrase →
  click its center), "type hello into the search box" (find box →
  click it → type), "scroll down", "press enter" — each real input
  action goes through the optional ``confirm_fn`` (the CLI prompts;
  voice asks aloud; surfaces without a confirm path reply honestly
  instead of acting silently);
- **falls through** to ``desktop_text_command`` for everything else —
  "open whatsapp" / "switch to workspace 3" stay desktop control, so
  the screen layer never breaks existing behavior.

Never raises; a missing tool degrades to an honest message.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional

from .controller import InputController, ScreenController
from .parsers import ScreenIntent, parse_screen_intent

logger = logging.getLogger("friday_v6.screen.nl")

#: ``confirm_fn(description) -> bool`` — the operator's explicit
#: approval for a real input action (click/type/keys). None → the
#: action is refused honestly (never silent input).
ConfirmFn = Optional[Callable[[str], bool]]


class ScreenTextHandler:
    """Screen NL handler — injectable controllers + fallback interpreter.

    The voice/CLI surfaces pass their own ``desktop_fallback`` (the
    desktop handler); tests inject fake controllers so everything is
    hermetic.
    """

    def __init__(self, screen: ScreenController | None = None,
                 input_ctl: InputController | None = None,
                 desktop_fallback: Callable[[str], str] | None = None,
                 confirm_fn: ConfirmFn = None) -> None:
        self._screen = screen or ScreenController()
        self._input = input_ctl or InputController()
        self._fallback = desktop_fallback
        self._confirm = confirm_fn

    def handle(self, text: str) -> str:
        """Route one utterance; returns Friday's reply (never raises)."""
        raw = (text or "").strip()
        if not raw:
            return ""
        intent = parse_screen_intent(raw)
        if intent is None:
            # Not a screen command — fall through to desktop control.
            if self._fallback:
                try:
                    return self._fallback(raw) or ""
                except Exception as exc:
                    logger.debug(f"screen→desktop fallback failed: {exc}")
            return ""
        try:
            if intent.action == "read":
                return self._read()
            if intent.action == "click":
                return self._click(intent)
            if intent.action == "type":
                return self._type(intent)
            if intent.action == "scroll":
                return self._scroll(intent)
            if intent.action == "key":
                return self._press(intent)
        except Exception as exc:
            logger.warning(f"screen handler failed: {exc}")
            return f"Sorry, I couldn't do that with the screen: {exc}"
        return ""

    # ── read ───────────────────────────────────────────────────────

    def _read(self) -> str:
        res = self._screen.ocr()
        if not res.ok:
            return f"I can't see the screen right now — {res.message}"
        words = res.words or []
        if not words:
            return "I can see the screen, but I can't read any text on it."
        # Group words into readable lines (same vertical band).
        lines: list[str] = []
        current: list[tuple[int, str]] = []
        for w in sorted(words, key=lambda w: (w.top, w.left)):
            if current and abs(w.top - current[-1][0]) > 15:
                lines.append(" ".join(t for _, t in current))
                current = []
            current.append((w.top, w.text))
        if current:
            lines.append(" ".join(t for _, t in current))
        joined = "\n".join(lines)
        # Keep the reply bounded (the operator asked; show the gist).
        shown = joined[:600]
        more = f" … ({len(words)} word(s) total)" if len(joined) > 600 else ""
        return f"Here's what's on your screen:\n{shown}{more}"

    # ── click ──────────────────────────────────────────────────────

    def _click(self, intent: ScreenIntent) -> str:
        found = self._screen.find(intent.target)
        if not found.ok or not found.position:
            return (found.message if not found.ok
                    else f"I can't see '{intent.target}' to click it.")
        x, y = found.position
        description = f"click '{intent.target}' at ({x}, {y})"
        if not self._confirmed(description):
            return (f"May I {description}? Say 'yes' to allow it, or "
                    f"'no' to stop me.")
        result = self._input.click(x, y)
        if result.ok:
            return f"Clicked {intent.target}."
        return f"I couldn't click {intent.target} — {result.message}"

    # ── type ───────────────────────────────────────────────────────

    def _type(self, intent: ScreenIntent) -> str:
        text = intent.detail
        if intent.target:
            found = self._screen.find(intent.target)
            if not found.ok or not found.position:
                return (found.message if not found.ok
                        else f"I can't see '{intent.target}' to type into it.")
            x, y = found.position
            description = (f"click '{intent.target}' at ({x}, {y}) "
                           f"and type '{text}'")
            if not self._confirmed(description):
                return f"May I {description}? Say 'yes' to allow it."
            result = self._input.click(x, y)
            if not result.ok:
                return f"I couldn't focus '{intent.target}' — {result.message}"
        else:
            if not self._confirmed(f"type '{text}' into the focused window"):
                return f"May I type '{text}'? Say 'yes' to allow it."
        result = self._input.type_text(text)
        if result.ok:
            return f"Typed '{text[:60]}'."
        return f"I couldn't type that — {result.message}"

    # ── scroll / keys ──────────────────────────────────────────────

    def _scroll(self, intent: ScreenIntent) -> str:
        direction = intent.detail or "down"
        if not self._confirmed(f"scroll {direction}"):
            return f"May I scroll {direction}? Say 'yes' to allow it."
        result = self._input.scroll(direction)
        if result.ok:
            return f"Scrolled {direction}."
        return f"I couldn't scroll — {result.message}"

    def _press(self, intent: ScreenIntent) -> str:
        key = intent.target
        if not self._confirmed(f"press {key}"):
            return f"May I press {key}? Say 'yes' to allow it."
        result = self._input.press(key)
        if result.ok:
            return f"Pressed {key}."
        return f"I couldn't press {key} — {result.message}"

    # ── confirm ────────────────────────────────────────────────────

    def _confirmed(self, description: str) -> bool:
        """Real input needs the operator's explicit approval."""
        if self._confirm is None:
            return False
        try:
            return bool(self._confirm(description))
        except Exception as exc:
            logger.debug(f"screen confirm failed: {exc}")
            return False


def screen_text_command(text: str,
                        confirm_fn: ConfirmFn = None) -> str:
    """The ONE screen surface — module-level entry (matches
    ``desktop_text_command``'s shape so the router can use either)."""
    handler = ScreenTextHandler(confirm_fn=confirm_fn)
    return handler.handle(text)


__all__ = ["ScreenTextHandler", "screen_text_command"]
