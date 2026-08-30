"""
The core "is this pick good?" engine.

Given a player, a stat (points/rebounds/assists/etc.), a line, and
an opponent, this estimates the probability the player goes OVER
(or UNDER) that line, using:

  1. A recency-weighted distribution of the player's own performance
     in that stat (recent games count more than older ones).
  2. A matchup adjustment based on how well the opponent defends
     players at that position (proxy for "who's likely guarding him"
     — see README Methodology/Limitations for why this is a team-level
     proxy rather than an individual-defender lookup).
  3. A small home/away adjustment, only applied when there's enough
     sample size to trust it.

The adjusted mean/std feed a normal-distribution probability
(P(stat >= line), with a continuity correction since these are
discrete counting stats) — stdlib only, no scipy required.

This is a statistical estimate from historical patterns, not a
guarantee — see README for the full disclaimer.
"""
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "nba_props.db"

STAT_COLUMNS = {
    "PTS": "points",
    "REB": "rebounds",
    "AST": "assists",
    "STL": "steals",
    "BLK": "blocks",
    "PRA": None,  # points + rebounds + assists, handled specially
}


@dataclass
class PropResult:
    player_name: str
    stat: str
    line: float
    direction: str
    opponent_abbr: str
    sample_size: int
    recency_weighted_mean: float
    season_mean: float
    matchup_factor: float
    home_away_factor: float
    adjusted_mean: float
    adjusted_std: float
    probability: float
    historical_hit_rate: float
    confidence: str
    notes: list = field(default_factory=list)


