"""Hermetic tests for Wave 1 — the MemoryFact bridge (vault ⇄ DB).

The W1 exit criterion: *fact → wiki note → recall; DB rows intact.*
Every test asserts BOTH sides of the single write path, against a tmp
vault root + tmp SQLite DB (never ~/.friday):

- remember() writes the SQLite row (value/source/confidence intact per
  direct FactMemory reads) AND a wiki note with ``sources:`` frontmatter
  that vault search can find.
- recall() reads the structured truth; the note path is derivable.
- forget() removes both sides; subject-wide forget removes all notes.
- Never-crash: a missing DB still writes the note, and vice versa.
- `friday6 fact store|recall|list|forget` handlers: exit codes, JSON
  purity, bare-key → operator subject.
"""

from __future__ import annotations

import json
import types

import pytest

from friday_v6 import db
from friday_v6.memory import FactMemory
from friday_v6.vault import MemoryFact, Vault, parse_frontmatter


def _args(**kw):
    defaults = dict(key=None, value=None, source="cli", confidence=0.7,
                    policy="usage", subject=None, limit=50, root=None,
                    db=None, json=False)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _capture(capsys, fn, *args, **kw):
    code = fn(*args, **kw)
    out, err = capsys.readouterr()
    return code, out, err


def _bridge(tmp_path):
    conn = db.connect(tmp_path / "v4.db")
    vault = Vault(tmp_path / "root")
    return MemoryFact(conn=conn, vault=vault), conn


