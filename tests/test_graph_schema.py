"""Tests for the FROZEN Task Graph JSON schema (Milestone 9.1+ contract).

This is the stable public interface consumed by every downstream execution
component (Worker Registry, Capability Resolver, Scheduler, Runtime, Review,
Repair, external integrations). The compiler and Planning are FROZEN and
untouched — this module validates the JSON the compiler emits.

The tests enforce the boundary:
- The schema vocabulary EXACTLY matches the compiler's enums (parity), so the
  contract cannot silently drift from what the compiler produces.
- A real compiler export round-trips through validate_task_graph / load_task_graph.
- Malformed JSON (missing id, unknown task_type/capability, dangling edge, non-
  acyclic, wrong schema_version) is REJECTED.
"""

from __future__ import annotations

import copy
import json

import pytest

from src.friday.planning.compiler import (
    TaskType, _ALL_CAPS, _COMPLEXITY_ORDER, _PRIORITY_ORDER)
from src.friday.planning.graph_schema import (
    SCHEMA_VERSION, TASK_TYPES, CAPABILITIES, PRIORITIES, COMPLEXITIES,
    EFFORTS, TASK_STATUSES, CONFIDENCES, EDGE_KINDS,
    SchemaError, validate_task_graph, load_task_graph, _detect_cycle)

from tests.test_graph_dogfood import _seed  # reuse the live-seed fixture setup
import sqlite3
from src.friday.db import SCHEMA, _migrate
from src.friday.planning import TaskGraphEngine


# --------------------------------------------------------------------------
# vocabulary parity: the schema's closed sets MUST equal the compiler's
# --------------------------------------------------------------------------

def test_task_type_parity():
    assert set(TASK_TYPES) == set(TaskType.all())


def test_capability_parity():
    assert set(CAPABILITIES) == set(_ALL_CAPS)


def test_priority_parity():
    assert set(PRIORITIES) == set(_PRIORITY_ORDER)


def test_complexity_parity():
    assert set(COMPLEXITIES) == set(_COMPLEXITY_ORDER)


def test_schema_version_constant():
    assert SCHEMA_VERSION == 1


# --------------------------------------------------------------------------
# real compiler export validates clean
# --------------------------------------------------------------------------

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    _seed(conn)
    yield conn
    conn.close()


@pytest.fixture
def oauth_export(db):
    g = TaskGraphEngine(db).generate("Implement OAuth")
    return g.to_json()


def test_real_export_validates(db):
    for goal in ("Implement OAuth", "Refactor authentication",
                 "Extract shared Rust crates", "Build worker system",
                 "Improve Vivaha architecture"):
        g = TaskGraphEngine(db).generate(goal)
        # must not raise
        validate_task_graph(g.to_json())


def test_real_export_loads(oauth_export):
    sg = load_task_graph(oauth_export)
    assert sg.graph_id == oauth_export["graph_id"]
    assert len(sg.tasks) == oauth_export["task_count"]
    assert sg.tasks[0].id in {t["id"] for t in oauth_export["tasks"]}


def test_load_is_typed_view(oauth_export):
    sg = load_task_graph(oauth_export)
    t = sg.tasks[0]
    assert isinstance(t.required_capabilities, list)
    assert isinstance(t.dependencies, list)
    # every task carries the mandatory structured fields
    for t in sg.tasks:
        assert t.task_type in TASK_TYPES
        assert t.acceptance_criteria
        assert t.verification
        assert t.rollback


def test_round_trip_json_string(oauth_export):
    s = json.dumps(oauth_export)
    loaded = load_task_graph(json.loads(s))  # must not raise after re-parse
    assert loaded.task_count == oauth_export["task_count"]


# --------------------------------------------------------------------------
# rejection of malformed graphs
# --------------------------------------------------------------------------

def test_reject_unknown_task_type(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][0]["task_type"] = "time_travel"
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_unknown_capability(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][0]["required_capabilities"].append("kubernetes_operator")
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_unknown_priority(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][0]["priority"] = "urgent"
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_dangling_edge(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["edges"].append({"from": bad["tasks"][0]["id"],
                         "to": "taskgraph:plan:nonexistent#t99",
                         "kind": "depends_on"})
    # edge_count no longer matches -> caught; also to-unknown task caught
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_duplicate_task_id(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][1]["id"] = bad["tasks"][0]["id"]
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_non_acyclic(oauth_export):
    bad = copy.deepcopy(oauth_export)
    # Add a back-edge root->leaf to close a cycle.
    ids = [t["id"] for t in bad["tasks"]]
    bad["edges"].append({"from": ids[0], "to": ids[-1], "kind": "depends_on"})
    bad["edge_count"] = len(bad["edges"])
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_wrong_schema_version(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["metadata"]["schema_version"] = 999
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_missing_top_level_key(oauth_export):
    bad = copy.deepcopy(oauth_export)
    del bad["goal"]
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_acyclic_false(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["metadata"]["acyclic"] = False
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_empty_acceptance_criteria(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][0]["acceptance_criteria"] = []
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_unknown_edge_kind(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["edges"][0]["kind"] = "blocks"
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_unknown_task_status(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][0]["status"] = "mysterious"
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_count_mismatch(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["task_count"] = bad["task_count"] + 1  # lies about count
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


def test_reject_task_graph_id_mismatch(oauth_export):
    bad = copy.deepcopy(oauth_export)
    bad["tasks"][0]["graph_id"] = "taskgraph:someone-else"
    with pytest.raises(SchemaError):
        validate_task_graph(bad)


# --------------------------------------------------------------------------
# cycle detector parity with compiler
# --------------------------------------------------------------------------

def test_schema_cycle_detector_acyclic(oauth_export):
    ids = [t["id"] for t in oauth_export["tasks"]]
    assert _detect_cycle(oauth_export["edges"], ids) is False


def test_schema_cycle_detector_cyclic():
    ids = ["a", "b", "c"]
    cyc = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
           {"from": "c", "to": "a"}]
    assert _detect_cycle(cyc, ids) is True


# --------------------------------------------------------------------------
# downstream consumer reads ONLY the schema (no compiler import)
# --------------------------------------------------------------------------

def test_consumer_needs_only_schema_module():
    import importlib
    # graph_schema must not import the compiler (boundary integrity).
    mod = importlib.import_module("src.friday.planning.graph_schema")
    mod_file = mod.__file__ or ""
    src_text = open(mod_file).read()
    assert "from .compiler" not in src_text
    assert "import compiler" not in src_text
