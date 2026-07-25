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
    updated_at              TEXT NOT NULL,
    source                  TEXT
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
    symbolic                TEXT NOT NULL DEFAULT '{}',
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
-- Worker Genesis: proposed workers (capability gap proposals awaiting review).
-- ===========================================================================

-- Proposals for new workers detected from capability gaps. Never auto-approved;
-- status transitions: pending -> approved | rejected. Only approved proposals
-- are registered into the live Worker Registry.
CREATE TABLE IF NOT EXISTS proposed_workers (
    id                  TEXT PRIMARY KEY,
    detected_from_goal  TEXT NOT NULL,
    capability_gap      TEXT NOT NULL,
    draft_manifest_json TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL,
    reviewed_at         TEXT
);

CREATE TABLE IF NOT EXISTS operator_preferences (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    set_at      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'explicit' CHECK(source IN ('explicit', 'derived'))
);

-- ===========================================================================
-- M10.1 Observer Cursors (Law 18 — per-layer bookkeeping, NOT operator state)
-- ===========================================================================

-- Normalized per-observer set of already-observed session IDs. One row per
-- (observer_name, session_id) composite PK. Used by RuntimeObserver to track
-- which sessions have been observed without relying on timestamp watermarks
-- (which have a clock-mismatch off-by-one). The cursor is mutable bookkeeping
-- in the Observation layer (Law 18 compliant: separated from operator state).
-- Append-only fact tables (observations, runtime_*) are never mutated by
-- this cursor — it only points INTO them.
-- Versioned per Law 24: schema_version = 1.
CREATE TABLE IF NOT EXISTS observed_session_ids (
    observer_name   TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    schema_version  TEXT NOT NULL DEFAULT '1',
    PRIMARY KEY (observer_name, session_id)
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
    payload              TEXT,                    -- JSON: worker_id + input args, for replay
    verification_passed  INTEGER,
    verification_evidence TEXT NOT NULL DEFAULT '{}',
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

-- ===========================================================================
-- Indexes for hot-path WHERE clauses (FK columns, status filters, etc.)
-- ===========================================================================
-- These are purely additive. No schema changes, no column additions.
-- CREATE INDEX IF NOT EXISTS guarantees idempotency on reconnect.

-- Graph/task FK lookups (tasks.graph_id, task_edges.graph_id, etc.)
CREATE INDEX IF NOT EXISTS idx_tasks_graph_id ON tasks(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_edges_graph_id ON task_edges(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_history_graph_id ON task_history(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_evolution_graph_id ON task_evolution(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_graphs_status ON task_graphs(status);

-- Resolver FK lookups
CREATE INDEX IF NOT EXISTS idx_resolver_evolution_graph_id ON resolver_evolution(graph_id);
CREATE INDEX IF NOT EXISTS idx_resolver_history_assignment_id ON resolver_history(assignment_id);

-- Scheduler FK lookups
CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_graph_id ON scheduler_tasks(graph_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_history_graph_id ON scheduler_history(graph_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_history_schedule_id ON scheduler_history(schedule_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_graph_id ON scheduler_runs(graph_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_evolution_graph_id ON scheduler_evolution(graph_id);

-- Worker & proposal lookups
CREATE INDEX IF NOT EXISTS idx_worker_history_worker_id ON worker_history(worker_id);
CREATE INDEX IF NOT EXISTS idx_proposed_workers_status ON proposed_workers(status);
-- Knowledge evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_knowledge_history_knowledge_id ON knowledge_history(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_evolution_events_knowledge_id ON evolution_events(knowledge_id);

-- Understanding evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_understanding_history_understanding_id ON understanding_history(understanding_id);
CREATE INDEX IF NOT EXISTS idx_understanding_evolution_understanding_id ON understanding_evolution(understanding_id);

-- Initiative evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_initiative_history_initiative_id ON initiative_history(initiative_id);
CREATE INDEX IF NOT EXISTS idx_initiative_evolution_initiative_id ON initiative_evolution(initiative_id);

-- Insight evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_insight_history_insight_id ON insight_history(insight_id);
CREATE INDEX IF NOT EXISTS idx_insight_evolution_insight_id ON insight_evolution(insight_id);

-- Plan evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_plan_history_plan_id ON plan_history(plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_evolution_plan_id ON plan_evolution(plan_id);

-- Relationship cross-reference lookups (OR filter on repo_a/repo_b)
CREATE INDEX IF NOT EXISTS idx_relationships_repo_a ON relationships(repo_a);
CREATE INDEX IF NOT EXISTS idx_relationships_repo_b ON relationships(repo_b);

-- Runtime FK lookups
CREATE INDEX IF NOT EXISTS idx_runtime_sessions_schedule_id ON runtime_sessions(schedule_id);
CREATE INDEX IF NOT EXISTS idx_runtime_events_session_id ON runtime_events(session_id);
CREATE INDEX IF NOT EXISTS idx_runtime_tasks_session_id ON runtime_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_runtime_results_session_id ON runtime_results(session_id);
CREATE INDEX IF NOT EXISTS idx_runtime_results_execution_id ON runtime_results(execution_id);
CREATE INDEX IF NOT EXISTS idx_runtime_history_session_id ON runtime_history(session_id);
CREATE INDEX IF NOT EXISTS idx_runtime_evolution_session_id ON runtime_evolution(session_id);
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
    if "worker_kind" not in worker_cols:
        conn.execute(
            "ALTER TABLE workers ADD COLUMN worker_kind TEXT NOT NULL DEFAULT 'function'")
    # Pillar B Stage 4: exemplars column on mined_patterns for skill formation.
    if "mined_patterns" in _existing_tables(conn):
        mp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mined_patterns)")}
        if "exemplars" not in mp_cols:
            conn.execute(
                "ALTER TABLE mined_patterns ADD COLUMN exemplars TEXT NOT NULL DEFAULT '{}'")
    # Phase 3: symbolic task intent (planner emits engineering op, resolver
    # enriches with repo info). Additive JSON column on tasks.
    task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "symbolic" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN symbolic TEXT NOT NULL DEFAULT '{}'")
    # Phase 1.5: execution-contract result (executor's own contract check,
    # distinct from process exit code). Additive; backfilled as NULL.
    rr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runtime_results)")}
    if "verification_passed" not in rr_cols:
        conn.execute(
            "ALTER TABLE runtime_results ADD COLUMN verification_passed INTEGER")
    # Phase 4: structured verification evidence (test summary, git diff, symbol
    # counts) proving the verdict. Additive JSON column.
    if "verification_evidence" not in rr_cols:
        conn.execute(
            "ALTER TABLE runtime_results ADD COLUMN verification_evidence "
            "TEXT NOT NULL DEFAULT '{}'")
    if "payload" not in rr_cols:
        conn.execute(
            "ALTER TABLE runtime_results ADD COLUMN payload TEXT")
    # Suggestion -> Graph Bridge: add source column to task_graphs.
    tg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_graphs)")}
    if "source" not in tg_cols:
        conn.execute(
            "ALTER TABLE task_graphs ADD COLUMN source TEXT")

    # Phase 5: repair_proposals and repair_history tables (Law 16) + indexes.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repair_proposals (
            id                  TEXT PRIMARY KEY,
            original_graph_id   TEXT NOT NULL,
            original_task_id    TEXT NOT NULL,
            failure_reason      TEXT NOT NULL,
            capability          TEXT NOT NULL DEFAULT '',
            repair_depth        INTEGER NOT NULL DEFAULT 0,
            decision            TEXT NOT NULL,
            evidence_ids        TEXT NOT NULL DEFAULT '[]',
            proposed_goal       TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            created_at          TEXT NOT NULL,
            reviewed_at         TEXT,
            schema_version      TEXT NOT NULL DEFAULT '1'
        );
        CREATE TABLE IF NOT EXISTS repair_history (
            proposal_id         TEXT NOT NULL REFERENCES repair_proposals(id) ON DELETE CASCADE,
            event_type          TEXT NOT NULL,
            detail              TEXT NOT NULL DEFAULT '',
            recorded_at         TEXT NOT NULL,
            PRIMARY KEY (proposal_id, recorded_at)
        );
        CREATE INDEX IF NOT EXISTS idx_repair_proposals_status ON repair_proposals(status);
        CREATE INDEX IF NOT EXISTS idx_repair_history_proposal_id ON repair_history(proposal_id);
    """)

    # Operator profile_history table (append-only audit log for preference changes).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profile_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'explicit',
            changed_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_profile_history_key ON profile_history(key);
    """)

    # Phase 4: watch_history table (created fresh each time).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watch_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            outcome     TEXT NOT NULL DEFAULT 'running',
            repos_scanned INTEGER NOT NULL DEFAULT 0,
            repos_changed INTEGER NOT NULL DEFAULT 0,
            knowledge_updated INTEGER NOT NULL DEFAULT 0,
            understanding_updated INTEGER NOT NULL DEFAULT 0,
            initiatives_changed INTEGER NOT NULL DEFAULT 0,
            insights_changed INTEGER NOT NULL DEFAULT 0,
            new_pending_initiatives INTEGER NOT NULL DEFAULT 0,
            error_detail TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_initiatives (
            id               TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            statement        TEXT NOT NULL,
            initiative_type  TEXT NOT NULL,
            confidence       TEXT NOT NULL,
            understanding_ids TEXT NOT NULL DEFAULT '',
            knowledge_ids    TEXT NOT NULL DEFAULT '',
            detected_at      TEXT NOT NULL,
            watch_run_id     INTEGER NOT NULL REFERENCES watch_history(id),
            reviewed         INTEGER NOT NULL DEFAULT 0,
            reviewed_at      TEXT,
            dismissed_at     TEXT,
            action_taken     TEXT
        );
    """)
    conn.commit()

    # Phase 7: Self-Improvement (Meta-Engine) tables.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS capability_gaps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            description     TEXT NOT NULL,
            evidence_refs   TEXT NOT NULL DEFAULT '[]',
            frequency       INTEGER NOT NULL DEFAULT 0,
            score           REAL NOT NULL DEFAULT 0.0,
            status          TEXT NOT NULL DEFAULT 'open',
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_capability_gaps_status ON capability_gaps(status);
        CREATE INDEX IF NOT EXISTS idx_capability_gaps_score ON capability_gaps(score);
        CREATE TABLE IF NOT EXISTS self_improvement_runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            gap_id              INTEGER NOT NULL REFERENCES capability_gaps(id),
            plan_id             TEXT NOT NULL DEFAULT '',
            sandbox_path        TEXT NOT NULL DEFAULT '',
            diff_path           TEXT NOT NULL DEFAULT '',
            verification_result TEXT NOT NULL DEFAULT '{}',
            verification_log    TEXT NOT NULL DEFAULT '',
            deployed            INTEGER NOT NULL DEFAULT 0,
            human_approved      INTEGER NOT NULL DEFAULT 0,
            human_reviewed_at   TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_si_runs_gap_id ON self_improvement_runs(gap_id);
    """)
    conn.commit()

    # Pillar B: Action Log table (append-only event log for user + Friday actions).
    # Used by sequence mining (Stage 2) to detect repeated patterns. Each row
    # is one action event with context for enrichment.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            action_type     TEXT NOT NULL,
            target          TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '{}',
            workspace_id    TEXT,
            project         TEXT,
            session_id      TEXT,
            confidence      TEXT NOT NULL DEFAULT 'observed',
            observed_at     TEXT NOT NULL,
            recorded_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_actions_source ON actions(source);
        CREATE INDEX IF NOT EXISTS idx_actions_action_type ON actions(action_type);
        CREATE INDEX IF NOT EXISTS idx_actions_observed_at ON actions(observed_at);
        CREATE INDEX IF NOT EXISTS idx_actions_project ON actions(project);
    """)
    conn.commit()

    # Pillar B Stage 2: Mined patterns table (persisted sequence-mining results).
    # Each row is one discovered pattern: a repeated action subsequence with
    # frequency count and context metadata. Deterministic, LLM-free.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mined_patterns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence_json   TEXT NOT NULL,
            count           INTEGER NOT NULL DEFAULT 0,
            distinct_sessions INTEGER NOT NULL DEFAULT 0,
            first_seen      TEXT NOT NULL DEFAULT '',
            last_seen       TEXT NOT NULL DEFAULT '',
            common_workspace TEXT NOT NULL DEFAULT '',
            common_project  TEXT NOT NULL DEFAULT '',
            confidence      TEXT NOT NULL DEFAULT 'derived',
            mined_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mined_patterns_count ON mined_patterns(count);
    """)
    conn.commit()

    # Pillar B Stage 3: Workflow intents table (LLM-labeled workflow descriptions
    # derived from mined action patterns). Each row is one labeled intent with
    # the LLM's description, steps, and confidence. FK to mined_patterns so
    # intents cascade-delete when patterns are cleared.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_intents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id          INTEGER NOT NULL REFERENCES mined_patterns(id) ON DELETE CASCADE,
            intent_label        TEXT NOT NULL,
            intent_description  TEXT NOT NULL DEFAULT '',
            steps_text          TEXT NOT NULL DEFAULT '[]',
            confidence          TEXT NOT NULL DEFAULT 'low',
            pattern_summary     TEXT NOT NULL DEFAULT '',
            labeled_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_intents_pattern_id ON workflow_intents(pattern_id);
        CREATE INDEX IF NOT EXISTS idx_workflow_intents_confidence ON workflow_intents(confidence);
    """)
    conn.commit()

    # Pillar B Stage 4: Formed skills table (deployable replay workflows derived
    # from labeled intents).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS formed_skills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_intent_id INTEGER NOT NULL REFERENCES workflow_intents(id) ON DELETE CASCADE,
            task_graph      TEXT NOT NULL,
            exemplars       TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0,
            last_invoked_at TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_formed_skills_wfi ON formed_skills(workflow_intent_id);
    """)
    conn.commit()

    # Cross-project correlation: project docs table (PRDs, design docs, architecture).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS project_docs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id     INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            path        TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL,
            doc_type    TEXT NOT NULL DEFAULT 'design',
            ingested_at TEXT NOT NULL,
            checksum    TEXT NOT NULL DEFAULT '',
            UNIQUE(repo_id, path)
        );
        CREATE INDEX IF NOT EXISTS idx_project_docs_repo ON project_docs(repo_id);
        CREATE INDEX IF NOT EXISTS idx_project_docs_type ON project_docs(doc_type);
    """)
    conn.commit()

    # Cross-project correlation: per-pair results (append-only, resumable).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS correlation_results (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_a_id    INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            repo_b_id    INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            structural_score REAL NOT NULL DEFAULT 0.0,
            semantic_score    REAL,
            semantic_reason   TEXT,
            semantic_label    TEXT,
            semantic_confidence TEXT,
            volatility         REAL NOT NULL DEFAULT 0.0,
            run_at         TEXT NOT NULL,
            UNIQUE(repo_a_id, repo_b_id, run_at)
        );
        CREATE INDEX IF NOT EXISTS idx_correlation_scores ON correlation_results(structural_score DESC);
    """)
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
        "evidence TEXT NOT NULL DEFAULT '[]', symbolic TEXT NOT NULL DEFAULT '{}', "
        "status TEXT NOT NULL DEFAULT 'pending', "
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
    source: Optional[str] = None  # provenance: e.g. "suggestion:<id>", "initiative:<id>"


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
    symbolic: str = "{}"
    status: str = "pending"
    confidence: str = "medium"
    sequence: int = 0


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
                     updated_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                 g.created_at, g.updated_at, g.source),
            )
            # Scrub stale tasks + edges before re-inserting, so recompilation
            # doesn't leave orphan rows from a prior generation with different
            # task count.
            conn.execute("DELETE FROM tasks WHERE graph_id=?", (g.id,))
            conn.execute("DELETE FROM task_edges WHERE graph_id=?", (g.id,))
        for t in tasks:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, graph_id, plan_id, milestone_order, title, description,
                     task_type, required_capabilities, complexity, priority,
                     estimated_effort, dependencies, inputs, outputs,
                     acceptance_criteria, verification, rollback, evidence,
                     symbolic, status, confidence, sequence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    evidence=excluded.evidence, symbolic=excluded.symbolic,
                    status=excluded.status,
                    confidence=excluded.confidence, sequence=excluded.sequence
                """,
                (t.id, t.graph_id, t.plan_id, t.milestone_order, t.title,
                 t.description, t.task_type, t.required_capabilities, t.complexity,
                 t.priority, t.estimated_effort, t.dependencies, t.inputs, t.outputs,
                 t.acceptance_criteria, t.verification, t.rollback, t.evidence,
                 t.symbolic, t.status, t.confidence, t.sequence),
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


def update_task_graph_source(conn: sqlite3.Connection, gid: str,
                              source: str) -> None:
    """Set the provenance/source tag on a graph (e.g. 'suggestion:<id>')."""
    conn.execute(
        "UPDATE task_graphs SET source = ? WHERE id = ?", (source, gid))
    conn.commit()


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
    source = r["source"] if "source" in r.keys() else None
    return TaskGraphRow(
        id=r["id"], goal=r["goal"], plan_id=r["plan_id"],
        plan_type=r["plan_type"], task_count=r["task_count"],
        edge_count=r["edge_count"],
        critical_path_length=r["critical_path_length"],
        parallel_groups=r["parallel_groups"], status=r["status"],
        created_at=r["created_at"], updated_at=r["updated_at"],
        source=source,
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
        evidence=r["evidence"], symbolic=r["symbolic"], status=r["status"],
        confidence=r["confidence"], sequence=r["sequence"],
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
    worker_kind: str = "function"


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
             schema_version, created_at, updated_at, availability, manifest_ref,
             worker_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            availability=excluded.availability, manifest_ref=excluded.manifest_ref,
            worker_kind=excluded.worker_kind
        """,
        (
            w.id, w.name, w.kind, w.description, w.capabilities,
            w.supported_languages, w.supported_task_types, w.supported_plan_types,
            w.limitations, w.estimated_speed, w.estimated_cost, w.context_window,
            w.parallelism, int(w.requires_network), int(w.requires_filesystem),
            int(w.requires_git), int(w.requires_python), int(w.requires_shell),
            w.confidence, w.version, w.status, w.schema_version,
            w.created_at, w.updated_at, w.availability, w.manifest_ref,
            w.worker_kind,
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


# ---------------------------------------------------------------------------
# Worker Genesis: proposed_workers table access
# ---------------------------------------------------------------------------


@dataclass
class ProposedWorkerRow:
    """One proposed worker (capability gap proposal awaiting review)."""
    id: str
    detected_from_goal: str
    capability_gap: str
    draft_manifest_json: str
    status: str  # pending | approved | rejected
    created_at: str
    reviewed_at: Optional[str] = None


def insert_proposed_worker(
    conn: sqlite3.Connection, pw: ProposedWorkerRow
) -> None:
    """Insert a proposed worker. Idempotent on id.

    Uses commit_if_top so this function can be safely called from within
    an outer atomic() transaction block without prematurely committing.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO proposed_workers
            (id, detected_from_goal, capability_gap, draft_manifest_json,
             status, created_at, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pw.id, pw.detected_from_goal, pw.capability_gap,
            pw.draft_manifest_json, pw.status, pw.created_at, pw.reviewed_at,
        ),
    )
    commit_if_top(conn)


def get_proposed_worker(
    conn: sqlite3.Connection, pid: str
) -> Optional[ProposedWorkerRow]:
    row = conn.execute(
        "SELECT * FROM proposed_workers WHERE id = ?", (pid,)
    ).fetchone()
    if row is None:
        return None
    return ProposedWorkerRow(
        id=row["id"], detected_from_goal=row["detected_from_goal"],
        capability_gap=row["capability_gap"],
        draft_manifest_json=row["draft_manifest_json"],
        status=row["status"], created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
    )


def get_proposed_workers(
    conn: sqlite3.Connection, status: Optional[str] = None
) -> List[ProposedWorkerRow]:
    """Get proposed workers, optionally filtered by status."""
    if status:
        rows = conn.execute(
            "SELECT * FROM proposed_workers WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM proposed_workers ORDER BY created_at DESC"
        ).fetchall()
    return [
        ProposedWorkerRow(
            id=r["id"], detected_from_goal=r["detected_from_goal"],
            capability_gap=r["capability_gap"],
            draft_manifest_json=r["draft_manifest_json"],
            status=r["status"], created_at=r["created_at"],
            reviewed_at=r["reviewed_at"],
        )
        for r in rows
    ]


def update_proposed_worker_status(
    conn: sqlite3.Connection, pid: str, status: str,
    reviewed_at: Optional[str] = None,
) -> None:
    """Update a proposal's status (pending -> approved | rejected)."""
    if reviewed_at is None:
        reviewed_at = now_iso()
    conn.execute(
        "UPDATE proposed_workers SET status = ?, reviewed_at = ? WHERE id = ?",
        (status, reviewed_at, pid),
    )
    commit_if_top(conn)


def delete_proposed_worker(
    conn: sqlite3.Connection, pid: str
) -> None:
    """Remove a proposed worker by id (for cleanup)."""
    conn.execute("DELETE FROM proposed_workers WHERE id = ?", (pid,))
    commit_if_top(conn)


# ---------------------------------------------------------------------------
# Operator Identity: operator_preferences (Phase 1 — model + explicit CLI only)
# ---------------------------------------------------------------------------


@dataclass
class OperatorPreferenceRow:
    """One operator preference (explicitly set or evidence-derived)."""
    key: str
    value: str
    set_at: str
    source: str  # 'explicit' | 'derived'


def set_operator_preference(
    conn: sqlite3.Connection, key: str, value: str,
    source: str = "explicit",
) -> None:
    """Insert or update one operator preference.

    Uses source='explicit' for `friday profile set` commands; source='derived'
    for evidence-computed fields (never triggered by inference — see operator.py
    for the derive-on-read-only discipline).

    Records the change in profile_history for audit. The old value is captured
    before the upsert so the history shows the actual transition. When the value
    is unchanged, no history row is written (avoids log noise on re-derivation).

    Use commit_if_top for safe composition inside atomic() blocks.
    """
    # Capture the old value before upsert.
    old_row = conn.execute(
        "SELECT value FROM operator_preferences WHERE key = ?",
        (key,),
    ).fetchone()
    old_value = old_row["value"] if old_row else None

    conn.execute(
        """
        INSERT INTO operator_preferences (key, value, set_at, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value, set_at=excluded.set_at, source=excluded.source
        """,
        (key, value, now_iso(), source),
    )

    # Write to profile_history only when the value actually changed.
    if old_value != value:
        conn.execute(
            "INSERT INTO profile_history (key, old_value, new_value, source, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, old_value, value, source, now_iso()),
        )

    commit_if_top(conn)


def get_operator_preference(
    conn: sqlite3.Connection, key: str
) -> Optional[OperatorPreferenceRow]:
    """Get one preference by key, or None."""
    row = conn.execute(
        "SELECT * FROM operator_preferences WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return OperatorPreferenceRow(
        key=row["key"], value=row["value"],
        set_at=row["set_at"], source=row["source"],
    )


def get_all_operator_preferences(
    conn: sqlite3.Connection, source: Optional[str] = None
) -> list[OperatorPreferenceRow]:
    """Get all operator preferences, optionally filtered by source."""
    if source:
        rows = conn.execute(
            "SELECT * FROM operator_preferences WHERE source = ? ORDER BY key",
            (source,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM operator_preferences ORDER BY key"
        ).fetchall()
    return [
        OperatorPreferenceRow(
            key=r["key"], value=r["value"],
            set_at=r["set_at"], source=r["source"],
        )
        for r in rows
    ]


def unset_operator_preference(conn: sqlite3.Connection, key: str) -> bool:
    """Delete one preference. Returns True if a row was actually removed."""
    cur = conn.execute(
        "DELETE FROM operator_preferences WHERE key = ?", (key,)
    )
    commit_if_top(conn)
    return cur.rowcount > 0


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
             stderr, artifacts, exit_code, duration_ms, error, payload,
             verification_passed, verification_evidence, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["execution_id"], row["session_id"], row["task_id"],
         row.get("worker_id"),
         1 if row.get("success") else 0,
         row.get("stdout", ""), row.get("stderr", ""), row.get("artifacts", "[]"),
         row.get("exit_code"), row.get("duration_ms", 0), row.get("error", ""),
         row.get("payload"),
         row.get("verification_passed"), row.get("verification_evidence", "{}"),
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


# ===========================================================================
# Meta-Engine storage (Phase 7) — capability gaps + self-improvement runs.
# ===========================================================================


def get_capability_gaps(conn, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM capability_gaps WHERE status = ? ORDER BY score DESC",
            (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM capability_gaps ORDER BY score DESC").fetchall()
    return [dict(r) for r in rows]


def get_capability_gap(conn, gap_id):
    row = conn.execute(
        "SELECT * FROM capability_gaps WHERE id = ?", (gap_id,)).fetchone()
    return dict(row) if row else None


def insert_capability_gap(conn, row):
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO capability_gaps
           (description, evidence_refs, frequency, score, status,
            attempt_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["description"], row.get("evidence_refs", "[]"),
         row.get("frequency", 0), row.get("score", 0.0),
         row.get("status", "open"), row.get("attempt_count", 0),
         row.get("created_at", datetime.now(timezone.utc).isoformat()),
         row.get("updated_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def update_capability_gap(conn, gap_id, **kw):
    sets = ", ".join(f"{k} = ?" for k in kw)
    vals = list(kw.values()) + [gap_id]
    conn.execute(f"UPDATE capability_gaps SET {sets} WHERE id = ?", vals)
    conn.commit()


def get_si_runs(conn, gap_id=None):
    if gap_id is not None:
        rows = conn.execute(
            "SELECT * FROM self_improvement_runs WHERE gap_id = ? ORDER BY created_at DESC",
            (gap_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM self_improvement_runs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_si_run(conn, run_id):
    row = conn.execute(
        "SELECT * FROM self_improvement_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def insert_si_run(conn, row):
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO self_improvement_runs
           (gap_id, plan_id, sandbox_path, diff_path, verification_result,
            verification_log, deployed, human_approved, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["gap_id"], row.get("plan_id", ""), row.get("sandbox_path", ""),
         row.get("diff_path", ""), row.get("verification_result", "{}"),
         row.get("verification_log", ""),
         1 if row.get("deployed") else 0,
         1 if row.get("human_approved") else 0,
         row.get("created_at", datetime.now(timezone.utc).isoformat()),
         row.get("updated_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def update_si_run(conn, run_id, **kw):
    sets = ", ".join(f"{k} = ?" for k in kw)
    vals = list(kw.values()) + [run_id]
    conn.execute(f"UPDATE self_improvement_runs SET {sets} WHERE id = ?", vals)
    conn.commit()


# ===========================================================================
# Pillar B Stage 2 — Sequence Mining (mined_patterns table helpers)
# ===========================================================================


def insert_mined_pattern(conn, row):
    """Persist one mined pattern row. Returns the new row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO mined_patterns
           (sequence_json, count, distinct_sessions, first_seen, last_seen,
            common_workspace, common_project, confidence, mined_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["sequence_json"], row.get("count", 0),
         row.get("distinct_sessions", 0),
         row.get("first_seen", ""), row.get("last_seen", ""),
         row.get("common_workspace", ""), row.get("common_project", ""),
         row.get("confidence", "derived"),
         row.get("mined_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def get_mined_patterns(conn, min_count=0, limit=50):
    """Return mined patterns, most frequent first, optionally filtered by min_count."""
    if min_count > 0:
        rows = conn.execute(
            "SELECT * FROM mined_patterns WHERE count >= ? "
            "ORDER BY count DESC LIMIT ?", (min_count, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM mined_patterns ORDER BY count DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def clear_mined_patterns(conn):
    """Delete all mined patterns (for re-mining)."""
    conn.execute("DELETE FROM mined_patterns")
    conn.commit()


# ===========================================================================
# Pillar B Stage 3 — Workflow Intents (LLM-labeled workflow descriptions)
# ===========================================================================


def insert_workflow_intent(conn, row):
    """Persist one workflow intent. Returns the new row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO workflow_intents
           (pattern_id, intent_label, intent_description, steps_text,
            confidence, pattern_summary, labeled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["pattern_id"], row["intent_label"],
         row.get("intent_description", ""),
         row.get("steps_text", "[]"),
         row.get("confidence", "low"),
         row.get("pattern_summary", ""),
         row.get("labeled_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def get_workflow_intents(conn, min_confidence=None, limit=50):
    """Return workflow intents, newest first, optionally filtered by min confidence."""
    if min_confidence:
        rows = conn.execute(
            "SELECT * FROM workflow_intents WHERE confidence >= ? "
            "ORDER BY labeled_at DESC LIMIT ?",
            (min_confidence, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workflow_intents ORDER BY labeled_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_workflow_intents_for_pattern(conn, pattern_id):
    """Return all workflow intents for a specific pattern."""
    rows = conn.execute(
        "SELECT * FROM workflow_intents WHERE pattern_id = ? "
        "ORDER BY labeled_at DESC", (pattern_id,)).fetchall()
    return [dict(r) for r in rows]


def clear_workflow_intents(conn):
    """Delete all workflow intents (re-label on re-mine)."""
    conn.execute("DELETE FROM workflow_intents")
    conn.commit()


# ===========================================================================
# Pillar B Stage 4 — Formed Skills (replayable workflow skills)
# ===========================================================================


def insert_formed_skill(conn, row: dict) -> int:
    """Persist one formed skill. Returns the new row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO formed_skills
           (workflow_intent_id, task_graph, exemplars,
            invocation_count, last_invoked_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["workflow_intent_id"], row["task_graph"], row.get("exemplars", "{}"),
         row.get("invocation_count", 0), row.get("last_invoked_at"),
         row.get("created_at", datetime.now(timezone.utc).isoformat()),
         row.get("updated_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def get_formed_skill(conn, skill_id: int) -> Optional[dict]:
    """Return a formed skill by id."""
    row = conn.execute(
        "SELECT * FROM formed_skills WHERE id = ?", (skill_id,)
    ).fetchone()
    return dict(row) if row else None


def get_formed_skill_by_intent(conn, workflow_intent_id: int) -> Optional[dict]:
    """Return a formed skill for a given workflow intent, or None."""
    row = conn.execute(
        "SELECT * FROM formed_skills WHERE workflow_intent_id = ?",
        (workflow_intent_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_formed_skills(conn) -> list[dict]:
    """Return all formed skills, newest first."""
    rows = conn.execute(
        "SELECT * FROM formed_skills ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def increment_formed_skill_invocation(conn, skill_id: int) -> None:
    """Increment invocation count and update last_invoked_at."""
    from datetime import datetime, timezone
    conn.execute(
        "UPDATE formed_skills SET invocation_count = invocation_count + 1, "
        "last_invoked_at = ?, updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat(), skill_id)
    )
    conn.commit()


def delete_formed_skill(conn, skill_id: int) -> None:
    """Delete a formed skill by id."""
    conn.execute("DELETE FROM formed_skills WHERE id = ?", (skill_id,))
    conn.commit()


# ===========================================================================
# Cross-Project Correlation — project docs + per-pair correlation results
# ===========================================================================


def upsert_project_doc(conn, row: dict) -> int:
    """Insert or update a project doc by (repo_id, path). Returns row id."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO project_docs (repo_id, path, title, content, doc_type, ingested_at, checksum)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo_id, path) DO UPDATE SET
               title=excluded.title, content=excluded.content,
               doc_type=excluded.doc_type, ingested_at=excluded.ingested_at,
               checksum=excluded.checksum""",
        (row["repo_id"], row["path"], row.get("title", ""), row["content"],
         row.get("doc_type", "design"), now, row.get("checksum", "")))
    conn.commit()
    return cur.lastrowid


def get_project_docs(conn, repo_id: int) -> list[dict]:
    """Return all project docs for a repo."""
    rows = conn.execute(
        "SELECT * FROM project_docs WHERE repo_id = ? ORDER BY ingested_at DESC",
        (repo_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_project_docs(conn) -> list[dict]:
    """Return all project docs across all repos."""
    rows = conn.execute(
        "SELECT * FROM project_docs ORDER BY ingested_at DESC").fetchall()
    return [dict(r) for r in rows]


def insert_correlation_result(conn, row: dict) -> int:
    """Persist one correlation result. Returns row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO correlation_results
           (repo_a_id, repo_b_id, structural_score, semantic_score,
            semantic_reason, semantic_label, semantic_confidence,
            volatility, run_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["repo_a_id"], row["repo_b_id"],
         row.get("structural_score", 0.0),
         row.get("semantic_score"),
         row.get("semantic_reason"),
         row.get("semantic_label"),
         row.get("semantic_confidence"),
         row.get("volatility", 0.0),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return cur.lastrowid


def get_recent_correlations(conn, min_structural: float = 0.0, limit: int = 20) -> list[dict]:
    """Return most recent correlation results, optionally filtered by structural score."""
    if min_structural > 0:
        rows = conn.execute(
            "SELECT * FROM correlation_results WHERE structural_score >= ? "
            "ORDER BY run_at DESC LIMIT ?", (min_structural, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM correlation_results ORDER BY run_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_repo_commit_count_recent(conn, repo_id: int, days: int = 30) -> int:
    """Count commits in the last N days for a repo using snapshot history."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Use git snapshots to estimate activity — count distinct snapshot dates.
    row = conn.execute(
        "SELECT COUNT(DISTINCT date(observed_at)) as c FROM snapshots "
        "WHERE repo_path = (SELECT path FROM repositories WHERE id = ?) "
        "AND observed_at >= ?", (repo_id, cutoff)).fetchone()
    return row["c"] if row else 0
