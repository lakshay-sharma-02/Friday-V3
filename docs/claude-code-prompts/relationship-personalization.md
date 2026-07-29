# Relationship & Personalization — Prompt for Claude Code

## Intent
FRIDAY doesn't just remember facts about Tony — she has a *relationship* with him. She knows his mood, adapts her tone over time, and that rapport is what makes her feel like a partner, not a tool. Your current system has `memory.py` (long-term fact storage) and `conversation_learner.py` (extracts preferences). But the relationship is flat — no deepening rapport, no emotional awareness, no adaptive tone.

The goal: a system that builds a **real partnership** — one that gets warmer over time, reads the room, and feels like it knows *you*, not just your code.

## What to build

### Phase 1: Emotional State Detection

Create `src/friday/sentiment.py` with a `SentimentDetector`.

**What it does:**
- Analyzes user messages for emotional tone
- Dimensions: frustration, urgency, happiness, confusion, neutrality, sarcasm
- Outputs a `Sentiment` dataclass: `(tone: str, confidence: float, signal: str)` — signal explains why
- Runs on every user message, stores as `sentiment_observations` table:
  `(timestamp, channel, message_hash, tone, confidence, signal, conversation_id)`

**Approach:**
- **Deterministic** (always available, first pass): keyword + punctuation heuristics
  - "!" → urgency marker
  - "???" / "WTF" / "why the..." → frustration
  - "lol" / "haha" / "nice" → positive
  - "?", "what", "how" → neutral/curious
  - Short responses ("ok", "k", "fine") → could be neutral or frustrated — context needed
- **LLM optional**: use a lightweight classification prompt on the message
- **Trend tracking**: don't classify single messages in isolation — track rolling sentiment over the last N exchanges

**Key design:**
- Sentiment is a signal for other systems, not a standalone feature
- Never makes decisions based on sentiment alone — it's one input among many
- Sentiment is append-only, no deletion
- Confidence < 0.6 → don't store (avoid noise)

### Phase 2: Adaptive Tone / Deepening Rapport

The persona system currently uses a fixed prompt (`FRIDAY_PERSONA` in `prompts.py`). Add **tone modulation** based on relationship depth.

**The relationship depth model:**
Depth levels, tracked per operator:

- **Level 0 — Stranger** (first 5 conversations): Formal, polite, slightly reserved. "I'm Friday. How can I help you today?"
- **Level 1 — Acquaintance** (5-20 conversations, or known name): Warm, direct. Uses the operator's name naturally.
- **Level 2 — Partner** (20+ conversations, some preferences known): Casual, occasionally witty, proactive. "Morning. Three things before you dive in..."
- **Level 3 — Confidant** (50+ conversations, preferences + habits + personal context known): Can be blunt, uses in-jokes, finishes thoughts. "That Cargo.toml looks suspicious. Let me check..."
- **Level 4 — Trusted** (100+ conversations, deep history): "I remember you tried this approach on project X 3 months ago. Didn't work then — here's why it's different now."

**What changes per level:**
- Greeting warmth
- How much unsolicited advice is offered
- Whether Friday uses humor (casual observations vs. formal directness)
- How abbreviated responses can be (short = partner, long = stranger)
- Whether Friday refers to past conversations naturally
- Proactivity aggressiveness (Level 0: never interrupt; Level 2: interrupt for important stuff)

**Implementation:**
- Add `relationship_depth` to `operator_preferences` table (computed, not explicitly set)
- Computed from: total conversations, conversations with explicit preferences, name known, sentiment history (positive sentiment = faster depth progression)
- Inject relationship depth into the persona prompt via `build_directive()` in `prompts.py`
- Store a `tone_history` table: `(conversation_id, depth_at_time, tone_used, user_sentiment_avg)`

