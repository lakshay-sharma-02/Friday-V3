# Wave 19 — Polish, Scale & Dogfood ✅ SHIPPED (2026-08)

> **The directive:** *the sentence, proven under real use.* Waves 1–18
> built the surfaces, the safe core, and the sentence clauses; Wave 19
> is where the sentence has to *be true in daily use* — single
> presence, natural language, learning ceiling gone, any task,
> personality that adapts. Everything after this is refinement, not
> building.
>
> Governance: [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) —
> the five acceptance tests, the definition of done.

---

## Why this wave

The MASTER_PLAN exit condition for Wave 19:

> **Exit condition:** the sentence is *true in daily use* — single
> presence, natural language, learning ceiling gone, any task,
> personality that adapts.

That is not proven by more features. It is proven by:

1. **An instrument** — the five MCU acceptance tests, runnable, green,
   hermetic, driven through natural language only (slice 1, shipped).
2. **A bug sweep** — the five most annoying bugs found and fixed
   (slice 1 shipped the first two: the deterministic research
   classification gap + the correlate-pair extraction bug; slice 2
   shipped the other three: the "with … and …" research pair, failed
   commands hiding *why*, and "how's it going" answering "I don't know
   yet" with no mission).
3. **The dogfood loop** — Friday used for Friday's own engineering
   (this wave doc, the harness, the bug sweep, the benchmarks — and
   slice 3 found the benchmark tool itself reporting false `n/a`s).
4. **The floor** — benchmarks, docs site, installer, migration guide,
   **1085** hermetic tests.

---

## Slice 1 — The MCU acceptance harness ✅ SHIPPED

### The instrument

`tests/test_mcu_acceptance.py` — the five proof moments
(MCU_FRIDAY_STANDARD.md §3), each driven through the **shared NL brain**
(`nl_router.TextCommandHandler` — the exact path talk / voice / web /
mobile route through), no flags, no syntax, hermetic (tmp DBs, fake
`claude` binaries, seeded repos):

| Moment | Sentence | Verified |
|---|---|---|
| 1 · Mission shepherding | "ship the auth refactor by Friday" → mission created → manual step completed → "how's it going" names the next step (and honestly says "no mission in flight" when there is none) → "replan this mission" re-decomposes through Claude Code and reports "plan changed because…" | ✅ 6 tests |
| 2 · Workflow copying | "watch me do this" → captured → "learn this" → shadow skill → daemon sweep verifies → operator approves promotion → dispatch offers next step → "yes, run it" runs it through the gate | ✅ 2 tests |
| 3 · Deep reasoning | "what's the deal between X and Y" (and the "with … and …" variant) → researched, evidence-cited, ranged estimate (seeded hermetic repos); a bare research lead never hijacks a stronger intent | ✅ 4 tests |
| 4 · Adaptive identity | "be more casual, Tony" → tone shifts → persists → "why do you talk that way" explains it; "be yourself again" resets | ✅ 2 tests |
| 5 · Capability composition | "figure out why the build fails and fix it" → decomposed onto the Claude Code CLI through the gate → audited → result surfaced (and honest when claude is absent) | ✅ 2 tests |

**11 hermetic tests.** This is the instrument the exit condition is
measured by: every future wave re-runs it, and the sentence stays
proven.

### Bug 1 — the deterministic research classification gap (fixed)

`"what's the deal between X and Y"` — the MCU deep-reasoning sentence —
scored ASK ("what" + "is" = 2) above RESEARCH ("what's the deal" = 1)
in the deterministic NLU fallback, so on a no-LLM machine the answer
was the honest but useless "I don't know yet" instead of a researched
estimate. The LLM path classified it correctly; the offline path never
could.

**Fix:** a research-lead tie-break in `nlu/intent.py`'s fallback (same
pattern as the desktop / deny / memory tie-breaks — fallback only, the
LLM still decides in production). `"what's the deal between X and Y"`,
`"what is the deal between …"`, `"… vs …"` / `"… versus …"`, and
"integration cost between …" now route to RESEARCH deterministically.

### Bug 2 — the correlate-pair extraction bug (fixed)

The research router split `"analyze X vs Y"` on a marker list that hit
`" between "` **before** the pair separator, then stripped the research
lead off the first operand — so `"what's the deal between X and Y"`
became `correlate("", "Y")`. The lead was never an operand.

**Fix:** `nl_router._extract_pair` — `"between X and Y"` splits on
"between" first and the pair on "and", never treating the lead as an
operand; `"vs"` / `"versus"` / plain `"and"` keep working.

### Why these two bugs first

