# Friday V4 — Web UI Requirements (for Gemini)

> Copy-paste this document (or the "Build prompt" section at the end) into
> Gemini to build the Friday V4 web dashboard. It describes exactly what
> exists on the backend today so the UI can be built against the real API —
> don't invent new endpoints unless a new one is explicitly listed.

---

## 1. Product context

Friday V4 is a local-first AI "operating partner" that runs on the user's
machine. It ships as a Python package (`friday4` CLI) with:

- **Daemon** — background service (observers, proactive intelligence,
  periodic security scans, desktop notifications).
- **Voice** — wake-word + STT/TTS conversation ("Hey Friday, what's new?").
- **Desktop** — window manager control (focus, workspaces, screenshots,
  tray icon, notifications) across Hyprland/GNOME/KDE/macOS/Windows.
- **Security** — a Wave-3 vulnerability scanner + quality gate that grades a
  project folder (A–F) and notifies on findings.
- **Proactive intelligence** — pattern learner + drift/anomaly detection.
- **V3 bridge** — a read-only connection to the older "Friday V3" data
  sources (ambient event feed, observations). V3 is a data source only;
  V4 is the product.

The web dashboard is the visual surface of all of this: a browser page that
shows daemon health, security status, intelligence, proactive patterns, the
V3 bridge, voice config, and the ambient feed — with a "run security scan"
action. It already exists in a functional-but-basic form; this spec upgrades
it to a polished, product-grade UI.

## 2. Hard constraints (non-negotiable)

1. **Local-first.** The page is served from `http://127.0.0.1:8899` on the
   user's own machine. No cloud, no telemetry, no login.
2. **Pure-stdlib Python backend.** The server is `http.server` based — no
   FastAPI/Flask/Django. The frontend can use any framework **but must work
   fully offline**: no CDN scripts/fonts; everything (CSS, JS, icons, fonts)
   is served by the same local server or inlined.
3. **Graceful degradation.** Every subsystem (daemon, security, intelligence,
   proactive, V3, voice) may be unavailable. The UI must render an
   informative empty/offline state per card — never a broken page or a 500.
4. **Read-only V3.** The UI must never write to V3. V4 stays the product.
5. **Security.** The server binds localhost by default. The UI sends no data
   anywhere except the local API.
6. **Consistent aesthetic.** Dark, technical, "command center" look (see
   Design system). This is a developer tool — it should feel like an Iron
   Man/JARVIS control panel, not a SaaS marketing page.

## 3. Backend API contract (what you build against)

All endpoints are `GET` unless noted. Responses are JSON. Every payload is
already guarded server-side — missing subsystems come back as empty/neutral
fields, **not** HTTP errors (except the explicit error cases noted).

### `GET /api/overview` — everything for the main screen

```jsonc
{
  "daemon": {
    "running": true,             // pid liveness
    "state": "running",          // status-file state
    "uptime_seconds": 3600.0,
    "notification_count": 42,
    "components": {              // per-component up/down
      "notifier": true,
      "proactive": true,
      "intelligence": true,
      "security": true
    }
  },
  "security": {
    "scans": 7,
    "last_error": null,          // string when last scan failed
    "report": {                  // null when no scan yet
      "grade": "A",              // "A"|"B"|"C"|"D"|"F"
      "score": 100,              // 0–100
      "counts_by_severity": { "critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0 },
      "scanned_at": "2026-08-01T12:00:00"
    }
  },
  "intelligence": {
    "available": true,
    "drift": { "metrics_tracked": 5, "total_samples": 1200 },
    "anomaly": {
      "anomalies_logged": 8,
      "recent_anomalies": [
        { "category": "dirty_repos", "z_score": 3.2, "timestamp": "2026-08-01T11:00:00" }
      ]
    }
  },
  "proactive": {
    "available": true,
    "sessions": 12,
    "patterns": { "action_pairs_learned": 34, "state_transitions": 156 }
  },
  "v3": {
    "available": true,
    "counts": { "by_source": { "terminal": 8, "github": 2 } },  // 24h observation counts
    "daemon_state": "running",
    "ambient_recent": [                                        // newest first, ≤12
      { "title": "3/8 repos changed", "event_type": "repo_changed",
        "timestamp": "2026-08-01T11:30:00", "priority": 2, "project": "codebuff", "detail": "" }
    ]
  },
  "voice": {
    "available": true,
    "tts_provider": "edge-tts",
    "hotword": "disabled"
  }
}
```

### `GET /api/security` — last scan only

Same `security` object as in overview (for deep-linking / a dedicated view).

### `GET /api/ambient` — recent V3 ambient events

```jsonc
[ { "title": "...", "event_type": "...", "timestamp": "...",
    "priority": 1, "project": "...", "detail": "..." } ]
```

### `GET /api/projects` — scan-path suggestions

