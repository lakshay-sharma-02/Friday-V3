"""Window Manager Abstraction for Friday V4.

A unified interface for desktop window management across desktop
environments. Follows the Wave 2 plan structure:

    DesktopAbstraction (base interface)  →  wm_abstraction.py
    HyprlandAdapter                      →  hyprland_adapter.py
    GNOMEAdapter                         →  gnome_adapter.py
    KDEAdapter                           →  kde_adapter.py
    MacOSAdapter                         →  macos_adapter.py
    WindowsAdapter                       →  windows_adapter.py

`WindowManager` is the auto-detecting facade used by the CLI, voice
router, and proactive engines. It picks the right adapter for the
current desktop environment and delegates to it, so callers can write
platform-independent code:

    wm = WindowManager()
    status = wm.get_status()
    windows = wm.list_windows()
    wm.focus_smart("code editor")
    wm.switch_workspace(2)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import ClassVar, Optional
from urllib.parse import quote_plus

logger = logging.getLogger("friday_v6.desktop.wm")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class WindowInfo:
    """Information about a single window on the desktop."""

    window_id: str = ""
    title: str = ""
    app_class: str = ""       # Window class (e.g., "kitty", "firefox")
    workspace_id: int = 0
    workspace_name: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    monitor: int = 0
    pid: int = 0
    floating: bool = False
    fullscreen: bool = False
    is_active: bool = False

    @property
    def app_name(self) -> str:
        """Human-readable app name from class."""
        name_map = {
            "kitty": "Code Editor",
            "alacritty": "Terminal",
            "foot": "Terminal",
            "wezterm": "Terminal",
            "Code": "VS Code",
            "code-oss": "VS Code",
            "firefox": "Browser",
            "firefoxdeveloperedition": "Browser",
            "chromium": "Browser",
            "chromium-browser": "Browser",
            "google-chrome": "Browser",
            "brave-browser": "Browser",
            "zen": "Browser",
            "thunar": "File Manager",
            "nautilus": "File Manager",
            "dolphin": "File Manager",
            "slack": "Slack",
            "discord": "Discord",
            "spotify": "Spotify",
            "zcode": "ZCode",
            "obsidian": "Obsidian",
        }
        return name_map.get(self.app_class, self.app_class.title())


@dataclass
class WorkspaceInfo:
    """Information about a workspace."""

    id: int = 0
    name: str = ""
    monitor: str = ""
    window_count: int = 0
    is_active: bool = False
    last_window_title: str = ""


@dataclass
class MonitorInfo:
    """Information about a monitor/display."""

    name: str = ""
    width: int = 0
    height: int = 0
    refresh_rate: float = 0.0
    is_active: bool = False
    active_workspace: int = 0
    scale: float = 1.0
    make: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# Smart Window Resolver — Maps Natural Names to Actual Apps
# ---------------------------------------------------------------------------


class SmartWindowResolver:
    """Maps natural language app names to actual window classes/titles.

    This is what enables saying "Friday, focus code editor" and having
    Friday understand you mean kitty or VS Code.

    Resolution strategy:
      1. Direct match — "kitty" → class:kitty
      2. Semantic match — "code editor" → ["kitty", "code", "alacritty"]
      3. Fuzzy match — "browser" → ["firefox", "chromium", "brave"]
      4. Best guess — pick the most likely window from open windows
    """

    # Natural name → possible window classes
    SEMANTIC_MAP: ClassVar[dict[str, list[str]]] = {
        "code editor": ["kitty", "Code", "code-oss", "alacritty",
                        "wezterm", "foot", "zcode"],
        "terminal": ["kitty", "alacritty", "wezterm", "foot",
                     "gnome-terminal", "konsole"],
        "editor": ["Code", "code-oss", "vim", "nvim", "neovim",
                   "sublime_text", "jetbrains"],
        "browser": ["firefox", "firefoxdeveloperedition",
                    "chromium", "chromium-browser",
                    "google-chrome", "brave-browser", "zen"],
        "files": ["thunar", "nautilus", "dolphin", "nemo"],
        "file manager": ["thunar", "nautilus", "dolphin", "nemo"],
        "chat": ["slack", "discord", "telegram", "whatsapp"],
        "slack": ["slack"],
        "discord": ["discord"],
        "music": ["spotify"],
        "spotify": ["spotify"],
        "vs code": ["Code", "code-oss"],
        "vscode": ["Code", "code-oss"],
        "zcode": ["zcode"],
        "obsidian": ["obsidian"],
        "notes": ["obsidian", "logseq"],
        "settings": ["gnome-control-center", "systemsettings",
                     "xfce4-settings-manager"],
    }

    # Common words to strip from natural language queries
    _STOP_WORDS: ClassVar[set[str]] = {"the", "a", "an", "my", "to",
                                        "please", "could", "would", "can",
                                        "switch", "focus", "open", "go"}

    @classmethod
    def resolve(cls, query: str, open_windows: list[WindowInfo]) -> Optional[str]:
        """Resolve a natural language query to a window class.

        Args:
            query: Natural language (e.g., "focus code editor", "switch to browser")
            open_windows: List of currently open windows

        Returns:
            Window class to focus, or None if no match found
        """
        query_lower = query.lower().strip()

        # Strip stop words and commands
        words = [w for w in query_lower.split()
                 if w not in cls._STOP_WORDS]

        # Try each word/phrase as a semantic lookup
        for phrase in [query_lower, *words]:
            # Check semantic map
            if phrase in cls.SEMANTIC_MAP:
                candidates = cls.SEMANTIC_MAP[phrase]
                # Find the first open window matching any candidate
                for w in open_windows:
                    if w.app_class.lower() in [c.lower() for c in candidates]:
                        logger.info(f"Resolved '{phrase}' → {w.app_class}")
                        return w.app_class

            # Check direct class match
            for w in open_windows:
                if phrase == w.app_class.lower():
                    return w.app_class
                # Check if phrase is contained in title
                if phrase in w.title.lower():
                    return w.app_class

        # If nothing matched but we have open windows, return None
        return None

    @classmethod
    def suggest_for_window(cls, window: WindowInfo) -> list[str]:
        """Suggest natural language names for a window.

        Used to help users discover what they can say.
        """
        suggestions = []
        for name, classes in cls.SEMANTIC_MAP.items():
            if window.app_class.lower() in [c.lower() for c in classes]:
                suggestions.append(name)
        return suggestions


# ---------------------------------------------------------------------------
# Natural-language desktop command router (CLI/web surface)
# ---------------------------------------------------------------------------

#: Verbs that START a desktop sub-command (compound splitting). Only a
#: fragment beginning with one of these is treated as a NEW command —
#: "open youtube and cristiano ronaldo channel" stays one command because
#: "cristiano" isn't a verb, while "open chrome and open whatsapp" splits.
_DESKTOP_VERBS: tuple[str, ...] = (
    "open", "launch", "switch", "focus", "show", "take", "go",
    "screenshot", "start", "run", "search", "look", "find", "google",
)

#: Browser name → binary candidates (checked in order).
_BROWSER_ALIASES: dict[str, tuple[str, ...]] = {
    "brave": ("brave", "brave-browser"),
    "chrome": ("google-chrome", "google-chrome-stable", "chromium"),
    "chromium": ("chromium", "chromium-browser"),
    "firefox": ("firefox", "firefox-esr"),
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
    "safari": ("safari",),
}

#: Default browser preference (first installed wins).
_DEFAULT_BROWSER_ORDER: tuple[str, ...] = (
    "brave", "brave-browser", "google-chrome", "chromium",
    "chromium-browser", "firefox", "firefox-esr", "microsoft-edge",
)

#: Natural app name → binary candidates (launch fallback when focus fails).
_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "chrome": ("google-chrome", "chromium"),
    "code": ("code", "code-oss"),
    "vscode": ("code", "code-oss"),
    "vs code": ("code", "code-oss"),
    "terminal": ("kitty", "alacritty", "gnome-terminal", "konsole"),
    "files": ("nautilus", "dolphin", "thunar"),
    "file manager": ("nautilus", "dolphin", "thunar"),
    "spotify": ("spotify",),
    "discord": ("discord",),
    "slack": ("slack",),
    "obsidian": ("obsidian",),
    "whatsapp": ("whatsapp", "org.whatsapp.WhatsApp"),
}

#: Known web destinations — "open whatsapp" → web.whatsapp.com.
_WEB_DESTINATIONS: dict[str, str] = {
    "whatsapp": "https://web.whatsapp.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "maps": "https://maps.google.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org",
    "spotify": "https://open.spotify.com",
    "netflix": "https://www.netflix.com",
    "instagram": "https://www.instagram.com",
    "twitter": "https://x.com",
    "facebook": "https://www.facebook.com",
    "linkedin": "https://www.linkedin.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "notion": "https://www.notion.so",
    "leetcode": "https://leetcode.com",
    "programiz": "https://www.programiz.com",
}

#: Canonical display names for the spoken reply ("WhatsApp", "YouTube").
_SITE_DISPLAY: dict[str, str] = {
    "whatsapp": "WhatsApp", "youtube": "YouTube", "gmail": "Gmail",
    "google": "Google", "maps": "Google Maps", "github": "GitHub",
    "stackoverflow": "Stack Overflow", "reddit": "Reddit",
    "wikipedia": "Wikipedia", "spotify": "Spotify", "netflix": "Netflix",
    "instagram": "Instagram", "twitter": "X (Twitter)",
    "facebook": "Facebook", "linkedin": "LinkedIn",
    "chatgpt": "ChatGPT", "claude": "Claude", "notion": "Notion",
    "leetcode": "LeetCode", "programiz": "Programiz",
}

#: Sites with a site-search URL template ("open youtube and <query>").
_SITE_SEARCH_TEMPLATES: dict[str, str] = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "github": "https://github.com/search?q={q}",
}

#: Last-resort: unknown target → web search.
_WEB_SEARCH_URL = "https://www.google.com/search?q={q}"

#: Task verbs — an utterance whose target reads like *work* ("open a
#: python venv and install requests") is NOT a desktop command and NOT
#: a web search: it falls through to the brain (PLAN mission / EXECUTE
#: via Claude Code). Only noun-phrase searches ("open c++ compiler of
#: programiz") and explicit searches ("search for X") stay here.
#: Mirrors ``nlu.intent._TASK_VERBS`` — the desktop interpreter's
#: fall-through must agree with the classifier, or an LLM that misroutes
#: "open main.py and fix it" to desktop would web-search work the arms
#: could do. Kept in sync with the repair verbs (fix/debug/…).
_TASK_VERBS: tuple[str, ...] = (
    "create", "set up", "clone", "install", "organize", "build",
    "write", "download", "configure", "initialize", "scaffold",
    "deploy", "migrate", "refactor", "implement", "automate",
    "rename", "fetch", "pull", "push", "commit",
    "fix", "debug", "repair", "rewrite", "optimize", "tune",
)

#: Task nouns — a target that reads like *work-in-progress* rather than
#: an app to open ("open a fresh project for a discord bot", "open a
#: python venv"). These fall through to the brain too — Friday never
#: web-searches a project it could scaffold.
_TASK_NOUNS: tuple[str, ...] = (
    "project", "venv", "virtualenv", "repo", "repository", "module",
    "package", "workflow", "pipeline", "bot", "extension", "plugin",
    "template", "website", "api", "database", "migration",
)

#: Explicit search phrasings — these are web searches, always.
_SEARCH_PHRASES: tuple[tuple[str, str], ...] = (
    ("search for ", ""), ("search ", ""), ("look up ", ""),
    ("google ", ""), ("find ", ""), ("search the web for ", ""),
)


def _find_browser(name: Optional[str] = None) -> Optional[str]:
    """First installed binary for a browser name (or the default)."""
    if name:
        for candidate in _BROWSER_ALIASES.get(name, (name,)):
            path = shutil.which(candidate)
            if path:
                return path
    for candidate in _DEFAULT_BROWSER_ORDER:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _browser_label(browser: Optional[str]) -> str:
    """Human label for a browser name (for the spoken reply)."""
    if not browser:
        return "your browser"
    if browser in _BROWSER_ALIASES:
        return browser.capitalize()
    return browser


def _ends_app_like(target: str) -> bool:
    """Whether a target names an *app* ("todo app", "notes tool")."""
    from .app_aliases import is_app_like
    return is_app_like(target)


def _resolve_app(target: str) -> Optional[str]:
    """Map a natural app name to an *installed* binary, or None.

    ``None`` means the app isn't on this machine — the NL layer then
    falls through to a web destination, a web search, or (for personal
    apps) a teaching prompt. Resolution order:

    1. **Learned aliases** (Wave 20 app-learning): "todo app" taught
       once via "my todo app is obsidian" resolves to its binary
       forever, on every surface.
    2. Builtin natural-name aliases ("code" → code/code-oss, …).
    3. The target itself on ``PATH`` ("brave" → brave).
    4. The target minus an app-like suffix ("spotify app" → "spotify").

    Only resolvable, installed binaries are ever handed to
    ``launch_app`` — the interpreter never claims "Launching whatsapp."
    when nothing was launched.
    """
    low = target.strip().lower()
    from .app_aliases import resolve_learned
    learned = resolve_learned(low)
    # Learned aliases may have been SYNCED from another machine where the
    # app is installed — only resolve when the binary exists here, so a
    # synced alias for an uninstalled app degrades gracefully (falls
    # through) instead of firing a dead launch.
    if learned and ((os.path.isabs(learned) and os.path.exists(learned))
                    or shutil.which(learned)):
        return learned
    for candidate in _APP_ALIASES.get(low, ()):
        if shutil.which(candidate):
            return candidate
    if shutil.which(low):
        return low
    # "spotify app" → "spotify" (only when it still sounds like an app).
    if _ends_app_like(low):
        words = low.rsplit(" ", 1)
        if len(words) == 2 and shutil.which(words[0]):
            return words[0]
    return None


def _has_word(text: str, word: str) -> bool:
    return bool(re.search(rf"\b{re.escape(word)}\b", text))


def _has_task_verb(text: str) -> bool:
    """Whether a target reads like *work* rather than a search/app.

    "open a python venv and install requests" has the task verb
    "install" → True (the brain takes it); "open a fresh project for a
    discord bot" has the task noun "project" → True (the brain
    scaffolds it); "open c++ compiler of programiz" has neither →
    False (web search). Word-boundary matching so "settings" never
    trips on "set".
    """
    return (any(_has_word(text, w) for w in _TASK_VERBS)
            or any(_has_word(text, w) for w in _TASK_NOUNS))


def _split_desktop_commands(text: str) -> list[str]:
    """Split a compound utterance into desktop sub-commands.

    Splits on commas and on " and / then / also " but ONLY when the
    following token starts a desktop verb — so "open chrome on workspace
    3 and open whatsapp" is two commands, while "open youtube and
    cristiano ronaldo channel in it" stays ONE (the "and" joins the
    search phrase, not a second verb). An utterance that reads like a
    TASK ("clone the repo and open it in my editor") is never split —
    it falls through to the brain whole so the Claude arms can do it.
    """
    if _has_task_verb(text):
        return [text.strip()]
    # Multi-word separators first ("and then", "and also", "and also
    # then"), then single-word ones (",", "and", "then", "also") — but
    # ONLY when the following token starts a desktop verb, so "open
    # youtube and cristiano ronaldo channel in it" stays ONE command (the
    # "and" joins the search phrase, not a second verb). The lookahead
    # verb set is DERIVED from _DESKTOP_VERBS so splitting and verb
    # extraction can never drift apart ("open brave and search for
    # rust" splits; "open chrome and google hyprland docs" splits).
    connector = (r"(?:,\s*|\b(?:and\s+then|and\s+also|then|also|"
                 r"and)\s+)")
    split_verbs = "|".join(re.escape(v) for v in _DESKTOP_VERBS)
    marked = re.sub(
        rf"\s*{connector}(?=(?:please\s+)?(?:{split_verbs})\b)",
        "|||", text, flags=re.IGNORECASE)
    parts = [p.strip(" ,;") for p in marked.split("|||")]
    return [p for p in parts if p]


def _open_in_browser(wm, url: str, *, browser: Optional[str] = None,
                     label: Optional[str] = None) -> str:
    """Open a URL in a browser; returns the spoken reply (never raises).

    Prefers the WM's ``open_url`` hook (adapters may own the opener);
    falls back to launching a browser binary directly. ``label`` is the
    human description ("WhatsApp"/"a web search for 'x'") for the reply.
    """
    opened = False
    opener = getattr(wm, "open_url", None)
    if callable(opener):
        try:
            opened = bool(opener(url, browser=browser))
        except Exception:
            opened = False
    if not opened:
        binary = _find_browser(browser)
        if binary:
            try:
                subprocess.Popen([binary, url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                opened = True
            except Exception:
                opened = False
    if opened:
        who = _browser_label(browser or _default_browser_name())
        return f"Opened {label or url} in {who}."
    return f"I couldn't open {label or url}."


def _default_browser_name() -> Optional[str]:
    """The name of the default browser (first installed), for replies."""
    binary = _find_browser()
    if not binary:
        return None
    base = os.path.basename(binary)
    for name, candidates in _BROWSER_ALIASES.items():
        if base in candidates:
            return name
    return base


def desktop_text_command(text: str) -> str:
    """Route one natural-language desktop command to the window manager.

    The §2 Wave-2 hardening entry point for the text surfaces (``friday6
    talk``, the web dashboard chat, and the phone companion) — desktop
    control is no longer voice-only. Speaks real desktop:

    - compound commands: "open chrome on workspace 3 and open whatsapp"
    - workspace targeting: "open chrome on workspace 3"
    - browser choice: "open youtube in firefox"
    - web destinations: "open whatsapp" → web.whatsapp.com
    - site search: "open youtube and cristiano ronaldo channel"
    - web-search fallback: "open c++ compiler of programiz"

    Never raises: an unavailable desktop degrades to an honest message.
    Returns the response string, or "" when nothing matched (callers fall
    through to the normal chat fallback).
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    try:
        wm = WindowManager()
    except Exception as exc:
        logger.debug(f"desktop_text_command: no WM ({exc})")
        return "Desktop control isn't available on this system."
    if not wm.is_available:
        return "Desktop control isn't available on this system."

    # Read-queries take precedence over action words.
    if ("what am i working on" in lower or "what's on my screen" in lower
            or "what is open" in lower or "what's open" in lower
            or "what windows" in lower or "whats open" in lower
            or "show desktop" in lower):
        return _desktop_status_text(wm)

    # App learning — "my todo app is obsidian" teaches Friday once.
    # Detected here (before splitting) so every frame — including
    # "open my todo app with obsidian", which is also a real command —
    # learns AND opens. Wrapped: a store/binary hiccup must never crash
    # the utterance (never-crash law).
    try:
        learned = _handle_learning_phrase(raw, wm)
    except Exception as exc:
        logger.debug(f"app-learning failed ({exc})")
        learned = None
    if learned is not None:
        return learned

    replies: list[str] = []
    context: dict = {"browser": None}
    for command in _split_desktop_commands(raw):
        try:
            # A teaching frame inside a compound ("open my todo app with
            # obsidian and open whatsapp") still teaches: check each
            # sub-command, not just the whole utterance.
            taught = _handle_learning_phrase(command, wm)
            if taught is not None:
                replies.append(taught)
                continue
            reply = _execute_desktop_command(wm, command, context)
        except Exception as exc:
            # Never-crash law: one failing sub-command (adapter
            # subprocess error, missing tool) degrades to its own
            # honest reply, never aborts the compound utterance.
            logger.debug(f"desktop sub-command failed ({exc})")
            reply = (f"I couldn't do that part ({command.strip()[:40]}).")
        if reply:
            replies.append(reply)
    return " ".join(replies)


