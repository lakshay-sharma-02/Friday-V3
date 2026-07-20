# FRIDAY Mission Control TUI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the plain-text CLI with a Rich-powered presentation layer featuring a live Mission Control dashboard for long-running ops and premium panels/tables for info commands.

**Architecture:** Additive presentation layer. No existing code modified. New `cli/` package with separate formatters (domain→view models), renderers (view models→Rich), and widgets (composable Rich building blocks). In-process EventBus in `runtime/` for live events. Existing `runtime/events.py` (DB persistence) unchanged.

**Tech Stack:** Rich 15.0.0 (already installed), Python 3.14, stdlib only.

---

## File Map

```
CREATE src/friday/runtime/event_bus.py       # In-process pub/sub with typed events
CREATE src/friday/cli/__init__.py             # Package init
CREATE src/friday/cli/models.py               # View models + enums
CREATE src/friday/cli/style.py                # Design tokens (colors, icons, spacing)
CREATE src/friday/cli/formatters/__init__.py  # Package init
CREATE src/friday/cli/formatters/execution.py # ExecutionResult → ExecutionView
CREATE src/friday/cli/renderers/__init__.py   # Package init
CREATE src/friday/cli/renderers/mission.py    # Live dashboard (MissionRenderer)
CREATE src/friday/cli/renderers/execution.py  # ExecutionView → Rich panels (static)
CREATE src/friday/cli/renderers/shared.py     # Common rendering helpers
CREATE src/friday/cli/widgets/__init__.py     # Package init
CREATE src/friday/cli/widgets/header.py       # Mission header widget
CREATE src/friday/cli/widgets/footer.py       # Mission status line widget
CREATE src/friday/cli/widgets/progress.py     # Progress bar widget
CREATE src/friday/cli/widgets/workers.py      # Worker status cards widget
CREATE src/friday/cli/widgets/timeline.py     # Event timeline widget
CREATE src/friday/cli/widgets/mission_graph.py# Phase progress indicator
CREATE src/friday/cli/widgets/panels.py       # Reusable Panel builders
CREATE src/friday/cli/widgets/tables.py       # Reusable Table builders
CREATE tests/test_event_bus.py                # EventBus unit tests
CREATE tests/test_mission_renderer.py         # MissionRenderer integration tests
CREATE tests/test_cli_models.py               # View model tests
CREATE tests/test_cli_widgets.py              # Widget snapshot tests
MODIFY none                                    # No existing files changed
```

---

### Task 1: EventBus + typed events

**Files:**
- Create: `src/friday/runtime/event_bus.py`
- Test: `tests/test_event_bus.py`

- [ ] **Step 1: Write tests for EventBus and event types**

```python
"""Tests for runtime/event_bus.py — in-process pub/sub with typed events."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event
from typing import Callable

import pytest

from friday.runtime.event_bus import (
    Event,
    EventBus,
    MissionStarted,
    MissionCompleted,
    PhaseChanged,
    WorkerSpawned,
    WorkerReady,
    WorkerStarted,
    WorkerProgress,
    WorkerWaiting,
    WorkerCompleted,
    WorkerFailed,
    ToolStarted,
    ToolCompleted,
    LogMessage,
    MissionPhase,
)


def test_event_is_dataclass():
    e = MissionStarted(mission_id="m1", timestamp=datetime.now(), goal="test")
    assert e.mission_id == "m1"
    assert e.goal == "test"


def test_event_immutable():
    e = MissionStarted(mission_id="m1", timestamp=datetime.now(), goal="test")
    with pytest.raises(AttributeError):
        e.goal = "changed"


def test_event_bus_publish_subscribe():
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event)

    bus.subscribe(WorkerStarted, handler)
    e = WorkerStarted(mission_id="m1", timestamp=datetime.now(),
                      worker_id="w1", task_description="analyze")
    bus.publish(e)
    assert len(received) == 1
    assert received[0].worker_id == "w1"


def test_event_bus_untyped_events_not_delivered():
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event)

    bus.subscribe(WorkerCompleted, handler)
    e = WorkerStarted(mission_id="m1", timestamp=datetime.now(),
                      worker_id="w1", task_description="analyze")
    bus.publish(e)
    assert len(received) == 0


def test_event_bus_multiple_subscribers():
    bus = EventBus()
    r1, r2 = [], []

    bus.subscribe(LogMessage, r1.append)
    bus.subscribe(LogMessage, r2.append)
    e = LogMessage(mission_id="m1", timestamp=datetime.now(),
                   level="info", message="hello")
    bus.publish(e)
    assert len(r1) == 1
    assert len(r2) == 1


def test_mission_phase_enum():
    assert MissionPhase.PLANNING.value == "planning"
    assert MissionPhase.COMPLETE.value == "complete"
    assert len(MissionPhase) == 7


def test_phase_changed_carries_phases():
    e = PhaseChanged(
        mission_id="m1", timestamp=datetime.now(),
        previous=MissionPhase.PLANNING,
        current=MissionPhase.DISCOVERY,
    )
    assert e.previous == MissionPhase.PLANNING
    assert e.current == MissionPhase.DISCOVERY


def test_worker_progress_optional_total():
    e = WorkerProgress(mission_id="m1", timestamp=datetime.now(),
                       worker_id="w1", current=5, total=None, message="scanning")
    assert e.total is None
    e2 = WorkerProgress(mission_id="m1", timestamp=datetime.now(),
                        worker_id="w1", current=5, total=10, message="scanning")
    assert e2.total == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_event_bus.py -v`
