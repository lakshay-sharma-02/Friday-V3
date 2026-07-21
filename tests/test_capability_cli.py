import argparse
import pytest
from friday.cli_capability import cmd_capability
from friday.db import connect
from friday.worker.engine import WorkerRegistry


def _args(token, worker=None):
    return argparse.Namespace(token=token, worker=worker)


def test_capability_list_prints_workers(capsys):
    conn = connect(":memory:")
    reg = WorkerRegistry(conn)
    reg.register_from_manifest({
        "name": "Claude Code", "kind": "cli", "implementation": "cli",
        "provider": "anthropic", "origin": "external", "id": "worker:claude",
        "capabilities": ["Refactoring"], "requirements": ["claude"],
        "supported_task_types": ["refactor"], "supported_plan_types": ["feature"]})
    rc = cmd_capability(_args("list"), conn=conn)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Claude Code" in out


def test_capability_info_prints_availability(capsys):
    conn = connect(":memory:")
    reg = WorkerRegistry(conn)
    reg.register_from_manifest({
        "name": "Claude Code", "kind": "cli", "implementation": "cli",
        "provider": "anthropic", "origin": "external", "id": "worker:claude",
        "capabilities": ["Refactoring"], "requirements": ["claude"],
        "supported_task_types": ["refactor"], "supported_plan_types": ["feature"]})
    rc = cmd_capability(_args("info", worker="Claude Code"), conn=conn)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Claude Code" in out
    assert "Availability" in out


def test_capability_benchmark_runs(capsys):
    conn = connect(":memory:")
    rc = cmd_capability(_args("benchmark"), conn=conn)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Documentation" in out
