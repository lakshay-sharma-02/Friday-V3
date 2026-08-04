# Friday V5 — Claude Engine, Vault Memory, Terminal HUD

Date: 2026-08-04
Status: Approved design (brainstorming)

## Vision

V4 is a rules-brain around a SQLite DB (43k LOC): keyword scoring + pattern
matching, not comprehension. V5 flips the architecture:

- **Claude Code = the engine** — routes every request to the right skill.
- **Vault of linked markdown = the memory** — no database.
- **Local voice = the ears+mouth** — STT/TTS, private and free.
- **Textual HUD = the face** — one screen for vitals, schedule, commands.

## Decisions (user-locked)

1. **Standalone `friday_v5` package**, sibling of `friday_v4`. Copies code we
   want; no V4 imports.
2. **V4 reuse: voice stack + de-DB'd bridge only.** `desktop/wm`, `nlu`,
   `reasoning`, `execution`, `collab` are NOT adopted — Claude + skills + vault
   replace them.
3. **HUD built on Textual** — the one justified new dependency.
4. **Approach A: vault is the single source of truth** for both memory AND the
   HUD screen. No engine↔HUD event bus.
5. **HUD takes typed input** — routes to the same `Engine` as voice.

## Architecture

```
        typed / voice
             │  text
             ▼
┌─────────────────────┐   engine.ask(text)   ┌──────────────────────┐
│   HUD (W4)          │ ───────────────────► │   Engine             │
│   Textual, one      │                      │   (persistent        │
│   screen, no tabs   │ ◄─────────────────── │    Claude session)   │
└──────────┬──────────┘   on_output stream   └──────────┬───────────┘
           │ reads  (poll ~2s)                         │ session
           ▼                                            ▼
┌──────────────────────────────────────────────────────────────┐
│                        VAULT (plain files)                    │
│   raw/  wiki/  outputs/  permissions/  notices/               │
└──────────────────────────────────────────────────────────────┘
           ▲
           │ writes — Claude's tools (Read/Edit/Write/Glob/Grep)
┌──────────┴───────────┐
│  Claude Code session  │  ← routes via .claude/skills/*/SKILL.md
│  (the actual brain)  │
└──────────────────────┘
```

### Laws

- **Vault = single source of truth.** Memory AND screen both read it. Engine
  writes `raw/` every turn; skills write `wiki/` + `outputs/`; HUD polls and
  renders. No DB, no bus, no shared state.
- **Engine = pure pump.** Text in → streamed text out → vault write. Zero
  internal state beyond the bridge session.
- **Claude = the brain.** Routing to skills is tool-calling, not a classifier.
  V5 writes no NLU code.
- **Voice = copied V4 stack**, injectable `route_function = engine.ask_sync`.
  Already in the tree.
- **HUD = mirror + input.** Polls vault every ~2s; subscribes to `on_output`
  for live typing; drives `Engine.allow()/deny()` for permission asks.

## Skills (W1)

Routing is Claude's tool-calling; skills are just files.

| Skill | File | Purpose | Vault writes |
|---|---|---|---|
| `schedule` | `.claude/skills/schedule/SKILL.md` | agenda in `vault/wiki/schedule.md` — add/view/confirm | `wiki/schedule.md` |
| `research` | `.claude/skills/research/SKILL.md` | distilled note per topic | `wiki/<topic>.md` |
| `execute` | `.claude/skills/execute/SKILL.md` | cautious Bash, report | `outputs/<task>.md` |
| `remember` | `.claude/skills/remember/SKILL.md` | facts → people/me notes | `wiki/people.md`, `wiki/me.md` |
| `hud` | `.claude/skills/hud/SKILL.md` | what the HUD can show, keep notes HUD-parseable | (documentation) |

Each SKILL.md = frontmatter (`name` + `description` — what triggers the load)
+ body (how-to: paths, format, `[[links]]` convention).

### Laws

- Skills are only invoked when the moment needs them — Claude reads the
  description, pulls in the md, follows it. That IS the routing.
- Skills write in a HUD-parseable convention: a stable YAML-ish metadata block
  at the top of wiki notes (e.g. `schedule.md` has `status:` + `datetime:`
  lines) so the HUD can render an agenda without Claude's help. The vault is
  the screen, so notes must be machine-readable AND human-readable.
- Skill bodies are short (≈30–50 lines): paths, format rules, examples. If a
  skill needs more, it links to a wiki note instead of growing the md.

## Vault (memory + screen)

