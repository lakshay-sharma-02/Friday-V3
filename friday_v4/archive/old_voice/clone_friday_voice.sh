#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# FRIDAY Voice Cloner — Install XTTS-v2 and clone FRIDAY's voice
# ---------------------------------------------------------------------------
# Run this from the friday_v4/ directory:
#   cd ~/Projects/Friday\ V3/friday_v4
#   bash clone_friday_voice.sh
#
# Prerequisites:
#   - Python 3.11 (installed at ~/.local/bin/python3.11)
#   - ~/.friday/voices/friday.wav (already downloaded and cleaned!)
# ---------------------------------------------------------------------------
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "╔══════════════════════════════════════════════╗"
echo "║     FRIDAY Voice Cloner                      ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Check voice sample exists
VOICE_SAMPLE="$HOME/.friday/voices/friday.wav"
if [ ! -f "$VOICE_SAMPLE" ]; then
    echo "❌ Voice sample not found at $VOICE_SAMPLE"
    echo "   Download it first from the YouTube compilation"
    exit 1
fi
echo "✅ Voice sample: $VOICE_SAMPLE ($(du -h "$VOICE_SAMPLE" | cut -f1))"

# Use existing Python 3.11 venv or create it
VENV="$DIR/.venv311"
if [ ! -d "$VENV" ]; then
    echo "📦 Creating Python 3.11 virtualenv..."
    ~/.local/bin/python3.11 -m venv "$VENV"
fi
source "$VENV/bin/activate"

echo "📦 Installing XTTS-v2 (coqui-tts)..."
echo "   This will download torch (~2GB) and the XTTS model (~3GB)."
echo "   First time: ~10-15 minutes. Subsequent runs: instant."
echo ""

pip install --upgrade pip setuptools wheel -q
pip install TTS edge-tts soundfile -q

echo ""
echo "📦 Installing Friday V4 voice interface..."
pip install -e . --no-deps -q

echo ""
echo "✅ XTTS-v2 installed!"
echo ""
echo "Next: Clone the FRIDAY voice and test it"
echo ""
echo "  .venv311/bin/python -c \""
echo "from TTS.api import TTS"
echo "import soundfile as sf"
echo ""
echo "tts = TTS('tts_models/multilingual/multi-dataset/xtts_v2')"
echo ""
echo "# Clone from our voice sample"
echo "tts.tts_to_file("
echo "    text='Hello, I am FRIDAY, your AI operating partner.',"
echo "    speaker_wav='$HOME/.friday/voices/friday.wav',"
echo "    language='en',"
echo "    file_path='/tmp/friday_cloned.wav',"
echo ")"
echo "print('✅ FRIDAY voice cloned! Saved to /tmp/friday_cloned.wav')"
echo "\""
echo ""
echo "Then play it:"
echo "  ffplay /tmp/friday_cloned.wav"
echo ""
echo "Or test with the full CLI:"
echo "  cd '$(dirname "$0")'"
echo "  PYTHONPATH=src .venv311/bin/python -m friday_v4.cli_talk voice test"
echo ""
