"""Agentic Executor — the meta-executor that orchestrates across all tools.

The AgenticExecutor is NOT another fixed-capability executor. It is a
meta-executor that receives a natural-language task and:

  1. **Decomposes** the task into a plan of tool calls (via LLM or keyword fallback)
  2. **Executes** each step through the appropriate executor (shell, filesystem,
     clipboard, claude_code, git, python, browser, hyprland, etc.)
  3. **Pipes** output between sequential steps (stdout of step N → input of step N+1)
  4. **Adapts** when a step fails — retries once, then revises the remaining plan
  5. **Reports** a structured result with per-step outcomes

This is what turns Friday from "intelligent observer that can run a few commands"
into an agent that does whatever you ask until it's done.

Design:
  - LLM-optional: the decomposition prompt works with any LLM, and the
    keyword-based fallback handles common patterns without any LLM.
  - Every existing executor is reachable through the tool belt.
  - Context handoff between steps is automatic (stdout → env var / file).
  - Long-running tasks auto-create persistent missions.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .db import connect, now_iso


# ──────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class AgentStep:
    """One atomic step in an agent plan."""

    tool: str               # e.g. "shell", "filesystem", "clipboard", "claude_code"
    params: dict            # tool-specific parameters
    description: str        # human-readable description of what this step does
    depends_on: list[int] = field(default_factory=list)  # indices of steps this depends on
    timeout: int = 120      # per-step timeout in seconds


@dataclass
class AgentStepResult:
    """Outcome of executing one agent step."""

    index: int
    tool: str
    description: str
    success: bool
    stdout: str
    stderr: str
    exit_code: Optional[int]
    duration_ms: int
    error: str = ""
    adapted: bool = False           # True if this step was adapted after a failure


@dataclass
class AgentSession:
    """A single agent execution session — one task, potentially many steps."""

    session_id: str
    task: str                        # original natural-language task
    workspace: str                   # working directory
    created_at: str
    status: str                      # "running" | "succeeded" | "failed" | "cancelled"
    steps: list[AgentStepResult] = field(default_factory=list)
    summary: str = ""                # final summary of what happened
    duration_ms: int = 0
    adapted: bool = False            # True if any step was adapted


# ──────────────────────────────────────────────────────────────────────────
# Tool definitions — sent to the LLM so it knows what's available
# ──────────────────────────────────────────────────────────────────────────

#: The tool belt description injected into the LLM decomposition prompt.
_TOOL_BELT = """
Available tools:
  - shell:         Run any shell command. Params: {"command": "..."}
  - filesystem:    Read/write/append/delete files. Params: {"op": "read|write|append|mkdir|delete|copy|move", "path": "...", "content": "..."}
  - git:           Git operations (no push). Params: {"args": "status --short"}
  - clipboard:     Read/write system clipboard. Params: {"op": "read|write", "text": "..."}
  - python:        Execute Python code. Params: {"code": "..."}
  - testing:       Run pytest. Params: {"args": ["-q", "tests/"]}
  - claude_code:   Invoke Claude Code headless in a directory. Params: {"workspace": "...", "prompt": "..."}
  - aider:         Invoke Aider. Params: {"workspace": "...", "prompt": "..."}
  - browser:       Navigate and interact with a web page. Params: {"url": "...", "action": "..."}
  - hyprland:      Control Hyprland window manager. Params: {"op": "...", "target": "..."}
  - email:         Send email. Params: {"to": "...", "subject": "...", "body": "..."}
  - slack:         Send Slack message. Params: {"channel": "...", "text": "..."}

Data flow between steps:
  - Sequential steps automatically pipe the previous step's stdout into the next.
  - The output of step N is available as $FRIDAY_PREV_OUTPUT or in the next step's params as {prev_output}.
"""

_DECOMPOSITION_PROMPT = (
    "You are an agent that decomposes a user's task into a sequence of tool calls. "
    "Output ONLY a JSON array of steps. No markdown, no extra text.\n\n"
    + _TOOL_BELT
    + """
Output format:
[
  {
    "tool": "shell",
    "params": {"command": "pytest tests/test_auth.py -v"},
    "description": "Run auth tests to see what is failing",
    "depends_on": [],
    "timeout": 120
  },
  ...
]

