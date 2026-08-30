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


def load_csv(conn, csv_path, table_name):
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        placeholders = ",".join("?" for _ in header)
        rows = list(reader)
        conn.executemany(
            f"INSERT INTO {table_name} ({','.join(header)}) VALUES ({placeholders})",
            rows,
        )
    print(f"  loaded {len(rows):>7} rows into {table_name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", default=str(ROOT / "data" / "seed"))
    args = parser.parse_args()
    seed_dir = Path(args.seed_dir)

    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())

    print(f"Loading data from {seed_dir}:")
    for csv_name, table_name in TABLES:
        csv_path = seed_dir / csv_name
        if not csv_path.exists():
            raise SystemExit(f"Missing {csv_path} — run generate_seed_data.py or fetch_data.py first.")
        load_csv(conn, csv_path, table_name)

    conn.commit()
    conn.close()
    print(f"\nDatabase built at {DB_PATH}")


if __name__ == "__main__":
    main()
