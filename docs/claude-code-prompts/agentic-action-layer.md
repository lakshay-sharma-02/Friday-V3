# Agentic Action Layer — Unified "Understand + Act" Pipeline

## Intent
This is the single biggest missing piece. The fundamental architectural flaw.

Currently Friday has **two silos**:
```
ask() pipeline        → Q&A only. Anaphora = re-ask, not re-execute
execute() pipeline    → 22 fixed executors, no general "do whatever it takes"
```
No clipboard. No AI-to-AI handoff. No "open a terminal, run command, pipe output to claude, get work done, report back."

**The fix**: A single unified pipeline that takes ANY natural-language request, decides if it's a question or an action, and if it's an action — decomposes it into steps ACROSS executors, pipes data between them, persists until done, and reports the result in natural language.

This is what turns Friday from "intelligent observer that can run a few commands" into **"agent that does whatever you ask until it's done."**

## Architecture

### Phase 1: The General Agentic Executor

Create `src/friday/agent.py`. A single executor that has ACCESS to ALL other executors AND can decide which to use.

**What it is:**
Not another fixed-capability executor. A **meta-executor** that receives a natural-language task and orchestrates across all available tools.

**Tool belt (all existing + new):**
- `shell` — run any command, capture stdout/stderr/exit code
- `filesystem` — read/write/append/replace/mkdir/delete/copy/move
- `git` — git operations (except push)
- `browser` — navigate, click, type, read, screenshot
- `hyprland` — focus window, launch app, switch workspace, close window
- `clipboard` — **NEW**: read/write system clipboard (xclip/wl-clipboard/pbpaste)
- `claude_code` — invoke claude code headless in a specific directory with a prompt
- `aider` / `gemini` / `codex` — other AI assistants
- `ai_ask` — ask Friday's own Q&A pipeline and get structured answer back
- `protocol` — run a named protocol
- `python` — execute arbitrary Python
- `wait` — create a persistent watcher and wait for condition
- `search` — use Friday's code search
- `impact` — use change impact analysis
- `narrative` — get codebase story
- `mission` — create sub-mission

**How it works:**

1. **Receive task**: Natural language string + optional context (workspace, session ID)
2. **Decompose**: Use the LLM to break the task into a plan of tool calls
   - Each step is: `{tool: "shell", params: {command: "pytest"}, description: "run tests"}`
   - Steps can be sequential (pipe output from step 1 into step 2) or parallel
   - The LLM outputs a structured JSON plan
3. **Execute steps**: Run each step through the appropriate executor
   - Sequential steps: feed output of step N as input to step N+1
   - Parallel steps: run in thread pool
   - Each step gets a timeout (default 120s, configurable)
   - Failed steps: re-try once, then adapt the plan
4. **Adapt**: If a step fails, feed the error back to the LLM to revise remaining plan
5. **Report**: Return a structured result: `{success, summary, steps: [{tool, status, output_summary, duration}]}`

### Phase 2: Unified Routing Layer

Currently `friday ask "deploy the app"` explains deploy. `friday do "deploy the app"` executes. `friday talk "deploy the app"` routes through the persona engine which may or may not execute.

**Fix**: Upgrade `IdentityEngine.process()` in `persona/engine.py` to be the SINGLE entry point for ALL interactions (CLI, Telegram, Slack, Discord, voice).

**New routing logic:**
```
Incoming text
    ↓
1. Is it a chitchat/greeting? → respond directly (existing)
2. Is it a simple Q&A? → ask() pipeline (existing)
3. Is it an action? → AgenticExecutor (NEW)
4. Unsure? → Ask clarifying question: "Do you want me to answer that or do it?"
```

**How to decide between Q&A and action:**
- **Fast path** (deterministic, always available): keyword match
  - "deploy", "run", "execute", "do", "make", "create", "fix", "build", "open", "close", "copy", "move", "delete", "install", "start", "stop", "restart" → likely action
  - "what", "why", "how", "who", "when", "where", "explain", "describe", "tell me" → likely Q&A
- **LLM path** (when LLM available): classify intent with the persona prompt
- **Ambiguous**: ask

**Key design:**
- The agent executor prompt includes the FULL list of available tools with their capabilities and limitations
- The persona engine stores conversation history so the agent can reference previous turns
- All channels (CLI, Telegram, Slack, Discord) go through the SAME routing path

### Phase 3: Clipboard Bridge (NEW Executor)

Create clipboard read/write capability. This is the CRITICAL enabler for "copy output, paste into claude code."

**Implementation in `src/friday/runtime/clipboard_executor.py`:**

```python
class ClipboardExecutor(Executor):
    worker_id = "worker:clipboard"

    def execute(self, task):
        op = payload.get("op")  # "read" | "write"
        if op == "read":
            text = subprocess.run(["wl-paste"], ...)  # or xclip, pbpaste
            return ExecutionResult(success=True, stdout=text)
        elif op == "write":
            text = payload.get("text", "")
            subprocess.run(["wl-copy"], input=text, ...)  # or xclip, pbcopy
            return ExecutionResult(success=True)
```

