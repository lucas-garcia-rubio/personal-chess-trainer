ALTER TABLE games ADD COLUMN canonical_document TEXT;

UPDATE games
SET canonical_document = json_array(
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    json_extract(raw_document, '$.moves'),
    white,
    black,
    game_result
)
WHERE origin = 'lichess';

PRAGMA user_version = 4;
