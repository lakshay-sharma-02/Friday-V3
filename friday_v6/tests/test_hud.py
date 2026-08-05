"""Hermetic tests for Wave 3 — the Textual HUD (Friday's face).

Covers:
- hud/parsers.py: schedule/notice/activity parsers + pure render
  functions (NO Textual import — fully hermetic)
- hud/vitals.py: format_vitals resilience (psutil optional)
- hud/controller.py: the HUD brain — routes the SAME TextCommandHandler,
  mirrors the durable ambient stream, resolves the SAME autonomy asks
  (allow/deny), vault panels degrade honestly
- hud/__init__.py: is_available + run_hud degrade path (no Textual →
  printed hint, exit 1; never a crash)
- cli_hud.py: `friday6 hud` parses and calls run_hud
- app.py: HUD constructible + compose works (Textual present here;
  the test skips gracefully if the optional dep is missing)

Safety laws verified:
- The pure layer has zero Textual imports (hermetic).
- Missing DB/vault/ambient → honest empty values, never a crash.
- The HUD input goes through the same handler as every surface.
- Permission asks are the same durable asks as phone/web/CLI.
"""

from __future__ import annotations

from pathlib import Path

from friday_v6 import db
from friday_v6.hud import parsers, run_hud, is_available
from friday_v6.hud.vitals import format_vitals


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# hud/parsers.py — pure, no Textual
# ==========================================================================


class TestHudParsers:
    def test_parse_schedule_skips_frontmatter_and_done(self, tmp_path):
        p = tmp_path / "schedule.md"
        p.write_text("---\nstatus: active\n---\n\n"
                     "- 2026-08-05 09:00 — Standup (15 min)\n"
                     "- [x] 2026-08-04 10:00 — done thing\n",
                     encoding="utf-8")
        items = parsers.parse_schedule(p)
        assert items == ["2026-08-05 09:00 — Standup (15 min)"]

    def test_parse_schedule_missing_file(self, tmp_path):
        assert parsers.parse_schedule(tmp_path / "nope.md") == []

    def test_parse_notice_text_strips_meta(self, tmp_path):
        p = tmp_path / "1700000000-hello.md"
        p.write_text("# Notice\n\n- **at**: 2023\n- **id**: 1700000000\n\n"
                     "standup at 9am\n", encoding="utf-8")
        assert parsers.parse_notice_text(p) == "standup at 9am"

    def test_tail_log_last_lines(self, tmp_path):
        p = tmp_path / "2026-08-04.log"
        p.write_text("line1\nline2\nline3\n", encoding="utf-8")
        assert parsers.tail_log(p, 2) == ["line2", "line3"]
        assert parsers.tail_log(tmp_path / "missing.log", 2) == []

    def test_render_functions(self):
        assert "nothing scheduled" in parsers.render_schedule([])
        assert "Standup" in parsers.render_schedule(
            ["2026-08-05 09:00 — Standup"])
        assert parsers.render_notices([{"text": "standup at 9am"}]) \
            == "· standup at 9am"
        assert "(no activity yet today)" in parsers.render_activity([])
        assert "allow" not in parsers.render_permissions([])
        assert parsers.render_permissions(
            [{"id": "abc", "command": "git status"}]) == "[abc] git status"
        assert parsers.render_stream([]) == "(idle)"
        assert "you: hello" in parsers.render_stream(["you: hello"])

    def test_render_search(self):
        assert parsers.render_search([], "index") == "nothing found"
        assert parsers.render_search([], "grep") == "nothing found"
        one = parsers.render_search(["wiki/auth.md: shared auth"], "index")
        assert "auth" in one and "(fts)" in one and "1 hit" not in one
        many = parsers.render_search(["a", "b", "c"], "grep")
        assert "(grep, 3 hits)" in many
        # More matches than shown → honest "showing X of N".
        truncated = parsers.render_search([f"h{i}" for i in range(12)], "fts")
        assert "showing 8 of 12" in truncated
        # Hit text is bounded for the panel.
        long = parsers.render_search(["x" * 300], "index")
        assert len(max(long.splitlines(), key=len)) <= 125

    def test_find_terms_pure(self):
        """The /find routing decision is pure + hermetic (no Textual)."""
        assert parsers._find_terms("/find auth") == "auth"
        assert parsers._find_terms("/find   auth module") == "auth module"
        assert parsers._find_terms("/find") == ""       # bare → ask
        assert parsers._find_terms("/findx") == ""      # not a find
        assert parsers._find_terms("find auth") == ""   # no slash
        assert parsers._find_terms("hello") == ""
        assert parsers._find_terms("  /find auth  ") == "auth"

    def test_format_ambient_event(self):
        from friday_v6.ambient import Event, Priority
        ev = Event("security", "2 high-sev vulns", Priority.IMPORTANT)
        out = parsers.format_ambient_event(ev)
        assert "[security]" in out and "2 high-sev vulns" in out

    def test_module_has_no_textual_import(self):
        """The pure layer must stay hermetic — no Textual IMPORT."""
        src = Path(parsers.__file__).read_text(encoding="utf-8")
        assert "import textual" not in src.lower()


