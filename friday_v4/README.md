# Friday V4 🚀

> **An ambient, proactive, multi-modal AI operating partner.**
> Inspired by Tony Stark's FRIDAY.

## ⭐ Project Status: V4 Is the Main Project

**V4 is the project being actively built right now.**

- V3 is largely built but **inconsistent** — it is not the deliverable.
- V4 has its own roadmap (`docs/ROADMAP.md`) and is developed here on its
  own terms.
- V4 **imports only the V3 modules that are properly built** and useful
  (e.g. persona/IdentityEngine, ambient feed, db layer) — as a dependency,
  never as the foundation V4 is subordinated to.
- If a V3 module is missing or broken, the answer is to build it properly
  in V4 — not to patch V3 wholesale.

## Quick Start

V4 installs its CLI as **`friday4`** (aliased `friday-v4`) so it doesn't
clash with V3's `friday` command when both are installed side by side.

```bash
# Install V4 alongside V3
pip install -e friday_v4/

# Start a voice session (hotword mode, or hold Ctrl+Space with --push-to-talk)
friday4 talk
friday4 talk --push-to-talk

# Voice diagnostics / setup wizard
friday4 voice status
friday4 voice setup

# Desktop awareness & control
friday4 desktop status
```

## ⚠️ Running under ZCode (this machine)

ZCode's AppImage sets `APPIMAGE`/`APPDIR` env vars, which make **every**
`python` invocation report the AppImage as `sys.executable`. This breaks
venvs (bin/python symlinks point at the AppImage, site-packages never
loads, no numpy/voice deps). Always run python through the fixed venv
with those vars unset:

```bash
cd friday_v4
env -u APPIMAGE -u APPDIR .venv312/bin/python -m friday_v4.cli_talk talk
```

Never create venvs with the AppImage env vars set — they'll be unusable.
If a venv's `bin/python` is a symlink to `ZCode-*.AppImage`, rebuild it:

```bash
rm -rf .venv312
env -u APPIMAGE -u APPDIR /usr/sbin/python3.12 -m venv .venv312
env -u APPIMAGE -u APPDIR .venv312/bin/pip install -e . -e ../  # reinstall editable pkgs
```

## What's New in V4

| Feature | Status | Description |
|---------|--------|-------------|
| 🗣️ Voice Interface | Phase 1 | Speak to Friday, hear responses aloud |
| 🖥️ Desktop Integration | Phase 2 | Cross-platform WM control, system tray |
| 🔒 Security Scanning | Phase 3 | Vulnerability scanning, secret detection |
| 🤝 Collaboration | Phase 4 | Multi-instance team workspaces |
| 📱 Mobile Companion | Phase 5 | Phone app, push notifications |
| 🧠 Advanced Intelligence | Phase 6 | Drift prediction, anomaly detection |
| 📝 IDE Integration | Phase 7 | VS Code, IntelliJ extensions |

## Architecture

V4 is the main project. It selectively imports only properly-built V3
modules (persona/IdentityEngine, ambient, db) — nothing is treated as a
"frozen core" V4 must preserve.

```
┌────────────────────────────────────────┐
│       Friday V4 Surfaces              │
│  Voice │ Mobile │ Web │ IDE │ Desktop  │
├────────────────────────────────────────┤
│    Friday V4 Communication Bus         │
├────────────────────────────────────────┤
│    Friday V4 Intelligence Layer        │
├────────────────────────────────────────┤
│  V3 modules selectively imported       │
│  persona │ ambient │ db               │
└────────────────────────────────────────┘
```

## Project Structure

```
friday_v4/
├── docs/           # Architecture, plans, specs
├── src/friday_v4/  # V4 source code
│   ├── voice/      # Speech-to-text, text-to-speech
│   ├── desktop/    # WM abstraction, IDE integration
│   ├── mobile/     # Companion app API
│   ├── collab/     # Multi-instance collaboration
│   ├── security/   # Vulnerability scanning
│   ├── intelligence/ # Drift detection, predictions
│   ├── network/    # SSH, webhooks, remote access
│   └── proactive/  # Anticipation engine
└── tests/          # V4 test suite
```

## License

Same as Friday V3.
