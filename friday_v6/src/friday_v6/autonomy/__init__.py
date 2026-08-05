"""Autonomy — Friday's own judgment → action loop.

The layer that makes Friday run *itself*. Every other layer is a
building block (observer, patterns, skills, missions, gate); this one
closes the loop between judgment and action:

    gather candidates (dispatch offers + mission next-steps)
      → judge through the permission gate
        → AUTO     : execute silently (read-only, audited)
        → CONFIRM  : durable permission request → operator "yes" → execute
        → NEVER    : never autonomously (operator override only)

Two safety laws govern everything here:

1. **The gate is the permission system.** The loop never bypasses
   ``PermissionGate``. AUTO work runs by itself; CONFIRM work is asked
   about durably (the ask survives restarts and resolves through any
   surface: talk, voice, web, ``friday6 autonomy approve``); NEVER work
   is never proposed or executed — it needs the operator's explicit
   ``force`` instruction, exactly like every other Friday surface.

2. **Operator overrides win.** When the operator says "no", "don't do
   that", or "do it a different way", the declined action is recorded in
   ``operator_overrides`` and the loop never proposes that action_type
   (or that command) again — until the operator clears it. Friday's own
   judgment is always subordinate to the operator's explicit words.

This is the "act, don't just suggest" extension the MASTER_PLAN's
Wiring Law demands: every capability already reaches talk/web/voice;
autonomy adds the daemon-driven decision loop that acts on them.
"""

from __future__ import annotations

try:
    from .loop import AutonomyAgent, AutonomyResult, AutonomyOutcome
    _AUTONOMY_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    AutonomyAgent = None  # type: ignore
    AutonomyResult = None  # type: ignore
    AutonomyOutcome = None  # type: ignore
    _AUTONOMY_AVAILABLE = False


def is_available() -> bool:
    return _AUTONOMY_AVAILABLE


__all__ = ["AutonomyAgent", "AutonomyResult", "AutonomyOutcome",
           "is_available", "_AUTONOMY_AVAILABLE"]
