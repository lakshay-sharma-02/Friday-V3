"""Mission planner — goal → step decomposition (Wave 9).

The planner turns a goal statement into an ordered list of
:class:`StepPlan` objects — each one either an *executable* step (it
maps onto an execution-layer action_type) or a *manual* step (Friday
has no executor for it, so the operator completes it).

Strategy (per WAVE_9_AGENCY_CORE.md §4.3):
- **Deterministic first, always works.** If ``understanding.resolve``
  can map the goal to an execution action, that's the plan. Template
  rules cover common multi-step workflows (test → lint, etc.).
- **LLM enhances later, never gates.** An optional ``enhance`` callable
  can be injected to produce richer plans; without it the rules still
  return a valid, safe plan.
- **Never invents.** Unrecognized goals become a single manual step —
  Friday never fabricates an action it can't run.

Hermetic: pure logic, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .models import make_step_payload

#: Optional LLM-style enhancer: (goal) -> list[StepPlan] | None.
Enhancer = Callable[[str], Optional[list["StepPlan"]]]


@dataclass(frozen=True)
class StepPlan:
    """One planned step (not yet persisted)."""

    title: str
    action_type: Optional[str] = None   # None = manual step
    command: str = ""
    cwd: Optional[str] = None

    def to_payload(self) -> dict:
        return make_step_payload(self.action_type, self.command, self.cwd)


#: Template rules: (keyword, title, action_type, command). Checked in
#: order; the first match wins. These cover the workflow Friday can
#: actually execute today.
_TEMPLATES: tuple[tuple[str, str, str, str], ...] = (
    ("lint", "Run the linter", "shell", "lint"),
    ("test", "Run the test suite", "testing", ""),
    ("typecheck", "Run the type checker", "shell", "typecheck"),
    ("build", "Build the project", "shell", "build"),
)


class Planner:
    """Deterministic goal → steps decomposition."""

    def __init__(self, enhancer: Optional[Enhancer] = None) -> None:
        self._enhancer = enhancer

    # ── Public API ────────────────────────────────────────────────────

    def plan(self, goal: str, cwd: Optional[str] = None) -> list[StepPlan]:
        """Decompose ``goal`` into steps (never raises, never empty).

        Resolution order:
          1. Optional enhancer (LLM) — richest plan if provided.
          2. ``understanding.resolve`` — one executable step when the
             goal maps directly onto an execution action.
          3. Template rules — common workflows.
          4. Manual single step — Friday tracks the goal, the operator
             does the work.
        """
        if self._enhancer:
            try:
                enhanced = self._enhancer(goal)
                if enhanced:
                    return _with_cwd(enhanced, cwd)
            except Exception:
                pass  # enhancer failure → deterministic fallback

        resolved = self._resolve_goal(goal)
        if resolved:
            return _with_cwd([resolved], cwd)

        for keyword, title, action_type, command in _TEMPLATES:
            if re.search(rf"\b{re.escape(keyword)}\b", goal.lower()):
                return _with_cwd(
                    [StepPlan(title, action_type, command)], cwd)

        return [StepPlan(goal, None, "", cwd)]

    def can_plan(self, goal: str) -> bool:
        """Whether this goal maps to at least one executable step."""
        if self._resolve_goal(goal):
            return True
        return any(re.search(rf"\b{re.escape(k)}\b", goal.lower())
                   for k, *_rest in _TEMPLATES)

    # ── Internals ─────────────────────────────────────────────────────

    def _resolve_goal(self, goal: str) -> Optional[StepPlan]:
        """Map a goal onto an execution action via understanding."""
        try:
            from ..understanding import resolve
        except ImportError:
            return None
        try:
            action = resolve(goal)
            if action and action.can_execute:
                kw = action.to_execution() or {}
                return StepPlan(goal, kw.get("action_type"),
                                kw.get("command", ""))
        except Exception:
            return None
        return None


def _with_cwd(steps: list[StepPlan], cwd: Optional[str]) -> list[StepPlan]:
    if not cwd:
        return steps
    return [StepPlan(s.title, s.action_type, s.command, cwd)
            for s in steps]


__all__ = ["Planner", "StepPlan", "Enhancer"]
