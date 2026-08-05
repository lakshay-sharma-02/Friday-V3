# Friday V4 — VS Code Extension

Friday lives inside your editor. The extension shells out to the
`friday4` CLI — the same gated, audited surface as `friday4 talk` — so
it adds no server, no new attack surface, and works whether or not the
Friday daemon is running.

## Features

| Command | What it does |
|---------|--------------|
| **Friday: Ask Friday…** (`Ctrl+Alt+T`) | Natural-language ask through the ONE NLU point — "open main.py in the editor", "what's wrong with auth.py", "run the tests", "my todo app is obsidian". Response in the Friday output channel. |
| **Friday: Diagnose This File** | `what's wrong with <current-file>` → LSP/AST diagnostics for the file you're editing. |
| **Friday: Open This File in the Editor** | `jump to line <N> of <current-file>` → the detected editor reveals the file at your cursor. |
| **Friday: Show Status** | `friday4 status` — the full layer overview. |
| **Learned Apps sidebar** | The activity-bar **Friday** view lists the apps Friday has learned ("todo app → obsidian"). Refresh button included. |
| **Status bar** | `$(sparkle) Friday` — click to ask; spins while Friday works. |

## Prerequisites

- Friday V4 installed (`friday4` on your shell `PATH`).
- Node 18+ (build only).

If `friday4` isn't on the PATH VS Code inherits (common on macOS GUI
launches), set `friday.binaryPath` to the venv binary, e.g.
`/home/you/Projects/friday_v4/.venv312/bin/friday4`.

## Build

```bash
cd desktop/ide/vscode-extension
npm install
npm run compile        # → dist/extension.js
```

## Install (dev)

1. Open VS Code → Extensions (`Ctrl+Shift+X`) → `…` → **Install from
   VSIX** after packaging, or:
2. **Run**: open this folder in VS Code and press `F5` (Extension
   Development Host).
3. Package a shareable VSIX: `npm run package` (needs `@vscode/vsce`).

## Safety

The extension never forces an action. If Friday's permission gate needs
confirmation (an execute intent), the extension reports it honestly and
points you to the terminal (`friday4 talk "…"`) to approve. `--json`
mode fails closed, exactly like the CLI.

## Design notes

- **Zero server dependency**: every command is a `friday4` subprocess —
  the documented surface, pure-stdlib, gated.
- **Never crashes**: CLI errors surface as honest messages in the
  output channel / notifications, matching the never-crash law.
- Wave 6's LSP client already gives Friday IDE *analysis*; this
  extension is the *presence* layer — Friday at your fingertips, with
  the same NL everywhere.
