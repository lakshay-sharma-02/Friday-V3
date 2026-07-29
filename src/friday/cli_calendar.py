"""Calendar CLI — configure and authenticate Google Calendar OAuth.

``friday calendar auth``     Interactive Google Calendar OAuth setup
``friday calendar status``   Show which calendar provider is active
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".friday"
CREDENTIALS_PATH = CREDENTIALS_DIR / "google_credentials.json"
TOKEN_PATH = CREDENTIALS_DIR / "google_calendar_token.json"


def cmd_calendar(args: argparse.Namespace) -> int:
    """Dispatch calendar subcommands."""
    from .presentation.cli_format import header, green, yellow, red, gray

    action = getattr(args, "action", "status")

    if action == "status":
        return _show_status()

    if action == "auth":
        return _run_auth()

    print(f"Unknown calendar action: {action}")
    print(gray("  Usage: friday calendar auth | friday calendar status"))
    return 1


def _show_status() -> int:
    """Show which calendar provider is configured."""
    from .presentation.cli_format import header, green, yellow, red, gray
    from .observation.calendar_observer import (
        _configured_google_provider,
        _configured_caldav_provider,
        _configured_ics,
    )

    print(header("Calendar Status", "provider check"))
    print()

    google = _configured_google_provider()
    caldav = _configured_caldav_provider()
    ics = _configured_ics()

    if google:
        print(green(f"  ✓ {google.describe()}"))
    else:
        print(gray("  ○ Google Calendar — not configured"))
        print(gray("    Run: friday calendar auth"))

    if caldav:
        print(green(f"  ✓ {caldav.describe()}"))

    if ics:
        print(green(f"  ✓ ICS file: {ics}"))

    if not google and not caldav and not ics:
        print(yellow("  No calendar provider configured."))
        print()
        print(gray("  Options (easiest first):"))
        print(gray("    1. friday calendar auth  — Google Calendar OAuth"))
        print(gray("    2. export FRIDAY_CALENDAR_ICS=~/path/to/calendar.ics"))
        print(gray("    3. export FRIDAY_CALDAV_URL=... (Fastmail, iCloud, etc.)"))
        print(gray("    4. export FRIDAY_GOOGLE_CAL_API_KEY=... (public calendars only)"))

    return 0


def _run_auth() -> int:
    """Interactive Google Calendar OAuth setup."""
    from .presentation.cli_format import header, green, yellow, red, gray

    print(header("Calendar Auth", "Google Calendar OAuth"))
    print()

    # Step 1: Check if credentials file already exists.
    if CREDENTIALS_PATH.exists():
        print(green(f"  ✓ Credentials found at {CREDENTIALS_PATH}"))
        return _do_oauth_flow()

    # Step 2: Guide the user.
    print(yellow("  No Google OAuth credentials found."))
    print()
    print("  You need to paste your OAuth client secrets from Google Cloud Console.")
    print()
    print(gray("  ── Quick steps ─────────────────────────────────────────────"))
    print(gray("  1. Go to: https://console.cloud.google.com/"))
    print(gray("  2. Select your project"))
    print(gray('  3. APIs & Services → Credentials'))
    print(gray("  4. Find your OAuth 2.0 Client ID"))
    print(gray("  5. Click the download button (⬇) on that row"))
    print(gray("  6. A file named client_secret_XXXXX.json will download"))
    print(gray("  7. Copy its contents and paste below"))
    print(gray("  ────────────────────────────────────────────────────────────"))
    print()
    print(gray("  (Or just copy the client_secret string — I already have your client_id)"))
    print()

    # Step 3: Accept paste input.
    try:
        print("  Paste the JSON (or just the client_secret) and press Ctrl+D:")
        print()
        lines = sys.stdin.read()
    except (EOFError, KeyboardInterrupt):
        print()
        print(yellow("  Cancelled."))
        return 1

    raw = lines.strip()
    if not raw:
        print(red("  Nothing provided."))
        return 1

    # Step 4: Parse what the user pasted.
    client_id = "144822803968-2ch2e5i6joc3uj362u99ci4kg84u8b7t.apps.googleusercontent.com"
    client_secret = None

    # Try parsing as JSON (full client_secret file).
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            installed = data.get("installed", data)
            client_id = installed.get("client_id", client_id)
            client_secret = installed.get("client_secret")
    except (ValueError, TypeError):
        # Not JSON — treat as raw client_secret string.
        client_secret = raw.strip()

    if not client_secret:
        print(red("  Could not extract client_secret from what you pasted."))
        print(gray("  Paste the full JSON from the downloaded client_secret_XXXXX.json file."))
        return 1

    # Step 5: Write credentials file.
    credentials = {
        "installed": {
            "client_id": client_id,
            "project_id": "",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
        }
    }

    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    print(green(f"  ✓ Credentials saved to {CREDENTIALS_PATH}"))

    # Step 6: Run the OAuth flow.
    return _do_oauth_flow()


def _do_oauth_flow() -> int:
    """Run the Google OAuth local server flow to get a token."""
    from .presentation.cli_format import header, green, yellow, red, gray

    print()
    print(header("OAuth Flow", "authorize in browser"))
    print()
    print(gray("  Opening your browser to sign in to Google..."))
    print()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(red("  Missing dependency."))
        print()
        print("  Install it:")
        print(gray("    pip install google-auth-oauthlib google-api-python-client"))
        print()
        print(gray("  Then run again: friday calendar auth"))
        return 1

    SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as exc:
        print(red(f"  OAuth flow failed: {exc}"))
        print()
        print(gray("  Troubleshooting:"))
        print(gray("  - Make sure your browser opened and you clicked Allow"))
        print(gray("  - Check the redirect URI matches http://localhost"))
        print(gray("  - Re-download credentials from Cloud Console if needed"))
        return 1

    import time as _time

    token = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "expires_at": creds.expiry.timestamp() if creds.expiry else 0,
    }

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")

    print(green(f"  ✅ Token saved to {TOKEN_PATH}"))
    print()
    print(yellow("  ── Add this to your shell config ──"))
    print(f'  export FRIDAY_GOOGLE_CAL_TOKEN="{TOKEN_PATH}"')
    print(yellow("  ──────────────────────────────────"))
    print()
    print(gray("  Or run this now to test:"))
    print(gray(f'    export FRIDAY_GOOGLE_CAL_TOKEN="{TOKEN_PATH}"'))
    print(gray("    friday observers  # should show calendar as healthy"))

    return 0
