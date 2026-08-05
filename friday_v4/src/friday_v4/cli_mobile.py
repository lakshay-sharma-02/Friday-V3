"""CLI commands for `friday4 mobile` — companion app + transport (Wave 15/7).

Usage:
    friday4 mobile serve [--host 127.0.0.1] [--port 8900] [--db PATH]
    friday4 mobile push  [--once] [--db PATH]    # drain the durable queue

The phone is another surface of the same Friday: ``serve`` runs the
pure-stdlib companion server — the installable PWA at ``/`` plus the
API (status / conversation / talk / SSE events / pairing) — and
``push`` delivers queued ambient events to the configured transporter
(log by default — paired devices' Expo tokens by default once the
operator pairs a phone).

Design laws: never crash (missing DB renders neutral output), local
by default, pure stdlib.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess

logger = logging.getLogger("friday_v4.cli_mobile")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"


def _print_logo(title: str):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def cmd_mobile_pair(args: argparse.Namespace) -> int:
    """Generate a one-time pairing code for the companion app (Wave 7).

    The operator types this code into the phone app alongside the
    device's push token; the server verifies it (10-minute TTL, one
    use) and binds the phone to this Friday. Print it, don't send it
    anywhere.
    """
    from .mobile import PairingService

    _print_logo("Mobile Pairing")
    service = PairingService(db_path=args.db)
    code = service.generate()
    print(f"  Your one-time pairing code:  {_GREEN}{_BOLD}{code}{_RESET}")
    print(f"  {_DIM}Valid for 10 minutes, single use. Enter it in the")
    print(f"  Friday app under 'Pair this device'.{_RESET}\n")
    print(f"  {_DIM}Then see what's paired: `friday4 mobile devices`{_RESET}\n")
    return 0


def cmd_mobile_devices(args: argparse.Namespace) -> int:
    """List every phone paired to this Friday (Wave 7)."""
    from .mobile import PairingService

    _print_logo("Mobile Devices")
    service = PairingService(db_path=args.db)
    devices = service.devices()
    if not devices:
        print(f"  {_DIM}No phones paired yet — `friday4 mobile pair` to")
        print(f"  generate a code, then pair from the app.{_RESET}\n")
        return 0
    for d in devices:
        name = d.get("name") or d.get("platform") or "phone"
        print(f"  {_GREEN}✔{_RESET} {name:<16} {_DIM}"
              f"{d.get('platform')} · paired {d.get('created_at', '')[:16]}"
              f" · seen {d.get('last_seen', '')[:16]} · "
              f"{str(d.get('id'))[:8]}{_RESET}")
    print(f"\n  {_DIM}Unpair: `friday4 mobile unpair <device-id>`{_RESET}\n")
    return 0


def cmd_mobile_unpair(args: argparse.Namespace) -> int:
    """Remove a paired phone by device id (Wave 7)."""
    from .mobile import PairingService

    _print_logo("Mobile Unpair")
    service = PairingService(db_path=args.db)
    if service.remove(args.device_id):
        print(f"  {_GREEN}✔{_RESET} Device {args.device_id} removed — it will "
              f"no longer receive pushes.\n")
        return 0
    print(f"  {_DIM}No device with id {args.device_id} (see "
          f"`friday4 mobile devices`).{_RESET}\n")
    return 1


def _token_from_args(args) -> str:
    """--token flag, else the FRIDAY_V4_MOBILE_TOKEN env (empty = open)."""
    import os
    token = (args.token or "").strip() or os.environ.get(
        "FRIDAY_V4_MOBILE_TOKEN", "").strip()
    return token


def _normalize_bind(host: str, port: int) -> tuple:
    """Parse a ``--host`` value that may be a bare address, ``host:port``,
    or a full URL — so pasting what ``friday4 mobile remote`` prints
    just works instead of dying with ``Name or service not known``.

        "0.0.0.0"                    → ("0.0.0.0", port)
        "100.64.0.5:8900"            → ("100.64.0.5", 8900)
        "http://100.64.0.5:8900/"    → ("100.64.0.5", 8900)
        "[::1]:8900"                 → ("::1", 8900)
    """
    h = (host or "").strip().strip("/")
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0]
    # IPv6 literal [::1]:8900 (bracketed — the URL-correct form).
    if h.startswith("["):
        end = h.find("]")
        if end >= 0:
            addr = h[1:end]
            rest = h[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                return addr, int(rest[1:])
            return addr, port
    # Bare unbracketed IPv6 (``fe80::1``) is invalid as a URL host but
    # would mis-split here — leave it untouched rather than guess.
    if h.count(":") > 1 and not h.startswith("["):
        return h, port
    if ":" in h:
        addr, _, maybe = h.rpartition(":")
        if maybe.isdigit():
            return addr or "0.0.0.0", int(maybe)
    return h or "0.0.0.0", port


def _spawn_cloudflare_tunnel(port: int):
    """Spawn ``cloudflared tunnel --url`` beside the server (Wave 22).

    Returns ``(proc, reader_thread)`` on success, ``(None, None)`` when
    cloudflared isn't installed (the caller prints the neutral hint —
    the never-crash law: exposing is an enhancement, not a gate).
    """
    import threading
    if not shutil.which("cloudflared"):
        return None, None
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    def _read():
        import re
        found = False
        try:
            for line in proc.stdout or ():
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com",
                              line)
                if m and not found:
                    found = True
                    print(f"  {_GREEN}◈{_RESET} Public URL (anywhere, free):"
                          f" {_CYAN}{m.group(0)}{_RESET}", flush=True)
                    print(f"  {_DIM}  Enter it in the phone app's Status tab —"
                          f" set a token (`--token`) to gate it.{_RESET}",
                          flush=True)
                # Keep draining cloudflared's merged output after the URL
                # is found — a full pipe would block the child's writes.
        except Exception:
            pass

    thread = threading.Thread(target=_read, daemon=True,
                              name="cloudflare-url")
    thread.start()
    return proc, thread


def cmd_mobile_serve(args: argparse.Namespace) -> int:
    """Start the companion server: the PWA + the API (blocks until Ctrl+C)."""
    from .mobile import create_api_server

    token = _token_from_args(args)
    host, port = _normalize_bind(args.host, args.port)
    _print_logo("Mobile Companion")
    try:
        server = create_api_server(host=host, port=port,
                                   db_path=args.db, token=token or None)
    except OSError as exc:
        print(f"  {_RED}✘ Could not bind {host}:{port} — {exc}{_RESET}")
        print(f"  {_DIM}  --host takes an address (0.0.0.0, 100.64.x.x) —")
        print(f"  {_DIM}  or host:port, or a full http://… URL.{_RESET}")
        print(f"  {_DIM}  Try: friday4 mobile serve --host 0.0.0.0{_RESET}")
        return 1
    port = server.server_address[1]
    # flush=True: the URLs must be visible immediately even when stdout
    # is redirected (`friday4 mobile serve | tee log`), not just on a TTY
    # — the same lesson as the web dashboard's serve.
    print(f"  {_DIM}Phone app:{_RESET}     {_CYAN}http://{host}:{port}/{_RESET}", flush=True)
    print(f"  {_DIM}Companion API:{_RESET} {_CYAN}http://{host}:{port}/api/status{_RESET}", flush=True)
    tailscale_url = ""
    tunnel_proc = None
    if host in ("127.0.0.1", "localhost"):
        print(f"  {_DIM}On your phone, use this machine's LAN IP and restart", flush=True)
        print(f"  {_DIM}with `friday4 mobile serve --host 0.0.0.0`{_RESET}", flush=True)
    else:
        # Bound to 0.0.0.0 — show the anywhere URLs (Tailscale + tunnel).
        ts = _tailscale_ip()
        if ts:
            tailscale_url = f"http://{ts}:{port}"
            print(f"  {_GREEN}◈{_RESET} Anywhere (Tailscale):"
                  f" {_CYAN}{tailscale_url}{_RESET}", flush=True)
        if getattr(args, "tunnel", None) == "cloudflare":
            tunnel_proc, _t = _spawn_cloudflare_tunnel(port)
            if tunnel_proc is None:
                print(f"  {_YELLOW}◐{_RESET} `cloudflared` not installed — install"
                      f" it or use Tailscale for anywhere access.", flush=True)
    if token:
        print(f"  {_YELLOW}◐{_RESET} API token set — the phone app and PWA need it", flush=True)
        print(f"  {_DIM}  on their Status tab before the API answers.{_RESET}", flush=True)
    print(f"  {_DIM}Then: `friday4 mobile pair` → enter the code in the app's", flush=True)
    print(f"  {_DIM}Device tab → 'Add to Home Screen' for an app icon.{_RESET}", flush=True)
    print(f"  {_DIM}Reachable from anywhere? `friday4 mobile remote`{_RESET}", flush=True)
    print(f"  {_DIM}Press Ctrl+C to stop.{_RESET}\n", flush=True)

    # System tray (Wave 22 — like 9router: one icon, always present).
    tray = None
    if getattr(args, "tray", False):
        try:
            from .mobile.tray import MobileTray
            tray = MobileTray(base_url=f"http://{host}:{port}",
                              db_path=args.db,
                              on_stop=lambda: server.shutdown())
            if tray.start():
                print(f"  {_GREEN}◈{_RESET} System tray icon active — right-click"
                      f" for dashboard / URLs / pair / stop.", flush=True)
            else:
                tray = None
        except Exception as exc:
            logger.debug(f"mobile tray failed: {exc}")
            tray = None
    if tray is not None:
        tray.update_urls(tailscale_url=tailscale_url or None)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if tunnel_proc is not None:
            try:
                tunnel_proc.terminate()
            except Exception:
                pass
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
    print(f"  {_GREEN}✔ Companion server stopped.{_RESET}")
    return 0


def _lan_ips() -> list:
    """This machine's LAN IPv4 addresses (best-effort, stdlib only)."""
    import socket
    ips: list = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and "." in ip:
                ips.append(ip)
    except Exception:
        pass
    # The UDP-connect trick also finds the primary outbound interface IP.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip not in ips:
            ips.append(ip)
    except Exception:
        pass
    return sorted(set(ips))


