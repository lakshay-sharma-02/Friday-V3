"""Natural-language router — utterance → Friday does something (Wave 9).

The shared handler that makes the MCU interaction real: *"Friday, run
the tests"* is resolved by the ONE NLU point (:mod:`friday_v4.nlu`
``resolve()`` — LLM-first, rules fallback) into an execution action,
which then flows through the real pipeline (gate → sandbox → audit).
No canned fallback for real requests.

Every surface uses the same entry point:

- ``friday4 talk "run the tests"`` (see ``cli_nl.py``)
- ``VoiceRouter.route()`` — voice falls back here after desktop /
  briefing checks (see ``voice/router.py``)
- future web chat

Design laws:
- **Never crash** — every branch is guarded; ``handle()`` returns a
  response (or a graceful error message) for any input.
- **One command language** — ``resolve()`` is the single interpreter;
  this module adds the *act* part.
- **Ambiguity is reported** — EXECUTE without a resolvable executor
  asks "what would you like me to run?" instead of guessing.
- **Manual steps are never auto-completed** — a mission step with no
  executor is completed by the operator (``handle_manual``); Friday
  never invents a result.

Hermetic: no I/O at import; DB/execution only touched when a branch
needs them. Optional ``desktop_handler`` lets the voice router reuse
its existing desktop command routing (the CLI passes None).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .nlu import Intent, resolve

logger = logging.getLogger("friday_v4.nl_router")

#: Leading verbs/phrases stripped from the first correlate operand so
#: ``correlate()`` receives clean repo names or paths.
_RESEARCH_LEAD_VERBS = (
    "analyze ", "compare ", "correlate ", "research ",
    "what's the deal ", "what is the deal ",
)


def _strip_research_lead(text: str) -> str:
    """Strip a leading research verb/phrase ("analyze X" → "X")."""
    lower = text.strip().lower()
    for verb in _RESEARCH_LEAD_VERBS:
        if lower.startswith(verb):
            return text.strip()[len(verb):].strip()
    return text.strip()


def _split_and_operands(text: str):
    """Split "X and Y" at the first ' and ' (both sides non-empty)."""
    parts = re.split(r"\s+and\s+", text, maxsplit=1,
                     flags=re.IGNORECASE)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None


def _extract_pair(text: str):
    """The two research operands from an utterance, or None.

    Wave 19 fix: the MCU phrasing "what's the deal between X and Y"
    has its research lead *before* the pair separator — the old marker
    loop split at " between " and then stripped the first operand to an
    empty string, correlating "" against Y. Now the lead is never an
    operand:

    - "between X and Y" → (X, Y)
    - "analyze X vs Y" / "compare A versus B" → (X, Y) with the lead
      stripped
    - plain "X and Y" → (X, Y) with the lead stripped
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    lower = stripped.lower()
    if " between " in lower:
        pair = _split_and_operands(stripped.split(" between ", 1)[1])
        if pair:
            return pair
    # "what's the deal with X and Y" — the "with" pair separator (Wave
    # 19 slice 2 sweep). The research lead ("what's the deal") sits
    # before the separator, so the pair is extracted from the tail and
    # the lead is never an operand; "compare X with Y" (no "and")
    # splits at the separator with the lead stripped.
    if " with " in lower:
        left, right = re.split(r"\s+with\s+", stripped, maxsplit=1,
                               flags=re.IGNORECASE)
        pair = _split_and_operands(right)
        if pair:
            return pair
        left_clean = _strip_research_lead(left).strip()
        if left_clean and right.strip():
            return left_clean, right.strip()
    for marker in (" vs. ", " vs ", " versus "):
        if marker in lower:
            parts = re.split(re.escape(marker), stripped, maxsplit=1,
                             flags=re.IGNORECASE)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return (_strip_research_lead(parts[0]).strip(),
                        parts[1].strip())
    pair = _split_and_operands(stripped)
    if pair:
        return _strip_research_lead(pair[0]).strip(), pair[1]
    return None


#: Replan/adapt phrases (Wave 18 close-out). "replan" itself and any
#: "<verb> the/my plan" phrasing re-decompose the latest mission's goal
#: (through Claude Code when ``FRIDAY_V4_CLAUDE_PLANNER`` is set).
#: ``_is_replan_request`` is conservative: "create a plan" / "ship the
#: auth refactor" never match, so they stay mission *creation*.
_REPLAN_VERBS = ("replan", "re-plan", "revise", "change", "update",
                 "adapt", "adjust", "rework")


def _is_replan_request(text: str) -> bool:
    """Whether an utterance asks to replan an existing mission."""
    lower = (text or "").lower()
    if "replan" in lower or "re-plan" in lower:
        return True
    return any(
        verb in lower and ("plan" in lower or "mission" in lower)
        for verb in _REPLAN_VERBS[2:])


#: What the operator replanning actually said — the reason recorded on
#: the adaptation ("plan changed because …") stays honest.

def _replan_reason(text: str) -> str:
    return f"operator asked me to replan ({text.strip()})"


#: ``desktop_handler(text) -> str`` — routes desktop intents (focus /
#: switch / open / launch). The voice router passes its own; the CLI
#: passes None and we respond honestly that desktop control isn't wired.
DesktopHandler = Callable[[str], str]

#: ``confirm_fn(description) -> bool`` — human decision for CONFIRM
#: actions. CLI prompts y/N; voice asks & listens; None = fail closed.
ConfirmFn = Optional[Callable[[str], bool]]


@dataclass
class TalkResult:
    """One utterance's outcome — what Friday understood and did."""

    text: str
    intent: str                          # Intent value (execute/plan/...)
    action: str                          # executed | mission_created | desktop | chat | denied | failed | clarification
    response: str = ""                   # the natural-language reply
    action_type: Optional[str] = None    # executor used (execute only)
    command: str = ""
    goal: Optional[str] = None
    mission_id: Optional[str] = None
    action_id: Optional[str] = None      # audit trail id
    status: Optional[str] = None         # execution status (succeeded/denied/...)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "intent": self.intent,
            "action": self.action,
            "response": self.response,
            "action_type": self.action_type,
            "command": self.command,
            "goal": self.goal,
            "mission_id": self.mission_id,
            "action_id": self.action_id,
            "status": self.status,
        }


