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


def _today() -> dt.date:
    return dt.date.today()


def _known_techs(conn) -> set[str]:
    techs: set[str] = set()
    for r in q.all_repositories(conn):
        if r.id is not None:
            techs |= {t.tech for t in get_technologies(conn, r.id)}
    return techs


def classify(question: str, conn) -> str:
    """Determine the query intent from the question text + known entities."""
    qlow = question.lower()
    techs = {t.lower() for t in _known_techs(conn)}

    if any(w in qlow for w in ("hello", "hi ", "hey", "thanks", "thank you", "who are you")):
        return "chitchat"
    if "compare" in qlow or " vs " in qlow or " versus " in qlow or "difference" in qlow:
        return "compare"
    # Architecture explanation / how-it-works.
    if any(w in qlow for w in (
        "how is", "how does", "how do", "architecture", "architect", "explain",
        "built", "structure", "entry point", "entry points", "startup",
        "how it works", "how it's built", "components", "implement",
    )):
        return "architecture"
    # Cross-repo similarity / reuse / shared code.
    if any(w in qlow for w in (
        "similar", "similarities", "duplicate", "duplicated", "share code",
        "sharing code", "shared code", "reuse", "reusable", "overlap",
        "same layout", "alike", "compare the architectures",
    )):
        return "similarity"
    if "related" in qlow or "how are" in qlow or "connection" in qlow:
        return "related"
    if "why" in qlow or "purpose" in qlow or "what is" in qlow or "what does" in qlow or "tell me about" in qlow:
        return "describe"
    if "inactive" in qlow or "abandoned" in qlow or "stale" in qlow or "dead" in qlow:
        return "inactive"
    if "newest" in qlow or "recent" in qlow or "latest" in qlow:
        return "newest"
    if "most active" in qlow or "work on next" in qlow or "should i" in qlow:
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
        r = _detect_repo(question, conn)
        if not r or r.id is None:
            ev.raw["note"] = "could not identify repository"
            return ev
        others = [o for o in q.all_repositories(conn) if o.id is not None and o.id != r.id]
        rels = []
        for o in others:
            pairs = q.relationships_between(conn, r.id, o.id)
            if pairs:
                rels.append(f"{o.name}: " + "; ".join(f"{p.kind} ({p.evidence})" for p in pairs))
        if rels:
            ev.blocks = rels
        else:
            ev.blocks = [f"No evidence-backed relationships found for {r.name}."]
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
        lines.append("Evidence:")
        lines.append("- " + arch.evidence.replace("\n", "\n- "))
        if comps:
            lines.append("Major components:")
            for c in comps:
                lines.append(f"- {c.name} ({c.evidence})")
        if eps:
            lines.append("Primary entry points:")
            for e in eps:
                lines.append(f"- {e.kind}: {e.detail} ({e.evidence})")
        if arch.data_flow:
            lines.append("Data flow:")
            lines.append("- " + "\n- ".join(arch.data_flow))
        if arch.known_patterns:
            lines.append("Known patterns:")
            lines.append("- " + "\n- ".join(arch.known_patterns))
        if arch.complexity:
            lines.append(f"Potential complexity: {arch.complexity}")
        ev.blocks = lines
        ev.raw["repo"] = r.name
        ev.raw["architecture"] = arch.architecture
        return ev

    if intent == "similarity":
        shared_comp = q.shared_components(conn)
        shared_ep = q.shared_entry_points(conn)
        pairs = q.similar_layouts(conn)
        reuse = q.reuse_opportunities(conn)
        blocks: list[str] = []
        if reuse:
            blocks.append("Realistic shared-code opportunities (evidence-backed):")
            for line in reuse:
                blocks.append(f"- {line}")
        if pairs:
            blocks.append("Repositories with similar layouts/architecture:")
            for a, b in pairs:
                blocks.append(f"- {a} and {b}")
        if not blocks:
            blocks = ["No evidence-backed cross-repository similarities found."]
        ev.blocks = blocks
        ev.raw["shared_components"] = shared_comp
        ev.raw["shared_entry_points"] = shared_ep
        ev.raw["similar_layouts"] = [list(p) for p in pairs]
        return ev

    if intent == "describe":
        r = _detect_repo(question, conn)
        if not r or r.id is None:
            ev.raw["note"] = "could not identify repository"
            return ev
        card = q.identity_card(conn, r.id, today)
        ev.blocks = [_card_text(card)]
        ev.raw["repo"] = r.name
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
        active = q.most_active(conn, today, 3)
        newest = q.newest_repos(conn, 3)
        dirty = [r.name for r in q.all_repositories(conn) if r.is_dirty]
        lines = []
        if active:
            lines.append(f"Most active by commit frequency: {active[0][0].name} "
                         f"(~{active[0][1]:.1f} commits/day)")
        if newest:
            lines.append(f"Newest project: {newest[0].name}")
        if dirty:
            lines.append(f"Has uncommitted changes: {', '.join(dirty)}")
        ev.blocks = lines or ["No activity data available."]
        ev.raw["recommend"] = lines
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
    intent = classify(question, conn)
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
