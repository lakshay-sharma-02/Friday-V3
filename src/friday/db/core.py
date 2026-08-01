"""SQLite storage for Friday's knowledge base.

Schema is deliberately flat: relationships and cross-project observations are
re-derived at summary time from stored rows, so we never persist derived pairs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union


def db_path() -> Path:
    override = os.environ.get("FRIDAY_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".friday" / "friday.db"


# Legacy — kept for reference; migrations/*.sql is the source of truth.
# Remove once no external code imports this constant.
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
);-- M8.3: Understanding Engine. Write-only layer on top of Knowledge. NEVER
-- reads observations/context directly. Every understanding cites knowledge ids.
-- Append-only history, evolution tables removed (dead code). Uses layer_history instead.
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

-- Phase A: Conversation Log. Append-only log of every IdentityEngine exchange.
-- Used by the daemon's downstream LLM extraction (Phase B) to learn operator
-- identity and preferences without brittle regex patterns.
CREATE TABLE IF NOT EXISTS conversation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    routing         TEXT NOT NULL DEFAULT '',
    user_message    TEXT NOT NULL,
    friday_reply    TEXT NOT NULL,
    conversation_at TEXT NOT NULL,
    processed       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversation_log_channel ON conversation_log(channel);
CREATE INDEX IF NOT EXISTS idx_conversation_log_processed ON conversation_log(processed);
CREATE INDEX IF NOT EXISTS idx_conversation_log_conversation_at ON conversation_log(conversation_at DESC);

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

-- Generic append-only layer history for all entity types.
-- Replaces the 6 dead evolution tables (evolution_events, understanding_evolution,
-- initiative_evolution, insight_evolution, plan_evolution, task_evolution).
-- Every row records one state transition for any entity type.
CREATE TABLE IF NOT EXISTS layer_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type         TEXT NOT NULL,
    entity_id           TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    previous_state      TEXT,
    new_state           TEXT,
    reason              TEXT NOT NULL DEFAULT '',
    metadata            TEXT NOT NULL DEFAULT '{}',
    recorded_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_layer_history_entity ON layer_history(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_layer_history_recorded ON layer_history(recorded_at);

-- ===========================================================================
-- Indexes for hot-path WHERE clauses (FK columns, status filters, etc.)
-- ===========================================================================
-- These are purely additive. No schema changes, no column additions.
-- CREATE INDEX IF NOT EXISTS guarantees idempotency on reconnect.

-- Graph/task FK lookups (tasks.graph_id, task_edges.graph_id, etc.)
CREATE INDEX IF NOT EXISTS idx_tasks_graph_id ON tasks(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_edges_graph_id ON task_edges(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_history_graph_id ON task_history(graph_id);
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

-- Understanding evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_understanding_history_understanding_id ON understanding_history(understanding_id);

-- Initiative evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_initiative_history_initiative_id ON initiative_history(initiative_id);

-- Insight evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_insight_history_insight_id ON insight_history(insight_id);

-- Plan evolution FK lookups
CREATE INDEX IF NOT EXISTS idx_plan_history_plan_id ON plan_history(plan_id);

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
    # Schema is now applied through versioned migration files.
    # The initial SCHEMA string is applied as sql_initial via
    # _run_sql_migrations(), which reads files from migrations/
    # and tracks them in _schema_versions. This replaces the old
    # conn.executescript(SCHEMA) call, reducing per-connect overhead.
    _run_sql_migrations(conn)
    _run_pending_migrations(conn)
    return conn



# ── Versioned Schema Migrations ────────────────────────────────────
# Each step is tracked in _schema_versions so it runs exactly once.
# NEVER reorder, remove, or modify existing steps — only APPEND.

_MIGRATIONS: list[tuple[str, str, Callable[..., None]]] = []


def _register(version: str, description: str):
    """Decorator to register a migration step."""
    def deco(fn):
        _MIGRATIONS.append((version, description, fn))
        return fn
    return deco


@_register("001", "M2/M4 repository columns + evidence-strength model")
def _mig_001(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(repositories)")}
    for col, ctype in (("maturity", "TEXT"), ("readme_quality", "TEXT"), ("readme_completeness", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE repositories ADD COLUMN {col} {ctype}")
    for table, col in (("relationships", "strength"), ("components", "strength"), ("architecture", "confidence")):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT 'Medium'")


@_register("002", "M8.1.5 static knowledge marker")
def _mig_002(conn):
    know_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge)")}
    if "is_static" not in know_cols:
        conn.execute("ALTER TABLE knowledge ADD COLUMN is_static INTEGER NOT NULL DEFAULT 0")


@_register("003", "M9.2.5 observations PRIMARY KEY")
def _mig_003(conn):
    _ensure_observations_pk(conn)


@_register("004", "M9.2.5 FK-bearing tables")
def _mig_004(conn):
    _ensure_fk_tables(conn)


@_register("005", "M9.2.5 schema_version columns")
def _mig_005(conn):
    know_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge)")}
    if "schema_version" not in know_cols:
        conn.execute("ALTER TABLE knowledge ADD COLUMN schema_version TEXT NOT NULL DEFAULT '1.0'")
    for table in ("understanding", "insights", "initiatives", "workers", "plans"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "schema_version" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN schema_version TEXT NOT NULL DEFAULT '1.0'")


@_register("006", "M9.3 resolver_history surrogate PK")
def _mig_006(conn):
    _ensure_resolver_history_pk(conn)


@_register("007", "M9.8 snapshots signature columns")
def _mig_007(conn):
    _ensure_snapshots_signature_cols(conn)


@_register("008", "M10 worker availability + manifest_ref + worker_kind")
def _mig_008(conn):
    worker_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)")}
    if "availability" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN availability TEXT NOT NULL DEFAULT 'available'")
    if "manifest_ref" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN manifest_ref TEXT")
    if "worker_kind" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN worker_kind TEXT NOT NULL DEFAULT 'function'")


@_register("009", "Pillar B Stage 4 exemplars on mined_patterns")
def _mig_009(conn):
    if "mined_patterns" in _existing_tables(conn):
        mp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mined_patterns)")}
        if "exemplars" not in mp_cols:
            conn.execute("ALTER TABLE mined_patterns ADD COLUMN exemplars TEXT NOT NULL DEFAULT '{}'")


@_register("010", "Phase 3 symbolic task intent")
def _mig_010(conn):
    task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "symbolic" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN symbolic TEXT NOT NULL DEFAULT '{}'")


@_register("011", "Phase 1.5+4 runtime_results verification columns")
def _mig_011(conn):
    rr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runtime_results)")}
    if "verification_passed" not in rr_cols:
        conn.execute("ALTER TABLE runtime_results ADD COLUMN verification_passed INTEGER")
    if "verification_evidence" not in rr_cols:
        conn.execute("ALTER TABLE runtime_results ADD COLUMN verification_evidence TEXT NOT NULL DEFAULT '{}'")
    if "payload" not in rr_cols:
        conn.execute("ALTER TABLE runtime_results ADD COLUMN payload TEXT")


@_register("012", "Suggestion -> Graph Bridge source column")
def _mig_012(conn):
    tg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_graphs)")}
    if "source" not in tg_cols:
        conn.execute("ALTER TABLE task_graphs ADD COLUMN source TEXT")


@_register("013", "Repair + profile_history + watch tables")
def _mig_013(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repair_proposals (
            id TEXT PRIMARY KEY, original_graph_id TEXT NOT NULL, original_task_id TEXT NOT NULL,
            failure_reason TEXT NOT NULL, capability TEXT NOT NULL DEFAULT '', repair_depth INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL, evidence_ids TEXT NOT NULL DEFAULT '[]', proposed_goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, reviewed_at TEXT,
            schema_version TEXT NOT NULL DEFAULT '1'
        );
        CREATE TABLE IF NOT EXISTS repair_history (
            proposal_id TEXT NOT NULL REFERENCES repair_proposals(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL,
            PRIMARY KEY (proposal_id, recorded_at)
        );
        CREATE INDEX IF NOT EXISTS idx_repair_proposals_status ON repair_proposals(status);
        CREATE INDEX IF NOT EXISTS idx_repair_history_proposal_id ON repair_history(proposal_id);
        CREATE TABLE IF NOT EXISTS profile_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
            old_value TEXT, new_value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'explicit',
            changed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_profile_history_key ON profile_history(key);
    """)
    conn.commit()


