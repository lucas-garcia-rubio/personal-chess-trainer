-- Derived delivery cursors only; visible history is never compacted here.
ALTER TABLE discussions ADD COLUMN session_checkpoint TEXT;
PRAGMA user_version = 9;
