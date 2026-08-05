"""Intent — LLM-first classification with deterministic fallback (Wave 13a).

The ONE NLU point: ``classify(text)`` asks the LLM first
(``LLMClient.parse_utterance``); the deterministic rules (kept from
Wave 9) run **only when the LLM is absent/offline** — never as the
primary path. The result is an :class:`IntentResult` with intent,
confidence, and slots, consumed by ``resolver.resolve()``.

Design:
- LLM primary: intent + entities + confidence come from the model.
- Rules fallback: identical shape, so callers never know which path ran.
- Never crash: no LLM, bad JSON, empty utterance → UNKNOWN + the
  caller's clarification path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .llm import LLMClient

logger = logging.getLogger("friday_v6.nlu.intent")


class Intent(str, Enum):
    """The high-level thing Friday is being asked to do (single set)."""

    EXECUTE = "execute"
    ASK = "ask"
    PLAN = "plan"
    DESKTOP = "desktop"
    GREETING = "greeting"
    HELP = "help"
    RESEARCH = "research"
    SKILL = "skill"
    ACCEPT = "accept"
    DENY = "deny"
    SECURITY = "security"
    MEMORY = "memory"
    STYLE = "style"
    IDE = "ide"          # diagnose/analyze code in the editor (Wave 6)
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    """One utterance's classification + slots (LLM or rules fallback)."""

    intent: Intent
    text: str
    confidence: float = 0.0
    action_type: Optional[str] = None   # shell | git | file | python | testing
    command: str = ""
    target: Optional[str] = None
    goal: Optional[str] = None
    entity_values: list[dict] = field(default_factory=list)
    needs_clarification: bool = False
    clarification: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "text": self.text,
            "confidence": self.confidence,
            "action_type": self.action_type,
            "command": self.command,
            "target": self.target,
            "goal": self.goal,
            "entities": self.entity_values,
            "needs_clarification": self.needs_clarification,
            "clarification": self.clarification,
        }


# ── deterministic fallback (ONLY when the LLM is unavailable) ─────────

#: Agentic-goal markers (Wave 18) — phrases that signal a *complex
#: task* with no single concrete command, e.g. "figure out why the
#: build fails and fix it". When EXECUTE resolves to an empty/no
#: command but the utterance reads like one of these, the task is
#: delegated to the Claude Code CLI (which can read, run, edit, and
#: iterate) instead of failing with "empty command".
_AGENTIC_MARKERS: tuple[str, ...] = (
    "figure out", "figure this out", "find out why", "work out why",
    "debug", "investigate", "troubleshoot", "diagnose", "root cause",
    "fix the", "fix this", "fix it", "fix a", "make the",
    "make it work", "get it working", "get the tests", "get the build",
    "repair", "sort out", "look into", "trace the", "resolve the",
    "why is the build", "why does the build", "why won't the build",
    # Wave 20 open-ended tasks: goal-shaped work with no single concrete
    # command ("create a python venv and install requests") delegates
    # to the Claude Code CLI instead of clarifying "what would you like
    # me to run?". "set up a venv"-style phrasing already scores PLAN
    # (mission); these catch the EXECUTE-classified cousins.
    "create", "set up", "set-up", "clone", "install", "organize",
    "build a", "build the", "write a", "write the", "download",
    "configure", "initialize", "scaffold", "deploy", "migrate",
    "refactor", "implement", "automate", "rename", "move the",
    "delete the", "fetch", "pull the", "push the", "commit the",
)

#: Words that score EXECUTE in the fallback for goal-shaped requests
#: ("debug the memory leak" has no run/test/git verb — it must still
#: classify as EXECUTE so the agentic check can route it to claude).
_AGENTIC_SCORE_WORDS: tuple[str, ...] = (
    "debug", "investigate", "troubleshoot", "diagnose", "repair",
    "install", "create", "clone", "organize", "configure",
)

