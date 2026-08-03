# Wave 18 — Claude Code Hands

> **The sentence clause:** *no task it can't pick up* — capability
> composition, made real with an agent that can actually do the work.
>
> **One-line summary:** Friday stays the brain (NLU → gate → audit →
> memory); the local **Claude Code CLI** becomes the hands for complex
> agentic goals. "figure out why the build fails and fix it" now
> delegates to Claude Code — through the same gated, sandboxed, audited
> pipeline as every other action.
>
> **Status:** ✅ SHIPPED — 2026-08. 25 hermetic tests. Suite green.

---

## 1. Why this wave

The honest gap review (Wave 16/17 close-out) found Friday could
*execute, reason, remember, and learn* — but **"figure out why the
build fails and fix it" was the one sentence that didn't work**: the
resolver produced an EXECUTE with an *empty command* (a guaranteed
"empty command" failure) or a thin 0-step mission. The MCU test #1 /
#5 moment — capability *composition* — needed an agent that can read a
repo, run the build, see the failure, fix it, and re-run.

The user's directive was explicit: **"let's not make our Friday run the
code itself — use the Claude Code CLI on the system. Claude Code is
smart itself so it won't mess up either."**

So: Friday = orchestrator + safety floor. Claude Code = the hands.

## 2. Design

### Where it plugs in

A new **executor** in the Wave 9 pipeline — `ClaudeCodeExecutor`
(`action_type = "claude"`), registered in `execution/executors.py` and
exported from `execution/__init__.py`. Nothing else about the pipeline
changed: it is `classify → record → confirm? → sandbox.run → finish`
like every other executor.

The executor's **command IS the task**:

```
claude -p "<task>" --output-format json --model fable \
       --allowedTools "Bash Read Edit Write Glob Grep"
```

- `-p` (print mode) + `--output-format json` → a single JSON result line
  with `result` / `is_error` / `terminal_reason` / `permission_denials`
  / `total_cost_usd`.
- `--model fable` → the gateway model proven in this workspace
  (settings.json's `ANTHROPIC_MODEL=cc/claude-sonnet-5` env is broken —
  404; the CLI flag overrides it). Configurable via
  `FRIDAY_V4_CLAUDE_MODEL`.
- `--allowedTools "Bash Read Edit Write Glob Grep"` → Claude can read,
  edit, run bash — but **no network tools** (WebFetch absent from the
  allowlist), so it can't exfiltrate or reach out.

### The gate (safety floor stays Friday's)

The task text is classified like any command:

- **CONFIRM by default** — delegating is state-changing, so the
  operator confirms (interactive y/N, voice ask, or `--force`).
- **Destructive *phrases* escalate to NEVER** — but phrase-level, not
  bare-word: `"push my changes to origin"`, `"deploy to production"`,
  `"rm -rf"`, `"drop database"` → NEVER (operator override only). A
  *diagnostic* request — `"investigate the deploy failure and tell me
  why"` — stays CONFIRM. Bare-word sniffing ("push"/"deploy") would
  false-positive on exactly the diagnostic tasks this wave exists for.

`_CLAUDE_DANGEROUS_PHRASES` in `execution/executors.py`.

### The sandbox

- cwd-rooted (path allowlist follows the workspace being acted on)
- **timeout-bounded** — default 600s (`FRIDAY_V4_CLAUDE_TIMEOUT`),
  never hangs the daemon
- **env-sanitized** — secret-looking vars stripped (Claude Code's own
  auth comes from its settings file, not the child env)
- **stdin=/dev/null** — the sandbox's `run()` now defaults stdin to
  DEVNULL so a delegated task never waits on the operator's terminal
  (Claude prints a "no stdin" warning otherwise)

### Result parsing

`_parse_result` maps the CLI's JSON back onto a `SandboxResult`:

| CLI JSON | Result |
|---|---|
| `is_error: false` | succeeded — `result` text surfaced |
| `is_error: true` | failed — the CLI's own message surfaced |
| `terminal_reason: error_limit/max_turns` | failed (context exhausted) |
| non-JSON stdout | raw text surfaced (proxy warning etc.) |
| `permission_denials: [...]` | succeeded + a note that N tool calls were denied |

A missing `claude` CLI → structured failure ("claude CLI not found"),
never an exception — the daemon law.

## 3. The NL path (Law 1)

Every surface uses the ONE NLU point, so routing lives in the resolver:

