# Phase 1: Ambient Feed + Proactive Intelligence

**Status:** Design  
**Date:** 2026-07-27  
**Inspired by:** Tony Stark's Friday — an ambient, proactive, always-on presence

---

## Executive Summary

Today, Friday's daemon runs silently in the background. It observes, learns, and discovers things — but the only way you find out is by running a CLI command, or by catching a desktop notification that says _"3 repos changed, 2 knowledge updates, 1 new initiative."_

This design turns that into a **living ambient presence** — a scrollable feed of what Friday is noticing, a dashboard you can glance at, and proactive alerts that feel like an AI partner tapping you on the shoulder.

**Three deliverables, ordered by impact:**

| # | Deliverable | Effort | Impact |
|---|-------------|--------|--------|
| 1 | Ambient Event Feed (core infrastructure) | 2-3 days | Foundations everything else builds on |
| 2 | Proactive Notification Engine | 1-2 days | Makes Friday feel alive |
| 3 | Terminal Dashboard (`friday dashboard`) | 3-5 days | The "wow" moment — a live Rich UI |

---

## 1. Ambient Event Feed (Core Infrastructure)

### 1.1 Problem

Currently, the daemon produces a flat log file and a flat status JSON. There's no structured, queryable history of "what Friday has noticed." The notification system is a single `_notify()` call that dumps summary text.

### 1.2 Solution

A new `ambient_feed` table + Python module that turns every notable daemon discovery into a **structured event** — stored, queryable, and displayable.

### 1.3 Data Model

```python
@dataclass
class AmbientEvent:
    id: str                    # auto-increment or ULID
    timestamp: str             # ISO 8601
    event_type: str            # see EventType enum below
    title: str                 # Short headline — fits in 60 chars
    detail: str                # Longer description — 1-3 sentences
    source: str                # e.g. "daemon-cycle", "observer", "meta-engine"
    priority: int              # 0 = info, 1 = noteworthy, 2 = important, 3 = critical
    category: str              # see Category enum below
    dismissed: bool            # User has seen/dismissed it
    actionable: bool           # Whether user can take action
    action_label: str | None   # e.g. "Review initiatives"
    action_command: str | None # e.g. "friday review pending"
    mission_id: str | None     # Link to a runtime mission, if applicable
    graph_id: str | None       # Link to a task graph, if applicable

class EventType(str, Enum):
    # Discovery events
    REPO_CHANGED = "repo_changed"
    KNOWLEDGE_UPDATED = "knowledge_updated"
    UNDERSTANDING_DERIVED = "understanding_derived"
    
    # Intelligence events
    NEW_INITIATIVE = "new_initiative"
    NEW_INSIGHT = "new_insight"
    HIGH_SEVERITY_SUGGESTION = "high_severity_suggestion"
    NEW_PATTERN = "new_pattern"
    INTENT_LABELED = "intent_labeled"
    SKILL_FORMED = "skill_formed"
    CROSS_PROJECT_CORRELATION = "cross_project_correlation"
    
    # Status events
    CYCLE_COMPLETE = "cycle_complete"
    CYCLE_FAILED = "cycle_failed"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    KILL_SWITCH_DEACTIVATED = "kill_switch_deactivated"
    
    # Quality events
    SKILL_DRIFT_DETECTED = "skill_drift_detected"
    CAPABILITY_GAP_DETECTED = "capability_gap_detected"
    AUTO_DISPATCHED = "auto_dispatched"
    
    # Execution events (for runtime missions)
    MISSION_STARTED = "mission_started"
    MISSION_COMPLETED = "mission_completed"
    TASK_FAILED = "task_failed"
    
    # User action events
    USER_ASKED = "user_asked"
    USER_EXECUTED = "user_executed"

class Category(str, Enum):
    WORKSPACE = "workspace"         # Repo changes, knowledge updates
    INTELLIGENCE = "intelligence"   # Insights, initiatives, patterns
    QUALITY = "quality"             # Drift, gaps, failures
    EXECUTION = "execution"         # Missions, tasks
    SYSTEM = "system"               # Daemon status, kill switch
    USER = "user"                   # User actions
```

