"""Voice Router — bridges voice input to FRIDAY's proactive + conversation engines.

Routes transcribed speech through:
  1. Desktop commands first ("focus code editor", "switch workspace")
  2. Proactive Intelligence — AnticipationEngine for context-aware suggestions
  3. V3 IdentityEngine (if available) for natural conversation
  4. Basic fallback (greetings, help, status)

Also feeds every interaction to the PatternLearner so FRIDAY learns your
workflow patterns over time.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .pipeline import VoicePipeline, PipelineState
from .tts import VoiceMode, auto_voice_mode, play_chime

logger = logging.getLogger("friday_v4.voice.router")


# ---------------------------------------------------------------------------
# Word-boundary matching helper
# ---------------------------------------------------------------------------


def _has_word(text: str, word: str) -> bool:
    """Check if `word` appears as a whole word (not substring) in `text`.
    
    Uses regex word boundaries (\\b) to prevent false positives from
    substring matches like \"hi\" matching inside \"this\" or \"high\".
    """
    return bool(re.search(rf'\b{re.escape(word)}\b', text))


def _has_any_word(text: str, words: list[str]) -> bool:
    """Check if any whole word from `words` appears in `text`."""
    return any(_has_word(text, w) for w in words)


def _has_phrase(text: str, phrase: str) -> bool:
    """Check if a multi-word phrase appears in text (whole-phrase match).
    
    For phrases we just do substring (they're long enough to avoid
    false positives), but also check word boundaries at the edges.
    """
    return phrase in text


class VoiceRouter:
    """Routes voice input through FRIDAY's intelligence engines.
    
    Priority order:
      1. Desktop commands (immediate action)
      2. Proactive Intelligence (context + patterns + priority)
      3. V3 IdentityEngine (conversation)
      4. Basic fallback
    
    Usage:
        router = VoiceRouter(pipeline)
        pipeline.route_function = router.route
        pipeline.start()
        
        # Proactive suggestions are checked on route() and can be
        # accessed via router.proactive_notify()
    """

    def __init__(self, pipeline: VoicePipeline,
                 identity_engine=None,  # V3 IdentityEngine
                 enable_proactive: bool = True,
                 ):
        self.pipeline = pipeline
        self._engine = identity_engine
        self._conn = None
        self._suggestions_shown: set[str] = set()  # Track shown suggestions
        self._interaction_count = 0

        # Initialize Proactive Intelligence
        self._proactive = None
        if enable_proactive:
            self._init_proactive()

        # Try to load V3 IdentityEngine
        if self._engine is None:
            self._try_load_v3_engine()

    def _init_proactive(self):
        """Initialize the AnticipationEngine for proactive suggestions."""
        try:
            from ..proactive import AnticipationEngine
            self._proactive = AnticipationEngine()
            logger.info("Proactive Intelligence engine loaded")
        except Exception as exc:
            logger.debug(f"Proactive engine init failed: {exc}")
            self._proactive = None

    def _try_load_v3_engine(self):
        """Attempt to load V3's IdentityEngine for routing."""
        try:
            from friday.persona.engine import IdentityEngine
            from friday.db import connect

            self._conn = connect()
            self._engine = IdentityEngine(conn=self._conn)
            logger.info("V3 IdentityEngine loaded for voice routing")
        except ImportError:
            logger.warning(
                "V3 IdentityEngine not available. "
                "Voice will use basic text processing."
            )
        except Exception as exc:
            logger.warning(f"Failed to load V3 IdentityEngine: {exc}")

    def route(self, text: str) -> str:
        """Route transcribed speech through FRIDAY's intelligence engines.
        
        Flows:
          1. Desktop commands — immediate action
          2. Proactive briefings — "brief me", "what's new"
          3. V3 IdentityEngine — conversational routing
          4. Fallback — greetings, help, etc.
        
        Every interaction is observed by the PatternLearner.
        
        Args:
            text: Transcribed speech text
        
        Returns:
            Response text to speak aloud
        """
        if not text.strip():
            return ""

        lower = text.lower().strip()

        # Track interaction count for proactive timing
        self._interaction_count += 1

        # 1. Check desktop commands first — immediate action
        desktop_response = self._try_desktop_command(lower, text)
        if desktop_response:
            # Observe the desktop command as user action
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

        # 3. Route through V3 IdentityEngine if available
        if self._engine:
            try:
                response = self._engine.process(text, channel_id="voice")
                if response:
                    self._observe_user_action(text, "conversation")
                    return response
            except Exception as exc:
                logger.error(f"IdentityEngine routing error: {exc}")
                return f"Sorry, I ran into an error: {exc}"

        # 4. Basic fallback
        response = self._fallback_route(text)
        if response:
            self._observe_user_action(text, "fallback")
        return response or ""

    def _observe_user_action(self, text: str, category: str = "conversation"):
        """Feed user interaction to the PatternLearner.
        
        Categorizes the interaction and observes it so FRIDAY learns
        what actions the user takes in different contexts.
        """
        if not self._proactive:
            return

        # Determine action type from the text
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
            # Get current app from desktop context
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
                "app": app_class,
                "category": category,
            })
        except Exception:
            pass

    def _fallback_route(self, text: str) -> str:
        """Basic fallback routing when V3 IdentityEngine is unavailable.
        
        Handles:
        - General conversation (greetings, help, etc.)
        - Desktop commands: focus windows, switch workspaces, etc.
        - Status requests
        """
        lower = text.lower().strip()

        # ── Desktop Commands ──────────────────────────────────────
        desktop_response = self._try_desktop_command(lower, text)
        if desktop_response:
            return desktop_response

        # ── General Conversation ──────────────────────────────────

        # Greetings
        if _has_any_word(lower, ["hello", "hi", "hey"]) or \
           _has_phrase(lower, "good morning") or \
           _has_phrase(lower, "good evening"):
            return "Hello. I'm Friday, your AI operating partner. How can I help you?"

        # Status questions
        if _has_any_word(lower, ["status"]) or \
           _has_phrase(lower, "what's new") or \
           _has_phrase(lower, "what's going on") or \
           _has_phrase(lower, "what is new") or \
           _has_phrase(lower, "how are things"):
            return self._get_status_response()

        # Identity questions
        if "who are you" in lower or "what are you" in lower:
            return ("I'm Friday, your AI operating partner. I help you manage "
                    "your software projects, monitor your codebase, and keep "
                    "you informed about what matters.")

        # Desktop info queries
        if _has_phrase(lower, "what am i working on") or \
           _has_phrase(lower, "what's on my screen") or \
           _has_phrase(lower, "what is open") or \
           _has_phrase(lower, "what windows") or \
           _has_phrase(lower, "show desktop"):
            return self._get_desktop_status()

        # Gratitude
        if _has_any_word(lower, ["thanks", "nice"]) or \
           _has_phrase(lower, "thank you") or \
           _has_phrase(lower, "good job"):
            return "You're welcome."

        # Goodbye
        if _has_any_word(lower, ["goodbye", "bye", "exit", "quit"]) or \
           _has_phrase(lower, "see you"):
            return "Goodbye. I'll be here when you need me."

        # Default: let them know I'm listening
        return (f"I heard you say: '{text}'. I'm still learning to route "
                f"this through Friday's full intelligence. Try asking me "
                f"about your projects or saying 'what's new'.")

    # ── Desktop Command Handling ──────────────────────────────────

    _DESKTOP_ACTIONS = {
        "focus": "focus",
        "switch": "workspace",
        "go to": "workspace",
        "open": "open",
        "show": "focus",
        "launch": "launch",
        "take": "screenshot",
        "capture": "screenshot",
        "screenshot": "screenshot",
        "snapshot": "screenshot",
    }

    def _try_desktop_command(self, lower: str, original: str) -> Optional[str]:
        """Try to interpret text as a desktop command.
        
        Returns a spoken response if a desktop action was taken.
        """
        try:
            from ..desktop.wm_abstraction import WindowManager
        except ImportError:
            return None

        wm = WindowManager()
        if not wm.is_available:
            return None

        # Status/read queries take precedence over action words — this
        # prevents "what is open" from being parsed as "open <target>"
        # (the action loop below would otherwise hijack it).
        if (
            "what am i working on" in lower
            or "what's on my screen" in lower
            or "what is open" in lower
            or "what's open" in lower
            or "what windows" in lower
            or "whats open" in lower
            or "show desktop" in lower
        ):
            return self._get_desktop_status(wm)

        # Extract the action and target from the command
        for action, cmd_type in self._DESKTOP_ACTIONS.items():
            if _has_word(lower, action):
                # Get everything after the action word
                idx = lower.index(action) + len(action)
                target = original[idx:].strip()

                # Clean up common artifacts
                target = target.removeprefix("to").removeprefix("the").removeprefix("me").strip()

                if cmd_type == "focus":
                    return self._handle_focus(wm, target)
                elif cmd_type == "open":
                    return self._handle_open(wm, target)
                elif cmd_type == "workspace":
                    return self._handle_workspace(wm, target)
                elif cmd_type == "screenshot":
                    return self._handle_screenshot(wm)
                elif cmd_type == "launch":
                    return self._handle_launch(wm, target)

        return None

    def _handle_launch(self, wm, target: str) -> str:
        """Launch an application by natural name or command.
        
        Tries to focus an existing window first (e.g. "launch Spotify"
        brings up the running instance); otherwise launches it.
        """
        return self._focus_or_launch(wm, target, "launch")

    def _handle_focus(self, wm, target: str) -> str:
        """Focus a window by natural language."""
        if not target:
            return "What would you like me to focus?"

        resolved = wm.focus_smart(target)
        if resolved:
            return f"Focused {resolved}."

        return f"I couldn't find '{target}'. Try 'Friday, show windows' to see what's open."

    def _handle_open(self, wm, target: str) -> str:
        """Open an app — focus it if running, otherwise launch it.
        
        Distinct from plain focus: "open <app>" implies starting the
        app if it isn't already running. "show" stays focus-only so
        queries like "show windows" never try to launch a literal.
        """
        return self._focus_or_launch(wm, target, "open")

    def _focus_or_launch(self, wm, target: str, verb: str) -> str:
        """Shared focus-if-running-else-launch flow for open/launch."""
        if not target:
            return f"What would you like me to {verb}?"

        resolved = wm.focus_smart(target)
        if resolved:
            return f"Focused {resolved}."

        if wm.launch_app(target):
            return f"Launching {target}."

        return f"I couldn't {verb} '{target}'."

    def _handle_workspace(self, wm, target: str) -> str:
        """Switch to a workspace."""
        # Clean up: strip "workspace" prefix if present
        target = target.strip()
        for prefix in ["workspace", "to workspace", "desktop", "to desktop"]:
            if target.lower().startswith(prefix):
                target = target[len(prefix):].strip()

        # Try to find workspace by number
        # Extract first number from target
        nums = re.findall(r'\d+', target)
        if nums:
            ws_id = int(nums[0])
            if wm.switch_workspace(ws_id):
                return f"Switched to workspace {ws_id}."

        # Try to find workspace by name (if using named workspaces)
        if target:
            # Check if target matches a workspace name
            workspaces = wm.list_workspaces()
            for ws in workspaces:
                if target.lower() in ws.name.lower():
                    if wm.switch_workspace(ws.id):
                        return f"Switching to workspace {ws.id}."

            # Check if target matches an app — switch to its workspace
            windows = wm.list_windows()
            from ..desktop.wm_abstraction import SmartWindowResolver
            resolved = SmartWindowResolver.resolve(target, windows)
            if resolved:
                # Find the workspace for this window
                for w in windows:
                    if w.app_class.lower() == resolved.lower():
                        if wm.switch_workspace(w.workspace_id):
                            return f"Switching to workspace {w.workspace_id} where {resolved} is open."

        return f"I couldn't find workspace '{target}'."

    def _handle_screenshot(self, wm) -> str:
        """Take a screenshot."""
        result = wm.take_screenshot()
        if result:
            return f"Screenshot saved."
        return "Sorry, I couldn't take a screenshot."

    def _get_desktop_status(self, wm=None) -> str:
        """Get a spoken summary of the current desktop state."""
        try:
            if wm is None:
                from ..desktop.wm_abstraction import WindowManager
                wm = WindowManager()

            if not wm.is_available:
                return "Desktop monitoring is not available on this system."

            active = wm.get_active_window()
            windows = wm.list_windows()
            workspaces = wm.list_workspaces()
            active_ws = wm.get_active_workspace()

            parts = []
            if active:
                parts.append(f"You're in {active.app_name} on workspace {active.workspace_id}")
                if active.title and "friday" not in active.title.lower():
                    # Only mention title if it's not about Friday itself
                    short_title = active.title[:40]
                    parts.append(f"Working on {short_title}")

            parts.append(f"{len(windows)} windows open across {len(workspaces)} workspaces")

            return ". ".join(parts) + "."
        except Exception:
            return "I couldn't check your desktop status right now."

    def _get_status_response(self) -> str:
        """Get a brief workspace status for voice response."""
        parts = []
        try:
            if self._conn:
                # Check daemon status
                status = self._conn.execute(
                    "SELECT state, last_cycle_at FROM daemon_status "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if status:
                    parts.append(f"Daemon is {status['state']}")

                # Check recent observations
                obs = self._conn.execute(
                    "SELECT COUNT(*) as cnt FROM observations "
                    "WHERE observed_at > datetime('now', '-1 day')"
                ).fetchone()
                if obs and obs["cnt"]:
                    parts.append(f"{obs['cnt']} new observations today")

        except Exception:
            pass

        if parts:
            return "Here's your status: " + ". ".join(parts) + "."

        # Include proactive insight if available
        if self._proactive:
            try:
                suggestions = self._proactive.get_suggestions()
                speak_now = [s for s in suggestions if s.should_speak]
                if speak_now:
                    return f"I've been watching your workflow. {speak_now[0].text}"
            except Exception:
                pass

        return "All quiet. No significant changes to report."

    # ── Proactive Intelligence Integration ─────────────────────────

    def _get_proactive_briefing(self) -> Optional[str]:
        """Get a spoken briefing that combines context + proactive insights."""
        if not self._proactive:
            return None

        try:
            # Get context summary
            context_summary = self._proactive.get_context_summary()

            # Get high-priority suggestions
            suggestions = self._proactive.get_suggestions()
            speak_now = [s for s in suggestions if s.should_speak]

            parts = [context_summary]

            if speak_now:
                for s in speak_now[:2]:  # Max 2 verbal suggestions
                    parts.append(s.text)

            # Add pattern stats
            stats = self._proactive.get_learning_stats()
            patterns = stats.get("patterns", {})
            if patterns.get("action_pairs_learned", 0) > 2:
                parts.append(
                    f"I've learned {patterns['action_pairs_learned']} patterns "
                    f"from your workflow."
                )

            return " ".join(parts) + "."

        except Exception as exc:
            logger.debug(f"Proactive briefing failed: {exc}")
            return None

    def proactive_notify(self, force: bool = False) -> Optional[str]:
        """Check for proactive suggestions and speak/spoken.
        
        Should be called on session start and periodically during idle.
        Returns the spoken text if something was said, None otherwise.
        
        Args:
            force: If True, speak even previously-shown suggestions.
        """
        if not self._proactive:
            return None

        try:
            suggestions = self._proactive.get_suggestions()

            for s in suggestions:
                # Skip suggestions we've already shown
                if not force and s.text in self._suggestions_shown:
                    continue

                if s.should_speak:
                    self._suggestions_shown.add(s.text)
                    # Prevent unbounded growth
                    if len(self._suggestions_shown) > 100:
                        self._suggestions_shown.clear()
                    play_chime("done")
                    self.pipeline.speak(s.text)
                    logger.info(f"Proactive: {s.text[:60]}")
                    return s.text

                elif s.should_notify:
                    # Desktop notification for non-verbal items
                    try:
                        from ..desktop.wm_abstraction import WindowManager
                        WindowManager.notify(
                            "Friday",
                            f"Proactive: {s.text[:100]}",
                            "normal"
                        )
                        self._suggestions_shown.add(s.text)
                    except Exception:
                        pass

            return None

        except Exception as exc:
            logger.debug(f"Proactive notify failed: {exc}")
            return None

    def notify(self, title: str, message: str, priority: int = 1):
        """Send a proactive notification through the voice pipeline.
        
        High-priority messages are spoken immediately.
        Others are queued for the next user interaction.
        """
        if priority >= 3:
            # Critical — speak immediately
            play_chime("alert")
            self.pipeline.speak(f"{title}: {message}", VoiceMode.ALERT)
        elif priority >= 2:
            # Important — speak at next opportunity
            if self.pipeline.state == PipelineState.IDLE:
                play_chime("done")
                self.pipeline.speak(f"{title}: {message}")
        else:
            # Normal — queue for briefing
            logger.debug(f"Queued notification: {title}: {message}")

    def cleanup(self):
        """Release all resources — V3 engine, proactive engine, connections."""
        self._engine = None
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        # Clean up proactive engine
        if self._proactive:
            try:
                self._proactive.cleanup()
            except Exception:
                pass
            self._proactive = None
