"""Hermetic tests for Wave 16 — the Capability Registry (Law 7, self-extension).

Covers:
- capability/registry.py: Capability model, registration, list/by_intent/
  by_layer/search/describe/summary, learned-skill capabilities
- capability/builtins.py: executors + providers + intents + surfaces
- reasoning: QuestionType.CAPABILITY + capability_provider (Wiring Law:
  "what can you do" is answered from the real registry)
- nl_router: HELP intent answered from the registry (no hardcoded list)
- cli_capability: list / describe / count commands

Safety laws verified:
- Every registry read degrades gracefully (missing DB → empty, never crash).
- Learned skills become capabilities (Law 2 + Law 7 meet).
- "what can you do" cites v4.capabilities evidence — never a guess.
"""

from __future__ import annotations

from friday_v4 import db
from friday_v4.capability import Capability, CapabilityRegistry


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# capability/registry.py — model + registry
# ==========================================================================


class TestCapabilityModel:
    def test_fields_and_dict(self):
        cap = Capability(id="executor:shell", name="shell",
                         description="run a shell command",
                         intents=("execute", "shell"), layer="executor",
                         permission_level="confirm")
        d = cap.to_dict()
        assert d["id"] == "executor:shell"
        assert d["intents"] == ["execute", "shell"]
        assert d["layer"] == "executor"
        assert d["permission_level"] == "confirm"


