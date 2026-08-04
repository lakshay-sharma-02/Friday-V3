"""VoiceNotifier — turn engine output into audible proactive notices.

W2: the proactive seam. The engine's ``on_output`` callback feeds
every final assistant message here; messages that are proactive (not
the direct reply to a user turn) are spoken and written to
``vault/notices/`` as a durable ping the HUD can surface later.

Kept tiny on purpose: no routing, no state — a passive observer.
"""
from __future__ import annotations

import datetime
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("friday_v5.voice.notifier")

NOTICE_SLUG_RE = re.compile(r"[^a-z0-9-]+")
OnNotice = Callable[[dict], None]


class VoiceNotifier:
    """Speak + persist one proactive notice."""

    def __init__(self, vault_root: Optional[Path | str] = None) -> None:
        self.vault_root = Path(vault_root) if vault_root else \
            Path(__file__).resolve().parent.parent.parent / "vault"
        self.notices_dir = self.vault_root / "notices"
        self.notices_dir.mkdir(parents=True, exist_ok=True)
        self.speak: Callable[[str], bool] = lambda text: False

    def notify(self, text: str,
               on_notice: Optional[OnNotice] = None) -> Optional[Path]:
        """Speak + write a notice file. Returns the file path."""
        text = (text or "").strip()
        if not text:
            return None
        ts = int(time.time())
        slug = NOTICE_SLUG_RE.sub("-", text.lower())[:40].strip("-")
        path = self.notices_dir / f"{ts}-{slug or 'notice'}.md"
        stamp = datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        body = f"# Notice\n\n- **at**: {stamp}\n- **id**: {ts}\n\n{text}\n"
        try:
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            logger.warning(f"notifier write failed: {exc}")
            return None
        try:
            self.speak(text)
        except Exception as exc:
            logger.debug(f"notifier speak failed: {exc}")
        if on_notice is not None:
            try:
                on_notice({"id": ts, "text": text, "path": str(path)})
            except Exception as exc:
                logger.debug(f"notifier callback failed: {exc}")
        return path