**Key design:**
- Depth only INCREASES, never decreases (no punishment for a frustrating day)
- The depth computation is deterministic — LLM is never asked "what level are you?"
- Operator can see their level: `friday profile depth` → "Partner level (32 conversations, 4 preferences, name known)"
- Depth affects persona prompt but never overrides safety rules

### Phase 3: Long-term Relationship Graph

Extend `src/friday/memory.py` with a **relationship graph** — not just fact storage, but a model of how you and Friday interact over time.

**What it stores:**
- Interaction frequency over time (per week, per day)
- Topics you discuss most (extracted from question categories)
- Your preferred interaction times (you always ask questions at 10am and 4pm)
- How you prefer answers (verbose vs. terse, code examples vs. explanations)
- What you ignore (you frequently dismiss proactive suggestions about project A → lower proactivity for A)
- What you act on (you frequently accept certain types of suggestions → higher confidence for similar suggestions)

**How it's used:**
- Friday chooses the best time to be proactive (when you're most receptive)
- Friday knows what to NOT bother you about (learned indifference)
- Friday tailors response length to your demonstrated preference
- Friday knows when you're most productive (peak coding hours) and plans analysis/cleanup for off-hours

**Implementation:**
- `relationship_metrics` table: `(metric_key, metric_value, computed_at, window_days)`
- Metrics computed via a daemon post-cycle hook (not on every interaction)
- All metrics are decayed — older interactions count less than recent ones
- `friday profile relationship` → shows the relationship graph summary

### Phase 4: Natural Memory Integration

Currently `memory.py` stores facts and `build_memory_context()` feeds them into the prompt. But the integration is mechanical. Upgrade it:

**What changes:**
- Memory facts are not just dumped into the prompt — they're selectively injected based on what's relevant to the current conversation
- When the user asks about project A, only inject memories related to project A, not the entire memory store
- When the user mentions a topic, check if there's a memory about it before the LLM answers
- Memories are referenced NATURALLY: "I remember you mentioned you prefer Rust for CLI tools. This would be a good fit for that."

**How:**
- Add a `memory_relevance` step before building the prompt: given the current question, score each memory fact by keyword overlap with the question + recent conversation context
- Only inject memories with score > 0.5
- Include the relevance signal with each injected memory so the LLM knows WHY it's relevant
- Let the LLM decide whether to use it (prompt says "You have this relevant memory — use it naturally if it helps")

## Files to touch
- `src/friday/sentiment.py` (new) — SentimentDetector, tone classification
- `src/friday/persona/prompts.py` — inject relationship depth into FRIDAY_PERSONA
- `src/friday/persona/engine.py` — wire sentiment + depth into response building
- `src/friday/memory.py` — add relationship graph, relevance scoring, selective injection
- `src/friday/operator/engine.py` — relationship depth computation
- `src/friday/daemon.py` — sentiment observation hook, relationship metrics hook
- `src/friday/db.py` — add `sentiment_observations`, `tone_history`, `relationship_metrics` tables
- `src/friday/cli.py` — add `friday sentiment`, `friday profile depth`, `friday profile relationship`
- `tests/test_sentiment.py` (new)
- `tests/test_relationship.py` (new)
- `tests/test_memory_relevance.py` (new extension)

## Acceptance criteria
1. User says "This is so frustrating" → sentiment detected as frustration, stored
2. After 5 conversations → relationship depth level 1 → Friday uses operator's name
3. After 20 conversations → depth level 2 → Friday is proactively suggesting improvements
4. `friday profile depth` → "Partner level (32 conversations)"
5. After repeatedly ignoring suggestions about project X → Friday stops suggesting things about project X
6. Memory relevance: ask "Tell me about my auth system" → only auth-related memories injected, not all memories
7. Memory reference: "I remember you prefer async over threading" → Friday says this naturally in relevant conversation
8. Sentiment never makes decisions alone — it's a signal for the tone modifier
9. Level 0 response is noticeably more formal than Level 3 response (greeting, length, humor usage)
10. Depth never decreases — a bad day doesn't reset the relationship
