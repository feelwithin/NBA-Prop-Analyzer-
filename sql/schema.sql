-- ============================================================
-- NBA Prop Analyzer — Database Schema
-- SQLite dialect. Designed for REAL historical data (see
-- scripts/fetch_data.py) but works identically with the shipped
-- synthetic demo dataset. Supports MULTIPLE SEASONS loaded at once
-- (see fetch_data.py --append) — every stat query is season-scoped.
-- ============================================================

PRAGMA foreign_keys = ON;

DROP VIEW IF EXISTS v_player_vs_opponent_history;
DROP VIEW IF EXISTS v_team_position_defense;
DROP VIEW IF EXISTS v_team_defense_rating;
DROP VIEW IF EXISTS v_player_home_away_splits;
DROP VIEW IF EXISTS v_player_rolling_stats;
DROP TABLE IF EXISTS player_game_logs;
DROP TABLE IF EXISTS games;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS teams;

-- ------------------------------------------------------------
-- Teams: the 30 real NBA franchises. Team identity/conference/
-- division doesn't change season to season, so one row per team
-- covers every season loaded.
-- ------------------------------------------------------------
CREATE TABLE teams (
    team_id       INTEGER PRIMARY KEY,
    team_name     TEXT NOT NULL,
    abbreviation  TEXT NOT NULL UNIQUE,
    city          TEXT NOT NULL,
    conference    TEXT NOT NULL CHECK (conference IN ('East', 'West')),
    division      TEXT NOT NULL
);

-- ------------------------------------------------------------
-- Players. A real player keeps the same player_id across every
-- season (that's how fetch_data.py --append merges rookies/new
-- players in without duplicating anyone already known); team_id
-- here reflects their MOST RECENTLY FETCHED roster only — for the
-- team they played for in a specific game, use
-- player_game_logs.team_id instead, which is per-game and always
-- correct regardless of trades.
-- ------------------------------------------------------------
CREATE TABLE players (
    player_id     INTEGER PRIMARY KEY,
    full_name     TEXT NOT NULL,
    team_id       INTEGER NOT NULL REFERENCES teams(team_id),
    position      TEXT NOT NULL CHECK (position IN ('G', 'F', 'C')),
    role          TEXT NOT NULL CHECK (role IN ('star', 'starter', 'bench'))
);

-- ------------------------------------------------------------
-- Games: one row per game. game_id is TEXT — real fetches use the
-- NBA's own GAME_ID string directly (globally unique across every
-- season, so games from different seasons can never collide when
-- appended), and the synthetic generator mints season-prefixed IDs
-- for the same reason.
-- ------------------------------------------------------------
CREATE TABLE games (
    game_id        TEXT PRIMARY KEY,
    game_date      TEXT NOT NULL,
    season         TEXT NOT NULL,
    home_team_id   INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id   INTEGER NOT NULL REFERENCES teams(team_id),
    home_score     INTEGER NOT NULL,
    away_score     INTEGER NOT NULL
);

CREATE INDEX idx_games_season ON games(season);

-- ------------------------------------------------------------
-- Player box-score log: one row per player per game they played.
-- Denormalized with opponent_team_id / is_home / season so it can
-- be filtered directly without re-deriving from games each time —
-- this is the core "fact table" the prop model reads from.
-- log_id is TEXT: game_id + player_id is always unique per game,
-- so this needs no separate counter to stay collision-free across
-- appended fetch runs.
-- ------------------------------------------------------------
CREATE TABLE player_game_logs (
    log_id          TEXT PRIMARY KEY,
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    game_date       TEXT NOT NULL,
    season          TEXT NOT NULL,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    opponent_team_id INTEGER NOT NULL REFERENCES teams(team_id),
    is_home         INTEGER NOT NULL CHECK (is_home IN (0, 1)),
    minutes         INTEGER NOT NULL,
    points          INTEGER NOT NULL,
    rebounds        INTEGER NOT NULL,
    assists         INTEGER NOT NULL,
    steals          INTEGER NOT NULL,
    blocks          INTEGER NOT NULL,
    turnovers       INTEGER NOT NULL,
    fg_made         INTEGER NOT NULL,
    fg_attempted    INTEGER NOT NULL,
    three_made      INTEGER NOT NULL,
    three_attempted INTEGER NOT NULL,
    ft_made         INTEGER NOT NULL,
    ft_attempted    INTEGER NOT NULL
);

CREATE INDEX idx_pgl_player ON player_game_logs(player_id);
CREATE INDEX idx_pgl_opponent ON player_game_logs(opponent_team_id);
CREATE INDEX idx_pgl_game ON player_game_logs(game_id);
CREATE INDEX idx_pgl_date ON player_game_logs(game_date);
CREATE INDEX idx_pgl_season ON player_game_logs(season);
CREATE INDEX idx_players_team ON players(team_id);

