"""SQLite storage for Friday's knowledge base.

Schema is deliberately flat: relationships and cross-project observations are
re-derived at summary time from stored rows, so we never persist derived pairs.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


def db_path() -> Path:
    override = os.environ.get("FRIDAY_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".friday" / "friday.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    default_branch  TEXT,
    is_dirty        INTEGER NOT NULL DEFAULT 0,
    first_commit_date TEXT,
    last_commit_date TEXT,
    remote_url      TEXT,
    commit_count    INTEGER,
    readme_summary  TEXT,
    license         TEXT,
    primary_author  TEXT,
    ingestion_time  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS languages (
    repo_id     INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    language    TEXT NOT NULL,
    file_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, language)
);

CREATE TABLE IF NOT EXISTS technologies (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    tech      TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    PRIMARY KEY (repo_id, tech)
);

CREATE TABLE IF NOT EXISTS relationships (
    id       INTEGER PRIMARY KEY,
    repo_a   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    repo_b   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    evidence TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    strength TEXT NOT NULL DEFAULT 'Medium'
);

CREATE TABLE IF NOT EXISTS architecture (
    repo_id         INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    architecture    TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    data_flow       TEXT,
    known_patterns  TEXT,
    complexity      TEXT,
    confidence      TEXT
);

CREATE TABLE IF NOT EXISTS components (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    strength  TEXT NOT NULL DEFAULT 'Medium',
    PRIMARY KEY (repo_id, name)
);

CREATE TABLE IF NOT EXISTS entry_points (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    PRIMARY KEY (repo_id, kind, detail)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY,
    observed_at      TEXT NOT NULL,
    repo_path        TEXT NOT NULL,
    repo_name        TEXT,
    default_branch   TEXT,
    commit_count     INTEGER,
    last_commit_date TEXT,
    is_dirty         INTEGER NOT NULL DEFAULT 0,
    readme_hash      TEXT,
    architecture_hash TEXT,
    identity_hash    TEXT,
    head_sha         TEXT,
    manifest_hash    TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id          TEXT NOT NULL PRIMARY KEY,
    observed_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    aspect      TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '',
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    repositories    TEXT NOT NULL,
    primary_repo    TEXT,
    observations    TEXT NOT NULL,
    activity        TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    duration_min    REAL NOT NULL,
    branch          TEXT,
    summary         TEXT,
    built_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_verified       TEXT,
    verification_count  INTEGER NOT NULL DEFAULT 0,
    is_static           INTEGER NOT NULL DEFAULT 0,
    schema_version      TEXT NOT NULL DEFAULT '1.0'
);

-- M8.2: Knowledge Evolution. Append-only. History is never mutated.
-- One full snapshot of every knowledge entry as it stood after a build.
CREATE TABLE IF NOT EXISTS knowledge_history (
    build_at            TEXT NOT NULL,
    knowledge_id        TEXT NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    verification_count  INTEGER NOT NULL DEFAULT 0,
    is_static            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_at, knowledge_id)
);

-- M8.2: deterministic evolution events derived from history diffs.
-- Every record references: knowledge id, previous version, new version,
-- evidence ids, timestamp, reason. Append-only.
CREATE TABLE IF NOT EXISTS evolution_events (
    id                  TEXT PRIMARY KEY,
    build_at            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    knowledge_id        TEXT NOT NULL,
    previous_confidence TEXT,
    new_confidence      TEXT,
    previous_status     TEXT,
    new_status          TEXT,
    previous_statement  TEXT,
    new_statement       TEXT,
    reason              TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL DEFAULT '',
    related_ids         TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);

-- M8.3: Understanding Engine. Write-only layer on top of Knowledge. NEVER
-- reads observations/context directly. Every understanding cites knowledge ids.
-- Append-only history + evolution, mirroring knowledge_history/evolution_events.
CREATE TABLE IF NOT EXISTS understanding (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    status              TEXT NOT NULL,
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    build_at            TEXT NOT NULL,
    retired_at          TEXT,
    schema_version      TEXT NOT NULL DEFAULT '1.0'
);

-- One append-only snapshot of every understanding per build. Never mutated.
CREATE TABLE IF NOT EXISTS understanding_history (
    build_at            TEXT NOT NULL,
    understanding_id    TEXT NOT NULL REFERENCES understanding(id) ON DELETE CASCADE,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    status              TEXT NOT NULL,
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    reinforced_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_at, understanding_id)
);

-- Deterministic evolution events derived from understanding history diffs.
CREATE TABLE IF NOT EXISTS understanding_evolution (
    id                  TEXT PRIMARY KEY,
    build_at            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    understanding_id    TEXT NOT NULL REFERENCES understanding(id) ON DELETE CASCADE,
    previous_confidence TEXT,
    new_confidence      TEXT,
    previous_status     TEXT,
    new_status          TEXT,
    previous_statement  TEXT,
    new_statement       TEXT,
    reason              TEXT NOT NULL,
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);

-- M8.4: Initiative Engine. Write-only layer on top of Understanding. NEVER
-- reads observations/context/repositories directly. Every initiative cites
-- understanding ids (and knowledge ids). Append-only history + evolution +
-- relationships (merge/split), mirroring the understanding tables.
CREATE TABLE IF NOT EXISTS initiatives (
    id                          TEXT PRIMARY KEY,
    title                       TEXT NOT NULL,
    initiative_type             TEXT NOT NULL,
    status                      TEXT NOT NULL,
    confidence                  TEXT NOT NULL,
    statement                   TEXT NOT NULL DEFAULT '',
    started_at                  TEXT,
    updated_at                  TEXT NOT NULL,
    completed_at                TEXT,
    participating_repositories   TEXT NOT NULL DEFAULT '',
    understanding_ids           TEXT NOT NULL DEFAULT '',
    knowledge_ids               TEXT NOT NULL DEFAULT '',
    build_at                    TEXT NOT NULL,
    created_at                  TEXT NOT NULL DEFAULT '',
    schema_version              TEXT NOT NULL DEFAULT '1.0'
);

-- One append-only snapshot of every initiative per build. Never mutated.
CREATE TABLE IF NOT EXISTS initiative_history (
    build_at               TEXT NOT NULL,
    initiative_id          TEXT NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE,
    title                  TEXT NOT NULL,
    initiative_type        TEXT NOT NULL,
    status                 TEXT NOT NULL,
    confidence             TEXT NOT NULL,
    started_at             TEXT,
    completed_at           TEXT,
    participating_repositories TEXT NOT NULL DEFAULT '',
    understanding_ids      TEXT NOT NULL DEFAULT '',
    knowledge_ids          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (build_at, initiative_id)
);

-- Deterministic lifecycle / merge / split events derived from history diffs.
CREATE TABLE IF NOT EXISTS initiative_evolution (
    id                  TEXT PRIMARY KEY,
    build_at            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    initiative_id       TEXT NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE,
    parent_ids          TEXT NOT NULL DEFAULT '',
    child_ids           TEXT NOT NULL DEFAULT '',
    previous_status     TEXT,
    new_status          TEXT,
    previous_confidence TEXT,
    new_confidence      TEXT,
    previous_title      TEXT,
    new_title           TEXT,
    reason              TEXT NOT NULL,
    understanding_ids   TEXT NOT NULL DEFAULT '',
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);

-- Explicit merge/split edges. Parent/child references preserved forever.
CREATE TABLE IF NOT EXISTS initiative_relationships (
    id                  TEXT PRIMARY KEY,
    relationship_type    TEXT NOT NULL,   -- 'merge' or 'split'
    parent_ids          TEXT NOT NULL DEFAULT '',
    child_ids           TEXT NOT NULL DEFAULT '',
    build_at            TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    note                TEXT
);

-- M8.5: Insight Engine. Write-only layer on top of Understanding/Initiatives/
-- Knowledge. NEVER reads observations/context/repositories directly. Every
-- insight cites understanding ids (and/or initiative ids and/or knowledge ids).
-- Append-only history + evolution, mirroring the understanding/insight tables.
-- Insights are EPHEMERAL: a build retires insights whose triggering conditions
-- no longer hold, so the layer stays a live "what deserves attention" feed.
CREATE TABLE IF NOT EXISTS insights (
    id                      TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    insight_type            TEXT NOT NULL,
    statement               TEXT NOT NULL,
    status                  TEXT NOT NULL,
    confidence              TEXT NOT NULL,
    started_at              TEXT,
    updated_at              TEXT NOT NULL,
    retired_at              TEXT,
    understanding_ids       TEXT NOT NULL DEFAULT '',
    initiative_ids          TEXT NOT NULL DEFAULT '',
    knowledge_ids           TEXT NOT NULL DEFAULT '',
    build_at                TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT '',
    schema_version          TEXT NOT NULL DEFAULT '1.0'
);

-- One append-only snapshot of every insight per build. Never mutated.
CREATE TABLE IF NOT EXISTS insight_history (
    build_at                TEXT NOT NULL,
    insight_id              TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
    title                   TEXT NOT NULL,
    insight_type            TEXT NOT NULL,
    statement               TEXT NOT NULL,
    status                  TEXT NOT NULL,
    confidence              TEXT NOT NULL,
    understanding_ids       TEXT NOT NULL DEFAULT '',
    initiative_ids          TEXT NOT NULL DEFAULT '',
    knowledge_ids           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (build_at, insight_id)
);

-- Deterministic lifecycle (Candidate->Observed->Verified->Stable->Retired) and
-- retirement events derived from build diffs. Append-only.
CREATE TABLE IF NOT EXISTS insight_evolution (
    id                      TEXT PRIMARY KEY,
    build_at                TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    insight_id              TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
    previous_status         TEXT,
    new_status              TEXT,
    previous_confidence     TEXT,
    new_confidence          TEXT,
    previous_statement      TEXT,
    new_statement           TEXT,
    reason                  TEXT NOT NULL,
    understanding_ids       TEXT NOT NULL DEFAULT '',
    initiative_ids          TEXT NOT NULL DEFAULT '',
    knowledge_ids           TEXT NOT NULL DEFAULT '',
    timestamp               TEXT NOT NULL
);

-- M9.0: Planning Engine. Write-only layer on TOP of Insights/Initiatives/
-- Understanding/Knowledge. NEVER reads observations/context/repositories/git
-- directly. NEVER executes, edits files, or calls workers. Every plan cites
-- initiative ids (and/or insight ids and/or understanding ids and/or knowledge
-- ids). Append-only history + evolution, mirroring the insight tables. Plans
-- are structured (milestones/dependencies/risks/verification/rollback/evidence
-- references); only then rendered into human text. NEVER overloads initiatives.

CREATE TABLE IF NOT EXISTS plans (
    id                      TEXT PRIMARY KEY,
    goal                    TEXT NOT NULL,
    plan_type              TEXT NOT NULL,
    confidence              TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'planned',
    affected_initiative_ids TEXT NOT NULL DEFAULT '',
    affected_insight_ids   TEXT NOT NULL DEFAULT '',
    affected_understanding_ids TEXT NOT NULL DEFAULT '',
    affected_knowledge_ids TEXT NOT NULL DEFAULT '',
    milestones              TEXT NOT NULL DEFAULT '',
    dependencies            TEXT NOT NULL DEFAULT '',
    risks                   TEXT NOT NULL DEFAULT '',
    verification            TEXT NOT NULL DEFAULT '',
    rollback                TEXT NOT NULL DEFAULT '',
    estimated_complexity    TEXT NOT NULL DEFAULT '',
    estimated_effort        TEXT NOT NULL DEFAULT '',
    plan_text               TEXT NOT NULL DEFAULT '',
    schema_version          TEXT NOT NULL DEFAULT '1.0',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- One append-only snapshot of every plan per generation. Never mutated.
CREATE TABLE IF NOT EXISTS plan_history (
    generated_at           TEXT NOT NULL,
    plan_id                TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    goal                   TEXT NOT NULL,
    plan_type              TEXT NOT NULL,
    confidence             TEXT NOT NULL,
    status                 TEXT NOT NULL,
    affected_initiative_ids TEXT NOT NULL DEFAULT '',
    affected_insight_ids   TEXT NOT NULL DEFAULT '',
    affected_understanding_ids TEXT NOT NULL DEFAULT '',
    affected_knowledge_ids TEXT NOT NULL DEFAULT '',
    milestones              TEXT NOT NULL DEFAULT '',
    dependencies            TEXT NOT NULL DEFAULT '',
    risks                   TEXT NOT NULL DEFAULT '',
    verification            TEXT NOT NULL DEFAULT '',
    rollback                TEXT NOT NULL DEFAULT '',
    estimated_complexity    TEXT NOT NULL DEFAULT '',
    estimated_effort        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (generated_at, plan_id)
);

-- Deterministic lifecycle (Planned->Refined->Approved->Superseded) and
-- supersession events derived from plan diffs. Append-only.
CREATE TABLE IF NOT EXISTS plan_evolution (
    id                      TEXT PRIMARY KEY,
    generated_at            TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    plan_id                 TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    previous_status         TEXT,
    new_status              TEXT,
    previous_confidence     TEXT,
    new_confidence          TEXT,
    reason                  TEXT NOT NULL,
    affected_initiative_ids TEXT NOT NULL DEFAULT '',
    affected_insight_ids    TEXT NOT NULL DEFAULT '',
    affected_understanding_ids TEXT NOT NULL DEFAULT '',
    affected_knowledge_ids  TEXT NOT NULL DEFAULT '',
    timestamp               TEXT NOT NULL
);

-- M9.1: Task Graph Compiler. Write-only layer on TOP of the Planning Engine.
-- Compiles a structured Plan (milestones/dependencies/verification/rollback)
-- into a deterministic, acyclic task DAG that future Workers consume. NEVER
-- executes, edits files, or calls workers. NEVER reads observations/context/
-- git/repositories directly — input is a Plan object only. Append-only history
-- + evolution per graph, mirroring the plan tables. NEVER overloads plans.

CREATE TABLE IF NOT EXISTS task_graphs (
    id                      TEXT PRIMARY KEY,
    goal                    TEXT NOT NULL,
    plan_id                 TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    plan_type               TEXT NOT NULL,
    task_count              INTEGER NOT NULL DEFAULT 0,
    edge_count              INTEGER NOT NULL DEFAULT 0,
    critical_path_length    INTEGER NOT NULL DEFAULT 0,
    parallel_groups         INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'compiled',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                      TEXT PRIMARY KEY,
    graph_id                TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    plan_id                 TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    milestone_order         INTEGER NOT NULL DEFAULT 0,
    title                   TEXT NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    task_type               TEXT NOT NULL,
    required_capabilities   TEXT NOT NULL DEFAULT '',
    complexity              TEXT NOT NULL DEFAULT 'medium',
    priority                TEXT NOT NULL DEFAULT 'medium',
    estimated_effort        TEXT NOT NULL DEFAULT 'medium',
    dependencies            TEXT NOT NULL DEFAULT '',
    inputs                  TEXT NOT NULL DEFAULT '[]',
    outputs                 TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria     TEXT NOT NULL DEFAULT '[]',
    verification            TEXT NOT NULL DEFAULT '[]',
    rollback                TEXT NOT NULL DEFAULT '[]',
    evidence                TEXT NOT NULL DEFAULT '[]',
    status                  TEXT NOT NULL DEFAULT 'pending',
    confidence              TEXT NOT NULL DEFAULT 'medium',
    sequence                INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_edges (
    id                      TEXT PRIMARY KEY,
    graph_id                TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    from_task               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    to_task                 TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind                    TEXT NOT NULL DEFAULT 'depends_on'
);

CREATE TABLE IF NOT EXISTS task_history (
    generated_at           TEXT NOT NULL,
    graph_id               TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    goal                   TEXT NOT NULL,
    task_count             INTEGER NOT NULL DEFAULT 0,
    edge_count             INTEGER NOT NULL DEFAULT 0,
    critical_path_length   INTEGER NOT NULL DEFAULT 0,
    parallel_groups        INTEGER NOT NULL DEFAULT 0,
    tasks_json             TEXT NOT NULL DEFAULT '',
    edges_json             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (generated_at, graph_id)
);

CREATE TABLE IF NOT EXISTS task_evolution (
    id                      TEXT PRIMARY KEY,
    generated_at            TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    graph_id                TEXT NOT NULL,
    previous_status         TEXT,
    new_status              TEXT,
    reason                  TEXT NOT NULL,
    task_count              INTEGER NOT NULL DEFAULT 0,
    edge_count              INTEGER NOT NULL DEFAULT 0,
    timestamp               TEXT NOT NULL
);

-- M9.2: Worker Registry. WRITE-ONLY layer on TOP of the Task Graph Compiler.
-- Describes workers (capability profiles) and NOTHING else. NEVER executes,
-- schedules, selects, or runs work. Append-only history + version log. NEVER
-- overloads the Task Graph. Dedicated tables; every lower layer unchanged.
-- Provider-agnostic from day one: workers are generic capability profiles
-- (kind = llm/cli/function/agent/tool/service), not special-cased providers.

CREATE TABLE IF NOT EXISTS workers (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    capabilities            TEXT NOT NULL DEFAULT '',
    supported_languages     TEXT NOT NULL DEFAULT '',
    supported_task_types    TEXT NOT NULL DEFAULT '',
    supported_plan_types    TEXT NOT NULL DEFAULT '',
    limitations             TEXT NOT NULL DEFAULT '',
    estimated_speed         TEXT NOT NULL DEFAULT '',
    estimated_cost          TEXT NOT NULL DEFAULT '',
    context_window          INTEGER NOT NULL DEFAULT 0,
    parallelism             INTEGER NOT NULL DEFAULT 1,
    requires_network        INTEGER NOT NULL DEFAULT 0,
    requires_filesystem     INTEGER NOT NULL DEFAULT 0,
    requires_git            INTEGER NOT NULL DEFAULT 0,
    requires_python         INTEGER NOT NULL DEFAULT 0,
    requires_shell          INTEGER NOT NULL DEFAULT 0,
    confidence              TEXT NOT NULL DEFAULT 'medium',
    version                 TEXT NOT NULL DEFAULT '1.0.0',
    status                  TEXT NOT NULL DEFAULT 'active',
    schema_version          TEXT NOT NULL DEFAULT '1.0',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    availability            TEXT NOT NULL DEFAULT 'available',
    manifest_ref            TEXT
);

-- Normalized one-row-per-(worker,capability) so the future Capability Resolver
-- can query "which workers have capability X" without parsing a joined string.
CREATE TABLE IF NOT EXISTS worker_capabilities (
    worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    capability              TEXT NOT NULL,
    PRIMARY KEY (worker_id, capability)
);

-- Append-only snapshot of every worker per registration event. Never mutated.
CREATE TABLE IF NOT EXISTS worker_history (
    registered_at           TEXT NOT NULL,
    worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    name                    TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    version                 TEXT NOT NULL,
    status                  TEXT NOT NULL,
    capabilities            TEXT NOT NULL DEFAULT '',
    limitations             TEXT NOT NULL DEFAULT '',
    event_type              TEXT NOT NULL,
    note                    TEXT,
    PRIMARY KEY (registered_at, worker_id)
);

-- Append-only per-version log (version upgrades recorded forever).
CREATE TABLE IF NOT EXISTS worker_versions (
    worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    version                 TEXT NOT NULL,
    registered_at           TEXT NOT NULL,
    changelog               TEXT,
    PRIMARY KEY (worker_id, version)
);

-- ===========================================================================
-- M9.3 Capability Resolver (dedicated tables; Worker Registry is NOT overloaded)
-- ===========================================================================

-- One Task -> Worker mapping per resolution. Append-only; `updated_at` may
-- change on deterministic re-resolution, but prior states live in history.
CREATE TABLE IF NOT EXISTS resolver_assignments (
    assignment_id          TEXT NOT NULL PRIMARY KEY,
    graph_id               TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    task_id                TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id              TEXT REFERENCES workers(id) ON DELETE SET NULL,
    status                 TEXT NOT NULL,
    confidence             TEXT NOT NULL,
    reason                 TEXT NOT NULL DEFAULT '',
    matched_capabilities  TEXT NOT NULL DEFAULT '[]',
    missing_capabilities  TEXT NOT NULL DEFAULT '[]',
    selection_strategy    TEXT NOT NULL,
    schema_version         TEXT NOT NULL DEFAULT '1.0',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (graph_id, task_id)
);

-- Append-only snapshot of every resolution run (never updated, only inserted).
-- Surrogate autoincrement PK guarantees a new row per run even when two runs
-- share the same resolved_at (sub-millisecond re-resolution). Never mutated.
CREATE TABLE IF NOT EXISTS resolver_history (
    hid                   INTEGER PRIMARY KEY AUTOINCREMENT,
    resolved_at            TEXT NOT NULL,
    assignment_id         TEXT,
    graph_id              TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    task_id               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id             TEXT REFERENCES workers(id) ON DELETE SET NULL,
    status                TEXT NOT NULL,
    confidence            TEXT NOT NULL,
    score_total           INTEGER NOT NULL DEFAULT 0,
    matched_capabilities  TEXT NOT NULL DEFAULT '[]',
    missing_capabilities  TEXT NOT NULL DEFAULT '[]',
    selection_strategy    TEXT NOT NULL,
    FOREIGN KEY (assignment_id)
        REFERENCES resolver_assignments(assignment_id) ON DELETE SET NULL
);

-- Evolution of the resolver's own decisions (assignment churn over runs).
CREATE TABLE IF NOT EXISTS resolver_evolution (
    evolved_at            TEXT NOT NULL,
    graph_id             TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    task_id              TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_worker_id       TEXT REFERENCES workers(id) ON DELETE SET NULL,
    to_worker_id         TEXT REFERENCES workers(id) ON DELETE SET NULL,
    change_type          TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (evolved_at, task_id, from_worker_id, to_worker_id)
);

-- ===========================================================================
-- M9.4 Task Scheduler (dedicated tables; Resolver/Task Graph NOT overloaded)
-- ===========================================================================

-- One scheduled task per (graph, task). Re-scheduling UPDATES the live row in
-- place (never INSERT OR REPLACE — that would cascade-delete history). The
-- initial runnable state is recorded; the Runtime mutates states forward later.
CREATE TABLE IF NOT EXISTS scheduler_tasks (
    schedule_id          TEXT NOT NULL PRIMARY KEY,
    graph_id             TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    assignment_id        TEXT NOT NULL REFERENCES resolver_assignments(assignment_id) ON DELETE SET NULL,
    task_id              TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id            TEXT REFERENCES workers(id) ON DELETE SET NULL,
    phase                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL,
    priority             INTEGER NOT NULL DEFAULT 0,
    wave                 INTEGER NOT NULL DEFAULT 1,
    dependency_count     INTEGER NOT NULL DEFAULT 0,
    estimated_start      INTEGER,
    estimated_finish     INTEGER,
    blocked_reason       TEXT NOT NULL DEFAULT '',
    confidence           TEXT NOT NULL DEFAULT 'low',
    selection_strategy   TEXT NOT NULL DEFAULT 'single',
    schema_version       TEXT NOT NULL DEFAULT '1.0',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- Append-only snapshot of every scheduling run (never updated, only inserted).
-- Surrogate autoincrement PK guarantees a new row per run even when two runs
-- share the same scheduled_at (sub-millisecond re-scheduling).
CREATE TABLE IF NOT EXISTS scheduler_history (
    hid                   INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at         TEXT NOT NULL,
    schedule_id           TEXT NOT NULL,
    graph_id             TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    task_id              TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id            TEXT REFERENCES workers(id) ON DELETE SET NULL,
    wave                 INTEGER NOT NULL DEFAULT 1,
    status               TEXT NOT NULL,
    priority             INTEGER NOT NULL DEFAULT 0,
    assignment_id        TEXT,
    FOREIGN KEY (assignment_id)
        REFERENCES resolver_assignments(assignment_id) ON DELETE SET NULL
);

-- Evolution of the scheduler's decisions (wave/state churn over runs).
CREATE TABLE IF NOT EXISTS scheduler_evolution (
    evolved_at           TEXT NOT NULL,
    schedule_id          TEXT NOT NULL,
    graph_id             TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    task_id              TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_wave            INTEGER,
    to_wave              INTEGER,
    from_state           TEXT,
    to_state             TEXT,
    change_type          TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (evolved_at, task_id, from_state, to_state)
);

-- One run-level record per scheduling run (runnable ordering summary).
CREATE TABLE IF NOT EXISTS scheduler_runs (
    run_id               TEXT NOT NULL PRIMARY KEY,
    graph_id             TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    goal                 TEXT NOT NULL DEFAULT '',
    wave_count           INTEGER NOT NULL DEFAULT 0,
    task_count           INTEGER NOT NULL DEFAULT 0,
    critical_path_length INTEGER NOT NULL DEFAULT 0,
    max_parallelism      INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'scheduled',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- ===========================================================================
-- M9.5 Execution Runtime
-- ===========================================================================

-- One execution session per schedule run.
CREATE TABLE IF NOT EXISTS runtime_sessions (
    session_id           TEXT NOT NULL PRIMARY KEY,
    schedule_id          TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE,
    state                TEXT NOT NULL DEFAULT 'created',
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    schema_version       TEXT NOT NULL DEFAULT '1.0',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- Append-only event log for a session.
CREATE TABLE IF NOT EXISTS runtime_events (
    eid                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id             TEXT NOT NULL,
    session_id           TEXT NOT NULL REFERENCES runtime_sessions(session_id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    task_id              TEXT NOT NULL DEFAULT '',
    worker_id            TEXT,
    detail               TEXT NOT NULL DEFAULT '',
    at                   TEXT NOT NULL
);

-- Per-task execution record (latest state). Updated in place as a task moves
-- PENDING -> RUNNING -> terminal (the only mutable runtime table).
CREATE TABLE IF NOT EXISTS runtime_tasks (
    execution_id         TEXT NOT NULL PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES runtime_sessions(session_id) ON DELETE CASCADE,
    schedule_id          TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    worker_id            TEXT,
    wave                 INTEGER NOT NULL DEFAULT 1,
    attempt              INTEGER NOT NULL DEFAULT 1,
    status               TEXT NOT NULL,
    started_at           TEXT,
    finished_at          TEXT,
    duration_ms          INTEGER,
    exit_code            INTEGER,
    error                TEXT NOT NULL DEFAULT '',
    output_reference     TEXT,
    schema_version       TEXT NOT NULL DEFAULT '1.0',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- Immutable outcome of each execution attempt (append-only; never updated).
CREATE TABLE IF NOT EXISTS runtime_results (
    result_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id         TEXT NOT NULL REFERENCES runtime_tasks(execution_id) ON DELETE CASCADE,
    session_id           TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    worker_id            TEXT,
    success              INTEGER NOT NULL,
    stdout               TEXT NOT NULL DEFAULT '',
    stderr               TEXT NOT NULL DEFAULT '',
    artifacts            TEXT NOT NULL DEFAULT '[]',
    exit_code            INTEGER,
    duration_ms          INTEGER NOT NULL DEFAULT 0,
    error                TEXT NOT NULL DEFAULT '',
    recorded_at          TEXT NOT NULL
);

-- Append-only snapshot of every session run (never updated, only inserted).
CREATE TABLE IF NOT EXISTS runtime_history (
    hid                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           TEXT NOT NULL,
    schedule_id          TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    worker_id            TEXT,
    status               TEXT NOT NULL,
    attempt              INTEGER NOT NULL DEFAULT 1,
    at                   TEXT NOT NULL
);

-- Decision/state evolution across sessions (append-only).
CREATE TABLE IF NOT EXISTS runtime_evolution (
    evolved_at           TEXT NOT NULL,
    session_id           TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    from_state           TEXT,
    to_state             TEXT,
    change_type          TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (evolved_at, task_id, from_state, to_state)
);
"""


