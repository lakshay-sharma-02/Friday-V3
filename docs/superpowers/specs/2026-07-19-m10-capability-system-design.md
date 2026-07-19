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

## 1. Worker model — manifest is the single source of truth

The `WorkerManifest` is the ONLY store of static worker identity. The registry
row (`Worker` / `WorkerRow`) holds ONLY mutable runtime state:

| field           | type                          | meaning |
|-----------------|-------------------------------|---------|
| `id`            | `worker:<name>`               | stable identity |
| `manifest_ref`  | `str`                         | id of the manifest it was built from |
| `availability`  | `available\|unavailable\|error` | runtime install state |
| `version`       | `str`                         | manifest version (bumped on upgrade) |

Everything else (implementation, provider, capabilities, requirements,
supported_*, description, estimated_*, confidence, origin) lives in the
manifest and is READ through it. **No field is duplicated** between manifest
and registry — this eliminates an entire class of synchronization bugs.

A worker NEVER mutates its own manifest at runtime. Only `availability`,
derived `health`, and `version` (on upgrade) may change. The flow is always
`Manifest → Registry Row → Runtime`, never the reverse.

Future-proofing: a synthesized worker is simply
`implementation="plugin", provider="friday", origin="generated"`. No later
architecture change.

---

## 2. WorkerManifest — immutable capability declaration

Every worker (native, CLI, API, plugin, or future-synthesized) declares one
immutable manifest. The runtime never cares where a manifest came from.

```python
@dataclass(frozen=True)
class WorkerManifest:
    name: str
    implementation: str          # native|cli|api|mcp|plugin
    provider: str                # anthropic|openai|google|deepseek|local|friday
    origin: str                  # builtin|external|generated
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

`frozen=True`: a manifest is a capability declaration, not mutable metadata.
Registry rows are built FROM manifests; native workers register via manifest,
external adapters each return a static manifest (e.g. Claude Code:
`Refactoring, Large Context, Documentation, Architecture Review, Testing`).

---

## 3. CLIWorker base + Invocation + worker-owned verification

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
    stream: bool = False          # reserved: streaming support (not impl now)

class CLIWorker(Worker):
    def execute(self, task) -> ExecutionResult:
        inv = self.build_invocation(task)     # subclass responsibility
        proc = subprocess.run(inv.argv, input=inv.stdin,
                              cwd=inv.cwd, env=inv.env or None,
                              capture_output=True, text=True,
                              timeout=inv.timeout)
        return self._to_result(proc)
    def build_invocation(self, task) -> Invocation:
        raise NotImplementedError
    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        # default: exit 0 + non-empty stdout (sane for external AI)
        return VerificationResult(
            passed=result.exit_code == 0 and bool(result.stdout.strip()),
            reason="exit_code==0 and stdout non-empty")
```

Each external adapter implements ONLY `build_invocation(task)`. The runtime
executes an `Invocation` generically — it does not know Claude/Codex/Gemini
exist. Adapters that later need JSON-over-stdin or HTTP override `execute()`
and reuse their own serialization.

### Per-adapter serialization (no hardcoded prompts in the runtime)

Each adapter owns translation of a `RuntimeTask` into its native format:

- `ClaudeCodeWorker.build_invocation` → `claude --print <prompt>`
- `CodexWorker` → `codex <prompt>`
- `GeminiWorker` → `gemini <prompt>` (or stdin per version)
- `OpenCodeWorker` → `opencode <prompt>`
- `AiderWorker` → `aider --message <prompt>`
- `DeepSeekWorker` → CLI (`deepseek <prompt>`) if binary present, else API
  (HTTP POST to configured endpoint) when `DEEPSEEK_API_KEY` set.

The runtime never sees prompts.

### `verify(task, result)` lives in the worker interface

Verification is worker-defined, not baked into the runtime. Every worker
exposes `verify(task, result) -> VerificationResult(passed, reason)`:

- Shell → exit code 0.
- Git → `git status` clean / expected ref.
- Python/Testing → pytest exit 0.
- Claude (external AI) → exit 0 + stdout + expected artifact exists.
- Future Docker worker → container started; K8s worker → deployment healthy.

The runtime simply calls `worker.verify(...)` — no branching, no per-kind
`if`. This scales to any future implementation.

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

### ExecutionResult gains provenance

Extend `ExecutionResult` with `worker_id`, `started_at`, `ended_at`,
`metadata`. Provenance lets Friday answer "which worker produced this file?"
later without schema changes.

---

## 4. Discovery + Availability Sync — `friday capability discover`

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