They are the two things that made the *exact* MCU sentences fail
offline. The harness caught them because it tests the concrete
sentences, not paraphrases (the lesson from Wave 16's ordering bug).

---

## Slice 2 — the remaining three annoying bugs ✅ SHIPPED (this change)

The sweep: a battery of ~35 real utterances driven through the shared
NL brain (`TextCommandHandler`) on a fresh DB + `friday4 doctor`. Three
truly annoying behaviors surfaced, all fixed with hermetic regression
tests:

### Bug 3 — "what's the deal **with** X and Y" didn't research

The MCU deep-reasoning sentence has a natural "with" variant
("what's the deal with vivaha and MindWell?") that the slice-1 tie-break
didn't cover — it classified ASK and answered "I don't know yet". Only
the "between … and …" / "… vs …" forms researched.

**Fix:** `nlu/intent.py` — the research pair tie-break now fires when
"with … and …" BOTH appear (a bare lead like "what's the deal with my
security scan" still scans — regression-guarded). `nl_router._extract_pair`
understands the "with" separator: the lead ("what's the deal") is never
an operand, "compare X with Y" splits cleanly too.

### Bug 4 — failed commands hid *why*

"git status" in a non-repo directory replied "That didn't work —
failed." with the real reason ("fatal: not a git repository…") buried.
The failed path dropped the executor's combined output.

**Fix:** `nl_router._run_execution` surfaces the first line of the
output/stderr on failure — "That didn't work — failed. fatal: not a
git repository…" (claude failures already did this; now every
executor does).

### Bug 5 — "how's it going" with no missions said "I don't know yet"

With zero missions, the MISSION provider returned no evidence, so the
honest-but-useless "I don't know yet" came back — even though Friday
*did* query the DB and *knows* there's no mission.

**Fix:** `reasoning/providers.mission_provider` answers the empty case
with real state (evidence `v4.missions`: "no missions in flight") and
points at how to start one: "You don't have a mission in flight right
now — say 'ship the auth refactor by Friday' and I'll plan one."

### Why these three

The slice-2 sweep rule: an utterance that a user would *naturally say*
must do what it looks like it does. All three were natural phrasing
hitting wrong/opaque outcomes offline, and all three now have hermetic
tests (harness + `test_nl_router` + `test_reasoning`).

---

## Slice 3 — dogfood, benchmarks, docs site, installer ✅ SHIPPED (this change)

### Benchmarks — and two silent `n/a` bugs fixed

`tools/benchmarks.py` now measures **all seven** daily-use operations:

the old run printed `n/a` for `v4.reasoning_answer` and
`v4.collab_merge_20` — which the tool reports for *failed* measurements
(exceptions are caught, never raised). Both were real bugs in the
benchmark itself:

- `reasoning_answer` called `answer(conn, "…")` — the engine signature
  is `answer(question_text, conn=None, …)`, so the sqlite connection
  was passed as the question text and `AttributeError`ed silently.
- `collab_merge` fed the CRDT the *display* entry shape
  (`source`/`subject`/`timestamp`) instead of the wire shape
  (`id`/`peer_id`/`ts`/`payload`/`deleted`) — `state()` raised
  `KeyError('ts')` silently.

Both fixed with a comment so the shapes stay honest. Current floor
(best-of-N ms): db_connect 0.31, mission roundtrip 1.79, reasoning
answer 1.11, research analyze 0.21, collab merge 0.46, ambient publish
1.01, security scan 367.72 (fixture).

### Docs site — 23 pages, zero broken links

`tools/build_docs_site.py` → `site/` writes the full docs corpus
(index + every `docs/*.md`), verified: 23 pages, index links all
resolve, and the Wave 19 page renders the slice close-outs.

### Installer — verified end-to-end, one gap fixed

`install.sh` verified in a throwaway venv (`--venv /tmp/...`): venv
created, editable install succeeds, `friday4 doctor` green, `friday4
talk "hello"` answers, `friday4 status` degrades gracefully for
disabled subsystems. **Gap fixed:** `install.sh` lacked the executable
bit (`-rw-r--r--`) while the docs say `./install.sh` — now `-rwxr-xr-x`,
and `./install.sh --help` works.

### Migration guide — claims verified against the real CLI

Every command the guide promises exists and matches: `friday4 talk`,
`ask`, `research analyze/correlate/briefing/report`, `execute
shell/git/file/python/testing/ssh/claude`, `daemon start/status/stop`,
`web`, `memory store/recall/forget/list/status`, `security scan`. The
V3→V4 mental model (read-only bridge, `v4.db` separate from
`friday.db`) matches the code.

### Bonus find — a test hermeticity bug the smoke test exposed

The installer smoke test (`friday4 talk "hello"` in the fresh venv)
popped a real shared session into `~/.friday/v4.db`, which flipped a
previously-green Wave 15 test: `test_conversation_state_read_only_never_creates`
called `dashboard.conversation_state()` **without** pointing `db.connect`
at its tmp DB (its sibling test does), so it read the real home DB — a
latent hermeticity violation that only passed because the home DB
happened to be empty. Fixed to follow the sibling's convention; the
wave's "no real ~/.friday" promise now holds for every test in the
file. (The dogfood loop catching a test that reads real state is
exactly why slice 3 exists.)

### Suite

**1085 hermetic tests green** (the 900+ floor was passed long ago).

---

## Definition of done for Wave 19

- [x] The five MCU acceptance tests pass through **speech only** —
      the harness (slice 1) is the instrument; voice surfaces route
      through the same `nl_router` brain it drives (voice uses
      `TextCommandHandler` directly — the exact path the harness
      drives).
- [x] The five most annoying bugs found and fixed (slice 1 shipped
      two; slice 2 shipped the other three).
- [x] Benchmarks / docs site / installer / migration guide verified.
- [x] Suite ≥ 900 hermetic tests, nothing red (**1085**).
- [x] The sentence is measurably true in daily use (tracker, §7 — all
      five clauses ✅).

---

## Close-out (the wave recipe)

**What was learned:** the exit condition is an *instrument*, not a
feature list. The harness (slice 1) caught real classification bugs in
the exact MCU sentences; the sweep (slice 2) found natural phrasings
hitting wrong/opaque outcomes; slice 3 found the *tools themselves*
reporting false negatives (`n/a` = failed) — the dogfood loop is what
makes the floor honest. Everything after this is refinement, not
building.
