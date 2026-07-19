"""Deterministic capability benchmark (M10). Compares workers at the CAPABILITY
level (pass/fail + duration) -- NOT a "smartest AI" score. CLI-agnostic: the CLI
just calls BenchmarkRunner. Friday can later invoke it automatically (e.g. when
it notices a worker slowed down) -- same code."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Callable, List


@dataclass
class BenchmarkTask:
    capability: str
    payload: str
    expect_nonempty_stdout: bool = True


@dataclass
class BenchmarkResult:
    worker: str
    passed: bool
    duration_ms: int
    detail: str = ""


class BenchmarkRunner:
    def __init__(self, tasks: List[BenchmarkTask],
                 workers: List[tuple]) -> None:
        # workers: [(worker_id, callable(payload)->(stdout, exit_code))]
        self.tasks = tasks
        self.workers = workers

    def run(self) -> dict:
        out: dict = {}
        for task in self.tasks:
            rows = []
            for wid, fn in self.workers:
                t0 = time.monotonic()
                try:
                    stdout, code = fn(task.payload)
                except Exception as e:
                    dur = int((time.monotonic() - t0) * 1000)
                    rows.append(BenchmarkResult(
                        worker=wid, passed=False, duration_ms=dur,
                        detail=f"error: {type(e).__name__}: {e}"))
                    continue
                dur = int((time.monotonic() - t0) * 1000)
                passed = (code == 0) and (
                    not task.expect_nonempty_stdout or bool((stdout or "").strip()))
                rows.append(BenchmarkResult(
                    worker=wid, passed=passed, duration_ms=dur,
                    detail=f"exit={code}"))
            out[task.capability] = rows
        return out