#: Task verbs that turn a *desktop*-looking phrase into an open-ended
#: task ("open a python venv and install requests"). When one follows
#: a desktop verb the fallback reclassifies the utterance to the brain
#: (PLAN mission / EXECUTE via Claude Code) instead of the desktop
#: handler — never hardcoded workflows, never a web-search of a task.
_TASK_VERBS: tuple[str, ...] = (
    "create", "set up", "clone", "install", "organize", "build",
    "write", "download", "configure", "initialize", "scaffold",
    "deploy", "migrate", "refactor", "implement", "automate",
    "rename", "fetch", "pull", "push", "commit",
    # Repair verbs — "open main.py and fix it" is WORK for the Claude
    # arms, never a silent "open" that drops the task.
    "fix", "debug", "repair", "rewrite", "optimize", "tune",
)

#: Task nouns — "open a fresh project for a discord bot" is scaffolding
#: work, not a desktop command, even though no *verb* follows "open".
#: Kept to unambiguous build artifacts: "server"/"app" stay desktop
#: (open an installed app), "project"/"repo"/"venv" go to the brain.
_TASK_NOUNS: tuple[str, ...] = (
    "project", "venv", "virtualenv", "repo", "repository", "module",
    "package", "workflow", "pipeline", "bot", "extension", "plugin",
    "template", "website", "api", "database", "migration",
)


def is_agentic_goal(text: str) -> bool:
    """Whether an utterance reads like a complex goal, not a command.

    Used to route empty-command EXECUTE intents to the claude executor:
    "git status" (concrete) stays on the gated executors; "figure out
    why the build fails and fix it" (goal-shaped) delegates to Claude
    Code. Pure questions ("how do I debug") classify ASK/HELP and never
    reach this check.
    """
    lower = (text or "").lower()
    return any(marker in lower for marker in _AGENTIC_MARKERS)


_EXCLUSIVE: dict[Intent, tuple[str, ...]] = {
    Intent.GREETING: ("hello", "hi", "hey", "thanks", "thank you",
                      "good morning", "good evening", "good night",
                      "goodbye", "bye", "see you"),
    Intent.HELP: ("help", "what can you do", "what do you do",
                  "how do i use", "what commands"),
}