- **`nlu/resolver.py`** — `_EXECUTION_TYPES` gains `"claude"`. When an
  EXECUTE intent resolves to an empty/no command **and** the utterance
  reads like a complex goal (`_AGENTIC_MARKERS`: "figure out", "find
  out why", "debug", "investigate", "troubleshoot", "diagnose",
  "root cause", "fix the", "make the", "get the tests", …) → route to
  `claude`, command = the full task text.
- **`nlu/intent.py`** — `is_agentic_goal()` (shared markers) + the
  fallback scores agentic verbs ("debug", "investigate", …) so offline
  classification also lands on EXECUTE, and sets `action_type="claude"`
  directly so the offline path doesn't ask "what would you like me to
  run?".
- **`nlu/llm.py`** — the LLM prompt gains `claude` as a valid
  action_type with the same rule ("complex agentic goal with no single
  concrete command → action_type 'claude', command = the full goal").

**Concrete commands stay native** (the user's choice — complex only):
`"git status"` → git, `"run the tests"` → testing, `"read README.md"`
→ file. Claude is not a toll gate on every command.

### What now works (verified)

| You say | Friday does |
|---|---|
| "figure out why the build fails and fix it" | → claude executor → Claude Code reads/runs/fixes → result surfaced |
| "fix the failing test" | → claude (no path entity → not a bare pytest) |
| "debug the memory leak" | → claude |
| "investigate the crash" | → claude |
| "push my changes to origin" | **NEVER** — denied without `--force` |
| "git status" / "run the tests" / "read README.md" | native executors, instant |

## 4. Surfaces

- **CLI:** `friday4 execute claude "<task>"` (choices list updated) +
  `friday4 talk "figure out why the build fails and fix it"` (NL path,
  with confirm/force semantics) — exit codes: 0 ok / 2 denied / 1 failed.
- **Voice / web:** inherited automatically — they route through
  `nl_router`/the ONE NLU point, so a spoken "figure out why the build
  fails" delegates the same way (voice confirm asks aloud).
- **Capability registry:** `executor:claude` — "what can you do" tells
  the truth.

## 4.5 Planning through Claude Code (close-out)

Friday stays the planner too, when opted in. The Wave 9 `Planner`
(`missions/planner.py`) has an unused `enhancer` hook — an
LLM-style `(goal) -> list[StepPlan] | None` that enriches the
deterministic decomposition. **`ClaudePlanner`**
(`missions/claude_planner.py`) fills it: an agentic goal
("ship the auth refactor by Friday") is decomposed by Claude Code
through the same gate → sandbox → audit shape as the executor, with
one deliberate difference — planning is **read-only**:

```
claude -p "<plan prompt>" --output-format json --model <model> \
       --allowedTools "Read Glob Grep"        # inspect, never edit/run
```

- **Gate** — the goal text is classified with the same
  `_CLAUDE_DANGEROUS_PHRASES`; a NEVER goal ("deploy to production by
  Friday") is refused outright, and the deterministic planner turns it
  into a *manual* step (nothing executes).
- **Sandbox** — cwd-rooted, timeout-bounded, env-sanitized,
  stdin=/dev/null, read-only tools. The steps it produces execute
  later through the *real* gate → sandbox → audit pipeline, one by
  one, with the operator's confirm at each state-changing step.
- **Audit** — the delegation is recorded (`action_type = "claude_plan"`)
  with its gate level and outcome.
- **Parse** — the plan JSON maps onto `StepPlan` objects; unknown
  `action_type` values become manual steps (Friday never invents an
  executor). Any failure (missing CLI, is_error, timeout, malformed
  JSON) → `None` → the deterministic planner stands.
- **Opt-in** — `FRIDAY_V4_CLAUDE_PLANNER=1` (same convention as Wave
  13's `FRIDAY_V4_LLM`); without it, mission planning is pure
  deterministic and never touches claude (hermetic by default).
  Config: model/timeout reuse `FRIDAY_V4_CLAUDE_MODEL` /
  `FRIDAY_V4_CLAUDE_TIMEOUT`; tool allowlist is
  `FRIDAY_V4_CLAUDE_PLAN_TOOLS`.
- **One construction point** — `make_planner(cwd, conn)` (in
  `missions/claude_planner.py`, re-exported from `missions/`) returns
  the claude-enhanced `Planner` when the opt-in env is set and the
  plain deterministic one otherwise. `MissionEngine.__init__` uses it
  for its default planner, so **`create()` and `replan()`** both
  decompose through Claude Code under the same opt-in — the autonomy
  loop, CLI, and NL surface all inherit one planner.
- **Replan by NL** — "replan this mission" / "change the plan" /
  "revise my plan" route to `nl_router._replan_response`: the latest
  ACTIVE mission (else the latest) is re-decomposed on its own goal
  through Claude Code, and the Wave 9 adaptation contract reports
  "plan changed because …" honestly. Mission *creation* phrases
  ("create a plan", "ship the auth refactor by Friday") never match
  — `_is_replan_request` is conservative.
- **The Wiring Law CLI (W19 slice 0)** — `friday4 mission
  create|list|status|replan|advance|start|pause|cancel|complete|delete`
  (`cli_missions.py`): the missions layer's debug hatch, using the
  SAME `make_planner` construction point, so Claude Code decomposition
  honors the same `FRIDAY_V4_CLAUDE_PLANNER` opt-in and never
  diverges from the NL path. `--json` for scripting.

**MCU test #1, upgraded:** "ship the auth refactor by Friday" with the
opt-in set gets a repo-grounded step plan from Claude Code, still
persisted, tracked, and advanced by Friday's mission engine — and
"replan this mission" re-decomposes it through the same path, reporting
exactly what changed.

## 5. Wiring table

| Consumer | Wired? |
|---|---|
| Mission planning — create + replan (`make_planner`; `MissionEngine` default; `nl_router` PLAN + replan paths, `FRIDAY_V4_CLAUDE_PLANNER` opt-in) | ✅ |
| `friday4 mission` CLI (create/list/status/replan/advance/lifecycle — same `make_planner` point) | ✅ |
| `friday4 talk` (nl_router `_run_execution`) | ✅ |
| `friday4 execute claude "<task>"` | ✅ |
| Voice router (via nl_router fallback) | ✅ (inherited) |
| Web chat (via nl_router) | ✅ (inherited) |
| Capability registry (`executor:claude`) | ✅ |
| Daemon | n/a (on-demand executor, like ssh) |
| Reasoning | n/a (not a question type — it's an action) |

## 6. What was learned

- **The model env in settings.json was broken** (`cc/claude-sonnet-5`
  → 404). The fix is passing `--model fable` explicitly; the executor
  defaults to that and reads `FRIDAY_V4_CLAUDE_MODEL` for overrides.
- **Print mode + `--allowedTools` works for a full agent**: Claude ran
  `git status --short` through the gateway and returned the count — no
  permission prompt, `permission_denials: []`.
- **stdin matters**: without stdin=/dev/null, `claude -p` waits ~3s
  for "no stdin data" before proceeding. The sandbox now defaults stdin
  to DEVNULL (safe for every executor).
- **Bare-word gate sniffing is wrong for tasks**: a natural-language
  task mentioning "push"/"deploy" is often a *diagnostic* ("why does
  the deploy fail"). Phrase-level sniffing keeps the safety floor
  without blocking the exact requests this wave exists for.

## 7. Tests

`tests/test_claude_executor.py` — 25 hermetic tests:

- registration + gate (CONFIRM default, phrase→NEVER, diagnostic stays
  CONFIRM), capability registry
- fake `claude` binary (parametrized modes: ok / error / denied_tools /
  naked / hang) → success parse, structured failure, denied-tools note,
  raw-text fallback
- missing CLI → graceful failure, still audited
- gate semantics: denied without force, NEVER even with a bare confirm,
  `--force` overrides, confirm_fn approves
- timeout (hang mode → timed_out, never hangs)
- resolver routing (agentic → claude; concrete → native; LLM-explicit
  claude; ask stays ask)
- NL router end-to-end (success surfaces Claude's result; failure
  surfaces Claude's error)
- CLI exit codes (0 / 1 / 2)

`tests/test_claude_planner.py` — 27 hermetic tests (close-out): plan
JSON parsing (bare/fenced), unknown action_type → manual step, every
failure mode → `None` (deterministic floor), gate refusal of
NEVER goals (audited `denied`, `permission_level=never`), audit rows
(`action_type="claude_plan"`), CLI arg shape (`-p` / `--model` /
read-only `--allowedTools`), NL router end-to-end with the
`FRIDAY_V4_CLAUDE_PLANNER` opt-in (mission created from Claude's plan;
no-cli fallback; claude failure survives), the hermeticity guard
(claude never consulted without the opt-in), and the replan close-out
(`make_planner` env gating, `MissionEngine.replan` through Claude,
NL "replan this mission" path, no-mission honesty, failure survival,
conservative replan detection).

`tests/test_cli_missions.py` — 11 hermetic tests (W19 slice 0):
create/status roundtrip, JSON output, list + status filter, lifecycle
transitions, replan reports "plan changed because…" through the
`make_planner` point, advance of a manual step, and the hermeticity
guard (claude never consulted without the opt-in).
