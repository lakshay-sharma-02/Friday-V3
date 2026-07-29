"""Optional LLM summarization via an OpenAI-compatible proxy.

Uses only the standard library (urllib) — no third-party HTTP dependency.
Configured through environment variables:

  FRIDAY_LLM_API_KEY   (required to enable; your 9router key)
  FRIDAY_LLM_MODEL     (required — no hardcoded default)
  FRIDAY_LLM_BASE_URL  (optional; defaults to http://localhost:20128/v1)

Returns None on any failure so callers fall back to deterministic extraction.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

DEFAULT_BASE_URL = "http://localhost:20128/v1"

_SYSTEM_PROMPT = (
    "You summarize software project READMEs for a durable knowledge base. "
    "Be concise and factual. Extract only what the text supports."
)

_USER_TEMPLATE = """Summarize the following README in this exact structure:

Purpose:
<one or two sentences on what the project is for>

Maturity:
<one word: WIP / Alpha / Beta / Stable / Unknown>

Important features:
- <feature 1>
- <feature 2>

Roadmap:
- <upcoming item 1, or "None stated">

Do not invent details. If a section is absent, write "None stated".

README:
===
{readme}
==="""


FALLBACK_PROVIDERS = [
    # Primary (9router proxy) tried first — local, fastest.
    {
        "name": "Primary",
        "url_env": "FRIDAY_LLM_BASE_URL",
        "key_env": "FRIDAY_LLM_API_KEY",
        "model_env": "FRIDAY_LLM_MODEL",
        "default_url": DEFAULT_BASE_URL,
    },
    # Groq — fast API, free tier.
    {
        "name": "Groq",
        "url_env": "GROQ_BASE_URL",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_url": "https://api.groq.com/openai/v1",
        "default_model": "llama3-70b-8192",
    },
    # OpenRouter — broad model selection, free models available.
    {
        "name": "OpenRouter",
        "url_env": "OPENROUTER_BASE_URL",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemini-flash-1.5",
    },
    # Gemini — Google's free tier.
    {
        "name": "Gemini",
        "url_env": "GEMINI_BASE_URL",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-1.5-flash",
    },
    # Ollama — fully local, last resort.
    {
        "name": "Ollama",
        "url_env": "OLLAMA_BASE_URL",
        "key_env": "OLLAMA_API_KEY",
        "model_env": "OLLAMA_MODEL",
        "default_url": "http://localhost:11434/v1",
        "default_key": "dummy",
    }
]

# ---------------------------------------------------------------------------
# LLM response cache (LRU, shared across all callers)
# ---------------------------------------------------------------------------

from collections import OrderedDict as _OrderedDict
import threading as _threading
import time as _time

_LLM_CACHE: _OrderedDict[int, tuple[str, float]] = _OrderedDict()
_LLM_CACHE_LOCK = _threading.Lock()
_LLM_CACHE_MAX = 256
_LLM_CACHE_TTL = 600  # 10 minutes


def _cache_key(system: str, user: str) -> int:
    return hash((system, user))


def _cache_get(key: int) -> str | None:
    now = _time.time()
    with _LLM_CACHE_LOCK:
        entry = _LLM_CACHE.get(key)
        if entry is None:
            return None
        result, ts = entry
        if now - ts > _LLM_CACHE_TTL:
            del _LLM_CACHE[key]
            return None
        _LLM_CACHE.move_to_end(key)
        return result


def _cache_set(key: int, value: str) -> None:
    now = _time.time()
    with _LLM_CACHE_LOCK:
        _LLM_CACHE[key] = (value, now)
        while len(_LLM_CACHE) > _LLM_CACHE_MAX:
            _LLM_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Parallel model caller — fire all models/providers at once.
# ---------------------------------------------------------------------------


def _parallel_call(
    system: str,
    user: str,
    timeout_per_model: int = 30,
    timeout_total: int = 45,
) -> str | None:
    """Call ALL configured model+provider combinations in parallel.

    Returns the first successful response. This eliminates the sequential
    fallback chain that made every call wait for N timeouts.

    ``timeout_per_model``: how long to wait per individual request.
    ``timeout_total``: overall deadline for the parallel batch.
    """
    from concurrent.futures import (
        ThreadPoolExecutor as _TPE,
        as_completed as _as_completed,
    )

    # Build all (provider, model, url, key) combos.
    combos: list[tuple[str, str, str, str]] = []
    for p in FALLBACK_PROVIDERS:
        key = os.environ.get(p["key_env"], p.get("default_key"))
        model = os.environ.get(p["model_env"], p.get("default_model"))
        base = os.environ.get(p["url_env"], p.get("default_url", DEFAULT_BASE_URL)).rstrip("/")
        if model:
            combos.append((p["name"], model, base, key or ""))

    if not combos:
        return None

    headers_base = {"Content-Type": "application/json"}

    def _try(combo: tuple[str, str, str, str]) -> str | None:
        name, model, base, key = combo
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
        }
        headers = dict(headers_base)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_per_model) as resp:
                raw = resp.read().decode("utf-8")
            return _extract_content(raw)
        except Exception:
            return None

    with _TPE(max_workers=min(len(combos), 8)) as pool:
        futures = {pool.submit(_try, c): c[0] for c in combos}
        try:
            for f in _as_completed(futures, timeout=timeout_total):
                result = f.result()
                if result:
                    return result
        except Exception:
            pass

    return None


def _enabled() -> bool:
    for p in FALLBACK_PROVIDERS:
        model = os.environ.get(p["model_env"], p.get("default_model"))
        if model:
            return True
    return False


def _call(system: str, user: str) -> Optional[str]:
    """Single OpenAI-compatible chat call. Returns assistant text, or None on any
    failure (disabled model, network/parse/proxy error) so callers fall back
    deterministically. SSE and single-object responses are both handled.

    Uses three speed optimizations:
    1. Response cache — repeated (system, user) pairs return instantly
    2. Parallel model probing — all providers/models fired concurrently
    3. Tight timeouts — 30s per model, 45s total
    """
    if not _enabled():
        return None

    # Check cache.
    ck = _cache_key(system, user)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    # Parallel probe all providers.
    result = _parallel_call(system, user, timeout_per_model=30, timeout_total=45)
    if result:
        _cache_set(ck, result)

    return result


def summarize(readme_text: str) -> Optional[str]:
    """Return an LLM-generated summary, or None if disabled or on any error."""
    return _call(
        _SYSTEM_PROMPT,
        _USER_TEMPLATE.format(readme=readme_text[:12000]),
    )


_PLAN_SYSTEM = (
    "You generate precise, minimal execution task lists for software engineering "
    "goals. Output ONLY valid JSON. Each task has: title, task_type (one of: "
    "implementation/testing/documentation/analysis/configuration/research), "
    "symbolic (with op and parameters), and acceptance_criteria (non-empty list). "
    "Be concise. For trivial single-step goals, return one task.\n\n"
    "CRITICAL: The project's language/stack is specified in the context below. "
    "ALL generated code MUST use that language. If the project is a Python project, "
    "write Python files (.py) — never Go, never JavaScript."
)

_PLAN_USER = """Given this engineering goal, produce a JSON task list.

