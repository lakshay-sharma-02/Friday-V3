"""Hermetic tests for Wave 0 — the vault (linked-markdown memory).

Covers, all against tmp dirs (never ~/.friday):

- Vault file ops: raw append-only log, wiki notes + [[slug]] resolution,
  newest-first listing, grep query, [[links]] extraction, notices.
- FTS index (cache, never truth): rebuild round-trip over wiki/raw/
  notices/outputs, incremental refresh, safe-match query, status.
- ``Vault.search``: index-first, grep fallback when the index is deleted
  (the Wave 0 exit criterion).
- `friday6 vault ls|find|note` + `friday6 index rebuild|status` handlers:
  exit codes, JSON purity, and the grep-fallback hint.
"""

from __future__ import annotations

import datetime
import json
import types

import pytest

from friday_v6.vault import Vault, VaultIndex, default_vault
from friday_v6.vault.index import _safe_match


def _args(**kw):
    defaults = dict(terms=[], limit=20, name=None, text=None, root=None,
                    json=False)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _capture(capsys, fn, *args, **kw):
    code = fn(*args, **kw)
    out, err = capsys.readouterr()
    return code, out, err


class TestVaultFileOps:
    def test_init_creates_layout(self, tmp_path):
        vault = Vault(tmp_path)
        for d in ("raw", "wiki", "outputs", "notices"):
            assert (tmp_path / d).is_dir()

    def test_log_appends_to_todays_file(self, tmp_path):
        vault = Vault(tmp_path)
        path = vault.log("user", "run the tests")
        assert path.name == f"{datetime.date.today().isoformat()}.log"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("[") and "] user" in text
        assert "run the tests" in text
        # Append-only: a second log() adds, never rewrites.
        vault.log("friday", "done")
        assert text in path.read_text(encoding="utf-8")

    def test_note_writes_slugged_file_and_resolves(self, tmp_path):
        vault = Vault(tmp_path)
        path = vault.note("Auth Refactor", "# Auth\nShared auth module.")
        assert path.name == "Auth-Refactor.md"
        assert vault.note_path("[[Auth Refactor]]") == path
        assert vault.note_path("Auth Refactor") == path

    def test_list_wiki_newest_first(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("first", "a")
        vault.note("second", "b")
        names = [p.stem for p in vault.list_wiki()]
        assert names == ["second", "first"]

    def test_query_greps_wiki_and_raw(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "shared auth module for the family")
        vault.log("user", "talked about the auth refactor")
        hits = vault.query("auth")
        assert len(hits) == 2
        assert any("auth.md" in h for h in hits)
        assert any(".log" in h for h in hits)
        # Limit applies.
        assert len(vault.query("auth", limit=1)) == 1

    def test_links_from(self, tmp_path):
        vault = Vault(tmp_path)
        assert vault.links_from("see [[Auth Refactor]] and [[db]] here") == \
            ["Auth Refactor", "db"]

    def test_notices(self, tmp_path):
        vault = Vault(tmp_path)
        (vault.notices / "1-deploy.md").write_text(
            "# Notice\n- **severity**: info\n\nDeploy finished green.",
            encoding="utf-8")
        (vault.notices / "2-tests.md").write_text(
            "Tests are failing on main.", encoding="utf-8")
        latest = vault.latest_notices()
        assert [n["id"] for n in latest] == [2, 1]
        assert latest[1]["text"] == "Deploy finished green."
        assert vault.notice_text(vault.notices / "2-tests.md") == \
            "Tests are failing on main."

    def test_default_vault_is_lazy(self, tmp_path, monkeypatch):
        """Import must be side-effect free — the vault dir is only
        created when default_vault() is called (never at import)."""
        import friday_v6.vault.vault as vault_mod
        target = tmp_path / "home" / ".friday" / "v6_vault"
        monkeypatch.setattr(vault_mod, "DEFAULT_VAULT", target)
        assert not target.exists()
        v = default_vault()
        assert target.is_dir()
        assert v.wiki.is_dir()


class TestVaultIndex:
    def test_rebuild_round_trip(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "shared auth module for the family")
        vault.log("user", "fixing the auth refactor")
        idx = VaultIndex(tmp_path)
        count = idx.rebuild()
        assert count == 2
        assert idx.exists()
        hits = idx.query("auth")
        assert {h["path"] for h in hits} == {"wiki/auth.md", "raw/" + datetime.date.today().isoformat() + ".log"}
        st = idx.status()
        assert st["exists"] is True and st["docs"] == 2 and st["fts5"] is True

    def test_refresh_picks_up_new_files(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module")
        idx = VaultIndex(tmp_path)
        assert idx.rebuild() == 1
        vault.note("deploy", "deploy pipeline to production")
        assert idx.refresh() >= 1  # the new note
        assert {h["name"] for h in idx.query("deploy")} == {"deploy.md"}

    def test_query_safe_with_punctuation(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module v2 for the family")
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        # Punctuation that would break a raw FTS5 MATCH is neutralized:
        # each word is quoted whole, punctuation included, so the query
        # parses and still matches the underlying tokens.
        assert idx.query("auth!!! module??")
        assert _safe_match("auth!!!") == '"auth!!!"'
        assert _safe_match("") == ""

    def test_query_missing_index_is_empty(self, tmp_path):
        idx = VaultIndex(tmp_path)
        assert idx.query("auth") == []
        assert idx.status()["exists"] is False

    def test_rebuild_replaces_old_rows(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth v1")
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        vault.note("auth", "auth v2 with db module")
        idx.rebuild()
        hits = idx.query("db")
        assert len(hits) == 1
        assert hits[0]["name"] == "auth.md"

    def test_refresh_removes_stale_rows(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module")
        vault.note("old", "old note to delete")
        idx = VaultIndex(tmp_path)
        assert idx.rebuild() == 2
        (vault.wiki / "old.md").unlink()
        assert idx.refresh() >= 1        # the deleted file's rows
        assert idx.query("old") == []
        assert {h["name"] for h in idx.query("auth")} == {"auth.md"}

    def test_refresh_unchanged_is_zero(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module")
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        assert idx.refresh() == 0        # nothing touched

    def test_safe_match_strips_embedded_quotes(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module for the family")
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        # An embedded quote would be an FTS5 syntax error; stripped,
        # the word still matches — never a crash, never a silent miss.
        assert idx.query('"auth" module')
        assert _safe_match('"auth" module') == '"auth" AND "module"'


class TestVaultSearch:
    def test_search_uses_index_when_present(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "shared auth module for the family")
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        hits = vault.search("auth")
        assert hits and "auth.md" in hits[0]

    def test_search_falls_back_to_grep_when_index_deleted(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "shared auth module for the family")
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        idx.db_path.unlink()          # cache deleted — cache, not truth
        assert not idx.exists()
        hits = vault.search("auth")   # must still answer via grep
        assert hits and "auth.md" in hits[0]

    def test_search_no_hits_is_empty(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module")
        assert vault.search("zzz-nothing-here") == []

    def test_search_multi_term_and_limit(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("one", "alpha beta gamma")
        vault.note("two", "alpha only")
        hits = vault.search("alpha beta")
        assert len(hits) >= 1
        assert len(vault.search("alpha", limit=1)) == 1

    def test_search_with_source_reports_source(self, tmp_path):
        vault = Vault(tmp_path)
        vault.note("auth", "auth module")
        lines, source = vault.search_with_source("auth")
        assert source == "grep"          # no index built yet
        assert lines
        idx = VaultIndex(tmp_path)
        idx.rebuild()
        lines, source = vault.search_with_source("auth")
        assert source == "index"
        assert lines and "auth.md" in lines[0]


class TestVaultCLI:
    def test_note_find_roundtrip(self, tmp_path, capsys):
        from friday_v6.cli_vault import (
            EXIT_OK,
            cmd_vault_find,
            cmd_vault_note,
        )
        root = tmp_path / "root"
        code, _, _ = _capture(capsys, cmd_vault_note,
                              _args(name="auth", text="shared auth module",
                                    root=root))
        assert code == EXIT_OK
        code, out, _ = _capture(capsys, cmd_vault_find,
                                _args(terms=["auth"], root=root))
        assert code == EXIT_OK
        assert "auth.md" in out

    def test_note_accepts_stdin_dash(self, tmp_path, capsys, monkeypatch):
        from friday_v6.cli_vault import EXIT_OK, cmd_vault_note
        monkeypatch.setattr("sys.stdin", types.SimpleNamespace(
            read=lambda: "piped note content"))
        code, _, _ = _capture(capsys, cmd_vault_note,
                              _args(name="piped", text="-",
                                    root=tmp_path / "root"))
        assert code == EXIT_OK
        assert (tmp_path / "root" / "wiki" / "piped.md").exists()

    def test_ls_lists_notes(self, tmp_path, capsys):
        from friday_v6.cli_vault import EXIT_OK, cmd_vault_ls, cmd_vault_note
        root = tmp_path / "root"
        _capture(capsys, cmd_vault_note,
                 _args(name="alpha", text="a", root=root))
        code, out, _ = _capture(capsys, cmd_vault_ls, _args(root=root))
        assert code == EXIT_OK
        assert "alpha" in out

    def test_find_json_pure(self, tmp_path, capsys):
        from friday_v6.cli_vault import EXIT_OK, cmd_vault_find, cmd_vault_note
        root = tmp_path / "root"
        _capture(capsys, cmd_vault_note,
                 _args(name="auth", text="shared auth module", root=root))
        code, out, _ = _capture(capsys, cmd_vault_find,
                                _args(terms=["auth"], root=root, json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["terms"] == "auth"
        assert any("auth.md" in h for h in data["hits"])

    def test_find_with_no_index_falls_back_to_grep(self, tmp_path, capsys):
        from friday_v6.cli_vault import EXIT_OK, cmd_vault_find, cmd_vault_note
        root = tmp_path / "root"
        _capture(capsys, cmd_vault_note,
                 _args(name="auth", text="shared auth module", root=root))
        # Never rebuilt → no index → grep, with an honest hint.
        code, out, _ = _capture(capsys, cmd_vault_find,
                                _args(terms=["auth"], root=root))
        assert code == EXIT_OK
        assert "grep" in out
        assert "index rebuild" in out

    def test_find_no_hits_is_honest(self, tmp_path, capsys):
        from friday_v6.cli_vault import EXIT_FAILED, cmd_vault_find
        root = tmp_path / "root"
        from friday_v6.vault import Vault
        Vault(root).note("auth", "auth module")
        code, out, _ = _capture(capsys, cmd_vault_find,
                                _args(terms=["zzz-nothing"], root=root))
        assert code == EXIT_FAILED
        assert "Nothing" in out

    def test_index_rebuild_and_status(self, tmp_path, capsys):
        from friday_v6.cli_vault import (
            EXIT_OK,
            cmd_index_rebuild,
            cmd_index_status,
        )
        root = tmp_path / "root"
        from friday_v6.vault import Vault
        Vault(root).note("auth", "shared auth module")
        code, out, _ = _capture(capsys, cmd_index_rebuild,
                                _args(root=root, json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["rebuilt"] is True and data["docs"] == 1
        code, out, _ = _capture(capsys, cmd_index_status,
                                _args(root=root, json=True))
        assert code == EXIT_OK
        assert json.loads(out)["exists"] is True