# ==========================================================================
# hud/vitals.py — pure formatting
# ==========================================================================


class TestHudVitals:
    def test_format_vitals_renders_readings(self):
        out = format_vitals(cpu=12.0, mem_gb=3.2, disk_pct=61.0)
        assert "cpu 12%" in out
        assert "mem 3.2G" in out
        assert "disk 61%" in out

    def test_format_vitals_handles_missing(self):
        out = format_vitals(cpu=None, mem_gb=None, disk_pct=None)
        assert "cpu ?" in out and "mem ?" in out and "disk ?" in out

    def test_vitals_module_has_no_textual_import(self):
        from friday_v6.hud import vitals
        src = Path(vitals.__file__).read_text(encoding="utf-8")
        assert "import textual" not in src.lower()


# ==========================================================================
# hud/controller.py — the HUD brain (same handler, same asks)
# ==========================================================================


class TestHudController:
    def _controller(self, tmp_path):
        from friday_v6.hud.controller import HudController
        return HudController(conn=_conn(tmp_path),
                             vault_root=str(tmp_path / "vault"))

    def test_handle_routes_through_shared_brain(self, tmp_path):
        ctrl = self._controller(tmp_path)
        reply = ctrl.handle("hello")
        assert "Friday" in reply  # the same handler's greeting
        lines = ctrl.stream_lines(5)
        assert any("you: hello" in l for l in lines)
        assert any("Friday" in l for l in lines)

    def test_handle_empty_is_noop(self, tmp_path):
        ctrl = self._controller(tmp_path)
        assert ctrl.handle("   ") == ""
        assert ctrl.handle("") == ""

    def test_handle_never_raises(self, tmp_path):
        ctrl = self._controller(tmp_path)
        reply = ctrl.handle("\x00\x01 weird \xff input") or "ok"
        assert isinstance(reply, str)

    def test_stream_mirrors_ambient_replay(self, tmp_path):
        conn = _conn(tmp_path)
        from friday_v6.ambient import AmbientBus, Event
        AmbientBus(conn).publish(Event("security", "2 high-sev vulns"))
        from friday_v6.hud.controller import HudController
        ctrl = HudController(conn=conn, vault_root=str(tmp_path / "vault"))
        lines = ctrl.stream_lines(10)
        assert any("security" in l and "high-sev" in l for l in lines)

    def test_pending_asks_and_deny(self, tmp_path):
        """The HUD resolves the SAME durable asks as phone/web/CLI."""
        conn = _conn(tmp_path)
        rid = db.create_permission_request(
            conn, "run the deploy script", "shell",
            command="deploy.sh", source="hud-test")
        from friday_v6.hud.controller import HudController
        ctrl = HudController(conn=conn, vault_root=str(tmp_path / "vault"))
        pending = ctrl.pending_asks()
        assert any(r["id"] == rid for r in pending)
        assert ctrl.deny(rid) is True
        assert ctrl.pending_asks() == []

    def test_allow_without_pending_is_honest(self, tmp_path):
        ctrl = self._controller(tmp_path)
        assert ctrl.allow("nope") is None

    def test_schedule_notices_activity_degrade(self, tmp_path):
        ctrl = self._controller(tmp_path)
        assert ctrl.schedule() == []          # no schedule.md → empty
        assert ctrl.notices() == []           # no notices → empty
        assert ctrl.activity() == []          # no raw log → empty
        assert "cpu" in ctrl.vitals()         # vitals always render

    def test_controller_has_no_textual_import(self):
        from friday_v6.hud import controller
        src = Path(controller.__file__).read_text(encoding="utf-8")
        assert "import textual" not in src.lower()

    def test_search_uses_vault_fts_first_grep_fallback(self, tmp_path):
        """Wave 6 — FTS in HUD search: the controller searches the vault
        through the SAME index-first / grep-fallback path as `vault find`."""
        from friday_v6.hud.controller import HudController
        vault_root = tmp_path / "vault"
        from friday_v6.vault import Vault, VaultIndex
        v = Vault(vault_root)
        v.note("auth", "shared auth module for the family")
        ctrl = HudController(conn=_conn(tmp_path), vault_root=str(vault_root))
        # No index built yet → grep floor.
        hits, source = ctrl.search("auth")
        assert hits and any("auth.md" in h for h in hits)
        assert source == "grep"
        # Rebuild the FTS cache → index answers.
        VaultIndex(vault_root).rebuild()
        hits, source = ctrl.search("auth")
        assert hits and any("auth.md" in h for h in hits)
        assert source == "index"
        # No hits → honest empty, never a crash.
        assert ctrl.search("zzz-nothing") == ([], "grep")