def _handle_learning_phrase(raw: str, wm) -> Optional[str]:
    """Teach Friday a personal app mapping; None when not a teaching phrase.

    Frames (parsed by ``app_aliases.parse_learning_phrase``): "my todo
    app is obsidian", "use obsidian for my todo app", "set my todo app
    to obsidian", "open my todo app with obsidian". Only *resolvable*
    binaries are learned — an unresolvable one gets an honest "I couldn't
    find that command" reply and nothing is saved. After teaching, the
    app is opened (focus if running, else launch) so the mapping is
    confirmed in front of the operator.
    """
    from .app_aliases import learn_alias, parse_learning_phrase
    parsed = parse_learning_phrase(raw)
    if parsed is None:
        return None
    name, binary = parsed
    resolved = learn_alias(name, binary)
    if resolved is None:
        return (f"I couldn't find '{binary}' on this machine — "
                f"is it installed? I haven't saved '{name}' yet.")
    # Spoken reply uses the friendly basename, not the absolute path
    # ("obsidian", not "/usr/bin/obsidian") — but launching uses the
    # full resolved binary.
    display = os.path.basename(resolved)
    opened = ""
    try:
        if wm.is_available:
            focused = wm.focus_smart(name)
            if focused:
                opened = f"Opening {focused}."
            elif wm.launch_app(resolved):
                opened = f"Launching {display}."
    except Exception as exc:
        logger.debug(f"learning open failed ({exc})")
    fallback = "I'll open it next time you ask."
    return (f"Got it — '{name}' is {display}. "
            f"{opened or fallback}").strip()


