# Wave 22 — Agent Bridge & Anywhere Access ✅ SHIPPED (2026-08)

> **The directive:** *type `CLAUDE:` in the phone/PWA chat and have it
> handed to one persistent Claude Code session — context accumulating
> until `CLAUDE END` — with Claude's tool-permission asks surfacing in
> the PWA for approval. And: use Friday from your phone from anywhere
> in the world, free.*
>
> Governance: [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) — the
> wiring law (one session, every surface), the never-crash law (no SDK
> → neutral message, never a raise), Law 1 (NL path in the same change).

---

## Why this wave

Two gaps, one wave.

**1. The one-shot handoff wasn't a session.** Wave 18/20 route
agentic work through `claude -p` — a one-shot subprocess with no memory
and no permission routing. The operator's other assistant (Claude
Code) is a *working session*: it keeps context, asks before dangerous
tools, and does real work across many turns. The phone/PWA chat had no
way to hold that session — `CLAUDE:` was the obvious bridge surface.

**2. "Use Friday from my phone" stopped at the LAN.** The companion
server always runs on the PC (that's where the power lives — desktop
control, executors, the bridge). The phone had to be on the same
Wi-Fi. From anywhere in the world, there was no path — and no way to
expose one *safely* even if there was (the mobile API had zero auth).

This wave delivers both: a persistent bridged Claude session behind the
chat, and a free, *safe* remote-access story (Tailscale / Cloudflare
tunnel + an optional bearer token gating the API).

---

## What shipped

### 1. The CLAUDE: bridge (`agent/`)

- **`agent/bridge.py` — `ClaudeBridge`:** ONE long-lived Claude Code
  session (the Agent SDK spawns the same `claude` CLI, same 9router
  settings, same `~/.claude/settings.json`). `CLAUDE: <text>` in the
  companion chat forwards to that session (constant `session_id`, so
  context accumulates); `CLAUDE END` closes it; the next `CLAUDE:`
  starts fresh. A daemon thread runs the SDK's event loop; HTTP
  handler threads enqueue prompts thread-safe.
- **`agent/permissions.py` — the permission weave:** the SDK's
  `can_use_tool` callback records a durable `permission_requests` row
  (source=`bridge`), publishes an IMPORTANT ambient event (the PWA's
  Live feed shows "Claude Code wants to Bash: …"), and blocks the tool
  call on an asyncio future. "yes, run it" / "no" from **any** surface
  resolves it through the normal `AutonomyAgent.accept/deny` path —
  which special-cases `source=="bridge"` to resolve the SDK future
  instead of executing a shell command.
- **Routing:** `POST /api/talk` — text starting `CLAUDE:` (or exactly
  `CLAUDE END`) routes to the bridge; everything else is untouched
  (the one NLU point). `GET /api/agent/status` reports
  available/active/busy for the PWA badge.
- **Never-crash:** `claude_agent_sdk` is a lazy import. Not installed →
  `available()` is False, `send()` returns a neutral message, the PWA
  shows the fallback. The bridge is hermetic-tested against a fake SDK.

### 2. Anywhere access (free) — `friday4 mobile remote` + token auth

- **`friday4 mobile remote`** prints every way to reach Friday from
  the phone: the PC's LAN IPs (auto-detected), the Tailscale 100.x URL
  (auto-detected when `tailscale` is installed), and the free
  Cloudflare quick-tunnel one-liner (`cloudflared tunnel --url
  http://127.0.0.1:8900`) — plus token guidance. That's the answer to
  "anywhere in the world, free": Tailscale (encrypted, no
  port-forwarding) or a public tunnel.