class TestMemoryFactBridge:
    def test_remember_writes_both_sides(self, tmp_path):
        bridge, conn = _bridge(tmp_path)
        try:
            result = bridge.remember(
                "operator", "prefers_rust", "Rust",
                source="voice:2026-08-01", confidence=0.9)
            assert result is not None
            assert result["note"] is not None

            # Structured side — DB rows intact, direct read.
            fact = FactMemory(conn).recall_one("operator", "prefers_rust")
            assert fact is not None
            assert fact.value == "Rust"
            assert fact.source == "voice:2026-08-01"
            assert fact.confidence == pytest.approx(0.9)

            # Prose side — the wiki note exists with sources: frontmatter.
            note_path = bridge.note_path("operator", "prefers_rust")
            assert note_path.name == "operator-prefers_rust.md"
            text = note_path.read_text(encoding="utf-8")
            assert "sources: voice:2026-08-01" in text
            fm = parse_frontmatter(text)
            assert fm["subject"] == "operator"
            assert fm["predicate"] == "prefers_rust"
            assert fm["sources"] == "voice:2026-08-01"
            assert "Rust" in text

            # The vault can find the note (prose is searchable).
            assert any("operator-prefers_rust.md" in h
                       for h in bridge.vault.search("rust"))
        finally:
            conn.close()

    def test_recall_roundtrip_and_sources(self, tmp_path):
        bridge, conn = _bridge(tmp_path)
        try:
            bridge.remember("operator", "timezone", "Europe/Berlin",
                            source="talk")
            fact = bridge.recall_one("operator", "timezone")
            assert fact is not None and fact.value == "Europe/Berlin"
            assert bridge.sources_for("operator", "timezone") == ["talk"]
            assert bridge.recall_one("operator", "nope") is None
        finally:
            conn.close()

    def test_reaffirm_strengthens_db_and_rewrites_note(self, tmp_path):
        bridge, conn = _bridge(tmp_path)
        try:
            bridge.remember("operator", "name", "Lakshay",
                            source="voice:1", confidence=0.6)
            bridge.remember("operator", "name", "Lakshay",
                            source="voice:2", confidence=0.6)
            # Single row, boosted confidence (MemoryStore semantics).
            assert FactMemory(conn).count() == 1
            fact = FactMemory(conn).recall_one("operator", "name")
            assert fact.confidence == pytest.approx(0.7)
            # Note rewritten with the latest source.
            assert bridge.sources_for("operator", "name") == ["voice:2"]
        finally:
            conn.close()

    def test_forget_removes_both_sides(self, tmp_path):
        bridge, conn = _bridge(tmp_path)
        try:
            bridge.remember("operator", "name", "Lakshay")
            assert bridge.note_path("operator", "name").exists()
            assert bridge.forget("operator", "name") is True
            assert FactMemory(conn).recall_one("operator", "name") is None
            assert not bridge.note_path("operator", "name").exists()
            # Already gone — honest False, never a crash.
            assert bridge.forget("operator", "name") is False
        finally:
            conn.close()

    def test_forget_subject_removes_all_notes(self, tmp_path):
        bridge, conn = _bridge(tmp_path)
        try:
            bridge.remember("operator", "name", "Lakshay")
            bridge.remember("operator", "location", "Berlin")
            bridge.remember("project", "active", "yes")
            assert bridge.forget("operator") is True
            assert bridge.recall(subject="operator") == []
            assert not bridge.note_path("operator", "name").exists()
            assert not bridge.note_path("operator", "location").exists()
            # The other subject's note survives.
            assert bridge.note_path("project", "active").exists()
        finally:
            conn.close()

    def test_missing_db_still_writes_note(self, tmp_path):
        # Never-crash: no DB → the prose side still works.
        vault = Vault(tmp_path / "root")
        bridge = MemoryFact(conn=None, vault=vault)
        result = bridge.remember("operator", "name", "Lakshay", source="talk")
        assert result is not None
        assert result["fact"] is None
        assert result["note"] is not None
        assert bridge.recall_one("operator", "name") is None
        assert bridge.sources_for("operator", "name") == ["talk"]

    def test_remember_without_value_is_honest(self, tmp_path):
        bridge, conn = _bridge(tmp_path)
        try:
            assert bridge.remember("operator", "name", "   ") is None
            assert FactMemory(conn).count() == 0
            assert not bridge.note_path("operator", "name").exists()
        finally:
            conn.close()

    def test_forget_without_db_cleans_notes(self, tmp_path):
        # No DB at all — remember() still wrote notes, and forget()
        # must still delete them (glob-based, never depends on recall).
        vault = Vault(tmp_path / "root")
        bridge = MemoryFact(conn=None, vault=vault)
        bridge.remember("operator", "name", "Lakshay", source="talk")
        bridge.remember("operator", "location", "Berlin", source="talk")
        assert bridge.forget("operator") is True
        assert not bridge.note_path("operator", "name").exists()
        assert not bridge.note_path("operator", "location").exists()
        # Single-predicate forget with no row (drift) still cleans.
        bridge.remember("operator", "timezone", "Berlin", source="talk")
        assert bridge.forget("operator", "timezone") is True
        assert not bridge.note_path("operator", "timezone").exists()

    def test_remember_with_bad_vault_still_writes_db(self, tmp_path):
        # Reverse degradation: the vault side fails → the DB must win.
        conn = db.connect(tmp_path / "v4.db")
        try:
            vault = Vault(tmp_path / "root")
            vault.wiki.rmdir()
            vault.wiki.write_text("blocked", encoding="utf-8")
            bridge = MemoryFact(conn=conn, vault=vault)
            result = bridge.remember("operator", "name", "Lakshay",
                                     source="talk")
            assert result is not None
            assert result["fact"] is not None   # DB side landed
            assert result["note"] is None       # vault side failed
            assert FactMemory(conn).recall_one(
                "operator", "name").value == "Lakshay"
        finally:
            conn.close()


