"""Claude Code as the mission planner (Wave 18 close-out).

The ``Planner.enhancer`` hook (``missions/planner.py``) is connected to
the local Claude Code CLI: for an agentic goal ("ship the auth refactor
by Friday") Claude Code reads the workspace and returns a concrete,
ordered step plan that maps onto Friday's executor vocabulary
(``shell | git | file | python | testing | claude | null``). The
deterministic :class:`~friday_v4.missions.planner.Planner` is always
the floor — :class:`ClaudePlanner` returns ``None`` on any failure and
the goal falls back to Friday's own rules (never crash, degrade
silently — the daemon law).

Same shape as every Wave 18 delegation, one difference in degree:

- **Gate** — the goal text is classified with the shared destructive-
  phrase sniffing (``_CLAUDE_DANGEROUS_PHRASES``). A NEVER goal
  ("deploy to production by Friday") is *refused*: ``None``, so the
  deterministic planner turns it into a manual step and nothing runs.
- **Sandbox** — ``claude -p`` runs cwd-rooted, timeout-bounded,
  env-sanitized, stdin=/dev/null, with **read-only tools**
  (``--allowedTools "Read Glob Grep"``): planning inspects the repo
  but never edits or executes. The steps it produces are executed
  later through the real gate → sandbox → audit pipeline.
- **Audit** — the delegation is recorded in the audit trail
  (``action_type = "claude_plan"``) when a connection is available,
  with its gate level and outcome.
- **Parse** — the CLI's JSON result is mapped onto :class:`StepPlan`
  objects. Unknown ``action_type`` values become *manual* steps
  (``action_type=None``) — Friday never invents an executor.

Config: model + timeout reuse ``FRIDAY_V4_CLAUDE_MODEL`` /
``FRIDAY_V4_CLAUDE_TIMEOUT``; the plan tool allowlist is
``FRIDAY_V4_CLAUDE_PLAN_TOOLS`` (default ``"Read Glob Grep"``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from ..execution.audit import AuditLogger
from ..execution.executors import _CLAUDE_DANGEROUS_PHRASES, _claude_config
from ..execution.gate import PermissionGate, PermissionLevel
from ..execution.sandbox import Sandbox
from ..nlu.resolver import _EXECUTION_TYPES
from ..security.tooling import find_tool
from .planner import Planner, StepPlan

logger = logging.getLogger("friday_v4.missions.claude_planner")

#: The executor vocabulary Friday can actually run. A plan step whose
#: action_type is not in this set becomes a *manual* step.
_PLAN_ACTION_TYPES = _EXECUTION_TYPES

#: Default read-only tool allowlist for planning. Claude may inspect the
#: workspace to ground its plan, but never edits or runs anything — the
#: produced steps are executed later through Friday's own gated pipeline.
_DEFAULT_PLAN_TOOLS = "Read Glob Grep"

#: Upper bound on steps accepted from a plan (defensive against a
#: runaway decomposition — missions stay digestible).
_MAX_STEPS = 12

#: The instruction Claude Code receives. It must answer with ONLY a JSON
#: object — the parser accepts that object directly, fenced, or embedded
#: in prose (never crashes on any shape).
_PLAN_PROMPT = (
    "You are the planner for Friday, an AI operating partner. The "
    "operator's goal is: {goal!r}\n\n"
    "Read the repository enough to ground your plan, then respond with "
    "ONLY a JSON object of exactly this shape:\n"
    '{{"plan": [{{"title": "short human-readable title", '
    '"action_type": "shell|git|file|python|testing|claude|null", '
    '"command": "exact executor argument"}}]}}\n'
    "Rules:\n"
    "- action_type must be one of: shell, git, file, python, testing, "
    "claude (the executors Friday can run) — or null for a step only a "
    "human can do.\n"
    "- command is what Friday passes to that executor, e.g. "
    '"pytest tests/" for testing, "status" for git, "run.py" for '
    "python. Empty string for null steps.\n"
    "- 1 to 8 steps, each single-purpose, in execution order.\n"
    "- Do not invent commands you have not checked against the repo; "
    "when unsure, use null (manual step).\n"
    "- Return the JSON only — no markdown fences, no prose."
)


class ClaudePlanner:
    """An :data:`~friday_v4.missions.planner.Enhancer` backed by Claude Code.

    ``__call__(goal) -> list[StepPlan] | None`` — compatible with the
    deterministic ``Planner``'s enhancer contract: ``None`` means "no
    plan from the enhancer" and the deterministic rules take over.
    """

    action_type = "claude_plan"

    def __init__(self, cwd: Optional[str | Path] = None,
                 conn=None,
                 model: Optional[str] = None,
                 allowed_tools: Optional[str] = None,
                 timeout_seconds: Optional[float] = None,
                 gate: Optional[PermissionGate] = None,
                 sandbox: Optional[Sandbox] = None) -> None:
        cfg = _claude_config()
        self.cwd = str(cwd) if cwd is not None else None
        self.conn = conn
        self.model = model or cfg["model"]
        self.allowed_tools = allowed_tools or os.environ.get(
            "FRIDAY_V4_CLAUDE_PLAN_TOOLS", _DEFAULT_PLAN_TOOLS)
        self.timeout_seconds = timeout_seconds or cfg["timeout_seconds"]
        self.gate = gate or PermissionGate(
            dangerous=_CLAUDE_DANGEROUS_PHRASES)
        roots = [Path(self.cwd).resolve()] if self.cwd else None
        self.sandbox = sandbox or Sandbox(allowed_roots=roots)

    # ── Enhancer protocol ────────────────────────────────────────────

    def __call__(self, goal: str) -> Optional[list[StepPlan]]:
        """Decompose ``goal`` through Claude Code, or ``None`` on any
        refusal/failure (the deterministic planner takes over). Never
        raises."""
        task = (goal or "").strip()
        if not task:
            return None

        # Gate: destructive plan goals are refused outright — a plan is
        # never even requested for something Friday must not do. (The
        # deterministic fallback then yields a *manual* step, so
        # nothing executes without the operator doing it themselves.)
        try:
            level = self.gate.classify("claude", task)
        except Exception:  # defensive — gate is pure logic, but never crash
            level = PermissionLevel.CONFIRM
        if level == PermissionLevel.NEVER:
            logger.info("claude planner refused destructive goal %r", task)
            aid = self._audit_record(task, level)
            self._audit_finish(aid, "denied",
                               output="refused by gate (never)")
            return None

        claude = find_tool("claude")
        if not claude:
            # No hands available — the deterministic planner is the floor.
            return None

        prompt = _PLAN_PROMPT.format(goal=task)
        args = [
            claude, "-p", prompt, "--output-format", "json",
            "--model", self.model,
            "--allowedTools", self.allowed_tools,
        ]
        aid = self._audit_record(task, level)
        try:
            res = self.sandbox.run(args, cwd=self.cwd,
                                   timeout=self.timeout_seconds)
        except Exception as exc:  # defensive — never crash the caller
            logger.debug("claude plan run failed: %s", exc)
            self._audit_finish(aid, "failed", output=str(exc))
            return None

        if res.timed_out:
            self._audit_finish(aid, "timed_out",
                               output="planning timed out")
            return None

        steps = self._parse(res)
        if steps is None:
            self._audit_finish(aid, "failed",
                               output=(res.output or res.error
                                       or "no parseable plan")[:2000])
            return None
        self._audit_finish(
            aid, "succeeded",
            output=f"planned {len(steps)} step(s): "
                   + " · ".join(s.title for s in steps[:8]))
        return steps

    # ── Result parsing ───────────────────────────────────────────────

    def _parse(self, res) -> Optional[list[StepPlan]]:
        """Map the CLI's JSON result onto StepPlans (None on any shape
        that can't yield a usable plan)."""
        if res.timed_out or res.error:
            return None
        out = (res.stdout or "").strip()
        if not out:
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("is_error"):
            return None
        reason = str(data.get("terminal_reason") or "")
        if reason in ("error_limit", "max_turns"):
            return None

        raw = data.get("plan")
        if raw is None:
            raw = data.get("steps")
        if raw is None:
            raw = self._plan_from_result_text(data.get("result") or "")
        if not isinstance(raw, list) or not raw:
            return None

        steps: list[StepPlan] = []
        for i, item in enumerate(raw[:_MAX_STEPS]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:120] \
                or f"step {i + 1}"
            at = item.get("action_type")
            action_type = (at if isinstance(at, str)
                           and at in _PLAN_ACTION_TYPES else None)
            command = str(item.get("command") or "").strip()
            steps.append(StepPlan(title, action_type, command))
        return steps or None

    @staticmethod
    def _plan_from_result_text(text: str):
        """The plan list from the assistant's result text.

        Accepts a bare JSON object, a fenced one (```json … ```), or a
        JSON object embedded in prose — the first ``{ … }`` span wins.
        Returns ``None`` when nothing parseable is found.
        """
        text = (text or "").strip()
        if not text:
            return None
        fenced = re.sub(r"```(?:json)?\s*|\s*```", "", text)
        for cand in (fenced, text):
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                for key in ("plan", "steps"):
                    if key in data:
                        return data[key]
                return None
            if isinstance(data, list):
                return data
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
            if isinstance(data, dict):
                for key in ("plan", "steps"):
                    if key in data:
                        return data[key]
        return None

    # ── Audit (the delegation is a first-class action) ───────────────

    def _audit_record(self, task: str, level: PermissionLevel) -> Optional[str]:
        if self.conn is None:
            return None
        try:
            return AuditLogger(self.conn).record(
                self.action_type, goal=task, command=task,
                cwd=str(self.cwd or ""), permission_level=level.value)
        except Exception as exc:  # defensive — never crash the planner
            logger.debug("claude plan audit record failed: %s", exc)
            return None

    def _audit_finish(self, action_id: Optional[str], status: str,
                      output: str = "") -> None:
        if not action_id:
            return
        try:
            AuditLogger(self.conn).finish(action_id, status, output=output)
        except Exception as exc:  # defensive
            logger.debug("claude plan audit finish failed: %s", exc)


def make_planner(cwd: Optional[str | Path] = None,
                 conn=None) -> Planner:
    """The mission planner — the single construction point.

    ``FRIDAY_V4_CLAUDE_PLANNER`` set (explicit opt-in, Wave 18
    close-out — same convention as Wave 13's ``FRIDAY_V4_LLM``):
    the deterministic :class:`Planner` carries a :class:`ClaudePlanner`
    enhancer, so goal decomposition delegates to the Claude Code CLI
    (gated, sandboxed, audited, read-only). Otherwise — or on any
    enhancer construction failure — the plain deterministic planner is
    returned: mission planning is hermetic by default and never gates
    on claude being present.
    """
    if os.environ.get("FRIDAY_V4_CLAUDE_PLANNER"):
        try:
            return Planner(enhancer=ClaudePlanner(cwd=cwd, conn=conn))
        except Exception:  # defensive — the deterministic floor always stands
            logger.debug("claude planner unavailable — deterministic")
    return Planner()


__all__ = ["ClaudePlanner", "make_planner"]
