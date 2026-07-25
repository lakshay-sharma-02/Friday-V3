"""Pillar B Stage 2 — Sequence Mining (Pillar B — Workflow Learning).

Deterministic, LLM-free pattern miner that detects repeated action sequences
from the ``actions`` table. The output is a set of ``MinedPattern`` records:
frequent subsequences with metadata (frequency, typical context, first/last seen).

Algorithm
---------
1. **Sessionization**: actions are grouped into *sessions* by time proximity
   (actions ≤ 30 min apart belong to the same session). A session represents a
   continuous block of user/Friday activity.
2. **N-gram mining**: within each session, all contiguous subsequences of length
   2-5 are extracted. Each n-gram is represented as a tuple of (action_type,
   normalized_target) — normalized because absolute workspace numbers or app
   PIDs are not meaningful for pattern matching (e.g., "switch to workspace 3"
   and "switch to workspace 5" are the same *pattern* of "workspace switch").
3. **Frequency counting**: each n-gram is counted across all sessions. An n-gram
   is a ``Pattern`` only when it appears in ≥ ``min_support`` sessions.
4. **Output**: patterns are returned sorted by frequency descending, each with
   the canonical sequence, count, distinct sessions, first/last seen, and the
   most common workspace/project context.

No LLM, no randomness, no expensive computation — the miner runs in O(N * L)
where N = number of actions and L = max pattern length (default 5). The result
is purely data-driven: if a pattern appears twice, it's reported; if it appears
once, it's not (unless min_support=1, which is the default for discovery).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from .db import now_iso

#: Max minutes between consecutive actions to consider them part of the same
#: user session. Actions further apart start a new session.
_DEFAULT_SESSION_GAP_MINUTES = 30

#: Minimum sessions a subsequence must appear in to be reported as a pattern.
_DEFAULT_MIN_SUPPORT = 2

#: Max length of a mined subsequence (n-gram). Longer patterns are less useful.
_DEFAULT_MAX_LENGTH = 5

#: Action types whose target value should be normalized (abstracted) for pattern
#: matching. E.g. ``workspace_switch: "3"`` → ``workspace_switch: "<workspace>"``.
_NORMALIZABLE_TYPES = frozenset({
    "workspace_switch", "app_launch", "app_close", "window_focus",
})

#: Per-ngram-key concrete value distributions
#: {ngram_tuple: {step_idx_str: {concrete_value: count}}}
_PATTERN_EXEMPLARS: dict = {}


@dataclass
class MinedPattern:
    """A repeated action sequence discovered by the miner.

    ``sequence`` is a list of ``(action_type, target)`` tuples representing
    the canonical pattern. ``count`` is how many sessions this pattern appeared
    in. ``context`` stores the most common workspace/project observed alongside
    the pattern.
    """

    sequence: list[tuple[str, str]]  # [(action_type, normalized_target), ...]
    count: int = 0                    # number of sessions this pattern appeared in
    distinct_sessions: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    common_workspace: str = ""
    common_project: str = ""
    confidence: str = "derived"       # always "derived" for deterministic mining
    exemplars: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sequence": [(t, tg) for t, tg in self.sequence],
            "count": self.count,
            "distinct_sessions": len(self.distinct_sessions),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "common_workspace": self.common_workspace,
            "common_project": self.common_project,
            "confidence": self.confidence,
        }

    def to_text(self) -> str:
        steps = " → ".join(
            f"{t}({tg})" if tg else t
            for t, tg in self.sequence
        )
        return (
            f"[{self.count}x] {steps}  "
            f"(first: {self.first_seen[:10]}, last: {self.last_seen[:10]})"
        )


# ---------------------------------------------------------------------------
# Main mining entry point
# ---------------------------------------------------------------------------


def mine_sequences(
    conn,
    min_support: int = _DEFAULT_MIN_SUPPORT,
    max_length: int = _DEFAULT_MAX_LENGTH,
    session_gap_minutes: int = _DEFAULT_SESSION_GAP_MINUTES,
) -> list[MinedPattern]:
    """Run the full sequence mining pipeline.

    Reads all actions from the DB, sessionizes them, mines for frequent
    subsequences, and returns detected patterns sorted by frequency (most
    frequent first).

    Args:
        conn: Open SQLite connection.
        min_support: Minimum sessions a pattern must appear in.
        max_length: Max length of a mined subsequence.
        session_gap_minutes: Max gap between actions in a session.

    Returns:
        List of MinedPattern objects, sorted by count descending.
    """
    actions = _load_actions(conn)
    if not actions:
        return []

    sessions = _sessionize(actions, gap_minutes=session_gap_minutes)
    if not sessions:
        return []

    patterns = _mine(sessions, min_support=min_support, max_length=max_length)
    return _sort_and_enrich(patterns, sessions)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_actions(conn) -> list[dict]:
    """Load all action rows ordered by observed_at."""
    rows = conn.execute(
        "SELECT * FROM actions ORDER BY observed_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sessionization
# ---------------------------------------------------------------------------


def _sessionize(
    actions: list[dict],
    gap_minutes: int = _DEFAULT_SESSION_GAP_MINUTES,
) -> list[list[dict]]:
    """Group consecutive actions into sessions by time proximity.

    If two consecutive actions are more than ``gap_minutes`` apart, a new
    session starts. Each session is a list of action dicts in chronological
    order.
    """
    if not actions:
        return []

    sessions: list[list[dict]] = []
    current: list[dict] = [actions[0]]
    gap_seconds = gap_minutes * 60

    for i in range(1, len(actions)):
        prev_ts = _parse_iso(actions[i - 1]["observed_at"])
        cur_ts = _parse_iso(actions[i]["observed_at"])
        if prev_ts is not None and cur_ts is not None:
            diff = (cur_ts - prev_ts).total_seconds()
            if diff > gap_seconds:
                sessions.append(current)
                current = []
        current.append(actions[i])

    if current:
        sessions.append(current)

    return sessions


def _parse_iso(ts: str):
    """Parse an ISO-format timestamp string, or None on failure."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# N-gram mining
