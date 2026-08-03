"""Wave 12 benchmark tool — V4 vs V3 where importable, pure stdlib.

Measures the operations that matter for daily use so regressions are
visible: DB connect/migrate, security scan on a fixture tree, research
analyze, reasoning answer, collab CRDT merge, and ambient publish. When
the V3 ``friday`` package is importable, comparable V3 ops are measured
too (V4's own suite runs standalone — V3 is optional heritage).

Usage:
    python tools/benchmarks.py [--iterations N] [--json]

Pure stdlib: ``time.perf_counter`` + ``tempfile``; never crashes — a
failed measurement prints ``n/a`` instead of raising.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent


def _timeit(fn: Callable, iterations: int = 3) -> Optional[float]:
    """Best (fastest) wall time over ``iterations`` runs, in ms."""
    best: Optional[float] = None
    for _ in range(iterations):
        try:
            start = time.perf_counter()
            fn()
            elapsed = (time.perf_counter() - start) * 1000.0
        except Exception:
            return None
        best = elapsed if best is None else min(best, elapsed)
    return best


# ── V4 measurements ─────────────────────────────────────────────────────


def _v4_fixture(tmp: Path) -> Path:
    """A tiny fixture tree so scans/analyze have real (small) input.

    Idempotent: ``v4_benchmarks`` and ``v3_benchmarks`` share one tmp
    dir in ``main()``, so creation must tolerate re-entry.
    """
    repo = tmp / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text(
        "import os\n\ndef main():\n    print('hi')\n")
    (repo / "pyproject.toml").write_text(
        "[project]\nname='bench'\nversion='0.1.0'\n"
        "dependencies = ['requests>=2.31']\n")
    (repo / "README.md").write_text("# Bench repo\n\nA fixture.\n")
    return repo


def v4_benchmarks(tmp: Path, iterations: int = 3) -> dict:
    """Benchmark the V4 paths that matter for daily use."""
    from friday_v4 import db
    from friday_v4.security.scanner import VulnerabilityScanner
    from friday_v4.research import analyze

    results: dict = {}

    def db_connect():
        conn = db.connect(tmp / "v4.db")
        conn.close()
    results["v4.db_connect_migrate"] = _timeit(db_connect, iterations)

    def db_roundtrip():
        conn = db.connect(tmp / "v4.db")
        mid = db.create_mission(conn, "bench")
        db.add_mission_step(conn, mid, "step")
        db.list_missions(conn, limit=10)
        conn.close()
    results["v4.db_mission_roundtrip"] = _timeit(db_roundtrip, iterations)

    repo = _v4_fixture(tmp)

    def security_scan():
        VulnerabilityScanner().scan(repo)
    results["v4.security_scan_fixture"] = _timeit(security_scan, 1)

    def research_analyze():
        analyze(repo)
    results["v4.research_analyze"] = _timeit(research_analyze, iterations)

    def reasoning_answer():
        from friday_v4.reasoning import answer
        conn = db.connect(tmp / "v4.db")
        # NOTE (Wave 19 slice 3): the engine signature is
        # ``answer(question_text, conn=None, ...)`` — the old call
        # ``answer(conn, text)`` passed the connection as the question
        # and raised AttributeError (silently caught → "n/a").
        answer("what's the status of my projects", conn=conn)
        conn.close()
    results["v4.reasoning_answer"] = _timeit(reasoning_answer, iterations)

    def collab_merge():
        from friday_v4.collab.coordinator import Coordinator
        coord = Coordinator(peer_id="bench", state_dir=tmp / "collab")
        # NOTE (Wave 19 slice 3): the CRDT's wire shape is
        # ``{id, peer_id, ts, payload, deleted}`` — the old batch used
        # the *display* shape (source/subject/timestamp) which made
        # ``state()`` raise KeyError('ts') (silently caught → "n/a").
        coord.merge_entries([
            {"id": f"o{i}", "peer_id": "p", "ts": 1000 + i,
             "payload": {"source": "git", "subject": "repo",
                          "aspect": "commits", "value": str(i)},
             "deleted": False}
            for i in range(20)])
    results["v4.collab_merge_20"] = _timeit(collab_merge, iterations)

    def ambient_publish():
        from friday_v4.ambient import AmbientBus, Event
        conn = db.connect(tmp / "v4.db")
        bus = AmbientBus(conn)
        bus.publish(Event("bench", "x", source="bench"))
        conn.close()
    results["v4.ambient_publish"] = _timeit(ambient_publish, iterations)

    return {k: (round(v, 2) if v is not None else None)
            for k, v in results.items()}


# ── V3 measurements (optional heritage comparison) ──────────────────────


def v3_available() -> bool:
    try:
        import friday  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def v3_benchmarks(tmp: Path, iterations: int = 3) -> dict:
    """Comparable V3 measurements when the legacy package is installed."""
    if not v3_available():
        return {}
    results: dict = {}

    def v3_connect():
        import friday.db  # type: ignore
        friday.db.connect()
    results["v3.db_connect"] = _timeit(v3_connect, iterations)

    repo = _v4_fixture(tmp)

    def v3_scan():
        from friday.security import scanner  # type: ignore
        scanner.scan(repo)
    results["v3.security_scan_fixture"] = _timeit(v3_scan, 1)

    return {k: (round(v, 2) if v is not None else None)
            for k, v in results.items()}


# ── CLI ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks",
                                     description="Friday V4 benchmarks")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        v4 = v4_benchmarks(tmp, args.iterations)
        v3 = v3_benchmarks(tmp, args.iterations) if v3_available() else {}

    if args.json:
        print(json.dumps({"v4": v4, "v3": v3}, indent=2, default=str))
        return 0

    print("\n  Friday V4 — benchmarks (best of N runs, ms)\n")
    for name, ms in sorted(v4.items()):
        print(f"  {name:<32} {ms if ms is not None else 'n/a'}")
    if v3:
        print("\n  V3 (legacy, optional comparison)\n")
        for name, ms in sorted(v3.items()):
            print(f"  {name:<32} {ms if ms is not None else 'n/a'}")
    else:
        print("\n  V3 not importable — V4-only run (heritage optional)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