Goal: {goal}

Relevant context from the knowledge base:
{evidence}

Return a JSON object with a single key "tasks", an array of task objects.
Each task object has:
- "title": short imperative description
- "task_type": one of "implementation", "testing", "documentation", "analysis", "configuration", "research"
- "symbolic": an object with:
    - "op": string operation name
    - "path": file path if relevant (else "")
    - "content": file content if a file should be created (else "")
    - "command": shell command if one should be run (else "")
    - "goal": the original goal
- "acceptance_criteria": list of strings describing success conditions
- "parallel_next": boolean, true if this task can run in parallel with the next

For trivial "create a file named X containing Y" goals, return ONE task
with task_type "implementation", symbolic.op "create_file", symbolic.path,
and symbolic.content.

For "run command X" goals, return ONE task with task_type "configuration",
symbolic.op "run_command", symbolic.command.

IMPORTANT for testing tasks: When creating a test file (task_type "testing"),
ALWAYS set symbolic.command to the command that runs the test, e.g.
"python -m pytest test_file.py -v". This is NOT optional — verification
runs this command to confirm the test passes.

Be concise. Do not fabricate details the goal doesn't supply."""

def plan_goal(goal: str, evidence_summary: str = "") -> Optional[str]:
    """Return a JSON task list for a goal, or None if LLM is unavailable."""
    evidence = evidence_summary.strip() or "(none available — plan from goal only)"
    return _call(
        _PLAN_SYSTEM,
        _PLAN_USER.format(goal=goal[:2000], evidence=evidence[:4000]),
    )


def _extract_content(raw: str) -> Optional[str]:
    """Return the assistant text from either a single JSON object or an SSE
    stream. Proxies may respond with a non-streamed ``chat.completion`` object
    or a streamed sequence of ``data: {...chunk...}`` lines (and a trailing
    ``data: [DONE]``)."""
    raw = raw.strip()
    if not raw:
        return None

    # Some proxies append a trailing `data: [DONE]` after a single JSON object
    # (no newline). Strip any SSE trailer line before the single-object parse.
    candidate = _strip_sse_trailer(raw)

    # Fast path: a single non-streamed JSON object.
    if not candidate.startswith("data:"):
        try:
            obj = json.loads(candidate)
            content = obj["choices"][0]["message"]["content"]
            # Some proxies return content as a JSON object (dict) rather than a
            # string — serialise it back so callers get valid JSON.
            if isinstance(content, str):
                return content.strip()
            return json.dumps(content, ensure_ascii=False)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            pass

    # SSE stream: concatenate delta.content across chunks.
    parts: list[str] = []
    for line in candidate.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if obj.get("object") != "chat.completion.chunk":
            continue
        delta = obj.get("choices", [{}])[0].get("delta", {})
        content = delta.get("content")
        if content:
            parts.append(content)
    if parts:
        return "".join(parts).strip()
    return None


