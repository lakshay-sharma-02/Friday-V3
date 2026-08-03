# Waves 16 & 17 — Capability Registry + Adaptive Identity ✅ SHIPPED (2026-08)

> **The directive:** *no task it can't pick up* and *adapts its
> personality to you* — the two clauses that were still open after
> Wave 14. Shipped together because they share one NLU point and one
> wiring surface.
>
> Governance: [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) — Law 7
> (self-extension: every capability registered), Law 8 (adaptive
> identity, data-driven never hardcoded), Law 1 (NL path in the same
> change).

---

## Wave 16 — Capability Registry (Law 7)

### The design

Friday knows what it can do. A registry holds every capability —
executors, reasoning providers, NL intents, surfaces, **and learned
skills** — with the natural-language intents that reach each one.
"what can you do" is answered from the registry, never a hardcoded
string; learning a skill registers a new capability (Law 2 + Law 7
meet — Friday grows abilities without code).

```
capability/
  registry.py  — Capability model + CapabilityRegistry
                 (list/by_intent/by_layer/search/describe/summary)
  builtins.py  — executors (shell/git/file/python/testing/ssh) +
                 providers (question types) + intents (the ONE NLU
                 point) + surfaces (talk/voice/web/desktop/…)
```

### Wiring (the Wiring Law)

| Consumer | Status | How |
|---|---|---|
| `reasoning/providers.py` | ✅ | `capability_provider` for `QuestionType.CAPABILITY` — "what can you do" cites `v4.capabilities` |
| `nl_router.py` HELP | ✅ | "what can you do" / "help" answered from the registry (no hardcoded list) |
| `friday4 capability list/describe/count` | ✅ | `cli_capability.py` debug hatch |
| Web dashboard | ✅ | `/api/capability` + Capabilities card (total, layers, learned skills) |
| Self-extension | ✅ | `list()` merges promoted/verified skills as `skill:<name>` capabilities |

### MCU test

"Friday, what can you do?" → a real, evidence-cited list of Friday's
actual registered abilities — including skills it has learned. ✅

---

## Wave 17 — Adaptive Identity (MCU test #4)

### The design

**"Be more casual, Tony."** An explicit tone-direction persists across
restarts, applies on every surface, and is explainable — Friday can
always say *why* she talks the way she does. Direction wins over the
depth-derived default; depth keeps computing underneath (the two never
collide). Consent-first: only an explicit direction is stored, verbatim
with its request words.

```
"be more casual" ──► Intent.STYLE ──► relationship.set_direction(tone=casual)
                          │                └─► relationships.preferences.tone_direction
                          ▼                     (tone, verbosity, request, set_at)
"why do you talk that way?" ──► ASK ──► style_provider ──► "I talk casual because
                        you asked me to on <date>: 'be more casual'"
"be yourself again" ──► STYLE(reset) ──► clear_direction()
```

### Wiring (the Wiring Law)

| Consumer | Status | How |
|---|---|---|
| `db.py` | ✅ | `set/get/clear_tone_direction` (preferences JSON; depth column untouched) |
| `relationship/tones.py` | ✅ | `ToneDirection` + `effective_tone`/`effective_verbosity` (direction wins) |
| `relationship/depth.py` | ✅ | `set_direction`/`clear_direction`/`direction`/`explain_tone`; `status()` reports effective; `refresh()` preserves |
| `persona/engine.py` | ✅ | `profile()` tone follows the effective direction |
| `nlu` | ✅ | `Intent.STYLE` (LLM-first, rules fallback: casual/formal/reset/…) |
| `nl_router.py` | ✅ | `_style_response` — set/reset/clarify; ASK routes "why do you talk that way" to reasoning |
| `reasoning/providers.py` | ✅ | `style_provider` for `QuestionType.STYLE` (cites stored request + depth) |
| `briefing/` | ✅ | morning/evening briefing tone adapts to the explicit direction |
| `friday4 relationship tone [tone] [--verbosity N] [--reset]` | ✅ | CLI debug hatch; status shows the direction |
| Web dashboard | ✅ | Relationship card shows the explicit direction line |

### MCU test (five-moment #4)

"Be more casual, Tony" → different tone this session → persisted next
session → explainable why ("I'm casual because you asked me to be").
✅

---

## What actually shipped (close-out)

- **46 hermetic tests** — `tests/test_wave16_capability.py` (20) +
  `tests/test_wave17_tone_direction.py` (26): db persistence, effective
  tone merge, refresh preservation, Intent.STYLE fallback +
  LLM-still-wins, router set/reset/explain, reasoning providers citing
  evidence, briefing tone, CLI commands, web dashboard probes.
- **Bug fixed along the way:** `QuestionType` ordering — "what are your
  capabilities" contains identity's "what are you" phrase as a
  substring, so CAPABILITY/STYLE rules moved ahead of IDENTITY.
- **Bug fixed along the way:** `RelationshipEngine.refresh()` was
  passing the depth-derived tone into `status()`, which would clobber
  an explicit direction on every daemon sweep — the tone arg is no
  longer passed (status computes effective).

## What we learned

- **Direction over depth is a merge, not a replacement.** Storing the
  override separately (preferences JSON) keeps the depth column honest
  and lets Friday explain *both*: "you asked me to be casual" AND "we'd
  otherwise be at depth 0.85". One store, two views.
- **The registry's first value is honesty.** "What can you do" was a
  hardcoded string before; now it's the truth, and it grows when
  Friday learns. The hard part isn't the data model — it's keeping the
  answer from being noise. Layer-grouped summaries keep it readable.
- **Ordering bugs are invisible to happy-path tests.** The
  "your-capabilities" substring collision only surfaced when testing
  the exact user phrasing end-to-end — worth always testing the
  concrete MCU sentences, not paraphrases.

---

*Next: Wave 18 — Polish, Scale & Dogfood (the sentence, proven under
real use). The five MCU acceptance tests, all passing through speech
only.*
