# Presentation & Interface — Prompt for Claude Code

## Intent
FRIDAY shows Tony holograms — rotating schematics, HUD overlays, visual data that he can reach into. Your Friday has a Rich TUI dashboard (command_center.py) and ambient feed, but it's text-only on a terminal. The gap: Friday needs a **visual presence** that matches the sophistication of its internal architecture.

This covers: HUD-style terminal notifications, architecture visualization, web interface, mobile companion, terminal inline mode, rich interactive reports.

## What to build

### Phase 1: HUD-Style Terminal Notifications

Your current notification system writes to the ambient feed and sends to Telegram/Slack/Discord. But if the operator is in a terminal, there's no **heads-up display** — no subtle overlay, no status bar, no compact heads-up.

Build into `src/friday/presentation/hud.py`:

**Features:**
- A persistent terminal status line (like tmux status bar) that shows:
  - Friday status: 🟢 watching / 🟡 analyzing / 🔴 error / 💤 idle
  - Presence state: 🧘 focus / 🪑 at desk / 🚶 away
  - Active watchers count: 👁 3
  - Pending items: 📨 2
  - Last cycle: 34s ago
- Non-blocking popup notifications in the terminal (like Telegram toast):
  - Appears in a floating panel for 5s, then disappears
  - Shows: watcher fired, build status change, resource alert
  - Doesn't disturb what the user is typing
- Configurable: `friday hud on` / `friday hud off` / `friday hud compact` / `friday hud full`

**Implementation:**
- Uses `rich.live` with a separate rendering thread (like dashboard already does, but minimal)
- Detects if terminal supports ANSI escape codes (if not, disable)
- The status line is always rendered on the last line of the terminal
- Popups render above the status line, covering the last 3 lines for 5s
- When the user is typing (stdin has pending input), popups are suppressed

### Phase 2: Architecture Visualization

Create `src/friday/presentation/arch_viz.py`.

**Features:**
- `friday viz arch` → renders an ASCII architecture diagram of the workspace or a specific project
- `friday viz deps` → renders a dependency graph (project → project dependencies)
- `friday viz timeline` → renders a visual timeline of today's activity
- `friday viz impact <symbol>` → renders the impact tree visually

**Output formats:**
- Terminal: Rich Tree + Table (already available via Rich)
- Image: if `graphviz` or `asciidoctor` available, generate SVG/PNG
- Mermaid: generate Mermaid.js markup that can be rendered in GitHub or any Mermaid viewer
- Fallback: nested indented tree (no special rendering)

**Key design:**
- All visualization is deterministic — no LLM
- Generates structured intermediate data first (nodes + edges), then renders
- Each format is a separate renderer — add new renderers without changing the data model
- `friday viz arch --format mermaid` → Mermaid output, `--format tree` → ASCII tree, `--format image` → PNG

### Phase 3: Web Interface

Create a lightweight web server in `src/friday/presentation/web/` that serves the dashboard as a web app.

**Features:**
- Local web server (Flask or FastAPI or just stdlib `http.server` with JS)
- Shows: ambient feed (live-updating), system health, telemetry, watchers, processes, missions
- Real-time updates via Server-Sent Events (SSE) — no WebSocket dependency needed
- Minimal JS — mostly server-rendered HTML with SSE for live updates
- `friday web` → starts server on `localhost:8321` (or configurable port)
- `friday web --open` → starts server and opens browser

**Implementation:**
- Use stdlib `http.server` + Jinja templates OR minimal FastAPI if already available
- SSE endpoint pushes new ambient events, telemetry updates, and status changes
- Pages:
  - `/` — dashboard overview (like command_center.py)
  - `/feed` — ambient feed with filtering
  - `/telemetry` — live system vitals
  - `/watchers` — watcher management
  - `/missions` — mission overview
- Auth: bind to localhost only (no auth needed). For remote access, the operator uses SSH tunnel.
- Auto-refresh via SSE — no polling

