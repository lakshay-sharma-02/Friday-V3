"""CLI commands for `friday4 collab` — Wave 5 collaboration.

Usage:
    friday4 collab start [--workspace NAME] [--port N] [--beacon-port N]
                         # run a coordinator (sync + discovery)
    friday4 collab status                                # local snapshot
    friday4 collab peers                                 # discovered peers
    friday4 collab observations [--limit N]              # merged observations
    friday4 collab add <json-or-text>                    # record an observation
    friday4 collab share [--host H --port P]             # one-shot sync
    friday4 collab perms <list|add|remove> ...

State lives in ~/.friday/collab/state.json so a stopped coordinator
keeps its merged observations, ACLs, and last-known peers.
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from .collab.coordinator import Coordinator

logger = logging.getLogger("friday_v4.cli_collab")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"

_ROLE_COLORS = {"owner": _CYAN, "member": _GREEN, "reader": _DIM}


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Collaboration{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    print()


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


def _print_ok(text: str):
    print(f"  {_GREEN}✓ {text}{_RESET}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _print_row(key: str, value):
    print(f"  {_DIM}{key:<18}{_RESET}{value}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_collab_start(args: argparse.Namespace) -> int:
    """Run a coordinator (sync server + discovery) until Ctrl+C."""
    coordinator = Coordinator(
        peer_id=args.peer_id,  # None → stable persisted id
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    if not coordinator.start():
        _print_error("could not start — check ports and try again")
        return 1
    _print_logo()
    _print_ok(f"collaboration running · workspace {coordinator.workspace}")
    _print_dim(f"peer: {coordinator.peer_id}")
    _print_dim(f"sync port {coordinator.sync_port} · "
               f"beacon port {coordinator.beacon_port}")
    print()
    _print_dim("discovering peers… (Ctrl+C to stop)")
    try:
        while True:
            peers = coordinator.peers()
            if peers:
                print()
                _print_row("peers", f"{len(peers)} live")
                for p in peers:
                    _print_dim(
                        f"  · {p.peer_id} @ {p.host}:{p.port} "
                        f"[{p.workspace}]")
            coordinator.sync_once()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        coordinator.stop()
        _print_dim("coordinator stopped · state saved")
    return 0


def cmd_collab_status(args: argparse.Namespace) -> int:
    """Print a local snapshot of the coordinator state (no network)."""
    coordinator = Coordinator(
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    status = coordinator.status()
    _print_logo()
    _print_row("workspace", status["workspace"])
    _print_row("peer", status["peer_id"])
    _print_row("running", "yes" if status["running"] else "no")
    _print_row("observations", f"{status['observations']} "
                               f"({status['live_observations']} live)")
    _print_row("peers (live)", f"{len(status['peers'])}")
    _print_row("sync / beacon", f"{status['sync_port']} / "
                                f"{status['beacon_port']}")
    perms = status["permissions"]
    _print_row("members", f"{len(perms['members'])}")
    for peer_id, role in perms["members"].items():
        color = _ROLE_COLORS.get(role, _RESET)
        _print_dim(f"  · {peer_id}  {color}{role}{_RESET}")
    return 0


def cmd_collab_peers(args: argparse.Namespace) -> int:
    """List discovered peers (live, from the running coordinator state)."""
    coordinator = Coordinator(
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    peers = coordinator.peers()
    _print_logo()
    if not peers:
        _print_dim("no live peers — is another coordinator running?")
        return 0
    for p in peers:
        _print_row(p.peer_id, f"{p.host}:{p.port} [{p.workspace}]")
    return 0


def cmd_collab_observations(args: argparse.Namespace) -> int:
    """List merged observations (newest first)."""
    coordinator = Coordinator(
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    observations = coordinator.observations(limit=args.limit)
    _print_logo()
    if not observations:
        _print_dim("no observations yet")
        return 0
    for obs in observations:
        ts = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(obs.get("ts", 0) / 1000))
        payload = obs.get("payload", {})
        _print_row(f"{obs.get('peer_id')}", f"{ts} {payload}")
    return 0


def cmd_collab_add(args: argparse.Namespace) -> int:
    """Record a local observation (accepts JSON or a plain text string)."""
    coordinator = Coordinator(
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    raw = " ".join(args.text).strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"text": raw}
    obs_id = coordinator.add_observation(payload)
    if not obs_id:
        _print_error("could not record observation")
        return 1
    _print_ok(f"observation recorded · {obs_id}")
    return 0


def cmd_collab_share(args: argparse.Namespace) -> int:
    """One-shot sync against an explicit peer (no discovery needed)."""
    coordinator = Coordinator(
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    if coordinator.sync is None:
        from .collab.sync import SyncEngine
        coordinator.sync = SyncEngine(
            store=coordinator.store, peer_id=coordinator.peer_id,
            workspace=coordinator.workspace, port=args.port,
        )
    _print_logo()
    try:
        result = coordinator.sync.sync_with(args.host, args.peer_port)
    except Exception as exc:
        _print_error(f"sync failed: {exc}")
        return 1
    if not result.get("accepted"):
        _print_error(f"peer rejected the handshake "
                     f"({result.get('reason', 'unknown')})")
        return 1
    _print_ok(f"synced — sent {result['sent']}, received "
              f"{result['received']}, applied {result['applied']}")
    return 0


def cmd_collab_perms(args: argparse.Namespace) -> int:
    """Manage workspace ACLs: list, add <peer> <role>, remove <peer>."""
    coordinator = Coordinator(
        workspace=args.workspace,
        sync_port=args.port,
        beacon_port=args.beacon_port,
    )
    action = args.perms_command
    if action == "list":
        for peer_id, role in coordinator.permissions.members().items():
            color = _ROLE_COLORS.get(role, _RESET)
            _print_row(peer_id, f"{color}{role}{_RESET}")
        if not coordinator.permissions.members():
            _print_dim("no members — add one with "
                       "`friday4 collab perms add <peer> <role>`")
        return 0
    if action == "add":
        if not args.peer_id:
            _print_error("usage: friday4 collab perms add <peer> <role>")
            return 1
        coordinator.add_member(args.peer_id, args.role)
        _print_ok(f"{args.peer_id} → {args.role}")
        return 0
    if action == "remove":
        if not args.peer_id:
            _print_error("usage: friday4 collab perms remove <peer>")
            return 1
        if coordinator.remove_member(args.peer_id):
            _print_ok(f"removed {args.peer_id}")
            return 0
        _print_dim(f"{args.peer_id} is not a member")
        return 0
    _print_error(f"unknown perms command: {action}")
    return 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_collab_commands(collab_parser) -> None:
    """Attach the collab subcommands to a parser.

    Shared by the ``friday4 collab`` dispatch (via build_collab_parser)
    and the standalone ``python -m friday_v4.cli_collab`` entry, which
    already *is* ``friday4 collab`` — so it must not nest one more
    ``collab`` level.
    """
    collab_sub = collab_parser.add_subparsers(dest="collab_command")

    start_parser = collab_sub.add_parser(
        "start", help="Run a coordinator (sync + discovery)")
    start_parser.add_argument("--workspace", default="default",
                              help="Workspace name (default: default)")
    start_parser.add_argument("--peer-id", default=None,
                              help="Override this instance's peer id")
    start_parser.add_argument("--port", type=int, default=9876,
                              help="Sync (TCP) port (default: 9876)")
    start_parser.add_argument("--beacon-port", type=int, default=9988,
                              help="Discovery (UDP) port (default: 9988)")
    start_parser.set_defaults(func=cmd_collab_start)

    status_parser = collab_sub.add_parser(
        "status", help="Show the collaboration snapshot")
    status_parser.add_argument("--workspace", default="default")
    status_parser.add_argument("--port", type=int, default=9876)
    status_parser.add_argument("--beacon-port", type=int, default=9988)
    status_parser.set_defaults(func=cmd_collab_status)

    peers_parser = collab_sub.add_parser(
        "peers", help="List discovered peers")
    peers_parser.add_argument("--workspace", default="default")
    peers_parser.add_argument("--port", type=int, default=9876)
    peers_parser.add_argument("--beacon-port", type=int, default=9988)
    peers_parser.set_defaults(func=cmd_collab_peers)

    obs_parser = collab_sub.add_parser(
        "observations", help="List merged observations")
    obs_parser.add_argument("--limit", type=int, default=None,
                            help="Max observations to show")
    obs_parser.add_argument("--workspace", default="default")
    obs_parser.add_argument("--port", type=int, default=9876)
    obs_parser.add_argument("--beacon-port", type=int, default=9988)
    obs_parser.set_defaults(func=cmd_collab_observations)

    add_parser = collab_sub.add_parser(
        "add", help="Record an observation (JSON or plain text)")
    add_parser.add_argument("text", nargs="+",
                            help="Observation payload (JSON object or text)")
    add_parser.add_argument("--workspace", default="default")
    add_parser.add_argument("--port", type=int, default=9876)
    add_parser.add_argument("--beacon-port", type=int, default=9988)
    add_parser.set_defaults(func=cmd_collab_add)

    share_parser = collab_sub.add_parser(
        "share", help="One-shot sync against an explicit peer")
    share_parser.add_argument("--host", default="127.0.0.1",
                              help="Peer host (default: 127.0.0.1)")
    share_parser.add_argument("--peer-port", type=int, default=9876,
                              help="Peer sync port (default: 9876)")
    share_parser.add_argument("--workspace", default="default")
    share_parser.add_argument("--port", type=int, default=9876)
    share_parser.add_argument("--beacon-port", type=int, default=9988)
    share_parser.set_defaults(func=cmd_collab_share)

    # Shared options for the perms sub-sub-parsers (cmd_collab_perms reads
    # workspace/port/beacon-port to construct a Coordinator).
    perms_common = argparse.ArgumentParser(add_help=False)
    perms_common.add_argument("--workspace", default="default")
    perms_common.add_argument("--port", type=int, default=9876)
    perms_common.add_argument("--beacon-port", type=int, default=9988)
    perms_parser = collab_sub.add_parser(
        "perms", help="Manage workspace ACLs")
    perms_sub = perms_parser.add_subparsers(dest="perms_command")
    list_parser = perms_sub.add_parser(
        "list", parents=[perms_common], help="List members")
    list_parser.set_defaults(func=cmd_collab_perms)
    add_perm_parser = perms_sub.add_parser(
        "add", parents=[perms_common], help="Add a member")
    add_perm_parser.add_argument("peer_id", help="Peer id")
    add_perm_parser.add_argument(
        "role", nargs="?", default="member",
        choices=["owner", "member", "reader"],
        help="Role (default: member)")
    add_perm_parser.set_defaults(func=cmd_collab_perms)
    remove_perm_parser = perms_sub.add_parser(
        "remove", parents=[perms_common], help="Remove a member")
    remove_perm_parser.add_argument("peer_id", help="Peer id")
    remove_perm_parser.set_defaults(func=cmd_collab_perms)


def build_collab_parser(subparsers) -> None:
    """Attach the ``collab`` subcommand to the ``friday4`` parser."""
    collab_parser = subparsers.add_parser(
        "collab", help="Collaboration — multi-instance sync",
        description="Share observations across Friday instances "
                    "(LAN sync, pure stdlib).",
    )
    _add_collab_commands(collab_parser)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v4.cli_collab`."""
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="friday4 collab")
    _add_collab_commands(parser)

    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args) or 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