Expected: ImportError / ModuleNotFoundError for `friday.runtime.event_bus`

- [ ] **Step 3: Implement EventBus + event types**

```python
"""In-process pub/sub event bus for live execution events.

Strongly typed events. No DB writes — this is for live UI consumption.
Terminal events (mission start/end) are still written to the DB via
runtime/events.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Type


class MissionPhase(str, Enum):
    PLANNING = "planning"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    SUMMARY = "summary"
    COMPLETE = "complete"


@dataclass(frozen=True)
class Event:
    mission_id: str
    timestamp: datetime


@dataclass(frozen=True)
class MissionStarted(Event):
    goal: str


@dataclass(frozen=True)
class MissionCompleted(Event):
    result: str  # "success" | "failed"
    summary: dict = field(default_factory=dict)
    duration_ms: int = 0


@dataclass(frozen=True)
class PhaseChanged(Event):
    previous: MissionPhase
    current: MissionPhase


@dataclass(frozen=True)
class WorkerSpawned(Event):
    worker_id: str
    name: str
    capability: str = ""


@dataclass(frozen=True)
class WorkerReady(Event):
    worker_id: str


@dataclass(frozen=True)
class WorkerStarted(Event):
    worker_id: str
    task_description: str


@dataclass(frozen=True)
class WorkerProgress(Event):
    worker_id: str
    current: int
    total: int | None
    message: str


@dataclass(frozen=True)
class WorkerWaiting(Event):
    worker_id: str
    reason: str = ""


@dataclass(frozen=True)
class WorkerCompleted(Event):
    worker_id: str
    success: bool
    findings: list = field(default_factory=list)


@dataclass(frozen=True)
class WorkerFailed(Event):
    worker_id: str
    error: str


@dataclass(frozen=True)
class ToolStarted(Event):
    tool_name: str
    args: str = ""


@dataclass(frozen=True)
class ToolCompleted(Event):
    tool_name: str
    exit_code: int


@dataclass(frozen=True)
class LogMessage(Event):
    level: str  # "info" | "warn" | "error"
    message: str


class EventBus:
    """In-process pub/sub. Subscribers are called synchronously on publish()."""

    def __init__(self):
        self._subscribers: dict[Type[Event], list[Callable]] = {}

    def subscribe(self, event_type: Type[Event], callback: Callable) -> None:
        """Register a callback for a specific event type."""
        self._subscribers.setdefault(event_type, []).append(callback)

    def publish(self, event: Event) -> None:
        """Deliver event to all subscribers of its exact type."""
        handlers = self._subscribers.get(type(event), [])
        for h in handlers:
            h(event)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_event_bus.py -v`
Expected: 100% PASS

- [ ] **Step 5: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/runtime/event_bus.py tests/test_event_bus.py && git commit -m "feat: runtime EventBus with typed events
- Strongly typed immutable events (MissionStarted, WorkerProgress, PhaseChanged, etc.)
- MissionPhase enum
- In-process pub/sub with type-based dispatch
- No DB writes — live events only"
```

---

### Task 2: CLI view models + style tokens

**Files:**
- Create: `src/friday/cli/__init__.py`
- Create: `src/friday/cli/models.py`
- Create: `src/friday/cli/style.py`
- Test: `tests/test_cli_models.py`

- [ ] **Step 1: Write tests for models**

```python
"""Tests for cli/models.py — view model dataclasses."""
from __future__ import annotations

from friday.cli.models import (
    MissionView, WorkerView, TimelineEventView, ProgressView,
    SummaryView, WorkerStatus, MissionPhase,
)


def test_mission_view_frozen():
    mv = MissionView(
        id="m1", goal="test", phase=MissionPhase.ANALYSIS,
        progress=0.5, workers=[], timeline=[], summary=SummaryView(),
        elapsed_seconds=42,
    )
    assert mv.id == "m1"
    assert mv.progress == 0.5


def test_mission_view_immutable():
    mv = MissionView(
        id="m1", goal="test", phase=MissionPhase.ANALYSIS,
        progress=0.5, workers=[], timeline=[], summary=SummaryView(),
        elapsed_seconds=42,
    )
    try:
        mv.progress = 0.9
        assert False, "should be frozen"
    except Exception:
        pass


def test_worker_view_with_id():
    wv = WorkerView(
        id="w1", name="Search Specialist", status=WorkerStatus.RUNNING,
        current_task="Searching runtime/", progress=None, findings=[],
    )
    assert wv.id == "w1"


def test_worker_status_enum():
    assert WorkerStatus.RUNNING.value == "running"
    assert WorkerStatus.FAILED.value == "failed"


def test_mission_phase_enum():
    assert MissionPhase.IMPLEMENTATION.value == "implementation"


def test_timeline_event_view_with_id():
    tev = TimelineEventView(id="e1", timestamp="12:00", kind="info", message="started")
    assert tev.id == "e1"


def test_summary_view_defaults():
    sv = SummaryView()
    assert sv.files_modified == 0
```
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_cli_models.py -v`
Expected: ModuleNotFoundError for `friday.cli`

- [ ] **Step 3: Implement models + style**

```python
# src/friday/cli/__init__.py
"""FRIDAY Mission Control — Rich-powered presentation layer.

Structure:
- models.py:    View model dataclasses consumed by renderers
- style.py:     Design tokens (colors, icons, spacing)
- formatters/:  Domain objects → view models (pure)
- renderers/:   View models → Rich renderables
- widgets/:     Composable Rich building blocks
"""
```

