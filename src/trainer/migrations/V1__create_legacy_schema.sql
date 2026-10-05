CREATE TABLE games (
    source_id TEXT PRIMARY KEY,
    raw_document TEXT NOT NULL,
    analysis_document TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    opponent TEXT NOT NULL,
    result TEXT NOT NULL,
    speed TEXT NOT NULL,
    critical_count INTEGER NOT NULL
);

PRAGMA user_version = 2;
