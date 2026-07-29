-- Friday DB migration: sql008_runtime.sql

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

CREATE INDEX IF NOT EXISTS idx_runtime_sessions_schedule_id ON runtime_sessions(schedule_id);
CREATE INDEX IF NOT EXISTS idx_runtime_events_session_id ON runtime_events(session_id);

CREATE INDEX IF NOT EXISTS idx_runtime_tasks_session_id ON runtime_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_runtime_results_session_id ON runtime_results(session_id);

CREATE INDEX IF NOT EXISTS idx_runtime_results_execution_id ON runtime_results(execution_id);
CREATE INDEX IF NOT EXISTS idx_runtime_history_session_id ON runtime_history(session_id);

CREATE INDEX IF NOT EXISTS idx_runtime_evolution_session_id ON runtime_evolution(session_id);

CREATE INDEX IF NOT EXISTS idx_layer_history_entity ON layer_history(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_layer_history_recorded ON layer_history(recorded_at);

