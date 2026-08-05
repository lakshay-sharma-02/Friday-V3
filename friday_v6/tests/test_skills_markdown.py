"""Hermetic tests for the Wave 2 markdown skills — V5's SKILL.md port.

The bundled skills are package data (``skills/markdown_skills/``); the
library also scans ``~/.friday/v6_skills/`` for operator-authored
skills that override bundled ones by name. Every test uses tmp dirs for
the user skills — never the real home directory.

Covered:
- discovery: 5 bundled skills, frontmatter parsed, body intact
- operator override: user SKILL.md with a matching name wins
- malformed files skipped (never-crash)
- explicit invocation routing ("use the schedule skill …")
- description-token matching ("add standup to my agenda" → schedule)
- NL routing: explicit invocation → Claude path when bridge available,
  deterministic floor when not; discovery answers from the vault brain
- CLI handlers: md list / show / match (text + JSON purity)
"""

from __future__ import annotations

import json

from friday_v6 import db
from friday_v6.skills import MarkdownSkill, MarkdownSkillLibrary
from friday_v6.skills.markdown import _parse_skill_file


def _user_skill(tmp_path, name: str, description: str, body: str = "# Body"):
    """Write an operator SKILL.md into the tmp user skills dir."""
    d = tmp_path / "user_skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8")
    return d / "SKILL.md"


def _library(tmp_path=None) -> MarkdownSkillLibrary:
    """The bundled library, optionally with a tmp user dir."""
    if tmp_path is None:
        return MarkdownSkillLibrary()
    return MarkdownSkillLibrary(user_dir=tmp_path / "user_skills")


class TestDiscovery:
    def test_bundled_skills_are_discoverable(self, monkeypatch):
        # Hermetic operator dir — a real ~/.friday/v6_skills/ (e.g. a
        # user-created skill) must not leak into the bundled count.
        monkeypatch.setenv("FRIDAY_V6_SKILLS_DIR", "/nonexistent/empty")
        lib = _library()
        skills = lib.list()
        names = {s.name for s in skills}
        assert names == {"execute", "proactive", "remember", "research",
                         "schedule"}
        # Every bundled skill carries the V5 frontmatter contract.
        for s in skills:
            assert s.name
            assert s.description
            assert s.body
            assert s.source == "bundled"

    def test_frontmatter_parsed_and_body_intact(self):
        lib = _library()
        schedule = lib.get("schedule")
        assert schedule is not None
        assert "agenda" in schedule.description.lower()
        assert "read first" in schedule.body.lower()     # body survives
        assert "v6_vault" in schedule.body               # V6 path convention

    def test_operator_skill_overrides_bundled_by_name(self, tmp_path):
        _user_skill(tmp_path, "schedule", "The operator's own agenda rules.")
        lib = _library(tmp_path)
        schedule = lib.get("schedule")
        assert schedule.source == "user"
        assert "operator's own" in schedule.description

    def test_operator_only_skill_appears(self, tmp_path):
        _user_skill(tmp_path, "deploy", "Release the Friday deploy.")
        lib = _library(tmp_path)
        assert {s.name for s in lib.list()} >= {"deploy"}

    def test_malformed_files_skipped(self, tmp_path):
        bad = tmp_path / "broken"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
        (bad / "SKILL.md").unlink()  # simulate unreadable? no — write real
        (bad / "SKILL.md").write_text("garbage", encoding="utf-8")
        empty = tmp_path / "empty"
        empty.mkdir(parents=True)
        (empty / "SKILL.md").write_text("---\nname: no_desc\n---\n",  # missing description
                                        encoding="utf-8")
        lib = MarkdownSkillLibrary(bundled_dir=tmp_path / "nothere",
                                   user_dir=tmp_path)
        # never-crash: malformed/partial files just don't appear
        assert lib.get("broken") is None
        assert lib.get("no_desc") is None
        assert lib.list() == []

    def test_empty_library_is_valid(self, tmp_path):
        lib = MarkdownSkillLibrary(bundled_dir=tmp_path / "nope",
                                   user_dir=tmp_path / "nope2")
        assert lib.list() == []

    def test_parse_skill_file_never_raises(self, tmp_path):
        assert _parse_skill_file(tmp_path / "missing.md", "user") is None
        weird = tmp_path / "weird.md"
        weird.write_text("\x00\x01---\nname: x\n", encoding="utf-8",
                         errors="replace")
        assert _parse_skill_file(weird, "user") is None


