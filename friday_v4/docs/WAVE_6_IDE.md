# Wave 6 — IDE Integration

**Status:** ✅ SHIPPED (2026-08)
**Date:** 2026-08-03
**Promise:** *"Friday lives inside your editor."*
**MCU connection:** *no task it can't pick up* — the editor is a
capability like any other: detected, analyzed, controlled, and composed
with execution and the Claude Code arms.

---

## Why this wave exists

Wave 6 was the plan's **empty placeholder** — `desktop/ide/__init__.py`
tried to import a nonexistent `lsp_client` and set `_LSP_AVAILABLE =
False`. The ROADMAP promised an LSP client, a VS Code extension, inline
review, and status-bar integration, and none of it existed. This wave
built the real thing: not a stub, a masterpiece.

## What shipped

### 1. Adaptive editor detection — `desktop/ide/detection.py`

Friday figures out **which editor is actually there** and adapts to it:

- **Environment** (strongest): `TERM_PROGRAM=vscode` / `VSCODE_*` →
  VS Code; `NVIM` → Neovim; `GIO_LAUNCHED_DESKTOP_FILE` → JetBrains /
  VS Code.
- **Processes** (POSIX `ps`): `code`, `idea`, `pycharm`, `webstorm`,
  `goland`, `nvim`, `sublime_text`, `emacs`, …
- **Config dirs**: `~/.config/Code`, `~/.config/JetBrains/<Product>`,
  `~/.config/nvim`, `~/.config/sublime-text`, `~/.emacs.d`.

Each `DetectedIDE` carries its launcher, LSP capability, control
capability, and confidence + source. `friday4 status` reports it; the
CLI's `open`/`reveal` adapt their argv per kind.

### 2. Pure-stdlib LSP client — `desktop/ide/lsp_client.py`

**No pygls, no lsprotocol** — a hand-rolled JSON-RPC 2.0 client over
stdio that speaks enough of the Language Server Protocol to be useful:

- `initialize` / `initialized` handshake
- `textDocument/didOpen` with the file's real text
- `textDocument/diagnostic` (LSP 3.17 on-demand pull) with a
  `publishDiagnostics` push fallback
- `textDocument/documentSymbol` (hierarchical + flat)
- `shutdown` / `exit`, timeout-bounded, background reader thread,
  graceful degrade (`start() -> False` on a missing/broken server)

Project markers select the server (`pyproject.toml` → pyright /
basedpyright / pylsp; `package.json` → typescript-language-server;
`go.mod` → gopls; `Cargo.toml` → rust-analyzer), with single-file
extension fallback so a lone `.ts` still gets TypeScript tooling.

### 3. Always-on analyzer — `desktop/ide/ast_analyzer.py`

When no server is available, Friday **still has an opinion**: the
stdlib `ast` finds syntax errors, undefined names (F821), unused
imports (F401), and shadowed builtins (A002) — deterministic,
conservative, capped, sorted by line. `analyze_file()` tries LSP first
and degrades to AST; the result's `method` (`lsp` / `ast` / `none`)
keeps the answer honest about which analyzer produced it.

### 4. Editor control — `desktop/ide/controller.py`

`open` / `reveal` / `run`, argv-adapted per editor:

| Editor | open | reveal |
|---|---|---|
| VS Code | `code -r file` | `code -r -g file:line` |
| JetBrains | `idea file` | `idea --line N file` |
| Neovim | `nvim file` | `nvim +N file` |
| Sublime | `subl file` | `subl file:N` |
| Emacs | `emacs file` | `emacs +N file` |

No editor detected → platform opener (`xdg-open` / `open`). Everything
argv-based, timeout-bounded, never raises.

### 5. One command language — NL on every surface

`Intent.IDE` is a first-class intent with **conservative** tie-breaks
(ordered last so bare-word ties keep the pre-existing meaning):

- `"what's wrong with src/main.py"` → IDE, target `src/main.py`
- `"diagnose auth.py"`, `"lint src/main.py"`, `"why won't this compile"`
- Guards regression-tested: `"run the tests"` → EXECUTE,
  `"diagnose the memory leak"` → EXECUTE/claude (no file),
  `"analyze vivaha"` → RESEARCH, `"check my deps"` → SECURITY,
  `"git status"` → EXECUTE.