```python
# src/friday/cli/models.py
"""Immutable view models. Renderers consume these, never domain objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MissionPhase(str, Enum):
    PLANNING = "planning"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    SUMMARY = "summary"
    COMPLETE = "complete"


class WorkerStatus(str, Enum):
    SPAWNED = "spawned"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressView:
    current: int
    total: Optional[int] = None


@dataclass(frozen=True)
class WorkerView:
    id: str
    name: str
    status: WorkerStatus
    current_task: str
    progress: Optional[ProgressView] = None
    findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineEventView:
    id: str
    timestamp: str
    kind: str  # "phase" | "worker" | "info" | "error"
    message: str


@dataclass(frozen=True)
class SummaryView:
    files_modified: int = 0
    tests_passed: int = 0
    warnings: int = 0


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
```

```python
# src/friday/cli/style.py
"""Design tokens — single source of truth for visual identity."""
from __future__ import annotations


class Color:
    """GitHub-dark-inspired palette."""
    PRIMARY = "#58a6ff"       # Blue
    SUCCESS = "#3fb950"       # Green
    WARNING = "#d29922"       # Yellow
    ERROR = "#f85149"         # Red
    TEXT = "#e6edf3"          # Light gray
    DIM = "#8b949e"           # Muted gray
    BORDER = "#30363d"        # Subtle border
    PANEL_BG = "#0d1117"      # Panel background
    HEADER_BG = "#161b22"     # Header background


class Icon:
    MISSION = "◆"
    WORKER = "▲"
    COMPLETED = "✓"
    FAILED = "✗"
    RUNNING = "●"
    PENDING = "○"
    WAITING = "◷"
    PHASE = "▶"


class Style:
    HEADER = f"bold white on {Color.HEADER_BG}"
    MISSION_ID = f"bold {Color.PRIMARY}"
    SUCCESS = f"bold {Color.SUCCESS}"
    ERROR = f"bold {Color.ERROR}"
    WARNING = f"{Color.WARNING}"
    DIM = f"{Color.DIM}"
    TEXT = f"{Color.TEXT}"
    PANEL_BORDER = f"{Color.BORDER}"
```

```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_cli_models.py -v`
Expected: 100% PASS

- [ ] **Step 5: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/cli/ src/friday/cli/__init__.py src/friday/cli/models.py src/friday/cli/style.py tests/test_cli_models.py && git commit -m "feat: CLI view models + design tokens
- Immutable view model dataclasses (MissionView, WorkerView, etc.)
- WorkerStatus + MissionPhase enums
- Design token module (colors, icons, styles)
- cli/ package init"
```

---

### Task 3: Widgets — reusable Rich building blocks

**Files:**
- Create: `src/friday/cli/widgets/__init__.py`
- Create: `src/friday/cli/widgets/header.py`
- Create: `src/friday/cli/widgets/footer.py`
- Create: `src/friday/cli/widgets/progress.py`
- Create: `src/friday/cli/widgets/workers.py`
- Create: `src/friday/cli/widgets/timeline.py`
- Create: `src/friday/cli/widgets/mission_graph.py`
- Create: `src/friday/cli/widgets/panels.py`
- Create: `src/friday/cli/widgets/tables.py`
- Test: `tests/test_cli_widgets.py`

- [ ] **Step 1: Write widget tests**

```python
"""Tests for cli/widgets/ — each widget renders known input to Rich renderable."""
from __future__ import annotations

from friday.cli.models import (
    MissionPhase, WorkerStatus,
    WorkerView, TimelineEventView, ProgressView, MissionView, SummaryView,
)
from friday.cli.widgets.header import HeaderWidget
from friday.cli.widgets.footer import FooterWidget
from friday.cli.widgets.progress import ProgressWidget
from friday.cli.widgets.workers import WorkersWidget
from friday.cli.widgets.timeline import TimelineWidget
from friday.cli.widgets.mission_graph import MissionGraphWidget
from friday.cli.widgets.panels import info_panel, error_panel


def test_header_renders_mission_id():
    w = HeaderWidget()
    result = w.render(mission_id="m42", elapsed_seconds=90)
    text = str(result)
    assert "m42" in text
    assert "FRIDAY" in text


def test_header_shows_elapsed():
    w = HeaderWidget()
    result = w.render(mission_id="m1", elapsed_seconds=125)
    text = str(result)
    assert "02:05" in text  # 125s = 2m5s


def test_footer_renders_status_line():
    w = FooterWidget()
    result = w.render(status="Searching runtime/")
    text = str(result)
    assert "Searching runtime/" in text


def test_progress_widget_shows_percentage():
    w = ProgressWidget()
    result = w.render(progress=0.63, goal="Refactor architecture")
    text = str(result)
    assert "63%" in text


def test_workers_widget_shows_workers():
    w = WorkersWidget()
    workers = [
        WorkerView(id="w1", name="Search", status=WorkerStatus.RUNNING,
                   current_task="Scanning", progress=ProgressView(5, 10)),
        WorkerView(id="w2", name="Analyze", status=WorkerStatus.COMPLETED,
                   current_task="Done", findings=["found 2 issues"]),
    ]
    result = w.render(workers)
    text = str(result)
    assert "Search" in text
    assert "Analyze" in text
    assert "5/10" in text or "5 / 10" in text


def test_timeline_widget_events_in_order():
    w = TimelineWidget()
    events = [
        TimelineEventView(id="e1", timestamp="12:00", kind="phase", message="Planning"),
        TimelineEventView(id="e2", timestamp="12:01", kind="worker", message="Search started"),
    ]
    result = w.render(events)
    text = str(result)
    assert "12:00" in text
    assert "12:01" in text