def _tailscale_ip() -> str:
    """The 100.x Tailscale IP if the tailscale binary is present ('' else)."""
    if not shutil.which("tailscale"):
        return ""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"],
                             capture_output=True, text=True, timeout=5)
        ip = (out.stdout or "").strip().splitlines()
        return ip[0] if ip else ""
    except Exception:
        return ""


def cmd_mobile_remote(args: argparse.Namespace) -> int:
    """Print every way to reach Friday from your phone — LAN, Tailscale,
    and a free public tunnel — plus the exact URL to enter in the app.

    This is the "use Friday from anywhere" answer: the companion server
    always runs on THIS PC (that's where the power lives — desktop
    control, executors, the bridge). ``remote`` tells you which address
    to type into the phone app's Status tab for each reach path:

      - Same Wi-Fi  → the PC's LAN IP (default, zero setup)
      - Anywhere    → Tailscale (free, encrypted, no port-forwarding)
                      if the ``tailscale`` binary is installed here
      - Anywhere    → a free Cloudflare quick tunnel (no account):
                      cloudflared tunnel --url http://127.0.0.1:8900

    Security: for any path that leaves the LAN, run the server with a
    token first (``--token`` or FRIDAY_V4_MOBILE_TOKEN) — otherwise
    anyone with the URL can drive this PC. The PWA shell itself is
    public; the API (the power) is what the token gates.
    """
    _print_logo("Mobile — Reach Friday")
    token = _token_from_args(args)
    lan = _lan_ips()
    ts = _tailscale_ip()
    if not lan and not ts:
        print(f"  {_RED}✘ Couldn't detect any reachable address.{_RESET}")
        print(f"  {_DIM}  Start the server: `friday4 mobile serve --host 0.0.0.0`{_RESET}")
        print(f"  {_DIM}  then ask your PC's IP (ip addr) and enter it in the{_RESET}")
        print(f"  {_DIM}  phone app's Status tab.{_RESET}\n")
        return 1
    if lan:
        print(f"  {_GREEN}◈{_RESET} Same Wi-Fi (LAN):")
        for ip in lan:
            print(f"      {_CYAN}http://{ip}:8900{_RESET}")
    if ts:
        print(f"  {_GREEN}◈{_RESET} Anywhere (Tailscale — free, encrypted):")
        print(f"      {_CYAN}http://{ts}:8900{_RESET}")
        print(f"  {_DIM}    (install the Tailscale app on the phone, sign in")
        print(f"     with the same account, then enter that URL.){_RESET}")
    else:
        print(f"  {_DIM}◈ Anywhere (Tailscale): install `tailscale` here + on the")
        print(f"     phone (free), then re-run `friday4 mobile remote` for")
        print(f"     the 100.x URL — no port-forwarding needed.{_RESET}")
    print(f"  {_DIM}◈ Anywhere (free public tunnel, no account): run beside")
    print(f"     the server:\n")
    print(f"       cloudflared tunnel --url http://127.0.0.1:8900\n")
    print(f"     and enter the printed https://….trycloudflare.com URL.")
    print(f"     (Install cloudflared: https://developers.cloudflare.com/"
          f"cloudflare-one/connections/connect-networks/downloads/)")
    print(f"\n  {_YELLOW}◐{_RESET} Security: the LAN paths above are private. For the")
    print(f"  public paths (Tailscale is fine, tunnels are not), start the")
    if token:
        print(f"  server with a token — one is set now "
              f"(FRIDAY_V4_MOBILE_TOKEN / --token).")
    else:
        print(f"  server with a token first:  friday4 mobile serve "
              f"--host 0.0.0.0 --token \"$(openssl rand -hex 16)\"")
        print(f"  and enter the same token in the phone app's Status tab.")
    print(f"\n  {_DIM}Then pair: `friday4 mobile pair` → code into the app's")
    print(f"  Device tab.{_RESET}\n")
    return 0


