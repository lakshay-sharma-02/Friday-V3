"""Initiative Engine (Milestone 8.4).

Write-only layer above Understanding. Derives durable long-running engineering
initiatives from accumulated understanding (plus knowledge-evolution events and
knowledge). The Brain consumes initiatives as another evidence source; it never
computes them.

Idempotent. Deterministic. Running `build` twice on the same evidence produces
the same initiative rows (INSERT OR REPLACE on deterministic ids + INSERT OR
IGNORE on evolution/relationship events). Merge and split are explicit, evidence-
driven operations that preserve parent/child references forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..db import (
    atomic,
    InitiativeEvolutionRow,
    InitiativeHistoryRow,
    InitiativeRelationshipRow,
    get_all_initiatives,
    get_all_understanding,
    get_initiative_by_id,
    insert_initiative,
    insert_initiative_evolution,
    insert_initiative_history,
    insert_initiative_relationships,
    initiative_evolution_all,
    initiative_evolution_for,
    initiative_history_for,
    initiative_relationships_all,
    latest_initiative_snapshot,
)
from ..knowledge.store import get_all_knowledge
from .confidence import (
    Contributor,
    aggregate_confidence,
    explain_score,
    status_from_confidence,
)
from .derivation import Candidate, detect
from .models import (
    Initiative,
    InitiativeConfidence,
    InitiativeStatus,
    now_iso,
)
from ..understanding import Understanding


@dataclass
class InitiativeBuildResult:
    total: int
    created: int
    updated: int
    active: int
    review: int
    candidates: int
    events: int = 0

    def to_text(self) -> str:
        lines = [
            "Initiative Engine",
            "",
            f"Total initiatives: {self.total}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Active: {self.active}",
            f"Review: {self.review}",
            f"Candidates: {self.candidates}",
            f"Evolution events: {self.events}",
            "",
            "Done.",
        ]
        return "\n".join(lines) + "\n"


class InitiativeEngine:
    """Derives and stores initiatives. WRITE entrypoint: build()."""

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- WRITE ----------------------------------------------------------------

    def build(self, build_at: Optional[str] = None) -> InitiativeBuildResult:
        """Derive initiatives from understanding + knowledge-evolution + knowledge.

        Idempotent: same lower-layer state -> same initiatives. Existing
        initiatives are preserved (created_at/status history); only confidence/
        status advance upward. Retired/archived initiatives are not auto-
        resurrected by a rebuild.
        """
        if build_at is None:
            build_at = now_iso()

        understanding = [Understanding.from_row(r) for r in get_all_understanding(self.conn)]
        knowledge = get_all_knowledge(self.conn)
        evo_events = self._evolution_events()

        valid_uids = {u.id for u in understanding if u.id}
        valid_kids = {k.id for k in knowledge if k.id}

        candidates = detect(understanding, knowledge, evo_events)
        merged = self._merge_candidates(candidates)

        existing_rows = [Initiative.from_row(r) for r in get_all_initiatives(self.conn)]
        existing_by_key = {(i.type, i.title): i for i in existing_rows}

        created = updated = 0
        to_persist: List[Initiative] = []

        for cand in merged.values():
            # Drop dangling citations; if none remain, skip (must cite evidence).
            uids = [i for i in cand.understanding_ids if i in valid_uids]
            kids = [i for i in cand.knowledge_ids if i in valid_kids]
            if not uids and not kids:
                continue

            contributors = self._contributors_for(cand, understanding, knowledge)
            conf = aggregate_confidence(contributors, cand.repos)
            status = status_from_confidence(conf, len(uids) + len(kids))
            repos = sorted(set(cand.repos))

            key = (cand.type, cand.title)
            prev = existing_by_key.get(key)

            if prev is None:
                i = Initiative(
                    type=cand.type,
                    title=cand.title,
                    statement=cand.statement,
                    status=status,
                    confidence=conf,
                    participating_repositories=repos,
                    understanding_ids=uids,
                    knowledge_ids=kids,
                    build_at=build_at,
                    started_at=build_at if status != InitiativeStatus.CANDIDATE else None,
                    created_at=build_at,
                    updated_at=build_at,
                )
                created += 1
            else:
                preserved = prev.status if prev.status in (
                    InitiativeStatus.ARCHIVED, InitiativeStatus.COMPLETED,
                    InitiativeStatus.DORMANT,
                ) else None
                i = Initiative(
                    type=cand.type,
                    title=cand.title,
                    statement=prev.statement or cand.statement,
                    status=preserved if preserved is not None else status,
                    confidence=conf,
                    participating_repositories=sorted(set(
                        prev.participating_repositories + repos)),
                    understanding_ids=list(dict.fromkeys(prev.understanding_ids + uids)),
                    knowledge_ids=list(dict.fromkeys(prev.knowledge_ids + kids)),
                    build_at=build_at,
                    started_at=prev.started_at or (
                        build_at if status != InitiativeStatus.CANDIDATE else None),
                    completed_at=prev.completed_at,
                    created_at=prev.created_at,
                    updated_at=build_at,
                )
                updated += 1
            to_persist.append(i)

        # Whole build is one atomic transaction (Part F).
        with atomic(self.conn):
            insert_initiative(self.conn, [i.to_row() for i in to_persist])
            n_events = self._record_evolution(build_at, to_persist)

        all_i = get_all_initiatives(self.conn)
        cand_n = sum(1 for i in all_i if i.status == InitiativeStatus.CANDIDATE)
        active_n = sum(1 for i in all_i if i.status == InitiativeStatus.ACTIVE)
        review_n = sum(1 for i in all_i if i.status == InitiativeStatus.REVIEW)

        return InitiativeBuildResult(
            total=len(all_i), created=created, updated=updated,
            active=active_n, review=review_n, candidates=cand_n,
            events=n_events,
        )

    # --- MERGE ----------------------------------------------------------------
    # Combine >=2 existing initiatives into one, preserving parent references.

    def merge(
        self, parent_ids: List[str], title: Optional[str] = None,
        build_at: Optional[str] = None,
    ) -> Optional[str]:
        if build_at is None:
            build_at = now_iso()
        parents = [self.initiative_by_id(p) for p in parent_ids]
        parents = [p for p in parents if p is not None]
        if len(parents) < 2:
            return None

        child_title = title or " + ".join(p.title for p in parents)
        child_type = parents[0].type
        uids = list(dict.fromkeys(sum((p.understanding_ids for p in parents), [])))
        kids = list(dict.fromkeys(sum((p.knowledge_ids for p in parents), [])))
        repos = sorted(set(sum((p.participating_repositories for p in parents), [])))

        contributors = self._contributors_for_ids(uids, kids)
        conf = aggregate_confidence(contributors, repos)
        status = status_from_confidence(conf, len(uids) + len(kids))

        child = Initiative(
            type=child_type, title=child_title, status=status, confidence=conf,
            participating_repositories=repos, understanding_ids=uids, knowledge_ids=kids,
            build_at=build_at, started_at=build_at,
            created_at=build_at, updated_at=build_at,
        )
        insert_initiative(self.conn, [child.to_row()])
        cid = child.id or child._generate_id()

        self._record_relationships(build_at, "merge", parent_ids, [cid], uids, kids,
                                   f"Initiatives merged into '{child_title}'.")
        # Mark parents archived (a merged initiative supersedes its parts).
        for p in parents:
            pid = p.id or p._generate_id()
            self._transition(pid, InitiativeStatus.ARCHIVED, build_at,
                             reason=f"Merged into '{child_title}'.")
        return cid

    # --- SPLIT ----------------------------------------------------------------
    # Break one initiative into >=2 children, retaining the parent reference.

    def split(
        self, parent_id: str, titles: List[str],
        build_at: Optional[str] = None,
    ) -> List[str]:
        if build_at is None:
            build_at = now_iso()
        parent = self.initiative_by_id(parent_id)
        if parent is None or len(titles) < 2:
            return []
        parent_uids = parent.understanding_ids
        parent_kids = parent.knowledge_ids
        parent_repos = parent.participating_repositories
        pid = parent.id or parent._generate_id()

        # Distribute understanding ids round-robin across children.
        children: List[Initiative] = []
        cids: List[str] = []
        for idx, t in enumerate(titles):
            uids = [u for j, u in enumerate(parent_uids) if j % len(titles) == idx]
            kids = [k for j, k in enumerate(parent_kids) if j % len(titles) == idx]
            # Repos shared by all children (they co-existed in the parent).
            contributors = self._contributors_for_ids(uids, kids)
            conf = aggregate_confidence(contributors, parent_repos)
            status = status_from_confidence(conf, len(uids) + len(kids))
            child = Initiative(
                type=parent.type, title=t, status=status, confidence=conf,
                participating_repositories=list(parent_repos),
                understanding_ids=uids, knowledge_ids=kids,
                build_at=build_at, started_at=build_at,
                created_at=build_at, updated_at=build_at,
            )
            insert_initiative(self.conn, [child.to_row()])
            children.append(child)
            cids.append(child.id or child._generate_id())

        self._record_relationships(build_at, "split", [pid], cids, parent_uids,
                                   parent_kids, f"Initiative split into {len(titles)} parts.")
        self._transition(pid, InitiativeStatus.ARCHIVED, build_at,
                         reason=f"Split into: {', '.join(titles)}.")
        return cids

    # --- READ (never mutate) --------------------------------------------------

    def all_initiatives(self) -> List[Initiative]:
        return [Initiative.from_row(r) for r in get_all_initiatives(self.conn)]

    def initiative_by_id(self, iid: str) -> Optional[Initiative]:
        row = get_initiative_by_id(self.conn, iid)
        return Initiative.from_row(row) if row else None

    def initiatives_by_type(self, itype: str) -> List[Initiative]:
        from ..db import get_initiative_by_type
        return [Initiative.from_row(r) for r in get_initiative_by_type(self.conn, itype)]

    def explain(self, iid: str) -> Tuple[Optional[Initiative], Dict, List, List, List]:
        """Return (initiative, score_breakdown, history, evolution, relationships)."""
        i = self.initiative_by_id(iid)
        if i is None:
            return None, {}, [], [], []
        understanding = {u.id: Understanding.from_row(u) for u in get_all_understanding(self.conn)}
        knowledge = {k.id: k for k in get_all_knowledge(self.conn)}
        contributors = self._build_contributors(i, understanding, knowledge)
        score, breakdown = explain_score(contributors, i.participating_repositories)
        hist = initiative_history_for(self.conn, iid)
        evo = initiative_evolution_for(self.conn, iid)
        rels = [
            r for r in initiative_relationships_all(self.conn)
            if iid in (r.parent_ids.split(",") + r.child_ids.split(","))
        ]
        return i, breakdown, hist, evo, rels

    def timeline(self) -> List[InitiativeEvolutionRow]:
        return initiative_evolution_all(self.conn)

    def relationships(self) -> List[InitiativeRelationshipRow]:
        return initiative_relationships_all(self.conn)

    # --- internals ------------------------------------------------------------

    def _evolution_events(self):
        from ..db import evolution_events_all
        return evolution_events_all(self.conn)

    def _merge_candidates(self, candidates: List[Candidate]) -> Dict[tuple, Candidate]:
        out: Dict[tuple, Candidate] = {}
        for c in candidates:
            key = c.key()
            if key in out:
                prev = out[key]
                merged_ids = list(dict.fromkeys(prev.understanding_ids + c.understanding_ids))
                merged_k = list(dict.fromkeys(prev.knowledge_ids + c.knowledge_ids))
                merged_r = sorted(set(prev.repos + c.repos))
                out[key] = Candidate(
                    type=c.type, title=c.title,
                    statement=prev.statement if len(prev.statement) >= len(c.statement)
                    else c.statement,
                    understanding_ids=merged_ids, knowledge_ids=merged_k, repos=merged_r,
                )
            else:
                out[key] = c
        return out

    def _contributors_for(
        self, cand: Candidate, understanding, knowledge
    ) -> List[Contributor]:
        u_by = {u.id: u for u in understanding if u.id}
        k_by = {k.id: k for k in knowledge if k.id}
        out: List[Contributor] = []
        for i in cand.understanding_ids:
            u = u_by.get(i)
            if u is None:
                continue
            out.append(Contributor(
                evidence_id=i, source_type="understanding",
                weight={"weak": 1, "medium": 2, "strong": 4}.get(
                    u.confidence.value if hasattr(u, "confidence") else "weak", 1),
                repo="", agrees=True,
            ))
        for i in cand.knowledge_ids:
            k = k_by.get(i)
            if k is None:
                continue
            out.append(Contributor(
                evidence_id=i, source_type="knowledge",
                weight={"weak": 1, "medium": 2, "strong": 4}.get(
                    k.confidence.value if hasattr(k, "confidence") else "weak", 1),
                repo="", agrees=True,
            ))
        return out

    def _contributors_for_ids(self, uids, kids) -> List[Contributor]:
        understanding = {u.id: Understanding.from_row(u) for u in get_all_understanding(self.conn)}
        knowledge = {k.id: k for k in get_all_knowledge(self.conn)}
        cand = Candidate(
            type=None, title="", understanding_ids=list(uids), knowledge_ids=list(kids))
        return self._contributors_for(cand, list(understanding.values()),
                                 list(knowledge.values()))

    def _build_contributors(self, i: Initiative, understanding, knowledge) -> List[Contributor]:
        cand = Candidate(
            type=i.type, title=i.title,
            understanding_ids=i.understanding_ids, knowledge_ids=i.knowledge_ids)
        return self._contributors_for(cand, list(understanding.values()),
                                 list(knowledge.values()))

    def _record_evolution(
        self, build_at: str, to_persist: List[Initiative],
    ) -> int:
        """Append history snapshot + derive evolution events. Returns event count."""
        prev = {h.initiative_id: h for h in latest_initiative_snapshot(self.conn)}
        insert_initiative_history(self.conn, [
            InitiativeHistoryRow(
                build_at=build_at,
                initiative_id=i.id or i._generate_id(),
                title=i.title,
                initiative_type=i.type.value,
                status=i.status.value,
                confidence=i.confidence.value,
                started_at=i.started_at,
                completed_at=i.completed_at,
                participating_repositories=",".join(i.participating_repositories),
                understanding_ids=",".join(i.understanding_ids),
                knowledge_ids=",".join(i.knowledge_ids),
            )
            for i in to_persist
        ])

        events: List[InitiativeEvolutionRow] = []
        for i in to_persist:
            iid = i.id or i._generate_id()
            prev_h = prev.get(iid)
            if prev_h is None:
                events.append(self._event(
                    build_at, "Started", iid, None, i.status.value, None,
                    i.confidence.value, None, i.title,
                    f"Initiative emerged with {len(i.understanding_ids)} understanding "
                    f"and {len(i.knowledge_ids)} knowledge (status {i.status.value}).",
                    [], [], i.understanding_ids, i.knowledge_ids,
                ))
                continue
            prev_conf = InitiativeConfidence.from_str(prev_h.confidence)
            if self._conf_order(i.confidence) > self._conf_order(prev_conf):
                events.append(self._event(
                    build_at, "Strengthened", iid, prev_h.status, i.status.value,
                    prev_h.confidence, i.confidence.value, prev_h.title, i.title,
                    f"Confidence grew {prev_conf.value}->{i.confidence.value}.",
                    [], [], i.understanding_ids, i.knowledge_ids,
                ))
            if self._status_rank(i.status) > self._status_rank(
                    InitiativeStatus.from_str(prev_h.status)):
                et = "Completed" if i.status == InitiativeStatus.COMPLETED else (
                    "Archived" if i.status == InitiativeStatus.ARCHIVED else "Advanced")
                events.append(self._event(
                    build_at, et, iid, prev_h.status, i.status.value,
                    prev_h.confidence, i.confidence.value, prev_h.title, i.title,
                    f"Lifecycle advanced {prev_h.status}->{i.status.value}.",
                    [], [], i.understanding_ids, i.knowledge_ids,
                ))
            if i.title != prev_h.title and i.confidence == prev_conf:
                events.append(self._event(
                    build_at, "Renamed", iid, prev_h.status, i.status.value,
                    prev_h.confidence, i.confidence.value, prev_h.title, i.title,
                    "Title refined; prior wording retained.",
                    [], [], i.understanding_ids, i.knowledge_ids,
                ))
        insert_initiative_evolution(self.conn, events)
        return len(events)

    def _record_relationships(
        self, build_at, rtype, parent_ids, child_ids, uids, kids, note,
    ) -> None:
        from ..db import InitiativeRelationshipRow
        row = InitiativeRelationshipRow(
            id=f"{build_at}:{rtype}:{':'.join(child_ids)}",
            relationship_type=rtype,
            parent_ids=",".join(parent_ids),
            child_ids=",".join(child_ids),
            build_at=build_at, created_at=build_at, note=note,
        )
        insert_initiative_relationships(self.conn, [row])

    def _transition(self, iid: str, status: InitiativeStatus, build_at: str,
                    reason: str) -> None:
        from ..db import update_initiative_status
        completed = build_at if status == InitiativeStatus.COMPLETED else None
        update_initiative_status(self.conn, iid, status.value, completed)
        ev = self._event(
            build_at, "Advanced" if status != InitiativeStatus.ARCHIVED else "Archived",
            iid, None, status.value, None, None, None, None,
            reason, [], [], [], [],
        )
        insert_initiative_evolution(self.conn, [ev])

    @staticmethod
    def _event(
        build_at, etype, iid, prev_status, new_status, prev_conf, new_conf,
        prev_title, new_title, reason, parent_ids, child_ids, uids, kids,
    ) -> InitiativeEvolutionRow:
        return InitiativeEvolutionRow(
            id=f"{build_at}:{etype}:{iid}",
            build_at=build_at,
            event_type=etype,
            initiative_id=iid,
            previous_status=prev_status,
            new_status=new_status,
            previous_confidence=prev_conf,
            new_confidence=new_conf,
            previous_title=prev_title,
            new_title=new_title,
            reason=reason,
            parent_ids=",".join(parent_ids),
            child_ids=",".join(child_ids),
            understanding_ids=",".join(uids),
            knowledge_ids=",".join(kids),
            timestamp=build_at,
        )

    @staticmethod
    def _conf_order(c: InitiativeConfidence) -> int:
        return {"weak": 0, "medium": 1, "strong": 2}[c]

    @staticmethod
    def _status_rank(s: InitiativeStatus) -> int:
        return {
            InitiativeStatus.CANDIDATE: 0, InitiativeStatus.STARTED: 1,
            InitiativeStatus.ACTIVE: 2, InitiativeStatus.BLOCKED: 3,
            InitiativeStatus.REVIEW: 4, InitiativeStatus.COMPLETED: 5,
            InitiativeStatus.DORMANT: 6, InitiativeStatus.ARCHIVED: 7,
        }[s]
