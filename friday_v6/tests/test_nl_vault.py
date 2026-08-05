"""Hermetic tests for the Wave 1 NL wiring — MemoryFact in the brain.

'remember that X' now writes BOTH the SQLite row and a vault wiki note
with ``sources:`` frontmatter through the bridge; 'what do you know
about X' answers from the vault + fact rows, evidence-cited, never
fabricated. Every test passes a tmp vault_root + tmp DB — never
~/.friday.
"""

from __future__ import annotations

from friday_v6 import db
from friday_v6.memory import FactMemory
from friday_v6.vault import MemoryFact


def _handler(tmp_path, conn=None):
    from friday_v6.nl_router import TextCommandHandler
    return TextCommandHandler(conn=conn or db.connect(tmp_path / "v4.db"),
                              vault_root=str(tmp_path / "vault"))


def _bridge(tmp_path, conn=None):
    from friday_v6.vault import Vault
    return MemoryFact(conn=conn or db.connect(tmp_path / "v4.db"),
                      vault=Vault(tmp_path / "vault"))


class TestNlRemember:
    def test_remember_writes_both_sides_through_brain(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("remember that I prefer Rust for tooling")
        assert result.action == "memory"
        assert "Noted" in result.response
        assert "vault" in result.response          # the note is named

        # Structured side — the derived predicate key, intact.
        fact = FactMemory(handler.conn).recall_one("operator",
                                                   "prefer-rust-tooling")
        assert fact is not None
        assert "prefer Rust" in fact.value
        assert fact.source == "talk"

        # Prose side — the wiki note exists with sources: frontmatter.
        bridge = _bridge(tmp_path, handler.conn)
        note = bridge.read_note("operator", "prefer-rust-tooling")
        assert note is not None
        assert "sources: talk" in note

    def test_two_remembered_things_get_own_notes(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        handler.handle("remember that the deploy uses git push")
        facts = FactMemory(handler.conn).recall(subject="operator")
        # Two distinct keys, not one overwritten catch-all.
        assert len(facts) == 2
        assert {f.predicate for f in facts} == {"prefer-rust-tooling",
                                                "deploy-uses-git"}
        bridge = _bridge(tmp_path, handler.conn)
        assert bridge.note_path("operator", "deploy-uses-git").exists()

    def test_forget_removes_row_and_note_through_brain(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        bridge = _bridge(tmp_path, handler.conn)
        assert bridge.note_path("operator", "prefer-rust-tooling").exists()
        result = handler.handle("forget that")
        assert result.action == "memory"
        assert "forgotten" in result.response
        assert FactMemory(handler.conn).count(subject="operator") == 0
        assert not bridge.note_path("operator", "prefer-rust-tooling").exists()

    def test_remember_without_db_is_honest(self):
        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=None)
        result = handler.handle("remember that I prefer Rust")
        assert result.action == "failed"   # no memory connected — no vault
        assert "memory" in result.response


class TestNlVaultAsk:
    def test_what_do_you_know_answers_from_vault(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        result = handler.handle("what do you know about rust")
        assert result.action == "chat"
        assert "prefer Rust" in result.response
        assert "vault" in result.response          # cites the note
        assert "operator-prefer-rust-tooling.md" in result.response

    def test_what_do_you_remember_variant(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that the deploy uses git push")
        result = handler.handle("what do you remember about deploy")
        assert result.action == "chat"
        assert "git push" in result.response

    def test_unknown_topic_is_honest(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("what do you know about quantumflux")
        assert result.action == "chat"
        assert "don't have anything" in result.response
        assert "remember that" in result.response   # teaches, never fakes

    def test_answers_cite_vault_notes_even_without_db_row(self, tmp_path):
        """A note written by `friday6 vault note` (no DB row) is still
        answerable — the vault IS readable memory."""
        handler = _handler(tmp_path)
        bridge = _bridge(tmp_path, handler.conn)
        bridge.vault.note("Architecture Notes",
                          "The auth module uses JWT tokens.")
        result = handler.handle("what do you know about jwt")
        assert result.action == "chat"
        assert "auth module" in result.response


class TestNlVaultOps:
    """The vault speaks natural language (Wave 0 → NL): 'find X in my
    vault' searches instead of opening a web search, 'search the vault
    for X' searches instead of creating a mission, 'list my vault
    notes' lists, 'rebuild the index' reindexes — all through the ONE
    brain, pre-dispatch so the resolver's misroutes never win."""

    def test_find_in_my_vault_searches(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        result = handler.handle("find rust in my vault")
        assert result.action == "vault"
        assert "rust" in result.response.lower()
        assert "operator-prefer-rust-tooling" in result.response

    def test_search_the_vault_for_searches(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that the deploy uses git push")
        result = handler.handle("search the vault for deploy")
        assert result.action == "vault"
        assert "deploy-uses-git" in result.response

    def test_vault_search_no_match_is_honest(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("find quantumflux in my vault")
        assert result.action == "vault"
        assert "nothing" in result.response.lower()

    def test_list_my_vault_notes(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        handler.handle("remember that the deploy uses git push")
        result = handler.handle("list my vault notes")
        assert result.action == "vault"
        assert "prefer-rust-tooling" in result.response
        assert "deploy-uses-git" in result.response

    def test_list_empty_vault_is_honest(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("what's in my vault")
        assert result.action == "vault"
        assert "empty" in result.response.lower()

    def test_rebuild_the_index(self, tmp_path):
        handler = _handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        result = handler.handle("rebuild the index")
        assert result.action == "vault"
        assert "index" in result.response.lower()

    def test_plain_search_not_hijacked(self, tmp_path):
        """'search for X' without 'vault' stays a web search — the
        vault hook only fires on vault-anchored phrases."""
        handler = _handler(tmp_path)
        result = handler.handle("search for rust")
        assert result.action != "vault"

    def test_memory_phrase_with_vault_notes_not_hijacked(self, tmp_path):
        """'remember that my vault notes are important' is a MEMORY
        utterance — bare 'vault notes' substrings must not route it to
        the vault list (the pre-dispatch hook is anchored to action
        verbs for exactly this reason)."""
        handler = _handler(tmp_path)
        result = handler.handle(
            "remember that my vault notes are important")
        assert result.action == "memory"

    def test_vault_search_never_reaches_desktop(self, tmp_path,
                                                monkeypatch):
        """The Brave regression: 'find rust in my vault' must not call
        the desktop handler — the vault hook wins pre-dispatch."""
        calls = []
        handler = _handler(tmp_path)
        handler.desktop_handler = lambda t: calls.append(t) or "web search"
        result = handler.handle("find rust in my vault")
        assert result.action == "vault"
        assert calls == []          # desktop never saw it

    def test_missing_vault_never_crashes(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("list my vault notes")
        assert result.action in ("vault", "chat", "failed")


class TestNlPredicate:
    def test_memory_predicate_derivation(self, tmp_path):
        handler = _handler(tmp_path)
        assert handler._memory_predicate("I prefer Rust for tooling") == \
            "prefer-rust-tooling"
        assert handler._memory_predicate("deploy uses git push") == \
            "deploy-uses-git"
        assert handler._memory_predicate("x") == "note"   # too short
