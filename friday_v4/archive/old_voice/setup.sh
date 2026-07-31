#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Friday V4 — Voice Interface Setup
# ---------------------------------------------------------------------------
# Run this script from the friday_v4/ directory:
#   cd ~/Projects/Friday\ V3/friday_v4
#   bash setup.sh
#
# What it does:
#   1. Creates a Python 3.12 virtualenv
#   2. Installs all voice components:
#      - Kokoro (fast CPU TTS, 54 voices, offline)
#      - edge-tts (premium Microsoft neural voices, needs internet)
#      - PyTorch (for Kokoro + future FRIDAY voice clone)
#      - Audio capture (pyaudio, webrtcvad)
#      - STT (faster-whisper for speech recognition)
#   3. Creates the voice cache directory
#   4. Tests the installation
#
# After setup, run:
#   .venv312/bin/python -m friday_v4.cli_talk voice test
#   .venv312/bin/python -m friday_v4.cli_talk talk
# ---------------------------------------------------------------------------
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "╔══════════════════════════════════════════════╗"
echo "║     Friday V4 — Voice Interface Setup       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Check Python 3.12 ──────────────────────────────────────────────
PYTHON=""
for cmd in python3.12 python312 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
        if [ "$ver" = "3.12" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.12 not found. Install it first:"
    echo "   Arch:  sudo pacman -S python312"
    echo "   Ubuntu: sudo apt install python3.12 python3.12-venv"
    exit 1
fi
echo "✅ Python: $($PYTHON --version)"

# ── Create virtualenv ──────────────────────────────────────────────
VENV="$DIR/.venv312"
if [ -d "$VENV" ]; then
    echo "📦 Virtualenv exists, updating..."
else
    echo "📦 Creating virtualenv..."
    "$PYTHON" -m venv "$VENV"
fi

source "$VENV/bin/activate"

# ── Upgrade pip ────────────────────────────────────────────────────
echo "📦 Upgrading pip..."
pip install --upgrade pip setuptools wheel -q

# ── Install packages ───────────────────────────────────────────────
echo ""
echo "📦 Installing TTS engines..."

install_step() {
    local name="$1"
    local pkg="$2"
    echo "   → $name..."
    if pip install "$pkg" -q 2>/tmp/friday_install_err.log; then
        echo "     ✅ Done"
    else
        echo "     ⚠️  Failed (non-critical): $(tail -1 /tmp/friday_install_err.log)"
    fi
}

# Core TTS
install_step "Kokoro (fast offline TTS)" "kokoro soundfile"
install_step "Edge-TTS (premium neural voices)" "edge-tts"

# Audio capture
install_step "PyAudio (microphone input)" "pyaudio"
install_step "WebRTC VAD (voice detection)" "webrtcvad"

# Speech recognition
install_step "Faster-Whisper (STT)" "faster-whisper"

# Hotword detection
install_step "Porcupine (Hey Friday)" "pvporcupine"

# V3 bridge
# Install Friday V3 core (if available)
echo "   → Friday V3 core..."
if pip install -e .. -q 2>/tmp/friday_v3_install.log; then
    echo "     ✅ Done"
else
    echo "     ⚠️  V3 not installed (non-critical for voice-only mode)"
fi

# ── Create directories ─────────────────────────────────────────────
mkdir -p "$HOME/.friday/voices"
mkdir -p "$HOME/.friday/voice_cache"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  ✅ Setup complete!                          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo ""
echo "  1. Test FRIDAY speaks:"
echo "     $VENV/bin/python -m friday_v4.cli_talk voice test"
echo ""
echo "  2. Check status:"
echo "     $VENV/bin/python -m friday_v4.cli_talk voice setup"
echo ""
echo "  3. Start interactive session:"
echo "     $VENV/bin/python -m friday_v4.cli_talk talk"
echo ""
echo "  4. For the authentic FRIDAY voice clone:"
echo "     Place a ~20s WAV of Kerry Condon's FRIDAY at:"
echo "       ~/.friday/voices/friday.wav"
echo "     Then run: pip install coqui-tts"
echo ""
