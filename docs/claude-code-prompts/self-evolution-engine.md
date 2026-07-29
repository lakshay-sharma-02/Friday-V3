# Self-Evolution Engine — Friday Upgrades Itself

## Intent
You asked: *"Can Friday, when asked 'make yourself capable of speaking', push its code to git, write voice code, test it, deploy it, and if it breaks, rollback?"*

**The answer is yes — and the meta-engine exists already.** But it's limited to generating tiny single-purpose workers with stdlib-only code. It can't add voice support, Discord integration, or website analytics — those need pip dependencies, config changes, new files across multiple directories, and sometimes hardware access.

What we need: a **self-evolution loop** that can add WHOLE NEW CAPABILITIES to Friday by writing real code, installing real dependencies, running real tests, and deploying with rollback safety.

This is the most "MCU FRIDAY" feature possible — the system that upgrades itself.

## What's Already Built (don't rebuild)

The meta-engine at `src/friday/meta/` already has:
- **`gap_analyzer.py`** — detects missing capabilities from runtime failures
- **`si_planner.py`** — generates worker code via LLM for simple gaps
- **`sandbox.py`** — isolated git worktree for testing new code
- **`verification.py`** — 3-stage verification gate (own tests + regression + evidence replay)
- **`deploy.py`** — human-approved merge with feature flags

What's MISSING: the ability to generate COMPLEX multi-file capabilities with dependencies, config changes, and existing-code modifications.

## What to Build

### Phase 1: Upgrade the Sandbox to Handle Real Capabilities

Currently `sandbox.py` only supports:
- Running tests inside the sandbox
- Applying patches via `git am` or `git apply`

**Upgrades needed:**

**1. Install dependencies inside the sandbox:**
```python
def install_deps(self, deps: list[str]) -> dict:
    """Install pip packages inside the sandbox.
    
    Args:
        deps: List of package names (e.g. ["edge-tts", "faster-whisper"])
    
    Returns: {success, output, failed_packages}
    """
```

The sandbox should run `pip install <pkg>` inside the sandbox checkout. If a package fails to install, report it and let the planner decide whether to continue or abort.

**2. Parse existing code for modification:**
```python
def read_file(self, relative_path: str) -> str:
    """Read a file from the sandbox checkout."""
    
def file_exists(self, relative_path: str) -> bool:
    """Check if a file exists in the sandbox."""
```

The planner needs to READ existing files (cli.py, daemon.py, pyproject.toml) to know how to modify them.

**3. Run a specific test file:**
```python
def test_file(self, test_path: str) -> dict:
    """Run a specific test file and return {passed, output, duration_ms}."""
```

Not just the full regression suite — targeted testing during development.

**4. Dry-run mode:**
```python
def dry_run(self, changes: list[dict]) -> dict:
    """Simulate changes without actually modifying the sandbox.
    
    Returns what WOULD change: files created, files modified, deps added.
    Useful for "what if I add voice support?" queries.
    """
```

**5. Rollback snapshot:**
```python
def snapshot(self) -> str:
    """Capture a git commit hash for rollback."""
    
def rollback(self, commit_hash: str) -> bool:
    """Revert sandbox to a previous snapshot."""
```

### Phase 2: Upgrade the Planner to Handle Multi-File Capabilities

Currently `si_planner.py` generates a single Python file implementing a Worker execute() contract. Upgrade it to generate WHOLE FEATURES.

**New prompt template (replace the current one):**

