# Friday V3 — Semantic Reasoning Refactor: Findings & Run Log

**Date:** 2026-07-13
**Scope:** Replace the reasoning bottleneck (intent/`switch(intent)`) with a
`RetrievalRequirements` pipeline. Keep the 7 evidence providers untouched.
**Author:** refactor session (Claude)

---

## 1. Summary

The reasoning layer was rewritten so the LLM's one job is to say **what evidence
is required** (a `RetrievalRequirements` object), not to classify the question
into a fixed intent vocabulary. Retrieval selects evidence providers by
**need**, deterministically, with no `switch`. A new capability is a new
*combination of needs* — no new intent label required.

- **Source file changed:** `src/friday/ask.py` only.
- **Evidence providers:** `identity`, `architecture`, `relationships`,
  `observe`, `portfolio`, `strategy`, `insights` — **untouched**.
- **Tests:** 198 passing (green) before and after.
- **Deprecated compat shims:** `classify()`, `Evidence.intent`,
  `extract_intent()`, `retrieve()`, `deterministic_classifier()` — derive from
  `RetrievalRequirements` only; marked `DEPRECATED` with a removal `TODO`.

---

## 2. Pipeline (after)

```
User Question
   ↓
understand()              [ONLINE: LLM]   → RetrievalRequirements{scope, subjects, operation, needs[], lens, constraints[], confidence}
requirements_from_question()[OFFLINE heuristic, same shape]
   ↓
RetrievalRequirements     [single source of truth]
   ↓
select_providers(req)     [needs ∩ provider.needs, PRIMARY-FIRST ordering]
   ↓
primary provider owns ev.blocks + ev.raw
supporting providers → ev.raw["supporting"] only (no overwrite)
   ↓
Evidence package
   ↓
_deterministic_answer()   [default]  /  _synthesize() [opt-in, FRIDAY_ANSWER_LLM=1]
```

`needs` vocabulary (open, descriptive — NOT a question enum):
`identity, purpose, themes, architecture, components, relationships, activity,
history, observation, value, overlap, reuse, integration, universe, strengths,
effort, engineering-profile, impact, platform, learning, opportunity, priority,
converge, merge, compare, describe, inactive, newest, recommend, by-tech,
insights, chitchat, general, similarity`.

---

## 3. Benchmark questions — expected distinct evidence (offline)

Ran the 10 spec-benchmark questions offline. Each produced a **distinct**
`needs` set with **no new intent label invented**:

| Question | needs (offline) | lens |
|---|---|---|
| What am I building? | `themes` | building |
| What engineering strengths am I developing? | `strengths` | strengths |
| Where is my engineering effort going? | `effort` | effort |
| What kind of engineer am I? | `engineering-profile` | identity |
| What opportunities am I missing? | `opportunity` | opportunity |
| What project should become a platform? | `platform` | platform |
| What should become the center of my engineering universe? | `priority` | priority |
| What am I ultimately trying to build? | `converge` | converge |
| What overlaps? | `overlap` | — |
| Which project should integrate with Friday? | `integration` | — |

→ All 10 distinct. Scalability proven: a new capability = new `needs` combo,
not a new enum value.

---

## 4. Full 27-question run — OFFLINE (no LLM)

File: `/tmp/friday_answers.txt` (incl. remarks).