def _execute_desktop_command(wm, command: str, context: dict) -> str:
    """Run one desktop sub-command; returns its spoken reply (or "")."""
    verb, target = _desktop_verb_target(command)
    if verb in ("switch", "go"):
        return _desktop_workspace(wm, target)
    if verb in ("focus", "show"):
        if not target:
            return "What would you like me to focus?"
        resolved = wm.focus_smart(target)
        return f"Focused {resolved}." if resolved else \
            f"I couldn't find '{target}'."
    if verb in ("screenshot", "take"):
        return "Screenshot saved." if wm.take_screenshot() \
            else "Sorry, I couldn't take a screenshot."
    if verb in ("search", "look", "find", "google"):
        return _desktop_search(wm, target, verb)
    if verb in ("open", "launch", "start", "run"):
        return _desktop_open_or_launch(wm, target, verb, context,
                                       raw=command)
    return ""


def _desktop_verb_target(command: str) -> tuple[Optional[str], str]:
    """The leading desktop verb and the target that follows it."""
    low = command.strip().lower()
    for verb in _DESKTOP_VERBS:
        # Whole-word match so "open" never matches inside "opening".
        if re.match(rf"{re.escape(verb)}\b", low):
            target = command[len(verb):].strip()
            for prefix in ("the ", "a ", "an ", "my "):
                if target.lower().startswith(prefix):
                    target = target[len(prefix):].strip()
            return verb, target
    return None, ""