```python
CAPABILITY_SYSTEM_PROMPT = """
You are Friday's self-evolution engine. You upgrade Friday's capabilities.
You receive a capability request and produce a structured plan of changes.

CAPABILITY REQUEST: {request}

EXISTING CODEBASE STRUCTURE:
{codebase_map}

EXISTING DEPENDENCIES (pyproject.toml):
{pyproject}

You must output a JSON plan:

```json
{
  "capability_name": "voice_support",
  "description": "Add speech-to-text and text-to-speech to Friday",
  "new_files": [
    {
      "path": "src/friday/services/voice.py",
      "content": "..."
    }
  ],
  "modified_files": [
    {
      "path": "src/friday/cli.py",
      "content": "..." // FULL file content after modification
    }
  ],
  "dependencies": ["edge-tts", "faster-whisper"],
  "config_changes": {
    "env_vars": ["FRIDAY_VOICE_ENABLED", "FRIDAY_TTS_VOICE"],
    "defaults": {"FRIDAY_VOICE_ENABLED": "false"}
  },
  "test_files": [
    {
      "path": "tests/test_voice.py",
      "content": "..."
    }
  ],
  "rollback_risk": "low" | "medium" | "high",
  "verification_steps": [
    "python -m pytest tests/test_voice.py -x",
    "python -m pytest tests/ -x --tb=short"
  ]
}
```

RULES:
- Every modified file must include the COMPLETE file content, not just the diff
- New files must include __init__.py entries if added to a package
- Dependencies must be real pip packages with correct names
- Rollback_risk assessment: low = new files only, no existing code changes; medium = modifies existing files; high = changes core architecture or DB schema
- Verification steps must be runnable commands
- Keep new files under 500 lines unless absolutely necessary
- Follow existing code style (type hints, docstrings, existing patterns)
- For new CLI commands, follow the pattern in cli.py (argparse, cmd_* functions)
- For new executors, follow the pattern in runtime/executors.py
- For new services, follow the pattern in services/ (llm.py, email.py)
- New capabilities must be feature-flagged (disabled by default)
"""
```

**Key design changes from current planner:**
- Outputs MULTIPLE files, not one
- Can modify EXISTING files (cli.py, daemon.py, pyproject.toml)
- Includes dependency changes
- Includes config/env var changes
- Includes test files
- Has rollback risk assessment
- The plan is JSON, not raw Python

### Phase 3: Upgrade Deploy to Support Feature Flags

Currently `deploy.py` registers workers with a 'beta' feature flag. Upgrade for full capabilities:

**Feature flag table:**
```sql
CREATE TABLE capability_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,           -- "voice", "discord_bot", "web_analytics"
    description TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,  -- 0=disabled, 1=enabled
    installed INTEGER NOT NULL DEFAULT 0, -- 0=not installed, 1=installed
    deps_installed INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    last_used_at TEXT,
    rollback_commit TEXT                 -- commit hash to rollback to
);
```

**Capability lifecycle:**
```
Request → Plan → Sandbox → Verify → Stage (flag=0) → Enable (flag=1) → Live
                                                ↓
                                         Rollback ←→ Error
```

**Deploy pipeline upgrade:**
```python
def deploy_capability(conn, plan: dict) -> str:
    """Deploy a new capability through the full pipeline.
    
    1. Create sandbox from Friday's repo
    2. Write all new files
    3. Apply all modified files
    4. Install dependencies
    5. Run new capability's tests
    6. Run full regression suite
    7. If all pass: create rollback commit, merge to main
    8. Register capability flag (disabled)
    9. Report success
    
    Returns: capability_name for enable/rollback
    """
```

### Phase 4: CLI Commands

**`friday upgrade <description>`** — the main entry point:
```
friday upgrade "make yourself capable of speaking"
friday upgrade "add a discord bot so i can message you from discord"
friday upgrade "analyze analytics from my websites and apps"
friday upgrade "add a whatsapp integration"
friday upgrade "monitor my server's uptime and alert me"
```

This command:
1. Asks "This will modify Friday's own code. Create a sandbox, generate the capability, verify it, and deploy? (y/N)"
2. Creates sandbox
3. Calls the upgraded LLM planner with the codebase context
4. Writes/modifies files in sandbox
5. Runs tests
6. If tests pass: commits to sandbox, merges to main (or creates PR)
7. Enables the feature flag
8. Reports: "✅ Voice support added. Try: friday ask --voice 'hello' or set FRIDAY_VOICE_ENABLED=true"

**`friday upgrade list`** — list installed capabilities and their status:
```
Voice           ✅ Enabled    edge-tts, faster-whisper
Discord Bot     ✅ Enabled    
Web Analytics   ❌ Disabled   (installed but off)
WhatsApp        ⏳ Pending    (planned, not deployed)
```

**`friday upgrade rollback <name>`** — rollback a capability:
```
friday upgrade rollback voice
→ Reverting to pre-voice commit <hash>...
→ Regression suite: 1642 passed, 0 failed
→ Voice support removed.
```