class TestFactCLI:
    def test_store_recall_roundtrip_shows_note(self, tmp_path, capsys):
        from friday_v6.cli_fact import (
            EXIT_OK,
            cmd_fact_recall,
            cmd_fact_store,
        )
        root = tmp_path / "root"
        dbp = tmp_path / "v4.db"
        code, out, _ = _capture(capsys, cmd_fact_store,
                                _args(key="operator.prefers_rust",
                                      value="Rust", source="voice:1",
                                      root=root, db=dbp))
        assert code == EXIT_OK
        assert "Noted" in out
        code, out, _ = _capture(capsys, cmd_fact_recall,
                                _args(key="operator.prefers_rust",
                                      root=root, db=dbp))
        assert code == EXIT_OK
        assert "Rust" in out
        assert "operator-prefers_rust.md" in out      # the note path

    def test_bare_key_uses_operator_subject(self, tmp_path, capsys):
        from friday_v6.cli_fact import EXIT_OK, cmd_fact_store
        code, _, _ = _capture(capsys, cmd_fact_store,
                              _args(key="name", value="Lakshay",
                                    root=tmp_path / "root",
                                    db=tmp_path / "v4.db"))
        assert code == EXIT_OK
        conn = db.connect(tmp_path / "v4.db")
        assert FactMemory(conn).recall_one("operator", "name").value == "Lakshay"
        conn.close()

    def test_recall_missing_is_honest(self, tmp_path, capsys):
        from friday_v6.cli_fact import EXIT_FAILED, cmd_fact_recall
        code, out, _ = _capture(capsys, cmd_fact_recall,
                                _args(key="operator.nope",
                                      root=tmp_path / "root",
                                      db=tmp_path / "v4.db"))
        assert code == EXIT_FAILED
        assert "don't remember" in out.lower()

    def test_json_pure(self, tmp_path, capsys):
        from friday_v6.cli_fact import (
            EXIT_OK,
            cmd_fact_recall,
            cmd_fact_store,
        )
        root = tmp_path / "root"
        dbp = tmp_path / "v4.db"
        _capture(capsys, cmd_fact_store, _args(key="operator.name",
                                               value="Lakshay", root=root,
                                               db=dbp))
        code, out, _ = _capture(capsys, cmd_fact_recall,
                                _args(key="operator.name", root=root,
                                      db=dbp, json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["subject"] == "operator"
        assert data["predicate"] == "name"
        assert data["value"] == "Lakshay"
        assert data["note"] and data["note"].endswith("operator-name.md")

    def test_list_filters_by_subject_with_notes(self, tmp_path, capsys):
        from friday_v6.cli_fact import (
            EXIT_OK,
            cmd_fact_list,
            cmd_fact_store,
        )
        root = tmp_path / "root"
        dbp = tmp_path / "v4.db"
        _capture(capsys, cmd_fact_store, _args(key="operator.name",
                                               value="Lakshay", root=root,
                                               db=dbp))
        _capture(capsys, cmd_fact_store, _args(key="project.active",
                                               value="yes", root=root,
                                               db=dbp))
        code, out, _ = _capture(capsys, cmd_fact_list,
                                _args(subject="operator", root=root,
                                      db=dbp, json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["predicate"] == "name"
        assert data[0]["note"] and "operator-name.md" in data[0]["note"]

    def test_store_without_db_uses_default_connect(self, tmp_path, capsys,
                                                   monkeypatch):
        """No --db → the CLI still connects (default path convention),
        like `friday6 memory`."""
        from friday_v6 import db as db_mod
        from friday_v6.cli_fact import EXIT_OK, cmd_fact_store
        real_connect = db_mod.connect
        called: dict = {}

        def fake_connect(path=None):
            called["path"] = path
            return real_connect(tmp_path / "v4.db")

        monkeypatch.setattr(db_mod, "connect", fake_connect)
        code, _, _ = _capture(capsys, cmd_fact_store,
                              _args(key="operator.name", value="Lakshay",
                                    root=tmp_path / "root"))
        assert code == EXIT_OK
        assert called["path"] is None      # default DB path convention

    def test_forget_removes_both_sides_via_cli(self, tmp_path, capsys):
        from friday_v6.cli_fact import (
            EXIT_OK,
            cmd_fact_forget,
            cmd_fact_store,
        )
        root = tmp_path / "root"
        dbp = tmp_path / "v4.db"
        _capture(capsys, cmd_fact_store, _args(key="operator.name",
                                               value="Lakshay", root=root,
                                               db=dbp))
        code, out, _ = _capture(capsys, cmd_fact_forget,
                                _args(key="operator.name", root=root,
                                      db=dbp))
        assert code == EXIT_OK
        assert "Forgotten" in out
        assert not (root / "wiki" / "operator-name.md").exists()
        conn = db.connect(dbp)
        assert FactMemory(conn).recall_one("operator", "name") is None
        conn.close()
