"""Tone & verbosity selection by relationship depth (Wave 10 §3.3).

The wave-10 law: *tone adapts via relationship depth — not hardcoded.*
The deeper Friday and the operator get, the warmer the tone and the
longer Friday's briefings may be. The MCU feel — *"You're not a morning
person — I kept the briefing to 3 lines"* — is implemented as **morning
brevity**: before a configurable hour, the briefing length drops one
notch (detailed → standard → short) regardless of depth.

Wave 17 (adaptive identity) adds **explicit tone-direction**: "be more
casual, Tony" overrides the depth-derived tone until the operator says
otherwise. Explicit direction wins; depth remains the default. Both are
gradual and explainable — Friday can always say *why* she talks the way
she does.

Tone changes are gradual and explainable: bands are wide, so a depth
change of a few points never flips the personality.

Usage::

    tones = ToneSelector()
    tones.tone_for(0.7)             # → "friendly"
    tones.verbosity_for(0.7)        # → 4
    tones.briefing_length(0.7)      # → "detailed" (afternoon)
    tones.briefing_length(0.7, hour=9)  # → "standard" (morning brevity)

    direction = ToneDirection(tone="casual", verbosity=2)
    tones.effective_tone(0.7, direction)      # → "casual" (direction wins)
    tones.effective_verbosity(0.7, direction) # → 2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Depth bands → tone (gradual, wide bands — no sudden shifts).
_TONE_BANDS: list[tuple[float, str]] = [
    (0.00, "neutral"),
    (0.25, "neutral"),
    (0.35, "warm"),
    (0.55, "warm"),
    (0.70, "friendly"),
    (0.85, "close"),
]

#: Explicit tones an operator may request ("be more casual/formal/
#: friendly/warm/close"). ``None`` = no override (depth-derived).
DIRECTION_TONES: tuple[str, ...] = ("casual", "formal", "warm",
                                    "friendly", "close", "neutral")

#: Depth bands → verbosity 1..5 (how much Friday elaborates).
_VERBOSITY_BANDS: list[tuple[float, int]] = [
    (0.00, 1),
    (0.30, 2),
    (0.55, 3),
    (0.75, 4),
    (0.90, 5),
]

#: Depth bands → briefing length (how much the morning brief covers).
_BRIEFING_BANDS: list[tuple[float, str]] = [
    (0.00, "short"),
    (0.40, "standard"),
    (0.70, "detailed"),
]

#: Hour before which briefings are shortened (morning brevity).
MORNING_UNTIL_HOUR = 11


@dataclass(frozen=True)
class ToneDirection:
    """An explicit tone-direction override (Wave 17 adaptive identity).

    ``tone`` (one of :data:`DIRECTION_TONES`) and/or ``verbosity`` (1..5)
    override the depth-derived defaults until cleared. ``request`` keeps
    the operator's exact words so Friday can explain *why* she talks the
    way she does ("I'm briefer because you asked me to be").
    """

    tone: Optional[str] = None
    verbosity: Optional[int] = None
    request: str = ""
    set_at: str = ""

    @property
    def active(self) -> bool:
        return bool(self.tone or self.verbosity)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["ToneDirection"]:
        """Build from a stored dict (db.get_tone_direction shape)."""
        if not data or not isinstance(data, dict):
            return None
        tone = data.get("tone")
        verbosity = data.get("verbosity")
        if tone is None and verbosity is None:
            return None
        parsed_verbosity = None
        try:
            if verbosity is not None:
                parsed_verbosity = int(verbosity)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            parsed_verbosity = None
        return cls(
            tone=str(tone) if tone else None,
            verbosity=parsed_verbosity,
            request=str(data.get("request") or ""),
            set_at=str(data.get("set_at") or ""),
        )


def _band_label(bands: list[tuple[float, object]], value: float,
                default) -> object:
    label = default
    for threshold, name in bands:
        if value >= threshold:
            label = name
    return label


def tone_for(depth: float) -> str:
    """Tone by depth: neutral → warm → friendly → close."""
    return _band_label(_TONE_BANDS, depth, "neutral")  # type: ignore[return-value]


def verbosity_for(depth: float) -> int:
    """1..5 — how much Friday elaborates at this depth."""
    return _band_label(_VERBOSITY_BANDS, depth, 1)  # type: ignore[return-value]


def briefing_length(depth: float, hour: Optional[int] = None) -> str:
    """Briefing length, with morning brevity.

    Args:
        depth: relationship depth (0..1).
        hour: local hour 0..23. When ``hour < MORNING_UNTIL_HOUR`` the
            length drops one notch (detailed → standard → short).
            ``None`` = afternoon/default (no shortening).
    """
    length = _band_label(_BRIEFING_BANDS, depth, "short")  # type: ignore[assignment]
    if hour is not None and hour < MORNING_UNTIL_HOUR:
        length = _shorten(length)
    return length


def _shorten(length: str) -> str:
    if length == "detailed":
        return "standard"
    if length == "standard":
        return "short"
    return "short"


def effective_tone(depth: float,
                   direction: Optional[ToneDirection] = None) -> str:
    """The tone Friday speaks with: explicit direction wins, else depth."""
    if direction is not None and direction.tone:
        return direction.tone
    return tone_for(depth)


def effective_verbosity(depth: float,
                        direction: Optional[ToneDirection] = None) -> int:
    """1..5 — direction verbosity wins, else depth-derived."""
    if direction is not None and direction.verbosity:
        return direction.verbosity
    return verbosity_for(depth)


class ToneSelector:
    """Tone + verbosity + briefing selection by depth (stateless)."""

    def tone_for(self, depth: float) -> str:
        return tone_for(depth)

    def verbosity_for(self, depth: float) -> int:
        return verbosity_for(depth)

    def briefing_length(self, depth: float,
                        hour: Optional[int] = None) -> str:
        return briefing_length(depth, hour=hour)

    def effective_tone(self, depth: float,
                       direction: Optional[ToneDirection] = None) -> str:
        return effective_tone(depth, direction=direction)

    def effective_verbosity(self, depth: float,
                            direction: Optional[ToneDirection] = None) -> int:
        return effective_verbosity(depth, direction=direction)

    def describe(self, depth: float, hour: Optional[int] = None) -> dict:
        """Full tone profile for status/CLI/web surfaces."""
        return {
            "tone": self.tone_for(depth),
            "verbosity": self.verbosity_for(depth),
            "briefing": self.briefing_length(depth, hour=hour),
        }


__all__ = ["DIRECTION_TONES", "MORNING_UNTIL_HOUR", "ToneDirection",
           "ToneSelector", "briefing_length", "effective_tone",
           "effective_verbosity", "tone_for", "verbosity_for"]
