# The MCU FRIDAY Standard

> The constitution. Every wave, feature, and commit is judged against
> this document. If a build doesn't move Friday toward *this*, it doesn't
> get built — regardless of how clever it is.
>
> **The standard in one sentence:** MCU FRIDAY is a single presence that
> speaks natural language, has no learning ceiling, no task it can't pick
> up, and adapts its personality to you.
>
> **Route companion:** [**THE FRIDAY MASTER PLAN**](MASTER_PLAN.md) turns
> that sentence into waves (11 → 18) — the *how*; this Standard is the
> *judge* of everything built along the route.

---

## 1. What MCU FRIDAY Is

Tony doesn't type commands. He says things. And FRIDAY:

- **Learns anything** — remembers what you tell her, what you do, what
  you mean. Not transcripts — *understanding*.
- **Copies any workflow** — watch once, replay, verify, own it. "Watch
  me do this" is a feature, not a dream.
- **Understands everything** — "what's the deal between vivaha and
  MindWell?" gets a *researched, cited, synthesized* answer. Not "I don't
  know yet."
- **Does anything** — any task expressible in the world: code, shell,
  files, git, remote machines, your browser, your phone. With a safety
  floor, never a capability ceiling.
- **Extends itself** — new abilities are *learned and registered*, not
  coded. Friday grows without you writing Friday.
- **Tones itself** — "be more casual, Tony" shifts her personality this
  session and keeps it. Identity is adaptive, explicit-consent, never
  hardcoded.
- **Anticipates** — researches before you ask, briefs you before you
  wonder, interrupts only for what matters.
- **Is one presence** — the same Friday on voice, in the terminal, on
  the web, on your phone. Same memory, same context, same voice.

---

## 2. The Ten Laws (build gates)

Every feature must satisfy every law. A feature that breaks a law is
rejected; a wave that ships without satisfying all ten is not MCU level.

| # | Law | Meaning | Current state |
|---|-----|---------|---------------|
| 1 | **Natural language is the only surface** | Users say things; Friday does them. CLI exists for ops/debug only — never the product surface. `friday4 talk "…"` is the product; `friday4 execute` is a debug hatch. | 🟡 NLU + NL router shipped; many capabilities still CLI-only (memory, skills, collab, desktop) |
| 2 | **No learning ceiling** | Anything demonstrable is learnable: memory forms *models* (propositions + provenance + confidence), skills form from demonstration (shadow → verify → promote), and both generalize to new contexts. | 🟡 Facts + shadow-first skills exist; learning is manual and narrow — no "watch me" capture, no generalization |
| 3 | **No answer without evidence** | Every answer cites its ground truth; empty evidence → "I don't know yet". Never fabrication. | 🟢 Reasoning providers + evidence engine shipped |
| 4 | **No action without audit** | Every action: gate → sandbox → audit → undo where possible. | 🟢 Execution layer shipped |
| 5 | **Understands, not records** | Memory stores *meaning* — what you prefer, why, when it matters — not transcripts. Transcript is raw material, not memory. | 🟡 Memory stores verbatim statements + facts; no meaning extraction, no inference |
| 6 | **Reasoning, not lookup** | Answers synthesize across evidence (research, cross-project, impact, synthesis). Deterministic providers are the floor; LLM synthesis is the expected ceiling. | 🔴 Deterministic providers only; zero LLM; no research/synthesis layer |
| 7 | **Self-extension** | Every capability is registered (capability registry), so Friday can compose, learn, and add abilities without code changes. | 🔴 No registry; every new capability is hand-written Python |
| 8 | **Adaptive identity** | Tone, verbosity, and persona shift with explicit direction ("be more casual") and relationship depth — and persist. Never hardcoded. | 🟡 Relationship depth → tone exists; no explicit tone-direction, no persistence of style requests |
| 9 | **Proactive before reactive** | Friday researches before asked, briefs before wondered, interrupts only for what matters. Polling is the enemy; push is the standard. | 🔴 Everything polls; no ambient push, no briefings |
| 10 | **One presence, every surface** | Same memory, same context, same conversation across voice / terminal / web / desktop / mobile. | 🟡 Same DB everywhere; no cross-surface continuity of context |

**Never-simplify-away items** (the floor that stays): hermetic tests,
never-crash, degrade silently, V4-is-the-product, V3 read-only bridge.

---

## 3. The MCU Test (admission gate)

Before any feature ships, ask: **"would Tony say 'that's my FRIDAY' — or
'that's a CLI tool with extra steps'?"**

If a human must type a command, use a flag, read docs, or remember a
syntax — the feature is not done. The natural-language path must be
*the* path, tested end-to-end, in the same change as the capability
(Wiring Law).

