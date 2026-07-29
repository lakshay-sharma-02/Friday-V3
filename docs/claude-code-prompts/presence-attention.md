# Presence & Attention — Prompt for Claude Code

## Intent
FRIDAY in the MCU KNOWS when to speak and when to shut up. She doesn't interrupt Tony mid-sentence unless it's an emergency. Your current implementation has a notification toggle (`no_notifications=true`) and a cooldown timer. That's not presence — that's a mute button.

The goal: Friday knows your state — at desk, in flow, in a meeting, away, sleeping — and adjusts interrupt behavior accordingly. Not a toggle. An *intelligent attention system*.

## What to build

### Phase 1: Presence detection layer
Create `src/friday/presence.py` with a `PresenceDetector` class that answers one question: **What's the operator's current state?**

- **State machine**: `AWAY | DESK_IDLE | DESK_ACTIVE | IN_MEETING | DEEP_FOCUS | SLEEPING`
- **Signals** (pulled from existing observers):
  - Hyprland heartbeat (last active timestamp) — desk presence
  - Keyboard/mouse idle time (via `xprintidle` or Hyprland idle inhibition) — desk activity
  - Calendar observer — current/future events (in_meeting)
  - Git/terminal activity recency — deep focus vs idle
  - Time of day + activity hours over last N days — sleeping vs awake
- **Fusion**: Combine signals deterministically. Calendar event + no keyboard for 30s = "in meeting." Heavy git activity + keyboard active = "deep focus." No heartbeat for 15 min = "away."

**Key design decisions:**
- Deterministic — no LLM for state classification
- Every signal is optional — graceful degradation if an observer is missing
- State is a function of the last N minutes, not instant (smoothing to avoid flicker)
- Expose as `friday status` CLI command that shows current state + signal breakdown
- Persist state changes to the DB as observation facts so the timeline records "went idle" / "returned"

### Phase 2: Attention-aware interrupt queue
In `src/friday/proactive.py` (NOT a new file — upgrade the existing one), replace the simple cooldown system with a priority queue gated by presence state.

Currently `_PROACT_WORTHY_EVENT_TYPES` + `_COOLDOWN_HOURS`. Replace with:

- **Urgent** (priority 3): Kill switch, build failure, security issue → fires regardless of state
- **Important** (priority 2): Drift detected, capability gaps, high-value suggestions → fires only when DESK_ACTIVE or returns from AWAY
- **Normal** (priority 1): New patterns, insights, learnings → held for next briefing, never fires mid-flow
- **Routine** (priority 0): Cycle complete, knowledge updated → not worth conversation ever

**Escalation rules:**
```
DEEP_FOCUS → only urgent passes through
IN_MEETING → urgent + important queued for after meeting
DESK_ACTIVE → urgent + important immediately
DESK_IDLE → urgent immediately, rest queued
AWAY / SLEEPING → everything queued, burst on return
```

The "queued for later" items are pushed to the ambient feed AND a new `deferred_interrupts` table:
- `id, event_type, priority, message, created_at, state_at_creation, delivered_at`
- When presence transitions to a more permissive state, check this table and deliver the highest-priority pending item for each event type

### Phase 3: Contextual DND
Remove the existing `no_notifications` toggle behavior. Replace with:

- **Auto DND**: Automatically enters DND when `DEEP_FOCUS` state detected
- **Manual override**: `friday focus on` / `friday focus off` — explicitly set focus mode for N minutes
- **Calendar-aware**: If your calendar says "Engineering deep work 2-4pm", Friday knows not to interrupt
- **Scheduled focus**: `friday focus on 2h` — auto-cancels after timer
- Show DND status in CLI prompt and dashboard

The existing `no_notifications` preference key in `operator_preferences` should be REFRAMED — instead of "mute everything" it should be "disallow interruptions above priority X during focus."

## Files to touch
- `src/friday/presence.py` (new) — PresenceDetector, state machine, signal fusion
- `src/friday/proactive.py` — upgrade _get_signal_summary, add presence gate
- `src/friday/persona/engine.py` — refactor no_notifications, wire presence into interrupt behavior
- `src/friday/db.py` — add `deferred_interrupts` table migration
- `src/friday/cli.py` — add `friday status`, `friday focus` commands
- `src/friday/daemon.py` — hook presence detection into daemon cycle
- `src/friday/ambient.py` — add `presence_changed` event type
- `tests/test_presence.py` (new)
- `tests/test_proactive.py` — update for presence-gated tests

## Acceptance criteria
1. `friday status` shows: "At desk, active. In focus (34m of deep work). Next interrupt queue: 2 items."
2. While coding with frequent git commits + terminal activity → proactive messages are deferred
3. Open a calendar event → Friday detects "in meeting" → suppresses all non-urgent messages
4. Leave desk for 30min → return → Friday says "Welcome back. You missed 3 things."
5. `friday focus on 90min` → blocks all notifications, auto recovers
6. No existing tests break
