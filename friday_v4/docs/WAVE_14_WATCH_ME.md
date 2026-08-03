# Wave 14 — Watch Me (demonstration capture) ✅ SHIPPED (2026-08)

> **The directive:** *no learning ceiling* — **"copy any workflow."** The
> operator demonstrates something once ("watch me do this"); Friday
> watches the audit trail, parameterizes the demonstration into a skill,
> and generalizes it (repo + command context) so it matches the real
> workflow — never a literal replay. Repetition is *noticed* before the
> operator asks (the MCU "I noticed you do this every time" moment).
>
> Parent waves: [Wave 10 — Memory & Identity](WAVE_10_MEMORY_IDENTITY.md)
> (shadow-first skills), [Wave 13a — ONE NLU Point](WAVE_13A_ONE_NLU_POINT.md)
> (the SKILL intent rides the single parser). Governance: [THE MCU FRIDAY
> STANDARD](MCU_FRIDAY_STANDARD.md) — Law 4 (never crash), Law 6 (skills
> enhance, never gate), Law 7 (operator consent).

---

## 1. The design

### The audit log IS the demonstration record

"Watch me" = **tag a window on the audit trail.** Every executed action
is already logged (what, when, result) by the execution layer. A watch
just records a start time + context; `stop()` parameterizes everything
executed inside that window into a skill. No new capture pipeline, no
recording of keystrokes — the demonstrated *work* is the demonstration.

```
watch me do this ──► start_watch(name, context=repo)     # tag a window
   (operator works — every action is audited as usual)
learn this ──────► actions_between(start, now)           # the demonstration
                    └──► generalized steps (repo context, dupes collapsed)
                           └──► shadow skill (confidence 0 — never executes)
```

### `skills/watcher.py` — WatchRecorder

| Method | Behavior |
|--------|----------|
| `start(name, note, context)` | Opens a watch (single-active enforced at the DB layer; a new watch closes the previous). |
| `stop(watch_id, name)` | Closes the watch, generalizes captured actions into a **shadow** skill (repo context, consecutive-dup collapse), links `watches.skill_id`. |
| `capture(watch_id)` | The actions recorded so far (read-only). |
| `active()` | The live watch, if any. |

**Generalization (wave-14 spec):** each step carries the repo it ran in
(`cwd` basename). `ShadowExecutor`/`SkillDispatcher` now match on
**context — repo + command + trigger** — not literal replays: `pytest -q`
in `friday_v4` is the *same step* wherever it runs in that repo, and is a
*different context* in another repo. Steps without a repo (wave-10
skills) keep the old type/command match (back-compat).

**Safety (wave-10 law, preserved):** everything formed starts in
`shadow` with confidence 0 — inert. Promotion still requires N shadow
matches + the operator's explicit `friday4 skills promote` approval.
Nothing executes while watching or learning.

**Reuse-by-name, honestly:** `stop()` reuses an existing skill by name
ONLY when its first step matches what was just demonstrated (same
workflow → no duplicate). A same-name skill with *different* steps never
swallows the fresh demonstration — the new skill gets a versioned name
(`deploy-2`). The operator's just-performed work is never discarded.

### `skills/noticer.py` — RepetitionNoticer

The MCU five-moment #2: while the operator works, Friday notices a
repeated ordered sequence in the audit log and **offers** to form a
skill — it does not form one silently.

- Reuses `ReplayExecutor.find_patterns` for the repeated sequences.
- Skips patterns already covered by a skill (first-step signature match)
  — never re-offers what Friday already knows.
- **Pure read:** noticing forms nothing. The operator accepts (via
  `friday4 skills noticed` → `watch`, or the NL "learn this"), then the
  watch path forms the skill.

### NL paths — through the ONE NLU point

`Intent.SKILL` is added to the single parser (`nlu/`) — LLM-first, with
deterministic fallback words (`watch me`, `learn this`, `stop watching`,
…). `nl_router` routes it:

| Utterance | Action |
|-----------|--------|
| "watch me do this" | `_skill_start` — opens a watch ("I'm watching — go ahead.") |
| "learn this" / "stop watching" | `_skill_stop` — forms the shadow skill |
| "what did you learn" | ASK → reasoning `skills_provider` (cites real skills) |
| "show me your skills" | ASK → `skills_provider` |
| "yes, run it" / "go ahead" | `Intent.ACCEPT` → `_accept_response` — runs the matching dispatch suggestion's next step through gate → sandbox → audit; a multi-step suggestion becomes a **supervised mission** (first step runs now, the rest is tracked) |

"what did you learn" stays an **ASK** intent (the fallback's ASK words
win the tie), so it routes to the reasoning engine's `skills_provider`
and is answered from the real skills registry — the Wiring Law's
"reasoning cites the new state" checkbox.

---

## 2. Wiring (the Wiring Law)

| Consumer | Wiring |
|----------|--------|
| `nl_router.py` | `Intent.SKILL` → `_skill_response` (start/stop/summary) via `TextCommandHandler` — voice, `friday4 talk`, and web chat all reach it through the shared handler. |
| `cli_skills.py` | `friday4 skills watch [name]` · `watch-stop [name]` · `noticed` · `dispatch` |
| `voice/router.py` | `proactive_notify` offers the matching skill suggestion ("want me to run X next?"); "yes, run it" → ACCEPT → spoken result |
| `daemon.py` | `SkillLearner.sweep_once` runs `RepetitionNoticer` **before** `learn()` — offers reflect what isn't yet covered, then shadow skills form; offers surface in `last_report` (`offers`, `offer_lines`) for status/briefing. **`DispatchOfferer`** — a new daemon component that periodically offers the top dispatch suggestion on context match (desktop notify + durable `dispatch` ambient event, deduped); never executes. |
| `reasoning/providers.py` | `skills_provider` registered for `QuestionType.SKILLS` — "what did you learn" answers cite `v4.skills`. |
| `db.py` | Migration v5: `watches` table (single-active enforced). |

