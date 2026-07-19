# Friday V3 Dogfooding Run — 2026-07-14

## 0. Setup & what "wire up the LLM" meant

The LLM was **partially** wired before this run:
- `ingest` README summaries → used LLM when `FRIDAY_LLM_*` set (they were).
- `ask` query *understanding* (`understand()`) → used LLM when enabled.
- `ask` *answer synthesis* (`_synthesize()`) → **gated behind `FRIDAY_ANSWER_LLM=1`** (was `0`).

Wiring action: set `FRIDAY_ANSWER_LLM=1` in `.env` so answers are LLM-synthesized instead of raw evidence dumps. Also the original `.env` used the free `openrouter/tencent/hy3:free` model. The first trial run showed `ask_01` took **117s** — too slow for 53 commands. Per instruction, benchmarked alternatives:

| model | warm call 1 | warm call 2 |
|---|---|---|
| `openrouter/tencent/hy3:free` (orig) | ~117s (first) | — |
| `kr/claude-sonnet-4.5` | 18.0s | 1.7s |
| `kr/claude-haiku-4.5` | 36.8s (cold) | **1.3s** |

Chose **`kr/claude-haiku-4.5`** — lowest warm latency and cheapest tier, appropriate for high-volume `ask` calls. (Cold haiku 36.8s was a one-off proxy warmup; subsequent calls ~1–14s.)

Final `.env`:
```
FRIDAY_LLM_API_KEY=...
FRIDAY_LLM_MODEL=kr/claude-haiku-4.5
FRIDAY_LLM_BASE_URL=http://localhost:20128/v1
FRIDAY_ANSWER_LLM=1
```

## 1. How it was run
`bash dogfood_run/run.sh`. Each command captured to `dogfood_run/<tag>.out` with exit code + duration in `dogfood_run/summary_<ts>.tsv`. DB reset at start (`rm -f ~/.friday/friday.db`).

## 2. Results: non-ask commands

| step | command | exit | dur | actual |
|---|---|---|---|---|
| 00 | `rm -f ~/.friday/friday.db` | 0 | 0.01s | DB cleared |
| 01 | `friday ingest ~/Projects` | 0 | 46.6s | "Ingested 8 of 8 repositories (5 with LLM README summaries)." |
| 02 | `friday observe` | 0 | 5.5s | "No significant workspace changes detected." (fresh DB) |
| 03 | `friday context` | 0 | 0.27s | empty notice → "Run: friday context build" |
| 04 | `friday context build` | 0 | 0.62s | built sessions |
| 05 | `friday context` | 0 | 0.28s | context summary (sessions/active time/focus) |
| 06 | `friday sessions` | 0 | 0.30s | listed sessions |
| 07 | `friday timeline` | 0 | 0.31s | timeline entries |
| 08 | `friday knowledge` | 0 | 0.31s | list grouped by type (33 entries) |
| 09 | `friday knowledge build` | 0 | 0.65s | "Total knowledge: 33 … Created: 33" |
| 10 | `friday knowledge` | 0 | 0.27s | re-listed |
| 11 | `friday knowledge verify` | 0 | 0.33s | "Observed: 33 / Medium: 32 / Strong: 1 / Candidates needing verification: 0" |
| **12–16** | `friday knowledge explain 1..5` | **2** | ~0.3s | **FAIL: "error: knowledge not found: N"** — see Finding A |

### Finding A — `knowledge explain 1..5` are invalid commands
Knowledge IDs are **timestamp-based strings**, not sequential integers:
`2026-07-14T13:58:31.066719+00:00:project_identity:Aether`. So `explain 1` can never match. This is a real mismatch between the prescribed command list and the actual CLI contract. **Re-ran with the real first-5 IDs → all exit 0** (see `dogfood_run/explain/kx_1..5.out`). `explain` itself works correctly.

## 3. Results: `friday ask` (all 53 — exit 0 unless noted)

All `ask` commands returned exit 0 (no crashes/hangs). Duration (new haiku run):

