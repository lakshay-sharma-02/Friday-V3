"""WatchRecorder — explicit demonstration capture, "watch me" (Wave 14 §3.1).

The MASTER_PLAN's *"watch me do this"*: the audit log IS the
demonstration record — every executed action is already logged (what,
when, result). ``WatchRecorder`` simply **tags a window** on that trail:

    watch = WatchRecorder(conn)
    wid = watch.start(name="deploy routine")   # "watch me do this"
    # ... operator works; every audited action is captured ...
    skill = watch.stop(wid)                     # "learn this" → shadow skill

``stop`` parameterizes the captured actions into a **generalized** skill:
steps carry the repo context they happened in (so shadow/dispatch match
by context — repo + command — not literal replays), and consecutive
duplicate actions are collapsed so the skill is tight. The formed skill
starts in ``shadow`` with confidence 0 — inert until shadow-verified +
operator-promoted (the wave-10 safety law).

Never crashes: no active watch, empty capture, or unreadable DB yields
``None``/``[]`` — never an exception.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .. import db
from .registry import SkillRegistry
from .shadow import _step_matches

logger = logging.getLogger("friday_v4.skills.watcher")


def _generalized_steps(actions: list[dict]) -> list[dict]:
    """Parameterize audited actions into skill steps with repo context.

    Each step carries the repo it happened in (``cwd`` basename) so
    matching generalizes across sessions: ``pytest -q`` in ``friday_v4``
    is the *same step* wherever it runs in that repo. Exact consecutive
    duplicates are collapsed (a watch often repeats ``git status``).
    """
    steps: list[dict] = []
    for a in actions:
        cwd = (a.get("cwd") or "").strip()
        step = {
            "action_type": (a.get("action_type") or "").strip(),
            "command": (a.get("command") or "").strip(),
            "goal": (a.get("goal") or "").strip(),
            "repo": Path(cwd).name if cwd else "",
        }
        if not step["action_type"]:
            continue
        if steps and steps[-1] == step:
            continue  # collapse consecutive duplicates
        steps.append(step)
    return steps


def _desktop_event_steps(events: list[dict]) -> list[dict]:
    """Shape observed desktop events like audited actions for capture.

    ``event_type`` (app_switch/app_focus) maps to the step's
    ``action_type``; ``app`` is the command (the thing that was opened);
    the repo carried by the event (when probing succeeded) becomes the
    step's ``cwd`` so the same generalization + repo-context matching as
    audited actions applies. Events with no app are skipped.
    """
    steps: list[dict] = []
    for e in events or []:
        app = (e.get("app") or "").strip()
        if not app:
            continue
        # ``created_at`` is carried through so ``_ordered_capture`` can
        # interleave desktop events with audited actions chronologically
        # (``_generalized_steps`` ignores it — harmless extra key).
        steps.append({
            "action_type": (e.get("event_type") or "app_switch").strip(),
            "command": app,
            "goal": (e.get("title") or "").strip(),
            "cwd": (e.get("repo") or "").strip(),
            "created_at": e.get("created_at", ""),
        })
    return steps


def _ordered_capture(actions: list[dict], desktop: list[dict]) -> list[dict]:
    """Interleave audited actions and desktop events in real time order.

    Both lists arrive oldest-first. Merging by source (``actions +
    desktop``) would put every audited action before every app open even
    when the app open happened mid-demonstration. Each item is tagged
    with its ``created_at`` and stable sort order (rowid within a
    source), then the two lists are zipped by timestamp. Desktop events
    are pre-shaped via ``_desktop_event_steps`` so they carry the same
    keys ``_generalized_steps`` expects (``cwd`` from the event repo).
    """
    tagged: list[tuple[str, int, dict]] = []
    for i, a in enumerate(actions or []):
        tagged.append((a.get("created_at", ""), i, a))
    shaped = _desktop_event_steps(desktop)
    for i, e in enumerate(shaped):
        tagged.append((e.get("created_at", ""), i + len(actions or []), e))
    tagged.sort(key=lambda t: (t[0], t[1]))
    return [item for _, _, item in tagged]


def _name_from_hint(hint: str, steps: list[dict]) -> str:
    """A skill name from the operator's words or the step sequence."""
    hint = (hint or "").strip().lower()
    if hint:
        name = "-".join(hint.split())[:60]
        return name or "watch-me"
    base = "-".join(s.get("action_type", "") for s in steps[:4])
    base = "-".join(p for p in base.split("-") if p)
    return base or "watch-me"


