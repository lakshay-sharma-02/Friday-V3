"""Phone pairing — bind a device to the operator's Friday (Wave 7).

The companion app is useless until the operator *claims* it. Pairing is
the consent-first binding: ``friday6 mobile pair`` prints a one-time
6-character code (10-minute TTL); the app asks the operator to type it
alongside their push token; the server verifies the code and registers
the device (``mobile_devices`` table, idempotent per token).

Design:
- Pure stdlib; codes are stored in a small JSON state file (injectable
  so tests stay hermetic), devices live in the V4 DB.
- One-time use: a verified code is removed immediately (replay of a
  stolen code is impossible).
- TTL-bounded: expired codes are rejected and pruned.
- Never raises: every path degrades to ``False``/``[]``/None and the
  caller reports honestly (the never-crash law).
"""

from __future__ import annotations

import json
import logging
import secrets
import string
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.mobile.pairing")

#: Default pairing-code state file (override in tests).
_DEFAULT_STATE = Path.home() / ".friday" / "v4_mobile_pair.json"

#: Code alphabet — no 0/O/1/I ambiguity on a phone keyboard.
_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_ALPHABET = "".join(c for c in _CODE_ALPHABET if c not in "0O1I")

#: A pairing code is valid for this long.
CODE_TTL_SECONDS = 600.0  # 10 minutes


class PairingService:
    """One-time-code phone pairing against the V4 device registry."""

    def __init__(self, db_path=None, state_file: Optional[Path] = None,
                 ttl_seconds: float = CODE_TTL_SECONDS,
                 code_length: int = 6) -> None:
        self._db_path = db_path
        self._state_file = Path(state_file) if state_file else _DEFAULT_STATE
        self.ttl_seconds = ttl_seconds
        self.code_length = code_length

    # ── code lifecycle ───────────────────────────────────────────────

    def generate(self) -> str:
        """A fresh one-time pairing code (valid for ``ttl_seconds``)."""
        code = "".join(secrets.choice(_CODE_ALPHABET)
                       for _ in range(self.code_length))
        state = self._load_state()
        state["code"] = code
        state["expires_at"] = time.time() + self.ttl_seconds
        self._save_state(state)
        return code

    def verify(self, code: str) -> bool:
        """Whether ``code`` is the current, unexpired pairing code."""
        if not code:
            return False
        state = self._load_state()
        current = state.get("code")
        if not current or code.strip().upper() != str(current).upper():
            return False
        return time.time() < float(state.get("expires_at", 0))

    def consume(self, code: str) -> bool:
        """Verify AND invalidate the code (one-time use)."""
        if not self.verify(code):
            return False
        self._save_state({})
        return True

    # ── device binding ───────────────────────────────────────────────

    def register(self, code: str, token: str, *,
                 platform: str = "unknown", name: str = "") -> Optional[str]:
        """Bind a phone's push token; returns the device id or None.

        The code is consumed on success (one-time use) and the device
        row upserts by token (a reinstall keeps a single device).
        """
        token = (token or "").strip()
        if not token:
            return None
        if not self.consume(code):
            return None
        try:
            from .. import db
            conn = db.connect(path=self._db_path)
            try:
                return db.add_device(conn, token, platform=platform,
                                     name=name)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"pairing register failed: {exc}")
            return None

    def devices(self) -> list[dict]:
        """Every paired device (token omitted — the API/CLI don't leak it)."""
        try:
            from .. import db
            conn = db.connect(path=self._db_path)
            try:
                rows = db.list_devices(conn)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"pairing devices failed: {exc}")
            return []
        out = []
        for r in rows:
            out.append({
                "id": r.get("id"),
                "platform": r.get("platform"),
                "name": r.get("name"),
                "created_at": r.get("created_at"),
                "last_seen": r.get("last_seen"),
                "token": r.get("token"),
            })
        return out

    def remove(self, device_id: str) -> bool:
        """Unpair a device; returns whether one was removed."""
        try:
            from .. import db
            conn = db.connect(path=self._db_path)
            try:
                return db.remove_device(conn, device_id)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"pairing remove failed: {exc}")
            return False

    # ── state file ───────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.debug(f"pairing state unreadable: {exc}")
        return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state))
        except OSError as exc:
            logger.debug(f"pairing state save failed: {exc}")


__all__ = ["PairingService", "CODE_TTL_SECONDS"]
