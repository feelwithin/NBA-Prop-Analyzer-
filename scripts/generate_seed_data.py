"""
Generate a reproducible SYNTHETIC demo dataset for the NBA Prop
Analyzer, so the whole pipeline (schema, feature views, prop model,
CLI, app) is runnable and testable with zero setup.

- Teams are REAL (30 current franchises, correct conference/division).
- Players, games, and box scores are SIMULATED. Crucially, each
  team is assigned a hidden per-position defensive strength factor
  (Guards/Forwards/Centers), and simulated box scores are generated
  AGAINST that factor — so the dataset actually encodes realistic
  "tougher defense suppresses stats" matchup effects for the prop
  model to detect and react to. This makes it a legitimate test of
  the model's matchup-adjustment logic, not just noise.
- Generates TWO seasons so multi-season support (see fetch_data.py
  --append and app.py's season picker) has something real to test
  against: a "prior" season and a "current" season, with a handful
  of brand-new players only in the current season (simulating
  rookies who weren't in the league yet during the prior one).

For real players (e.g. Shai Gilgeous-Alexander) and real matchup
effects, use scripts/fetch_data.py instead (requires internet).

Usage:
    python scripts/generate_seed_data.py
Writes CSVs to data/seed/.
"""
import csv
import random
from pathlib import Path

random.seed(7)

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"
SEED_DIR.mkdir(parents=True, exist_ok=True)

SEASONS = ["2024-25", "2025-26"]  # [prior, current]
ROOKIES_PER_SEASON_B = 15  # league-wide, simulating a draft class

TEAMS = [
    (1, "Celtics", "BOS", "Boston", "East", "Atlantic"),
    (2, "Nets", "BKN", "Brooklyn", "East", "Atlantic"),
    (3, "Knicks", "NYK", "New York", "East", "Atlantic"),
    (4, "76ers", "PHI", "Philadelphia", "East", "Atlantic"),
    (5, "Raptors", "TOR", "Toronto", "East", "Atlantic"),
    (6, "Bulls", "CHI", "Chicago", "East", "Central"),
    (7, "Cavaliers", "CLE", "Cleveland", "East", "Central"),
    (8, "Pistons", "DET", "Detroit", "East", "Central"),
    (9, "Pacers", "IND", "Indiana", "East", "Central"),
    (10, "Bucks", "MIL", "Milwaukee", "East", "Central"),
    (11, "Hawks", "ATL", "Atlanta", "East", "Southeast"),
    (12, "Hornets", "CHA", "Charlotte", "East", "Southeast"),
    (13, "Heat", "MIA", "Miami", "East", "Southeast"),
    (14, "Magic", "ORL", "Orlando", "East", "Southeast"),
    (15, "Wizards", "WAS", "Washington", "East", "Southeast"),
    (16, "Nuggets", "DEN", "Denver", "West", "Northwest"),
    (17, "Timberwolves", "MIN", "Minnesota", "West", "Northwest"),
    (18, "Thunder", "OKC", "Oklahoma City", "West", "Northwest"),
    (19, "Trail Blazers", "POR", "Portland", "West", "Northwest"),
    (20, "Jazz", "UTA", "Utah", "West", "Northwest"),
    (21, "Warriors", "GSW", "Golden State", "West", "Pacific"),
    (22, "Clippers", "LAC", "LA Clippers", "West", "Pacific"),
    (23, "Lakers", "LAL", "LA Lakers", "West", "Pacific"),
    (24, "Suns", "PHX", "Phoenix", "West", "Pacific"),
    (25, "Kings", "SAC", "Sacramento", "West", "Pacific"),
    (26, "Mavericks", "DAL", "Dallas", "West", "Southwest"),
    (27, "Rockets", "HOU", "Houston", "West", "Southwest"),
    (28, "Grizzlies", "MEM", "Memphis", "West", "Southwest"),
    (29, "Pelicans", "NOP", "New Orleans", "West", "Southwest"),
    (30, "Spurs", "SAS", "San Antonio", "West", "Southwest"),
]

FIRST_NAMES = [
    "James", "Michael", "Chris", "DeMarcus", "Anthony", "Jalen", "Jordan",
    "Marcus", "Devin", "Tyler", "Malik", "Isaiah", "Jaylen", "Cameron",
    "Xavier", "Elijah", "Nathan", "Terrence", "Andre", "Kevin", "Brandon",
    "Julian", "Darius", "Trevon", "Amir", "Caleb", "Jamal", "Zion",
    "Miles", "Reggie", "Aaron", "Dominic", "Eric", "Grant", "Ivan",
]
LAST_NAMES = [
    "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson", "Moore",
    "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin",
    "Thompson", "Robinson", "Clark", "Rodriguez", "Lewis", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Green", "Baker", "Adams",
    "Nelson", "Carter", "Mitchell", "Perez", "Roberts", "Turner", "Phillips",
]

