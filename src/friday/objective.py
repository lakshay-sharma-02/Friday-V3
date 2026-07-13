"""Deterministic Engineering Judgment layer — Milestone 6.6.

This is NOT an intent system, NOT a router, NOT a planner, and NOT an LLM.
It sits between RetrievalRequirements and provider selection and answers one
question the rest of the pipeline cannot:

    "What kind of engineering judgment is being requested?"

RetrievalRequirements tells us WHAT evidence exists / is needed. The objective
tells us WHY the user is asking — which engineering question they want answered.
That distinction is the whole point: the same evidence bag (themes + purpose +
identity) must answer *different* questions depending on the objective, and the
answer must be framed accordingly. Without this layer, "What am I building?" and
"What themes keep repeating?" collapse into one themes dump, and "Explain Friday"
returns portfolio themes because nothing said "this is an EXPLAIN objective, so
purpose + architecture lead, themes are last".

The objective is an ANSWER OBJECTIVE, not a routing label: it drives (1) which
evidence matters most (priority weights per need), and (2) how the answer is
structured (the contract). It never invents new intents — it maps an existing
RetrievalRequirements onto one of a fixed set of engineering-judgment shapes,
disambiguating only when the model returned a broad, conflicting needs bag.

Everything here is deterministic and pure (no LLM). A new objective = one enum
member + a priority tuple + a contract + (optionally) an evidence function.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from . import query


# ---------------------------------------------------------------------------
# The engineering-judgment objectives
# ---------------------------------------------------------------------------
#
# Each is a SHAPE OF ANSWER the user wants, not a bucket for the question. The
# names deliberately describe the engineering act ("explain", "compare",
# "prioritize", "profile", ...) — not a noun the question contains.


class Objective:
    EXPLAIN = "explain"            # describe one subject's nature & purpose
    COMPARE = "compare"            # two subjects against each other
    PRIORITIZE = "prioritize"      # what to continue / center on (momentum)
    PLATFORM = "platform"          # which should become a platform
    PROFILE = "profile"            # what kind of engineer am I
    THEMES = "themes"              # what am I building (purpose-level)
    THEME_REPEAT = "theme-repeat"  # what themes keep repeating (across time)
    DIRECTION = "direction"        # strategic direction / converge
    LESSONS = "lessons"            # repeated engineering lessons
    HABITS = "habits"              # engineering habits
    ASSUMPTIONS = "assumptions"    # assumptions keep repeating
    DRIFT = "drift"                # which project drifted / evolved
    EFFORT = "effort"              # where is effort going
    STRENGTHS = "strengths"        # capabilities being developed
    OVERLAP = "overlap"            # which projects overlap
    MERGE = "merge"                # which should not merge
    INSIGHTS = "insights"          # something I haven't noticed / surprising
    VALUE = "value"                # most valuable project
    INTEGRATION = "integration"    # which should integrate with Friday
    RELATIONSHIPS = "relationships"# how is X related to Y
    ARCHITECTURE = "architecture"  # how is X built
    SIMILARITY = "similarity"      # shared implementations / reuse
    INACTIVE = "inactive"          # stalled / abandoned
    NEWEST = "newest"              # most recent
    RECOMMEND = "recommend"        # what to work on next
    BY_TECH = "by-tech"            # which projects use a tech
    UNIVERSE = "universe"          # engineering-universe overview
    SURPRISE = "surprise"          # what surprises you
    EVOLVE = "evolve"              # how would you evolve the portfolio
    CHITCHAT = "chitchat"
    GENERAL = "general"


# ---------------------------------------------------------------------------
# Evidence-priority weights — what matters for each objective
# ---------------------------------------------------------------------------
#
# Providers are selected by need, but the SAME need can be central to one
# objective and noise to another. weight_evidence() re-ranks the chosen
# providers by how much that objective cares about each need, so an EXPLAIN
# answer leads with purpose/architecture and buries themes, while a PROFILE
# answer leads with identity/themes and buries activity. This is what stops
# answer collapse: priority is objective-dependent, not need-dependent.
#
# 10 = answer-defining, 5 = supporting, 1 = context-only, 0 = ignore.

_PRIORITY: dict[str, dict[str, int]] = {
    Objective.EXPLAIN: {
        "describe": 10, "identity": 10, "purpose": 10, "architecture": 6,
        "components": 4, "activity": 3, "history": 3, "relationships": 2,
        "themes": 1, "observation": 1, "value": 1, "effort": 0,
    },
    Objective.COMPARE: {
        "compare": 10, "identity": 8, "architecture": 8, "purpose": 8,
        "relationships": 5, "activity": 2, "themes": 2, "components": 4,
        "observation": 1,
    },
    Objective.PRIORITIZE: {
        "priority": 10, "impact": 9, "recommend": 8, "effort": 6,
        "activity": 6, "value": 5, "themes": 3, "relationships": 4,
        "blockers": 7,
    },
    Objective.PLATFORM: {
        "platform": 10, "reuse": 8, "components": 7, "architecture": 6,
        "overlap": 5, "relationships": 4, "integration": 3, "themes": 2,
    },
    Objective.PROFILE: {
        "engineering-profile": 10, "identity": 10, "themes": 8, "strengths": 7,
        "effort": 4, "relationships": 5, "components": 5, "activity": 2,
        "architecture": 3, "learning": 4,
    },
    Objective.THEMES: {
        "themes": 10, "purpose": 9, "identity": 7, "effort": 3, "strengths": 1,
        "activity": 1, "engineering-profile": 2,
    },
    Objective.THEME_REPEAT: {
        "themes": 10, "insights": 7, "observation": 6, "history": 5,
        "purpose": 6, "universe": 4, "converge": 4, "effort": 2,
    },
    Objective.DIRECTION: {
        "converge": 10, "themes": 8, "universe": 7, "direction": 9,
        "priority": 6, "effort": 5, "insights": 4,
    },
    Objective.LESSONS: {
        "insights": 10, "lessons": 10, "observation": 6, "relationships": 4,
        "similarity": 5, "reuse": 4, "themes": 3, "architecture": 2,
    },
    Objective.HABITS: {
        "habits": 10, "relationships": 8, "engineering-profile": 7,
        "themes": 6, "components": 6, "architecture": 4, "effort": 5,
    },
    Objective.ASSUMPTIONS: {
        "assumptions": 10, "insights": 8, "themes": 7, "purpose": 6,
        "relationships": 5, "history": 4, "observation": 4, "converge": 3,
    },
    Objective.DRIFT: {
        "drift": 10, "observation": 9, "history": 8, "purpose": 7,
        "architecture": 6, "activity": 5, "themes": 3,
    },
    Objective.EFFORT: {
        "effort": 10, "activity": 9, "observation": 6, "history": 5,
        "priority": 4, "themes": 2, "engineering-profile": 1,
    },
    Objective.STRENGTHS: {
        "strengths": 10, "effort": 4, "themes": 1, "engineering-profile": 3,
        "components": 7, "architecture": 7, "learning": 6,
    },
    Objective.OVERLAP: {
        "overlap": 10, "similarity": 8, "reuse": 7, "relationships": 6,
        "architecture": 5, "components": 4, "merge": 3,
    },
    Objective.MERGE: {
        "merge": 10, "integration": 8, "overlap": 7, "relationships": 6,
        "similarity": 5, "reuse": 5, "architecture": 4,
    },
    Objective.INSIGHTS: {
        "insights": 10, "observation": 6, "themes": 3, "universe": 4,
        "similarity": 4, "relationships": 3,
    },
    Objective.VALUE: {
        "value": 10, "impact": 7, "priority": 5, "activity": 4,
        "themes": 3, "relationships": 3,
    },
    Objective.INTEGRATION: {
        "integration": 10, "relationships": 6, "overlap": 5, "themes": 3,
        "architecture": 4, "reuse": 4,
    },
    Objective.RELATIONSHIPS: {
        "relationships": 10, "related": 10, "overlap": 5, "similarity": 4,
        "architecture": 3, "merge": 3,
    },
    Objective.ARCHITECTURE: {
        "architecture": 10, "components": 8, "describe": 4, "activity": 2,
        "relationships": 2, "themes": 1,
    },
    Objective.SIMILARITY: {
        "similarity": 10, "reuse": 9, "overlap": 7, "architecture": 5,
        "relationships": 4, "components": 4,
    },
    Objective.INACTIVE: {"inactive": 10, "activity": 5, "observation": 3},
    Objective.NEWEST: {"newest": 10},
    Objective.RECOMMEND: {
        "recommend": 10, "priority": 8, "impact": 6, "effort": 5,
        "activity": 5, "value": 4,
    },
    Objective.BY_TECH: {"by-tech": 10},
    Objective.UNIVERSE: {
        "universe": 10, "themes": 7, "overlap": 5, "relationships": 5,
        "integration": 4, "effort": 3,
    },
    Objective.SURPRISE: {
        "insights": 10, "surprise": 10, "observation": 6, "universe": 4,
        "themes": 3, "relationships": 3,
    },
    Objective.EVOLVE: {
        "evolve": 10, "priority": 9, "converge": 8, "platform": 7,
        "opportunity": 7, "merge": 6, "themes": 5, "impact": 5,
        "effort": 4, "direction": 6,
    },
}


# ---------------------------------------------------------------------------
# Answer contracts — the structure each objective MUST produce
# ---------------------------------------------------------------------------
#
# A contract is a list of section headings. The renderer (_render_contract in
# ask.py) orders the evidence blocks to match it, and the answer becomes one
# coherent structure instead of "whatever the providers happened to emit".
# Empty template = free-form prose (provider owns structure); non-empty = the
# renderer re-orders evidence to the listed sections.

_CONTRACTS: dict[str, list[str]] = {
    Objective.EXPLAIN: [
        "Purpose", "Problem it solves", "Current maturity",
        "Architecture", "Important observations",
    ],
    Objective.COMPARE: [
        "Shared goal", "Different goals", "Architecture differences",
        "Technology differences", "Current maturity", "Recommendation",
    ],
    Objective.PLATFORM: [
        "Current product", "Reusable capabilities", "Candidate abstractions",
        "Risks", "Recommendation",
    ],
    Objective.PROFILE: [
        "Primary strengths", "Recurring domains", "Technologies",
        "Working style", "Blind spots",
    ],
    Objective.MERGE: [
        "Shared responsibilities", "Shared architecture", "Shared abstractions",
        "Coupling risks", "Recommendation",
    ],
    Objective.PRIORITIZE: [
        "Center of attention", "Why now (momentum / blockers)",
        "What to deprioritize", "Recommendation",
    ],
    Objective.THEMES: [
        "Recurring themes", "What each project is (by stated purpose)",
        "Stated intent",
    ],
    Objective.THEME_REPEAT: [
        "Themes that keep repeating", "Evidence they repeat",
        "What is NOT repeating",
    ],
    Objective.DIRECTION: [
        "Where the work is converging", "Thesis", "What would change it",
    ],
    Objective.LESSONS: [
        "Repeated lessons", "Where they came from", "What to carry forward",
    ],
    Objective.HABITS: [
        "Recurring engineering decisions", "Structural habits",
        "What it says about the work",
    ],
    Objective.ASSUMPTIONS: [
        "Assumptions that keep showing up", "Where they appear",
        "Risk if unexamined",
    ],
    Objective.DRIFT: [
        "Project", "Original direction", "Current state", "Drift / evolution",
    ],
    Objective.EFFORT: [
        "Active uncommitted work", "Recent commit velocity",
        "Where effort has stalled",
    ],
    Objective.STRENGTHS: [
        "Systems and architectures built", "Repeated patterns",
        "Language breadth", "Complex systems taken on",
    ],
    Objective.OVERLAP: [
        "Shared responsibility / problem", "Shared architecture",
        "Shared abstractions", "Coupling risks",
    ],
    Objective.INSIGHTS: ["Insight", "Evidence"],
    Objective.VALUE: ["Most valuable", "Signals", "Confidence"],
    Objective.INTEGRATION: ["Candidate", "Reason", "Confidence"],
    Objective.RELATIONSHIPS: ["Relationship", "Evidence"],
    Objective.ARCHITECTURE: ["Architecture", "Components", "Entry points"],
    Objective.SIMILARITY: ["Shared code", "Shared architecture"],
    Objective.INACTIVE: ["Stalled", "Days"],
    Objective.NEWEST: ["Newest"],
    Objective.RECOMMEND: ["Recommendation", "Why", "Next"],
    Objective.BY_TECH: ["Projects using tech"],
    Objective.UNIVERSE: ["Themes", "Reuse", "Integration point"],
    Objective.SURPRISE: ["Surprise", "Evidence"],
    Objective.EVOLVE: [
        "Where to invest", "What to consolidate", "What to let go",
        "Recommendation",
    ],
}


# Canonical evidence need that OWNS the answer for each objective. When the
# objective is chosen, this need is guaranteed into the ordered needs (and leads)
# so the right provider runs even if the understanding step returned a broad bag
# that omitted it. Distinct from other objectives' canonical needs — this is what
# keeps the answers from collapsing together.
_OBJECTIVE_CANONICAL_NEED: dict[str, str] = {
    Objective.EXPLAIN: "describe",
    Objective.COMPARE: "compare",
    Objective.PRIORITIZE: "priority",
    Objective.PLATFORM: "platform",
    Objective.PROFILE: "engineering-profile",
    Objective.THEMES: "themes",
    Objective.THEME_REPEAT: "theme-repeat",
    Objective.DIRECTION: "converge",
    Objective.LESSONS: "lessons",
    Objective.HABITS: "habits",
    Objective.ASSUMPTIONS: "assumptions",
    Objective.DRIFT: "drift",
    Objective.EFFORT: "effort",
    Objective.STRENGTHS: "strengths",
    Objective.OVERLAP: "overlap",
    Objective.MERGE: "merge",
    Objective.INSIGHTS: "insights",
    Objective.VALUE: "value",
    Objective.INTEGRATION: "integration",
    Objective.RELATIONSHIPS: "relationships",
    Objective.ARCHITECTURE: "architecture",
    Objective.SIMILARITY: "similarity",
    Objective.INACTIVE: "inactive",
    Objective.NEWEST: "newest",
    Objective.RECOMMEND: "recommend",
    Objective.BY_TECH: "by-tech",
    Objective.UNIVERSE: "universe",
    Objective.SURPRISE: "surprise",
    Objective.EVOLVE: "evolve",
    Objective.CHITCHAT: "chitchat",
    Objective.GENERAL: "general",
}


@dataclass
class ObjectiveDecision:
    """The judgment: what engineering question is being asked, and how to answer.

    - objective: the answer-objective shape (engineering act, not a label).
    - needs: the (possibly re-prioritized) evidence needs to fetch — the primary
      need leads. Same vocabulary as RetrievalRequirements.needs.
    - lens: sub-focus within a provider (strategy axis / portfolio mode), or None.
    - contract: ordered answer sections the renderer must honor.
    - reason: why this objective was chosen (for debugging / benchmarks).
    """

    objective: str
    needs: list[str]
    lens: Optional[str] = None
    contract: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def primary_need(self) -> Optional[str]:
        return self.needs[0] if self.needs else None


# ---------------------------------------------------------------------------
# Disambiguation — map RetrievalRequirements -> ObjectiveDecision
# ---------------------------------------------------------------------------
#
# Inbound needs can be (a) a clean single-lens set from the offline heuristic or
# a well-formed LLM call, or (b) a broad, conflicting bag from a permissive LLM.
# We never add keyword routing — we read the needs the understanding step
# already produced and pick the objective by which engineering question those
# needs most coherently answer. The lens is authoritative when present (it was
# set explicitly for exactly this disambiguation); otherwise we rank.


# Objectives whose (needs[0], lens) pins a specific objective.
_LENS_OBJECTIVE: dict[tuple[str, Optional[str]], str] = {
    ("platform", "platform"): Objective.PLATFORM,
    ("merge", "merge"): Objective.MERGE,
    ("converge", "converge"): Objective.DIRECTION,
    ("priority", "priority"): Objective.PRIORITIZE,
    ("impact", "impact"): Objective.PRIORITIZE,
    ("learning", "learning"): Objective.STRENGTHS,
    ("opportunity", "opportunity"): Objective.EVOLVE,
    ("themes", "building"): Objective.THEMES,
    ("themes", "strengths"): Objective.STRENGTHS,
    ("themes", "effort"): Objective.EFFORT,
    ("themes", "identity"): Objective.PROFILE,
    ("engineering-profile", "identity"): Objective.PROFILE,
    ("engineering-profile", "strengths"): Objective.STRENGTHS,
    ("engineering-profile", "effort"): Objective.EFFORT,
    ("overlap", None): Objective.OVERLAP,
    ("similarity", None): Objective.SIMILARITY,
    ("reuse", None): Objective.SIMILARITY,
    ("value", None): Objective.VALUE,
    ("integration", None): Objective.INTEGRATION,
    ("relationships", None): Objective.RELATIONSHIPS,
    ("related", None): Objective.RELATIONSHIPS,
    ("architecture", None): Objective.ARCHITECTURE,
    ("components", None): Objective.ARCHITECTURE,
    ("inactive", None): Objective.INACTIVE,
    ("newest", None): Objective.NEWEST,
    ("recommend", None): Objective.RECOMMEND,
    ("by-tech", None): Objective.BY_TECH,
    ("universe", None): Objective.UNIVERSE,
    ("insights", None): Objective.INSIGHTS,
    ("compare", None): Objective.COMPARE,
    ("describe", None): Objective.EXPLAIN,
    ("theme-repeat", None): Objective.THEME_REPEAT,
    ("lessons", None): Objective.LESSONS,
    ("habits", None): Objective.HABITS,
    ("assumptions", None): Objective.ASSUMPTIONS,
    ("drift", None): Objective.DRIFT,
    ("surprise", None): Objective.SURPRISE,
    ("evolve", None): Objective.EVOLVE,
    ("chitchat", None): Objective.CHITCHAT,
    ("general", None): Objective.GENERAL,
}

# Single-need => objective (no lens needed).
_SINGLE_NEED_OBJECTIVE: dict[str, str] = {
    "platform": Objective.PLATFORM,
    "themes": Objective.THEMES,
    "merge": Objective.MERGE,
    "converge": Objective.DIRECTION,
    "priority": Objective.PRIORITIZE,
    "impact": Objective.PRIORITIZE,
    "learning": Objective.STRENGTHS,
    "opportunity": Objective.EVOLVE,
    "overlap": Objective.OVERLAP,
    "similarity": Objective.SIMILARITY,
    "reuse": Objective.SIMILARITY,
    "value": Objective.VALUE,
    "integration": Objective.INTEGRATION,
    "relationships": Objective.RELATIONSHIPS,
    "related": Objective.RELATIONSHIPS,
    "architecture": Objective.ARCHITECTURE,
    "components": Objective.ARCHITECTURE,
    "inactive": Objective.INACTIVE,
    "newest": Objective.NEWEST,
    "recommend": Objective.RECOMMEND,
    "by-tech": Objective.BY_TECH,
    "universe": Objective.UNIVERSE,
    "insights": Objective.INSIGHTS,
    "compare": Objective.COMPARE,
    "describe": Objective.EXPLAIN,
    "chitchat": Objective.CHITCHAT,
    "general": Objective.GENERAL,
}


def _rank_objective(needs: set[str], lens: Optional[str], req=None) -> str:
    """Pick the best-fitting objective for a needs SET (may be broad/bag).

    Order matters: more specific engineering questions win over generic ones so
    a bag like {themes, purpose, universe} becomes THEMES (what am I building),
    not UNIVERSE; a bag like {themes, insights, converge} becomes THEME_REPEAT;
    {engineering-profile, identity, themes} becomes PROFILE.
    """
    n = needs
    # --- Explicit answer-acts (checked first; most specific first) -------------
    # The LLM emits broad, overlapping bags (e.g. "Explain X" -> identity,
    # purpose, architecture, components, relationships, activity, history,
    # observation). Incidental context needs (observation, history, themes) must
    # NOT hijack the explicit engineering act the question asks for, so explicit
    # answer-acts win over reflective/theme logic, and the more specific act wins.
    if "compare" in n:
        return Objective.COMPARE
    if "merge" in n:
        return Objective.MERGE
    if "platform" in n:
        return Objective.PLATFORM
    if "drift" in n:
        return Objective.DRIFT
    if "overlap" in n:
        return Objective.OVERLAP
    if "similarity" in n or "reuse" in n:
        return Objective.SIMILARITY
    if "integration" in n:
        return Objective.INTEGRATION
    if "value" in n:
        return Objective.VALUE
    if "relationships" in n or "related" in n:
        return Objective.RELATIONSHIPS
    if "architecture" in n or "components" in n:
        return Objective.ARCHITECTURE
    if "describe" in n or "purpose" in n:
        # EXPLAIN only when a specific project is named; otherwise `purpose` is
        # just context for a workspace/portfolio question (e.g. "what am I
        # building?"), which must NOT collapse into an explain dump.
        if req is not None and (req.scope == "repo"
                                or (req.subjects and req.subjects != ["workspace"])):
            return Objective.EXPLAIN
        # No subject: `purpose` is context — fall through to THEMES below.
    if "effort" in n:
        return Objective.EFFORT
    if "engineering-profile" in n:
        return Objective.PROFILE
    if "strengths" in n:
        return Objective.STRENGTHS
    if "inactive" in n:
        return Objective.INACTIVE
    if "newest" in n:
        return Objective.NEWEST
    if "recommend" in n:
        return Objective.RECOMMEND
    if "by-tech" in n:
        return Objective.BY_TECH
    if "universe" in n:
        return Objective.UNIVERSE
    # --- Reflection / theme logic: explicit reflective acts win over the
    #     generic `insights` catch-all below, so a bag that contains
    #     `theme-repeat` (or assumptions/lessons/habits) resolves to that act,
    #     not to a generic INSIGHTS/THEME dump. `surprise` still wins over
    #     plain insights. (Regression: test_judge_disambiguates_broad_bags.) ---
    if "assumptions" in n:
        return Objective.ASSUMPTIONS
    if "lessons" in n:
        return Objective.LESSONS
    if "habits" in n:
        return Objective.HABITS
    if "theme-repeat" in n:
        return Objective.THEME_REPEAT
    # `themes` + (insights|converge) means "themes that keep repeating" — a
    # distinct reflective act, not a one-shot themes dump nor generic insights.
    if "themes" in n and ("insights" in n or "converge" in n):
        return Objective.THEME_REPEAT
    if "insights" in n:
        if "surprise" in n:
            return Objective.SURPRISE
        return Objective.INSIGHTS
    # "What am I building?" signature: identity + purpose, no single subject,
    # no explicit act. The LLM often omits the `themes` need here, so match on
    # the identity+purpose pairing that uniquely means "what am I building".
    if "identity" in n and "purpose" in n:
        return Objective.THEMES
    if "converge" in n:
        return Objective.DIRECTION
    if "priority" in n or "impact" in n:
        return Objective.PRIORITIZE
    if "opportunity" in n:
        return Objective.EVOLVE
    if "identity" in n and "themes" in n:
        return Objective.PROFILE
    if "observation" in n or "history" in n:
        if "themes" in n or "purpose" in n:
            return Objective.THEME_REPEAT
        return Objective.DRIFT
    if "themes" in n:
        return Objective.THEMES
    return Objective.GENERAL


def judge(req) -> ObjectiveDecision:
    """The deterministic engineering-judgment step.

    Takes a RetrievalRequirements, returns the ObjectiveDecision that names the
    engineering question being asked and how to answer it. Pure and offline-safe.

    Disambiguation order (most reliable signal first). The understanding step
    already produces structured fields (scope / lens / subjects) — we trust
    those before the noisy `needs` bag, because the LLM emits inconsistent
    need vocabularies (e.g. a compare question may omit the `compare` need, or
    scatter stray `value`/`overlap` needs). This is NOT keyword routing on the
    question text — it reads the structured understanding output:
      1. scope == "compare"            -> COMPARE (whatever the needs say)
      2. scope == "repo" / subjects set -> EXPLAIN (a single-project question)
      3. lens is a strategy axis       -> that strategy objective
      4. lens is a portfolio mode       -> that portfolio objective
      5. (needs[0], lens) pins one      -> that objective
      6. single unambiguous need        -> that objective
      7. otherwise rank the whole bag  -> _rank_objective
    """
    needs = list(req.needs)
    nset = set(needs)
    lens = req.lens

    # 1. Scope is the strongest structured signal.
    if req.scope == "compare":
        obj = Objective.COMPARE
    # 2. A single-project question is signalled by scope == "repo" — the
    #    understanding step set this explicitly for ONE named repository. A
    #    relationship question about that one repo also names it, so it wins
    #    over EXPLAIN when the relationships need is present.
    #
    #    We do NOT collapse on `req.subjects` being non-empty. The online
    #    understanding step sometimes dumps EVERY repository into `subjects`
    #    for a workspace-wide question (e.g. "What am I building?"). Treating
    #    that as a single-project EXPLAIN was the root-cause bug: a workspace
    #    question collapsed to one repo's describe dump. Workspace questions
    #    must fall through to lens/needs ranking so they land on THEMES /
    #    PROFILE / etc. (and the EvidenceScope layer keys off the objective,
    #    never off a polluted subjects list).
    elif req.scope == "repo":
        if nset & {"relationships", "related"}:
            obj = Objective.RELATIONSHIPS
        # "Explain X's architecture" is a repo-scoped question but an ARCHITECTURE
        # act, not a plain EXPLAIN — route to the architecture provider so it
        # renders data_flow / known_patterns (regression: those lines were lost
        # because the describe path never emits them).
        elif nset & {"architecture", "components"}:
            obj = Objective.ARCHITECTURE
        else:
            obj = Objective.EXPLAIN
    # 3. Lens that names a strategy axis.
    elif lens in ("impact", "platform", "learning", "opportunity", "priority",
                  "converge", "merge"):
        obj = _LENS_OBJECTIVE.get((lens, lens), Objective.GENERAL)
    # 4. Lens that names a portfolio mode (always carried on the "themes" need).
    elif lens in ("building", "strengths", "effort", "identity"):
        obj = _LENS_OBJECTIVE.get(("themes", lens), Objective.GENERAL)
    # 5. (needs[0], lens) pins one objective.
    elif (needs[0] if needs else "", lens) in _LENS_OBJECTIVE:
        obj = _LENS_OBJECTIVE[(needs[0], lens)]
    # 6. Single unambiguous need.
    elif len(needs) == 1 and needs[0] in _SINGLE_NEED_OBJECTIVE:
        obj = _SINGLE_NEED_OBJECTIVE[needs[0]]
    # 7. Rank the whole bag (subject-aware fallback).
    else:
        obj = _rank_objective(nset, lens, req)

    # Re-prioritize needs by how much this objective cares about each.
    priority = _PRIORITY.get(obj, {})

    # Guarantee the canonical need that OWNS this objective's answer leads, even
    # if the understanding step returned a broad bag that omitted it (this is the
    # key anti-collapse mechanism: THEMES and THEME_REPEAT own different needs).
    canonical = _OBJECTIVE_CANONICAL_NEED.get(obj)
    base = list(needs)
    if canonical and canonical not in base:
        base.append(canonical)

    ordered = sorted(set(base), key=lambda nd: -priority.get(nd, 0))
    # Keep the lens-driven primary first if it has real weight.
    if lens and lens in ordered:
        ordered = [lens] + [x for x in ordered if x != lens]
    elif canonical and canonical in ordered:
        ordered = [canonical] + [x for x in ordered if x != canonical]
    elif needs and needs[0] in ordered:
        ordered = [needs[0]] + [x for x in ordered if x != needs[0]]
    if not ordered:
        ordered = needs or ["general"]

    # Lens is meaningful only when it maps to this objective's provider.
    final_lens = lens if lens in ordered and obj != Objective.GENERAL else None

    return ObjectiveDecision(
        objective=obj,
        needs=ordered,
        lens=final_lens,
        contract=list(_CONTRACTS.get(obj, [])),
        reason=f"needs={sorted(nset)} lens={lens!r} -> {obj}",
    )


def weight_evidence(objective: str, need: str) -> int:
    """Priority weight (0-10) of `need` for `objective`. Used to order providers
    and to decide supporting vs primary context."""
    return _PRIORITY.get(objective, {}).get(need, 0)


def contract_for(objective: str) -> list[str]:
    return list(_CONTRACTS.get(objective, []))


# ---------------------------------------------------------------------------
# EvidenceScope — how much of the workspace the answer requires
# ---------------------------------------------------------------------------
#
# A SEPARATE axis from the objective. The objective names the engineering
# QUESTION ("what am I building"); the EvidenceScope names the EVIDENCE SPAN
# ("all repositories"). The pipeline assembles evidence by scope BEFORE
# reasoning, so a workspace question can never be answered from one repo's
# describe dump. Scope is derived from the objective (deterministic, never
# from keywords) — see scope_for().

class EvidenceScope:
    PROJECT = "project"          # exactly one repository
    RELATIONSHIP = "relationship"  # exactly two repositories
    WORKSPACE = "workspace"      # all repositories, cross-project synthesis
    PORTFOLIO = "portfolio"      # every repository, one-line summaries
    TIMELINE = "timeline"        # historical observations / git evolution
    OBSERVATION = "observation"  # observation records + current git status


# Objective -> EvidenceScope. Workspace-wide questions map to WORKSPACE /
# PORTFOLIO / TIMELINE; single-repo questions map to PROJECT; compare maps to
# RELATIONSHIP. Multi-scope questions (effort = WORKSPACE + OBSERVATION) carry
# a secondary scope in the helper below. This is the GENERAL mapping the
# EvidenceScope layer keys off — no keyword routing.
_SCOPE_FOR_OBJECTIVE: dict[str, str] = {
    Objective.EXPLAIN: EvidenceScope.PROJECT,
    Objective.ARCHITECTURE: EvidenceScope.PROJECT,
    Objective.COMPARE: EvidenceScope.RELATIONSHIP,
    Objective.RELATIONSHIPS: EvidenceScope.RELATIONSHIP,
    Objective.BY_TECH: EvidenceScope.PROJECT,
    Objective.SIMILARITY: EvidenceScope.WORKSPACE,
    Objective.OVERLAP: EvidenceScope.WORKSPACE,
    Objective.UNIVERSE: EvidenceScope.WORKSPACE,
    Objective.INTEGRATION: EvidenceScope.WORKSPACE,
    Objective.THEMES: EvidenceScope.WORKSPACE,
    Objective.THEME_REPEAT: EvidenceScope.WORKSPACE,
    Objective.EFFORT: EvidenceScope.WORKSPACE,
    Objective.VALUE: EvidenceScope.WORKSPACE,
    Objective.RECOMMEND: EvidenceScope.WORKSPACE,
    Objective.INACTIVE: EvidenceScope.WORKSPACE,
    Objective.NEWEST: EvidenceScope.WORKSPACE,
    Objective.PRIORITIZE: EvidenceScope.PORTFOLIO,
    Objective.PLATFORM: EvidenceScope.PORTFOLIO,
    Objective.PROFILE: EvidenceScope.PORTFOLIO,
    Objective.STRENGTHS: EvidenceScope.PORTFOLIO,
    Objective.LESSONS: EvidenceScope.PORTFOLIO,
    Objective.HABITS: EvidenceScope.PORTFOLIO,
    Objective.ASSUMPTIONS: EvidenceScope.PORTFOLIO,
    Objective.MERGE: EvidenceScope.PORTFOLIO,
    Objective.DIRECTION: EvidenceScope.PORTFOLIO,
    Objective.EVOLVE: EvidenceScope.PORTFOLIO,
    Objective.INSIGHTS: EvidenceScope.PORTFOLIO,
    Objective.SURPRISE: EvidenceScope.PORTFOLIO,
    Objective.DRIFT: EvidenceScope.TIMELINE,
    Objective.GENERAL: EvidenceScope.WORKSPACE,
    Objective.CHITCHAT: EvidenceScope.PROJECT,
}


def scope_for(objective: str) -> str:
    """The primary EvidenceScope an objective's answer requires."""
    return _SCOPE_FOR_OBJECTIVE.get(objective, EvidenceScope.WORKSPACE)


