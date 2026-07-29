-- Friday DB migration: sql003_knowledge_understanding.sql

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

CREATE INDEX IF NOT EXISTS idx_knowledge_history_knowledge_id ON knowledge_history(knowledge_id);

CREATE INDEX IF NOT EXISTS idx_understanding_history_understanding_id ON understanding_history(understanding_id);

