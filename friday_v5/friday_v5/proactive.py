"""Proactive — watch the vault for new notices.

W3: Claude writes a ``vault/notices/<ts>-<slug>.md`` when it notices
something worth surfacing. This class polls that dir (the vault is
the single source of truth — no event bus) and fires ``on_notice``
for each new file. The HUD renders them; the notifier speaks them.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from friday_v5.vault import Vault

logger = logging.getLogger("friday_v5.proactive")

OnNotice = Callable[[dict], None]


class Proactive:
    """Poll ``vault/notices``; fire ``on_notice`` for new pings."""

    def __init__(self, vault_root: Optional[Path | str] = None,
                 interval: float = 2.0) -> None:
        self.root = Path(vault_root) if vault_root else \
            Path(__file__).resolve().parent.parent / "vault"
        self.notices_dir = self.root / "notices"
        self.notices_dir.mkdir(parents=True, exist_ok=True)
        self._vault = Vault(self.root)
        self.interval = interval
        self.on_notice: Optional[OnNotice] = None
        self._seen: set[int] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def check(self) -> list[dict]:
        """One poll: return notices not seen yet (marks them seen)."""
        out: list[dict] = []
        for p in sorted(self.notices_dir.glob("*.md")):
            try:
                nid = int(p.stem.split("-")[0])
            except (ValueError, IndexError):
                continue
            if nid in self._seen:
                continue
            self._seen.add(nid)
            out.append({"id": nid, "text": self._vault.notice_text(p) or p.stem,
                        "path": str(p)})
        # Drop stale ids so a fresh notice reusing an old timestamp
        # still gets picked up.
        seen = {p.stem.split("-")[0] for p in self.notices_dir.glob("*.md")}
        self._seen = {nid for nid in self._seen if str(nid) in seen}
        return out

    def seen(self) -> set[int]:
        """Ids of notices already surfaced (copy)."""
        return set(self._seen)

    def mark_seen(self, nid: int) -> None:
        """Mark an id as seen without scanning (HUD pre-seeds)."""
        self._seen.add(nid)

    def start(self) -> None:
        """Background poll thread (daemon, idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="friday-proactive",
                                        daemon=True)
        self._thread.start()

    def start_watch(self) -> None:
        """Alias — watch for new notices (same as start())."""
        self.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for notice in self.check():
                    if self.on_notice is not None:
                        try:
                            self.on_notice(notice)
                        except Exception as exc:
                            logger.debug(f"proactive on_notice: {exc}")
            except Exception as exc:
                logger.debug(f"proactive poll failed: {exc}")
            self._stop.wait(self.interval)