```jsonc
[ "/home/you/code/project-a", "/home/you/code/project-b" ]  // ≤40 entries
```

### `POST /api/scan` — trigger a security scan

- Body: `{ "path": "/abs/or/~/path" }` (omit or `"."` to scan server cwd).
- Success: `200 {"started": true}` — scan runs async; refresh overview to
  see the new grade.
- Errors: `400 {"started": false, "error": "not a directory: …"}` for a bad
  path; `409 {"started": false, "error": "scan already running"}` when busy.
- The scan button must disable + show "scanning…" until the next poll
  returns (give it a few seconds).

### `GET /` — the page itself

The single-page app is served here. Everything else (assets) must also be
served locally — do **not** reference CDNs.

## 4. Screens / views

The dashboard is one SPA. Recommended structure (adapt freely, but keep all
these sections reachable):

### 4.1 Global chrome
- Top bar: `◆ FRIDAY` wordmark + `V4 · DASHBOARD` tag, a **daemon status
  pill** (green pulsing dot "daemon running" / red "daemon stopped"), and a
  "last updated HH:MM:SS" timestamp that refreshes on every poll.
- Sidebar or top-nav with sections: **Overview · Security · Intelligence ·
  Proactive · V3 / Ambient · Voice · About**. Active section highlighted.
- Footer: `friday4 web · local-first · data from your machine only`.

### 4.2 Overview (default landing)
- Daemon card: running/stopped, uptime (humanized: `1h 5m`, `45s`), number of
  notifications raised, per-component up/down chips (notifier, proactive,
  intelligence, security).
- Security card: big grade letter (A–F, color-coded) + score /100, scan
  count, last scan time, per-severity count chips (critical/high/medium/low/
  info, colored, only non-zero shown, "clean — no findings" when empty).
- Intelligence card: metrics tracked, drift samples, anomalies logged, and a
  small list of recent anomalies (category, z-score, timestamp).
- Proactive card: sessions, action pairs learned, state transitions.
- V3 bridge card: available yes/no, daemon state, 24h observation counts by
  source.
- Voice card: TTS provider, hotword state.
- Ambient feed card: the 10 most recent events (title, time).

### 4.3 Security view
- Big grade panel (letter + score + counts) as the hero.
- **Run scan** panel: text input with datalist suggestions from
  `/api/projects`, a "run security scan" button, inline error/success
  message, busy state while scanning.
- Findings detail: render the report's severity counts; if `last_error`
  present, show it prominently.
- (Backend note: a full per-finding list is not in the API today — render
  what the report provides. Do not fabricate a findings table from nothing.)

### 4.4 Intelligence view
- Drift stats (metrics tracked, samples) and anomaly history (recent
  anomalies list with z-score + time). Empty state when none logged.

### 4.5 Proactive view
- Session count, pattern stats. Empty state when no sessions yet.

### 4.6 V3 / Ambient view
- Bridge availability, daemon state, observation counts by source, and the
  full ambient feed (timestamps, priorities, projects). Empty state when V3
  is absent ("V3 not installed — this view is a read-only bridge").

### 4.7 Voice view
- Provider + hotword config readout. If unavailable, show a setup hint
  ("install a TTS provider to enable voice").

### 4.8 About view
- Version, the four waves shipped (Voice, Desktop, Security, Smart), the
  roadmap pointer, and the "local-first" statement.

## 5. Interaction requirements

1. **Live refresh.** Poll `/api/overview` every ~10s (the current page does).
   Show the "updated HH:MM:SS" timestamp. When the daemon flips running↔
   stopped, the pill + daemon card must update without a full reload.
2. **Run scan.** As specified in 4.3: debounce/disable double-submit, handle
   400 (bad path → show error under the input) and 409 (scan already
   running → informational), then re-poll and show the new grade.
3. **Navigation.** Client-side (no full page loads). Deep-linkable sections
   via `#/security` style hashes is a nice-to-have.
4. **Empty & offline states.** Every card must handle: no data yet ("no
   scans run yet — run one"), subsystem missing ("intelligence store not
   available"), connection lost (server down → show a connection-error
   screen with "is `friday4 web` still running?").
5. **Micro-interactions.** Hover states on buttons/chips/cards, subtle
   transitions, pulsing live-dot for daemon. Keep them fast and quiet — no
   long animations.

## 6. Design system