The answer is evidence-shaped: `"I found 2 error(s) in auth.py (via
ast): line 3: undefined name 'get_token'; line 5: shadowed builtin:
list."` — or `"no issues found"` for a clean file, or `"Which file?"`
when no target is named. **Never fabricates.**

### 6. Reasoning — the CODE backstop

`QuestionType.CODE` + `code_provider` answer code questions asked as
questions (the LLM can classify "what's wrong with X" as ASK): evidence
is cited as `v4.ide.lsp` / `v4.ide.ast`. No file → honest "I don't know
yet".

### 7. Composition — the arms

- **Claude Code arms:** `FRIDAY_V4_IDE_PREFLIGHT=1` → Friday's own
  diagnostics for the workspace/file the delegated task touches ride
  along via `claude --append-system-prompt` — the agent starts from
  what Friday already knows. Never blocks, never raises.
- **Execution preflight:** the same opt-in adds a heads-up
  (`N issue(s) in auth.py`) to the audit goal and the spoken reply when
  a command names a source file.
- **CLI:** `friday4 ide detect / diagnose / symbols / open / reveal /
  run` — `run` goes through the **same gated execution pipeline**
  (gate → sandbox → audit) as `friday4 talk`.

## The Wiring Law table

| Consumer | Wired? |
|---|---|
| **Entry points** (talk / voice / web) | ✅ all route through `nl_router` → `Intent.IDE` → `_ide_response` |
| **CLI surface** | ✅ `friday4 ide …` registered in `cli_talk.main` |
| **Reasoning provider** | ✅ `QuestionType.CODE` + `code_provider` (evidence `v4.ide.*`) |
| **Status feed** | ✅ `_probe_ide` in `STATUS_PROBES` (`friday4 status`) |
| **Capability registry** | ✅ `surface:ide`, `provider:code`, `intent:ide` |
| **Execution / Claude arms** | ✅ `FRIDAY_V4_IDE_PREFLIGHT` → preflight note + `--append-system-prompt` |
| **Daemon schedules** | ✅ none needed — IDE analysis is on-demand (no decay/sweep); the status probe covers liveness |

## Not built (honest)

- **TypeScript VS Code extension** (sidebar, status bar, decorations) —
  the editor is reachable today via the CLI (`code -r`, `code -g
  file:line`) and the LSP protocol; an extension would only add an
  in-UI surface. A future refinement, not a gap in the wave's promise:
  analysis, control, and NL review all work now.

## Tests (hermetic — no real editor, no real server)

`tests/test_ide_wave6.py` — **42 tests**:

- **LSP client** against a fake language server script (initialize →
  diagnostic pull → symbols → shutdown; missing binary degrades; a
  server that dies mid-session raises a clean LSPError, never an
  AttributeError — regression for the EOF-sentinel review fix).
- **AST analyzer** (syntax error, undefined name, unused import,
  shadowed builtin, clean file, missing file; review regressions:
  `except ... as err` is never a false undefined-name, and
  `from x import *` is never flagged unused).
- **Facade** (`analyze_file` AST fallback / LSP path / missing file;
  symbol outline; language-server lookup).
- **Detection** (env / process / config signals; none-detected).
- **NL** (IDE phrases classify; existing intents not hijacked; handler
  answers with diagnostics; clean-file and clarification paths).
- **Reasoning** (CODE backstop; clean-file; unknown-target silence).
- **Wiring** (capability registry ids; status probe; CLI import).
- **Composition** (claude `--append-system-prompt` on/off; router
  preflight note).

## Close-out

- Wave 6 is no longer a placeholder: detection + LSP + AST + control +
  NL + reasoning + CLI + composition all ship and are wired.
- The ROADMAP table now shows **✅ SHIPPED (2026-08)**; PLAN.md Phase 7
  and the command table are updated; MASTER_PLAN §1 has the Wave 6 row.
- Validation: full suite green (see the wave's test run), targeted
  suites green, code review applied.