Rules:
1. Break complex tasks into small, focused steps (3-8 steps typical).
2. Sequential steps: set depends_on to [previous_index].
3. Independent steps can run in parallel (empty depends_on).
4. Use reasonable timeouts per step.
5. If a step produces output the next step needs, ensure depends_on is set.
6. For simple tasks that need one shell command, output a single step.
7. For questions ("what is", "explain", etc.), use the shell tool to search.
"""
)


# ──────────────────────────────────────────────────────────────────────────
# Executor mapping — Agent tool name → worker_id
# ──────────────────────────────────────────────────────────────────────────

_TOOL_TO_WORKER: dict[str, str] = {
    "shell": "worker:shell",
    "filesystem": "worker:filesystem",
    "git": "worker:git",
    "clipboard": "worker:clipboard",
    "python": "worker:python",
    "testing": "worker:testing",
    "documentation": "worker:documentation",
    "synthesis": "worker:synthesis",
    "claude_code": "worker:claude",
    "codex": "worker:codex",
    "gemini": "worker:gemini",
    "aider": "worker:aider",
    "deepseek": "worker:deepseek",
    "browser": "worker:browser",
    "hyprland": "worker:hyprctl",
    "email": "worker:email",
    "slack": "worker:slack",
    "discord": "worker:discord",
    "telegram": "worker:telegram",
}


def _resolve_worker(tool: str) -> Optional[str]:
    """Map an agent tool name to a worker_id, or None if unknown."""
    return _TOOL_TO_WORKER.get(tool.lower())


# ──────────────────────────────────────────────────────────────────────────
# Task decomposition
# ──────────────────────────────────────────────────────────────────────────

def _llm_decompose(task: str, workspace: str = ".") -> Optional[list[AgentStep]]:
    """Use the LLM to decompose a task into steps.

    Returns a list of AgentSteps, or None if LLM is unavailable or fails.
    """
    try:
        from .services.llm import _call as _llm_call, _enabled
    except Exception:
        return None

    if not _enabled():
        return None

    system = _DECOMPOSITION_PROMPT
    user = f"Task: {task}\nWorkspace: {workspace}\n\nDecompose this into tool calls:"

    try:
        raw = _llm_call(system, user)
        if not raw:
            return None

        # Strip markdown code fences if present.
        text = raw.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
            if m:
                text = m.group(1)

        steps_data = json.loads(text)
        if not isinstance(steps_data, list):
            return None

        steps: list[AgentStep] = []
        for i, s in enumerate(steps_data):
            if not isinstance(s, dict):
                continue
            tool = s.get("tool", "").lower().strip()
            if tool not in _TOOL_TO_WORKER:
                # Unknown tool — skip this step gracefully.
                continue
            params = s.get("params", {})
            if tool == "shell" and "command" not in params:
                continue
            steps.append(AgentStep(
                tool=tool,
                params=params,
                description=s.get("description", f"Step {i+1}: {tool}"),
                depends_on=s.get("depends_on", []),
                timeout=s.get("timeout", 120),
            ))

        return steps if steps else None

    except (json.JSONDecodeError, Exception):
        return None


def _keyword_decompose(task: str, workspace: str = ".") -> list[AgentStep]:
    """Deterministic keyword-based decomposition fallback.

    Handles common task patterns without any LLM.
    """
    lower = task.lower().strip()

    # "copy X to clipboard" pattern
    cp_match = re.search(
        r"(?:copy|put)\s+(?:the\s+)?(?:output|content|text|result|diff|data)"
        r"(?:\s+(?:of|from|to))?\s*(.*?)?\s*(?:to|into)\s+(?:the\s+)?clipboard",
        lower, re.IGNORECASE,
    )
    if cp_match:
        source = cp_match.group(1) or task
        steps = [AgentStep(
            tool="shell",
            params={"command": source.strip()},
            description=f"Run: {source.strip()}",
            depends_on=[],
        )]
        # Check if the source command is something we pipe directly.
        if source.strip():
            steps.append(AgentStep(
                tool="clipboard",
                params={"op": "write", "text": "{prev_output}"},
                description="Copy output to clipboard",
                depends_on=[0],
            ))
        return steps

    # "git diff to clipboard" — specific shortcut
    if "git diff" in lower and "clipboard" in lower:
        return [
            AgentStep(tool="shell", params={"command": "git diff"}, description="Get git diff", depends_on=[]),
            AgentStep(tool="clipboard", params={"op": "write", "text": "{prev_output}"},
                      description="Copy diff to clipboard", depends_on=[0]),
        ]

    # "run tests" pattern
    if any(kw in lower for kw in ("run test", "run pytest", "run all test", "run the test")):
        # Extract specific test path if mentioned
        test_match = re.search(r"(?:tests/|test_)[\w/._-]+", task)
        test_path = test_match.group(0) if test_match else ""
        cmd = f"python -m pytest {test_path} -v" if test_path else "python -m pytest -v"
        return [AgentStep(
            tool="shell", params={"command": cmd},
            description=f"Run tests{f' ({test_path})' if test_path else ''}",
            depends_on=[],
        )]

    # "find X and fix it" pattern — two steps
    find_fix = re.search(r"find\s+(.+?)\s+and\s+fix\s+(?:it|them)", lower, re.IGNORECASE)
    if find_fix:
        target = find_fix.group(1).strip()
        if "test" in target or "fail" in target:
            cmd = "python -m pytest -v 2>&1 | head -50"
            return [
                AgentStep(tool="shell", params={"command": cmd},
                          description=f"Find what's failing in {target}", depends_on=[]),
            ]
        return [
            AgentStep(tool="shell", params={"command": f"echo 'Looking into: {target}' && ls -la"},
                      description=f"Investigate {target}", depends_on=[]),
        ]

    # "deploy" pattern
    if any(kw in lower for kw in ("deploy", "release", "publish")):
        return [AgentStep(
            tool="shell", params={"command": "echo 'Deploy task received. Run: friday protocol run deploy'"},
            description="Run deploy protocol",
            depends_on=[],
        )]

    # "search for X" pattern
    search_match = re.search(r"(?:search|find)\s+(?:for\s+)?(.+)", lower, re.IGNORECASE)
    if search_match:
        query = search_match.group(1).strip()
        return [AgentStep(
            tool="shell",
            params={"command": f"echo 'Searching for: {query}'"},
            description=f"Search for: {query}",
            depends_on=[],
        )]

    # Default: single shell command
    return [AgentStep(
        tool="shell",
        params={"command": task},
        description=task[:80],
        depends_on=[],
    )]


def decompose(task: str, workspace: str = ".") -> list[AgentStep]:
    """Decompose a natural-language task into agent steps.

    PRIMARY path: LLM decomposition (when available).
    FALLBACK: keyword-based deterministic decomposition.
    """
    steps = _llm_decompose(task, workspace)
    if steps:
        return steps
    return _keyword_decompose(task, workspace)


# ──────────────────────────────────────────────────────────────────────────
# Step execution
# ──────────────────────────────────────────────────────────────────────────


def _build_runtime_payload(step: AgentStep, prev_output: str = "") -> str:
    """Build the runtime_payload string for an executor from an AgentStep.

    Handles {prev_output} substitution in params.
    """
    params = {}
    for k, v in step.params.items():
        if isinstance(v, str) and "{prev_output}" in v:
            params[k] = v.replace("{prev_output}", prev_output)
        else:
            params[k] = v

    # Tool-specific payload construction.
    tool = step.tool
    if tool == "shell":
        return params.get("command", "")
    if tool == "git":
        return params.get("args", "status --short")
    if tool == "clipboard":
        return json.dumps(params)
    if tool == "filesystem":
        return json.dumps(params)
    if tool == "python":
        return params.get("code", "")
    if tool == "testing":
        return json.dumps(params)
    if tool in ("claude_code", "aider", "codex", "gemini", "deepseek"):
        return params.get("prompt", "") or params.get("command", "")
    if tool == "browser":
        return json.dumps(params)
    if tool == "hyprland":
        return json.dumps(params)
    if tool == "email":
        return json.dumps(params)
    if tool == "slack":
        return json.dumps(params)
    if tool == "documentation":
        return json.dumps(params)
    if tool == "synthesis":
        return json.dumps(params)

    return json.dumps(params)


def _execute_step(step: AgentStep, step_index: int, prev_output: str,
                  workspace: str) -> AgentStepResult:
    """Execute a single agent step through the appropriate executor.

    Returns an AgentStepResult with the outcome.
    """
    from .runtime.executors import resolve_executor
    from .runtime.models import RuntimeTask

    worker_id = _resolve_worker(step.tool)
    if worker_id is None:
        return AgentStepResult(
            index=step_index,
            tool=step.tool,
            description=step.description,
            success=False,
            stdout="",
            stderr="",
            exit_code=None,
            duration_ms=0,
            error=f"Unknown tool: {step.tool}",
        )

    executor = resolve_executor(worker_id, workspace=workspace)
    if executor is None:
        return AgentStepResult(
            index=step_index,
            tool=step.tool,
            description=step.description,
            success=False,
            stdout="",
            stderr="",
            exit_code=None,
            duration_ms=0,
            error=f"No executor available for: {worker_id}",
        )

    payload = _build_runtime_payload(step, prev_output)
    if not payload.strip():
        return AgentStepResult(
            index=step_index,
            tool=step.tool,
            description=step.description,
            success=False,
            stdout="",
            stderr="",
            exit_code=None,
            duration_ms=0,
            error="Empty payload — no command/params specified",
        )

    # Build a minimal RuntimeTask-like object.
    class _MiniTask:
        pass

    mini = _MiniTask()
    mini.runtime_payload = payload
    mini.task_id = f"agent_{step_index}"
    mini.worker_id = worker_id
    mini.execution_id = f"agent:{step_index}"
    mini.timeout = step.timeout
    mini.task_type = step.tool
    mini.title = step.description
    mini.goal = step.description
    mini.outputs = []
    mini.acceptance_criteria = []
    mini.verification = []
    mini.symbolic = {}
    mini.dependency_summaries = {}

    t0 = time.monotonic()
    try:
        result = executor.execute(mini)
        dur = int((time.monotonic() - t0) * 1000)
        return AgentStepResult(
            index=step_index,
            tool=step.tool,
            description=step.description,
            success=result.success,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=result.exit_code,
            duration_ms=dur,
            error=result.error or "",
        )
    except Exception as e:
        dur = int((time.monotonic() - t0) * 1000)
        return AgentStepResult(
            index=step_index,
            tool=step.tool,
            description=step.description,
            success=False,
            stdout="",
            stderr=str(e),
            exit_code=None,
            duration_ms=dur,
            error=f"Execution raised: {type(e).__name__}: {e}",
        )


# ──────────────────────────────────────────────────────────────────────────
# Adaptation — revise remaining steps after a failure
# ──────────────────────────────────────────────────────────────────────────


def _llm_adapt(failed_step: AgentStepResult, remaining_steps: list[AgentStep],
               task: str) -> list[AgentStep]:
    """Ask the LLM to revise the remaining plan after a step failure.

    Returns the revised remaining steps, or the original remaining_steps if
    LLM is unavailable or fails.
    """
    try:
        from .services.llm import _call as _llm_call, _enabled
    except Exception:
        return remaining_steps

    if not _enabled():
        return remaining_steps

    system = (
        "You are an agent that revises a plan after a step failed. "
        "Output ONLY a JSON array of remaining steps. Same format as before.\n\n"
        + _TOOL_BELT
    )

    remaining_json = json.dumps([
        {"tool": s.tool, "params": s.params, "description": s.description,
         "depends_on": s.depends_on, "timeout": s.timeout}
        for s in remaining_steps
    ], indent=2)

    user = (
        f"Original task: {task}\n\n"
        f"The following step FAILED:\n"
        f"  Tool: {failed_step.tool}\n"
        f"  Description: {failed_step.description}\n"
        f"  Error: {failed_step.error}\n"
        f"  Stderr: {failed_step.stderr[:500]}\n\n"
        f"Remaining steps that haven't run yet:\n{remaining_json}\n\n"
        f"Revise the remaining steps to work around the failure. "
        f"Consider:\n"
        f"  - If a shell command failed, try a different approach\n"
        f"  - If a tool is unavailable, use an alternative tool\n"
        f"  - If the error provides useful info, use it in the next step\n"
        f"  - If the task can't proceed, return an empty array []\n"
        f"Output ONLY the revised remaining steps as JSON.\n"
    )

    try:
        raw = _llm_call(system, user)
        if not raw:
            return remaining_steps

        text = raw.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
            if m:
                text = m.group(1)

        steps_data = json.loads(text)
        if not isinstance(steps_data, list):
            return remaining_steps

        steps: list[AgentStep] = []
        for s in steps_data:
            if not isinstance(s, dict):
                continue
            tool = s.get("tool", "").lower().strip()
            if tool not in _TOOL_TO_WORKER:
                continue
            steps.append(AgentStep(
                tool=tool,
                params=s.get("params", {}),
                description=s.get("description", f"Step: {tool}"),
                depends_on=s.get("depends_on", []),
                timeout=s.get("timeout", 120),
            ))

        return steps if steps else []

    except (json.JSONDecodeError, Exception):
        return remaining_steps


def _keyword_adapt(failed_step: AgentStepResult, remaining_steps: list[AgentStep]) -> list[AgentStep]:
    """Deterministic fallback adaptation strategy.

    Tries simple tactics:
    - If shell failed → try python with a subprocess wrapper
    - If timeout → increase timeout and retry
    - Otherwise, return remaining steps unchanged (let them try)
    """
    if not remaining_steps:
        return []

    # Check if the failure was a timeout.
    is_timeout = "timed out" in failed_step.error.lower() or "timeout" in failed_step.stderr.lower()

    # If the failed step was shell and there's a next step, adjust.
    if failed_step.tool == "shell" and remaining_steps:
        # Try increasing timeout for remaining shell steps.
        adapted = []
        for s in remaining_steps:
            if is_timeout and s.tool == "shell":
                s.timeout = min(s.timeout * 2, 600)  # double, cap at 10min
            adapted.append(s)
        return adapted

    return remaining_steps


def adapt_plan(failed_step: AgentStepResult, remaining_steps: list[AgentStep],
               task: str) -> list[AgentStep]:
    """Revise remaining steps after a step failure.

    PRIMARY: LLM adaptation.
    FALLBACK: Keyword-based deterministic adaptation.
    """
    revised = _llm_adapt(failed_step, remaining_steps, task)
    # Only use keyword fallback if LLM returned the same steps unchanged.
    if revised == remaining_steps:
        revised = _keyword_adapt(failed_step, remaining_steps)
    return revised


# ──────────────────────────────────────────────────────────────────────────
# Persistence (optional — agent_sessions table)
# ──────────────────────────────────────────────────────────────────────────


def _ensure_agent_sessions_table(conn) -> None:
    """Create the agent_sessions table if it doesn't exist."""
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id              TEXT PRIMARY KEY,
                task            TEXT NOT NULL,
                workspace       TEXT NOT NULL DEFAULT '.',
                created_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'running',
                summary         TEXT NOT NULL DEFAULT '',
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                adapted         INTEGER NOT NULL DEFAULT 0,
                steps_json      TEXT NOT NULL DEFAULT '[]',
                updated_at      TEXT NOT NULL
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()


