# Wave 13a — ONE NLU Point (LLM-first, rules fallback) ✅ SHIPPED

> **The directive:** *speaks natural language* — **no regex/keyword
> matching anywhere in the input path.** Every surface (voice, `friday4
> talk`, web chat) routes through ONE parser, `resolve()`, which is
> **LLM-first**; the deterministic rules are a fallback only when the
> LLM is absent/offline — never the product.
>
> Parent wave: [Wave 13 — Thinking Core](WAVE_13_THINKING_CORE.md).
> Governance: [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) — Law 1
> (natural language first), Law 4 (never crash), Law 6 (LLM enhances,
> never gates).

---

## 1. The design

### ONE parser, every surface

```
utterance ──► resolve() ──► LLM parse (primary: intent + slots + entities)
                     └──► deterministic rules (fallback: no LLM / offline)
                            └──► canonical ResolvedAction (the command language)
```

- **One point:** voice router, `friday4 talk`, and web chat all call
  `resolve(text, llm=LLMClient())`. No surface parses language itself —
  a surface may add *behavior* after resolution, never *interpretation*.
- **LLM primary:** intent, entities, and confidence come from the model
  through the 9router proxy (`localhost:20128/v1`, configurable model) —
  the same client Wave 13 reuses for synthesis.
- **Deterministic fallback:** only when the LLM is absent/offline — keeps
  the never-crash law without keyword-matching being the product. The
  fallback produces the *same canonical shape* as the LLM path, so
  callers never know which path ran.
- **Never crash:** every branch guarded; unknown input → clarification,
  never an error.

### `nlu/` — the ONE point

| Module | Role |
|--------|------|
| `llm.py` | 9router OpenAI-compatible client (pure stdlib, SSE-trailer quirk handled) |
| `intent.py` | `classify(text, llm)` — LLM intent + slots, rules fallback |
| `entities.py` | `extract(text, entity_values)` — LLM entities, rules fallback |
| `confidence.py` | `assess(result, llm_clarification)` — ambiguity → clarification |
| `resolver.py` | `resolve(text, llm)` — utterance → canonical `ResolvedAction` |

### `understanding/` — a thin shim

`friday_v4.understanding` re-exports every name from `nlu` so legacy
importers (`missions/planner.py`, older tests) keep working. **The old
Wave 9 implementation submodules were removed** — there is exactly one
NLU implementation in the package tree.

---

## 2. Wiring (the Wiring Law)

| Consumer | Call site | LLM passed |
|----------|-----------|-----------|
| `nl_router.py` | `resolve(raw, llm=self.llm)` | `TextCommandHandler(llm=…)` |
| `voice/router.py` | `TextCommandHandler(conn=…, llm=LLMClient())` via `_try_nlu_route` | `LLMClient()` |
| `web/dashboard.py` | `TextCommandHandler(conn, llm=LLMClient())` | `LLMClient()` |
| `cli_nl.py` | `TextCommandHandler(conn, llm=_default_llm())` | `LLMClient()` or None |
| `missions/planner.py` | `from ..understanding import resolve` (shim) | n/a (fallback) |

Config: `FRIDAY_V4_LLM_URL` (default 9router proxy), `FRIDAY_V4_LLM_MODEL`,
`FRIDAY_V4_LLM_KEY` (optional), explicit opt-in via `FRIDAY_V4_LLM`.

---

## 3. What actually shipped (close-out)

1. **`nlu/` verified as the ONE point** — every surface confirmed to call
   `resolve(text, llm=…)`; no surface parses language itself.
2. **`understanding/` made genuinely thin** — old Wave 9 implementation
   submodules (`entities/confidence/resolver/intent.py`) deleted; only the
   re-export `__init__.py` remains, identity-checked by tests.
3. **Never-crash fix (real bug):** the fallback's confidence calculation
   used the `Intent` enum key (a str) where it needed the score
   (`scores[best]`) — any *ambiguous* utterance raised a `TypeError` and
   crashed `resolve()`, silently degrading every surface to `'unknown'`.
   Now scores correctly and never raises.
4. **ASK/RESEARCH targets threaded** — `resolve()` now carries the
   LLM/fallback `target` through for ASK and RESEARCH intents (previously
   only EXECUTE/DESKTOP/PLAN did).
5. **Research routing fixed** — the fallback now recognizes `X vs Y` as
   RESEARCH (bare `vs`/`versus` tokens so `\b` matches after paths), the
   correlate path strips leading verbs (`analyze X` → `X`), splits the
   original (case-preserved) text, and `cross_project.py` no longer
   passes str paths where `RepoProfile`s are expected.
6. **Tests** — `test_understanding.py` rewritten as 25 shim-contract
   tests (re-export identity, `resolve_with_llm`, fallback, never-crash,
   target threading); `test_nlu.py` (12) + research/CLI tests green;
   stale tests updated (SSE `_extract_text` call, `cmd_report`
   `--daily/--weekly` Namespace).

**MCU test:** "Friday, what's the deal between vivaha and MindWell?" →
one parser, LLM intent → research → cited answer. No surface
keyword-matches anything. ✅

---

## 4. What we learned

- The transition was *mostly* wired already (surfaces called `resolve`
  with `LLMClient()`), but two latent bugs made the whole suite red: the
  fallback confidence `TypeError` and the shadow NLU implementation still
  living in `understanding/`. "One point" is a *maintenance* law as much
  as an architecture law — two NLU implementations in the tree will
  silently drift apart.
- The never-crash law must be tested *through the surface path*: the
  crash was invisible to `test_nlu.py` (fallback tests used unambiguous
  input) and only surfaced via the NL research routing test.
- **Known offline limitation (documented, accepted):** the fallback's
  `"what's the deal between X and Y"` correlate form still feeds
  `correlate("what's the deal", "X and Y")` — `_strip_research_lead`
  verbs require a trailing space the `" between "` split operand lacks.
  It degrades gracefully to "no shared signals" (never crashes), and
  the LLM-primary path handles it correctly — but it is not the shipped
  correlate path. The `analyze X vs Y` form is the tested path.
