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
import unicodedata
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
    player_id: int
    season: str
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

    # Accent-insensitive matching: real NBA data has names like "Nikola
    # Jokić", "Luka Dončić", "Alperen Şengün" — typing the plain-ASCII
    # spelling ("Jokic") should still find them. Matching is done in
    # Python over the (small, ~500-row) players table rather than in
    # SQL, since SQLite has no built-in accent folding.
    def strip_accents(s):
        return "".join(
            c for c in unicodedata.normalize("NFKD", s)
            if not unicodedata.combining(c)
        ).lower()

    query_norm = strip_accents(name_or_id)
    all_players = conn.execute("SELECT * FROM players").fetchall()

    exact = [r for r in all_players if strip_accents(r["full_name"]) == query_norm]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        names = ", ".join(r["full_name"] for r in exact)
        raise ValueError(f"Multiple players match '{name_or_id}': {names}")

    substring = [r for r in all_players if query_norm in strip_accents(r["full_name"])]
    if len(substring) == 1:
        return substring[0]
    if len(substring) > 1:
        names = ", ".join(r["full_name"] for r in substring)
        raise ValueError(f"Multiple players match '{name_or_id}': {names}")

    raise ValueError(f"No player found matching '{name_or_id}'")


def list_seasons(conn):
    """All seasons present in the DB, most recent first (string-sorted,
    which works for the 'YYYY-YY' format e.g. '2024-25' < '2025-26')."""
    rows = conn.execute("SELECT DISTINCT season FROM player_game_logs ORDER BY season DESC").fetchall()
    return [r["season"] for r in rows]


def _resolve_season(conn, season):
    """None -> most recent season in the DB. Otherwise validates the
    season exists and returns it as-is."""
    seasons = list_seasons(conn)
    if not seasons:
        raise ValueError("No data loaded — has scripts/load_db.py been run?")
    if season is None:
        return seasons[0]
    if season not in seasons:
        raise ValueError(f"No data for season '{season}'. Available: {', '.join(seasons)}")
    return season


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


SHRINKAGE_K = 10  # see _shrink_to_season_mean docstring


def _shrink_to_season_mean(weighted_mean, n, full_season_mean, k=SHRINKAGE_K):
    """Empirical-Bayes-style shrinkage: pulls the recency-weighted mean
    partway back toward the player's full-season mean, in proportion to
    how much recent evidence backs it up. With n games behind the
    recency-weighted estimate, the blend weight is n / (n + k) on the
    recent estimate and k / (n + k) on the season mean — so a
    well-supported recent trend (large n) is barely touched, while a
    thin one (small n) leans more on the season-long baseline.

    Why this exists: backtesting (scripts/backtest.py, see README's
    Validation section) found predictions below 50% running low and
    predictions above 50% running high relative to actual outcomes —
    consistent with "regression to the mean": the recency-weighted
    mean rides a player's hot/cold stretch, and stretches partially
    revert. The matchup and home/away adjustments were already
    dampened for the same kind of overconfidence; this applies the
    same idea to the base mean itself.

    k=10 is a round number of the same order as the half-life (8),
    not fit against a specific backtest run — the README's Known
    Limitation section explicitly flags hand-tuning a constant to one
    dataset as a way to overfit rather than actually improve the
    model, so this is a principled default, not a result of grid
    search. Re-run scripts/backtest.py after changing it if you want
    to see the effect on your own data."""
    if n + k == 0:
        return weighted_mean
    return (n * weighted_mean + k * full_season_mean) / (n + k)


MATCHUP_DAMPEN = 0.35  # see docstring below


