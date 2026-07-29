-- Friday DB migration: sql004_initiatives_insights.sql

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

CREATE TABLE IF NOT EXISTS initiative_relationships (
    id                  TEXT PRIMARY KEY,
    relationship_type    TEXT NOT NULL,   -- 'merge' or 'split'
    parent_ids          TEXT NOT NULL DEFAULT '',
    child_ids           TEXT NOT NULL DEFAULT '',
    build_at            TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    note                TEXT
);

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

CREATE INDEX IF NOT EXISTS idx_initiative_history_initiative_id ON initiative_history(initiative_id);

CREATE INDEX IF NOT EXISTS idx_insight_history_insight_id ON insight_history(insight_id);

