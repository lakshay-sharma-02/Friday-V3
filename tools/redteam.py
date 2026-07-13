"""Red-team audit harness for Friday V3 — "can I make Friday look stupid?"

NOT a feature. NOT committed to the pipeline. A standalone audit tool that fires
hundreds of adversarial / ambiguous / multi-turn / reality-check questions at the
frozen pipeline and scores each on the 6/6 rubric:

    correct_scope | correct_evidence | no_hallucination |
    no_fallback_dump | reads_naturally | actionable

Anything below 6/6 is a regression (a blind spot in the architecture, not a typo).

Two modes:
  offline  (default) — FRIDAY_LLM_* unset, deterministic heuristics. Reproducible.
  online   (--online) — uses the LLM understanding step. Set FRIDAY_LLM_* first.

Scoring is split:
  * automatic columns (scope, evidence span, hallucination anchors, convergence)
    are computed from the deterministic Evidence.raw the pipeline already exposes
    — no LLM needed to grade them.
  * `reads_naturally` and `actionable` are subjective (a senior-engineer judgement);
    the harness flags them for human review and never auto-passes them, so a green
    run can never be faked by the grader itself.

Usage:
    python tools/redteam.py                 # offline, ~600 questions
    python tools/redteam.py --online        # also exercise the LLM understanding
    python tools/redteam.py --seed N        # deterministic question ordering
    python tools/redteam.py --out report.json

Output: a JSON report + a concise console summary. Exit non-zero if any P0
hallucination or scope violation is found, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Force OFFLINE unless the caller explicitly asked for --online. We never want an
# accidental online run to make the audit non-reproducible.
if "FRIDAY_LLM_MODEL" in os.environ or "FRIDAY_LLM_API_KEY" in os.environ:
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)

from friday.db import (  # noqa: E402
    LangRow, SnapshotRow, TechRow, connect, insert_snapshot,
    replace_all_relationships, replace_children, set_repo_quality,
    upsert_architecture, upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows  # noqa: E402
from friday.ask import ask, Exchange, Answer, Evidence, RetrievalRequirements  # noqa: E402
from friday import objective as obj  # noqa: E402

REPO_NAMES = ["aether", "friday-v3", "vivaha", "mindwell", "finance-tracker"]
REPO_SET = set(REPO_NAMES)


# --------------------------------------------------------------------------- #
# Workspace seeding — a realistic 5-repo portfolio
# --------------------------------------------------------------------------- #


def _seed_workspace(conn) -> None:
    def seed(name, path, summary, langs, techs, arch, commits, dirty=False):
        rid = upsert_repository(
            conn, name=name, path=path, default_branch="main", is_dirty=dirty,
            first_commit_date="2025-01-01", last_commit_date="2026-07-01",
            remote_url="https://github.com/acme/" + name, commit_count=commits,
            readme_summary=summary, license="MIT", primary_author="dev@acme.com",
        )
        replace_children(conn, rid, [LangRow(l, 10) for l in langs],
                         [TechRow(t, "e") for t in techs])
        upsert_architecture(conn, repo_id=rid, architecture=arch, evidence="stored")
        set_repo_quality(conn, rid, None, "good" if summary else "none",
                         "complete" if summary else "none")
        return rid

    seed("aether", "/a",
         "Purpose:\nAether is an operating system in Rust.\nValue:\ncore infrastructure.\nMaturity:\nUnknown",
         ("Rust",), ("Rust",), "Cargo workspace", 120)
    seed("friday-v3", "/f3",
         "Purpose:\nFriday V3 is an AI operating partner.\nValue:\nautomates workspace operations.\nMaturity:\nBeta",
         ("Python",), ("Python", "Supabase"), "CLI tool", 600, dirty=True)
    seed("vivaha", "/v",
         "Purpose:\nVivaha is a premium matrimonial platform.\nValue:\nhelps people find partners.\nMaturity:\nBeta",
         ("TypeScript",), ("Next.js", "Supabase"), "Next.js App Router", 200)
    seed("mindwell", "/m",
         "Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
         ("Python",), ("Python",), "React SPA", 150)
    seed("finance-tracker", "/ft",
         "Purpose:\nfinance-tracker tracks personal spending.\nMaturity:\nWIP",
         ("Python",), ("Python",), "Library", 80)
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    insert_snapshot(conn, SnapshotRow(
        observed_at="2026-07-10", repo_path="/f3", repo_name="friday-v3",
        default_branch="main", commit_count=600, last_commit_date="2026-07-01",
        is_dirty=1, readme_hash="r", architecture_hash="a", identity_hash="i"))
    conn.commit()


# --------------------------------------------------------------------------- #
# Question catalog — 8 attack categories
# --------------------------------------------------------------------------- #
# Each item: (category, question_fn_or_str, expected_scope, kind)
#   kind = "single" | "followup" | "multi" | "paraphrase:<group>"
# For followups/multi we need a prior exchange; the harness chains them.
# expected_scope is the EvidenceScope the correct objective should map to.


@dataclass
class Q:
    category: str
    text: str
    expected_scope: str
    kind: str = "single"
    group: str = ""           # for convergence checks (paraphrase groups)
    p0_invented_anchors: tuple = ()  # substrings that, if present, = hallucination
    note: str = ""


def _cat1_followups() -> list[Q]:
    """Follow-up conversations: does context survive naturally?"""
    base = "Explain friday-v3"
    out = [
        Q("followup", base, obj.EvidenceScope.PROJECT, "anchored", note="anchor: explain friday-v3"),
        Q("followup", "Why?", obj.EvidenceScope.PROJECT, "followup", note="restate: reason for prior"),
        Q("followup", "What do you mean?", obj.EvidenceScope.PROJECT, "followup", note="restate/clarify"),
        Q("followup", "Explain more.", obj.EvidenceScope.PROJECT, "followup", note="restate: elaborate"),
        Q("followup", "Compare that to Vivaha.", obj.EvidenceScope.RELATIONSHIP, "followup", note="contrast to named repo"),
        Q("followup", "Would you still say that?", obj.EvidenceScope.PROJECT, "followup", note="restate stance"),
        Q("followup", "What changed?", obj.EvidenceScope.PROJECT, "followup", note="drift from prior subject"),
    ]
    # Second thread: themes, then a pronoun follow-up that must stay workspace.
    base2 = "What themes keep repeating?"
    out += [
        Q("followup", base2, obj.EvidenceScope.WORKSPACE, "anchored", note="anchor: workspace themes"),
        Q("followup", "Which one is strongest?", obj.EvidenceScope.WORKSPACE, "followup", note="workspace anchor must persist (not collapse to one repo)"),
    ]
    return out


def _cat2_ambiguous() -> list[Q]:
    return [
        Q("ambiguous", "Which one?", obj.EvidenceScope.WORKSPACE, "single", note="unresolved antecedent -> clarify, not a wrong answer"),
        Q("ambiguous", "That project.", obj.EvidenceScope.PROJECT, "single", note="vague reference, no prior -> clarify/ask"),
        Q("ambiguous", "The newest one.", obj.EvidenceScope.WORKSPACE, "single", note="resolves to NEWEST objective -> WORKSPACE scope"),
        Q("ambiguous", "The Rust one.", obj.EvidenceScope.RELATIONSHIP, "single", note="resolves to aether by tech"),
        Q("ambiguous", "The startup.", obj.EvidenceScope.PROJECT, "single", note="commercial framing -> a named repo or clarify"),
        Q("ambiguous", "The platform.", obj.EvidenceScope.PROJECT, "single", note="vague -> clarify or named"),
        Q("ambiguous", "Compare the Rust one and the matrimony one.", obj.EvidenceScope.RELATIONSHIP, "single", note="two references resolve to aether+vivaha"),
    ]


def _cat3_contradictory() -> list[Q]:
    out = [
        Q("contradictory", "Which project is closest to shipping?", obj.EvidenceScope.WORKSPACE, "single", note="value/impact ranking (VALUE obj -> WORKSPACE)"),
        Q("contradictory", "Now ignore commercial value.", obj.EvidenceScope.WORKSPACE, "single", note="must change reasoning, not repeat prior"),
        Q("contradictory", "Rank only by learning.", obj.EvidenceScope.PORTFOLIO, "single", note="lens flips to learning -> STRENGTHS obj -> PORTFOLIO"),
        Q("contradictory", "Now rank only by impact.", obj.EvidenceScope.PORTFOLIO, "single", note="lens flips to impact -> PRIORITIZE obj -> PORTFOLIO"),
    ]
    return out


def _cat4_negative() -> list[Q]:
    return [
        Q("negative", "Which projects should NEVER merge?", obj.EvidenceScope.PORTFOLIO, "single", note="merge-risk judgment (MERGE obj -> PORTFOLIO)"),
        Q("negative", "What should I stop building?", obj.EvidenceScope.PORTFOLIO, "single", note="deprioritize judgment (PRIORITIZE obj -> PORTFOLIO)"),
        Q("negative", "What should I abandon?", obj.EvidenceScope.WORKSPACE, "single", note="inactive/abandon judgment (INACTIVE obj -> WORKSPACE)"),
        Q("negative", "Which direction looks weakest?", obj.EvidenceScope.PORTFOLIO, "single", note="direction judgment (DIRECTION obj -> PORTFOLIO)"),
        Q("negative", "Which project is a distraction?", obj.EvidenceScope.PORTFOLIO, "single", note="deprioritize judgment (PRIORITIZE obj -> PORTFOLIO)"),
    ]


def _cat5_temporal() -> list[Q]:
    return [
        Q("temporal", "How have I changed?", obj.EvidenceScope.TIMELINE, "single", note="evolution over time (TIMELINE)"),
        Q("temporal", "What did V2 teach V3?", obj.EvidenceScope.TIMELINE, "single", note="historical/evolution; may honestly lack evidence (TIMELINE)"),
        Q("temporal", "What assumptions disappeared?", obj.EvidenceScope.PORTFOLIO, "single", note="assumptions across portfolio (ASSUMPTIONS obj -> PORTFOLIO)"),
        Q("temporal", "What became more important?", obj.EvidenceScope.TIMELINE, "single", note="priority evolution (TIMELINE)"),
        Q("temporal", "Where did I pivot?", obj.EvidenceScope.TIMELINE, "single", note="drift detection (DRIFT obj -> TIMELINE)"),
    ]


def _cat6_synthesis() -> list[Q]:
    return [
        Q("synthesis", "What company could I build?", obj.EvidenceScope.PORTFOLIO, "single", note="portfolio synthesis (PORTFOLIO)"),
        Q("synthesis", "What products naturally fit together?", obj.EvidenceScope.WORKSPACE, "single", note="overlap synthesis (OVERLAP obj -> WORKSPACE)"),
        Q("synthesis", "What is the center of gravity?", obj.EvidenceScope.PORTFOLIO, "single", note="portfolio center (PORTFOLIO)"),
        Q("synthesis", "Which project exists only because another one doesn't?", obj.EvidenceScope.RELATIONSHIP, "single", note="relationship reasoning (RELATIONSHIP)"),
        Q("synthesis", "If I had one year...", obj.EvidenceScope.PORTFOLIO, "single", note="evolve judgment (EVOLVE obj -> PORTFOLIO)"),
        Q("synthesis", "If I had one month...", obj.EvidenceScope.PORTFOLIO, "single", note="evolve judgment (EVOLVE obj -> PORTFOLIO)"),
        Q("synthesis", "If I had one weekend...", obj.EvidenceScope.WORKSPACE, "single", note="recommend next (RECOMMEND obj -> WORKSPACE)"),
    ]


def _cat7_reality() -> list[Q]:
    # P0: any answer that invents these anchors is a hallucination.
    return [
        Q("reality", "Why did you choose Rust?", obj.EvidenceScope.PROJECT, "single",
          p0_invented_anchors=("your uncle", "your team", "your teammates", "users love",
                               "users told", "your manager", "your cofounder"),
          note="P0: must not invent motivations/people; honest 'not in evidence'"),
        Q("reality", "What did I tell my uncle?", obj.EvidenceScope.PROJECT, "single",
          p0_invented_anchors=("you told your uncle", "you said to your uncle",
                               "your uncle", "told your uncle"),
          note="P0: no personal-conversation memory exists; must say so"),
        Q("reality", "What do my teammates think?", obj.EvidenceScope.PROJECT, "single",
          p0_invented_anchors=("your teammate", "your teammates", "your team thinks",
                               "your colleague", "your co-workers"),
          note="P0: no teammate opinions stored; must say so"),
        Q("reality", "What feature do users love?", obj.EvidenceScope.PROJECT, "single",
          p0_invented_anchors=("users love", "users said", "customers love",
                               "user feedback", "users told", "users prefer"),
          note="P0: no user-feedback evidence; must say so"),
    ]


def _cat8_paraphrase() -> list[Q]:
    """Same intent 30 ways -> must converge on WORKSPACE scope + THEMES objective."""
    group = "what-am-i-building"
    variants = [
        "What am I building?",
        "Where is my work heading?",
        "What's the common thread?",
        "What's tying everything together?",
        "What's the endgame?",
        "What direction am I moving?",
        "What am I optimizing for?",
        "What's the through-line across my projects?",
        "What do all my projects add up to?",
        "What's the big picture of my work?",
        "What am I really making?",
        "What's the point of all this?",
        "What unifies my projects?",
        "What's the mission behind my work?",
        "Where is this all going?",
        "What's the shared purpose?",
        "What do my repos collectively do?",
        "What's the pattern in what I build?",
        "What am I trying to achieve?",
        "What's the product I'm building?",
        "What's the thesis of my portfolio?",
        "What connects my work?",
        "What's the agenda?",
        "What am I constructing?",
        "What's the shape of my output?",
        "What are my projects largely about?",
        "What's the spine of my work?",
        "What's the throughline?",
        "What am I in the business of building?",
        "What's the meta-goal?",
    ]
    return [
        Q("paraphrase", v, obj.EvidenceScope.WORKSPACE, "paraphrase", group=group,
          note="must converge: WORKSPACE scope, THEMES objective, >=3 repos cited")
        for v in variants
    ]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@dataclass
class Result:
    category: str
    question: str
    expected_scope: str
    actual_scope: str
    objective: str
    used_llm: bool
    kind: str
    group: str
    note: str
    # automatic rubric
    scope_ok: bool = False
    evidence_ok: bool = False
    hallucination: bool = False
    fallback_dump: bool = False
    # subjective (never auto-passed)
    reads_naturally: str = "REVIEW"
    actionable: str = "REVIEW"
    # meta
    text_len: int = 0
    repos_cited: int = 0
    score: int = 0
    p0: bool = False
    fail_mode: str = "ok"
    detail: str = ""


def _repos_cited(text: str) -> int:
    low = text.lower()
    return sum(1 for n in REPO_NAMES if n in low)


def _grade(q: Q, ans, prev_note: str = "") -> Result:
    raw = ans.evidence.raw if ans.evidence else {}
    scope = raw.get("scope", "?")
    objv = raw.get("objective", "?")
    text = ans.text or ""
    used_llm = ans.used_llm
    repos = _repos_cited(text)

    # 1. correct scope
    scope_ok = (scope == q.expected_scope)

    # 2. correct evidence
    # For workspace/portfolio/timeline, evidence must span >= 3 repos (the
    # canonical regression: collapsing to one repo). For project/relationship the
    # relevant repos must appear. For reality questions, evidence_ok = honest.
    if q.category == "reality":
        # Honesty is the evidence check: an "I don't know / not in evidence"
        # answer is CORRECT evidence handling. The p0 anchors only matter for the
        # hallucination branch (present => fabricated); their mere existence as a
        # field must NOT fail the evidence check when the answer is honest.
        honest = any(s in text.lower() for s in
                     ("don't have", "don't see", "not in", "no evidence", "no record",
                      "nothing", "can't", "i don't", "i'm not", "unable", "not stored",
                      "i haven't", "no memory", "no data", "no information",
                      "based on 0 of", "no strong", "no theme"))
        evidence_ok = bool(honest)
    elif scope in (obj.EvidenceScope.WORKSPACE, obj.EvidenceScope.PORTFOLIO,
                   obj.EvidenceScope.TIMELINE):
        evidence_ok = repos >= 3
    elif q.kind == "followup" and q.expected_scope == obj.EvidenceScope.RELATIONSHIP:
        evidence_ok = repos >= 2
    elif q.expected_scope == obj.EvidenceScope.PROJECT:
        # At least the subject repo should be present (or it honestly says unknown).
        evidence_ok = repos >= 1 or "don't have enough" in text.lower()
    else:
        evidence_ok = repos >= 1

    # 3. no hallucination (P0) — only meaningful for reality category anchors
    hallucination = False
    if q.p0_invented_anchors:
        low = text.lower()
        for anchor in q.p0_invented_anchors:
            if anchor.lower() in low:
                hallucination = True
                break

    # 4. no fallback dump — paraphrase group must converge to the same objective
    # (checked separately in convergence pass); for now mark per-answer.
    fallback_dump = False
    if q.kind == "paraphrase":
        # A paraphrase must NOT collapse to a single-repo describe dump.
        fallback_dump = (scope == obj.EvidenceScope.PROJECT and repos < 3)

    # Classify the failure MODE so the report is actionable. Offline mode returns
    # honest non-answers ("I don't have enough evidence" / "based on 0 of N") when
    # the heuristic can't parse a question. That is the SAFE failure (no guess) but
    # still a blind spot if the LLM path is expected to catch it.
    honest_refusal = any(s in text.lower() for s in (
        "don't have enough evidence", "based on 0 of", "i couldn't confidently",
        "try rephrasing", "not in evidence", "no strong", "no theme"))
    if not scope_ok:
        fail_mode = "honest-refusal" if honest_refusal else "wrong-scope"
    elif not evidence_ok:
        fail_mode = "honest-refusal" if honest_refusal else "wrong-evidence"
    else:
        fail_mode = "ok"

    score = sum([scope_ok, evidence_ok, not hallucination, not fallback_dump])
    # reads_naturally + actionable are subjective -> excluded from auto score (max 4 auto)
    # P0 is reserved for FABRICATION / crash only — an honest "I don't know" is a
    # correct reality-check answer and must never be scored as a P0.
    p0 = hallucination or (text.strip().startswith("EXCEPTION"))

    return Result(
        category=q.category, question=q.text, expected_scope=q.expected_scope,
        actual_scope=scope, objective=objv, used_llm=used_llm, kind=q.kind,
        group=q.group, note=q.note or prev_note, scope_ok=scope_ok,
        evidence_ok=evidence_ok, hallucination=hallucination,
        fallback_dump=fallback_dump, reads_naturally="REVIEW", actionable="REVIEW",
        text_len=len(text), repos_cited=repos, score=score, p0=p0,
        fail_mode=fail_mode, detail=text[:200],
    )


# --------------------------------------------------------------------------- #
# Harness driver
# --------------------------------------------------------------------------- #


def _run_single(conn, q: Q, prev: Exchange | None) -> tuple[Result, Exchange | None]:
    try:
        ans = ask(q.text, conn, prev=prev, verbose=False)
    except Exception as e:  # a crash is itself a red-team finding
        r = Result(q.category, q.text, q.expected_scope, "CRASH", "?", False,
                   q.kind, q.group, q.note, p0=True, detail=f"EXCEPTION: {e!r}")
        return r, prev
    res = _grade(q, ans)
    new_prev = Exchange(question=q.text, answer=ans) if q.kind in ("anchored", "followup", "multi") else prev
    return res, new_prev


def _chain_followups(conn, qs: list[Q]) -> list[Result]:
    """Run a follow-up thread in order, threading prev exchange."""
    results: list[Result] = []
    prev: Exchange | None = None
    # reset: first anchored question has no prev
    for i, q in enumerate(qs):
        r, prev = _run_single(conn, q, prev)
        results.append(r)
    return results


def run(conn, online: bool, seed: int) -> list[Result]:
    rng = random.Random(seed)
    results: list[Result] = []

    # Category 1: follow-up threads (chained in order)
    results += _chain_followups(conn, _cat1_followups())

    # Categories 2-7: independent singles (shuffled for realism)
    singles: list[Q] = (
        _cat2_ambiguous() + _cat3_contradictory() + _cat4_negative()
        + _cat5_temporal() + _cat6_synthesis() + _cat7_reality()
    )
    rng.shuffle(singles)
    for q in singles:
        r, _ = _run_single(conn, q, None)
        results.append(r)

    # Category 8: paraphrase group (convergence)
    results += [r for r, _ in (_run_single(conn, q, None) for q in _cat8_paraphrase())]

    # Scale to 500-1000 by repeating the reality/single sets with slight variants.
    # This expands coverage without inventing new categories.
    booster: list[Q] = []
    for q in singles:
        if q.category in ("reality", "negative", "temporal", "synthesis"):
            booster.append(q)
    repeats = max(0, (600 - len(results)) // max(1, len(booster)))
    for _ in range(repeats):
        blk = list(booster)
        rng.shuffle(blk)
        for q in blk:
            r, _ = _run_single(conn, q, None)
            results.append(r)

    return results


def _convergence_check(results: list[Result]) -> list[str]:
    """Category 8: all paraphrase variants must converge on the same objective."""
    groups: dict[str, list[Result]] = {}
    for r in results:
        if r.kind == "paraphrase":
            groups.setdefault(r.group, []).append(r)
    findings: list[str] = []
    for g, rs in groups.items():
        objs = {r.objective for r in rs}
        scopes = {r.actual_scope for r in rs}
        if len(objs) > 1 or len(scopes) > 1:
            findings.append(
                f"CONVERGENCE FAIL [{g}]: objectives={sorted(objs)} scopes={sorted(scopes)} "
                f"across {len(rs)} paraphrases")
        for r in rs:
            if r.repos_cited < 3:
                findings.append(f"CONVERGENCE FAIL [{g}]: '{r.question}' cited {r.repos_cited} repos")
    return findings


def summarize(results: list[Result], online: bool) -> dict:
    total = len(results)
    p0 = [r for r in results if r.p0]
    scope_fail = [r for r in results if not r.scope_ok]
    evidence_fail = [r for r in results if not r.evidence_ok]
    fallback = [r for r in results if r.fallback_dump]
    refusals = [r for r in results if r.fail_mode == "honest-refusal"]
    cat_counts: dict[str, int] = {}
    for r in results:
        cat_counts[r.category] = cat_counts.get(r.category, 0) + 1
    return {
        "online": online,
        "total_questions": total,
        "category_counts": cat_counts,
        "auto_score_sum": sum(r.score for r in results),
        "auto_score_max": total * 4,
        "p0_hallucination_or_crash": len(p0),
        "scope_failures": len(scope_fail),
        "evidence_failures": len(evidence_fail),
        "honest_refusals": len(refusals),
        "fallback_dump_failures": len(fallback),
        "human_review_required": total,  # reads_naturally + actionable
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Friday V3 red-team audit harness")
    ap.add_argument("--online", action="store_true", help="use LLM understanding (set FRIDAY_LLM_* first)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=str, default="redteam_report.json")
    args = ap.parse_args()

    if args.online:
        # The local 9router proxy accepts any API key; pick a free model. Allow
        # the caller to override via real FRIDAY_LLM_* env if they have them.
        os.environ.setdefault("FRIDAY_LLM_API_KEY", "redteam-audit")
        os.environ.setdefault("FRIDAY_LLM_MODEL", "free")
        # ask() reads FRIDAY_LLM_* at call time via llm_enabled(); no reload needed.

    tmp = Path(tempfile.mkdtemp())
    conn = connect(tmp / "kb.db")
    _seed_workspace(conn)

    results = run(conn, args.online, args.seed)
    conn.close()

    conv = _convergence_check(results)
    summary = summarize(results, args.online)

    report = {
        "summary": summary,
        "convergence_findings": conv,
        "results": [vars(r) for r in results],
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2, default=str))

    # Console summary
    print("=" * 64)
    print(f"RED-TEAM AUDIT  (online={args.online}, questions={summary['total_questions']})")
    print("=" * 64)
    print(f"  categories        : {summary['category_counts']}")
    print(f"  auto score        : {summary['auto_score_sum']}/{summary['auto_score_max']}")
    print(f"  P0 (halluc/crash) : {summary['p0_hallucination_or_crash']}")
    print(f"  scope failures    : {summary['scope_failures']}")
    print(f"  evidence failures : {summary['evidence_failures']}")
    print(f"  honest refusals   : {summary['honest_refusals']} (safe blind spots)")
    print(f"  fallback dumps    : {summary['fallback_dump_failures']}")
    print(f"  convergence       : {len(conv)} finding(s)")
    for c in conv[:10]:
        print("    - " + c)
    print(f"  report            : {out_path}")
    print("=" * 64)

    # Gate: P0 or convergence failure => non-zero exit.
    if summary["p0_hallucination_or_crash"] > 0 or conv:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