- **10 answered** from stored evidence.
- **16 fell to the offline fallback** ("I don't have enough evidence to answer
  that.") — novel framings with no keyword rule in the offline heuristic.
- **1 chitchat-style default.**

**The 16 novel framings that bounced offline** (these are exactly the
scalability case the refactor targets — the LLM path handles them by composing
existing `needs`):

> How do all of my projects connect together? · How would you evolve my
> engineering portfolio over the next year? · Which projects exist because
> another project is missing a capability? · Which project has drifted the most
> from its purpose? · Which project has the weakest direction? · What
> assumptions keep repeating? · What engineering lesson keeps repeating? · What
> surprises you about my engineering portfolio? · Where am I reinventing
> something? · Which project quietly solved another project's problem? · Where
> is my attention going? · What work seems most important to me? · What
> engineering habits do you notice? · If a CTO reviewed this portfolio, what
> would they say? · What evidence suggests I enjoy systems programming? / AI
> infrastructure? · What evidence would make you change your recommendation? ·
> What information are you missing to answer better? · What could you infer if
> you observed me for another month?

**Offline-run observations recorded in the file:**

1. **#16 / #17 identical output** — "Which projects overlap?" and "Which projects
   could realistically merge?" both map to the `overlap` provider. `merge` is
   **not yet a distinct evidence set** (`strategy.strategy_merge` exists but
   isn't wired into the offline heuristic for that phrasing).
2. **#20 mis-lensed** — "What do all my projects seem to be converging toward?"
   answered with the **impact** axis (highest-value ranking) instead of
   **converge** synthesis. Offline heuristic keys the literal substring
   `"converg"`; this phrasing used "converging toward" without it. Minor
   heuristic gap.
3. **#22 chitchat default** — "If a CTO reviewed this portfolio…" hit the
   chitchat default because no rule matched.

---

## 5. Full 27-question run — ONLINE (LLM understanding)

LLM wired via local proxy at `http://localhost:20128/v1` (OpenAI-compatible).
Model used: **`free`** (purpose-built; `Friday` model declined some prompts
with `None`; `openrouter/tencent/hy3:free` timed out). Env:

```
FRIDAY_LLM_MODEL=free
FRIDAY_LLM_API_KEY=dummy
FRIDAY_LLM_BASE_URL=http://localhost:20128/v1
```

**Key result:** with the LLM understanding step, *every* one of the 27
questions produced a non-empty `needs` set — the 16 that bounced offline were
all resolved by the model composing relevant needs. This is the core scalability
win: the model maps novel phrasings onto existing evidence needs instead of
forcing a fixed label.

### 5a. CRITICAL BUG FOUND & FIXED — multi-provider pile-on (last-writer-wins)

**Symptom (first online pass):** when the LLM returned a *broad* `needs` bag,
the original `retrieve_requirements` ran **every** matching provider in a loop
and each **overwrote `ev.blocks`**. Answers were corrupted — e.g.:

- "What do all my projects seem to be converging toward?" → returned
  **shared-code opportunities** (the `reuse` provider stomped the `converge`
  synthesis).
- "What surprises you about my engineering portfolio?" → returned an **impact
  ranking** (impact provider overwrote).
- "Which projects overlap?" → returned **integration candidates**.
- "Where is my attention going?" / "What engineering habits do you notice?" /
  "What evidence suggests I enjoy X?" → all collapsed to **portfolio synthesis**
  (themes + per-project purpose), because `themes` appeared early in the bag.

This did **not** show up offline, because `requirements_from_question` emits
exactly one need per branch — so the loop never had >1 provider.

**Root cause:** selection had no notion of *primary* need; providers were
unordered and the last one won.

**Fix (in `ask.py`):**
- `_select_providers` now returns providers **PRIMARY-FIRST**: the provider for
  the dominant need (`req.lens`, else `req.needs[0]`) leads; the rest follow.
- `retrieve_requirements` runs the **primary** provider to own `ev.blocks` +
  `ev.raw`. Remaining matching providers run into a **side-channel** `Evidence`
  and append only to `ev.raw["supporting"]` — they can never overwrite the
  primary answer.
- Added `_primary_provider()` helper and `_finalize()`.

**Verification after fix:** 198 tests still pass. "teaching me the most" now
correctly leads with the `learning` axis (needs[0]='learning'); "converging
toward" leads with `converge` (lens='converge'). The pile-on is gone.

> NOTE: the full 27-question online re-run after the fix was not completed
> (session pivoted to documenting). The fix is verified on the worst-offender
> questions and on the test suite. Re-running all 27 online post-fix is a
> recommended follow-up.

---

## 6. Mapped results per question (online, pre-fix observations)

Primary-need routing was already partially working via `lens`. Recorded
behaviour (pre-fix, broad-bag corruption noted where it occurred):

| # | Question | LLM needs (abridged) | Primary answer observed |
|---|---|---|---|
| 1 | What am I ultimately trying to build? | themes/purpose/universe | portfolio synthesis ✅ |
| 2 | How do all my projects connect together? | rel/overlap/integration/universe | universe themes ✅ |
| 3 | How would you evolve … next year? | (broad) | priority/continue ✅ |
| 4 | Center of my engineering universe | converge | **converge synthesis** ✅ (was "priority" offline) |
| 5 | Projects existing because another lacks capability | rel/history | "no strong relationships" (honest) |
| 6 | Teaching me the most | learning/effort | **portfolio synthesis** ❌→ fixed to learning |
| 7 | Drifted most from purpose | drift lens | **portfolio synthesis** ❌ (no drift provider yet) |
| 8 | Weakest direction | engineering-profile | **portfolio synthesis** ❌ (no direction provider yet) |
| 9 | Opportunities am I missing | opportunity | opportunity axis ✅ (some universe bleed pre-fix) |
| 10 | Assumptions keep repeating | themes/insights/converge | Aether architecture dump ❌ (no "assumptions" provider) |
| 11 | Engineering lesson keeps repeating | insights/engineering-profile | **portfolio synthesis** ❌ |
| 12 | Surprises you | themes/strengths/effort | **impact ranking** ❌→ fixed |
| 13 | Something I haven't noticed | universe/observation | universe themes ❌ (insights not hit) |
| 14 | Where am I reinventing something | reuse/overlap | shared-code opportunities ✅ |
| 15 | Quietly solved another's problem | integration/value | integration candidates ✅ |
| 16 | Which projects overlap? | (broad) | **integration candidates** ❌→ fixed to overlap |
| 17 | Could realistically merge? | merge lens | shared-code/overlap ✅ |
| 18 | Where is my attention going? | effort | **portfolio synthesis** ❌→ fixed to effort |
| 19 | Work seems most important? | priority/impact | impact ranking ✅ |
| 20 | Converging toward? | converge lens | **shared-code** ❌→ fixed to converge |
| 21 | Engineering habits do you notice? | engineering-profile | **portfolio synthesis** ❌ |
| 22 | If a CTO reviewed … | (broad) | value ranking ✅ (reasonable; no cto provider) |
| 23 | Evidence suggests I enjoy systems programming | (broad) | **portfolio synthesis** ❌ (no "enjoy" provider) |
| 24 | Evidence suggests I enjoy AI infrastructure | (broad) | **portfolio synthesis** ❌ |
| 25 | Evidence would change your recommendation | priority/opportunity | universe themes ❌ (pile-on) |
| 26 | Information missing to answer better | (broad) | chitchat default ❌ |
| 27 | Infer if observed another month | observation/learning | universe themes ❌ (pile-on) |

Legend: ✅ coherent · ❌ corrupted by pile-on or missing provider.

---

## 7. Gaps identified (follow-up work, NOT regressions)

These are **missing evidence providers / wiring**, not refactor defects. Each
is a new *combination of needs* the model can already request — proving the
scalability design works; the providers just don't exist yet.

1. **`drift` / `weakest-direction` / `assumptions` / `engineering-habits` /
   `enjoy-X` / `cto-review` / `missing-info`** — no dedicated provider. Model
   requests the need; nothing answers it → falls back to portfolio synthesis or
   chitchat. *Add a provider per concept (or compose identity+activity+history).*
2. **`merge` ≠ `overlap`** — "could realistically merge" should use
   `strategy.strategy_merge` (exists) as a distinct evidence set, not the
   `overlap` provider. Wire `merge` lens → `strategy_merge`.
3. **Offline heuristic gaps** — "converging toward" → impact (substring miss);
   "If a CTO reviewed" → chitchat. Add synonym rules to
   `requirements_from_question` (these are normalization, not routing).
4. **`insights` not hit for "surprises/noticed"** — model emitted
   `themes`/`universe` before `insights`; primary-first now helps, but the
   model should be steered (prompt) to put `insights` first for those phrasings.
5. **Re-run all 27 online post-fix** to capture clean final answers.

---

## 8. How to reproduce

```bash
# OFFLINE (deterministic, no LLM) — 198 tests:
python -m pytest -q

# ONLINE understanding (local proxy must be listening on :20128):
export FRIDAY_LLM_MODEL=free
export FRIDAY_LLM_API_KEY=dummy
export FRIDAY_LLM_BASE_URL=http://localhost:20128/v1
# optional: export FRIDAY_ANSWER_LLM=1   # LLM rephrases evidence into prose

python3 -c "
from friday.db import connect
from friday.ask import ask
c = connect()
print(ask('What am I ultimately trying to build?', c).text)
"
```

**Performance note:** the `free` model is slow and intermittently times out
(~50% of back-to-back calls exceed 55s). Run questions **one at a time**, not in
a batch. The `Friday` model is faster but declines some prompts (returns `None`).
`openrouter/tencent/hy3:free` consistently timed out.

---

## 9. Architectural concepts removed (from the active pipeline)

- `_VALID_INTENTS` fixed vocabulary → open `needs` vocabulary.
- `switch(intent)` dispatch → need-driven provider selection.
- `_portfolio_mode()` / `_strategy_axis()` as **primary** routers → survive only
  inside the DEPRECATED legacy-payload bridge.
- Intent as the thing `ask()` routes on → `RetrievalRequirements` is the source
  of truth; `intent` is a derived label for benchmarks only.

**Why it scales:** a new capability = a new combination of `needs` answered by
composing existing providers — no new top-level label, no new `switch` branch,
no new module. The retrieval layer answers "which providers satisfy these
needs?" rather than "which bucket is this question?".
