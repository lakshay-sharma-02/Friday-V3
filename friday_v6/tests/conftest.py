"""Add src/ + project root to sys.path for test imports."""

import sys
from pathlib import Path

# Add package src directory so imports work without installing
PROJECT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
# Add the project root so `from tools.benchmarks import ...` and other
# tool-module imports work under plain `pytest` (not just `python -m`).
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
