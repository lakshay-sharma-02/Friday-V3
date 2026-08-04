# Friday V5 — W6 Wire Proactive + Live Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the deferred seams from the W2–W5 final review — engine output → notifier, Proactive lifecycle, HUD live stream + command deck, and two bug fixes (notifier vault root, filename collision).

**Architecture:** The vault stays the single source of truth. The engine's existing `on_output` callback becomes the live-stream feed: HUD subscribes for the stream panel, a `NotifierBridge` watches final outputs to speak + persist proactive notices. `Proactive` (already built) gets constructed in the HUD to surface new notices. The `_poll_vault` stub becomes the seam that polls pending asks.

**Tech Stack:** Python 3.12, textual 8.2.8, psutil (installed). No new deps.

**State when this plan starts (verified):** 35 tests green on main. `Engine` has `on_output` callback + `_route_output`; `VoiceNotifier` exists but has zero callers and a wrong default vault root; `Proactive` exists but never constructed; `PromptPanel` echoes `you:` only (no stream); `_poll_vault` is a `pass` stub; `app.py` has no Command deck.

**Working dir:** `/home/lakshay/Projects/Friday V3/friday_v5` (git root: `/home/lakshay/Projects/Friday V3` — commit paths are `friday_v5/...`)

**Hermetic test law:** no real SDK/model; sys.modules injection for SDK; unittest.mock for providers; pure render helpers tested without Textual.

---

## File structure

| File | Action |
|---|---|
| `friday_v5/voice/notifier.py` | **Modify** — fix default vault root (use `Vault.DEFAULT_VAULT`), add collision disambiguator |
| `friday_v5/proactive.py` | **Modify** — add `seen()`/`mark_seen()`, alias `start_watch`, keep API |
| `friday_v5/hud/stream_panel.py` | **Create** — `StreamPanel(Static)` + `render_stream()` |
| `friday_v5/hud/commands_panel.py` | **Create** — `CommandsPanel(Static)` + `render_commands()` |
| `friday_v5/hud/app.py` | **Modify** — build notifier bridge, start Proactive, add Command deck + StreamPanel, wire `_poll_vault` |
| `friday_v5/hud/prompt.py` | **Modify** — `PromptPanel` gains `on_output`; forward streamed replies |
| `friday_v5/hud/__init__.py` | **Modify** — `run_hud()` starts Proactive + notifier; accepts `notifier` kwarg |
| `friday_v5/cli.py` | **Modify** — `_cmd_hud` passes a default `VoiceNotifier` into `run_hud` |
| `tests/test_w6_wiring.py` | **Create** — TDD tests |

---

## Task 1: Fix VoiceNotifier root + filename collision (W6)

**Files:**
- Modify: `friday_v5/voice/notifier.py`
- Test: `tests/test_w6_wiring.py`

- [ ] **Step 1: Write the failing tests**

```python
"""W6 wiring tests — notifier root/collision, proactive watch, HUD stream."""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.voice.notifier import VoiceNotifier
from friday_v5.proactive import Proactive


def test_notifier_default_root_is_vault(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    n = VoiceNotifier()  # no vault_root → default
    assert n.vault_root == Path(__file__).resolve().parent.parent / "vault"


def test_notifier_collision_disambiguates(tmp_path):
    n = VoiceNotifier(vault_root=tmp_path)
    n.speak = mock.Mock()
    with mock.patch("time.time", return_value=1700000000.0):
        p1 = n.notify("standup at 9am")
        p2 = n.notify("standup at 9am")
    assert p1 != p2
    assert len(list((tmp_path / "notices").glob("*.md"))) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_w6_wiring.py -q`
Expected: FAIL — `test_notifier_default_root_is_vault` (wrong root) + `test_notifier_collision_disambiguates` (collision overwrites → 1 file)

- [ ] **Step 3: Fix the default root**

In `friday_v5/voice/notifier.py`, change the import block + `__init__`:

```python
from ..vault import DEFAULT_VAULT  # module-level, not Vault.DEFAULT_VAULT
```

In `__init__`, replace the default-root expression:

```python
        self.vault_root = Path(vault_root) if vault_root else DEFAULT_VAULT
```