class TestMatching:
    def test_explicit_invocation(self):
        lib = _library()
        # Adjacency: "<name> skill" is an unambiguous invocation.
        assert lib.explicit_name("use the schedule skill to add standup") \
            is not None
        assert lib.explicit_name("run your research skill") is not None
        assert lib.explicit_name("use the schedule skills") is not None
        # "<name> skills" (plural) also matches.
        # Not invocations: names an *unknown* skill ("deploy" isn't
        # bundled), or the word "skill" isn't adjacent to a known name.
        assert lib.explicit_name("please remember the deploy skill") is None
        assert lib.explicit_name("how do I execute the skills checklist") \
            is None
        # Plain work never hijacks: no skill name in these.
        assert lib.explicit_name("run the tests") is None
        assert lib.explicit_name("what skills do you have") is None

    def test_description_match(self):
        lib = _library()
        assert lib.match("add standup to my agenda tomorrow") is not None
        assert lib.match("research postgres and write a note") is not None
        # A single coincidental word isn't enough.
        assert lib.match("banana") is None
        assert lib.match("") is None

    def test_crlf_frontmatter_parses(self, tmp_path):
        """Windows line endings must not break the frontmatter parse."""
        d = tmp_path / "user_skills" / "windows"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(
            b"---\r\nname: windows\r\ndescription: A CRLF skill.\r\n"
            b"---\r\n\r\n# Windows\r\n\r\nBody text.\r\n")
        lib = _library(tmp_path)
        skill = lib.get("windows")
        assert skill is not None
        assert skill.description == "A CRLF skill."
        assert "Body text" in skill.body

    def test_skill_dataclass_to_dict(self, tmp_path):
        _user_skill(tmp_path, "deploy", "Release the deploy.")
        s = _library(tmp_path).get("deploy")
        d = s.to_dict()
        assert d["name"] == "deploy"
        assert d["source"] == "user"
        assert isinstance(MarkdownSkill, type)


