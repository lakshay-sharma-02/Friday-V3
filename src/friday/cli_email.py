"""CLI commands for the email communication layer.

``friday email config``         — show current email configuration status.
``friday email inbox [--limit]`` — list recent inbox emails.
``friday email send <to> <subject>`` — send an email (body from stdin or prompt).
``friday email setup``           — show instructions for configuring email.
"""

from __future__ import annotations

import argparse
import shlex
import sys

from .services.email import (
    EmailConfig,
    list_recent_emails,
    send_email,
)


def cmd_email_config(args: argparse.Namespace) -> int:
    """Show current email configuration."""
    config = EmailConfig.from_env()
    print(str(config))
    return 0


def cmd_email_inbox(args: argparse.Namespace) -> int:
    """List recent inbox emails."""
    limit = getattr(args, "limit", 20) or 20
    emails = list_recent_emails(limit=limit)
    if not emails:
        print("No emails found (or email not configured).")
        print("Run `friday email config` to check configuration.")
        print("Run `friday email setup` for setup instructions.")
        return 0

    print(f"Recent emails (last {len(emails)}):\n")
    for e in emails:
        unread = "●" if e.get("unread") else " "
        subj = (e.get("subject") or "?").strip()[:60]
        from_ = (e.get("from") or "?").strip()[:40]
        date = (e.get("date") or "").strip()[:19]
        snippet = (e.get("snippet") or "").strip()[:80]
        print(f"  [{unread}] {subj}")
        print(f"         from {from_}  {date}")
        if snippet:
            print(f"         {snippet}")
        print()
    return 0


def cmd_email_send(args: argparse.Namespace) -> int:
    """Send an email."""
    to = getattr(args, "to", "") or ""
    subject = getattr(args, "subject", "") or ""

    if not to:
        print("error: recipient required: friday email send <to> <subject>",
              file=sys.stderr)
        return 2

    if not subject:
        print("error: subject required: friday email send <to> <subject>",
              file=sys.stderr)
        return 2

    # Read body from stdin if piped, otherwise prompt.
    if not sys.stdin.isatty():
        body = sys.stdin.read().strip()
    else:
        print("Body (Ctrl+D to send, Ctrl+C to cancel):")
        try:
            body_lines = []
            while True:
                try:
                    line = input()
                    body_lines.append(line)
                except EOFError:
                    break
            body = "\n".join(body_lines)
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 1

    if not body:
        body = "(no body)"

    ok, err = send_email(to, subject, body)
    if ok:
        print(f"Email sent to {to}: {subject}")
        return 0
    else:
        print(f"Failed to send email: {err}", file=sys.stderr)
        return 1


def cmd_email_setup(args: argparse.Namespace) -> int:
    """Show email setup instructions."""
    print("Email Configuration")
    print("==================\n")
    print("Friday uses IMAP (read) and SMTP (send) to work with your email.\n")
    print("For Gmail (recommended):")
    print("  1. Enable 2-Factor Authentication at:")
    print("     https://myaccount.google.com/security\n")
    print("  2. Create an App Password at:")
    print("     https://myaccount.google.com/apppasswords\n")
    print("  3. Add to your .env file:\n")
    print("     FRIDAY_EMAIL_USERNAME=your.email@gmail.com")
    print("     FRIDAY_EMAIL_PASSWORD=your-16-char-app-password\n")
    print("  Optional overrides:")
    print("     FRIDAY_EMAIL_IMAP_SERVER=imap.gmail.com  (default)")
    print("     FRIDAY_EMAIL_IMAP_PORT=993               (default)")
    print("     FRIDAY_EMAIL_SMTP_SERVER=smtp.gmail.com  (default)")
    print("     FRIDAY_EMAIL_SMTP_PORT=587               (default)")
    print("     FRIDAY_EMAIL_FROM=Friday Bot             (display name)\n")
    print("For other providers:")
    print("  Set the IMAP/SMTP server and port for your provider.")
    print("  Use your regular email password or an app-specific password.\n")
    print("After setting up, run:")
    print("  friday email config      # verify configuration")
    print("  friday email inbox       # test reading email")
    print("  friday email send <to> <subject>   # test sending")
    return 0


def cmd_email(args: argparse.Namespace) -> int:
    """Dispatch friday email subcommands."""
    action = getattr(args, "action", None)

    if action == "config":
        return cmd_email_config(args)
    elif action == "inbox":
        return cmd_email_inbox(args)
    elif action == "send":
        return cmd_email_send(args)
    elif action == "setup":
        return cmd_email_setup(args)
    else:
        print("Unknown email subcommand.", file=sys.stderr)
        print("Usage: friday email <config|inbox|send|setup>", file=sys.stderr)
        return 2
