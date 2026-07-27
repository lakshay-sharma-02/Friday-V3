"""CLI commands for Friday Identity.

``friday identity``               — show identity status (active channels, context).
``friday identity chat``           — interactive chat session with Friday.
``friday identity telegram``       — enable/disable Telegram listener for identity chat.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .db import connect
from .persona import IdentityEngine


def cmd_identity_status(args: argparse.Namespace) -> int:
    """Show identity status."""
    engine = IdentityEngine()
    channel_count = len(engine.contexts)
    exchanges = sum(len(c.exchanges) for c in engine.contexts.values())

    print("Friday Identity")
    print("===============\n")
    print(f"Name: {engine.config.name}")
    print(f"Active conversations: {channel_count}")
    print(f"Total exchanges: {exchanges}")
    print()
    if engine.contexts:
        print("Active channels:")
        for cid, ctx in engine.contexts.items():
            print(f"  {cid} — {len(ctx.exchanges)} exchange(s)")
    else:
        print("No active conversations. Start one with:")
        print("  friday identity chat")
        print("  or message Friday on Telegram/Slack/Discord")
    return 0


def cmd_identity_chat(args: argparse.Namespace) -> int:
    """Interactive chat session with Friday."""
    engine = IdentityEngine()
    print(f"\n=== {engine.config.name} ===")
    print(engine.config.greeting)
    print("(type 'exit' to quit)\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Friday> Later! I'll be here.\n")
            break

        reply = engine.process(user_input, channel_id="cli")
        print(f"Friday> {reply}")
        print()

    return 0


def cmd_identity_telegram(args: argparse.Namespace) -> int:
    """Start or stop the Telegram polling loop for identity chat."""
    action = getattr(args, "action", "start")
    if action == "start":
        return _start_telegram_listener()
    elif action == "stop":
        return _stop_telegram_listener()
    else:
        print("Usage: friday identity telegram <start|stop>", file=sys.stderr)
        return 2


def _start_telegram_listener() -> int:
    """Start a one-shot poll for Telegram messages and respond to them."""
    try:
        from .services.telegram import TelegramConfig, _get_updates, _send_message
    except ImportError:
        print("Telegram module not available.", file=sys.stderr)
        return 1

    config = TelegramConfig.from_env()
    if not config.configured:
        print("Telegram not configured. Set FRIDAY_TELEGRAM_BOT_TOKEN in .env.", file=sys.stderr)
        return 1

    engine = IdentityEngine()
    # Use a simple offset tracker to avoid re-processing old messages.
    offset_file = Path("/tmp/friday_telegram_offset.txt")
    offset: Optional[int] = None
    try:
        raw = offset_file.read_text().strip()
        if raw:
            offset = int(raw)
    except (OSError, ValueError):
        pass

    updates = _get_updates(config, limit=10, timeout=5, offset=offset)
    if not updates:
        print("No new Telegram messages.")
        return 0

    # Track the highest update_id for next poll.
    max_id = max(u["update_id"] for u in updates if u.get("update_id"))
    try:
        offset_file.write_text(str(max_id + 1))
    except OSError:
        pass

    responded = 0
    seen_chat_ids: list[str] = []
    for u in updates:
        chat_id = u.get("chat_id")
        if chat_id and chat_id not in seen_chat_ids:
            seen_chat_ids.append(chat_id)
        text = u.get("text", "")
        if not text or not chat_id:
            continue
        channel_id = f"telegram:{chat_id}"
        reply = engine.process(text, channel_id=channel_id)
        if reply:
            _send_message(config, str(chat_id), reply)
            responded += 1

    print(f"Processed {responded} message(s) from {len(seen_chat_ids)} chat(s).")
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    """Dispatch friday identity subcommands."""
    action = getattr(args, "action", None)

    if action == "chat":
        return cmd_identity_chat(args)
    elif action == "telegram":
        return cmd_identity_telegram(args)
    else:
        return cmd_identity_status(args)
