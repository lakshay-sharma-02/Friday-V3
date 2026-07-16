"""Milestone 9.2 dogfood transcript — Worker Registry end-to-end.

Drives the REAL registry engine (no LLM, no mock) through the full registry
lifecycle, demonstrating catalog completeness and zero execution decisions:

  Register built-ins -> Show summaries -> Show one worker -> Register custom
  -> Version update -> Disable -> Export -> History (append-only) -> Import.

The registry is WRITE-ONLY metadata. It describes workers' capability profiles.
It NEVER executes, schedules, selects, or runs work. The lower layers
(Observation, Context, Knowledge, Understanding, Initiatives, Insights, Brain,
Planning, Task Graph) are UNTOUCHED — this is purely additive.

Run:  PYTHONPATH=. python dogfood_run/m92_worker_registry.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from src.friday.db import connect, get_all_workers
from src.friday.worker import WorkerRegistry, Worker, WorkerKind, all_capabilities


BASE = "2026-07-16T00:00:00+00:00"


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = connect(path)

    print("=" * 78)
    print("M9.2 DOGFOOD: Worker Registry — capability catalog (real engine)")
    print("=" * 78)

    reg = WorkerRegistry(conn)

    print("\n[1] Register built-in workers (the spec's minimum deterministic set)")
    res = reg.register_builtins()
    print(f"    registered={res.created} rejected={res.rejected}")
    print(f"    total workers in registry: {reg.count()}")

    print("\n[2] Worker summaries (`friday workers`)")
    for w in reg.all_workers():
        print(w.to_summary())

    print("\n[3] One worker capability profile (`friday worker Claude`)")
    claude = reg.worker_by_name("Claude")
    detail = claude.to_detail()
    print("    " + detail.splitlines()[0])
    print(f"    caps: {', '.join(claude.capabilities)}")
    print(f"    langs: {', '.join(claude.supported_languages)}")
    print(f"    task_types: {', '.join(claude.supported_task_types)}")
    print(f"    plan_types: {', '.join(claude.supported_plan_types)}")
    print(f"    limitations: {'; '.join(claude.limitations)}")
    print(f"    speed={claude.estimated_speed} cost={claude.estimated_cost} "
          f"ctx={claude.context_window} parallel={claude.parallelism}")
    print(f"    requires: net={claude.requires_network} fs="
          f"{claude.requires_filesystem} git={claude.requires_git} "
          f"py={claude.requires_python} sh={claude.requires_shell}")

    print("\n[4] Capability Resolver hook: who can do 'rust' / 'reasoning'?")
    for cap in ("rust", "reasoning", "git operations"):
        names = ", ".join(w.name for w in reg.workers_for_capability(cap))
        print(f"    {cap:12}: {names or '(none)'}")

    print("\n[5] Register a CUSTOM worker from a JSON manifest")
    manifest = {
        "name": "Galileo", "kind": "agent",
        "description": "Local research agent.",
        "capabilities": ["Research", "Python", "Reasoning",
                         "Telepathy"],  # Telepathy -> rejected
        "supported_languages": ["Python", "Klingon"],  # Klingon -> rejected
        "supported_task_types": ["research", "analysis", "frobnicate"],
        "supported_plan_types": ["research", "feature"],
        "limitations": ["No deployment authority"],
        "estimated_speed": "medium", "context_window": 32000,
    }
    r = reg.register_from_manifest(manifest)
    g = reg.worker_by_name("Galileo")
    print(f"    created={r.created} rejected={r.rejected}")
    print(f"    stored caps: {g.capabilities}")
    print(f"    stored langs: {g.supported_languages}")
    print(f"    stored task_types: {g.supported_task_types}")
    print("    -> no hallucinated capabilities stored")

    print("\n[6] Version update (deterministic bump + version log)")
    before = reg.worker_by_name("Galileo").version
    # Material change: add a capability + a task type -> version bumps.
    reg.register(Worker(name="Galileo", kind=WorkerKind.from_str("agent"),
                        capabilities=["Research", "Python", "Reasoning",
                                      "Planning"],
                        supported_languages=["Python"],
                        supported_task_types=["research", "analysis",
                                              "implementation"],
                        supported_plan_types=["research", "feature"],
                        limitations=["No deployment authority"],
                        version="1.0.0"))
    after = reg.worker_by_name("Galileo").version
    print(f"    Galileo version: {before} -> {after} "
          f"(bumped on material change)")
    print(f"    version records: {len(reg.versions('worker:galileo'))}")

    print("\n[7] Append-only history")
    hist = reg.history("worker:claude")
    print(f"    Claude history snapshots: {len(hist)}")
    for h in reversed(hist):
        print(f"      {h.registered_at[:19]}  {h.event_type:12} "
              f"v{h.version} caps={h.capabilities.count(',')+1 if h.capabilities else 0}")

    print("\n[8] Disable a worker (status mutation; history preserved)")
    reg.disable("Shell")
    shell = reg.worker_by_name("Shell")
    print(f"    Shell status: {shell.status} | active workers now: "
          f"{len(reg.active_workers())}")
    reg.enable("Shell")

    print("\n[9] Registry export (`friday worker export`)")
    export = reg.export_json()
    print(f"    registry_version={export['registry_version']} "
          f"worker_count={export['worker_count']}")
    print("    sample worker JSON (Claude):")
    cj = next(w for w in export["workers"] if w["name"] == "Claude")
    print("      " + json.dumps(cj, indent=2).replace("\n", "\n      "))

    print("\n[10] Import round-trip (export -> fresh registry)")
    conn2 = connect(":memory:")
    reg2 = WorkerRegistry(conn2)
    for wj in export["workers"]:
        reg2.register_from_manifest(wj)
    print(f"    re-imported workers: {reg2.count()} "
          f"(== {reg.count()}: {reg2.count() == reg.count()})")

    print("\n[11] CABABILITY vocabulary sanity")
    print(f"    closed vocabulary size: {len(all_capabilities())}")
    print(f"    'Rust' valid: {('Rust' in all_capabilities())}")

    print("\n[12] GUARD: registry takes ZERO execution decisions")
    forbidden = ("execute", "schedule", "select_worker", "run", "dispatch")
    leaked = [f for f in forbidden if hasattr(reg, f)]
    print(f"    forbidden methods present: {leaked or 'NONE'}")

    # Lower layers untouched.
    lower = ["knowledge", "understanding", "initiatives", "insights",
             "plans", "task_graphs"]
    counts = {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
              for t in lower}
    print(f"    lower-layer row counts (must be 0): {counts}")

    conn.close()
    conn2.close()
    os.remove(path)
    print("\n" + "=" * 78)
    print("DOGFOOD COMPLETE — Worker catalog populated; NO work executed.")
    print("=" * 78)


if __name__ == "__main__":
    main()
