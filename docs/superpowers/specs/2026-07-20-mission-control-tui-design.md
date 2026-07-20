# FRIDAY Mission Control TUI — Design Spec

## Overview

Replace the current plain-text CLI with a Rich-powered presentation layer that gives users continuous situational awareness during long-running operations and premium structured output for quick commands.

**No behavior change.** All domain logic stays untouched. The CLI commands remain the same entry points. This is purely a presentation upgrade.

---

## Architecture

```text
src/friday/
├── cli/
│   ├── __init__.py
│   ├── models.py              # View models: MissionView, WorkerView, TimelineEventView, etc.
│   ├── style.py               # Colors, icons, borders, spacing (single source of truth)
│   │
│   ├── formatters/            # Domain objects → view models (pure, testable)
│   │   ├── mission.py
│   │   ├── knowledge.py
│   │   └── execution.py
│   │
│   ├── renderers/             # View models → Rich renderables
│   │   ├── mission.py         # Live dashboard for long-running ops
│   │   ├── knowledge.py       # KnowledgeResult → Panels + Tables
│   │   ├── execution.py       # ExecutionResult → Task state panels
│   │   ├── summary.py         # Summary view
│   │   └── shared.py          # Common rendering helpers
│   │
│   └── widgets/               # Composable Rich building blocks
│       ├── header.py
│       ├── footer.py
│       ├── progress.py
│       ├── workers.py
│       ├── timeline.py
│       ├── status.py
│       ├── tables.py
│       ├── trees.py
│       └── panels.py
│
├── runtime/
│   ├── events.py              # Existing DB-persisted terminal events (unchanged)
│   ├── event_bus.py           # NEW: in-process pub/sub for live events
│   └── ...
```

### Key rule

> Renderers must never call runtime logic, execute commands, or mutate state. They consume immutable view models and produce Rich output. All state changes originate from the runtime event bus.

---

## Package details

### `cli/models.py` — View models

Immutable dataclasses that renderers consume. Examples:

```python
@dataclass(frozen=True)
class MissionView:
    id: str
    goal: str
    phase: MissionPhase
    progress: float  # 0.0–1.0
    workers: list[WorkerView]
    timeline: list[TimelineEventView]
    summary: SummaryView
    elapsed_seconds: int

@dataclass(frozen=True)
class WorkerView:
    name: str
    status: WorkerStatus  # idle | running | completed | failed
    current_task: str
    progress: ProgressView | None

@dataclass(frozen=True)
class TimelineEventView:
    timestamp: str
    kind: str
    message: str

@dataclass(frozen=True)
class ProgressView:
    current: int
    total: int | None

@dataclass(frozen=True)
class SummaryView:
    files_modified: int
    tests_passed: int
    warnings: int
```

### `cli/style.py` — Design system

Single source of truth for visual tokens. Not just colors — borders, spacing, icons, typography.

```python
class Color:
    PRIMARY = "#58a6ff"       # Blue (GitHub accent)
    SUCCESS = "#3fb950"       # Green
    WARNING = "#d29922"       # Yellow
    ERROR = "#f85149"         # Red
    TEXT = "#e6edf3"          # Light gray
    DIM = "#8b949e"           # Muted gray
    BORDER = "#30363d"        # Subtle border

class Icon:
    WORKER = "▲"
    COMPLETED = "✓"
    FAILED = "✗"
    RUNNING = "●"
    PENDING = "○"
    MISSION = "◆"

class Spacing:
    PANEL_PADDING = (1, 2)
    SECTION_GAP = 1
```

### `runtime/event_bus.py` — In-process events

Strongly typed immutable events. No strings, no `payload: dict`.

```python
@dataclass(frozen=True)
class Event:
    mission_id: str
    timestamp: datetime

@dataclass(frozen=True)
class MissionStarted(Event): ...
@dataclass(frozen=True)
class MissionCompleted(Event):
    result: str  # "success" | "failed"
    summary: SummaryView

@dataclass(frozen=True)
class PhaseChanged(Event):
    previous: str
    current: str

@dataclass(frozen=True)
class WorkerStarted(Event):
    worker_id: str
    name: str

@dataclass(frozen=True)
class WorkerProgress(Event):
    worker_id: str
    current: int
    total: int | None
    message: str

@dataclass(frozen=True)
class WorkerCompleted(Event):
    worker_id: str
    success: bool

@dataclass(frozen=True)
class WorkerFailed(Event):
    worker_id: str
    error: str

@dataclass(frozen=True)
class LogMessage(Event):
    level: str  # "info" | "warn" | "error"
    message: str
```

EventBus:

```python
class EventBus:
    """In-process pub/sub. No DB writes for live events."""
    _subscribers: dict[type, list[Callable]]

    def publish(self, event: Event) -> None
    def subscribe(self, event_type: type[Event], callback: Callable) -> None
```

Terminal events (mission_completed, etc.) are still persisted via `runtime/events.py` (DB-backed). Live events flow only to in-process subscribers.

---

## Data flow

### Live path (execute, implement, fix, refactor, benchmark, repair, review)

```
User runs "friday execute <goal>"

CLI creates RuntimeEngine + EventBus
CLI subscribes MissionRenderer to EventBus

Engine.execute(schedule)
  → publishes events on EventBus as work progresses
    (PhaseChanged, WorkerStarted, WorkerProgress, WorkerCompleted, ...)

EventBus fans out to subscribers
  → MissionRenderer receives events
  → Accumulates into MissionState (an aggregate snapshot)
  → Rich Live display refreshes at throttled cadence (10–20 FPS)

On completion:
  → Terminal event persisted to runtime/events.py (DB)
  → Final static MissionView rendered
  → Live mode ends
```

