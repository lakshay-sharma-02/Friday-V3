"""Vault FTS index — a rebuildable SQLite FTS5 *cache* over the vault.

V6 law: the index is a cache, never the truth. The vault files are
ground truth and ``Vault.query`` (grep) always works; the index only
makes ``vault find`` fast.

- ``<vault>/.index/fts.db`` — deleted or rebuilt freely.
- :meth:`VaultIndex.rebuild` — full reindex (drop + create + insert).
- :meth:`VaultIndex.refresh` — incremental: mtime-compare, update only
  changed files (cheap stat scan, safe to run before every query).
- :meth:`VaultIndex.query` — FTS5 MATCH with a snippet per hit.
- Never crashes: missing FTS5 / corrupt db → ``fts_available()`` is
  False and callers fall back to grep.

Hermetic: tests build the index over tmp dirs; nothing touches
``~/.friday`` unless the vault root points there.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

from .vault import DEFAULT_VAULT

logger = logging.getLogger("friday_v6.vault.index")

#: Columns scanned into the index. Paths are stored relative to the
#: vault root so the cache is portable across machines/moves.
_WIKI_PATTERN = "wiki/*.md"
_RAW_PATTERN = "raw/*.log"
_NOTICE_PATTERN = "notices/*.md"
_OUTPUT_PATTERN = "outputs/*"

#: Per-file content cap — a huge output file must not blow up the index.
_CONTENT_MAX = 1_000_000

_FTS_TABLE = "vault_fts"
_MTIME_TABLE = "file_mtimes"

_SCHEMA_FTS = (
    f"CREATE VIRTUAL TABLE {_FTS_TABLE} USING fts5("
    "path, name, content, tokenize='porter unicode61')"
)
_SCHEMA_MTIME = (
    f"CREATE TABLE {_MTIME_TABLE} ("
    "path TEXT PRIMARY KEY, mtime REAL)"
)

#: FTS5 availability is probed once per process (module-level cache).
_fts_probe: Optional[bool] = None


def fts5_available() -> bool:
    """Whether the platform's sqlite3 ships FTS5 (cached probe)."""
    global _fts_probe
    if _fts_probe is None:
        try:
            conn = sqlite3.connect(":memory:")
            try:
                conn.execute(_SCHEMA_FTS)
                _fts_probe = True
            except sqlite3.OperationalError:
                _fts_probe = False
            finally:
                conn.close()
        except Exception:
            _fts_probe = False
    return _fts_probe


def _safe_match(terms: str) -> str:
    """Turn free text into an FTS5-safe MATCH expression.

    Each word (>=2 chars) becomes a double-quoted phrase so arbitrary
    punctuation in the operator's query can never break the query
    parser. Embedded double quotes are stripped from tokens (a quote
    inside a phrase is an FTS5 syntax error — stripped, the word still
    matches). Returns ``""`` when there is nothing to search for (the
    caller then returns no hits — never a crash).
    """
    words: list[str] = []
    for w in re.split(r"\s+", (terms or "").strip()):
        token = w.replace('"', "")
        if len(token) >= 2:
            words.append(token)
    return " AND ".join(f'"{w}"' for w in words)


def _iter_vault_files(root: Path):
    """Yield (rel_path, name) for every file the index covers."""
    for pattern in (_WIKI_PATTERN, _RAW_PATTERN, _NOTICE_PATTERN,
                    _OUTPUT_PATTERN):
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            yield rel, path.name


