"""Prompt panel — typed input routed to the same brain as every surface.

``/find <terms>`` is the HUD's FTS search: it queries the vault through
the SAME index-first/grep-fallback path as ``friday6 vault find`` and
shows results inline (cache, not truth). Everything else routes through
``HudController.handle`` — the same ``TextCommandHandler`` as voice,
CLI, web, and phone (one presence, one brain).
"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Input, Static

from .parsers import _find_terms, render_search


class PromptPanel(Vertical):
    """Input box → HudController.handle; reply shown + streamed."""

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        # ``markup=False``: replies and /find hits are user/vault text
        # and may contain ``[...]`` — plain text, not Rich markup
        # (Textual >= 8 would swallow bracket content or raise on a bad
        # closing tag like ``[/find term]``).
        self._output = Static("", markup=False)
        self._input = Input(placeholder="ask Friday…  (/find terms searches the vault)")

    def compose(self):
        yield self._output
        yield self._input

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self._input.value = ""
        terms = _find_terms(text)
        if terms:
            hits, source = self._controller.search(terms)
            self._output.update(render_search(hits, source))
            return
        if text.strip().lower() == "/find":
            # Bare ``/find`` — honest "find what?" (never routed to the
            # brain as an ordinary slash-utterance).
            self._output.update("find what? e.g. /find auth")
            return
        reply = self._controller.handle(text)
        if reply:
            self._output.update(reply)
