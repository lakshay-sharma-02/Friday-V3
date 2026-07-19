# M10 — Capability System: Design

Frozen architecture. This milestone EXTENDS the existing worker architecture;
it does not replace it. No new managers, engines, planners, contexts,
pipelines, databases, or registries.

The capability system is composed entirely from existing pieces:

- `worker/models.py` + `worker/engine.py` — registry, capability profiles,
  closed-vocabulary validation.
- `resolver/` (M9.3) — deterministic `rank_workers` / `select_assignment`.
- `runtime/` (M9.5) — `Worker.execute(task) -> ExecutionResult`, executor,
  `runtime_results` history.
- `review/` — `ReviewReport` (verdict + confidence).

New surface area is deliberately small: a metadata field extension, a
`CLIWorker` base + 6 thin external adapters, a `DiscoveryResult` model, a
`WorkerManifest` canonical description, a verification step before review, and
four `friday capability` CLI commands.

---

## 1. Worker model extension (metadata only)

Add to the existing `Worker` dataclass (`worker/models.py`):

| field          | type                              | meaning |
|----------------|-----------------------------------|---------|
| `implementation` | `native\|cli\|api\|mcp\|plugin` | how the worker runs |
| `provider`       | `anthropic\|openai\|google\|deepseek\|local\|friday` | who supplies it |
| `availability`   | `available\|unavailable\|error`  | runtime install state |
| `manifest_ref`   | `Optional[str]`                  | id of the `WorkerManifest` it was built from |

`version` already exists. No new DB table: these map onto existing
`WorkerRow` columns (extend the row + `to_row`/`from_row`).

Future-proofing: a synthesized worker is simply
`implementation="plugin", provider="friday"`. No later architecture change.

---

## 2. WorkerManifest — canonical self-description

Every worker (native, CLI, API, plugin, or future-synthesized) declares one
manifest. The registry row is DERIVED from the manifest; the runtime never
cares where a manifest came from.

```python
@dataclass
class WorkerManifest:
    name: str
    implementation: str          # native|cli|api|mcp|plugin
    provider: str                # anthropic|openai|google|deepseek|local|friday
    capabilities: list[str]      # closed vocabulary (validated)
    requirements: list[str]      # e.g. ["claude"] for PATH binaries,
                                 # or ["DEEPSEEK_API_KEY"] for API workers
    supported_task_types: list[str]
    supported_plan_types: list[str]
    supported_languages: list[str] = []
    description: str = ""
    supports_workspace: bool = False
    supports_streaming: bool = False
    supports_files: bool = False
    supports_patch: bool = False
    estimated_speed: str = "unknown"
    estimated_cost: str = "unknown"
    confidence: str = "medium"
```

Native workers get manifests alongside their existing registration. External
adapters each return a static manifest (capabilities they advertise, e.g.
Claude Code: `Refactoring, Large Context, Documentation, Architecture Review,
Testing`).

---

## 3. CLIWorker base + Invocation

`CLIWorker` (new base in `runtime/workers.py`) owns ALL subprocess mechanics:
argv execution, timeout, cwd, env, stdout/stderr capture, exit_code →
`ExecutionResult`. It knows nothing about any specific tool.

```python
@dataclass
class Invocation:
    argv: list[str]
    stdin: Optional[str] = None
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = _DEFAULT_TIMEOUT

class CLIWorker(Worker):
    def execute(self, task) -> ExecutionResult:
        inv = self.build_invocation(task)     # subclass responsibility
        proc = subprocess.run(inv.argv, input=inv.stdin,
                              cwd=inv.cwd, env=inv.env or None,
                              capture_output=True, text=True,
                              timeout=inv.timeout)
        return _ok_or_fail(proc)
    def build_invocation(self, task) -> Invocation:
        raise NotImplementedError
```

Each external adapter implements ONLY `build_invocation(task)` (or, for
non-subprocess transports, overrides `execute` and calls its own
`serialize(task)`). The runtime executes an `Invocation` generically — it does
not know Claude/Codex/Gemini exist.

### Per-adapter serialization (no hardcoded prompts in the runtime)

Each adapter owns translation of a `RuntimeTask` into its native format:

- `ClaudeCodeWorker.build_invocation` → `claude --print <prompt>`
- `CodexWorker` → `codex <prompt>`
- `GeminiWorker` → `gemini <prompt>` (or stdin per version)
- `OpenCodeWorker` → `opencode <prompt>`
- `AiderWorker` → `aider --message <prompt>`
- `DeepSeekWorker` → CLI (`deepseek <prompt>`) if binary present, else API
  (HTTP POST to configured endpoint) when `DEEPSEEK_API_KEY` set.

The runtime never sees prompts. A tool that later needs JSON-over-stdin or
HTTP simply overrides `execute()` and reuses `serialize(task)`.

### Adapter list (all 6, auto-detected)

| Worker | Binary (PATH) | Requirement |
|--------|---------------|-------------|
| Claude Code | `claude` | `claude` |
| Codex CLI | `codex` | `codex` |
| Gemini CLI | `gemini` | `gemini` |
| OpenCode | `opencode` | `opencode` |
| Aider | `aider` | `aider` |
| DeepSeek | `deepseek` or API key | `deepseek` / `DEEPSEEK_API_KEY` |

