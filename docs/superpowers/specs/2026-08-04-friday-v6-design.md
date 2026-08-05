# Friday V6 — The V4 Evolution (Verified Best-of-Merge)

Date: 2026-08-04 (revised after source-level verification)
Rev. 2026-08-05: Decision 1 revised — V6 is a standalone `friday_v6/`
package seeded from a full copy of V4; V4 is frozen as the reference
product. Re-verified: 1,376 tests green in the `friday_v6/` namespace.
Status: Approved design — revision of the original merge blueprint
Supersedes: the first V6 draft (V5-core rewrite). Replaced by the
verified facts below.

## ⚠️ What changed and why

The first draft said "Core = V5's architecture, rewrite as a standalone
package." Source-level verification (2026-08-04) contradicted that:

| Claim (draft 1) | Verified fact | Source |
|---|---|---|
| "V4's NLU is keyword scoring, not comprehension" | **FALSE — V4 is LLM-first** (`classify()` via LLM; rules only offline) | `nlu/resolver.py` docstring + code |
| "V4's suite is green (assumed)" | **TRUE — 1376 passed, 0 failed** (run 2026-08-04, ~4 min) | full `pytest` run |
| "V4 surfaces are portable" | **TRUE — desktop/ + voice/ have ZERO sqlite3; mobile/ has one fallback block** | code search |
| "Port is thin" | **TRUE — exactly one brain call site per surface** | `mobile/api.py:249`, `web/dashboard.py:442` |
| "V5 voice is a copy of V4" | **TRUE — identical line counts, router swapped only** | `diff -rq` + `wc -l` |
| "V3 rigor is real" | **TRUE — evidence-scoped retrieval, provider composition, coverage audit** | `ask.py` |
| V3 suite state | **UNVERIFIED** — 2,458 collected; README (07-27) says 1,656 pass / 9 pre-existing fail; suite has grown since | not run (too long) |

**Conclusion:** V4 is already the product with a comprehension-first,
safety-floored brain — not a rules-brain to replace. A from-scratch V6
would discard 1,376 verified green tests and re-implement `nl_router`
behaviors (IDE tie-breaks, app-learning hooks, durable asks, mission
adaptation) as skill prompts. **V6 is therefore the verified V4 core
re-seeded into the standalone `friday_v6/` package — add the V5 wins
(vault memory, Textual HUD, deeper Claude comprehension) and the V3
evidence discipline on top; V4 stays frozen as the reference.**

## Vision

V4 (43K LOC, 1376 green tests) is the shipped, tested, multi-surface
product. V5 (5K LOC, 42 tests) proved three things V4 lacks: **a vault
of linked markdown as human-readable memory, a Textual HUD as a real
face, and "Claude does the routing" as a simplification.** V3 proved an
**evidence discipline** (scope-checked retrieval, never fabricate) that
V4 already partially honors.

**V6 = V4 + vault + HUD + Claude-comprehension-first + evidence
discipline.** The verified V4 core re-seeded into `friday_v6/`, with
the new layers on top — same green core, frozen reference, new layers.

## Why V4 is the base — and not V5 (decided after verification)

The honest either/or is: **graft V4's product onto V5's clean core**
(upgrade V5), or **graft V5's vault/HUD onto V4's tested product**
(upgrade V4). The evidence says upgrade V4:

1. **V5's core premise is factually false.** V5's design spec built its
   whole case on "V4 is a rules-brain: keyword scoring, not
   comprehension." `nlu/resolver.py` shows V4 is **LLM-first** (Wave
   13a) with rules only as the offline floor, and it already delegates
   agentic goals to Claude Code. The main thing V5 set out to "fix"
   was already fixed in V4.
2. **V5's engine IS V4's bridge, de-DB'd.** V5's `engine.py` is
   documented as a port of V4's `agent/bridge.py` with the DB removed.
   V4 already has the persistent Claude session. The entire V5 delta is
   three things: vault, HUD, skills — all of which port to V4 as
   additive layers in days.
3. **Upgrading V5 to product parity = rebuilding V4.** The moment V5
   needs missions with status, durable permission asks, audit trails,
   security scanning, collab sync, or mobile push (all of which V4
   ships today), its "no DB" purity breaks and you re-add structured
   storage — plus every executor, surface, and gate. That is months of
   work re-learning 1,376 tests of accumulated lessons.