A separate **availability sync** step updates only `availability` on the
registry rows (workers are already registered; only their current runtime
state changes). Mental model: `Discovery → Availability Sync → Registry`.
This reality/known-capability separation keeps discovery trivially testable.

---

## 5. Routing (reused, no special cases, deterministic)

`resolver.select_assignment` already does deterministic, evidence-based,
non-LLM routing. M10 only widens the worker pool to include external adapters.
No `if task == architecture: use Claude`. External workers advertise capabilities
(`Architecture Review`, `Large Context`, `Refactoring`, `Testing`,
`Documentation`) exactly like native workers; the existing score/rank logic
applies unchanged.

**Routing stays deterministic.** Derived health is NOT fed into selection
implicitly. Only declared capabilities + objective availability drive routing.
If health-based routing is wanted later, it must be an explicit, debuggable
policy — not implicit learning. The user asks for a capability; Friday selects
an implementation. The capability (e.g. "Code Refactoring") is stable; the
implementation (Claude today, Codex tomorrow, a generated worker later) is
what the resolver picks.

---

## 6. Execution flow — verification before review

```
worker.execute(task) -> ExecutionResult   (provenance attached)
        ↓
worker.verify(task, result) -> VerificationResult   # objective correctness
        ↓ (always runs first)
review(task, result)           # quality (optional, skipped if verify fails)
        ↓
persist(result + verification + review verdict)
```

- **Verification** = worker-owned (see §3). Objective correctness: exit code,
  file exists, pytest pass, container healthy. No reason to ask Review whether
  pytest passed.
- **Review** = quality (`review.ReviewReport` verdict/confidence). Runs after
  verification; skipped when verification fails (no point reviewing broken
  output).
- Persisted in `runtime_results` (success + review verdict + verification
  outcome + provenance).

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
  tasks, provider, implementation, origin.
- `friday capability benchmark` — CLI calls a standalone `BenchmarkRunner`
  (NOT a CLI-only concept). Runs deterministic benchmark tasks across AVAILABLE
  workers; compares at the CAPABILITY level (pass/fail + duration), e.g.
  "Documentation Task A → Claude: Pass 4.3s; Gemini: Pass 6.1s; Native:
  Pass 0.2s". No "smartest AI" subjective score. Friday learns "Claude tends
  to do well on documentation", not a brittle numeric rank. Later, Friday can
  invoke the same `BenchmarkRunner` automatically (e.g. when it notices a
  worker slowed down) — same code.

---

## 9. Tests (regression)

Extend `tests/test_workers.py`, `tests/test_resolver.py`; add
`tests/test_capability_cli.py`, `tests/test_discovery.py`,
`tests/test_benchmark.py`. Cover:

- Discovery: available worker, unavailable worker (missing binary),
  missing-dependency reporting, no crash on absent tool.
- Routing: external adapter selected by capability (not by name branch);
  health never implicitly influences selection.
- Execution: adapter runs a real invocation; unavailable adapter reports
  failure, not crash.
- verify-before-review: worker.verify ordering; verification failure skips
  review; each worker's verify is distinct (shell vs git vs pytest vs AI).
- Capability metadata: manifest → registry row (no field duplication);
  closed-vocabulary validation; immutable manifest.
- Derived health: computed from seeded `runtime_results`.
- Benchmark: deterministic pass/duration comparison via `BenchmarkRunner`.
- Provenance: `ExecutionResult` carries `worker_id`/timestamps.

Full suite must remain green.

---

## 10. Files touched (summary)

- `src/friday/worker/models.py` — `WorkerManifest` (frozen, with `origin`);
  `Worker` row reduced to `id/manifest_ref/availability/version`;
  `VerificationResult`; `ExecutionResult` provenance fields.
- `src/friday/worker/engine.py` — register from manifest; availability sync.
- `src/friday/runtime/workers.py` — `Invocation` (+`stream`), `CLIWorker` base
  (owns subprocess + `verify` default); 6 external adapters; provenance.
- `src/friday/runtime/engine.py` / `executor.py` — wire
  execute→verify→review→persist.
- `src/friday/runtime/discovery.py` (new) — `discover() -> DiscoveryResult`.
- `src/friday/runtime/benchmark.py` (new) — `BenchmarkRunner` (CLI-agnostic).
- `src/friday/cli_capability.py` (new) — discover/list/info/benchmark.
- `src/friday/cli.py` — register `capability` subparser.
- Tests as in §9.

No new DB tables. No new subsystems. Architecture frozen.