class TestNlRouting:
    def test_explicit_invocation_floor_without_bridge(self, tmp_path,
                                                     monkeypatch):
        """Bridge unavailable → the deterministic floor answers from the
        skill file (hermetic: the SDK may or may not be installed)."""
        _force_no_bridge(monkeypatch)
        handler = _handler(tmp_path)
        result = handler.handle("use the schedule skill to add standup")
        assert result.action == "skill"
        assert "schedule" in result.response
        assert "skill" in result.response
        assert "v6_vault" in result.response or "agenda" in result.response

    def test_explicit_invocation_other_skills(self, tmp_path, monkeypatch):
        _force_no_bridge(monkeypatch)
        handler = _handler(tmp_path)
        for phrase, name in (
                ("run your research skill on postgres", "research"),
                ("use the remember skill", "remember"),
                ("run the execute skill", "execute")):
            result = handler.handle(phrase)
            assert result.action == "skill", phrase
            assert name in result.response, phrase

    def test_plain_work_not_hijacked(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("run the tests")
        assert result.action != "skill"     # no skill name in "run the tests"

    def test_discovery_answers_from_brain(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("what skills do you have")
        assert result.action == "chat"
        assert "schedule" in result.response
        assert "research" in result.response

    def test_list_your_skills_routes_too(self, tmp_path):
        """'list your skills' classifies as unknown — the pre-dispatch
        overview hook still answers it (Wave 2 discoverability)."""
        handler = _handler(tmp_path)
        result = handler.handle("list your skills")
        assert result.action == "chat"
        assert "schedule" in result.response

    def test_operator_override_visible_through_brain(self, tmp_path,
                                                    monkeypatch):
        """An operator SKILL.md overriding a bundled name is what the
        NL surface routes to (source + description from the user file)."""
        _force_no_bridge(monkeypatch)
        _user_skill(tmp_path, "schedule",
                    "The operator's own agenda rules.")
        handler = _handler(tmp_path, lib=_library(tmp_path))
        result = handler.handle("use the schedule skill")
        assert result.action == "skill"
        assert "operator's own" in result.response

    def test_discovery_shows_learned_workflows_too(self, tmp_path):
        handler = _handler(tmp_path)
        # Teach a shadow skill so the overview lists both layers.
        from friday_v6.skills import SkillRegistry
        reg = SkillRegistry(handler.conn)
        reg.create("run-tests", steps=[{"action_type": "shell",
                                        "command": "pytest -q"}])
        result = handler.handle("show me your skills")
        assert "run-tests" in result.response
        assert "schedule" in result.response

    def test_bridge_path_routes_when_available(self, tmp_path, monkeypatch):
        """SDK mocked available → the prompt goes to the bridge session."""
        sent = {}
        import friday_v6.agent.bridge as bridge_mod

        class _FakeBridge:
            def available(self):
                return True

            def send(self, prompt: str) -> dict:
                sent["prompt"] = prompt
                return {"ok": True, "ended": False, "response": "on it"}

        monkeypatch.setattr(bridge_mod, "get_bridge", lambda db_path=None:
                            _FakeBridge())
        handler = _handler(tmp_path)
        result = handler.handle("use the schedule skill to add standup")
        assert result.action == "skill_routed"
        assert "schedule" in result.response
        assert "operator request" in sent["prompt"].lower()
        assert "schedule" in sent["prompt"]       # the skill body rides along

    def test_bridge_failure_degrades_to_floor(self, tmp_path, monkeypatch):
        """SDK available but send fails → honest floor, never a lie."""
        import friday_v6.agent.bridge as bridge_mod

        class _FailBridge:
            def available(self):
                return True

            def send(self, prompt: str) -> dict:
                return {"ok": False, "ended": False, "response": "nope"}

        monkeypatch.setattr(bridge_mod, "get_bridge", lambda db_path=None:
                            _FailBridge())
        handler = _handler(tmp_path)
        result = handler.handle("use the schedule skill to add standup")
        # Honest failure (Wave 2 fix): when the SDK is present but the
        # session can't connect, the operator hears WHY — the response
        # names the bridge failure and still surfaces the skill.
        assert result.action == "skill_failed"
        assert "schedule" in result.response
        assert "bridge" in result.response.lower()


def _handler(tmp_path, lib=None):
    from friday_v6.nl_router import TextCommandHandler
    return TextCommandHandler(conn=db.connect(tmp_path / "v4.db"),
                              vault_root=str(tmp_path / "vault"),
                              md_skills=lib)


def _force_no_bridge(monkeypatch) -> None:
    """Make the CLAUDE bridge report unavailable (hermetic floor path)."""
    import friday_v6.agent.bridge as bridge_mod

    class _NoBridge:
        def available(self):
            return False

    monkeypatch.setattr(bridge_mod, "get_bridge",
                        lambda db_path=None: _NoBridge())


class TestSkillCreation:
    """Friday extends itself: 'create a skill called X that does Y'
    authors a SKILL.md into the operator skills dir, immediately
    discoverable + invocable (Wave 2 follow-up)."""

    def test_create_skill_writes_skil_md(self, tmp_path):
        lib = MarkdownSkillLibrary(user_dir=tmp_path / "user_skills")
        handler = _handler(tmp_path, lib=lib)
        result = handler.handle(
            "create a skill called deploy that runs the tests")
        assert result.action == "skill_created"
        assert "deploy" in result.response
        skill_file = tmp_path / "user_skills" / "deploy" / "SKILL.md"
        assert skill_file.exists()
        text = skill_file.read_text(encoding="utf-8")
        assert "name: deploy" in text
        assert "runs the tests" in text        # the description rides along

    def test_created_skill_is_invocable(self, tmp_path):
        """The created skill shows in discovery and routes on invocation."""
        lib = MarkdownSkillLibrary(user_dir=tmp_path / "user_skills")
        handler = _handler(tmp_path, lib=lib)
        handler.handle("create a skill called deploy that runs the tests")
        # Discovery lists it.
        overview = handler.handle("what skills do you have")
        assert "deploy (user)" in overview.response
        # Invocation routes it (adjacency: "deploy skill").
        created = handler.handle("use the deploy skill")
        assert created.action in ("skill", "skill_routed")
        assert "deploy" in created.response

    def test_create_skill_without_description(self, tmp_path):
        lib = MarkdownSkillLibrary(user_dir=tmp_path / "user_skills")
        handler = _handler(tmp_path, lib=lib)
        result = handler.handle("create a skill called archive")
        assert result.action == "skill_created"
        skill_file = tmp_path / "user_skills" / "archive" / "SKILL.md"
        assert skill_file.exists()

    def test_create_skill_unwritable_is_honest(self, tmp_path, monkeypatch):
        """A dir that can't be written degrades to an honest error."""
        from pathlib import Path
        blocked = tmp_path / "blocked" / "skills"
        blocked.parent.mkdir()
        (blocked.parent / "skills").write_text("i am a file",
                                               encoding="utf-8")
        lib = MarkdownSkillLibrary(user_dir=blocked)
        handler = _handler(tmp_path, lib=lib)
        result = handler.handle("create a skill called locked")
        assert result.action == "failed"
        assert "couldn't create" in result.response

    def test_non_create_phrase_returns_none(self, tmp_path):
        lib = MarkdownSkillLibrary(user_dir=tmp_path / "user_skills")
        handler = _handler(tmp_path, lib=lib)
        # Not a create request — other routing owns it.
        assert handler._skill_create_response(
            "use the schedule skill") is None

    def test_description_uses_matched_name_not_first_occurrence(self,
                                                               tmp_path):
        """The description slices from the regex match, not ``find`` —
        a name appearing earlier in the sentence must not garble it."""
        lib = MarkdownSkillLibrary(user_dir=tmp_path / "user_skills")
        handler = _handler(tmp_path, lib=lib)
        result = handler.handle(
            "deploy is my favorite tool, create a skill called deploy "
            "that runs the tests")
        assert result.action == "skill_created"
        text = (tmp_path / "user_skills" / "deploy" / "SKILL.md")\
            .read_text(encoding="utf-8")
        assert "runs the tests" in text
        assert "favorite tool" not in text


class TestCli:
    def _run(self, args: list[str]):
        from friday_v6.cli_skills import main
        return main(args)

    def test_md_list_json(self, tmp_path, capsys, monkeypatch):
        import types
        from friday_v6.cli_skills import cmd_skills_md_list
        # Hermetic: point the operator skills dir at an empty tmp dir so
        # a real ~/.friday/v6_skills/ never leaks into the test.
        monkeypatch.setenv("FRIDAY_V6_SKILLS_DIR", str(tmp_path / "skills"))
        code = cmd_skills_md_list(types.SimpleNamespace(json=True))
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert len(payload) == 5
        assert {s["name"] for s in payload} == {"execute", "proactive",
                                                "remember", "research",
                                                "schedule"}

    def test_cmd_handlers_json_pure(self, tmp_path, capsys, monkeypatch):
        from friday_v6.cli_skills import (cmd_skills_md_list,
                                          cmd_skills_md_show,
                                          cmd_skills_md_match)
        import types
        # Hermetic operator dir — a real ~/.friday/v6_skills/ would add
        # operator skills to the bundled five.
        monkeypatch.setenv("FRIDAY_V6_SKILLS_DIR", str(tmp_path / "skills"))
        code = cmd_skills_md_list(types.SimpleNamespace(json=True))
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert {s["name"] for s in payload} == {"execute", "proactive",
                                                "remember", "research",
                                                "schedule"}

        code = cmd_skills_md_show(types.SimpleNamespace(name="schedule",
                                                        json=True))
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert payload["name"] == "schedule"
        assert "body" in payload

        code = cmd_skills_md_match(types.SimpleNamespace(
            text=["use", "the", "schedule", "skill"], json=True))
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert payload["match"]["name"] == "schedule"
        assert payload["explicit"]["name"] == "schedule"

    def test_md_show_missing_skill(self, capsys):
        import types
        from friday_v6.cli_skills import cmd_skills_md_show
        code = cmd_skills_md_show(types.SimpleNamespace(name="nope",
                                                        json=True))
        assert code == 3        # usage error, not a crash
