"""Friday V5 CLI — the terminal face until the HUD lands.

``friday5 ask "..."``  → engine → vault (streams Claude's reply)
``friday5 status``     → bridge + vault health
``friday5 vault ls``   → wiki notes
``friday5 vault find`` → grep the vault
``friday5 end``        → close the persistent session
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .bridge import ClaudeBridge
from .engine import Engine
from .skills import load_skills, render_all
from .vault import Vault

console = Console()


def _cmd_status(args) -> int:
    bridge = ClaudeBridge()
    st = bridge.status()
    vault = Vault()
    skills = load_skills()
    console.print(Panel(
        f"[bold]bridge[/bold]  available={st['available']} "
        f"active={st['active']} busy={st['busy']}\n"
        f"[bold]vault[/bold]   {vault.root}\n"
        f"  raw={len(list(vault.raw.glob('*.log')))}d  "
        f"wiki={len(list(vault.wiki.glob('*.md')))}n  "
        f"outputs={len(list(vault.outputs.iterdir()))}f\n"
        f"[bold]skills[/bold]  {len(skills)} "
        f"({', '.join(s.name for s in skills) or 'none'})",
        title="Friday V5 status"))
    return 0


def _cmd_ask(args) -> int:
    vault = Vault()
    console.print("[dim]Friday V5 — engine online. Stream follows.[/dim]")
    engine = Engine(vault=vault, on_output=_stream)
    result = engine.ask(" ".join(args.text))
    # Streamed output replaces the intro line; block for the final
    # answer so the CLI holds until Claude finishes.
    reply = engine.wait(timeout=120.0)
    if not reply:
        console.print("\n[red]no answer — check `friday5 status`[/red]")
        return 1
    console.print("\n[dim]— answer —[/dim]")
    console.print(Markdown(reply))
    return 0 if result.get("ok") else 1


_streaming = False


def _stream(text: str, final: bool) -> None:
    global _streaming
    if not _streaming:
        _streaming = True
        console.print()
    console.print(text if final else text + " ", end="")


def _cmd_vault(args) -> int:
    vault = Vault()
    if args.action == "ls":
        notes = vault.list_wiki()
        if not notes:
            console.print("(wiki empty)")
            return 0
        for p in notes:
            console.print(f"[cyan]{p.name}[/cyan]  {p.stat().st_size}B")
        return 0
    if args.action == "find":
        hits = vault.query(args.terms)
        if not hits:
            console.print("(no matches)")
            return 0
        for h in hits:
            console.print(h)
        return 0
    if args.action == "log":
        path = vault.log(args.role or "note", args.text)
        console.print(f"logged → {path}")
        return 0
    console.print("usage: friday5 vault {ls|find|log}")
    return 2


def _cmd_skills(args) -> int:
    console.print(render_all())
    return 0


def _cmd_talk(args) -> int:
    """Push-to-talk voice session (hold ctrl+space) with typed fallback."""
    from .engine import Engine
    from .voice import VoicePipeline, config_from_env

    vault = Vault()
    engine = Engine(vault=vault, on_output=None)
    config = config_from_env()
    pipe = VoicePipeline(config=config)

    # The engine's synchronous ask is the router backend; the reply is
    # spoken aloud by the pipeline's TTS.
    def route(text: str) -> str:
        reply = engine.ask_sync(text, timeout=120.0)
        if reply:
            pipe.speak(reply)
        return reply

    pipe.route_function = route

    # Hold-to-talk via the optional `keyboard` lib; fall back to typed
    # input when it's unavailable (V4's proven pattern).
    try:
        import keyboard  # type: ignore
    except Exception:
        keyboard = None  # type: ignore

    if not pipe.start():
        console.print("[red]No audio backend — run `friday5 "
                      "voice-status`.[/red]")
        return 1

    ptt_bound = False
    if keyboard is not None:
        def _on_text(text: str) -> None:
            console.print(f"[cyan]you:[/cyan] {text}")
            route(text)
        try:
            _bind_push_to_talk(pipe, _on_text, key=args.key)
            ptt_bound = True
        except Exception:
            ptt_bound = False

    if ptt_bound:
        console.print(f"[dim]Friday V5 — hold {args.key} to talk, "
                      "release to send. Ctrl+C to quit.[/dim]")
    else:
        console.print("[dim]Friday V5 — type (no push-to-talk hotkey "
                      "available). Ctrl+C to quit.[/dim]")

    try:
        while True:
            if not ptt_bound:
                line = input("> ")
                if line.strip():
                    console.print(f"[cyan]you:[/cyan] {line}")
                    route(line)
            else:
                pipe.wait_until_stopped(timeout=0.5)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        pipe.stop()
    return 0


def _bind_push_to_talk(pipeline, on_text, key: str = "ctrl+space") -> bool:
    """Bind a hold-to-talk global hotkey via the `keyboard` lib."""
    import keyboard  # type: ignore
    key = key.lower()
    keyboard.on_press_key(key, lambda e: pipeline.push_to_talk())
    keyboard.on_release_key(
        key, lambda e: on_text(pipeline.stop_recording_and_process()))
    return True


def _cmd_voice(args) -> int:
    """Hotword-driven voice session."""
    from .engine import Engine
    from .voice import VoicePipeline, config_from_env
    vault = Vault()
    engine = Engine(vault=vault, on_output=None)
    config = config_from_env()
    pipe = VoicePipeline(config=config)

    def route(text: str) -> str:
        reply = engine.ask_sync(text, timeout=120.0)
        if reply:
            pipe.speak(reply)
        return reply

    pipe.on_transcription = lambda text: console.print(
        f"[cyan]you:[/cyan] {text}")
    pipe.route_function = route
    console.print(f"[dim]Friday V5 — say \"{config.hotword}\" then "
                  "speak. Ctrl+C to quit.[/dim]")
    if not pipe.start():
        console.print("[red]No audio backend — run `friday5 "
                      "voice-status`.[/red]")
        return 1
    try:
        # The engine loop runs in its own thread; the main thread just
        # waits for Ctrl+C.
        while pipe.is_running:
            pipe.wait_until_stopped(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        pipe.stop()
    return 0


def _cmd_voice_status(args) -> int:
    from .voice import (
        VoicePipeline, config_from_env, list_input_devices,
    )
    config = config_from_env()
    pipe = VoicePipeline(config=config)
    in_dev = list_input_devices()
    console.print(Panel(
        f"[bold]hotword[/bold]    {config.hotword}\n"
        f"[bold]stt[/bold]        {config.stt_model}\n"
        f"[bold]tts[/bold]        {config.tts_provider} "
        f"(voice {config.tts_voice or 'default'})\n"
        f"[bold]inputs[/bold]     {len(in_dev)} found",
        title="Friday V5 voice"))
    for d in in_dev[:5]:
        console.print(f"  {d.name}  ({d.channels}ch)")
    return 0


def _cmd_end(args) -> int:
    engine = Engine(vault=Vault())
    res = engine.bridge.end()
    console.print("session closed" if res.get("ended") else "no session")
    return 0


def _cmd_perm(args) -> int:
    from .permissions import VaultPermissions
    store = VaultPermissions(Vault().root)
    files = store.pending_files()
    if not files:
        console.print("(no pending permission asks)")
        return 0
    for p in files:
        head = p.read_text(encoding="utf-8").splitlines()
        tool = next((l for l in head if l.startswith("- **tool**")), "")
        req = next((l for l in head if l.startswith("- **request**")), "")
        console.print(f"[yellow]{p.name}[/yellow]  {tool.strip('* ')} {req.strip('* ')}")
    return 0


def _cmd_allow(args) -> int:
    engine = Engine(vault=Vault())
    ok = engine.allow(args.id, args.reason or "approved")
    console.print(f"approved {args.id}" if ok else f"[red]no pending ask {args.id}[/red]")
    return 0 if ok else 1


def _cmd_deny(args) -> int:
    engine = Engine(vault=Vault())
    ok = engine.deny(args.id, args.reason or "denied")
    console.print(f"denied {args.id}" if ok else f"[red]no pending ask {args.id}[/red]")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="friday5",
                                description="Friday V5 — Claude engine, vault memory")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="send a request to the engine")
    a.add_argument("text", nargs="+", help="what you want")
    a.set_defaults(func=_cmd_ask)

    sub.add_parser("status", help="bridge + vault health").set_defaults(
        func=_cmd_status)

    v = sub.add_parser("vault", help="inspect the vault")
    vv = v.add_subparsers(dest="action", required=True)
    vv.add_parser("ls", help="list wiki notes").set_defaults(action="ls")
    f = vv.add_parser("find", help="grep the vault")
    f.add_argument("terms", nargs="+")
    f.set_defaults(action="find")
    lg = vv.add_parser("log", help="append to raw")
    lg.add_argument("text", nargs="+")
    lg.add_argument("--role", default="note")
    lg.set_defaults(action="log")
    v.set_defaults(func=_cmd_vault)

    sub.add_parser("skills", help="list available skills").set_defaults(
        func=_cmd_skills)
    sub.add_parser("end", help="close the Claude session").set_defaults(
        func=_cmd_end)

    tk = sub.add_parser("talk", help="push-to-talk voice session")
    tk.add_argument("--key", default="ctrl+space",
                    help="hold-to-talk hotkey (default ctrl+space)")
    tk.set_defaults(func=_cmd_talk)
    sub.add_parser("voice", help="hotword voice session").set_defaults(
        func=_cmd_voice)
    sub.add_parser("voice-status", help="voice diagnostics").set_defaults(
        func=_cmd_voice_status)

    sub.add_parser("perm", help="list pending permission asks").set_defaults(
        func=_cmd_perm)
    al = sub.add_parser("allow", help="approve a pending tool ask")
    al.add_argument("id")
    al.add_argument("--reason", default="")
    al.set_defaults(func=_cmd_allow)
    dn = sub.add_parser("deny", help="deny a pending tool ask")
    dn.add_argument("id")
    dn.add_argument("--reason", default="")
    dn.set_defaults(func=_cmd_deny)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
