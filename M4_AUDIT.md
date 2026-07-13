# Friday V3 — Engineering Knowledge Gap Audit (Pre-M4)

**Dogfooding milestone. Deliverable = understanding, not code.**

Grounded in the *real* KB state (inspected 2026-07-13) and the actual Haiku
test run where 4 of 5 hard questions failed.

---

## Step 1 — What Friday currently stores (and what that can answer)

| Category | Stored? | Answers | Fundamentally CANNOT answer |
|---|---|---|---|
| Git metadata (commits, dates, dirty, license, author) | ✓ 7 repos | inactive / newest / most-active / majority-commits / blockers | effort *over time*, intent |
| README summary (Purpose/Maturity/Value/Roadmap) | **4/7** (missing: Friday V2, Friday V3, finance-tracker) | "what is X", coarse themes, identity | anything needing purpose of the 3 missing repos |
| Technologies (detected) | ✓ 16 rows | by-tech, tech-themes, overlap(persistence) | problem domain, decisions |
| Architecture (label+evidence+confidence) | ✓ 7/7 | overlap, merge-candidates, themes-via-arch | *why* it is that way, evolution |
| Components (Weak concepts) | ✓ 22 rows | architecture deep-dive | responsibility, problem solved |
| Entry points | ✓ 131 rows | startup/architecture | — |
| Relationships (52 rows: 41 Weak / 11 Medium / 0 Strong) | ✓ | "how related" (Medium only), merge-candidates | intent, evolution |
| Derived at read-time (themes, value, overlap, integration, universe) | ✓ | "what am I building", "most valuable", "merge/integrate" | tension, drift, lessons |

**Key structural fact:** the store is a *single static snapshot per repo*.
There is **no temporal dimension** anywhere — no original-purpose, no history,
no evolution, no effort time-series. Every "over time" question is therefore
unanswerable by construction, not by reasoning quality.

---

## Step 2 — Real questions, mapped to evidence

1. **What am I building?** → EXISTS. Themes from purpose(4/7)+tech+arch. ✓ *working*
2. **Tension: stated intent vs actual effort?** → commit share (vivaha 43%) +
   purpose(4/7) exist. **Missing:** *stated* goals/roadmap parsed & compared;
   purpose for 3 repos. Not derivable (no stated-intent table). Needs future
   observation + historical effort series. ✗ *Haiku failed*
3. **Which project is most valuable?** → EXISTS (value ranking). ✓ *working*
4. **Which should eventually merge?** → Medium rels exist (MindWell–vivaha;
   Friday–V3–finance-tracker cluster via shared-tech/architecture). **Missing:**
   responsibility overlap (needs purpose all 7). Mostly answerable now. ✓ *partial*
5. **Which quietly solved another's problem?** → arch labels + purpose(4/7) exist.
   **Missing:** explicit problem-statements per project + cross-project
   problem-domain map; purpose for 3 repos. Not derivable from static store.
   ✗ *Haiku failed*
6. **What did Friday V2 teach V3?** → arch(Library vs CLI)+components+tech exist.
   **Missing:** *evolution* — what changed/why/lessons. No temporal store.
   Requires historical (commit logs, migration notes). ✗ *Haiku failed*
7. **Engineering lessons repeating?** → Missing: decisions/rationale/abandoned
   approaches. None stored. Requires observation + history. ✗
8. **Assumptions changed over time?** → Missing: temporal intent snapshots /
   decision records. Requires historical. ✗
9. **Which drifted from original purpose?** → current purpose(4/7) exists.
   **Missing:** *original* purpose (historical). Requires historical. ✗

**Finding:** 3 of 9 are fully working (1,3,4-partial). 6 require knowledge the
store structurally lacks. Of those 6, **five trace to two root gaps**:
(a) *purpose missing for 3 repos*, (b) *no temporal/historical dimension*.

---

## Step 3 — Missing knowledge, categorized (only what failures justify)

- **A. Purpose Completeness** — 3 repos lack purpose/problem. Prerequisite for
  1(fine), 4, 5. *Not new schema — an ingest gap.*
- **B. Stated Intent** (goals / roadmap / commitments) — needed for tension (2).
  Partially extractable from README Roadmap lines already stored-but-unused.
- **C. Project Evolution / History** — original purpose, version-to-version
  change, decisions, abandoned approaches, assumption changes, lessons,
  effort-over-time. Needed for 6,7,8,9,2(partial). *Not derivable from a static
  snapshot; requires git history + periodic re-ingest diffing.*
- **D. Engineering Decisions & Rationale** — why-X / abandoned / repeated
  lessons. Needed for 7,8. *Mostly requires future observation (user input /
  ADRs / issue tracks); least derivable.* → **defer from M4 core.**