# ==========================================================================
# hud/__init__.py — availability + degrade path
# ==========================================================================


class TestHudInit:
    def test_is_available(self):
        assert isinstance(is_available(), bool)

    def test_run_hud_degrades_without_textual(self, capsys, monkeypatch):
        """Missing Textual → printed hint + exit 1, never a crash.

        ``None`` in ``sys.modules`` makes ``import textual`` raise
        ImportError deterministically (the standard blocking trick),
        so ``run_hud`` takes its degrade branch regardless of whether
        the optional dep is installed here.
        """
        import sys
        monkeypatch.setitem(sys.modules, "textual", None)
        assert run_hud() == 1
        assert "textual" in capsys.readouterr().out.lower()

    def test_hud_package_exports(self):
        import friday_v6.hud as hud
        for name in ("HudController", "run_hud", "is_available",
                     "format_vitals", "render_schedule"):
            assert getattr(hud, name) is not None
        assert hasattr(hud, "__all__")


# ==========================================================================
# cli_hud.py — friday6 hud
# ==========================================================================


class TestCliHud:
    def test_hud_command_parses(self):
        from friday_v6.cli_hud import build_hud_parser
        import argparse
        parser = argparse.ArgumentParser()
        build_hud_parser(parser.add_subparsers())
        args = parser.parse_args(["hud", "--root", "/tmp/x"])
        assert getattr(args, "func") is not None

    def test_cmd_hud_calls_run_hud(self, tmp_path, monkeypatch, capsys):
        from friday_v6 import cli_hud
        calls = []
        def fake_run_hud(**kw):
            calls.append(kw)
            return 0
        monkeypatch.setattr("friday_v6.hud.run_hud", fake_run_hud)
        from types import SimpleNamespace
        args = SimpleNamespace(root=str(tmp_path / "vault"), db=None)
        assert cli_hud.cmd_hud(args) == 0
        assert calls and calls[0]["vault_root"] == str(tmp_path / "vault")
        assert calls[0]["conn"] is None

    def test_cmd_hud_resolves_db_path_to_connection(self, tmp_path,
                                                    monkeypatch):
        """``--db PATH`` must reach run_hud as a real connection, not a
        raw string — otherwise asks/stream read nothing."""
        from friday_v6 import cli_hud
        db_path = tmp_path / "v4.db"
        conn = db.connect(db_path)
        try:
            calls = []
            def fake_run_hud(**kw):
                calls.append(kw)
                return 0
            monkeypatch.setattr("friday_v6.hud.run_hud", fake_run_hud)
            from types import SimpleNamespace
            args = SimpleNamespace(root=str(tmp_path / "vault"),
                                   db=str(db_path))
            assert cli_hud.cmd_hud(args) == 0
            got = calls[0]["conn"]
            assert got is not None
            assert hasattr(got, "execute")  # a connection, not a str
            assert got is not conn          # and not the test's own
        finally:
            conn.close()


# ==========================================================================
# hud/app.py — the Textual App (skips if the optional dep is missing)
# ==========================================================================


