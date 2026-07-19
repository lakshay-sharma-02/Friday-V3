"""ObserverRegistry (Milestone 7).

Holds the set of available observers. Registration is explicit and order-stable
so `friday observers` lists them deterministically. The engine iterates the
registry; adding a new observer (Terminal, GitHub, ...) is a one-line register
call — no engine change.
"""

from __future__ import annotations

from .interface import Observer


class ObserverRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Observer] = {}

    def register(self, observer: Observer) -> None:
        if observer.name in self._by_name:
            raise ValueError(f"observer already registered: {observer.name}")
        self._by_name[observer.name] = observer

    def get(self, name: str) -> Observer:
        return self._by_name[name]

    def all(self) -> list[Observer]:
        """All observers in registration order."""
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return list(self._by_name)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


def default_registry() -> ObserverRegistry:
    """Registry seeded with the built-in observers.

    Future observers are added here as they are implemented; the engine and CLI
    pick them up automatically.
    """
    from .artifact_observer import ArtifactObserver
    from .calendar_observer import CalendarObserver
    from .git_observer import GitObserver
    from .github_observer import GitHubObserver
    from .research_observer import ResearchObserver
    from .terminal_observer import TerminalObserver

    reg = ObserverRegistry()
    reg.register(GitObserver())
    reg.register(TerminalObserver())
    reg.register(ArtifactObserver())
    reg.register(GitHubObserver())
    reg.register(ResearchObserver())
    reg.register(CalendarObserver())
    return reg