POSITIONS = ["G", "F", "C"]
ROLE_WEIGHTS = [("star", 2), ("starter", 6), ("bench", 7)]  # per team

# Calibrated so a typical 13-man active roster sums to ~110-115 pts/gm
ROLE_PROFILES = {
    "star": dict(pts=(21, 5), reb=(6, 2.5), ast=(5, 2.5), stl=(1.2, 0.5),
                 blk=(0.6, 0.4), tov=(2.8, 1.0), min=(33, 4)),
    "starter": dict(pts=(9, 3), reb=(4, 2), ast=(2.3, 1.4), stl=(0.8, 0.4),
                     blk=(0.4, 0.3), tov=(1.4, 0.7), min=(24, 5)),
    "bench": dict(pts=(4, 2), reb=(2, 1.3), ast=(1, 0.9), stl=(0.5, 0.3),
                   blk=(0.2, 0.2), tov=(0.7, 0.5), min=(12, 5)),
}

_used_names = set()


def _new_name():
    while True:
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        if name not in _used_names:
            _used_names.add(name)
            return name


def build_role_list():
    roles = []
    for role, count in ROLE_WEIGHTS:
        roles.extend([role] * count)
    return roles


def generate_players(start_id=1):
    players = []
    player_id = start_id
    for team_id, *_ in TEAMS:
        for role in build_role_list():
            position = random.choice(POSITIONS)
            players.append((player_id, _new_name(), team_id, position, role))
            player_id += 1
    return players, player_id


def generate_rookies(start_id, count):
    """A handful of brand-new players, only added for the 'current'
    season — simulates a draft class that didn't exist in the prior
    season's data at all."""
    rookies = []
    player_id = start_id
    for _ in range(count):
        team_id = random.choice(TEAMS)[0]
        position = random.choice(POSITIONS)
        role = random.choice(["starter", "bench"])  # rookies rarely start as "star" role
        rookies.append((player_id, _new_name(), team_id, position, role))
        player_id += 1
    return rookies, player_id


def generate_team_defense_factors():
    """Each team gets a per-position defensive multiplier around 1.0.
    < 1.0 = suppresses that position's stats (tough defense);
    > 1.0 = gives up more than average (weak defense).
    Drawn from a realistic NBA-like spread (~15% std dev). Regenerated
    per season so a team's defensive strength can plausibly differ
    year to year, same as in reality."""
    factors = {}
    for team_id, *_ in TEAMS:
        factors[team_id] = {
            pos: max(0.65, min(1.4, random.gauss(1.0, 0.15)))
            for pos in POSITIONS
        }
    return factors


def sample_positive_int(mean, sd, lo=0):
    return max(lo, round(random.gauss(mean, sd)))


def generate_schedule(season, season_index, games_per_team=25):
    """Round-based random schedule; always terminates (even team count
    means every team gets paired each round it still needs games).
    game_id is prefixed with the season so IDs never collide across
    seasons when merged into one games.csv."""
    team_ids = [t[0] for t in TEAMS]
    counts = {tid: 0 for tid in team_ids}
    matchup_pool = []

    while min(counts.values()) < games_per_team:
        needy = [tid for tid in team_ids if counts[tid] < games_per_team]
        random.shuffle(needy)
        for i in range(0, len(needy) - 1, 2):
            home, away = needy[i], needy[i + 1]
            if random.random() < 0.5:
                home, away = away, home
            matchup_pool.append((home, away))
            counts[home] += 1
            counts[away] += 1

    season_tag = season.replace("-", "")
    games = []
    start_year = int(season.split("-")[0])
    start_month, start_day = 10, 22
    for i, (home, away) in enumerate(matchup_pool):
        game_id = f"SYN{season_tag}-{i:05d}"
        day_offset = i // 6
        month = start_month + (start_day + day_offset - 1) // 28
        day = (start_day + day_offset - 1) % 28 + 1
        year = start_year if month <= 12 else start_year + 1
        month = month if month <= 12 else month - 12
        game_date = f"{year}-{month:02d}-{day:02d}"
        games.append((game_id, game_date, season, home, away))
    return games