class TestHudApp:
    def test_app_composes(self):
        """HUD constructible + composes all panels (Textual present)."""
        pytest_importorskip_textual()
        from friday_v6.hud.controller import HudController
        from friday_v6.hud.app import HUD
        import tempfile
        conn = db.connect(tempfile.mkdtemp() + "/v4.db")
        try:
            ctrl = HudController(conn=conn, vault_root=tempfile.mkdtemp())
            app = HUD(controller=ctrl)
            assert app is not None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def test_run_test_headless(self):
        """The App runs headless (Textual pilot) — input routes the brain."""
        pytest_importorskip_textual()
        import tempfile
        from friday_v6.hud.controller import HudController
        from friday_v6.hud.app import HUD
        import asyncio

        conn = db.connect(tempfile.mkdtemp() + "/v4.db")
        ctrl = HudController(conn=conn, vault_root=tempfile.mkdtemp())
        app = HUD(controller=ctrl)

        async def _drive():
            async with app.run_test() as pilot:
                await pilot.pause()
                # Type an utterance into the prompt → routed via the brain.
                from textual.widgets import Input
                inp = app.query_one(Input)
                inp.value = "hello"
                await pilot.press("enter")
                await pilot.pause()
                assert "Friday" in ctrl.stream_lines(5)[-1] or \
                       any("Friday" in l for l in ctrl.stream_lines(5))

        try:
            asyncio.run(_drive())
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def test_run_test_find_command(self):
        """`/find <terms>` in the HUD prompt searches the vault (FTS),
        NOT the brain — same index-first path as `vault find`."""
        pytest_importorskip_textual()
        import tempfile
        from friday_v6.hud.controller import HudController
        from friday_v6.hud.app import HUD
        from friday_v6.vault import Vault
        import asyncio

        vault_root = tempfile.mkdtemp()
        Vault(vault_root).note("auth", "shared auth module for the family")
        conn = db.connect(tempfile.mkdtemp() + "/v4.db")
        ctrl = HudController(conn=conn, vault_root=vault_root)
        app = HUD(controller=ctrl)

        async def _drive():
            async with app.run_test() as pilot:
                await pilot.pause()
                from textual.widgets import Input
                inp = app.query_one(Input)
                inp.value = "/find auth"
                await pilot.press("enter")
                await pilot.pause()
                from friday_v6.hud.prompt import PromptPanel
                out = app.query_one(PromptPanel)._output.render()
                assert "auth.md" in str(out)
                # A bare /find with no terms asks honestly.
                inp.value = "/find"
                await pilot.press("enter")
                await pilot.pause()
                # Re-render after the second submit: ``render()`` returns
                # a snapshot, so the old ``out`` cannot reflect the new
                # output (asserting on it would fail on every Textual).
                out = app.query_one(PromptPanel)._output.render()
                assert "find what" in str(out)

        try:
            asyncio.run(_drive())
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def test_run_test_permission_button(self):
        """A pending ask renders real allow/deny buttons; pressing deny
        resolves the SAME durable ask (no execution — deny only)."""
        pytest_importorskip_textual()
        import tempfile
        from friday_v6.hud.controller import HudController
        from friday_v6.hud.app import HUD
        from friday_v6.hud.permissions_panel import PermissionsPanel
        import asyncio

        conn = db.connect(tempfile.mkdtemp() + "/v4.db")
        rid = db.create_permission_request(
            conn, "run the deploy script", "shell",
            command="deploy.sh", source="hud-test")
        ctrl = HudController(conn=conn, vault_root=tempfile.mkdtemp())
        app = HUD(controller=ctrl)

        async def _drive():
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = app.query_one(PermissionsPanel)
                rendered = str(panel._summary.render())
                assert rid in rendered  # the ask is listed
                assert "run the deploy script" in rendered
                # Press the deny button for the ask → resolved.
                await pilot.click("#deny-0")
                await pilot.pause()
                assert ctrl.pending_asks() == []
                assert "no pending asks" in str(panel._summary.render())

        try:
            asyncio.run(_drive())
        finally:
            try:
                conn.close()
            except Exception:
                pass


def pytest_importorskip_textual():
    try:
        import textual  # noqa: F401
    except Exception:
        import pytest
        pytest.skip("textual not installed — skipping app test")