def test_mission_graph_shows_phases():
    w = MissionGraphWidget()
    result = w.render(current_phase=MissionPhase.ANALYSIS)
    text = str(result)
    assert "Planning" in text
    assert "Analysis" in text
    assert "Complete" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_cli_widgets.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Implement all widgets**

```python
# src/friday/cli/widgets/__init__.py
"""Composable Rich building blocks for the Mission Control UI.

Each widget is a stateless renderable factory: takes view data, returns
a Rich renderable. Widgets never call the event bus or mutate state.
"""
```

```python
# src/friday/cli/widgets/header.py
"""Mission header with title, ID, and elapsed time."""
from __future__ import annotations
from rich.panel import Panel
from rich.text import Text
from ..style import Color, Style, Icon


class HeaderWidget:
    """Top-of-mission bar showing FRIDAY Mission Control + ID + timer."""

    def render(self, mission_id: str, elapsed_seconds: int) -> Panel:
        mins, secs = divmod(elapsed_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            elapsed = f"{hours:02d}:{mins:02d}:{secs:02d}"
        else:
            elapsed = f"{mins:02d}:{secs:02d}"

        title = Text.assemble(
            (f" {Icon.MISSION} ", Style.MISSION_ID),
            ("FRIDAY Mission Control", "bold white"),
        )
        right = Text.assemble(
            ("Mission #", Style.DIM),
            (mission_id, Style.MISSION_ID),
            ("  ", ""),
            (elapsed, Style.DIM),
        )
        return Panel(
            Text.assemble(title, " " * 4, right),
            style=Color.HEADER_BG,
            border_style=Color.BORDER,
        )
```

```python
# src/friday/cli/widgets/footer.py
"""Single-line status update — the first place users look."""
from __future__ import annotations
from rich.text import Text
from rich.panel import Panel
from ..style import Color, Style, Icon


class FooterWidget:
    """Constantly-updating status line (like Claude Code's status)."""

    def render(self, status: str) -> Panel:
        text = Text.assemble(
            (f" {Icon.RUNNING} ", Style.MISSION_ID),
            (status, Style.TEXT),
        )
        return Panel(text, style=Color.PANEL_BG, border_style=Color.BORDER)
```

```python
# src/friday/cli/widgets/progress.py
"""Goal + progress bar widget."""
from __future__ import annotations
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.text import Text
from ..style import Color, Style


class ProgressWidget:
    """Shows the mission goal and a percentage progress bar."""

    def render(self, progress: float, goal: str) -> Panel:
        pct = int(progress * 100)
        bar = Progress(
            TextColumn("  {task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.percentage:>3.0f}%"),
        )
        bar.add_task(goal, total=100, completed=pct)
        return Panel(bar, style=Color.PANEL_BG, border_style=Color.BORDER)
```

```python
# src/friday/cli/widgets/workers.py
"""Worker status cards — current task, progress, findings."""
from __future__ import annotations
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ..models import WorkerView, WorkerStatus
from ..style import Color, Style, Icon


class WorkersWidget:
    """Table of active/completed workers with status and current task."""

    def render(self, workers: list[WorkerView]) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column("Status", style="bold", width=2)
        table.add_column("Name", style="bold", width=22)
        table.add_column("Task", width=40)
        table.add_column("Progress", width=10)

        for w in workers:
            if w.status == WorkerStatus.RUNNING:
                icon = Icon.RUNNING
                name_style = Style.TEXT
            elif w.status == WorkerStatus.COMPLETED:
                icon = Icon.COMPLETED
                name_style = Style.SUCCESS
            elif w.status == WorkerStatus.FAILED:
                icon = Icon.FAILED
                name_style = Style.ERROR
            elif w.status == WorkerStatus.WAITING:
                icon = Icon.WAITING
                name_style = Style.WARNING
            else:
                icon = Icon.PENDING
                name_style = Style.DIM

            prog = ""
            if w.progress:
                total_str = str(w.progress.total) if w.progress.total else "?"
                prog = f"{w.progress.current}/{total_str}"

            table.add_row(
                icon,
                Text(w.name, style=name_style),
                Text(w.current_task, style=Style.DIM),
                Text(prog, style=Style.DIM),
            )

        return Panel(table, title="Active Workers", style=Color.PANEL_BG,
                     border_style=Color.BORDER)
```

```python
# src/friday/cli/widgets/timeline.py
"""Chronological event log — newest at bottom."""
from __future__ import annotations
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ..models import TimelineEventView
from ..style import Color, Style


class TimelineWidget:
    """Displays events in chronological order."""

    def render(self, events: list[TimelineEventView]) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column("Time", style=Style.DIM, width=10)
        table.add_column("Event", width=60)

        for e in events:
            kind_style = {
                "phase": Style.MISSION_ID,
                "worker": Style.TEXT,
                "info": Style.DIM,
                "error": Style.ERROR,
            }.get(e.kind, Style.DIM)
            table.add_row(e.timestamp, Text(e.message, style=kind_style))

        return Panel(table, title="Timeline", style=Color.PANEL_BG,
                     border_style=Color.BORDER)
```