### MissionState aggregation

Instead of renderers mutating UI pieces directly per event:

```
WorkerProgress event
  → MissionState.update(event)   # pure, deterministic
  → MissionRenderer.render(state)  # single pass from snapshot
```

`MissionState` is an internal accumulator class that receives events and produces a `MissionView` on demand. The renderer never touches UI piecemeal — it renders the entire snapshot each frame.

### Static path (knowledge list, explain, resolve, plan, ...)

```
User runs "friday knowledge list"

Engine returns KnowledgeResult (domain object)
Formatter converts to KnowledgeView (view model)
Renderer converts KnowledgeView → Rich renderable
Prints to stdout, exits (no Live wrapper)
```

---

## Live vs Commands

| Mode | Commands | Rendering |
|------|----------|-----------|
| **Live** | `execute`, `implement`, `fix`, `refactor`, `benchmark`, `repair`, `review` | Rich `Live` + `Layout` with throttled redraws |
| **Static** | everything else (list, explain, resolve, plan, knowledge, identity, ...) | Rich Panel/Table/Tree, instant render, no Live |

---

## Mission Layout (Live mode)

```
┌────────────────────────────────────────────────────────────┐
│ FRIDAY Mission Control                          Mission #41│
├────────────────────────────────────────────────────────────┤
│ Goal                                                   68% │
│ Refactor runtime architecture                           ██ │
├────────────────────────────────────────────────────────────┤
│ Current Phase                                             │
│ Architecture Analysis                                     │
├────────────────────────────────────────────────────────────┤
│ Active Workers                                            │
│ Architecture Analyst     Reading dependency graph    00:14│
│ Search Specialist        Searching runtime/          00:08│
│ Test Engineer            Collecting tests            00:03│
├────────────────────────────────────────────────────────────┤
│ Timeline                                                 │
│ 12:03:12  Repository mapped                              │
│ 12:04:01  Runtime analyzed                               │
│ 12:04:58  Dependency graph built                         │
│ 12:06:12  61 worker references found                     │
├────────────────────────────────────────────────────────────┤
│ Status                                                    │
│ 143 files · 18 modified · 121 tests · 0 warnings         │
└────────────────────────────────────────────────────────────┘
```

Header shows mission ID + elapsed time. Progress bar is percentage through the execution plan. Timeline is newest-bottom (auto-scroll). Workers show name, current task, elapsed per-worker. Footer is a single always-updating status sentence.

---

## Widget composition

```python
class MissionRenderer:
    """Composed from independent widgets."""

    def __init__(self):
        self.header = HeaderWidget()
        self.progress = ProgressWidget()
        self.phase = PhaseWidget()
        self.workers = WorkersWidget()
        self.timeline = TimelineWidget()
        self.footer = FooterWidget()
        self.layout = Layout()

    def render(self, state: MissionState) -> Layout:
        view = state.to_view()
        self.header.update(view)
        self.progress.update(view.progress)
        self.phase.update(view.phase)
        self.workers.update(view.workers)
        self.timeline.update(view.timeline)
        self.footer.update(view.status_line)
        return self.layout
```

Each widget is independently testable. Widgets don't call the event bus. They're set from outside by the renderer.

---

## Throttling

Renderer refreshes at maximum ~15 FPS. Events are consumed immediately into `MissionState`, but Rich `Live` refresh only triggers at cadence:

```python
class ThrottledLive:
    def __init__(self, renderable, refresh_per_second=15):
        self._live = Live(renderable, refresh_per_second=refresh_per_second)
```

This prevents terminal redraw for every `FileScanned` event while keeping the UI buttery smooth.

---

## Events summary

| Event | Fields | Persisted? |
|-------|--------|-----------|
| `MissionStarted` | goal, mission_id | Yes (runtime/events) |
| `MissionCompleted` | result, summary, duration_ms | Yes (runtime/events) |
| `PhaseChanged` | previous, current | No |
| `WorkerStarted` | worker_id, name | No |
| `WorkerProgress` | worker_id, current, total, message | No |
| `WorkerCompleted` | worker_id, success | No, unless mission terminal |
| `WorkerFailed` | worker_id, error | No |
| `LogMessage` | level, message | No |
| `ToolStarted` | tool_name, args | No |
| `ToolCompleted` | tool_name, exit_code | No |

---

## Testing strategy

| Layer | What to test | How |
|-------|-------------|-----|
| **EventBus** | publish→subscribe, multiple subscribers, typing | Unit |
| **Formatters** | Known domain object → expected view model | Unit (pure functions) |
| **Renderers** | Known view model → Rich output contains expected text | Snapshot / assert renderable |
| **Widgets** | Each widget in isolation with known inputs | Snapshot / assert renderable |
| **MissionState** | Sequence of events → expected final state | Unit (event ordering tests) |
| **Event sequences** | Given events [A,B,C], assert renderer ends in expected state | Integration |
| **Live integration** | End-to-end: mock EventBus, emit events, assert Live output | Integration |

---

## Dependencies

- `rich` — already available in the environment
- No new third-party dependencies
- No `textual` (future upgrade path if needed)

---

## Success criteria

1. `friday execute "<goal>"` shows a live Mission Control dashboard
2. `friday knowledge list` shows beautiful Rich panels/tables (instant)
3. All existing CLI commands still work with backward-compatible output
4. All 1121+ tests still pass (0 new failures)
5. No runtime code imports from `cli/` — rendering is purely additive
6. `grep -rn "from.*cli\|import.*cli" src/friday/runtime/` returns nothing