def _known_semantic_category(target: str) -> bool:
    """Whether a target is a known *category* ("browser", "editor")."""
    return target.strip().lower() in SmartWindowResolver.SEMANTIC_MAP


def _is_personal_app(target: str, raw: str) -> bool:
    """Whether an unresolved target is a *personal app* Friday should
    learn, not web-search: the operator said "my X" (unless X is a
    known category like "browser") or the target ends in an app-like
    word ("todo app", "notes tool")."""
    if _ends_app_like(target):
        return True
    has_my = re.search(r"\bmy\b", f" {raw.lower()} ")
    return bool(has_my and not _known_semantic_category(target))


def _desktop_open_or_launch(wm, target: str, verb: str,
                            context: dict, *, raw: str = "") -> str:
    """open/launch — workspace, browser, web-destination, app, search,
    or teaching prompt."""
    if not target:
        return f"What would you like me to {verb}?"

    # --- qualifiers ---
    workspace = None
    m = re.search(r"\b(?:on|in|to)\s+workspace\s+(\d+)\b", target, re.I)
    if m:
        workspace = int(m.group(1))
        target = (target[:m.start()] + " " + target[m.end():]).strip()

    browser = None
    for name in _BROWSER_ALIASES:
        if re.search(rf"\b(?:in|with|using)\s+{re.escape(name)}\b",
                     target, re.I):
            browser = name
            target = re.sub(rf"\b(?:in|with|using)\s+{re.escape(name)}\b",
                            "", target, flags=re.I).strip()
            break
    # "in it / on it / there" → the browser the previous command opened.
    if browser is None and re.search(r"\b(?:in|on)\s+it\b|\bthere\b",
                                     target, re.I):
        browser = context.get("browser")
        target = re.sub(r"\b(?:in|on)\s+it\b|\bthere\b", "",
                        target, flags=re.I).strip()

    # --- known web destination WITH a query? ("open youtube and <q>") ---
    # Checked before app-launch so "open youtube and cristiano ronaldo
    # channel" site-searches instead of failing to launch a "youtube"
    # binary. Plain destinations ("open spotify") fall through — an
    # installed app wins over the website.
    low = target.lower().strip()
    for site, url in _WEB_DESTINATIONS.items():
        if re.match(rf"{re.escape(site)}\b", low):
            query = target[len(site):].strip(" ,")
            # "open youtube and cristiano ronaldo channel" → the "and"
            # is a phrase connector, not a second command — strip it and
            # other leading filler from the search query.
            query = re.sub(r"^(?:and|for|about|on)\s+", "", query,
                           flags=re.I)
            if query:
                if workspace is not None:
                    try:
                        wm.switch_workspace(workspace)
                    except Exception:
                        pass
                display = _SITE_DISPLAY.get(site, site.capitalize())
                template = _SITE_SEARCH_TEMPLATES.get(site)
                if template:
                    url = template.format(q=quote_plus(query))
                    label = f"{display} — searching '{query}'"
                else:
                    url = _WEB_SEARCH_URL.format(q=quote_plus(
                        f"{site} {query}"))
                    label = f"a search for '{site} {query}'"
                reply = _open_in_browser(wm, url, browser=browser,
                                         label=label)
                if browser:
                    context["browser"] = browser
                elif reply.startswith("Opened") and " in " in reply:
                    opened_in = reply.split(" in ", 1)[1].rstrip(".")
                    context["browser"] = opened_in.lower()
                return reply
            break  # plain destination → handled after app-launch

    # --- an installed app (focus if open, else launch) ---
    # Launching is gated on a *resolvable binary*: `launch_app` can't
    # tell whether the app exists (adapters spawn `sh -c` fire-and-
    # forget), so the NL layer never claims "Launching X" unless X is
    # actually installed. Unresolvable targets fall through to a web
    # destination or a web search instead.
    app = _resolve_app(target)
    if app is not None:
        if workspace is not None:
            try:
                wm.switch_workspace(workspace)
            except Exception:
                pass
        resolved = wm.focus_smart(target)
        if resolved:
            suffix = f" on workspace {workspace}" if workspace is not None else ""
            return f"Focused {resolved}.{suffix}"
        if wm.launch_app(app):
            suffix = f" on workspace {workspace}" if workspace is not None else ""
            return f"Launching {target}.{suffix}"

    # --- known semantic category not installed → focus if it's open ---
    # "open my browser" / "open the terminal" when no binary resolves:
    # focus the running window rather than falling to a useless search.
    if _known_semantic_category(target):
        resolved = wm.focus_smart(target)
        if resolved:
            return f"Focused {resolved}."

    # --- known web destination (no query) / explicit URL ---
    # Reached only when the app isn't installed locally: "open whatsapp"
    # → web.whatsapp.com, "open spotify" → open.spotify.com.
    plain_url = None
    plain_label = None
    if low.startswith(("http://", "https://", "www.")):
        plain_url = low if "://" in low else "https://" + low
        plain_label = target
    else:
        for site, url in _WEB_DESTINATIONS.items():
            if re.match(rf"{re.escape(site)}\b", low):
                plain_url = url
                plain_label = _SITE_DISPLAY.get(site, site.capitalize())
                break
    if plain_url is not None:
        if workspace is not None:
            try:
                wm.switch_workspace(workspace)
            except Exception:
                pass
        reply = _open_in_browser(wm, plain_url, browser=browser,
                                 label=plain_label)
        if browser:
            context["browser"] = browser
        elif reply.startswith("Opened") and " in " in reply:
            opened_in = reply.split(" in ", 1)[1].rstrip(".")
            context["browser"] = opened_in.lower()
        return reply

    # --- honest fallback: web search or the brain ---
    # Open-ended tasks fall THROUGH to the brain ("open a python venv
    # and install requests" → PLAN mission / EXECUTE via Claude Code):
    # the desktop layer never web-searches work the arms could do.
    # Explicit searches ("search for X") and plain noun-phrase queries
    # ("open c++ compiler of programiz") stay here as web searches.
    search_query = None
    for phrase, _ in _SEARCH_PHRASES:
        if target.lower().startswith(phrase):
            search_query = target[len(phrase):].strip()
            break
    if search_query is None and _has_task_verb(target):
        return ""
    # A personal app Friday hasn't met yet ("my todo app", "todo app")
    # gets a teaching prompt — never a useless web search for the
    # operator's own app.
    if search_query is None and _is_personal_app(target, raw):
        return (f"I don't know what '{target}' is yet. Teach me once — "
                f"say 'my {target} is <command>' and I'll remember it.")
    query = search_query or target
    url = _WEB_SEARCH_URL.format(q=quote_plus(query))
    if workspace is not None:
        try:
            wm.switch_workspace(workspace)
        except Exception:
            pass
    reply = _open_in_browser(wm, url, browser=browser,
                             label=f"a web search for '{query}'")
    if browser:
        context["browser"] = browser
    return reply


