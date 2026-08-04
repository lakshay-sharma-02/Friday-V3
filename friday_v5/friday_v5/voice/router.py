"""VoiceRouter — transcription → engine → spoken reply.

V5's own router (V4's was DB-bound to NLU classifiers, skills
registries, and autonomy agents — all gone here). This one is thin by
design: the transcription goes to the engine, the engine's Claude
session does the thinking, and the final answer is spoken back.

Improvement over V4: the route function is injectable, so the CLI and
the HUD can reuse the same router with different backends. The engine
wires itself in as ``route_function``.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("friday_v5.voice.router")

#: (transcription) -> reply text. The engine provides this; callers
#: that want plain text (no voice) can provide their own.
RouteFn = Callable[[str], str]


class VoiceRouter:
    """Routes each transcription through a backend and speaks the
    reply. Thread-safe: the voice engine's loop thread calls
    ``route()``; the engine's bridge replies on its own worker thread
    and hands the answer back here to speak."""

    def __init__(self, route_function: Optional[RouteFn] = None,
                 tts=None) -> None:
        self.route_function = route_function
        self._tts = tts
        self._lock = threading.Lock()

    def route(self, text: str) -> str:
        """One transcription → reply. Calls the injected route
        function (synchronously, may block), speaks the reply."""
        text = (text or "").strip()
        if not text:
            return ""
        fn = self.route_function
        if fn is None:
            return f"Friday here — you said: {text}"
        try:
            reply = fn(text)
        except Exception as exc:
            logger.warning(f"voice route failed: {exc}")
            reply = f"Sorry, I hit an error: {exc}"
        return reply or ""

    def speak(self, text: str) -> bool:
        """Speak a reply aloud via the injected TTS (no-op when None)."""
        if not text or self._tts is None:
            return False
        try:
            return bool(self._tts.speak(text))
        except Exception as exc:
            logger.warning(f"voice speak failed: {exc}")
            return False