If the requirement is absent, the worker is registered but `availability =
unavailable`. No crash, no import-time failure.

---

## 4. Discovery — `friday capability discover`

`discover() -> DiscoveryResult`. Discovery is READ-ONLY reality-scanning; it
does NOT mutate the registry.

```python
@dataclass
class DiscoveryResult:
    available: list[str]      # worker ids present + runnable
    unavailable: list[str]    # declared but missing dep
    missing_deps: dict[str, list[str]]  # worker -> missing requirements
```

Scan sources:
- PATH via `shutil.which(req)` for each manifest requirement that is a binary.
- Configured API keys/env (`DEEPSEEK_API_KEY`, etc.) for API workers.
- Available MCP servers (from a known config list / env).

A separate `register(discovery)` step updates `availability` on the registry
rows. This reality/known-capability separation keeps discovery trivially
testable.

---

## 5. Routing (reused, no special cases)

`resolver.select_assignment` already does deterministic, evidence-based,
non-LLM routing. M10 only widens the worker pool to include external adapters.
No `if task == architecture: use Claude`. External workers advertise capabilities
(`Architecture Review`, `Large Context`, `Refactoring`, `Testing`,
`Documentation`) exactly like native workers; the existing score/rank logic
applies unchanged. Routing stays deterministic.

---

## 6. Execution flow — verification before review

```
worker.execute(task) -> ExecutionResult
        ↓
verify(task, result)            # objective correctness
        ↓ (always runs first)
review(task, result)           # quality (optional, skipped if verify fails)
        ↓
persist(result + verification + review verdict)
```

- **Verification** = objective, per worker kind:
  - native deterministic (shell/git/pytest/file): exit code / file-exists /
    pytest-exit-0 as today.
  - external AI (Claude/Codex/Gemini/...): `exit_code == 0` AND non-empty
    `stdout` (the model produced output). No fabricated success — a crash or
    empty reply fails verification.
  No reason to ask Review whether pytest passed.
- **Review** = quality (`review.ReviewReport` verdict/confidence). Runs after
  verification; skipped when verification fails (no point reviewing broken
  output).
- Persisted in `runtime_results` (success + review verdict + verification
  outcome).

Friday remains the orchestrator. External AI output is verified then reviewed;
it never modifies Friday architecture.

---

## 7. Derived health (no new table)

Health is DERIVED from `runtime_results` (facts persist, summaries derive):

- success rate = `success=1` count / total, by `worker_id`
- avg duration = mean `duration_ms` by `worker_id`
- verification failures = count where verification failed
- last success / last failure = max `recorded_at` by status

Computed on `capability info <worker>`; benchmarked later only if slow.

---

## 8. CLI — `friday capability ...`

- `friday capability discover` — scan PATH/APIs/MCP; print
  available / unavailable / missing-deps.
- `friday capability list` — registered workers (reuses
  `WorkerRegistry.all_workers`).
- `friday capability info <worker>` — capabilities, derived health, supported
  tasks, provider, implementation.
- `friday capability benchmark` — run deterministic benchmark tasks across
  AVAILABLE workers; compare at the CAPABILITY level (pass/fail + duration),
  e.g. "Documentation Task A → Claude: Pass 4.3s; Gemini: Pass 6.1s; Native:
  Pass 0.2s". No "smartest AI" subjective score. Friday learns "Claude tends
  to do well on documentation", not a brittle numeric rank.

---

## 9. Tests (regression)

Extend `tests/test_workers.py`, `tests/test_resolver.py`; add
`tests/test_capability_cli.py` and `tests/test_discovery.py`. Cover:

- Discovery: available worker, unavailable worker (missing binary),
  missing-dependency reporting, no crash on absent tool.
- Routing: external adapter selected by capability (not by name branch).
- Execution: adapter runs a real invocation; unavailable adapter reports
  failure, not crash.
- Review integration: verify-before-review ordering; verification failure
  skips review.
- Capability metadata: manifest → registry row; closed-vocabulary validation.
- Derived health: computed from seeded `runtime_results`.
- Benchmark: deterministic pass/duration comparison.

Full suite must remain green.

---

## 10. Files touched (summary)

- `src/friday/worker/models.py` — add `implementation`, `provider`,
  `availability`, `manifest_ref`; `WorkerManifest` dataclass.
- `src/friday/worker/engine.py` — register from manifest; update availability.
- `src/friday/runtime/workers.py` — `Invocation`, `CLIWorker` base; 6 external
  adapters; `verify()` step.
- `src/friday/runtime/engine.py` / `executor.py` — wire verify→review→persist.
- `src/friday/runtime/discovery.py` (new) — `discover() -> DiscoveryResult`.
- `src/friday/cli_capability.py` (new) — discover/list/info/benchmark.
- `src/friday/cli.py` — register `capability` subparser.
- Tests as in §9.

No new DB tables. No new subsystems. Architecture frozen.