def _autostart_dir() -> str:
    """The XDG autostart dir (respects XDG_CONFIG_HOME, like 9router)."""
    import os
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(cfg, "autostart")


def _friday4_command() -> str:
    """How to invoke Friday from an autostart entry.

    Prefers the installed ``friday4`` console script (what the operator
    runs); falls back to ``sys.executable -m friday_v4.cli_talk`` so the
    entry also works in a venv-only install. The returned string is
    *quoted* for the Desktop-Entry Exec field — project/venv paths
    routinely contain spaces (e.g. ``~/Projects/Friday V3/…``) which
    would otherwise split the command.
    """
    import shutil
    import sys
    exe = shutil.which("friday4")
    if exe:
        return f'"{exe}"'
    return f'"{sys.executable}" -m friday_v4.cli_talk'


def cmd_mobile_autostart(args: argparse.Namespace) -> int:
    """Write ~/.config/autostart/friday4-mobile.desktop (like 9router).

    The entry launches ``friday4 mobile serve --host 0.0.0.0 --tray``
    on every login — the companion is always up and the tray icon
    (dashboard / URLs / pair / stop) is always present. Token, when
    given, is baked into the command line so a public tunnel stays
    gated from the very first request.
    """
    import os
    import stat
    token = _token_from_args(args)
    d = _autostart_dir()
    os.makedirs(d, exist_ok=True)
    friday = _friday4_command()

    # 0.0.0.0 bind: reachable via every interface (LAN + Tailscale).
    parts = [friday, "mobile", "serve", "--host", "0.0.0.0", "--tray"]
    if token:
        parts += ["--token", token]
    if getattr(args, "tunnel", None) == "cloudflare":
        parts += ["--tunnel", "cloudflare"]
    exec_line = " ".join(parts)

    path = os.path.join(d, "friday4-mobile.desktop")
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Friday V4 — Mobile Companion\n"
        "Comment=Friday companion server + tray (PWA, API, push)\n"
        f"Exec={exec_line}\n"
        "Hidden=false\n"
        "NoDisplay=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    with open(path, "w") as fh:
        fh.write(content)
    # 0600: the Exec line may carry `--token <secret>` — keep it
    # user-only (and it's a per-user autostart entry anyway).
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    _print_logo("Mobile Autostart")
    print(f"  {_GREEN}✔{_RESET} Autostart entry written: {_CYAN}{path}{_RESET}")
    print(f"  {_DIM}  Friday's companion server + tray will start on your")
    print(f"  next login — reachable from your phone via LAN / Tailscale.")
    if token:
        print(f"  {_YELLOW}◐{_RESET} Token baked in — the API is gated from")
        print(f"  the first request (enter it in the app's Status tab).")
    print(f"\n  {_DIM}Start it right now without logging out:"
          f"\n    {exec_line}{_RESET}\n")
    return 0


