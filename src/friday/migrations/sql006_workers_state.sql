-- Friday DB migration: sql006_workers_state.sql

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

CREATE TABLE IF NOT EXISTS worker_capabilities (
    worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    capability              TEXT NOT NULL,
    PRIMARY KEY (worker_id, capability)
);

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

CREATE TABLE IF NOT EXISTS worker_versions (
    worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    version                 TEXT NOT NULL,
    registered_at           TEXT NOT NULL,
    changelog               TEXT,
    PRIMARY KEY (worker_id, version)
);

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

CREATE TABLE IF NOT EXISTS observed_session_ids (
    observer_name   TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    schema_version  TEXT NOT NULL DEFAULT '1',
    PRIMARY KEY (observer_name, session_id)
);

CREATE INDEX IF NOT EXISTS idx_worker_history_worker_id ON worker_history(worker_id);
CREATE INDEX IF NOT EXISTS idx_proposed_workers_status ON proposed_workers(status);

CREATE INDEX IF NOT EXISTS idx_conversation_log_channel ON conversation_log(channel);
CREATE INDEX IF NOT EXISTS idx_conversation_log_processed ON conversation_log(processed);

CREATE INDEX IF NOT EXISTS idx_conversation_log_conversation_at ON conversation_log(conversation_at DESC);