def _desktop_search(wm, target: str, verb: str) -> str:
    """"search for X" / "look up X" / "google X" → web search."""
    if not target:
        return f"What would you like me to {verb} for?"
    query = target
    for phrase, _ in _SEARCH_PHRASES:
        if target.lower().startswith(phrase):
            query = target[len(phrase):].strip()
            break
    # "look up fastapi docs" → target "up fastapi docs" → strip "up".
    if verb == "look" and query.lower().startswith("up "):
        query = query[3:].strip()
    # "search for X" → target "for X" (verb extraction stripped only
    # "search") → drop the leading "for".
    for lead in ("for ", "about ", "on "):
        if query.lower().startswith(lead):
            query = query[len(lead):].strip()
            break
    if not query:
        return f"What would you like me to {verb} for?"
    url = _WEB_SEARCH_URL.format(q=quote_plus(query))
    return _open_in_browser(wm, url, label=f"a web search for '{query}'")


def _desktop_workspace(wm, target: str) -> str:
    target = target.strip()
    for prefix in ("workspace", "to workspace", "desktop", "to desktop"):
        if target.lower().startswith(prefix):
            target = target[len(prefix):].strip()
    nums = [int(n) for n in target.split() if n.isdigit()]
    if nums:
        if wm.switch_workspace(nums[0]):
            return f"Switched to workspace {nums[0]}."
    if target:
        try:
            for ws in wm.list_workspaces():
                if target.lower() in ws.name.lower():
                    if wm.switch_workspace(ws.id):
                        return f"Switching to workspace {ws.id}."
        except Exception:
            pass
        windows = wm.list_windows()
        resolved = SmartWindowResolver.resolve(target, windows)
        if resolved:
            for w in windows:
                if w.app_class.lower() == resolved.lower():
                    if wm.switch_workspace(w.workspace_id):
                        return (f"Switching to workspace {w.workspace_id} "
                                f"where {resolved} is open.")
    return f"I couldn't find workspace '{target}'."