def secondary_scopes(objective: str) -> list[str]:
    """Extra scopes a question also draws from (e.g. EFFORT = activity + history)."""
    if objective == Objective.EFFORT:
        return [EvidenceScope.OBSERVATION]
    if objective == Objective.ASSUMPTIONS:
        return [EvidenceScope.TIMELINE]
    if objective in (Objective.PROFILE, Objective.PRIORITIZE, Objective.STRENGTHS):
        return [EvidenceScope.TIMELINE]
    return []


# ---------------------------------------------------------------------------
# Evidence functions for objectives that have no dedicated provider yet
# ---------------------------------------------------------------------------
#
# These derive the answer from already-persisted evidence (no new storage, no
# LLM, no intents). They return (blocks, raw) like a provider, so the rest of
# the pipeline is unchanged. Each is a DISTINCT evidence cut so the objective
# produces a DISTINCT answer — that is the whole point.

from .db import get_all_relationships as _get_all_relationships  # noqa: E402


def evidence_lessons(conn, today: dt.date) -> tuple[list[str], dict]:
    """Repeated engineering lessons: what the body of work keeps teaching.

    Derived from duplicated-functionality / shared-abstraction relationships
    (the same problem solved twice) + converging tech trends. Distinct from
    THEMES (purpose) and INSIGHTS (surprising facts).
    """
    from .insights import _engineering_insights

    blocks: list[str] = []
    lessons: list[str] = []
    rels = _get_all_relationships(conn)
    name_by_id = {r.id: r.name for r in query.all_repositories(conn)}
    seen: set[tuple[str, str]] = set()
    for rel in rels:
        if rel.strength == "Weak":
            continue
        if rel.kind in ("duplicated-functionality", "shared-abstraction",
                        "shared-implementation"):
            key = tuple(sorted((rel.repo_a, rel.repo_b)))
            if key in seen:
                continue
            seen.add(key)
            an, bn = name_by_id.get(rel.repo_a), name_by_id.get(rel.repo_b)
            if an and bn:
                lessons.append(
                    f"You keep solving the same class of problem: {an} and {bn} "
                    f"repeat it ({rel.evidence}). The lesson is to extract it "
                    f"once rather than rebuild it per project.")
    eng = [i.text for i in _engineering_insights(conn, today)]
    if eng:
        lessons.append("Patterns the evidence keeps surfacing: " + " ".join(eng))
    if lessons:
        blocks.append("Engineering lessons that keep repeating:")
        blocks.extend(f"- {l}" for l in lessons)
        blocks.append(
            "Confidence: Medium — derived from repeated problem-solving patterns "
            "and converging technology, not from a single project's README.")
    else:
        blocks = ["I don't see a repeated engineering lesson yet — no project "
                  "pair repeats a problem or converges on a pattern in the stored "
                  "evidence. Keep ingesting and I'll surface them as they appear."]
    return blocks, {"lessons": lessons}


