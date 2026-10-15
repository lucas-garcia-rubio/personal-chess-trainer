ALTER TABLE discussions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);

PRAGMA user_version = 6;