### 1.4 SQL Schema

```sql
CREATE TABLE IF NOT EXISTS ambient_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    source TEXT NOT NULL DEFAULT 'daemon',
    priority INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'system',
    dismissed INTEGER NOT NULL DEFAULT 0,
    actionable INTEGER NOT NULL DEFAULT 0,
    action_label TEXT,
    action_command TEXT,
    mission_id TEXT,
    graph_id TEXT
);

CREATE INDEX idx_ambient_feed_timestamp ON ambient_feed(timestamp DESC);
CREATE INDEX idx_ambient_feed_dismissed ON ambient_feed(dismissed);
CREATE INDEX idx_ambient_feed_priority ON ambient_feed(priority);
CREATE INDEX idx_ambient_feed_category ON ambient_feed(category);
```

### 1.5 Python Module: `src/friday/ambient.py`

```python
"""Ambient Event Feed — structured event log for proactive intelligence.

Every notable discovery in the daemon cycle produces a structured event here.
The feed is the source of truth for:
  - The terminal dashboard (``friday dashboard``)
  - Desktop notification decisions (what to alert on)
  - Historical querying ("what did Friday notice yesterday?")
"""

def push_event(conn, event: AmbientEvent) -> int:
    """Insert an event into the feed. Returns the event ID."""

def get_feed(conn, limit: int = 50, offset: int = 0,
             category: str | None = None,
             min_priority: int = 0) -> list[AmbientEvent]:
    """Query the feed with filters."""

def get_unread_count(conn) -> int:
    """Number of undismissed events."""

def dismiss_event(conn, event_id: int) -> None:
    """Mark an event as dismissed (read)."""

def dismiss_all(conn) -> None:
    """Mark all events as dismissed."""

def get_latest_of_type(conn, event_type: str) -> AmbientEvent | None:
    """Get the most recent event of a given type (for dedup)."""

def summarize_recent(conn, hours: int = 24) -> dict:
    """Summarize recent activity by category + priority.
    
    Returns something like:
    {
        "total_events": 42,
        "by_category": {"workspace": 15, "intelligence": 20, ...},
        "high_priority": 3,
        "unread": 18,
        "latest_event": AmbientEvent(...),
    }
    """
```

### 1.6 Integration Points

The following locations in `daemon.py`'s `_run_cycle()` become event producers:

| Code Location | Event Type | Priority | Category |
|--------------|-----------|----------|----------|
| After `refresh(conn)` — if repos changed | `REPO_CHANGED` | 1 | workspace |
| After `refresh(conn)` — if knowledge updated | `KNOWLEDGE_UPDATED` | 1 | workspace |
| After `_harvest_initiatives()` — new initiatives | `NEW_INITIATIVE` | 2 | intelligence |
| After suggestions — if high severity | `HIGH_SEVERITY_SUGGESTION` | 2 | intelligence |
| After gap analysis — new gaps | `CAPABILITY_GAP_DETECTED` | 2 | quality |
| After sequence mining — new patterns | `NEW_PATTERN` | 1 | intelligence |
| After intent labeling — high conf intents | `INTENT_LABELED` | 1 | intelligence |
| After skill formation — new skills | `SKILL_FORMED` | 2 | intelligence |
| After cross-project correlation | `CROSS_PROJECT_CORRELATION` | 1 | intelligence |
| After drift detection — drifted skills | `SKILL_DRIFT_DETECTED` | 2 | quality |
| After auto-dispatch | `AUTO_DISPATCHED` | 1 | quality |
| Cycle succeeded | `CYCLE_COMPLETE` | 0 | system |
| Cycle failed | `CYCLE_FAILED` | 3 | system |
| Kill switch toggled | `KILL_SWITCH_*` | 3 | system |

For each integration point, the current `_log(...)` call **remains** (system log is for debugging), but a new `push_event(...)` call is added alongside it.

---

## 2. Proactive Notification Engine