_SCORE: dict[Intent, tuple[str, ...]] = {
    # ACCEPT is ordered FIRST so a tie with EXECUTE ('run it' = run +
    # run it) resolves to ACCEPT — accepting a dispatch suggestion is the
    # intended meaning of a bare 'yes'/'do it'/'run it'. A concrete
    # execute command ('yes, run the tests') still wins on score (2 > 1).
    Intent.ACCEPT: ("yes", "yeah", "yep", "sure thing", "sounds good",
                    "go ahead", "do it", "run it", "proceed", "confirmed",
                    "accept"),
    # MEMORY ordered early so explicit-consent fact storage wins ties with
    # ASK ('is it true I prefer X?' → the 'i prefer' phrase → MEMORY).
    Intent.MEMORY: ("remember that", "don't forget", "do not forget",
                    "note that", "i prefer", "i like", "forget that",
                    "forget about", "forget", "teach me to remember"),
    # DENY words are the operator's explicit veto — the autonomy loop
    # records them as overrides. Placed AFTER MEMORY so "don't forget X"
    # (explicit-consent storage) wins the 'don't' tie, and BEFORE
    # EXECUTE so "don't run the tests" denies instead of running.
    Intent.DENY: ("no", "nope", "nah", "don't", "do not", "decline",
                  "never mind", "not now", "skip it", "deny",
                  "cancel that", "cancel the", "don't run", "don't do",
                  "stop that", "not that", "leave it", "different way",
                  "do it differently", "do that differently", "skip this"),
    # SECURITY ordered before ASK so 'is my code secure' (ASK 'is' +
    # SECURITY phrase tie) resolves to a scan, not a question.
    Intent.SECURITY: ("scan", "security", "vulnerab", "audit my",
                      "audit this", "check my deps", "check dependencies",
                      "check my dependencies", "check for secrets",
                      "check for vulnerabilities", "is my code secure",
                      "is my project secure", "security scan"),
    Intent.EXECUTE: ("run", "execute", "test", "tests", "pytest", "lint",
                     "linter", "ruff", "git", "python", "script", "build",
                     "compile", "mypy", "typecheck", "read", "write",
                     "delete", "move", "append", *_AGENTIC_SCORE_WORDS),
    Intent.ASK: ("what", "who", "when", "where", "why", "how",
                 "tell me", "explain", "summarize", "show me", "is",
                 "are", "did", "does"),
    Intent.PLAN: ("plan", "mission", "goal", "i need to", "i want to",
                  "by friday", "by monday", "deadline", "schedule",
                  "ship", "create a task", "set up", "improve", "refactor",
                  "migrate", "implement", "design", "fix", "add", "make",
                  "ensure", "prepare", "automate", "configure", "integrate",
                  "deploy", "launch", "upgrade", "clean up", "move to",
                  "start"),
    Intent.DESKTOP: ("focus", "switch", "workspace", "open", "launch",
                     "screenshot", "capture", "close", "minimize",
                     "maximize", "go to", "show windows", "windows",
                     "search", "look up", "google", "find"),
    Intent.RESEARCH: ("analyze", "correlate", "integration cost",
                      "what's the deal", "what is the deal", "compare",
                      "briefing", "brief me", "narrative", "report",
                      "research", "summary of the repo", "what's this repo",
                      "what is this repo", "readme", "vs", "versus"),
    Intent.SKILL: ("watch me", "watch this", "watch me do", "learn this",
                   "learn that", "learn from this", "learn from that",
                   "stop watching", "stop watch", "watch", "learn",
                   "teach yourself", "i'll show you", "i will show you",
                   "remember this", "record this"),
    # STYLE — adaptive identity (Wave 17): "be more casual", "be more
    # formal", "less chatter", "be yourself again". Tone-direction is an
    # explicit operator preference, never extracted from passive speech.
    Intent.STYLE: ("be more casual", "be casual", "be more formal",
                   "be formal", "more casual", "more formal", "less formal",
                   "tone down", "be friendly", "be warmer", "be less chatty",
                   "less chatter", "be briefer", "be more brief", "be brief",
                   "be more detailed", "be more verbose", "talk to me like",
                   "speak to me like", "be yourself", "back to normal",
                   "reset your tone", "reset your personality",
                   "casual", "formal"),
    # IDE (Wave 6/21) — diagnosing/analyzing code AND controlling the
    # editor (open/reveal). Ordered LAST so bare-word ties ('lint',
    # 'analyze', 'check', 'diagnose') resolve to the pre-existing
    # intents (EXECUTE/RESEARCH/SECURITY); the file-target tie-break
    # below promotes the real IDE asks.
    Intent.IDE: ("what's wrong with", "what is wrong with",
                 "why won't this compile", "why won't it compile",
                 "why is this file", "why does this file",
                 "why is this error", "why does this error",
                 "lint this", "lint the", "check this file",
                 "analyze this file", "analyse this file",
                 "diagnose this file", "diagnose", "analyze", "analyse",
                 "check", "lint", "syntax error", "syntax errors",
                 "compile error", "compile errors", "errors in",
                 "error in", "issues in", "problems in",
                 "is my code clean", "is this code clean", "check my code",
                 "code review", "review this file",
                 "what are the errors", "what is the error",
                 # Wave 21 — editor control verbs (file-token tie-break
                 # below does the heavy lifting; these seed the score).
                 "jump to line", "go to line", "take me to line",
                 "reveal", "in the editor", "in the ide", "in vscode",
                 "in code", "in sublime", "in neovim", "in nvim",
                 "in pycharm", "in intellij", "open file", "show file",
                 "open the file"),
}

_ACTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("testing", ("test", "tests", "pytest", "run the tests")),
    ("git", ("git",)),
    ("python", ("python", "script")),
    ("file", ("read", "write", "delete", "move", "append")),
    ("shell", ("lint", "linter", "ruff", "mypy", "typecheck",
               "build", "compile")),
)


def _count(lower: str, words: tuple[str, ...]) -> int:
    return sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", lower))