@_register("014", "Phase 4 watch + pending_initiatives tables")
def _mig_014(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
            outcome TEXT NOT NULL DEFAULT 'running', repos_scanned INTEGER NOT NULL DEFAULT 0,
            repos_changed INTEGER NOT NULL DEFAULT 0, knowledge_updated INTEGER NOT NULL DEFAULT 0,
            understanding_updated INTEGER NOT NULL DEFAULT 0, initiatives_changed INTEGER NOT NULL DEFAULT 0,
            insights_changed INTEGER NOT NULL DEFAULT 0, new_pending_initiatives INTEGER NOT NULL DEFAULT 0,
            error_detail TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_initiatives (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, statement TEXT NOT NULL,
            initiative_type TEXT NOT NULL, confidence TEXT NOT NULL,
            understanding_ids TEXT NOT NULL DEFAULT '', knowledge_ids TEXT NOT NULL DEFAULT '',
            detected_at TEXT NOT NULL, watch_run_id INTEGER NOT NULL REFERENCES watch_history(id),
            reviewed INTEGER NOT NULL DEFAULT 0, reviewed_at TEXT, dismissed_at TEXT, action_taken TEXT
        );
    """)
    conn.commit()


@_register("015", "Phase 7 meta-engine tables")
def _mig_015(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS capability_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL,
            evidence_refs TEXT NOT NULL DEFAULT '[]', frequency INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0.0, status TEXT NOT NULL DEFAULT 'open',
            attempt_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_capability_gaps_status ON capability_gaps(status);
        CREATE INDEX IF NOT EXISTS idx_capability_gaps_score ON capability_gaps(score);
        CREATE TABLE IF NOT EXISTS self_improvement_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gap_id INTEGER NOT NULL REFERENCES capability_gaps(id),
            plan_id TEXT NOT NULL DEFAULT '', sandbox_path TEXT NOT NULL DEFAULT '',
            diff_path TEXT NOT NULL DEFAULT '', verification_result TEXT NOT NULL DEFAULT '{}',
            verification_log TEXT NOT NULL DEFAULT '', deployed INTEGER NOT NULL DEFAULT 0,
            human_approved INTEGER NOT NULL DEFAULT 0, human_reviewed_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_si_runs_gap_id ON self_improvement_runs(gap_id);
    """)
    conn.commit()


