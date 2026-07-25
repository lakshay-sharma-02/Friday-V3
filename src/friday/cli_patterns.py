"""CLI command for Pillar B Stage 2 — Sequence Mining.

``friday patterns``        — show persisted patterns.
``friday patterns mine``   — run the miner, persist results, display.
``friday patterns clear``  — delete all persisted patterns.
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import (
    clear_mined_patterns,
    clear_workflow_intents,
    connect,
    get_mined_patterns,
    get_workflow_intents,
    insert_mined_pattern,
    insert_workflow_intent,
    now_iso,
)
from .sequence_miner import format_patterns, mine_sequences


def cmd_patterns(args: argparse.Namespace) -> int:
    """Dispatch ``friday patterns [mine|clear|label]``."""
    action = getattr(args, "action", None)

    if action == "mine":
        return _mine()
    elif action == "clear":
        return _clear()
    elif action == "label":
        return _label()
    elif action == "form":
        return _form(args)
    else:
        return _show(args)


def _show(args: argparse.Namespace) -> int:
    """Show persisted patterns."""
    conn = connect()
    min_count = getattr(args, "min_count", 0)
    limit = getattr(args, "limit", 50)
    patterns = get_mined_patterns(conn, min_count=min_count, limit=limit)
    conn.close()

    if not patterns:
        print("No mined patterns yet.")
        print()
        print("To run sequence mining:")
        print("  friday patterns mine")
        return 0

    # Rehydrate into MinedPattern objects for the formatter.
    from .sequence_miner import MinedPattern

    objs = []
    for p in patterns:
        seq = json.loads(p["sequence_json"]) if isinstance(p["sequence_json"], str) else p["sequence_json"]
        objs.append(MinedPattern(
            sequence=[tuple(item) for item in seq],
            count=p["count"],
            first_seen=p.get("first_seen", ""),
            last_seen=p.get("last_seen", ""),
            common_workspace=p.get("common_workspace", ""),
            common_project=p.get("common_project", ""),
        ))

    print(format_patterns(objs))
    return 0


def _mine() -> int:
    """Run the miner, persist results, display."""
    conn = connect()
    try:
        # Clear old patterns first (replace, not append).
        clear_mined_patterns(conn)

        patterns = mine_sequences(conn)
        if not patterns:
            print("Sequence mining complete — no frequent patterns found.")
            print("Keep using Friday to accumulate more actions.")
            return 0

        # Persist each pattern.
        for p in patterns:
            insert_mined_pattern(conn, {
                "sequence_json": json.dumps([[t, tg] for t, tg in p.sequence]),
                "count": p.count,
                "distinct_sessions": p.count,
                "first_seen": p.first_seen,
                "last_seen": p.last_seen,
                "common_workspace": p.common_workspace,
                "common_project": p.common_project,
                "confidence": "derived",
                "exemplars": json.dumps(p.exemplars) if p.exemplars else "{}",
                "mined_at": now_iso(),
            })

        print(format_patterns(patterns))
        print(f"Persisted {len(patterns)} pattern(s).")
    finally:
        conn.close()
    return 0


def _label() -> int:
    """Run intent labeling on all mined patterns."""
    conn = connect()
    try:
        patterns = get_mined_patterns(conn)
        if not patterns:
            print("No mined patterns to label.")
            print("Run `friday patterns mine` first.")
            return 0

        # Clear old intents.
        clear_workflow_intents(conn)

        from .intent_labeler import WorkflowIntent, label_intent, format_intents

        intents: list[WorkflowIntent] = []
        for p in patterns:
            seq = json.loads(p["sequence_json"]) if isinstance(p["sequence_json"], str) else p["sequence_json"]
            intent = label_intent(
                pattern_sequence=[tuple(item) for item in seq],
                pattern_count=p["count"],
                workspace=p.get("common_workspace", ""),
                project=p.get("common_project", ""),
            )
            insert_workflow_intent(conn, {
                "pattern_id": p["id"],
                "intent_label": intent.intent_label,
                "intent_description": intent.intent_description,
                "steps_text": json.dumps(intent.steps),
                "confidence": intent.confidence,
                "pattern_summary": json.dumps([[t, tg] for t, tg in intent.pattern_seq]),
                "labeled_at": intent.labeled_at,
            })
            intents.append(intent)

        print(format_intents(intents))
        print(f"Labeled {len(intents)} intent(s).")
    finally:
        conn.close()
    return 0


def _form(args: argparse.Namespace) -> int:
    """Run skill formation on current workflow intents."""
    from .skill_formation import form_skills, format_formed_skills

    conn = connect()
    try:
        skills = form_skills(conn)
        if skills:
            print(format_formed_skills(skills))
            print(f"Formed {len(skills)} skill(s).")
            print()
            print("To review and promote a skill to active:")
            print("  friday meta promote --worker <worker_name>")
        else:
            print("No new skills formed.")
            print("Run `friday patterns mine` and `friday patterns label` first,")
            print("or use --force to re-form existing intents (overwrites).")
        return 0
    finally:
        conn.close()


def _clear() -> int:
    """Delete all mined patterns and intents."""
    conn = connect()
    clear_mined_patterns(conn)
    clear_workflow_intents(conn)
    conn.close()
    print("All mined patterns and intents cleared.")
    return 0
