"""Evidence-scoped providers — where answers get their ground truth.

One provider per question type, registered in a registry. Each provider
gathers *evidence* from real state (the V4 DB — missions, actions,
memories — and, when present, V3's read-only bridge) and returns
:class:`Answer` with citations, or ``None`` when it has no evidence for
the question. No provider fabricates — the engine turns an empty
evidence set into an honest "I don't know yet".

Hermetic design: providers take a ``conn`` (V4 DB) and read only via
``friday_v4.db`` typed helpers; V3 data is optional and additive.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Callable, Optional

from .. import db
from .evidence import Answer, Evidence
from .question import Question, QuestionType

logger = logging.getLogger("friday_v4.reasoning.providers")


#: A provider: (question, conn) -> Answer | None (None = no evidence).
Provider = Callable[[Question, object], Optional[Answer]]


def _v3() -> Optional[object]:
    """The read-only V3 bridge, or None when unavailable."""
    try:
        from ..proactive.v3source import V3DataSource
        src = V3DataSource()
        return src if src.is_available() else None
    except Exception as exc:
        logger.debug(f"V3 source unavailable: {exc}")
        return None


# ── Providers ─────────────────────────────────────────────────────────


#: Phrases that ask about the *operator* ("who am I") rather than
#: about Friday ("who are you").
_OPERATOR_QUESTIONS = (
    "who am i", "what do you know about me", "tell me about myself",
    "what do you remember about me", "do you know my name",
    "what's my name", "what is my name", "remember my name",
)


def identity_provider(question: Question, conn) -> Optional[Answer]:
    """Identity questions — Friday's self-knowledge AND the operator.

    Two flavors (wave-10 persona):

    - "who are you" → Friday's own declaration of identity, cited as
      ``v4.self`` (deterministic self-knowledge, not fabrication).
    - "who am I" → the operator's stored identity, read through the
      ``IdentityEngine`` (facts with provenance, ``v4.persona``); an
      empty profile → ``None`` → "I don't know yet" (never invents).

    One provider answers both so every surface (voice/ask/web) gets
    identical identity answers.
    """
    if question.type != QuestionType.IDENTITY:
        return None
    lower = (question.text or "").lower()
    if any(q in lower for q in _OPERATOR_QUESTIONS):
        return _operator_identity_answer(question, conn)
    return _friday_identity_answer(question)


def _friday_identity_answer(question: Question) -> Answer:
    """'who are you' — Friday's deterministic self-declaration."""
    evidence = [Evidence(
        source="v4.self",
        claim="Friday V4 — AI operating partner: project status, "
              "execution, missions, memory, evidence-cited answers")]
    text = ("I'm Friday, your AI operating partner. I watch your "
            "projects, run your tooling in a sandbox, track missions, "
            "remember what matters to you, and answer your questions "
            "with evidence.")
    return Answer(question.text, text, evidence, QuestionType.IDENTITY,
                  confidence=1.0)


def _operator_identity_answer(question: Question, conn) -> Optional[Answer]:
    """'who am I' — the operator's own words, verbatim, or None.

    Reads the conversation log via the persona ``IdentityEngine`` (a
    verbatim view over what the operator actually said — no keyword
    extraction). Each statement is cited as ``v4.exchanges`` evidence;
    an empty log → None → "I don't know yet" (never fabricates).
    """
    try:
        from ..persona import IdentityEngine
        engine = IdentityEngine(conn)
        data = engine.identity_answer()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"persona engine unavailable: {exc}")
        return None
    facts = data.get("facts") or []
    if not facts:
        return None  # nothing said yet → "I don't know yet"
    evidence = [
        Evidence(source="v4.exchanges", claim=claim) for claim in facts
    ]
    text = "Here's what I know about you: " + "; ".join(facts) + "."
    return Answer(question.text, text, evidence, QuestionType.IDENTITY,
                  confidence=0.95)


def status_provider(question: Question, conn) -> Optional[Answer]:
    """'what's the status of my projects?' — missions + recent actions."""
    if question.type != QuestionType.STATUS:
        return None
    evidence: list[Evidence] = []

    missions = db.list_missions(conn, limit=20) or []
    if missions:
        by_status: dict[str, int] = {}
        for m in missions:
            by_status[m.get("status", "planned")] = \
                by_status.get(m.get("status", "planned"), 0) + 1
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
        evidence.append(Evidence(
            source="v4.missions",
            claim=f"{len(missions)} mission(s) — {parts}",
            when=missions[0].get("updated_at", "")))

    recent = (db.recent_actions(conn, limit=10) or [])
    if recent:
        ok = sum(1 for a in recent if a.get("status") == "succeeded")
        evidence.append(Evidence(
            source="v4.actions",
            claim=f"{len(recent)} recent actions, {ok} succeeded",
            when=recent[0].get("created_at", "")))

    if not evidence:
        return None
    lines = [e.claim for e in evidence]
    text = ("Here's the state of things: " + "; ".join(lines) + ".")
    return Answer(question.text, text, evidence, QuestionType.STATUS,
                  confidence=0.9)


