"""Proper split of monolithic db.py into db/ package.

core.py: schema, data classes, connect(), migrations, utilities
queries.py: all CRUD query functions (with local @dataclass defs)
"""
import re
from pathlib import Path

DB_PATH = Path("src/friday/db.py")
PKG_DIR = Path("src/friday/db")

DB_TEXT = DB_PATH.read_text(encoding="utf-8")

# Split point: line number (0-indexed) for the first query function
SPLIT_LINE = 2015  # Line 2016 = def upsert_repository(

lines = DB_TEXT.split("\n")
core_lines = lines[:SPLIT_LINE]
queries_lines = lines[SPLIT_LINE:]

# ============================================================
# Write core.py
# ============================================================
# First, add typing imports that queries.py will use
core_text = "\n".join(core_lines)

PKG_DIR.mkdir(parents=True, exist_ok=True)

# Add import for Dict and Union (needed for insert_layer_history signature)
# The original db.py already has these imports

(PKG_DIR / "core.py").write_text(core_text, encoding="utf-8")
print(f"core.py: {len(core_lines)} lines")

# ============================================================
# Write queries.py
# ============================================================
queries_text = "\n".join(queries_lines)

# Fix the imports: the queries file needs to import from .core only what
# exists there. It *defines* the rest locally (SnapshotRow, ObservationRow, etc.)
# 
# We need to add the necessary stdlib imports and then import from .core
# what's actually there: Repository, LangRow, TechRow, RelationshipRow,
# ArchitectureRow, ComponentRow, EntryPointRow, connect, now_iso,
# commit_if_top, insert_layer_history, atomic
#
# The queries file currently starts with a function definition, but needs
# module-level imports. We'll prepend them.

queries_preamble = '''"""CRUD queries for the Friday knowledge base.

Split from the original monolithic db.py — all query functions live here.
Shared definitions (schema, classes, migrations, connect) are in core.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from .core import (
    Repository, LangRow, TechRow, RelationshipRow, ArchitectureRow,
    ComponentRow, EntryPointRow, connect, now_iso, commit_if_top,
    insert_layer_history, atomic,
)

'''

# Skip the first blank lines and function def to find real start
# The split starts at the line with "def upsert_repository("
# But we need to prepend our preamble and skip that line since it starts
# the original queries section
first_func_line = None
for i, line in enumerate(queries_lines):
    stripped = line.strip()
    if stripped.startswith("def ") or stripped.startswith("@") or stripped.startswith("class ") or stripped.startswith("# --") or stripped.startswith("# =="):
        first_func_line = i
        break

if first_func_line is not None:
    remaining = "\n".join(queries_lines[first_func_line:])
else:
    remaining = "\n".join(queries_lines)

(PKG_DIR / "queries.py").write_text(queries_preamble + remaining, encoding="utf-8")
print(f"queries.py: {len(queries_preamble.splitlines()) + len(remaining.splitlines())} lines")

# ============================================================
# Write __init__.py
# ============================================================
init_text = '''"""Friday knowledge-base storage layer.

Re-exports from core (schema, classes, migrations, utilities) and
queries (all CRUD functions) so existing ``from friday.db import X``
imports continue to work without changes.
"""

from .core import *
from .queries import *

'''

(PKG_DIR / "__init__.py").write_text(init_text, encoding="utf-8")
print(f"__init__.py written")

print("\nDone. Verify with: python -c 'from friday.db import *'")