def cmd_mobile_no_autostart(args: argparse.Namespace) -> int:
    """Remove the autostart entry (idempotent)."""
    import os
    d = _autostart_dir()
    path = os.path.join(d, "friday4-mobile.desktop")
    _print_logo("Mobile Autostart")
    if os.path.exists(path):
        os.remove(path)
        print(f"  {_GREEN}✔{_RESET} Removed {path}")
    else:
        print(f"  {_DIM}No autostart entry present — nothing to remove.")
    print(f"  {_DIM}(The running server keeps running until you stop it.){_RESET}\n")
    return 0


def cmd_mobile_push(args: argparse.Namespace) -> int:
    """Drain the durable ambient queue to the push transporter.

    Without ``--once`` this loops every ``--poll`` seconds (a tiny
    poll-style push loop for hosts where the companion can't keep an
    SSE connection open); with ``--once`` it delivers a single batch
    (scriptable / cron-friendly).
    """
    import time

    from .mobile import PushNotificationService

    _print_logo("Mobile Push")
    # The daemon owns this schedule now (MobilePushWorker, Wave 15) —
    # when it's running, a manual `friday4 mobile push` mostly drains
    # what the daemon already delivered (same persisted cursor, so no
    # double-delivery — but also usually nothing new). Informational
    # hint, not a block.
    try:
        from .daemon import is_running
        if is_running():
            print(f"  {_YELLOW}◐{_RESET} The daemon is running and already "
                  f"drains the queue on a schedule "
                  f"(shared cursor — no double-delivery).")
    except Exception:
        pass
    # Wave 7: with no explicit destination, push to every paired
    # device's Expo token (the daemon does this on its own schedule too).
    transporter = _print_transporter if args.verbose else None
    if transporter is None and args.expo_token:
        from .mobile import expo_transporter
        transporter = expo_transporter(args.expo_token)
    elif transporter is None:
        from .mobile import fanout_transporter
        transporter = fanout_transporter(db_path=args.db)
    service = PushNotificationService(
        db_path=args.db,
        transporter=transporter,
        min_priority=args.min_priority)
    print(f"  {_DIM}cursor: {service.cursor}{_RESET}")
    try:
        while True:
            delivered = service.poll_once()
            if delivered:
                print(f"  {_GREEN}✔ delivered {delivered} event(s) — "
                      f"cursor {service.cursor}{_RESET}")
            if args.once:
                break
            for _ in range(max(int(args.poll / 0.5), 1)):
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    print(f"  {_DIM}total delivered this run: "
          f"{service.delivered_total}{_RESET}\n")
    return 0