@_register("016", "Pillar B action log + pattern pipeline")
def _mig_016(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            action_type TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
            workspace_id TEXT, project TEXT, session_id TEXT,
            confidence TEXT NOT NULL DEFAULT 'observed', observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_actions_source ON actions(source);
        CREATE INDEX IF NOT EXISTS idx_actions_action_type ON actions(action_type);
        CREATE INDEX IF NOT EXISTS idx_actions_observed_at ON actions(observed_at);
        CREATE INDEX IF NOT EXISTS idx_actions_project ON actions(project);
        CREATE TABLE IF NOT EXISTS mined_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sequence_json TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0, distinct_sessions INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL DEFAULT '', last_seen TEXT NOT NULL DEFAULT '',
            common_workspace TEXT NOT NULL DEFAULT '', common_project TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'derived', mined_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mined_patterns_count ON mined_patterns(count);
        CREATE TABLE IF NOT EXISTS workflow_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id INTEGER NOT NULL REFERENCES mined_patterns(id) ON DELETE CASCADE,
            intent_label TEXT NOT NULL, intent_description TEXT NOT NULL DEFAULT '',
            steps_text TEXT NOT NULL DEFAULT '[]', confidence TEXT NOT NULL DEFAULT 'low',
            pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_intents_pattern_id ON workflow_intents(pattern_id);
        CREATE INDEX IF NOT EXISTS idx_workflow_intents_confidence ON workflow_intents(confidence);
        CREATE TABLE IF NOT EXISTS formed_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_intent_id INTEGER NOT NULL REFERENCES workflow_intents(id) ON DELETE CASCADE,
            task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_formed_skills_wfi ON formed_skills(workflow_intent_id);
    """)
    conn.commit()


@_register("017", "Cross-project docs + correlation results")
def _mig_017(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS project_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            path TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
            doc_type TEXT NOT NULL DEFAULT 'design', ingested_at TEXT NOT NULL,
            checksum TEXT NOT NULL DEFAULT '', UNIQUE(repo_id, path)
        );
        CREATE INDEX IF NOT EXISTS idx_project_docs_repo ON project_docs(repo_id);
        CREATE INDEX IF NOT EXISTS idx_project_docs_type ON project_docs(doc_type);
        CREATE TABLE IF NOT EXISTS correlation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_a_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            repo_b_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            structural_score REAL NOT NULL DEFAULT 0.0, semantic_score REAL,
            semantic_reason TEXT, semantic_label TEXT, semantic_confidence TEXT,
            volatility REAL NOT NULL DEFAULT 0.0, run_at TEXT NOT NULL,
            UNIQUE(repo_a_id, repo_b_id, run_at)
        );
        CREATE INDEX IF NOT EXISTS idx_correlation_scores ON correlation_results(structural_score DESC);
    """)
    conn.commit()


