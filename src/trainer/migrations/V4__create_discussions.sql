CREATE TABLE discussions (
    id INTEGER PRIMARY KEY,
    game_origin TEXT NOT NULL,
    game_origin_id TEXT NOT NULL,
    base_ply INTEGER NOT NULL CHECK (base_ply >= 0),
    base_fen TEXT NOT NULL,
    active_node_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (game_origin, game_origin_id, base_ply),
    FOREIGN KEY (game_origin, game_origin_id)
        REFERENCES games(origin, origin_id) ON DELETE CASCADE,
    FOREIGN KEY (active_node_id) REFERENCES variation_nodes(id)
);

CREATE TABLE discussion_messages (
    id INTEGER PRIMARY KEY,
    discussion_id INTEGER NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('player', 'trainer')),
    body TEXT NOT NULL CHECK (length(body) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (discussion_id) REFERENCES discussions(id) ON DELETE CASCADE
);

CREATE TABLE variation_nodes (
    id INTEGER PRIMARY KEY,
    discussion_id INTEGER NOT NULL,
    parent_id INTEGER,
    author TEXT NOT NULL CHECK (author IN ('player', 'trainer', 'evaluator')),
    move_uci TEXT NOT NULL,
    move_san TEXT NOT NULL,
    resulting_fen TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (discussion_id) REFERENCES discussions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES variation_nodes(id) ON DELETE CASCADE
);

PRAGMA user_version = 5;
