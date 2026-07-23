"""Understanding Engine (Milestone 8.3).

Write-only layer above Knowledge. Derives durable engineering understanding
from accumulated knowledge and knowledge-evolution events. The Brain consumes
understanding as another evidence source; it never computes understanding.

Idempotent. Deterministic. Running `build` twice on the same knowledge produces
the same understanding rows (INSERT OR REPLACE on deterministic ids + INSERT OR
IGNORE on evolution events).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..db import (
    atomic,
    UnderstandingEvolutionRow,
    UnderstandingHistoryRow,
    evolution_events_all,
    get_all_understanding,
    insert_understanding,
    insert_understanding_evolution,
    insert_understanding_history,
    latest_understanding_snapshot,
    understanding_evolution_all,
    understanding_evolution_for,
    understanding_history_for,
    update_understanding_status,
)
from ..knowledge.store import get_all_knowledge
from ..services.llm import _enabled as llm_enabled
from .confidence import (
    Contributor,
    aggregate_confidence,
    explain_score,
    status_from_confidence,
)
from .derivation import Candidate, detect
from .models import (
    Understanding,
    UnderstandingConfidence,
    UnderstandingStatus,
    now_iso,
)


@dataclass
class UnderstandingBuildResult:
    total: int
    created: int
    updated: int
    verified: int
    stable: int
    candidates: int
    events: int = 0
    note: str = ""

    def to_text(self) -> str:
        lines = [
            "Understanding Engine",
            "",
            f"Total understanding: {self.total}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Verified: {self.verified}",
            f"Stable: {self.stable}",
            f"Candidates: {self.candidates}",
            f"Evolution events: {self.events}",
            "",
        ]
        if self.note:
            lines.insert(2, f"Note: {self.note}")
            lines.insert(3, "")
        lines.append("Done.")
        return "\n".join(lines) + "\n"


class UnderstandingEngine:
    """Derives and stores understanding. WRITE entrypoint: build()."""

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- WRITE ----------------------------------------------------------------

    def build(self, build_at: Optional[str] = None) -> UnderstandingBuildResult:
        """Derive understanding from knowledge + knowledge-evolution.

        Idempotent: same knowledge -> same understanding. Existing understanding
        is preserved (its created_at/status history); only confidence/status
        advance upward (Candidate→...→Stable). Retired understanding is not
        auto-resurrected by a rebuild.
        """
        if build_at is None:
            build_at = now_iso()

        knowledge = get_all_knowledge(self.conn)
        evo_events = evolution_events_all(self.conn)

        # Confirm every understanding references valid knowledge ids (no dangling
        # citations; the Brain's confidence in understanding rests on this).
        valid_ids = {k.id for k in knowledge if k.id}

        candidates = detect(knowledge, evo_events)
        merged = self._merge_candidates(candidates)

        # When no LLM is configured and there's knowledge, note it.
        no_per_subject = False
        if knowledge and not llm_enabled():
            no_per_subject = True

        # Normalize to Understanding dataclasses so .status/.confidence are enums
        # (the db rows carry plain strings).
        existing_rows = [Understanding.from_row(r) for r in get_all_understanding(self.conn)]
        existing = {u.id or u._generate_id(): u for u in existing_rows}
        existing_by_key = {(u.type, u.subject): u for u in existing.values()}

        created = updated = verified = 0
        to_persist: List[Understanding] = []

        for cand in merged.values():
            # Drop contributors whose knowledge no longer exists (keeps citations
            # valid). If none remain, skip — understanding must cite knowledge.
            kid_ids = [i for i in cand.knowledge_ids if i in valid_ids]
            if not kid_ids:
                continue

            contributors = self._contributors_for(cand, knowledge)
            conf = aggregate_confidence(contributors)
            subject_k = self._subject_knowledge(knowledge, cand)
            status = status_from_confidence(conf, len(kid_ids))

            key = (cand.type, cand.subject)
            prev = existing_by_key.get(key)

            if prev is None:
                u = Understanding(
                    type=cand.type,
                    subject=cand.subject,
                    statement=cand.statement,
                    confidence=conf,
                    status=status,
                    knowledge_ids=kid_ids,
                    build_at=build_at,
                    created_at=build_at,
                    updated_at=build_at,
                )
                created += 1
            else:
                # Preserve lifecycle: a Retired understanding stays retired until
                # an explicit reactivation clears it (mirrors knowledge evolution).
                preserved = prev.status if prev.status == UnderstandingStatus.RETIRED else None
                u = Understanding(
                    type=cand.type,
                    subject=cand.subject,
                    statement=cand.statement if cand.statement else prev.statement,
                    confidence=conf,
                    status=preserved if preserved is not None else status,
                    knowledge_ids=list(dict.fromkeys(
                        prev.knowledge_ids.split(",") + kid_ids
                    )) if isinstance(prev.knowledge_ids, str)
                    else list(dict.fromkeys(prev.knowledge_ids + kid_ids)),
                    build_at=build_at,
                    created_at=prev.created_at,
                    updated_at=build_at,
                    retired_at=prev.retired_at,
                )
                updated += 1
                if prev.status in (UnderstandingStatus.OBSERVED, UnderstandingStatus.VERIFIED):
                    verified += 1
            to_persist.append(u)

        # Whole build is one atomic transaction: live rows, history, and
        # evolution are written together or not at all (Part F).
        with atomic(self.conn):
            insert_understanding(self.conn, [u.to_row() for u in to_persist])
            # Append-only history + evolution.
            n_events = self._record_evolution(build_at, to_persist)

        all_u = get_all_understanding(self.conn)
        candidates_n = sum(1 for u in all_u if u.status == UnderstandingStatus.CANDIDATE)
        stable_n = sum(1 for u in all_u if u.status == UnderstandingStatus.STABLE)

        note = "Per-subject understanding requires an LLM — none configured." if no_per_subject else ""

        return UnderstandingBuildResult(
            total=len(all_u),
            created=created,
            updated=updated,
            verified=verified,
            stable=stable_n,
            candidates=candidates_n,
            events=n_events,
            note=note,
        )

    # --- READ (never mutate) --------------------------------------------------

    def all_understanding(self) -> List[Understanding]:
        return [Understanding.from_row(r) for r in get_all_understanding(self.conn)]

    def understanding_by_id(self, uid: str) -> Optional[Understanding]:
        from ..db import get_understanding_by_id

        row = get_understanding_by_id(self.conn, uid)
        return Understanding.from_row(row) if row else None

    def understanding_by_type(self, utype: str) -> List[Understanding]:
        from ..db import get_understanding_by_type

        return [Understanding.from_row(r) for r in get_understanding_by_type(self.conn, utype)]

    def explain(self, uid: str) -> Tuple[Optional[Understanding], Dict, List, List]:
        """Return (understanding, score_breakdown, history, evolution)."""
        u = self.understanding_by_id(uid)
        if u is None:
            return None, {}, [], []
        knowledge = {k.id: k for k in get_all_knowledge(self.conn)}
        contributors = [
            Contributor(
                knowledge_id=i,
                source_type=knowledge[i].type.value if i in knowledge else "unknown",
                weight={"weak": 1, "medium": 2, "strong": 4}.get(
                    knowledge[i].confidence.value if i in knowledge else "weak", 1
                ),
                agrees=True,
            )
            for i in u.knowledge_ids
            if i in knowledge
        ]
        score, breakdown = explain_score(contributors)
        hist = understanding_history_for(self.conn, uid)
        evo = understanding_evolution_for(self.conn, uid)
        return u, breakdown, hist, evo

    def evolution_timeline(self) -> List[UnderstandingEvolutionRow]:
        return understanding_evolution_all(self.conn)

    def history_timeline(self, uid: str) -> List[UnderstandingHistoryRow]:
        return understanding_history_for(self.conn, uid)

    # --- internals ------------------------------------------------------------

    def _merge_candidates(self, candidates: List[Candidate]) -> Dict[tuple, Candidate]:
        """Union knowledge ids for candidates sharing (type, subject)."""
        out: Dict[tuple, Candidate] = {}
        for c in candidates:
            key = c.key()
            if key in out:
                # Keep the longest (most specific) statement; union knowledge ids.
                prev = out[key]
                merged_ids = list(dict.fromkeys(prev.knowledge_ids + c.knowledge_ids))
                out[key] = Candidate(
                    type=c.type,
                    subject=c.subject,
                    statement=prev.statement if len(prev.statement) >= len(c.statement)
                    else c.statement,
                    knowledge_ids=merged_ids,
                )
            else:
                out[key] = c
        return out

    def _subject_knowledge(self, knowledge: List, cand: Candidate):
        for k in knowledge:
            if k.id in cand.knowledge_ids:
                return k
        return None

    def _contributors_for(self, cand: Candidate, knowledge: List) -> List[Contributor]:
        by_id = {k.id: k for k in knowledge if k.id}
        out: List[Contributor] = []
        for i in cand.knowledge_ids:
            k = by_id.get(i)
            if k is None:
                continue
            out.append(Contributor(
                knowledge_id=i,
                source_type=k.type.value,
                weight={"weak": 1, "medium": 2, "strong": 4}.get(k.confidence.value, 1),
                agrees=True,
            ))
        return out

    def _record_evolution(
        self,
        build_at: str,
        to_persist: List[Understanding],
    ) -> int:
        """Append history snapshot + derive evolution events. Returns event count."""
        # Read the PRIOR snapshot first — the new snapshot is written below and
        # would otherwise overwrite it, leaving nothing to diff against.
        prev = {h.understanding_id: h for h in latest_understanding_snapshot(self.conn)}

        # 1) Full append-only snapshot of this build.
        insert_understanding_history(self.conn, [
            UnderstandingHistoryRow(
                build_at=build_at,
                understanding_id=u.id or u._generate_id(),
                type=u.type.value,
                subject=u.subject,
                statement=u.statement,
                confidence=u.confidence.value,
                status=u.status.value,
                knowledge_ids=",".join(u.knowledge_ids),
                created_at=u.created_at,
                updated_at=u.updated_at,
                reinforced_count=len(u.knowledge_ids),
            )
            for u in to_persist
        ])

        events: List[UnderstandingEvolutionRow] = []
        for u in to_persist:
            uid = u.id or u._generate_id()
            ev_ids = set(u.knowledge_ids)
            prev_h = prev.get(uid)

            if prev_h is None:
                events.append(self._event(
                    build_at, "Strengthened", uid, None, u.confidence.value,
                    None, u.status.value, None, u.statement,
                    f"Understanding emerged with {len(ev_ids)} supporting knowledge "
                    f"(status {u.status.value}).",
                    ",".join(sorted(ev_ids)),
                ))
                continue

            prev_conf = UnderstandingConfidence.from_str(prev_h.confidence)
            if self._conf_order(u.confidence) > self._conf_order(prev_conf):
                added = ev_ids - set(prev_h.knowledge_ids.split(","))
                events.append(self._event(
                    build_at, "Strengthened", uid, prev_h.confidence, u.confidence.value,
                    prev_h.status, u.status.value, prev_h.statement, u.statement,
                    f"Confidence grew {prev_conf.value}->{u.confidence.value} as "
                    f"supporting knowledge rose "
                    f"{len(prev_h.knowledge_ids.split(',')) if prev_h.knowledge_ids else 0}"
                    f"->{len(ev_ids)}.",
                    ",".join(sorted(added)),
                ))

            if self._status_rank(u.status) > self._status_rank(
                UnderstandingStatus.from_str(prev_h.status)
            ):
                events.append(self._event(
                    build_at, "Stabilized" if u.status == UnderstandingStatus.STABLE
                    else "Verified", uid, prev_h.confidence, u.confidence.value,
                    prev_h.status, u.status.value, prev_h.statement, u.statement,
                    f"Lifecycle advanced {prev_h.status}->{u.status.value}.",
                    ",".join(sorted(ev_ids)),
                ))

            if u.statement != prev_h.statement and u.confidence == prev_conf:
                events.append(self._event(
                    build_at, "Superseded", uid, prev_h.confidence, u.confidence.value,
                    prev_h.status, u.status.value, prev_h.statement, u.statement,
                    "Statement refined by newer knowledge; prior wording retained.",
                    ",".join(sorted(ev_ids)),
                ))

        insert_understanding_evolution(self.conn, events)
        return len(events)

    @staticmethod
    def _event(
        build_at, etype, uid, prev_conf, new_conf, prev_status, new_status,
        prev_stmt, new_stmt, reason, knowledge_ids,
    ) -> UnderstandingEvolutionRow:
        return UnderstandingEvolutionRow(
            id=f"{build_at}:{etype}:{uid}",
            build_at=build_at,
            event_type=etype,
            understanding_id=uid,
            previous_confidence=prev_conf,
            new_confidence=new_conf,
            previous_status=prev_status,
            new_status=new_status,
            previous_statement=prev_stmt,
            new_statement=new_stmt,
            reason=reason,
            knowledge_ids=knowledge_ids,
            timestamp=build_at,
        )

    @staticmethod
    def _conf_order(c: UnderstandingConfidence) -> int:
        return {"weak": 0, "medium": 1, "strong": 2}[c]

    @staticmethod
    def _status_rank(s: UnderstandingStatus) -> int:
        return {
            UnderstandingStatus.CANDIDATE: 0,
            UnderstandingStatus.OBSERVED: 1,
            UnderstandingStatus.VERIFIED: 2,
            UnderstandingStatus.STABLE: 3,
            UnderstandingStatus.RETIRED: 4,
        }[s]