def simulate_game(game, players_by_team, defense_factors):
    game_id, game_date, season, home_id, away_id = game
    all_logs = []

    def play_team(team_id, opponent_id):
        team_players = players_by_team[team_id]
        opp_def = defense_factors[opponent_id]
        active = [p for p in team_players if p[4] != "bench"]
        active += [p for p in team_players if p[4] == "bench" and random.random() < 0.7]
        team_points = 0
        rows = []
        for p in active:
            player_id, name, tid, pos, role = p
            profile = ROLE_PROFILES[role]
            def_mult = opp_def[pos]  # opponent's defense vs this position
            minutes = sample_positive_int(*profile["min"], lo=4)
            pts = sample_positive_int(profile["pts"][0] * def_mult, profile["pts"][1])
            reb = sample_positive_int(profile["reb"][0] * def_mult, profile["reb"][1])
            ast = sample_positive_int(profile["ast"][0] * def_mult, profile["ast"][1])
            stl = sample_positive_int(*profile["stl"])
            blk = sample_positive_int(*profile["blk"])
            tov = sample_positive_int(*profile["tov"])
            fga = max(1, round(pts / 1.05 / 2) + random.randint(0, 4))
            fgm = min(fga, max(0, round(fga * random.uniform(0.38, 0.58))))
            three_a = round(fga * random.uniform(0.1, 0.45))
            three_m = min(three_a, max(0, round(three_a * random.uniform(0.28, 0.42))))
            fta = round(pts * random.uniform(0.05, 0.25))
            ftm = min(fta, max(0, round(fta * random.uniform(0.65, 0.9))))
            team_points += pts
            is_home = 1 if team_id == home_id else 0
            log_id = f"{game_id}_{player_id}"
            rows.append((
                log_id, game_id, game_date, season, player_id, tid, opponent_id, is_home,
                minutes, pts, reb, ast, stl, blk, tov, fgm, fga, three_m, three_a, ftm, fta
            ))
        return team_points, rows

    home_points, home_rows = play_team(home_id, away_id)
    away_points, away_rows = play_team(away_id, home_id)
    all_logs = home_rows + away_rows
    finalized_game = (game_id, game_date, season, home_id, away_id, home_points, away_points)
    return finalized_game, all_logs


def main():
    season_a, season_b = SEASONS

    base_players, next_id = generate_players(start_id=1)
    rookies, next_id = generate_rookies(next_id, ROOKIES_PER_SEASON_B)
    all_players = base_players + rookies  # players.csv holds everyone ever seen, like a real merged fetch

    players_by_team_a = {}
    for p in base_players:
        players_by_team_a.setdefault(p[2], []).append(p)

    players_by_team_b = {}
    for p in base_players + rookies:
        players_by_team_b.setdefault(p[2], []).append(p)

    all_games, all_logs = [], []
    for season_index, (season, players_by_team) in enumerate(
        [(season_a, players_by_team_a), (season_b, players_by_team_b)]
    ):
        defense_factors = generate_team_defense_factors()
        schedule = generate_schedule(season, season_index, games_per_team=25)
        for game in schedule:
            finalized_game, logs = simulate_game(game, players_by_team, defense_factors)
            all_games.append(finalized_game)
            all_logs.extend(logs)

    with open(SEED_DIR / "teams.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["team_id", "team_name", "abbreviation", "city", "conference", "division"])
        w.writerows(TEAMS)

    with open(SEED_DIR / "players.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["player_id", "full_name", "team_id", "position", "role"])
        w.writerows(all_players)

    with open(SEED_DIR / "games.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["game_id", "game_date", "season", "home_team_id", "away_team_id", "home_score", "away_score"])
        w.writerows(all_games)

    with open(SEED_DIR / "player_game_logs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "log_id", "game_id", "game_date", "season", "player_id", "team_id", "opponent_team_id",
            "is_home", "minutes", "points", "rebounds", "assists", "steals", "blocks",
            "turnovers", "fg_made", "fg_attempted", "three_made", "three_attempted",
            "ft_made", "ft_attempted",
        ])
        w.writerows(all_logs)

    print(f"Generated {len(TEAMS)} teams, {len(all_players)} players "
          f"({len(rookies)} rookies added only in {season_b}), "
          f"{len(all_games)} games across {len(SEASONS)} seasons, {len(all_logs)} box-score log rows.")
    print(f"CSVs written to {SEED_DIR}")


if __name__ == "__main__":
    main()