### 2.1 Problem

The current `_notify()` call is a blunt instrument: it aggregates everything into one message and fires a desktop notification. There's no:
- Priority-based routing
- Deduplication ("you already told me that")
- Smart grouping ("3 repos changed, not 3 separate notifications")
- User preference (some things are important to me, others aren't)

### 2.2 Solution

Replace the current notification block in `_do_cycle()` with a **NotificationEngine** that uses rules + priorities to decide:
1. Should this event generate a notification?
2. What channel(s) should it go to?
3. How should it be phrased?

### 2.3 Design

```python
class NotificationEngine:
    """Routes ambient events to notification channels based on rules."""
    
    CHANNELS = {
        "desktop": _notify_desktop,    # notify-send / osascript
        "telegram": _notify_telegram,  # existing Telegram bot
        "feed": _notify_feed,          # always — feed entry is the minimum
        "sound": _notify_sound,        # optional audible alert
    }
    
    RULES = [
        # Critical errors → everything
        Rule(min_priority=3, channels=["desktop", "telegram", "sound", "feed"]),
        # Important discoveries → desktop + telegram
        Rule(event_types=["new_initiative", "high_severity_suggestion", 
                          "skill_drift_detected", "capability_gap_detected"],
             min_priority=2, channels=["desktop", "telegram", "feed"]),
        # Normal discoveries → feed only
        Rule(min_priority=1, channels=["feed"]),
        # Routine → feed only (and even then, only if new)
        Rule(min_priority=0, channels=["feed"], dedup=True),
    ]
```

### 2.4 Smart Notification Content

Instead of the current `". ".join(notify_parts) + "."`, each event type gets a **templated message** that sounds like a real assistant:

| Event Type | Template |
|-----------|----------|
| `REPO_CHANGED` | "I noticed changes in {count} repo(s): {names}. Want me to summarize?" |
| `NEW_INITIATIVE` | "A new engineering initiative has emerged: **{title}**. It's based on {evidence_count} observations." |
| `HIGH_SEVERITY_SUGGESTION` | "I found a promising integration opportunity: **{title}**. This could save significant effort." |
| `SKILL_DRIFT_DETECTED` | "Heads up — {count} skill(s) are degrading. They may need re-formation. Run `friday skills drift` to review." |
| `CAPABILITY_GAP_DETECTED` | "I found {count} capability gap(s) in my execution pipeline. `friday meta analyze` for details." |
| `CROSS_PROJECT_CORRELATION` | "I noticed {repos} share some interesting architectural patterns. They might benefit from consolidation." |
| `CYCLE_FAILED` | "I ran into an issue during my last observation cycle: {error}. I'll keep trying." |

### 2.5 Deduplication

The same notification should not fire twice within `N` hours for the same event type. This prevents notification spam when the daemon cycles every 15 minutes.

```python
def should_notify(conn, event_type: str, cooldown_hours: int = 6) -> bool:
    """Returns False if we already notified for this event_type recently."""
```

---

## 3. Terminal Dashboard (`friday dashboard`)

### 3.1 Problem

There is no "Friday interface" — only individual CLI commands. The user has to remember to check things. There's no way to see "what's Friday up to?" at a glance.

### 3.2 Solution

A live Rich-based terminal dashboard that shows:

```
┌──────────────────────────────────────────────────────────────┐
│  ◆ FRIDAY Dashboard                          Active ● 15m    │
├──────────────────────────────────────────────────────────────┤
│ ┌──────────────┐ ┌─────────────────────────────────────────┐ │
│ │ Status       │ │ Feed                                     │ │
│ │──────────────│ │──────────────────────────────────────────│ │
│ │ Daemon: ● ON │ │ ● 10m ago  New initiative: "TypeScript   │ │
│ │ Last cycle:  │ │            Engineering Initiative"       │ │
│ │  2m ago (4s) │ │                                           │ │
│ │ Repos: 16    │ │ ● 12m ago  3 repos changed (vivaha,     │ │
│ │ Skills: 3    │ │            codebuff, aether)             │ │
│ │ Initiatives: │ │                                           │ │
│ │  4 pending   │ │ ● 18m ago  Cross-project correlation    │ │
│ │              │ │            detected between vivaha &     │ │
│ │ ⚠ 2 drifted  │ │            aether (shared React + TS)    │ │
│ │   skills     │ │                                           │ │
│ │              │ │ ● 1h ago   Capability gap: no Docker    │ │
│ │ ? 1 new gap  │ │            executor available            │ │
│ └──────────────┘ └─────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│  ▶ Friday is idle. Last cycle found 1 new suggestion.        │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 Implementation

**Tech Stack:** Python Rich library (already a project dependency)

**Architecture:**
- `src/friday/presentation/ambient/dashboard.py` — the Rich Layout
- `src/friday/presentation/ambient/feed_widget.py` — scrollable feed display
- `src/friday/presentation/ambient/status_panel.py` — status summary panel
- `friday dashboard` CLI command — new entry point

**Live Updates:** 
- Uses Rich's `Live` display with a refresh thread
- Polls `ambient_feed` table every 2 seconds for new events
- Polls daemon status every 5 seconds
- Auto-refreshes the display

**User Interaction:**
- Scrolling through feed entries (keyboard up/down/space)
- `d` to dismiss an entry
- `enter` on an actionable entry to execute its command
- `r` to refresh immediately
- `q` or `ctrl+c` to exit

### 3.4 Widget Components

```python
class AmbientDashboard:
    """Full-screen live dashboard compositing all widgets."""
    
    def render(self, status: DaemonStatus, feed: list[AmbientEvent],
               summary: AmbientSummary) -> Layout:
        ...

class FeedWidget:
    """Scrollable list of ambient events with priority coloring."""
    
    def render(self, events: list[AmbientEvent], scroll_offset: int) -> Panel:
        ...

class StatusPanel:
    """Quick-status summary — daemon state, unread count, key metrics."""
    
    def render(self, status: DaemonStatus, summary: AmbientSummary) -> Panel:
        ...

class FooterWidget:
    """Context-sensitive footer with keyboard shortcuts and status."""
    
    def render(self, current_state: DashboardState) -> Panel:
        ...
```

---

## 4. Implementation Order

### Step 1: Ambient Event Feed (Day 1-2)
- [ ] Create `src/friday/ambient.py` with `push_event()`, `get_feed()`, etc.
- [ ] Add `ambient_feed` table to schema migration
- [ ] Add `ambient/__init__.py` module structure
- [ ] Wire into daemon's `_run_cycle()` — 12+ push_event calls post-cycle

### Step 2: Notification Engine (Day 3)
- [ ] Build `NotificationEngine` in `src/friday/ambient/notification.py`
- [ ] Replace the notification block in `daemon.py`'s `_do_cycle()`
- [ ] Add deduplication logic
- [ ] Add per-event-type templates
- [ ] Remove the old `notify_parts` aggregation

### Step 3: Terminal Dashboard (Day 4-7)
- [ ] Create `src/friday/presentation/ambient/` module
- [ ] Build `StatusPanel` widget
- [ ] Build `FeedWidget` widget with scrolling
- [ ] Build `AmbientDashboard` layout
- [ ] Add `friday dashboard` CLI command
- [ ] Add keyboard interaction (dismiss, scroll, execute)
- [ ] Add live polling thread for auto-refresh

### Step 4: Polish (Day 8-10)
- [ ] Color-coding by event type + priority
- [ ] Sound notifications for critical events
- [ ] Desktop notification improvements (click → open Friday)
- [ ] `friday feed` — simple feed listing (non-interactive)
- [ ] `friday feed --dismiss-all` — clear the feed

---

## 5. Key Design Decisions

### Why Rich and not a web dashboard?
The terminal is where Friday already lives. Rich is already a dependency. Building a web server (even with Flask/FastAPI) adds a surface area for security, port management, browser launching, etc. The terminal dashboard is the **fastest path to "wow"** — 3 days vs 3 weeks.

A web dashboard can come in Phase 2.

### Why new SQL table and not just the log?
The log is unstructured text. The `ambient_feed` table is structured, queryable, and allows `WHERE priority >= 2` or `WHERE category = 'intelligence'`. This powers both the dashboard and smart notification routing.

### Why not use the existing EventBus?
The EventBus is for **in-process live execution events** (mission started, worker spawned). It's transient — events are not persisted. The ambient feed needs persistence (survive daemon restart), queryability, and a different event taxonomy. They complement each other.

### Why a dedicated module instead of extending daemon.py?
`daemon.py` is already 700+ lines. Adding a 200-line notification engine + event producers there would make it unmaintainable. The ambient module is a clean separation of concerns.

---

## 6. Visual Mockups

### The Dashboard (terminal, Rich-based)

```
┌────────────────────────────────────────────────────────────────┐
│  ◆ FRIDAY Dashboard                       ● Running 15m cycle   │
├────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────┐ ┌────────────────────────────────────┐ │
│ │ ⚡ Status            │ │ 📡 Live Feed                      │ │
│ │                      │ │                                    │ │
│ │ Daemon     ● ON      │ │ 🔴 2m ago — New initiative         │ │
│ │ Cycle      #142      │ │   "TypeScript Engineering Init."   │ │
│ │ Duration   3.2s      │ │   → 12 observations support this   │ │
│ │ Repos      16        │ │                                    │ │
│ │ Skills     3 active  │ │ 🟡 5m ago — Skill drift detected   │ │
│ │             2 drifted│ │   "Test Runner" is degrading       │ │
│ │ Gaps       1 open    │ │   → accuracy dropped from 89→72%   │ │
│ │ Initiatives 4 pending│ │                                    │ │
│ │                      │ │ 🟢 12m ago — Cross-project match   │ │
│ │ Unread: 7    ●●○○○○○ │ │   "vivaha ↔ aether" share React   │ │
│ │                      │ │   + TypeScript stack               │ │
│ │                      │ │                                    │ │
│ │                      │ │ 🟢 18m ago — 3 repos changed       │ │
│ │                      │ │   vivaha, codebuff, aether         │ │
│ │                      │ │                                    │ │
│ └──────────────────────┘ └────────────────────────────────────┘ │
├────────────────────────────────────────────────────────────────┤ │
│  ⚡ [space] scroll  [d] dismiss  [enter] act  [r] refresh  [q] quit │
└────────────────────────────────────────────────────────────────┘
```

### The Notification (desktop, before vs after)

**Before:**
> Friday — Workspace Update
> 3/16 repos changed. 2 knowledge updates. 1 new initiative. 1 high-severity integration suggestion.

**After:**
> Friday — New initiative emerged
> I've identified a "TypeScript Engineering Initiative" based on 12 observations across your workspace. You have 4 pending initiatives. Run `friday review pending` to review them.

---

## 7. Success Metrics

| Metric | Before | After (target) |
|--------|--------|----------------|
| Time to understand "what's going on" | Open terminal, type `friday daemon status`, read output | Glance at dashboard (instant) |
| Notification usefulness | "X repos changed" — vague, ignored | "Initiative emerged in vivaha" — specific, actionable |
| Proactive discoveries missed | Unknown — user never knew | All high-priority events surfaced in feed + notification |
| User engagement | User runs Friday commands | User glances at dashboard, clicks to act |
| "Feels like an assistant" rating | 2/10 | 7/10 |

---

## 8. Open Questions for Discussion

1. **Feed retention** — How long should events live in the feed? Auto-prune after 7 days? 30 days? Infinite?
2. **Notification cooldowns** — 6 hours good for dedup? Should it be per-event-type-configurable?
3. **Dashboard accessibility** — What if the terminal is in a tmux session? Should we auto-launch on `friday daemon start`?
4. **Sound** — Built-in terminal bell? System notification sound? Only for priority 3?
5. **Click-to-act** — Terminal dashboard can't actually open URLs. Should we print actionable commands instead?