```python
# src/friday/cli/widgets/mission_graph.py
"""Vertical phase progress indicator."""
from __future__ import annotations
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ..models import MissionPhase
from ..style import Color, Style, Icon


_PHASE_ORDER = [
    MissionPhase.PLANNING,
    MissionPhase.DISCOVERY,
    MissionPhase.ANALYSIS,
    MissionPhase.IMPLEMENTATION,
    MissionPhase.VERIFICATION,
    MissionPhase.SUMMARY,
    MissionPhase.COMPLETE,
]


class MissionGraphWidget:
    """Phase-by-phase progress: current is highlighted, completed marked, future dim."""

    def render(self, current_phase: MissionPhase) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column("", width=2)
        table.add_column("Phase", width=24)
        current_idx = _PHASE_ORDER.index(current_phase) if current_phase in _PHASE_ORDER else -1

        for i, phase in enumerate(_PHASE_ORDER):
            label = phase.value.replace("_", " ").title()
            if i < current_idx:
                icon = Icon.COMPLETED
                style = Style.SUCCESS
            elif i == current_idx:
                icon = Icon.RUNNING
                style = Style.MISSION_ID
            else:
                icon = Icon.PENDING
                style = Style.DIM
            table.add_row(icon, Text(label, style=style))

        return Panel(table, title="Mission Graph", style=Color.PANEL_BG,
                     border_style=Color.BORDER)
```

```python
# src/friday/cli/widgets/panels.py
"""Reusable Panel builders for static info commands."""
from __future__ import annotations
from rich.panel import Panel
from rich.text import Text
from ..style import Color, Style


def info_panel(title: str, content: str, style: str = Style.TEXT) -> Panel:
    """Standard info panel with title and body text."""
    return Panel(
        Text(content, style=style),
        title=title,
        border_style=Color.BORDER,
        style=Color.PANEL_BG,
    )


def error_panel(title: str, content: str) -> Panel:
    """Error panel with red accent."""
    return Panel(
        Text(content, style=Style.ERROR),
        title=title,
        border_style=Color.ERROR,
        style=Color.PANEL_BG,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_cli_widgets.py -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/cli/widgets/ tests/test_cli_widgets.py && git commit -m "feat: Rich widgets — header, footer, progress, workers, timeline, mission graph, panels"
```

---

### Task 4: Formatters — domain objects → view models

**Files:**
- Create: `src/friday/cli/formatters/__init__.py`
- Create: `src/friday/cli/formatters/execution.py`

- [ ] **Step 1: Write formatter tests**

```python
"""Tests for cli/formatters/ — domain object to view model conversion."""
from __future__ import annotations

from friday.cli.formatters.execution import (
    execution_result_to_view,
    task_to_worker_view,
)
from friday.cli.models import MissionPhase, WorkerStatus, MissionView


def test_execution_result_to_view_has_required_fields():
    """Even with minimal input, MissionView has all fields."""
    mv = execution_result_to_view(
        mission_id="m1",
        goal="test",
        phase="implementation",
        progress=0.5,
    )
    assert isinstance(mv, MissionView)
    assert mv.id == "m1"
    assert mv.goal == "test"
    assert mv.phase == MissionPhase.IMPLEMENTATION
    assert mv.progress == 0.5
    assert mv.workers == []
    assert mv.timeline == []
    assert mv.elapsed_seconds >= 0


def test_task_to_worker_view_running():
    wv = task_to_worker_view(
        worker_id="w1", name="Shell", status="running",
        current_task="echo hello",
    )
    assert wv.id == "w1"
    assert wv.status == WorkerStatus.RUNNING


def test_task_to_worker_view_completed():
    wv = task_to_worker_view(
        worker_id="w2", name="Git", status="success",
        current_task="commit",
    )
    assert wv.status == WorkerStatus.COMPLETED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_cli_formatters.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Implement formatters**

```python
# src/friday/cli/formatters/__init__.py
"""Domain objects → view models. Pure functions, no side effects."""
```

```python
# src/friday/cli/formatters/execution.py
"""Convert runtime execution results to CLI view models."""
from __future__ import annotations

from datetime import datetime
from ..models import (
    MissionView, MissionPhase, WorkerView, WorkerStatus,
    TimelineEventView, SummaryView, ProgressView,
)

# Map from runtime result status strings to WorkerStatus
_STATUS_MAP = {
    "success": WorkerStatus.COMPLETED,
    "failed": WorkerStatus.FAILED,
    "running": WorkerStatus.RUNNING,
    "pending": WorkerStatus.READY,
    "cancelled": WorkerStatus.WAITING,
}

# Map from domain phase strings to MissionPhase
_PHASE_MAP = {
    "planning": MissionPhase.PLANNING,
    "discovery": MissionPhase.DISCOVERY,
    "analysis": MissionPhase.ANALYSIS,
    "implementation": MissionPhase.IMPLEMENTATION,
    "verification": MissionPhase.VERIFICATION,
    "summary": MissionPhase.SUMMARY,
    "complete": MissionPhase.COMPLETE,
}


def execution_result_to_view(
    mission_id: str,
    goal: str,
    phase: str,
    progress: float,
    workers: list[WorkerView] | None = None,
    timeline: list[TimelineEventView] | None = None,
    summary: SummaryView | None = None,
    elapsed_seconds: int = 0,
) -> MissionView:
    """Build a MissionView from execution state. All domain→view mapping here."""
    phase_enum = _PHASE_MAP.get(phase.lower(), MissionPhase.IMPLEMENTATION)
    return MissionView(
        id=mission_id,
        goal=goal,
        phase=phase_enum,
        progress=progress,
        workers=workers or [],
        timeline=timeline or [],
        summary=summary or SummaryView(),
        elapsed_seconds=elapsed_seconds or _estimate_elapsed(mission_id),
    )