- **Optional bearer token** (`friday4 mobile serve --token <secret>`
  or `FRIDAY_V4_MOBILE_TOKEN`): when set, every `/api/*` route
  requires `Authorization: Bearer <token>` (or `?token=` — the SSE
  stream can't set headers via EventSource). The PWA shell stays
  public (it must load for the operator to enter the token); the API
  — *the power* — is gated. This is what makes a public tunnel safe.
- **Client support:** the PWA gets a TOKEN card on the Status tab
  (stored locally, sent as Bearer on every call); the native Expo app
  gets a token field on StatusScreen (persisted in AsyncStorage, sent
  by `ApiClient` and `useEvents`).

### 3. Always-on + always-reachable: tray, autostart, one-command expose

- **`friday4 mobile serve --tray`** — a system tray icon (like 9router):
  menu = Open dashboard / Show remote URLs / Pair a device / Status /
  Quit(stop server). Reuses the Wave-2 `SystemTray` (pystray), which got
  a **real bug fixed**: pystray's X11 backend encodes the tooltip as
  latin-1, so the em-dash in "Friday — running" crashed icon build with
  `UnicodeEncodeError`; a `_ascii()` sanitizer now maps non-ASCII to
  safe chars, and pystray's own noisy "Failed to dock" logger is
  silenced so a tray-less session degrades silently (server keeps
  serving — verified live).
- **`friday4 mobile autostart`** — writes `~/.config/autostart/
  friday4-mobile.desktop` (XDG-aware, `chmod 700` because the Exec line
  can carry `--token`), mirroring 9router's autostart pattern: on every
  login, `friday4 mobile serve --host 0.0.0.0 --tray` starts — Friday is
  always up and always in the tray. `friday4 mobile no-autostart`
  removes it idempotently. `--token`/`--tunnel` bake into the entry.
  The Exec is quoted so space-containing paths (`~/Projects/Friday
  V3/…`) don't split the command.
- **`friday4 mobile serve --tunnel cloudflare`** — spawns
  `cloudflared tunnel --url http://127.0.0.1:<port>`, parses the
  `https://….trycloudflare.com` URL from its output and prints it (the
  free no-account public URL), terminates the child on shutdown, and
  keeps draining its output so a full pipe never blocks it. Missing
  `cloudflared` → a hint, never a crash.
- **`--host` accepts what `remote` prints** — the operator pasted
  `--host 100.74.85.17:8900/` (the URL `remote` prints) and got
  `Name or service not known`. `_normalize_bind()` now accepts a bare
  address, `host:port`, a full `http(s)://host:port/path` URL, trailing
  slashes, and bracketed IPv6. Regression-tested.

### 4. The wiring law, honored

- **Every surface:** `CLAUDE:` works from the PWA and the native app
  (both post to `/api/talk`); bridge asks resolve from voice/CLI/web
  too ("yes, run it" is surface-independent).
- **Daemon:** nothing new to schedule — the bridge runs inside the
  mobile serve process; the push daemon already drains ambient events
  (including bridge progress) to the phone.
- **CLI surface:** `friday4 mobile remote` + `--token` on `serve`.
- **Hermetic tests:** `tests/test_agent_bridge.py` (22 tests — session
  lifecycle, prefix parsing, permission weave, autonomy hook, PWA
  round-trip, degraded no-SDK path) and `tests/test_wave7_mobile.py`
  (token gate: 401 without/wrong token, 200 with Bearer or `?token=`,
  PWA shell public, open-by-default on LAN, `remote` LAN/Tailscale/
  degrade/token-aware).

---

## MCU acceptance

| Test | Result |
|------|--------|
| `CLAUDE: list files in cwd` → one persistent session streams to the Live feed | ✅ (SDK verified live in the workspace: model fable → oc/deepseek-v4-flash-free via 9router, PONG returned) |
| `CLAUDE: <more>` → same context, second prompt | ✅ (constant `session_id`) |
| Tool ask → PWA "May I Bash: …?" → "yes, run it" → tool runs inside the session | ✅ (durable ask + future resolution) |
| `CLAUDE END` → session closes, next `CLAUDE:` is fresh | ✅ |
| No SDK installed → "install claude-agent-sdk" neutral message, no crash | ✅ |
| `friday4 mobile remote` from anywhere in the world story | ✅ (LAN IP detected live; Tailscale + tunnel guidance) |
| Token-gated API over a public tunnel | ✅ (401 without, streams with) |

---

## How to use it

```bash
# Bridge — the PWA/phone chat:
#   CLAUDE: refactor the auth module
#   (Claude works; watch the Live feed; approve tool asks inline)
#   CLAUDE END

# Anywhere access:
friday4 mobile serve --host 0.0.0.0 --token "$(openssl rand -hex 16)"
friday4 mobile remote          # prints LAN / Tailscale / tunnel URLs

# Free anywhere path (no port-forwarding): install Tailscale on the PC
# and the phone, then enter http://<100.x.y.z>:8900 in the app's
# Status tab. Or, no account: run `cloudflared tunnel --url
# http://127.0.0.1:8900` beside the server and enter the https://…
# trycloudflare.com URL. Both free; the token keeps the power gated.
```

The bridge needs `claude-agent-sdk` (optional extra: `pip install
-e '.[agent]'`).