class TextCommandHandler:
    """Understands an utterance and *does* it (shared by CLI + voice)."""

    def __init__(self, conn=None,
                 desktop_handler: Optional[DesktopHandler] = None,
                 cwd: Optional[str] = None,
                 llm: Optional[object] = None) -> None:
        self.conn = conn
        self.desktop_handler = desktop_handler
        self.cwd = cwd
        #: The ONE NLU point's LLM client (Wave 13a). None → deterministic
        #: fallback inside resolve().
        self.llm = llm

    # ── Public API ────────────────────────────────────────────────────

    def handle(self, text: str, *, confirm_fn: ConfirmFn = None,
               force: bool = False,
               cwd: Optional[str] = None,
               manual_result: str = "") -> TalkResult:
        """Interpret ``text`` and act on it (never raises).

        Every utterance is recorded verbatim into the conversation log
        (the brain learns from what you actually said — no keywords),
        then routed. ``force``/``confirm_fn`` pass through to the
        execution gate; ``cwd`` pins the working directory;
        ``manual_result`` honors manual mission steps.
        """
        raw = (text or "").strip()
        if not raw:
            return TalkResult(raw, "", "chat", response="I'm listening.")
        result = self._route(raw, confirm_fn=confirm_fn, force=force,
                             cwd=cwd or self.cwd,
                             manual_result=manual_result)
        self._log_exchange(raw, result)
        return result

    def _route(self, raw: str, *, confirm_fn: ConfirmFn, force: bool,
               cwd: Optional[str],
               manual_result: str = "") -> TalkResult:
        """The interpretation core of :meth:`handle` (no logging here)."""
        workdir = cwd
        try:
            action = resolve(raw, llm=self.llm)
        except Exception as exc:  # defensive — never crash
            logger.warning(f"nl_router resolve failed: {exc}")
            return TalkResult(raw, "unknown", "chat",
                              response="I couldn't understand that — "
                                       "could you rephrase it?")

        intent = action.intent
        try:
            if action.needs_clarification:
                return TalkResult(
                    raw, intent.value, "clarification",
                    response=action.clarification
                    or "Could you be more specific?")

            if intent == Intent.EXECUTE:
                if action.can_execute:
                    return self._run_execution(raw, action,
                                               confirm_fn=confirm_fn,
                                               force=force,
                                               cwd=workdir)
                return TalkResult(
                    raw, intent.value, "clarification",
                    response="What would you like me to run?")

            if intent == Intent.PLAN:
                if _is_replan_request(raw):
                    return self._replan_response(raw, action, cwd=workdir)
                return self._create_mission(raw, action, cwd=workdir)

            if intent == Intent.DESKTOP:
                return self._desktop(raw)

            if intent == Intent.IDE:
                return self._ide_response(raw, action, cwd=workdir)

            if intent == Intent.GREETING:
                return TalkResult(
                    raw, intent.value, "chat",
                    response="Hello. I'm Friday. How can I help?")

            if intent == Intent.HELP:
                return self._help_response(raw)

            if intent == Intent.ASK:
                return self._ask_response(raw, action)

            if intent == Intent.RESEARCH:
                return self._research_response(raw, action)

            if intent == Intent.SKILL:
                return self._skill_response(raw, action)

            if intent == Intent.ACCEPT:
                return self._accept_response(raw, action,
                                             confirm_fn=confirm_fn,
                                             force=force, cwd=workdir)

            if intent == Intent.DENY:
                return self._deny_response(raw)

            if intent == Intent.SECURITY:
                return self._security_response(raw, action, cwd=workdir)

            if intent == Intent.MEMORY:
                return self._memory_response(raw, action)

            if intent == Intent.STYLE:
                return self._style_response(raw, action)

            return TalkResult(
                raw, intent.value, "chat",
                response="I didn't understand that. Try 'run the tests', "
                         "'git status', 'ship the auth refactor', or "
                         "'analyze the integration cost between X and Y'.")
        except Exception as exc:  # defensive — never crash
            logger.warning(f"nl_router handle failed: {exc}")
            return TalkResult(raw, intent.value, "failed",
                              response=f"Sorry, I ran into an error: {exc}")

    def handle_manual(self, mission_id: str, result: str) -> TalkResult:
        """Mark a mission's manual step completed (operator confirms).

        The mission is started first if needed — the engine only
        advances ACTIVE missions, and a freshly planned mission's
        manual step could never complete otherwise.
        """
        try:
            from .missions import MissionEngine, MissionStatus
            engine = MissionEngine(self.conn)
            mission = engine.get(mission_id)
            if mission and mission.status == MissionStatus.PLANNED:
                engine.start(mission_id)
            outcome = engine.advance(mission_id, manual_result=result)
            if outcome.action == "manual_completed":
                return TalkResult(
                    mission_id, "plan", "manual_completed",
                    response=f"Recorded — manual step done: {result or 'done'}.",
                    mission_id=mission_id)
            return TalkResult(
                mission_id, "plan", "failed",
                response=f"Couldn't update the mission: {outcome.message}",
                mission_id=mission_id)
        except Exception as exc:
            logger.warning(f"handle_manual failed: {exc}")
            return TalkResult(mission_id, "plan", "failed",
                              response=f"Sorry: {exc}", mission_id=mission_id)

    # ── Internals ─────────────────────────────────────────────────────

    def _run_execution(self, text: str, action,
                       *, confirm_fn: ConfirmFn, force: bool,
                       cwd: Optional[str] = None) -> TalkResult:
        kw = action.to_execution() or {}
        action_type = kw.get("action_type")
        command = kw.get("command", "")
        goal = kw.get("goal", text)
        # Wave 6 IDE preflight: when the command names a source file,
        # Friday's IDE/static diagnostics ride along (audit goal + a
        # heads-up in the reply). Explicit opt-in via
        # FRIDAY_V4_IDE_PREFLIGHT — never blocks, never slows the
        # default path. Claude delegations carry their own preflight
        # (the executor appends the context to Claude's system prompt).
        preflight = ("" if action_type == "claude"
                     else self._ide_preflight_note(action_type, command, cwd))
        if preflight:
            goal = f"{goal} | ide-preflight: {preflight}"
        try:
            from .execution import execute
            result = execute(action_type, command,
                             cwd=cwd,
                             conn=self.conn,
                             confirm_fn=confirm_fn,
                             force=force,
                             goal=goal)
        except Exception as exc:
            logger.warning(f"nl_router execute failed: {exc}")
            return TalkResult(text, "execute", "failed",
                              response=f"The action failed: {exc}",
                              action_type=action_type, command=command,
                              goal=goal)

        status = getattr(result, "status", "failed")
        out = getattr(result, "output", "") or ""
        aid = getattr(result, "action_id", None)
        if status == "succeeded":
            # Claude Code results are answers, not "Done — <task>" —
            # surface the first line of Claude's actual response.
            if action_type == "claude":
                first = out.strip().splitlines()[:1]
                spoken = first[0][:400] if first else "Done."
            else:
                first = out.strip().splitlines()[:1]
                spoken = (f"Done — {command or action_type or 'it'}"
                          f"{': ' + first[0][:160] if first else ''}.")
                if preflight:
                    spoken = f"{spoken} (heads-up: {preflight}.)"
            return TalkResult(text, "execute", "executed", response=spoken,
                              action_type=action_type, command=command,
                              goal=goal, action_id=aid, status=status)
        if status == "denied":
            return TalkResult(text, "execute", "denied",
                              response="I won't do that without your "
                                       "confirmation.",
                              action_type=action_type, command=command,
                              goal=goal, action_id=aid, status=status)
        # For delegated claude tasks the error text is the *answer* —
        # surface the CLI's own message ("model exploded", "error
        # limit") instead of a generic "failed.".
        if action_type == "claude":
            first = out.strip().splitlines()[:1]
            why = first[0][:400] if first else ""
            return TalkResult(
                text, "execute", "failed",
                response=(f"Claude Code couldn't finish that"
                          f"{': ' + why if why else '.'}"),
                action_type=action_type, command=command,
                goal=goal, action_id=aid, status=status)
        # Wave 19 slice 2: surface *why* a real command failed — "git
        # status" in a non-repo said only "That didn't work — failed."
        # with the actual stderr ("fatal: not a git repository…")
        # buried. The first line of the combined output is the reason.
        first = out.strip().splitlines()[:1]
        why = first[0][:200] if first else ""
        return TalkResult(
            text, "execute", "failed",
            response=(f"That didn't work — {status}."
                      + (f" {why}" if why else "")),
            action_type=action_type, command=command,
            goal=goal, action_id=aid, status=status)

    def _resolve_ide_path(self, target: str,
                          cwd: Optional[str] = None):
        """Resolve an IDE target to an absolute path (never raises)."""
        from pathlib import Path as _Path
        try:
            p = _Path(target).expanduser()
            if not p.is_absolute():
                base = _Path(cwd).resolve() if cwd else _Path.cwd()
                p = base / p
            return p.resolve()
        except Exception as exc:
            logger.debug(f"ide path resolve failed: {exc}")
            return _Path(target)

    def _ide_preflight_note(self, action_type: str, command: str,
                            cwd: Optional[str] = None) -> str:
        """'N issue(s) in <file>' for a command touching a source file.

        Wave 6 composition: Friday's IDE/static analysis rides along
        with execution. Only runs when ``FRIDAY_V4_IDE_PREFLIGHT`` is
        opted in, only for commands that name a file, and never raises
        — a missing analyzer is a silent skip.
        """
        try:
            from .desktop.ide import analyze_file, preflight_opted_in
            if not preflight_opted_in():
                return ""
            m = re.search(r"\b([\w./\\-]+\.\w{1,10})\b", command or "")
            if not m:
                return ""
            target = m.group(1)
            path = self._resolve_ide_path(target, cwd)
            res = analyze_file(path, cwd=cwd)
            if res.diagnostics:
                errors = res.error_count
                label = f"{errors} error(s)" if errors else \
                    f"{res.issue_count} issue(s)"
                return f"{label} in {res.display_path} (via {res.method})"
        except Exception as exc:
            logger.debug(f"ide preflight skipped: {exc}")
        return ""

    def _ide_response(self, text: str, action,
                      cwd: Optional[str] = None) -> TalkResult:
        """IDE intents — "what's wrong with src/main.py" → diagnostics.

        Wave 6: the target file is analyzed through the IDE layer (LSP
        when a server is available, the built-in AST analyzer always)
        and the findings are reported with their method. No file → ask;
        no findings → honest "nothing wrong"; unanalyzable → honest
        "can't analyze". Never fabricates.
        """
        target = getattr(action, "target", None) or ""
        if not target:
            return TalkResult(
                text, "ide", "clarification",
                response="Which file should I look at? Try 'what's wrong "
                         "with src/main.py' or 'diagnose auth.py'.")
        path = self._resolve_ide_path(target, cwd)
        try:
            from .desktop.ide import analyze_file
            res = analyze_file(path, cwd=cwd)
        except Exception as exc:
            logger.warning(f"ide analysis failed: {exc}")
            return TalkResult(text, "ide", "failed",
                              response=f"I couldn't analyze {target}: {exc}.")
        if res.method == "none":
            return TalkResult(
                text, "ide", "chat",
                response=f"{target} isn't a readable source file I can "
                         "analyze.")
        if not res.diagnostics:
            return TalkResult(
                text, "ide", "chat",
                response=f"I looked at {res.display_path} — no issues found "
                         f"(via {res.method}).",
                status="succeeded", goal=target)
        errors = res.error_count
        issues = "; ".join(d.brief() for d in res.diagnostics[:3])
        # "3 error(s)" only when every finding is an error; mixed findings
        # are "N issue(s) (1 error)" — never overstate severity.
        if errors and errors == res.issue_count:
            label = f"{res.issue_count} error(s)"
        elif errors:
            label = f"{res.issue_count} issue(s) ({errors} error)"
        else:
            label = f"{res.issue_count} issue(s)"
        response = (f"I found {label} in "
                    f"{res.display_path} (via {res.method}): {issues}.")
        return TalkResult(text, "ide", "ide", response=response,
                          status="succeeded", goal=target)

    def _create_mission(self, text: str, action,
                        cwd: Optional[str] = None) -> TalkResult:
        """PLAN intents — "ship the auth refactor by Friday" → mission.

        With ``FRIDAY_V4_CLAUDE_PLANNER=1`` (explicit opt-in, Wave 18
        close-out — same convention as Wave 13's ``FRIDAY_V4_LLM``),
        the planner carries the ``ClaudePlanner`` enhancer: goal
        decomposition delegates to the Claude Code CLI (gated,
        sandboxed, audited — read-only tools). Any refusal/failure
        falls back to the deterministic planner, so mission creation
        never crashes and never depends on Claude.
        """
        goal = action.goal or text
        try:
            from .missions import (
                MissionEngine,
                MissionStatus,
                make_planner,
            )
            # One planner construction point: with
            # ``FRIDAY_V4_CLAUDE_PLANNER`` set, goal decomposition
            # delegates to Claude Code (gated, sandboxed, audited);
            # otherwise the deterministic planner — never a crash, never
            # a dependency on claude.
            engine = MissionEngine(
                self.conn, planner=make_planner(cwd=cwd, conn=self.conn))
            mission = engine.create(goal)
            if not mission:
                return TalkResult(text, "plan", "failed",
                                  response="I couldn't create that mission.")
            steps = len(mission.steps)
            exe = sum(1 for s in mission.steps if s.is_executable)
            return TalkResult(
                text, "plan", "mission_created",
                response=(f"Mission created: '{mission.title}' — {steps} "
                          f"step(s), {exe} I can execute. I'll track its "
                          f"progress as you work through it."),
                goal=goal, mission_id=mission.id, status=mission.status.value)
        except Exception as exc:
            logger.warning(f"nl_router mission create failed: {exc}")
            return TalkResult(text, "plan", "failed",
                              response=f"I couldn't create that mission: {exc}",
                              goal=goal)

    def _replan_response(self, text: str, action,
                         cwd: Optional[str] = None) -> TalkResult:
        """'replan this mission' — re-decompose the latest mission.

        The latest ACTIVE mission (else the latest mission overall) is
        re-planned on its own goal: ``MissionEngine.replan`` runs the
        planner again — through Claude Code when opted in — and reports
        "plan changed because …" honestly (the Wave 9 adaptation
        contract). Honest when there is no mission yet.
        """
        try:
            from .missions import MissionEngine, MissionStatus, make_planner
        except Exception as exc:
            logger.warning(f"nl_router replan: missions unavailable: {exc}")
            return TalkResult(text, "plan", "failed",
                              response="I couldn't access my missions.")
        try:
            engine = MissionEngine(
                self.conn, planner=make_planner(cwd=cwd, conn=self.conn))
            mission = None
            for m in engine.list(status=MissionStatus.ACTIVE.value):
                mission = m
                break
            if mission is None:
                for m in engine.list():
                    mission = m
                    break
            if mission is None:
                return TalkResult(
                    text, "plan", "chat",
                    response="I don't have a mission to replan yet — say "
                             "'ship the auth refactor by Friday' and I'll "
                             "plan it.")
            goal = mission.title or action.goal or text
            report_res = engine.replan(
                mission.id, goal, reason=_replan_reason(text), cwd=cwd)
            reloaded = engine.get(mission.id)
            steps = len(reloaded.steps) if reloaded else 0
            msg = getattr(report_res, "message", "") or "plan updated"
            if getattr(report_res, "changed", False):
                response = (f"{msg} The replanned mission has {steps} "
                            f"step(s).")
            else:
                response = (f"I re-planned '{mission.title}' — the plan "
                            f"didn't change ({steps} step(s)).")
            return TalkResult(
                text, "plan", "mission_replanned", response=response,
                mission_id=mission.id,
                status=(reloaded.status.value if reloaded else None))
        except Exception as exc:
            logger.warning(f"nl_router replan failed: {exc}")
            return TalkResult(
                text, "plan", "failed",
                response=f"I couldn't replan that mission: {exc}")

    def _desktop(self, text: str) -> TalkResult:
        if self.desktop_handler is None:
            return TalkResult(
                text, "desktop", "chat",
                response="Desktop control isn't wired here — try the voice "
                         "interface.")
        try:
            response = self.desktop_handler(text) or ""
            return TalkResult(text, "desktop", "desktop",
                              response=response or "Done.")
        except Exception as exc:
            logger.warning(f"desktop handler failed: {exc}")
            return TalkResult(text, "desktop", "failed",
                              response=f"Desktop action failed: {exc}")

    def _recent_history(self) -> list[dict]:
        """Recent exchanges (oldest first) for follow-up context.

        Wave 13: the reasoning engine's LLM synthesis prompt receives
        this so "and the tests?" after "what's the status?" resolves
        with context. Never raises — empty history when unavailable.
        """
        if self.conn is None:
            return []
        from . import db
        return db.recent_exchange_history(self.conn)

    def _log_exchange(self, text: str, result: TalkResult) -> None:
        """Record the utterance + reply in the conversation log (verbatim).

        The brain learns from what the operator *actually said* — no
        keywords, no extraction. Both the reasoning conversation
        provider ("what did we talk about") and the persona engine
        ("who am I") read this log. Guarded: never raises; skipped when
        no connection is available (e.g. voice router passes none).
        """
        if self.conn is None:
            return
        try:
            from . import db
            # Wave 15 — one presence: every surface (talk/voice/web)
            # appends to the SAME shared session (one per UTC day), so
            # a conversation started here continues on every other
            # surface and is recalled by time window.
            sid = db.get_or_create_shared_session(self.conn)
            if not sid:
                return
            intent = getattr(result, "intent", "") or ""
            db.log_exchange(self.conn, sid, "user", text, intent=intent)
            db.log_exchange(self.conn, sid, "friday",
                            getattr(result, "response", "") or "",
                            intent=intent)
        except Exception as exc:
            logger.debug(f"exchange log failed: {exc}")

    def _ask_response(self, text: str, action) -> TalkResult:
        """Answer an ASK intent via the reasoning engine (evidence-cited).

        Falls back to the proactive status line only when reasoning is
        unavailable; never fabricates an answer. Wave 13: recent
        exchanges are threaded into the reasoning call so voice/talk
        follow-ups get the same conversation context as ``friday4 ask``.
        """
        # Pending-permission questions route to the autonomy loop, not
        # the reasoning engine: "what's pending" / "what are you asking"
        # lists Friday's open permission asks (surfaced through every
        # surface — talk, voice, web chat). The operator can then say
        # "yes, run it" / "no" to resolve them.
        lower = text.lower()
        if any(p in lower for p in ("what's pending", "what is pending",
                                    "what are you asking", "pending requests",
                                    "waiting on permission",
                                    "need my permission", "need my approval",
                                    "permission requests", "what do you need")):
            return self._pending_response(text)
        try:
            from .reasoning import answer as reason
            ans = reason(text, conn=self.conn,
                         history=self._recent_history())
            if ans.known and ans.text:
                return TalkResult(text, "ask", "chat",
                                  response=ans.text,
                                  goal=ans.question_type.value)
        except Exception as exc:
            logger.debug(f"reasoning unavailable ({exc}) — status fallback")
        # Degraded path: proactive status line (never a lie).
        try:
            from .proactive import AnticipationEngine
            pro = AnticipationEngine()
            try:
                suggestions = pro.get_suggestions()
                speak_now = [s for s in suggestions if s.should_speak]
                if speak_now:
                    return TalkResult(
                        text, "ask", "chat",
                        response=f"Here's what I've noticed: {speak_now[0].text}")
            finally:
                pro.cleanup()
        except Exception:
            pass
        return TalkResult(text, "ask", "chat",
                          response="I don't know yet — I don't have "
                                   "evidence about that. Ask me about "
                                   "project status, recent activity, "
                                   "or mission progress.")


    def _pending_response(self, text: str) -> TalkResult:
        """List Friday's open permission asks (the operator's view).

        "What's pending" / "what are you asking" surfaces the durable
        CONFIRM asks the autonomy loop raised — through the same NL
        surface (talk/voice/web). Honest when nothing is waiting, and
        it names the next ask so the operator can approve it with
        "yes, run it" or deny it with "no".
        """
        try:
            from .autonomy import AutonomyAgent
            conn = self.conn
            own = False
            if conn is None:
                from . import db
                conn = db.connect()
                own = True
            try:
                agent = AutonomyAgent(conn=conn)
                pending = agent.pending(limit=10)
                if not pending:
                    return TalkResult(
                        text, "ask", "chat",
                        response="Nothing is waiting on your permission "
                                 "right now — I'm either running AUTO work "
                                 "or waiting for context.")
                lines = " ".join(
                    f"{i + 1}. {r.get('description') or r.get('command') or r.get('action_type') or '?'}"
                    for i, r in enumerate(pending))
                return TalkResult(
                    text, "ask", "chat",
                    response=f"I'm waiting on {len(pending)} thing(s): {lines}. "
                             f"Say 'yes, run it' to approve or 'no' to decline.")
            finally:
                if own:
                    conn.close()
        except Exception as exc:
            logger.warning(f"nl_router pending failed: {exc}")
            return TalkResult(
                text, "ask", "failed",
                response="I couldn't read my pending requests right now.")

    def _research_response(self, text: str, action) -> TalkResult:
        """RESEARCH intents — Wave 11 surfaces (analyze/correlate/briefing).

        Law 1: the NL path is the product. Repo names come from the
        resolver's REPO/PATH entities or the raw text; the research
        layer stays evidence-cited and never raises.
        """
        try:
            from .research import analyze, correlate
            from .briefing import build_briefing
        except ImportError as exc:
            return TalkResult(text, "research", "failed",
                              response=f"Research layer unavailable: {exc}")

        lower = text.lower()
        # Briefing: "brief me" / "morning briefing" / "what happened".
        if "brief" in lower or "briefing" in lower:
            try:
                kind = "evening" if "evening" in lower else "morning"
                conn = self.conn
                from . import db as _db
                if conn is None:
                    conn = _db.connect()
                    own = True
                else:
                    own = False
                try:
                    b = build_briefing(conn, kind=kind)
                finally:
                    if own:
                        conn.close()
                return TalkResult(text, "research", "chat",
                                  response=b.text)
            except Exception as exc:
                return TalkResult(text, "research", "failed",
                                  response=f"Briefing failed: {exc}")

        # Correlate: "analyze X vs Y" / "what's the deal between X and Y".
        # ``_extract_pair`` handles the lead-before-pair phrasing (the
        # research lead is never an operand) and preserves the original
        # case so mixed-case repo names/paths survive.
        pair = _extract_pair(text)
        if pair:
            a, b = pair
            try:
                est = correlate(a, b)
                if not (est.shared_languages or est.overlapping_files):
                    return TalkResult(text, "research", "chat",
                                      response=f"I compared {a} and {b} — "
                                               "no shared signals found yet.")
                lines = "; ".join(est.evidence)
                return TalkResult(text, "research", "chat",
                                  response=lines)
            except Exception as exc:
                return TalkResult(text, "research", "failed",
                                  response=f"Correlation failed: {exc}")

        # Single-repo analyze: "analyze X" / "what's this repo".
        target = None
        for ent in action.entities:
            if ent.type.value in ("repo", "path"):
                target = ent.value
                break
        if not target:
            for prefix in ("analyze ", "what's this repo ", "what is this "
                           "repo "):
                if lower.startswith(prefix):
                    target = text[len(prefix):].strip()
                    break
        if not target:
            return TalkResult(text, "research", "clarification",
                              response="Analyze which repo? "
                                       "Try 'analyze vivaha' or "
                                       "'what's the deal between X and Y'.")
        try:
            profile = analyze(target)
            if not profile.available:
                return TalkResult(text, "research", "chat",
                                  response=f"{target} isn't a readable "
                                           "directory.")
            lines = "; ".join(profile.evidence) or "No notable signals."
            return TalkResult(text, "research", "chat", response=lines)
        except Exception as exc:
            return TalkResult(text, "research", "failed",
                              response=f"Analysis failed: {exc}")
    def _deny_response(self, text: str) -> TalkResult:
        """DENY intents — "no", "don't do that", "do it differently".

        The operator's veto. Resolves the oldest pending permission ask
        as denied and records an operator override so the autonomy loop
        never proposes that action again (until cleared). Honest when
        nothing is pending.
        """
        try:
            from .autonomy import AutonomyAgent
            conn = self.conn
            own = False
            if conn is None:
                from . import db
                conn = db.connect()
                own = True
            try:
                # The injected conn wins — resolution hits the same DB
                # the ask was created on (hermetic CLI tests + surfaces).
                agent = AutonomyAgent(conn=conn)
                pending = agent.pending(limit=1)
                if not pending:
                    return TalkResult(
                        text, "deny", "chat",
                        response="I don't have a pending request to "
                                 "cancel right now.")
                req = pending[0]
                if agent.deny(req["id"], reason=text):
                    what = req.get("description") or req.get("command") \
                        or "that action"
                    return TalkResult(
                        text, "deny", "denied",
                        response=(f"Understood — I won't do '{what}' and "
                                  f"I've noted it. I won't suggest it "
                                  f"again."),
                        goal=what)
                return TalkResult(
                    text, "deny", "failed",
                    response="I couldn't cancel that request.")
            finally:
                if own:
                    conn.close()
        except Exception as exc:
            logger.warning(f"nl_router deny failed: {exc}")
            return TalkResult(
                text, "deny", "failed",
                response=f"I couldn't do that: {exc}")

    def _accept_response(self, text: str, action, *,
                         confirm_fn: ConfirmFn, force: bool,
                         cwd: Optional[str] = None) -> TalkResult:
        """ACCEPT intents — "yes, run it" approves a dispatch suggestion.

        The operator is confirming Friday's earlier offer ("That matches
        your 'run-tests' skill — want me to run 'git status' next?"). The
        acceptance re-derives the current matching suggestion (skills
        match on context — the same recent actions that produced the
        offer) and runs its next step through the full execution
        pipeline: gate → sandbox → audit.

        Safety (gate semantics): the operator's "yes" IS the
        confirmation for CONFIRM-level steps (passed as a pre-approved
        ``confirm_fn``). NEVER-level steps (push/deploy) are only
        bypassable by an explicit ``force`` override — a bare "yes"
        never escalates them; they are denied honestly.
        """
        # The autonomy loop's durable permission asks resolve first — the
        # operator's "yes" approves whatever Friday asked permission for
        # (daemon → permission request → "yes, run it"). Falls back to
        # dispatch suggestions when nothing is pending.
        try:
            from . import db as _db
            pending = (_db.pending_permission_requests(self.conn, limit=1)
                       if self.conn is not None else [])
        except Exception:
            pending = []
        if pending:
            return self._accept_permission_request(
                text, pending[0], force=force, cwd=cwd)
        try:
            from .skills import SkillDispatcher
            dispatcher = SkillDispatcher(self.conn)
            suggestions = dispatcher.suggest(limit=1)
        except Exception as exc:
            logger.warning(f"accept: dispatch unavailable ({exc})")
            return TalkResult(
                text, "accept", "failed",
                response="I couldn't check for a pending suggestion.")
        if not suggestions:
            return TalkResult(
                text, "accept", "chat",
                response="I don't have a pending suggestion right now — "
                         "try 'what should I do next' or 'friday4 skills "
                         "dispatch' to see what matches your context.")
        suggestion = suggestions[0]
        name = suggestion.get("skill_name") or "that workflow"
        next_steps = suggestion.get("next_steps") or []
        if not next_steps:
            return TalkResult(
                text, "accept", "chat",
                response=f"Your '{name}' skill has nothing left to run.")
        # Multi-step acceptance → supervised mission ("watch me" →
        # dispatch → mission). The operator's "yes" approves the whole
        # remaining workflow: the steps become a mission so progress is
        # tracked (status / briefing / progress feed) and adaptation is
        # explicit. The first step executes NOW through the gate — the
        # same confirm/force semantics as the single-step path.
        if len(next_steps) > 1:
            return self._accept_as_mission(text, name, next_steps,
                                           force=force, cwd=cwd)
        step = next_steps[0]
        action_type = (step.get("action_type") or "").strip()
        command = (step.get("command") or "").strip()
        if not action_type:
            return TalkResult(
                text, "accept", "chat",
                response=f"Your '{name}' skill's next step isn't executable "
                         "by me.")
        try:
            from .execution import execute
            # The operator already said yes — that is the CONFIRM approval.
            # force stays as the caller passed it: NEVER steps (push,
            # deploy) remain blocked without an explicit override.
            result = execute(
                action_type, command, cwd=cwd, conn=self.conn,
                confirm_fn=lambda _desc: True, force=force,
                goal=f"skill '{name}' next step")
        except Exception as exc:
            logger.warning(f"accept execute failed: {exc}")
            return TalkResult(
                text, "accept", "failed",
                response=f"That didn't work — {exc}.",
                action_type=action_type, command=command, goal=name)

        status = getattr(result, "status", "failed")
        aid = getattr(result, "action_id", None)
        out = getattr(result, "output", "") or ""
        if status == "succeeded":
            first = out.strip().splitlines()[:1]
            spoken = (f"Done — {command or action_type or 'it'}"
                      f"{': ' + first[0][:160] if first else ''}.")
            return TalkResult(
                text, "accept", "executed", response=spoken,
                action_type=action_type, command=command, goal=name,
                action_id=aid, status=status)
        if status == "denied":
            return TalkResult(
                text, "accept", "denied",
                response=(f"The next step '{command}' needs an explicit "
                          f"override — I won't do that on a bare yes. "
                          f"Use friday4 talk --force."),
                action_type=action_type, command=command, goal=name,
                action_id=aid, status=status)
        return TalkResult(
            text, "accept", "failed",
            response=f"That didn't work — {status}.",
            action_type=action_type, command=command, goal=name,
            action_id=aid, status=status)

    def _accept_permission_request(self, text: str, req: dict, *,
                                   force: bool,
                                   cwd: Optional[str] = None) -> TalkResult:
        """Approve a pending permission request from the autonomy loop.

        The operator's "yes" approves the durable ask; the action runs
        through the real gate → sandbox → audit (via AutonomyAgent.accept
        — the operator's words are the CONFIRM approval, NEVER steps stay
        blocked without force).
        """
        rid = req.get("id")
        what = (req.get("description") or req.get("command")
                or req.get("action_type") or "it")
        try:
            from .autonomy import AutonomyAgent
            outcome = AutonomyAgent(conn=self.conn).accept(rid, force=force)
        except Exception as exc:
            logger.warning(f"accept permission request failed: {exc}")
            return TalkResult(text, "accept", "failed",
                              response=f"I couldn't run that: {exc}",
                              goal=what)
        if not outcome:
            return TalkResult(
                text, "accept", "chat",
                response="That request is no longer pending.", goal=what)
        status = outcome.get("status", "failed")
        aid = outcome.get("action_id")
        out = outcome.get("output", "") or ""
        if status == "succeeded":
            first = out.strip().splitlines()[:1]
            spoken = (f"Done — {what}"
                      f"{': ' + first[0][:160] if first else ''}.")
            return TalkResult(text, "accept", "executed", response=spoken,
                              action_type=req.get("action_type"),
                              command=req.get("command"),
                              goal=what, action_id=aid, status=status,
                              mission_id=None)
        if status == "denied":
            return TalkResult(
                text, "accept", "denied",
                response=(f"'{what}' needs an explicit override — I won't "
                          f"do that on a bare yes."),
                action_type=req.get("action_type"),
                command=req.get("command"),
                goal=what, action_id=aid, status=status)
        return TalkResult(
            text, "accept", "failed",
            response=f"That didn't work — {status}.",
            action_type=req.get("action_type"),
            command=req.get("command"),
            goal=what, action_id=aid, status=status)

    def _accept_as_mission(self, text: str, name: str,
                           next_steps: list[dict], *,
                           force: bool,
                           cwd: Optional[str] = None) -> TalkResult:
        """Run a multi-step accepted suggestion as a supervised mission.

        The dispatch suggestion's remaining steps become a mission
        (``MissionEngine.create`` with an explicit plan) and the first
        step executes immediately through the real pipeline — same gate
        semantics as the single-step accept (the operator's "yes" is the
        CONFIRM approval; ``force`` stays the caller's, so NEVER steps
        remain blocked without an explicit override).

        The mission persists afterward: status, briefings and the
        progress feed report it, and adaptation is explicit
        ("plan changed because…"). Never crashes — any failure returns
        an honest error response.
        """
        try:
            from .missions import MissionEngine, StepPlan
            engine = MissionEngine(self.conn)
            plan = [
                StepPlan(
                    title=f"{name}: {s.get('command') or s.get('action_type') or 'step'}",
                    action_type=(s.get("action_type") or "").strip() or None,
                    command=(s.get("command") or "").strip(),
                    cwd=cwd,
                )
                for s in next_steps
            ]
            mission = engine.create(f"continue: {name}", plan=plan)
            if not mission:
                return TalkResult(
                    text, "accept", "failed",
                    response="I couldn't start that as a mission.")
            engine.start(mission.id)
            outcome = engine.advance(
                mission.id, confirm_fn=lambda _desc: True, force=force)
        except Exception as exc:
            logger.warning(f"accept mission failed: {exc}")
            return TalkResult(
                text, "accept", "failed",
                response=f"That didn't work — {exc}.", goal=name)

        mid = outcome.mission_id
        exe = outcome.execution or {}
        # The first remaining step is what ran now (execution.to_dict()
        # carries status/output/action_id but not the command).
        first_step = next_steps[0] or {}
        step_action = (first_step.get("action_type") or "").strip()
        step_command = (first_step.get("command") or "").strip()
        if outcome.action == "executed":
            first = (exe.get("output") or "").strip().splitlines()[:1]
            spoken = (f"Done — {step_command or step_action or 'the first step'}"
                      f"{': ' + first[0][:160] if first else ''}. "
                      f"I'll track the rest of '{name}' as mission {mid}.")
            return TalkResult(
                text, "accept", "executed", response=spoken,
                action_type=step_action,
                command=step_command, goal=name,
                mission_id=mid, status=exe.get("status", "succeeded"),
                action_id=exe.get("action_id"))
        if outcome.action == "denied":
            return TalkResult(
                text, "accept", "denied",
                response=(f"The first step needs an explicit override — I "
                          f"won't do that on a bare yes. The mission {mid} "
                          f"is saved; use friday4 talk --force to run it."),
                goal=name, mission_id=mid, status="denied")
        if outcome.action in ("manual_completed", "none_pending"):
            # A step with no executor, or all steps already done — never
            # misreport as a failure (defensive: SkillDispatcher steps
            # always carry an action_type in practice).
            return TalkResult(
                text, "accept", "chat",
                response=f"I've tracked '{name}' as mission {mid} — "
                         f"{outcome.message}.",
                goal=name, mission_id=mid)
        return TalkResult(
            text, "accept", "failed",
            response=f"That didn't work — {outcome.message}.",
            goal=name, mission_id=mid, status="failed")

    def _security_response(self, text: str, action,
                           cwd: Optional[str] = None) -> TalkResult:
        """SECURITY intents — "scan my repo" runs the Wave 3 scanner.

        §2 hardening: security is no longer CLI-only. The NL trigger
        runs ``VulnerabilityScanner`` over the named path (or the
        working directory), returns the graded report, and publishes
        high-severity findings onto the ambient bus (Wave 11 push — the
        briefing/web feed see them). Never crashes; a missing scanner or
        unreadable path degrades to an honest error.
        """
        target = getattr(action, "target", None) or ""
        path = target.strip() if target else (cwd or ".")
        try:
            from .security.scanner import VulnerabilityScanner
            report = VulnerabilityScanner().scan_quick(path, threshold="low")
        except Exception as exc:
            logger.warning(f"security scan failed: {exc}")
            return TalkResult(
                text, "security", "failed",
                response=f"I couldn't scan {path or 'this project'}: {exc}.")

        # Wave 11 push: high/critical findings become durable ambient
        # events so the briefing and web feed surface them (never blocks
        # — a missing bus degrades silently).
        self._push_security_findings(report)

        summary = report.summary()
        counts = report.counts_by_severity()
        high = counts.get("high", 0) + counts.get("critical", 0)
        if high:
            top = [f for f in report.findings if f.severity in ("high", "critical")]
            detail = "; ".join(
                f"{f.title} ({f.severity})" for f in top[:3])
            response = (f"Security scan of {path}: {summary}. "
                        f"{high} high/critical finding(s): {detail}.")
        else:
            response = f"Security scan of {path}: {summary} — all clear."
        return TalkResult(text, "security", "security", response=response,
                          status="succeeded", goal=path)

    def _push_security_findings(self, report) -> None:
        """Publish high/critical findings onto the ambient bus (durable)."""
        try:
            high = [f for f in report.findings
                    if f.severity in ("high", "critical")]
            if not high:
                return
            from .ambient import AmbientBus, Event, Priority
            bus = AmbientBus(self.conn) if self.conn else AmbientBus()
            for f in high[:5]:
                loc = f.file or f.package or "?"
                bus.publish(Event(
                    topic="security",
                    payload=f"{f.severity.upper()} — {loc} · "
                            f"{(f.detail or f.title or '')[:120]}",
                    priority=Priority.IMPORTANT,
                    source="nl.security"))
        except Exception as exc:
            logger.debug(f"security ambient push failed: {exc}")

    def _memory_response(self, text: str, action) -> TalkResult:
        """MEMORY intents — "remember that X" stores, "forget X" removes.

        §2 hardening (Wave 10): memory is no longer CLI-only. With the
        operator's explicit consent ("remember that…", "I prefer…"),
        the statement becomes a proposition in ``FactMemory`` (subject
        ``operator``, provenance ``talk``); "forget X" removes it.
        Storing never fabricates — the operator's words are the fact.
        """
        try:
            from .memory import FactMemory, DECAY_TIME
        except Exception as exc:
            logger.warning(f"memory layer unavailable: {exc}")
            return TalkResult(
                text, "memory", "failed",
                response="I couldn't access my memory right now.")
        if self.conn is None:
            return TalkResult(
                text, "memory", "failed",
                response="I don't have my memory connected here.")

        lower = text.lower()
        trigger = getattr(action, "target", None) or ""
        fact_text = self._memory_fact_text(text, action)
        if trigger == "forget" or "forget" in lower:
            return self._memory_forget(text, fact_text)
        if not fact_text:
            return TalkResult(
                text, "memory", "clarification",
                response="What should I remember? Try 'remember that I "
                         "prefer Rust' or 'note that the deploy uses git "
                         "push'.")
        try:
            facts = FactMemory(self.conn)
            facts.remember("operator", "note", fact_text,
                           source="talk", confidence=0.85,
                           decay_policy=DECAY_TIME)
        except Exception as exc:
            logger.warning(f"memory store failed: {exc}")
            return TalkResult(
                text, "memory", "failed",
                response=f"I couldn't store that: {exc}.")
        return TalkResult(
            text, "memory", "memory",
            response=(f"Noted — I'll remember that. "
                      f"(Say 'forget that' to remove it.)"),
            goal=fact_text)

    def _memory_fact_text(self, text: str, action) -> str:
        """The fact statement from an utterance (LLM goal or rules strip)."""
        goal = getattr(action, "goal", None) or ""
        if goal and goal.strip() and goal.strip().lower() not in (
                "remember", "forget"):
            return goal.strip()
        lower = text.lower()
        for prefix in ("remember that ", "remember ", "note that ",
                       "don't forget that ", "do not forget that ",
                       "i prefer ", "i like "):
            if lower.startswith(prefix):
                return text[len(prefix):].strip().rstrip(".!")
        return ""

    def _memory_forget(self, text: str, fact_text: str) -> TalkResult:
        """Forget a stored fact (subject-scoped, honest when empty)."""
        try:
            from .memory import FactMemory
            facts = FactMemory(self.conn)
            removed = False
            if fact_text:
                # Forget any operator note matching the phrase.
                for fact in facts.recall(subject="operator", limit=1000):
                    if (fact_text.lower() in fact.value.lower()
                            or fact_text.lower() in fact.key.lower()):
                        if facts.forget(fact.subject, fact.predicate):
                            removed = True
            else:
                removed = facts.forget("operator")
        except Exception as exc:
            logger.warning(f"memory forget failed: {exc}")
            return TalkResult(
                text, "memory", "failed",
                response=f"I couldn't forget that: {exc}.")
        if removed:
            return TalkResult(
                text, "memory", "memory",
                response="Done — I've forgotten that.")
        return TalkResult(
            text, "memory", "chat",
            response="I don't have anything stored that matches.")

    def _help_response(self, text: str) -> TalkResult:
        """HELP intents — answered from the real capability registry.

        Wave 16 (Law 7): "what can you do" is the truth about Friday's
        registered capabilities (executors + providers + intents +
        surfaces + learned skills), never a hardcoded string. Degrades
        to the classic concise help when the registry is unavailable.
        """
        try:
            from .capability import describe_capabilities
            description = describe_capabilities(self.conn)
            if description and "Here's what I can do" in description:
                return TalkResult(
                    text, "help", "chat",
                    response=(description + " Try 'run the tests', "
                              "'ship the auth refactor', or 'what can "
                              "you do'."))
        except Exception as exc:
            logger.debug(f"help via registry failed: {exc}")
        return TalkResult(
            text, "help", "chat",
            response="I can run your tests and tooling, check git, read "
                     "and edit files, run scripts, track missions, and "
                     "answer questions with evidence. Try 'run the tests' "
                     "or 'ship the auth refactor'.")

    def _style_response(self, text: str, action) -> TalkResult:
        """STYLE intents — adaptive identity (Wave 17, MCU test #4).

        "be more casual, Tony" → tone shifts this session AND persists;
        Friday can always explain why she talks the way she does. The
        direction is stored in the relationship layer (direction wins
        over the depth-derived default) and read back by every surface
        (reasoning style_provider answers "why do you talk that way").

        Consent-first, never extracted from passive speech: only an
        explicit direction is stored, verbatim with its request words.
        """
        try:
            from .relationship import RelationshipEngine, DIRECTION_TONES
        except Exception as exc:
            logger.warning(f"style: relationship layer unavailable: {exc}")
            return TalkResult(text, "style", "failed",
                              response="I couldn't change my tone right now.")

        lower = text.lower()
        target = getattr(action, "target", None) or ""
        request = getattr(action, "goal", None) or text
        conn = self.conn
        own = False
        if conn is None:
            try:
                from . import db
                conn = db.connect()
                own = True
            except Exception:
                conn = None
        try:
            engine = RelationshipEngine(conn)
            # Reset path: "be yourself again" / "back to normal".
            if target == "reset" or any(w in lower for w in (
                    "be yourself", "back to normal", "reset your tone",
                    "reset your personality")):
                status = engine.clear_direction()
                if own and conn is not None:
                    conn.close()
                return TalkResult(
                    text, "style", "style",
                    response=("Done — I'm back to my natural tone, which "
                              "adapts to how close we've become."))

            tone = None
            verbosity = None
            # Verbosity directions: "less chatter" / "be briefer" → 2,
            # "be more detailed" → 4. A tone target wins if also present.
            if target in ("brief", "detailed"):
                verbosity = 2 if target == "brief" else 4
            if target in DIRECTION_TONES:
                tone = target
            # Fallback: pull the tone straight from the utterance when
            # the resolver didn't thread it (LLM path sets target).
            if tone is None:
                for t in DIRECTION_TONES:
                    if t in lower:
                        tone = t
                        break
            if tone is None and verbosity is None:
                if own and conn is not None:
                    conn.close()
                return TalkResult(
                    text, "style", "clarification",
                    response=("How would you like me to talk? Try 'be more "
                              "casual', 'be more formal', 'less chatter', or "
                              "'be yourself again'."))

            status = engine.set_direction(tone=tone, verbosity=verbosity,
                                          request=request)
            # The reply names only what was explicitly requested — a
            # verbosity-only direction ("less chatter") must never
            # echo the *depth-derived* tone as "more neutral".
            what: list[str] = []
            if tone is not None:
                what.append(f"more {tone}")
            if verbosity is not None:
                label = "briefly" if verbosity <= 2 else "in detail"
                what.append(label)
            phrase = " and ".join(what) or "that way"
            if own and conn is not None:
                conn.close()
            return TalkResult(
                text, "style", "style",
                response=(f"Got it — I'll talk {phrase} from now on. "
                          f"(Say 'why do you talk that way' and I'll "
                          f"tell you why.)"),
                goal=request)
        except Exception as exc:
            logger.warning(f"nl_router style failed: {exc}")
            if own and conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return TalkResult(text, "style", "failed",
                              response=f"I couldn't do that: {exc}")

    def _skill_response(self, text: str, action) -> TalkResult:
        """SKILL intents — Wave 14 demonstration capture (watch me).

        The audit log IS the demonstration record: "watch me do this"
        tags a window on it; "learn this" / "stop watching" parameterize
        what the operator did into a shadow skill; "what did you learn"
        lists the skills formed. Repo context generalizes the match.
        """
        lower = text.lower()
        trigger = getattr(action, "target", None) or ""
        try:
            from . import db
            conn = self.conn
            own = False
            if conn is None:
                conn = db.connect()
                own = True
            try:
                if ("stop watching" in lower or "stop watch" in lower
                        or trigger == "stop"
                        or "learn this" in lower or "learn that" in lower
                        or "learn from" in lower or "teach yourself" in lower
                        or "remember this" in lower or "record this" in lower):
                    return self._skill_stop(text, conn)
                if ("what did you learn" in lower
                        or "what have you learned" in lower
                        or "what do you know" in lower
                        or "show me your skills" in lower):
                    return self._skill_summary(text, conn)
                if ("watch" in lower or "learn" in lower
                        or "show you" in lower or trigger == "start"):
                    return self._skill_start(text, conn)
            finally:
                if own:
                    conn.close()
        except Exception as exc:
            logger.warning(f"nl_router skill failed: {exc}")
            return TalkResult(text, "skill", "failed",
                              response=f"I couldn't do that: {exc}")
        return TalkResult(
            text, "skill", "clarification",
            response="Say 'watch me do this' to start, 'learn this' to "
                     "form a skill, or 'what did you learn' to see my skills.")

    def _skill_start(self, text: str, conn) -> TalkResult:
        """'watch me do this' — open a demonstration capture."""
        from .skills import WatchRecorder
        name = _skill_name_hint(text)
        watcher = WatchRecorder(conn)
        wid = watcher.start(name=name)
        if not wid:
            return TalkResult(text, "skill", "failed",
                              response="I couldn't start watching.")
        hint = f" as '{name}'" if name else ""
        return TalkResult(
            text, "skill", "watching",
            response=f"I'm watching{hint} — go ahead. When you're done, "
                     f"say 'learn this'.",
            goal=name or None)

    def _skill_stop(self, text: str, conn) -> TalkResult:
        """'learn this' / 'stop watching' — form a skill from the capture."""
        from .skills import WatchRecorder
        watcher = WatchRecorder(conn)
        name = _skill_name_hint(text)
        if watcher.active() is None:
            return TalkResult(
                text, "skill", "chat",
                response="I wasn't watching anything. Say 'watch me do "
                         "this' to start.")
        formed = watcher.stop(name=name)
        if not formed:
            return TalkResult(
                text, "skill", "chat",
                response="I didn't catch any actions to learn from — "
                         "try 'watch me do this' then do the thing.")
        skill = formed["skill"]
        count = formed.get("actions", 0)
        return TalkResult(
            text, "skill", "skill_formed",
            response=(f"I watched {count} action(s) and formed the "
                      f"skill '{skill.name}' ({len(skill.steps)} step(s)). "
                      f"It's in shadow mode — I'll never run it until "
                      f"you approve it after it verifies."),
            goal=skill.name)

    def _skill_summary(self, text: str, conn) -> TalkResult:
        """'what did you learn' — the skills Friday has formed."""
        from .skills import SkillRegistry
        skills = SkillRegistry(conn).list(limit=20)
        if not skills:
            return TalkResult(
                text, "skill", "chat",
                response="I haven't formed any skills yet. Say 'watch me "
                         "do this' and I'll learn from you.")
        lines = "; ".join(
            f"{s.name} ({s.verification_state}, {len(s.steps)} steps)"
            for s in skills[:10])
        return TalkResult(
            text, "skill", "chat",
            response=f"I've learned {len(skills)} skill(s): {lines}.")