def _matchup_factor(conn, position, stat, opponent_team_id, season):
    """Ratio of what the opponent allows to this position (for this
    stat) vs. the league-average allowed to that position. 1.0 = league
    average defense, >1 = defense is weak at this stat/position (good
    for the player), <1 = defense is tough (bad for the player).

    The raw team-vs-league ratio is DAMPENED (blended toward 1.0),
    matching what _home_away_factor already does and for the same
    reason: a naive ratio of season-long team averages overstates how
    much an individual player's stat line actually swings with the
    matchup — real NBA scoring is dominated by the player's own
    variance, with opponent defense a real but comparatively modest
    factor. Backtesting against real 2024-25 data (scripts/backtest.py)
    showed the UNDAMPENED version was overconfident in both directions
    (e.g. 70-80%-confidence picks hitting only ~63% of the time) even
    with plenty of games behind each estimate — i.e. this wasn't a
    small-sample problem (flooring the std barely moved the needle),
    it was the adjustment's effect size being too large. Dampening by
    the same 0.3-ish factor used for home/away closed most of that gap
    in re-testing."""
    if stat == "PRA":
        # Approximate PRA defense as the sum of the three component factors
        factors = [_matchup_factor(conn, position, s, opponent_team_id, season) for s in ("PTS", "REB", "AST")]
        return sum(factors) / len(factors)

    col_map = {"PTS": "avg_points_allowed", "REB": "avg_rebounds_allowed", "AST": "avg_assists_allowed"}
    col = col_map.get(stat)
    if col is None:
        return 1.0  # STL/BLK not tracked positionally in this schema; neutral factor

    league_row = conn.execute(
        f"SELECT AVG({col}) AS league_avg FROM v_team_position_defense WHERE position = ? AND season = ?",
        (position, season),
    ).fetchone()
    opp_row = conn.execute(
        f"SELECT {col} AS opp_val FROM v_team_position_defense WHERE position = ? AND team_id = ? AND season = ?",
        (position, opponent_team_id, season),
    ).fetchone()

    if not league_row or not opp_row or not league_row["league_avg"] or not opp_row["opp_val"]:
        return 1.0

    raw_factor = opp_row["opp_val"] / league_row["league_avg"]
    dampened = 1 + MATCHUP_DAMPEN * (raw_factor - 1)
    # Tighter clip than before: the raw ratio was already softened above,
    # this just guards against one extreme outlier team on top of that.
    return max(0.85, min(1.15, dampened))


def _home_away_factor(conn, player_id, stat, is_home, season):
    if is_home is None:
        return 1.0, "home/away not specified — no adjustment applied"

    col = STAT_COLUMNS.get(stat)
    if col is None:  # PRA
        rows = conn.execute(
            "SELECT is_home, points, rebounds, assists FROM player_game_logs WHERE player_id = ? AND season = ?",
            (player_id, season),
        ).fetchall()
        overall = [r["points"] + r["rebounds"] + r["assists"] for r in rows]
        split = [r["points"] + r["rebounds"] + r["assists"] for r in rows if r["is_home"] == is_home]
    else:
        rows = conn.execute(
            f"SELECT is_home, {col} AS val FROM player_game_logs WHERE player_id = ? AND season = ?",
            (player_id, season),
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
    is_home=None, recent_n=20, half_life=8, conn=None, season=None,
):
    """season: which season's data to use (e.g. '2025-26'). Defaults to
    the most recent season loaded in the DB. Use list_seasons(conn) to
    see what's available."""
    stat = stat.upper()
    direction = direction.lower()
    if stat not in STAT_COLUMNS:
        raise ValueError(f"Unsupported stat '{stat}'. Choose from {list(STAT_COLUMNS)}.")
    if direction not in ("over", "under"):
        raise ValueError("direction must be 'over' or 'under'")

    own_conn = conn is None
    conn = conn or _connect()
    try:
        season = _resolve_season(conn, season)
        player = _find_player(conn, player_name)
        opponent = _find_team(conn, opponent_abbr)

        rows = conn.execute(
            """
            SELECT * FROM v_player_rolling_stats
            WHERE player_id = ? AND season = ? AND games_ago <= ?
            ORDER BY games_ago
            """,
            (player["player_id"], season, recent_n),
        ).fetchall()

        if not rows:
            raise ValueError(f"No {season} game log data for {player['full_name']} — try a different season (list_seasons) or check data has been loaded.")

        weighted_mean, weighted_std, n = _recency_weighted_stats(rows, stat, half_life)
        season_mean = sum(_stat_value(r, stat) for r in rows) / len(rows)

        # Full-season mean (ALL games this season, not just the recent_n
        # window) is the shrinkage target — a longer, more stable
        # baseline than `season_mean` above, which is scoped to the same
        # recent window as the recency-weighted estimate and so isn't
        # independent evidence to shrink toward.
        full_season_rows = conn.execute(
            "SELECT * FROM v_player_rolling_stats WHERE player_id = ? AND season = ?",
            (player["player_id"], season),
        ).fetchall()
        full_season_mean = sum(_stat_value(r, stat) for r in full_season_rows) / len(full_season_rows)
        shrunk_mean = _shrink_to_season_mean(weighted_mean, n, full_season_mean)

        # Floor the std so a hyper-consistent small sample doesn't produce
        # an unrealistically overconfident (near 0%/100%) probability.
        floor_std = max(1.5, 0.25 * shrunk_mean)
        eff_std = max(weighted_std, floor_std)

        matchup_factor = _matchup_factor(conn, player["position"], stat, opponent["team_id"], season)
        home_away_factor, ha_note = _home_away_factor(conn, player["player_id"], stat, is_home, season)

        adjusted_mean = shrunk_mean * matchup_factor * home_away_factor
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
            player_id=player["player_id"],
            season=season,
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