**Key design:**
- The web server is a separate process (not embedded in the daemon) — run via `friday web`
- Zero new required dependencies — if Flask/FastAPI not installed, use stdlib only
- All data comes from the DB — the web server is a READ-ONLY view (no mutations through the web interface in v1)

### Phase 4: Mobile Companion

Not a full mobile app — that's a separate project. The v1 approach is a **Progressive Web App (PWA)** served by the web interface:

- Add a PWA manifest + service worker to the web server
- Supports: push notifications (via a lightweight WebSocket or SSE relay), ambient feed view, watcher acknowledge
- The PWA is just the web interface + manifest + service worker — no native code

If a full mobile app is desired later, the web API is already there.

### Phase 5: Terminal Inline Mode

Currently, Friday is a CLI that prints output and exits. Add an **inline mode** where Friday runs in the terminal as a persistent REPL-like overlay:

- `friday inline` → enters inline mode
- The terminal splits: bottom 3 lines are Friday's input line, above is the conversation
- Commands are typed naturally ("what's the status?", "deploy the app", "who am I?")
- Friday responds inline without leaving the current working context
- This is different from `friday chat` which is a conversation loop — inline mode keeps your shell context visible

**Implementation:**
- Uses `prompt_toolkit` if available, else `readline` + raw input
- The input area supports: history (arrow up/down), tab completion (repo names, command names)
- Above the input area, the last 20 exchanges are shown in a scrollable region
- Signals: Ctrl+C clears input, Ctrl+D exits inline mode, Ctrl+L clears the conversation display

### Phase 6: Rich Interactive Reports

Create `src/friday/presentation/reports.py`.

**Features:**
- `friday report daily` → rich HTML report for the day (emailable)
- `friday report weekly` → weekly engineering summary
- `friday report impact <symbol>` → impact analysis as a formatted document
- Reports are: HTML (for email/web), Markdown (for GitHub), or PDF (if docx/pdf skill is available)

**Key design:**
- Reuses `briefing.py` for daily report content
- Reports are generated on demand, not automatic
- Each report format is a separate renderer (HTMLReportRenderer, MarkdownReportRenderer, etc.)
- Reports include: charts (ASCII sparklines in terminal, SVG in HTML)

## Files to touch
- `src/friday/presentation/hud.py` (new) — terminal HUD overlay
- `src/friday/presentation/arch_viz.py` (new) — architecture/dependency visualization
- `src/friday/presentation/web/` (new directory) — web server app
- `src/friday/presentation/web/app.py` — Flask/FastAPI/stdlib routes
- `src/friday/presentation/web/templates/` — server-rendered HTML
- `src/friday/presentation/web/static/` — minimal CSS/JS for SSE + PWA
- `src/friday/presentation/reports.py` (new) — rich report generation
- `src/friday/cli.py` — add `friday hud`, `friday viz`, `friday web`, `friday inline`, `friday report` commands
- `src/friday/db.py` — shared data access (already exists)
- `pyproject.toml` — optional deps: `jinja2`, `pillow`, `graphviz`
- `tests/test_hud.py` (new)
- `tests/test_arch_viz.py` (new)
- `tests/test_web.py` (new)

## Acceptance criteria
1. `friday hud on` → persistent status bar at bottom of terminal with Friday status
2. Watcher fires while you're in terminal → floating popup appears for 5s, then disappears
3. `friday viz arch --format tree` → ASCII tree of project structure
4. `friday viz deps --format mermaid` → Mermaid graph markup
5. `friday web` → localhost:8321 serves the dashboard with live ambient feed via SSE
6. `friday inline` → REPL-style overlay with history and tab completion
7. `friday report daily` → HTML report with today's summary, formatted and ready to share
8. HUD doesn't break when terminal is resized
9. Web interface is usable on mobile (responsive design or PWA)
10. All features degrade gracefully — no required runtime dependencies beyond what's installed
