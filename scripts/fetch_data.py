"""
Pull REAL NBA data via the free `nba_api` package (wraps stats.nba.com)
into the exact schema this project uses, so scripts/load_db.py works
unchanged on real data — same shape as the shipped synthetic dataset.

*** Run this on your own machine, not in a sandboxed/offline env ***
It needs internet access to stats.nba.com and can take several
minutes for a full season (rate-limited on purpose to be a good
citizen of a free, unofficial API).

Setup:
    pip install nba_api pandas

Usage:
    python scripts/fetch_data.py --season 2024-25
    python scripts/fetch_data.py --season 2025-26 --append

Multiple seasons: by default this OVERWRITES data/seed/*.csv with
just the one season fetched. Pass --append to instead MERGE this
season's data in alongside whatever's already there — that's how you
build up a multi-season database (e.g. keep 2024-25 as your stable
baseline and add 2025-26, rookies included, without losing the
first). Real game IDs from the NBA API are globally unique across
seasons, so appended seasons never collide; players are merged by
their persistent player_id, so a player already known keeps their
history and a brand-new one (a rookie) just gets added.

Output:
    Writes data/seed/teams.csv, players.csv, games.csv,
    player_game_logs.csv — same columns as generate_seed_data.py, so
    `python scripts/load_db.py` picks them up with no changes.

Notes on scope:
  - Real, accurate conference/division per team (hardcoded below —
    this doesn't change season to season, unlike rosters).
  - Real rosters + positions via CommonTeamRoster (one call/team).
  - Real box scores for every game in the season via LeagueGameLog
    (two calls total: team-level for final scores, player-level for
    box scores) — much more efficient than looping per-game.
  - "Who's likely guarding him" — this schema tracks TEAM-level
    defense by position (see v_team_position_defense), not individual
    defender assignments. Real individual-matchup data does exist in
    the NBA's tracking stats (nba_api.stats.endpoints has
    leagueseasonmatchups / matchupsrollup), but it's heavier to pull
    (per-matchup-pair calls) and less complete historically. That's a
    natural Phase 2 extension once the core model is validated — see
    README "Roadmap".
"""
import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"

# Accurate conference/division — stable across seasons, keyed by abbreviation.
CONF_DIV = {
    "BOS": ("East", "Atlantic"), "BKN": ("East", "Atlantic"), "NYK": ("East", "Atlantic"),
    "PHI": ("East", "Atlantic"), "TOR": ("East", "Atlantic"),
    "CHI": ("East", "Central"), "CLE": ("East", "Central"), "DET": ("East", "Central"),
    "IND": ("East", "Central"), "MIL": ("East", "Central"),
    "ATL": ("East", "Southeast"), "CHA": ("East", "Southeast"), "MIA": ("East", "Southeast"),
    "ORL": ("East", "Southeast"), "WAS": ("East", "Southeast"),
    "DEN": ("West", "Northwest"), "MIN": ("West", "Northwest"), "OKC": ("West", "Northwest"),
    "POR": ("West", "Northwest"), "UTA": ("West", "Northwest"),
    "GSW": ("West", "Pacific"), "LAC": ("West", "Pacific"), "LAL": ("West", "Pacific"),
    "PHX": ("West", "Pacific"), "SAC": ("West", "Pacific"),
    "DAL": ("West", "Southwest"), "HOU": ("West", "Southwest"), "MEM": ("West", "Southwest"),
    "NOP": ("West", "Southwest"), "SAS": ("West", "Southwest"),
}


def require_nba_api():
    try:
        import nba_api  # noqa: F401
    except ImportError:
        sys.exit(
            "nba_api is not installed. Run: pip install nba_api pandas\n"
            "(This script must be run somewhere with internet access to stats.nba.com.)"
        )


def fetch_teams():
    from nba_api.stats.static import teams as static_teams
    rows = []
    for t in static_teams.get_teams():
        conf, div = CONF_DIV.get(t["abbreviation"], ("", ""))
        rows.append((t["id"], t["nickname"], t["abbreviation"], t["city"], conf, div))
    return rows


def fetch_rosters(team_rows, season, sleep_s):
    from nba_api.stats.endpoints import commonteamroster
    players = {}
    for team_id, name, abbr, *_ in team_rows:
        print(f"  roster: {abbr}...")
        for attempt in range(3):
            try:
                df = commonteamroster.CommonTeamRoster(team_id=team_id, season=season).get_data_frames()[0]
                break
            except Exception as e:
                print(f"    retry ({e})")
                time.sleep(2)
        else:
            print(f"    skipping {abbr} after 3 failed attempts")
            continue
        for _, r in df.iterrows():
            pos_raw = str(r.get("POSITION") or "F")
            pos = pos_raw[0].upper() if pos_raw and pos_raw[0].upper() in ("G", "F", "C") else "F"
            role = "starter"  # nba_api doesn't label star/bench; downstream stats speak for themselves
            players[int(r["PLAYER_ID"])] = (int(r["PLAYER_ID"]), r["PLAYER"], team_id, pos, role)
        time.sleep(sleep_s)
    return list(players.values())


