# Friday Companion — native app (Expo / React Native)

The phone as another surface of the same Friday. Four tabs:

- **Status** — transport health, shared-session summary, server URL, push
- **Chat** — the ONE presence thread, read + appended through the same
  `nl_router` brain as talk/voice/web (so "open brave" from the phone
  opens it on the PC, and "what did we talk about this morning?" resumes
  the terminal conversation)
- **Feed** — live ambient event stream over the SSE durable queue
- **Devices** — one-time-code pairing + paired-device management

The companion **server** is `friday4 mobile serve` (pure-stdlib HTTP, port
8900). This repo folder is the native client for it.

## Quick start (development)

```bash
cd mobile/app
npm install          # installs Expo SDK 54 + the pinned deps
npx tsc --noEmit     # typecheck (the API contract is typed in src/api.ts)
npx expo start       # Expo Go on a device on the same Wi-Fi, or a simulator
```

Point the app at your PC: on the **Status** tab, set the server URL to
`http://<PC-LAN-IP>:8900` (the phone must reach the PC; `friday4 mobile
serve` prints the LAN URL). Then pair on the **Devices** tab with the
one-time code from `friday4 mobile pair`.

## Background push (the real native payoff)

Remote push needs **a development build** (Expo Go cannot receive remote
notifications) and a **physical device** (simulators cannot receive
remote push either):

1. Create an **EAS project** (free) — `npx eas-cli init` — and put its
   `projectId` into `app.json` → `expo.extra.eas.projectId` (replace
   `REPLACE_WITH_YOUR_EAS_PROJECT_ID`).
2. Build a dev client: `npx eas build --profile development --platform android`
   (or iOS). Install it on your phone.
3. `npx expo start --dev-client`, pair the phone, and the daemon's
   `MobilePushWorker` fans ambient events out via Expo's push service.

## Account & store checklist (shipping)

| Item | Cost | Notes |
|------|------|-------|
| EAS account (`expo.dev`) | Free | Needed for any cloud build + push tokens |
| Android — Google Play developer | $25 one-time | `eas build -p android --profile production` → Play Console upload |
| iOS — Apple Developer Program | **$99/yr** | Required for ANY iOS device install or App Store release (`eas build -p ios`) |
| iOS push | included | APNs via Expo; needs the Apple dev account |
| Firebase Cloud Messaging | Free | Android remote push goes through FCM (Expo manages the integration) |

Android is the cheap path to try push (Play $25 once). iOS cannot be
tested on a real iPhone without the $99/yr account.

## Contract validation

`src/api.ts` types every endpoint the app calls. The Python side is
hermetic-tested in `tests/test_wave7_mobile.py`, including the SSE
stream (regression: the stream must deliver queued events, and stay
idle — never crash — when empty). Run it with:

```bash
cd <repo root> && .venv312/bin/python -m pytest tests/test_wave7_mobile.py -q
```

## Files

```
mobile/app/
  App.tsx                four-tab shell + server-URL persistence
  index.ts               Expo entry (registerRootComponent)
  src/api.ts             typed client for the companion API (the contract)
  src/useEvents.ts       SSE hook over the durable queue (fetch streaming)
  src/push.ts            Expo push token registration (physical device)
  src/theme.ts           FRIDAY theme tokens
  src/screens/           Status · Chat · Feed · Devices
```