def evidence_habits(conn, today: dt.date) -> tuple[list[str], dict]:
    """Engineering habits: the recurring decisions/structures across projects.

    Derived from Medium+ relationships (the decisions you keep making) +
    repeated architectures. Distinct from PROFILE (who you are) and STRENGTHS
    (capabilities built).
    """
    blocks: list[str] = []
    rels = _get_all_relationships(conn)
    name_by_id = {r.id: r.name for r in query.all_repositories(conn)}
    decs: list[str] = []
    seen: set[tuple[str, str]] = set()
    for rel in rels:
        if rel.strength == "Weak":
            continue
        if rel.kind in ("shared-architecture", "shared-framework", "shared-db",
                        "shared-deployment", "shared-config"):
            key = tuple(sorted((rel.repo_a, rel.repo_b)))
            if key in seen:
                continue
            seen.add(key)
            an, bn = name_by_id.get(rel.repo_a), name_by_id.get(rel.repo_b)
            if an and bn:
                decs.append(f"you repeatedly choose a shared {rel.kind.replace('shared-', '')} "
                             f"({an} + {bn}: {rel.evidence})")
    if decs:
        blocks.append("Engineering habits (recurring decisions):")
        blocks.extend(f"- {d}" for d in decs)
    # Repeated architecture = a structural habit.
    from .portfolio import _repo_facts
    facts = _repo_facts(conn, today)
    arch_counts: dict[str, list[str]] = {}
    for f in facts.values():
        a = f["arch_raw"]
        if a and a != "Unknown":
            arch_counts.setdefault(a, []).append(f["name"])
    repeated = {k: v for k, v in arch_counts.items() if len(v) >= 2}
    if repeated:
        blocks.append("Structural habit — systems you keep reaching for:")
        for label, names in repeated.items():
            blocks.append(f"- {label} (in {', '.join(names)})")
    if blocks:
        blocks.append(
            "Confidence: Medium — derived from repeated engineering decisions and "
            "structures, not self-reported intent.")
    else:
        blocks = ["I don't see a clear engineering habit yet — no repeated "
                  "decision or structure across your projects is recorded. "
                  "Keep ingesting more repositories."]
    return blocks, {"habits": decs}