def activity_provider(question: Question, conn) -> Optional[Answer]:
    """'what did I do recently?' — the audit trail + V3 observations."""
    if question.type != QuestionType.ACTIVITY:
        return None
    evidence: list[Evidence] = []

    recent = (db.recent_actions(conn, limit=15) or [])
    if recent:
        top = recent[:5]
        claims = [f"{a.get('action_type', '?')}: "
                  f"{(a.get('command') or a.get('goal') or '')[:50]}"
                  for a in top]
        evidence.append(Evidence(
            source="v4.actions",
            claim=", ".join(claims),
            when=top[0].get("created_at", "")))

    src = _v3()
    if src is not None:
        try:
            obs = src.recent_observations(hours=24, limit=5)
            if obs:
                subjects = sorted({o.get("subject", "") for o in obs
                                   if o.get("subject")})
                evidence.append(Evidence(
                    source="v3.observations",
                    claim="recent activity: " + ", ".join(subjects[:5]),
                    when=obs[0].get("observed_at", "")))
        except Exception as exc:
            logger.debug(f"v3 observations failed: {exc}")

    if not evidence:
        return None
    lines = [e.claim for e in evidence]
    text = ("Here's what's been happening: " + "; ".join(lines) + ".")
    return Answer(question.text, text, evidence, QuestionType.ACTIVITY,
                  confidence=0.85)


def mission_provider(question: Question, conn) -> Optional[Answer]:
    """'how's the auth refactor going?' / 'how's it going?' — shepherding.

    Wave 19 (MCU test #1): the answer names the mission's **next step**
    (or its blocker) — Friday proposes next steps, it doesn't just
    report a percentage. With no named target ("how's it going"), the
    latest ACTIVE mission is the one being shepherded (else the newest
    mission overall). No missions → None → the engine's honest "I
    don't know yet".
    """
    if question.type != QuestionType.MISSION:
        return None
    missions = db.list_missions(conn, limit=20) or []
    if not missions:
        # Wave 19 slice 2: "how's it going" with zero missions answered
        # "I don't know yet" — but Friday *does* know: there is no
        # mission. Say so honestly and point at how to start one (the
        # DB was queried; "none" is real state, not fabrication).
        return Answer(
            question.text,
            "You don't have a mission in flight right now — say "
            "'ship the auth refactor by Friday' and I'll plan one.",
            [Evidence(source="v4.missions",
                      claim="no missions in flight")],
            QuestionType.MISSION, confidence=0.95)

    evidence: list[Evidence] = []
    target = (question.target or "").lower()
    if target:
        matches = [m for m in missions
                   if target in (m.get("title", "") or "").lower()]
    else:
        # "how's it going" — the ACTIVE mission is the one being
        # shepherded; fall back to the newest mission overall.
        matches = ([m for m in missions if m.get("status") == "active"]
                   or missions[:1])
    if not matches:
        return None
    for m in matches[:3]:
        mid = m.get("id", "")
        steps = db.list_mission_steps(conn, mid) or []
        done = sum(1 for s in steps if s.get("status") == "completed")
        total = len(steps)
        progress = (done / total) if total else 1.0
        claim = (f"'{m.get('title', '')[:50]}' {int(progress * 100)}% "
                 f"({done}/{total} steps, {m.get('status', '?')})")
        # Shepherding: name the next step (or the blocker). Steps are
        # position-ordered; the first pending step is what runs next.
        pending = [s for s in steps if s.get("status") == "pending"]
        failed = [s for s in steps if s.get("status") == "failed"]
        if pending:
            nxt = pending[0]
            payload = _step_payload(nxt)
            at = payload.get("action_type")
            claim += (f"; next: '{nxt.get('title', '')[:50]}'"
                      + (f" ({at})" if at else " (manual)"))
        elif failed:
            claim += (f"; blocked on failed step "
                      f"'{failed[0].get('title', '')[:50]}'")
        evidence.append(Evidence(source="v4.missions", claim=claim,
                                 when=m.get("updated_at", "")))

    text = "; ".join(e.claim for e in evidence) + "."
    return Answer(question.text, text, evidence, QuestionType.MISSION,
                  confidence=0.9)