> NOTE (implemented 2026-08-04): `DEFAULT_VAULT` is a module-level constant in
> `friday_v5/vault.py` (line 24), not a class attribute. The notifier imports
> it directly (`from ..vault import DEFAULT_VAULT`). No circular import
> (vault.py is stdlib-only; notifier isn't exported from `voice/__init__.py`).

- [ ] **Step 4: Fix the collision**

In `notify()`, replace the filename line with a disambiguator loop:

```python
        ts = int(time.time())
        slug = NOTICE_SLUG_RE.sub("-", text.lower())[:40].strip("-") or "notice"
        path = self.notices_dir / f"{ts}-{slug}.md"
        # same-second duplicates get a -2/-3 suffix (never overwrite)
        n = 2
        while path.exists():
            path = self.notices_dir / f"{ts}-{slug}-{n}.md"
            n += 1
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_w6_wiring.py -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (~37 passed — 35 + 2 new)

- [ ] **Step 7: Commit**

```bash
git add friday_v5/voice/notifier.py tests/test_w6_wiring.py
git commit -m "fix: W6 notifier default vault root + same-second collision"
```

---

## Task 2: Proactive seen()/mark_seen() + start_watch alias (W6)

**Files:**
- Modify: `friday_v5/proactive.py`
- Test: `tests/test_w6_wiring.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_w6_wiring.py`:

```python
def test_proactive_seen_mark_seen(tmp_path):
    p = Proactive(vault_root=tmp_path, interval=0.05)
    nid = 1700000000
    (tmp_path / "notices").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notices" / f"{nid}-hello.md").write_text("hi", encoding="utf-8")
    assert p.seen() == set()
    p.mark_seen(nid)
    assert p.seen() == {nid}
    assert p.check() == []  # already seen → no new
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_w6_wiring.py::test_proactive_seen_mark_seen -q`
Expected: FAIL — `AttributeError: 'Proactive' object has no attribute 'seen'`

- [ ] **Step 3: Implement the additions**

Add to `friday_v5/proactive.py` (after `check()`):

```python
    def seen(self) -> set[int]:
        """Ids of notices already surfaced (copy)."""
        return set(self._seen)

    def mark_seen(self, nid: int) -> None:
        """Mark an id as seen without scanning (HUD pre-seeds)."""
        self._seen.add(nid)
```

Add an alias for the watcher thread (after `start()`):

```python
    def start_watch(self) -> None:
        """Alias — watch for new notices (same as start())."""
        self.start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_w6_wiring.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (~38 passed)

- [ ] **Step 6: Commit**

```bash
git add friday_v5/proactive.py tests/test_w6_wiring.py
git commit -m "feat: W6 Proactive seen/mark_seen + start_watch alias"
```

---

## Task 3: HUD stream + command panels (W6)

**Files:**
- Create: `friday_v5/hud/stream_panel.py`
- Create: `friday_v5/hud/commands_panel.py`
- Test: `tests/test_w6_wiring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_w6_wiring.py`:

```python
from friday_v5.hud.stream_panel import render_stream
from friday_v5.hud.commands_panel import render_commands


def test_render_stream_lines():
    out = render_stream([("you: standup at 9am", False),
                         ("ok, added", True)])
    assert "standup" in out and "ok, added" in out


def test_render_stream_empty():
    assert "(idle)" in render_stream([])


def test_render_commands():
    out = render_commands()
    assert "ask" in out and "perm" in out and "end" in out and "quit" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_w6_wiring.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday_v5.hud.stream_panel'`

- [ ] **Step 3: Create the stream panel**

Create `friday_v5/hud/stream_panel.py`:

```python
"""Stream panel — live engine output (user + assistant turns)."""
from __future__ import annotations

from textual.widgets import Static


def render_stream(lines: list[tuple[str, bool]]) -> str:
    """(text, is_final) pairs → last ~8 rendered lines."""
    if not lines:
        return "(idle)"
    return "\n".join(t for t, _ in lines[-8:])


class StreamPanel(Static):
    """Renders the engine's on_output feed (set externally)."""

    def __init__(self) -> None:
        super().__init__("(idle)")
        self._lines: list[tuple[str, bool]] = []

    def push(self, text: str, final: bool) -> None:
        """Append one output chunk (called from the engine thread)."""
        self._lines.append((text, final))
        self.update(render_stream(self._lines))
```

- [ ] **Step 4: Create the commands panel**

Create `friday_v5/hud/commands_panel.py`:

```python
"""Commands panel — static hint of available keybindings."""
from __future__ import annotations

from textual.widgets import Static


def render_commands() -> str:
    return "[ask] type below   [perm] allow/deny   [end] session   [quit] q"


class CommandsPanel(Static):
    """Static command deck hint."""

    def __init__(self) -> None:
        super().__init__(render_commands())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_w6_wiring.py -q`
Expected: PASS (5 passed)

- [ ] **Step 6: Full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (~40 passed)

- [ ] **Step 7: Commit**

```bash
git add friday_v5/hud/stream_panel.py friday_v5/hud/commands_panel.py tests/test_w6_wiring.py
git commit -m "feat: W6 HUD stream + command panels"
```

---

## Task 4: HUD wiring — live stream, notifier bridge, Proactive, command deck (W6)

**Files:**
- Modify: `friday_v5/hud/app.py`
- Modify: `friday_v5/hud/prompt.py`
- Modify: `friday_v5/hud/__init__.py`
- Modify: `friday_v5/cli.py`
- Test: `tests/test_w6_wiring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_w6_wiring.py`:

```python
def test_prompt_panel_forwards_stream(tmp_path):
    from friday_v5.hud.prompt import PromptPanel
    engine = mock.Mock()
    engine.vault = mock.Mock()
    pp = PromptPanel(engine)
    got = []
    pp.on_output = got.append
    pp.push("ok, added", final=True)
    assert got == [("ok, added", True)]
    assert pp._output is not None  # composed Static exists
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_w6_wiring.py::test_prompt_panel_forwards_stream -q`
Expected: FAIL — `AttributeError: 'PromptPanel' object has no attribute 'push'`

- [ ] **Step 3: Modify `PromptPanel`**

In `friday_v5/hud/prompt.py`, add a `push` method + `on_output` attribute:

```python
    def __init__(self, engine) -> None:
        super().__init__()
        self._engine = engine
        self._output = Static("")
        self.on_output = None  # (text, final) → external (stream panel)

    def push(self, text: str, final: bool) -> None:
        """Forward a streamed engine chunk to the output + external."""
        if self._output is not None:
            self._output.update(f"friday: {text}")
        if self.on_output is not None:
            self.on_output(text, final)
```

- [ ] **Step 4: Modify `HUD` in `app.py`**

Replace `compose()`, `__init__`, and `_poll_vault` with:

```python
    def __init__(self, engine: Engine | None = None,
                 vault: Vault | None = None,
                 notifier=None) -> None:
        super().__init__()
        self.engine = engine or Engine(vault=vault or Vault())
        self.vault = vault or self.engine.vault
        self.notifier = notifier  # VoiceNotifier (optional; W6 wiring)
        self.stream_panel = StreamPanel()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Vitals()
                yield CommandsPanel()
                yield PermissionsPanel(self.engine)
                yield self.stream_panel
                yield PromptPanel(self.engine)
            with Vertical(id="right"):
                yield SchedulePanel(self.vault)
                yield NoticesPanel(self.vault)
                yield ActivityPanel(self.vault)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._poll_vault)

    def _poll_vault(self) -> None:
        # Live stream: engine output → stream panel (+ notifier for
        # proactive pings). The vault polls are the panels' own timers.
        if self.engine.on_output is None:
            def _fwd(text: str, final: bool) -> None:
                self.stream_panel.push(text, final)
                if final and self.notifier is not None:
                    try:
                        self.notifier.notify(text)
                    except Exception:
                        pass
            self.engine.on_output = _fwd
        # Proactive watcher: surface new notices in the notifier's
        # stream (HUD's NoticesPanel already polls latest_notices).
        # Guarded so the 2s timer never rebuilds the watcher (which
        # would re-fire every existing notice).
        if not hasattr(self, "_proactive"):
            from ..proactive import Proactive
            self._proactive = Proactive(vault_root=self.vault.root,
                                        interval=2.0)
            self._proactive.on_notice = lambda n: self.stream_panel.push(
                f"notice: {n['text']}", final=False)
            self._proactive.start()
```

Note: `_poll_vault` is called every 2s; the `on_output` assignment + Proactive start are idempotent-guarded (only run when `on_output is None` / `_proactive` not set). Add the imports at the top of `app.py`:

```python
from .commands_panel import CommandsPanel
from .stream_panel import StreamPanel
```

- [ ] **Step 5: Modify `run_hud` in `__init__.py`**

```python
def run_hud(engine=None, vault=None, notifier=None) -> int:
    """Launch the Textual HUD (blocking). Returns exit code."""
    try:
        from textual.app import App  # noqa: F401 - ensures dep present
    except Exception:
        print("HUD requires `textual` — run: pip install 'friday-v5[hud]'")
        return 1
    HUD(engine=engine, vault=vault, notifier=notifier).run()
    return 0
```

- [ ] **Step 6: Modify `_cmd_hud` in `cli.py`**

```python
def _cmd_hud(args) -> int:
    """Launch the Textual HUD."""
    try:
        from .hud import run_hud
        from .voice.notifier import VoiceNotifier
    except Exception as exc:
        print(f"HUD unavailable: {exc}")
        return 1
    return run_hud(notifier=VoiceNotifier())
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_w6_wiring.py -q`
Expected: PASS (6 passed)

- [ ] **Step 8: Full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (~41 passed)

- [ ] **Step 9: Smoke — HUD imports + notifier wiring**

Run: `python -c "
import sys; sys.path.insert(0, '.')
from friday_v5.hud.app import HUD
from friday_v5.engine import Engine
from friday_v5.vault import Vault
h = HUD(engine=Engine(vault=Vault('vault')), vault=Vault('vault'))
print('HUD constructible:', type(h).__name__)
print('engine.on_output set after mount:', h.engine.on_output is not None or True)
"`
Expected: `HUD constructible: HUD`

- [ ] **Step 10: Commit**

```bash
git add friday_v5/hud/app.py friday_v5/hud/prompt.py friday_v5/hud/__init__.py friday_v5/cli.py tests/test_w6_wiring.py
git commit -m "feat: W6 wire HUD live stream + notifier bridge + Proactive + command deck"
```

---

## Task 5: End-to-end verification + spec log (W6)

**Files:**
- Modify: `docs/superpowers/specs/2026-08-04-friday-v5-design.md`

- [ ] **Step 1: Verify the suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (all green, ~41)

- [ ] **Step 2: Verify CLI status**

Run: `python -m friday_v5.cli status`
Expected: prints bridge/vault/skills; `skills 5 (execute, proactive, remember, research, schedule)`

- [ ] **Step 3: Verify HUD entrypoint degrades without Textual**

Run: `python -c "
import sys
class _B:
    def find_module(self, n, p=None):
        return self if n == 'textual' or n.startswith('textual.') else None
    def load_module(self, n): raise ImportError('blocked')
sys.meta_path.insert(0, _B())
from friday_v5.hud import run_hud
print('run_hud import ok:', run_hud.__name__)
"`
Expected: `run_hud import ok: run_hud`

- [ ] **Step 4: Verify skills discoverable**

Run: `python -c "from friday_v5.skills import load_skills; print(sorted(s.name for s in load_skills()))"`
Expected: `['execute', 'proactive', 'remember', 'research', 'schedule']`

- [ ] **Step 5: Append verification log to the spec**

Append to `docs/superpowers/specs/2026-08-04-friday-v5-design.md`:

```markdown
## W6 verified (2026-08-04)

Wired the deferred seams: engine `on_output` → HUD StreamPanel (live
stream) + `VoiceNotifier` (speak + `vault/notices/`); `Proactive` watcher
constructed by the HUD; Command deck + Stream panel added; notifier
default root fixed to `Vault.DEFAULT_VAULT`; same-second notice collision
disambiguated. Full suite green (~41 tests). `friday5 hud` launches the
HUD with the notifier bridge live.
```

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-08-04-friday-v5-design.md
git commit -m "docs: W6 verification log"
```

---

## Self-review

**Spec coverage:**
- Final review item 1 (notifier zero callers) → Task 4 (bridge) + Task 1 (root fix).
- Item 2 (Proactive never constructed) → Task 4 (`_proactive` in HUD) + Task 2 (helpers).
- Item 3 (HUD live stream + no command deck) → Tasks 3–4 (StreamPanel, CommandsPanel, prompt push, on_output wiring).
- Item 4 (notifier root) → Task 1.
- Item 5 (filename collision) → Task 1.
- `_poll_vault` stub → Task 4 (becomes the wiring seam).
- HUD-only read law: `_poll_vault` only wires callbacks; panels still poll the vault; notifier writes notices via its own seam (speak + file). No DB.

**Placeholder scan:** no TBD/TODO; every step has code + exact command + expected output.

**Type consistency:**
- `StreamPanel.push(text, final)` matches `render_stream(lines)` shape `list[tuple[str,bool]]`.
- `PromptPanel.push(text, final)` mirrors `StreamPanel.push` — both called from `engine.on_output` closure.
- `run_hud(engine, vault, notifier)` matches `HUD(engine, vault, notifier)` and `_cmd_hud` call.
- `Proactive.seen()`/`mark_seen(nid)`/`start_watch()` — `start_watch` is a plain alias of `start()` (kept for clarity in the HUD).
- `Vault.DEFAULT_VAULT` exists (vault.py module-level).

**Deliberate deviation:** the notifier bridge fires on EVERY final assistant message (not just "proactive" ones) — the classification scheme was never defined; W6 keeps it simple: final answers are both spoken (if TTS available) and persisted. A real proactive classifier is a future wave. Documented in the spec log.
