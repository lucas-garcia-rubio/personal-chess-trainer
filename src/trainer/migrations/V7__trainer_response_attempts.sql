CREATE TABLE trainer_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discussion_id INTEGER NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
    request_id INTEGER NOT NULL REFERENCES discussion_messages(id) ON DELETE CASCADE,
    retry_of INTEGER REFERENCES trainer_attempts(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted', 'failed', 'limited')),
    message_id INTEGER REFERENCES discussion_messages(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX one_running_trainer_response
    ON trainer_attempts(discussion_id) WHERE status = 'running';
CREATE TABLE trainer_events (
    attempt_id INTEGER NOT NULL REFERENCES trainer_attempts(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (attempt_id, sequence)
);
PRAGMA user_version = 8;
