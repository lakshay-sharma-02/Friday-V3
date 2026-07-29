-- Friday DB migration: sql007_resolver_scheduler.sql

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

CREATE INDEX IF NOT EXISTS idx_resolver_evolution_graph_id ON resolver_evolution(graph_id);
CREATE INDEX IF NOT EXISTS idx_resolver_history_assignment_id ON resolver_history(assignment_id);

CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_graph_id ON scheduler_tasks(graph_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_history_graph_id ON scheduler_history(graph_id);

CREATE INDEX IF NOT EXISTS idx_scheduler_history_schedule_id ON scheduler_history(schedule_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_graph_id ON scheduler_runs(graph_id);

CREATE INDEX IF NOT EXISTS idx_scheduler_evolution_graph_id ON scheduler_evolution(graph_id);

