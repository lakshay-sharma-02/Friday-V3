"""File-based permission tests — hermetic, no real SDK/model."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.permissions import (  # noqa: E402
    VaultPermissions,
    make_can_use_tool,
    make_pre_tool_hook,
    registry,
)


def _clear_registry():
    registry._asks.clear()


def test_vault_tools_auto_allowed(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()
    try:
        can_use_tool = make_can_use_tool(tmp_path, loop, timeout=0.1)
        for tool in ("Read", "Edit", "Write", "Glob", "Grep"):
            res = loop.run_until_complete(
                can_use_tool(tool, {"file_path": "/x"}, None))
            assert res == {"behavior": "allow"}, tool
    finally:
        loop.close()


def test_bash_writes_ask_then_denies_on_timeout(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()
    try:
        can_use_tool = make_can_use_tool(tmp_path, loop, timeout=0.3)
        res = loop.run_until_complete(
            can_use_tool("Bash", {"command": "rm -rf /tmp/x"}, None))
        assert res["behavior"] == "deny"
        # ask file moved to denied archive
        pending = list((tmp_path / "permissions" / "pending").glob("*.md"))
        denied = list((tmp_path / "permissions" / "denied").glob("*.md"))
        assert pending == []
        assert len(denied) == 1
        body = denied[0].read_text()
        assert "Bash" in body and "rm -rf" in body
        assert "resolved" in body
    finally:
        loop.close()


def test_allow_resolves_bash(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()


def test_vault_tools_auto_allowed(tmp_path):
    loop = asyncio.new_event_loop()
    try:
        can_use_tool = make_can_use_tool(tmp_path, loop, timeout=0.1)
        for tool in ("Read", "Edit", "Write", "Glob", "Grep"):
            res = loop.run_until_complete(
                can_use_tool(tool, {"file_path": "/x"}, None))
            assert res == {"behavior": "allow"}, tool
    finally:
        loop.close()


def test_bash_writes_ask_then_denies_on_timeout(tmp_path):
    loop = asyncio.new_event_loop()
    try:
        can_use_tool = make_can_use_tool(tmp_path, loop, timeout=0.2)
        res = loop.run_until_complete(
            can_use_tool("Bash", {"command": "rm -rf /tmp/x"}, None))
        assert res["behavior"] == "deny"
        # ask file moved to denied archive
        pending = list((tmp_path / "permissions" / "pending").glob("*.md"))
        denied = list((tmp_path / "permissions" / "denied").glob("*.md"))
        assert pending == []
        assert len(denied) == 1
        body = denied[0].read_text()
        assert "Bash" in body and "rm -rf" in body
        assert "resolved" in body
    finally:
        loop.close()


def test_allow_resolves_bash(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()
    try:
        can_use_tool = make_can_use_tool(tmp_path, loop, timeout=5.0)
        # approve via the sidecar, exactly as a separate ``friday5
        # allow`` process does
        async def ask_and_approve():
            task = asyncio.ensure_future(
                can_use_tool("Bash", {"command": "ls"}, None))
            for _ in range(50):
                asks = list(
                    (tmp_path / "permissions" / "pending").glob("*.md"))
                if asks:
                    rid = asks[0].stem
                    (tmp_path / "permissions" / "pending" /
                     f"{rid}.decision").write_text(
                        "allow", encoding="utf-8")
                    break
                await asyncio.sleep(0.01)
            return await task

        res = loop.run_until_complete(ask_and_approve())
        assert res == {"behavior": "allow"}
        # archived to approved, not denied
        denied = list((tmp_path / "permissions" / "denied").glob("*.md"))
        approved = list((tmp_path / "permissions" / "approved").glob("*.md"))
        assert denied == []
        assert len(approved) == 1
    finally:
        loop.close()


def test_gated_tools_list():
    from friday_v5.permissions import GATED_TOOLS, AUTO_TOOLS
    assert "Bash" in GATED_TOOLS
    assert not (AUTO_TOOLS & GATED_TOOLS)


def test_pre_tool_hook_allows_vault_tools(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()
    try:
        hook = make_pre_tool_hook(tmp_path, loop)
        for tool in ("Read", "Edit", "Write", "Glob", "Grep"):
            out = loop.run_until_complete(hook(
                {"tool_name": tool, "tool_input": {"file_path": "/x"}},
                None, None))
            assert out["permissionDecision"] == "allow", tool
            assert out["hookEventName"] == "PreToolUse"
    finally:
        loop.close()


def test_pre_tool_hook_gates_bash(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()
    try:
        hook = make_pre_tool_hook(tmp_path, loop, timeout=0.3)
        out = loop.run_until_complete(hook(
            {"tool_name": "Bash",
             "tool_input": {"command": "rm -rf /tmp/x"}},
            None, None))
        assert out["permissionDecision"] == "deny"
        # ask archived to denied
        denied = list((tmp_path / "permissions" / "denied").glob("*.md"))
        assert len(denied) == 1
        assert "Bash" in denied[0].read_text()
    finally:
        loop.close()


def test_hook_approves_via_sidecar(tmp_path):
    _clear_registry()
    loop = asyncio.new_event_loop()
    try:
        hook = make_pre_tool_hook(tmp_path, loop, timeout=5.0)

        async def ask_and_decide():
            task = asyncio.ensure_future(hook(
                {"tool_name": "Bash",
                 "tool_input": {"command": "ls"}},
                None, None))
            # write the sidecar as the operator would (separate process)
            for _ in range(50):
                asks = list(
                    (tmp_path / "permissions" / "pending").glob("*.md"))
                if asks:
                    rid = asks[0].stem
                    (tmp_path / "permissions" / "pending" /
                     f"{rid}.decision").write_text(
                        "allow", encoding="utf-8")
                    break
                await asyncio.sleep(0.01)
            return await task

        out = loop.run_until_complete(ask_and_decide())
        assert out["permissionDecision"] == "allow"
        approved = list((tmp_path / "permissions" / "approved").glob("*.md"))
        assert len(approved) == 1
    finally:
        loop.close()
