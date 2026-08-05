# Wave 21 — IDE Control ✅ SHIPPED (2026-08)

> **The directive:** *control the IDE, analyze which IDE is it and adapt
> accordingly — along with execution and the Claude Code arms. A
> masterpiece.*
>
> Governance: [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) — the
> wiring law (every capability speaks on every surface), Law 1 (NL path
> in the same change).

---

## Why this wave

Wave 6 shipped IDE **analysis** — "what's wrong with main.py" answers
with LSP/AST diagnostics — and the controller primitives
(`open_file` / `reveal` / `run_command` in `desktop/ide/controller.py`),
but the *control* half was only reachable through `friday4 ide open` /
`reveal`. The **natural-language** path stopped at diagnostics:

- "open main.py in the editor" → **desktop web-search** ("main.py" was
  not an app!), never the editor.
- "jump to line 42 of cli_talk.py" → unknown/ask.
- "reveal auth.py" → unknown.
- "open main.py and fix it" → the "fix" was silently dropped if the
  phrase reached the IDE path — work the Claude arms should own.

The user's ask was unambiguous: Friday should *drive* the editor from
the same one NLU point as everything else, adapt to whichever IDE is
actually there, and hand repair-work to the Claude Code arms.

---

## What shipped

### 1. Editor-control NLU (`nlu/intent.py`)

- **IDE control keywords** join the diagnostic ones: "jump to line",
  "go to line", "take me to line", "reveal", "in the editor", "in the
  ide", "in vscode", …
- **Source-file tie-break:** a leading `open`/`show`/`go`/`jump`/…
  verb + a *source-file target* (whitelisted extension: py, js, ts,
  rs, go, c, md, json, …) wins IDE over desktop — **unless** the
  utterance reads like work, in which case the task override (Wave 20)
  already routes it to the brain:
  - `open main.py in the editor` → IDE (open)
  - `jump to line 42 of cli_talk.py` → IDE (reveal)
  - `open main.py and fix it` → **EXECUTE → Claude Code** (fix/debug/
    repair/rewrite added to the task verbs — a silent "open" that drops
    the task is now impossible)
  - `open brave` / `open youtube.com` → **desktop** (no hijack — .com
    isn't a source extension)
- `_ide_target` / `_ide_line` / `_ide_control_verb` — extract the file,
  the line ("line 42 of X", "X:42"), and whether the ask is open,
  reveal, or diagnosis ("show me the errors in main.py" stays analysis).

### 2. The router controls the editor (`nl_router.py`)

`_ide_response` now dispatches:

| Utterance | Action | Result |
|-----------|--------|--------|
| "open src/main.py in the editor" | `controller.open_file(ide, path)` | opens in the detected IDE |
| "jump to line 42 of cli_talk.py" | `controller.reveal(ide, path, 42)` | reveals the line |
| "reveal auth.py:7" | `controller.reveal(ide, path, 7)` | line from `file:line` |
| "reveal auth.py" (no line) | `controller.open_file(ide, path)` | opens the file |
| "what's wrong with main.py" | LSP/AST analysis (Wave 6) | unchanged |
| "open main.py and fix it" | EXECUTE → Claude Code gate | the arms take it |

Every control is **adapted to the detected IDE** (`detect()` →
VS Code `code -r/-g`, JetBrains `--line`, Neovim `+N`, Sublime
`file:line`, Emacs `+N`; OS opener as fallback) and **never crashes**
(missing file/editor → honest reply). Because voice, CLI, web, and
phone all route through `TextCommandHandler`, the control works on
**every surface** with zero per-surface wiring.

### 3. Composition with the Claude arms (no dropped work)

"open X and fix it" style utterances classify EXECUTE and flow through
the gated Claude Code executor — the file gets fixed, not just opened.
The repair verbs (fix / debug / repair / rewrite / optimize / tune)
were added to the Wave 20 task-verb list so the IDE tie-break can never
swallow a task behind a bare "open".

---

## Verified live (2026-08, VS Code detected on this machine)

```
detected IDE: VS Code (vscode)
open:   (True, 'opened nl_router.py in VS Code')
reveal: (True, 'revealed nl_router.py:42 in VS Code')
```

---

## Tests

`tests/test_ide_control.py` (24 hermetic — tmp DB, fake detection, fake
controller, tmp files):

- Classifier: control + diagnostic phrases → IDE; "open brave" /
  "open youtube.com" / "open the editor" stay desktop; "open main.py
  and fix it" → EXECUTE.
- Parsing: open target, "line 42 of X", "X:42", diagnostics-not-control.
- Router: open adapts to the detected IDE, jump-to-line reveals with
  the line, no-line reveal opens, missing file is honest, no-file asks
  which file, diagnostics untouched, "open the editor" → desktop focus.

**Count:** 1282 → **1306** tests (full suite).

## Close-out

Wave 6 made Friday *read* the editor; Wave 21 makes Friday *drive* it —
"open main.py in the editor", "jump to line 42 of cli_talk.py" —
adapted to whichever IDE is actually there, on every surface, with
repair-work handed to the Claude Code arms instead of being dropped.
ROADMAP / MASTER_PLAN updated; docs site rebuilt.
