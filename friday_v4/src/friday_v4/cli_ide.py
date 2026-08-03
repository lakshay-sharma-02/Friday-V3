"""`friday4 ide` — the IDE integration surface (Wave 6).

One command to see what Friday knows about your editor and your code:

    friday4 ide                      status summary
    friday4 ide detect               which editor is present (and how to reach it)
    friday4 ide diagnose FILE        issues in a file (LSP → AST fallback)
    friday4 ide symbols FILE         functions/classes outline
    friday4 ide open FILE            open a file in the editor
    friday4 ide reveal FILE LINE     jump to a line
    friday4 ide run CMD              run a command through the gated execution pipeline

Design laws: never crash (every command degrades honestly); the NL
surface (\"what's wrong with src/main.py\") is the primary interface —
this is the same layer behind it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .cli_status import _DIM, _GREEN, _RED, _RESET, _row, _print_logo

logger = logging.getLogger("friday_v4.cli_ide")


def _load():
    """The IDE module (guarded — never crashes the CLI)."""
    from .desktop import ide as mod
    return mod


def cmd_ide(args: argparse.Namespace) -> int:
    """`friday4 ide` — status summary (default subcommand)."""
    _print_logo("IDE Integration")
    mod = _load()
    try:
        detected = mod.detect_all()
    except Exception as exc:
        print(f"  {_RED}✘{_RESET} ide module failed: {exc}")
        return 1
    if not detected:
        _row("editor", "none detected (static analysis still available)", None)
    else:
        for ide in detected:
            _row("editor", f"{ide.name} ({ide.kind})", True)
            _row("launcher", ide.launcher or "—", ide.control_capable)
            _row("lsp", "capable" if ide.lsp_capable else "no",
                 ide.lsp_capable)
            _row("source", ide.source, None)
            print()
    preflight = mod.preflight_opted_in()
    _row("preflight", "on (FRIDAY_V4_IDE_PREFLIGHT)" if preflight
         else "off (set FRIDAY_V4_IDE_PREFLIGHT=1)", preflight)
    print()
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """`friday4 ide detect` — the editor(s) Friday sees."""
    _print_logo("IDE Detection")
    mod = _load()
    for ide in mod.detect_all():
        mark = "→" if ide.confidence == max(
            (i.confidence for i in mod.detect_all()), default=0.0) else " "
        print(f"  {mark} {_GREEN}{ide.name}{_RESET} "
              f"({ide.kind}, launcher: {ide.launcher or '—'}, "
              f"LSP: {'yes' if ide.lsp_capable else 'no'}, "
              f"conf {ide.confidence:.1f}, via {ide.source})")
    if not mod.detect_all():
        print(f"  {_DIM}No editor detected — analysis falls back to the "
              f"built-in static analyzer.{_RESET}")
    print()
    return 0


def _resolve_file(target: str, cwd: str | None) -> Path:
    p = Path(target).expanduser()
    if not p.is_absolute():
        base = Path(cwd).resolve() if cwd else Path.cwd()
        p = base / p
    return p.resolve()


def cmd_diagnose(args: argparse.Namespace) -> int:
    """`friday4 ide diagnose FILE` — issues in a file."""
    _print_logo("Diagnose")
    mod = _load()
    p = _resolve_file(args.file, args.cwd)
    try:
        res = mod.analyze_file(p, cwd=args.cwd)
    except Exception as exc:
        print(f"  {_RED}✘{_RESET} analysis failed: {exc}")
        return 1
    print(f"  {_DIM}{res.display_path} — via {res.method}{_RESET}")
    if res.method == "none":
        print(f"  {_DIM}Not a readable source file I can analyze.{_RESET}")
        print()
        return 0
    if not res.diagnostics:
        _row("result", "no issues found", True)
        print()
        return 0
    for d in res.diagnostics[:25]:
        icon = _RED if d.severity == 1 else _DIM
        print(f"  {icon}{d.severity_name:<8} line {d.line:<5} "
              f"{d.brief()}{_RESET}")
    _row("total", str(len(res.diagnostics)), None)
    print()
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    """`friday4 ide symbols FILE` — the file's outline."""
    _print_logo("Symbols")
    mod = _load()
    p = _resolve_file(args.file, args.cwd)
    try:
        syms = mod.symbols(p, cwd=args.cwd)
    except Exception as exc:
        print(f"  {_RED}✘{_RESET} symbol lookup failed: {exc}")
        return 1
    if not syms:
        print(f"  {_DIM}No symbols found.{_RESET}")
        print()
        return 0
    for s in syms[:40]:
        print(f"  {_GREEN}{s.kind_name:<9}{_RESET} {s.name:<32} "
              f"{_DIM}line {s.line}{_RESET}")
    print()
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    """`friday4 ide open FILE` — open in the detected editor."""
    _print_logo("Open")
    mod = _load()
    ide = mod.detect()
    if ide is None:
        print(f"  {_DIM}No editor detected — using the system opener.{_RESET}")
    ok, detail = mod.open_in_ide(args.file, ide=ide)
    print(f"  {_GREEN if ok else _RED}{'✓' if ok else '✘'}{_RESET} {detail}")
    print()
    return 0 if ok else 1