def _step_payload(row: dict) -> dict:
    """The step's payload dict (the DB stores it as JSON text)."""
    raw = row.get("payload") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def memory_provider(question: Question, conn) -> Optional[Answer]:
    """'what do you know about X?' — stored memory facts (Wave 10 typed).

    Reads through the typed ``FactMemory`` layer (provenance-aware, same
    decay/strengthen semantics as the rest of the memory layer), never
    the raw table. When the question names a target ("tell me about the
    auth refactor"), only facts mentioning it are cited; otherwise the
    newest facts are surfaced. No facts → None → "I don't know yet".
    """
    if question.type != QuestionType.MEMORY:
        return None
    try:
        from ..memory import FactMemory
        facts = FactMemory(conn)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"memory layer unavailable: {exc}")
        return None

    target = (question.target or "").strip().lower()
    rows = facts.recall(limit=200)
    if target:
        rows = [f for f in rows
                if target in f.key.lower() or target in f.value.lower()][:8]
    else:
        rows = rows[:8]
    if not rows:
        return None

    evidence: list[Evidence] = []
    for f in rows:
        src = f" (from {f.source})" if f.source else ""
        evidence.append(Evidence(
            source="v4.memories",
            claim=f"{f.predicate}: {(f.value or '')[:80]}{src}",
            when=f.updated_at))
    text = ("Here's what I remember: " + "; ".join(e.claim for e in evidence)
            + ".")
    return Answer(question.text, text, evidence, QuestionType.MEMORY,
                  confidence=0.9)


#: Conversation time windows (Wave 15 — one presence). Matched as
#: substrings of the question, in order ("earlier today" must precede
#: "today"). All timestamps are stored in UTC, so windows are computed
#: in UTC; ``date()``-free lexical comparisons keep the queries robust.
_CONVERSATION_WINDOWS: tuple[str, ...] = (
    "this morning", "this afternoon", "this evening", "tonight",
    "earlier today", "today", "yesterday", "last night",
    "this week", "last week",
)


def _conversation_window(text: str,
                         now: Optional[datetime.datetime] = None):
    """(label, since_iso, until_iso|None) for a time-windowed recall.

    Returns None when the question names no window (the provider falls
    back to the recent-N behavior). ``now`` injectable for deterministic
    tests; defaults to the current UTC time. Never raises.
    """
    lower = (text or "").lower()
    for label in _CONVERSATION_WINDOWS:
        if label not in lower:
            continue
        utc = (now or datetime.datetime.now(datetime.timezone.utc))
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=datetime.timezone.utc)
        utc = utc.astimezone(datetime.timezone.utc)
        day = utc.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = day + datetime.timedelta(days=1)
        yesterday = day - datetime.timedelta(days=1)
        monday = day - datetime.timedelta(days=utc.weekday())

        def iso(dt: datetime.datetime) -> str:
            return dt.isoformat(timespec="microseconds")

        if label in ("today", "earlier today"):
            return label, iso(day), None
        if label == "this morning":
            return label, iso(day), iso(day.replace(hour=12))
        if label == "this afternoon":
            return label, iso(day.replace(hour=12)), iso(day.replace(hour=18))
        if label in ("this evening", "tonight"):
            return label, iso(day.replace(hour=18)), iso(tomorrow)
        if label == "yesterday":
            return label, iso(yesterday), iso(day)
        if label == "last night":
            return label, iso(yesterday.replace(hour=18)), iso(day)
        if label == "this week":
            return label, iso(monday), None
        if label == "last week":
            return label, iso(monday - datetime.timedelta(days=7)), iso(monday)
    return None