def _strip_sse_trailer(raw: str) -> str:
    """Remove a trailing `data: [DONE]` (or any `data:` line) appended after a
    JSON object, so the object can be parsed directly."""
    raw = raw.rstrip()
    # If it ends with the SSE trailer, drop it.
    if raw.endswith("[DONE]"):
        idx = raw.rfind("data:")
        if idx != -1:
            raw = raw[:idx].rstrip()
    # Also drop an inline `data: [DONE]` stuck to the object with no newline.
    marker = 'data: [DONE]'
    if marker in raw:
        raw = raw.replace(marker, "").rstrip()
    # A `data:` line anywhere else (rare) -> take only the JSON portion.
    if "data:" in raw and not raw.startswith("data:"):
        head = raw.split("data:", 1)[0].rstrip()
        if head.startswith("{"):
            raw = head
    return raw


# ---------------------------------------------------------------------------
# Structured output — call the LLM and parse JSON from the response
# ---------------------------------------------------------------------------


def _parse_json_response(
    raw: str,
    required_keys: Optional[list[str]] = None,
) -> Optional[Any]:
    """Parse JSON from an LLM text response.

    Handles the common LLM output quirks that every caller was independently
    implementing:

    - Markdown code fence stripping (`````json ... `````, ````` ... `````)
    - JSON object extraction from surrounding prose (finds ``{...}``)
    - JSON array extraction (finds ``[...]``)
    - Optional required-key validation for dict results

    Args:
        raw: The raw LLM response text.
        required_keys: If provided and the result is a dict, all these keys
            must be present or the parse is considered failed.

    Returns:
        Parsed JSON data (``dict`` or ``list``), or ``None`` if no valid
        JSON could be extracted.
    """
    content = raw.strip()
    if not content:
        return None

    # Strip markdown code fences (```json ... ``` or just ``` ... ```).
    if "```" in content:
        start = content.find("```")
        end = content.rfind("```")
        if start != end:
            inner = content[start + 3:end].strip()
            if inner.startswith("json"):
                inner = inner[4:].strip()
            content = inner

    # Try direct parse first (fast path for well-formed responses).
    try:
        data = json.loads(content)
        _validate_required_keys(data, required_keys)
        return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find a JSON object ``{...}`` within the text.
    brace_start = content.find("{")
    if brace_start != -1:
        brace_end = content.rfind("}")
        if brace_end > brace_start:
            candidate = content[brace_start:brace_end + 1]
            try:
                data = json.loads(candidate)
                _validate_required_keys(data, required_keys)
                return data
            except (json.JSONDecodeError, ValueError):
                pass

    # Try to find a JSON array ``[...]`` within the text.
    bracket_start = content.find("[")
    if bracket_start != -1:
        bracket_end = content.rfind("]")
        if bracket_end > bracket_start:
            try:
                return json.loads(content[bracket_start:bracket_end + 1])
            except json.JSONDecodeError:
                pass

    return None


def _validate_required_keys(data: Any, required_keys: Optional[list[str]]) -> None:
    """Validate that all required keys exist in the parsed data.

    Only applies when ``data`` is a ``dict`` and ``required_keys`` is set.
    Raises ``ValueError`` with the list of missing keys.
    """
    if required_keys is None:
        return
    if not isinstance(data, dict):
        raise ValueError(f"Expected dict, got {type(data).__name__}")
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise ValueError(f"Missing required keys: {missing}")


def _call_structured(
    system: str,
    user: str,
    system_suffix: str = "",
    required_keys: Optional[list[str]] = None,
) -> Optional[Any]:
    """Call the LLM and return parsed structured JSON output.

    Wraps ``_call()`` with:
    1. A JSON-mode instruction appended to the system prompt
       (unless a custom ``system_suffix`` is provided)
    2. ``_parse_json_response()`` to handle markdown fences, prose-wrapped
       JSON, and optional key validation

    This eliminates the duplicated ``json.loads()`` + fence-stripping logic
    that every structured-output caller was independently implementing.

    Args:
        system: System prompt (JSON-mode instruction is appended).
        user: User message.
        system_suffix: Optional suffix appended to ``system``. Defaults to
            ``"\\n\\nRespond with ONLY valid JSON. No markdown, no explanation."``
        required_keys: If provided, the parsed dict must contain all these
            keys or ``None`` is returned.

    Returns:
        Parsed JSON (``dict`` or ``list``), or ``None`` on any failure.
        Never raises.
    """
    if not system_suffix:
        system_suffix = "\n\nRespond with ONLY valid JSON. No markdown, no explanation."

    raw = _call(system + system_suffix, user)
    if not raw:
        return None

    try:
        return _parse_json_response(raw, required_keys=required_keys)
    except Exception:
        return None