def evidence_assumptions(conn, today: dt.date) -> tuple[list[str], dict]:
    """Assumptions that keep repeating: the premises baked into the work.

    Derived from the *themes* (the premises you keep building on) + duplicated
    functionality (the assumption that two problems are the same) + shared-*
    relationships. Distinct from THEMES (what you're building) and LESSONS
    (what you learned).
    """
    from .portfolio import detect_themes

    blocks: list[str] = []
    assumptions: list[str] = []
    themes = detect_themes(conn, today)
    if themes:
        for t in themes[:3]:
            assumptions.append(
                f"You keep assuming work belongs under '{t.theme}' "
                f"({', '.join(t.repos)} projects rest on that premise).")
    # Duplicated functionality = the assumption two problems are identical.
    rels = _get_all_relationships(conn)
    name_by_id = {r.id: r.name for r in query.all_repositories(conn)}
    seen: set[tuple[str, str]] = set()
    for rel in rels:
        if rel.strength == "Weak":
            continue
        if rel.kind == "duplicated-functionality":
            key = tuple(sorted((rel.repo_a, rel.repo_b)))
            if key in seen:
                continue
            seen.add(key)
            an, bn = name_by_id.get(rel.repo_a), name_by_id.get(rel.repo_b)
            if an and bn:
                assumptions.append(
                    f"You assumed {an} and {bn} are the same problem "
                    f"({rel.evidence}) — worth checking that premise.")
    if assumptions:
        blocks.append("Assumptions that keep showing up in your work:")
        blocks.extend(f"- {a}" for a in assumptions)
        blocks.append(
            "Confidence: Medium — derived from recurring themes and repeated "
            "problem framings, not from anything you stated explicitly.")
    else:
        blocks = ["I don't see a repeating assumption yet — no theme or repeated "
                  "problem framing is evident in the stored evidence. "
                  "Keep ingesting more repositories."]
    return blocks, {"assumptions": assumptions}