@dataclass
class Repository:
    id: Optional[int]
    name: str
    path: str
    default_branch: Optional[str]
    is_dirty: bool
    first_commit_date: Optional[str]
    last_commit_date: Optional[str]
    remote_url: Optional[str]
    commit_count: Optional[int]
    readme_summary: Optional[str]
    license: Optional[str]
    primary_author: Optional[str]
    ingestion_time: str
    maturity: Optional[str] = None
    readme_quality: Optional[str] = None
    readme_completeness: Optional[str] = None


@dataclass
class LangRow:
    language: str
    file_count: int


@dataclass
class TechRow:
    tech: str
    evidence: str


@dataclass
class RelationshipRow:
    repo_a: int
    repo_b: int
    kind: str
    evidence: str
    priority: int = 0
    strength: str = "Medium"


@dataclass
class ArchitectureRow:
    repo_id: int
    architecture: str
    evidence: str
    data_flow: Optional[str]
    known_patterns: Optional[str]
    complexity: Optional[str]
    confidence: Optional[str] = None


@dataclass
class ComponentRow:
    repo_id: int
    name: str
    evidence: str
    strength: str = "Medium"


@dataclass
class EntryPointRow:
    repo_id: int
    kind: str
    detail: str
    evidence: str


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    if path is None:
        path = db_path()
    # Handle in-memory database
    if isinstance(path, str) and path == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        if isinstance(path, str):
            path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive schema changes idempotently (M2/M4 columns)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(repositories)")}
    for col, ctype in (
        ("maturity", "TEXT"),
        ("readme_quality", "TEXT"),
        ("readme_completeness", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE repositories ADD COLUMN {col} {ctype}")
    # M4: evidence-strength model.
    for table, col in (
        ("relationships", "strength"),
        ("components", "strength"),
        ("architecture", "confidence"),
    ):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT 'Medium'")
    # M8.1.5: static vs temporal knowledge marker.
    know_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge)")}
    if "is_static" not in know_cols:
        conn.execute("ALTER TABLE knowledge ADD COLUMN is_static INTEGER NOT NULL DEFAULT 0")
    # M9.2.5: observations must have a PRIMARY KEY so INSERT OR REPLACE dedupes.
    # Existing databases created the table without one; rebuild it in place.
    _ensure_observations_pk(conn)
    # M9.2.5: referential integrity. Rebuild FK-bearing tables that predate the
    # FK schema so no orphan tasks/graphs/history/evolution rows can persist.
    _ensure_fk_tables(conn)
    # M9.2.5: contract versioning (Law 24). Add schema_version column where
    # missing; existing rows are treated as the current version by the loader.
    know_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge)")}
    if "schema_version" not in know_cols:
        conn.execute(
            "ALTER TABLE knowledge ADD COLUMN schema_version TEXT NOT NULL DEFAULT '1.0'")
    # M9.2.5: contract versioning (Law 24) for understanding/insight/initiative.
    for table in ("understanding", "insights", "initiatives", "workers", "plans"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "schema_version" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN schema_version TEXT NOT NULL DEFAULT '1.0'")
    # M9.3: resolver_history gained a surrogate AUTOINCREMENT PK so sub-millisecond
    # re-resolutions append instead of colliding on (resolved_at, assignment_id).
    # Rebuild in place for databases created before the change.
    _ensure_resolver_history_pk(conn)
    # M9.8: snapshots gains head_sha + manifest_hash to store the
    # ingest-independent change signature used by `friday observe --changed`.
    _ensure_snapshots_signature_cols(conn)
    # M10: worker availability + manifest_ref columns for runtime install state.
    # Additive; safe on DBs created before these columns existed in SCHEMA
    # (CREATE IF NOT EXISTS does not backfill columns onto pre-existing tables).
    worker_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)")}
    if "availability" not in worker_cols:
        conn.execute(
            "ALTER TABLE workers ADD COLUMN availability TEXT NOT NULL DEFAULT 'available'")
    if "manifest_ref" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN manifest_ref TEXT")
    conn.commit()


def _ensure_snapshots_signature_cols(conn: sqlite3.Connection) -> None:
    """Add head_sha / manifest_hash to snapshots if absent (idempotent)."""
    if "snapshots" not in _existing_tables(conn):
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if "head_sha" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN head_sha TEXT")
    if "manifest_hash" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN manifest_hash TEXT")


def _ensure_resolver_history_pk(conn: sqlite3.Connection) -> None:
    """Rebuild resolver_history with an AUTOINCREMENT surrogate PK if missing.

    Idempotent: skips when the table already has the `hid` column.
    """
    if "resolver_history" not in _existing_tables(conn):
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(resolver_history)")}
    if "hid" in cols:
        return
    conn.execute(
        "CREATE TABLE resolver_history_new ("
        "hid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "resolved_at TEXT NOT NULL, assignment_id TEXT, "
        "graph_id TEXT NOT NULL, task_id TEXT NOT NULL, "
        "worker_id TEXT, status TEXT NOT NULL, confidence TEXT NOT NULL, "
        "score_total INTEGER NOT NULL DEFAULT 0, "
        "matched_capabilities TEXT NOT NULL DEFAULT '[]', "
        "missing_capabilities TEXT NOT NULL DEFAULT '[]', "
        "selection_strategy TEXT NOT NULL, "
        "FOREIGN KEY (assignment_id) REFERENCES resolver_assignments(assignment_id) "
        "ON DELETE SET NULL)")
    conn.execute(
        "INSERT INTO resolver_history_new "
        "(resolved_at, assignment_id, graph_id, task_id, worker_id, status, "
        "confidence, score_total, matched_capabilities, missing_capabilities, "
        "selection_strategy) "
        "SELECT resolved_at, assignment_id, graph_id, task_id, worker_id, status, "
        "confidence, score_total, matched_capabilities, missing_capabilities, "
        "selection_strategy FROM resolver_history")
    conn.execute("DROP TABLE resolver_history")
    conn.execute("ALTER TABLE resolver_history_new RENAME TO resolver_history")


def _ensure_observations_pk(conn: sqlite3.Connection) -> None:
    """Rebuild `observations` with PRIMARY KEY(id) on databases that lack it.

    SQLite cannot ALTER ADD a PRIMARY KEY, so copy rows into a new table and
    swap. Idempotent: skips if the current table already declares the PK.
    """
    if "observations" not in _existing_tables(conn):
        return
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(observations)")]
    if "id" in cols and _column_is_pk(conn, "observations", "id"):
        return
    conn.execute("CREATE TABLE observations_new ("
                 "id TEXT NOT NULL PRIMARY KEY, "
                 "observed_at TEXT NOT NULL, source TEXT NOT NULL, "
                 "subject TEXT NOT NULL, aspect TEXT NOT NULL, value TEXT NOT NULL, "
                 "confidence TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', detail TEXT)")
    conn.execute(
        "INSERT OR REPLACE INTO observations_new "
        "(id, observed_at, source, subject, aspect, value, confidence, scope, detail) "
        "SELECT id, observed_at, source, subject, aspect, value, confidence, scope, detail "
        "FROM observations")
    conn.execute("DROP TABLE observations")
    conn.execute("ALTER TABLE observations_new RENAME TO observations")