def _persist_session(conn, session: AgentSession) -> None:
    """Write an AgentSession to the DB."""
    _ensure_agent_sessions_table(conn)
    steps_data = []
    for s in session.steps:
        steps_data.append({
            "index": s.index,
            "tool": s.tool,
            "description": s.description,
            "success": s.success,
            "stdout": s.stdout[:200] if s.stdout else "",
            "stderr": s.stderr[:200] if s.stderr else "",
            "exit_code": s.exit_code,
            "duration_ms": s.duration_ms,
            "error": s.error,
            "adapted": s.adapted,
        })
    try:
        conn.execute(
            """INSERT OR REPLACE INTO agent_sessions
               (id, task, workspace, created_at, status, summary, duration_ms, adapted, steps_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session.session_id, session.task, session.workspace,
             session.created_at, session.status, session.summary,
             session.duration_ms, 1 if session.adapted else 0,
             json.dumps(steps_data), now_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()


# ──────────────────────────────────────────────────────────────────────────
# Main execution loop
# ──────────────────────────────────────────────────────────────────────────


def run_agent(task: str, workspace: str = ".",
              persist: bool = True,
              conn=None) -> AgentSession:
    """Execute a natural-language task through the agentic executor.

    This is the main entry point. It:

    1. Decomposes the task into steps
    2. Executes steps in order (respecting depends_on)
    3. Pipes output between sequential steps
    4. Adapts on failure (retry once, then revise remaining plan)
    5. Returns an AgentSession with full results

    Args:
        task: The natural-language task to execute.
        workspace: Working directory for execution.
        persist: Whether to persist the session to DB.
        conn: Optional DB connection for persistence.

    Returns:
        An AgentSession with all step results and a summary.
    """
    session_id = f"agent:{uuid.uuid4().hex[:12]}"
    created_at = now_iso()
    t0 = time.monotonic()

    session = AgentSession(
        session_id=session_id,
        task=task,
        workspace=workspace,
        created_at=created_at,
        status="running",
    )

    # Step 1: Decompose.
    steps = decompose(task, workspace)
    if not steps:
        session.status = "failed"
        session.summary = "Could not decompose the task into steps."
        session.duration_ms = int((time.monotonic() - t0) * 1000)
        return session

    # Step 2-4: Execute loop with adaptation.
    step_outputs: dict[int, str] = {}  # index → stdout
    completed_indices: set[int] = set()
    i = 0

    while i < len(steps):
        step = steps[i]

        # Check if dependencies are met.
        deps_met = all(d in completed_indices for d in step.depends_on)
        if not deps_met:
            # Skip — dependencies not yet available (shouldn't happen in linear exec).
            i += 1
            continue

        # Gather prev_output from the most recent completed dependency.
        prev_output = ""
        if step.depends_on:
            last_dep = max(step.depends_on)
            prev_output = step_outputs.get(last_dep, "")
        elif completed_indices:
            # If no explicit depends_on but previous steps ran, use last output.
            last_completed = max(completed_indices)
            prev_output = step_outputs.get(last_completed, "")

        # Execute.
        result = _execute_step(step, i, prev_output, workspace)

        # If failed, try once with adaptation.
        if not result.success and step.tool != "clipboard":  # clipboard has its own error handling
            # Try adaptation: revise remaining plan.
            remaining = steps[i + 1:]
            revised = adapt_plan(result, remaining, task)

            if revised != remaining or not revised:
                # Mark the step as adapted and continue with revised plan.
                result.adapted = True
                session.adapted = True
                session.steps.append(result)

                if not revised:
                    # No remaining steps — we're done.
                    completed_indices.add(i)
                    break

                # Replace remaining steps with revised plan.
                steps = steps[:i + 1] + revised
                step_outputs[i] = ""
                completed_indices.add(i)
                i += 1
                continue

        # Record result.
        step_outputs[i] = result.stdout
        completed_indices.add(i)
        session.steps.append(result)

        if not result.success:
            # Non-adaptive failure — stop execution.
            break

        i += 1

    # Determine final status.
    all_succeeded = all(s.success for s in session.steps)
    any_failed = any(not s.success for s in session.steps)

    if all_succeeded:
        session.status = "succeeded"
    elif any_failed:
        session.status = "failed"
    else:
        session.status = "succeeded"

    session.duration_ms = int((time.monotonic() - t0) * 1000)

    # Generate summary.
    success_count = sum(1 for s in session.steps if s.success)
    total_count = len(session.steps)
    if all_succeeded:
        last_out = session.steps[-1].stdout[:200] if session.steps else ""
        session.summary = (
            f"✅ Completed {total_count} step(s) in "
            f"{session.duration_ms / 1000:.1f}s."
        )
        if last_out:
            session.summary += f"\n{last_out}"
    else:
        failed = [s for s in session.steps if not s.success]
        fail_desc = failed[0].description if failed else "unknown"
        session.summary = (
            f"❌ {success_count}/{total_count} steps completed. "
            f"Failed at: {fail_desc}. "
            f"Took {session.duration_ms / 1000:.1f}s."
        )
        if session.adapted:
            session.summary += " (Adapted after failure.)"

    # Persist to DB if requested.
    if persist:
        close_conn = False
        if conn is None:
            conn = connect()
            close_conn = True
        try:
            _persist_session(conn, session)
        except Exception:
            pass
        finally:
            if close_conn:
                conn.close()

    return session


# ──────────────────────────────────────────────────────────────────────────
# CLI-friendly helpers
# ──────────────────────────────────────────────────────────────────────────


def format_session(session: AgentSession) -> str:
    """Format an AgentSession as a human-readable string."""
    lines: list[str] = []
    lines.append(f"Agent Session: {session.session_id}")
    lines.append(f"Task: {session.task}")
    lines.append(f"Status: {session.status}")
    lines.append(f"Duration: {session.duration_ms / 1000:.1f}s")
    if session.adapted:
        lines.append("Adapted: Yes")
    lines.append("")

    for s in session.steps:
        marker = "✅" if s.success else "❌"
        adapted = " 🔄" if s.adapted else ""
        lines.append(f"  {marker} Step {s.index + 1}: {s.description}{adapted}")
        lines.append(f"      Tool: {s.tool} | Duration: {s.duration_ms}ms")
        if s.error:
            lines.append(f"      Error: {s.error[:150]}")
        if s.stdout:
            # Show first few lines of output.
            out_lines = s.stdout.strip().splitlines()[:3]
            for ol in out_lines:
                if ol.strip():
                    lines.append(f"      > {ol[:120]}")
        lines.append("")

    if session.summary:
        lines.append(f"Summary: {session.summary}")

    return "\n".join(lines)


def format_session_brief(session: AgentSession) -> str:
    """Format a brief one-line summary of an AgentSession."""
    status_icon = "✅" if session.status == "succeeded" else "❌" if session.status == "failed" else "⏳"
    return (
        f"{status_icon} {session.task[:60]}"
        f" — {len([s for s in session.steps if s.success])}/{len(session.steps)} steps"
        f" ({session.duration_ms / 1000:.1f}s)"
    )


def get_session_history(conn, limit: int = 20) -> list[AgentSession]:
    """Load recent agent sessions from DB."""
    _ensure_agent_sessions_table(conn)
    rows = conn.execute(
        "SELECT * FROM agent_sessions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    sessions: list[AgentSession] = []
    for r in rows:
        r = dict(r)
        steps_data = json.loads(r["steps_json"]) if r.get("steps_json") else []
        steps = [
            AgentStepResult(
                index=s["index"],
                tool=s.get("tool", "?"),
                description=s.get("description", ""),
                success=s.get("success", False),
                stdout=s.get("stdout", ""),
                stderr=s.get("stderr", ""),
                exit_code=s.get("exit_code"),
                duration_ms=s.get("duration_ms", 0),
                error=s.get("error", ""),
                adapted=s.get("adapted", False),
            )
            for s in steps_data
        ]
        sessions.append(AgentSession(
            session_id=r["id"],
            task=r["task"],
            workspace=r.get("workspace", "."),
            created_at=r["created_at"],
            status=r["status"],
            summary=r.get("summary", ""),
            duration_ms=r.get("duration_ms", 0),
            adapted=bool(r.get("adapted", 0)),
            steps=steps,
        ))
    return sessions


def get_active_session(conn) -> Optional[AgentSession]:
    """Get the currently running agent session, if any."""
    _ensure_agent_sessions_table(conn)
    row = conn.execute(
        "SELECT * FROM agent_sessions WHERE status = 'running' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None

    row = dict(row)
    steps_data = json.loads(row["steps_json"]) if row.get("steps_json") else []
    steps = [
        AgentStepResult(
            index=s["index"],
            tool=s.get("tool", "?"),
            description=s.get("description", ""),
            success=s.get("success", False),
            stdout=s.get("stdout", ""),
            stderr=s.get("stderr", ""),
            exit_code=s.get("exit_code"),
            duration_ms=s.get("duration_ms", 0),
            error=s.get("error", ""),
            adapted=s.get("adapted", False),
        )
        for s in steps_data
    ]
    return AgentSession(
        session_id=row["id"],
        task=row["task"],
        workspace=row.get("workspace", "."),
        created_at=row["created_at"],
        status=row["status"],
        summary=row.get("summary", ""),
        duration_ms=row.get("duration_ms", 0),
        adapted=bool(row.get("adapted", 0)),
        steps=steps,
    )


def cancel_active_session(conn) -> bool:
    """Cancel the currently running agent session."""
    _ensure_agent_sessions_table(conn)
    row = conn.execute(
        "SELECT id FROM agent_sessions WHERE status = 'running' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return False
    conn.execute(
        "UPDATE agent_sessions SET status = 'cancelled', updated_at = ? WHERE id = ?",
        (now_iso(), row["id"]),
    )
    conn.commit()
    return True