def fetch_games_and_logs(season, season_type, sleep_s):
    from nba_api.stats.endpoints import leaguegamelog

    print("  team game log (for final scores)...")
    team_df = leaguegamelog.LeagueGameLog(
        season=season, season_type_all_star=season_type, player_or_team_abbreviation="T"
    ).get_data_frames()[0]
    time.sleep(sleep_s)

    print("  player game log (box scores)...")
    player_df = leaguegamelog.LeagueGameLog(
        season=season, season_type_all_star=season_type, player_or_team_abbreviation="P"
    ).get_data_frames()[0]

    # Build games.csv from the team-level log: each GAME_ID has two rows
    # (home = 'vs.' in MATCHUP, away = '@'). GAME_ID is used AS-IS (it's
    # already a unique string from the NBA's own system, globally unique
    # across every season) rather than remapped to a sequential integer —
    # that's what lets --append merge multiple seasons with zero risk of
    # two different games colliding on the same id.
    games = {}
    for _, r in team_df.iterrows():
        game_id = r["GAME_ID"]
        is_home = " vs. " in r["MATCHUP"]
        games.setdefault(game_id, {"date": r["GAME_DATE"]})
        if is_home:
            games[game_id]["home_team_id"] = int(r["TEAM_ID"])
            games[game_id]["home_score"] = int(r["PTS"])
        else:
            games[game_id]["away_team_id"] = int(r["TEAM_ID"])
            games[game_id]["away_score"] = int(r["PTS"])

    game_rows = []
    valid_game_ids = set()
    for game_id, g in games.items():
        if "home_team_id" not in g or "away_team_id" not in g:
            continue  # incomplete pairing, skip
        valid_game_ids.add(game_id)
        game_rows.append((game_id, g["date"], season, g["home_team_id"], g["away_team_id"], g["home_score"], g["away_score"]))
    game_rows.sort(key=lambda row: row[1])  # by date

    # Build player_game_logs.csv. Also track every (player_id -> name, team_id)
    # seen here — the roster snapshot (CommonTeamRoster) can miss players who
    # only briefly appeared that season (10-day contracts, call-ups, players
    # who changed teams), and any such gap would violate the players<-logs
    # foreign key later. main() backfills those from this dict.
    log_rows = []
    players_seen = {}
    for _, r in player_df.iterrows():
        game_id = r["GAME_ID"]
        if game_id not in valid_game_ids:
            continue
        g = games[game_id]
        team_id = int(r["TEAM_ID"])
        is_home = 1 if team_id == g.get("home_team_id") else 0
        opponent_team_id = g["away_team_id"] if is_home else g["home_team_id"]
        player_id = int(r["PLAYER_ID"])
        players_seen[player_id] = (r["PLAYER_NAME"], team_id)
        log_id = f"{game_id}_{player_id}"  # unique per game+player, no counter needed

        def num(col, default=0):
            val = r.get(col)
            try:
                return int(val) if val == val else default  # NaN check
            except (TypeError, ValueError):
                return default

        log_rows.append((
            log_id, game_id, g["date"], season, player_id, team_id,
            opponent_team_id, is_home, num("MIN"), num("PTS"), num("REB"), num("AST"),
            num("STL"), num("BLK"), num("TOV"), num("FGM"), num("FGA"),
            num("FG3M"), num("FG3A"), num("FTM"), num("FTA"),
        ))

    return game_rows, log_rows, players_seen


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {len(rows)} rows -> {path}")


def read_csv_rows(path):
    """Returns (header, list of tuples) or (None, []) if the file doesn't exist."""
    if not path.exists():
        return None, []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, [tuple(row) for row in reader]


def merge_by_key(existing_rows, new_rows, key_index):
    """Merge new_rows into existing_rows, keyed by the value at key_index.
    New rows win on a key collision (e.g. re-fetching the same season
    overwrites stale data for it); anything only in existing_rows (a
    prior season's games/players not touched by this fetch) is kept."""
    merged = {row[key_index]: row for row in existing_rows}
    for row in new_rows:
        merged[str(row[key_index])] = row
    return list(merged.values())


