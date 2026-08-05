# WAVE 7 — Mobile: Friday in Your Pocket

**Status:** ✅ **SHIPPED (2026-08)**
**Promise:** the phone is another surface of the *same* Friday — same
brain, same memory, same live events — not a separate product.
**Predecessor truth:** Wave 15 closed the *transport* stub (durable-queue
push, pairing registry, companion API). What was still missing — and what
this wave ships — is the **phone client itself**: the app that lives on
the operator's phone.

---

## What shipped

### 1. The companion PWA — the real phone app (`mobile/app/`)

`friday4 mobile serve` now serves the app at **`/`** (not just the API):
an installable, phone-first web app with four surfaces:

| Screen | What it does | API |
|--------|--------------|-----|
| **Chat** | The shared one-presence thread — a conversation started in the terminal continues here ("what did we talk about this morning?" works from the phone). Send through the same `nl_router` brain as talk/voice/web. **Desktop control included**: "open brave" from the phone focuses/launches Brave on your PC (the companion server runs on it), via the same `desktop_text_command` the CLI and web chat use. | `GET /api/conversation` · `POST /api/talk` |
| **Live** | The durable-queue push feed over SSE — security findings, suggestions, briefing land in real time with priority badges and an unread count. The cursor (`?since=`) is persisted, so a phone that was closed **replays what it missed** on reconnect. | `GET /api/events` |
| **Status** | Transport health, shared-session summary, exchanges today, pairing state. | `GET /api/status` |
| **Device** | Consent-first pairing: the operator types the 6-character code from `friday4 mobile pair`; the app registers itself with a locally-generated `pwa-` token. Unpair in-app. Install prompt (Add to Home Screen). | `POST /api/devices/register` · `touch` · `DELETE` |

Installability is real, not decorative: `manifest.json` (standalone,
dark, 192/512 + maskable icons), a **service worker** that caches only
the shell (every `/api/*` request passes straight to the network — live
state is never served stale from cache), and **generated PNG icons**
written by a pure-stdlib generator (`tools/gen_mobile_icons.py` — the
FRIDAY diamond rendered with `zlib`/`struct`, no Pillow).

Honest platform note: browsers only register a service worker over
**HTTPS or localhost**. Reached over a plain `http://<lan-ip>:8900` the
app still fully works — install simply degrades to the browser's
**"Add to Home Screen"** (a shortcut to the same PWA), which is exactly
what the in-app install button tells you.

### 2. Server-side wiring (`mobile/api.py`, `cli_mobile.py`)

- Static app files are served from a **fixed allowlist** (no path
  traversal possible); a missing `app/` dir degrades to an **API-only
  server**, never a crash (the never-crash law).
- `POST /api/devices/touch` — the app's liveness ping updates
  `last_seen` (honest: reports whether the device actually exists).
- **Phone → PC control (Wave 19 wiring):** `/api/talk` now threads the
  same `desktop_handler` as `friday4 talk` and the web chat, so
  "focus code editor", "open brave", "switch to workspace 2", and
  "what's on my screen" from the phone's Chat tab act on the PC the
  companion server runs on. Hermetic tests (`TestPhoneDesktopControl`)
  pin the exact POST → desktop-handler routing, and a missing/unavailable
  desktop degrades to an honest reply, never a crash.
- `friday4 mobile serve` prints the app URL (with `flush=True` — visible
  even when piped) and, on localhost, tells you to restart with
  `--host 0.0.0.0` + your machine's LAN IP to reach it from a phone.

### 3. Push honesty (`mobile/push.py`)

`fanout_transporter` now **skips non-Expo tokens**: a PWA device streams
events over its SSE connection, so the daemon never POSTs a bogus token
to exp.host. Real background push stays exactly where it belongs — a
paired **Expo token** from the native app.

### 4. The RN/Expo scaffold (`mobile/app-rn/`)

The literal ROADMAP promise, checked in as the **documented native path**
for *true background push*: `package.json` / `app.json` (Expo ~52, dark,
portrait, notifications), `App.js` (Chat · Live · Device tabs),
`src/api.js` (the exact server contract, plus a **fetch-based SSE reader**
— RN has no `EventSource`), and screens mirroring the PWA. The README is
explicit: it needs Node/Expo to build and **is not validated by this
repo's Python test suite** — the server contract it speaks is (hermetic
tests), the app itself is not.

---

## How to use it (the five-minute dogfood)

```bash
friday4 mobile serve --host 0.0.0.0        # on your machine (LAN)
# → open http://<lan-ip>:8900/ on your phone (or Add to Home Screen)
friday4 mobile pair                        # prints a 6-char code
# → Device tab in the app → type the code → paired
friday4 mobile devices                     # shows "Android Pixel · web-pwa"
```

The daemon keeps pushing important/critical events to paired **Expo**
tokens on its own schedule (`MobilePushWorker`); the PWA sees everything
live over SSE while it's open.

## The honest not-built list

- **The RN app is a scaffold, not a shipped binary** — building it needs
  Node/Expo/a device, none of which are part of this repo (same treatment
  as the Wave 6 VS Code extension). Its server contract is real and
  tested; the app itself is the next step for whoever runs `npm install`.
- **Voice input on the phone** — the PWA sends text through the same NLU
  brain; on-device speech-to-text is a future refinement (the voice
  pipeline already exists on the desktop side).
- **True background push to the PWA** — Web Push (Push API) or a
  notification-permission flow is future work; today the PWA's live
  channel is SSE while open, and background push is the native app's job.

## Tests

`tests/test_wave7_app.py` (15 hermetic tests) + the existing
`test_wave7_mobile.py` (tokens updated to real Expo format):

- PWA serving: root shell, asset content types, real-PNG icons (signature
  + IHDR dims), favicon, 404s, **API-only degrade when the app dir is
  missing**.
- Installability: manifest contract (standalone, maskable icons),
  service worker's `/api/` passthrough guard, every manifest asset
  servable, allowlist completeness, and the SSE feed's **reconnect
  contract** (the cursor is mutable + re-read at connect time, so a
  dropped stream reconnects with the latest `since` — never the
  boot-time one, never duplicate feed items).
- App-shape pairing: the PWA's exact register POST → device listed
  **without token leak**; touch (exists/doesn't) honesty; in-app unpair.
- Fanout guard: a paired PWA device is skipped; only the Expo token
  receives the push.

**Count:** 15 new → the full suite moves 1127 → **1142**.

## Close-out

Wave 7's promise — *Friday in your pocket* — is true in daily use: the
phone opens the same Friday as the terminal, dashboard, and voice; the
one presence follows you; live events reach the phone. MASTER_PLAN /
ROADMAP / PLAN updated to ✅ SHIPPED; docs site rebuilt.
