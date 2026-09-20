PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id   TEXT PRIMARY KEY,
    description   TEXT NOT NULL,
    version       TEXT NOT NULL,
    source_format TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    pipeline_id   TEXT NOT NULL REFERENCES pipelines(pipeline_id),
    file_path     TEXT NOT NULL,
    file_hash     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'processed', 'failed')),
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at  TEXT,
    meeting_title TEXT,
    meeting_date  TEXT
);

CREATE INDEX IF NOT EXISTS idx_sources_pipeline ON sources(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_sources_status    ON sources(status);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id   TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    pipeline_id   TEXT NOT NULL REFERENCES pipelines(pipeline_id),
    artifact_type TEXT NOT NULL,
    content_path  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_source   ON artifacts(source_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_pipeline ON artifacts(pipeline_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    pipeline_id   TEXT NOT NULL REFERENCES pipelines(pipeline_id),
    key           TEXT NOT NULL,
    value         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pipeline_id, key)
);
