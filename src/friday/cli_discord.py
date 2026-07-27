"""CLI commands for the Discord communication layer.

``friday discord config``           — show current Discord configuration status.
``friday discord guilds``           — list accessible guilds (servers).
``friday discord channels <guild>`` — list text channels in a guild.
``friday discord send <channel> <text>`` — post a message to a channel.
``friday discord setup``            — show setup instructions.
"""

from __future__ import annotations

import argparse
import sys

from .services.discord import DiscordConfig, list_guilds, list_channels_for_guild, post_message


def cmd_discord_config(args: argparse.Namespace) -> int:
    """Show current Discord configuration."""
    config = DiscordConfig.from_env()
    print(str(config))
    return 0


def cmd_discord_guilds(args: argparse.Namespace) -> int:
    """List accessible Discord guilds."""
    guilds = list_guilds(limit=10)
    if not guilds:
        print("No guilds found (or Discord not configured).")
        print("Run `friday discord config` to check configuration.")
        return 0

    print(f"Accessible guilds ({len(guilds)}):\n")
    for g in guilds:
        gid = g.get("id", "?")
        gname = g.get("name", "?")
        print(f"  {gname} ({gid})")
    return 0


def cmd_discord_channels(args: argparse.Namespace) -> int:
    """List text channels in a Discord guild."""
    guild_id = getattr(args, "guild_id", "") or ""
    if not guild_id:
        print("error: guild ID required: friday discord channels <guild_id>",
              file=sys.stderr)
        return 2

    channels = list_channels_for_guild(guild_id)
    if not channels:
        print(f"No text channels found in guild {guild_id} (or Discord not configured).")
        return 0

    print(f"Text channels in guild {guild_id}:\n")
    for ch in channels:
        cid = ch.get("id", "?")
        cname = ch.get("name", "?")
        print(f"  #{cname} ({cid})")
    return 0


def cmd_discord_send(args: argparse.Namespace) -> int:
    """Post a message to a Discord channel."""
    channel = getattr(args, "channel", "") or ""
    content = getattr(args, "content", "") or ""
    if isinstance(content, list):
        content = " ".join(content)

    if not channel:
        print("error: channel ID required: friday discord send <channel_id> <text>",
              file=sys.stderr)
        return 2

    if not content:
        print("error: message content required: friday discord send <channel_id> <text>",
              file=sys.stderr)
        return 2

    ok, err = post_message(channel, content)
    if ok:
        print(f"Message posted to channel {channel}")
        return 0
    else:
        print(f"Failed to post message: {err}", file=sys.stderr)
        return 1


def cmd_discord_setup(args: argparse.Namespace) -> int:
    """Show Discord setup instructions."""
    print("Discord Configuration")
    print("=====================\n")
    print("Friday uses the Discord REST API to read and write messages.\n")
    print("Setup steps:")
    print("  1. Go to https://discord.com/developers/applications and create a new app\n")
    print("  2. Go to 'Bot' section and click 'Add Bot'\n")
    print("  3. Under 'Privileged Gateway Intents', enable:")
    print("     - MESSAGE CONTENT INTENT (required to read message content)\n")
    print("  4. Copy the bot token (click 'Reset Token' if needed)\n")
    print("  5. Invite the bot to your server using the OAuth2 URL Generator:")
    print("     - Scopes: bot")
    print("     - Bot Permissions: Send Messages, Read Message History, View Channels\n")
    print("  6. Add to your .env file:\n")
    print("     FRIDAY_DISCORD_BOT_TOKEN=your-bot-token-here\n")
    print("After setting up, run:")
    print("  friday discord config       # verify configuration")
    print("  friday discord guilds       # test listing servers")
    print("  friday discord send <channel_id> 'Hello from Friday!'  # test sending")
    return 0


def cmd_discord(args: argparse.Namespace) -> int:
    """Dispatch friday discord subcommands."""
    action = getattr(args, "action", None)

    if action == "config":
        return cmd_discord_config(args)
    elif action == "guilds":
        return cmd_discord_guilds(args)
    elif action == "channels":
        return cmd_discord_channels(args)
    elif action == "send":
        return cmd_discord_send(args)
    elif action == "setup":
        return cmd_discord_setup(args)
    else:
        print("Unknown discord subcommand.", file=sys.stderr)
        print("Usage: friday discord <config|guilds|channels|send|setup>", file=sys.stderr)
        return 2