# ---------------------------------------------------------------------------


def _mine(
    sessions: list[list[dict]],
    min_support: int = _DEFAULT_MIN_SUPPORT,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> list[MinedPattern]:
    """Mine frequent subsequences from sessionized actions.

    For each session, generates all contiguous subsequences (n-grams) of length
    2 through max_length. Counts how many *distinct sessions* each n-gram
    appears in. Returns patterns with count ≥ min_support.
    """
    from collections import Counter

    _PATTERN_EXEMPLARS.clear()

    # ngram_key -> set of session indices (for distinct session counting)
    ngram_sessions: dict[tuple, set[int]] = {}

    for sidx, session in enumerate(sessions):
        # Normalize each action in the session.
        normalized = [_normalize(a) for a in session]
        seen_in_session: set[tuple] = set()

        for length in range(2, max_length + 1):
            for i in range(len(normalized) - length + 1):
                ngram = tuple(normalized[i: i + length])
                # Track concrete target values for exemplar extraction.
                for pos, raw_action in enumerate(session[i: i + length]):
                    concrete_target = raw_action.get("target", "") or ""
                    if concrete_target:
                        pos_key = str(pos)
                        if ngram not in _PATTERN_EXEMPLARS:
                            _PATTERN_EXEMPLARS[ngram] = {}
                        pos_map = _PATTERN_EXEMPLARS[ngram].setdefault(pos_key, {})
                        pos_map[concrete_target] = pos_map.get(concrete_target, 0) + 1
                if ngram not in seen_in_session:
                    seen_in_session.add(ngram)
                    ngram_sessions.setdefault(ngram, set()).add(sidx)

    # Build MinedPattern objects for ngrams meeting min_support.
    patterns: list[MinedPattern] = []
    for ngram, session_indices in ngram_sessions.items():
        if len(session_indices) < min_support:
            continue
        patterns.append(MinedPattern(
            sequence=list(ngram),
            count=len(session_indices),
            exemplars=_PATTERN_EXEMPLARS.get(ngram, {}),
        ))

    return patterns


def _normalize(action: dict) -> tuple[str, str]:
    """Normalize an action for pattern matching.

    Returns (action_type, normalized_target). Targets for normalizable action
    types are abstracted (e.g., workspace "3" → "<workspace>", app "firefox"
    → "<app>"). This ensures that "switch to workspace 3" and "switch to
    workspace 5" are recognized as the same pattern.
    """
    atype = action.get("action_type", "")
    target = action.get("target", "")

    if atype in _NORMALIZABLE_TYPES:
        if target:
            target = f"<{atype.split('_')[0]}>"
        else:
            target = f"<{atype.split('_')[0]}>"

    return (atype, target)


# ---------------------------------------------------------------------------
# Enrichment and sorting
# ---------------------------------------------------------------------------


def _sort_and_enrich(
    patterns: list[MinedPattern],
    sessions: list[list[dict]],
) -> list[MinedPattern]:
    """Sort patterns by frequency and enrich with time/context metadata."""
    # Build a lookup: session index -> first observed_at, workspace, project.
    session_meta: list[dict] = []
    for sess in sessions:
        if sess:
            session_meta.append({
                "first_ts": sess[0].get("observed_at", ""),
                "last_ts": sess[-1].get("observed_at", ""),
                "workspaces": _collect_field(sess, "workspace_id"),
                "projects": _collect_field(sess, "project"),
            })
        else:
            session_meta.append({})

    for p in patterns:
        indices = [
            i for i, sess in enumerate(sessions)
            if _pattern_in_session(p.sequence, sess)
        ]
        p.count = len(indices)
        timestamps = [
            session_meta[i] for i in indices if i < len(session_meta)
        ]
        if timestamps:
            p.first_seen = min(
                m.get("first_ts", "") for m in timestamps if m.get("first_ts")
            ) if any(m.get("first_ts") for m in timestamps) else ""
            p.last_seen = max(
                m.get("last_ts", "") for m in timestamps if m.get("last_ts")
            ) if any(m.get("last_ts") for m in timestamps) else ""
            p.common_workspace = _most_common(
                ws for m in timestamps for ws in m.get("workspaces", [])
            )
            p.common_project = _most_common(
                pr for m in timestamps for pr in m.get("projects", [])
            )

    patterns.sort(key=lambda p: p.count, reverse=True)
    return patterns


def _pattern_in_session(
    pattern_seq: list[tuple[str, str]],
    session: list[dict],
) -> bool:
    """Check if a pattern appears as a *contiguous* subsequence in a session."""
    normed = [_normalize(a) for a in session]
    plen = len(pattern_seq)
    for i in range(len(normed) - plen + 1):
        if normed[i: i + plen] == pattern_seq:
            return True
    return False


def _collect_field(session: list[dict], field: str) -> list[str]:
    """Collect non-empty values of *field* from a session."""
    return [
        a.get(field, "") or ""
        for a in session
        if a.get(field)
    ]


def _most_common(items: Iterator[str]) -> str:
    """Return the most common item from an iterable, or empty string."""
    from collections import Counter
    counts: Counter = Counter()
    for item in items:
        if item:
            counts[item] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return ""


# ---------------------------------------------------------------------------
# CLI-friendly report
# ---------------------------------------------------------------------------


def format_patterns(patterns: list[MinedPattern]) -> str:
    """Render mined patterns as a human-readable report."""
    if not patterns:
        return "No patterns found yet — keep using Friday to accumulate actions."

    lines = ["Sequence Mining Report", "=" * 40, ""]
    for i, p in enumerate(patterns, 1):
        lines.append(f"{i}. {p.to_text()}")
        context_parts = []
        if p.common_workspace:
            context_parts.append(f"ws={p.common_workspace}")
        if p.common_project:
            context_parts.append(f"project={p.common_project}")
        if context_parts:
            lines.append(f"   Context: {', '.join(context_parts)}")
        lines.append("")
    return "\n".join(lines)