**Deterministic fallback (no clipboard tools):**
- Read: read from a temp file `~/.friday/clipboard_bridge.txt`
- Write: write to the same temp file
- Note in output: "⚠ No system clipboard tool found — used file bridge"

### Phase 4: Context Handoff Between Steps & AI Tools

The core enabler for "run this command, give output to claude code."

**Data flow model:**
Each agent step produces an `AgentOutput`:
```python
@dataclass
class AgentOutput:
    stdout: str        # text output
    stderr: str        # error output
    files: list[str]   # files created/modified
    exit_code: int
    duration_ms: int
```

When step N+1 depends on step N:
- If the next step is `claude_code`, its prompt includes: "The previous step produced this output: {stdout}\n\nNow do: {next_step_description}"
- If the next step is `shell`, its command can reference previous output via environment variable `$FRIDAY_PREV_OUTPUT` or a temp file `~/.friday/agent_flow.txt`
- If the next step is `clipboard write`, the previous step's stdout is automatically piped in

**Claude Code handoff specifically:**
```python
# After a shell step produces output:
step_1_output = agent.steps[0].stdout

# Claude Code step uses that output as context:
step_2 = AgentStep(
    tool="claude_code",
    params={
        "workspace": "/path/to/project",
        "prompt": f"Previous command output: {step_1_output}\n\nNow fix the build error shown above."
    }
)
```

### Phase 5: Persistent Mission Integration

When the agent receives a task that will take multiple cycles (long builds, multi-step processes), it creates a **PersistentMission** (reuse `mission.py`):

```python
# Agent auto-creates a mission for long tasks
mission = MissionEngine(conn).create(
    goal="Fix the failing test in auth module",
    steps=[
        {"tool": "shell", "command": "pytest tests/test_auth.py", "description": "See what's failing"},
        {"tool": "claude_code", "workspace": ".", "prompt": "Fix the test failures shown above"},
        {"tool": "shell", "command": "pytest tests/test_auth.py", "description": "Verify the fix"},
    ]
)
```

The mission engine advances one step per daemon cycle, reports progress to ambient feed.

### Phase 6: CLI Entry Points

**`friday do <task>`** — the primary entry point (upgrade existing `cli_nl.py`):
```
friday do "copy the test output, paste to claude in the backend repo, fix the errors"
friday do "run the deploy protocol for staging then check if it's healthy"
friday do "find where we handle JWTs, explain the pattern to me"
```

**`friday agent`** — agent management:
```
friday agent status              → show current agent session / task
friday agent history             → past agent runs
friday agent cancel              → cancel current agent task
```

**`friday do` upgrade**:
- Currently `friday do` uses `classify_intent()` which maps to one handler
- New: `friday do` feeds into the AgenticExecutor directly
- The old keyword-based routing becomes the fast-path fallback
- The LLM-powered decomposition becomes the primary path

## Files to touch
- `src/friday/agent.py` (NEW) — AgenticExecutor, AgentStep, AgentOutput, decomposition, execution loop
- `src/friday/runtime/clipboard_executor.py` (NEW) — clipboard read/write
- `src/friday/persona/engine.py` — upgrade IdentityEngine.process() with Q&A vs action routing
- `src/friday/cli_nl.py` — upgrade `friday do` to feed into AgenticExecutor
- `src/friday/cli_agent.py` (NEW) — agent management CLI
- `src/friday/cli.py` — register `friday agent` command
- `src/friday/runtime/executors.py` — register ClipboardExecutor in resolve_executor() table
- `src/friday/mission.py` — add `import_from_agent_steps()` factory
- `src/friday/objective.py` — add `AGENT` objective type
- `src/friday/db.py` — add `agent_sessions` table (optional, for persistence)
- `tests/test_agent.py` (NEW)
- `tests/test_clipboard_executor.py` (NEW)
- `tests/test_persona_routing.py` (NEW or extend existing)

## Acceptance Criteria
1. `friday do "copy the current git diff to clipboard"` → runs `git diff`, writes output to clipboard (or file bridge)
2. `friday do "run tests, if they pass deploy to staging"` → runs pytest, checks exit code, runs deploy protocol
3. `friday do "find what's failing in auth tests and fix it"` → runs tests, captures failure, feeds into claude code, verifies fix
4. `friday ask "deploy the app"` → NEW: asks "do you want me to do that or explain it?" (ambiguous)
5. `friday do "deploy the app"` → runs the deploy protocol (unambiguous action)
6. `friday do "tell me what my architecture looks like"` → routed to Q&A pipeline (unambiguous question)
7. `friday agent status` → shows current task, progress, last step output
8. Agent decomposes complex tasks into sequential steps automatically
9. When a step fails, agent adapts the remaining plan (not just abort)
10. ClipboardExecutor works with xclip, wl-clipboard, and falls back to file bridge
11. All 22 existing executors are reachable through the AgenticExecutor
12. No existing tests break