def _desktop_status_text(wm) -> str:
    try:
        active = wm.get_active_window()
        windows = wm.list_windows()
        workspaces = wm.list_workspaces()
        parts = []
        if active:
            parts.append(f"You're in {active.app_name} on "
                         f"workspace {active.workspace_id}")
            if active.title and "friday" not in active.title.lower():
                parts.append(f"Working on {active.title[:40]}")
        parts.append(f"{len(windows)} windows open across "
                     f"{len(workspaces)} workspaces")
        return ". ".join(parts) + "."
    except Exception:
        return "I couldn't check your desktop status right now."


# ---------------------------------------------------------------------------
# Desktop Environment Detection
# ---------------------------------------------------------------------------


def _shquote(s: str) -> str:
    """Minimal POSIX single-quote escaping for shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"


def detect_desktop_environment() -> str:
    """Detect the current desktop environment / window manager.

    Returns one of: "hyprland", "gnome", "kde", "sway", "i3",
    "macos", "windows", "unknown"
    """
    if os.name == "nt":
        return "windows"

    if sys.platform == "darwin":
        return "macos"

    # Hyprland
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"

    # Wayland compositors
    if os.environ.get("WAYLAND_DISPLAY"):
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "gnome" in de:
            return "gnome"
        if "kde" in de or "plasma" in de:
            return "kde"
        if "sway" in de:
            return "sway"
        return "wayland"

    # X11
    if os.environ.get("DISPLAY"):
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "gnome" in de:
            return "gnome"
        if "kde" in de or "plasma" in de:
            return "kde"
        if "i3" in de:
            return "i3"
        return "x11"

    return "unknown"


# ---------------------------------------------------------------------------
# Generic desktop notification helpers (used by the base adapter)
# ---------------------------------------------------------------------------


def _notify_linux(title: str, message: str, urgency: str = "normal",
                  timeout_ms: Optional[int] = None) -> bool:
    """Send a desktop notification via notify-send (Linux).

    Args:
        timeout_ms: Auto-dismiss timeout in milliseconds. Passed as
            ``notify-send -t`` so the banner fades on daemons that honor
            it (dunst, mako, KDE). GNOME ignores this only for
            ``critical`` urgency, which is why auto-dismissable
            notifications must not use ``critical``. When None the server
            default applies.
    """
    try:
        cmd = ["notify-send", "-a", "Friday", "-u", urgency]
        if timeout_ms is not None:
            cmd += ["-t", str(int(timeout_ms))]
        subprocess.run(
            cmd + [title, message],
            capture_output=True, timeout=3,
        )
        return True
    except Exception:
        return False


def _notify_macos(title: str, message: str) -> bool:
    """Send a desktop notification via osascript (macOS)."""
    script = (
        f'display notification "{message}" with title "Friday: {title}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


def _notify_windows(title: str, message: str) -> bool:
    """Send a desktop notification via PowerShell balloon tip (Windows)."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(3000, '{title}', '{message}', "
        "[System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Milliseconds 3500; $n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DesktopAbstraction — Base Interface