4. **Upgrading V4 is bounded and reversible.** Vault + HUD + skills +
   abort are additive; the 1,376 green tests stay green; removing a
   layer is trivial. There is no big-bang risk.
5. **The "no DB" purity is a symptom of size, not a design win.** V4's
   DB is the answer to problems V5 hasn't hit yet, not legacy cruft.

**The synthesis:** upgrade V4 *in the direction of* V5's architecture —
vault-first prose memory, skill routing, HUD face — but on the tested
base. V5's philosophy is the north star; V4 is the vehicle, re-seeded
as the `friday_v6/` package so the vehicle stays frozen while V6
evolves.

## Decisions (user-locked)

1. **V6 is a standalone package; V4 is frozen.** The repo seeds
   `friday_v6/` from a full copy of V4 (verified: 1,376 tests green in
   the V6 namespace, 2026-08-05). V4 stays untouched as the reference
   product; all new capability (vault, HUD, skills, abort) lands in
   `friday_v6/` only. No from-scratch rewrite.
2. **The brain stays V4's** — `nlu/resolver.py` is already LLM-first
   with deterministic fallback, and `nl_router.py` already delegates
   agentic goals to Claude Code. We *deepen* that, we don't replace it.
3. **Vault = the new memory surface.** A `friday_v4/vault/` module
   (ported from V5) becomes the operator-facing memory: append-only
   `raw/`, distilled `wiki/` with `[[links]]`, `outputs/`. The SQLite
   DB stays the source of truth for *structured state* (missions,
   permission asks, audit, sessions); the vault is where memory is
   *read and written as prose*. `MemoryFact` reads/writes both.
4. **HUD = the new face.** V5's Textual HUD is ported (vitals, stream,
   schedule, notices, permission buttons, input) and wired to the same
   `TextCommandHandler` every other surface uses.
5. **Claude-comprehension-first.** The LLM-first resolve stays; the
   `CLAUDE:` bridge stays; new skills (execute/proactive/remember/
   research/schedule) are the *documented* Claude path. The
   deterministic rules remain the safety floor — never gated behind
   the network.
6. **Search index is a cache.** SQLite FTS over the vault, rebuildable.
   Grep stays ground truth.
7. **No new hard dependencies.** Textual + psutil are optional extras
   (already proven by V5). The never-crash law holds everywhere.

## Architecture

```
        voice │ mobile │ web │ IDE │ desktop │ HUD
             │         │       all surfaces → TextCommandHandler.handle()
             ▼         ▼
┌───────────────────────────────┐
│  nl_router.py  (V4, stays)    │   LLM-first resolve() → deterministic floor
│  ──────────────────────────── │
│  execution/  missions/  ask/  │   (1,376 tests — untouched)
└──────┬─────────────────┬──────┘
       │                 │
       ▼                 ▼
┌─────────────────┐  ┌──────────────────────────────────────────┐
│ CLAUDE: bridge  │  │  VAULT (NEW, from V5)                    │
│ (V4, stays)     │  │  raw/ wiki/ outputs/ notices/            │
│ persistent      │──►│  + FTS index.db (rebuildable cache)     │
│ session         │  └──────────────────────────────────────────┘
└─────────────────┘          ▲
       │                     │ MemoryFact reads/writes prose + structured rows
       ▼                     │
┌─────────────────┐  ┌──────────────────────────────────────────┐
│ HUD (NEW, V5)   │  │ SQLite DB (V4, stays)                    │
│ Textual         │  │ missions/ asks/ audit/ sessions/         │
│ vitals+stream+  │  └──────────────────────────────────────────┘
│ schedule+perm   │
└─────────────────┘
```

### Laws

- **The green core is sacred.** Nothing in V6 changes `execution/`,
  `missions/`, `nlu/`, `db.py` contracts. New capability is a new
  layer, never a modification of a verified one (V3 Law 25).
- **DB = structured truth, vault = prose memory.** Missions, asks,
  audit, sessions stay in SQLite (they need queries and transactions).
  The vault carries what a human wants to *read*: raw turn log, wiki
  notes, outputs. `MemoryFact` is the single bridge between them.
- **LLM enhances, never gates** (V3's philosophy, V4's practice). The
  Claude path may answer better; the deterministic path always answers.
  No request is a dead-end because the network is down.