def conversation_provider(question: Question, conn) -> Optional[Answer]:
    """'what did we talk about?' — conversation history, time-aware.

    Reads the ``sessions``/``exchanges`` tables and summarizes what was
    discussed: the user turns with their intent labels. Wave 15: a
    time-windowed question ("what did we talk about this morning?" /
    "yesterday" / "last night") filters the log to that window — so a
    conversation started in the terminal this morning is recalled from
    voice or the web dashboard. No window → the recent-N behavior.
    No exchanges in scope → None (the engine answers "I don't know
    yet" — never fabricates).
    """
    if question.type != QuestionType.CONVERSATION:
        return None
    window = _conversation_window(question.text)
    if window:
        label, since, until = window
        exchanges = db.recent_exchanges_since(
            conn, since, until, limit=100) or []
    else:
        label = None
        exchanges = db.recent_exchanges(conn, limit=20) or []
    if not exchanges:
        return None

    user_turns = [e for e in exchanges if e.get("role") == "user"]
    topics = [
        (e.get("content") or "").strip()[:60]
        for e in user_turns[:5]
    ]
    intents = sorted({e.get("intent", "") for e in exchanges
                      if e.get("intent")})
    total = len(exchanges)

    claim_parts: list[str] = [f"{total} exchange(s) recorded"]
    if topics:
        claim_parts.append("you asked: " + " · ".join(f"{t!r}" for t in topics))
    if intents:
        claim_parts.append("topics: " + ", ".join(intents[:6]))
    evidence = [
        Evidence(source="v4.exchanges", claim="; ".join(claim_parts),
                 when=exchanges[0].get("created_at", ""))
    ]
    when = f" {label}" if label else ""
    text = (f"Here's what we talked about{when}: "
            + "; ".join(claim_parts) + ".")
    return Answer(question.text, text, evidence,
                  QuestionType.CONVERSATION, confidence=0.85)


def collab_provider(question: Question, conn) -> Optional[Answer]:
    """'what's my team working on?' — peer observations (Wave 5 §2 hardening).

    Reads the collaboration layer's shared observations + known peers
    (read-only; the coordinator's ACL/permissions are respected — only
    what the workspace exposes is surfaced). No collab state → None →
    the engine's honest "I don't know yet". Never raises.
    """
    if question.type != QuestionType.COLLAB:
        return None
    try:
        from ..collab import Coordinator
        coord = Coordinator()
        observations = coord.observations(limit=20) or []
        peers = coord.peers() or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"collab layer unavailable: {exc}")
        return None
    if not observations and not peers:
        return None

    evidence: list[Evidence] = []
    if peers:
        names = [getattr(p, "peer_id", "") or str(p) for p in peers]
        evidence.append(Evidence(
            source="v4.collab.peers",
            claim=f"{len(peers)} peer(s): " + ", ".join(names[:5])))
    obs = observations[:8]
    if obs:
        lines = []
        for o in obs:
            payload = o.get("payload") or {}
            subj = payload.get("subject") or o.get("peer_id") or "?"
            kind = payload.get("kind") or payload.get("category") \
                or payload.get("type") or "observation"
            lines.append(f"{subj}: {kind}")
        evidence.append(Evidence(
            source="v4.collab.observations",
            claim=f"{len(obs)} shared observation(s): " + "; ".join(lines)))

    text = ("Here's what your team has shared: "
            + "; ".join(e.claim for e in evidence) + ".")
    return Answer(question.text, text, evidence, QuestionType.COLLAB,
                  confidence=0.85)


def style_provider(question: Question, conn) -> Optional[Answer]:
    """'why do you talk that way?' — the explainable adaptive identity.

    Wave 17 (MCU test #4): Friday can always say *why* she talks the way
    she does. Reads the relationship layer's stored tone-direction
    (operator's exact words + when) as evidence; without one, the
    honest answer is that tone adapts to relationship depth. Never
    fabricates — a missing relationship layer → None → "I don't know".
    """
    if question.type != QuestionType.STYLE:
        return None
    try:
        from ..relationship import RelationshipEngine
        engine = RelationshipEngine(conn)
        explanation = engine.explain_tone()
        direction = engine.direction()
        status = engine.status()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"relationship layer unavailable: {exc}")
        return None

    evidence: list[Evidence] = []
    if direction is not None:
        request = direction.request or "(tone direction)"
        when = direction.set_at
        claim = f"operator requested {direction.tone or 'different'} " \
                f"tone: {request!r}"
        evidence.append(Evidence(source="v4.relationships", claim=claim,
                                 when=when))
    depth = status.get("depth", 0.0)
    evidence.append(Evidence(
        source="v4.relationships",
        claim=f"relationship depth {depth:.2f} ({status.get('level', '?')})"))

    text = explanation
    return Answer(question.text, text, evidence, QuestionType.STYLE,
                  confidence=0.9)


