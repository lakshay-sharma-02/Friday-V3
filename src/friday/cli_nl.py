"""Natural language command dispatcher — `friday do <text>`.

Routes unstructured natural language to the appropriate Friday CLI command
handler using LLM-based intent classification with keyword fallback.

Design (two layers):

1. **LLM classifier** (primary) — calls the configured LLM (9router proxy,
   OpenRouter, Groq, etc.) to classify the intent from a compact command list.
   Returns JSON ``{"intent": "command_name"}``.

2. **Keyword fallback** — if the LLM is unavailable or returns something
   unrecognised, falls back to keyword matching (same as v1).

Each intent maps to a builder function that constructs the right
``argparse.Namespace`` for the target command handler.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# LLM intent classification
# ---------------------------------------------------------------------------

_INTENT_MAP: dict[str, str] = {
    "ask": "Answer questions, get information, chitchat, greeting, or general help",
    "execute": "Run a goal, deploy something, execute a task, start/stop a server",
    "create": "Create a file, write code, generate a script",
    "plan": "Create an execution plan for a goal",
    "graph": "Compile a task graph for a goal",
    "observe": "Refresh workspace knowledge, scan for changes, observe repos",
    "summary": "Show workspace overview, status, what's going on",
    "patterns": "Show mined patterns, mine, label, or form patterns",
    "knowledge": "Build, show, or explain knowledge base entries",
    "analyze": "Analyze a repository's architecture or structure",
    "profile": "Show or manage operator profile and preferences",
    "skills": "List, invoke, or analyze formed skills",
    "actions": "Show recent action log (what Friday has done)",
    "review": "Review plans, graphs, runtime sessions, or workspace",
    "daemon": "Start, stop, restart, or check daemon status",
    "doctor": "Run system diagnostics and health checks",
    "autonomy": "Manage autonomy settings, kill switch, action permissions",
    "email": "Send or check emails",
    "slack": "Interact with Slack workspace",
    "discord": "Interact with Discord server",
    "telegram": "Interact with Telegram bot",
    "initiatives": "Build, show, or manage engineering initiatives",
    "insights": "Build, show, or manage engineering insights",
    "identity": "Manage Friday's persona/identity",
    "suggest": "Get cross-project integration suggestions",
    "integrate": "Analyze 2+ repos for integration opportunities",
    "portfolio": "Workspace portfolio: themes, overlap, value ranking",
    "strategy": "Strategic judgment: impact, platform, learning, opportunity",
    "repair": "Approve or reject repair proposals for failed tasks",
    "correlate": "Find correlations between repositories",
}

_LLM_SYSTEM_PROMPT = """You are an intent classifier for Friday, an AI operating partner.
Given a user's natural language request, determine which command to run.

Available commands:
{intent_list}

Respond with ONLY this JSON (no other text, no markdown):
{{"intent": "command_name"}}

