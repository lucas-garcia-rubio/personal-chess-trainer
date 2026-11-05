-- Support each independent filter and newest-first Games within a single value.
CREATE INDEX games_result_played_at ON games(result, played_at DESC);
CREATE INDEX games_time_control_played_at ON games(time_control, played_at DESC);
CREATE INDEX games_speed_played_at ON games(speed, played_at DESC);

PRAGMA user_version = 11;
