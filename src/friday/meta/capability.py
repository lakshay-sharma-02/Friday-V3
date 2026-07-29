"""Capability Flag system — lifecycle management for self-evolution capabilities.

Each deployed capability is tracked in the ``capability_flags`` table with
its status, dependencies, and rollback point. The lifecycle:

    Request → Plan → Sandbox → Verify → Stage (flag=0) → Enable (flag=1) → Live
                                                    ↓
                                             Rollback ←→ Error

Usage:
    from friday.meta.capability import CapabilityRegistry, CapabilityFlag

    registry = CapabilityRegistry(conn)
    flag = registry.add("voice_support", "Add TTS/STT to Friday")
    registry.enable("voice_support")
    registry.disable("voice_support")
    all_flags = registry.list_all()
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..db import now_iso


@dataclass
class CapabilityFlag:
    """A deployed capability with its lifecycle state.

    Persisted in the ``capability_flags`` table.
    """

    name: str
    description: str = ""
    enabled: bool = False
    installed: bool = False
    deps_installed: bool = False
    plan_json: str = "{}"
    added_at: str = ""
    enabled_at: Optional[str] = None
    rollback_commit: Optional[str] = None
    last_used_at: Optional[str] = None

    @property
    def status_label(self) -> str:
        """Human-readable status label."""
        if not self.installed:
            return "pending"
        if self.enabled:
            return "enabled"
        return "disabled"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "installed": self.installed,
            "deps_installed": self.deps_installed,
            "plan_json": self.plan_json,
            "added_at": self.added_at,
            "enabled_at": self.enabled_at,
            "rollback_commit": self.rollback_commit,
            "last_used_at": self.last_used_at,
        }


class CapabilityRegistry:
    """Manages the lifecycle of deployed capabilities.

    All operations are best-effort (never raise).
    """

    def __init__(self, conn):
        self.conn = conn
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create the capability_flags table if it doesn't exist."""
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS capability_flags (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT NOT NULL UNIQUE,
                    description     TEXT NOT NULL DEFAULT '',
                    enabled         INTEGER NOT NULL DEFAULT 0,
                    installed       INTEGER NOT NULL DEFAULT 0,
                    deps_installed  INTEGER NOT NULL DEFAULT 0,
                    plan_json       TEXT NOT NULL DEFAULT '{}',
                    added_at        TEXT NOT NULL,
                    enabled_at      TEXT,
                    rollback_commit TEXT,
                    last_used_at    TEXT
                )
            """)
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    def add(self, name: str, description: str = "",
            plan_json: str = "{}",
            rollback_commit: Optional[str] = None) -> Optional[CapabilityFlag]:
        """Register a new capability flag (disabled by default)."""
        now = now_iso()
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO capability_flags "
                "(name, description, enabled, installed, deps_installed, plan_json, added_at, rollback_commit) "
                "VALUES (?, ?, 0, 1, 0, ?, ?, ?)",
                (name, description, plan_json, now, rollback_commit or ""),
            )
            self.conn.commit()
            return self.get(name)
        except Exception:
            self.conn.rollback()
            return None

    def get(self, name: str) -> Optional[CapabilityFlag]:
        """Get a capability flag by name."""
        try:
            row = self.conn.execute(
                "SELECT * FROM capability_flags WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            r = dict(row)
            return CapabilityFlag(
                name=r["name"],
                description=r.get("description", ""),
                enabled=bool(r["enabled"]),
                installed=bool(r["installed"]),
                deps_installed=bool(r["deps_installed"]),
                plan_json=r.get("plan_json", "{}"),
                added_at=r["added_at"],
                enabled_at=r.get("enabled_at"),
                rollback_commit=r.get("rollback_commit"),
                last_used_at=r.get("last_used_at"),
            )
        except Exception:
            return None

    def enable(self, name: str) -> bool:
        """Enable a deployed capability."""
        now = now_iso()
        try:
            self.conn.execute(
                "UPDATE capability_flags SET enabled = 1, enabled_at = ? WHERE name = ?",
                (now, name),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def disable(self, name: str) -> bool:
        """Disable a deployed capability."""
        try:
            self.conn.execute(
                "UPDATE capability_flags SET enabled = 0 WHERE name = ?", (name,)
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def set_rollback_commit(self, name: str, commit_hash: str) -> bool:
        """Record the rollback commit for a capability."""
        try:
            self.conn.execute(
                "UPDATE capability_flags SET rollback_commit = ? WHERE name = ?",
                (commit_hash, name),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def mark_deps_installed(self, name: str) -> bool:
        """Mark a capability's dependencies as installed."""
        try:
            self.conn.execute(
                "UPDATE capability_flags SET deps_installed = 1 WHERE name = ?",
                (name,),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def touch(self, name: str) -> bool:
        """Update the last_used_at timestamp."""
        now = now_iso()
        try:
            self.conn.execute(
                "UPDATE capability_flags SET last_used_at = ? WHERE name = ?",
                (now, name),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def remove(self, name: str) -> bool:
        """Remove a capability flag entirely."""
        try:
            self.conn.execute(
                "DELETE FROM capability_flags WHERE name = ?", (name,)
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            return False

    def list_all(self) -> list[CapabilityFlag]:
        """List all registered capability flags."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM capability_flags ORDER BY added_at DESC"
            ).fetchall()
            result = []
            for r in rows:
                r = dict(r)
                result.append(CapabilityFlag(
                    name=r["name"],
                    description=r.get("description", ""),
                    enabled=bool(r["enabled"]),
                    installed=bool(r["installed"]),
                    deps_installed=bool(r.get("deps_installed", 0)),
                    plan_json=r.get("plan_json", "{}"),
                    added_at=r["added_at"],
                    enabled_at=r.get("enabled_at"),
                    rollback_commit=r.get("rollback_commit"),
                    last_used_at=r.get("last_used_at"),
                ))
            return result
        except Exception:
            return []

    def get_last_deployed(self) -> Optional[CapabilityFlag]:
        """Get the most recently deployed capability (for auto-rollback checks)."""
        try:
            row = self.conn.execute(
                "SELECT * FROM capability_flags WHERE installed = 1 "
                "ORDER BY added_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            r = dict(row)
            return CapabilityFlag(
                name=r["name"], description=r.get("description", ""),
                enabled=bool(r["enabled"]), installed=bool(r["installed"]),
                deps_installed=bool(r.get("deps_installed", 0)),
                plan_json=r.get("plan_json", "{}"),
                added_at=r["added_at"], enabled_at=r.get("enabled_at"),
                rollback_commit=r.get("rollback_commit"),
                last_used_at=r.get("last_used_at"),
            )
        except Exception:
            return None
