# Friday V4 🚀

> **An ambient, proactive, multi-modal AI operating partner.**
> Inspired by Tony Stark's FRIDAY, built on Friday V3's frozen core.

## Quick Start

```bash
# Install V4 alongside V3
pip install -e friday_v4/

# Start voice session
friday talk

# Check status
friday daemon status
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

V4 builds *on top of* V3's frozen core. It never modifies V3.

```
┌────────────────────────────────────────┐
│       Friday V4 Surfaces              │
│  Voice │ Mobile │ Web │ IDE │ Desktop  │
├────────────────────────────────────────┤
│    Friday V4 Communication Bus         │
├────────────────────────────────────────┤
│    Friday V4 Intelligence Layer        │
├────────────────────────────────────────┤
│      Friday V3 Core (Frozen)           │
│  106K LOC │ 273 files │ 1656 tests     │
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
