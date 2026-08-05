"""Package import-safety tests.

Every ``friday_v6`` subpackage must be importable without side effects —
including the future-wave stubs (mobile) whose internal modules aren't
built yet. This guards against the ``ide/`` landmine class of bug where
a stub package crashed on import.
"""

from __future__ import annotations

import importlib

import pytest


class TestPackageImports:
    @pytest.mark.parametrize(
        "pkg",
        [
            "friday_v6",
            "friday_v6.voice",
            "friday_v6.desktop",
            "friday_v6.desktop.ide",
            "friday_v6.proactive",
            "friday_v6.mobile",
            "friday_v6.collab",
            "friday_v6.security",
            "friday_v6.intelligence",
            "friday_v6.network",
            "friday_v6.execution",
            "friday_v6.understanding",
            "friday_v6.missions",
            "friday_v6.reasoning",
            "friday_v6.memory",
            "friday_v6.persona",
            "friday_v6.relationship",
            "friday_v6.skills",
            "friday_v6.vault",
            "friday_v6.hud",
        ],
    )
    def test_package_imports_clean(self, pkg: str):
        module = importlib.import_module(pkg)
        assert module is not None
        # Stub packages should advertise their planned contents
        assert hasattr(module, "__all__")

    def test_mobile_available_after_wave15(self):
        """Wave 15 shipped the mobile transport — the companion package
        is importable, reports itself available, and exposes the push
        service + companion API."""
        module = importlib.import_module("friday_v6.mobile")
        assert callable(getattr(module, "is_available"))
        assert module.is_available() is True
        for name in ("MobileAPI", "create_api_server",
                     "PushNotificationService", "Notification"):
            assert getattr(module, name) is not None

    def test_network_available_after_wave12(self):
        """Wave 12 folded SSH into the execution layer — the network
        package now reports itself as available via ``ssh_available()``
        and keeps the executor importable for compatibility."""
        module = importlib.import_module("friday_v6.network")
        assert module.is_available() is True
        assert module.ssh_available() is True
        assert module.SSHExecutor is not None

    def test_collab_available_after_wave5(self):
        """Wave 5 shipped — the collaboration layer is importable and
        reports itself as available (stub-state marker flipped)."""
        module = importlib.import_module("friday_v6.collab")
        assert module.is_available() is True
        for name in ("Coordinator", "ObservationCRDT", "PeerDiscovery",
                     "PermissionManager", "SyncEngine"):
            assert getattr(module, name) is not None

    def test_intelligence_available_after_wave4(self):
        """Wave 4 shipped — the intelligence layer is importable and reports
        itself as available (stub-state marker flipped)."""
        module = importlib.import_module("friday_v6.intelligence")
        assert module.is_available() is True

    def test_security_available_after_wave3(self):
        """Wave 3 shipped — the security layer is importable and reports
        itself as available (stub-state marker flipped)."""
        module = importlib.import_module("friday_v6.security")
        assert module.is_available() is True

    def test_understanding_available_after_wave9(self):
        """Wave 9 shipped — the understanding layer is importable and
        reports itself as available."""
        module = importlib.import_module("friday_v6.understanding")
        assert module.is_available() is True
        for name in ("resolve", "classify", "extract", "assess"):
            assert getattr(module, name) is not None

    def test_nl_router_importable(self):
        """The shared natural-language handler imports cleanly and is
        wired for use by both cli_nl and the voice router."""
        from friday_v6.nl_router import TextCommandHandler, TalkResult, \
            voice_confirm
        assert TextCommandHandler is not None
        assert TalkResult is not None
        assert voice_confirm is not None

    def test_reasoning_available_after_wave9(self):
        """Wave 9 shipped — the reasoning layer is importable and
        reports itself as available."""
        module = importlib.import_module("friday_v6.reasoning")
        assert module.is_available() is True
        for name in ("answer", "parse", "validate", "Evidence"):
            assert getattr(module, name) is not None

    def test_cli_ask_importable(self):
        """`friday6 ask` (evidence-cited answers) imports cleanly."""
        from friday_v6.cli_ask import cmd_ask, build_ask_parser
        assert cmd_ask is not None
        assert build_ask_parser is not None

    def test_cli_memory_importable(self):
        """`friday6 memory` (Wave 10 facts CLI) imports cleanly."""
        from friday_v6.cli_memory import (cmd_memory_store,
                                          build_memory_parser)
        assert cmd_memory_store is not None
        assert build_memory_parser is not None

    def test_cli_nl_importable(self):
        """`friday6 talk` (natural language) imports cleanly."""
        from friday_v6.cli_nl import cmd_talk, build_talk_parser
        assert cmd_talk is not None
        assert build_talk_parser is not None

    def test_execution_available_after_wave9(self):
        """Wave 9 shipped — the execution layer is importable and reports
        itself as available."""
        module = importlib.import_module("friday_v6.execution")
        assert module.is_available() is True
        for name in ("execute", "PermissionGate", "Sandbox",
                     "AuditLogger", "UndoManager"):
            assert getattr(module, name) is not None

    def test_missions_available_after_wave9(self):
        """Wave 9 shipped — the missions layer is importable and reports
        itself as available."""
        module = importlib.import_module("friday_v6.missions")
        assert module.is_available() is True
        for name in ("MissionEngine", "Planner", "Scheduler",
                     "progress_feed"):
            assert getattr(module, name) is not None

    def test_memory_available_after_wave10(self):
        """Wave 10 shipped — the memory layer is importable and reports
        itself as available."""
        module = importlib.import_module("friday_v6.memory")
        assert module.is_available() is True
        for name in ("FactMemory", "MemoryStore", "WorkingMemory",
                     "DecayReport"):
            assert getattr(module, name) is not None

    def test_persona_available_after_wave10(self):
        """Wave 10 shipped — the persona layer is importable and reports
        itself as available (verbatim conversation-log view, no keywords)."""
        module = importlib.import_module("friday_v6.persona")
        assert module.is_available() is True
        for name in ("IdentityEngine", "record_statement",
                     "recent_statements", "build_persona_context"):
            assert getattr(module, name) is not None

    def test_cli_persona_importable(self):
        """`friday6 persona` (Wave 10 identity CLI) imports cleanly."""
        from friday_v6.cli_persona import (cmd_persona_remember,
                                           build_persona_parser)
        assert cmd_persona_remember is not None
        assert build_persona_parser is not None

    def test_relationship_available_after_wave10(self):
        """Wave 10 shipped — the relationship layer is importable and
        reports itself as available."""
        module = importlib.import_module("friday_v6.relationship")
        assert module.is_available() is True
        for name in ("RelationshipEngine", "ToneSelector",
                     "compute_depth", "level_name", "briefing_length"):
            assert getattr(module, name) is not None

    def test_skills_available_after_wave10(self):
        """Wave 10 shipped — the skills layer is importable and reports
        itself as available."""
        module = importlib.import_module("friday_v6.skills")
        assert module.is_available() is True
        for name in ("SkillRegistry", "ReplayExecutor", "ShadowExecutor",
                     "SkillDispatcher"):
            assert getattr(module, name) is not None

    def test_cli_relationship_importable(self):
        """`friday6 relationship` (Wave 10 CLI) imports cleanly."""
        from friday_v6.cli_relationship import (cmd_relationship_status,
                                                build_relationship_parser)
        assert cmd_relationship_status is not None
        assert build_relationship_parser is not None

    def test_cli_skills_importable(self):
        """`friday6 skills` (Wave 10 CLI) imports cleanly."""
        from friday_v6.cli_skills import (cmd_skills_list,
                                          build_skills_parser)
        assert cmd_skills_list is not None
        assert build_skills_parser is not None

    def test_desktop_exports(self):
        """The desktop package exports the Wave 2 surface."""
        from friday_v6.desktop import (
            DesktopAbstraction,
            DesktopNotificationChannel,
            GlobalHotkeys,
            SystemTray,
            WindowManager,
        )
        assert WindowManager is not None
        assert DesktopAbstraction is not None
        assert SystemTray is not None
        assert GlobalHotkeys is not None
        assert DesktopNotificationChannel is not None

    def test_cli_mobile_importable(self):
        """`friday6 mobile` (Wave 15 companion transport) imports cleanly."""
        from friday_v6.cli_mobile import (build_mobile_parser,
                                          cmd_mobile_serve, cmd_mobile_push)
        assert build_mobile_parser is not None
        assert cmd_mobile_serve is not None
        assert cmd_mobile_push is not None

    def test_cli_missions_importable(self):
        """`friday6 mission` (Wave 9/18 CLI — Claude Code planner surface)
        imports cleanly."""
        from friday_v6.cli_missions import (build_mission_parser,
                                            cmd_mission_create,
                                            cmd_mission_replan)
        assert build_mission_parser is not None
        assert cmd_mission_create is not None
        assert cmd_mission_replan is not None