- **Palette** (from the current page — keep it, it's good):
  `--bg: #0a0e17`, `--panel: #111a2c`, `--panel2: #0d1524`,
  `--line: #1e2b44`, `--text: #dfe6f3`, `--dim: #7c8aa5`,
  `--cyan: #38d4f5`, `--green: #3ddc97`, `--red: #ff5c6c`,
  `--amber: #ffb454`, `--yellow: #ffd166`.
- **Type:** monospace-first (`SF Mono / JetBrains Mono / ui-monospace`) —
  it's a developer tool. Headings uppercase with wide letter-spacing.
- **Layout:** responsive card grid (`repeat(auto-fit, minmax(320px, 1fr))`),
  generous padding, rounded 12px cards with subtle gradient
  (`linear-gradient(180deg, var(--panel), var(--panel2))`) and 1px borders.
- **Background:** deep-navy radial gradient, dark at the bottom.
- **Status language:** LED dots (ok=green glow, warn=amber, bad=red), chips,
  severity badges. Grade A/B green, C yellow, D amber, F red.
- **Logo:** `◆ FRIDAY` diamond glyph in cyan.

## 7. Accessibility & quality

- Semantic HTML, `aria-label`s on icon-only controls, keyboard-navigable
  buttons/inputs.
- Contrast: text on panel backgrounds must pass (the palette is already
  high-contrast).
- No console errors. All fetches wrapped in try/catch with a visible
  fallback (never a white screen).
- The page must render and work with **JavaScript disabled? No** — it's a
  JS SPA; but it must fail *visibly* and gracefully, not silently.
- Responsive: usable from ~360px wide (phone in a browser) to ultrawide.

## 8. Out of scope (do not build)

- Login/auth, cloud sync, multi-user.
- Mobile app, push notifications.
- Editing V3 data or any backend state beyond triggering scans.
- Rewriting the backend server or adding frameworks to it.

## 9. Definition of done

- All 8 views render correct data from the real API.
- Every subsystem can be unavailable without a broken page.
- Scan flow works end-to-end (trigger → busy → updated grade).
- Fully offline: no CDN requests in the network tab.
- Responsive + accessible + no console errors.
- Visual design matches the palette and feels like a polished command
  center, not a prototype.

---

## Build prompt (paste this into Gemini)

> Build a single-page web dashboard for "Friday V4", a local-first Python AI
> assistant. It is served from http://127.0.0.1:8899 by a pure-stdlib Python
> http.server — you are ONLY building the frontend (one HTML page + inline
> CSS/JS, no build step, no CDN, fully offline).
>
> The page is a dark, technical "command center" (palette: bg #0a0e17,
> panels #111a2c/#0d1524, border #1e2b44, text #dfe6f3, dim #7c8aa5,
> accents cyan #38d4f5 / green #3ddc97 / red #ff5c6c / amber #ffb454 /
> yellow #ffd166; monospace font; rounded 12px cards; cyan "◆ FRIDAY"
> wordmark with "V4 · DASHBOARD" tag).
>
> Backend API (build against these exact shapes — every field is guarded,
> missing subsystems return empty values, never HTTP 500):
> - GET /api/overview → {daemon:{running,state,uptime_seconds,notification_count,components:{name:bool}}, security:{scans,last_error,report:{grade,score,counts_by_severity:{critical,high,medium,low,info},scanned_at}}, intelligence:{available,drift:{metrics_tracked,total_samples},anomaly:{anomalies_logged,recent_anomalies:[{category,z_score,timestamp}]}}, proactive:{available,sessions,patterns:{action_pairs_learned,state_transitions}}, v3:{available,counts:{by_source},daemon_state,ambient_recent:[{title,event_type,timestamp,priority,project,detail}]}, voice:{available,tts_provider,hotword}}
> - GET /api/security → same security object
> - GET /api/ambient → [ambient events]
> - GET /api/projects → [project dir strings]
> - POST /api/scan {path} → 200 {started:true} | 400 bad path | 409 already running
> - GET / → the page
>
> Sections (sidebar or top-nav, client-side navigation, no full reloads):
> Overview · Security · Intelligence · Proactive · V3/Ambient · Voice ·
> About. Overview shows daemon card (running pill, uptime, notification
> count, per-component up/down chips), security card (big color-coded grade
> A–F + score/100 + severity count chips + last scan time), intelligence
> card (drift + anomaly stats + recent anomalies), proactive card (sessions,
> patterns), V3 card (availability, daemon state, observation counts), voice
> card (provider, hotword), and an ambient feed (10 events).
>
> The Security view is the hero feature: a big grade panel plus a "run
> security scan" panel with a path input fed by /api/projects datalist
> suggestions, a scan button with busy/disabled state, inline 400/409 error
> handling, and a post-scan refresh showing the new grade.
>
> Requirements: poll /api/overview every 10s and show "updated HH:MM:SS";
> every card needs a graceful empty/offline state (no data yet / subsystem
> missing / server unreachable — never a white screen); all fetches
> try/catch'd with visible fallbacks; no console errors; responsive from
> 360px to ultrawide; semantic HTML + keyboard-accessible controls; hover
> states and subtle transitions; daemon running/stopped pill updates live.
> No CDN anything. Make it feel like an Iron Man control panel, not a SaaS
> page.