# ---------------------------------------------------------------------------


class DesktopAbstraction:
    """Base interface for a desktop environment adapter.

    Platform adapters (Hyprland, GNOME, KDE, macOS, Windows) subclass this
    and implement the operations that make sense for their environment.
    Operations that don't apply return safe defaults (empty lists, False,
    None) so callers never crash on an unsupported platform.

    The interface is deliberately small and covers the Wave 2 scope:
      - Read:   status, windows, active window, workspaces, monitors
      - Act:    focus window, switch workspace, launch app, screenshot
      - Signal: desktop notifications
    """

    name: str = "unknown"

    # ── Availability ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """Whether this adapter can talk to the current desktop."""
        return False

    @property
    def desktop_environment(self) -> str:
        return self.name

    # ── Read ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Full desktop status — workspaces, active window, monitors."""
        if not self.is_available():
            return {"error": f"Desktop environment '{self.name}' not supported yet"}
        return {"desktop": self.name}

    def list_windows(self) -> list[WindowInfo]:
        """List all open windows across all workspaces."""
        return []

    def get_active_window(self) -> Optional[WindowInfo]:
        """Get the currently focused window."""
        return None

    def list_workspaces(self) -> list[WorkspaceInfo]:
        """List all workspaces."""
        return []

    def get_active_workspace(self) -> Optional[WorkspaceInfo]:
        """Get the currently active workspace."""
        for ws in self.list_workspaces():
            if ws.is_active:
                return ws
        return None

    def list_monitors(self) -> list[MonitorInfo]:
        """List connected monitors/displays."""
        return []

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        """Focus a window by class, title, or pid."""
        return False

    def focus_smart(self, query: str) -> Optional[str]:
        """Focus a window using natural language.

        Uses SmartWindowResolver to figure out what the user means.
        Returns the focused window class, or None if nothing matched.
        """
        windows = self.list_windows()
        resolved = SmartWindowResolver.resolve(query, windows)
        if resolved:
            if self.focus(resolved, "class"):
                return resolved
        return None

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        """Switch to a workspace by ID or name."""
        return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        """Launch an application, optionally in a directory.

        Args:
            app: Application name, command, or executable path
            path: Optional working directory / project to open
        """
        return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot of the current workspace.

        Returns the path to the saved screenshot, or None on failure.
        """
        return None

    def open_url(self, url: str, browser: Optional[str] = None) -> bool:
        """Open a URL in a browser (named, or the default).

        The NL desktop layer's web destination: "open whatsapp" opens
        ``web.whatsapp.com`` through this hook. The base implementation
        launches a browser binary; adapters may override (e.g. macOS
        ``open -a``). Never raises — returns False when nothing could
        open the URL.
        """
        binary = _find_browser(browser)
        if not binary:
            return False
        try:
            subprocess.Popen([binary, url],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            logger.warning(f"open_url failed: {exc}")
            return False

    # ── Notifications ─────────────────────────────────────────────

    def setup_instructions(self) -> str:
        """Return human-readable setup instructions for this platform.

        Subclasses override this when the desktop environment needs tools,
        permissions, or configuration before the adapter can work. The CLI
        surfaces this via ``friday6 desktop platforms`` when unavailable.
        """
        return (
            f"Desktop integration for '{self.name}' is not available on "
            "this machine. Install the required tools and try again."
        )

    @staticmethod
    def notify(title: str, message: str, urgency: str = "normal",
               timeout_ms: Optional[int] = None) -> bool:
        """Send a desktop notification on the current platform.

        Args:
            title: Notification title
            message: Notification body
            urgency: "low", "normal", or "critical" (Linux only)
            timeout_ms: Auto-dismiss timeout in milliseconds (Linux only).
                Defaults to the server default when None. Prefer an
                explicit timeout over ``urgency="critical"`` for anything
                that should fade on its own — critical banners are
                persistent on GNOME and several other desktops.
        """
        if os.name == "nt":
            return _notify_windows(title, message)
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        return _notify_linux(title, message, urgency, timeout_ms)

    # ── Helpers ───────────────────────────────────────────────────

    def _run(self, cmd: list[str], timeout: int = 5) -> Optional[str]:
        """Run a subprocess and return stdout, or None on failure."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"[{self.name}] command failed ({cmd[0]}): {exc}")
        return None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} available={self.is_available()}>"


