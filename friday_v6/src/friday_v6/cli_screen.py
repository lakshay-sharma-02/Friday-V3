"""CLI commands for `friday6 screen` — Friday's eyes and hands (Wave 23).

Usage:
    friday6 screen capture              # Screenshot → PNG path
    friday6 screen ocr                  # Capture + read the text on screen
    friday6 screen find <target>        # Locate "login button" → coordinates
    friday6 screen click <x> <y>        # Click at coordinates (asks first)
    friday6 screen click --target <t>   # Find "login" on screen, then click
    friday6 screen type <text>          # Type into the focused window (asks)
    friday6 screen scroll [up|down]     # Scroll the focused window (asks)
    friday6 screen key <name>           # Press enter / ctrl+c / … (asks)

The NL path is the product: `friday6 "click the login button"`. This
CLI is the debug hatch — and the only place real input actions can be
confirmed non-interactively (--yes, the operator's explicit override).

Safety: clicking/typing/keys are REAL input to your desktop — the CLI
prompts y/N by default and only acts on an explicit yes.
"""

from __future__ import annotations

import argparse
import logging

from .screen.controller import InputController, ScreenController

logger = logging.getLogger("friday_v6.cli_screen")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V6 — Screen{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _confirm(description: str, yes: bool = False) -> bool:
    """Operator approval for a real input action (EOF → safe deny)."""
    if yes:
        return True
    try:
        print(f"\n  {_YELLOW}→ Friday wants to:{_RESET} {description}")
        answer = input(f"  {_BOLD}Allow? [y/N] {_RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def cmd_screen_capture(args: argparse.Namespace) -> int:
    """`friday6 screen capture` — screenshot → PNG path."""
    screen = ScreenController(output_dir=args.out)
    result = screen.capture(args.output)
    if result.ok:
        print(f"  {_GREEN}📸 Captured{_RESET}")
        print(f"  {_DIM}  {result.image_path}{_RESET}")
        return 0
    print(f"  {_RED}✗ {result.message}{_RESET}")
    return 1


def cmd_screen_ocr(args: argparse.Namespace) -> int:
    """`friday6 screen ocr` — read the text currently on screen."""
    screen = ScreenController(output_dir=args.out)
    result = screen.ocr(args.image)
    if not result.ok:
        print(f"  {_RED}✗ {result.message}{_RESET}")
        return 1
    words = result.words or []
    if not words:
        print(f"  {_DIM}I can't read any text on the screen.{_RESET}")
        return 0
    # Group into lines by vertical band.
    lines: list[str] = []
    current: list[tuple[int, str]] = []
    for w in sorted(words, key=lambda w: (w.top, w.left)):
        if current and abs(w.top - current[-1][0]) > 15:
            lines.append(" ".join(t for _, t in current))
            current = []
        current.append((w.top, w.text))
    if current:
        lines.append(" ".join(t for _, t in current))
    for line in lines:
        print(f"  {_DIM}{line}{_RESET}")
    print(f"\n  {_CYAN}→ {len(words)} word(s) read{_RESET}")
    return 0


def cmd_screen_find(args: argparse.Namespace) -> int:
    """`friday6 screen find <target>` — locate text on screen."""
    from .screen.parsers import find_click_target, find_phrase_region
    screen = ScreenController(output_dir=args.out)
    result = screen.ocr(args.image)
    if not result.ok:
        print(f"  {_RED}✗ {result.message}{_RESET}")
        return 1
    words = result.words or []
    target = " ".join(args.target)
    region = find_phrase_region(words, target)
    if region:
        left = min(w.left for w in region)
        top = min(w.top for w in region)
        right = max(w.left + w.width for w in region)
        bottom = max(w.top + w.height for w in region)
        print(f"  {_GREEN}✓ Found '{target}'{_RESET}")
        print(f"  {_DIM}  center: ({(left + right) // 2}, "
              f"{(top + bottom) // 2}){_RESET}")
        return 0
    word = find_click_target(words, target)
    if word:
        x, y = word.center
        print(f"  {_GREEN}✓ Found '{target}'{_RESET}")
        print(f"  {_DIM}  center: ({x}, {y}){_RESET}")
        return 0
    print(f"  {_YELLOW}! I can't see '{target}' on the screen.{_RESET}")
    return 1


def cmd_screen_click(args: argparse.Namespace) -> int:
    """`friday6 screen click <x> <y>` — click (or --target to find first)."""
    screen = ScreenController(output_dir=args.out)
    input_ctl = InputController()
    if args.target:
        found = screen.find(" ".join(args.target))
        if not found.ok or not found.position:
            print(f"  {_RED}✗ {found.message}{_RESET}")
            return 1
        x, y = found.position
        what = f"click '{' '.join(args.target)}' at ({x}, {y})"
    else:
        x, y = args.x, args.y
        what = f"click at ({x}, {y})"
    if not _confirm(what, yes=args.yes):
        print(f"  {_DIM}· Declined.{_RESET}")
        return 2
    result = input_ctl.click(x, y, button=args.button)
    if result.ok:
        print(f"  {_GREEN}✓ {result.message}{_RESET}")
        return 0
    print(f"  {_RED}✗ {result.message}{_RESET}")
    return 1


def cmd_screen_type(args: argparse.Namespace) -> int:
    """`friday6 screen type <text>` — type into the focused window."""
    text = " ".join(args.text)
    if not _confirm(f"type '{text[:60]}'", yes=args.yes):
        print(f"  {_DIM}· Declined.{_RESET}")
        return 2
    result = InputController().type_text(text)
    if result.ok:
        print(f"  {_GREEN}✓ {result.message}{_RESET}")
        return 0
    print(f"  {_RED}✗ {result.message}{_RESET}")
    return 1


def cmd_screen_scroll(args: argparse.Namespace) -> int:
    """`friday6 screen scroll [up|down]` — scroll the focused window."""
    direction = args.direction
    if not _confirm(f"scroll {direction}", yes=args.yes):
        print(f"  {_DIM}· Declined.{_RESET}")
        return 2
    result = InputController().scroll(direction, args.amount)
    if result.ok:
        print(f"  {_GREEN}✓ {result.message}{_RESET}")
        return 0
    print(f"  {_RED}✗ {result.message}{_RESET}")
    return 1


def cmd_screen_key(args: argparse.Namespace) -> int:
    """`friday6 screen key <name>` — press enter / ctrl+c / …"""
    key = " ".join(args.key)
    if not _confirm(f"press {key}", yes=args.yes):
        print(f"  {_DIM}· Declined.{_RESET}")
        return 2
    result = InputController().press(key)
    if result.ok:
        print(f"  {_GREEN}✓ {result.message}{_RESET}")
        return 0
    print(f"  {_RED}✗ {result.message}{_RESET}")
    return 1


def cmd_screen_status(args: argparse.Namespace) -> int:
    """`friday6 screen status` — which screen tools exist (honest)."""
    screen = ScreenController(output_dir=getattr(args, "out", None))
    caps = screen.capabilities()
    _print_logo()
    labels = {
        "capture": "capture (grim / gnome-screenshot / import)",
        "ocr": "ocr (tesseract)",
        "mouse": "click (ydotool / xdotool)",
        "type": "type (wtype / xdotool)",
        "keys": "keys (wtype / xdotool)",
    }
    for key, label in labels.items():
        marker = "✓" if caps.get(key) else "○"
        print(f"  {_GREEN if caps.get(key) else _DIM}  {marker} "
              f"{label}{_RESET}")
    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def _build_screen_subcommands(screen_sub) -> None:
    """Register `friday6 screen <cmd>` subcommands on a subparsers object.

    Shared by both the integrated `friday6` CLI (via build_screen_parser)
    and the standalone `python -m friday_v6.cli_screen` entry point —
    same pattern as ``cli_desktop._build_desktop_subcommands``.
    """
    screen_sub.add_parser("status", help="Which screen tools exist").set_defaults(
        func=cmd_screen_status)

    pc = screen_sub.add_parser("capture", help="Screenshot → PNG path")
    pc.add_argument("-o", "--output", type=str, default=None,
                    help="Output PNG path (default ~/.friday/screen/)")
    pc.add_argument("--out", type=str, default=None,
                    help="Output directory (default ~/.friday/screen/)")
    pc.set_defaults(func=cmd_screen_capture)

    po = screen_sub.add_parser("ocr", help="Capture + read the text on screen")
    po.add_argument("image", nargs="?", default=None,
                    help="OCR an existing image instead of capturing")
    po.add_argument("--out", type=str, default=None,
                    help="Output directory (default ~/.friday/screen/)")
    po.set_defaults(func=cmd_screen_ocr)

    pf = screen_sub.add_parser("find", help="Locate text on screen → coordinates")
    pf.add_argument("target", nargs="+", help="e.g. 'login button'")
    pf.add_argument("image", nargs="?", default=None,
                    help="OCR an existing image instead of capturing")
    pf.add_argument("--out", type=str, default=None,
                    help="Output directory (default ~/.friday/screen/)")
    pf.set_defaults(func=cmd_screen_find)

    pc2 = screen_sub.add_parser("click", help="Click at coordinates (or --target)")
    pc2.add_argument("x", nargs="?", type=int, default=None)
    pc2.add_argument("y", nargs="?", type=int, default=None)
    pc2.add_argument("--target", nargs="+", default=None,
                     help="Find this on screen first, then click it")
    pc2.add_argument("--button", choices=["left", "right", "middle"],
                     default="left")
    pc2.add_argument("--yes", "-y", action="store_true",
                     help="Non-interactive approval (explicit override)")
    pc2.add_argument("--out", type=str, default=None,
                     help="Output directory (default ~/.friday/screen/)")
    pc2.set_defaults(func=cmd_screen_click)

    pt = screen_sub.add_parser("type", help="Type into the focused window")
    pt.add_argument("text", nargs="+")
    pt.add_argument("--yes", "-y", action="store_true",
                    help="Non-interactive approval (explicit override)")
    pt.set_defaults(func=cmd_screen_type)

    ps = screen_sub.add_parser("scroll", help="Scroll the focused window")
    ps.add_argument("direction", nargs="?", default="down",
                    choices=["up", "down"])
    ps.add_argument("--amount", type=int, default=3,
                    help="Scroll steps (default 3)")
    ps.add_argument("--yes", "-y", action="store_true",
                    help="Non-interactive approval (explicit override)")
    ps.set_defaults(func=cmd_screen_scroll)

    pk = screen_sub.add_parser("key", help="Press a key / shortcut")
    pk.add_argument("key", nargs="+", help="e.g. enter, ctrl+c, alt+tab")
    pk.add_argument("--yes", "-y", action="store_true",
                    help="Non-interactive approval (explicit override)")
    pk.set_defaults(func=cmd_screen_key)


def build_screen_parser(subparsers) -> None:
    """Build subparser for `friday6 screen` (used by the integrated CLI)."""
    p = subparsers.add_parser(
        "screen",
        help="See and touch the screen (capture / ocr / find / click / type)",
        description="Friday's eyes and hands: capture the screen, read it "
                    "with OCR, find on-screen text, click, type, scroll, and "
                    "press keys. The NL path is the product "
                    "(`friday6 \"click the login button\"`).",
    )
    screen_sub = p.add_subparsers(dest="screen_command")
    _build_screen_subcommands(screen_sub)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_screen`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 screen")
    subparsers = parser.add_subparsers(dest="screen_command")
    _build_screen_subcommands(subparsers)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
