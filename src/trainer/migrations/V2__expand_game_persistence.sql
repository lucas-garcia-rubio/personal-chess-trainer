CREATE TABLE games_v2 (
    origin TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    raw_document TEXT NOT NULL,
    headers_document TEXT NOT NULL,
    analysis_document TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    played_at INTEGER NOT NULL,
    white TEXT NOT NULL,
    black TEXT NOT NULL,
    game_result TEXT NOT NULL,
    time_control TEXT NOT NULL,
    eco TEXT,
    opening TEXT,
    opponent TEXT NOT NULL,
    result TEXT NOT NULL,
    speed TEXT NOT NULL,
    critical_count INTEGER NOT NULL,
    PRIMARY KEY (origin, origin_id)
);

INSERT INTO games_v2 (
    origin, origin_id, source_id, raw_document, headers_document,
    analysis_document, created_at, played_at, white, black, game_result,
    time_control, eco, opening, opponent, result, speed, critical_count
)
SELECT
    'lichess',
    source_id,
    source_id,
    raw_document,
    json_patch(
        json_object(
            'Site', 'https://lichess.org/' || source_id,
            'Date', strftime('%Y.%m.%d', created_at / 1000, 'unixepoch'),
            'White', json_extract(raw_document, '$.players.white.user.name'),
            'Black', json_extract(raw_document, '$.players.black.user.name'),
            'Result', CASE json_extract(raw_document, '$.winner')
                WHEN 'white' THEN '1-0'
                WHEN 'black' THEN '0-1'
                ELSE '1/2-1/2'
            END,
            'GameId', source_id,
            'UTCDate', strftime('%Y.%m.%d', created_at / 1000, 'unixepoch'),
            'UTCTime', strftime('%H:%M:%S', created_at / 1000, 'unixepoch'),
            'Variant', COALESCE(json_extract(raw_document, '$.variant'), 'standard'),
            'Termination', json_extract(raw_document, '$.status')
        ),
        json_patch(
            CASE
                WHEN json_type(raw_document, '$.arenaTour.name') IS NOT NULL
                    THEN json_object(
                        'Event',
                        json_extract(raw_document, '$.arenaTour.name')
                    )
                WHEN json_type(raw_document, '$.source') IS NOT NULL
                    THEN json_object('Event', json_extract(raw_document, '$.source'))
                ELSE '{}'
            END,
            json_patch(
                json_object(
                    'TimeControl',
                    printf(
                        '%d+%d',
                        json_extract(raw_document, '$.clock.initial'),
                        json_extract(raw_document, '$.clock.increment')
                    )
                ),
                json_patch(
                    CASE WHEN json_type(raw_document, '$.opening.eco') IS NOT NULL
                        THEN json_object('ECO', json_extract(raw_document, '$.opening.eco'))
                        ELSE '{}'
                    END,
                    CASE WHEN json_type(raw_document, '$.opening.name') IS NOT NULL
                        THEN json_object('Opening', json_extract(raw_document, '$.opening.name'))
                        ELSE '{}'
                    END
                )
            )
        )
    ),
    json_set(
        analysis_document,
        '$.evaluator',
        json_object(
            'source_kind', 'lichess-server',
            'name', 'Lichess',
            'version', NULL,
            'parameters', json('{}')
        )
    ),
    created_at,
    created_at,
    json_extract(raw_document, '$.players.white.user.name'),
    json_extract(raw_document, '$.players.black.user.name'),
    CASE json_extract(raw_document, '$.winner')
        WHEN 'white' THEN '1-0'
        WHEN 'black' THEN '0-1'
        ELSE '1/2-1/2'
    END,
    printf(
        '%d+%d',
        json_extract(raw_document, '$.clock.initial'),
        json_extract(raw_document, '$.clock.increment')
    ),
    json_extract(raw_document, '$.opening.eco'),
    json_extract(raw_document, '$.opening.name'),
    opponent,
    result,
    speed,
    critical_count
FROM games;

DROP TABLE games;
ALTER TABLE games_v2 RENAME TO games;

UPDATE games
SET headers_document = json_set(
    headers_document,
    '$.WhiteElo',
    CAST(json_extract(raw_document, '$.players.white.rating') AS TEXT)
)
WHERE json_type(raw_document, '$.players.white.rating') IS NOT NULL;

UPDATE games
SET headers_document = json_set(
    headers_document,
    '$.BlackElo',
    CAST(json_extract(raw_document, '$.players.black.rating') AS TEXT)
)
WHERE json_type(raw_document, '$.players.black.rating') IS NOT NULL;

UPDATE games
SET headers_document = json_set(
    headers_document,
    '$.WhiteRatingDiff',
    printf('%+d', json_extract(raw_document, '$.players.white.ratingDiff'))
)
WHERE json_type(raw_document, '$.players.white.ratingDiff') IS NOT NULL;

UPDATE games
SET headers_document = json_set(
    headers_document,
    '$.BlackRatingDiff',
    printf('%+d', json_extract(raw_document, '$.players.black.ratingDiff'))
)
WHERE json_type(raw_document, '$.players.black.ratingDiff') IS NOT NULL;

CREATE INDEX games_created_at ON games(created_at DESC);

PRAGMA user_version = 3;