def capability_provider(question: Question, conn) -> Optional[Answer]:
    """'what can you do?' — answered from the real capability registry.

    Wave 16 (Law 7): Friday knows what it can do. The registry merges
    built-in capabilities (executors, providers, intents, surfaces) with
    learned skills, so the answer is the truth about Friday's current
    abilities — never a hardcoded list. No registry → None → "I don't
    know yet" (honest).
    """
    if question.type != QuestionType.CAPABILITY:
        return None
    try:
        from ..capability import CapabilityRegistry, describe_capabilities
        summary = CapabilityRegistry(conn).summary()
        description = describe_capabilities(conn)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"capability registry unavailable: {exc}")
        return None
    if not summary.get("total"):
        return None

    by_layer = summary.get("by_layer") or {}
    parts = ", ".join(f"{k}: {v}" for k, v in sorted(by_layer.items()))
    evidence = [
        Evidence(source="v4.capabilities",
                 claim=f"{summary['total']} registered capability(ies) — {parts}")
    ]
    return Answer(question.text, description, evidence,
                  QuestionType.CAPABILITY, confidence=0.95)


def code_provider(question: Question, conn) -> Optional[Answer]:
    """'what's wrong with auth.py' — real IDE/static diagnostics (Wave 6).

    The ASK backstop for code questions: the target file is analyzed
    through the IDE layer (LSP when a server is available, the built-in
    AST analyzer always) and the findings are cited as evidence
    (``v4.ide.<method>``). A file with zero findings is real state
    ("no issues found"), like the mission provider's "no missions".
    No target file, or an unanalyzable path → None → the honest
    "I don't know yet". Never fabricates.
    """
    if question.type != QuestionType.CODE:
        return None
    target = (question.target or "").strip()
    if not target:
        return None
    try:
        from ..desktop.ide import analyze_file
        res = analyze_file(target)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"code provider analyze failed: {exc}")
        return None
    if res.method == "none":
        return None
    display = res.display_path
    method = res.method
    if not res.diagnostics:
        return Answer(
            question.text,
            f"I checked {display} — no issues found (via {method}).",
            [Evidence(source=f"v4.ide.{method}",
                      claim=f"no issues in {display}")],
            QuestionType.CODE, confidence=0.9)
    claims = [d.brief() for d in res.diagnostics[:3]]
    errors = res.error_count
    # "3 error(s)" only when every finding is an error; mixed findings
    # are "N issue(s) (1 error)" — never overstate severity.
    if errors and errors == res.issue_count:
        label = f"{res.issue_count} error(s)"
    elif errors:
        label = f"{res.issue_count} issue(s) ({errors} error)"
    else:
        label = f"{res.issue_count} issue(s)"
    evidence = [Evidence(
        source=f"v4.ide.{method}",
        claim=f"{label} in {display}: " + "; ".join(claims))]
    text = f"I found {label} in {display}: " + "; ".join(claims) + "."
    return Answer(question.text, text, evidence, QuestionType.CODE,
                  confidence=0.9)


def skills_provider(question: Question, conn) -> Optional[Answer]:
    """'what did you learn?' — skills Friday has formed (Wave 14).

    Reads the real ``skills`` registry (typed, shadow-first) so ASK
    answers cite what Friday actually learned — never a guess. The
    question must be a SKILLS question ("what have you learned", "show
    me your skills") and at least one skill must exist; otherwise None
    → the engine's honest "I don't know yet".
    """
    if question.type != QuestionType.SKILLS:
        return None
    try:
        from ..skills import SkillRegistry
        skills = SkillRegistry(conn).list(limit=100)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"skills layer unavailable: {exc}")
        return None
    if not skills:
        return None

    evidence: list[Evidence] = []
    for s in skills[:8]:
        claim = (f"'{s.name}' — {len(s.steps)} step(s), "
                 f"{s.verification_state}")
        evidence.append(Evidence(source="v4.skills", claim=claim,
                                 when=s.updated_at))
    text = ("Here's what I've learned: " + "; ".join(e.claim for e in evidence)
            + ".")
    return Answer(question.text, text, evidence, QuestionType.SKILLS,
                  confidence=0.9)


# ── Wave 13 — LLM synthesis (Law 6: enhances, never gates) ──────────

#: Values that mean "explicitly off" for ``FRIDAY_V4_LLM``.
_LLM_OFF = ("", "0", "false", "no", "off", "none")


def _llm_opted_in() -> bool:
    """Explicit opt-in via the ``FRIDAY_V4_LLM`` env var (truthy value)."""
    return os.environ.get("FRIDAY_V4_LLM", "").strip().lower() not in _LLM_OFF