def cmd_reveal(args: argparse.Namespace) -> int:
    """`friday4 ide reveal FILE LINE` — jump to a line."""
    _print_logo("Reveal")
    mod = _load()
    ide = mod.detect()
    if ide is None:
        print(f"  {_DIM}No editor detected — using the system opener.{_RESET}")
    ok, detail = mod.reveal_in_ide(args.file, args.line, ide=ide)
    print(f"  {_GREEN if ok else _RED}{'✓' if ok else '✘'}{_RESET} {detail}")
    print()
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    """`friday4 ide run CMD` — through the SAME gated execution pipeline.

    The IDE surface composes with execution: the command runs through
    gate → sandbox → audit exactly like `friday4 talk \"run …\"` — and
    when FRIDAY_V4_IDE_PREFLIGHT is set, the IDE diagnostics for the
    file the command touches ride along in the audit goal.
    """
    _print_logo("Run in workspace")
    try:
        from .execution import execute
    except Exception as exc:
        print(f"  {_RED}✘{_RESET} execution layer unavailable: {exc}")
        return 1
    from . import db
    conn = db.connect()

    def _confirm(description: str) -> bool:
        try:
            answer = input(f"  {description} [y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    result = execute("shell", args.command, cwd=args.cwd, conn=conn,
                     confirm_fn=_confirm, force=args.force,
                     goal=f"ide run: {args.command}")
    conn.close()
    if result.status == "succeeded":
        first = (result.output or "").strip().splitlines()[:5]
        for line in first:
            print(f"  {_GREEN}✓{_RESET} {line[:200]}")
        print()
        return 0
    if result.status == "denied":
        print(f"  {_DIM}Denied by the gate — {result.output[:160]}{_RESET}")
        print()
        return 1
    print(f"  {_RED}✘{_RESET} {result.status}: "
          f"{(result.output or '').strip().splitlines()[:1] or ['failed']}")
    print()
    return 1


def build_ide_parser(subparsers) -> None:
    """Register `friday4 ide` (Wave 6 IDE integration)."""
    parser = subparsers.add_parser(
        "ide", help="IDE integration (LSP analysis + editor control)",
        description="Friday inside your editor: detect it, analyze code "
                    "through LSP (AST fallback), open/reveal files, and "
                    "run workspace commands through the gated pipeline.",
    )
    ide_sub = parser.add_subparsers(dest="ide_command")

    p = ide_sub.add_parser("detect", help="Show the editor(s) Friday detects")
    p.set_defaults(func=cmd_detect)

    p = ide_sub.add_parser("diagnose", help="Show issues in a file")
    p.add_argument("file", help="Path to the source file")
    p.add_argument("--cwd", type=str, default=None,
                   help="Working directory (default: current)")
    p.set_defaults(func=cmd_diagnose)

    p = ide_sub.add_parser("symbols", help="Show a file's outline")
    p.add_argument("file", help="Path to the source file")
    p.add_argument("--cwd", type=str, default=None,
                   help="Working directory (default: current)")
    p.set_defaults(func=cmd_symbols)

    p = ide_sub.add_parser("open", help="Open a file in the detected editor")
    p.add_argument("file", help="Path to the file")
    p.set_defaults(func=cmd_open)

    p = ide_sub.add_parser("reveal", help="Reveal a line in the editor")
    p.add_argument("file", help="Path to the file")
    p.add_argument("line", type=int, help="1-based line number")
    p.set_defaults(func=cmd_reveal)

    p = ide_sub.add_parser("run", help="Run a command through the gated pipeline")
    p.add_argument("command", help="The shell command to run")
    p.add_argument("--cwd", type=str, default=None,
                   help="Working directory (default: current)")
    p.add_argument("--force", action="store_true",
                   help="Bypass the confirm gate (operator override)")
    p.set_defaults(func=cmd_run)

    parser.set_defaults(func=cmd_ide)


__all__ = ["build_ide_parser", "cmd_ide", "cmd_detect", "cmd_diagnose",
           "cmd_symbols", "cmd_open", "cmd_reveal", "cmd_run"]