def _fallback_classify(text: str) -> IntentResult:
    """Deterministic rules — the offline fallback, same shape as LLM."""
    raw = (text or "").strip()
    lower = raw.lower()
    for intent, words in _EXCLUSIVE.items():
        if _count(lower, words):
            return IntentResult(intent=intent, text=raw, confidence=1.0)

    # App-learning phrases ("my todo app is obsidian", "use obsidian for
    # my todo app") are DESKTOP: the desktop interpreter teaches the
    # mapping and opens the app. Without this the "is" frame would land
    # on ASK and the app would never be learned (offline / voice path).
    try:
        from ..desktop.app_aliases import is_learning_phrase
        if is_learning_phrase(raw):
            return IntentResult(intent=Intent.DESKTOP, text=raw,
                                confidence=1.0)
    except Exception:
        pass  # desktop layer unavailable → normal scoring below

    scores = {i: _count(lower, w) for i, w in _SCORE.items()}

    # ── §2 hardening targeted tie-breaks (fallback only; the LLM decides
    # in production). These keep the offline path landing on the intent
    # the new NL surfaces expect:
    #
    # - Desktop read-queries are DESKTOP, not ASK: "what's on my screen"
    #   is answered by the window manager, not the reasoning layer.
    # - A leading desktop verb with an object is a concrete command even
    #   when a plan word shares it ("launch firefox" vs the plan-ish
    #   "launch the migration", which still keeps launch+migrate = 2
    #   plan signals and wins on score).
    for phrase in ("what's on my screen", "what is on my screen",
                   "what am i working on", "what's open", "what is open",
                   "what windows", "what's on screen", "show desktop"):
        if phrase in lower:
            scores[Intent.DESKTOP] += 2
    first = lower.split()[0] if lower.split() else ""
    if (first in ("focus", "switch", "open", "launch", "screenshot",
                  "close", "capture", "go", "show", "take")
            and scores[Intent.DESKTOP] > 0):
        scores[Intent.DESKTOP] += 1

    # ── Open-ended-task override (fallback only). A *desktop* verb
    # with a *task* verb/noun after it is not a desktop command — "open
    # a python venv and install requests", "open a fresh project for a
    # discord bot", "clone the repo and open it in my editor". The
    # brain must win outright (PLAN mission for scaffolding nouns /
    # EXECUTE via Claude Code for task verbs) so the desktop handler
    # never web-searches work the arms could do. Pure desktop opens
    # ("open whatsapp", "open chrome on workspace 3", "open the
    # server") contain no task verb/noun and keep the DESKTOP boost.
    task_verb_hit = _count(lower, _TASK_VERBS) > 0
    task_noun_hit = _count(lower, _TASK_NOUNS) > 0
    if first in ("open", "launch", "start", "run", "go", "create"):
        if task_noun_hit:
            scores[Intent.PLAN] += 2      # "open a fresh project" → scaffold
        elif task_verb_hit:
            scores[Intent.EXECUTE] += 2   # "open … and install requests" → do it

    # ── IDE control tie-break (fallback only, Wave 21). "open
    # src/main.py in the editor", "jump to line 42 of cli_talk.py",
    # "reveal auth.py" are EDITOR control, not desktop launches: a
    # leading open/show verb + a source-file target (whitelisted
    # extension) wins IDE — unless the utterance also reads like WORK
    # (task verb/noun), in which case the task override above already
    # routed it to the brain ("open main.py and fix it" → Claude).
    # Web destinations stay DESKTOP ("open youtube.com" — .com is not
    # a source extension). "jump/go/reveal" phrases score IDE via the
    # keyword table; this tie-break only needs the open/show case.
    if (first in ("open", "show", "go", "jump", "take", "bring")
            and not task_verb_hit and not task_noun_hit
            and re.search(_IDE_FILE_RE, lower)):
        scores[Intent.IDE] += 3

    # ── Autonomy denial tie-breaks (fallback only). The operator's
    # veto must win the offline path too: "do it a different way"
    # matches ACCEPT's "do it" AND DENY's "different way" — a 1-1 tie
    # that iteration order would hand to ACCEPT. The operator is
    # declining; DENY must win.
    if scores[Intent.DENY] > 0 and (
            "different way" in lower or "differently" in lower
            or "another way" in lower):
        scores[Intent.DENY] += 1

    # ── Research pair tie-break (fallback only). The MCU deep-
    # reasoning sentence "what's the deal between X and Y" scores ASK
    # ("what" + "is") above RESEARCH ("what's the deal" = 1) — the
    # research phrasing must win so the offline/no-LLM path still
    # researches instead of answering "I don't know yet". Same for the
    # compare phrasing "compare A versus B" (ASK's "is"/"are" can tie)
    # and the "with" variant "what's the deal with X and Y" (Wave 19
    # slice 2 sweep).
    #
    # Deliberately scoped to the PAIR markers only: a bare research
    # *lead* ("what's the deal with my security scan") must not hijack
    # a stronger intent (SECURITY=scan) — the lead alone is ambiguous;
    # the pair is unmistakably research. "with … and …" fires only when
    # BOTH appear (the scan phrase has no " and " pair).
    pair_marker = (" between " in lower or " vs " in lower
                   or " versus " in lower
                   or "integration cost" in lower
                   or (" with " in lower and " and " in lower))
    if scores[Intent.RESEARCH] > 0 and pair_marker:
        scores[Intent.RESEARCH] += 2

    # ── IDE tie-break (Wave 6, fallback only). The editor intents carry
    # short trigger words that legitimately belong to other intents too
    # ('lint' → EXECUTE, 'analyze' → RESEARCH, 'check' → SECURITY,
    # 'diagnose' → EXECUTE-agentic). IDE wins ONLY when the ask is
    # unmistakably about a concrete code target — a file path token
    # ('src/main.py') or a compile/syntax wording — otherwise the
    # pre-existing intent keeps the utterance. "diagnose the memory
    # leak" (no file) still routes to the Claude arms; "lint
    # src/main.py" lands on the IDE.
    _has_file_token = bool(re.search(r"\b[\w./\\-]+\.\w{1,10}\b", raw))
    if (scores[Intent.IDE] > 0
            and (_has_file_token
                 or "why won't this compile" in lower
                 or "why won't it compile" in lower)
            and any(w in lower for w in (
                "diagnose ", "check ", "analyze ", "analyse ", "lint",
                "compile error", "compile errors", "syntax error",
                "syntax errors", "errors in", "error in", "issues in",
                "problems in", "what's wrong", "what is wrong",
                "why won't", "is my code", "is this code"))):
        scores[Intent.IDE] += 2

    best = max(scores, key=scores.get)
    if best == Intent.EXECUTE and is_agentic_goal(raw):
        # Wave 18 hands: a complex agentic goal ("debug the memory
        # leak") has no concrete run/test/git verb — classify it as the
        # claude executor directly so the resolver delegates instead of
        # clarifying with "What would you like me to run?".
        result = IntentResult(intent=Intent.EXECUTE, text=raw,
                              confidence=0.9)
        result.action_type = "claude"
        result.command = raw
        result.target = raw
        return result
    if scores[best] == 0:
        # Bare "remember" / "forget" with nothing else to score is an
        # explicit memory command — route it so the handler asks "What
        # should I remember?" instead of falling through to UNKNOWN.
        # (ASK-y phrasings like "what do you remember" already scored
        # above and never reach this branch.)
        if "remember" in lower or "forget" in lower:
            result = IntentResult(intent=Intent.MEMORY, text=raw,
                                  confidence=0.6)
            result.target = _memory_trigger(raw)
            return result
        return IntentResult(intent=Intent.UNKNOWN, text=raw, confidence=0.0)
    best_score = scores[best]
    second = max(s for i, s in scores.items() if i != best)
    confidence = round(best_score / (best_score + second), 2) if second else 1.0
    result = IntentResult(intent=best, text=raw, confidence=confidence)
    if best == Intent.EXECUTE:
        for atype, words in _ACTION_RULES:
            if any(re.search(rf"\b{re.escape(w)}\b", lower) for w in words):
                result.action_type = atype
                if atype == "git":
                    m = re.search(r"\bgit\s+([a-z-]+)", lower)
                    result.command = m.group(1) if m else ""
                break
        result.target = result.command or None
    elif best == Intent.PLAN:
        result.goal = _goal(raw)
    elif best == Intent.DESKTOP:
        result.target = _desktop_target(raw)
    elif best == Intent.SKILL:
        result.target = _skill_trigger(raw)
    elif best == Intent.ACCEPT:
        result.target = "suggestion"
    elif best == Intent.DENY:
        result.target = "pending"
    elif best == Intent.SECURITY:
        result.target = _security_target(raw)
    elif best == Intent.MEMORY:
        result.target = _memory_trigger(raw)
    elif best == Intent.STYLE:
        result.target = _style_trigger(raw)
    elif best == Intent.IDE:
        result.target = _ide_target(raw)
    return result