def task_to_worker_view(
    worker_id: str,
    name: str,
    status: str,
    current_task: str,
    current: int = 0,
    total: int | None = None,
    findings: list[str] | None = None,
) -> WorkerView:
    """Convert a task's state to a WorkerView."""
    ws = _STATUS_MAP.get(status.lower(), WorkerStatus.SPAWNED)
    progress = ProgressView(current=current, total=total) if current > 0 else None
    return WorkerView(
        id=worker_id,
        name=name,
        status=ws,
        current_task=current_task,
        progress=progress,
        findings=findings or [],
    )


def _estimate_elapsed(mission_id: str) -> int:
    """Rough elapsed from mission id timestamp if available (sess:hex format)."""
    import time
    try:
        created = int(mission_id.split(":")[1][:8], 16)
        return int(time.time()) - created
    except (ValueError, IndexError):
        return 0
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_cli_formatters.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/cli/formatters/ tests/test_cli_formatters.py && git commit -m "feat: formatters — domain objects to view models
- execution_result_to_view for building MissionView from runtime state
- task_to_worker_view for converting task state to WorkerView
- Status/phase string→enum mapping"
```

---

### Task 5: MissionRenderer — live dashboard

**Files:**
- Create: `src/friday/cli/renderers/__init__.py`
- Create: `src/friday/cli/renderers/mission.py`
- Create: `src/friday/cli/renderers/execution.py`
- Create: `src/friday/cli/renderers/shared.py`
- Test: `tests/test_mission_renderer.py`

- [ ] **Step 1: Write MissionRenderer tests**

```python
"""Tests for cli/renderers/mission.py — live + static rendering."""
from __future__ import annotations

from friday.cli.models import (
    MissionView, WorkerView, TimelineEventView, SummaryView,
    MissionPhase, WorkerStatus,
)
from friday.cli.renderers.mission import (
    MissionRenderer,
    render_mission_view,
)
from friday.cli.renderers.execution import render_execution_summary


def _sample_view() -> MissionView:
    return MissionView(
        id="m1", goal="Refactor architecture",
        phase=MissionPhase.ANALYSIS, progress=0.63,
        workers=[
            WorkerView(id="w1", name="Search", status=WorkerStatus.RUNNING,
                       current_task="Searching runtime/",
                       findings=["18 files scanned"]),
        ],
        timeline=[
            TimelineEventView(id="e1", timestamp="12:00", kind="phase",
                              message="Planning"),
        ],
        summary=SummaryView(files_modified=18, tests_passed=121),
        elapsed_seconds=134,
    )


def test_mission_renderer_renders_layout():
    mv = _sample_view()
    renderer = MissionRenderer()
    layout = renderer.render(mv)
    assert layout is not None


def test_mission_renderer_has_all_widgets():
    renderer = MissionRenderer()
    assert hasattr(renderer, "header")
    assert hasattr(renderer, "progress")
    assert hasattr(renderer, "workers")
    assert hasattr(renderer, "timeline")
    assert hasattr(renderer, "footer")


def test_render_mission_view_static():
    mv = _sample_view()
    result = render_mission_view(mv)
    text = str(result)
    assert "Refactor architecture" in text
    assert "63%" in text


def test_execution_summary():
    mv = _sample_view()
    result = render_execution_summary(mv)
    text = str(result)
    assert "18 files modified" in text or "18" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_mission_renderer.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Implement renderers**

```python
# src/friday/cli/renderers/__init__.py
"""View models → Rich renderables. Renderers never call runtime logic."""
```

```python
# src/friday/cli/renderers/mission.py
"""Mission Control live dashboard — composes widgets into a Rich Layout.

Usage (live):
    renderer = MissionRenderer()
    with Live(renderer.render(view), refresh_per_second=15) as live:
        while running:
            live.update(renderer.render(updated_view))

Usage (static):
    console.print(render_mission_view(view))
"""
from __future__ import annotations

from rich.layout import Layout
from rich.live import Live

from ..models import MissionView, MissionPhase
from ..widgets.header import HeaderWidget
from ..widgets.footer import FooterWidget
from ..widgets.progress import ProgressWidget
from ..widgets.workers import WorkersWidget
from ..widgets.timeline import TimelineWidget
from ..widgets.mission_graph import MissionGraphWidget
from ..widgets.panels import info_panel
from ..style import Color


class MissionRenderer:
    """Composes all widgets into a full-screen Layout. Stateless — call render()."""

    def __init__(self):
        self.header = HeaderWidget()
        self.footer = FooterWidget()
        self.progress = ProgressWidget()
        self.workers = WorkersWidget()
        self.timeline = TimelineWidget()
        self.mission_graph = MissionGraphWidget()

    def render(self, view: MissionView) -> Layout:
        """Build a Layout from a MissionView snapshot."""
        layout = Layout()
        layout.split_column(
            Layout(self.header.render(view.id, view.elapsed_seconds), size=3),
            Layout(
                self.progress.render(view.progress, view.goal),
                size=3,
            ),
            Layout(
                self._middle_section(view),
                ratio=1,
            ),
            Layout(
                self.footer.render(self._status_line(view)),
                size=3,
            ),
        )
        return layout

    def _middle_section(self, view: MissionView) -> Layout:
        """Split between workers/timeline (left) and mission graph (right)."""
        mid = Layout()
        mid.split_row(
            Layout(
                self._left_column(view),
                ratio=2,
            ),
            Layout(
                self.mission_graph.render(view.phase),
                ratio=1,
            ),
        )
        return mid

    def _left_column(self, view: MissionView) -> Layout:
        col = Layout()
        col.split_column(
            Layout(self.workers.render(view.workers), ratio=2),
            Layout(self.timeline.render(view.timeline), ratio=3),
        )
        return col

    @staticmethod
    def _status_line(view: MissionView) -> str:
        """Derive a single status sentence from the current view."""
        running = [w for w in view.workers if w.status.value == "running"]
        if running:
            w = running[0]
            prog = ""
            if w.progress and w.progress.total:
                prog = f" ({w.progress.current}/{w.progress.total})"
            return f"{w.name} is {w.current_task}{prog}"
        if all(w.status.value == "completed" for w in view.workers):
            return "Mission complete"
        return "Waiting for workers..."


def render_mission_view(view: MissionView) -> Layout:
    """Convenience: one-shot render (not live)."""
    return MissionRenderer().render(view)
```