@_register("018", "Autonomy permissions table")
def _mig_018(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS autonomy_permissions (
            action_type TEXT NOT NULL PRIMARY KEY, default_level TEXT NOT NULL,
            override_level TEXT, auto_downgraded TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            consecutive_successes INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()


@_register("019", "Phase 1 ambient event feed")
def _mig_019(conn):
    if "ambient_feed" in _existing_tables(conn):
        af_cols = {r["name"] for r in conn.execute("PRAGMA table_info(ambient_feed)")}
        if "project" not in af_cols:
            conn.execute("ALTER TABLE ambient_feed ADD COLUMN project TEXT NOT NULL DEFAULT ''")
        if "payload" not in af_cols:
            conn.execute("ALTER TABLE ambient_feed ADD COLUMN payload TEXT NOT NULL DEFAULT ''")
        if "confidence" not in af_cols:
            conn.execute("ALTER TABLE ambient_feed ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
        if "salience" not in af_cols:
            conn.execute("ALTER TABLE ambient_feed ADD COLUMN salience REAL NOT NULL DEFAULT 0.0")
        conn.commit()
    from ..ambient import AMBIENT_FEED_SCHEMA
    conn.executescript(AMBIENT_FEED_SCHEMA)
    conn.commit()


@_register("020", "Memory layer + working memory tables")
def _mig_020(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general', source TEXT NOT NULL DEFAULT 'conversation',
            channel TEXT, channel_id TEXT, context TEXT,
            confidence REAL NOT NULL DEFAULT 1.0, recency_score REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_memory_key ON knowledge_memory(key);
        CREATE INDEX IF NOT EXISTS idx_memory_category ON knowledge_memory(category);
        CREATE INDEX IF NOT EXISTS idx_memory_active ON knowledge_memory(is_active);
    """)
    conn.commit()
    if "knowledge_memory" in _existing_tables(conn):
        try:
            mem_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge_memory)")}
            if "recency_score" not in mem_cols:
                conn.execute("ALTER TABLE knowledge_memory ADD COLUMN recency_score REAL NOT NULL DEFAULT 1.0")
                conn.commit()
        except Exception:
            pass
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS working_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, context_key TEXT NOT NULL, value TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'working', source TEXT NOT NULL DEFAULT 'system',
            context TEXT, priority INTEGER NOT NULL DEFAULT 0,
            ttl_seconds INTEGER NOT NULL DEFAULT 3600, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_working_memory_key ON working_memory(context_key);
        CREATE INDEX IF NOT EXISTS idx_working_memory_expires ON working_memory(expires_at);
    """)
    conn.commit()


@_register("021", "Shadow mode runs table")
def _mig_021(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id INTEGER NOT NULL REFERENCES formed_skills(id) ON DELETE CASCADE,
            run_at TEXT NOT NULL, step_count INTEGER NOT NULL DEFAULT 0,
            steps_matched INTEGER NOT NULL DEFAULT 0, steps_mismatched INTEGER NOT NULL DEFAULT 0,
            exemplar_comparison TEXT NOT NULL DEFAULT '{}', overall_match_score REAL NOT NULL DEFAULT 0.0,
            outcome TEXT NOT NULL DEFAULT 'matched', promoted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_runs_skill_id ON shadow_runs(skill_id);
        CREATE INDEX IF NOT EXISTS idx_shadow_runs_outcome ON shadow_runs(outcome);
    """)
    conn.commit()


@_register("022", "Drop dead evolution tables")
def _mig_022(conn):
    for _dead_table in (
        "evolution_events", "understanding_evolution", "initiative_evolution",
        "insight_evolution", "plan_evolution", "task_evolution",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {_dead_table}")
    conn.commit()


@_register("024", "Presence & Attention: deferred_interrupts table")
def _mig_024(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS deferred_interrupts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            state_at_creation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deferred_interrupts_delivered
            ON deferred_interrupts(delivered_at);
        CREATE INDEX IF NOT EXISTS idx_deferred_interrupts_priority
            ON deferred_interrupts(priority DESC);
    """)
    conn.commit()


@_register("026", "Sandbox & Safety: simulation_log, rollback_snapshots")
def _mig_026(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS simulation_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            action          TEXT NOT NULL,
            action_type     TEXT NOT NULL DEFAULT '',
            sandbox_type    TEXT NOT NULL DEFAULT 'tempdir',
            success         INTEGER NOT NULL DEFAULT 0,
            outcome_summary TEXT NOT NULL DEFAULT '',
            duration_ms     INTEGER NOT NULL DEFAULT 0,
            files_changed   INTEGER NOT NULL DEFAULT 0,
            has_diff        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_simulation_log_created
            ON simulation_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_simulation_log_action_type
            ON simulation_log(action_type);
        CREATE TABLE IF NOT EXISTS rollback_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id       TEXT NOT NULL DEFAULT '',
            action_type     TEXT NOT NULL DEFAULT '',
            action_desc     TEXT NOT NULL DEFAULT '',
            snapshot_path   TEXT NOT NULL DEFAULT '',
            snapshot_type   TEXT NOT NULL DEFAULT 'file',
            head_sha        TEXT,
            file_count      INTEGER NOT NULL DEFAULT 0,
            reversible      INTEGER NOT NULL DEFAULT 1,
            restored_at     TEXT,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rollback_snapshots_created
            ON rollback_snapshots(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_rollback_snapshots_action_id
            ON rollback_snapshots(action_id);
    """)
    conn.commit()


@_register("025", "System Intelligence: telemetry, process baseline, build history")
def _mig_025(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS telemetry_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            cpu_percent     REAL NOT NULL DEFAULT 0,
            cpu_count       INTEGER NOT NULL DEFAULT 0,
            memory_percent  REAL NOT NULL DEFAULT 0,
            memory_total    INTEGER NOT NULL DEFAULT 0,
            disk_percent    REAL NOT NULL DEFAULT 0,
            disk_total      INTEGER NOT NULL DEFAULT 0,
            swap_percent    REAL NOT NULL DEFAULT 0,
            processes       INTEGER NOT NULL DEFAULT 0,
            load_1m         REAL NOT NULL DEFAULT 0,
            health          TEXT NOT NULL DEFAULT 'green',
            per_cpu_json    TEXT NOT NULL DEFAULT '[]',
            per_disk_json   TEXT NOT NULL DEFAULT '{}',
            per_net_json    TEXT NOT NULL DEFAULT '{}',
            top_cpu_json    TEXT NOT NULL DEFAULT '[]',
            top_mem_json    TEXT NOT NULL DEFAULT '[]',
            gpu_json        TEXT,
            system_uptime   REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
            ON telemetry_snapshots(timestamp DESC);
        CREATE TABLE IF NOT EXISTS process_baseline (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            cmdline         TEXT NOT NULL DEFAULT '',
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            seen_count      INTEGER NOT NULL DEFAULT 1,
            known           INTEGER NOT NULL DEFAULT 0,
            user_label      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_process_baseline_name
            ON process_baseline(name);
        CREATE INDEX IF NOT EXISTS idx_process_baseline_known
            ON process_baseline(known);
        CREATE TABLE IF NOT EXISTS build_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            project         TEXT NOT NULL DEFAULT '',
            command         TEXT NOT NULL DEFAULT '',
            success         INTEGER NOT NULL DEFAULT 0,
            exit_code       INTEGER,
            duration_ms     INTEGER NOT NULL DEFAULT 0,
            error_count     INTEGER NOT NULL DEFAULT 0,
            warning_count   INTEGER NOT NULL DEFAULT 0,
            slow_test_count INTEGER NOT NULL DEFAULT 0,
            output_text     TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_build_history_timestamp
            ON build_history(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_build_history_project
            ON build_history(project);
        CREATE INDEX IF NOT EXISTS idx_build_history_success
            ON build_history(success);
    """)
    conn.commit()


@_register("023", "Autonomous actions table")
def _mig_023(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS autonomous_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
            source_summary TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', worker_id TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '', motivation TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', auto_level TEXT NOT NULL DEFAULT 'auto',
            session_id TEXT, result_json TEXT, executed_at TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_auto_actions_status ON autonomous_actions(status);
        CREATE INDEX IF NOT EXISTS idx_auto_actions_source ON autonomous_actions(source);
        CREATE INDEX IF NOT EXISTS idx_auto_actions_created ON autonomous_actions(created_at);
    """)
    conn.commit()


@_register("027", "Relationship & Personalization: sentiment, tone, relationship metrics")
def _mig_027(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sentiment_observations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            channel         TEXT NOT NULL DEFAULT '',
            message_hash    TEXT NOT NULL,
            tone            TEXT NOT NULL,
            confidence      REAL NOT NULL,
            signal          TEXT NOT NULL DEFAULT '',
            conversation_id TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sentiment_timestamp
            ON sentiment_observations(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_sentiment_conversation
            ON sentiment_observations(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_sentiment_tone
            ON sentiment_observations(tone);

        CREATE TABLE IF NOT EXISTS tone_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL DEFAULT '',
            depth_at_time   INTEGER NOT NULL DEFAULT 0,
            tone_used       TEXT NOT NULL DEFAULT 'neutral',
            user_sentiment_avg REAL NOT NULL DEFAULT 0.0,
            recorded_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tone_history_conversation
            ON tone_history(conversation_id);

        CREATE TABLE IF NOT EXISTS relationship_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_key      TEXT NOT NULL,
            metric_value    TEXT NOT NULL,
            computed_at     TEXT NOT NULL,
            window_days     INTEGER NOT NULL DEFAULT 7
        );
        CREATE INDEX IF NOT EXISTS idx_rel_metrics_key
            ON relationship_metrics(metric_key);
        CREATE INDEX IF NOT EXISTS idx_rel_metrics_computed
            ON relationship_metrics(computed_at DESC);
    """)
    conn.commit()


@_register("032", "Collaboration External: PR review cache table")
def _mig_032(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pr_reviews (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            repo            TEXT NOT NULL,
            pr_number       INTEGER NOT NULL,
            pr_title        TEXT NOT NULL DEFAULT '',
            pr_author       TEXT NOT NULL DEFAULT '',
            base_branch     TEXT NOT NULL DEFAULT '',
            head_branch     TEXT NOT NULL DEFAULT '',
            diff_summary    TEXT NOT NULL DEFAULT '',
            concerns        TEXT NOT NULL DEFAULT '',
            suggestions      TEXT NOT NULL DEFAULT '',
            severity         TEXT NOT NULL DEFAULT 'info',
            auto_posted      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            UNIQUE(repo, pr_number)
        );
        CREATE INDEX IF NOT EXISTS idx_pr_reviews_repo
            ON pr_reviews(repo);
        CREATE INDEX IF NOT EXISTS idx_pr_reviews_created
            ON pr_reviews(created_at DESC);
    """)
    conn.commit()


@_register("033", "Self-Evolution Engine: capability_flags table")
def _mig_033(conn):
    """Create the capability_flags table for self-evolution capability lifecycle."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS capability_flags (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            description     TEXT NOT NULL DEFAULT '',
            enabled         INTEGER NOT NULL DEFAULT 0,
            installed       INTEGER NOT NULL DEFAULT 0,
            deps_installed  INTEGER NOT NULL DEFAULT 0,
            plan_json       TEXT NOT NULL DEFAULT '{}',
            added_at        TEXT NOT NULL,
            enabled_at      TEXT,
            rollback_commit TEXT,
            last_used_at    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_capability_flags_name
            ON capability_flags(name);
        CREATE INDEX IF NOT EXISTS idx_capability_flags_enabled
            ON capability_flags(enabled);
        CREATE INDEX IF NOT EXISTS idx_capability_flags_added
            ON capability_flags(added_at DESC);
    """)
    conn.commit()


@_register("031", "Collaboration External: guide_sessions + translation_cache tables")
def _mig_031(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS guide_sessions (
            id              TEXT PRIMARY KEY,
            protocol_name   TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            current_step    INTEGER NOT NULL DEFAULT 0,
            total_steps     INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'running',
            channel         TEXT NOT NULL DEFAULT 'cli',
            steps_json      TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_guide_sessions_status
            ON guide_sessions(status);
        CREATE INDEX IF NOT EXISTS idx_guide_sessions_created
            ON guide_sessions(created_at DESC);
        CREATE TABLE IF NOT EXISTS translation_cache (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            text_hash       TEXT NOT NULL,
            source_lang     TEXT NOT NULL,
            target_lang     TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            UNIQUE(text_hash, source_lang, target_lang)
        );
        CREATE INDEX IF NOT EXISTS idx_translation_cache_lookup
            ON translation_cache(text_hash, source_lang, target_lang);
    """)
    conn.commit()


@_register("030", "Analysis & Insight: code_dependencies import graph table")
def _mig_030(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS code_dependencies (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id         INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            file_path       TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            dep_type        TEXT NOT NULL,
            resolved_path   TEXT NOT NULL DEFAULT '',
            resolved_repo   TEXT NOT NULL DEFAULT '',
            line_number     INTEGER NOT NULL DEFAULT 0,
            built_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_code_deps_repo
            ON code_dependencies(repo_id);
        CREATE INDEX IF NOT EXISTS idx_code_deps_file
            ON code_dependencies(file_path);
        CREATE INDEX IF NOT EXISTS idx_code_deps_symbol
            ON code_dependencies(symbol);
        CREATE INDEX IF NOT EXISTS idx_code_deps_dep_type
            ON code_dependencies(dep_type);
        CREATE TABLE IF NOT EXISTS code_imports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id         INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            source_file     TEXT NOT NULL,
            imported_module TEXT NOT NULL,
            import_type     TEXT NOT NULL DEFAULT 'direct',
            line_number     INTEGER NOT NULL DEFAULT 0,
            built_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_code_imports_source
            ON code_imports(source_file);
        CREATE INDEX IF NOT EXISTS idx_code_imports_module
            ON code_imports(imported_module);
        CREATE INDEX IF NOT EXISTS idx_code_imports_repo
            ON code_imports(repo_id);
    """)
    conn.commit()


@_register("029", "Daily Operations: daily_summaries + briefing_log tables")
def _mig_029(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            summary_type    TEXT NOT NULL DEFAULT 'morning',
            content         TEXT NOT NULL,
            headline        TEXT NOT NULL DEFAULT '',
            event_count     INTEGER NOT NULL DEFAULT 0,
            commit_count    INTEGER NOT NULL DEFAULT 0,
            repo_count      INTEGER NOT NULL DEFAULT 0,
            has_blockers    INTEGER NOT NULL DEFAULT 0,
            generated_at    TEXT NOT NULL,
            delivered       INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date, summary_type)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_summaries_date
            ON daily_summaries(date DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_summaries_type
            ON daily_summaries(summary_type);
        CREATE TABLE IF NOT EXISTS briefing_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            briefing_type   TEXT NOT NULL DEFAULT 'morning',
            source          TEXT NOT NULL DEFAULT 'daemon',
            headline        TEXT NOT NULL DEFAULT '',
            summary         TEXT NOT NULL DEFAULT '',
            generated_at    TEXT NOT NULL,
            delivered_to    TEXT NOT NULL DEFAULT '',
            UNIQUE(date, briefing_type, source)
        );
        CREATE INDEX IF NOT EXISTS idx_briefing_log_date
            ON briefing_log(date DESC);
    """)
    conn.commit()


@_register("028", "Agentic Action Layer: agent_sessions table")
def _mig_028(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id              TEXT PRIMARY KEY,
            task            TEXT NOT NULL,
            workspace       TEXT NOT NULL DEFAULT '.',
            created_at      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'running',
            summary         TEXT NOT NULL DEFAULT '',
            duration_ms     INTEGER NOT NULL DEFAULT 0,
            adapted         INTEGER NOT NULL DEFAULT 0,
            steps_json      TEXT NOT NULL DEFAULT '[]',
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_status
            ON agent_sessions(status);
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_created
            ON agent_sessions(created_at DESC);
    """)
    conn.commit()


def _run_sql_migrations(conn: sqlite3.Connection) -> None:
    """Apply SQL schema migrations from files in src/friday/migrations/.

    Each ``.sql`` file is a migration step that runs exactly once, tracked
    in ``_schema_versions`` with version = filename (e.g. ``sql001_core_tables``).
    Files are applied in lexicographic order (``sql001_*`` → ``sql002_*`` → …).

    Replaces the old ``conn.executescript(SCHEMA)`` on every connect().
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_versions ("
        "version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r["version"] for r in conn.execute("SELECT version FROM _schema_versions")}

    # core.py lives in the db/ package (one level below src/friday/), so the
    # migrations dir resolves via parent.parent. Fall back to parent for the
    # legacy monolithic db.py layout in case this module is ever moved back.
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not migrations_dir.is_dir():
        migrations_dir = Path(__file__).resolve().parent / "migrations"
    if not migrations_dir.is_dir():
        return  # No migrations directory — nothing to apply

    for sql_path in sorted(migrations_dir.glob("*.sql")):
        version = sql_path.stem  # e.g. "sql001_core_tables"
        if version in applied:
            continue
        try:
            sql = sql_path.read_text(encoding="utf-8")
        except OSError:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO _schema_versions (version, name, applied_at) VALUES (?, ?, ?)",
            (version, str(sql_path.name), now_iso()))
        conn.commit()


def _run_pending_migrations(conn: sqlite3.Connection) -> None:
    """Apply only unapplied schema migrations.

    Each migration step runs exactly once (tracked in _schema_versions).
    New migrations APPEND to _MIGRATIONS — never reorder or modify existing.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_versions ("
        "version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r["version"] for r in conn.execute("SELECT version FROM _schema_versions")}
    for version, name, fn in _MIGRATIONS:
        if version not in applied:
            fn(conn)
            conn.execute(
                "INSERT INTO _schema_versions (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now_iso()))
            conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Legacy wrapper — applies SQL + Python migrations."""
    _run_sql_migrations(conn)
    _run_pending_migrations(conn)
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


def insert_layer_history(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    event_type: str,
    previous_state: Optional[str] = None,
    new_state: Optional[str] = None,
    reason: str = "",
    metadata: Optional[Union[str, Dict]] = None,
) -> None:
    """Append one state transition to layer_history.

    Generic evolution record for any entity type. Replaces the 6 dead
    evolution tables (understanding_evolution, initiative_evolution, etc.).
    ``metadata`` can be a JSON string or a dict (which will be serialized).
    """
    meta_str: str
    if metadata is None:
        meta_str = "{}"
    elif isinstance(metadata, dict):
        meta_str = json.dumps(metadata, separators=(",", ":"), default=str)
    else:
        meta_str = metadata
    conn.execute(
        "INSERT INTO layer_history "
        "(entity_type, entity_id, event_type, previous_state, new_state, "
        "reason, metadata, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, event_type, previous_state, new_state,
         reason, meta_str, now_iso()),
    )
    commit_if_top(conn)


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


