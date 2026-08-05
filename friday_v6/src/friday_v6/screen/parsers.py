"""Pure parsers for the screen layer — no I/O, fully hermetic (Wave 23).

Two jobs, both pure:

1. **Intent detection** — recognize natural-language *screen* commands
   ("what's on my screen", "click the login button", "type hello",
   "scroll down", "press enter"). The verbs are deliberately
   conservative so ordinary utterances ("click on youtube" — a web
   destination the desktop layer opens) are never hijacked: a screen
   verb only fires with an explicit screen-y target ("click the X
   button", "click on the blue button at the top").

2. **tesseract TSV → OCR words** — parse ``tesseract … tsv`` output
   into :class:`OCRWord` rows and find the clickable region for a
   spoken target ("login button" → its center coordinates).

The subprocess work lives in :mod:`friday_v6.screen.controller`;
everything here is testable with plain strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── OCR word model ──────────────────────────────────────────────────

@dataclass(frozen=True)
class OCRWord:
    """One recognized word with its bounding box (tesseract TSV)."""

    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float  # 0..100

    @property
    def center(self) -> tuple[int, int]:
        """Center of the word's box — where Friday clicks."""
        return self.left + self.width // 2, self.top + self.height // 2


def parse_ocr_tsv(tsv: str) -> list[OCRWord]:
    """Parse ``tesseract … tsv`` output into OCR words (word-level rows).

    Tesseract TSV columns:
        level page block par line word left top width height conf text

    Only ``level == 5`` (word) rows with a non-empty text and usable
    confidence are kept. Missing/empty input → ``[]`` (honest empty —
    the caller reports "I can't read anything").
    """
    words: list[OCRWord] = []
    for line in (tsv or "").splitlines():
        line = line.strip()
        if not line or line.startswith("level\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            if int(parts[0]) != 5:          # word level
                continue
            text = parts[11].strip()
            if not text:
                continue
            left, top, width, height = (int(parts[6]), int(parts[7]),
                                        int(parts[8]), int(parts[9]))
            conf = float(parts[10])
            if conf < 0:
                continue
            words.append(OCRWord(text, left, top, width, height, conf))
        except (ValueError, IndexError):
            continue  # malformed row — skip, never crash
    return words


def _normalize(text: str) -> str:
    """Lowercase + strip punctuation for matching."""
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def find_click_target(words: list[OCRWord], target: str) -> OCRWord | None:
    """The OCR word to click for a spoken target, or None.

    Matching is *contains-based* on the whole normalized word text:
    target "login" matches an OCR word "login" or "login," — not the
    word "logging" (substring of the *word*, never inside it). The
    best (highest-confidence) match wins; None when nothing matches
    (the caller answers honestly — never clicks blind).
    """
    norm = _normalize(target)
    if not norm or not words:
        return None
    best: OCRWord | None = None
    for w in words:
        if norm == _normalize(w.text) and (best is None or w.conf > best.conf):
            best = w
    return best


def find_phrase_region(words: list[OCRWord], target: str
                       ) -> tuple[OCRWord, ...] | None:
    """The ordered OCR words covering a multi-word target, or None.

    "login button" → the OCR words ``[login, button]`` (in order, on
    the same line region, each word matching its target word) so the
    click point is the *span's* center, not the first word's. Requires
    every target term to match a distinct OCR word — a missing term
    means the phrase isn't on screen (honest None, never a guess).
    """
    terms = [t for t in _normalize(target).split() if t]
    if not terms or not words:
        return None
    used: set[int] = set()
    picked: list[OCRWord] = []
    for term in terms:
        match = None
        for i, w in enumerate(words):
            if i in used:
                continue
            if term == _normalize(w.text):
                match = (i, w)
                break
        if match is None:
            return None
        used.add(match[0])
        picked.append(match[1])
    # Words must sit near each other (same horizontal band) — otherwise
    # "the quick" could match words a screen apart.
    tops = [w.top for w in picked]
    if max(tops) - min(tops) > 60:
        return None
    return tuple(sorted(picked, key=lambda w: w.left))


# ── Natural-language intent detection ──────────────────────────────

@dataclass(frozen=True)
class ScreenIntent:
    """A recognized screen command: action + target (both normalized)."""

    action: str      # "read" | "click" | "type" | "scroll" | "key"
    target: str = ""  # e.g. "login button" ("" when none)
    detail: str = ""  # e.g. "down"/"up" (scroll), the text to type


#: Screen READ phrases — "what's on my screen" (already a desktop
#: read-query; screen answers with real OCR when available).
_READ_PHRASES = (
    "what's on my screen", "what is on my screen",
    "what do you see on my screen", "read my screen",
    "what's showing on my screen", "what is showing on my screen",
)

#: Screen click verbs (conservative — see module docstring).
_CLICK_VERBS = ("click", "click on", "tap", "press", "hit", "select")

#: Screen scroll verbs — the bare "scroll" needs an up/down qualifier
#: or the end of the utterance, so "scroll of truth" (an idiom) is
#: never hijacked.
_SCROLL_VERBS = ("scroll down", "scroll up")

#: Type verbs.
_TYPE_VERBS = ("type ", "type into ", "type in ", "enter ", "input ")

#: Key names — "press enter" → key "enter".
_KEY_NAMES = ("enter", "return", "escape", "esc", "tab", "space",
              "backspace", "delete", "up", "down", "left", "right",
              "home", "end", "page up", "page down", "ctrl+c", "ctrl-c",
              "ctrl+v", "ctrl-v", "ctrl+a", "ctrl-a", "ctrl+s", "ctrl-s",
              "ctrl+z", "ctrl-z", "ctrl+shift+s", "ctrl-shift-s",
              "ctrl+shift+p", "ctrl-shift-p", "ctrl+shift+c",
              "ctrl-shift-c", "alt+tab", "alt-tab", "super", "windows")


def _starts_with_any(text: str, phrases: tuple[str, ...]) -> str | None:
    low = text.lower()
    for p in phrases:
        if low.startswith(p):
            return p
    return None


def parse_screen_intent(text: str) -> ScreenIntent | None:
    """Recognize a screen command in an utterance, or None.

    Conservative rules (never hijack ordinary chat):

    - **read:** exact-ish phrases only ("what's on my screen").
    - **click:** verb + an explicit screen-y target — the target must
      contain an *object marker* ("button", "icon", "tab", "link",
      "box", "field", "menu", "window", "dialog", "option", "tab")
      OR end with an app-like/on-screen noun. Bare "click on youtube"
      (a web destination) and "click here" (too vague) return None.
    - **type:** "type <text> [into <target>]".
    - **scroll:** "scroll [up|down] [N lines]".
    - **key:** "press <key>" with a known key name.
    """
    raw = (text or "").strip()
    low = raw.lower()
    if not low:
        return None

    for phrase in _READ_PHRASES:
        if phrase in low:
            return ScreenIntent("read")

    # press <key> — before click (press enter / press esc are keys).
    if low.startswith("press "):
        rest = low[6:].strip()
        if rest in _KEY_NAMES or any(rest.startswith(k + " ") for k in _KEY_NAMES):
            return ScreenIntent("key", target=rest.split()[0])

    # click / tap / select <target> — needs an explicit screen-y target.
    click_verb = _starts_with_any(low, _CLICK_VERBS)
    if click_verb:
        target = raw[len(click_verb):].strip()
        # Strip fillers one at a time: "click on the submit button" →
        # verb "click on" → target "the submit button" → strip "the"
        # → "submit button". Also normalizes "click on at X" (never).
        for _ in range(3):
            target = re.sub(r"^(?:the|a|an|on|at)\s+", "", target,
                            flags=re.IGNORECASE).strip()
        if _is_screen_target(target):
            return ScreenIntent("click", target=target)
        return None  # "click on youtube" → desktop layer (web destination)

    # scroll [up|down] — qualifier required ("scroll of truth" is not a
    # scroll command).
    scroll_verb = _starts_with_any(low, _SCROLL_VERBS)
    if scroll_verb:
        direction = "down" if scroll_verb == "scroll down" else "up"
        return ScreenIntent("scroll", detail=direction)
    if low.startswith("scroll"):
        rest = low[6:].strip()
        if rest in ("", "down", "up"):
            return ScreenIntent("scroll", detail=rest or "down")

    # type <text> [into <target>]
    for verb in _TYPE_VERBS:
        if low.startswith(verb):
            rest = raw[len(verb):].strip()
            target = ""
            m = re.search(r"\s+into\s+(?:the\s+)?(.+)$", rest, re.IGNORECASE)
            if m and _is_screen_target(m.group(1)):
                target = m.group(1)
                rest = rest[:m.start()].strip()
            if rest:
                return ScreenIntent("type", target=target, detail=rest)
            return None

    return None


#: Nouns that mark a target as an on-screen element (clickable).
_SCREEN_TARGET_MARKERS = (
    "button", "icon", "tab", "link", "box", "field", "menu", "window",
    "dialog", "option", "dropdown", "checkbox", "toggle", "slider",
    "search", "send", "submit", "save", "cancel", "ok", "close",
    "settings", "profile", "login", "sign in", "sign up", "install",
    "update", "allow", "deny", "accept", "reject", "open", "next",
    "back", "previous", "continue", "confirm", "delete", "edit",
    "add", "create", "refresh", "reload", "stop", "play", "pause",
)


def _is_screen_target(target: str) -> bool:
    """Whether a click/type target is an on-screen element, not a URL/app.

    "login button" → True; "youtube" / "youtube.com" / "vs code" →
    False (the desktop layer owns those). URL-ish targets are never
    screen clicks.
    """
    low = (target or "").strip().lower()
    if not low:
        return False
    if "://" in low or low.startswith("www.") or low.endswith((".com",
                                                               ".org",
                                                               ".net",
                                                               ".io")):
        return False
    return any(marker in low for marker in _SCREEN_TARGET_MARKERS)


__all__ = [
    "OCRWord",
    "ScreenIntent",
    "find_click_target",
    "find_phrase_region",
    "parse_ocr_tsv",
    "parse_screen_intent",
]
