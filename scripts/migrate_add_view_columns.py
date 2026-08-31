"""
One-time migration for an EXISTING data/nba_props.db: adds the new
prop stats (Turnovers, 3-Pointers Made, and the FanDuel-style combo
props Pts+Reb, Pts+Ast, Reb+Ast, Blocks+Steals) by rebuilding the
v_player_rolling_stats view to expose the box-score columns they need.

Why this exists: the underlying player_game_logs TABLE already had
every column these new stats need (turnovers, three_made) — it's the
VIEW the app actually queries that was missing three_made. Views are
just saved queries, so fixing this doesn't touch or re-fetch any of
your already-loaded game data; it only changes what one query exposes.
If you ever rebuild the database from scratch (fetch_data.py + this
repo's current load_db.py), you get the fix automatically and don't
need to run this — it's only for a database that already existed
before this change.

Usage:
    python scripts/migrate_add_view_columns.py
"""
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "nba_props.db"

NEW_VIEW_SQL = """
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
    pgl.three_made,
    ROW_NUMBER() OVER (
        PARTITION BY pgl.player_id, pgl.season ORDER BY pgl.game_date DESC
    ) AS games_ago
FROM player_game_logs pgl
JOIN players p ON p.player_id = pgl.player_id
JOIN teams ot   ON ot.team_id = pgl.opponent_team_id;
"""


def main():
    if not DB_PATH.exists():
        print(f"{DB_PATH} not found — nothing to migrate (a fresh load already has the fix).")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP VIEW IF EXISTS v_player_rolling_stats;")
    conn.execute(NEW_VIEW_SQL)
    conn.commit()
    conn.close()
    print("v_player_rolling_stats rebuilt with three_made — TOV/FG3M/PR/PA/RA/STOCKS props are now available.")


if __name__ == "__main__":
    main()
