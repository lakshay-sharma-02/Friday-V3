"""Voice Router — bridges voice input to FRIDAY's intelligence engines.

Routes transcribed speech through, in priority order:
  1. Desktop commands ("focus code editor", "switch workspace")
  2. Proactive briefings ("brief me", "what's new")
  3. V3 IdentityEngine (if available) for natural conversation
  4. Basic fallback (greetings, help, status)

Every interaction is fed to the PatternLearner so FRIDAY learns the
user's workflow over time.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .chimes import play_chime
from .pipeline import VoicePipeline, PipelineState
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
                 identity_engine=None,  # V3 IdentityEngine
                 enable_proactive: bool = True):
        self.pipeline = pipeline
        self._engine = identity_engine
        self._conn = None
        self._wm = None
        self._suggestions_shown: set[str] = set()
        self._interaction_count = 0
        self._proactive = None

        if enable_proactive:
            self._init_proactive()
        if self._engine is None:
            self._try_load_v3_engine()

    # ── Engine loading ─────────────────────────────────────────────────

    def _init_proactive(self) -> None:
        try:
            from ..proactive import AnticipationEngine
            self._proactive = AnticipationEngine()
            logger.info("Proactive Intelligence engine loaded")
        except Exception as exc:
            logger.debug(f"Proactive engine init failed: {exc}")
            self._proactive = None

    def _try_load_v3_engine(self) -> None:
        try:
            from friday.persona.engine import IdentityEngine
            from friday.db import connect
            self._conn = connect()
            self._engine = IdentityEngine(conn=self._conn)
            logger.info("V3 IdentityEngine loaded for voice routing")
        except ImportError:
            logger.warning("V3 IdentityEngine not available — using fallback routing")
        except Exception as exc:
            logger.warning(f"Failed to load V3 IdentityEngine: {exc}")

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

        # 3. V3 IdentityEngine
        if self._engine:
            try:
                response = self._engine.process(text, channel_id="voice")
                if response:
                    self._observe_user_action(text, "conversation")
                    return response
            except Exception as exc:
                logger.error(f"IdentityEngine routing error: {exc}")
                return f"Sorry, I ran into an error: {exc}"

        # 4. Fallback
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

    # ── Fallback routing ───────────────────────────────────────────────

    def _fallback_route(self, text: str) -> str:
        lower = text.lower().strip()
        desktop_response = self._try_desktop_command(lower, text)
        if desktop_response:
            return desktop_response

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

    _DESKTOP_ACTIONS = {
        "focus": "focus", "switch": "workspace", "go to": "workspace",
        "open": "open", "show": "focus", "launch": "launch",
        "take": "screenshot", "capture": "screenshot", "screenshot": "screenshot",
        "snapshot": "screenshot",
    }

    def _get_wm(self):
        """Lazily construct and cache the WindowManager facade."""
        if self._wm is None:
            try:
                from ..desktop.wm_abstraction import WindowManager
                self._wm = WindowManager()
            except ImportError:
                self._wm = None
        return self._wm

    def _try_desktop_command(self, lower: str, original: str) -> Optional[str]:
        wm = self._get_wm()
        if not wm or not wm.is_available:
            return None

        # Read-queries take precedence over action words
        if ("what am i working on" in lower or "what's on my screen" in lower
                or "what is open" in lower or "what's open" in lower
                or "what windows" in lower or "whats open" in lower
                or "show desktop" in lower):
            return self._get_desktop_status(wm)

        for action, cmd_type in self._DESKTOP_ACTIONS.items():
            if _has_word(lower, action):
                idx = lower.index(action) + len(action)
                target = original[idx:].strip()
                for prefix in ("to", "the", "me"):
                    target = target.removeprefix(prefix).strip()
                if cmd_type == "focus":
                    return self._handle_focus(wm, target)
                if cmd_type == "open":
                    return self._handle_open(wm, target)
                if cmd_type == "workspace":
                    return self._handle_workspace(wm, target)
                if cmd_type == "screenshot":
                    return self._handle_screenshot(wm)
                if cmd_type == "launch":
                    return self._handle_launch(wm, target)
        return None

    def _handle_focus(self, wm, target: str) -> str:
        if not target:
            return "What would you like me to focus?"
        resolved = wm.focus_smart(target)
        if resolved:
            return f"Focused {resolved}."
        return (f"I couldn't find '{target}'. Try 'Friday, show windows' "
                f"to see what's open.")

    def _handle_open(self, wm, target: str) -> str:
        return self._focus_or_launch(wm, target, "open")

    def _handle_launch(self, wm, target: str) -> str:
        return self._focus_or_launch(wm, target, "launch")

    def _focus_or_launch(self, wm, target: str, verb: str) -> str:
        if not target:
            return f"What would you like me to {verb}?"
        resolved = wm.focus_smart(target)
        if resolved:
            return f"Focused {resolved}."
        if wm.launch_app(target):
            return f"Launching {target}."
        return f"I couldn't {verb} '{target}'."

    def _handle_workspace(self, wm, target: str) -> str:
        target = target.strip()
        for prefix in ("workspace", "to workspace", "desktop", "to desktop"):
            if target.lower().startswith(prefix):
                target = target[len(prefix):].strip()
        nums = re.findall(r'\d+', target)
        if nums:
            ws_id = int(nums[0])
            if wm.switch_workspace(ws_id):
                return f"Switched to workspace {ws_id}."
        if target:
            try:
                for ws in wm.list_workspaces():
                    if target.lower() in ws.name.lower():
                        if wm.switch_workspace(ws.id):
                            return f"Switching to workspace {ws.id}."
            except Exception:
                pass
            windows = wm.list_windows()
            from ..desktop.wm_abstraction import SmartWindowResolver
            resolved = SmartWindowResolver.resolve(target, windows)
            if resolved:
                for w in windows:
                    if w.app_class.lower() == resolved.lower():
                        if wm.switch_workspace(w.workspace_id):
                            return f"Switching to workspace {w.workspace_id} where {resolved} is open."
        return f"I couldn't find workspace '{target}'."

    def _handle_screenshot(self, wm) -> str:
        if wm.take_screenshot():
            return "Screenshot saved."
        return "Sorry, I couldn't take a screenshot."

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
        parts = []
        try:
            if self._conn:
                status = self._conn.execute(
                    "SELECT state, last_cycle_at FROM daemon_status "
                    "ORDER BY rowid DESC LIMIT 1").fetchone()
                if status:
                    parts.append(f"Daemon is {status['state']}")
                obs = self._conn.execute(
                    "SELECT COUNT(*) as cnt FROM observations "
                    "WHERE observed_at > datetime('now', '-1 day')").fetchone()
                if obs and obs["cnt"]:
                    parts.append(f"{obs['cnt']} new observations today")
        except Exception:
            pass
        if parts:
            return "Here's your status: " + ". ".join(parts) + "."
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
                parts.append(f"I've learned {patterns['action_pairs_learned']} patterns from your workflow.")
            return " ".join(parts) + "."
        except Exception as exc:
            logger.debug(f"Proactive briefing failed: {exc}")
            return None

    def proactive_notify(self, force: bool = False) -> Optional[str]:
        """Check for proactive suggestions on session start / idle."""
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
        self._engine = None
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._proactive:
            try:
                self._proactive.cleanup()
            except Exception:
                pass
            self._proactive = None
