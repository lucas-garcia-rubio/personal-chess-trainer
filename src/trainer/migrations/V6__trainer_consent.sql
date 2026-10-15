CREATE TABLE trainer_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consent_accepted INTEGER NOT NULL CHECK (consent_accepted IN (0, 1))
);
INSERT INTO trainer_preferences VALUES (1, 0);

ALTER TABLE discussions ADD COLUMN opencode_session_id TEXT;

PRAGMA user_version = 7;