Checklist: every entry point that should reach SKILL does (voice / CLI /
web via the shared handler) ✅ · the daemon runs the noticer on its
schedule ✅ · reasoning cites real skills ✅ · a CLI command exposes the
layer (`friday4 skills watch/…`) ✅ · wiring tests hermetic ✅.

---

## 3. What actually shipped (close-out)

1. **`watches` table (db migration v5)** — `start_watch` /
   `end_watch` / `get_watch` / `active_watch` / `list_watches` /
   `actions_between`; single-active-watch enforced; `end_watch` links the
   formed skill. Stale `test_db` schema assertions (v3) updated to v5 +
   the `watches` table — the pre-existing `test_db` ×3 failures are gone.
2. **`WatchRecorder`** — explicit "watch me" capture → generalized shadow
   skill (repo context, consecutive-dup collapse, honest reuse-by-name).
3. **`RepetitionNoticer`** — "I noticed you do this every time" offers;
   pure read; skips already-learned patterns.
4. **Generalization** — `_step_matches` (shadow + dispatch) enforces repo
   context when a step carries one; back-compat for context-free steps.
5. **SKILL intent through the ONE point** — `Intent.SKILL` + fallback
   words + LLM prompt (`skill` in the allowed set) + resolver target
   threading; LLM interpretation still wins over keywords (tested).
6. **`nl_router._skill_response`** — watch / learn / stop / summary NL
   paths on every surface via `TextCommandHandler`.
7. **Reasoning** — `QuestionType.SKILLS` + `skills_provider` (ASK cites
   real skills; no skills → honest "I don't know yet").
8. **CLI** — `friday4 skills watch`, `watch-stop`, `noticed`, `dispatch`
   (next-step suggestions on context match, read-only).
9. **Daemon** — `SkillLearner` runs the noticer before `learn()` and
   surfaces offers in `last_report`.
10. **Tests** — `tests/test_wave14_watch_me.py` (33 tests): db lifecycle,
    watcher generalization + reuse-honesty, noticer offers + pure-read,
    NLU SKILL fallback + LLM-still-wins, router watch→learn→summarize,
    reasoning provider, CLI commands, daemon offers + never-executes.
    Close-out: `tests/test_skill_accept_nl.py` (11 accept tests + 5
    mission-integration tests) — ACCEPT classification, gate-through
    execution, NEVER denial without force, multi-step accept → supervised
    mission, voice offer + spoken result. `tests/test_daemon.py` adds
    `DispatchOfferer` tests (offers on match, dedupe, never-executes,
    daemon wiring).

**MCU test (five-moment #2):** "Friday, watch me do this" → operator
runs a workflow → "learn this" → shadow skill formed with repo context;
shadowed on the next matching workflow; promoted only with approval;
auto-dispatched on later matches. ✅

---

### NL accept — the dispatch offer closes its loop (close-out)

`Intent.ACCEPT` (“yes, run it” / “go ahead”) completes the dispatch
story: Friday *offers* the next step of a matching promoted skill
(voice `proactive_notify`), the operator says yes, and `_accept_response`
runs that next step through the real pipeline — gate → sandbox → audit.
The operator's yes **is** the CONFIRM approval (passed as a pre-approved
`confirm_fn`); a NEVER-level next step (git push) is still **denied**
without an explicit `force` override — a bare yes never escalates
anything. All surfaces share it (one `nl_router`, voice/web/CLI).

### Dispatch → mission — multi-step acceptance is supervised (close-out)

When a dispatch suggestion has **2+ remaining steps**, accepting it no
longer fires-and-forgets: `_accept_response` forms a **mission** from the
remaining steps (`MissionEngine.create` with an explicit plan), starts
it, and executes the first step *now* through the gate (same
confirm/force semantics — the operator's yes is the CONFIRM approval,
NEVER stays force-only). The mission persists: status, briefings, and
the progress feed track it, adaptation stays explicit ("plan changed
because…"), and a denied first step saves the mission for an explicit
`--force` rerun. Single-step suggestions keep the direct execution path
(no wrapper).

### Auto-dispatch on idle — the daemon offers skills without being asked

The `DispatchOfferer` daemon component closes the *offer* side of the
loop on a schedule: every `dispatch_interval` (default 1 h, `--no-dispatch-offer`
to disable) it re-checks the current context and, when a promoted skill
matches and the offer hasn't been surfaced recently (bounded dedupe), it
raises a desktop notification **and** publishes a durable `dispatch`
event on the ambient bus (the web feed / SSE see it). It never executes
anything — the operator's "yes, run it" still flows through the ONE NLU
point → the gate → execution (or a mission). Wired like every daemon
component: config flag, build/start, status component (`dispatch`),
shutdown stop.

---

## 4. What we learned

- The audit-log-as-demonstration design cost almost nothing: no new
  capture machinery, and every safety property (shadow-first, never
  executes) came from the wave-10 layer for free.
- **Noticer-before-learn ordering matters:** running `learn()` first
  would silently pre-empt every offer — the "I noticed" moment must be
  surfaced before the pattern becomes a skill, or it never exists.
- **Honest reuse matters more than dedup:** silently linking a fresh,
  different demonstration to a same-name skill would throw away the
  operator's work. The first-step-match rule + versioned-name fallback
  keeps dedup without data loss.
- The one remaining pre-existing red (`test_cli_collab` conflicting
  subparser "research") is unrelated to this wave and predates it.