def _clean_llm_text(text: str) -> str:
    """Trim fences/prose so only the answer text remains (never raises)."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text[:1200]


def llm_provider(question: Question, conn=None, best: Optional[Answer] = None,
                 history: Optional[list[dict]] = None,
                 llm: Optional[object] = None) -> Optional[Answer]:
    """Wave 13 — LLM synthesis over the deterministic best (never without it).

    Law 6 ("reasoning, not lookup"): deterministic providers are the
    floor; LLM synthesis is the ceiling. This provider takes the best
    deterministic answer (``best``) and asks the LLM to rewrite it as a
    natural, conversational synthesis — across the **SAME** evidence, so
    citations stay attached and nothing is fabricated.

    It is *optional* and *never gates*:

    - No explicit opt-in (``FRIDAY_V4_LLM`` env) → ``None``.
    - LLM client unavailable / network / parse failure → ``None``.
    - No evidence (``best`` unknown or evidence-less) → ``None`` — the
      honest "I don't know yet" is never sent to the LLM to invent.
    - The LLM output is attached to the ORIGINAL evidence list, so the
      judgment pass (``validate``) still guarantees evidence or silence.

    ``history`` (recent exchanges, oldest first) gives follow-up context
    ("and the tests?") — ``friday4 ask`` is conversation-capable. An
    injected ``llm`` (any object with ``.chat(messages, max_tokens=...)``
    and ``.available``) bypasses the env opt-in for tests and surfaces
    that construct their own client.
    """
    if best is None or not best.evidence:
        return None  # nothing to synthesize — "I don't know" stays real
    if llm is None:
        if not _llm_opted_in():
            return None
        try:
            from ..nlu import LLMClient
            llm = LLMClient()
        except Exception as exc:
            logger.debug(f"llm client unavailable: {exc}")
            return None
    try:
        available = getattr(llm, "available", True)
        if callable(available):
            available = available()
        if not available:
            return None
    except Exception:
        return None

    evidence_block = "\n".join(f"- {e.cite()}" for e in best.evidence)
    history_block = ""
    if history:
        turns = []
        for h in history[-8:]:
            role = "operator" if (h.get("role") == "user") else "friday"
            content = (h.get("content") or "").strip()[:200]
            if content:
                turns.append(f"{role}: {content}")
        if turns:
            history_block = "Recent conversation:\n" + "\n".join(turns) + "\n"

    user = (
        f"Question: {question.text}\n"
        f"{history_block}\n"
        f"Evidence:\n{evidence_block}\n\n"
        f"Draft answer to improve:\n{best.text}"
    )
    try:
        text = llm.chat([
            {"role": "system", "content": _llm_system_prompt()},
            {"role": "user", "content": user},
        ], max_tokens=600)
    except Exception as exc:
        logger.debug(f"llm synthesis failed: {exc}")
        return None
    cleaned = _clean_llm_text(text or "")
    if not cleaned:
        return None
    # The citations stay attached — the evidence list is unchanged, so
    # judgment's evidence-or-silence rule still holds for the enhanced
    # answer.
    return Answer(question=best.question, text=cleaned,
                  evidence=best.evidence, question_type=best.question_type,
                  confidence=best.confidence)


def _llm_system_prompt() -> str:
    return (
        "You are Friday, an AI operating partner. Rewrite the draft answer "
        "into a natural, conversational reply.\n"
        "Rules:\n"
        "- Use ONLY the evidence provided. Never invent facts, numbers, "
        "names, dates, or sources.\n"
        "- If the evidence does not answer the question, answer 'I don't "
        "know yet' — never guess.\n"
        "- Keep it concise and helpful (a few sentences).\n"
        "- Output only the reply text — no citations, no 'source:' "
        "prefixes, no markdown headers."
    )


#: Registry — the wave-9 "provider registry" (one concept per provider).
#: Deterministic providers are the floor; ``llm_provider`` is the Wave 13
#: optional ceiling applied over the best deterministic answer by the
#: engine (it is not registered here because it *enhances* the registry's
#: output rather than competing with it — no LLM → identical behavior).
PROVIDERS: tuple[Provider, ...] = (
    identity_provider,
    status_provider,
    activity_provider,
    mission_provider,
    memory_provider,
    conversation_provider,
    skills_provider,
    collab_provider,
    style_provider,
    capability_provider,
    code_provider,
)


__all__ = ["PROVIDERS", "Provider", "activity_provider",
           "capability_provider", "code_provider", "collab_provider",
           "conversation_provider", "identity_provider", "llm_provider",
           "memory_provider", "mission_provider", "skills_provider",
           "status_provider", "style_provider"]
