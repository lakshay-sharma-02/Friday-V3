-- Friday DB migration: sql005_plans_tasks.sql

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

CREATE INDEX IF NOT EXISTS idx_plan_history_plan_id ON plan_history(plan_id);

CREATE INDEX IF NOT EXISTS idx_tasks_graph_id ON tasks(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_edges_graph_id ON task_edges(graph_id);

CREATE INDEX IF NOT EXISTS idx_task_history_graph_id ON task_history(graph_id);
CREATE INDEX IF NOT EXISTS idx_task_graphs_status ON task_graphs(status);