def check_header_compatible(path, existing_header, expected_header):
    """Guards against silently corrupting --append merges: if the file on
    disk was written by an older version of this script with a different
    column layout (e.g. no `season` column, before the multi-season
    update), naively merging its rows with new rows shifts every old
    row's columns out of position — values land under the wrong column
    name with nothing to catch it until much later (a confusing wall of
    'unresolved foreign key' skips in load_db.py, or worse, silently
    wrong stats). Fail loudly here instead."""
    if existing_header is not None and existing_header != expected_header:
        raise SystemExit(
            f"\n{path} has an outdated column layout and can't be safely merged with --append:\n"
            f"  found:    {existing_header}\n"
            f"  expected: {expected_header}\n\n"
            f"This happens when the existing file was fetched with an older version of this "
            f"script. Merging anyway would corrupt it (columns would shift out of position).\n\n"
            f"Fix: re-fetch your base season fresh (WITHOUT --append) to bring data/seed/*.csv "
            f"up to the current format, then re-run your --append fetch for the newer season:\n"
            f"  python3 scripts/fetch_data.py --season <your base season, e.g. 2024-25>\n"
            f"  python3 scripts/fetch_data.py --season <newer season, e.g. 2025-26> --append\n"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2025-26", help="e.g. 2025-26")
    parser.add_argument("--season-type", default="Regular Season",
                         choices=["Regular Season", "Playoffs", "Pre Season"])
    parser.add_argument("--sleep", type=float, default=0.6, help="seconds between API calls (be polite)")
    parser.add_argument("--append", action="store_true",
                         help="merge this season into the existing data/seed/*.csv instead of overwriting")
    args = parser.parse_args()

    require_nba_api()
    SEED_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching teams...")
    team_rows = fetch_teams()
    write_csv(SEED_DIR / "teams.csv",
              ["team_id", "team_name", "abbreviation", "city", "conference", "division"], team_rows)

    print("Fetching rosters (30 calls, this takes a bit)...")
    player_rows = fetch_rosters(team_rows, args.season, args.sleep)

    print("Fetching season game logs...")
    game_rows, log_rows, players_seen = fetch_games_and_logs(args.season, args.season_type, args.sleep)

    # Backfill any player who has box-score rows but wasn't on the roster
    # snapshot (call-ups, 10-day contracts, mid-season signings) — otherwise
    # their game log rows would violate the players<-logs foreign key and
    # load_db.py would refuse to load ANY data. Position is unknown for
    # these, so default to 'F' (documented, not guessed silently).
    known_ids = {p[0] for p in player_rows}
    backfilled = 0
    for player_id, (name, team_id) in players_seen.items():
        if player_id not in known_ids:
            player_rows.append((player_id, name, team_id, "F", "starter"))
            backfilled += 1
    if backfilled:
        print(f"  backfilled {backfilled} player(s) missing from roster snapshots "
              f"(position defaulted to 'F' — likely call-ups/trades)")

    PLAYERS_HEADER = ["player_id", "full_name", "team_id", "position", "role"]
    GAMES_HEADER = ["game_id", "game_date", "season", "home_team_id", "away_team_id", "home_score", "away_score"]
    LOGS_HEADER = ["log_id", "game_id", "game_date", "season", "player_id", "team_id", "opponent_team_id", "is_home",
                   "minutes", "points", "rebounds", "assists", "steals", "blocks", "turnovers",
                   "fg_made", "fg_attempted", "three_made", "three_attempted", "ft_made", "ft_attempted"]

    if args.append:
        print(f"Merging {args.season} into existing data/seed/ (--append)...")
        players_header, existing_players = read_csv_rows(SEED_DIR / "players.csv")
        games_header, existing_games = read_csv_rows(SEED_DIR / "games.csv")
        logs_header, existing_logs = read_csv_rows(SEED_DIR / "player_game_logs.csv")

        check_header_compatible(SEED_DIR / "players.csv", players_header, PLAYERS_HEADER)
        check_header_compatible(SEED_DIR / "games.csv", games_header, GAMES_HEADER)
        check_header_compatible(SEED_DIR / "player_game_logs.csv", logs_header, LOGS_HEADER)

        player_rows = merge_by_key(existing_players, [tuple(str(v) for v in row) for row in player_rows], key_index=0)
        game_rows = merge_by_key(existing_games, [tuple(str(v) for v in row) for row in game_rows], key_index=0)
        log_rows = merge_by_key(existing_logs, [tuple(str(v) for v in row) for row in log_rows], key_index=0)
        print(f"  merged totals: {len(player_rows)} players, {len(game_rows)} games, {len(log_rows)} log rows")

    write_csv(SEED_DIR / "players.csv", PLAYERS_HEADER, player_rows)
    write_csv(SEED_DIR / "games.csv", GAMES_HEADER, game_rows)
    write_csv(SEED_DIR / "player_game_logs.csv", LOGS_HEADER, log_rows)

    print(f"\nDone. Now run: python scripts/load_db.py")


if __name__ == "__main__":
    main()
