# The Friday Master Plan — "One Sentence, One Product"

> **THE SENTENCE (the only spec that matters):**
>
> ### MCU FRIDAY is a single presence that speaks natural language, has no learning ceiling, no task it can't pick up, and adapts its personality to you.
>
> Every wave below exists to make that sentence true. If a wave doesn't
> serve one of its four clauses, it doesn't get built.
>
> Governance: [**THE MCU FRIDAY STANDARD**](MCU_FRIDAY_STANDARD.md) — ten
> laws, five MCU acceptance tests, one definition of done. This plan is
> the *route*; the Standard is the *judge*.

---

## 0. The sentence, decomposed

| Clause | Means | Ships in |
|--------|-------|----------|
| **single presence** | Same memory, context, identity across voice / terminal / web / desktop / mobile. One Friday, not five tools. | Wave 15 |
| **speaks natural language** | Speech is the *only* user surface. Every capability has an NL path; CLI is a debug hatch. | Continuous (Law 1) |
| **no learning ceiling** | Anything demonstrable is learnable — memory forms models, skills form from watching, both generalize. | Waves 13, 14, 16 |
| **no task it can't pick up** | Capability registry + composition: Friday decomposes any request into known capabilities. | Wave 16 |
| **adapts its personality to you** | Explicit tone-direction persists; identity is data-driven, consent-first, never hardcoded. | Wave 17 |

**The through-line:** Waves 1–12 built (and are building) the *surfaces*
and the *safe core*. Waves 13–17 build the *sentence*. Waves 11–18 build
the whole path; shipped waves get hardened (§2) so the foundation is
strong underneath everything new.

---

## 1. Where we are (already shipped / in flight)

| Wave | What | Status |
|------|------|--------|
| 1 | Voice interaction | ✅ SHIPPED |
| 2 | Desktop presence | ✅ SHIPPED |
| 3 | Security & quality | ✅ SHIPPED |
| 4 | Proactive intelligence | ✅ SHIPPED |
| 5 | Collaboration | ✅ SHIPPED |
| 6 | IDE Integration — adaptive editor detection (VS Code / JetBrains / Neovim / Sublime / Emacs), pure-stdlib LSP client, always-on AST analyzer, editor control (open/reveal/run), NL path ("what's wrong with X"), CODE reasoning provider, claude preflight composition | ✅ SHIPPED |
| 7 | Mobile Companion — the phone surface itself: installable PWA (`mobile/app/`, served by `friday4 mobile serve`) — one-time-code pairing, shared-thread chat, live SSE push feed with durable replay, status; native Expo app (`mobile/app/` — TypeScript, typechecked) as the documented background-push path with build + account steps in its README; `mobile serve` prints the app URL | ✅ SHIPPED |
| 9 | Agency Core — the brain (DB, NLU, reasoning, missions, execution, NL router) | ✅ SHIPPED |
| 10 | Memory & identity (memory, persona, relationship, skills scaffold) | ✅ SHIPPED |
| 11 | Research & reflection (research, synthesis, briefing, ambient push) | ✅ SHIPPED |
| 12 | Polish & scale (benchmarks, docs site, installer, migration guide, SSH executor) | ✅ SHIPPED |
| 13 | Thinking Core — LLM synthesis over evidence (enhance, never gate) | ✅ SHIPPED |
| 13a | ONE NLU Point — `resolve()` LLM-first, rules fallback; `understanding/` = thin shim | ✅ SHIPPED |
| 14 | Watch Me — demonstration capture (audit-log tagging, repetition notices, SKILL NL paths) | ✅ SHIPPED |
| 15 | One Presence — shared context session + web-chat polish + ambient push to every surface + mobile transport (one session follows you across surfaces; "what did we talk about this morning?" answered from any surface) | ✅ SHIPPED |
| 16 | Capability & Self-Extension — registry (executors/providers/intents/surfaces + learned skills), "what can you do" from the registry | ✅ SHIPPED |
| 17 | Adaptive Identity — tone-direction ("be more casual" persists + explainable), effective-tone merge, briefing adapts | ✅ SHIPPED |
| 18 | Claude Code Hands — complex agentic goals ("figure out why the build fails and fix it") delegate to the local Claude Code CLI through the same gate → sandbox → audit pipeline | ✅ SHIPPED |
| 19 | Polish, Scale & Dogfood — the sentence, proven under real use (five MCU acceptance tests through speech only, five most annoying bugs, benchmarks, docs site, installer, migration guide verified, 1085 tests) | ✅ SHIPPED |

**Foundation present:** **1085 hermetic tests green** (W19 close-out),
evidence-honest reasoning, **LLM synthesis over evidence** (Wave 13 — enhances, never
gates; conversation-capable ``friday4 ask``), gated/audited/undo-able
execution, memory with provenance and decay, shadow-first skills, daemon
with sweeps/learners, web dashboard, V3 read-only bridge, **ambient
push** (Wave 11 SSE + shared bus), and Wave 12 polish (benchmarks, docs
site, installer, migration guide, SSH executor). The shell and the safe
core are real, and **Wave 13a (ONE NLU Point) is shipped** — every
surface routes through one LLM-first `resolve()`, `understanding/` is a
thin shim, and the fallback never crashes (it classifies honestly
instead). **Wave 14 (Watch Me) is shipped** — explicit "watch me do
this" capture, repetition notices, and SKILL NL paths all land on the
audit log. **Wave 15 (One Presence) is SHIPPED** — the shared-context
session (one thread per day across talk/ask/voice/web/mobile),
time-window recall ("what did we talk about this morning?" from any
surface), web-chat hydrate (the browser resumes the terminal thread),
ambient push reaching every surface (voice speak / desktop banner via
surface channels), and mobile transport via the durable queue
(`PushNotificationService` + companion API — status / conversation /
talk / SSE). See `WAVE_15_ONE_PRESENCE.md`.