def _print_transporter(notification) -> None:
    from .mobile import Notification
    if not isinstance(notification, Notification):
        return
    print(f"  {_CYAN}[{notification.topic}]{_RESET} "
          f"{notification.payload[:140]}")


def build_mobile_parser(subparsers) -> None:
    """Register `friday4 mobile` (used by the integrated CLI)."""
    parser = subparsers.add_parser(
        "mobile", help="Mobile companion transport",
        description="Run the companion API / push transport so your "
                    "phone is another surface of the same Friday.",
    )
    mobile_sub = parser.add_subparsers(dest="mobile_command")

    serve = mobile_sub.add_parser(
        "serve", help="Run the companion server: the phone app + API",
        description="Serves the installable companion PWA at / plus "
                    "/api/status, /api/conversation, POST /api/talk, "
                    "pairing, and SSE /api/events (durable-queue push).")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Bind address — 0.0.0.0, an IP, host:port, or a "
                            "full http://… URL (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8900,
                       help="Port to listen on (default: 8900)")
    serve.add_argument("--db", default=None,
                       help="V4 state DB path (default: ~/.friday/v4.db)")
    serve.add_argument("--token", default=None,
                       help="Bearer token gating every /api/* route "
                            "(default: FRIDAY_V4_MOBILE_TOKEN env; none = "
                            "open on the LAN)")
    serve.add_argument("--tray", action="store_true",
                       help="Show a system tray icon (open dashboard / URLs / "
                            "pair / stop) — like 9router")
    serve.add_argument("--tunnel", choices=["cloudflare"], default=None,
                       help="Spawn a free Cloudflare quick tunnel beside the "
                            "server and print the public URL (needs the "
                            "`cloudflared` binary)")
    serve.set_defaults(func=cmd_mobile_serve)

    remote = mobile_sub.add_parser(
        "remote",
        help="Every way to reach Friday from your phone (LAN / Tailscale / tunnel)",
        description="Prints the exact URL to enter in the phone app for "
                    "each reach path — same Wi-Fi, Tailscale (free, "
                    "anywhere), or a free public tunnel — plus token "
                    "guidance so exposing Friday is safe.")
    remote.add_argument("--token", default=None,
                        help="Token to display as configured (default: "
                             "FRIDAY_V4_MOBILE_TOKEN env)")
    remote.add_argument("--db", default=None,
                        help="V4 state DB path (default: ~/.friday/v4.db)")
    remote.set_defaults(func=cmd_mobile_remote)

    autostart = mobile_sub.add_parser(
        "autostart",
        help="Start the companion server + tray on every login (like 9router)",
        description="Writes an XDG autostart entry (~/.config/autostart/"
                    "friday4-mobile.desktop) that launches "
                    "`friday4 mobile serve --host 0.0.0.0 --tray` when you "
                    "log in — Friday is always reachable from your phone. "
                    "Mirrors the 9router desktop entry pattern.")
    autostart.add_argument("--token", default=None,
                           help="Token baked into the autostart command "
                                "(default: FRIDAY_V4_MOBILE_TOKEN env; none = "
                                "open — Tailscale is private anyway)")
    autostart.add_argument("--tunnel", choices=["cloudflare"], default=None,
                           help="Also spawn the free Cloudflare tunnel on login")
    autostart.add_argument("--db", default=None,
                           help="V4 state DB path (default: ~/.friday/v4.db)")
    autostart.set_defaults(func=cmd_mobile_autostart)

    no_autostart = mobile_sub.add_parser(
        "no-autostart",
        help="Remove the login autostart entry",
        description="Deletes ~/.config/autostart/friday4-mobile.desktop if "
                    "present (idempotent).")
    no_autostart.set_defaults(func=cmd_mobile_no_autostart)

    push = mobile_sub.add_parser(
        "push", help="Deliver the durable queue to the push transporter",
        description="Replays ambient events since the persisted cursor "
                    "to paired devices' Expo tokens (or a --expo-token; "
                    "log by default when nothing is paired).")
    push.add_argument("--once", action="store_true",
                      help="Deliver one batch and exit (scriptable)")
    push.add_argument("--poll", type=float, default=60.0,
                      help="Seconds between polls in loop mode (default 60)")
    push.add_argument("--min-priority", type=int, default=0,
                      help="Only deliver events at/above this priority "
                           "(0 routine, 1 important, 2 critical)")
    push.add_argument("--expo-token", default=None,
                      help="Deliver to this single Expo push token")
    push.add_argument("--verbose", "-v", action="store_true",
                      help="Print each delivered notification")
    push.add_argument("--db", default=None,
                      help="V4 state DB path (default: ~/.friday/v4.db)")
    push.set_defaults(func=cmd_mobile_push)

    pair = mobile_sub.add_parser(
        "pair", help="Generate a one-time pairing code for the app",
        description="Prints a 6-character code (10 min TTL, single use) "
                    "the companion app exchanges for a push binding.")
    pair.add_argument("--db", default=None,
                      help="V4 state DB path (default: ~/.friday/v4.db)")
    pair.set_defaults(func=cmd_mobile_pair)

    devices = mobile_sub.add_parser(
        "devices", help="List phones paired to this Friday")
    devices.add_argument("--db", default=None,
                         help="V4 state DB path (default: ~/.friday/v4.db)")
    devices.set_defaults(func=cmd_mobile_devices)

    unpair = mobile_sub.add_parser(
        "unpair", help="Remove a paired phone by device id")
    unpair.add_argument("device_id", help="The device id from `mobile devices`")
    unpair.add_argument("--db", default=None,
                        help="V4 state DB path (default: ~/.friday/v4.db)")
    unpair.set_defaults(func=cmd_mobile_unpair)


if __name__ == "__main__":  # pragma: no cover - standalone entry
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 mobile")
    build_mobile_parser(parser)
    args = parser.parse_args()
    if hasattr(args, "func"):
        raise SystemExit(args.func(args) or 0)
    parser.print_help()
