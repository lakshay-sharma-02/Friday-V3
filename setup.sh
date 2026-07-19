#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# Friday V3 environment setup
# ──────────────────────────────────────────────────────────────
# Run this after pulling new friday source code or when friday
# execute fails with "no worker for task" / "claude not found".
#
# Usage:
#   source setup.sh   # sources so TMPDIR persists in current shell
#   # OR
#   bash setup.sh     # sets TMPDIR for subprocesses only (use export)
#
# ──────────────────────────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
export REPO_DIR

# ---- 0. Pip install (if not already) ----
cd "$REPO_DIR"
if ! python3 -c "import friday" 2>/dev/null; then
  echo "[setup] Installing friday package ..."
  pip install -e . 2>&1 | tail -3
fi

# ---- 1. npm-global on PATH (claude resolves) ----
NPM_BIN="$HOME/.npm-global/bin"
if [[ -d "$NPM_BIN" && ":$PATH:" != *":$NPM_BIN:"* ]]; then
  export PATH="$NPM_BIN:$PATH"
  echo "[setup] Added $NPM_BIN to PATH"
fi

# ---- 2. TMPDIR (avoid /tmp filling up) ----
export TMPDIR="$HOME/tmp"
mkdir -p "$TMPDIR"
echo "[setup] TMPDIR=$TMPDIR"

# ---- 3. Re-register workers in DB ----
# Wipes stale worker rows and re-registers from current source,
# so LLM reasoning profiles are renamed to ... llm and the
# external CLI tools (worker:claude, worker:codex, etc.) get
# their correct capabilities.
echo "[setup] Re-registering workers in DB ..."
cd "$REPO_DIR"
python3 - <<'PYEOF'
import sqlite3, os, sys
sys.path.insert(0, "src")
from friday.worker.engine import WorkerRegistry

db = os.path.expanduser(os.environ.get("FRIDAY_DB", "~/.friday/friday.db"))
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
try:
    conn.execute("PRAGMA foreign_keys = OFF")
    for t in ("workers", "worker_history", "worker_versions",
              "worker_capabilities", "resolver_history",
              "resolver_evolution", "resolver_assignments",
              "runtime_results", "runtime_events", "runtime_tasks",
              "runtime_sessions"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
finally:
    conn.execute("PRAGMA foreign_keys = ON")

reg = WorkerRegistry(conn)
nb = reg.register_builtins()
ne = reg.register_external()
conn.close()
print(f"  builtins: {nb}")
print(f"  external: {ne}")
PYEOF

echo "[setup] Done."
echo ""
echo "Now run:"
echo "  cd \"$REPO_DIR\" && friday execute \"your goal\""