def evidence_drift(conn, today: dt.date) -> tuple[list[str], dict]:
    """Which project drifted / evolved: compare observation history to now.

    Uses M5 observe diffs (purpose/architecture changes) + maturity/activity.
    Distinct from INSIGHTS (surprising facts) and UNIVERSE (overview).
    """
    from .observe import diff_snapshots, latest_observation
    from .portfolio import _repo_facts

    blocks: list[str] = []
    snaps = latest_observation(conn)
    if len(snaps) >= 2:
        changes = diff_snapshots([snaps[1]], [snaps[0]])
        by_repo: dict[str, list[str]] = {}
        for c in changes:
            if c.repo and c.kind not in ("no changes",):
                by_repo.setdefault(c.repo, []).append(c.kind)
        if by_repo:
            blocks.append("Projects that have drifted or evolved most recently:")
            for repo, kinds in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
                blocks.append(f"- {repo}: " + ", ".join(kinds) + ".")
            blocks.append(
                "Confidence: Medium — derived from recorded observation history, "
                "not from a single snapshot.")
        else:
            blocks = ["No drift detected between the last two observations — "
                      "the workspace looks stable."]
    else:
        facts = [(f["name"], f["maturity"] + (" (active, uncommitted work)"
                  if f["is_dirty"] else "")) for f in _repo_facts(conn, today).values()]
        if facts:
            blocks.append("No observation history to measure drift against, but "
                          "here is each project's current direction:")
            for name, direction in facts:
                blocks.append(f"- {name}: {direction}")
        else:
            blocks = ["I can't measure drift yet — no observation history is "
                      "stored. Run `friday observe` twice to capture evolution."]
    return blocks, {"drift": blocks}