| tag | question | dur(s) | behavior |
|---|---|---|---|
| ask_01 | What engineering knowledge have you accumulated? | 18.4 | Full synthesized prose, Confidence: Strong |
| ask_02 | What stable engineering knowledge do you have? | 8.1 | Answered |
| ask_03 | What have you learned about my engineering? | 9.1 | Answered |
| ask_04 | What do you know about my projects now? | 5.7 | Answered |
| ask_05 | What long-term engineering trends have you observed? | 7.5 | Answered |
| ask_06 | What recurring engineering habits have you learned? | 5.7 | Answered |
| ask_07 | Which technologies am I consistently investing in? | 5.6 | "React and Supabase" — but "Evidence covers only 2 of 8" |
| ask_08 | How has my engineering direction evolved? | 5.2 | Answered |
| ask_09 | What project relationships have become stronger? | 21.0 | Answered (slow) |
| ask_10 | Which knowledge is weakly supported? | 6.6 | Answered |
| ask_11 | What am I working on? | 7.1 | Answered |
| ask_12 | What have I been working on? | 8.2 | Answered |
| ask_13 | What have I been building? | 6.0 | Answered |
| ask_14 | What do you know about what I'm building? | 7.2 | Answered |
| ask_15 | What engineering knowledge do you have? | 7.0 | Answered |
| ask_16 | How has my engineering changed? | 8.9 | Answered |
| ask_17 | How have my interests evolved? | 8.1 | Answered |
| ask_18 | Which technologies are becoming more important? | 5.6 | Answered |
| ask_19 | Which technologies are becoming less important? | 4.5 | Answered |
| ask_20 | What trends are strengthening? | 4.9 | Answered |
| ask_21 | What trends are fading? | 4.4 | Answered |
| ask_22 | What has remained stable? | 6.1 | Answered |
| ask_23 | Which projects reinforce each other? | 4.5 | Answered |
| ask_24 | Which projects depend on each other? | 5.8 | Answered |
| ask_25 | Which projects influence each other? | 6.5 | Answered |
| ask_26 | Which project has become infrastructure? | 8.5 | Answered |
| ask_27 | Which projects are converging? | 14.2 | Answered |
| ask_28 | Which projects are diverging? | 4.7 | Answered |
| ask_29 | What engineering habits have you learned? | 7.4 | Answered |
| ask_30 | What engineering patterns repeat? | 5.7 | Answered |
| ask_31 | What do I consistently do? | 4.8 | Answered |
| ask_32 | What workflow keeps repeating? | 5.9 | Answered |
| ask_33 | What bottlenecks have become recurring? | 5.8 | Answered |
| ask_34 | What engineering strengths keep appearing? | 9.1 | Answered |
| ask_35 | What engineering belief have I abandoned? | 5.2 | Answered |
| ask_36 | What mistake do I keep making? | 6.1 | Answered |
| ask_37 | What am I avoiding? | 5.4 | Answered |
| ask_38 | What surprised you? | 5.2 | Answered |
| ask_39 | What changed my mind? | 4.5 | Answered |
| ask_40 | What did I learn this month? | 7.0 | Answered |
| ask_41 | What engineering philosophy do you follow? | 10.3 | Answered |
| ask_42 | Who am I becoming as an engineer? | 9.9 | Answered |
| 43 | `friday chat` (8-turn) | 41.3 | See Finding B |
| ask_44 | Explain Friday | 5.8 | **Degraded: "not enough evidence"** (Finding C) |
| ask_45 | Explain Friday V3 | 5.0 | Answered |
| ask_46 | Compare Friday and Friday V3 | 4.7 | Answered |
| ask_47 | Which project should I continue? | 6.4 | Strong recommendation w/ reasoning |
| ask_48 | What should I work on today? | 5.6 | Answered |
| ask_49 | Where is my engineering effort going? | 5.8 | Answered |
| ask_50 | What kind of engineer do I seem to be? | 7.3 | Answered |
| ask_51 | Tell me something I haven't noticed. | 14.6 | Noted MindWell commit concentration |
| ask_52 | Which project should become a platform? | 6.4 | MindWell (React/Supabase reuse w/ vivaha) |
| ask_53 | Which projects should eventually merge? | 5.8 | **Degraded: "not enough evidence"** (Finding C) |

## 4. Findings (bugs / quality issues)

### Finding A — `knowledge explain 1..5` invalid (commands in the list are wrong)
IDs are timestamp strings; `explain <int>` always returns exit 2. Fix: use real IDs (or add an integer alias / list-index). Re-ran corrected → exit 0.

### Finding B — `friday chat` follow-up resolution breaks on short prompts
In the 8-turn chat, the first answer was good. But follow-ups "How confident are you?", "Why?", "Which evidence supports that?" returned:
> "I don't have enough evidence to answer that. The Evidence block shows I'm based on 0 of 8 repositories…"

The short follow-ups did **not** resolve against the previous exchange (`resolve_followup`) and fell through to a retrieval that returned empty evidence — then the LLM honestly said "no evidence." So multi-turn context threading is unreliable for pronoun/ellipsis follow-ups in chat. (Note: `ask` single-shot works; the defect is in follow-up handling / the `prev` handoff in `cmd_chat`.)

### Finding C — Retrieval under-fetches for synthesis; several "big-picture" asks degrade
Many answers report "Evidence covers only N of 8 repositories" (ask_07: 2/8, ask_51: 1/8). Two asks fully degraded to "I don't have enough evidence":
- `ask_44` Explain Friday — LLM said evidence "conflates Friday with system package directories (pip)" and asked for a README. (Friday has no README → `identity.explain_project` had nothing to ground on.)
- `ask_53` Which projects should merge — evidence listed themes but not per-repo purposes/integration, so LLM refused.

Root cause: the retrieval step returns a thin evidence block for open-ended/comparative questions, and the LLM is (correctly) refusing to hallucinate rather than answer. This is *safe* behavior but produces weak answers where a human would expect synthesis across the knowledge base.

### Finding D — `friday observe` on a fresh DB says "no changes" (expected)
First `observe` after reset correctly reports no prior baseline. Subsequent runs would diff. Not a bug.

## 5. Performance
- **Slowest:** `ingest` (46.6s — 5 LLM README summaries over network), `ask_09` (21.0s), `ask_51` (14.6s), `ask_27` (14.2s), `43_chat` (41.3s total for 8 turns).
- First haiku call was cold (36.8s proxy warmup); steady-state asks 4–10s.
- `context`/`knowledge`/`sessions`/`timeline` are all sub-second (pure DB reads).

## 6. Conclusion
LLM is fully wired: ingest summaries + query understanding + answer synthesis all live. The pipeline ran end-to-end with **zero crashes**. Usable output quality is good for project-specific and trend questions; weaknesses are (A) the `explain` integer-ID assumption in the command list, (B) chat follow-up context loss, and (C) thin retrieval on big-picture/comparative questions causing safe-but-weak refusals.

## 7. Artifacts
- `dogfood_run/run.sh` — driver
- `dogfood_run/summary_<ts>.tsv` — exit code + duration per step
- `dogfood_run/<tag>.out` — full stdout per command
- `dogfood_run/explain/kx_1..5.out` — corrected knowledge explains
- `dogfood_run/43_chat.out` — full chat transcript