If unsure, or if the user is just greeting or making chitchat, use "ask"."""


def _llm_classify(text: str) -> Optional[str]:
    """Use the configured LLM to classify the user's intent.

    Returns the intent name (e.g. ``'execute'``) or ``None`` if the LLM
    is unavailable or returns something unrecognised.

    Tries multiple models sequentially to handle quota limits on free tier:
    1. ``FRIDAY_LLM_MODEL`` env var (primary, from 9router)
    2. ``openrouter/google/gemma-4-26b-a4b-it:free``
    3. ``openrouter/google/gemma-4-31b-it:free``
    4. ``openrouter/nvidia/nemotron-3-super-120b-a12b:free``
    """
    import json as _json
    import os as _os
    import urllib.request as _urllib

    base = _os.environ.get(
        "FRIDAY_LLM_BASE_URL", "http://localhost:20128/v1"
    ).rstrip("/")

    # Model fallback list: primary from env, then known free models.
    primary_model = _os.environ.get("FRIDAY_LLM_MODEL", "")
    fallback_models = [
        "openrouter/google/gemma-4-26b-a4b-it:free",
        "openrouter/google/gemma-4-31b-it:free",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
    ]
    models = [primary_model] + [m for m in fallback_models if m != primary_model]
    models = [m for m in models if m]  # remove empties

    intent_lines = "\n".join(
        f"  {name}: {desc}" for name, desc in _INTENT_MAP.items()
    )
    prompt = _LLM_SYSTEM_PROMPT.format(intent_list=intent_lines)
    headers_base = {"Content-Type": "application/json"}
    key = _os.environ.get("FRIDAY_LLM_API_KEY", "")
    if key:
        headers_base["Authorization"] = f"Bearer {key}"

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"User request: {text}"},
            ],
            "temperature": 0.0,
        }
        req = _urllib.Request(
            f"{base}/chat/completions",
            data=_json.dumps(payload).encode("utf-8"),
            headers=dict(headers_base),  # copy so we can mutate per-model
            method="POST",
        )
        try:
            with _urllib.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except Exception:
            continue

        # Strip trailing SSE trailer (e.g. `data: [DONE]`).
        raw = raw.strip()
        if "[DONE]" in raw and "data:" in raw:
            idx = raw.rfind("data:")
            if idx != -1:
                raw = raw[:idx].rstrip()
        if not raw:
            continue

        try:
            obj = _json.loads(raw)
            result = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not result:
                continue
            result = result.strip()
        except (_json.JSONDecodeError, KeyError, IndexError, TypeError):
            continue

        # Handle markdown code fences in the result.
        if "```" in result:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result, re.DOTALL)
            if m:
                result = m.group(1).strip()

        try:
            obj = _json.loads(result)
            intent = obj.get("intent", "").strip().lower()
        except (_json.JSONDecodeError, TypeError):
            lower = result.lower()
            for name in _INTENT_MAP:
                if name in lower:
                    return name
            continue

        if intent in _INTENT_MAP:
            return intent
        for name in _INTENT_MAP:
            if name in intent or intent in name:
                return name

    return None


# ---------------------------------------------------------------------------
# Builder functions — each constructs the correct argparse.Namespace
# ---------------------------------------------------------------------------


def _build_ask(text: str):
    from .cli import cmd_ask

    ns = argparse.Namespace(question=text, verbose=False)
    return cmd_ask, ns


def _build_create(text: str):
    """Direct file creation — no execute pipeline, no mission control.

    Extracts filename from "name it X" or "named X", then generates
    content via a single fast LLM call and writes the file.
    """
    # Extract filename: "name it xyz.py" or "named xyz.py" or last word.
    m = re.search(r"(?:name it|named)\s+['\"]?(.+?)(?:$|\s)", text, re.IGNORECASE)
    if m:
        filename = m.group(1).strip().strip("'\".,;!")
    else:
        # Fallback: last word that looks like a filename.
        words = text.split()
        filename = words[-1].strip("'\".,;!")
        # If no extension, add .py as default.
        if "." not in filename:
            filename = filename + ".py"

    # Safety: use just the basename (strip directory components).
    import os as _os
    filename = _os.path.basename(filename)
    if not filename:
        filename = "output.py"

    ns = argparse.Namespace(
        filename=filename,
        description=text,
    )
    from .cli import cmd_create_file
    return cmd_create_file, ns


def _build_execute(text: str):
    from .cli_execute import cmd_execute

    ns = argparse.Namespace(
        goal=[text],
        workspace=".",
        yes=False,
        dry_run=False,
    )
    return cmd_execute, ns


def _build_plan(text: str):
    from .cli_planning import cmd_plan

    goal = re.sub(
        r"^(plan|how to|steps to)\s+", "", text, flags=re.IGNORECASE
    ).strip()
    ns = argparse.Namespace(
        goal=goal or text,
        action=None,
        plan_id=None,
        id=None,
    )
    return cmd_plan, ns


def _build_graph(text: str):
    from .cli_graph import cmd_graph

    goal = re.sub(
        r"^(graph|task graph)\s+", "", text, flags=re.IGNORECASE
    ).strip()
    ns = argparse.Namespace(
        goal=goal or text,
        action=None,
        graph_id=None,
        id=None,
    )
    return cmd_graph, ns


def _build_observe(text: str):
    from .cli import cmd_observe

    only_changed = any(
        w in text.lower() for w in ("changed", "modified", "updated")
    )
    ns = argparse.Namespace(
        repo=None,
        changed=only_changed,
        summary=False,
    )
    return cmd_observe, ns


def _build_summary(text: str):
    from .cli import cmd_summary

    ns = argparse.Namespace()
    return cmd_summary, ns


def _build_patterns(text: str):
    from .cli_patterns import cmd_patterns

    action = None
    lower = text.lower()
    if "mine" in lower:
        action = "mine"
    elif "label" in lower:
        action = "label"
    elif "form" in lower:
        action = "form"
    elif "clear" in lower:
        action = "clear"
    ns = argparse.Namespace(action=action, force=False, min_count=0, limit=50)
    return cmd_patterns, ns


def _build_knowledge(text: str):
    from .cli_knowledge import cmd_knowledge

    action = None
    lower = text.lower()
    if "build" in lower:
        action = "build"
    elif "explain" in lower:
        action = "explain"
    elif "history" in lower:
        action = "history"
    elif "evolution" in lower:
        action = "evolution"
    ns = argparse.Namespace(action=action, knowledge_id=None, id=None, verbose=False)
    return cmd_knowledge, ns


def _build_analyze(text: str):
    from .cli import cmd_analyze

    # Try to extract a path or repo name.
    repo_match = re.search(
        r"(?:analyze|architecture of|analyse)\s+['\"]?(.+?)['\"]?(?:\s*$|\.)",
        text,
        re.IGNORECASE,
    )
    repo = repo_match.group(1) if repo_match else text
    # If text contains repo-like paths, try to extract the most likely one.
    if repo == text and "/" in text:
        # Probably a path — use the last segment as the repo identifier.
        parts = text.split()
        repo = parts[-1]
    ns = argparse.Namespace(repository=repo)
    return cmd_analyze, ns


def _build_profile(text: str):
    from .cli_profile import cmd_profile

    action = "show"
    lower = text.lower()
    if "set " in lower:
        action = "set"
    elif "unset" in lower:
        action = "unset"
    elif "history" in lower:
        action = "history"
    elif "derive" in lower:
        action = "derive"
    ns = argparse.Namespace(action=action, key=None, value=None)
    return cmd_profile, ns


def _build_skills(text: str):
    from .cli_skills import cmd_skills

    action = "list"
    name = None
    lower = text.lower()
    if "run " in lower:
        action = "run"
        m = re.search(r"run\s+['\"]?(.+?)['\"]?(?:\s*$|\.)", text, re.IGNORECASE)
        name = m.group(1) if m else None
    elif "drift" in lower:
        action = "drift"
    ns = argparse.Namespace(action=action, name=name, on_failure=None)
    return cmd_skills, ns


def _build_actions(text: str):
    from .cli_actions import cmd_actions

    ns = argparse.Namespace(n=50, source=None)
    return cmd_actions, ns


def _build_review(text: str):
    from .cli_review import cmd_review

    token = None
    lower = text.lower()
    if "plan" in lower:
        token = "plan"
    elif "graph" in lower:
        token = "graph"
    elif "runtime" in lower:
        token = "runtime"
    elif "portfolio" in lower:
        token = "portfolio"
    ns = argparse.Namespace(token=token, rest=[], id=None)
    return cmd_review, ns


def _build_correlate(text: str):
    from .cli import cmd_correlate

    ns = argparse.Namespace(detail=None, scan_docs=False)
    return cmd_correlate, ns


def _build_daemon(text: str):
    from .cli_daemon import cmd_daemon

    action = "status"
    lower = text.lower()
    if "start" in lower:
        action = "start"
    elif "stop" in lower:
        action = "stop"
    elif "restart" in lower:
        action = "restart"
    elif "log" in lower:
        action = "logs"
    ns = argparse.Namespace(action=action, interval=900, no_notify=False, lines=50)
    return cmd_daemon, ns


def _build_doctor(text: str):
    from .doctor import cmd_doctor

    ns = argparse.Namespace()
    return cmd_doctor, ns


def _build_autonomy(text: str):
    from .cli_autonomy import cmd_autonomy

    subcommand = "status"
    lower = text.lower()
    if "enable" in lower:
        subcommand = "enable"
    elif "disable" in lower:
        subcommand = "disable"
    elif "kill" in lower:
        subcommand = "kill"
    elif "resume" in lower:
        subcommand = "resume"
    ns = argparse.Namespace(subcommand=subcommand, action_type=None, level=None)
    return cmd_autonomy, ns


def _build_email(text: str):
    from .cli_email import cmd_email

    action = None
    lower = text.lower()
    if "inbox" in lower:
        action = "inbox"
    elif "send" in lower:
        action = "send"
    elif "config" in lower:
        action = "config"
    elif "setup" in lower:
        action = "setup"
    ns = argparse.Namespace(action=action, to=None, subject=None, limit=20)
    return cmd_email, ns


def _build_slack(text: str):
    from .cli_slack import cmd_slack

    action = None
    lower = text.lower()
    if "channel" in lower:
        action = "channels"
    elif "send" in lower:
        action = "send"
    elif "config" in lower:
        action = "config"
    elif "setup" in lower:
        action = "setup"
    ns = argparse.Namespace(action=action, channel=None, text=[], limit=20)
    return cmd_slack, ns


def _build_discord(text: str):
    from .cli_discord import cmd_discord

    action = None
    lower = text.lower()
    if "guild" in lower:
        action = "guilds"
    elif "channel" in lower:
        action = "channels"
    elif "send" in lower:
        action = "send"
    elif "config" in lower:
        action = "config"
    elif "setup" in lower:
        action = "setup"
    ns = argparse.Namespace(
        action=action, guild_id=None, channel=None, content=[]
    )
    return cmd_discord, ns


def _build_initiatives(text: str):
    from .cli_initiative import cmd_initiatives

    action = None
    lower = text.lower()
    if "build" in lower:
        action = "build"
    elif "timeline" in lower:
        action = "timeline"
    ns = argparse.Namespace(action=action, initiative_id=None, id=None)
    return cmd_initiatives, ns


def _build_insights(text: str):
    from .cli_insight import cmd_insights

    action = None
    lower = text.lower()
    if "build" in lower:
        action = "build"
    elif "evolution" in lower:
        action = "evolution"
    ns = argparse.Namespace(action=action, insight_id=None, id=None)
    return cmd_insights, ns


def _build_identity(text: str):
    from .cli_identity import cmd_identity

    action = None
    lower = text.lower()
    if "chat" in lower:
        action = "chat"
    elif "telegram" in lower:
        action = "telegram"
    ns = argparse.Namespace(action=action, sub=None)
    return cmd_identity, ns


def _build_suggest(text: str):
    from .cli_suggest import cmd_suggest

    ns = argparse.Namespace(graph=None)
    return cmd_suggest, ns


def _build_integrate(text: str):
    from .cli_integration import cmd_integrate

    # Extract repo names from "integrate vivaha aether" or "merge repos x y"
    m = re.search(
        r"(?:integrate|merge\s+repos)\s+(.+?)$", text, re.IGNORECASE
    )
    repos = m.group(1).strip().split() if m else text.split()[1:]
    ns = argparse.Namespace(repos=repos if repos else [])
    return cmd_integrate, ns


def _build_repair(text: str):
    from .cli_repair import cmd_repair

    action = "pending"
    lower = text.lower()
    if "approve" in lower:
        action = "approve"
    elif "reject" in lower:
        action = "reject"
    ns = argparse.Namespace(action=action, rest=[])
    return cmd_repair, ns


def _build_portfolio(text: str):
    from .cli_portfolio import cmd_portfolio_dispatch

    token = None
    lower = text.lower()
    if "theme" in lower:
        token = "themes"
    elif "overlap" in lower:
        token = "overlap"
    elif "ranking" in lower or "value" in lower:
        token = "ranking"
    elif "recommend" in lower:
        token = "recommendations"
    elif "integration" in lower:
        token = "integrations"
    ns = argparse.Namespace(token=token)
    return cmd_portfolio_dispatch, ns


def _build_strategy(text: str):
    from .cli_strategy import cmd_strategy

    token = None
    lower = text.lower()
    if "impact" in lower:
        token = "impact"
    elif "platform" in lower:
        token = "platform"
    elif "learning" in lower:
        token = "learning"
    elif "opportunity" in lower:
        token = "opportunity"
    elif "priority" in lower or "prioritize" in lower:
        token = "priority"
    elif "merge" in lower:
        token = "merge"
    elif "converge" in lower:
        token = "converge"
    ns = argparse.Namespace(token=token)
    return cmd_strategy, ns


_INTENT_HANDLERS: dict[str, Callable[[str], Any]] = {
    "ask": _build_ask,
    "create": _build_create,
    "execute": _build_execute,
    "plan": _build_plan,
    "graph": _build_graph,
    "observe": _build_observe,
    "summary": _build_summary,
    "patterns": _build_patterns,
    "knowledge": _build_knowledge,
    "analyze": _build_analyze,
    "profile": _build_profile,
    "skills": _build_skills,
    "actions": _build_actions,
    "review": _build_review,
    "daemon": _build_daemon,
    "doctor": _build_doctor,
    "autonomy": _build_autonomy,
    "email": _build_email,
    "slack": _build_slack,
    "discord": _build_discord,
    "initiatives": _build_initiatives,
    "insights": _build_insights,
    "identity": _build_identity,
    "suggest": _build_suggest,
    "integrate": _build_integrate,
    "portfolio": _build_portfolio,
    "strategy": _build_strategy,
    "repair": _build_repair,
    "correlate": _build_correlate,
}

# Also alias "telegram" and "slack" etc. that use the same builder.
_INTENT_HANDLERS["telegram"] = _build_identity  # shares identity builder


# ---------------------------------------------------------------------------
# Keyword fallback intent table — ordered by priority
# ---------------------------------------------------------------------------

_IntentDef = tuple[int, tuple[str, ...], Callable[[str], Any]]

_INTENTS: list[_IntentDef] = [
    # Communication channels
    (10, ("email", "send email", "check email", "my inbox", "inbox"), _build_email),
    (10, ("slack",), _build_slack),
    (10, ("discord",), _build_discord),
    # System control
    (20, ("daemon", "start daemon", "stop daemon", "restart daemon", "daemon status", "daemon log"), _build_daemon),
    (20, ("doctor", "system health", "health check"), _build_doctor),
    (20, ("kill switch", "autonomy", "enable autonomy", "disable autonomy"), _build_autonomy),
    (20, ("identity", "friday identity", "chat with friday"), _build_identity),
    # Knowledge & understanding
    (30, ("knowledge", "what do you know", "build knowledge"), _build_knowledge),
    (30, ("initiative", "build initiative", "initiative timeline"), _build_initiatives),
    (30, ("insight", "build insight", "insight evolution"), _build_insights),
    # File creation (high priority — must come before execute)
    (35, ("create ", "make ", "write ", "generate "), _build_create),
    # Workspace operations
    (40, ("plan", "how to", "steps to", "make a plan"), _build_plan),
    (40, ("graph", "task graph", "compile a graph"), _build_graph),
    (40, ("deploy", "run ", "execute", "start ", "stop "), _build_execute),
    (40, ("propose", "suggest", "find opportunities"), _build_suggest),
    (40, ("correlate", "find correlations"), _build_correlate),
    (40, ("integrate ", "merge repos"), _build_integrate),
    (40, ("repair", "repair proposals", "approve repair", "reject repair"), _build_repair),
    # Analysis & review
    (50, ("analyze",), _build_analyze),
    (50, ("review", "check"), _build_review),
    (50, ("portfolio", "workspace overview"), _build_portfolio),
    (50, ("strategy", "impact", "platform", "learning", "opportunity", "priority", "merge", "converge"), _build_strategy),
    # Patterns & skills
    (60, ("pattern", "workflow", "mine pattern", "label pattern"), _build_patterns),
    (60, ("skill", "formed skill", "run skill", "skill drift"), _build_skills),
    (60, ("action", "what have i done", "recent action"), _build_actions),
    # Observe & summarize
    (70, ("observe", "refresh", "scan", "update workspace"), _build_observe),
    (70, ("summary", "status", "overview", "state of", "what's going on"), _build_summary),
    (70, ("profile", "preference", "operator profile"), _build_profile),
    # Explain / describe (maps to ask)
    (80, ("what is", "tell me about", "explain", "describe", "how does", "why is", "where is", "who built", "show me", "list ", "what are", "what's"), _build_ask),
]


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fast keyword cache — pre-compile keyword matchers
# ---------------------------------------------------------------------------

# Sorted intent table (by priority, lowest first).
_SORTED_INTENTS: list[_IntentDef] = sorted(_INTENTS, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# LLM response cache
# ---------------------------------------------------------------------------

import threading as _threading
from collections import OrderedDict as _OrderedDict

_LLM_CACHE: _OrderedDict[int, tuple[str, str, str]] = _OrderedDict()
_LLM_CACHE_LOCK = _threading.Lock()
_LLM_CACHE_MAX = 128
_LLM_CACHE_TTL = 300  # seconds


def _cache_key(system: str, user: str) -> int:
    return hash((system, user))


def _cache_get(key: int) -> str | None:
    import time as _time
    now = _time.time()
    with _LLM_CACHE_LOCK:
        if key not in _LLM_CACHE:
            return None
        result, ts, prev = _LLM_CACHE[key]
        if now - ts > _LLM_CACHE_TTL:
            del _LLM_CACHE[key]
            return None
        # Move to end (most recently used).
        _LLM_CACHE.move_to_end(key)
        return result


def _cache_set(key: int, value: str) -> None:
    import time as _time
    now = _time.time()
    with _LLM_CACHE_LOCK:
        _LLM_CACHE[key] = (value, now, "")
        while len(_LLM_CACHE) > _LLM_CACHE_MAX:
            _LLM_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Parallel model caller — fire all models at once, take first response.
# ---------------------------------------------------------------------------


def _parallel_llm_call(system: str, user: str) -> str | None:
    """Call the configured LLM with parallel model probing.

    Fires ALL configured models concurrently and returns the first successful
    response. Uses a short timeout per model (15s) so a slow model doesn't
    block everything.
    """
    import json as _json
    import os as _os
    import urllib.request as _urllib
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _as_completed
    import time as _time

    base = _os.environ.get(
        "FRIDAY_LLM_BASE_URL", "http://localhost:20128/v1"
    ).rstrip("/")
    key = _os.environ.get("FRIDAY_LLM_API_KEY", "")
    primary_model = _os.environ.get("FRIDAY_LLM_MODEL", "")

    # Build the list of models to try.
    fallback_models = [
        "openrouter/google/gemma-4-26b-a4b-it:free",
        "openrouter/google/gemma-4-31b-it:free",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
    ]
    models = []
    if primary_model:
        models.append(primary_model)
    for m in fallback_models:
        if m not in models:
            models.append(m)

    if not models:
        return None

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    def _try_model(model: str) -> str | None:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
        }
        req = _urllib.Request(
            f"{base}/chat/completions",
            data=_json.dumps(payload).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with _urllib.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
        except Exception:
            return None

        # Use existing extractor from llm.py
        try:
            from .services.llm import _extract_content
            result = _extract_content(raw)
            if result:
                return result
        except Exception:
            pass
        return None

    # Fire ALL models concurrently.
    with _TPE(max_workers=len(models)) as pool:
        futures = {pool.submit(_try_model, m): m for m in models}
        for f in _as_completed(futures, timeout=20):
            result = f.result()
            if result:
                return result

    return None


def _llm_classify(text: str) -> Optional[str]:
    """Use the configured LLM to classify the user's intent.

    Only called by ``classify_intent`` AFTER the fast keyword check has
    already been tried — so this function purely handles the LLM probe.
    Uses parallel model probing + LRU cache.
    """

    intent_lines = "\n".join(
        f"  {name}: {desc}" for name, desc in _INTENT_MAP.items()
    )
    prompt = _LLM_SYSTEM_PROMPT.format(intent_list=intent_lines)
    user_prompt = f"User request: {text}"

    # Check cache first.
    ck = _cache_key(prompt, user_prompt)
    cached = _cache_get(ck)
    if cached is not None:
        return cached or None

    # Use parallel model probe.
    raw = _parallel_llm_call(prompt, user_prompt)
    if not raw:
        return None

    result = raw.strip()

    # Handle markdown code fences in the result.
    if "```" in result:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result, re.DOTALL)
        if m:
            result = m.group(1).strip()

    try:
        obj = json.loads(result)
        intent = obj.get("intent", "").strip().lower()
    except (json.JSONDecodeError, TypeError):
        # Try to find intent name in raw result text.
        lower_result = result.lower()
        for name in _INTENT_MAP:
            if name in lower_result:
                _cache_set(ck, name)
                return name
        return None

    if intent in _INTENT_MAP:
        _cache_set(ck, intent)
        return intent
    # Fuzzy match: intent name is a substring of result or vice versa.
    for name in _INTENT_MAP:
        if name in intent or intent in name:
            _cache_set(ck, name)
            return name

    return None


def classify_intent(text: str) -> tuple[Callable, argparse.Namespace]:
    """Classify natural language text and return (handler_fn, namespace).

    Classification order:
    1. Fast keyword scan (no LLM call for known commands) — sub-ms
    2. Cached LLM response — instant
    3. Parallel LLM classifier — all models probed concurrently, 15s max
    4. Keyword fallback (re-check after LLM fails)
    5. ``ask()`` as final fallback
    """
    lower = text.lower().strip()

    # Layer 1: Fast keyword scan before any LLM call.
    for _priority, keywords, builder in _SORTED_INTENTS:
        if any(kw in lower for kw in keywords):
            return builder(text)

    # Layer 2: LLM classifier (parallel models, cached).
    llm_intent = _llm_classify(text)
    if llm_intent and llm_intent in _INTENT_HANDLERS:
        builder = _INTENT_HANDLERS[llm_intent]
        return builder(text)

    # Layer 3: Keyword fallback (re-check — LLM may have returned garbage).
    for _priority, keywords, builder in _SORTED_INTENTS:
        if any(kw in lower for kw in keywords):
            return builder(text)

    # Layer 4: Final fallback — ask().
    return _build_ask(text)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def cmd_do(args: argparse.Namespace) -> int:
    """``friday do <text>`` — natural language command dispatcher.

    Parses the text, classifies the intent, builds the appropriate
    ``argparse.Namespace``, and dispatches to the matching CLI handler.
    """
    from .presentation.cli_format import header, error as perror

    text = " ".join(args.text)
    if not text.strip():
        print("  Friday — Natural Language Commands\n")
        print("  Just tell me what you want in plain English:")
        print()
        print("    friday what's the state of things")
        print("    friday show my recent patterns")
        print("    friday deploy the staging server")
        print("    friday refresh my workspace")
        print("    friday start the daemon")
        print("    friday what's the architecture")
        print("    friday send an email to lakshay")
        print("    friday analyze the codebuff project")
        print()
        return 0

    try:
        handler, ns = classify_intent(text)
    except Exception as exc:
        print(header("ask", "not sure"), file=sys.stderr)
        print(perror(f"Couldn't understand: {exc}"), file=sys.stderr)
        handler, ns = _build_ask(text)

    cmd_name = handler.__name__.replace("cmd_", "").replace("_dispatch", "")
    print(header(cmd_name, text[:80]), file=sys.stderr)
    print(file=sys.stderr)

    # Stream handler output directly — don't buffer.
    rc = handler(ns)

    if rc != 0:
        print(perror(f"Command returned exit code {rc}"))

    return rc
