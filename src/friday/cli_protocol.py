"""CLI for Named Protocols — ``friday protocol`` commands.

Usage::

    friday protocol list
    friday protocol create <name> --description "..." --step "name:worker:payload_json"
    friday protocol show <name> [--verbose]
    friday protocol run <name> [var=val ...] [--on-failure abort|skip]
    friday protocol delete <name>
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .presentation.cli_format import header, green, red, yellow, gray, cyan


def cmd_protocol(args: argparse.Namespace) -> int:
    """Dispatch ``friday protocol <action>``."""
    from .db import connect
    from .protocol import ProtocolEngine

    action = args.action or "list"
    conn = connect()

    try:
        eng = ProtocolEngine(conn)

        if action == "list":
            return _cmd_list(eng, args)
        elif action == "create":
            return _cmd_create(eng, args)
        elif action == "show":
            return _cmd_show(eng, args)
        elif action == "run":
            return _cmd_run(eng, args)
        elif action == "delete":
            return _cmd_delete(eng, args)
        else:
            print(f"  Unknown action: {action}")
            print(gray("  Available: list, create, show, run, delete"))
            return 2
    finally:
        conn.close()


def _cmd_list(eng, args: argparse.Namespace) -> int:
    """List all named protocols."""
    from .protocol import format_protocols

    protos = eng.list_all()
    print(header("Named Protocols", f"{len(protos)} protocol(s)"))
    print()
    print(format_protocols(protos))
    return 0


def _cmd_create(eng, args: argparse.Namespace) -> int:
    """Create a new named protocol."""
    from .protocol import ProtocolStep, Protocol

    name: str = args.name
    description: str = args.description or ""
    steps_raw: list[str] = args.step or []

    if not steps_raw:
        print(red("  error: at least one --step is required"))
        print(gray("  Format: --step \"name:worker_id:payload_json\""))
        print(gray("  Example: --step \"test:worker:shell:{\\\"command\\\":\\\"pytest\\\"}\""))
        return 1

    steps: list[ProtocolStep] = []
    for i, raw in enumerate(steps_raw):
        # Parse "name:worker_id:payload_json"
        # Worker IDs contain colons ("worker:shell"), so use maxsplit=3
        # and join the middle parts back into the worker_id.
        parts = raw.split(":", 3)
        if len(parts) < 4:
            print(red(f"  error: step #{i+1} is malformed (need name:worker_id:payload_json)"))
            print(gray(f"  Got: {raw}"))
            print(gray(f"  Example: --step \"test:worker:shell:{{\\\"cmd\\\":\\\"pytest\\\"}}\""))
            return 1
        step_name = parts[0]
        worker_id = f"{parts[1]}:{parts[2]}"
        payload_template = parts[3]

        steps.append(ProtocolStep(
            name=step_name,
            worker_id=worker_id,
            payload_template=payload_template,
        ))

    try:
        proto = eng.create(name=name, description=description, steps=steps)
        print(green(f"  ✓ Protocol '{name}' created ({len(steps)} step(s))"))
        if proto.variables:
            print(gray(f"    Variables: {', '.join(proto.variables)}"))
        return 0
    except ValueError as exc:
        print(red(f"  error: {exc}"))
        return 1


def _cmd_show(eng, args: argparse.Namespace) -> int:
    """Show a single protocol in detail."""
    from .protocol import format_protocol

    proto = eng.get(args.name)
    if proto is None:
        print(red(f"  error: Protocol '{args.name}' not found"))
        return 1

    print(format_protocol(proto, verbose=args.verbose))
    return 0


def _cmd_run(eng, args: argparse.Namespace) -> int:
    """Run a named protocol."""
    from .protocol import format_protocol

    name: str = args.name
    var_pairs: list[str] = args.variables or []
    on_failure: str = args.on_failure or "abort"

    # Parse variable=value pairs.
    variables: dict[str, str] = {}
    for pair in var_pairs:
        if "=" not in pair:
            print(yellow(f"  warning: ignoring '{pair}' (expected var=value)"))
            continue
        key, val = pair.split("=", 1)
        if key:
            variables[key] = val

    # Show what we're about to run.
    proto = eng.get(name)
    if proto is None:
        print(red(f"  error: Protocol '{name}' not found"))
        return 1

    print(header(f"Running Protocol: {name}", f"{len(proto.steps)} step(s)"))
    print(f"  {proto.description}")
    if variables:
        print(f"  Variables: {', '.join(f'{k}={v}' for k, v in variables.items())}")
    print()

    # Check for missing required variables.
    missing = [v for v in proto.variables if v not in variables]
    if missing:
        print(red(f"  error: Missing required variables: {', '.join(missing)}"))
        print(gray(f"  Usage: friday protocol run {name} {' '.join(f'{v}=<value>' for v in missing)}"))
        return 1

    if args.dry_run:
        print(gray("  Dry run — not executing."))
        for i, step in enumerate(proto.steps, 1):
            from .protocol import _resolve_template
            resolved = _resolve_template(step.payload_template, variables)
            print(f"    {i}. {step.name}")
            print(f"       Worker: {step.worker_id}")
            print(f"       Payload: {resolved}")
        return 0

    # Execute.
    result = eng.run(name, variables=variables, on_failure=on_failure)
    print()

    for step_result in result.get("steps", []):
        if step_result.get("success"):
            dur = step_result.get("duration_ms", 0)
            print(f"  {green('✓')} {step_result['step']} ({dur}ms)")
        else:
            print(f"  {red('✗')} {step_result['step']}")
            err = step_result.get("error", "Unknown error")
            print(f"      {red(err[:200])}")

    dur = result.get("total_duration_ms", 0)
    print()
    if result.get("success"):
        print(green(f"  ✓ All steps completed ({dur}ms)"))
    else:
        print(red(f"  ✗ Failed at step '{result.get('steps', [{}])[0].get('step', '?')}'"))
        if result.get("error"):
            print(red(f"    {result['error'][:200]}"))

    return 0 if result.get("success") else 1


def _cmd_delete(eng, args: argparse.Namespace) -> int:
    """Delete a named protocol."""
    if not args.yes:
        resp = input(f"  Delete protocol '{args.name}'? [y/N] ").strip().lower()
        if resp != "y":
            print(gray("  Cancelled."))
            return 0

    deleted = eng.delete(args.name)
    if deleted:
        print(green(f"  ✓ Protocol '{args.name}' deleted"))
        return 0
    print(red(f"  error: Protocol '{args.name}' not found"))
    return 1


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``protocol`` subcommand parser."""
    p = sub.add_parser(
        "protocol",
        help="Named multi-step macro procedures — create, run, and manage.",
    )
    p.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "create", "show", "run", "delete"],
        help="Action (default: list).",
    )
    p.add_argument(
        "name", nargs="?", default=None,
        help="Protocol name for show/run/delete/create.",
    )
    p.add_argument(
        "--description", "-d", type=str, default="",
        help="Protocol description (create action).",
    )
    p.add_argument(
        "--step", "-s", action="append", type=str, default=None,
        help="Step definition: name:worker_id:payload_json. Repeatable.",
    )
    p.add_argument(
        "variables", nargs="*", default=None,
        help="Variable assignments for run: var=value var=value ...",
    )
    p.add_argument(
        "--on-failure", type=str, default="abort",
        choices=["abort", "skip"],
        help="Step failure strategy (default: abort).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would run without executing.",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt (delete action).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show step details (show action).",
    )
    p.set_defaults(func=cmd_protocol)
