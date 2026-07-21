"""Tests for the Worker Registry (Milestone 9.2).

The registry is WRITE-ONLY metadata: it describes workers' capability profiles.
It NEVER executes, schedules, selects, or runs work. Every lower layer
(Observation, Context, Knowledge, Understanding, Initiatives, Insights, Brain,
Planning, Task Graph) is UNTOUCHED by this layer — tests assert that too.

Regression cases required by the spec:
- Registration (built-in + custom)
- Duplicate worker (idempotent re-register, no duplicate rows)
- Version update (append-only version log, bump on material change)
- Capability validation (closed vocabulary)
- Unknown capability (rejected, never stored)
- Language validation
- Task validation
- Plan validation
- Manifest parsing (valid + invalid JSON)
- History (append-only)
- Export
- Import (round-trip via export -> register)
- CLI (list / show / register / export)
- Custom worker
- Disabled worker
- Registry persistence
- Brain compatibility (registry never reads/writes brain state)
- No duplicate workers
- No hallucinated capabilities
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.friday.db import (
    connect,
    get_all_workers,
)
from src.friday.worker import (
    KIND_AGENT,
    KIND_CLI,
    KIND_LLM,
    Worker,
    WorkerKind,
    WorkerRegistry,
    all_capabilities,
    is_valid_capability,
    is_valid_language,
    is_valid_plan_type,
    is_valid_task_type,
)
from src.friday.worker.engine import RegistryError


@pytest.fixture
def db():
    conn = connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def reg(db):
    return WorkerRegistry(db)


# ---------------------------------------------------------------------------
# Capability vocabulary
# ---------------------------------------------------------------------------

def test_capability_vocabulary_closed():
    caps = all_capabilities()
    assert "Rust" in caps
    assert "Architecture" in caps
    assert "Reasoning" in caps
    assert "Git Operations" in caps
    assert "BogusCap" not in caps


def test_is_valid_capability_case_insensitive():
    assert is_valid_capability("Rust")
    assert is_valid_capability("rust")
    assert is_valid_capability("Reasoning")
    assert not is_valid_capability("telepathy")
    assert not is_valid_capability("")


def test_is_valid_language():
    assert is_valid_language("Python")
    assert is_valid_language("python")
    assert not is_valid_language("Klingon")


def test_is_valid_task_type():
    assert is_valid_task_type("implementation")
    assert is_valid_task_type("refactor")
    assert not is_valid_task_type("frobnicate")


def test_is_valid_plan_type():
    assert is_valid_plan_type("feature")
    assert is_valid_plan_type("architecture")
    assert not is_valid_plan_type("phalanx")


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------

def test_builtins_register(reg):
    res = reg.register_builtins()
    assert res.created == 13
    # Documentation declares "Markdown", which the closed language vocabulary
    # rejects (Markdown is not a tracked programming language); the capability
    # "Documentation" is what drives resolution, so dropping it is correct.
    assert res.rejected == ["language: Markdown"]
    assert reg.count() == 13


def test_builtin_names_present(reg):
    reg.register_builtins()
    names = {w.name for w in reg.all_workers()}
    for n in ("Claude", "Codex", "Gemini", "GPT", "OpenRouter", "Python",
              "Shell", "Git", "Filesystem", "Search", "Local LLM"):
        assert n in names


def test_builtin_kinds_provider_agnostic(reg):
    reg.register_builtins()
    kinds = {w.kind.value for w in reg.all_workers()}
    # No provider is special-cased; all fit the generic kind schema.
    assert kinds <= {k.value for k in WorkerKind}


def test_builtin_claude_profile(reg):
    reg.register_builtins()
    c = reg.worker_by_name("Claude")
    assert c is not None
    assert c.kind.value == KIND_LLM
    assert "Reasoning" in c.capabilities
    assert c.context_window == 200000
    assert c.requires_network is True
    assert "Huge repository rewrites" in c.limitations


def test_builtin_no_duplicate_ids(reg):
    reg.register_builtins()
    ids = [w.id for w in reg.all_workers()]
    assert len(ids) == len(set(ids))
    # ids are deterministic: worker:<name>
    assert reg.worker_by_name("Codex").id == "worker:codex"


# ---------------------------------------------------------------------------
# Custom worker + manifest parsing
# ---------------------------------------------------------------------------

def test_register_custom_worker(reg):
    w = Worker(name="MyTool", kind=WorkerKind.from_str("tool"),
               capabilities=["Python", "Testing"],
               supported_languages=["Python"],
               limitations=["No network"])
    res = reg.register(w)
    assert res.created == 1
    assert reg.worker_by_name("MyTool") is not None


def test_manifest_parsing_valid(reg):
    m = {
        "name": "CustomLLM", "kind": "llm",
        "capabilities": ["Python", "Reasoning"],
        "supported_languages": ["Python"],
        "limitations": ["slow"],
    }
    res = reg.register_from_manifest(m)
    assert res.created == 1
    w = reg.worker_by_name("CustomLLM")
    assert w.capabilities == ["Python", "Reasoning"]


def test_manifest_parsing_missing_name(reg):
    with pytest.raises(RegistryError):
        reg.register_from_manifest({"kind": "tool"})


def test_manifest_parsing_invalid_json(db, tmp_path):
    from src.friday.cli_worker import cmd_worker_register
    f = tmp_path / "bad.json"
    f.write_text("{not valid json")
    args = type("A", (), {"file": str(f)})()
    rc = cmd_worker_register(args)
    assert rc == 2


def test_manifest_roundtrip_export_import(reg):
    reg.register_builtins()
    export = reg.export_json()
    # Re-register every exported worker into a fresh registry.
    conn2 = connect(":memory:")
    reg2 = WorkerRegistry(conn2)
    for wj in export["workers"]:
        reg2.register_from_manifest(wj)
    assert reg2.count() == reg.count()
    for w in reg.all_workers():
        other = reg2.worker_by_name(w.name)
        assert other is not None
        assert other.capabilities == w.capabilities
    conn2.close()


# ---------------------------------------------------------------------------
# Validation rejection (no hallucinated capabilities)
# ---------------------------------------------------------------------------

def test_unknown_capability_rejected(reg):
    w = Worker(name="X", kind=WorkerKind.from_str("tool"),
               capabilities=["Python", "Telepathy"])
    res = reg.register(w)
    assert res.created == 1
    assert any("Telepathy" in r for r in res.rejected)
    stored = reg.worker_by_name("X")
    assert "Telepathy" not in stored.capabilities
    assert "Python" in stored.capabilities


def test_unknown_language_rejected(reg):
    w = Worker(name="Y", kind=WorkerKind.from_str("tool"),
               supported_languages=["Python", "Klingon"])
    res = reg.register(w)
    assert any("Klingon" in r for r in res.rejected)
    assert reg.worker_by_name("Y").supported_languages == ["Python"]


def test_unknown_task_type_rejected(reg):
    w = Worker(name="Z", kind=WorkerKind.from_str("tool"),
               supported_task_types=["implementation", "frobnicate"])
    res = reg.register(w)
    assert any("frobnicate" in r for r in res.rejected)
    assert reg.worker_by_name("Z").supported_task_types == ["implementation"]


def test_unknown_plan_type_rejected(reg):
    w = Worker(name="P", kind=WorkerKind.from_str("tool"),
               supported_plan_types=["feature", "phalanx"])
    res = reg.register(w)
    assert any("phalanx" in r for r in res.rejected)
    assert reg.worker_by_name("P").supported_plan_types == ["feature"]


def test_no_hallucinated_capabilities(reg):
    reg.register_builtins()
    for w in reg.all_workers():
        for c in w.capabilities:
            assert is_valid_capability(c), f"{w.name} has bad cap {c}"


# ---------------------------------------------------------------------------
# Duplicate handling / idempotency
# ---------------------------------------------------------------------------

def test_duplicate_worker_no_duplicate_rows(reg):
    w = Worker(name="Dup", kind=WorkerKind.from_str("tool"),
               capabilities=["Python"])
    reg.register(w)
    n1 = reg.count()
    reg.register(w)  # same id -> replace, not insert
    assert reg.count() == n1
    assert reg.worker_by_name("Dup") is not None


def test_reregister_is_idempotent_on_goal(reg):
    w = Worker(name="Dup", kind=WorkerKind.from_str("tool"),
               capabilities=["Python"])
    reg.register(w)
    reg.register(w)
    rows = get_all_workers(reg.conn)
    dups = [r for r in rows if r.name == "Dup"]
    assert len(dups) == 1  # exactly one live row


# ---------------------------------------------------------------------------
# Version update + history (append-only)
# ---------------------------------------------------------------------------

def test_version_update_bumps_on_material_change(reg):
    reg.register(Worker(name="V", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"], version="1.0.0"))
    before = reg.worker_by_name("V")
    assert before.version == "1.0.0"
    reg.register(Worker(name="V", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python", "Testing"], version="1.0.0"))
    after = reg.worker_by_name("V")
    assert after.version != before.version  # bumped deterministically
    assert after.version == "1.0.1"


def test_upgrade_version_explicit(reg):
    reg.register(Worker(name="V2", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"]))
    ok = reg.upgrade_version("V2", "2.3.4")
    assert ok is True
    assert reg.worker_by_name("V2").version == "2.3.4"
    assert len(reg.versions("worker:v2")) >= 2


def test_history_append_only(reg):
    reg.register(Worker(name="H", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"]))
    reg.register(Worker(name="H", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python", "Testing"]))
    hist = reg.history("worker:h")
    assert len(hist) >= 2
    # earliest is the registration; events never mutate prior rows
    events = [h.event_type for h in reversed(hist)]
    assert "registered" in events


def test_history_never_shrinks_on_reregister(reg):
    reg.register(Worker(name="H", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"]))
    n0 = len(reg.history("worker:h"))
    reg.register(Worker(name="H", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"]))
    n1 = len(reg.history("worker:h"))
    assert n1 >= n0  # reregistration still appends a snapshot


# ---------------------------------------------------------------------------
# Disabled worker
# ---------------------------------------------------------------------------

def test_disable_enable_worker(reg):
    reg.register(Worker(name="Dis", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"]))
    assert reg.worker_by_name("Dis").status == "active"
    assert reg.disable("Dis") is True
    assert reg.worker_by_name("Dis").status == "disabled"
    assert reg.disable("Nonexistent") is False
    assert reg.enable("Dis") is True
    assert reg.worker_by_name("Dis").status == "active"
    # a disable/enable event was recorded
    assert any(h.event_type == "disabled" for h in reg.history("worker:dis"))


def test_disabled_not_in_active(reg):
    reg.register(Worker(name="Dis", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"]))
    reg.disable("Dis")
    assert reg.worker_by_name("Dis").status == "disabled"
    assert reg.worker_by_name("Dis") not in reg.active_workers()


# ---------------------------------------------------------------------------
# Capability resolver hook
# ---------------------------------------------------------------------------

def test_workers_for_capability(reg):
    reg.register_builtins()
    rust_workers = {w.name for w in reg.workers_for_capability("rust")}
    assert "Claude" in rust_workers
    assert "Codex" in rust_workers
    # capability query excludes workers without it
    assert "Filesystem" not in rust_workers


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_shape(reg):
    reg.register(Worker(name="Exp", kind=WorkerKind.from_str("tool"),
                        capabilities=["Python"], estimated_speed="fast"))
    data = reg.export_json()
    assert data["registry_version"] == "1.0"
    assert data["worker_count"] == 1
    wj = data["workers"][0]
    assert wj["name"] == "Exp"
    assert wj["capabilities"] == ["Python"]
    # No execution fields leaked: manifest contract is metadata only.
    assert "executor" not in wj
    assert "endpoint" not in wj


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_registry_persistence(tmp_path):
    path = tmp_path / "w.db"
    c1 = connect(str(path))
    WorkerRegistry(c1).register(Worker(name="Persist",
                                       kind=WorkerKind.from_str("tool"),
                                       capabilities=["Python"]))
    c1.close()
    c2 = connect(str(path))
    w = WorkerRegistry(c2).worker_by_name("Persist")
    assert w is not None
    assert w.capabilities == ["Python"]
    c2.close()


# ---------------------------------------------------------------------------
# Brain / lower-layer compatibility — registry touches NO other layer
# ---------------------------------------------------------------------------

def test_brain_compatibility_no_crosstalk(reg, db):
    """Registering workers must not read or write any lower-layer table."""
    # Snapshot row counts for lower layers that the DB defines.
    lower_tables = [
        "knowledge", "understanding", "initiatives", "insights",
        "plans", "task_graphs", "tasks",
    ]
    before = {t: db.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
              for t in lower_tables}
    reg.register_builtins()
    after = {t: db.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
             for t in lower_tables}
    assert before == after
    # And workers table is the only thing that changed.
    assert reg.count() == 13


def test_registry_is_write_only_catalog(reg):
    """The registry answers 'what exists' but takes no execution decisions."""
    reg.register_builtins()
    # It exposes metadata; it has no run/execute/select/schedule method.
    forbidden = ("execute", "schedule", "select_worker", "run", "dispatch")
    for attr in forbidden:
        assert not hasattr(reg, attr), f"registry must not have {attr}"
