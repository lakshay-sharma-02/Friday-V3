-- Friday DB migration: sql002_observations_sessions.sql

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

