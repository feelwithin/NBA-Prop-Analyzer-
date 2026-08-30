"""
Build the SQLite database (data/nba_props.db) from sql/schema.sql
(which defines tables + feature views) and the CSVs in data/seed/.

Usage:
    python scripts/load_db.py [--seed-dir data/seed]
"""
import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "nba_props.db"
SCHEMA_PATH = ROOT / "sql" / "schema.sql"

TABLES = [
    ("teams.csv", "teams"),
    ("players.csv", "players"),
    ("games.csv", "games"),
    ("player_game_logs.csv", "player_game_logs"),
]


def load_csv(conn, csv_path, table_name, valid_fk=None):
    """valid_fk: optional dict of {column_name: set_of_valid_values} — rows
    whose value isn't in that set are skipped (with a warning) instead of
    crashing the whole load on a foreign-key violation. Used for
    player_game_logs, where real data can have a stray reference (a player
    the roster snapshot missed, etc.) that shouldn't take down everything
    else."""
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        placeholders = ",".join("?" for _ in header)
        rows = list(reader)

    skipped = 0
    if valid_fk:
        col_idx = {col: header.index(col) for col in valid_fk}
        kept = []
        for row in rows:
            if all(row[idx] in valid_fk[col] for col, idx in col_idx.items()):
                kept.append(row)
            else:
                skipped += 1
        rows = kept

    conn.executemany(
        f"INSERT INTO {table_name} ({','.join(header)}) VALUES ({placeholders})",
        rows,
    )
    print(f"  loaded {len(rows):>7} rows into {table_name}"
          + (f"  ({skipped} skipped: unresolved foreign key)" if skipped else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", default=str(ROOT / "data" / "seed"))
    args = parser.parse_args()
    seed_dir = Path(args.seed_dir)

    for csv_name, _ in TABLES:
        if not (seed_dir / csv_name).exists():
            raise SystemExit(f"Missing {seed_dir / csv_name} — run generate_seed_data.py or fetch_data.py first.")

    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())

    print(f"Loading data from {seed_dir}:")
    load_csv(conn, seed_dir / "teams.csv", "teams")
    load_csv(conn, seed_dir / "players.csv", "players")
    load_csv(conn, seed_dir / "games.csv", "games")

    valid_player_ids = {r[0] for r in conn.execute("SELECT player_id FROM players")}
    valid_game_ids = {r[0] for r in conn.execute("SELECT game_id FROM games")}
    valid_team_ids = {r[0] for r in conn.execute("SELECT team_id FROM teams")}
    load_csv(
        conn, seed_dir / "player_game_logs.csv", "player_game_logs",
        valid_fk={
            "player_id": {str(v) for v in valid_player_ids},
            "game_id": {str(v) for v in valid_game_ids},
            "team_id": {str(v) for v in valid_team_ids},
            "opponent_team_id": {str(v) for v in valid_team_ids},
        },
    )

    conn.commit()
    conn.close()
    print(f"\nDatabase built at {DB_PATH}")


if __name__ == "__main__":
    main()
