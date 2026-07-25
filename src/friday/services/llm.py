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
    {
        "name": "Primary",
        "url_env": "FRIDAY_LLM_BASE_URL",
        "key_env": "FRIDAY_LLM_API_KEY",
        "model_env": "FRIDAY_LLM_MODEL",
        "default_url": DEFAULT_BASE_URL,
    },
    {
        "name": "Groq",
        "url_env": "GROQ_BASE_URL",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_url": "https://api.groq.com/openai/v1",
        "default_model": "llama3-70b-8192",
    },
    {
        "name": "Gemini",
        "url_env": "GEMINI_BASE_URL",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-1.5-flash",
    },
    {
        "name": "OpenRouter",
        "url_env": "OPENROUTER_BASE_URL",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemini-flash-1.5",
    },
    {
        "name": "Ollama",
        "url_env": "OLLAMA_BASE_URL",
        "key_env": "OLLAMA_API_KEY",
        "model_env": "OLLAMA_MODEL",
        "default_url": "http://localhost:11434/v1",
        "default_key": "dummy",
    }
]

def _enabled() -> bool:
    for p in FALLBACK_PROVIDERS:
        key = os.environ.get(p["key_env"], p.get("default_key"))
        model = os.environ.get(p["model_env"], p.get("default_model"))
        if key and model:
            return True
    return False


def _call(system: str, user: str) -> Optional[str]:
    """Single OpenAI-compatible chat call. Returns assistant text, or None on any
    failure (disabled model, network/parse/proxy error) so callers fall back
    deterministically. SSE and single-object responses are both handled.
    Tries providers in sequence (fallback chain)."""
    if not _enabled():
        return None
        
    for p in FALLBACK_PROVIDERS:
        key = os.environ.get(p["key_env"], p.get("default_key"))
        model = os.environ.get(p["model_env"], p.get("default_model"))
        base = os.environ.get(p["url_env"], p.get("default_url", DEFAULT_BASE_URL)).rstrip("/")
        
        if not key or not model:
            continue
            
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            result = _extract_content(raw)
            if result:
                return result
        except Exception:
            continue
            
    return None


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