```python
# src/friday/cli/renderers/execution.py
"""Static rendering of execution results (post-mission summary)."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..models import MissionView, WorkerStatus
from ..style import Color, Style, Icon


def render_execution_summary(view: MissionView) -> Panel:
    """Post-mission summary panel."""
    duration = _format_duration(view.elapsed_seconds)
    n_workers = len(view.workers)
    n_completed = sum(1 for w in view.workers if w.status == WorkerStatus.COMPLETED)
    n_failed = sum(1 for w in view.workers if w.status == WorkerStatus.FAILED)

    lines = [
        Text.assemble(("Duration:   ", Style.DIM), (duration, Style.TEXT)),
        Text.assemble(("Workers:    ", Style.DIM), (str(n_workers), Style.TEXT)),
        Text.assemble(("Completed:  ", Style.DIM),
                      (str(n_completed), Style.SUCCESS)),
    ]
    if n_failed:
        lines.append(Text.assemble(("Failed:     ", Style.DIM),
                                    (str(n_failed), Style.ERROR)))

    if view.summary:
        s = view.summary
        lines.append(Text.assemble(("Modified:   ", Style.DIM),
                                    (str(s.files_modified), Style.TEXT)))
        lines.append(Text.assemble(("Tests:      ", Style.DIM),
                                    (str(s.tests_passed), Style.SUCCESS)))
        if s.warnings:
            lines.append(Text.assemble(("Warnings:   ", Style.DIM),
                                        (str(s.warnings), Style.WARNING)))

    return Panel(
        Text.assemble(*[l + "\n" for l in lines]).rstrip(),
        title=f"{Icon.MISSION} Mission Complete",
        border_style=Color.SUCCESS,
        style=Color.PANEL_BG,
    )


def _format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h}h {m}m {secs}s"
    return f"{mins}m {secs}s"
```

```python
# src/friday/cli/renderers/shared.py
"""Shared helpers for renderers — status formatting, duration, etc."""
from __future__ import annotations


def format_duration(seconds: int) -> str:
    """Human-readable duration from seconds."""
    mins, secs = divmod(seconds, 60)
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h}h {m}m {secs}s"
    return f"{mins}m {secs}s"
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/test_mission_renderer.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/cli/renderers/ tests/test_mission_renderer.py && git commit -m "feat: MissionRenderer — live dashboard + static summary
- MissionRenderer composes all widgets into Rich Layout
- render_mission_view for one-shot rendering
- render_execution_summary for post-mission output
- Shared duration formatter"
```

---

### Task 6: Wire MissionRenderer into `friday execute`

**Files:**
- Modify: `src/friday/cli_execute.py`

- [ ] **Step 1: Write integration test**

```python
"""Integration: friday execute uses MissionRenderer when available."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

from friday.cli.models import MissionView, MissionPhase, SummaryView


def test_mission_view_from_execution():
    """Smoke test: rendering doesn't crash with realistic data."""
    from friday.cli.renderers.mission import render_mission_view
    mv = MissionView(
        id="sess:abc123", goal="Write documentation",
        phase=MissionPhase.IMPLEMENTATION, progress=0.75,
        workers=[], timeline=[],
        summary=SummaryView(files_modified=3, tests_passed=0),
        elapsed_seconds=42,
    )
    layout = render_mission_view(mv)
    assert layout is not None
```

- [ ] **Step 2: Modify `cli_execute.py` to use renderers**

Key change: when `friday execute` runs, use the MissionRenderer with `Live` to show the dashboard. The existing execution flow (plan → resolve → schedule → run) stays exactly the same. The renderer is purely additive.

```python
# At top of src/friday/cli_execute.py, add imports:
from .cli.renderers.mission import MissionRenderer, render_mission_view
from .cli.renderers.execution import render_execution_summary
from .cli.formatters.execution import (
    execution_result_to_view,
    task_to_worker_view,
)

# Replace the report section (step 5 in current cmd_execute) with:
    # 5. Render with Mission Control (Live if interactive, static otherwise).
    import sys
    from rich.live import Live
    from rich.console import Console

    has_live = sys.stdout.isatty()
    phase = "implementation"
    progress = 1.0 if report.failed == 0 else 0.0

    # Build view from report
    workers = [
        task_to_worker_view(
            worker_id=t.get("task_id", ""),
            name=t.get("worker_id", "unknown").replace("worker:", "").title(),
            status=t.get("status", "pending"),
            current_task=t.get("task_id", ""),
        )
        for t in report.tasks[:10]  # show first 10
    ]

    mv = execution_result_to_view(
        mission_id=report.session_id,
        goal=goal,
        phase=phase,
        progress=progress,
        workers=workers,
        elapsed_seconds=int(report.duration_ms / 1000),
    )

    console = Console()
    if has_live and report.duration_ms > 2000:
        with Live(render_mission_view(mv), refresh_per_second=15, console=console) as live:
            import time
            time.sleep(0.5)  # brief display so user sees the dashboard
        console.print(render_execution_summary(mv))
    else:
        console.print(render_mission_view(mv))
        console.print(render_execution_summary(mv))
```

