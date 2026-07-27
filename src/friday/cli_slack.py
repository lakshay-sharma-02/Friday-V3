"""CLI commands for the Slack communication layer.

``friday slack config``          — show current Slack configuration status.
``friday slack channels``        — list accessible channels.
``friday slack send <channel> <text>`` — post a message to a channel.
``friday slack setup``           — show setup instructions.
"""

from __future__ import annotations

import argparse
import sys

from .services.slack import SlackConfig, list_channels, post_message


def cmd_slack_config(args: argparse.Namespace) -> int:
    """Show current Slack configuration."""
    config = SlackConfig.from_env()
    print(str(config))
    return 0


def cmd_slack_channels(args: argparse.Namespace) -> int:
    """List accessible Slack channels."""
    limit = getattr(args, "limit", 20) or 20
    channels = list_channels(limit=limit)
    if not channels:
        print("No channels found (or Slack not configured).")
        print("Run `friday slack config` to check configuration.")
        return 0

    print(f"Accessible channels ({len(channels)}):\n")
    for ch in channels:
        name = ch.get("name", "?")
        cid = ch.get("id", "?")
        members = ch.get("num_members", "?")
        topic = (ch.get("topic", {}) or {}).get("value", "") or ""
        topic_str = f" — {topic[:60]}" if topic else ""
        print(f"  #{name} ({cid}, {members} members){topic_str}")
    return 0


def cmd_slack_send(args: argparse.Namespace) -> int:
    """Post a message to a Slack channel."""
    channel = getattr(args, "channel", "") or ""
    text = getattr(args, "text", "") or ""
    if isinstance(text, list):
        text = " ".join(text)

    if not channel:
        print("error: channel required: friday slack send <channel> <text>",
              file=sys.stderr)
        return 2

    if not text:
        print("error: message text required: friday slack send <channel> <text>",
              file=sys.stderr)
        return 2

    ok, err = post_message(channel, text)
    if ok:
        print(f"Message posted to #{channel}")
        return 0
    else:
        print(f"Failed to post message: {err}", file=sys.stderr)
        return 1


def cmd_slack_setup(args: argparse.Namespace) -> int:
    """Show Slack setup instructions."""
    print("Slack Configuration")
    print("===================\n")
    print("Friday uses the Slack Web API to read and write messages.\n")
    print("Setup steps:")
    print("  1. Go to https://api.slack.com/apps and create a new app\n")
    print("  2. Under 'OAuth & Permissions', add these Bot Token Scopes:")
    print("     - channels:history  (read messages)")
    print("     - channels:read     (list channels)")
    print("     - chat:write        (send messages)")
    print("     - groups:read       (private channels)")
    print("     - groups:history    (private channel messages)")
    print("     - team:read         (basic team info)\n")
    print("  3. Install the app to your workspace")
    print("  4. Copy the Bot User OAuth Token (starts with xoxb-)\n")
    print("  5. Add to your .env file:\n")
    print("     FRIDAY_SLACK_BOT_TOKEN=xoxb-your-token-here\n")
    print("  Optional (for real-time events):")
    print("     FRIDAY_SLACK_APP_TOKEN=xapp-your-app-token\n")
    print("After setting up, run:")
    print("  friday slack config      # verify configuration")
    print("  friday slack channels    # test reading channels")
    print("  friday slack send #general 'Hello from Friday!'  # test sending")
    return 0


def cmd_slack(args: argparse.Namespace) -> int:
    """Dispatch friday slack subcommands."""
    action = getattr(args, "action", None)

    if action == "config":
        return cmd_slack_config(args)
    elif action == "channels":
        return cmd_slack_channels(args)
    elif action == "send":
        return cmd_slack_send(args)
    elif action == "setup":
        return cmd_slack_setup(args)
    else:
        print("Unknown slack subcommand.", file=sys.stderr)
        print("Usage: friday slack <config|channels|send|setup>", file=sys.stderr)
        return 2
