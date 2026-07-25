"""Meta-Loop daemon — background self-improvement loop.

Runs Gap Analyzer periodically, triggers the Planner on the top gap, and
reports status changes as Insights through the existing Insight engine so
"Friday is building itself a new capability" shows up via the same channels
as any other insight.
"""

from __future__ import annotations

from typing import Optional

from .gap_analyzer import analyze, GapReport
from ..db import (
    connect,
    get_capability_gaps,
    get_si_runs,
    insert_insight,
    get_all_insights,
    update_insight_status,
    now_iso as db_now,
)
from ..insight.models import (
    Insight,
    InsightType,
    InsightStatus,
    InsightConfidence,
)
from ..insight.engine import InsightEngine


def run_cycle(conn=None, dry_run: bool = False, gap_id: Optional[int] = None) -> GapReport:
    """Run one meta-loop cycle.

    1. Run gap analysis.
    2. If top gap is above threshold and no run exists for it, trigger planning.
    3. Surface status as Insights.

    Returns the GapReport.
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        report = analyze(conn)
        _surface_as_insights(conn, report)

        # If a specific gap_id is given, plan for it; otherwise use top gap.
        if gap_id is not None:
            all_gaps = get_capability_gaps(conn)
            target_gap = next((g for g in all_gaps if g.get("id") == gap_id), None)
        else:
            target_gap = _pick_top_gap(conn)

        if target_gap and not dry_run:
            _maybe_trigger_plan(conn, target_gap)

        return report
    finally:
        if own_conn:
            conn.close()


def _pick_top_gap(conn) -> Optional[dict]:
    gaps = get_capability_gaps(conn, status="open")
    if not gaps:
        return None
    viable = [g for g in gaps if g.get("attempt_count", 0) < 3]
    return viable[0] if viable else None


_SCORE_THRESHOLD = 1.0


def _maybe_trigger_plan(conn, gap: dict) -> None:
    score = gap.get("score", 0.0)
    if score < _SCORE_THRESHOLD:
        return
    if gap.get("status") != "open":
        return

    from .si_planner import plan_for_gap
    print(f"  meta: planning for gap #{gap['id']} ({gap['description']}) score={score}")
    plan_id = plan_for_gap(conn, gap["id"])
    if plan_id:
        print(f"  meta: plan {plan_id} generated")
    else:
        print(f"  meta: planning failed or not applicable")


def _surface_as_insights(conn, report: GapReport) -> None:
    """Surface meta-engine status as Insights so the user sees them in
    `friday insights` and integrated channels."""
    now = db_now()
    existing_rows = list(get_all_insights(conn))
    existing_meta = [
        Insight.from_row(r) for r in existing_rows
        if r.insight_type == InsightType.OPPORTUNITY.value
        and "meta-engine" in (r.title or "").lower()
    ]

    if not report.gaps:
        for ins in existing_meta:
            update_insight_status(conn, ins.id, InsightStatus.RETIRED.value, now)
        return

    # Gather insight ids that should still be active this cycle.
    active_titles: set[str] = set()

    for g in report.gaps:
        if g.get("status") != "open":
            continue
        title = f"Meta-Engine: {g['description'][:60]}"
        active_titles.add(title)
        score = g.get("score", 0.0)

        match = [i for i in existing_meta if title in i.title]
        if match:
            ins = match[0]
            ins.statement = (
                f"Gap detected (score={score}): {g['description']}. "
                f"Use `friday meta status` for details."
            )
            ins.confidence = (
                InsightConfidence.STRONG if score > 3.0
                else InsightConfidence.MEDIUM if score > 1.5
                else InsightConfidence.WEAK
            )
            ins.updated_at = now
            insert_insight(conn, [ins.to_row()])
        else:
            safe_id = f"meta:{g['id']}:{g['description'][:40]}"
            ins = Insight(
                id=safe_id,
                type=InsightType.OPPORTUNITY,
                title=title,
                statement=(
                    f"Gap detected (score={score}): {g['description']}. "
                    f"Use `friday meta status` for details."
                ),
                status=InsightStatus.CANDIDATE,
                confidence=(
                    InsightConfidence.STRONG if score > 3.0
                    else InsightConfidence.MEDIUM if score > 1.5
                    else InsightConfidence.WEAK
                ),
                understanding_ids=[],
                initiative_ids=[],
                knowledge_ids=[],
                build_at=now,
                created_at=now,
                updated_at=now,
            )
            insert_insight(conn, [ins.to_row()])

    # Retire meta insights whose gaps no longer exist.
    for ins in existing_meta:
        if ins.title not in active_titles:
            if ins.status != InsightStatus.RETIRED:
                update_insight_status(conn, ins.id, InsightStatus.RETIRED.value, now)
