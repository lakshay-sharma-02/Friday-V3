"""CLI commands for the Telegram communication layer.

``friday telegram config``           — show current Telegram configuration.
``friday telegram me``               — show bot info.
``friday telegram send <chat_id> <text>`` — send a message.
``friday telegram setup``            — show setup instructions.
"""

from __future__ import annotations

import argparse
import sys

from .services.telegram import TelegramConfig, get_bot_info, send_message


def cmd_telegram_config(args: argparse.Namespace) -> int:
    """Show current Telegram configuration."""
    config = TelegramConfig.from_env()
    print(str(config))
    if config.configured:
        info = get_bot_info()
        if info:
            print(f"  Bot username: @{info.get('username', '?')}")
            print(f"  Bot name: {info.get('first_name', '?')}")
    return 0


def cmd_telegram_me(args: argparse.Namespace) -> int:
    """Show bot info."""
    config = TelegramConfig.from_env()
    if not config.configured:
        print("Telegram not configured. Set FRIDAY_TELEGRAM_BOT_TOKEN in .env.")
        return 0
    info = get_bot_info()
    if info:
        print(f"Bot info:")
        print(f"  Username: @{info.get('username', '?')}")
        print(f"  Name: {info.get('first_name', '?')}")
        print(f"  ID: {info.get('id', '?')}")
    else:
        print("Could not fetch bot info. Check your token.")
    return 0


def cmd_telegram_send(args: argparse.Namespace) -> int:
    """Send a message to a Telegram chat."""
    chat_id = getattr(args, "chat_id", "") or ""
    text = getattr(args, "text", "") or ""
    if isinstance(text, list):
        text = " ".join(text)

    if not chat_id:
        print("error: chat_id required: friday telegram send <chat_id> <text>",
              file=sys.stderr)
        return 2

    if not text:
        print("error: message text required: friday telegram send <chat_id> <text>",
              file=sys.stderr)
        return 2

    ok, err = send_message(chat_id, text)
    if ok:
        print(f"Message sent to chat {chat_id}")
        return 0
    else:
        print(f"Failed to send message: {err}", file=sys.stderr)
        return 1


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    """Show Telegram setup instructions."""
    print("Telegram Configuration")
    print("======================\n")
    print("Friday uses the Telegram Bot API to send and receive messages.\n")
    print("Setup steps:")
    print("  1. Open Telegram and search for @BotFather\n")
    print("  2. Send /newbot and follow the prompts")
    print("  3. Copy the bot token (looks like: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11)\n")
    print("  4. Add to your .env file:\n")
    print("     FRIDAY_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11\n")
    print("  5. Start a chat with your bot and send /start\n")
    print("  6. To get your chat ID, send a message to the bot and check:")
    print("     friday telegram me\n")
    print("After setting up, run:")
    print("  friday telegram config          # verify configuration")
    print("  friday telegram me              # check bot info")
    print("  friday telegram send <chat_id> 'Hello from Friday!'  # test sending")
    print("\nTip: Telegram is ideal for Friday to send you notifications,")
    print("such as skill degradation alerts, execution summaries, or")
    print("autonomy escalation events — right to your phone.")
    return 0


def cmd_telegram(args: argparse.Namespace) -> int:
    """Dispatch friday telegram subcommands."""
    action = getattr(args, "action", None)

    if action == "config":
        return cmd_telegram_config(args)
    elif action == "me":
        return cmd_telegram_me(args)
    elif action == "send":
        return cmd_telegram_send(args)
    elif action == "setup":
        return cmd_telegram_setup(args)
    else:
        print("Unknown telegram subcommand.", file=sys.stderr)
        print("Usage: friday telegram <config|me|send|setup>", file=sys.stderr)
        return 2
