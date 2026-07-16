# Milestone 8.1.6 — Retrieval & Conversation Integration Sprint

## RETURN

### 1. Root causes found

**Part A — Chat follow-up context loss (the headline bug).**
`resolve_followup` keyed every follow-up off `ev.subject` (the single repo a
question was about) and only handled a narrow pattern set. Workspace/portfolio/
knowledge questions have `subject=None`, so meta-follow-ups
("How confident are you?", "What evidence supports that?", "Summarize it.",
"Explain further.") fell through to `None` → a FRESH retrieval of the bare
fragment ("why?", "confidence?") → the understanding step can't parse it →
empty evidence → "I don't have enough evidence" / "based on 0 of 8". Also:
"Compare that to Vivaha." couldn't anchor "that" (no subject) → clarify; and
trailing punctuation ("explain further.") defeated phrase matching.

**Part C — No adaptive coverage widening.** Workspace answers whose primary
provider under-fetched (e.g. "Which technologies am I consistently investing
in?" → GENERAL, 0/8) were never expanded to span the workspace.

**Part B/D — Knowledge not surfaced for broad questions.** The accumulated
Knowledge engine was only consulted for the KNOWLEDGE objective; portfolio-wide
questions ignored it.

**Part G — `knowledge explain 1` invalid.** IDs are timestamp strings; the
command list assumed sequential integers.

**Part H — No retrieval audit.** `--verbose` showed coverage but not which
providers were requested/returned.

**Pre-existing (out of scope, documented):** "Explain Friday" contamination —
`architecture.py` (frozen Observation layer) misattributes the environment's
`pip` packages (`/lib/python3.14/site-packages/pip`) as Friday's entry points.
This is the frozen architecture layer, not integration; left for M-observation.

### 2. Files modified
- `src/friday/ask.py` — rewrote `resolve_followup` (meta-follow-up handling +
  compare-with-no-subject → followup); added `("followup", prev)` result +
  `_answer_followup`/`_synthesize_followup`; added `_widen_evidence` +
  adaptive widening in `retrieve_requirements`; added `_COVERAGE_WIDEN_THRESHOLD`
  + `_WIDEN_OBJECTIVES`; added retrieval-audit capture; `_confidence_from_report`.
- `src/friday/cli.py` — `cmd_ask --verbose` renders the retrieval audit.
- `src/friday/cli_knowledge.py` — integer knowledge-ID alias via
  `resolve_knowledge_id` (extracted helper; timestamp IDs unchanged).
- `tests/test_m816_integration.py` — NEW (19 regression tests).

### 3. Why each change was required

- **`resolve_followup` rewrite (Part A):** the old resolver keyed off
  `ev.subject` (None for workspace questions) and lost context on meta
  follow-ups. New version detects meta-follow-ups (confidence/evidence/
  summarize/expand/compare) and returns `("followup", prev)` so `ask()`
  reuses the PREVIOUS Evidence package (it IS the evidence) and re-synthesizes
  — no fresh retrieval of the bare fragment. Trailing punctuation is stripped
  so "explain further." matches. Compare-with-no-subject becomes a follow-up
  that contrasts the prior evidence to the named project.
- **`_answer_followup` / `_synthesize_followup` (Part A):** synthesize the
  follow-up grounded ONLY in the previous Evidence (plus the named project's
  relationships/identity for a compare). Deterministic fallback restates the
  prior answer — context is never lost.
- **`_widen_evidence` + adaptive widening (Part B/C):** after assembly, a
  workspace/portfolio answer below 60% coverage (and in the descriptive
  objective whitelist) is widened ONCE with the accumulated Knowledge engine +
  portfolio identity + relationships, which span every repository. Re-measures
  coverage; never recurses. No unrelated evidence fetched.
- **Retrieval-audit capture (Part H):** records providers requested vs
  returned, knowledge_used, confidence on `ev.raw` so `ask --verbose` can show
  the audit without touching the normal answer.
- **Integer knowledge ID (Part G):** `resolve_knowledge_id` maps an integer to
  the Nth-newest item (sorted by created_at); timestamp IDs unchanged.
- **Knowledge priority (Part D):** satisfied by the widen step surfacing
  Knowledge for broad questions and by the pre-existing KNOWLEDGE objective
  priority.

### 4. New regression tests
19 tests in `tests/test_m816_integration.py` (Part I list).

### 5. Retrieval audit before/after

`ask --verbose` now prints a retrieval audit block, e.g. for "What am I building?":

```
Retrieval audit:
  Objective: themes
  Providers requested: _p_portfolio, _p_architecture
  Providers returned:  _p_portfolio
  Knowledge used:      no
  Confidence:          Medium
```

Before M8.1.6 there was no provider-requested/returned visibility at all.

### 6. Coverage improvement statistics

Original dogfood (REPORT.md, tencent/hy3 run) failures → M8.1.6 result:

| question | before | after |
|---|---|---|
| Which technologies am I consistently investing in? | GENERAL, 0/8, "based on 0 of 8" | PRIORITIZE, 6/8, synthesized |
| What am I building? | thin / narrow | THEMES, 8/8 |
| Which project should become a platform? | degraded refusal | MindWell, full recommendation |
| knowledge explain 1..5 | exit 2 (invalid) | exit 0 (integer alias) |

Workspace/portfolio questions now resolve to objectives whose primary provider
spans every repository (pct 1.0), so the adaptive widen is usually a no-op —
correct (no over-fetching). The widen mechanic itself is proven by
`test_widen_evidence_spans_workspace` (injects knowledge → spans Aether+Vivaha)
and `test_coverage_threshold_widens_narrow_workspace`.

### 7. Chat transcript proving follow-up continuity

Full transcript: `dogfood_run/43_chat.out`. Key results (the old run answered
every meta-follow-up with "I don't have enough evidence / based on 0 of 8"):

- "How confident are you?" → "Confidence: **Medium**." + grounded reasoning. ✓
- "What evidence supports that?" → lists 5 specific evidence facts. ✓
- "Compare that to Vivaha." → answered as a follow-up (synthesizes comparison). ✓
- "What knowledge is newest?" → "Friday V3 is newest, first commit 2026-07-12." ✓
- "What knowledge is oldest?" / "What changed?" / "Explain further." → answer
  from the previous Evidence package, honestly stating gaps (no context loss,
  no hallucination). ✓

No follow-up in the sequence produced the old "0 of 8" failure.

### 8. Confirmation: frozen layers unchanged
Brain (objective.py): unchanged. Observation: unchanged. Context: unchanged.
Knowledge engine: unchanged. RetrievalRequirements: unchanged. Engineering
Judgment (objective.py judge): unchanged. Only integration (ask.py follow-up +
widening, cli_knowledge alias, verbose audit) changed.

### 9. Updated test count
528 → 547 (net +19 new in tests/test_m816_integration.py; 0 regressions —
full suite `pytest tests/` passes green).

### Known limitations (documented, not fixed — out of sprint scope)
- **"Explain Friday" pip contamination**: `architecture.py` (frozen Observation
  layer) misattributes the environment's `pip` packages
  (`/lib/python3.14/site-packages/pip`) as Friday's entry points, producing a
  60+ line dump of pip `main()` functions. Root cause is in the frozen
  architecture scanner, not the integration layer. Left for M-observation.
- **"Which projects should merge?" LLM synthesis conservative**: the
  deterministic answer ("Don't merge Aether by default — earn it") is correct;
  the LLM synthesis on the strategy provider's 1/8 coverage chose to decline
  rather than summarize. Safe behavior; evidence package does include
  relationships + themes (Part F multi-source satisfied at retrieval level).
- **"Why?" / "What changed?" safe refusals**: the Evidence is a static snapshot
  with no stated motivations / no observation history, so the LLM honestly says
  it can't answer — correct per "Do NOT increase hallucination."