def evidence_theme_repeat(conn, today: dt.date) -> tuple[list[str], dict]:
    """Themes that keep repeating: purpose-level themes, framed as recurrence.

    Distinct from THEMES (a one-shot 'what am I building') and DIRECTION
    (where it's converging): this emphasizes *repetition across the portfolio*
    and what is NOT repeating.
    """
    from .portfolio import detect_themes
    blocks: list[str] = []
    themes = detect_themes(conn, today)
    strong = [t for t in themes if t.confidence in ("Strong", "Medium")]
    if strong:
        blocks.append("Themes that keep repeating across your projects:")
        for t in strong:
            blocks.append(f"- {t.theme} ({t.confidence} confidence): "
                          + ", ".join(t.repos) + ".")
        other = [t.theme for t in themes if t not in strong]
        if other:
            blocks.append("Themes that are NOT repeating (single-project only): "
                          + ", ".join(other) + ".")
        blocks.append(
            "Confidence: " + strong[0].confidence + " — derived from project "
            "purposes and roadmaps already stored.")
    else:
        blocks = ["No theme is repeating yet — too few projects state a purpose "
                  "that recurs. Run `friday analyze <path>` on more repos."]
    return blocks, {"theme_repeat": [t.theme for t in strong]}


def evidence_evolve(conn, today: dt.date) -> tuple[list[str], dict]:
    """How would you evolve the portfolio: a forward judgment built from the
    existing strategic axes (priority momentum, platform candidates, merge
    caution, opportunity leverage) — composed, not invented."""
    from .strategy import (
        strategy_priority, strategy_platform, strategy_merge, strategy_opportunity,
    )
    blocks: list[str] = []
    pri = strategy_priority(conn, today)
    plat = strategy_platform(conn, today)
    opp = strategy_opportunity(conn, today)
    merge = strategy_merge(conn, today)
    blocks.append("Where to invest next:")
    blocks.append("  " + (pri[0] if pri else "no momentum signal"))
    blocks.append("What to consolidate into a platform:")
    blocks.append("  " + (plat[0] if plat else "no platform candidate"))
    blocks.append("What to let go or keep separate:")
    blocks.append("  " + (merge[0] if merge else "nothing to oppose merging"))
    blocks.append("Leverage you are leaving on the table:")
    blocks.append("  " + (opp[0] if opp else "no missed leverage detected"))
    blocks.append(
        "Confidence: Medium — synthesized from current momentum, reusable "
        "capability, merge risk and leverage across the portfolio.")
    return blocks, {"evolve": blocks}


