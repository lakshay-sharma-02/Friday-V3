"""Insight Engine (Milestone 8.5).

Write-only layer above Initiatives. Derives rare, high-value engineering
insights from accumulated understanding (plus initiatives and
knowledge-evolution/knowledge). The Brain consumes insights as another
evidence source; it never computes them.

Idempotent. Deterministic. Running `build` twice on the same evidence produces
the same insight rows (INSERT OR REPLACE on deterministic ids + INSERT OR
IGNORE on evolution events).

EPHEMERALITY (the key design property): insights are not permanent. A build
that no longer finds the triggering conditions for an existing (non-retired)
insight RETIRES it. This keeps the layer a live "what deserves attention now"
feed, not another static fact table. Once retired, a future build that finds
the trigger again re-activates (re-emits) the same insight id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..db import (
    atomic,
    InsightEvolutionRow,
    InsightHistoryRow,
    get_all_insights,
    get_all_understanding,
    get_insight_by_id,
    insert_insight,
    insert_insight_evolution,
    insert_insight_history,
    insight_evolution_all,
    insight_evolution_for,
    latest_insight_snapshot,
    update_insight_status,
)
from ..initiative import InitiativeEngine
from ..knowledge.store import get_all_knowledge
from ..understanding import Understanding
from ..services.llm import _enabled as llm_enabled
from .confidence import (
    Contributor,
    aggregate_confidence,
    explain_score,
    status_from_confidence,
)
from .derivation import Candidate, detect
from .models import Insight, InsightConfidence, InsightStatus, now_iso


@dataclass
class InsightBuildResult:
    total: int
    created: int
    updated: int
    retired: int
    active: int
    events: int = 0
    note: str = ""

    def to_text(self) -> str:
        lines = [
            "Insight Engine",
            "",
            f"Total insights: {self.total}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Retired: {self.retired}",
            f"Active: {self.active}",
            f"Evolution events: {self.events}",
            "",
        ]
        if self.note:
            lines.insert(2, f"Note: {self.note}")
            lines.insert(3, "")
        lines.append("Done.")
        return "\n".join(lines) + "\n"


class InsightEngine:
    """Derives and stores insights. WRITE entrypoint: build()."""

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- WRITE ----------------------------------------------------------------

    def build(self, build_at: Optional[str] = None) -> InsightBuildResult:
        """Derive insights from understanding + initiatives + knowledge.

        Idempotent: same lower-layer state -> same insights. Insights whose
        triggering rules no longer fire are RETIRED (ephemerality). A later
        build that re-finds the trigger re-activates the same id.
        """
        if build_at is None:
            build_at = now_iso()

        understanding = [Understanding.from_row(r) for r in get_all_understanding(self.conn)]
        knowledge = get_all_knowledge(self.conn)
        initiatives = InitiativeEngine(self.conn).all_initiatives()

        valid_uids = {u.id for u in understanding if u.id}
        valid_iids = {i.id for i in initiatives if i.id}
        valid_kids = {k.id for k in knowledge if k.id}

        candidates = detect(understanding, initiatives, knowledge)
        merged = self._merge_candidates(candidates)

        existing_rows = [Insight.from_row(r) for r in get_all_insights(self.conn)]
        existing_by_key = {(i.type, i.title): i for i in existing_rows}

        created = updated = retired = 0
        to_persist: List[Insight] = []
        fired_keys = set()

        for cand in merged.values():
            # Drop dangling citations; if none remain, skip (must cite evidence).
            uids = [i for i in cand.understanding_ids if i in valid_uids]
            iids = [i for i in cand.initiative_ids if i in valid_iids]
            kids = [i for i in cand.knowledge_ids if i in valid_kids]
            if (not uids and not iids and not kids) or not _qualifies_local(cand):
                continue

            contributors = self._contributors_for(cand, understanding, initiatives, knowledge)
            conf = aggregate_confidence(contributors, cand.repos)
            status = status_from_confidence(
                conf, len(uids) + len(iids) + len(kids))
            repos = sorted(set(cand.repos))

            key = (cand.type, cand.title)
            fired_keys.add(key)
            prev = existing_by_key.get(key)

            if prev is None:
                i = Insight(
                    type=cand.type, title=cand.title, statement=cand.statement,
                    status=status, confidence=conf,
                    understanding_ids=uids, initiative_ids=iids, knowledge_ids=kids,
                    build_at=build_at,
                    started_at=build_at if status != InsightStatus.CANDIDATE else None,
                    created_at=build_at, updated_at=build_at,
                )
                created += 1
            else:
                was_retired = prev.status == InsightStatus.RETIRED
                i = Insight(
                    type=cand.type, title=cand.title,
                    statement=prev.statement or cand.statement,
                    status=status,
                    confidence=conf,
                    understanding_ids=list(dict.fromkeys(prev.understanding_ids + uids)),
                    initiative_ids=list(dict.fromkeys(prev.initiative_ids + iids)),
                    knowledge_ids=list(dict.fromkeys(prev.knowledge_ids + kids)),
                    build_at=build_at,
                    started_at=prev.started_at or (
                        build_at if status != InsightStatus.CANDIDATE else None),
                    retired_at=prev.retired_at if was_retired else None,
                    created_at=prev.created_at, updated_at=build_at,
                )
                updated += 1
            to_persist.append(i)

        # Retire insights whose triggering rule did not fire this build.
        for prev in existing_rows:
            if prev.status == InsightStatus.RETIRED:
                continue
            key = (prev.type, prev.title)
            if key not in fired_keys:
                self._retire(prev, build_at)
                retired += 1

        # Whole build is one atomic transaction (Part F).
        with atomic(self.conn):
            insert_insight(self.conn, [i.to_row() for i in to_persist])
            n_events = self._record_evolution(build_at, to_persist)

        all_i = get_all_insights(self.conn)
        active_n = sum(1 for i in all_i
                       if i.status != InsightStatus.RETIRED)

        # Check if understanding exists but no LLM is configured
        staging = get_all_understanding(self.conn)
        note = ""
        if staging and not llm_enabled():
            note = "Insights require an LLM — none configured."

        return InsightBuildResult(
            total=len(all_i), created=created, updated=updated,
            retired=retired, active=active_n, events=n_events,
            note=note,
        )

    # --- READ (never mutate) --------------------------------------------------

    def all_insights(self) -> List[Insight]:
        return [Insight.from_row(r) for r in get_all_insights(self.conn)]

    def active_insights(self) -> List[Insight]:
        return [i for i in self.all_insights()
                if i.status != InsightStatus.RETIRED]

    def insight_by_id(self, iid: str) -> Optional[Insight]:
        row = get_insight_by_id(self.conn, iid)
        return Insight.from_row(row) if row else None

    def insights_by_type(self, itype: str) -> List[Insight]:
        rows = self.conn.execute(
            "SELECT * FROM insights WHERE insight_type = ? "
            "ORDER BY updated_at DESC", (itype,)).fetchall()
        return [Insight.from_row(r) for r in rows]

    def explain(self, iid: str) -> Tuple[Optional[Insight], Dict, List, List, List, List]:
        """Return (insight, score_breakdown, u_ids, i_ids, k_ids, evolution)."""
        i = self.insight_by_id(iid)
        if i is None:
            return None, {}, [], [], [], []
        understanding = {u.id: Understanding.from_row(u) for u in get_all_understanding(self.conn)}
        knowledge = {k.id: k for k in get_all_knowledge(self.conn)}
        initiatives = {ix.id: ix for ix in InitiativeEngine(self.conn).all_initiatives()}
        contributors = self._build_contributors(i, understanding, initiatives, knowledge)
        score, breakdown = explain_score(contributors, [])
        evo = insight_evolution_for(self.conn, iid)
        return (i, breakdown,
                i.understanding_ids, i.initiative_ids, i.knowledge_ids, evo)

    def evolution(self) -> List[InsightEvolutionRow]:
        return insight_evolution_all(self.conn)

    # --- internals ------------------------------------------------------------

    def _retire(self, prev: Insight, build_at: str) -> None:
        update_insight_status(self.conn, prev.id or prev._generate_id(),
                              InsightStatus.RETIRED.value, build_at)
        ev = InsightEngine._event(
            build_at, "Retired", prev.id or prev._generate_id(), prev.status.value,
            InsightStatus.RETIRED.value, prev.confidence.value, prev.confidence.value,
            prev.statement, prev.statement,
            "Triggering conditions no longer hold; insight retired (ephemeral).",
            [], [], [],
        )
        insert_insight_evolution(self.conn, [ev])

    def _merge_candidates(self, candidates: List[Candidate]) -> Dict[tuple, Candidate]:
        out: Dict[tuple, Candidate] = {}
        for c in candidates:
            key = c.key()
            if key in out:
                prev = out[key]
                out[key] = Candidate(
                    type=c.type, title=c.title,
                    statement=prev.statement if len(prev.statement) >= len(c.statement)
                    else c.statement,
                    understanding_ids=list(dict.fromkeys(
                        prev.understanding_ids + c.understanding_ids)),
                    initiative_ids=list(dict.fromkeys(
                        prev.initiative_ids + c.initiative_ids)),
                    knowledge_ids=list(dict.fromkeys(
                        prev.knowledge_ids + c.knowledge_ids)),
                    repos=sorted(set(prev.repos + c.repos)),
                )
            else:
                out[key] = c
        return out

    def _contributors_for(
        self, cand: Candidate, understanding, initiatives, knowledge
    ) -> List[Contributor]:
        u_by = {u.id: u for u in understanding if u.id}
        i_by = {i.id: i for i in initiatives if i.id}
        k_by = {k.id: k for k in knowledge if k.id}
        out: List[Contributor] = []

        def wt(obj):
            c = getattr(obj, "confidence", None)
            v = getattr(c, "value", "weak") if c is not None else "weak"
            return {"weak": 1, "medium": 2, "strong": 4}.get(v, 1)

        # Map understanding -> its cited knowledge's repo provenance so the
        # cross-project multiplier can reward multi-repo reinforcement.
        k_repos = {k.id: [r for r in getattr(k, "evidence_ids", []) or []]
                   for k in knowledge if k.id}
        u_repos = {}
        for u in understanding:
            if not u.id:
                continue
            repos: List[str] = []
            for kid in getattr(u, "knowledge_ids", []) or []:
                repos.extend(k_repos.get(kid, []))
            u_repos[u.id] = sorted(set(repos))

        for i in cand.understanding_ids:
            u = u_by.get(i)
            if u is None:
                continue
            out.append(Contributor(evidence_id=i, source_type="understanding",
                                   weight=wt(u),
                                   repo=(u_repos.get(i) or [""])[0] or "",
                                   agrees=True))
        for i in cand.initiative_ids:
            ix = i_by.get(i)
            if ix is None:
                continue
            repos = getattr(ix, "participating_repositories", []) or []
            out.append(Contributor(evidence_id=i, source_type="initiative",
                                   weight=wt(ix), repo=(repos or [""])[0] or "",
                                   agrees=True))
        for i in cand.knowledge_ids:
            k = k_by.get(i)
            if k is None:
                continue
            repos = getattr(k, "evidence_ids", []) or []
            out.append(Contributor(evidence_id=i, source_type="knowledge",
                                   weight=wt(k), repo=(repos or [""])[0] or "",
                                   agrees=True))
        return out

    def _build_contributors(self, i: Insight, understanding, initiatives, knowledge):
        cand = Candidate(
            type=i.type, title=i.title, statement=i.statement,
            understanding_ids=i.understanding_ids,
            initiative_ids=i.initiative_ids, knowledge_ids=i.knowledge_ids)
        return self._contributors_for(
            cand, list(understanding.values()),
            list(initiatives.values()), list(knowledge.values()))

    def _record_evolution(
        self, build_at: str, to_persist: List[Insight],
    ) -> int:
        prev = {h.insight_id: h for h in latest_insight_snapshot(self.conn)}
        insert_insight_history(self.conn, [
            InsightHistoryRow(
                build_at=build_at,
                insight_id=i.id or i._generate_id(),
                title=i.title, insight_type=i.type.value, statement=i.statement,
                status=i.status.value, confidence=i.confidence.value,
                understanding_ids=",".join(i.understanding_ids),
                initiative_ids=",".join(i.initiative_ids),
                knowledge_ids=",".join(i.knowledge_ids),
            )
            for i in to_persist
        ])

        events: List[InsightEvolutionRow] = []
        for i in to_persist:
            iid = i.id or i._generate_id()
            prev_h = prev.get(iid)
            if prev_h is None:
                events.append(InsightEngine._event(
                    build_at, "Started", iid, None, i.status.value, None,
                    i.confidence.value, None, i.title,
                    f"Insight emerged with {i.understanding_count} understanding, "
                    f"{i.initiative_count} initiative, {i.knowledge_count} knowledge.",
                    i.understanding_ids, i.initiative_ids, i.knowledge_ids))
                continue
            prev_conf = InsightConfidence.from_str(prev_h.confidence)
            if self._conf_order(i.confidence) > self._conf_order(prev_conf):
                events.append(InsightEngine._event(
                    build_at, "Strengthened", iid, prev_h.status, i.status.value,
                    prev_h.confidence, i.confidence.value, prev_h.title, i.title,
                    f"Confidence grew {prev_conf.value}->{i.confidence.value}.",
                    i.understanding_ids, i.initiative_ids, i.knowledge_ids))
            if self._status_rank(i.status) > self._status_rank(
                    InsightStatus.from_str(prev_h.status)):
                et = "Stable" if i.status == InsightStatus.STABLE else (
                    "Verified" if i.status == InsightStatus.VERIFIED else "Advanced")
                events.append(InsightEngine._event(
                    build_at, et, iid, prev_h.status, i.status.value,
                    prev_h.confidence, i.confidence.value, prev_h.title, i.title,
                    f"Lifecycle advanced {prev_h.status}->{i.status.value}.",
                    i.understanding_ids, i.initiative_ids, i.knowledge_ids))
            if i.title != prev_h.title and prev_conf == i.confidence:
                events.append(InsightEngine._event(
                    build_at, "Renamed", iid, prev_h.status, i.status.value,
                    prev_h.confidence, i.confidence.value, prev_h.title, i.title,
                    "Title refined; prior wording retained.",
                    i.understanding_ids, i.initiative_ids, i.knowledge_ids))
        insert_insight_evolution(self.conn, events)
        return len(events)

    @staticmethod
    def _event(build_at, etype, iid, prev_status, new_status, prev_conf,
                new_conf, prev_title, new_title, reason, uids, iids, kids):
        return InsightEvolutionRow(
            id=f"{build_at}:{etype}:{iid}",
            build_at=build_at, event_type=etype, insight_id=iid,
            previous_status=prev_status, new_status=new_status,
            previous_confidence=prev_conf, new_confidence=new_conf,
            previous_statement=prev_title, new_statement=new_title, reason=reason,
            understanding_ids=",".join(uids), initiative_ids=",".join(iids),
            knowledge_ids=",".join(kids), timestamp=build_at)

    @staticmethod
    def _conf_order(c: InsightConfidence) -> int:
        return {"weak": 0, "medium": 1, "strong": 2}[c]

    @staticmethod
    def _status_rank(s: InsightStatus) -> int:
        return {
            InsightStatus.CANDIDATE: 0, InsightStatus.OBSERVED: 1,
            InsightStatus.VERIFIED: 2, InsightStatus.STABLE: 3,
            InsightStatus.RETIRED: 4,
        }[s]


def _qualifies_local(c: Candidate) -> bool:
    """Mirror derivation._qualifies for the engine's post-validity check."""
    n_u = len(c.understanding_ids)
    n_i = len(c.initiative_ids)
    n_k = len(c.knowledge_ids)
    if n_u >= 2:
        return True
    if n_u >= 1 and n_i >= 1:
        return True
    if n_k >= 3:
        return True
    return False
