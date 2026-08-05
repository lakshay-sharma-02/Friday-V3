"""Tests for the `friday6 collab` CLI surface."""

from __future__ import annotations

import argparse

import pytest


class _FakePermissions:
    def members(self):
        return {"host": "owner"}


class _FakeCoordinator:
    """Stand-in that never touches the real ~/.friday state dir."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.sync = _FakeSync()
        self.permissions = _FakePermissions()

    def start(self):
        return True

    def stop(self):
        return None

    def peers(self):
        return []

    def observations(self, limit=None):
        return [{"id": "k1", "peer_id": "bob", "ts": 1000,
                 "payload": {"text": "hello"}}]

    def add_observation(self, payload):
        return "obs-1"

    def status(self):
        return {
            "peer_id": "host", "workspace": self.kwargs.get("workspace",
                                                            "default"),
            "running": False, "sync_port": 9876, "beacon_port": 9988,
            "peers": [], "observations": 1, "live_observations": 1,
            "permissions": {"members": {"host": "owner"}},
        }

    def add_member(self, peer_id, role="member"):
        return None

    def remove_member(self, peer_id):
        return True


class _FakeSync:
    def sync_with(self, host, port):
        return {"accepted": True, "sent": 1, "received": 1, "applied": 1}


@pytest.fixture
def fake_coordinator(monkeypatch):
    monkeypatch.setattr("friday_v6.cli_collab.Coordinator",
                        _FakeCoordinator)
    return _FakeCoordinator


class TestCollabCLIParser:
    def test_parser_registers_subcommands(self):
        from friday_v6.cli_collab import build_collab_parser
        parser = argparse.ArgumentParser(prog="friday6")
        subparsers = parser.add_subparsers(dest="command")
        build_collab_parser(subparsers)
        args = parser.parse_args(
            ["collab", "status", "--workspace", "ws"])
        assert args.collab_command == "status"
        assert args.workspace == "ws"

    def test_perms_choices(self):
        from friday_v6.cli_collab import build_collab_parser
        parser = argparse.ArgumentParser(prog="friday6")
        subparsers = parser.add_subparsers(dest="command")
        build_collab_parser(subparsers)
        args = parser.parse_args(
            ["collab", "perms", "add", "bob", "reader"])
        assert args.perms_command == "add"
        assert args.peer_id == "bob"
        assert args.role == "reader"


class TestCollabCLICommands:
    def test_status_command(self, fake_coordinator):
        from friday_v6.cli_collab import main
        assert main(["status", "--workspace", "ws"]) == 0

    def test_add_json_payload(self, fake_coordinator):
        from friday_v6.cli_collab import main
        assert main(["add", '{"kind": "app_open", "app": "kitty"}']) == 0

    def test_add_text_payload(self, fake_coordinator):
        from friday_v6.cli_collab import main
        assert main(["add", "focused on", "codebuff"]) == 0

    def test_observations_command(self, fake_coordinator):
        from friday_v6.cli_collab import main
        assert main(["observations", "--limit", "5"]) == 0

    def test_share_command(self, fake_coordinator):
        from friday_v6.cli_collab import main
        assert main(["share", "--host", "127.0.0.1",
                     "--peer-port", "9999"]) == 0

    def test_perms_commands(self, fake_coordinator):
        from friday_v6.cli_collab import main
        assert main(["perms", "list"]) == 0
        assert main(["perms", "add", "bob", "member"]) == 0
        assert main(["perms", "remove", "bob"]) == 0

    def test_wired_into_friday6_main(self, fake_coordinator):
        """`friday6 collab status` routes through the main CLI dispatch."""
        from friday_v6 import cli_talk
        assert cli_talk.main(["collab", "status"]) == 0
