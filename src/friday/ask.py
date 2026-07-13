"""Conversational query over the knowledge base.

Evidence-first: Friday retrieves relevant rows from SQLite, builds an evidence
package, and only then (if an LLM is configured) asks the model to synthesize a
concise answer *from that evidence*. The LLM never retrieves and never invents.
When no LLM is configured, structured questions are answered deterministically;
ambiguous ones prompt the user to set FRIDAY_LLM_MODEL.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import query as q
from .db import Repository, get_technologies
from .identity import explain_project_from_conn
from .llm import _enabled as llm_enabled
from .llm import _extract_content

_CHITCHAT = {
    "hello", "hi", "hey", "thanks", "thank you", "ok", "okay", "cool", "nice",
    "who are you", "what are you", "help",
}


@dataclass
class Evidence:
    """The retrieved facts the answer must be grounded in."""

    intent: str
    blocks: list[str] = field(default_factory=list)  # human-readable evidence lines
    raw: dict = field(default_factory=dict)  # structured data for the LLM

    def is_empty(self) -> bool:
        return not self.blocks


@dataclass
class Answer:
    text: str
    evidence: Evidence
    used_llm: bool


@dataclass
class Intent:
    """Structured understanding of a question, produced by LLM extraction.

    The LLM NEVER retrieves or answers — it only identifies what the user is
    asking (intent), which entities are involved, whether a comparison is
    requested, whether the question spans the whole workspace, and how
    confident it is. Deterministic retrieval runs afterward from this object.
    """

    intent: str
    entities: list[str] = field(default_factory=list)
    compare: bool = False
    workspace: bool = False
    confidence: float = 1.0


# Canonical retrieval intents — must match the dispatch in `retrieve`. The LLM is
# instructed to return exactly one of these so the extracted intent drives the
# existing deterministic retrieval unchanged.
_VALID_INTENTS = {
    "chitchat", "compare", "related", "architecture", "describe", "similarity",
    "inactive", "newest", "recommend", "portfolio", "value", "overlap",
    "integration", "workspace", "by-tech", "insights", "general",
}


def _today() -> dt.date:
    return dt.date.today()


def _known_techs(conn) -> set[str]:
    techs: set[str] = set()
    for r in q.all_repositories(conn):
        if r.id is not None:
            techs |= {t.tech for t in get_technologies(conn, r.id)}
    return techs


def deterministic_classifier(question: str, conn) -> str:
    """OFFLINE fallback: keyword cascade over question text + known entities.

    Used only when no LLM is configured (or extraction fails). The spec forbids
    keyword routing as the *primary* path; this remains as graceful degradation,
    not as the default. Keep it in sync with the canonical intent set.
    """
    qlow = question.lower()
    techs = {t.lower() for t in _known_techs(conn)}

    if any(w in qlow for w in ("hello", "hi ", "hey", "thanks", "thank you", "who are you")):
        return "chitchat"
    if "compare" in qlow or " vs " in qlow or " versus " in qlow or "difference" in qlow:
        return "compare"
    # Workspace-level intents (Milestone 3.6) — placed before the generic
    # "which project" -> by-tech fallback so "which project is most valuable"
    # routes here, not to a technology lookup.
    if any(w in qlow for w in (
        "engineering universe", "how has my work evolved", "where is my work heading",
        "my direction", "overall picture", "big picture", "my career",
    )):
        return "workspace"
    if any(w in qlow for w in (
        "am i building", "themes", "patterns across", "what am i building",
        "seem to be building", "building", "emerge", "emerging",
        "repeatedly solving", "skills am i", "across my projects",
        "my portfolio", "my work", "what am i working on",
    )):
        return "portfolio"
    if "most valuable" in qlow or "highest value" in qlow or "worth most" in qlow:
        return "value"
    if "integrate" in qlow or "integration with friday" in qlow or "integration point" in qlow:
        return "integration"
    # Meaningful overlap (Milestone 3.6): "how do my projects overlap", "what
    # parts overlap" — compare along dimensions, not syntax. Placed before the
    # architecture matcher so "how do my projects overlap" doesn't become an
    # architecture deep-dive, and before similarity (shared code / reuse).
    if "overlap" in qlow:
        return "overlap"
    # Relationship questions first (so "how is X related to Y" is not swallowed
    # by the architecture matcher's "how is" rule). Audit §5: weak/strong
    # relationships must be presented distinctly.
    if "related" in qlow or "how are" in qlow or "connection" in qlow:
        return "related"
    # Architecture explanation / how-it-works (granular technical deep-dive).
    # Placed BEFORE the human-explain matcher so "Explain X's architecture"
    # routes here (data_flow / components), not to the onboarding summary.
    if any(w in qlow for w in (
        "how is", "how does", "how do", "architecture", "architect",
        "built", "structure", "entry point", "entry points", "startup",
        "how it works", "how it's built", "components", "implement",
    )):
        return "architecture"
    # Human explanations: purpose/identity first (Milestone 3.5 §2, §3).
    # "explain"/"walk me through"/"tell me about" map here; "what is" too, so
    # "Explain Vivaha." and "What is Aether?" produce the onboarding answer.
    if any(w in qlow for w in (
        "explain", "walk me through", "tell me about", "describe",
        "what is", "what does", "what are", "overview of",
    )):
        return "describe"
    # Cross-repo similarity / reuse / shared code.
    if any(w in qlow for w in (
        "similar", "similarities", "duplicate", "duplicated", "share code",
        "sharing code", "shared code", "reuse", "reusable",
        "same layout", "alike", "comparable", "teach each other",
        "compare the architectures",
    )):
        return "similarity"
    if "why" in qlow or "purpose" in qlow:
        return "describe"
    if "inactive" in qlow or "abandoned" in qlow or "stale" in qlow or "dead" in qlow:
        return "inactive"
    if "newest" in qlow or "recent" in qlow or "latest" in qlow:
        return "newest"
    if "most active" in qlow or "work on next" in qlow or "should i" in qlow:
        return "recommend"
    if any(w in qlow for w in (
        "continue", "pause", "most attention", "which project should i",
        "which should i", "what deserves", "deserve my",
    )):
        return "recommend"
    if "insight" in qlow or "observation" in qlow or "overview" in qlow:
        return "insights"
    if "share" in qlow or "use" in qlow or "which project" in qlow:
        for t in techs:
            if t.lower() in qlow:
                return "by-tech"
        for t in ("rust", "python", "go", "typescript", "java", "c++", "javascript"):
            if t in qlow:
                return "by-tech"
        return "by-tech"
    return "general"


def _intent_explanation() -> str:
    """System prompt for LLM intent extraction (spec: one prompt, JSON only).

    The model is explicitly told it is NOT answering — only identifying intent,
    entities, comparison, workspace scope, and confidence. No prose, no retrieval.
    """
    intents = ", ".join(sorted(_VALID_INTENTS))
    return (
        "You are Friday's intent extractor. You are NOT answering the question. "
        "You are ONLY identifying what the user is asking so a deterministic "
        "retriever can fetch the right evidence.\n"
        "Identify:\n"
        "  - intent: one of [" + intents + "]\n"
        "      chitchat = greeting, thanks, 'who are you'\n"
        "      compare = an explicit request to compare two repositories\n"
        "      related = how one repo relates to another\n"
        "      architecture = how a repo is built / its internals / components\n"
        "      describe = explain a single project's purpose / identity\n"
        "      similarity = shared code / reuse / similar layout across repos\n"
        "      inactive = which repos are stale / abandoned / dead\n"
        "      newest = which repos are newest / most recent\n"
        "      recommend = what to continue / pause / work on next / prioritize\n"
        "      portfolio = recurring themes / what the user is building / patterns\n"
        "      value = which project is most valuable / worth most\n"
        "      overlap = meaningful overlap BETWEEN projects (problem domain, "
        "architecture, persistence, business goal) — NOT shared code\n"
        "      integration = which project should integrate with Friday / merge into one system\n"
        "      workspace = the whole engineering universe / direction / evolution\n"
        "      by-tech = which projects use a technology or language\n"
        "      insights = workspace observations / overview\n"
        "      general = does not fit any other category\n"
        "  - entities: repository / project names mentioned (exact names from context)\n"
        "  - compare: true ONLY if the user explicitly wants two repos compared\n"
        "  - workspace: true if the question spans the WHOLE workspace, not one repo\n"
        "  - confidence: 0.0-1.0, your certainty about this classification\n"
        "Understand questions naturally — do NOT rely on literal keywords. "
        "e.g. 'What am I building?', 'What seems to connect my projects?', "
        "'What direction is my work heading?', 'If someone saw my repositories what "
        "would they think I build?' should all be portfolio. 'What overlaps?', "
        "'What should integrate?', 'What should eventually become one system?' are "
        "overlap / integration respectively.\n"
        "Return valid JSON only. No prose, no explanations, no markdown. "
        "If confidence is genuinely low (<0.3), return {\"intent\": \"Unknown\"}. "
        'Schema: {"intent": str, "entities": [str], "compare": bool, "workspace": bool, "confidence": float}'
    )


def extract_intent(question: str, conn) -> Optional[Intent]:
    """Structured intent extraction — the ONLY step that uses the LLM for
    understanding.

    OFFLINE (no LLM configured): returns the keyword classifier wrapped as an
    Intent (graceful degradation).
    ONLINE: calls the LLM. Returns the parsed Intent, or None when the model was
    available but genuinely uncertain (returned "Unknown" / an invalid label) —
    in which case the caller should admit it couldn't determine the question.
    The LLM never retrieves and never answers here.
    """
    if not llm_enabled():
        return Intent(intent=deterministic_classifier(question, conn))

    # Give the model the vocabulary it can reason against (names only — no PII).
    repo_names = [r.name for r in q.all_repositories(conn) if r.id is not None]
    names_block = ", ".join(repo_names) if repo_names else "(no repositories ingested yet)"

    base = os.environ.get("FRIDAY_LLM_BASE_URL", "http://localhost:20128/v1").rstrip("/")
    model = os.environ["FRIDAY_LLM_MODEL"]
    api_key = os.environ["FRIDAY_LLM_API_KEY"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _intent_explanation()},
            {"role": "user", "content": (
                f"Known projects in this workspace: {names_block}\n\n"
                f"Question: {question}"
            )},
        ],
        "temperature": 0.0,
    }
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        return None
    content = _extract_content(raw)
    if not content:
        return None
    # Strip a markdown ```json ... ``` fence if the model wrapped its answer.
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip().strip("`").strip()
    if content.startswith("json"):
        content = content[4:].strip()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None

    intent = data.get("intent")
    if intent == "Unknown" or not intent:
        # Honest uncertainty — signal the caller to say so plainly.
        return None
    if intent not in _VALID_INTENTS:
        return None  # unknown label — degrade to keyword fallback
    return Intent(
        intent=intent,
        entities=list(data.get("entities") or []),
        compare=bool(data.get("compare", False)),
        workspace=bool(data.get("workspace", False)),
        confidence=float(data.get("confidence", 0.5)),
    )


def classify(question: str, conn) -> str:
    """Public classify (kept for tests / direct callers).

    ONLINE: LLM intent extraction. OFFLINE: keyword fallback. Returns the
    canonical intent string consumed by `retrieve`.
    """
    intent = extract_intent(question, conn)
    if intent is not None:
        return intent.intent
    return deterministic_classifier(question, conn)


def _detect_repo(question: str, conn) -> Optional[Repository]:
    # Try explicit names in the question.
    qlow = question.lower()
    repos = q.all_repositories(conn)
    for r in repos:
        if r.name.lower() in qlow:
            return r
    # Strip question words, look for token overlap.
    cleaned = re.sub(r"[^a-z0-9 ]", " ", qlow)
    toks = {t for t in cleaned.split() if len(t) > 2 and t not in _STOP}
    for r in repos:
        rlow = r.name.lower()
        if any(t in rlow for t in toks):
            return r
    return None


_STOP = {
    "what", "which", "why", "who", "how", "are", "is", "the", "a", "an", "and",
    "or", "of", "to", "do", "does", "did", "you", "your", "my", "me", "i", "use",
    "using", "project", "projects", "repository", "repositories", "repo", "repos",
    "share", "sharing", "with", "about", "tell", "describe", "compare", "related",
    "between", "inactive", "abandoned", "stale", "newest", "recent", "latest",
    "most", "active", "should", "next", "insight", "observations", "overview",
}


def retrieve(question: str, intent: str, conn) -> Evidence:
    today = _today()
    ev = Evidence(intent=intent)

    if intent == "chitchat":
        return ev

    if intent == "compare":
        # Find all repositories explicitly named in the question (distinct).
        repos = q.all_repositories(conn)
        qlow = question.lower()
        named = [r for r in repos if r.name.lower() in qlow]
        # De-duplicate while preserving order.
        seen = set()
        targets = []
        for r in named:
            if r.name not in seen:
                seen.add(r.name)
                targets.append(r)
        if len(targets) < 2:
            # Fall back to the single detected repo if only one named.
            single = _detect_repo(question, conn)
            if single and single not in targets:
                targets.append(single)
        if len(targets) < 2:
            ev.raw["note"] = "could not identify two repositories to compare"
            return ev
        cards = []
        for r in targets[:2]:
            if r.id is not None:
                card = q.identity_card(conn, r.id, today)
                cards.append(_card_text(card))
                ev.raw.setdefault("compare", []).append(r.name)
        ev.blocks = cards
        return ev

    if intent == "related":
        qlow = question.lower()
        r = _detect_repo(question, conn)
        if not r or r.id is None:
            ev.raw["note"] = "could not identify repository"
            return ev
        # Weak relationships are coincidences (shared author/org/language), not
        # insight. Hide them unless the user explicitly asks to include weak ones.
        include_weak = any(w in qlow for w in ("weak", "all relationships", "everything", "including"))
        others = [o for o in q.all_repositories(conn) if o.id is not None and o.id != r.id]
        rels: list[str] = []
        for o in others:
            pairs = q.relationships_between(conn, r.id, o.id)
            if not pairs:
                continue
            strong = [p for p in pairs if p.strength != "Weak"]
            weak = [p for p in pairs if p.strength == "Weak"]
            # Answer WHY they are related, not just WHAT they share.
            why = [f"{p.kind.replace('shared-', 'shared ')} — {p.evidence}" for p in strong]
            if include_weak and weak:
                why += [f"weak coincidence: {p.kind.replace('shared-', 'shared ')} ({p.evidence})"
                        for p in weak]
            if why:
                rels.append(f"{o.name}: " + "; ".join(why))
        if rels:
            ev.blocks = rels
        elif include_weak:
            ev.blocks = [f"No relationships found for {r.name}."]
        else:
            ev.blocks = [
                f"No strong or medium relationships found for {r.name}. "
                f"(Weak coincidences like shared author/language are omitted — "
                f"ask 'including weak relationships' to see them.)"
            ]
        ev.raw["repo"] = r.name
        return ev

    if intent == "architecture":
        r = _detect_repo(question, conn)
        if not r or r.id is None:
            ev.raw["note"] = "could not identify repository"
            return ev
        arch = q.architecture_of(conn, r.id)
        comps = q.components_of(conn, r.id)
        eps = q.entry_points_of(conn, r.id)
        if arch is None:
            ev.blocks = [
                f"No architecture knowledge stored for {r.name}. Run `friday analyze {r.path}`."
            ]
            ev.raw["repo"] = r.name
            return ev
        lines = [f"{r.name} — {arch.architecture}"]
        if arch.confidence:
            lines.append(f"Confidence: {arch.confidence}")
        lines.append("Evidence:")
        lines.append("- " + arch.evidence.replace("\n", "\n- "))
        if comps:
            lines.append("Major components:")
            for c in comps:
                # Components are Weak concepts — say so, don't imply reusable code.
                lines.append(f"- {c.name} ({c.strength} evidence): {c.evidence}")
        if eps:
            # Group entry points by role (Application / Framework root / Utility).
            app_eps = [e for e in eps if e.kind in ("main()", "CLI", "FastAPI app",
                                                   "Flask app", "Next.js app", "Cargo binary", "Executable script")]
            util_eps = [e for e in eps if e.kind == "Utility script"]
            if app_eps:
                lines.append("Application entry points:")
                for e in app_eps:
                    lines.append(f"- {e.kind}: {e.detail} ({e.evidence})")
            if util_eps:
                lines.append("Utility scripts (not application entry points):")
                for e in util_eps:
                    lines.append(f"- {e.detail} ({e.evidence})")
        if arch.data_flow:
            lines.append("Data flow:")
            lines.append("- " + "\n- ".join(arch.data_flow.split("\n")))
        if arch.known_patterns:
            lines.append("Known patterns:")
            lines.append("- " + "\n- ".join(arch.known_patterns.split("\n")))
        if arch.complexity:
            lines.append(f"Potential complexity: {arch.complexity}")
        ev.blocks = lines
        ev.raw["repo"] = r.name
        ev.raw["architecture"] = arch.architecture
        return ev

    if intent == "similarity":
        pairs = q.similar_layouts(conn)
        reuse = q.reuse_opportunities(conn)
        blocks: list[str] = []
        # Actionable shared-code candidates (Medium/Strong evidence only).
        if reuse:
            blocks.append("Realistic shared-code opportunities (Medium/Strong evidence only):")
            for line in reuse:
                blocks.append(f"- {line}")
        # "Could these teach each other something?" — compare along dimensions,
        # not a flat dependency list (audit §9). Compare any pair that shares a
        # Medium/Strong relationship OR the same architecture label.
        name_by_id = {r.id: r.name for r in q.all_repositories(conn) if r.id is not None}
        id_by_name = {v: k for k, v in name_by_id.items()}
        compared: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        from .db import get_all_relationships

        for rel in get_all_relationships(conn):
            if rel.strength == "Weak":
                continue
            an, bn = name_by_id.get(rel.repo_a), name_by_id.get(rel.repo_b)
            if an and bn:
                seen_pairs.add(tuple(sorted((an, bn))))
        # Also compare same-architecture label pairs (no relationship needed).
        for a, b in pairs:
            seen_pairs.add(tuple(sorted((a, b))))

        for an, bn in seen_pairs:
            a_id, b_id = id_by_name.get(an), id_by_name.get(bn)
            if a_id is None or b_id is None:
                continue
            cmp = q.compare_repositories(conn, a_id, b_id)
            dims = [v for v in (
                cmp["architecture"], cmp["responsibilities"], cmp["deployment"],
                cmp["persistence"], cmp["interfaces"],
            ) if v]
            if dims:
                compared.append(f"- {an} and {bn}: " + "; ".join(dims) + ".")
        if compared:
            blocks.append("What these projects share (architecture, not just dependencies):")
            blocks.extend(compared)
        elif pairs:
            blocks.append("Repositories with similar architecture labels (verify before acting):")
            for a, b in pairs:
                blocks.append(f"- {a} and {b}")
        if not blocks:
            blocks = ["No evidence-backed cross-repository similarities found."]
        ev.blocks = blocks
        ev.raw["reuse"] = reuse
        ev.raw["similar_layouts"] = [list(p) for p in pairs]
        return ev

    if intent == "describe":
        r = _detect_repo(question, conn)
        if not r or r.id is None:
            ev.raw["note"] = "could not identify repository"
            return ev
        # Human explanation: purpose/identity first, then architecture (§2/§3/§11).
        text = explain_project_from_conn(conn, r.id, detailed=True)
        ev.blocks = [text]
        ev.raw["repo"] = r.name
        ev.raw["identity"] = True
        return ev

    if intent == "inactive":
        days = 180 if ("abandon" in question.lower()) else q.STALE_DAYS
        repos = q.inactive_repos(conn, today, days)
        label = "abandoned" if days >= q.ABANDONED_DAYS else "inactive"
        if repos:
            ev.blocks = [
                f"{r.name} is {label}: last commit {r.last_commit_date[:10]} "
                f"({(today - dt.date.fromisoformat(r.last_commit_date[:10])).days} days ago, "
                f"threshold {days} days)"
                for r in repos
            ]
        else:
            ev.blocks = [
                f"No repositories are {label}. Every repo has a commit within the "
                f"last {days} days (per git commit dates)."
            ]
        ev.raw["count"] = len(repos)
        return ev

    if intent == "newest":
        repos = q.newest_repos(conn, 3)
        ev.blocks = [
            f"{r.name}: first commit {r.first_commit_date[:10]}" for r in repos
        ]
        ev.raw["newest"] = [r.name for r in repos]
        return ev

    if intent == "recommend":
        # "Which should I continue?" — combine activity, blockers, importance,
        # business value, recent work, uncommitted changes, README maturity and
        # purpose (audit §10). Not commit counts alone.
        priorities = q.workspace_priorities(conn, today, n=3)
        if not priorities:
            ev.blocks = ["I don't have enough evidence to prioritize — no repositories are ingested."]
            return ev
        lines: list[str] = []
        top = priorities[0]
        top_repo, top_reasons = top
        lines.append(f"If you want the highest-leverage next step, continue {top_repo.name}.")
        lines.append("Why: " + "; ".join(top_reasons) + ".")
        if len(priorities) > 1:
            lines.append("Next after that:")
            for repo, reasons in priorities[1:]:
                lines.append(f"- {repo.name}: {'; '.join(reasons)}.")
        ev.blocks = lines
        ev.raw["recommend"] = lines
        return ev

    if intent == "portfolio":
        from .portfolio import portfolio_synthesis, detect_themes

        blocks = portfolio_synthesis(conn, today)
        ev.blocks = blocks
        ev.raw["portfolio"] = blocks
        ev.raw["themes"] = [
            {"theme": t.theme, "repos": t.repos, "confidence": t.confidence}
            for t in detect_themes(conn, today)
        ]
        return ev

    if intent == "value":
        from .portfolio import project_value_ranking

        ranked = project_value_ranking(conn, today)
        if not ranked:
            ev.raw["note"] = "no value signals available"
            return ev
        top = ranked[0]
        lines = [
            f"If 'most valuable' means accumulated evidence of purpose, business "
            f"value, activity and importance, {top.repo} ranks highest."
        ]
        for v in ranked[:3]:
            lines.append(f"- {v.repo} ({v.confidence} confidence): {'; '.join(v.signals)}.")
        lines.append(
            f"Confidence: {top.confidence} — based on stored purpose, business "
            f"value, activity and relationship evidence, not commit counts alone."
        )
        ev.blocks = lines
        ev.raw["value"] = [
            {"repo": v.repo, "confidence": v.confidence, "signals": v.signals}
            for v in ranked
        ]
        return ev

    if intent == "overlap":
        from .portfolio import meaningful_overlap

        results = meaningful_overlap(conn, today)
        if not results:
            ev.blocks = [
                "No meaningful cross-project overlap found in the stored evidence "
                "(architecture, shared purpose, persistence, auth, storage, config)."
            ]
            return ev
        blocks = ["Meaningful overlap across your projects (by responsibility and problem domain, not syntax):"]
        for o in results:
            blocks.append(f"- {o.a} and {o.b} ({o.confidence} confidence): "
                          + "; ".join(o.dimensions) + ".")
        ev.blocks = blocks
        ev.raw["overlap"] = [
            {"a": o.a, "b": o.b, "dimensions": o.dimensions, "confidence": o.confidence}
            for o in results
        ]
        return ev

    if intent == "integration":
        from .portfolio import integration_opportunities

        cands = integration_opportunities(conn, today)
        if not cands:
            ev.blocks = [
                "No project currently shows reasonable evidence for integration "
                "with Friday (no shared AI/assistant/workflow/OS purpose or "
                "overlapping technology). Confidence: Weak."
            ]
            ev.raw["integration"] = []
            return ev
        blocks = ["Candidates to integrate with Friday (reasoned from project identity):"]
        for c in cands:
            blocks.append(f"- {c.repo} ({c.confidence} confidence): {c.reason}.")
        ev.blocks = blocks
        ev.raw["integration"] = [
            {"repo": c.repo, "confidence": c.confidence, "reason": c.reason}
            for c in cands
        ]
        return ev

    if intent == "workspace":
        from .portfolio import engineering_universe

        lines = engineering_universe(conn, today)
        ev.blocks = lines
        ev.raw["universe"] = lines
        return ev

    if intent == "by-tech":
        tech = _detect_tech(question, conn)
        if tech:
            repos = q.projects_by_tech(conn, tech)
            if repos:
                ev.blocks = [f"{r.name} uses {tech}" for r in repos]
            else:
                ev.blocks = [f"No repositories use {tech} (per detected technologies)."]
            ev.raw["tech"] = tech
            ev.raw["repos"] = [r.name for r in repos]
            return ev
        lang = _detect_lang(question)
        if lang:
            repos = q.projects_by_language(conn, lang)
            ev.blocks = [f"{r.name} uses {lang}" for r in repos] or [f"No repositories use {lang}."]
            ev.raw["lang"] = lang
            return ev
        ev.raw["note"] = "could not identify a technology or language"
        return ev

    if intent == "insights":
        from .insights import generate_insights

        ins = generate_insights(conn, today)
        ev.blocks = [i.text for i in ins]
        ev.raw["insights"] = [i.text for i in ins]
        return ev

    # general
    ev.raw["note"] = "intent not recognized"
    return ev


def _detect_tech(question: str, conn) -> Optional[str]:
    qlow = question.lower()
    techs = _known_techs(conn)
    # Exact (case-insensitive) then substring.
    for t in techs:
        if t.lower() in qlow:
            return t
    aliases = {
        "rust": "Rust", "python": "Python", "go": "Go", "golang": "Go",
        "typescript": "TypeScript", "ts": "TypeScript", "java": "Java",
        "c++": "C++", "javascript": "JavaScript", "js": "JavaScript",
        "react": "React", "next": "Next.js", "nextjs": "Next.js",
        "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
        "supabase": "Supabase", "docker": "Docker", "sqlite": "SQLite",
        "postgres": "Postgres", "postgresql": "Postgres", "redis": "Redis",
        "pytorch": "PyTorch", "tensorflow": "TensorFlow",
    }
    for k, v in aliases.items():
        if re.search(rf"\b{re.escape(k)}\b", qlow) and v in techs:
            return v
    return None


def _detect_lang(question: str) -> Optional[str]:
    qlow = question.lower()
    for name, canon in (
        ("rust", "Rust"), ("python", "Python"), ("go", "Go"),
        ("typescript", "TypeScript"), ("java", "Java"), ("c++", "C++"),
        ("javascript", "JavaScript"),
    ):
        if name in qlow:
            return canon
    return None


def _card_text(card) -> str:
    if card is None:
        return "No data."
    from .summary import _purpose_line

    r = card.repo
    lines = [f"{r.name}"]
    lines.append(f"Purpose: {_purpose_line(r.readme_summary)}")
    if card.tech_names:
        lines.append("Technologies: " + ", ".join(sorted(card.tech_names)))
    if r.maturity and r.maturity != "Unknown":
        lines.append(f"Maturity: {r.maturity}")
    lines.append(f"Activity: {card.activity}")
    if card.key_observations:
        lines.append("Observations: " + "; ".join(card.key_observations))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are Friday, an operating partner that answers questions about the user's "
    "software projects using ONLY the provided Evidence. Rules:\n"
    "1. Answer concisely and in plain prose.\n"
    "2. Use ONLY facts present in the Evidence block. Never invent repositories, "
    "technologies, dates, or relationships.\n"
    "3. If the Evidence is insufficient to answer, say so plainly (e.g. "
    "'I don't have enough evidence to answer that.').\n"
    "4. For 'what should I work on next' style questions, you MAY offer a grounded "
    "suggestion derived from the activity signals in the Evidence (most active, "
    "newest, uncommitted changes), clearly framed as a suggestion, not a command.\n"
    "5. Cite the basis briefly where natural (README, git metadata, technology "
    "detection, relationships).\n"
    "6. PRIMACY: when a question is about one project, spend 80-90% of the answer "
    "on that project's purpose, context and meaning. Put its relationships and "
    "other repositories only near the END. Never open with implementation, "
    "architecture dumps, or component lists.\n"
    "7. CONFIDENCE: when the Evidence supports a judgement (value, overlap, "
    "integration, themes), state Confidence: Strong / Medium / Weak and the basis. "
    "Prefer context, purpose and engineering meaning; reserve architecture detail "
    "for the final part of the answer.\n"
    "Do not role-play or add commentary beyond the answer."
)


def _synthesize(question: str, ev: Evidence) -> Optional[str]:
    """Call the LLM to produce an answer from the evidence. Returns None on any
    failure (caller falls back)."""
    if not llm_enabled():
        return None
    evidence_str = "\n".join(ev.blocks) if ev.blocks else json.dumps(ev.raw, indent=2)
    if not evidence_str.strip():
        evidence_str = "(no retrieved evidence)"
    user = (
        f"Question: {question}\n\n"
        f"Evidence:\n{evidence_str}\n\n"
        f"Answer (grounded only in Evidence):"
    )
    # Reuse the SSE-aware client; we build the request manually to control prompt.
    import urllib.request

    base = os.environ.get("FRIDAY_LLM_BASE_URL", "http://localhost:20128/v1").rstrip("/")
    model = os.environ["FRIDAY_LLM_MODEL"]
    api_key = os.environ["FRIDAY_LLM_API_KEY"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
    }
    import json as _json

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        return _extract_content(raw)
    except Exception:
        return None


def _deterministic_answer(question: str, ev: Evidence, intent: str) -> str:
    if intent == "chitchat":
        return ("I'm Friday, your workspace operating partner. Ask me about your "
                "projects — which use a technology, what a project is for, how two "
                "repos relate, or which look abandoned.")
    if not ev.blocks:
        return ("I don't have enough evidence to answer that. Try rephrasing, or set "
                "FRIDAY_LLM_MODEL to let me handle open-ended questions.")
    # General fallback: just present the evidence plainly.
    return "\n".join(ev.blocks)


def ask(question: str, conn, verbose: bool = False) -> Answer:
    # ONLINE: LLM understands intent. OFFLINE / failure: keyword fallback.
    extracted = extract_intent(question, conn)
    if extracted is None:
        # Could not confidently determine intent (LLM unavailable, or it returned
        # "Unknown" / an invalid label). Honest, extremely-rare degradation.
        return Answer(
            text=("I couldn't confidently determine what you are asking. "
                  "Try rephrasing, or set FRIDAY_LLM_MODEL so I can interpret "
                  "open-ended questions."),
            evidence=Evidence(intent="general"),
            used_llm=False,
        )
    intent = extracted.intent

    ev = retrieve(question, intent, conn)

    text: Optional[str] = None
    used_llm = False
    if intent == "chitchat":
        text = _deterministic_answer(question, ev, intent)
    elif llm_enabled():
        text = _synthesize(question, ev)
        used_llm = text is not None
    if text is None:
        text = _deterministic_answer(question, ev, intent)

    return Answer(text=text, evidence=ev, used_llm=used_llm)