def _column_is_pk(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for r in conn.execute(f"PRAGMA table_info({table})"):
        if r["name"] == column:
            return bool(r["pk"])
    return False


# M9.2.5: FK-bearing DDL for tables that originally shipped without FKs.
# Used only by the migration to rebuild existing databases. New databases get
# these FKs directly from SCHEMA (executescript). Kept in sync with SCHEMA.
_FK_TABLE_DDL = {
    "tasks": (
        "CREATE TABLE tasks_new ("
        "id TEXT PRIMARY KEY, "
        "graph_id TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE, "
        "plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE, "
        "milestone_order INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL, "
        "description TEXT NOT NULL DEFAULT '', task_type TEXT NOT NULL, "
        "required_capabilities TEXT NOT NULL DEFAULT '', complexity TEXT NOT NULL DEFAULT 'medium', "
        "priority TEXT NOT NULL DEFAULT 'medium', estimated_effort TEXT NOT NULL DEFAULT 'medium', "
        "dependencies TEXT NOT NULL DEFAULT '', inputs TEXT NOT NULL DEFAULT '[]', "
        "outputs TEXT NOT NULL DEFAULT '[]', acceptance_criteria TEXT NOT NULL DEFAULT '[]', "
        "verification TEXT NOT NULL DEFAULT '[]', rollback TEXT NOT NULL DEFAULT '[]', "
        "evidence TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending', "
        "confidence TEXT NOT NULL DEFAULT 'medium', sequence INTEGER NOT NULL DEFAULT 0)"
    ),
    "task_edges": (
        "CREATE TABLE task_edges_new ("
        "id TEXT PRIMARY KEY, "
        "graph_id TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE, "
        "from_task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "to_task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
        "kind TEXT NOT NULL DEFAULT 'depends_on')"
    ),
    "task_graphs": (
        "CREATE TABLE task_graphs_new ("
        "id TEXT PRIMARY KEY, goal TEXT NOT NULL, "
        "plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE, "
        "plan_type TEXT NOT NULL, task_count INTEGER NOT NULL DEFAULT 0, "
        "edge_count INTEGER NOT NULL DEFAULT 0, critical_path_length INTEGER NOT NULL DEFAULT 0, "
        "parallel_groups INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'compiled', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    ),
    "task_history": (
        "CREATE TABLE task_history_new ("
        "generated_at TEXT NOT NULL, "
        "graph_id TEXT NOT NULL REFERENCES task_graphs(id) ON DELETE CASCADE, "
        "goal TEXT NOT NULL, task_count INTEGER NOT NULL DEFAULT 0, "
        "edge_count INTEGER NOT NULL DEFAULT 0, critical_path_length INTEGER NOT NULL DEFAULT 0, "
        "parallel_groups INTEGER NOT NULL DEFAULT 0, tasks_json TEXT NOT NULL DEFAULT '', "
        "edges_json TEXT NOT NULL DEFAULT '')"
    ),
    "knowledge_history": (
        "CREATE TABLE knowledge_history_new ("
        "build_at TEXT NOT NULL, "
        "knowledge_id TEXT NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE, "
        "type TEXT NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL, "
        "confidence TEXT NOT NULL, evidence_ids TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "verification_count INTEGER NOT NULL DEFAULT 0, is_static INTEGER NOT NULL DEFAULT 0)"
    ),
    "understanding_history": (
        "CREATE TABLE understanding_history_new ("
        "build_at TEXT NOT NULL, "
        "understanding_id TEXT NOT NULL REFERENCES understanding(id) ON DELETE CASCADE, "
        "type TEXT NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL, "
        "confidence TEXT NOT NULL, status TEXT NOT NULL, knowledge_ids TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "reinforced_count INTEGER NOT NULL DEFAULT 0)"
    ),
    "initiative_history": (
        "CREATE TABLE initiative_history_new ("
        "build_at TEXT NOT NULL, "
        "initiative_id TEXT NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE, "
        "title TEXT NOT NULL, initiative_type TEXT NOT NULL, status TEXT NOT NULL, "
        "confidence TEXT NOT NULL, started_at TEXT, completed_at TEXT, "
        "participating_repositories TEXT NOT NULL DEFAULT '', "
        "understanding_ids TEXT NOT NULL DEFAULT '', knowledge_ids TEXT NOT NULL DEFAULT '')"
    ),
    "insight_history": (
        "CREATE TABLE insight_history_new ("
        "build_at TEXT NOT NULL, "
        "insight_id TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE, "
        "title TEXT NOT NULL, insight_type TEXT NOT NULL, statement TEXT NOT NULL, "
        "status TEXT NOT NULL, confidence TEXT NOT NULL, "
        "understanding_ids TEXT NOT NULL DEFAULT '', initiative_ids TEXT NOT NULL DEFAULT '', "
        "knowledge_ids TEXT NOT NULL DEFAULT '')"
    ),
    "plan_history": (
        "CREATE TABLE plan_history_new ("
        "generated_at TEXT NOT NULL, "
        "plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE, "
        "goal TEXT NOT NULL, plan_type TEXT NOT NULL, confidence TEXT NOT NULL, "
        "status TEXT NOT NULL, affected_initiative_ids TEXT NOT NULL DEFAULT '', "
        "affected_insight_ids TEXT NOT NULL DEFAULT '', "
        "affected_understanding_ids TEXT NOT NULL DEFAULT '', "
        "affected_knowledge_ids TEXT NOT NULL DEFAULT '', milestones TEXT NOT NULL DEFAULT '', "
        "dependencies TEXT NOT NULL DEFAULT '', risks TEXT NOT NULL DEFAULT '', "
        "verification TEXT NOT NULL DEFAULT '', rollback TEXT NOT NULL DEFAULT '', "
        "estimated_complexity TEXT NOT NULL DEFAULT '', estimated_effort TEXT NOT NULL DEFAULT '')"
    ),
    "understanding_evolution": (
        "CREATE TABLE understanding_evolution_new ("
        "id TEXT PRIMARY KEY, build_at TEXT NOT NULL, event_type TEXT NOT NULL, "
        "understanding_id TEXT NOT NULL REFERENCES understanding(id) ON DELETE CASCADE, "
        "previous_confidence TEXT, new_confidence TEXT, previous_status TEXT, new_status TEXT, "
        "previous_statement TEXT, new_statement TEXT, reason TEXT NOT NULL, "
        "knowledge_ids TEXT NOT NULL DEFAULT '')"
    ),
    "initiative_evolution": (
        "CREATE TABLE initiative_evolution_new ("
        "id TEXT PRIMARY KEY, build_at TEXT NOT NULL, event_type TEXT NOT NULL, "
        "initiative_id TEXT NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE, "
        "parent_ids TEXT NOT NULL DEFAULT '', child_ids TEXT NOT NULL DEFAULT '', "
        "previous_status TEXT, new_status TEXT, previous_confidence TEXT, new_confidence TEXT, "
        "previous_title TEXT, new_title TEXT, reason TEXT NOT NULL, "
        "understanding_ids TEXT NOT NULL DEFAULT '', knowledge_ids TEXT NOT NULL DEFAULT '')"
    ),
    "insight_evolution": (
        "CREATE TABLE insight_evolution_new ("
        "id TEXT PRIMARY KEY, build_at TEXT NOT NULL, event_type TEXT NOT NULL, "
        "insight_id TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE, "
        "previous_status TEXT, new_status TEXT, previous_confidence TEXT, new_confidence TEXT, "
        "previous_statement TEXT, new_statement TEXT, reason TEXT NOT NULL, "
        "understanding_ids TEXT NOT NULL DEFAULT '', initiative_ids TEXT NOT NULL DEFAULT '', "
        "knowledge_ids TEXT NOT NULL DEFAULT '')"
    ),
    "plan_evolution": (
        "CREATE TABLE plan_evolution_new ("
        "id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, event_type TEXT NOT NULL, "
        "plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE, "
        "previous_status TEXT, new_status TEXT, previous_confidence TEXT, new_confidence TEXT, "
        "reason TEXT NOT NULL, affected_initiative_ids TEXT NOT NULL DEFAULT '', "
        "affected_insight_ids TEXT NOT NULL DEFAULT '', "
        "affected_understanding_ids TEXT NOT NULL DEFAULT '', "
        "affected_knowledge_ids TEXT NOT NULL DEFAULT '')"
    ),
    "worker_history": (
        "CREATE TABLE worker_history_new ("
        "registered_at TEXT NOT NULL, "
        "worker_id TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE, "
        "name TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL, "
        "capabilities TEXT NOT NULL DEFAULT '', limitations TEXT NOT NULL DEFAULT '', "
        "event_type TEXT NOT NULL, note TEXT)"
    ),
    "worker_versions": (
        "CREATE TABLE worker_versions_new ("
        "worker_id TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE, "
        "version TEXT NOT NULL, registered_at TEXT NOT NULL, changelog TEXT)"
    ),
}


def _ensure_fk_tables(conn: sqlite3.Connection) -> None:
    """Rebuild FK-bearing tables on existing databases that predate M9.2.5.

    A table is rebuilt only if it currently lacks the expected foreign key.
    SQLite cannot ALTER ADD a FK, so we copy rows into a *_new table (with FKs)
    and swap. Idempotent and safe for already-correct databases.
    """
    for table, ddl in _FK_TABLE_DDL.items():
        if table not in _existing_tables(conn):
            continue
        if _has_fk(conn, table):
            continue
        new = table + "_new"
        conn.execute(ddl)
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        col_csv = ", ".join(cols)
        conn.execute(
            f"INSERT OR REPLACE INTO {new} ({col_csv}) SELECT {col_csv} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {new} RENAME TO {table}")