### The five proof moments (MCU acceptance tests)

A wave is "MCU level" only when these work through **natural language
only**, no flags:

1. **Mission shepherding**
   `"Friday, ship the auth refactor by Friday"` → mission created →
   Friday proposes next steps, tracks progress, reports blockers, adapts
   when reality changes, reports "plan changed because…".

2. **Workflow copying**
   `"Watch me do this"` (or Friday noticing a repeated pattern) → skill
   formed → shadowed → verified → promoted → auto-dispatched when the
   context matches next time. Promotion requires approval. Failure
   demotes.

3. **Deep reasoning**
   `"What's the deal between vivaha and MindWell?"` → a researched,
   cited, ranged answer with confidence — because Friday did the work
   *before* being asked (cached research), not on the spot.

4. **Adaptive identity**
   `"Be more casual, Tony."` → tone shifts this session, persists, and
   Friday can tell you *why* she talks the way she does. `"Call me
   Lakshay"` already works — this is the same law extended to style.

5. **Capability composition**
   `"Figure out why the build fails and fix it."` → Friday decomposes,
   researches, executes through the gate, verifies, and reports with
   evidence — composing abilities it already has, none of which were
   written for that specific sentence.

---

## 4. Honest gap map (today)

| Capability | MCU level | Today | Gap |
|-----------|-----------|-------|-----|
| NL surface | Speak-only | `talk` NL + voice; rest CLI | Law 1: all capabilities need NL paths |
| Memory | Understands | Stores verbatim + facts | No meaning extraction, no inference, no forgetting-by-model |
| Skills | Watches → owns | Shadow-first scaffold, empty | No demonstration capture, no generalization, nothing formed |
| Reasoning | Synthesizes | Lookup with citations | No LLM, no research, no synthesis, no cross-project |
| Identity | Adapts on request | Depth → tone | No explicit tone-direction, no persistence |
| Proactivity | Researches before asked | Polls, suggests | No push, no briefings, no pre-research |
| Continuity | One presence | Same DB | No shared context across surfaces |
| Self-extension | Grows itself | None | No capability registry |
| Safety floor | Non-negotiable | ✅ solid | Keep |

**The honest verdict:** today's Friday is a well-built, safe, honest
*operator's assistant* — a shell with a working brain stem. The MCU
standard is the gap between "what's built" and Laws 1, 2, 5, 6, 7, 8, 9.

---

## 5. The path to MCU level (in order)

Order is deliberate — each step unlocks the next.

1. **LLM reasoning (Law 6 first)** — one LLM provider in
   `reasoning/providers.py` that *enhances* deterministic answers:
   synthesize across evidence, keep citations, keep "I don't know".
   Single highest-leverage change; everything else builds on it.
2. **Wave 11: research/synthesis/briefing/ambient (Laws 6, 9)** —
   cross-project analysis, deterministic cited reports, morning/evening
   briefings from real state, in-process event bus + durable queue
   (push replaces polling). The "researched before asked" layer.
3. **Demonstration capture (Law 2)** — "watch me do this": record an
   operator demo through the audit log, form a parameterized skill,
   shadow, promote. Fills the empty skills system.
4. **Meaning memory (Law 5)** — memory extracts propositions
   (preference, project, person) from conversations with explicit
   consent; identity answers draw on *models*, not transcripts.
5. **Capability registry (Law 7)** — every executor + skill + provider
   registered with natural-language intents; Friday composes them. This
   is what makes "figure out X" possible.
6. **Adaptive tone (Law 8)** — explicit tone-direction persists;
   persona profile carries style, not just facts.
7. **One presence (Law 10)** — shared context session across surfaces;
   mobile push via the Wave 11 queue.
8. **Wave 12 Polish** — benchmarks, installer, docs site, dogfooding.

Each step must land with its NL path (Law 1 + Wiring Law) in the same
change.

---

## 6. Definition of done for every future build

A build is done when all of the following are true:

- [ ] The natural-language path works end-to-end (no flags, no syntax)
- [ ] No answer without evidence / no action without audit (Laws 3–4)
- [ ] It can be *learned* or *extended* — or the capability is registered
      in the registry (Laws 2, 7)
- [ ] Identity/tone rules are data-driven, not hardcoded (Law 8)
- [ ] It surfaces through push or briefing, not a poll (Law 9)
- [ ] Works on every surface that should reach it (Law 10)
- [ ] Hermetic tests, never-crash, degrade silently
- [ ] The MCU test is passed: a sentence, not a command

**The question that ends all debate:**
> "If Tony could only *speak* to Friday — no keyboard, no flags, no
> config files — would this feature be reachable?"

If no, it's not MCU level. Fix it or cut it.
