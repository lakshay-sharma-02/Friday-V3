"""CLI commands for Strategic Judgment (Milestone 6.5B/C).

`friday strategy`          -> the converging thesis (where the work is heading).
`friday strategy impact`   -> which project has highest user value.
`friday strategy platform` -> which should become a reusable platform.
`friday strategy learning` -> which taught the most engineering-wise.
`friday strategy opportunity` -> leverage you're leaving on the table.
`friday strategy priority` -> what should be the center of attention now.
`friday strategy merge`    -> which project should stay independent.

Each axis is a DISTINCT reasoning lens over persisted evidence (M6.5B). The CLI
only surfaces the judgments ask.py can already route — no new strategy logic.
"""

from __future__ import annotations

import argparse

from .db import connect
from .strategy import (
    strategy_converge,
    strategy_impact,
    strategy_learning,
    strategy_merge,
    strategy_opportunity,
    strategy_platform,
    strategy_priority,
)

_AXES = {
    "impact": strategy_impact,
    "platform": strategy_platform,
    "learning": strategy_learning,
    "opportunity": strategy_opportunity,
    "priority": strategy_priority,
    "merge": strategy_merge,
    "converge": strategy_converge,
}


def cmd_strategy_axis(args: argparse.Namespace) -> int:
    """READ: run one strategic-judgment axis."""
    axis = getattr(args, "token", None) or "converge"
    fn = _AXES.get(axis, strategy_converge)
    conn = connect()
    for line in fn(conn):
        print(line)
    conn.close()
    return 0


def cmd_strategy(args: argparse.Namespace) -> int:
    """Dispatch friday strategy [<axis>]."""
    token = getattr(args, "token", None)
    if token and token not in _AXES:
        print(f"error: unknown strategy axis: {token}", file=__import__("sys").stderr)
        print("axes: " + ", ".join(_AXES), file=__import__("sys").stderr)
        return 2
    return cmd_strategy_axis(args)