def evidence_surprise(conn, today: dt.date) -> tuple[list[str], dict]:
    """What surprises you: the non-obvious engineering observations only.

    Mirrors INSIGHTS but explicitly excludes the obvious factual lines; this is
    the 'surprise me' framing. Returns just the engineering-insight slice.
    """
    from .insights import _engineering_insights
    eng = [i.text for i in _engineering_insights(conn, today)]
    if eng:
        blocks = ["Things about your engineering portfolio that are not obvious:"]
        blocks.extend(f"- {e}" for e in eng)
    else:
        blocks = ["Nothing non-obvious jumps out yet — no repeated solution, "
                  "converging trend, or commercial shift is recorded. Keep "
                  "ingesting and I'll surface surprises as they appear."]
    return blocks, {"surprise": eng}


def evidence_strategy(conn, today, axis: str) -> tuple[list[str], dict]:
    """Forward a need to an existing strategy axis (impact/platform/learning/
    opportunity/priority/converge/merge) — composed, not a new router."""
    from .strategy import (
        strategy_impact, strategy_platform, strategy_learning, strategy_opportunity,
        strategy_priority, strategy_converge, strategy_merge,
    )
    dispatch = {
        "impact": strategy_impact, "platform": strategy_platform,
        "learning": strategy_learning, "opportunity": strategy_opportunity,
        "priority": strategy_priority, "converge": strategy_converge,
        "merge": strategy_merge,
    }
    fn = dispatch.get(axis, strategy_priority)
    out = fn(conn, today)
    return out, {"strategy_axis": axis}