class TestCapabilityRegistry:
    def test_builtins_registered(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = CapabilityRegistry(conn)
            caps = reg.list()
            ids = {c.id for c in caps}
            # Executors — the gated execution layer.
            assert "executor:shell" in ids
            assert "executor:git" in ids
            assert "executor:testing" in ids
            assert "executor:ssh" in ids
            # Providers — the reasoning question types.
            assert "provider:status" in ids
            assert "provider:capability" in ids
            assert "provider:style" in ids
            # Intents — the ONE NLU point.
            assert "intent:execute" in ids
            assert "intent:style" in ids
            # Surfaces.
            assert "surface:talk" in ids
        finally:
            conn.close()

    def test_register_custom_capability(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = CapabilityRegistry(conn)
            reg.register(Capability(id="custom:deploy", name="deploy",
                                    description="deploy the stack",
                                    intents=("deploy",), layer="executor"))
            assert reg.get("custom:deploy") is not None
            assert reg.describe("custom:deploy") is not None
        finally:
            conn.close()

    def test_by_intent(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = CapabilityRegistry(conn)
            execs = {c.name for c in reg.by_intent("execute")}
            assert {"shell", "git", "file", "python", "testing", "ssh"} <= execs
            asks = {c.name for c in reg.by_intent("ask")}
            assert "status" in asks and "capability" in asks
        finally:
            conn.close()

    def test_by_layer_and_summary(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = CapabilityRegistry(conn)
            summary = reg.summary()
            assert summary["total"] >= 30
            assert summary["by_layer"].get("executor", 0) >= 6
            assert summary["by_layer"].get("intent", 0) >= 10
        finally:
            conn.close()

    def test_search(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = CapabilityRegistry(conn)
            hits = reg.search("ssh")
            assert hits and any(c.id == "executor:ssh" for c in hits)
        finally:
            conn.close()

    def test_learned_skill_is_a_capability(self, tmp_path):
        """Self-extension: learning a skill registers a new capability."""
        conn = _conn(tmp_path)
        try:
            from friday_v4.skills import SkillRegistry
            SkillRegistry(conn).create(
                "run-tests", steps=[{"action_type": "testing",
                                     "command": "pytest -q"}],
                verification_state="promoted")
            reg = CapabilityRegistry(conn)
            skill_caps = [c for c in reg.list() if c.layer == "skill"]
            assert any(c.id == "skill:run-tests" for c in skill_caps)
            assert reg.by_intent("run-tests")
        finally:
            conn.close()

    def test_missing_db_degrades(self):
        """A bad connection never crashes — empty result."""
        class BadConn:
            def execute(self, *a, **k):
                raise RuntimeError("no table")
        reg = CapabilityRegistry(BadConn())
        # Builtins still work (they don't need the DB).
        assert reg.count(include_skills=False) >= 30
        assert reg.list()  # builtins + no skills

    def test_describe_unknown_is_none(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert CapabilityRegistry(conn).describe("nope:missing") is None
        finally:
            conn.close()


# ==========================================================================
# reasoning — capability_provider (Wiring Law: ASK cites the real registry)
# ==========================================================================


class TestCapabilityProvider:
    def test_what_can_you_do_cites_registry(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v4.reasoning import answer
            ans = answer("what can you do?", conn=conn)
            assert ans.known
            assert "Here's what I can do" in ans.text
            assert any(e.source == "v4.capabilities" for e in ans.evidence)
        finally:
            conn.close()

    def test_what_are_your_capabilities(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v4.reasoning import answer
            ans = answer("what are your capabilities?", conn=conn)
            assert ans.known
            assert "executor" in ans.text or "can do" in ans.text
        finally:
            conn.close()

    def test_capability_question_type(self):
        from friday_v4.reasoning import QuestionType, classify
        assert classify("what can you do") == QuestionType.CAPABILITY
        assert classify("what are your capabilities") == QuestionType.CAPABILITY
        # Identity is not swallowed: "what are you" stays IDENTITY even
        # though "what are your capabilities" contains it as a substring.
        assert classify("what are you") == QuestionType.IDENTITY


# ==========================================================================
# nl_router — HELP answered from the registry (no hardcoded list)
# ==========================================================================


class TestHelpFromRegistry:
    def test_what_can_you_do_lists_real_capabilities(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=_conn(tmp_path))
        result = handler.handle("what can you do")
        assert result.action == "chat"
        assert "Here's what I can do" in result.response
        assert "executor" in result.response

    def test_help_works_without_db(self, monkeypatch):
        """HELP still answers without a DB — never crashes.

        Hermetic: the registry is built from builtins alone (no DB), so
        the answer is still honest and non-empty.
        """
        from friday_v4 import db as db_mod
        from friday_v4.nl_router import TextCommandHandler
        monkeypatch.setattr(db_mod, "default_db_path",
                            lambda: "/nonexistent/never-used/v4.db")
        handler = TextCommandHandler(conn=None)
        result = handler.handle("help")
        assert result.action == "chat"
        assert result.response.strip()


# ==========================================================================
# cli_capability — list / describe / count
# ==========================================================================


class TestCapabilityCli:
    def _args(self, tmp_path, **kw):
        from types import SimpleNamespace
        base = {"db": tmp_path / "v4.db", "json": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_list(self, tmp_path, capsys):
        from friday_v4.cli_capability import cmd_capability_list
        assert cmd_capability_list(self._args(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "registered capabilities" in out
        assert "executor" in out

    def test_describe(self, tmp_path, capsys):
        from friday_v4.cli_capability import cmd_capability_describe
        assert cmd_capability_describe(
            self._args(tmp_path, capability_id="executor:shell")) == 0
        assert "shell" in capsys.readouterr().out

    def test_describe_unknown(self, tmp_path, capsys):
        from friday_v4.cli_capability import cmd_capability_describe
        assert cmd_capability_describe(
            self._args(tmp_path, capability_id="nope")) == 3
        assert "no capability" in capsys.readouterr().out

    def test_count(self, tmp_path, capsys):
        from friday_v4.cli_capability import cmd_capability_count
        assert cmd_capability_count(self._args(tmp_path)) == 0
        assert "registered capabilities" in capsys.readouterr().out


# ==========================================================================
# Package-level — the layer exports the registry
# ==========================================================================


class TestWave16Exports:
    def test_capability_layer_exports(self):
        from friday_v4.capability import (
            Capability,
            CapabilityRegistry,
            is_available,
        )
        assert is_available() is True
        assert Capability is not None
        assert CapabilityRegistry is not None