def _existing_tables(conn: sqlite3.Connection) -> set:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _has_fk(conn: sqlite3.Connection, table: str) -> bool:
    return bool(list(conn.execute(f"PRAGMA foreign_key_list({table})")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def commit_if_top(conn: sqlite3.Connection) -> None:
    """Commit only when no explicit transaction is already open.

    Insert helpers call this instead of `conn.commit()` so that a caller which
    has opened a transaction (e.g. an engine `build()` wrapping its full
    multi-table persist) owns the single commit/rollback boundary. When no
    transaction is active the helper finalizes its own write (unchanged
    standalone behaviour). Part F: every multi-table write is atomic.
    """
    if not conn.in_transaction:
        conn.commit()


class atomic:
    """Context manager wrapping a block of writes in one transaction.

    Usage::

        with atomic(conn):
            insert_x(conn, rows)
            insert_x_history(conn, rows)

    Commits on clean exit; rolls back on any exception so a failure mid-build
    can never leave partially-written rows. Nested use is a no-op (the outermost
    transaction owns the boundary), matching SQLite's lack of nested commits.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._outer = False

    def __enter__(self) -> "atomic":
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
            self._outer = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._outer:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        return False



def upsert_repository(
    conn: sqlite3.Connection,
    *,
    name: str,
    path: str,
    default_branch: Optional[str],
    is_dirty: bool,
    first_commit_date: Optional[str],
    last_commit_date: Optional[str],
    remote_url: Optional[str],
    commit_count: Optional[int],
    readme_summary: Optional[str],
    license: Optional[str],
    primary_author: Optional[str],
) -> int:
    """Insert or update a repository by path; returns its row id."""
    cur = conn.execute(
        """
        INSERT INTO repositories
            (name, path, default_branch, is_dirty, first_commit_date, last_commit_date, remote_url,
             commit_count, readme_summary, license, primary_author, ingestion_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name=excluded.name,
            default_branch=excluded.default_branch,
            is_dirty=excluded.is_dirty,
            first_commit_date=excluded.first_commit_date,
            last_commit_date=excluded.last_commit_date,
            remote_url=excluded.remote_url,
            commit_count=excluded.commit_count,
            readme_summary=excluded.readme_summary,
            license=excluded.license,
            primary_author=excluded.primary_author,
            ingestion_time=excluded.ingestion_time
        """,
        (
            name,
            path,
            default_branch,
            int(is_dirty),
            first_commit_date,
            last_commit_date,
            remote_url,
            commit_count,
            readme_summary,
            license,
            primary_author,
            now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM repositories WHERE path = ?", (path,)).fetchone()
    return row["id"]


def replace_children(
    conn: sqlite3.Connection,
    repo_id: int,
    languages: list[LangRow],
    technologies: list[TechRow],
) -> None:
    conn.execute("DELETE FROM languages WHERE repo_id = ?", (repo_id,))
    conn.execute("DELETE FROM technologies WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
        [(repo_id, l.language, l.file_count) for l in languages],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
        [(repo_id, t.tech, t.evidence) for t in technologies],
    )
    conn.commit()


def get_repositories(conn: sqlite3.Connection) -> list[Repository]:
    rows = conn.execute("SELECT * FROM repositories ORDER BY name").fetchall()
    return [
        Repository(
            id=r["id"],
            name=r["name"],
            path=r["path"],
            default_branch=r["default_branch"],
            is_dirty=bool(r["is_dirty"]),
            first_commit_date=r["first_commit_date"],
            last_commit_date=r["last_commit_date"],
            remote_url=r["remote_url"],
            commit_count=r["commit_count"],
            readme_summary=r["readme_summary"],
            license=r["license"],
            primary_author=r["primary_author"],
            ingestion_time=r["ingestion_time"],
            maturity=r["maturity"],
            readme_quality=r["readme_quality"],
            readme_completeness=r["readme_completeness"],
        )
        for r in rows
    ]


def get_languages(conn: sqlite3.Connection, repo_id: int) -> list[LangRow]:
    rows = conn.execute(
        "SELECT language, file_count FROM languages WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    return [LangRow(language=r["language"], file_count=r["file_count"]) for r in rows]


def get_technologies(conn: sqlite3.Connection, repo_id: int) -> list[TechRow]:
    rows = conn.execute(
        "SELECT tech, evidence FROM technologies WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    return [TechRow(tech=r["tech"], evidence=r["evidence"]) for r in rows]


def set_repo_quality(
    conn: sqlite3.Connection,
    repo_id: int,
    maturity: Optional[str],
    readme_quality: Optional[str],
    readme_completeness: Optional[str],
) -> None:
    conn.execute(
        """
        UPDATE repositories
        SET maturity = ?, readme_quality = ?, readme_completeness = ?
        WHERE id = ?
        """,
        (maturity, readme_quality, readme_completeness, repo_id),
    )
    conn.commit()


def replace_relationships(
    conn: sqlite3.Connection, repo_id: int, rels: list[RelationshipRow]
) -> None:
    """Replace all stored relationships touching `repo_id`."""
    conn.execute(
        "DELETE FROM relationships WHERE repo_a = ? OR repo_b = ?", (repo_id, repo_id)
    )
    conn.executemany(
        """INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(r.repo_a, r.repo_b, r.kind, r.evidence, r.priority, r.strength) for r in rels],
    )
    conn.commit()


def replace_all_relationships(conn: sqlite3.Connection, rels: list[RelationshipRow]) -> None:
    """Wipe and rewrite the entire relationships table (used at ingest)."""
    conn.execute("DELETE FROM relationships")
    conn.executemany(
        """INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(r.repo_a, r.repo_b, r.kind, r.evidence, r.priority, r.strength) for r in rels],
    )
    conn.commit()


def get_relationships(conn: sqlite3.Connection, repo_id: int) -> list[RelationshipRow]:
    rows = conn.execute(
        """SELECT repo_a, repo_b, kind, evidence, priority, strength
           FROM relationships WHERE repo_a = ? OR repo_b = ? ORDER BY priority DESC, kind""",
        (repo_id, repo_id),
    ).fetchall()
    return [
        RelationshipRow(
            repo_a=r["repo_a"],
            repo_b=r["repo_b"],
            kind=r["kind"],
            evidence=r["evidence"],
            priority=r["priority"],
            strength=r["strength"],
        )
        for r in rows
    ]


def get_all_relationships(conn: sqlite3.Connection) -> list[RelationshipRow]:
    rows = conn.execute(
        "SELECT repo_a, repo_b, kind, evidence, priority, strength FROM relationships ORDER BY priority DESC, kind"
    ).fetchall()
    return [
        RelationshipRow(
            repo_a=r["repo_a"],
            repo_b=r["repo_b"],
            kind=r["kind"],
            evidence=r["evidence"],
            priority=r["priority"],
            strength=r["strength"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Architecture (Milestone 3)
# ---------------------------------------------------------------------------


def upsert_architecture(
    conn: sqlite3.Connection,
    *,
    repo_id: int,
    architecture: str,
    evidence: str,
    data_flow: Optional[str] = None,
    known_patterns: Optional[str] = None,
    complexity: Optional[str] = None,
    confidence: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO architecture
            (repo_id, architecture, evidence, data_flow, known_patterns, complexity, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id) DO UPDATE SET
            architecture=excluded.architecture,
            evidence=excluded.evidence,
            data_flow=excluded.data_flow,
            known_patterns=excluded.known_patterns,
            complexity=excluded.complexity,
            confidence=excluded.confidence
        """,
        (repo_id, architecture, evidence, data_flow, known_patterns, complexity, confidence),
    )
    conn.commit()


def get_architecture(conn: sqlite3.Connection, repo_id: int) -> Optional[ArchitectureRow]:
    row = conn.execute(
        "SELECT * FROM architecture WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    if row is None:
        return None
    return ArchitectureRow(
        repo_id=row["repo_id"],
        architecture=row["architecture"],
        evidence=row["evidence"],
        data_flow=row["data_flow"],
        known_patterns=row["known_patterns"],
        complexity=row["complexity"],
        confidence=row["confidence"],
    )


def replace_components(
    conn: sqlite3.Connection, repo_id: int, components: list[ComponentRow]
) -> None:
    conn.execute("DELETE FROM components WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO components (repo_id, name, evidence, strength) VALUES (?, ?, ?, ?)",
        [(repo_id, c.name, c.evidence, c.strength) for c in components],
    )
    conn.commit()


def get_components(conn: sqlite3.Connection, repo_id: int) -> list[ComponentRow]:
    rows = conn.execute(
        "SELECT repo_id, name, evidence, strength FROM components WHERE repo_id = ? ORDER BY name",
        (repo_id,),
    ).fetchall()
    return [
        ComponentRow(
            repo_id=r["repo_id"], name=r["name"], evidence=r["evidence"], strength=r["strength"]
        )
        for r in rows
    ]


def replace_entry_points(
    conn: sqlite3.Connection, repo_id: int, entries: list[EntryPointRow]
) -> None:
    conn.execute("DELETE FROM entry_points WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO entry_points (repo_id, kind, detail, evidence) VALUES (?, ?, ?, ?)",
        [(repo_id, e.kind, e.detail, e.evidence) for e in entries],
    )
    conn.commit()


def get_entry_points(conn: sqlite3.Connection, repo_id: int) -> list[EntryPointRow]:
    rows = conn.execute(
        "SELECT repo_id, kind, detail, evidence FROM entry_points WHERE repo_id = ? "
        "ORDER BY kind, detail",
        (repo_id,),
    ).fetchall()
    return [
        EntryPointRow(
            repo_id=r["repo_id"], kind=r["kind"], detail=r["detail"], evidence=r["evidence"]
        )
        for r in rows
    ]


def all_entry_points(conn: sqlite3.Connection) -> list[EntryPointRow]:
    """Every entry point across all repositories (for cross-repo similarity)."""
    rows = conn.execute(
        "SELECT repo_id, kind, detail, evidence FROM entry_points ORDER BY repo_id, kind"
    ).fetchall()
    return [
        EntryPointRow(
            repo_id=r["repo_id"], kind=r["kind"], detail=r["detail"], evidence=r["evidence"]
        )
        for r in rows
    ]


def all_components(conn: sqlite3.Connection) -> list[ComponentRow]:
    """Every component across all repositories (for cross-repo similarity)."""
    rows = conn.execute(
        "SELECT repo_id, name, evidence, strength FROM components ORDER BY repo_id, name"
    ).fetchall()
    return [
        ComponentRow(
            repo_id=r["repo_id"], name=r["name"], evidence=r["evidence"],
            strength=r["strength"],
        )
        for r in rows
    ]


def entry_points_by_kind(conn: sqlite3.Connection, kind: str) -> list[EntryPointRow]:
    rows = conn.execute(
        "SELECT repo_id, kind, detail, evidence FROM entry_points WHERE kind = ? "
        "ORDER BY repo_id",
        (kind,),
    ).fetchall()
    return [
        EntryPointRow(
            repo_id=r["repo_id"], kind=r["kind"], detail=r["detail"], evidence=r["evidence"]
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Observation snapshots (Milestone 5) — append-only, facts only.
# ---------------------------------------------------------------------------


@dataclass
class SnapshotRow:
    observed_at: str
    repo_path: str
    repo_name: Optional[str]
    default_branch: Optional[str]
    commit_count: Optional[int]
    last_commit_date: Optional[str]
    is_dirty: bool
    readme_hash: Optional[str]
    architecture_hash: Optional[str]
    identity_hash: Optional[str]
    head_sha: Optional[str] = None
    manifest_hash: Optional[str] = None


def insert_snapshot(conn: sqlite3.Connection, snap: SnapshotRow) -> None:
    """Append one observation row. Snapshots are never updated or deleted."""
    conn.execute(
        """
        INSERT INTO snapshots
            (observed_at, repo_path, repo_name, default_branch, commit_count,
             last_commit_date, is_dirty, readme_hash, architecture_hash, identity_hash,
             head_sha, manifest_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.observed_at,
            snap.repo_path,
            snap.repo_name,
            snap.default_branch,
            snap.commit_count,
            snap.last_commit_date,
            int(snap.is_dirty),
            snap.readme_hash,
            snap.architecture_hash,
            snap.identity_hash,
            snap.head_sha,
            snap.manifest_hash,
        ),
    )
    conn.commit()


def latest_observation(conn: sqlite3.Connection) -> list[SnapshotRow]:
    """All snapshot rows from the single most recent prior observation run.

    Call BEFORE writing the current run so a run never diffs against itself.
    Returns [] when no observations exist yet.
    """
    row = conn.execute("SELECT MAX(observed_at) AS t FROM snapshots").fetchone()
    if row is None or row["t"] is None:
        return []
    latest = row["t"]
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE observed_at = ? ORDER BY repo_path", (latest,)
    ).fetchall()
    return [
        SnapshotRow(
            observed_at=r["observed_at"],
            repo_path=r["repo_path"],
            repo_name=r["repo_name"],
            default_branch=r["default_branch"],
            commit_count=r["commit_count"],
            last_commit_date=r["last_commit_date"],
            is_dirty=bool(r["is_dirty"]),
            readme_hash=r["readme_hash"],
            architecture_hash=r["architecture_hash"],
            identity_hash=r["identity_hash"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Observation Engine storage (Milestone 7) — append-only generic observations.
# ---------------------------------------------------------------------------


@dataclass
class ObservationRow:
    """One persisted observation fact.

    `id` is the deterministic key `observed_at:source:subject:aspect` so the
    same fact written twice in one run is idempotent. With PRIMARY KEY(id) in
    place (M9.2.5), `INSERT OR REPLACE` collapses identical re-inserts instead
    of appending duplicates. `scope` qualifies the subject (e.g. a repository
    path) without overloading `subject`.
    """

    id: str
    observed_at: str
    source: str
    subject: str
    aspect: str
    value: str
    confidence: str
    scope: str = ""
    detail: Optional[str] = None

    def make_id(self) -> str:
        return f"{self.observed_at}:{self.source}:{self.subject}:{self.aspect}"


def insert_observations(conn: sqlite3.Connection, rows: list[ObservationRow]) -> None:
    """Append observations, idempotent on (observed_at, source, subject, aspect)."""
    for row in rows:
        row.id = row.make_id()
        conn.execute(
            """
            INSERT OR REPLACE INTO observations
                (id, observed_at, source, subject, aspect, value, confidence, scope, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id, row.observed_at, row.source, row.subject, row.aspect,
                row.value, row.confidence, row.scope, row.detail,
            ),
        )
    conn.commit()


def latest_observations(conn: sqlite3.Connection) -> list[ObservationRow]:
    """All observation rows from the single most recent prior observation run."""
    row = conn.execute("SELECT MAX(observed_at) AS t FROM observations").fetchone()
    if row is None or row["t"] is None:
        return []
    latest = row["t"]
    rows = conn.execute(
        "SELECT * FROM observations WHERE observed_at = ? "
        "ORDER BY source, subject, aspect",
        (latest,),
    ).fetchall()
    return [
        ObservationRow(
            id=r["id"],
            observed_at=r["observed_at"],
            source=r["source"],
            subject=r["subject"],
            aspect=r["aspect"],
            value=r["value"],
            confidence=r["confidence"],
            scope=r["scope"],
            detail=r["detail"],
        )
        for r in rows
    ]


def observation_state_as_of(
    conn: sqlite3.Connection, source: str, observed_at: str
) -> list[ObservationRow]:
    """Every observation for `source` that was current as of `observed_at`.

    Deterministic: the value of an (source, subject, aspect) triple at a given
    time is the one with the largest observed_at <= the requested time. Used to
    build a per-run prior state the engine diffs against without re-reading the
    writer.
    """
    rows = conn.execute(
        """
        SELECT o1.*
        FROM observations o1
        JOIN (
            SELECT source, subject, aspect, MAX(observed_at) AS t
            FROM observations
            WHERE source = ? AND observed_at <= ?
            GROUP BY source, subject, aspect
        ) o2 ON o2.source = o1.source AND o2.subject = o1.subject
            AND o2.aspect = o1.aspect AND o2.t = o1.observed_at
        ORDER BY o1.subject, o1.aspect
        """,
        (source, observed_at),
    ).fetchall()
    return [
        ObservationRow(
            id=r["id"],
            observed_at=r["observed_at"],
            source=r["source"],
            subject=r["subject"],
            aspect=r["aspect"],
            value=r["value"],
            confidence=r["confidence"],
            scope=r["scope"],
            detail=r["detail"],
        )
        for r in rows
    ]


def observations_all(conn: sqlite3.Connection) -> list[ObservationRow]:
    """Every observation row, newest first. For CLI inspection."""
    rows = conn.execute(
        "SELECT * FROM observations ORDER BY observed_at DESC, source, subject, aspect"
    ).fetchall()
    return [
        ObservationRow(
            id=r["id"],
            observed_at=r["observed_at"],
            source=r["source"],
            subject=r["subject"],
            aspect=r["aspect"],
            value=r["value"],
            confidence=r["confidence"],
            scope=r["scope"],
            detail=r["detail"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Engineering Context storage (Milestone 7.2) — append-only sessions.
# ---------------------------------------------------------------------------


@dataclass
class SessionRow:
    """One derived engineering session.

    References observation ids (comma-joined) rather than duplicating raw
    observation facts. `id` is deterministic (built_at:primary_repo:start_time)
    so rebuilding the same window is idempotent and append-only by window.
    """

    id: str
    start_time: str
    end_time: str
    repositories: str
    primary_repo: Optional[str]
    observations: str
    activity: str
    confidence: str
    duration_min: float
    branch: Optional[str]
    summary: Optional[str]
    built_at: str


def insert_sessions(conn: sqlite3.Connection, rows: list[SessionRow]) -> None:
    """Append sessions. Idempotent on `id` (same window rebuild replaces)."""
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (id, start_time, end_time, repositories, primary_repo,
                 observations, activity, confidence, duration_min, branch,
                 summary, built_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id, row.start_time, row.end_time, row.repositories,
                row.primary_repo, row.observations, row.activity,
                row.confidence, row.duration_min, row.branch, row.summary,
                row.built_at,
            ),
        )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[SessionRow]:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_session(row)


def sessions_all(conn: sqlite3.Connection) -> list[SessionRow]:
    """Every session, newest first."""
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY start_time DESC, id"
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def sessions_on_day(conn: sqlite3.Connection, day: str) -> list[SessionRow]:
    """Sessions whose start_time UTC date equals `day` (YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT * FROM sessions WHERE date(start_time) = ? "
        "ORDER BY start_time, id",
        (day,),
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def _row_to_session(r) -> SessionRow:
    return SessionRow(
        id=r["id"],
        start_time=r["start_time"],
        end_time=r["end_time"],
        repositories=r["repositories"],
        primary_repo=r["primary_repo"],
        observations=r["observations"],
        activity=r["activity"],
        confidence=r["confidence"],
        duration_min=r["duration_min"],
        branch=r["branch"],
        summary=r["summary"],
        built_at=r["built_at"],
    )


def latest_observation_time(conn: sqlite3.Connection) -> Optional[str]:
    """UTC timestamp of the most recent stored observation (read-only)."""
    row = conn.execute("SELECT MAX(observed_at) AS t FROM observations").fetchone()
    return row["t"] if row else None


def latest_session_built_at(conn: sqlite3.Connection) -> Optional[str]:
    """UTC timestamp of the most recent context build (read-only)."""
    row = conn.execute("SELECT MAX(built_at) AS t FROM sessions").fetchone()
    return row["t"] if row else None


# ---------------------------------------------------------------------------
# Knowledge Engine storage (Milestone 8.1) — append-only knowledge.
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeRow:
    """One accumulated knowledge entry."""

    id: str
    type: str
    subject: str
    statement: str
    confidence: str
    evidence_ids: str
    status: str
    created_at: str
    updated_at: str
    last_verified: Optional[str]
    verification_count: int
    is_static: int = 0
    schema_version: str = "1.0"


def update_knowledge_status(conn: sqlite3.Connection, knowledge_id: str, status: str) -> None:
    """Apply an evidence-driven lifecycle transition (Dormant/Retired/Reactivated).

    The ONLY live-row mutation the Knowledge Evolution layer performs. The prior
    version is preserved forever in knowledge_history; this only advances the
    latest row's status. Never used for confidence/evidence/statement.
    """
    conn.execute(
        "UPDATE knowledge SET status = ? WHERE id = ?", (status, knowledge_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Knowledge Evolution storage (Milestone 8.2) — append-only history + events.
# Nothing here is ever mutated. The Brain reads `knowledge` (unchanged);
# evolution layers derive change records on top.
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeHistoryRow:
    """One snapshot of a knowledge entry as of a single build."""

    build_at: str
    knowledge_id: str
    type: str
    subject: str
    statement: str
    confidence: str
    evidence_ids: str
    status: str
    created_at: str
    updated_at: str
    verification_count: int
    is_static: int = 0


@dataclass
class EvolutionEventRow:
    """One deterministic evolution event derived from a history diff."""

    id: str
    build_at: str
    event_type: str
    knowledge_id: str
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_statement: Optional[str]
    new_statement: Optional[str]
    reason: str
    evidence_ids: str
    related_ids: str
    timestamp: str


def insert_knowledge_history(conn: sqlite3.Connection, rows: List[KnowledgeHistoryRow]) -> None:
    """Append a full snapshot of knowledge state for one build. Idempotent on
    (build_at, knowledge_id); re-running the same build replaces that build's
    snapshot but never touches prior builds."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_history
                (build_at, knowledge_id, type, subject, statement, confidence,
                 evidence_ids, status, created_at, updated_at,
                 verification_count, is_static)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.knowledge_id, r.type, r.subject, r.statement,
                r.confidence, r.evidence_ids, r.status, r.created_at,
                r.updated_at, r.verification_count, int(r.is_static),
            ),
        )
    conn.commit()


def latest_knowledge_snapshot(conn: sqlite3.Connection) -> List[KnowledgeHistoryRow]:
    """The most recent prior build snapshot (read-only). [] on cold start."""
    row = conn.execute("SELECT MAX(build_at) AS t FROM knowledge_history").fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM knowledge_history WHERE build_at = ? ORDER BY knowledge_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_history(r) for r in rows]


def knowledge_history_for(conn: sqlite3.Connection, knowledge_id: str) -> List[KnowledgeHistoryRow]:
    """Every snapshot of one knowledge entry across all builds, oldest first."""
    rows = conn.execute(
        "SELECT * FROM knowledge_history WHERE knowledge_id = ? ORDER BY build_at",
        (knowledge_id,),
    ).fetchall()
    return [_row_to_history(r) for r in rows]


def insert_evolution_events(conn: sqlite3.Connection, rows: List[EvolutionEventRow]) -> None:
    """Append evolution events. Idempotent on id; never updates old rows."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO evolution_events
                (id, build_at, event_type, knowledge_id, previous_confidence,
                 new_confidence, previous_status, new_status, previous_statement,
                 new_statement, reason, evidence_ids, related_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.knowledge_id,
                r.previous_confidence, r.new_confidence, r.previous_status,
                r.new_status, r.previous_statement, r.new_statement, r.reason,
                r.evidence_ids, r.related_ids, r.timestamp,
            ),
        )
    conn.commit()


def evolution_events_all(conn: sqlite3.Connection) -> List[EvolutionEventRow]:
    """Every evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM evolution_events ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def evolution_events_for(conn: sqlite3.Connection, knowledge_id: str) -> List[EvolutionEventRow]:
    """Evolution events touching one knowledge entry, oldest first."""
    rows = conn.execute(
        "SELECT * FROM evolution_events WHERE knowledge_id = ? ORDER BY timestamp, id",
        (knowledge_id,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_history(r) -> KnowledgeHistoryRow:
    return KnowledgeHistoryRow(
        build_at=r["build_at"],
        knowledge_id=r["knowledge_id"],
        type=r["type"],
        subject=r["subject"],
        statement=r["statement"],
        confidence=r["confidence"],
        evidence_ids=r["evidence_ids"],
        status=r["status"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        verification_count=r["verification_count"] or 0,
        is_static=bool(r["is_static"]),
    )


def _row_to_event(r) -> EvolutionEventRow:
    return EvolutionEventRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        knowledge_id=r["knowledge_id"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_statement=r["previous_statement"],
        new_statement=r["new_statement"],
        reason=r["reason"],
        evidence_ids=r["evidence_ids"] or "",
        related_ids=r["related_ids"] or "",
        timestamp=r["timestamp"],
    )


# ---------------------------------------------------------------------------
# Understanding Engine storage (Milestone 8.3) — write-only layer over Knowledge.
# Append-only. The Brain reads `understanding` (new); knowledge tables unchanged.
# ---------------------------------------------------------------------------


@dataclass
class UnderstandingRow:
    """One derived engineering understanding."""

    id: str
    type: str
    subject: str
    statement: str
    confidence: str
    status: str
    knowledge_ids: str
    created_at: str
    updated_at: str
    build_at: str
    retired_at: Optional[str] = None
    schema_version: str = "1.0"


@dataclass
class UnderstandingHistoryRow:
    """One snapshot of an understanding as of a single build."""

    build_at: str
    understanding_id: str
    type: str
    subject: str
    statement: str
    confidence: str
    status: str
    knowledge_ids: str
    created_at: str
    updated_at: str
    reinforced_count: int = 0


@dataclass
class UnderstandingEvolutionRow:
    """One deterministic understanding evolution event."""

    id: str
    build_at: str
    event_type: str
    understanding_id: str
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_statement: Optional[str]
    new_statement: Optional[str]
    reason: str
    knowledge_ids: str
    timestamp: str


def insert_understanding(conn: sqlite3.Connection, rows: List[UnderstandingRow]) -> None:
    """Insert or replace understanding entries. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO understanding
                (id, type, subject, statement, confidence, status,
                 knowledge_ids, created_at, updated_at, build_at, retired_at,
                 schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type, subject=excluded.subject,
                statement=excluded.statement, confidence=excluded.confidence,
                status=excluded.status, knowledge_ids=excluded.knowledge_ids,
                updated_at=excluded.updated_at, build_at=excluded.build_at,
                retired_at=excluded.retired_at, schema_version=excluded.schema_version
            """,
            (
                r.id, r.type, r.subject, r.statement, r.confidence, r.status,
                r.knowledge_ids, r.created_at, r.updated_at, r.build_at,
                r.retired_at, r.schema_version,
            ),
        )
    commit_if_top(conn)


def get_all_understanding(conn: sqlite3.Connection) -> List[UnderstandingRow]:
    """Every understanding entry, newest first."""
    rows = conn.execute(
        "SELECT * FROM understanding ORDER BY updated_at DESC"
    ).fetchall()
    return [_row_to_understanding(r) for r in rows]


def get_understanding_by_id(conn: sqlite3.Connection, uid: str) -> Optional[UnderstandingRow]:
    row = conn.execute(
        "SELECT * FROM understanding WHERE id = ?", (uid,)
    ).fetchone()
    return _row_to_understanding(row) if row else None


def get_understanding_by_type(conn: sqlite3.Connection, utype: str) -> List[UnderstandingRow]:
    rows = conn.execute(
        "SELECT * FROM understanding WHERE type = ? ORDER BY updated_at DESC",
        (utype,),
    ).fetchall()
    return [_row_to_understanding(r) for r in rows]


def count_understanding(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM understanding").fetchone()
    return row["c"] if row else 0


def update_understanding_status(
    conn: sqlite3.Connection, uid: str, status: str, retired_at: Optional[str] = None
) -> None:
    """Apply a lifecycle transition (the only live-row mutation). History keeps
    the prior version forever."""
    if retired_at is not None:
        conn.execute(
            "UPDATE understanding SET status = ?, retired_at = ? WHERE id = ?",
            (status, retired_at, uid),
        )
    else:
        conn.execute(
            "UPDATE understanding SET status = ? WHERE id = ?", (status, uid)
        )
    conn.commit()


def insert_understanding_history(conn: sqlite3.Connection, rows: List[UnderstandingHistoryRow]) -> None:
    """Append a full snapshot of understanding state for one build. Idempotent on
    (build_at, understanding_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO understanding_history
                (build_at, understanding_id, type, subject, statement, confidence,
                 status, knowledge_ids, created_at, updated_at, reinforced_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.understanding_id, r.type, r.subject, r.statement,
                r.confidence, r.status, r.knowledge_ids, r.created_at,
                r.updated_at, r.reinforced_count,
            ),
        )
    commit_if_top(conn)


def latest_understanding_snapshot(conn: sqlite3.Connection) -> List[UnderstandingHistoryRow]:
    """The most recent prior build snapshot, [] on cold start."""
    row = conn.execute("SELECT MAX(build_at) AS t FROM understanding_history").fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM understanding_history WHERE build_at = ? ORDER BY understanding_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_understanding_history(r) for r in rows]


def understanding_history_for(conn: sqlite3.Connection, uid: str) -> List[UnderstandingHistoryRow]:
    """Every snapshot of one understanding, oldest first."""
    rows = conn.execute(
        "SELECT * FROM understanding_history WHERE understanding_id = ? ORDER BY build_at",
        (uid,),
    ).fetchall()
    return [_row_to_understanding_history(r) for r in rows]


def insert_understanding_evolution(conn: sqlite3.Connection, rows: List[UnderstandingEvolutionRow]) -> None:
    """Append understanding evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO understanding_evolution
                (id, build_at, event_type, understanding_id, previous_confidence,
                 new_confidence, previous_status, new_status, previous_statement,
                 new_statement, reason, knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.understanding_id,
                r.previous_confidence, r.new_confidence, r.previous_status,
                r.new_status, r.previous_statement, r.new_statement, r.reason,
                r.knowledge_ids, r.timestamp,
            ),
        )
    commit_if_top(conn)


def understanding_evolution_all(conn: sqlite3.Connection) -> List[UnderstandingEvolutionRow]:
    """Every understanding evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM understanding_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_understanding_evolution(r) for r in rows]


def understanding_evolution_for(conn: sqlite3.Connection, uid: str) -> List[UnderstandingEvolutionRow]:
    """Evolution events touching one understanding, oldest first."""
    rows = conn.execute(
        "SELECT * FROM understanding_evolution WHERE understanding_id = ? ORDER BY timestamp, id",
        (uid,),
    ).fetchall()
    return [_row_to_understanding_evolution(r) for r in rows]


def _row_to_understanding(r) -> UnderstandingRow:
    return UnderstandingRow(
        id=r["id"],
        type=r["type"],
        subject=r["subject"],
        statement=r["statement"],
        confidence=r["confidence"],
        status=r["status"],
        knowledge_ids=r["knowledge_ids"] or "",
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        build_at=r["build_at"],
        retired_at=r["retired_at"],
    )


def _row_to_understanding_history(r) -> UnderstandingHistoryRow:
    return UnderstandingHistoryRow(
        build_at=r["build_at"],
        understanding_id=r["understanding_id"],
        type=r["type"],
        subject=r["subject"],
        statement=r["statement"],
        confidence=r["confidence"],
        status=r["status"],
        knowledge_ids=r["knowledge_ids"] or "",
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        reinforced_count=r["reinforced_count"] or 0,
    )


def _row_to_understanding_evolution(r) -> UnderstandingEvolutionRow:
    return UnderstandingEvolutionRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        understanding_id=r["understanding_id"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_statement=r["previous_statement"],
        new_statement=r["new_statement"],
        reason=r["reason"],
        knowledge_ids=r["knowledge_ids"] or "",
        timestamp=r["timestamp"],
    )


# ===========================================================================
# Initiative Engine storage (Milestone 8.4) — write-only layer over
# Understanding. Append-only. The Brain reads `initiatives` (new); every
# lower layer (understanding/knowledge/observation/context) is unchanged.
# ===========================================================================


@dataclass
class InitiativeRow:
    """One derived long-running engineering initiative."""

    id: str
    title: str
    initiative_type: str
    status: str
    confidence: str
    updated_at: str
    build_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = ""
    statement: str = ""
    participating_repositories: str = ""
    understanding_ids: str = ""
    knowledge_ids: str = ""
    schema_version: str = "1.0"


@dataclass
class InitiativeHistoryRow:
    """One snapshot of an initiative as of a single build."""

    build_at: str
    initiative_id: str
    title: str
    initiative_type: str
    status: str
    confidence: str
    participating_repositories: str = ""
    understanding_ids: str = ""
    knowledge_ids: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class InitiativeEvolutionRow:
    """One deterministic initiative lifecycle / merge / split event."""

    id: str
    build_at: str
    event_type: str
    initiative_id: str
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_title: Optional[str]
    new_title: Optional[str]
    reason: str
    parent_ids: str = ""
    child_ids: str = ""
    understanding_ids: str = ""
    knowledge_ids: str = ""
    timestamp: str = ""


@dataclass
class InitiativeRelationshipRow:
    """One explicit merge or split edge (parents <-> children)."""

    id: str
    relationship_type: str
    parent_ids: str
    child_ids: str
    build_at: str
    created_at: str
    note: Optional[str] = None


def insert_initiative(conn: sqlite3.Connection, rows: List[InitiativeRow]) -> None:
    """Insert or replace initiative entries. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO initiatives
                (id, title, initiative_type, status, confidence, statement,
                 started_at, updated_at, completed_at, participating_repositories,
                 understanding_ids, knowledge_ids, build_at, created_at,
                 schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, initiative_type=excluded.initiative_type,
                status=excluded.status, confidence=excluded.confidence,
                statement=excluded.statement, started_at=excluded.started_at,
                updated_at=excluded.updated_at, completed_at=excluded.completed_at,
                participating_repositories=excluded.participating_repositories,
                understanding_ids=excluded.understanding_ids,
                knowledge_ids=excluded.knowledge_ids, build_at=excluded.build_at,
                created_at=excluded.created_at, schema_version=excluded.schema_version
            """,
            (
                r.id, r.title, r.initiative_type, r.status, r.confidence,
                r.statement,
                r.started_at, r.updated_at, r.completed_at,
                r.participating_repositories, r.understanding_ids,
                r.knowledge_ids, r.build_at, r.created_at, r.schema_version,
            ),
        )
    commit_if_top(conn)


def get_all_initiatives(conn: sqlite3.Connection) -> List[InitiativeRow]:
    """Every initiative entry, newest-first by updated_at."""
    rows = conn.execute(
        "SELECT * FROM initiatives ORDER BY updated_at DESC"
    ).fetchall()
    return [_row_to_initiative(r) for r in rows]


def get_initiative_by_id(
    conn: sqlite3.Connection, iid: str
) -> Optional[InitiativeRow]:
    row = conn.execute(
        "SELECT * FROM initiatives WHERE id = ?", (iid,)
    ).fetchone()
    return _row_to_initiative(row) if row else None


def get_initiative_by_type(
    conn: sqlite3.Connection, itype: str
) -> List[InitiativeRow]:
    rows = conn.execute(
        "SELECT * FROM initiatives WHERE initiative_type = ? ORDER BY updated_at DESC",
        (itype,),
    ).fetchall()
    return [_row_to_initiative(r) for r in rows]


def count_initiatives(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM initiatives").fetchone()
    return row["c"] if row else 0


def update_initiative_status(
    conn: sqlite3.Connection,
    iid: str,
    status: str,
    completed_at: Optional[str] = None,
) -> None:
    """Apply a lifecycle transition (the only live-row mutation). History keeps
    the prior version forever."""
    if completed_at is not None:
        conn.execute(
            "UPDATE initiatives SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, iid),
        )
    else:
        conn.execute(
            "UPDATE initiatives SET status = ? WHERE id = ?", (status, iid)
        )
    conn.commit()


def insert_initiative_history(
    conn: sqlite3.Connection, rows: List[InitiativeHistoryRow]
) -> None:
    """Append a full snapshot of initiative state for one build. Idempotent on
    (build_at, initiative_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO initiative_history
                (build_at, initiative_id, title, initiative_type, status,
                 confidence, started_at, completed_at,
                 participating_repositories, understanding_ids, knowledge_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.initiative_id, r.title, r.initiative_type,
                r.status, r.confidence, r.started_at, r.completed_at,
                r.participating_repositories, r.understanding_ids,
                r.knowledge_ids,
            ),
        )
    commit_if_top(conn)


def latest_initiative_snapshot(
    conn: sqlite3.Connection,
) -> List[InitiativeHistoryRow]:
    """The most recent prior build snapshot, [] on cold start."""
    row = conn.execute(
        "SELECT MAX(build_at) AS t FROM initiative_history"
    ).fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM initiative_history WHERE build_at = ? ORDER BY initiative_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_initiative_history(r) for r in rows]


def initiative_history_for(
    conn: sqlite3.Connection, iid: str
) -> List[InitiativeHistoryRow]:
    """Every snapshot of one initiative, oldest first."""
    rows = conn.execute(
        "SELECT * FROM initiative_history WHERE initiative_id = ? ORDER BY build_at",
        (iid,),
    ).fetchall()
    return [_row_to_initiative_history(r) for r in rows]


def insert_initiative_evolution(
    conn: sqlite3.Connection, rows: List[InitiativeEvolutionRow]
) -> None:
    """Append initiative evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO initiative_evolution
                (id, build_at, event_type, initiative_id, parent_ids, child_ids,
                 previous_status, new_status, previous_confidence,
                 new_confidence, previous_title, new_title, reason,
                 understanding_ids, knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.initiative_id, r.parent_ids,
                r.child_ids, r.previous_status, r.new_status,
                r.previous_confidence, r.new_confidence, r.previous_title,
                r.new_title, r.reason, r.understanding_ids, r.knowledge_ids,
                r.timestamp,
            ),
        )
    commit_if_top(conn)


def initiative_evolution_all(
    conn: sqlite3.Connection,
) -> List[InitiativeEvolutionRow]:
    """Every initiative evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM initiative_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_initiative_evolution(r) for r in rows]


def initiative_evolution_for(
    conn: sqlite3.Connection, iid: str
) -> List[InitiativeEvolutionRow]:
    """Evolution events touching one initiative, oldest first."""
    rows = conn.execute(
        "SELECT * FROM initiative_evolution WHERE initiative_id = ? "
        "ORDER BY timestamp, id",
        (iid,),
    ).fetchall()
    return [_row_to_initiative_evolution(r) for r in rows]


def insert_initiative_relationships(
    conn: sqlite3.Connection, rows: List[InitiativeRelationshipRow]
) -> None:
    """Append explicit merge/split edges. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO initiative_relationships
                (id, relationship_type, parent_ids, child_ids, build_at,
                 created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.relationship_type, r.parent_ids, r.child_ids,
                r.build_at, r.created_at, r.note,
            ),
        )
    commit_if_top(conn)


def initiative_relationships_all(
    conn: sqlite3.Connection,
) -> List[InitiativeRelationshipRow]:
    rows = conn.execute(
        "SELECT * FROM initiative_relationships ORDER BY created_at"
    ).fetchall()
    return [_row_to_initiative_relationship(r) for r in rows]


def _row_to_initiative(r) -> InitiativeRow:
    return InitiativeRow(
        id=r["id"],
        title=r["title"],
        initiative_type=r["initiative_type"],
        status=r["status"],
        confidence=r["confidence"],
        statement=r["statement"] or "",
        started_at=r["started_at"],
        updated_at=r["updated_at"],
        completed_at=r["completed_at"],
        participating_repositories=r["participating_repositories"] or "",
        understanding_ids=r["understanding_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        build_at=r["build_at"],
        created_at=r["created_at"] or "",
    )


def _row_to_initiative_history(r) -> InitiativeHistoryRow:
    return InitiativeHistoryRow(
        build_at=r["build_at"],
        initiative_id=r["initiative_id"],
        title=r["title"],
        initiative_type=r["initiative_type"],
        status=r["status"],
        confidence=r["confidence"],
        participating_repositories=r["participating_repositories"] or "",
        understanding_ids=r["understanding_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        started_at=r["started_at"],
        completed_at=r["completed_at"],
    )


def _row_to_initiative_evolution(r) -> InitiativeEvolutionRow:
    return InitiativeEvolutionRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        initiative_id=r["initiative_id"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_title=r["previous_title"],
        new_title=r["new_title"],
        reason=r["reason"],
        parent_ids=r["parent_ids"] or "",
        child_ids=r["child_ids"] or "",
        understanding_ids=r["understanding_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        timestamp=r["timestamp"],
    )


def _row_to_initiative_relationship(r) -> InitiativeRelationshipRow:
    return InitiativeRelationshipRow(
        id=r["id"],
        relationship_type=r["relationship_type"],
        parent_ids=r["parent_ids"] or "",
        child_ids=r["child_ids"] or "",
        build_at=r["build_at"],
        created_at=r["created_at"],
        note=r["note"],
    )


# ===========================================================================
# Insight Engine storage (Milestone 8.5) — write-only layer over
# Understanding/Initiatives/Knowledge. Append-only. The Brain reads `insights`
# (new); every lower layer (understanding/initiatives/knowledge/observation/
# context) is unchanged.
# ===========================================================================


@dataclass
class InsightRow:
    """One derived engineering insight worth human attention."""

    id: str
    title: str
    insight_type: str
    statement: str
    status: str
    confidence: str
    updated_at: str
    build_at: str
    started_at: Optional[str] = None
    retired_at: Optional[str] = None
    created_at: str = ""
    understanding_ids: str = ""
    initiative_ids: str = ""
    knowledge_ids: str = ""
    schema_version: str = "1.0"


@dataclass
class InsightHistoryRow:
    """One snapshot of an insight as of a single build."""

    build_at: str
    insight_id: str
    title: str
    insight_type: str
    statement: str
    status: str
    confidence: str
    understanding_ids: str = ""
    initiative_ids: str = ""
    knowledge_ids: str = ""


@dataclass
class InsightEvolutionRow:
    """One deterministic insight lifecycle / retirement event."""

    id: str
    build_at: str
    event_type: str
    insight_id: str
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_statement: Optional[str]
    new_statement: Optional[str]
    reason: str
    understanding_ids: str = ""
    initiative_ids: str = ""
    knowledge_ids: str = ""
    timestamp: str = ""


def insert_insight(conn: sqlite3.Connection, rows: List[InsightRow]) -> None:
    """Insert or replace insight entries. Idempotent on id (stable per rule)."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO insights
                (id, title, insight_type, statement, status, confidence,
                 started_at, updated_at, retired_at, understanding_ids,
                 initiative_ids, knowledge_ids, build_at, created_at,
                 schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, insight_type=excluded.insight_type,
                statement=excluded.statement, status=excluded.status,
                confidence=excluded.confidence, started_at=excluded.started_at,
                updated_at=excluded.updated_at, retired_at=excluded.retired_at,
                understanding_ids=excluded.understanding_ids,
                initiative_ids=excluded.initiative_ids,
                knowledge_ids=excluded.knowledge_ids, build_at=excluded.build_at,
                created_at=excluded.created_at, schema_version=excluded.schema_version
            """,
            (
                r.id, r.title, r.insight_type, r.statement, r.status,
                r.confidence, r.started_at, r.updated_at, r.retired_at,
                r.understanding_ids, r.initiative_ids, r.knowledge_ids,
                r.build_at, r.created_at, r.schema_version,
            ),
        )
    commit_if_top(conn)


def get_all_insights(conn: sqlite3.Connection) -> List[InsightRow]:
    """Every insight entry, newest-first by updated_at."""
    rows = conn.execute(
        "SELECT * FROM insights ORDER BY updated_at DESC"
    ).fetchall()
    return [_row_to_insight(r) for r in rows]


def get_insight_by_id(
    conn: sqlite3.Connection, iid: str
) -> Optional[InsightRow]:
    row = conn.execute(
        "SELECT * FROM insights WHERE id = ?", (iid,)
    ).fetchone()
    return _row_to_insight(row) if row else None


def get_insights_by_type(
    conn: sqlite3.Connection, itype: str
) -> List[InsightRow]:
    rows = conn.execute(
        "SELECT * FROM insights WHERE insight_type = ? ORDER BY updated_at DESC",
        (itype,),
    ).fetchall()
    return [_row_to_insight(r) for r in rows]


def count_insights(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM insights").fetchone()
    return row["c"] if row else 0


def update_insight_status(
    conn: sqlite3.Connection,
    iid: str,
    status: str,
    retired_at: Optional[str] = None,
) -> None:
    """Apply a lifecycle transition (the only live-row mutation). History keeps
    the prior version forever."""
    if retired_at is not None:
        conn.execute(
            "UPDATE insights SET status = ?, retired_at = ? WHERE id = ?",
            (status, retired_at, iid),
        )
    else:
        conn.execute(
            "UPDATE insights SET status = ? WHERE id = ?", (status, iid)
        )
    conn.commit()


def insert_insight_history(
    conn: sqlite3.Connection, rows: List[InsightHistoryRow]
) -> None:
    """Append a full snapshot of insight state for one build. Idempotent on
    (build_at, insight_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO insight_history
                (build_at, insight_id, title, insight_type, statement, status,
                 confidence, understanding_ids, initiative_ids, knowledge_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.insight_id, r.title, r.insight_type, r.statement,
                r.status, r.confidence, r.understanding_ids, r.initiative_ids,
                r.knowledge_ids,
            ),
        )
    commit_if_top(conn)


def latest_insight_snapshot(
    conn: sqlite3.Connection,
) -> List[InsightHistoryRow]:
    """The most recent prior build snapshot, [] on cold start."""
    row = conn.execute(
        "SELECT MAX(build_at) AS t FROM insight_history"
    ).fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM insight_history WHERE build_at = ? ORDER BY insight_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_insight_history(r) for r in rows]


def insight_history_for(
    conn: sqlite3.Connection, iid: str
) -> List[InsightHistoryRow]:
    """Every snapshot of one insight, oldest first."""
    rows = conn.execute(
        "SELECT * FROM insight_history WHERE insight_id = ? ORDER BY build_at",
        (iid,),
    ).fetchall()
    return [_row_to_insight_history(r) for r in rows]


def insert_insight_evolution(
    conn: sqlite3.Connection, rows: List[InsightEvolutionRow]
) -> None:
    """Append insight evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO insight_evolution
                (id, build_at, event_type, insight_id, previous_status,
                 new_status, previous_confidence, new_confidence,
                 previous_statement, new_statement, reason, understanding_ids,
                 initiative_ids, knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.insight_id, r.previous_status,
                r.new_status, r.previous_confidence, r.new_confidence,
                r.previous_statement, r.new_statement, r.reason,
                r.understanding_ids, r.initiative_ids, r.knowledge_ids,
                r.timestamp,
            ),
        )
    commit_if_top(conn)


def insight_evolution_all(
    conn: sqlite3.Connection,
) -> List[InsightEvolutionRow]:
    """Every insight evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM insight_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_insight_evolution(r) for r in rows]


def insight_evolution_for(
    conn: sqlite3.Connection, iid: str
) -> List[InsightEvolutionRow]:
    """Evolution events touching one insight, oldest first."""
    rows = conn.execute(
        "SELECT * FROM insight_evolution WHERE insight_id = ? "
        "ORDER BY timestamp, id",
        (iid,),
    ).fetchall()
    return [_row_to_insight_evolution(r) for r in rows]


def _row_to_insight(r) -> InsightRow:
    return InsightRow(
        id=r["id"],
        title=r["title"],
        insight_type=r["insight_type"],
        statement=r["statement"],
        status=r["status"],
        confidence=r["confidence"],
        started_at=r["started_at"],
        updated_at=r["updated_at"],
        retired_at=r["retired_at"],
        created_at=r["created_at"] or "",
        understanding_ids=r["understanding_ids"] or "",
        initiative_ids=r["initiative_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        build_at=r["build_at"],
    )


def _row_to_insight_history(r) -> InsightHistoryRow:
    return InsightHistoryRow(
        build_at=r["build_at"],
        insight_id=r["insight_id"],
        title=r["title"],
        insight_type=r["insight_type"],
        statement=r["statement"],
        status=r["status"],
        confidence=r["confidence"],
        understanding_ids=r["understanding_ids"] or "",
        initiative_ids=r["initiative_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
    )


def _row_to_insight_evolution(r) -> InsightEvolutionRow:
    return InsightEvolutionRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        insight_id=r["insight_id"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_statement=r["previous_statement"],
        new_statement=r["new_statement"],
        reason=r["reason"],
        understanding_ids=r["understanding_ids"] or "",
        initiative_ids=r["initiative_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        timestamp=r["timestamp"],
    )




# ===========================================================================
# Planning Engine storage (Milestone 9.0) — write-only layer on top of
# Insights/Initiatives/Understanding/Knowledge. Append-only. The Brain reads
# `plans` (new); every lower layer is unchanged.
# ===========================================================================


@dataclass
class PlanRow:
    """One derived engineering plan (structured, evidence-backed)."""

    id: str
    goal: str
    plan_type: str
    confidence: str
    status: str
    milestones: str
    dependencies: str
    risks: str
    verification: str
    rollback: str
    estimated_complexity: str
    estimated_effort: str
    plan_text: str
    created_at: str
    updated_at: str
    affected_initiative_ids: str = ""
    affected_insight_ids: str = ""
    affected_understanding_ids: str = ""
    affected_knowledge_ids: str = ""
    schema_version: str = "1.0"


@dataclass
class PlanHistoryRow:
    """One snapshot of a plan as of a single generation."""

    generated_at: str
    plan_id: str
    goal: str
    plan_type: str
    confidence: str
    status: str
    milestones: str
    dependencies: str
    risks: str
    verification: str
    rollback: str
    estimated_complexity: str
    estimated_effort: str
    affected_initiative_ids: str = ""
    affected_insight_ids: str = ""
    affected_understanding_ids: str = ""
    affected_knowledge_ids: str = ""


@dataclass
class PlanEvolutionRow:
    """One deterministic plan lifecycle / supersession event."""

    id: str
    generated_at: str
    event_type: str
    plan_id: str
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    reason: str
    timestamp: str
    affected_initiative_ids: str = ""
    affected_insight_ids: str = ""
    affected_understanding_ids: str = ""
    affected_knowledge_ids: str = ""


def insert_plan(conn: sqlite3.Connection, rows: List[PlanRow]) -> None:
    """Insert or replace plan entries. Idempotent on id (stable per goal)."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO plans
                (id, goal, plan_type, confidence, status,
                 affected_initiative_ids, affected_insight_ids,
                 affected_understanding_ids, affected_knowledge_ids,
                 milestones, dependencies, risks, verification, rollback,
                 estimated_complexity, estimated_effort, plan_text,
                 schema_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                goal=excluded.goal, plan_type=excluded.plan_type,
                confidence=excluded.confidence, status=excluded.status,
                affected_initiative_ids=excluded.affected_initiative_ids,
                affected_insight_ids=excluded.affected_insight_ids,
                affected_understanding_ids=excluded.affected_understanding_ids,
                affected_knowledge_ids=excluded.affected_knowledge_ids,
                milestones=excluded.milestones, dependencies=excluded.dependencies,
                risks=excluded.risks, verification=excluded.verification,
                rollback=excluded.rollback,
                estimated_complexity=excluded.estimated_complexity,
                estimated_effort=excluded.estimated_effort,
                plan_text=excluded.plan_text, schema_version=excluded.schema_version,
                updated_at=excluded.updated_at
            """,
            (
                r.id, r.goal, r.plan_type, r.confidence, r.status,
                r.affected_initiative_ids, r.affected_insight_ids,
                r.affected_understanding_ids, r.affected_knowledge_ids,
                r.milestones, r.dependencies, r.risks, r.verification,
                r.rollback, r.estimated_complexity, r.estimated_effort,
                r.plan_text, r.schema_version, r.created_at, r.updated_at,
            ),
        )
    commit_if_top(conn)


def get_all_plans(conn: sqlite3.Connection) -> List[PlanRow]:
    """Every plan entry, newest-first by updated_at."""
    rows = conn.execute("SELECT * FROM plans ORDER BY updated_at DESC").fetchall()
    return [_row_to_plan(r) for r in rows]


def get_plan_by_id(conn: sqlite3.Connection, pid: str) -> Optional[PlanRow]:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
    return _row_to_plan(row) if row else None


def get_plans_by_type(conn: sqlite3.Connection, ptype: str) -> List[PlanRow]:
    rows = conn.execute(
        "SELECT * FROM plans WHERE plan_type = ? ORDER BY updated_at DESC",
        (ptype,),
    ).fetchall()
    return [_row_to_plan(r) for r in rows]


def count_plans(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()
    return row["c"] if row else 0


def update_plan_status(conn: sqlite3.Connection, pid: str, status: str) -> None:
    """Apply a lifecycle transition. History keeps the prior version forever."""
    conn.execute("UPDATE plans SET status = ? WHERE id = ?", (status, pid))
    conn.commit()


def insert_plan_history(conn: sqlite3.Connection, rows: List[PlanHistoryRow]) -> None:
    """Append a full snapshot of plan state for one generation. Idempotent on
    (generated_at, plan_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO plan_history
                (generated_at, plan_id, goal, plan_type, confidence, status,
                 affected_initiative_ids, affected_insight_ids,
                 affected_understanding_ids, affected_knowledge_ids,
                 milestones, dependencies, risks, verification, rollback,
                 estimated_complexity, estimated_effort)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.generated_at, r.plan_id, r.goal, r.plan_type, r.confidence,
                r.status, r.affected_initiative_ids, r.affected_insight_ids,
                r.affected_understanding_ids, r.affected_knowledge_ids,
                r.milestones, r.dependencies, r.risks, r.verification,
                r.rollback, r.estimated_complexity, r.estimated_effort,
            ),
        )
    commit_if_top(conn)


def latest_plan_snapshot(conn: sqlite3.Connection) -> List[PlanHistoryRow]:
    """The most recent prior generation snapshot, [] on cold start."""
    row = conn.execute(
        "SELECT MAX(generated_at) AS t FROM plan_history"
    ).fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM plan_history WHERE generated_at = ? ORDER BY plan_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_plan_history(r) for r in rows]


def plan_history_for(conn: sqlite3.Connection, pid: str) -> List[PlanHistoryRow]:
    """Every snapshot of one plan, oldest first."""
    rows = conn.execute(
        "SELECT * FROM plan_history WHERE plan_id = ? ORDER BY generated_at",
        (pid,),
    ).fetchall()
    return [_row_to_plan_history(r) for r in rows]


def insert_plan_evolution(conn: sqlite3.Connection, rows: List[PlanEvolutionRow]) -> None:
    """Append plan evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO plan_evolution
                (id, generated_at, event_type, plan_id, previous_status,
                 new_status, previous_confidence, new_confidence, reason,
                 affected_initiative_ids, affected_insight_ids,
                 affected_understanding_ids, affected_knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.generated_at, r.event_type, r.plan_id, r.previous_status,
                r.new_status, r.previous_confidence, r.new_confidence, r.reason,
                r.affected_initiative_ids, r.affected_insight_ids,
                r.affected_understanding_ids, r.affected_knowledge_ids,
                r.timestamp,
            ),
        )
    commit_if_top(conn)


def plan_evolution_all(conn: sqlite3.Connection) -> List[PlanEvolutionRow]:
    """Every plan evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM plan_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_plan_evolution(r) for r in rows]


def plan_evolution_for(conn: sqlite3.Connection, pid: str) -> List[PlanEvolutionRow]:
    """Evolution events touching one plan, oldest first."""
    rows = conn.execute(
        "SELECT * FROM plan_evolution WHERE plan_id = ? ORDER BY timestamp, id",
        (pid,),
    ).fetchall()
    return [_row_to_plan_evolution(r) for r in rows]


# ===========================================================================
# Task Graph Compiler storage (Milestone 9.1) — write-only layer on top of
# the Planning Engine. Append-only. The Brain reads `task_graphs` (new); every
# lower layer (including plans) is unchanged.
# ===========================================================================


@dataclass
class TaskGraphRow:
    """One compiled task graph (a deterministic DAG of tasks)."""

    id: str
    goal: str
    plan_id: str
    plan_type: str
    task_count: int
    edge_count: int
    critical_path_length: int
    parallel_groups: int
    status: str
    created_at: str
    updated_at: str


@dataclass
class TaskRow:
    """One executable task node in a compiled graph."""

    id: str
    graph_id: str
    plan_id: str
    milestone_order: int
    title: str
    description: str
    task_type: str
    required_capabilities: str
    complexity: str
    priority: str
    estimated_effort: str
    dependencies: str
    inputs: str
    outputs: str
    acceptance_criteria: str
    verification: str
    rollback: str
    evidence: str
    status: str
    confidence: str
    sequence: int


@dataclass
class TaskEdgeRow:
    """One dependency edge in a compiled graph (from_task depends on to_task)."""

    id: str
    graph_id: str
    from_task: str
    to_task: str
    kind: str


@dataclass
class TaskHistoryRow:
    """One append-only snapshot of a graph as of a single compilation."""

    generated_at: str
    graph_id: str
    goal: str
    task_count: int
    edge_count: int
    critical_path_length: int
    parallel_groups: int
    tasks_json: str
    edges_json: str


@dataclass
class TaskEvolutionRow:
    """One deterministic task-graph evolution event (append-only)."""

    id: str
    generated_at: str
    event_type: str
    graph_id: str
    previous_status: Optional[str]
    new_status: Optional[str]
    reason: str
    task_count: int
    edge_count: int
    timestamp: str


def insert_task_graph(conn: sqlite3.Connection, graphs: List[TaskGraphRow],
                      tasks: List[TaskRow], edges: List[TaskEdgeRow]) -> None:
    """Persist one compiled graph: header, tasks, edges (idempotent on ids).

    All three groups are written atomically — a crash mid-write leaves no
    partial graph (e.g. a header with zero tasks).
    """
    conn.commit()  # close any open implicit transaction from prior raw writes
    conn.execute("BEGIN TRANSACTION")
    try:
        for g in graphs:
            conn.execute(
                """
                INSERT INTO task_graphs
                    (id, goal, plan_id, plan_type, task_count, edge_count,
                     critical_path_length, parallel_groups, status, created_at,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal, plan_id=excluded.plan_id,
                    plan_type=excluded.plan_type, task_count=excluded.task_count,
                    edge_count=excluded.edge_count,
                    critical_path_length=excluded.critical_path_length,
                    parallel_groups=excluded.parallel_groups, status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (g.id, g.goal, g.plan_id, g.plan_type, g.task_count, g.edge_count,
                 g.critical_path_length, g.parallel_groups, g.status,
                 g.created_at, g.updated_at),
            )
        for t in tasks:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, graph_id, plan_id, milestone_order, title, description,
                     task_type, required_capabilities, complexity, priority,
                     estimated_effort, dependencies, inputs, outputs,
                     acceptance_criteria, verification, rollback, evidence, status,
                     confidence, sequence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    graph_id=excluded.graph_id, plan_id=excluded.plan_id,
                    milestone_order=excluded.milestone_order, title=excluded.title,
                    description=excluded.description, task_type=excluded.task_type,
                    required_capabilities=excluded.required_capabilities,
                    complexity=excluded.complexity, priority=excluded.priority,
                    estimated_effort=excluded.estimated_effort,
                    dependencies=excluded.dependencies, inputs=excluded.inputs,
                    outputs=excluded.outputs,
                    acceptance_criteria=excluded.acceptance_criteria,
                    verification=excluded.verification, rollback=excluded.rollback,
                    evidence=excluded.evidence, status=excluded.status,
                    confidence=excluded.confidence, sequence=excluded.sequence
                """,
                (t.id, t.graph_id, t.plan_id, t.milestone_order, t.title,
                 t.description, t.task_type, t.required_capabilities, t.complexity,
                 t.priority, t.estimated_effort, t.dependencies, t.inputs, t.outputs,
                 t.acceptance_criteria, t.verification, t.rollback, t.evidence,
                 t.status, t.confidence, t.sequence),
            )
        for e in edges:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_edges
                    (id, graph_id, from_task, to_task, kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                (e.id, e.graph_id, e.from_task, e.to_task, e.kind),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_all_task_graphs(conn: sqlite3.Connection) -> List[TaskGraphRow]:
    rows = conn.execute(
        "SELECT * FROM task_graphs ORDER BY updated_at DESC").fetchall()
    return [_row_to_task_graph(r) for r in rows]


def get_task_graph_by_id(conn: sqlite3.Connection, gid: str) -> Optional[TaskGraphRow]:
    row = conn.execute(
        "SELECT * FROM task_graphs WHERE id = ?", (gid,)).fetchone()
    return _row_to_task_graph(row) if row else None


def get_tasks_for_graph(conn: sqlite3.Connection, gid: str) -> List[TaskRow]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE graph_id = ? ORDER BY sequence", (gid,)
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_edges_for_graph(conn: sqlite3.Connection, gid: str) -> List[TaskEdgeRow]:
    rows = conn.execute(
        "SELECT * FROM task_edges WHERE graph_id = ?", (gid,)).fetchall()
    return [_row_to_task_edge(r) for r in rows]


def count_task_graphs(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM task_graphs").fetchone()
    return row["c"] if row else 0


def update_task_graph_status(conn: sqlite3.Connection, gid: str,
                             status: str) -> None:
    conn.execute(
        "UPDATE task_graphs SET status = ? WHERE id = ?", (status, gid))
    conn.commit()


def insert_task_history(conn: sqlite3.Connection,
                        rows: List[TaskHistoryRow]) -> None:
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO task_history
                (generated_at, graph_id, goal, task_count, edge_count,
                 critical_path_length, parallel_groups, tasks_json, edges_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (r.generated_at, r.graph_id, r.goal, r.task_count, r.edge_count,
             r.critical_path_length, r.parallel_groups, r.tasks_json,
             r.edges_json),
        )
    conn.commit()


def latest_task_graph_snapshot(conn: sqlite3.Connection) -> List[TaskHistoryRow]:
    row = conn.execute(
        "SELECT MAX(generated_at) AS t FROM task_history").fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM task_history WHERE generated_at = ? ORDER BY graph_id",
        (row["t"],)).fetchall()
    return [_row_to_task_history(r) for r in rows]


def task_history_for(conn: sqlite3.Connection, gid: str) -> List[TaskHistoryRow]:
    rows = conn.execute(
        "SELECT * FROM task_history WHERE graph_id = ? ORDER BY generated_at",
        (gid,)).fetchall()
    return [_row_to_task_history(r) for r in rows]


def insert_task_evolution(conn: sqlite3.Connection,
                          rows: List[TaskEvolutionRow]) -> None:
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO task_evolution
                (id, generated_at, event_type, graph_id, previous_status,
                 new_status, reason, task_count, edge_count, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (r.id, r.generated_at, r.event_type, r.graph_id, r.previous_status,
             r.new_status, r.reason, r.task_count, r.edge_count, r.timestamp),
        )
    conn.commit()


def task_evolution_all(conn: sqlite3.Connection) -> List[TaskEvolutionRow]:
    rows = conn.execute(
        "SELECT * FROM task_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_task_evolution(r) for r in rows]


def task_evolution_for(conn: sqlite3.Connection, gid: str) -> List[TaskEvolutionRow]:
    rows = conn.execute(
        "SELECT * FROM task_evolution WHERE graph_id = ? ORDER BY timestamp, id",
        (gid,)).fetchall()
    return [_row_to_task_evolution(r) for r in rows]


def _row_to_task_graph(r) -> TaskGraphRow:
    return TaskGraphRow(
        id=r["id"], goal=r["goal"], plan_id=r["plan_id"],
        plan_type=r["plan_type"], task_count=r["task_count"],
        edge_count=r["edge_count"],
        critical_path_length=r["critical_path_length"],
        parallel_groups=r["parallel_groups"], status=r["status"],
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _row_to_task(r) -> TaskRow:
    return TaskRow(
        id=r["id"], graph_id=r["graph_id"], plan_id=r["plan_id"],
        milestone_order=r["milestone_order"], title=r["title"],
        description=r["description"], task_type=r["task_type"],
        required_capabilities=r["required_capabilities"],
        complexity=r["complexity"], priority=r["priority"],
        estimated_effort=r["estimated_effort"], dependencies=r["dependencies"],
        inputs=r["inputs"], outputs=r["outputs"],
        acceptance_criteria=r["acceptance_criteria"],
        verification=r["verification"], rollback=r["rollback"],
        evidence=r["evidence"], status=r["status"], confidence=r["confidence"],
        sequence=r["sequence"],
    )


def _row_to_task_edge(r) -> TaskEdgeRow:
    return TaskEdgeRow(
        id=r["id"], graph_id=r["graph_id"], from_task=r["from_task"],
        to_task=r["to_task"], kind=r["kind"],
    )


def _row_to_task_history(r) -> TaskHistoryRow:
    return TaskHistoryRow(
        generated_at=r["generated_at"], graph_id=r["graph_id"], goal=r["goal"],
        task_count=r["task_count"], edge_count=r["edge_count"],
        critical_path_length=r["critical_path_length"],
        parallel_groups=r["parallel_groups"], tasks_json=r["tasks_json"],
        edges_json=r["edges_json"],
    )


def _row_to_task_evolution(r) -> TaskEvolutionRow:
    return TaskEvolutionRow(
        id=r["id"], generated_at=r["generated_at"], event_type=r["event_type"],
        graph_id=r["graph_id"], previous_status=r["previous_status"],
        new_status=r["new_status"], reason=r["reason"],
        task_count=r["task_count"], edge_count=r["edge_count"],
        timestamp=r["timestamp"],
    )


def _row_to_plan(r) -> PlanRow:
    return PlanRow(
        id=r["id"],
        goal=r["goal"],
        plan_type=r["plan_type"],
        confidence=r["confidence"],
        status=r["status"],
        milestones=r["milestones"] or "",
        dependencies=r["dependencies"] or "",
        risks=r["risks"] or "",
        verification=r["verification"] or "",
        rollback=r["rollback"] or "",
        estimated_complexity=r["estimated_complexity"] or "",
        estimated_effort=r["estimated_effort"] or "",
        plan_text=r["plan_text"] or "",
        created_at=r["created_at"] or "",
        updated_at=r["updated_at"] or "",
        affected_initiative_ids=r["affected_initiative_ids"] or "",
        affected_insight_ids=r["affected_insight_ids"] or "",
        affected_understanding_ids=r["affected_understanding_ids"] or "",
        affected_knowledge_ids=r["affected_knowledge_ids"] or "",
    )


def _row_to_plan_history(r) -> PlanHistoryRow:
    return PlanHistoryRow(
        generated_at=r["generated_at"],
        plan_id=r["plan_id"],
        goal=r["goal"],
        plan_type=r["plan_type"],
        confidence=r["confidence"],
        status=r["status"],
        milestones=r["milestones"] or "",
        dependencies=r["dependencies"] or "",
        risks=r["risks"] or "",
        verification=r["verification"] or "",
        rollback=r["rollback"] or "",
        estimated_complexity=r["estimated_complexity"] or "",
        estimated_effort=r["estimated_effort"] or "",
        affected_initiative_ids=r["affected_initiative_ids"] or "",
        affected_insight_ids=r["affected_insight_ids"] or "",
        affected_understanding_ids=r["affected_understanding_ids"] or "",
        affected_knowledge_ids=r["affected_knowledge_ids"] or "",
    )


def _row_to_plan_evolution(r) -> PlanEvolutionRow:
    return PlanEvolutionRow(
        id=r["id"],
        generated_at=r["generated_at"],
        event_type=r["event_type"],
        plan_id=r["plan_id"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        reason=r["reason"],
        timestamp=r["timestamp"],
        affected_initiative_ids=r["affected_initiative_ids"] or "",
        affected_insight_ids=r["affected_insight_ids"] or "",
        affected_understanding_ids=r["affected_understanding_ids"] or "",
        affected_knowledge_ids=r["affected_knowledge_ids"] or "",
    )


# ===========================================================================
# Worker Registry storage (Milestone 9.2) — write-only layer describing workers.
# Append-only history + version log. Every lower layer unchanged. No execution.
# ===========================================================================


@dataclass
class WorkerRow:
    """One registered worker (a capability profile; NEVER an execution)."""

    id: str
    name: str
    kind: str
    description: str = ""
    capabilities: str = ""
    supported_languages: str = ""
    supported_task_types: str = ""
    supported_plan_types: str = ""
    limitations: str = ""
    estimated_speed: str = ""
    estimated_cost: str = ""
    context_window: int = 0
    parallelism: int = 1
    requires_network: bool = False
    requires_filesystem: bool = False
    requires_git: bool = False
    requires_python: bool = False
    requires_shell: bool = False
    confidence: str = "medium"
    version: str = "1.0.0"
    status: str = "active"
    schema_version: str = "1.0"
    created_at: str = ""
    updated_at: str = ""
    availability: str = "available"
    manifest_ref: Optional[str] = None


@dataclass
class WorkerHistoryRow:
    """One append-only snapshot of a worker per registration event."""

    registered_at: str
    worker_id: str
    name: str
    kind: str
    version: str
    status: str
    capabilities: str = ""
    limitations: str = ""
    event_type: str = "registered"
    note: Optional[str] = None


@dataclass
class WorkerVersionRow:
    """One append-only version record for a worker."""

    worker_id: str
    version: str
    registered_at: str
    changelog: Optional[str] = None


def insert_worker(conn: sqlite3.Connection, w: WorkerRow) -> None:
    """Insert or replace a worker by id (idempotent on id)."""
    conn.execute(
        """
        INSERT INTO workers
            (id, name, kind, description, capabilities, supported_languages,
             supported_task_types, supported_plan_types, limitations,
             estimated_speed, estimated_cost, context_window, parallelism,
             requires_network, requires_filesystem, requires_git,
             requires_python, requires_shell, confidence, version, status,
             schema_version, created_at, updated_at, availability, manifest_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, kind=excluded.kind, description=excluded.description,
            capabilities=excluded.capabilities,
            supported_languages=excluded.supported_languages,
            supported_task_types=excluded.supported_task_types,
            supported_plan_types=excluded.supported_plan_types,
            limitations=excluded.limitations, estimated_speed=excluded.estimated_speed,
            estimated_cost=excluded.estimated_cost,
            context_window=excluded.context_window, parallelism=excluded.parallelism,
            requires_network=excluded.requires_network,
            requires_filesystem=excluded.requires_filesystem,
            requires_git=excluded.requires_git, requires_python=excluded.requires_python,
            requires_shell=excluded.requires_shell, confidence=excluded.confidence,
            version=excluded.version, status=excluded.status,
            schema_version=excluded.schema_version, updated_at=excluded.updated_at,
            availability=excluded.availability, manifest_ref=excluded.manifest_ref
        """,
        (
            w.id, w.name, w.kind, w.description, w.capabilities,
            w.supported_languages, w.supported_task_types, w.supported_plan_types,
            w.limitations, w.estimated_speed, w.estimated_cost, w.context_window,
            w.parallelism, int(w.requires_network), int(w.requires_filesystem),
            int(w.requires_git), int(w.requires_python), int(w.requires_shell),
            w.confidence, w.version, w.status, w.schema_version,
            w.created_at, w.updated_at, w.availability, w.manifest_ref,
        ),
    )
    # Re-sync normalized capability rows.
    conn.execute(
        "DELETE FROM worker_capabilities WHERE worker_id = ?", (w.id,)
    )
    conn.executemany(
        "INSERT OR IGNORE INTO worker_capabilities (worker_id, capability) "
        "VALUES (?, ?)",
        [(w.id, c) for c in (w.capabilities.split(",") if w.capabilities else [])],
    )
    commit_if_top(conn)


def replace_worker_capabilities(
    conn: sqlite3.Connection, worker_id: str, capabilities: list[str]
) -> None:
    conn.execute(
        "DELETE FROM worker_capabilities WHERE worker_id = ?", (worker_id,)
    )
    conn.executemany(
        "INSERT OR IGNORE INTO worker_capabilities (worker_id, capability) "
        "VALUES (?, ?)",
        [(worker_id, c) for c in capabilities],
    )
    conn.commit()


def get_worker(conn: sqlite3.Connection, wid: str) -> Optional[WorkerRow]:
    row = conn.execute("SELECT * FROM workers WHERE id = ?", (wid,)).fetchone()
    return _row_to_worker(row) if row else None


def get_worker_by_name(conn: sqlite3.Connection, name: str) -> Optional[WorkerRow]:
    row = conn.execute(
        "SELECT * FROM workers WHERE name = ?", (name,)
    ).fetchone()
    return _row_to_worker(row) if row else None


def get_all_workers(conn: sqlite3.Connection) -> List[WorkerRow]:
    rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
    return [_row_to_worker(r) for r in rows]


def count_workers(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM workers").fetchone()
    return row["c"] if row else 0


def workers_with_capability(
    conn: sqlite3.Connection, capability: str
) -> List[WorkerRow]:
    """Capability Resolver query hook: every worker exposing `capability`.
    Case-insensitive (the vocabulary stores canonical-capitalized forms)."""
    rows = conn.execute(
        """
        SELECT w.* FROM workers w
        JOIN worker_capabilities c ON c.worker_id = w.id
        WHERE LOWER(c.capability) = LOWER(?)
        ORDER BY w.name
        """,
        (capability,),
    ).fetchall()
    return [_row_to_worker(r) for r in rows]


def update_worker_status(
    conn: sqlite3.Connection, wid: str, status: str
) -> None:
    """The only live-row mutation: enable/disable a worker. History keeps the
    prior version forever."""
    conn.execute("UPDATE workers SET status = ? WHERE id = ?", (status, wid))
    conn.commit()


def update_worker_version(
    conn: sqlite3.Connection, wid: str, version: str
) -> None:
    """Advance a worker's live version (the only other live-row mutation)."""
    conn.execute("UPDATE workers SET version = ? WHERE id = ?", (version, wid))
    conn.commit()


def update_worker_availability(conn: sqlite3.Connection, worker_id: str,
                               availability: str) -> None:
    """Update ONLY the availability column (runtime install state). Distinct
    from `status` (active/disabled); availability is available|unavailable|error."""
    conn.execute(
        "UPDATE workers SET availability = ? WHERE id = ?",
        (availability, worker_id))
    conn.commit()


def insert_worker_history(conn: sqlite3.Connection, rows: List[WorkerHistoryRow]) -> None:
    """Append a snapshot of worker state per registration/upgrade/disable event.
    Idempotent on (registered_at, worker_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO worker_history
                (registered_at, worker_id, name, kind, version, status,
                 capabilities, limitations, event_type, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.registered_at, r.worker_id, r.name, r.kind, r.version,
                r.status, r.capabilities, r.limitations, r.event_type, r.note,
            ),
        )
    conn.commit()


def insert_worker_version(conn: sqlite3.Connection, rows: List[WorkerVersionRow]) -> None:
    """Append a version record. Idempotent on (worker_id, version)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO worker_versions
                (worker_id, version, registered_at, changelog)
            VALUES (?, ?, ?, ?)
            """,
            (r.worker_id, r.version, r.registered_at, r.changelog),
        )
    conn.commit()


def worker_history_for(
    conn: sqlite3.Connection, wid: str
) -> List[WorkerHistoryRow]:
    """Every snapshot of one worker, newest first."""
    rows = conn.execute(
        "SELECT * FROM worker_history WHERE worker_id = ? ORDER BY registered_at DESC",
        (wid,),
    ).fetchall()
    return [_row_to_worker_history(r) for r in rows]


def worker_versions_for(
    conn: sqlite3.Connection, wid: str
) -> List[WorkerVersionRow]:
    rows = conn.execute(
        "SELECT * FROM worker_versions WHERE worker_id = ? ORDER BY registered_at",
        (wid,),
    ).fetchall()
    return [_row_to_worker_version(r) for r in rows]


def _row_to_worker(r) -> WorkerRow:
    return WorkerRow(
        id=r["id"], name=r["name"], kind=r["kind"],
        description=r["description"] or "",
        capabilities=r["capabilities"] or "",
        supported_languages=r["supported_languages"] or "",
        supported_task_types=r["supported_task_types"] or "",
        supported_plan_types=r["supported_plan_types"] or "",
        limitations=r["limitations"] or "",
        estimated_speed=r["estimated_speed"] or "",
        estimated_cost=r["estimated_cost"] or "",
        context_window=r["context_window"] or 0,
        parallelism=r["parallelism"] or 1,
        requires_network=bool(r["requires_network"]),
        requires_filesystem=bool(r["requires_filesystem"]),
        requires_git=bool(r["requires_git"]),
        requires_python=bool(r["requires_python"]),
        requires_shell=bool(r["requires_shell"]),
        confidence=r["confidence"] or "medium",
        version=r["version"] or "1.0.0",
        status=r["status"] or "active",
        created_at=r["created_at"] or "",
        updated_at=r["updated_at"] or "",
        availability=r["availability"] or "available",
        manifest_ref=r["manifest_ref"],
    )


def _row_to_worker_history(r) -> WorkerHistoryRow:
    return WorkerHistoryRow(
        registered_at=r["registered_at"], worker_id=r["worker_id"],
        name=r["name"], kind=r["kind"], version=r["version"],
        status=r["status"], capabilities=r["capabilities"] or "",
        limitations=r["limitations"] or "", event_type=r["event_type"] or "registered",
        note=r["note"],
    )


def _row_to_worker_version(r) -> WorkerVersionRow:
    return WorkerVersionRow(
        worker_id=r["worker_id"], version=r["version"],
        registered_at=r["registered_at"], changelog=r["changelog"],
    )


# ===========================================================================
# M9.3 Capability Resolver — persistence helpers (append-only history)
# ===========================================================================

@dataclass
class ResolverAssignmentRow:
    """One persisted Task -> Worker assignment (resolver_assignments)."""
    assignment_id: str
    graph_id: str
    task_id: str
    worker_id: Optional[str]
    status: str
    confidence: str
    reason: str
    matched_capabilities: str
    missing_capabilities: str
    selection_strategy: str
    schema_version: str
    created_at: str
    updated_at: str


def insert_resolver_assignment(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one assignment, or UPDATE an existing one in place.

    Uses UPDATE (not INSERT OR REPLACE) on re-resolution so the original row id
    is preserved — INSERT OR REPLACE would DELETE+INSERT, which cascades to
    resolver_history and breaks append-only history. The assignment's prior
    state lives in history; the live row is simply advanced.
    """
    existing = conn.execute(
        "SELECT 1 FROM resolver_assignments WHERE assignment_id = ?",
        (row["assignment_id"],),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO resolver_assignments
                (assignment_id, graph_id, task_id, worker_id, status, confidence,
                 reason, matched_capabilities, missing_capabilities,
                 selection_strategy, schema_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["assignment_id"], row["graph_id"], row["task_id"], row["worker_id"],
             row["status"], row["confidence"], row["reason"],
             row["matched_capabilities"], row["missing_capabilities"],
             row["selection_strategy"], row["schema_version"],
             row["created_at"], row["updated_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE resolver_assignments
                SET worker_id = ?, status = ?, confidence = ?, reason = ?,
                    matched_capabilities = ?, missing_capabilities = ?,
                    selection_strategy = ?, schema_version = ?, updated_at = ?
                WHERE assignment_id = ?
            """,
            (row["worker_id"], row["status"], row["confidence"], row["reason"],
             row["matched_capabilities"], row["missing_capabilities"],
             row["selection_strategy"], row["schema_version"],
             row["updated_at"], row["assignment_id"]),
        )


def insert_resolver_history(conn: sqlite3.Connection, row: dict) -> None:
    """Append one resolution-run snapshot (append-only, never updated)."""
    conn.execute(
        """
        INSERT INTO resolver_history
            (resolved_at, assignment_id, graph_id, task_id, worker_id, status,
             confidence, score_total, matched_capabilities,
             missing_capabilities, selection_strategy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["resolved_at"], row["assignment_id"], row["graph_id"],
         row["task_id"], row["worker_id"], row["status"], row["confidence"],
         row["score_total"], row["matched_capabilities"],
         row["missing_capabilities"], row["selection_strategy"]),
    )


def insert_resolver_evolution(conn: sqlite3.Connection, row: dict) -> None:
    """Record one assignment-change event (append-only)."""
    conn.execute(
        """
        INSERT INTO resolver_evolution
            (evolved_at, graph_id, task_id, from_worker_id, to_worker_id,
             change_type, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["evolved_at"], row["graph_id"], row["task_id"],
         row["from_worker_id"], row["to_worker_id"], row["change_type"],
         row["reason"]),
    )


def get_resolver_assignments(conn: sqlite3.Connection,
                             graph_id: Optional[str] = None
                             ) -> List[ResolverAssignmentRow]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM resolver_assignments ORDER BY graph_id, task_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolver_assignments WHERE graph_id = ? "
            "ORDER BY task_id", (graph_id,)).fetchall()
    return [_row_to_resolver_assignment(r) for r in rows]


def get_resolver_assignment(conn: sqlite3.Connection,
                           assignment_id: str) -> Optional[ResolverAssignmentRow]:
    row = conn.execute(
        "SELECT * FROM resolver_assignments WHERE assignment_id = ?",
        (assignment_id,)).fetchone()
    return _row_to_resolver_assignment(row) if row else None


def get_resolver_assignment_by_task(conn: sqlite3.Connection,
                                    task_id: str
                                    ) -> Optional[ResolverAssignmentRow]:
    """Lookup a resolver assignment by its task id (not assignment_id).

    Orders by `updated_at` (the live row's recency); `resolver_assignments`
    has no `resolved_at` column — per-run recency lives in `resolver_history`.
    """
    row = conn.execute(
        "SELECT * FROM resolver_assignments WHERE task_id = ? "
        "ORDER BY updated_at DESC", (task_id,)).fetchone()
    return _row_to_resolver_assignment(row) if row else None


def get_resolver_history(conn: sqlite3.Connection,
                        assignment_id: Optional[str] = None
                        ) -> list:
    if assignment_id is None:
        rows = conn.execute(
            "SELECT * FROM resolver_history ORDER BY resolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolver_history WHERE assignment_id = ? "
            "ORDER BY resolved_at", (assignment_id,)).fetchall()
    return [dict(r) for r in rows]


def get_resolver_evolution(conn: sqlite3.Connection,
                          graph_id: Optional[str] = None) -> list:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM resolver_evolution ORDER BY evolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolver_evolution WHERE graph_id = ? "
            "ORDER BY evolved_at", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def count_resolver_assignments(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM resolver_assignments").fetchone()
    return row["c"] if row else 0


def _row_to_resolver_assignment(r) -> ResolverAssignmentRow:
    return ResolverAssignmentRow(
        assignment_id=r["assignment_id"], graph_id=r["graph_id"],
        task_id=r["task_id"], worker_id=r["worker_id"], status=r["status"],
        confidence=r["confidence"], reason=r["reason"] or "",
        matched_capabilities=r["matched_capabilities"] or "[]",
        missing_capabilities=r["missing_capabilities"] or "[]",
        selection_strategy=r["selection_strategy"],
        schema_version=r["schema_version"] if "schema_version" in r.keys() else "1.0",
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


# ===========================================================================
# M9.4 Task Scheduler — persistence helpers (append-only history/evolution)
# ===========================================================================

@dataclass
class SchedulerTaskRow:
    """One persisted scheduled task (scheduler_tasks)."""
    schedule_id: str
    graph_id: str
    assignment_id: str
    task_id: str
    worker_id: Optional[str]
    phase: str
    status: str
    priority: int
    wave: int
    dependency_count: int
    estimated_start: Optional[int]
    estimated_finish: Optional[int]
    blocked_reason: str
    confidence: str
    selection_strategy: str
    schema_version: str
    created_at: str
    updated_at: str


def insert_scheduler_run(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or replace one scheduler run record (one per graph run)."""
    conn.execute(
        """
        INSERT INTO scheduler_runs
            (run_id, graph_id, goal, wave_count, task_count,
             critical_path_length, max_parallelism, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            goal=excluded.goal, wave_count=excluded.wave_count,
            task_count=excluded.task_count,
            critical_path_length=excluded.critical_path_length,
            max_parallelism=excluded.max_parallelism,
            status=excluded.status, updated_at=excluded.updated_at
        """,
        (row["run_id"], row["graph_id"], row["goal"], row["wave_count"],
         row["task_count"], row["critical_path_length"],
         row["max_parallelism"], row["status"],
         row["created_at"], row["updated_at"]),
    )


def insert_scheduler_task(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one scheduled task, or UPDATE an existing one in place.

    UPDATE (not INSERT OR REPLACE) preserves the row id so scheduler_history
    (FK ON DELETE SET NULL) is never cascade-deleted. Append-only history keeps
    the prior state.
    """
    existing = conn.execute(
        "SELECT 1 FROM scheduler_tasks WHERE schedule_id = ?",
        (row["schedule_id"],),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO scheduler_tasks
                (schedule_id, graph_id, assignment_id, task_id, worker_id,
                 phase, status, priority, wave, dependency_count,
                 estimated_start, estimated_finish, blocked_reason,
                 confidence, selection_strategy, schema_version,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["schedule_id"], row["graph_id"], row["assignment_id"],
             row["task_id"], row["worker_id"], row["phase"], row["status"],
             row["priority"], row["wave"], row["dependency_count"],
             row["estimated_start"], row["estimated_finish"],
             row["blocked_reason"], row["confidence"],
             row["selection_strategy"], row["schema_version"],
             row["created_at"], row["updated_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE scheduler_tasks
                SET graph_id = ?, assignment_id = ?, worker_id = ?,
                    phase = ?, status = ?, priority = ?, wave = ?,
                    dependency_count = ?, estimated_start = ?,
                    estimated_finish = ?, blocked_reason = ?,
                    confidence = ?, selection_strategy = ?, schema_version = ?,
                    updated_at = ?
                WHERE schedule_id = ?
            """,
            (row["graph_id"], row["assignment_id"], row["worker_id"],
             row["phase"], row["status"], row["priority"], row["wave"],
             row["dependency_count"], row["estimated_start"],
             row["estimated_finish"], row["blocked_reason"], row["confidence"],
             row["selection_strategy"], row["schema_version"],
             row["updated_at"], row["schedule_id"]),
        )


def insert_scheduler_history(conn: sqlite3.Connection, row: dict) -> None:
    """Append one scheduling-run snapshot (append-only, never updated)."""
    conn.execute(
        """
        INSERT INTO scheduler_history
            (scheduled_at, schedule_id, graph_id, task_id, worker_id, wave,
             status, priority, assignment_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["scheduled_at"], row["schedule_id"], row["graph_id"],
         row["task_id"], row["worker_id"], row["wave"], row["status"],
         row["priority"], row["assignment_id"]),
    )


def insert_scheduler_evolution(conn: sqlite3.Connection, row: dict) -> None:
    """Record one scheduler decision change (append-only)."""
    conn.execute(
        """
        INSERT INTO scheduler_evolution
            (evolved_at, schedule_id, graph_id, task_id, from_wave, to_wave,
             from_state, to_state, change_type, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["evolved_at"], row["schedule_id"], row["graph_id"],
         row["task_id"], row["from_wave"], row["to_wave"],
         row["from_state"], row["to_state"], row["change_type"],
         row["reason"]),
    )


def get_scheduler_tasks(conn: sqlite3.Connection,
                        graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_tasks ORDER BY graph_id, wave, priority DESC, task_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_tasks WHERE graph_id = ? "
            "ORDER BY wave, priority DESC, task_id", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduler_task(conn: sqlite3.Connection, schedule_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM scheduler_tasks WHERE schedule_id = ?",
        (schedule_id,)).fetchone()
    return dict(row) if row else None


def get_scheduler_runs(conn: sqlite3.Connection,
                       graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_runs ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_runs WHERE graph_id = ? "
            "ORDER BY created_at DESC", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduler_history(conn: sqlite3.Connection,
                          graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_history ORDER BY scheduled_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_history WHERE graph_id = ? "
            "ORDER BY scheduled_at", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduler_evolution(conn: sqlite3.Connection,
                            graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_evolution ORDER BY evolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_evolution WHERE graph_id = ? "
            "ORDER BY evolved_at", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# M9.5 Execution Runtime — persistence helpers
# ===========================================================================

def insert_runtime_session(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_sessions
            (session_id, schedule_id, state, started_at, finished_at,
             schema_version, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["session_id"], row["schedule_id"], row["state"],
         row["started_at"], row.get("finished_at"),
         row.get("schema_version", "1.0"),
         row["created_at"], row["updated_at"]),
    )


def update_runtime_session(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        UPDATE runtime_sessions
        SET state = ?, finished_at = ?, updated_at = ?
        WHERE session_id = ?
        """,
        (row["state"], row.get("finished_at"), row["updated_at"],
         row["session_id"]),
    )


def get_runtime_sessions(conn: sqlite3.Connection,
                         schedule_id: Optional[str] = None) -> List[dict]:
    if schedule_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_sessions ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_sessions WHERE schedule_id = ? "
            "ORDER BY created_at DESC", (schedule_id,)).fetchall()
    return [dict(r) for r in rows]


def get_runtime_session(conn: sqlite3.Connection,
                        session_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM runtime_sessions WHERE session_id = ?",
        (session_id,)).fetchone()
    return dict(row) if row else None


def insert_runtime_event(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_events
            (event_id, session_id, kind, task_id, worker_id, detail, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_id"], row["session_id"], row["kind"],
         row.get("task_id", ""), row.get("worker_id"),
         row.get("detail", ""), row["at"]),
    )


def get_runtime_events(conn: sqlite3.Connection,
                       session_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM runtime_events WHERE session_id = ? ORDER BY eid",
        (session_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_runtime_task(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or UPDATE a task's latest state in place (the only mutable
    runtime table). A crash mid-run leaves a consistent last-known state."""
    existing = conn.execute(
        "SELECT 1 FROM runtime_tasks WHERE execution_id = ?",
        (row["execution_id"],)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO runtime_tasks
                (execution_id, session_id, schedule_id, task_id, worker_id,
                 wave, attempt, status, started_at, finished_at, duration_ms,
                 exit_code, error, output_reference, schema_version,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["execution_id"], row["session_id"], row["schedule_id"],
             row["task_id"], row.get("worker_id"),
             row.get("wave", 1), row.get("attempt", 1), row["status"],
             row.get("started_at"), row.get("finished_at"),
             row.get("duration_ms"), row.get("exit_code"), row.get("error", ""),
             row.get("output_reference"),
             row.get("schema_version", "1.0"),
             row["created_at"], row["updated_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE runtime_tasks
            SET session_id = ?, schedule_id = ?, worker_id = ?, wave = ?,
                attempt = ?, status = ?, started_at = ?, finished_at = ?,
                duration_ms = ?, exit_code = ?, error = ?, output_reference = ?,
                updated_at = ?
            WHERE execution_id = ?
            """,
            (row["session_id"], row["schedule_id"], row.get("worker_id"),
             row.get("wave", 1), row.get("attempt", 1), row["status"],
             row.get("started_at"), row.get("finished_at"),
             row.get("duration_ms"), row.get("exit_code"), row.get("error", ""),
             row.get("output_reference"), row["updated_at"],
             row["execution_id"]),
        )


def get_runtime_tasks(conn: sqlite3.Connection,
                      session_id: Optional[str] = None) -> List[dict]:
    if session_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_tasks ORDER BY session_id, wave, task_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_tasks WHERE session_id = ? "
            "ORDER BY wave, task_id", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def get_runtime_task(conn: sqlite3.Connection,
                     execution_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM runtime_tasks WHERE execution_id = ?",
        (execution_id,)).fetchone()
    return dict(row) if row else None


def insert_runtime_result(conn: sqlite3.Connection, row: dict) -> None:
    """Append-only outcome of one execution attempt."""
    conn.execute(
        """
        INSERT INTO runtime_results
            (execution_id, session_id, task_id, worker_id, success, stdout,
             stderr, artifacts, exit_code, duration_ms, error, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["execution_id"], row["session_id"], row["task_id"],
         row.get("worker_id"),
         1 if row.get("success") else 0,
         row.get("stdout", ""), row.get("stderr", ""), row.get("artifacts", "[]"),
         row.get("exit_code"), row.get("duration_ms", 0), row.get("error", ""),
         row["recorded_at"]),
    )


def get_runtime_results(conn: sqlite3.Connection,
                        session_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM runtime_results WHERE session_id = ? ORDER BY result_id",
        (session_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_runtime_history(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_history
            (session_id, schedule_id, task_id, worker_id, status, attempt, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["session_id"], row["schedule_id"], row["task_id"],
         row.get("worker_id"), row["status"], row.get("attempt", 1),
         row["at"]),
    )


def get_runtime_history(conn: sqlite3.Connection,
                        session_id: Optional[str] = None) -> List[dict]:
    if session_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_history ORDER BY hid"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_history WHERE session_id = ? ORDER BY hid",
            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_runtime_evolution(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_evolution
            (evolved_at, session_id, task_id, from_state, to_state,
             change_type, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["evolved_at"], row["session_id"], row["task_id"],
         row.get("from_state"), row.get("to_state"),
         row["change_type"], row.get("reason", "")),
    )


def get_runtime_evolution(conn: sqlite3.Connection,
                          session_id: Optional[str] = None) -> List[dict]:
    if session_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_evolution ORDER BY evolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_evolution WHERE session_id = ? "
            "ORDER BY evolved_at", (session_id,)).fetchall()
    return [dict(r) for r in rows]