**`friday upgrade plan <description>`** — dry-run: show what would change without doing it:
```
friday upgrade plan "make yourself capable of speaking"
→ Would create:
    src/friday/services/voice.py (320 lines)
    tests/test_voice.py (180 lines)
→ Would modify:
    src/friday/cli.py (+15 lines) — add --voice flag
    src/friday/daemon.py (+8 lines) — voice thread hook
    pyproject.toml (+2 lines) — add edge-tts, faster-whisper
→ Risk: medium (modifies existing files)
→ Rollback is safe (git revert)
```

### Phase 5: Integration with the Agentic Action Layer

When `friday do "make yourself capable of speaking"` goes through the Agentic Action Layer, it should:
1. Recognize "capability upgrade" intent
2. Route to the Self-Evolution Engine instead of a normal executor
3. The evolution engine takes over with sandbox + tests + deploy

The agent executor should have a special case for self-upgrade tasks that **bypasses the normal executor dispatch** and goes directly to the meta-engine.

### Phase 6: Rollback Safety

The critical safety requirement: **Friday must never break itself.**

Safety guarantees:
1. **Sandbox isolation** — all code runs in an isolated git worktree/copy (BUILT)
2. **Test gates** — new capability tests + full regression must pass (BUILT, upgrade needed)
3. **Feature flags** — new capabilities are disabled by default (NEW)
4. **Rollback commit** — before any merge, a rollback point is recorded (NEW)
5. **Auto-rollback on crash** — if the daemon crashes after deployment, rollback on restart (NEW)

**Auto-rollback mechanism:**
```python
# In daemon.py, at startup:
def _check_auto_rollback(conn):
    """If daemon crashed since last capability deployment, auto-rollback."""
    last_deploy = get_last_capability_deploy(conn)
    if last_deploy and not daemon_exited_cleanly(last_deploy):
        # Daemon crashed — probably the new capability caused it
        rollback_capability(conn, last_deploy.name)
        notify_operator(f"⚠ Auto-rolled back '{last_deploy.name}' — caused daemon crash")
```

## Files to touch
- `src/friday/meta/sandbox.py` — add install_deps(), read_file(), test_file(), snapshot(), rollback(), dry_run()
- `src/friday/meta/si_planner.py` — replace with multi-file capability planner (CAPABILITY_SYSTEM_PROMPT)
- `src/friday/meta/deploy.py` — add deploy_capability(), handle multi-file deploys
- `src/friday/meta/verification.py` — add staged verification for multi-file changes
- `src/friday/meta/capability.py` (NEW) — CapabilityFlag model, registry, lifecycle management
- `src/friday/meta/__init__.py` — update to include capability module
- `src/friday/cli_meta.py` — add `friday upgrade` command group
- `src/friday/cli.py` — register `friday upgrade` commands
- `src/friday/daemon.py` — add auto-rollback check on startup, capability health check
- `src/friday/db.py` — add capability_flags table
- `src/friday/agent.py` — route self-upgrade tasks to meta-engine (if agent exists)
- `tests/test_capability.py` (NEW) — test sandbox upgrades, planner, deploy pipeline
- `tests/test_meta_sandbox.py` — extend for new sandbox methods

## Acceptance Criteria
1. `friday upgrade plan "make yourself capable of speaking"` → shows voice.py, test_voice.py, cli.py changes, pyproject.toml deps, risk assessment WITHOUT modifying anything
2. `friday upgrade "add a simple text file reader worker"` → generates worker, installs nothing, passes regression, enables feature
3. `friday upgrade list` → shows all deployed capabilities and their enabled/disabled status
4. `friday upgrade rollback voice` → reverts all voice-related changes, regression passes
5. Generated capability code follows existing code style (type hints, docstrings, patterns)
6. Dependencies are installed correctly and don't break existing imports
7. If a capability's tests fail, deploy is ABORTED with clear error message
8. If daemon crashes after deployment, on next startup it auto-rollbacks the last deployed capability
9. `friday upgrade` without args shows help
10. Existing meta-engine tests still pass (gap_analyzer, si_planner, verification)

## Database Schema

```sql
CREATE TABLE capability_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    installed INTEGER NOT NULL DEFAULT 0,
    deps_installed INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT NOT NULL DEFAULT '{}',
    added_at TEXT NOT NULL,
    enabled_at TEXT,
    rollback_commit TEXT,
    last_used_at TEXT
);
```