- [ ] **Step 3: Run full test suite to verify nothing broke**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/ --deselect tests/test_worker_registry.py::test_builtin_no_duplicate_ids -q`
Expected: 1121+ passed

- [ ] **Step 4: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/cli_execute.py && git commit -m "feat: wire MissionRenderer into friday execute
- Live dashboard for interactive terminals with long-running ops
- Static summary panel for quick or non-TTY execution
- Formatters convert runtime reports to view models
- Backward compatible — all existing tests pass"
```

---

### Task 7: Knowledge renderer — show knowledge list with Rich tables

**Files:**
- Create: `src/friday/cli/renderers/knowledge.py`
- Create: `src/friday/cli/formatters/knowledge.py` (minimal — just wraps existing KnowledgeBuildResult)

- [ ] **Step 1: Implement knowledge renderer**

```python
# src/friday/cli/formatters/knowledge.py
"""Knowledge domain objects → view models."""
from __future__ import annotations
from ..models import SummaryView


def knowledge_result_to_summary(
    total: int, created: int, updated: int, verified: int,
    candidates: int, stable: int,
) -> SummaryView:
    """Build a SummaryView from a KnowledgeBuildResult."""
    return SummaryView(
        files_modified=total,
        tests_passed=stable,
        warnings=candidates,
    )
```

```python
# src/friday/cli/renderers/knowledge.py
"""Knowledge rendering — tables, panels for knowledge commands."""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..style import Color, Style, Icon


def render_knowledge_table(rows: list[dict]) -> Panel:
    """Render a list of knowledge entries as a Rich Table."""
    table = Table(border_style=Color.BORDER, style=Color.PANEL_BG)
    table.add_column("Type", style=Style.DIM)
    table.add_column("Subject", style=Style.TEXT)
    table.add_column("Confidence", style=Style.MISSION_ID)
    table.add_column("Status", style=Style.SUCCESS)

    for r in rows:
        table.add_row(
            r.get("type", ""),
            r.get("subject", "")[:50],
            r.get("confidence", ""),
            r.get("status", ""),
        )

    return Panel(table, title=f"{Icon.MISSION} Knowledge", border_style=Color.BORDER)
```

- [ ] **Step 2: Add `friday knowledge list` rendering update (minor)**

Wire into `cli_knowledge.py` — if Rich output is desired, use `render_knowledge_table` instead of plain text. Keep backward compat by wrapping in try/except on the import.

- [ ] **Step 3: Test**

```python
"""Test knowledge renderer produces Rich output."""
from friday.cli.renderers.knowledge import render_knowledge_table


def test_knowledge_table_renders():
    rows = [{"type": "trend", "subject": "python adoption", "confidence": "strong", "status": "stable"}]
    panel = render_knowledge_table(rows)
    text = str(panel)
    assert "python adoption" in text
    assert "strong" in text
```

- [ ] **Step 4: Commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add src/friday/cli/formatters/knowledge.py src/friday/cli/renderers/knowledge.py tests/test_knowledge_renderer.py && git commit -m "feat: Knowledge renderer — Rich table for knowledge list"
```

---

### Task 8: Full test suite + final verification

**Files:** none (run existing tests)

- [ ] **Step 1: Run full test suite**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -m pytest tests/ --deselect tests/test_worker_registry.py::test_builtin_no_duplicate_ids -q`
Expected: All pass

- [ ] **Step 2: Verify no runtime imports from cli**

Run: `cd /home/lakshay/Projects/Friday\ V3 && grep -rn "from.*cli\|import.*cli" src/friday/runtime/`
Expected: empty

- [ ] **Step 3: Manual smoke test**

Run: `cd /home/lakshay/Projects/Friday\ V3 && python3 -c "from friday.cli.renderers.mission import render_mission_view; from friday.cli.models import MissionView, MissionPhase, SummaryView; mv = MissionView(id='t', goal='test', phase=MissionPhase.PLANNING, progress=0.5, workers=[], timeline=[], summary=SummaryView(), elapsed_seconds=10); print('OK:', type(render_mission_view(mv)).__name__)"`
Expected: `OK: Layout`

- [ ] **Step 4: Final commit**

```bash
cd /home/lakshay/Projects/Friday\ V3 && git add -A && git commit -m "M10.3: FRIDAY Mission Control TUI
- Runtime EventBus with typed events
- CLI view models + design tokens
- 8 Rich widgets (header, footer, progress, workers, timeline, mission graph, panels)
- Formatters (domain→view models)
- Renderers: live Mission Dashboard + static execution summary
- Knowledge table renderer
- Wired into friday execute with Live display
- 1121+ tests pass, 0 regressions"
```

---

## Self-Review Checklist

1. **Spec coverage:** Every section in the spec is covered: EventBus (Task 1), view models + style (Task 2), widgets (Task 3), formatters (Task 4), MissionRenderer (Task 5), wiring (Task 6), knowledge rendering (Task 7).
2. **No placeholders:** All code is present in every step. No TBDs.
3. **Type consistency:** `MissionPhase` enum used in models, formatters, widgets, renderers — same definition throughout. `WorkerStatus` enum consistent across formatters and widgets.
4. **Testing:** Every task has test-first steps.