def _security_target(text: str) -> Optional[str]:
    """The path/repo to scan, or None (scan the working directory)."""
    m = re.search(r"\b(?:scan|audit|check)\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    target = m.group(1).strip().rstrip(".!?")
    lower = target.lower()
    for prefix in ("my ", "this ", "the ", "our "):
        if lower.startswith(prefix):
            target = target[len(prefix):].strip()
            lower = target.lower()
    if lower in ("repo", "repository", "project", "code", "deps",
                 "dependencies", "secrets"):
        return None  # generic → cwd
    return target or None


def _memory_trigger(text: str) -> Optional[str]:
    """'remember' vs 'forget' — the memory write direction."""
    lower = text.lower()
    if "forget" in lower or "don't remember" in lower \
            or "don't recall" in lower:
        return "forget"
    return "remember"


def _style_trigger(text: str) -> Optional[str]:
    """The tone-direction target in an utterance ('be more casual' → casual).

    Wave 17 adaptive identity. Order matters: 'reset' words win ("be
    yourself again" / "back to normal" clear the direction), then the
    strongest requested tone wins. Returns None when nothing directional
    was found (caller asks for clarification).
    """
    lower = text.lower()
    if any(w in lower for w in ("be yourself", "back to normal",
                                "reset your tone", "reset your personality",
                                "reset")):
        return "reset"
    for tone in ("casual", "formal", "warm", "friendly", "close",
                 "neutral"):
        if tone in lower:
            return tone
    if any(w in lower for w in ("briefer", "brief", "less chatter",
                                "less chatty", "tone down", "shorter")):
        return "brief"
    if any(w in lower for w in ("detailed", "verbose", "longer",
                                "elaborate")):
        return "detailed"
    return None


#: Source-file extensions that make a token an *editor target* — the
#: IDE-control tie-break's whitelist, so "open youtube.com" stays a web
#: destination while "open main.py" opens the editor.
_IDE_FILE_EXTENSIONS = frozenset({
    "py", "pyw", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx",
    "rs", "go", "java", "kt", "kts", "c", "h", "cpp", "hpp", "cc",
    "cxx", "cs", "rb", "php", "swift", "scala", "hs", "ex", "exs",
    "erl", "clj", "lua", "r", "pl", "pm", "sh", "bash", "zsh",
    "css", "scss", "less", "html", "htm", "xml", "json", "yaml",
    "yml", "toml", "ini", "cfg", "conf", "md", "rst", "txt",
    "lock", "sql", "proto", "graphql", "gql", "vue", "svelte",
    "astro", "ipynb", "tf", "dockerfile", "cmake", "mk",
})

#: Matches a source-file token (whitelisted extension) in an utterance —
#: built from the whitelist so "open youtube.com" (.com not whitelisted)
#: stays a web destination while "open main.py" is an editor target.
_IDE_FILE_RE = re.compile(
    r"\b[\w./\\-]+\.(?:" + "|".join(sorted(_IDE_FILE_EXTENSIONS))
    + r")\b", re.IGNORECASE)


def _is_ide_file(token: str) -> bool:
    """Whether a token names a source file (whitelisted extension)."""
    m = re.search(r"\.([A-Za-z0-9]+)$", token.strip())
    return bool(m and m.group(1).lower() in _IDE_FILE_EXTENSIONS)


def _ide_line(text: str) -> Optional[int]:
    """The line number an IDE reveal targets, or None.

    "jump to line 42 of main.py" → 42; "reveal main.py:42" → 42.
    """
    m = re.search(r"\bline\s+(\d+)\b", text or "", re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"[\w./\\-]+\.\w+:(\d+)\b", text or "")
    return int(m.group(1)) if m else None


def _ide_control_verb(text: str) -> Optional[str]:
    """The editor-control action in an IDE utterance, or None.

    "jump to line 42 of main.py" / "reveal auth.py" → "reveal";
    "open main.py in the editor" → "open"; diagnostic phrases
    ("what's wrong with main.py", "show me the errors in main.py")
    → None (analysis, not control).
    """
    low = (text or "").strip().lower()
    if (re.search(r"\b(?:jump|go|take me|bring me)\s+to\s+line\b", low)
            or re.search(r"\breveal\b", low)):
        return "reveal"
    if (re.match(r"(?:please\s+)?(?:open|show)\b", low)
            and not re.search(r"\b(errors?|issues?|problems?|diagnos|"
                              r"analy|analyse|lint|review|what's|what is|why|"
                              r"syntax|compile|clean|warn)\b", low)):
        return "open"
    return None


def _ide_target(text: str) -> Optional[str]:
    """The file path an IDE ask targets, or None.

    "what's wrong with src/main.py" → src/main.py; "diagnose auth.py"
    → auth.py; "open src/main.py in the editor" → src/main.py; "jump
    to line 42 of cli_talk.py" → cli_talk.py. Falls back to any
    file-like token (``name.ext``) so "why won't this compile" without
    a named file stays target-less (the caller asks which file).
    """
    # Line-phrases first: "jump to line 42 of cli_talk.py" → the FILE
    # after "of/in", never the number.
    m = re.search(
        r"\b(?:jump|go|take me|bring me)\s+to\s+line\s+\d+\s+"
        r"(?:of|in)\s+([\w./\\-]+(?:\.\w+)?)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(".!?") or None
    triggers = ("what's wrong with", "what is wrong with", "diagnose",
                "check", "lint", "analyze", "analyse", "review",
                "error in", "errors in", "issues in", "problems in",
                "syntax error in", "compile error in", "reveal")
    pattern = (r"\b(?:" + "|".join(re.escape(t) for t in triggers)
               + r")\s+([\w./\\-]+(?:\.\w+)?)")
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        target = m.group(1).strip().rstrip(".!?")
        return target or None
    # Fallback: any source-file token (extension whitelist) — "open
    # src/main.py in the editor" → src/main.py. Web TLDs ("youtube.com")
    # are not whitelisted, so they stay desktop destinations.
    m = re.search(r"\b([\w./\\-]+\.\w{1,10})\b", text)
    if m and _is_ide_file(m.group(1)):
        return m.group(1)
    return None


def _skill_trigger(text: str) -> Optional[str]:
    """The demonstration trigger words in an utterance ('watch'/'learn')."""
    lower = text.lower()
    if "stop watching" in lower or "stop watch" in lower:
        return "stop"
    for word in ("watch", "learn", "teach", "remember this",
                 "record this", "show you"):
        if word in lower:
            return "start"
    return None


def _goal(text: str) -> str:
    low = text.lower()
    for prefix in ("i need to ", "i want to ", "plan: ", "please "):
        if low.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def _desktop_target(text: str) -> Optional[str]:
    for verb in ("focus", "switch", "open", "launch", "close"):
        m = re.search(rf"\b{verb}\s+(.+)", text, re.IGNORECASE)
        if m:
            target = m.group(1).strip().rstrip(".!")
            for skip in ("the", "to", "workspace"):
                if target.lower().startswith(skip + " "):
                    target = target[len(skip):].strip()
            return target or None
    return None


# ── the ONE entry point ───────────────────────────────────────────────

def classify(text: str, llm: Optional[LLMClient] = None) -> IntentResult:
    """LLM-first classification — deterministic rules only as fallback.

    Args:
        text: The utterance.
        llm: An LLM client (the single NLU point's provider). When None
            or unavailable, the deterministic rules run.

    Returns:
        An :class:`IntentResult` — never raises.
    """
    raw = (text or "").strip()
    if not raw:
        return IntentResult(intent=Intent.UNKNOWN, text="", confidence=0.0)

    if llm is not None:
        data = llm.parse_utterance(raw)
        if data is not None:
            try:
                intent = Intent(str(data.get("intent", "unknown")))
            except ValueError:
                intent = Intent.UNKNOWN
            return IntentResult(
                intent=intent,
                text=raw,
                confidence=float(data.get("confidence", 0.0) or 0.0),
                action_type=(data.get("action_type") or None),
                command=str(data.get("command") or ""),
                target=(data.get("target") or None),
                goal=(data.get("goal") or None),
                entity_values=[e for e in data.get("entities", [])
                               if isinstance(e, dict)],
                needs_clarification=bool(data.get("needs_clarification")),
                clarification=str(data.get("clarification") or ""),
            )

    return _fallback_classify(raw)


__all__ = ["Intent", "IntentResult", "classify", "is_agentic_goal",
           "_ide_target"]
