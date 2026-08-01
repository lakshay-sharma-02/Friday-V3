"""CLI for Self-Evolution Engine — `friday upgrade` command group.

Usage:
    friday upgrade plan "make yourself capable of speaking"    — dry-run: show what would change
    friday upgrade "add a simple text file reader worker"      — full pipeline: sandbox → verify → deploy
    friday upgrade list                                        — list installed capabilities
    friday upgrade enable <name>                               — enable a deployed capability
    friday upgrade disable <name>                              — disable a deployed capability
    friday upgrade rollback <name>                             — rollback a capability
"""

from __future__ import annotations

import argparse

from .db import connect
from .presentation.cli_format import header, green, yellow, red, gray, cyan, bold


_KNOWN_UPGRADE_ACTIONS = frozenset({
    "plan", "deploy", "run", "list", "enable", "disable", "rollback", "status", "show",
})


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Dispatch `friday upgrade` subcommands."""
    action = getattr(args, "action", None)
    request_raw = getattr(args, "request", "")
    if isinstance(request_raw, list):
        request_raw = " ".join(request_raw).strip()

    # If action is not a known keyword, treat it as the start of the request.
    if action and action not in _KNOWN_UPGRADE_ACTIONS:
        request_raw = f"{action} {request_raw}".strip()
        action = None

    # Inject for downstream use.
    args.request = request_raw

    # No action keyword AND no request → show help.
    if not action and not request_raw:
        return _show_upgrade_help()

    # No action keyword but there IS a request → default to deploy.
    if not action:
        return _upgrade_run(args)

    if action == "plan":
        return _upgrade_plan(args)
    if action in ("deploy", "run"):
        return _upgrade_run(args)
    if action == "list":
        return _upgrade_list()
    if action == "enable":
        return _upgrade_enable(args)
    if action == "disable":
        return _upgrade_disable(args)
    if action == "rollback":
        return _upgrade_rollback(args)
    if action in ("status", "show"):
        return _upgrade_status(args)

    print(f"Unknown upgrade action: {action}")
    print(gray("  Try: friday upgrade plan, friday upgrade list, friday upgrade enable <name>"))
    return 1


def _show_upgrade_help() -> int:
    print(header("Self-Evolution Engine", "friday upgrade"))
    print()
    print("  Friday can upgrade itself — you can add new capabilities through")
    print("  natural language requests. Each upgrade goes through a sandbox,")
    print("  verification, and is deployed with rollback safety.")
    print()
    print(gray("  Commands:"))
    print()
    print(gray("    friday upgrade plan \"<request>\""))
    print(gray("      Dry-run: show what would change without modifying anything"))
    print()
    print(gray("    friday upgrade \"<request>\""))
    print(gray("      Full pipeline: plan → sandbox → verify → deploy"))
    print()
    print(gray("    friday upgrade list"))
    print(gray("      List installed capabilities and their status"))
    print()
    print(gray("    friday upgrade enable <name>"))
    print(gray("      Enable a deployed capability"))
    print()
    print(gray("    friday upgrade disable <name>"))
    print(gray("      Disable a deployed capability"))
    print()
    print(gray("    friday upgrade rollback <name>"))
    print(gray("      Rollback a deployed capability to pre-deployment state"))
    print()
    print(gray("  Examples:"))
    print(gray('    friday upgrade plan "make yourself capable of speaking"'))
    print(gray('    friday upgrade "add a simple text file reader worker"'))
    print(gray("    friday upgrade list"))
    print(gray("    friday upgrade enable voice_support"))
    print(gray("    friday upgrade rollback voice_support"))
    return 0


def _get_request(args: argparse.Namespace) -> str:
    """Extract the upgrade request from args."""
    request = getattr(args, "request", None)
    if request:
        return request
    # Fall back to positional text.
    text = getattr(args, "text", None)
    if text:
        return " ".join(text) if isinstance(text, list) else text
    return ""


def _upgrade_plan(args: argparse.Namespace) -> int:
    """Dry-run: show what would change without modifying anything."""
    request = _get_request(args)
    if not request:
        print(red("  Specify what capability to add."))
        print(gray('  Example: friday upgrade plan "make yourself capable of speaking"'))
        return 1

    from .meta.sandbox import Sandbox
    from .meta.si_planner import (
        generate_capability_plan,
        update_capability_plan_with_deterministic_fallback,
        estimate_plan_changes,
        validate_capability_plan,
    )

    print(header("Upgrade Plan", "dry-run"))
    print(yellow(f"  Analyzing: {request}"))
    print()

    # Create sandbox for codebase context (no real changes).
    sandbox = Sandbox(label="plan_preview")
    try:
        sb_path = sandbox.create()
        print(gray(f"  Sandbox created for analysis"))
        print()

        conn = connect()
        try:
            plan = generate_capability_plan(request, sandbox, conn=conn)
        finally:
            conn.close()

        # Fallback to deterministic plan if LLM unavailable.
        plan = update_capability_plan_with_deterministic_fallback(request, plan)

        if not plan:
            print(red("  Could not generate a plan — both LLM and fallback failed."))
            sandbox.cleanup()
            return 1

        # Validate.
        errors = validate_capability_plan(plan)
        if errors:
            print(red("  Plan validation errors:"))
            for e in errors:
                print(f"    - {e}")
            sandbox.cleanup()
            return 1

        # Show the plan summary.
        cap_name = plan.get("capability_name", "?")
        print(header("Plan", cap_name))
        print()
        print(estimate_plan_changes(plan))
        print()

        # Show verification steps.
        ver_steps = plan.get("verification_steps", [])
        if ver_steps:
            print(gray("  Verification steps:"))
            for vs in ver_steps:
                print(f"    • {vs}")

        print()
        print(gray(f"  Run the full pipeline: friday upgrade \"{request}\""))

    finally:
        sandbox.cleanup()

    return 0


def _upgrade_run(args: argparse.Namespace) -> int:
    """Full pipeline: sandbox → deploy (CC when available, LLM fallback)."""
    request = _get_request(args)
    if not request:
        print(red("  Specify what capability to add."))
        print(gray('  Example: friday upgrade "make yourself capable of speaking"'))
        return 1

    # Confirm with the operator (safety gate).
    print(header("Self-Evolution", "deploy"))
    print(yellow(f"  This will modify Friday's own source code!"))
    print(f"  Request: {request}")

    print()
    answer = input(gray("  Continue? (y/N): ")).strip().lower()
    if answer not in ("y", "yes"):
        print(gray("  Aborted."))
        return 0

    from .meta.deploy import deploy_capability

    print()
    print(gray("  Deploying capability..."))

    conn = connect()
    try:
        result = deploy_capability(conn, request)
        if result:
            print()
            print(green(f"  ✅ Upgrade complete: {result}"))
            print(gray(f"  Enable it: friday upgrade enable {result}"))
            print(gray(f"  Check status: friday upgrade list"))
        else:
            print(red(f"  ❌ Upgrade failed"))
            return 1

    except Exception as e:
        print(red(f"  Error: {e}"))
        import traceback
        traceback.print_exc()
        return 1
    finally:
        conn.close()

    return 0


def _upgrade_list() -> int:
    """List installed capabilities and their status."""
    from .meta.capability import CapabilityRegistry

    conn = connect()
    try:
        registry = CapabilityRegistry(conn)
        flags = registry.list_all()

        if not flags:
            print(header("Capabilities", "none deployed"))
            print()
            print(gray("  No capabilities deployed yet."))
            print(gray('  Deploy one: friday upgrade "make yourself capable of speaking"'))
            print(gray('  Preview:    friday upgrade plan "make yourself capable of speaking"'))
            return 0

        print(header("Capabilities", f"{len(flags)} total"))
        print()

        for f in flags:
            if f.enabled:
                status = green("✅ Enabled")
            elif f.installed:
                status = yellow("❌ Disabled")
            else:
                status = gray("⏳ Pending")

            deps_str = green("deps ✓") if f.deps_installed else yellow("no deps")
            print(f"  {f.name:<25s} {status:<15s}  {deps_str}")
            if f.description:
                print(f"  {'':25s} {gray(f.description[:60])}")

            # Show rollback commit if available.
            if f.rollback_commit:
                print(f"  {'':25s} {gray(f'rollback: {f.rollback_commit[:12]}')}")
            print()

    finally:
        conn.close()

    return 0


def _upgrade_enable(args: argparse.Namespace) -> int:
    """Enable a deployed capability."""
    name = getattr(args, "name", "") or getattr(args, "request", "")
    if not name:
        print(red("  Specify a capability name."))
        print(gray("  Use: friday upgrade list"))
        return 1

    from .meta.capability import CapabilityRegistry

    conn = connect()
    try:
        registry = CapabilityRegistry(conn)
        flag = registry.get(name)
        if not flag:
            print(red(f"  Capability '{name}' not found."))
            print(gray("  Use: friday upgrade list"))
            return 1

        if flag.enabled:
            print(yellow(f"  Capability '{name}' is already enabled."))
            return 0

        registry.enable(name)
        print(green(f"  ✅ Capability '{name}' enabled."))
    finally:
        conn.close()

    return 0


def _upgrade_disable(args: argparse.Namespace) -> int:
    """Disable a deployed capability."""
    name = getattr(args, "name", "") or getattr(args, "request", "")
    if not name:
        print(red("  Specify a capability name."))
        print(gray("  Use: friday upgrade list"))
        return 1

    from .meta.capability import CapabilityRegistry

    conn = connect()
    try:
        registry = CapabilityRegistry(conn)
        flag = registry.get(name)
        if not flag:
            print(red(f"  Capability '{name}' not found."))
            print(gray("  Use: friday upgrade list"))
            return 1

        if not flag.enabled:
            print(yellow(f"  Capability '{name}' is already disabled."))
            return 0

        registry.disable(name)
        print(yellow(f"  Capability '{name}' disabled."))
    finally:
        conn.close()

    return 0


def _upgrade_rollback(args: argparse.Namespace) -> int:
    """Rollback a deployed capability."""
    name = getattr(args, "name", "") or getattr(args, "request", "")
    if not name:
        print(red("  Specify a capability name to rollback."))
        print(gray("  Use: friday upgrade list"))
        return 1

    # Confirm with the operator.
    print(header("Self-Evolution", "rollback"))
    print(yellow(f"  This will revert all changes made by '{name}'!"))
    print()
    answer = input(gray(f"  Rollback {name}? (y/N): ")).strip().lower()
    if answer not in ("y", "yes"):
        print(gray("  Aborted."))
        return 0

    from .meta.deploy import rollback_capability

    conn = connect()
    try:
        ok = rollback_capability(conn, name)
        if ok:
            print(green(f"  ✅ Capability '{name}' rolled back successfully."))
        else:
            print(red(f"  ❌ Rollback failed."))
            return 1
    finally:
        conn.close()

    return 0


def _upgrade_status(args: argparse.Namespace) -> int:
    """Show status of a specific capability."""
    name = getattr(args, "name", "") or getattr(args, "request", "")
    if not name:
        return _upgrade_list()

    from .meta.capability import CapabilityRegistry

    conn = connect()
    try:
        registry = CapabilityRegistry(conn)
        flag = registry.get(name)
        if not flag:
            print(red(f"  Capability '{name}' not found."))
            print(gray("  Use: friday upgrade list"))
            return 1

        print(header("Capability", flag.name))
        print(f"  Description:  {flag.description}")
        print(f"  Status:       {'✅ Enabled' if flag.enabled else '❌ Disabled' if flag.installed else '⏳ Pending'}")
        print(f"  Installed:    {'Yes' if flag.installed else 'No'}")
        print(f"  Deps:         {'Installed' if flag.deps_installed else 'Not installed'}")
        if flag.added_at:
            print(f"  Deployed at:  {flag.added_at[:19]}")
        if flag.enabled_at:
            print(f"  Enabled at:   {flag.enabled_at[:19]}")
        if flag.rollback_commit:
            print(f"  Rollback:     {flag.rollback_commit[:16]}")
        if flag.last_used_at:
            print(f"  Last used:    {flag.last_used_at[:19]}")

    finally:
        conn.close()

    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``upgrade`` subcommand parser for self-evolution."""
    p = sub.add_parser(
        "upgrade",
        help="Self-evolution engine — upgrade Friday's own capabilities."
    )
    action_help = ("One of: plan, deploy, run, list, enable, disable, rollback, status, show. "
                   "Omit to run the full pipeline with an inline request.")
    p.add_argument(
        "action",
        nargs="?",
        default=None,
        help=action_help,
    )
    p.add_argument(
        "request",
        nargs="*",  # Remaining words become the full request text.
        default="",
    )
    p.add_argument("--name", "-n", default="")
    p.set_defaults(func=cmd_upgrade)
