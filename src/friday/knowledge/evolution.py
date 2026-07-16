"""Knowledge Evolution (Milestone 8.2).

A write-only layer that derives *change* in knowledge over time. It sits on top
of the Knowledge Engine (8.1), which it never modifies, and feeds the Brain
(ask.py) exactly the same `knowledge` table it always read.

DESIGN RULE — evidence-driven, never clock-driven.
  A subject does NOT become dormant because 90 days passed. It becomes dormant
  because a *newer observation* proves it is no longer used (a removal/archived/
  inactive/deprecated signal, or its usage observations stopped arriving while a
  contradicting one did). Every transition cites the observation evidence that
  caused it. The wall clock is never a cause.

What it does, deterministically:
  Part A  Knowledge history  — append-only snapshot of every knowledge row per build.
  Part B  Confidence evolution — Strengthened/Weakened events as confidence bands move.
  Part C  Lifecycle (Candidate→Observed→Verified→Stable→Dormant→Retired).
  Part D  Evolution event types (Strengthened, Weakened, Supserseded, Retired,
          Contradicted, Merged, Split, Dormant, Reactivated).
  Part E  Evidence aging — recent observations weighted more (reporting only;
          historical evidence is never discarded).
  Part F  Contradiction — opposing evidence recorded, never overwritten.
  Part G  Merging/Splitting — parents retained, children linked via related_ids.
  Part H  Retirement — evidence-driven (removal/contradiction), remains queryable.

Nothing here mutates the `knowledge` table's confidence/evidence/statement. The
only live-row mutation allowed is a *status* transition (Dormant/Retired), and
every prior version lives forever in knowledge_history.

Thresholds (all deterministic, mirrored from the Knowledge Engine 8.1):
  evidence_count >= 40 -> strong ; >= 15 -> medium ; else weak.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from ..db import (
    KnowledgeHistoryRow,
    EvolutionEventRow,
    insert_evolution_events,
    insert_knowledge_history,
    knowledge_history_for,
    latest_knowledge_snapshot,
    observations_all,
    update_knowledge_status,
)
from .models import Knowledge, KnowledgeConfidence, KnowledgeStatus
from .store import get_all_knowledge

# --- deterministic thresholds -------------------------------------------------

STRONG_THRESHOLD = 40
MEDIUM_THRESHOLD = 15

# Observation aspects that indicate active usage of a subject.
USAGE_ASPECTS = {"technology", "language", "tool", "repository", "framework"}

# Terminal signals — the subject was explicitly removed/abandoned. These drive
# Retired (an end-of-life belief). Evidence-driven: the OBSERVATION VALUE, not
# elapsed time, decides retirement.
RETIRED_VALUES = {
    "removed", "deleted", "deprecated", "archived", "retired", "abandoned",
}

# Quiet signals — usage stopped but the subject may return. These drive
# Dormant / Contradicted (a weakened but recoverable belief).
DORMANT_VALUES = {
    "unused", "not used", "inactive", "false", "0", "none",
}

# Any value that contradicts active usage. Combined set, used for the broad
# "is the latest evidence contradicting?" check.
INACTIVE_VALUES = RETIRED_VALUES | DORMANT_VALUES


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def band_of(evidence_count: int) -> KnowledgeConfidence:
    """Map evidence count to a confidence band. Mirrors engine thresholds."""
    if evidence_count >= STRONG_THRESHOLD:
        return KnowledgeConfidence.STRONG
    if evidence_count >= MEDIUM_THRESHOLD:
        return KnowledgeConfidence.MEDIUM
    return KnowledgeConfidence.WEAK


def _conf_order(c: KnowledgeConfidence) -> int:
    return {
        KnowledgeConfidence.WEAK: 0,
        KnowledgeConfidence.MEDIUM: 1,
        KnowledgeConfidence.STRONG: 2,
    }[c]


# --- observation-derived usage signals ---------------------------------------


def _usage_signals(conn) -> Dict[str, Dict[str, object]]:
    """For every subject with a usage observation, compute an evidence-driven
    usage signal from the CURRENT observation data only.

    Driven by the MOST RECENT observation per subject — so a subject can be
    contradicted (latest obs says inactive) and later reactivated (a newer obs
    says used again). An old stray inactive observation never blocks reactivation.

    Returns {subject: {"active": int, "latest_active": bool, "latest_inactive":
    bool, "inactive": bool, "inactive_ids": [str]}} where `inactive` reflects the
    latest evidence (the cause of Dormant/Retired), not any historical signal.
    Elapsed idle time is NEVER a cause.
    """
    latest: Dict[str, tuple] = {}  # subject -> (observed_at, value, id)
    active_count: Dict[str, int] = defaultdict(int)
    for o in observations_all(conn):
        if o.aspect not in USAGE_ASPECTS:
            continue
        if str(o.value).strip().lower() not in INACTIVE_VALUES:
            active_count[o.subject] += 1
        cur = latest.get(o.subject)
        if cur is None or o.observed_at > cur[0]:
            latest[o.subject] = (o.observed_at, o.value, o.id)

    signals: Dict[str, Dict[str, object]] = {}
    for subject, (ts, value, oid) in latest.items():
        v = str(value).strip().lower()
        is_inactive = v in INACTIVE_VALUES
        is_retired = v in RETIRED_VALUES
        signals[subject] = {
            "active": active_count[subject],
            "latest_active": not is_inactive,
            "latest_inactive": is_inactive,
            "retired": is_retired,
            "inactive": is_inactive and not is_retired,
            "inactive_ids": [oid] if is_inactive else [],
        }
    return signals


# --- evidence aging (Part E, reporting only) ---------------------------------


def evidence_age_weight(evidence_id: str, build_at: str) -> float:
    """Recency weight in [0.25, 1.0]. Linear, NOT exponential.

    Evidence older than the build contributes less, but never vanishes. The
    age is read from the evidence id's embedded timestamp — this reflects how
    *old the underlying fact is in the data*, not an idle-time rule.
    """
    try:
        ts = evidence_id.split(":", 1)[0]
        age_days = max(
            0,
            (
                datetime.fromisoformat(build_at).date()
                - datetime.fromisoformat(ts).date()
            ).days,
        )
    except (ValueError, IndexError):
        return 1.0
    # Lose 0.25 weight per 180-day band, floor at 0.25. Deterministic.
    bands = age_days // 180
    return max(0.25, 1.0 - 0.25 * bands)


def weighted_evidence_score(evidence_ids: List[str], build_at: str) -> float:
    """Sum of recency weights. Historic evidence is retained, just weighted down."""
    return round(sum(evidence_age_weight(e, build_at) for e in evidence_ids), 2)


# --- main entrypoint ----------------------------------------------------------


def evolve(conn, build_at: Optional[str] = None) -> int:
    """Derive evolution events from the latest knowledge build.

    WRITE-only layer. Idempotent: re-running with the same data emits zero
    duplicate events (deterministic event ids + INSERT OR IGNORE).

    Returns the number of evolution events recorded this pass.
    """
    if build_at is None:
        build_at = now_iso()

    current = get_all_knowledge(conn)
    prev = {h.knowledge_id: h for h in latest_knowledge_snapshot(conn)}
    signals = _usage_signals(conn)

    # 1) Append a full snapshot of current knowledge (history never mutated).
    insert_knowledge_history(
        conn,
        [
            KnowledgeHistoryRow(
                build_at=build_at,
                knowledge_id=k.id or k._generate_id(),
                type=k.type.value,
                subject=k.subject,
                statement=k.statement,
                confidence=k.confidence.value,
                evidence_ids=",".join(k.evidence_ids),
                status=k.status.value,
                created_at=k.created_at,
                updated_at=k.updated_at,
                verification_count=k.verification_count,
                is_static=int(bool(k.is_static)),
            )
            for k in current
        ],
    )

    events: List[EvolutionEventRow] = []
    current_ids = {k.id or k._generate_id(): k for k in current}

    # 2) Diff every current entry against its previous snapshot.
    for kid, k in current_ids.items():
        prev_h = prev.get(kid)
        ev_ids = set(k.evidence_ids)
        if prev_h is None:
            # New knowledge appeared.
            events.append(_event(
                build_at, "Strengthened", kid, None, k.confidence.value,
                None, k.status.value, None, k.statement,
                f"Knowledge emerged with {k.evidence_count} evidence "
                f"(status {k.status.value}).",
                ",".join(sorted(ev_ids)),
            ))
            continue

        prev_conf = KnowledgeConfidence.from_str(prev_h.confidence)
        new_conf = k.confidence
        if _conf_order(new_conf) > _conf_order(prev_conf):
            added = ev_ids - set(prev_h.evidence_ids.split(","))
            events.append(_event(
                build_at, "Strengthened", kid, prev_h.confidence, k.confidence.value,
                prev_h.status, k.status.value, prev_h.statement, k.statement,
                f"Confidence grew {prev_conf.value}->{new_conf.value} as evidence "
                f"rose {len(prev_h.evidence_ids.split(',')) if prev_h.evidence_ids else 0}"
                f"->{k.evidence_count}.",
                ",".join(sorted(added)),
            ))
        elif _conf_order(new_conf) < _conf_order(prev_conf):
            # Weakened: only ever because contradicting/contradicted evidence
            # entered (the engine's own confidence is monotonic, so a drop here
            # means evolution lowered the effective signal — cite the cause).
            cause = _weaken_reason(k, signals, prev_h)
            events.append(_event(
                build_at, "Weakened", kid, prev_h.confidence, k.confidence.value,
                prev_h.status, k.status.value, prev_h.statement, k.statement,
                cause,
                ",".join(sorted(ev_ids & set(prev_h.evidence_ids.split(",")))),
            ))

        # Status advancement driven by the engine (Verified/Stable).
        if _status_rank(k.status) > _status_rank(KnowledgeStatus.from_str(prev_h.status)):
            events.append(_event(
                build_at, "Verified" if k.status == KnowledgeStatus.VERIFIED
                else "Stabilized", kid,
                prev_h.confidence, k.confidence.value,
                prev_h.status, k.status.value, prev_h.statement, k.statement,
                f"Lifecycle advanced {prev_h.status}->{k.status.value}.",
                ",".join(sorted(ev_ids)),
            ))

        # Statement changed without a confidence move -> Superseded/Contradicted.
        if k.statement != prev_h.statement and _conf_order(new_conf) == _conf_order(prev_conf):
            kind, reason = _statement_change_kind(k, signals)
            events.append(_event(
                build_at, kind, kid, prev_h.confidence, k.confidence.value,
                prev_h.status, k.status.value, prev_h.statement, k.statement,
                reason, ",".join(sorted(ev_ids)),
            ))

    # 3) Merge / Split detection (parents retained, children linked). Runs
    #    before lifecycle so 'went quiet' is judged from evidence signals, not
    #    the status the lifecycle step is about to set.
    events.extend(_merge_split_events(build_at, current_ids, prev, signals))

    # 4) Evidence-driven Dormant / Reactivated / Contradicted / Retired.
    events.extend(_lifecycle_events(conn, build_at, current_ids, prev, signals))

    # Persist. Idempotent: deterministic ids + INSERT OR IGNORE.
    insert_evolution_events(conn, events)
    return len(events)


def _event(
    build_at: str, etype: str, kid: str, prev_conf, new_conf,
    prev_status, new_status, prev_stmt, new_stmt, reason: str, evidence: str,
    related: str = "",
) -> EvolutionEventRow:
    return EvolutionEventRow(
        id=f"{build_at}:{etype}:{kid}",
        build_at=build_at,
        event_type=etype,
        knowledge_id=kid,
        previous_confidence=prev_conf,
        new_confidence=new_conf,
        previous_status=prev_status,
        new_status=new_status,
        previous_statement=prev_stmt,
        new_statement=new_stmt,
        reason=reason,
        evidence_ids=evidence,
        related_ids=related,
        timestamp=build_at,
    )


def _status_rank(s: KnowledgeStatus) -> int:
    return {
        KnowledgeStatus.CANDIDATE: 0,
        KnowledgeStatus.OBSERVED: 1,
        KnowledgeStatus.VERIFIED: 2,
        KnowledgeStatus.STABLE: 3,
        KnowledgeStatus.DORMANT: 4,
        KnowledgeStatus.RETIRED: 5,
    }[s]


def _weaken_reason(k: Knowledge, signals: dict, prev_h) -> str:
    sig = signals.get(k.subject)
    if sig and sig["inactive"]:
        return (
            f"Contradicting evidence: subject '{k.subject}' now shows "
            f"inactive/removed signals; confidence dropped "
            f"{prev_h.confidence}->{k.confidence.value}."
        )
    return (
        f"Effective confidence dropped {prev_h.confidence}->{k.confidence.value} "
        f"as recent supporting evidence aged relative to its peak."
    )


def _statement_change_kind(k: Knowledge, signals: dict) -> Tuple[str, str]:
    sig = signals.get(k.subject)
    if sig and sig["inactive"]:
        return (
            "Contradicted",
            f"Newer evidence contradicts prior statement; previous belief "
            f"retained in history. Reason: {k.subject} shows inactive/removed "
            f"signal.",
        )
    return (
        "Superseded",
        "Statement refined by newer evidence; previous wording retained in history.",
    )


def _lifecycle_events(conn, build_at, current_ids, prev, signals) -> List[EvolutionEventRow]:
    """Dormant / Reactivated / Contradicted / Retired — ALL evidence-driven."""
    out: List[EvolutionEventRow] = []
    for kid, k in current_ids.items():
        sig = signals.get(k.subject)
        if not sig:
            continue
        # Latest evidence is a terminal removal -> Retired (end-of-life belief,
        # still queryable). Driven by the observation VALUE, never the clock.
        if sig["retired"] and k.status != KnowledgeStatus.RETIRED:
            out.append(_event(
                build_at, "Retired", kid, None, k.confidence.value,
                None, KnowledgeStatus.RETIRED.value, None, k.statement,
                f"'{k.subject}' shows a terminal removal/deprecation signal "
                f"(retired, not deleted); remains queryable for historical ask.",
                ",".join(sig["inactive_ids"]),  # type: ignore[arg-type]
            ))
            update_knowledge_status(conn, kid, KnowledgeStatus.RETIRED.value)
        # Latest evidence is quiet (usage stopped) -> Contradicted (if it had real
        # prior confidence) or Dormant. May reactivate later; never clock-driven.
        elif sig["inactive"] and k.status not in (
            KnowledgeStatus.DORMANT, KnowledgeStatus.RETIRED
        ):
            # Contradicted when the subject previously held real (medium/strong)
            # active confidence; otherwise a plain Dormant transition.
            etype = (
                "Contradicted"
                if _conf_order(k.confidence) >= _conf_order(KnowledgeConfidence.MEDIUM)
                else "Dormant"
            )
            out.append(_event(
                build_at, etype, kid, None, k.confidence.value,
                None, KnowledgeStatus.DORMANT.value, None, k.statement,
                f"Newer observation shows '{k.subject}' is no longer used "
                f"(removal/archived/inactive signal). Prior belief retained in "
                f"history; transitioned {etype}, not deleted.",
                ",".join(sig["inactive_ids"]),  # type: ignore[arg-type]
            ))
            update_knowledge_status(conn, kid, KnowledgeStatus.DORMANT.value)

    # Reactivated: a previously Dormant/Retired subject whose LATEST evidence is
    # active usage again (a newer 'used' observation arrived).
    for kid, k in current_ids.items():
        if k.status in (KnowledgeStatus.DORMANT, KnowledgeStatus.RETIRED):
            sig = signals.get(k.subject)
            if sig and sig["latest_active"]:
                out.append(_event(
                    build_at, "Reactivated", kid, None, k.confidence.value,
                    k.status.value, KnowledgeStatus.OBSERVED.value,
                    None, k.statement,
                    f"New usage observation for '{k.subject}'; reactivated.",
                    ",".join(sorted(set(k.evidence_ids))),
                ))
                update_knowledge_status(conn, kid, KnowledgeStatus.OBSERVED.value)
    return out


def _merge_split_events(
    build_at: str,
    current_ids: Dict[str, Knowledge],
    prev: Dict[str, KnowledgeHistoryRow],
    signals: Dict[str, Dict[str, object]],
) -> List[EvolutionEventRow]:
    """Detect convergence (Merge) and divergence (Split). Parents retained."""
    out: List[EvolutionEventRow] = []
    prev_subjects = {h.subject for h in prev.values()}

    # Merge: a current entry whose subject is a combination of >=2 prior subjects.
    for kid, k in current_ids.items():
        tokens = _subject_tokens(k.subject)
        matched = [s for s in prev_subjects if s and s.lower() in tokens and s != k.subject]
        if len(matched) >= 2:
            out.append(_event(
                build_at, "Merged", kid, None, k.confidence.value,
                None, k.status.value, None, k.statement,
                f"Converges prior knowledge: {', '.join(matched)}.",
                ",".join(sorted(set(k.evidence_ids))),
                related=",".join(sorted(matched)),
            ))

    # Split: a prior belief went quiet (latest evidence inactive/retired) or
    # vanished this build while >=2 NEW subjects appeared. Deterministic
    # divergence signal. Judged from evidence signals, not post-lifecycle status.
    new_subjects = {
        k.subject for k in current_ids.values()
        if (k.id or k._generate_id()) not in prev
    }
    for pid, ph in prev.items():
        if ph.status in (KnowledgeStatus.RETIRED.value, KnowledgeStatus.DORMANT.value):
            continue
        cur = current_ids.get(pid)
        sig = signals.get(ph.subject)
        went_quiet = (
            cur is None
            or (sig is not None and (sig["inactive"] or sig["retired"]))
        )
        children = [s for s in new_subjects if s != ph.subject]
        if went_quiet and len(children) >= 2:
            out.append(_event(
                build_at, "Split", pid, ph.confidence, None,
                ph.status, KnowledgeStatus.RETIRED.value, ph.statement, None,
                f"Prior knowledge split into: {', '.join(children)}.",
                "", related=",".join(sorted(children)),
            ))
    return out


def _subject_tokens(subject: str) -> Set[str]:
    """Split a subject into comparable tokens (handles ',', '+', ' ', ':' sep)."""
    parts = set()
    for tok in subject.replace("+", " ").replace(",", " ").replace(":", " ").split():
        tok = tok.strip().lower()
        if tok:
            parts.add(tok)
    # Also keep the full original for exact-substring matching.
    parts.add(subject.strip().lower())
    return parts


# --- read helpers for CLI / Brain --------------------------------------------


def history_timeline(conn, knowledge_id: str) -> List[KnowledgeHistoryRow]:
    """Every snapshot of one knowledge entry, oldest first."""
    return knowledge_history_for(conn, knowledge_id)


def evolution_timeline(conn) -> List[EvolutionEventRow]:
    """All evolution events, newest first."""
    return evolution_events_all(conn)
