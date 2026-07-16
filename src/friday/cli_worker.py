"""CLI commands for the Worker Registry (Milestone 9.2).

`friday workers`                -> list all registered workers.
`friday worker <name>`          -> show one worker's full capability profile.
`friday worker register <file>` -> register a custom worker from a JSON manifest.
`friday worker export`          -> export the entire registry as JSON.

READ + WRITE against the registry catalog only. No execution, scheduling, or
worker selection anywhere in this module.
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import connect
from .worker import WorkerRegistry
from .worker.engine import RegistryError


def cmd_workers(args: argparse.Namespace) -> int:
    """READ: list all registered workers (id, kind, status, capabilities)."""
    conn = connect()
    reg = WorkerRegistry(conn)
    workers = reg.all_workers()
    conn.close()
    if not workers:
        print("No workers registered yet.\n")
        print("Run:\n")
        print("  friday worker register <builtin-manifest>.json\n")
        return 0
    print(f"Registered workers ({len(workers)}):\n")
    for w in workers:
        print(w.to_summary())
    return 0


def cmd_worker_show(args: argparse.Namespace) -> int:
    """READ: show one worker's full capability profile."""
    name = getattr(args, "name", None)
    if not name:
        print("error: worker name required (friday worker <name>)",
              file=sys.stderr)
        return 2
    conn = connect()
    reg = WorkerRegistry(conn)
    w = reg.worker_by_name(name)
    conn.close()
    if w is None:
        print(f"error: no such worker: {name}", file=sys.stderr)
        return 2
    sys.stdout.write(w.to_detail() + "\n")
    return 0


def cmd_worker_register(args: argparse.Namespace) -> int:
    """WRITE: register a custom worker from a JSON manifest file."""
    path = getattr(args, "file", None)
    if not path:
        print("error: manifest file required (friday worker register <file>)",
              file=sys.stderr)
        return 2
    try:
        text = open(path, "r", encoding="utf-8").read()
        manifest = json.loads(text)
    except OSError as exc:
        print(f"error: cannot read manifest: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON manifest: {exc}", file=sys.stderr)
        return 2

    conn = connect()
    reg = WorkerRegistry(conn)
    try:
        res = reg.register_from_manifest(manifest)
    except RegistryError as exc:
        conn.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    conn.close()
    sys.stdout.write(res.to_text())
    return 0


def cmd_worker_export(args: argparse.Namespace) -> int:
    """READ: export the entire registry as deterministic JSON."""
    conn = connect()
    reg = WorkerRegistry(conn)
    data = reg.export_json()
    conn.close()
    print(json.dumps(data, indent=2))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Dispatch friday worker subcommands.

    `friday worker`            -> list all workers
    `friday worker <name>`     -> show one worker
    `friday worker register --file <f>` -> register a custom worker
    `friday worker export`     -> export the registry JSON
    """
    token = getattr(args, "token", None)
    if token == "register":
        return cmd_worker_register(args)
    if token == "export":
        return cmd_worker_export(args)
    if token:
        # Show one worker by name.
        show_args = argparse.Namespace(name=token)
        return cmd_worker_show(show_args)
    return cmd_workers(args)
