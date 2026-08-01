-- Friday DB migration: sql009_capability_flags.sql
-- Capability flag table for the Self-Evolution Engine.
-- Tracks deployed capabilities, their status, dependencies, and rollback points.

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
