"""Voice Router — bridges voice input to FRIDAY's intelligence engines.

Routes transcribed speech through V4's own engines, in priority order:
  1. Desktop commands ("focus code editor", "switch workspace")
  2. Proactive briefings ("brief me", "what's new")
  3. Basic fallback (greetings, help, status)

V4 owns its conversation path. V3 is never used as a brain here; at
most its data may be consumed through read-only V4 data sources (e.g.
V3DataSource) elsewhere.

Every interaction is fed to the PatternLearner so FRIDAY learns the
user's workflow over time.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .chimes import play_chime
from .pipeline import PipelineState, VoicePipeline
from .tts import VoiceMode

logger = logging.getLogger("friday_v4.voice.router")


def _has_word(text: str, word: str) -> bool:
    """True if `word` appears as a whole word in `text`."""
    return bool(re.search(rf'\b{re.escape(word)}\b', text))


def _has_any_word(text: str, words: list[str]) -> bool:
    return any(_has_word(text, w) for w in words)


def _has_phrase(text: str, phrase: str) -> bool:
    return phrase in text


class VoiceRouter:
    """Routes voice input through FRIDAY's intelligence engines."""

    def __init__(self, pipeline: VoicePipeline,
                 enable_proactive: bool = True,
                 conn=None):
        """
        Args:
            pipeline: the voice pipeline (recording/TTS)
            enable_proactive: load the AnticipationEngine (default True)
            conn: V4 DB connection for the conversation log. When
                provided, every spoken utterance is recorded verbatim
                (feeds the reasoning conversation provider and the
                persona "who am I" answers — the Wiring Law: voice is a
                first-class entrypoint into the brain). None = no
                persistence (hermetic tests / degraded mode).
        """
        self.pipeline = pipeline
        self._conn = conn
        self._wm = None
        self._suggestions_shown: set[str] = set()
        self._interaction_count = 0
        self._proactive: Optional[Any] = None  # AnticipationEngine (lazy import)

        if enable_proactive:
            self._init_proactive()

    # ── Engine loading ─────────────────────────────────────────────────

    def _init_proactive(self) -> None:
        try:
            from ..proactive import AnticipationEngine
            self._proactive = AnticipationEngine()
            logger.info("Proactive Intelligence engine loaded")
        except Exception as exc:
            logger.debug(f"Proactive engine init failed: {exc}")
            self._proactive = None

    # ── Routing ────────────────────────────────────────────────────────

    def route(self, text: str) -> str:
        """Route transcribed speech through FRIDAY's engines."""
        if not text.strip():
            return ""
        lower = text.lower().strip()
        self._interaction_count += 1

        # 1. Desktop commands — immediate action
        desktop_response = self._try_desktop_command(lower, text)
        if desktop_response:
            self._observe_user_action(text, "desktop_command")
            return desktop_response

        # 2. Proactive briefings
        if self._proactive and (
            _has_any_word(lower, ["brief"]) or
            _has_phrase(lower, "what's new") or
            _has_phrase(lower, "what is new") or
            _has_phrase(lower, "what do you know")
        ):
            response = self._get_proactive_briefing()
            if response:
                return response

        # 3. Natural-language understanding — the Wave 9 brain.
        # "run the tests" now resolves to a real execution action and
        # flows through gate → sandbox → audit instead of the old
        # canned "I'm still learning" fallback.
        nlu_response = self._try_nlu_route(lower)
        if nlu_response:
            self._observe_user_action(text, "nlu")
            return nlu_response

        # 4. Fallback (greetings / canned chat)
        response = self._fallback_route(text)
        if response:
            self._observe_user_action(text, "fallback")
        return response or ""

    def _observe_user_action(self, text: str, category: str = "conversation"
                             ) -> None:
        """Feed user interaction to the PatternLearner."""
        if not self._proactive:
            return
        lower = text.lower()
        action_type = "user_input"
        if _has_any_word(lower, ["focus", "switch", "open", "go", "launch"]):
            action_type = "desktop_command"
        elif _has_any_word(lower, ["what", "who", "when", "where", "why", "how"]):
            action_type = "ask_question"
        elif _has_any_word(lower, ["status", "brief", "update"]) or "what's new" in lower:
            action_type = "check_status"
        elif _has_any_word(lower, ["hello", "hi", "hey", "thanks"]) or "good morning" in lower:
            action_type = "greeting"
        try:
            app_class = ""
            try:
                from ..desktop.wm_abstraction import WindowManager
                wm = WindowManager()
                active = wm.get_active_window()
                if active:
                    app_class = active.app_class
            except Exception:
                pass
            self._proactive.observe_activity(action_type, {
                "app": app_class, "category": category})
        except Exception:
            pass

    # ── NLU routing (Wave 9) ─────────────────────────────────────────

    def _try_nlu_route(self, lower: str) -> Optional[str]:
        """Route through the ONE NLU point (``nlu.resolve()``) → execution/missions.

        Returns a spoken response when the NLU layer produced an
        actionable outcome (executed action, created mission), or None
        to fall through to the canned fallback. Never raises.

        The desktop handler reuses this router's existing desktop
        command routing so ``nl_router`` never reimplements it.
        """
        try:
            from ..nl_router import TextCommandHandler, voice_confirm
        except ImportError as exc:
            logger.debug(f"NLU router unavailable: {exc}")
            return None
        try:
            llm = None
            try:
                from ..nlu import LLMClient
                llm = LLMClient()
            except Exception:
                llm = None
            handler = TextCommandHandler(
                conn=self._conn,
                llm=llm,
                desktop_handler=lambda t: self._try_desktop_command(
                    t.lower(), t) or "")
            result = handler.handle(
                lower,
                confirm_fn=voice_confirm(self._ask_voice_confirm),
            )
        except Exception as exc:
            logger.debug(f"NLU route failed: {exc}")
            return None
        if result.action in ("executed", "mission_created", "denied",
                             "failed", "security", "memory"):
            return result.response
        if result.action == "clarification":
            return result.response
        # ASK intents: the reasoning layer answers (evidence-cited) with
        # action="chat" — route those spoken questions through the brain
        # instead of dropping them to the canned fallback. This is the
        # Wave 9 wiring that made "who am I?" typed-only; voice inherits
        # the same answers now. ACCEPT chat responses ("I don't have a
        # pending suggestion") and executed/denied outcomes are spoken
        # too — the dispatch offer round-trip needs them. Greetings/help
        # still fall through to the canned flavor intentionally.
        if result.intent in ("ask", "accept") and result.response:
            return result.response
        return None  # chat/greeting → canned fallback keeps its flavor

    def _ask_voice_confirm(self, description: str) -> str:
        """Ask the operator (TTS) and capture a spoken y/N reply.

        Degrades to "" (deny) when the pipeline can't listen back.
        """
        try:
            self.pipeline.speak(f"{description} — proceed?")
            return self.pipeline.stop_recording_and_process() or ""
        except Exception:
            return ""

    # ── Fallback routing ───────────────────────────────────────────────

    def _fallback_route(self, text: str) -> str:
        lower = text.lower().strip()

        if _has_any_word(lower, ["hello", "hi", "hey"]) or \
           _has_phrase(lower, "good morning") or _has_phrase(lower, "good evening"):
            return "Hello. I'm Friday, your AI operating partner. How can I help you?"

        if _has_any_word(lower, ["status"]) or \
           _has_phrase(lower, "what's new") or _has_phrase(lower, "what's going on") or \
           _has_phrase(lower, "what is new") or _has_phrase(lower, "how are things"):
            return self._get_status_response()

        if "who are you" in lower or "what are you" in lower:
            return ("I'm Friday, your AI operating partner. I help you manage "
                    "your software projects, monitor your codebase, and keep "
                    "you informed about what matters.")

        if _has_phrase(lower, "what am i working on") or \
           _has_phrase(lower, "what's on my screen") or \
           _has_phrase(lower, "what is open") or \
           _has_phrase(lower, "what windows") or \
           _has_phrase(lower, "show desktop"):
            return self._get_desktop_status()

        if _has_any_word(lower, ["thanks", "nice"]) or _has_phrase(lower, "thank you") or \
           _has_phrase(lower, "good job"):
            return "You're welcome."

        if _has_any_word(lower, ["goodbye", "bye", "exit", "quit"]) or \
           _has_phrase(lower, "see you"):
            return "Goodbye. I'll be here when you need me."

        return (f"I heard you say: '{text}'. I'm still learning to route "
                f"this through Friday's full intelligence. Try asking me "
                f"about your projects or saying 'what's new'.")

    # ── Desktop commands ───────────────────────────────────────────────
    #
    # ONE desktop NL language everywhere. Voice delegates to the same
    # ``desktop_text_command`` interpreter the CLI, web dashboard, and
    # phone companion use — compound commands ("open chrome on workspace
    # 3 and open whatsapp"), workspace targeting, browser choice ("in
    # firefox"), web destinations ("open whatsapp" → web.whatsapp.com),
    # site search ("open youtube and cristiano ronaldo channel"), and
    # web-search fallback ("open c++ compiler of programiz") all work
    # spoken too. Returns None when nothing matched so callers fall
    # through to the next route.

    def _try_desktop_command(self, lower: str, original: str) -> Optional[str]:
        # Gate on the deterministic rules classifier (NOT the LLM-first
        # resolve() — the real-time voice path must stay instant): only a
        # genuine desktop intent reaches the desktop interpreter. "yes,
        # run it" is accept, not desktop.
        try:
            from ..nlu.intent import _fallback_classify
            if _fallback_classify(original).intent.value != "desktop":
                return None
        except Exception:
            return None
        try:
            from ..desktop.wm_abstraction import desktop_text_command
        except ImportError:
            return None
        response = desktop_text_command(original)
        return response or None

    def _get_desktop_status(self, wm=None) -> str:
        try:
            if wm is None:
                wm = self._get_wm()
            if not wm or not wm.is_available:
                return "Desktop monitoring is not available on this system."
            active = wm.get_active_window()
            windows = wm.list_windows()
            workspaces = wm.list_workspaces()
            parts = []
            if active:
                parts.append(f"You're in {active.app_name} on workspace {active.workspace_id}")
                if active.title and "friday" not in active.title.lower():
                    parts.append(f"Working on {active.title[:40]}")
            parts.append(f"{len(windows)} windows open across {len(workspaces)} workspaces")
            return ". ".join(parts) + "."
        except Exception:
            return "I couldn't check your desktop status right now."

    def _get_status_response(self) -> str:
        if self._proactive:
            try:
                suggestions = self._proactive.get_suggestions()
                speak_now = [s for s in suggestions if s.should_speak]
                if speak_now:
                    return f"I've been watching your workflow. {speak_now[0].text}"
            except Exception:
                pass
        return "All quiet. No significant changes to report."

    # ── Proactive integration ──────────────────────────────────────────

    def _get_proactive_briefing(self) -> Optional[str]:
        if not self._proactive:
            return None
        try:
            context_summary = self._proactive.get_context_summary()
            suggestions = self._proactive.get_suggestions()
            speak_now = [s for s in suggestions if s.should_speak]
            parts = [context_summary]
            for s in speak_now[:2]:
                parts.append(s.text)
            stats = self._proactive.get_learning_stats()
            patterns = stats.get("patterns", {})
            if patterns.get("action_pairs_learned", 0) > 2:
                parts.append(
                    f"I've learned {patterns['action_pairs_learned']} "
                    "patterns from your workflow.")
            return " ".join(parts) + "."
        except Exception as exc:
            logger.debug(f"Proactive briefing failed: {exc}")
            return None

    def _pending_permission_ask(self) -> Optional[str]:
        """A spoken prompt for the oldest pending permission ask.

        The autonomy loop asks permission durably; voice surfaces the
        ask on session start / idle so the operator can answer with
        "yes, run it" / "no" (both route through the ONE NLU point).
        Returns None when nothing is pending or the DB is unavailable.
        """
        if self._conn is None:
            return None
        try:
            from ..autonomy import AutonomyAgent
            pending = AutonomyAgent(conn=self._conn).pending(limit=1)
            if not pending:
                return None
            what = (pending[0].get("description") or pending[0].get("command")
                    or pending[0].get("action_type") or "that")
            return (f"I need your permission: {what}. "
                    f"Say yes, run it — or no.")
        except Exception as exc:
            logger.debug(f"Pending permission ask failed: {exc}")
            return None

    def _skill_offer(self) -> Optional[str]:
        """A dispatch-suggestion offer, if the current context matches.

        Wave 14 close-out: "yes, run it" only makes sense if Friday
        *offered* something first. On session start / idle, a matching
        promoted skill yields the natural offer: "That matches your
        'run-tests' skill — want me to run 'git status' next?" The
        operator's "yes, run it" then flows back through the ONE NLU
        point (Intent.ACCEPT) → the gate → execution.

        Returns the spoken offer text or None. Never raises — a missing
        DB/conn degrades to no offer.
        """
        if self._conn is None:
            return None
        try:
            from ..skills import SkillDispatcher
            dispatcher = SkillDispatcher(self._conn)
            suggestions = dispatcher.suggest(limit=1)
            if not suggestions:
                return None
            return dispatcher.prompt(suggestions[0])
        except Exception as exc:
            logger.debug(f"Skill offer failed: {exc}")
            return None

    def proactive_notify(self, force: bool = False) -> Optional[str]:
        """Check for proactive suggestions on session start / idle."""
        # Autonomy first: a pending permission ask is the most important
        # thing Friday is waiting on — the operator's "yes, run it" /
        # "no" resolves it through the same NL loop. Surface it before
        # dispatch offers / proactive hints.
        pending_ask = self._pending_permission_ask()
        if pending_ask and (force or pending_ask not in self._suggestions_shown):
            self._suggestions_shown.add(pending_ask)
            if len(self._suggestions_shown) > 100:
                self._suggestions_shown.clear()
            play_chime("done")
            self.pipeline.speak(pending_ask)
            logger.info(f"Autonomy ask: {pending_ask[:60]}")
            return pending_ask
        # Wave 14: the dispatch offer comes first — accepting it ("yes,
        # run it") is the operator-visible loop. Falls through to the
        # proactive engine when nothing matches a promoted skill.
        skill_offer = self._skill_offer()
        if skill_offer and (force or skill_offer not in self._suggestions_shown):
            self._suggestions_shown.add(skill_offer)
            if len(self._suggestions_shown) > 100:
                self._suggestions_shown.clear()
            play_chime("done")
            self.pipeline.speak(skill_offer)
            logger.info(f"Skill offer: {skill_offer[:60]}")
            return skill_offer
        if not self._proactive:
            return None
        try:
            suggestions = self._proactive.get_suggestions()
            for s in suggestions:
                if not force and s.text in self._suggestions_shown:
                    continue
                if s.should_speak:
                    self._suggestions_shown.add(s.text)
                    if len(self._suggestions_shown) > 100:
                        self._suggestions_shown.clear()
                    play_chime("done")
                    self.pipeline.speak(s.text)
                    logger.info(f"Proactive: {s.text[:60]}")
                    return s.text
                elif s.should_notify:
                    try:
                        from ..desktop.wm_abstraction import WindowManager
                        WindowManager.notify("Friday",
                                             f"Proactive: {s.text[:100]}",
                                             "normal")
                        self._suggestions_shown.add(s.text)
                    except Exception:
                        pass
            return None
        except Exception as exc:
            logger.debug(f"Proactive notify failed: {exc}")
            return None

    def notify(self, title: str, message: str, priority: int = 1) -> None:
        """Send a proactive notification through the voice pipeline."""
        if priority >= 3:
            play_chime("alert")
            self.pipeline.speak(f"{title}: {message}", VoiceMode.ALERT)
        elif priority >= 2:
            if self.pipeline.state == PipelineState.IDLE:
                play_chime("done")
                self.pipeline.speak(f"{title}: {message}")
        else:
            logger.debug(f"Queued notification: {title}: {message}")

    def cleanup(self) -> None:
        """Release all resources."""
        if self._proactive:
            try:
                self._proactive.cleanup()
            except Exception:
                pass
            self._proactive = None
