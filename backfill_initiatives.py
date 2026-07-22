#!/usr/bin/env python3
"""One-time migration: backfill existing initiatives with evidence-grounded
statements and merge duplicate-title entries.

Usage: python backfill_initiatives.py
       (uses FRIDAY_DB env var, or default ~/.friday/friday.db)

Post-migration: run `friday watch --run-once` then `friday review pending`
to verify the harvest path also produces correct output.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.friday.db import connect, get_all_initiatives, get_all_understanding, now_iso
from src.friday.knowledge.store import get_all_knowledge
from src.friday.initiative.engine import InitiativeEngine
from src.friday.initiative.models import Initiative, InitiativeStatus
from src.friday.understanding import Understanding


def main() -> int:
    db_path = os.environ.get("FRIDAY_DB") or str(Path.home() / ".friday" / "friday.db")
    print(f"Opening DB: {db_path}")
    conn = connect(Path(db_path))

    eng = InitiativeEngine(conn)

    # ---- Step 1: Run full build ----
    # build() calls _merge_candidates() (now dedup-by-title), calls
    # _synthesize_statement() for every initiative (no template filler), and
    # calls _merge_existing_duplicates() + _backfill_existing_statements().
    print("\n--- Step 1: Running InitiativeEngine.build() ---")
    result = eng.build()
    print(f"  Total: {result.total}, Created: {result.created}, "
          f"Updated: {result.updated}")

    # ---- Step 2: Regenerate pending_initiatives ----
    # Preserve existing review/dismiss state; update statements in place;
    # insert any missing high-confidence initiatives.
    print("\n--- Step 2: Regenerating pending_initiatives ---")

    # Create a placeholder watch_history row for FK.
    cur = conn.execute(
        "INSERT INTO watch_history (started_at, outcome) VALUES (?, 'migration')",
        (now_iso(),))
    watch_id = cur.lastrowid

    # Load existing pending with their review state.
    existing_pending = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, reviewed, dismissed_at FROM pending_initiatives"
        ).fetchall()
    }

    # Get all high-confidence initiatives.
    high_conf = conn.execute(
        "SELECT id, title, statement, initiative_type, confidence, "
        "understanding_ids, knowledge_ids "
        "FROM initiatives WHERE confidence IN ('medium', 'strong')"
    ).fetchall()

    from src.friday.cli_watch import _synthesize_initiative_statement
    updated = 0
    inserted = 0

    for r in high_conf:
        statement = _synthesize_initiative_statement(
            conn, r["id"], r["title"], r["initiative_type"],
            r["understanding_ids"], r["knowledge_ids"])

        if r["id"] in existing_pending:
            # Update statement in place; preserve reviewed/dismissed state.
            try:
                conn.execute(
                    "UPDATE pending_initiatives SET statement=? WHERE id=?",
                    (statement, r["id"]))
                updated += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"    error updating {r['id']}: {e}")
        else:
            # Insert new pending initiative.
            try:
                conn.execute(
                    "INSERT INTO pending_initiatives "
                    "(id, title, statement, initiative_type, confidence, "
                    "understanding_ids, knowledge_ids, detected_at, watch_run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], r["title"], statement, r["initiative_type"],
                     r["confidence"], r["understanding_ids"] or "",
                     r["knowledge_ids"] or "",
                     now_iso(), watch_id))
                inserted += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"    error inserting {r['id']}: {e}")

    print(f"  Updated: {updated}, Inserted: {inserted}")

    conn.close()
    print("\nMigration complete. Run: friday review pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
