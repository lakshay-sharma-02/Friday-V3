# Daily Operations — Prompt for Claude Code

## Intent
FRIDAY doesn't wait to be asked. In the MCU, she briefs Tony every morning — overnight updates, what needs attention, what changed. Your current system has diff tracking and ambient events but no **daily ritual**. The goal: a morning briefing and evening wrap-up that feel like a real operating partner covering your back.

This covers 4 features: **Morning Briefing**, **End-of-Day Summary**, **Spontaneous Code Review**, **Context-aware Standup Report**.

## What to build

### Phase 1: Morning Briefing

Create `src/friday/briefing.py` with a `DailyBriefing` class. It runs once per day (first daemon cycle after configurable hour, default 8:00 AM).

**What it produces** (structured, concise, natural-language):
1. **Since yesterday** — repos changed, commits, branches, PRs updated, issues changed
2. **What drifted** — skill health changes, knowledge degradation, gaps that emerged
3. **What was learned** — operator preferences detected, conversation insights, patterns mined
4. **What's on watch** — watchers that fired overnight, pending interrupts deferred from yesterday
5. **Today's calendar** — upcoming events, deadlines, meetings (from calendar observer)
6. **One thing worth knowing** — the single most significant finding from ambient analysis

**Delivery:**
- Push to ambient feed as a single `daily_briefing` event with structured payload
- If `FRIDAY_VOICE=true`, speak it
- If Telegram/Slack/Discord configured, send to preferred channel
- Always available via `friday briefing` CLI command
- Never repeat — only produce once per calendar day (check `briefing_delivered` DB flag per date)

**Key design:**
- LLM optional, deterministic fallback — templates that fill in blanks
- Briefing is always MAX 12 lines in terminal, 8 sentences when spoken
- "One thing worth knowing" is the headline — make it punchy
- If nothing changed since yesterday, briefing is 2 lines: "Quiet day. Nothing new. N repos unchanged."

### Phase 2: End-of-Day Summary

Same engine (`DailyBriefing` class), evening mode:
- Runs at configurable hour (default 21:00) or on `friday briefing --evening`
- Covers: what you worked on today (sessions), what changed, what's unresolved
- Longer format — 15-20 lines
- Flags anything that needs attention tomorrow (incomplete builds, open issues, pending PRs)
- Persists to a `daily_summaries` table as an append-only log

### Phase 3: Spontaneous Code Review

Add `src/friday/review_spontaneous.py`. This is NOT a CLI command — it's an autonomous background process.

**Triggers:**
- New branch detected (git observer)
- Uncommitted changes sitting for > 30min
- PR state change (GitHub observer — opened, updated)
- On daemon cycle, after the main analysis pass

**What it does:**
1. Collects diff (git diff, or diff between PR base and head)
2. Analyzes statistically: lines changed, files touched, risk patterns (large deletions, config changes, dependency bumps)
3. If LLM available: generates a 3-5 line code review summary — what's good, what's risky, what's worth discussing
4. Deterministic fallback: "30 files changed, 400+ lines deleted, 2 config files modified — worth a look."
5. If the diff is trivial (typo fix, comment change, dep bump) → **do nothing**
6. Only triggers if the diff is significant (configurable: > 20 lines changed AND not just comments)

**Delivery:**
- Not a separate message — folded into the next briefing or ambient feed as `spontaneous_review` event
- If priority is high (risky changes detected), push as an urgent event through the presence-gated interrupt queue

### Phase 4: Context-aware Standup Report

Add `friday standup` CLI command.

Produce a 5-7 line summary formatted for standup meetings:
- What I worked on: last 24h commit messages / session titles
- Blockers: failed builds, broken tests, unresolved issues
- Next: active branches, open PRs, pending reviews
- Working on: current session context (from `context/engine.py`)

Format: "Yesterday, I worked on {N} projects. Made {M} commits across {B} branches. {blocker_info}. Today I'm on {current_project}. {open_pr_count} PRs pending review."

No LLM needed — purely template-driven from DB state.

## Files to touch
- `src/friday/briefing.py` (new) — DailyBriefing, morning + evening modes
- `src/friday/review_spontaneous.py` (new) — autonomous diff review
- `src/friday/db.py` — add `daily_summaries`, `briefing_log` tables
- `src/friday/cli.py` — add `friday briefing`, `friday standup`, `friday yesterday` commands
- `src/friday/daemon.py` — hook briefing check into cycle (first cycle after 8am = brief)
- `src/friday/ambient.py` — add `daily_briefing`, `spontaneous_review` event types
- `src/friday/proactive.py` — fold spontaneous reviews into the interrupt queue
- `tests/test_briefing.py` (new)
- `tests/test_review_spontaneous.py` (new)

## Acceptance criteria
1. First daemon cycle after 8am generates a briefing → pushed to ambient feed + preferred channel
2. `friday briefing` shows the day's briefing (or "Not yet generated — run daemon or wait until 8am")
3. `friday standup` produces a 5-7 line standup summary from last 24h of data
4. Uncommitted changes on a branch for 45min → spontaneous review event in ambient feed
5. New PR opened on GitHub → spontaneous review analyzes diff, pushes event
6. Trivial diffs (< 20 lines, comments only) → no event
7. Evening briefing at configured hour shows today's work + tomorrow's queue
8. Running `friday briefing` twice on the same day returns the same briefing (cached)