def _connect():
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found — run scripts/load_db.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _normal_cdf(x, mean, std):
    if std <= 0:
        return 1.0 if x <= mean else 0.0
    z = (x - mean) / (std * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def _find_player(conn, name_or_id):
    if isinstance(name_or_id, int) or str(name_or_id).isdigit():
        row = conn.execute(
            "SELECT * FROM players WHERE player_id = ?", (int(name_or_id),)
        ).fetchone()
        if row:
            return row
    row = conn.execute(
        "SELECT * FROM players WHERE full_name = ?", (name_or_id,)
    ).fetchone()
    if row:
        return row
    # fuzzy fallback: case-insensitive substring match
    rows = conn.execute(
        "SELECT * FROM players WHERE full_name LIKE ? COLLATE NOCASE",
        (f"%{name_or_id}%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        names = ", ".join(r["full_name"] for r in rows)
        raise ValueError(f"Multiple players match '{name_or_id}': {names}")
    raise ValueError(f"No player found matching '{name_or_id}'")


def _find_team(conn, abbr_or_name):
    row = conn.execute(
        "SELECT * FROM teams WHERE abbreviation = ? COLLATE NOCASE", (abbr_or_name,)
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT * FROM teams WHERE team_name = ? COLLATE NOCASE", (abbr_or_name,)
    ).fetchone()
    if row:
        return row
    raise ValueError(f"No team found matching '{abbr_or_name}'")


def _stat_value(row, stat):
    if stat == "PRA":
        return row["points"] + row["rebounds"] + row["assists"]
    return row[STAT_COLUMNS[stat]]


def _recency_weighted_stats(rows, stat, half_life=8):
    """Exponential-decay weighting: a game `games_ago` back gets weight
    0.5 ** (games_ago / half_life). Returns (weighted_mean, std, n).

    The returned std is max(recency-weighted std, plain unweighted std
    over the same games). Why: exponential decay concentrates weight on
    a handful of recent games, which shrinks the EFFECTIVE sample size
    used for the variance estimate well below `n` — and a small sample
    systematically underestimates true variance, especially over a
    short recent stretch that can look artificially consistent (a
    "hot streak" reads as low volatility even though the player's real
    game-to-game swings, from blowouts, back-to-backs, minutes
    changes, etc., are wider). Backtesting against real NBA data
    (see scripts/backtest.py) showed this made the live model
    overconfident — high-confidence picks were hitting well below
    their stated probability — so we floor the std at the plain
    full-window sample std, which doesn't have that shrinkage problem,
    to keep the recency-weighted MEAN (still useful for trend) without
    inheriting an artificially tight spread."""
    values, weights = [], []
    for r in rows:
        v = _stat_value(r, stat)
        w = 0.5 ** ((r["games_ago"] - 1) / half_life)
        values.append(v)
        weights.append(w)

    total_w = sum(weights)
    if total_w == 0 or not values:
        return 0.0, 0.0, 0

    weighted_mean = sum(v * w for v, w in zip(values, weights)) / total_w
    weighted_var = sum(w * (v - weighted_mean) ** 2 for v, w in zip(values, weights)) / total_w
    weighted_std = math.sqrt(weighted_var)

    plain_mean = sum(values) / len(values)
    plain_var = sum((v - plain_mean) ** 2 for v in values) / len(values)
    plain_std = math.sqrt(plain_var)

    return weighted_mean, max(weighted_std, plain_std), len(values)


def _matchup_factor(conn, position, stat, opponent_team_id):
    """Ratio of what the opponent allows to this position (for this
    stat) vs. the league-average allowed to that position. 1.0 = league
    average defense, >1 = defense is weak at this stat/position (good
    for the player), <1 = defense is tough (bad for the player)."""
    if stat == "PRA":
        # Approximate PRA defense as the sum of the three component factors
        factors = [_matchup_factor(conn, position, s, opponent_team_id) for s in ("PTS", "REB", "AST")]
        return sum(factors) / len(factors)

    col_map = {"PTS": "avg_points_allowed", "REB": "avg_rebounds_allowed", "AST": "avg_assists_allowed"}
    col = col_map.get(stat)
    if col is None:
        return 1.0  # STL/BLK not tracked positionally in this schema; neutral factor

    league_row = conn.execute(
        f"SELECT AVG({col}) AS league_avg FROM v_team_position_defense WHERE position = ?",
        (position,),
    ).fetchone()
    opp_row = conn.execute(
        f"SELECT {col} AS opp_val FROM v_team_position_defense WHERE position = ? AND team_id = ?",
        (position, opponent_team_id),
    ).fetchone()

    if not league_row or not opp_row or not league_row["league_avg"] or not opp_row["opp_val"]:
        return 1.0

    factor = opp_row["opp_val"] / league_row["league_avg"]
    # Clip to avoid one small-sample outlier team blowing up the estimate
    return max(0.75, min(1.25, factor))


def _home_away_factor(conn, player_id, stat, is_home):
    if is_home is None:
        return 1.0, "home/away not specified — no adjustment applied"

    col = STAT_COLUMNS.get(stat)
    if col is None:  # PRA
        rows = conn.execute(
            "SELECT is_home, points, rebounds, assists FROM player_game_logs WHERE player_id = ?",
            (player_id,),
        ).fetchall()
        overall = [r["points"] + r["rebounds"] + r["assists"] for r in rows]
        split = [r["points"] + r["rebounds"] + r["assists"] for r in rows if r["is_home"] == is_home]
    else:
        rows = conn.execute(
            f"SELECT is_home, {col} AS val FROM player_game_logs WHERE player_id = ?",
            (player_id,),
        ).fetchall()
        overall = [r["val"] for r in rows]
        split = [r["val"] for r in rows if r["is_home"] == is_home]

    if len(split) < 5 or not overall:
        return 1.0, f"insufficient {'home' if is_home else 'away'} sample — no adjustment applied"

    overall_avg = sum(overall) / len(overall)
    split_avg = sum(split) / len(split)
    if overall_avg == 0:
        return 1.0, "no adjustment applied"

    raw_factor = split_avg / overall_avg
    # Dampen: blend 70% toward neutral to avoid overreacting to small splits
    factor = 1 + 0.3 * (raw_factor - 1)
    factor = max(0.85, min(1.15, factor))
    label = "home" if is_home else "away"
    return factor, f"{label} split factor {factor:.3f} from {len(split)} {label} games"


def estimate_prop_probability(
    player_name, stat, line, opponent_abbr, direction="over",
    is_home=None, recent_n=20, half_life=8, conn=None,
):
    stat = stat.upper()
    direction = direction.lower()
    if stat not in STAT_COLUMNS:
        raise ValueError(f"Unsupported stat '{stat}'. Choose from {list(STAT_COLUMNS)}.")
    if direction not in ("over", "under"):
        raise ValueError("direction must be 'over' or 'under'")

    own_conn = conn is None
    conn = conn or _connect()
    try:
        player = _find_player(conn, player_name)
        opponent = _find_team(conn, opponent_abbr)

        rows = conn.execute(
            """
            SELECT * FROM v_player_rolling_stats
            WHERE player_id = ? AND games_ago <= ?
            ORDER BY games_ago
            """,
            (player["player_id"], recent_n),
        ).fetchall()

        if not rows:
            raise ValueError(f"No game log data for {player['full_name']} — has data been loaded?")

        weighted_mean, weighted_std, n = _recency_weighted_stats(rows, stat, half_life)
        season_mean = sum(_stat_value(r, stat) for r in rows) / len(rows)

        # Floor the std so a hyper-consistent small sample doesn't produce
        # an unrealistically overconfident (near 0%/100%) probability.
        floor_std = max(1.5, 0.25 * weighted_mean)
        eff_std = max(weighted_std, floor_std)

        matchup_factor = _matchup_factor(conn, player["position"], stat, opponent["team_id"])
        home_away_factor, ha_note = _home_away_factor(conn, player["player_id"], stat, is_home)

        adjusted_mean = weighted_mean * matchup_factor * home_away_factor
        adjusted_std = eff_std

        # Continuity correction: P(X >= line) for a discrete stat
        threshold = line - 0.5
        p_over = 1 - _normal_cdf(threshold, adjusted_mean, adjusted_std)
        probability = p_over if direction == "over" else 1 - p_over

        hits = sum(1 for r in rows if (_stat_value(r, stat) >= line) == (direction == "over"))
        historical_hit_rate = hits / len(rows)

        if n >= 15:
            confidence = "high"
        elif n >= 8:
            confidence = "medium"
        else:
            confidence = "low (small sample)"

        notes = [ha_note]
        if matchup_factor > 1.05:
            notes.append(f"{opponent['abbreviation']} defends {player['position']} below league average (factor {matchup_factor:.3f}) — favorable matchup")
        elif matchup_factor < 0.95:
            notes.append(f"{opponent['abbreviation']} defends {player['position']} above league average (factor {matchup_factor:.3f}) — tough matchup")
        else:
            notes.append(f"{opponent['abbreviation']} defends {player['position']} near league average (factor {matchup_factor:.3f})")

        return PropResult(
            player_name=player["full_name"],
            stat=stat,
            line=line,
            direction=direction,
            opponent_abbr=opponent["abbreviation"],
            sample_size=n,
            recency_weighted_mean=round(weighted_mean, 2),
            season_mean=round(season_mean, 2),
            matchup_factor=round(matchup_factor, 3),
            home_away_factor=round(home_away_factor, 3),
            adjusted_mean=round(adjusted_mean, 2),
            adjusted_std=round(adjusted_std, 2),
            probability=round(probability, 4),
            historical_hit_rate=round(historical_hit_rate, 3),
            confidence=confidence,
            notes=notes,
        )
    finally:
        if own_conn:
            conn.close()


def combine_probabilities(results):
    """Naive combined probability for a multi-leg parlay, ASSUMING
    INDEPENDENCE. Real player props are often positively correlated
    (e.g., a big scoring night often comes with more shot attempts,
    sometimes fewer assists) — this is a simplification. See README."""
    combined = 1.0
    for r in results:
        combined *= r.probability
    return round(combined, 4)


if __name__ == "__main__":
    result = estimate_prop_probability("Marcus Johnson", "PTS", 20, "BOS", direction="over")
    print(result)