-- ============================================================
-- Feature views used by the prop probability engine.
-- (Defined here so schema + features load in one script; kept in
--  a separate, readable copy at sql/feature_queries.sql for review.)
-- Every view is season-scoped (grouped/partitioned by season, not
-- just by player/team) so stats from different seasons never blend
-- together silently — callers filter with `WHERE season = ?`.
-- ============================================================

-- Rolling per-player averages over their last N games (N handled
-- in Python via window sizing on top of this ordered view; here we
-- just expose a clean ordered row-per-game-played stream per stat).
-- games_ago resets per season, so "last 20 games" means 20 games
-- within the selected season, not bleeding into a prior one.
CREATE VIEW v_player_rolling_stats AS
SELECT
    pgl.player_id,
    p.full_name,
    pgl.season,
    pgl.game_id,
    pgl.game_date,
    pgl.opponent_team_id,
    ot.abbreviation AS opponent_abbr,
    pgl.is_home,
    pgl.minutes,
    pgl.points,
    pgl.rebounds,
    pgl.assists,
    pgl.steals,
    pgl.blocks,
    pgl.turnovers,
    ROW_NUMBER() OVER (
        PARTITION BY pgl.player_id, pgl.season ORDER BY pgl.game_date DESC
    ) AS games_ago
FROM player_game_logs pgl
JOIN players p ON p.player_id = pgl.player_id
JOIN teams ot   ON ot.team_id = pgl.opponent_team_id;

-- Home vs away per-player splits
CREATE VIEW v_player_home_away_splits AS
SELECT
    player_id,
    season,
    is_home,
    COUNT(*)              AS games_played,
    ROUND(AVG(points), 2)   AS avg_points,
    ROUND(AVG(rebounds), 2) AS avg_rebounds,
    ROUND(AVG(assists), 2)  AS avg_assists
FROM player_game_logs
GROUP BY player_id, season, is_home;

-- Team defensive rating: points allowed per game, plus a league
-- percentile rank WITHIN THAT SEASON (0 = stingiest defense, 1 =
-- worst defense).
CREATE VIEW v_team_defense_rating AS
WITH points_allowed AS (
    SELECT team_id, season, AVG(points_allowed) AS avg_points_allowed
    FROM (
        SELECT home_team_id AS team_id, season, away_score AS points_allowed FROM games
        UNION ALL
        SELECT away_team_id AS team_id, season, home_score AS points_allowed FROM games
    )
    GROUP BY team_id, season
)
SELECT
    t.team_id,
    t.team_name,
    t.abbreviation,
    pa.season,
    ROUND(pa.avg_points_allowed, 2) AS avg_points_allowed,
    ROUND(PERCENT_RANK() OVER (PARTITION BY pa.season ORDER BY pa.avg_points_allowed ASC), 3) AS defense_percentile
FROM points_allowed pa
JOIN teams t ON t.team_id = pa.team_id;

-- Team defense broken out by the position of the opposing player —
-- i.e. how many points/rebounds/assists a team gives up to Guards,
-- Forwards, and Centers specifically, WITHIN A GIVEN SEASON. This
-- is the closest proxy to "who guards him" available without
-- individual matchup-tracking data (see scripts/fetch_data.py
-- notes on Phase 2 matchup data).
CREATE VIEW v_team_position_defense AS
SELECT
    pgl.opponent_team_id AS team_id,
    pgl.season,
    p.position,
    COUNT(*)                 AS player_games_faced,
    ROUND(AVG(pgl.points), 2)   AS avg_points_allowed,
    ROUND(AVG(pgl.rebounds), 2) AS avg_rebounds_allowed,
    ROUND(AVG(pgl.assists), 2)  AS avg_assists_allowed
FROM player_game_logs pgl
JOIN players p ON p.player_id = pgl.player_id
GROUP BY pgl.opponent_team_id, pgl.season, p.position;

-- A player's historical performance specifically against one
-- opponent, WITHIN A GIVEN SEASON (small samples — used as a minor
-- signal, not the core of the model).
CREATE VIEW v_player_vs_opponent_history AS
SELECT
    player_id,
    season,
    opponent_team_id,
    COUNT(*)               AS games_played,
    ROUND(AVG(points), 2)     AS avg_points,
    ROUND(AVG(rebounds), 2)   AS avg_rebounds,
    ROUND(AVG(assists), 2)    AS avg_assists
FROM player_game_logs
GROUP BY player_id, season, opponent_team_id;
