#!/usr/bin/env bash
# Friday V4 — local installation script (Wave 12).
#
# Creates a dedicated venv, installs friday-v4 (editable, with the
# optional extras you ask for), and runs `friday4 doctor` to confirm
# the install. Pure-stdlib runtime; this script only needs python3.
#
# Usage:
#   ./install.sh                    # base install (voice TTS fallbacks)
#   ./install.sh --full             # everything (STT/hotword/security tools)
#   ./install.sh --voice            # voice extras
#   ./install.sh --dev              # dev extras (pytest, ruff, mypy)
#   ./install.sh --venv ~/.venvs/friday4
#
# V4 is the product: it runs standalone with zero V3 code. V3's DB is
# optional legacy *data* read through the read-only bridge.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/.venvs/friday4}"
EXTRAS=()

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)   EXTRAS+=("full") ;;
    --voice)  EXTRAS+=("voice") ;;
    --dev)    EXTRAS+=("dev") ;;
    --security) EXTRAS+=("security") ;;
    --collab) EXTRAS+=("collab") ;;
    --desktop) EXTRAS+=("desktop") ;;
    --venv)   VENV_DIR="$2"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info.minor)')"
if [[ "$PY_MAJOR" -lt 3 || "$PY_MINOR" -lt 12 ]]; then
  echo "Friday V4 requires Python >= 3.12 (found $PY_MAJOR.$PY_MINOR)" >&2
  exit 1
fi

echo "◆ Friday V4 — installing into $VENV_DIR"
mkdir -p "$VENV_DIR"
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip >/dev/null

EXTRA_ARGS=()
if [[ ${#EXTRAS[@]} -gt 0 ]]; then
  EXTRA_ARGS+=("$([[ ${#EXTRAS[@]} -eq 1 ]] && echo "${EXTRAS[0]}" \
    || printf '%s,' "${EXTRAS[@]}" | sed 's/,$//')")
fi

echo "◆ Installing friday-v4 (extras: ${EXTRA_ARGS[*]:-none})"
pip install -e "$HERE"${EXTRA_ARGS:+"[${EXTRA_ARGS[*]}]"}

echo
echo "◆ Verifying install…"
friday4 doctor || true
echo
echo "✓ Friday V4 installed. Try:  friday4 status   ·   friday4 talk \"hello\""
echo "  Re-activate later with:    source $VENV_DIR/bin/activate"
