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


def _enabled() -> bool:
    return bool(os.environ.get("FRIDAY_LLM_API_KEY") and os.environ.get("FRIDAY_LLM_MODEL"))


def _call(system: str, user: str) -> Optional[str]:
    """Single OpenAI-compatible chat call. Returns assistant text, or None on any
    failure (disabled model, network/parse/proxy error) so callers fall back
    deterministically. SSE and single-object responses are both handled."""
    if not _enabled():
        return None
    base = os.environ.get("FRIDAY_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ["FRIDAY_LLM_MODEL"]
    api_key = os.environ["FRIDAY_LLM_API_KEY"]
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
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        return _extract_content(raw)
    except Exception:
        return None


def summarize(readme_text: str) -> Optional[str]:
    """Return an LLM-generated summary, or None if disabled or on any error."""
    return _call(
        _SYSTEM_PROMPT,
        _USER_TEMPLATE.format(readme=readme_text[:12000]),
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
            return obj["choices"][0]["message"]["content"].strip()
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