- **Vault = single source of truth for what the HUD shows.** Schedule,
  notices, activity all read the vault (V5's proven pattern); the HUD
  polls ~2s and never writes.
- **Evidence discipline.** Wiki notes carry `sources:` frontmatter;
  `execute` writes a verification note; answers never fabricate.
- **Never-crash.** Missing Textual → HUD disabled, CLI works. Missing
  SDK → bridge reports unavailable, NLU rules answer.
- **Hermetic tests.** SDK/audio/WM/Textual mocked; vault ops tested
  against tmp dirs.

## What each version contributes (final)

| Source | Kept in V6 | Where |
|---|---|---|
| **V4** | The product: brain, surfaces, missions, execution, security, collab, mobile, web, IDE, daemon | frozen reference; re-seeded into `src/friday_v6/` (1,376 tests re-verified green) |
| **V5** | Vault module, Textual HUD, skills files, proactive notices pattern | new `vault/`, `hud/` (ported), `.claude/skills/` |
| **V3** | Evidence discipline: `sources:` frontmatter, review-owns-truth, kill switch, FTS-over-vault search | new `vault/`, `abort` command |

## New modules (all additive)

```
src/friday_v6/
├── vault/
│   ├── vault.py      ← V5 port: raw/ wiki/ outputs/ notices/ ([[links]], grep query)
│   ├── index.py      ← NEW: SQLite FTS cache + rebuild + query
│   └── memory.py     ← NEW: MemoryFact bridge (vault prose ⇄ DB facts)
├── hud/              ← V5 port: Textual HUD (vitals/stream/schedule/notices/perm/input)
├── skills/           ← V5 port: execute/proactive/remember/research/schedule
├── abort.py          ← NEW: kill switch (permissions/abort checked by the bridge hook)
└── cli_vault.py      ← NEW: friday6 vault ls|find|note, friday6 index rebuild
```

`friday6 hud`, `friday6 vault`, `friday6 index rebuild`, `friday6 abort`
are the new CLI entry points. Everything else is the re-seeded V4 core.

## Waves + build order

| Wave | Builds | Verify |
|---|---|---|
| W0 | vault/ port + FTS index + `friday4 vault` CLI | vault hermetic tests green; index rebuild round-trip |
| W1 | MemoryFact bridge (vault ⇄ DB) + `sources:` convention | fact → wiki note → recall; DB rows intact |
| W2 | skills/ port + `CLAUDE:` skill routing doc | skills discoverable; bridge degrade path |
| W3 | Textual HUD port + wiring to `TextCommandHandler` | HUD constructible; degrades without Textual |
| W4 | HUD permission buttons + notices + proactive | pending ask → HUD → allow/deny → execution |
| W5 | abort/kill switch in the bridge tool hook | abort mid-session (mocked); escalations test |
| W6 | polish: FTS in HUD search, schedule panel from vault | full suite green (target 1,376 + new), live smoke |

## Risk register (honest)

| Risk | Severity | Mitigation |
|---|---|---|
| Vault ⇄ DB dual-write drift | Medium | MemoryFact is the single bridge; one write path, tests assert both sides |
| HUD steals focus from CLI/voice | Low | HUD is optional extra; all surfaces still route the same handler |
| FTS index staleness | Low | rebuild command + background refresh on vault write; grep fallback |
| Claude network dependency | Medium | deterministic floor never gated; bridge degrades to rules |
| V3 suite state unknown | Info | V3 remains frozen/retired; not imported by V6 |

## Explicitly out of scope

- Rewriting V4's verified core; adopting V3's DB brain or V5's
  no-DB purity; Obsidian plugin; multi-user auth.
- V3 stays as an optional read-only data source via `v3source.py`.

## Exit criteria

- Existing 1,376 V4 tests stay green (re-verified in the `friday_v6/`
  namespace, 2026-08-05); new vault/HUD/abort suites green.
- `friday4 vault find` answers with the FTS index AND with grep when
  the index is deleted (cache, not truth).
- A wiki note written by Claude carries `sources:`; a completed
  execute writes a verification note.
- HUD shows schedule + notices from the vault, and a permission ask
  resolves via HUD buttons, mobile PWA, and CLI — the SAME ask.
- `friday4 abort` stops a (mocked) bridge session mid-turn.
- `friday4 status` is green with zero optional extras installed.
