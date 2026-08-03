# Wave 13 — The Thinking Core (LLM reasoning) ✅ SHIPPED

> **Why it serves the sentence:** *understands everything* — the single
> highest-leverage change in the whole plan. Built 2026-08 on top of
> Waves 9–12 (deterministic reasoning, memory, research, ambient push).

---

## 1. The design (Law 6: enhances, never gates)

Wave 13 adds **one LLM provider** to `reasoning/providers.py` that
synthesizes the deterministic answer engine's output. The architecture
stays honest by construction:

- **Deterministic providers are the floor.** The Wave 9 registry
  (`identity/status/activity/mission/memory/conversation`) gathers
  evidence and answers exactly as before. No LLM → byte-identical
  behavior. The floor never changes.
- **LLM synthesis is the ceiling.** After the engine picks the best
  deterministic answer, `llm_provider` asks the LLM to rewrite it as a
  natural, conversational synthesis — **across the same evidence**.
  Citations are attached verbatim to the enhanced answer; nothing is
  added, nothing is dropped.
- **Never fabricates.** The enhanced answer carries the original
  `Evidence` list unchanged, so the judgment pass (`validate()`) still
  guarantees *evidence or silence*. An evidence-less answer ("I don't
  know yet") is **never sent to the LLM** — the honest unknown stays
  real.
- **Optional by default.** Opt-in is explicit: the `FRIDAY_V4_LLM` env
  var (truthy) or an injected client (surfaces/tests pass their own).
  LLM down, network failure, empty/garbage output → `None` → the
  deterministic floor stands. Never a crash, never a hallucination.

### The client

The provider reuses the **existing `nlu.LLMClient`** — the pure-stdlib
(`urllib`) OpenAI-compatible client for the local **9router proxy**
(`localhost:20128/v1`, configurable model) with the SSE-trailer quirk
already handled at the client boundary. Config via `FRIDAY_V4_LLM_URL` /
`FRIDAY_V4_LLM_MODEL` / `FRIDAY_V4_LLM_KEY`. One client, one command
language, one entry point — voice, `friday4 talk`, `friday4 ask`, and
web chat all inherit it automatically.

### Conversation capability

`friday4 ask` is now conversation-capable:

- Recent exchanges are pulled (`db.recent_exchanges`, oldest first) and
  threaded into the LLM synthesis prompt as "Recent conversation:" —
  follow-ups like *"and the tests?"* resolve with context.
- The Q&A is **logged** to the conversation log (surface `ask`), so the
  Wave 9 `conversation_provider` ("what did we talk about?") sees asks,
  and the next ask sees this one as history.
- The same history threading is wired into `nl_router._ask_response`,
  so **voice and `friday4 talk` follow-ups get identical context** — one
  entry point, per the Wiring Law.

---

## 2. Wiring table (the Wiring Law)

| Consumer | Status | How |
|---|---|---|
| `friday4 ask` | ✅ | `cli_ask.cmd_ask` — passes history + logs the Q&A exchange |
| `friday4 talk` (NL router) | ✅ | `nl_router._ask_response` — passes `self._recent_history()` |
| Voice (`VoiceRouter`) | ✅ | routes ASK → `nl_router` → same path (one entry point) |
| Web chat (`/api/talk`) | ✅ | uses `nl_router`/reasoning — inherits automatically |
| Reasoning providers | ✅ | `llm_provider` in `providers.py`; engine applies post-pass |
| Daemon schedules | — | none needed (LLM is request-driven, not time-driven) |
| Briefings | — | remain deterministic (same evidence → same report) by design |
| CLI surface | ✅ | `friday4 ask` gained conversation capability (no new flags) |

---

## 3. MCU test

> "What's the deal between vivaha and MindWell?" → synthesized, cited,
> ranged — through `friday4 talk`, through voice.

With `FRIDAY_V4_LLM=1` (or a configured proxy), ASK/RESEARCH answers
come back as natural LLM prose **with the evidence citations still
listed** by the surface. Without the LLM, the same utterance gets the
honest deterministic research answer. Both paths are tested hermetic.

---

## 4. What actually shipped

- `reasoning/providers.py` — `llm_provider(question, conn, best=…,
  history=…, llm=…)` + `_llm_opted_in` / `_clean_llm_text` /
  `_llm_system_prompt`; exported from `reasoning/__init__.py`.
- `reasoning/engine.py` — `answer(…, history=None, llm=None)`; the LLM
  post-pass runs over the best deterministic answer; any failure keeps
  the floor.
- `cli_ask.py` — `_recent_history()` + `_log_exchange()`; `cmd_ask`
  threads history and logs the Q&A.
- `nl_router.py` — `TextCommandHandler._recent_history()`; ASK intents
  pass conversation context (voice/talk/web inherit).
- `tests/test_wave13_thinking_core.py` — 17 hermetic tests: opt-in
  gating, floor preservation on every failure mode, citation retention,
  "I don't know" never sent to the LLM, history threading, CLI exchange
  logging. FakeLLM, no network, tmp_path DBs.

## 5. What we learned

- **The post-pass beat the registry slot.** Adding the LLM as a
  competing provider in `PROVIDERS` would run it for *every* question
  type and risk it winning on confidence over evidence it didn't gather.
  Applying it over the chosen deterministic best keeps the floor
  untouched and the citations identical — a cleaner reading of Law 6.
- **The honest unknown is a feature, not a bug.** "I don't know yet"
  answers are evidence-less by definition; sending them to the LLM would
  invite fabrication. The engine returns before the post-pass, and a
  test locks that in (`test_no_evidence_never_asks_llm`).
- **Fence-stripping matters.** Local proxies occasionally wrap
  completions in ``` fences; `_clean_llm_text` handles it at the
  boundary so the surfaces never see markdown noise.

---

*Next: Wave 13a (ONE NLU Point) close-out and Wave 14 (Watch Me).*
