# Friday V4 — Wave 10: Memory & Identity (Friday Knows You)

> **Parent:** PLAN.md · **Sibling docs:** WAVE_9_AGENCY_CORE.md,
> WAVE_11_RESEARCH_REFLECTION.md
> **Context:** Solo developer, hygiene-first. Wave 9 gives Friday a brain;
> Wave 10 gives it a *relationship*.
> **Estimate:** 5–7 weeks solo.
> **Status:** ✅ SHIPPED (all 16 wiring gaps in [§8 Wiring Status](#8-wiring-status-per-the-wiring-law---see-architecturedoc) closed)

---

## 1. Why This Wave Exists

VISION.md makes the most powerful promise in the whole project:

> 🎤 "Friday, who am I?"
> 🎧 "You're Lakshay. You prefer Rust for performance-critical code,
>    Python for tooling. You're not a morning person, so I kept the
>    briefing short."

Today, that promise is **absent from V4's code**. The V3 boundary law was
right to cut V3's imports — but it also cut V3's *personality stack*
(`persona/engine.py`, `relationship.py`, `memory.py`, `conversation_learner.py`,
`skill_formation.py`), all of which V4 never rebuilt.

Wave 10 rebuilds identity, memory, relationship, and self-improvement —
**V4-native, stdlib-first, hermetic-tested**. This is the wave that makes
Friday feel like a partner instead of a tool.

---

## 2. The Memory Stack

```
                ┌─────────────────────────────────────┐
                │             SURFACES                │
                │  Voice · CLI · Web · proactive      │
                └──────────────────┬──────────────────┘
                                   │
                ┌──────────────────▼──────────────────┐
                │        persona/ (identity)          │
                │  name · preferences · tone          │
                └──────┬──────────────────┬───────────┘
                       │                  │
        ┌──────────────▼──────┐   ┌──────▼───────────────┐
        │     memory/         │   │  relationship/        │
        │  facts + working    │   │  depth → tone/verbosity│
        └──────────────┬──────┘   └──────────────────────┘
                       │
        ┌──────────────▼──────────────────────────────────┐
        │          db.py  (V4 state, sqlite)              │
        │  memories · facts · relationships · sessions    │
        └─────────────────────────────────────────────────┘
```

---

## 3. Deliverables

### 3.1 Memory Engine (`friday_v4/memory/`)

| File | Purpose |
|------|---------|
| `facts.py` | long-term memory: about you, your projects, your preferences (with decay) |
| `working.py` | session working memory / current context (V3 `WorkingMemory` concept) |
| `store.py` | typed DB access for memories + facts + decay sweeps |

**Design reference:** V3 `memory.py` (`MemoryEngine`: store/recall/forget/
decay_memories/extract_from_conversation; `WorkingMemory`: set_context/
evict/get_contexts_by_category).

**Key Rules:**
- Every memory fact carries: **source** (who said it / what event), **confidence**,
  **created/updated timestamps**, **decay policy**.
- Facts are *propositions with provenance*, never raw strings:
  `("operator", "prefers_python_for_tooling", confidence=0.9, source="voice:2026-08-01")`.
- `decay_memories` runs on a schedule — unused facts fade, confirmed facts strengthen.
- Nothing is written to `~/.friday` in tests (hermetic via `tmp_path`).

**MCU Friday Feel:**
```
🎤 [You]: "I prefer Rust for performance-critical code."
🎧 [Friday]: "Noted — storing that. I'll keep briefings shorter in the
             morning going forward."
```

### 3.2 Persona & Identity (`friday_v4/persona/`)

| File | Purpose |
|------|---------|
| `engine.py` | `IdentityEngine`: name learning, preference extraction, identity profile |
| `learn.py` | extract preferences from conversation ("call me Lakshay", "I prefer X") |
| `prompts.py` | persona prompt assembly (tone, history, relationship depth) |

> **Status (2026-08): built + wired, operator-amended (no keywords).**
> `persona/` ships (`engine.py`, `learn.py`, `prompts.py`) as a **verbatim
> view over the conversation log** — per operator direction there are *no
> keyword tables and no regex extraction* (the draft `Intent.PERSONA`
> classifier and `extract_name`/`extract_preference` extractors were
> removed in review). `nl_router.handle()` records every utterance
> word-for-word into the `exchanges` table (`_log_exchange`);
> `reasoning/providers.py` `identity_provider` answers "who am I" by
> quoting those statements back (`v4.exchanges` evidence) while "who are
> you" stays Friday's self-knowledge (`v4.self`); `friday4 persona
> profile/remember` lands the §4 CLI (see §8 rows 7–9).

**Design reference:** V3 `persona/engine.py` + `persona/prompts.py` +
`conversation_learner.py` (`_has_explicit_preference`, `_persist_extraction`).

**Key Rules:**
- Name/preference learning is **explicit-consent-first**: only extracts when
  the operator states a preference ("call me…", "I prefer…", "I like…").
- Identity profile is a *view over facts*, never a separate hidden store.
- Tone adapts via relationship depth (see 3.3) — not hardcoded.

**MCU Friday Feel:**
```
🎤 [You]: "Call me Lakshay, by the way."
🎧 [Friday]: "Got it — Lakshay. Anything else you want me to remember?"
```

### 3.3 Relationship (`friday_v4/relationship/`)

| File | Purpose |
|------|---------|
| `depth.py` | compute relationship depth from interaction history (V3 `operator/depth.py` concept) |
| `tones.py` | tone + verbosity selection by depth (morning brevity, afternoon detail) |

**Design reference:** V3 `operator/depth.py` (`compute_relationship_depth`),
`relationship.py`, `sentiment.py` (rolling sentiment).

**Key Rules:**
- Depth is computed from *real interaction data*: frequency, sentiment trend,
  preference confirmations, mission completions.
- Tone changes are gradual and explainable — never a sudden personality shift.

**MCU Friday Feel:**
```
🎤 [Friday]: "You're not a morning person — I kept the briefing to 3 lines.
             Want the full walkthrough after coffee?"
```

### 3.4 Skills & Self-Improvement (`friday_v4/skills/`)

V3's crown jewel was `skill_formation.py` (1,731 LOC): watch a workflow, form
a skill, verify, promote. V4 rebuilds it cleanly.

| File | Purpose |
|------|---------|
| `replay.py` | `ReplayExecutor`: learn from a demonstrated sequence (V3 concept) |
| `shadow.py` | `ShadowExecutor`: run in shadow mode, promote only when verified |
| `registry.py` | skill confidence, verification state, versioning |
| `dispatch.py` | auto-dispatch a skill when context matches |

**Key Rules:**
- Skills are stored as **parameterized step sequences** with evidence
  (the observation/action trail that produced them).
- **Shadow-first:** new skills run in shadow mode and record what they *would*
  do; promotion requires N successful shadow matches + operator approval.
- Registry tracks confidence, last-verified, failure count — a skill that
  starts failing is demoted, not silently kept.

**MCU Friday Feel:**
```
🎧 [Friday]: "I noticed you run pytest after editing tests every time.
             I've formed a skill for it — currently in shadow mode.
             Want me to auto-run it next time you finish a test file?"
```

---

## 4. File Count & CLI

- **~18 source files** + **~10 test files** across 4 new packages
  (`memory/`, `persona/`, `relationship/`, `skills/`).
- CLI wiring: `friday4 memory store/recall/forget`, `friday4 persona profile`,
  `friday4 relationship status`, `friday4 skills list/learn/promote/shadow`.

---

## 5. Test Strategy

- **memory/:** store/recall round-trip, decay schedule, provenance integrity,
  hermetic (no real `~/.friday`).
- **persona/:** explicit-consent extraction (only learns when operator states
  preference), no false extraction from casual phrasing.
- **relationship/:** depth monotonicity (more interaction → deeper, never
  suddenly shallower), tone mapping bounds.
- **skills/:** shadow-mode never executes real actions; promotion requires
  verification; failure demotion.

Target: **+90 tests** (V4 suite → ~600).

---

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Memory bloat / stale facts | Medium | Medium | Decay + confidence + provenance |
| Over-eager preference extraction | Medium | Medium | Explicit-consent-first rules |
| Skill auto-dispatch acts unsafely | Low | High | Shadow-first + operator approval |
| Relationship feels fake | Medium | Low | Depth driven by real data, gradual tone shifts |

---

## 7. Success Criteria

1. "Call me Lakshay" → remembered across restarts, used in tone. ✅
2. "I prefer Rust for perf-critical code" → stored fact with provenance, surfaced when relevant. ✅
3. Relationship depth rises with real interaction; briefing tone adapts. ✅ (`relationship/` + `RelationshipRefresher` + tone wiring — depth is monotonic, never suddenly shallower)
4. A skill formed in shadow mode is promoted only after verification + approval. ✅ (`skills/` shadow-first lifecycle + `SkillLearner` daemon sweep + `friday4 skills promote` as the explicit approval step)
5. Zero V3 imports (except `v3source.py`), hermetic tests, ~600 total. ✅

---

## 8. Wiring Status (per the Wiring Law — see ARCHITECTURE.md)

> **Wiring Law:** a layer is not done until it is wired into every consumer
> that should reach it (entry points, daemon schedules, reasoning
> providers, CLI, briefings) — in the same change, never as a follow-up.
> The `memory/` build shipped without its wiring; the table below is the
> backlog that must land before the memory layer counts as done.

| # | Gap | Where | Status |
|---|-----|-------|--------|
| 1 | `memory_provider` reads the raw `memories` table instead of the typed `FactMemory`/`MemoryStore` layer (decay + provenance-aware recall unused) | `reasoning/providers.py` | ✅ closed — typed `FactMemory` + target-scoped recall |
| 2 | No daemon decay sweep — `MemoryStore.decay()` never runs on a schedule | `daemon.py` | ✅ closed — `MemorySweeper` component (`memory_sweep`/`memory_interval` config) |
| 3 | No `friday4 memory store/recall/forget` CLI (§4 promised it) | `cli_memory.py` (missing) | ✅ closed — `store/recall/forget/list/status` |
| 4 | `VoiceRouter._try_nlu_route` drops ASK intents (action=`"chat"`) → spoken questions never reach reasoning | `voice/router.py` | ✅ closed — ASK intents return the reasoning answer |
| 5 | `WorkingMemory.current_context()` built, but no consumer renders it | briefing/status surfaces | ✅ closed — `friday4 proactive brief` + `friday4 memory status` |
| 6 | Web dashboard has no memory-facts card | `web/server.py` | ✅ closed — `memory_state()` + `/api/memory` + Memory card |
| 7 | "call me X" / "I prefer Y" had no identity learning path | `nl_router.py` + conversation log | ✅ closed (operator-directed) — **no keywords**: every utterance flows into the `exchanges` log via `_log_exchange`; the brain learns from what you *actually said* |
| 8 | Reasoning identity answers were static self-knowledge only (`v4.self`) | `reasoning/providers.py` | ✅ closed — `identity_provider` answers "who am I" by quoting your own words back (`v4.exchanges` via `IdentityEngine`) |
| 9 | No `friday4 persona` CLI (§4 promised `profile`) | `cli_persona.py` (missing) | ✅ closed — `profile` / `remember` (verbatim view, fixed standalone `main`) |
| 10 | Relationship depth was never computed from real data (§3.3) | `relationship/` (missing) | ✅ closed — `depth.py` (`RelationshipEngine`: signals → monotonic depth → persisted) + `tones.py` (tone/verbosity/briefing by depth, morning brevity) |
| 11 | No `friday4 relationship` CLI (§4 promised `status`) | `cli_relationship.py` (missing) | ✅ closed — `status` / `refresh` (depth, level, tone, verbosity, signals) |
| 12 | Skills/ self-improvement never rebuilt (§3.4) | `skills/` (missing) | ✅ closed — `registry.py` (shadow→verified→promoted, failure demotion) + `replay.py` (learn from audit log) + `shadow.py` (shadow-first, never executes) + `dispatch.py` (suggest on context match) |
| 13 | No `friday4 skills` CLI (§4 promised `list/learn/promote/shadow`) | `cli_skills.py` (missing) | ✅ closed — `list` / `learn` / `shadow` / `promote` / `status` |
| 14 | No daemon schedule runs skills/shadow or refreshes depth | `daemon.py` | ✅ closed — `SkillLearner` + `RelationshipRefresher` components (`skill_learn`/`skill_interval`/`relationship_refresh`/`relationship_interval` config) |
| 15 | Web dashboard shows no relationship/skills state | `web/dashboard.py` + `web/server.py` | ✅ closed — `relationship_state()` + `skills_state()` accessors, Relationship + Skills cards, `/api/relationship` + `/api/skills` |
| 16 | Persona tone was hardcoded `"default"` (§3.3: tone adapts via depth) | `persona/engine.py` + `persona/prompts.py` | ✅ closed — `IdentityEngine.profile()` tone comes from `RelationshipEngine` (never hardcoded); context block renders the tone line |

---

*This is the wave where Friday stops being a tool you type at and becomes a
partner you speak with — the emotional core of the MCU vision.*