class VaultIndex:
    """SQLite FTS5 cache over one vault root (never raises)."""

    def __init__(self, root: Optional[Path | str] = None) -> None:
        self.root = Path(root) if root else DEFAULT_VAULT
        self.db_dir = self.root / ".index"
        self.db_path = self.db_dir / "fts.db"

    # ── availability ─────────────────────────────────────────────────

    def fts_available(self) -> bool:
        """Whether FTS5 exists on this platform (never raises)."""
        return fts5_available()

    def exists(self) -> bool:
        """Whether an index database is present on disk."""
        return self.db_path.exists()

    # ── build ────────────────────────────────────────────────────────

    def _connect(self):
        self.db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=OFF")
        return conn

    def rebuild(self) -> int:
        """Full reindex of the vault. Returns the number of docs, or 0
        when FTS5 is unavailable (callers fall back to grep)."""
        if not fts5_available():
            return 0
        try:
            conn = self._connect()
            try:
                conn.execute(f"DROP TABLE IF EXISTS {_FTS_TABLE}")
                conn.execute(f"DROP TABLE IF EXISTS {_MTIME_TABLE}")
                conn.execute(_SCHEMA_FTS)
                conn.execute(_SCHEMA_MTIME)
                count = 0
                for rel, name in _iter_vault_files(self.root):
                    content = self._read(rel)
                    if content is None:
                        continue
                    conn.execute(
                        f"INSERT INTO {_FTS_TABLE}(path, name, content) "
                        "VALUES (?, ?, ?)", (rel, name, content))
                    conn.execute(
                        f"INSERT OR REPLACE INTO {_MTIME_TABLE}(path, mtime) "
                        "VALUES (?, ?)", (rel, self._mtime(rel)))
                    count += 1
                conn.commit()
                return count
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"vault index rebuild failed: {exc}")
            return 0

    def refresh(self) -> int:
        """Incremental update: index new/changed files, drop removed.

        Uses the ``file_mtimes`` bookkeeping table; a missing index is a
        no-op returning 0 (the caller falls back to grep). Returns the
        number of rows touched.

        Note: comparison is on ``st_mtime`` — on coarse-resolution
        filesystems a same-timestamp overwrite may be skipped until
        :meth:`rebuild`. The index is a cache; grep is the truth."""
        if not fts5_available() or not self.exists():
            return 0
        try:
            conn = self._connect()
            try:
                seen: set[str] = set()
                touched = 0
                for rel, name in _iter_vault_files(self.root):
                    seen.add(rel)
                    mtime = self._mtime(rel)
                    row = conn.execute(
                        f"SELECT mtime FROM {_MTIME_TABLE} WHERE path = ?",
                        (rel,)).fetchone()
                    if row is not None and row[0] == mtime:
                        continue  # unchanged
                    content = self._read(rel)
                    if content is None:
                        continue
                    conn.execute(
                        f"DELETE FROM {_FTS_TABLE} WHERE path = ?", (rel,))
                    conn.execute(
                        f"INSERT INTO {_FTS_TABLE}(path, name, content) "
                        "VALUES (?, ?, ?)", (rel, name, content))
                    conn.execute(
                        f"INSERT OR REPLACE INTO {_MTIME_TABLE}(path, mtime) "
                        "VALUES (?, ?)", (rel, mtime))
                    touched += 1
                # Drop rows for files that no longer exist.
                stale = conn.execute(
                    f"SELECT path FROM {_MTIME_TABLE}").fetchall()
                for (rel,) in stale:
                    if rel not in seen:
                        conn.execute(
                            f"DELETE FROM {_FTS_TABLE} WHERE path = ?", (rel,))
                        conn.execute(
                            f"DELETE FROM {_MTIME_TABLE} WHERE path = ?",
                            (rel,))
                        touched += 1
                conn.commit()
                return touched
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"vault index refresh failed: {exc}")
            return 0

    # ── query ────────────────────────────────────────────────────────

    def query(self, terms: str, limit: int = 20) -> list[dict]:
        """FTS5 search: ``[{path, name, snippet}, ...]``. Never raises.

        Returns [] when the index is missing, FTS5 is unavailable, the
        query has no words, or anything fails — callers fall back to
        grep."""
        if not fts5_available() or not self.exists():
            return []
        match = _safe_match(terms)
        if not match:
            return []
        try:
            conn = self._connect()
            try:
                sql = (f"SELECT path, name, snippet({_FTS_TABLE}, 2, '', '', "
                       f"' … ', 12) FROM {_FTS_TABLE} "
                       f"WHERE {_FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?")
                rows = conn.execute(sql, (match, limit)).fetchall()
                return [{"path": r[0], "name": r[1], "snippet": r[2] or ""}
                        for r in rows]
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"vault index query failed: {exc}")
            return []

    # ── status ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """Index state (never raises): ``{exists, docs, fts5, db}``."""
        docs = 0
        if self.fts_available() and self.exists():
            try:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    row = conn.execute(
                        f"SELECT count(*) FROM {_FTS_TABLE}").fetchone()
                    docs = int(row[0]) if row else 0
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug(f"vault index status failed: {exc}")
        return {
            "exists": self.exists(),
            "docs": docs,
            "fts5": self.fts_available(),
            "db": str(self.db_path),
        }

    # ── helpers ──────────────────────────────────────────────────────

    def _read(self, rel: str) -> Optional[str]:
        """File content capped at ``_CONTENT_MAX``; None on any error."""
        try:
            path = self.root / rel
            text = path.read_text(encoding="utf-8",
                                  errors="replace")
            return text[:_CONTENT_MAX]
        except OSError:
            return None

    def _mtime(self, rel: str) -> float:
        try:
            return (self.root / rel).stat().st_mtime
        except OSError:
            return 0.0


__all__ = ["VaultIndex", "fts5_available"]