# ---------------------------------------------------------------------------
# Adapter Registry
# ---------------------------------------------------------------------------

#: Names of every platform the abstraction layer supports (for `--help`).
SUPPORTED_PLATFORMS = [
    "hyprland",
    "gnome",
    "kde",
    "macos",
    "windows",
]


def create_adapter(de: str | None = None) -> DesktopAbstraction:
    """Create the adapter for a desktop environment name.

    Args:
        de: Desktop environment name (e.g. "hyprland", "gnome"). If None,
            auto-detects the current environment.

    Returns:
        A concrete adapter instance, or a bare ``DesktopAbstraction``
        (unavailable) if the environment is unknown/unsupported.
    """
    key = (de or detect_desktop_environment()).lower()

    # sway uses Hyprland-like wlroots IPC — best-effort via the
    # Hyprland adapter's hyprctl-style commands.
    if key in ("hyprland", "sway"):
        from .hyprland_adapter import HyprlandAdapter
        return HyprlandAdapter()
    if key == "gnome":
        from .gnome_adapter import GNOMEAdapter
        return GNOMEAdapter()
    if key in ("kde", "plasma"):
        from .kde_adapter import KDEAdapter
        return KDEAdapter()
    if key in ("macos", "darwin"):
        from .macos_adapter import MacOSAdapter
        return MacOSAdapter()
    if key in ("windows", "win32", "nt"):
        from .windows_adapter import WindowsAdapter
        return WindowsAdapter()

    # Generic X11/Wayland/i3: GNOMEAdapter's wmctrl/xdotool fallbacks work
    # on most X11 sessions and Shell Eval on GNOME Wayland.
    if key in ("wayland", "x11", "i3"):
        from .gnome_adapter import GNOMEAdapter
        return GNOMEAdapter()

    logger.debug(f"No adapter for '{key}', using base DesktopAbstraction")
    return DesktopAbstraction()


# ---------------------------------------------------------------------------
# WindowManager — Facade / Main Desktop Interface
# ---------------------------------------------------------------------------


class WindowManager:
    """Auto-detecting facade over the platform adapters.

    This is the class callers should use. It detects the current desktop
    environment, instantiates the matching adapter, and delegates to it.
    All public methods of ``DesktopAbstraction`` are available.

    Usage:
        wm = WindowManager()
        status = wm.get_status()
        windows = wm.list_windows()
        wm.focus_smart("code editor")
        wm.switch_workspace(2)
    """

    def __init__(self, de: str | None = None):
        self._de = de or detect_desktop_environment()
        self._adapter = create_adapter(self._de)
        logger.info(
            f"Desktop environment: {self._de} "
            f"({'available' if self.is_available else 'unavailable'})"
        )

    # ── Delegation ────────────────────────────────────────────────

    def __getattr__(self, name: str):
        """Delegate interface methods to the active adapter."""
        adapter = self.__dict__.get("_adapter")
        if adapter is None:
            raise AttributeError(name)
        # Only delegate public interface methods (avoids masking attributes)
        if name.startswith("_") or name in ("is_available", "desktop_environment"):
            raise AttributeError(name)
        attr = getattr(adapter, name)
        if attr is None:
            raise AttributeError(name)
        return attr

    @property
    def is_available(self) -> bool:
        return self._adapter.is_available()

    @property
    def desktop_environment(self) -> str:
        return self._de

    # Send a desktop notification on the current platform.
    notify = staticmethod(DesktopAbstraction.notify)

    def __repr__(self) -> str:
        return (f"<WindowManager de={self._de} "
                f"adapter={self._adapter.__class__.__name__} "
                f"available={self.is_available}>")
