"""CLI for Translation — ``friday translate``.

Usage::

    friday translate \"hello world\" --to es
    friday translate \"hello world\" --from en --to fr
    friday detect \"hola mundo\"
"""

from __future__ import annotations

import argparse
from .presentation.cli_format import header, green, gray


def cmd_translate(args: argparse.Namespace) -> int:
    """Translate text between languages."""
    from .db import connect
    from .translate import translate, SUPPORTED_LANGUAGES

    text: str = args.text
    source: str = getattr(args, "from_lang", "en") or "en"
    target: str = getattr(args, "to", "es") or "es"

    if not text.strip():
        print("  error: text to translate is required")
        return 1

    if source not in SUPPORTED_LANGUAGES:
        print(f"  Unsupported source language: {source}")
        return 1
    if target not in SUPPORTED_LANGUAGES:
        print(f"  Unsupported target language: {target}")
        return 1

    conn = connect()
    try:
        result = translate(text, source=source, target=target, conn=conn)
    finally:
        conn.close()

    if result != text or source == target:
        print(f"  {green('[Translate]')} {SUPPORTED_LANGUAGES.get(source, source)} → {SUPPORTED_LANGUAGES.get(target, target)}")
        print(f"  {result}")
    else:
        print(f"  {gray('[No translation available]')}")
        print(f"  {text}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """Detect the language of a text string."""
    from .translate import detect_language, SUPPORTED_LANGUAGES

    text: str = args.text
    if not text.strip():
        print("  error: text to detect is required")
        return 1

    lang = detect_language(text)
    name = SUPPORTED_LANGUAGES.get(lang, lang)
    print(f"  Detected language: {name} ({lang})")
    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``translate`` and ``detect`` subcommand parsers."""
    pt = sub.add_parser("translate", help="Translate text between languages.")
    pt.add_argument("text", help="Text to translate.")
    pt.add_argument("--from", "-f", dest="from_lang", default="en",
                    help="Source language code (default: en).")
    pt.add_argument("--to", "-t", default="es",
                    help="Target language code (default: es).")
    pt.set_defaults(func=cmd_translate)

    pd = sub.add_parser("detect", help="Detect the language of text.")
    pd.add_argument("text", help="Text to analyze.")
    pd.set_defaults(func=cmd_detect)