class WatchRecorder:
    """Tags a window on the audit trail and forms a skill from it."""

    def __init__(self, conn, registry: Optional[SkillRegistry] = None) -> None:
        self._conn = conn
        self._registry = registry or SkillRegistry(conn)

    # ── Capture ──────────────────────────────────────────────────────

    def start(self, name: str = "", note: str = "",
              context: str = "") -> Optional[str]:
        """Begin watching. Returns the watch id (None on failure).

        Only one watch is live at a time — starting a new one closes the
        previous (stopped, no skill). ``context`` defaults to the current
        working directory (the generalization signal).
        """
        ctx = context or _cwd()
        return db.start_watch(self._conn, name=name, context=ctx, note=note)

    def stop(self, watch_id: Optional[str] = None,
             name: Optional[str] = None) -> Optional[dict]:
        """End the watch and form a shadow skill from its actions.

        Args:
            watch_id: the watch to close. Defaults to the active watch.
            name: skill name override (operator's words usually).

        Returns ``{"skill": Skill, "actions": n}`` or None when there is
        no watch / nothing to learn. The skill starts in shadow with
        confidence 0 — never executed, promotion still needs approval.
        """
        wid = watch_id
        if wid is None:
            active = db.active_watch(self._conn)
            wid = active["id"] if active else None
        if wid is None:
            return None
        watch = db.get_watch(self._conn, wid)
        if not watch or watch.get("status") != "active":
            return None

        actions = db.actions_between(
            self._conn, watch.get("started_at", ""), db.now_iso()) or []
        # The desktop-observer bridge (Wave 14 close-out extension): app
        # opens/focuses observed while the watch was open are captured
        # too — "watch me" → open Brave → open VSCode → "learn this"
        # forms a real skill with open:app steps, not just audited
        # commands. Desktop events are parameterized the same way (repo
        # context, duplicate collapse) so they generalize.
        desktop = db.desktop_events_between(
            self._conn, watch.get("started_at", ""), db.now_iso()) or []
        # Merge chronologically, not by source: a desktop event that
        # happened *between* two audited actions must sit between them in
        # the demonstration (concatenating by source would reorder the
        # capture). ``_ordered_capture`` interleaves both lists on
        # created_at (microsecond-precision, so same-second ties are
        # resolved by real insert order).
        capture = _ordered_capture(actions, desktop)
        steps = _generalized_steps(capture)
        if not steps:
            db.end_watch(self._conn, wid)
            return None

        hint = name or watch.get("name") or ""
        skill_name = _name_from_hint(hint, steps)
        # Reuse an existing skill by name ONLY when its first step
        # matches what was just demonstrated — otherwise the operator's
        # fresh demonstration must not be silently discarded (a same-name
        # skill with different steps gets a version suffix instead).
        existing = self._registry.get(skill_name)
        if existing and existing.steps and _step_matches(existing.steps[0],
                                                         steps[0]):
            db.end_watch(self._conn, wid, skill_id=existing.id)
            return {"skill": existing, "actions": len(actions or [])}
        if existing:
            skill_name = _versioned_name(skill_name, self._registry)

        sid = self._registry.create(skill_name, steps=steps, confidence=0.0)
        if not sid:
            return None
        db.end_watch(self._conn, wid, skill_id=sid)
        skill = self._registry.get_by_id(sid)
        if not skill:
            return None
        return {"skill": skill, "actions": len(actions or [])}

    def capture(self, watch_id: str) -> list[dict]:
        """The actions recorded so far in a watch (oldest first)."""
        watch = db.get_watch(self._conn, watch_id)
        if not watch:
            return []
        return db.actions_between(
            self._conn, watch.get("started_at", ""), db.now_iso()) or []

    def active(self) -> Optional[dict]:
        """The active watch's dict, or None."""
        return db.active_watch(self._conn)


def _versioned_name(base: str, registry: SkillRegistry) -> str:
    """A unique name when ``base`` is taken: ``deploy-2``, ``deploy-3``…"""
    n = 2
    while registry.get(f"{base}-{n}") is not None:
        n += 1
    return f"{base}-{n}"


def _cwd() -> str:
    try:
        import os
        return os.getcwd()
    except Exception:
        return ""


__all__ = ["WatchRecorder", "_generalized_steps", "_name_from_hint"]