**Foundation missing (the sentence's gap):** demonstration capture,
a capability registry, cross-surface continuity, adaptive tone. Waves
14–17 close exactly that — and §2 makes sure the already-shipped waves
are pulled up to MCU standard *as* we build them, so nothing is left as
a CLI-only island. **Wave 16 (Capability Registry) and Wave 17
(Adaptive Identity) are now shipped** — the registry answers "what can
you do" from real state and grows with learned skills (Law 7), and
"be more casual, Tony" shifts tone this session, persists, and is
explainable (Law 8, MCU test #4). See `WAVE_16_17_ADAPTIVE.md`.
**Wave 19 (Polish, Scale & Dogfood) is now SHIPPED** — the five MCU
acceptance tests pass through the shared NL brain (speech-only
surface), the five most annoying bugs are fixed, and the floor
(benchmarks / docs site / installer / migration guide / 1085 tests) is
verified. **All five sentence clauses are ✅.** See
`WAVE_19_POLISH_DOGFOOD.md`.
**Wave 6 (IDE Integration) is now SHIPPED** — the editor is no longer
an empty placeholder: Friday detects which editor is present (VS Code /
JetBrains / Neovim / Sublime / Emacs — env, process, and config
signals), analyzes code through a pure-stdlib LSP client (pyright /
pylsp / typescript-language-server / gopls / rust-analyzer) with an
always-on AST fallback (syntax errors, undefined names, unused imports,
shadowed builtins), controls the editor (open / reveal / run), and
composes with execution: "what's wrong with auth.py" is an NL intent on
every surface, the reasoning layer has a CODE provider, and — when
`FRIDAY_V4_IDE_PREFLIGHT` is set — Friday's diagnostics ride along with
Claude Code delegations via `--append-system-prompt`. The TypeScript VS
Code extension (sidebar/status bar) remains the one explicitly-unbuilt
piece; the editor is reachable today through the CLI + LSP without it.
See `WAVE_6_IDE.md`.

---

## 2. Foundation Hardening — retro-fit the shipped waves

> **The rule:** a shipped wave isn't *done* until it speaks MCU. Every
> built wave below has gaps against the sentence; the hardening items
> close them. Each item lands with its natural wave (column "Lands
> with") or anytime it's small enough — and always with its NL path in
> the same change (Law 1 + Wiring Law). Never as a separate CLI-only
> feature.

| Built wave | MCU gap today | Hardening (retro-fit) | NL path (Law 1) | Lands with |
|---|---|---|---|---|
| **1 · Voice** | Voice is a *surface*, not *the* surface. Full spoken roundtrip (hear → think → act → speak) never verified end-to-end. | Verify the whole loop live: hotword → NLU → gate → execute → spoken result. Voice confirmation for every confirm-gate action (already asked aloud — verify). Reach ask/talk/memory/skills by voice. Barge-in + chimes verified. | "Friday, run the tests" → spoken execute → spoken result | Continuous + W13 |
| **2 · Desktop** | Context isn't fully ambient; volume control remains voice-only. | ✅ NL entry shipped: `desktop_text_command` routes focus / switch / launch / open / screenshot / workspace / status through `friday4 talk` + web chat (same verbs as the voice router — desktop is no longer CLI/voice-only). Active window/app feeds the context engine continuously (observer exists — verify it feeds reasoning, not just anticipation). | "switch to workspace 3", "screenshot the screen" — talk, web, voice | Continuous |
| **3 · Security** | Severity gating doesn't feed anticipation yet. | ✅ NL entry shipped: `Intent.SECURITY` — "scan my repo" → `VulnerabilityScanner` graded report, high/critical findings pushed onto the ambient bus (briefing/web feed see them). Never crashes (missing scanner degrades honestly). Severity gating feeds anticipation next. | "scan this repo" → graded report, pushed | W11 |
| **4 · Proactive** | Suggestions poll + notify only; can't be actioned by voice. | Suggestions actionable via NL ("yes, run it" → confirm → execute). Replace the suggestion poll with W11 push. Anticipation consumes memory/skills/missions — not just V3 + activity. | "what should I do next" → suggestion → "go" | W11 |
| **5 · Collab** | Peer obs don't feed briefings/anticipation yet. | ✅ NL entry shipped: `QuestionType.COLLAB` + `collab_provider` — "what's my team working on" → reasoning cites peer observations + known peers (read-only, coordinator perms respected). Peer obs feed briefings + anticipation next (single direction). | "what's happening in the team workspace" | W11 |
| **7 · Web** | Dashboard is read-mostly; no NL chat; polls. | NL chat surface on the dashboard (same `nl_router` — one command language everywhere). SSE push via W11 ambient. | Type/speak in the dashboard → the same Friday | W11 + W15 |
| **9 · Agency** | Reasoning is deterministic lookup; missions never exercised in real use; execution CLI-only. | W13 LLM provider (enhance, never gate). Mission shepherding: propose next step, report blockers, "plan changed because…" (MCU test #1). Executor breadth: web/network (W12). Mission progress by NL. | "ship the auth refactor by Friday" → shepherded; "how's it going?" | W13 + W16 |
| **10 · Identity** | Tone is depth-driven only; no explicit direction. | ✅ Memory NL entry shipped: `Intent.MEMORY` — "remember that I prefer Rust" / "what did I tell you about X" → facts with provenance, consent-first, never crashes. Tone-direction ("be more casual") pull-forward from W17 remains; meaning extraction continues there. Persona answers draw on models, not just quotes. | "call me Lakshay", "be more casual", "I prefer Rust…" | W17 + anytime |

**Tracking:** a hardening item is done when its NL path works end-to-end,
its test exists, and the wave doc's wiring table says so. The sentence
progress tracker (§7) marks the clause it moved.

---

## 3. The waves ahead

> Order is deliberate — each wave unlocks the next. Every wave must land
> with its natural-language path (Law 1) and hermetic tests in the same
> change (Wiring Law).

### Wave 11 — Research & Reflection ✅ SHIPPED (5–6 weeks)

**Why it serves the sentence:** *no task it can't pick up* and
*understands everything* — the "researched before asked" layer. Also the
transport for §2 hardening (ambient push replaces every poll).

- `research/` — architecture analysis, cross-project correlation, impact
  analysis, code search, README purpose recovery. Evidence-cited,
  ranged + confidence estimates, cached with invalidation.
- `synthesis/` — deterministic, evidence-cited reports (same evidence →
  same report).
- `briefing/` — morning/evening briefings from real V4 state (missions,
  security, drift, memory), tone- and time-of-day-adapted.
- `ambient/` — in-process event bus + durable queue + channels (voice
  speak, desktop notify, web SSE). **Push replaces polling** — the
  architectural fix the whole product has been waiting for; it also
  carries the §2 hardening (security findings, team obs, suggestions).
- CLI: `friday4 analyze / correlate / briefing / narrative / report`
  (debug hatches only — NL paths land with them).

**MCU test:** "Friday, analyze the integration cost between vivaha and
MindWell." → cited, ranged estimate. ✅

**What actually shipped (close-out):** `research/`, `synthesis/` +
`reports.py` (daily/weekly), `briefing/`, `ambient/` with **push wiring**
(SecurityScanner, ProactiveSuggestionChannel, and collab Coordinator all
publish onto one daemon-shared `AmbientBus`), web **SSE** `GET /api/events`
with dashboard EventSource (poll kept as fallback), and `friday4 research
report --daily|--weekly`. See `WAVE_11_RESEARCH_REFLECTION.md` §9.

---

### Wave 12 — Polish & Scale ✅ SHIPPED (4–6 weeks)

**Why it serves the sentence:** the *reliability floor* under every
other clause — MCU Friday must never crash, never degrade ungracefully.

- ✅ Performance benchmarks (`tools/benchmarks.py`, V3 vs V4 where importable), full hermetic suite (~800), docs site (`tools/build_docs_site.py` → `site/`), installer (`install.sh`), V3→V4 migration guide (`docs/MIGRATION_GUIDE.md`), dogfooding pass (ongoing).
- ✅ `network/` stub folded here: `ssh` executor slots into `execution/`
  behind the same gate → sandbox → audit (extends *no task it can't pick
  up* to remote machines).

---

### Wave 13 — The Thinking Core (LLM reasoning) ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *understands everything* — the single
highest-leverage change in the whole plan.

- ✅ One LLM provider in `reasoning/providers.py` that **enhances**
  deterministic answers (Law 6's "LLM enhances, never gates"):
  - synthesizes across evidence (never without it — citations kept),
  - answers ambiguity honestly, never fabricates ("I don't know" stays
    real),
  - optional — no LLM → current honest deterministic floor.
- ✅ **Default provider: the local 9router proxy** (`localhost:20128/v1`,
  configurable model — already proven in this workspace). Pure-stdlib
  client (`urllib`, reused `nlu.LLMClient`); SSE-trailer quirk handled
  at the client boundary. `FRIDAY_V4_LLM` env; provider pluggable;
  explicit opt-in.
- ✅ `friday4 ask` becomes conversation-capable (follow-ups, context —
  history threaded into the synthesis prompt, Q&A logged).
- ✅ Voice/talk/web inherit it automatically (one entry point via
  `nl_router`). §2 Wave 1 hardening (spoken roundtrip) rides along with
  the §2 pass.

**MCU test:** "What's the deal between vivaha and MindWell?" →
synthesized, cited, ranged — through `friday4 talk`, through voice. ✅

**What actually shipped (close-out):** `reasoning/providers.py`
`llm_provider()` post-pass over the deterministic best (citations kept
verbatim; evidence-less answers never sent to the LLM), engine
`answer(…, history=, llm=)`, conversation-capable `friday4 ask`
(history + Q&A logging), `nl_router` history threading for talk/voice/
web, 17 hermetic tests, wave doc. See
`WAVE_13_THINKING_CORE.md`.

### Wave 13a — ONE NLU Point (LLM-first, rules fallback) ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *speaks natural language* — the
directive: **no regex/keyword matching anywhere in the input path.**
Every surface (voice, `friday4 talk`, web chat) routes through ONE
parser, `resolve()`, which is **LLM-first**; the deterministic rules
are a fallback only when the LLM is absent/offline — never the product.

- `nlu/` package — the ONE point: `llm.py` (9router client),
  `intent.py` (LLM intent + slots, rules fallback), `entities.py` (LLM
  extraction, rules fallback), `confidence.py`, `resolver.py`
  (utterance → canonical action, LLM-first).
- `understanding/` becomes a thin compatibility shim re-exporting
  `nlu` (keeps `missions/planner.py` + tests working).
- `nl_router`, `voice/router.py`, `web/dashboard.py`, `cli_nl` all call
  `resolve(text, llm=LLMClient())` — one point, no surface parses
  language itself.
- `FRIDAY_V4_LLM_URL` / `FRIDAY_V4_LLM_MODEL` / `FRIDAY_V4_LLM_KEY` env
  config; explicit opt-in.
- Deterministic fallback kept ONLY for the never-crash law (offline/
  no-LLM), and clearly labeled fallback — never the primary path.

**MCU test:** "Friday, what's the deal between vivaha and MindWell?"
→ one parser, LLM intent → research → cited answer. No surface
keyword-matches anything. ✅

**What actually shipped (close-out):** the `nlu/` package is the ONE
point — `resolve(text, llm=LLMClient())` is LLM-first (9router client
in `llm.py`), with the deterministic rules only as the never-crash
fallback. Every surface is verified to route through it: `nl_router`
(`resolve(raw, llm=self.llm)`), `voice/router.py`, `web/dashboard.py`
(`TextCommandHandler(conn, llm=llm)`), `cli_nl.py` (`_default_llm()`).
`understanding/` is now a genuinely **thin shim** — the old Wave 9
implementation submodules were removed; only the re-export `__init__`
remains (identity-checked by tests). The close-out also fixed the
fallback's confidence bug (an ambiguous utterance used to raise a
`TypeError` and crash `resolve()` — now it scores correctly and never
crashes), threaded ASK/RESEARCH `target` through the resolver, taught
the fallback to recognize `X vs Y` as research, and fixed the research
layer bugs that blocked the NL correlate path (`_overlapping_names`
str/Path + `_strip_research_lead`). Test suite: `test_understanding.py`
rewritten as shim-contract tests + `test_nlu.py` + research/CLI tests —
25 + 12 + research tests hermetic and green.

---

### Wave 14 — Watch Me (demonstration capture) ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *no learning ceiling* — the "copy any
workflow" clause made real.

- **Raw material already exists:** the audit log IS the demonstration
  record — every action is logged (what, when, result). "Watch me" =
  tag a session on the audit trail and parameterize it into a skill.
- ✅ `watches` table (db migration v5) + `start_watch` / `end_watch` /
  `actions_between` — single-active watch, links the formed skill.
- ✅ `skills/watcher.py` `WatchRecorder` — explicit "watch me" capture
  → generalized **shadow** skill (repo context, consecutive-dup
  collapse, honest reuse-by-name: a same-name skill never swallows a
  fresh demonstration — it gets a versioned name).
- ✅ Repetition detection: `skills/noticer.py` `RepetitionNoticer`
  notices a repeated pattern before being asked ("you run pytest after
  editing tests every time") and *offers* to form a skill — the MCU
  "I noticed you do this every time" moment. Pure read: nothing forms
  until the operator accepts.
- ✅ Generalization: steps carry repo context; shadow/dispatch match by
  context (repo + command), not literal replays.
- ✅ NL paths through the ONE point: `Intent.SKILL` (LLM-first,
  fallback words `watch me` / `learn this` / `stop watching`) →
  `nl_router._skill_response` — "watch me do this", "learn this",
  "stop watching". "What did you learn" is ASK → reasoning
  `skills_provider` (cites real skills — Wiring Law).
- ✅ Daemon: `SkillLearner` runs the noticer *before* `learn()` so
  offers surface in `last_report` (`offers`, `offer_lines`).

**MCU test:** five-moment #2 — skill formed from watching, shadowed,
promoted with approval, auto-dispatches next time. ✅

**What actually shipped (close-out):** db migration v5 (`watches`
table + helpers), `WatchRecorder` (demonstration capture → generalized
shadow skill), `RepetitionNoticer` (offers, pure read), repo-context
matching in `_step_matches` (back-compat safe), `Intent.SKILL` through
the ONE NLU point (LLM prompt + fallback + resolver target), `nl_router`
SKILL routing on every surface, `QuestionType.SKILLS` + `skills_provider`
in reasoning, `friday4 skills watch / watch-stop / noticed / dispatch`, the daemon
noticer wiring, and the wave-10 lifecycle gap (shadow → verified now
happens when a sweep crosses the match threshold; promotion stays
operator-approved). Close-out: the NL accept loop — `Intent.ACCEPT`
("yes, run it") runs a dispatch suggestion's next step through the gate
(voice offers it in `proactive_notify`; NEVER steps stay denied without
`force`); **multi-step acceptances become supervised missions**
(dispatch → mission — first step runs now, the rest is tracked); and the
**daemon `DispatchOfferer`** auto-offers matching skills on a schedule
(desktop notify + durable `dispatch` ambient event, deduped, never
executes). Stale `test_db` schema assertions fixed (v3 → v5) — the
pre-existing `test_db` ×3 failures are gone. 33 hermetic tests in
`tests/test_wave14_watch_me.py`. See `WAVE_14_WATCH_ME.md`.

---

### Wave 15 — One Presence (cross-surface continuity) ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *single presence* — the first clause.

- ✅ Shared context session: a conversation started in the terminal
  continues on the web dashboard, in voice, and on the phone (session
  state in the DB, not per-surface).
- ✅ Ambient push from Wave 11 reaches every surface; mobile transport
  via the durable queue.
- ✅ Web dashboard chat polish: the browser hydrates today's shared
  thread (uses `nl_router` — one command language everywhere).
  Complements §2 Wave 7 hardening.
- ✅ Mobile transport: `PushNotificationService` (cursor-persisted
  durable-queue consumer) + companion API (`mobile/api.py` — status /
  conversation / talk / SSE) + `friday4 mobile serve|push` **and a
  daemon-scheduled `MobilePushWorker`** (drains the queue every
  `mobile_push_interval`, default 60s — the phone gets pushed without
  manual `friday4 mobile push`; `friday4 daemon start` exposes
  `--no-mobile-push` / `--mobile-push-interval` /
  `--mobile-push-priority`). **Operator-configurable destination:** the
  daemon's worker takes a `--mobile-push-hook "<shell command>"`
  (each notification's JSON piped to its stdin — e.g. a curl to
  ntfy.sh) or `--mobile-push-file <path>` (JSONL outbox) — or the
  `mobile_push` section of `~/.friday/v4_config.json` (fields
  `hook` / `file_path` / `interval` / `priority` / `enabled`;
  `FRIDAY_V4_MOBILE_PUSH_*` env). Wave 7 ships the phone surface
  itself: `friday4 mobile serve` now serves the **installable PWA** at
  `/` (pair with a one-time code, chat through the same brain, resume
  the shared thread, live SSE feed with a durable replay cursor) plus
  the companion API — the phone is another surface of the same
  Friday, not a separate product. A React Native / Expo scaffold
  (`mobile/app/` — TypeScript Expo app, typechecked, pinned deps) is
  the documented native path for true background push (needs Node to
  build and a dev build + physical device for push; the Python contract
  is validated hermetically in `tests/test_wave7_mobile.py`).
- ✅ Ambient surface channels: wildcard subscribe (`bus.subscribe("*")`),
  `speak_channel` (voice, CRITICAL) + `desktop_channel` (banner,
  IMPORTANT+) wired by `AmbientWorker.wire_channels` in the daemon.

**MCU test:** ask "what did we talk about this morning?" from voice and
get the conversation from the terminal session. ✅

**What actually shipped (close-out):** **slice 1 — shared context
session**: `db.get_or_create_shared_session` (one `surface='shared'`
session per UTC day; all conversational loggers — `nl_router`,
`cli_ask`, `persona/learn.record_statement` — join it, so voice/web/
mobile inherit through `TextCommandHandler`); **time-window recall** in
`conversation_provider` ("this morning / afternoon / evening / tonight /
today / yesterday / last night / this week / last week" via
`db.recent_exchanges_since`; empty window stays an honest unknown);
classifier reorder (CONVERSATION precedes ACTIVITY so "what did we
talk about yesterday" stays a conversation question). **Slice 2 — web
chat polish**: `web/dashboard.conversation_state()` + `GET
/api/conversation` + chat hydrate (the browser resumes the shared
thread). **Slice 3 — ambient push to every surface**: wildcard
subscribe + `speak_channel`/`desktop_channel` + daemon
`AmbientWorker.wire_channels`. **Slice 4 — mobile transport**: closed
the Wave 7 stub — `mobile/push.py` (`PushNotificationService`,
`file_transporter`), `mobile/api.py` (`MobileAPI`,
`create_api_server`), `cli_mobile.py` (`friday4 mobile serve|push`),
status probe, and **`daemon.MobilePushWorker`** (the daemon drains the
durable queue on a schedule — `DaemonConfig.mobile_push*` fields,
`friday4 daemon start` flags, `mobile` status row; hermetic daemon
tests disable it like every other component). **Close-out 2 —
operator-configurable push hook:** `mobile/push.command_transporter`
(shell command, notification JSON on stdin — never raises, never
wedges the queue) + `config.MobilePushConfig` (`mobile_push` section
of `~/.friday/v4_config.json` + `FRIDAY_V4_MOBILE_PUSH_*` env) +
`MobilePushWorker(hook=…, file_path=…)` + `friday4 daemon start
--mobile-push-hook/--mobile-push-file` (CLI flag wins, config file
fills the rest). **Tests**: 13 hermetic
(`test_wave15_one_presence.py`) + 38 hermetic
(`test_wave15_transport.py` incl. the hook suite) — full suite green.
See `WAVE_15_ONE_PRESENCE.md`.

---

### Wave 16 — Capability & Self-Extension (5–6 weeks)

**Why it serves the sentence:** *no task it can't pick up* — the
registry that makes composition possible.

- **Capability registry:** every executor, provider, skill, and surface
  registered with (intent, params, permission level, evidence rules).
  Friday *knows what it can do* — and can say so. (The §2 hardening
  items are the first entries that prove the registry.)
- **Composition:** "figure out why the build fails and fix it" →
  decompose → research → execute through the gate → verify → report
  with evidence. Composing existing capabilities, none written for that
  sentence.
- **Self-extension:** learning a new skill registers a new capability —
  Friday grows abilities without code (Laws 2 + 7 meet).
- **Reality-first verification:** claims about its own actions cite the
  audit trail.

**MCU test:** five-moment #5 — capability composition, end-to-end,
speech-only. ✅

---

### Wave 17 — Adaptive Identity (3–4 weeks)

**Why it serves the sentence:** *adapts its personality to you* — the
final clause, the emotional core.

- **Tone-direction:** "be more casual, Tony" shifts tone this session
  *and persists*. "Be more formal", "call me Lakshay", "less chatter" —
  all explicit-consent, all remembered, all explainable ("I'm briefer
  because you asked me to be").
- **Identity profile as a view over facts** (Standard Law 8): persona =
  stored preferences + relationship depth + tone direction — never a
  hidden hardcoded personality.
- **Morning/evening briefing tone** adapts to depth *and* explicit
  direction (Wave 11 briefing + Wave 10 relationship + this). §2 Wave 10
  hardening (meaning extraction) rides along.
- Gradual, explainable shifts — never a sudden personality change.

**MCU test:** five-moment #4 — "be more casual, Tony" → different tone
this session, persisted next session, explainable why. ✅

---

### Wave 18 — Claude Code Hands ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *no task it can't pick up* — the
composition clause, made real with an agent that can actually do the
work.

- ✅ **`executor:claude`** — a new gated executor whose command IS the
  natural-language task. It runs the local Claude Code CLI
  (`claude -p "<task>" --output-format json --model fable`
  `--allowedTools "Bash Read Edit Write Glob Grep"`) inside the same
  gate → sandbox → audit pipeline as every other executor: the task
  text is classified (CONFIRM default; destructive *phrases* like
  "push my changes" / "deploy to production" escalate to NEVER), the
  child env is sanitized, stdin is /dev/null, the run is timeout-bounded
  (default 600s, `FRIDAY_V4_CLAUDE_TIMEOUT`), and the JSON result is
  parsed back (is_error / terminal_reason / permission_denials). A
  missing `claude` CLI degrades to a structured failure — never a crash.
- ✅ **NL routing** — "figure out why the build fails and fix it",
  "fix the failing test", "debug the memory leak", "investigate the
  crash" all resolve to the claude executor (LLM prompt + deterministic
  fallback both know the marker phrases). Concrete commands ("git
  status", "run the tests", "read README.md") stay instant on Friday's
  own executors — Claude is the *hands for complex work*, not a toll
  gate on every command.
- ✅ CLI + registry: `friday4 execute claude "<task>"` and
  `executor:claude` in the capability registry ("what can you do"
  tells the truth).

**MCU test:** "Friday, figure out why the build fails and fix it." →
Friday delegates to Claude Code through the gate, audited, result
surfaced. ✅

**What actually shipped (close-out):** `ClaudeCodeExecutor` in
`execution/executors.py` (registered, exported, gated CONFIRM with
phrase-level NEVER sniffing), sandbox stdin=/dev/null, resolver
agentic-goal routing (+LLM prompt `claude` action_type, fallback
marker scoring), `_run_execution` surfaces Claude's result text, CLI
choice + capability entry, 25 hermetic tests
(`tests/test_claude_executor.py` with a fake `claude` binary).
**Close-out 2 — Claude Code as the mission planner:** `ClaudePlanner`
(`missions/claude_planner.py`) fills the `Planner.enhancer` hook —
agentic goals ("ship the auth refactor by Friday") decompose through
`claude -p` with **read-only** tools (`--allowedTools "Read Glob
Grep"`), gated (NEVER goals refused), sandboxed, and audited
(`action_type="claude_plan"`); any failure → the deterministic
planner stands. Opt-in via `FRIDAY_V4_CLAUDE_PLANNER=1` (Wave 13's
`FRIDAY_V4_LLM` convention — hermetic by default), wired into
`nl_router._create_mission` **and** `_replan_response` ("replan this
mission" / "change the plan" — talk/voice/web), with a single
`make_planner(cwd, conn)` construction point that `MissionEngine`
uses for its default planner, so `create()` and `replan()` both
decompose through Claude Code under the same opt-in. **Close-out 3
(W19 slice 0) — the Wiring Law CLI:** `friday4 mission
create|list|status|replan|advance|start|pause|cancel|complete|delete`
(`cli_missions.py`) exposes the layer's debug hatch through the SAME
`make_planner` point (Claude Code decomposition under the same opt-in;
`--json` for scripting). +27 hermetic tests
(`tests/test_claude_planner.py`) + 11 hermetic CLI tests
(`tests/test_cli_missions.py`). See `WAVE_18_CLAUDE_HANDS.md` §4.5.

---

### Wave 19 — Polish, Scale & Dogfood ✅ SHIPPED (2026-08)

**Why it serves the sentence:** the sentence, proven under real use.

- **✅ Slice 1 — the MCU acceptance harness** (`tests/test_mcu_acceptance.py`):
  the five proof moments driven through the shared NL brain
  (`nl_router.TextCommandHandler` — the path talk/voice/web/mobile
  route through), no flags, hermetic (tmp DBs, fake `claude`, seeded
  repos). This is the instrument the exit condition is measured by.
- **✅ Slice 1 — the first two most-annoying bugs fixed:** (1) the
  deterministic NLU fallback classified the MCU deep-reasoning
  sentence "what's the deal between X and Y" as ASK instead of
  RESEARCH (offline machines could never research); (2) the research
  router split "between … and …" so the research lead became an empty
  operand (`correlate("", Y)`). Both fixed (`nlu/intent.py`
  tie-break + `nl_router._extract_pair`), regression-guarded by the
  harness.
- **✅ Slice 2 — the remaining three most-annoying bugs fixed:** (3)
  the "with" variant ("what's the deal with X and Y") also
  classifies ASK — the research pair tie-break now covers "with …
  and …" and `_extract_pair` handles the "with" separator; (4)
  failed commands hid *why* ("git status" in a non-repo said only
  "That didn't work — failed." — `_run_execution` now surfaces the
  first line of stderr); (5) "how's it going" with zero missions
  answered "I don't know yet" — `mission_provider` now says honestly
  "You don't have a mission in flight…" with a pointer to start one.
  All regression-guarded (harness + `test_nl_router` +
  `test_reasoning`).
- **✅ Slice 3 — the floor verified, and the tools themselves
  dogfooded:** `tools/benchmarks.py` measured all seven daily-use ops
  (fixing two silent `n/a` bugs: `answer(conn, text)` arg order and
  the collab CRDT wire shape); `tools/build_docs_site.py` → 23-page
  `site/` with zero broken links; `install.sh` verified end-to-end in
  a throwaway venv (doctor/talk/status green) and its missing
  executable bit fixed (`./install.sh` now works); the migration
  guide's every command claim verified against the real `friday4`
  CLI. Suite: **1085 hermetic tests green.**

**Exit condition (met):** the sentence is *true in daily use* — single
presence, natural language, learning ceiling gone, any task, personality
that adapts. The five MCU acceptance tests pass through the shared NL
brain (speech-only surface), the five most annoying bugs are fixed, and
the floor (benchmarks / docs site / installer / migration guide / 1085
tests) is verified. Everything after this is refinement, not building.
See `WAVE_19_POLISH_DOGFOOD.md`.

### Wave 21 — IDE Control ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *any task* includes the editor. Wave 6
made Friday *read* it; Wave 21 makes Friday *drive* it.

- **✅ NL editor control:** "open src/main.py in the editor" opens,
  "jump to line 42 of cli_talk.py" / "reveal auth.py:7" reveal — all
  through the ONE NLU point, adapted to the detected IDE (VS Code /
  JetBrains / Neovim / Sublime / Emacs, OS opener fallback).
- **✅ Source-file tie-break:** a leading open/show/go + a source-file
  target (whitelisted ext) wins IDE; "open brave"/"open youtube.com"
  stay desktop; "open the editor" focuses the app.
- **✅ No dropped work:** "open main.py and fix it" → EXECUTE → the
  Claude Code gate (fix/debug/repair/rewrite added to the task verbs),
  so a bare "open" can never swallow a task.
- **✅ Every surface:** voice/CLI/web/phone route through
  `TextCommandHandler._ide_response` — zero per-surface wiring.

**Exit condition (met):** verified live on a VS Code machine
(open + reveal real files); 24 hermetic tests; suite 1306 green. See
`WAVE_21_IDE_CONTROL.md`.

### Wave 22 — Agent Bridge & Anywhere Access ✅ SHIPPED (2026-08)

**Why it serves the sentence:** *one presence* means the same Friday
wherever you are — and a Friday that can hold a *working session* with
Claude Code, not just fire one-shot commands.

- **✅ `CLAUDE:` bridge** (`agent/`): the companion chat forwards
  `CLAUDE: <text>` to ONE persistent Claude Code session (Agent SDK
  spawning the same `claude` CLI, same 9router settings) until
  `CLAUDE END` — context accumulates like a real working session.
  Tool-permission asks become durable `permission_requests`
  (source=`bridge`), surface on the ambient bus (Live feed), and
  resolve from any surface via "yes, run it"/"no" through
  `AutonomyAgent.accept/deny`. Lazy SDK import → never-crash without it.
- **✅ Anywhere access (free):** `friday4 mobile remote` prints the
  LAN IPs, the Tailscale 100.x URL (auto-detected), and the free
  Cloudflare quick-tunnel one-liner — the "use Friday from anywhere"
  answer. An optional bearer token (`serve --token` /
  `FRIDAY_V4_MOBILE_TOKEN`) gates every `/api/*` route (the PWA shell
  stays public) so exposing Friday over a tunnel is safe. PWA + native
  app both carry the token field. `serve --tunnel cloudflare` spawns
  the tunnel itself and prints the public URL; `--host` accepts the
  URL `remote` prints (host:port / full URL / trailing slash).
- **✅ Always on, always in the tray (like 9router):**
  `friday4 mobile serve --tray` shows a system tray icon (Open
  dashboard / Show remote URLs / Pair a device / Status / Stop — a
  latin-1 tooltip bug in the Wave-2 tray that crashed icon build is
  fixed); `friday4 mobile autostart` writes the 9router-style XDG
  entry (`~/.config/autostart/friday4-mobile.desktop`, chmod 700,
  quoted Exec) so the companion + tray start on every login, and
  `no-autostart` removes it.

**Exit condition (met):** bridge hermetic suite green (22 tests) with
SDK availability verified live (model fable → oc/deepseek-v4-flash-free
via 9router, PONG); token gate + `remote` tests green; full suite green.
See `WAVE_22_AGENT_BRIDGE.md`.

### Wave 20 — Desktop Natural Language ✅ SHIPPED (2026-08)

**Why it serves the sentence:** "any task, no hardcoded workflows" is
not proven by a bigger phrase catalog — it's proven by a *desktop
language* that speaks like a person and hands the rest to the arms.

- **✅ The NL desktop interpreter** (`desktop/wm_abstraction.py`):
  compound commands ("open chrome on workspace 3 and open whatsapp"),
  workspace qualifiers, browser qualifiers ("in firefox"), "in it"
  chaining, web destinations ("open whatsapp" → web.whatsapp.com),
  site search ("open youtube and cristiano ronaldo channel"),
  explicit search ("search for / look up / google X"), noun-phrase
  search fallback ("open c++ compiler of programiz"), honest
  install-gated launches, and explicit URLs. One language across
  voice, CLI, web, and phone.
- **✅ Task fall-through — the "everything" contract:** any utterance
  that reads like *work* ("open a python venv and install requests",
  "open a fresh project for a discord bot", "clone the repo and open
  it in my editor") is classified PLAN/EXECUTE by the NLU
  (task-verb/noun tie-break in `nlu/intent.py`) and falls through the
  desktop layer to the **Claude Code executor / mission planner** —
  never web-searched, never a hardcoded workflow.
- **✅ Voice un-fragmented:** the voice router's legacy desktop parser
  is gone; it routes through the same shared interpreter, gated on the
  shared NLU intent ("yes, run it" stays accept).
- **✅ App-learning loop (follow-up):** "open my todo app" teaches
  once ("my todo app is obsidian", "use obsidian for my todo app",
  "open my todo app with obsidian") and resolves forever after,
  persisted to `~/.friday/v4_desktop_aliases.json`; only resolvable
  binaries are learned; unknown personal apps get a teaching prompt,
  never a web search; `friday4 desktop aliases/teach/forget` CLI;
  LLM-robust via a pre-dispatch hook in the NL router.
- **✅ Cross-machine continuity (follow-up):** aliases publish as
  collab observations (`alias:<name>` CRDT keys — last-writer-wins
  across machines); `friday4 desktop aliases-sync` pushes local,
  syncs with peers, and merges remote aliases into the store;
  `_resolve_app` only launches a synced binary that exists *here*
  (uninstalled synced apps fall through, never dead-launch).

**Exit condition (met):** the five user examples resolve correctly on a
live desktop; the "handle everything" suite routes tasks to the agentic
arms; full suite green (1172+). See `WAVE_20_DESKTOP_NL.md`.

---

## 4. Wave dependency map

```
1-5,7,9,10  ✅ shipped — the surfaces + safe core
    │
    ▼
11 Research/Reflection ──► 12 Polish (floor)
    │                          │
    ▼                          ▼
13 Thinking Core ──► 14 Watch Me ──► 15 One Presence ──► 16 Capability
(LLM reasoning)     (demo capture)    (continuity)      & Self-Extension
                                                          │
                                                          ▼
                                                   17 Adaptive Identity
                                                          │
                                                          ▼
                                                   18 Claude Code Hands
                                                   (= complex tasks)
                                                          │
                                                          ▼
                                                   19 Polish & Dogfood
                                                    (= sentence true)
                                                          │
                                                          ▼
                                                   20 Desktop NL
                                                    (= talk to the PC)
```

- **11 → 13:** research feeds the LLM richer evidence; the LLM makes
  research synthesis smarter. 11 also carries most §2 hardening (push
  for security/team/suggestions).
- **13 → 14:** the LLM makes demonstration capture generalize ("what
  were you doing?") — but 14 works without it (audit-log based).
- **11 → 15:** ambient push + durable queue are the transport for one
  presence.
- **16 ← everything:** the registry only makes sense once capabilities
  exist to register; the §2 hardening items are its first entries.
- **17 ← 10, 11, 13:** identity needs relationship depth (10), briefing
  (11), and conversation (13) to adapt *well*.

---

## 5. The rules that never bend

1. **The Standard judges everything.** Ten laws, five MCU tests, one
   definition of done — in `MCU_FRIDAY_STANDARD.md`. A feature that
   fails the MCU test is rejected, however clever.
2. **NL path in the same change** (Law 1 + Wiring Law). No CLI-only
   capability, ever — including hardening of already-shipped waves.
3. **Evidence or silence** — no answer without evidence, no action
   without audit. Unchanged since Wave 9.
4. **Never crash, degrade silently, hermetic tests.** Unchanged since
   Wave 1.
5. **V4 is the product; V3 is read-only heritage.** Unchanged forever.
6. **Cut scope over cutting laws.** A wave that can't satisfy the
   standard in its time budget shrinks — it never bends the standard.

---

## 6. Definition of done for a wave

- [ ] The MCU acceptance test(s) for that wave pass through **speech
      only** — no flags, no syntax, no CLI-only path
- [ ] Natural-language entry point(s) shipped in the same change
- [ ] Evidence/audit laws hold (nothing fabricated, nothing unlogged)
- [ ] Hermetic tests added (suite grows; nothing red)
- [ ] Capability registered (Wave 16+), tone data-driven (Wave 17+)
- [ ] Any §2 hardening items that belong to this wave are closed
- [ ] The wave doc updated with "What Actually Shipped" + what was
      learned (the wave recipe)
- [ ] The sentence is measurably more true than before the wave

---

## 7. Sentence progress tracker

> Updated at the end of every wave. "Today" is where we are now; the
> waves column is what moves the clause.

| Clause | Today | Moved by | Done when |
|--------|-------|----------|-----------|
| **single presence** | **W15 SHIPPED** — one shared session follows you across surfaces (talk/ask/voice/web/mobile) + time-window recall + web-chat hydrate + ambient push to every surface + mobile transport | — | One session follows you across surfaces; "what did we talk about this morning" works from any surface ✅ |
| **speaks natural language** | `talk`/`ask`/voice reach the brain through ONE `resolve()` (Wave 13a); §2 hardening shipped NL entry points for security, memory, collab, and desktop (skills already had SKILL/ACCEPT) | §2 hardening (every built wave) + every new wave (Law 1) | No capability requires a flag or syntax — the MCU test passes for every feature |
| **no learning ceiling** | Facts + shadow-first skills scaffold; **Watch Me shipped** (watch → shadow skill, repetition notices, repo-context generalization) | W15→W16 (registry self-extension) | Anything demonstrable is learnable; skills generalize and auto-dispatch |
| **no task it can't pick up** | Shell/git/file/python/testing executors; **W16 SHIPPED (registry)**; **W18 SHIPPED (Claude Code hands — complex goals delegated, simple commands stay native)** | W12 (network executors), W13 (LLM decompose), W16 (registry), W18 (agentic delegation) | "figure out why the build fails and fix it" works, speech-only ✅ |
| **adapts its personality** | Depth → tone only; no explicit direction | **W17 SHIPPED** (tone-direction: persists, explainable) | "be more casual, Tony" shifts tone now, persists, explainable why ✅ |

*The sentence is the product. The waves are just the route to it.*