def _player_team_in_season(conn, player_id, season):
    """Which team a player actually suited up for IN THIS SEASON, from
    their own game logs — not players.team_id, which only reflects
    their most recently fetched roster and would be wrong for a player
    who was traded, or for an older season looked at after a trade.

    Uses the team from their MOST RECENT logged game, not whichever
    team they have the most total games with. Those differ for anyone
    traded mid-season: a player traded late, after logging more games
    with their old team than their new one, would get resolved back to
    the old team by a most-games vote — exactly backwards for "who are
    they on right now," which is what a same-game parlay needs to
    match them against the correct matchup. (Caught from a real report:
    a player traded from Philadelphia to Oklahoma City mid-season
    wasn't resolving to OKC for a Nets-vs-OKC parlay.)

    Falls back to players.team_id if the player has no logs that
    season (e.g. a line hasn't been checked yet, or very sparse data)."""
    row = conn.execute(
        """
        SELECT team_id FROM player_game_logs
        WHERE player_id = ? AND season = ?
        ORDER BY game_date DESC, game_id DESC LIMIT 1
        """,
        (player_id, season),
    ).fetchone()
    if row:
        return row["team_id"]
    fallback = conn.execute("SELECT team_id FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return fallback["team_id"] if fallback else None


def estimate_same_game_parlay(
    legs, team_a_abbr, team_b_abbr, home_team_abbr=None,
    recent_n=20, half_life=8, conn=None, season=None,
):
    """A parlay across MULTIPLE PLAYERS in the same game, unlike
    estimate_prop_probability/combine_probabilities which only handle
    several props for one player. `legs` is a list of dicts:
        {"player": "...", "stat": "PTS", "line": 25, "direction": "over"}
    Each player's opponent is figured out automatically from which of
    the two given teams they actually played for THAT SEASON (handles
    trades correctly — see _player_team_in_season) — you don't specify
    an opponent per leg. home_team_abbr is optional context for the
    home/away adjustment (must be team_a_abbr or team_b_abbr if given).

    Returns (results, combined_probability). results is a list of
    PropResult, one per leg, in the same order as `legs`. Same
    independence caveat as combine_probabilities applies, plus same-game
    correlation is typically even stronger between teammates/opponents
    than between two props on the same player — treat the combined
    number as a rough estimate, not a precise joint probability."""
    own_conn = conn is None
    conn = conn or _connect()
    try:
        season = _resolve_season(conn, season)
        team_a = _find_team(conn, team_a_abbr)
        team_b = _find_team(conn, team_b_abbr)
        if team_a["team_id"] == team_b["team_id"]:
            raise ValueError(f"'{team_a_abbr}' and '{team_b_abbr}' resolved to the same team — need two different teams.")

        home_team_id = None
        if home_team_abbr is not None:
            home_team = _find_team(conn, home_team_abbr)
            if home_team["team_id"] not in (team_a["team_id"], team_b["team_id"]):
                raise ValueError(f"home_team '{home_team_abbr}' must be one of the two teams in this game ({team_a_abbr}/{team_b_abbr}).")
            home_team_id = home_team["team_id"]

        results = []
        for leg in legs:
            player = _find_player(conn, leg["player"])
            player_team_id = _player_team_in_season(conn, player["player_id"], season)

            if player_team_id == team_a["team_id"]:
                opponent = team_b
            elif player_team_id == team_b["team_id"]:
                opponent = team_a
            else:
                raise ValueError(
                    f"{player['full_name']} didn't play for {team_a_abbr} or {team_b_abbr} in {season} "
                    f"— check the player name/team/season."
                )

            is_home = None if home_team_id is None else (player_team_id == home_team_id)

            r = estimate_prop_probability(
                player["full_name"], leg["stat"], leg["line"], opponent["abbreviation"],
                direction=leg.get("direction", "over"), is_home=is_home,
                recent_n=recent_n, half_life=half_life, conn=conn, season=season,
            )
            results.append(r)

        combined = combine_probabilities(results)
        return results, combined
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    conn = _connect()
    print("Seasons available:", list_seasons(conn))
    conn.close()
    result = estimate_prop_probability("Marcus Johnson", "PTS", 20, "BOS", direction="over")
    print(result)