```
vault/
├── raw/        2026-08-04.log   (append-only, one per day)
├── wiki/       schedule.md  people.md  me.md  <topic>.md
├── outputs/    <artifact>.md   (reports Friday ships)
├── permissions/  pending/*.md  approved/*.md  denied/*.md
└── notices/    <id>.md   (proactive pings, later wave)
```

- `raw/` — every turn, both sides, timestamped. The conversational memory.
- `wiki/` — distilled notes, `[[links]]` between them. The graph. Grep is the
  query.
- `permissions/` — the file-based gate already exists (pending → decision
  sidecar). HUD reads pending, drives allow/deny.
- `notices/` — new, W3: Claude writes a ping file, HUD surfaces it. No
  callback plumbing.

**Query model:** `Vault.query(terms)` already greps wiki+raw. For the HUD's
agenda view, `schedule.md`'s metadata block is parsed by a small pure function
(`vault.py` already has `links_from`).

### Laws

- Files are append-mostly; wiki notes are write-in-place by Claude's tools.
  Both fine — the file system is the graph.
- Human-readable always: a note must make sense in Obsidian and on the HUD.
- Datestamp everything in raw/; wiki notes carry frontmatter (`created:`,
  `updated:`, `status:`).

## HUD (W4, Textual)

One screen, no tabs:

```
┌──────────────┬─────────────────────────────────────────────┐
│ VITALS       │  FRIDAY — <engine status>                   │
│ cpu 12%      │  <last exchange / current stream, live>     │
│ mem 3.2G     │                                             │
│ disk 61%     │  ──────────────────────────────────────     │
│ audio ●       │  SCHEDULE (from wiki/schedule.md)          │
│              │  · 09:00 standup                            │
├──────────────┤  · 14:30 review                            │
│ COMMANDS     │                                             │
│ [ask] [perm] │  ──────────────────────────────────────     │
│ [end] [quit] │  NOTICES (from vault/notices/)              │
│              │  · 3 new vault notes this morning           │
├──────────────┤  ──────────────────────────────────────     │
│ INPUT        │  VAULT ACTIVITY (raw tail)                  │
│ >            │  [09:12] user  standup at 9am               │
│              │  [09:13] friday ok, added to schedule       │
└──────────────┴─────────────────────────────────────────────┘
```

- Left column: vitals (psutil — already a V4 dep), command deck, input box.
- Right: live stream, schedule, notices, vault activity.
- Polls vault every ~2s for schedule/notices/activity/permissions; engine
  `on_output` for live typing; ~1Hz render timer.
- Permission asks appear as `[allow]`/`[deny]` buttons → `Engine.allow()/deny()`.
- Input box routes to the same `Engine` as voice.

### Laws

- Read-only poll of the vault — HUD never writes except through the engine's
  permission path.
- Layout survives terminal resize; Textual handles it natively.
- Textual is the ONE new dependency (user decision already locked).

## Waves + build order

| Wave | Builds | Copies from V4 |
|---|---|---|
| W0 | skeleton (exists), bridge de-DB'd, vault, CLI | bridge.py (done) |
| W1 | 5 skills, verify engine→vault loop | — |
| W2 | voice session: PTT + hotword, `route_function=engine.ask_sync` | voice/ (already copied) |
| W3 | `notices/` + proactive: Claude writes a ping when it notices something worth surfacing | — |
| W4 | Textual HUD (vitals, stream, schedule, notices, input, perm buttons) | — |
| W5 | polish: chimes, barge-in tuning, HUD theming, vault search in HUD | voice chimes (done) |

Each wave is independently verifiable (CLI, voice, HUD each run standalone).

## Explicitly out of scope

- V4 `desktop/wm_abstraction`, `nlu/`, `reasoning/`, `execution/`, `collab/`
  adoption (decision 2).
- DB, ambient bus, daemon workers (V4's DB-bound architecture).
- Obsidian app integration (vault is plain files; Obsidian can open it, but
  V5 doesn't ship an Obsidian plugin).

## Verified (2026-08-04)

W2–W5 implemented: voice notifier (`voice/notifier.py`), vault notices +
proactive poller (`proactive.py`), proactive skill, HUD parsers/widgets/app
(`hud/`), `friday5 hud` entrypoint, `hud` pyproject extra, `packages=find`
fix. Full suite green (35 tests); HUD degrades gracefully without Textual;
`friday5 status` healthy (bridge unavailable = SDK not installed, never-crash
law holds); notifier→vault→`latest_notices` round-trip verified; skills
discoverable: execute, proactive, remember, research, schedule.
