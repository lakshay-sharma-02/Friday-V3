"""friday capability discover|list|info|benchmark (M10)."""
from __future__ import annotations
import argparse
import json
from .db import connect
from .worker.engine import WorkerRegistry, _EXTERNAL_MANIFESTS
from .runtime.discovery import discover
from .runtime.benchmark import BenchmarkRunner, BenchmarkTask


def cmd_capability(args: argparse.Namespace, conn=None) -> int:
    conn = conn or connect()
    reg = WorkerRegistry(conn)
    token = getattr(args, "token", None) or "list"
    if token == "discover":
        res = discover(_EXTERNAL_MANIFESTS)
        print(f"Available ({len(res.available)}): {', '.join(res.available) or '-'}")
        print(f"Unavailable ({len(res.unavailable)}): {', '.join(res.unavailable) or '-'}")
        for w, deps in res.missing_deps.items():
            print(f"  {w}: missing {', '.join(deps)}")
        reg.sync_availability(res)
        return 0
    if token == "list":
        for w in reg.all_workers():
            print(w.to_summary())
        return 0
    if token == "info":
        name = getattr(args, "worker", None)
        w = reg.worker_by_name(name) if name else None
        if w is None:
            print("error: worker not found", file=__import__("sys").stderr)
            return 2
        print(w.to_detail())
        print(f"  Availability: {getattr(w, 'availability', 'available')}")
        return 0
    if token == "benchmark":
        runner = BenchmarkRunner(
            [BenchmarkTask(capability="Documentation", payload="write a doc",
                           expect_nonempty_stdout=True)],
            [("worker:native", lambda p: ("native ok", 0))])
        rep = runner.run()
        print(json.dumps({k: [r.__dict__ for r in v] for k, v in rep.items()}, indent=2))
        return 0
    return 2
