"""LLM client for the ONE NLU point (Wave 13a).

Thin, pure-stdlib client for the local 9router proxy
(``localhost:20128/v1``, configurable model) with the known SSE-trailer
quirk handled at the boundary. ``parse_utterance`` asks the LLM for a
canonical action JSON and returns it — or ``None`` when the LLM is
unavailable (the deterministic fallback then takes over).

Never raises: every network/parse failure degrades to ``None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("friday_v6.nlu.llm")

#: Default 9router proxy (proven in this workspace).
DEFAULT_BASE_URL = "http://localhost:20128/v1"
# The brain's default model. Bare `deepseek-v4` is NOT a valid model ID
# on this proxy (OpenRouter 400 "not a valid model ID") — every classify
# silently fell back to rules until this was fixed. `oc/deepseek-v4-flash-free`
# is the operator's chosen route (fast, 2s); `free` is the same class of
# fast alias. Override with FRIDAY_V4_LLM_MODEL to use another route
# (e.g. nvidia/deepseek-ai/deepseek-v4-pro).
DEFAULT_MODEL = "oc/deepseek-v4-flash-free"


def config_from_env() -> dict:
    """LLM config from env (``FRIDAY_V4_LLM`` overrides the base URL).

    ``FRIDAY_V4_LLM_URL`` (default 9router proxy), ``FRIDAY_V4_LLM_MODEL``,
    ``FRIDAY_V4_LLM_KEY`` (optional — local proxies usually don't need one).
    """
    return {
        "base_url": os.environ.get("FRIDAY_V4_LLM_URL", DEFAULT_BASE_URL),
        "model": os.environ.get("FRIDAY_V4_LLM_MODEL", DEFAULT_MODEL),
        "api_key": os.environ.get("FRIDAY_V4_LLM_KEY", ""),
    }


class LLMClient:
    """OpenAI-compatible chat client over ``urllib`` (pure stdlib)."""

    def __init__(self, base_url: Optional[str] = None,
                 model: Optional[str] = None,
                 api_key: str = "",
                 timeout: float = 30.0) -> None:
        cfg = config_from_env()
        self.base_url = (base_url or cfg["base_url"]).rstrip("/")
        self.model = model or cfg["model"]
        self.api_key = api_key or cfg["api_key"]
        self.timeout = timeout

    @property
    def available(self) -> bool:
        """Best-effort liveness check (does not wait long)."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/models", method="GET")
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def chat(self, messages: list[dict], *, max_tokens: int = 400) -> Optional[str]:
        """One chat completion — returns the assistant text or None."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            method="POST", headers={"Content-Type": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.debug(f"llm chat failed: {exc}")
            return None
        # 9router SSE-trailer quirk: the response may arrive as an SSE
        # stream with a final `data:` chunk even for non-stream requests.
        text = _extract_text(raw)
        if text is None:
            logger.debug("llm returned no parseable text")
        return text

    def parse_utterance(self, utterance: str) -> Optional[dict]:
        """Ask the LLM for a canonical action JSON — or None.

        The prompt is the single command language: the LLM returns
        exactly the intents/action_types/entities the resolver consumes.
        """
        system = (
            "You are Friday's NLU. Parse the user's utterance into a "
            "canonical action JSON with EXACTLY these fields:\n"
            '{"intent": "execute|ask|plan|desktop|greeting|help|research|skill|accept|deny|security|memory|style|ide|unknown", '
            '"action_type": "shell|git|file|python|testing|claude|null", '
            '"command": "", "target": "", "goal": "", '
            '"entities": [{"type": "path|file|repo|app|time|person", "value": ""}], '
            '"needs_clarification": false, "clarification": ""}\n'
            "Rules: intent 'execute' for run/test/git/file/python actions; "
            "a COMPLEX agentic goal with no single concrete command "
            "('figure out why the build fails and fix it', 'debug the "
            "memory leak', 'fix the failing test') uses action_type "
            "'claude' with command = the full goal text; "
            "'ask' for questions; 'plan' for goals/missions ('ship X by "
            "Friday'); 'desktop' for window/workspace control; 'research' "
            "for analyze/correlate/briefing; 'skill' for demonstration "
            "capture ('watch me do this', 'learn this', 'stop watching'); "
            "'accept' for approving a suggestion or permission ask ('yes', "
            "'go ahead', 'run it'); 'deny' for declining a pending ask or "
            "redirecting an action ('no', 'do not run that', 'do it a "
            "different way', 'cancel that'); 'security' for scanning/auditing a project "
            "('scan my repo', 'check my dependencies', 'is my code "
            "secure') — target = path when named, else empty; 'memory' "
            "for storing/forgetting facts with consent ('remember that I "
            "prefer Rust', 'forget that') — target = 'remember' or "
            "'forget', goal = the fact statement; "
            "'ide' for diagnosing/analyzing code in the editor "
            "('what's wrong with auth.py', 'lint src/main.py', 'why "
            "won't this compile') — target = the file path; "
            "'style' for adaptive-identity tone direction ('be more "
            "casual', 'be more formal', 'less chatter', 'be yourself "
            "again') — target = the tone name "
            "('casual'/'formal'/'friendly'/'warm'/'close'/'neutral'/"
            "'brief'/'detailed'/'reset'), goal = the full request; "
            "'greeting'/'help' as obvious. "
            "Set needs_clarification=true when ambiguous. "
            "Respond with ONLY the JSON."
        )
        text = self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": utterance},
        ])
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Strip code fences / prose and retry once.
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                return None
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        return data


def _extract_text(raw: str) -> Optional[str]:
    """Pull the assistant text out of a (possibly SSE-shaped) response."""
    # 9router quirk: a plain completion may still carry an SSE trailer
    # (``{json}data: [DONE]``) after the JSON body — strip it first so
    # the normal parse wins and we never fall through to None.
    body = raw.rstrip()
    for marker in ("\ndata: [DONE]", "data: [DONE]"):
        if body.endswith(marker):
            body = body[: -len(marker)].rstrip()
            break
    try:
        # Normal JSON completion first.
        obj = json.loads(body)
        return obj["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    # SSE stream: lines "data: {json}" — the last data chunk wins.
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                last = chunk
    if last:
        try:
            obj = json.loads(last)
            return obj["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None
    return None


__all__ = ["LLMClient", "config_from_env", "DEFAULT_BASE_URL", "DEFAULT_MODEL"]
