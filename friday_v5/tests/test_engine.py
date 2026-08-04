"""Engine tests — SDK mocked, hermetic (no real model under test)."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.engine import Engine  # noqa: E402
from friday_v5.vault import Vault  # noqa: E402


class FakeClient:
    """A fake claude_agent_sdk client for the bridge's worker."""

    def __init__(self):
        self.deliver = None  # set by the test: bridge._handle_message
        self.connected = False
        self.disconnected = False
        self.received = []
        self.prompt_text = None

    async def connect(self, prompt):
        self.connected = True
        # The real bridge's connect generator never yields (it idles
        # forever to keep the CLI alive) — the fake must not consume
        # it, or it hangs.

    async def query(self, prompt, session_id):
        async for p in prompt:
            self.prompt_text = p["message"]["content"]
        self.received.append(session_id)
        # Emit a streamed chunk then the final result, exactly as the
        # real SDK surfaces them (deliver = bridge._handle_message, sync).
        self.deliver(_mk("AssistantMessage",
                         content=[_text_block("hi from ")]))
        self.deliver(_mk("ResultMessage", result="hi from friday"))

    async def receive_messages(self):
        if False:  # pragma: no cover - make this an async generator
            yield None

    async def disconnect(self):
        self.disconnected = True


class _text_block:
    type = "text"
    def __init__(self, text):
        self.text = text


def _mk(name, **kw):
    return type(name, (), kw)()


def _patch_sdk(monkeypatch, client):
    """Make claude_agent_sdk importable and inject the fake client.

    The bridge's worker thread builds its own client via
    ``claude_agent_sdk.client.ClaudeSDKClient`` — the fake must be a
    module-level factory, and the test wires ``client.deliver`` to the
    bridge's ``_handle_message`` once the engine's bridge exists.
    """
    import types as _types
    sdk = _types.ModuleType("claude_agent_sdk")
    client_mod = _types.ModuleType("claude_agent_sdk.client")
    types_mod = _types.ModuleType("claude_agent_sdk.types")

    def ClaudeSDKClient(options):
        client.options = options
        return client

    client_mod.ClaudeSDKClient = ClaudeSDKClient
    options_cls = type("ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None})
    matcher_cls = type("HookMatcher", (), {"__init__": lambda self, **kw: None})
    types_mod.ClaudeAgentOptions = options_cls
    types_mod.HookMatcher = matcher_cls
    sdk.client = client_mod
    sdk.types = types_mod
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.client", client_mod)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", types_mod)
    return client


def _wire(engine, client):
    """Route fake SDK messages through the bridge's handler."""
    client.deliver = engine.bridge._handle_message


def test_engine_routes_to_vault(tmp_path, monkeypatch):
    vault = Vault(tmp_path)
    client = _patch_sdk(monkeypatch, FakeClient())
    engine = Engine(vault=vault, cwd=tmp_path)
    _wire(engine, client)
    res = engine.ask("remember I like coffee")
    reply = engine.wait(timeout=5.0)
    assert res["ok"] is True
    assert reply == "hi from friday"
    # the engine prompt named the skill table + vault context
    assert "skills" in client.prompt_text
    assert "remember I like coffee" in client.prompt_text
    # user turn logged synchronously
    log = vault.raw / f"{__import__('datetime').date.today().isoformat()}.log"
    assert "remember I like coffee" in log.read_text()


def test_engine_emits_stream_and_final(tmp_path, monkeypatch):
    vault = Vault(tmp_path)
    seen = []
    client = _patch_sdk(monkeypatch, FakeClient())
    engine = Engine(vault=vault, cwd=tmp_path,
                    on_output=lambda t, f: seen.append((t, f)))
    _wire(engine, client)
    engine.ask("hello")
    reply = engine.wait(timeout=5.0)
    assert reply == "hi from friday"
    finals = [t for t, f in seen if f]
    assert finals and finals[-1] == "hi from friday"
    # final answer also logged to raw
    log = vault.raw / f"{__import__('datetime').date.today().isoformat()}.log"
    assert "hi from friday" in log.read_text()


def test_engine_unavailable_sdk_degrades(tmp_path, monkeypatch):
    vault = Vault(tmp_path)
    seen = []
    engine = Engine(vault=vault, cwd=tmp_path,
                    on_output=lambda t, f: seen.append((t, f)))
    # force available() False without touching the SDK
    engine.bridge._available = False
    res = engine.ask("hi")
    assert res["ok"] is False
    # the bridge emits a neutral final message on the callback
    assert any(f and "isn't available" in t for t, f in seen)


def test_skills_load_from_project_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / ".claude" / "skills"
    (skills_dir / "schedule").mkdir(parents=True)
    (skills_dir / "schedule" / "SKILL.md").write_text(
        "---\nname: schedule\ndescription: Manage the agenda\n---\nbody")
    monkeypatch.chdir(tmp_path)
    from friday_v5 import skills as skills_mod
    loaded = skills_mod.load_skills()
    assert [s.name for s in loaded] == ["schedule"]
    assert "Manage the agenda" in skills_mod.render_all()