def _skill_name_hint(text: str) -> str:
    """A skill-name hint from the utterance ('watch me do deploy' → deploy)."""
    lower = text.lower()
    for prefix in ("watch me do ", "watch me ", "watch this ",
                   "learn this ", "learn how to ", "learn "):
        if lower.startswith(prefix):
            rest = text[len(prefix):].strip()
            rest_lower = rest.lower()
            # Filler words ('watch me do this') are not a name — leave
            # the generated sequence name instead.
            if (rest and not rest_lower.startswith(("do ", "a ", "an "))
                    and rest_lower not in ("this", "that", "it", "the",
                                           "them", "those")):
                return rest
    return ""


def voice_confirm(ask_fn: Callable[[str], str],
                  yes_words: tuple[str, ...] = ("yes", "yeah", "y", "sure",
                                                "go", "do it")) -> ConfirmFn:
    """A confirm function for the voice surface.

    ``ask_fn(question)`` asks the operator (e.g. TTS + STT) and returns
    the transcribed reply; anything in ``yes_words`` approves. Any
    failure (no reply, unclear) denies safely.
    """
    def _confirm(description: str) -> bool:
        try:
            reply = (ask_fn(description) or "").strip().lower()
            return any(w in reply for w in yes_words)
        except Exception:
            return False
    return _confirm


__all__ = ["TextCommandHandler", "TalkResult", "voice_confirm",
           "DesktopHandler", "ConfirmFn"]
