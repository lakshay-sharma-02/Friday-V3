"""screen — Friday's eyes and hands (Wave 23).

- :mod:`friday_v6.screen.parsers` — pure intent detection + tesseract
  TSV → OCR words (hermetic, no I/O).
- :mod:`friday_v6.screen.controller` — capture / OCR / click / type /
  scroll / keys (never-crash, injectable runner).
- :mod:`friday_v6.screen.nl` — the ONE NL surface
  (:func:`screen_text_command`) that falls through to desktop control.
- :mod:`friday_v6.screen.recorder` — the watch-me screen sampler
  (``ScreenDemoRecorder``): what Friday *sees* while a demonstration
  runs, fed into the formed skill as screen-context steps.
"""

from .controller import ActionResult, InputController, ScreenController
from .nl import ScreenTextHandler, screen_text_command
from .parsers import (
    OCRWord,
    ScreenIntent,
    find_click_target,
    find_phrase_region,
    parse_ocr_tsv,
    parse_screen_intent,
)
from .recorder import ScreenDemoRecorder

__all__ = [
    "ActionResult",
    "InputController",
    "OCRWord",
    "ScreenController",
    "ScreenDemoRecorder",
    "ScreenIntent",
    "ScreenTextHandler",
    "find_click_target",
    "find_phrase_region",
    "parse_ocr_tsv",
    "parse_screen_intent",
    "screen_text_command",
]
