"""Add src/ to sys.path for test imports."""

import sys
from pathlib import Path

# Add package src directory so imports work without installing
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