(Rejected as unjustified: "blocked work" already partially covered by
`is_dirty`/stale; "business context" partially via README Value line.)

---

## Step 4 — Smallest possible extension (BUILD MODE gate)

| Category | Derive? | Infer? | From repo? | From README? | From git? | Verdict |
|---|---|---|---|---|---|---|
| A Purpose Completeness | yes (summarize) | — | yes | yes | — | **Fix ingest** (no schema) |
| B Stated Intent | parse Roadmap | partial | — | yes (Roadmap) | — | **Store parsed goals** (small col) |
| C Evolution/History | no | no | no | README diff | **yes** (commits/tags) | **Snapshot history** (append-only) |
| D Decisions/Rationale | no | no | no | ADRs? | commit msgs? | **Defer** (needs observation) |

`git log` confirms history *is* available locally (vivaha 184 commits back to
2026-07-05; V2/V3/Aether present) — so C is extractable, not invented.

---

## Step 5 — Evidence Matrix

| Question | Evidence needed | Exists? | Derivable? | New knowledge? | Confidence |
|---|---|---|---|---|---|
| 1 What am I building | purpose+tech+arch | ✓ | yes | no | High (works) |
| 2 Tension intent vs effort | stated goals + effort series | ✗ | no | B + history | Low→Med |
| 3 Most valuable | purpose/biz/activity/rels | ✓ | yes | no | High (works) |
| 4 Merge candidates | shared arch/framework/responsibility | ~ | partial | A completes it | Med (works partial) |
| 5 Quietly solved another's problem | problem-stmt per project | ✗ | no | A + problem-map | Low |
| 6 V2 taught V3 | evolution diff | ✗ | no | C (history) | Low |
| 7 Lessons repeating | decisions/rationale | ✗ | no | C/D | Low |
| 8 Assumptions changed | temporal intent | ✗ | no | C (history) | Low |
| 9 Drifted from purpose | original vs current purpose | ✗ | no | C (history) | Low |

The matrix shows the cliff clearly: questions 1/3/4 sit above the evidence line;
2/5/6/7/8/9 sit below it because of **A** (purpose gap) and **C** (no history).

---

## Step 6 — Recommended Milestone 4 (BUILD MODE)

Three capabilities, each passing the 4-question gate. No embeddings, agents,
graph DB, planners, or new frameworks. SQL + git only.

### M4-A — Close the purpose gap (highest leverage, zero schema)
- **User-visible:** "what am I building / which quietly solved another's problem"
  work for *all 7* repos, not 4.
- **Benchmark:** re-run Haiku battery Q4/Q5 → answers, not "no evidence".
- **Why existing can't:** Friday V2/V3/finance-tracker have empty `readme_summary`.
- **Smallest impl:** ensure `ingest` summarizes every README (already has the
  LLM path; make missing-summary a hard retry, not silent skip). **No new table.**

### M4-B — Stated Intent field
- **User-visible:** tension question (2) becomes answerable: "you said X, your
  commits show Y".
- **Benchmark:** "Where is the tension between what I say I'm building and where
  my effort goes?" → contrasts stated goals vs commit share.
- **Why existing can't:** Roadmap lines are stored in summary text but never
  parsed/compared; no "stated intent" concept.
- **Smallest impl:** parse `Roadmap:`/`Goals:` from existing summary into a
  `stated_intent` column on `repositories`; surface in tension + portfolio.

### M4-C — Purpose/maturity snapshot history (the temporal dimension)
- **User-visible:** drift (9), evolution (6), assumptions-changed (8),
  lessons-repeating (7).
- **Benchmark:** "What did Friday V2 teach Friday V3?" and "Which project
  drifted from its original purpose?" → evidence-backed, not "no evidence".
- **Why existing can't:** store is one static row per repo; no original purpose,
  no diff, no time.
- **Smallest impl:** append-only `purpose_history(repo_id, ingested_at, purpose,
  maturity, architecture)` written on every ingest; derive drift/evolution by
  comparing first vs latest snapshot + git first-commit README. **No query-time
  LLM; diff is deterministic.**

### Rejected for M4
- **D (decisions/rationale/lessons capture):** fails BUILD MODE — not derivable
  from repo/git/readme; needs observation/ADRs. Defer to a later, observation-
  based milestone.
- Any graph DB / vector / planner / agent proposal: forbidden by brief.

**Net:** M4 = 1 ingest fix + 1 column + 1 append-only table. That single
temporal table (M4-C) is the unlock for the entire "over time" question class
that the static store structurally forbids today.
