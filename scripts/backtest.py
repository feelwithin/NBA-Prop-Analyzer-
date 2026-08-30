"""
Backtest the prop model's CALIBRATION: when it says a pick has a 70%
chance of hitting, does it actually hit about 70% of the time across
many picks?

How it works (point-in-time, no lookahead):
    We walk through every game in the loaded season in chronological
    order. Before evaluating any game on a given date, the model only
    knows about games *strictly before* that date — both the player's
    own trailing performance and every team's position-defense stats
    are computed from history-so-far, never from the future. This
    avoids the classic backtesting mistake of "predicting" the past
    using information that didn't exist yet.

    Since we don't have access to real historical betting lines, each
    test case uses the player's own trailing average (rounded) as a
    synthetic "fair line," with over/under picked at random (seeded,
    reproducible). This tests whether the model's probability
    estimates are well-calibrated in general — not whether a specific
    sportsbook's lines are beatable, which would need real odds data.

Usage:
    python scripts/backtest.py
    python scripts/backtest.py --min-prior-games 15 --min-defense-games 20
"""
import argparse
import csv
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prop_model import _normal_cdf, DB_PATH  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "output"

STATS = ["points", "rebounds", "assists"]  # + PRA derived
HALF_LIFE = 8
RECENT_N = 20


def _weighted_mean_std(history, key, half_life=HALF_LIFE, recent_n=RECENT_N):
    """history: list of dicts, most-recent-last. Uses up to the last
    `recent_n` entries, weighting recency the same way prop_model does.
    Mirrors prop_model._recency_weighted_stats: std is floored at the
    plain (unweighted) sample std over the same window, since the
    recency-weighted estimate has a small effective sample size and
    tends to understate real variance — see that function's docstring."""
    recent = history[-recent_n:]
    n = len(recent)
    values, weights = [], []
    for i, h in enumerate(reversed(recent)):  # i=0 is most recent
        values.append(h[key])
        weights.append(0.5 ** (i / half_life))
    total_w = sum(weights)
    if total_w == 0:
        return 0.0, 0.0, 0
    mean = sum(v * w for v, w in zip(values, weights)) / total_w
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / total_w
    weighted_std = math.sqrt(var)

    plain_mean = sum(values) / len(values)
    plain_var = sum((v - plain_mean) ** 2 for v in values) / len(values)
    plain_std = math.sqrt(plain_var)

    return mean, max(weighted_std, plain_std), n


def _defense_factor(team_position_stats, position, team_id, key):
    """team_position_stats[(team_id, position)] = {'sum': {...}, 'count': n}
    Returns (factor, min_count_across_league) — factor clipped like prop_model."""
    team_entry = team_position_stats.get((team_id, position))
    if not team_entry or team_entry["count"] == 0:
        return 1.0, 0

    league_sum, league_count = 0.0, 0
    for (tid, pos), entry in team_position_stats.items():
        if pos == position:
            league_sum += entry["sum"][key]
            league_count += entry["count"]
    if league_count == 0:
        return 1.0, 0

    league_avg = league_sum / league_count
    team_avg = team_entry["sum"][key] / team_entry["count"]
    if league_avg == 0:
        return 1.0, team_entry["count"]
    factor = max(0.75, min(1.25, team_avg / league_avg))
    return factor, team_entry["count"]


def _home_away_factor(history, is_home, min_split=5):
    split = [h for h in history if h["is_home"] == is_home]
    if len(split) < min_split or not history:
        return {k: 1.0 for k in STATS}
    overall_avg = {k: sum(h[k] for h in history) / len(history) for k in STATS}
    split_avg = {k: sum(h[k] for h in split) / len(split) for k in STATS}
    # Return a per-stat dict; caller picks the relevant key
    factors = {}
    for k in STATS:
        if overall_avg[k] == 0:
            factors[k] = 1.0
            continue
        raw = split_avg[k] / overall_avg[k]
        factors[k] = max(0.85, min(1.15, 1 + 0.3 * (raw - 1)))
    return factors


def run_backtest(min_prior_games, min_defense_games, seed):
    rng = random.Random(seed)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT pgl.*, p.position
        FROM player_game_logs pgl
        JOIN players p ON p.player_id = pgl.player_id
        ORDER BY pgl.game_date, pgl.game_id
        """
    ).fetchall()
    conn.close()

    if not rows:
        raise SystemExit("No game log data found — run load_db.py first.")

    # Group by date so we predict on a whole date using only prior dates' state
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["game_date"]].append(r)

    player_history = defaultdict(list)  # player_id -> list of dicts, chronological
    team_position_stats = defaultdict(lambda: {"sum": {k: 0.0 for k in STATS}, "count": 0})

    results = []  # each: dict with predicted_prob, hit (0/1), stat, line, direction

    for date in sorted(by_date.keys()):
        # ---- PREDICT using state as of before `date` ----
        for r in by_date[date]:
            pid = r["player_id"]
            history = player_history[pid]
            if len(history) < min_prior_games:
                continue

            for stat in STATS + ["pra"]:
                if stat == "pra":
                    weighted_mean, weighted_std, n = _weighted_mean_pra(history)
                else:
                    weighted_mean, weighted_std, n = _weighted_mean_std(history, stat)

                factor, def_n = _defense_factor(
                    team_position_stats, r["position"], r["opponent_team_id"],
                    "points" if stat == "pra" else stat,
                )
                if def_n < min_defense_games:
                    continue  # not enough defensive sample yet to trust the adjustment

                ha_factors = _home_away_factor(history, r["is_home"])
                ha_factor = 1.0 if stat == "pra" else ha_factors.get(stat, 1.0)

                adjusted_mean = weighted_mean * factor * ha_factor
                floor_std = max(1.5, 0.25 * weighted_mean)
                adjusted_std = max(weighted_std, floor_std)

                line = round(weighted_mean)
                if line <= 0:
                    continue
                direction = "over" if rng.random() < 0.5 else "under"

                threshold = line - 0.5
                p_over = 1 - _normal_cdf(threshold, adjusted_mean, adjusted_std)
                predicted_prob = p_over if direction == "over" else 1 - p_over

                actual = r["points"] + r["rebounds"] + r["assists"] if stat == "pra" else r[stat]
                hit = 1 if (actual >= line) == (direction == "over") else 0

                results.append({
                    "player_id": pid, "game_date": date, "stat": stat.upper(),
                    "line": line, "direction": direction,
                    "predicted_prob": round(predicted_prob, 4), "hit": hit,
                })

        # ---- UPDATE state with this date's actual results ----
        for r in by_date[date]:
            pid = r["player_id"]
            player_history[pid].append({
                "points": r["points"], "rebounds": r["rebounds"], "assists": r["assists"],
                "is_home": r["is_home"],
            })
            key = (r["opponent_team_id"], r["position"])
            entry = team_position_stats[key]
            entry["sum"]["points"] += r["points"]
            entry["sum"]["rebounds"] += r["rebounds"]
            entry["sum"]["assists"] += r["assists"]
            entry["count"] += 1

    return results


def _weighted_mean_pra(history, half_life=HALF_LIFE, recent_n=RECENT_N):
    """Same std-flooring logic as _weighted_mean_std — see its docstring."""
    recent = history[-recent_n:]
    values, weights = [], []
    for i, h in enumerate(reversed(recent)):
        values.append(h["points"] + h["rebounds"] + h["assists"])
        weights.append(0.5 ** (i / half_life))
    total_w = sum(weights)
    if total_w == 0:
        return 0.0, 0.0, 0
    mean = sum(v * w for v, w in zip(values, weights)) / total_w
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / total_w
    weighted_std = math.sqrt(var)

    plain_mean = sum(values) / len(values)
    plain_var = sum((v - plain_mean) ** 2 for v in values) / len(values)
    plain_std = math.sqrt(plain_var)

    return mean, max(weighted_std, plain_std), len(recent)


def summarize(results):
    n = len(results)
    if n == 0:
        print("No test cases generated — try lowering --min-prior-games / --min-defense-games.")
        return

    brier = sum((r["predicted_prob"] - r["hit"]) ** 2 for r in results) / n
    baseline_brier = sum((0.5 - r["hit"]) ** 2 for r in results) / n
    accuracy = sum(1 for r in results if (r["predicted_prob"] >= 0.5) == bool(r["hit"])) / n

    print(f"\nBacktest summary ({n} test cases)")
    print("-" * 60)
    print(f"  Brier score (lower is better):        {brier:.4f}")
    print(f"  Baseline Brier (always predict 50%):  {baseline_brier:.4f}")
    print(f"  Directional accuracy:                 {accuracy:.1%}")

    # Calibration table: bucket by predicted probability, compare to actual hit rate
    buckets = defaultdict(list)
    for r in results:
        bucket = min(int(r["predicted_prob"] * 10), 9)  # 0-9
        buckets[bucket].append(r["hit"])

    print(f"\n  {'Predicted range':<18}{'N':>6}{'Avg predicted':>16}{'Actual hit rate':>18}")
    for b in sorted(buckets):
        hits = buckets[b]
        lo, hi = b / 10, (b + 1) / 10
        avg_pred = sum(
            r["predicted_prob"] for r in results
            if min(int(r["predicted_prob"] * 10), 9) == b
        ) / len(hits)
        actual_rate = sum(hits) / len(hits)
        print(f"  {lo:.0%}-{hi:.0%}{'':<10}{len(hits):>6}{avg_pred:>16.1%}{actual_rate:>18.1%}")

    print("\n  Well-calibrated means each row's 'avg predicted' and 'actual hit")
    print("  rate' columns are close — e.g. among all picks predicted ~70%,")
    print("  roughly 70% should actually hit.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-prior-games", type=int, default=10,
                         help="minimum games of history a player needs before being tested")
    parser.add_argument("--min-defense-games", type=int, default=15,
                         help="minimum sample size before trusting a team's position-defense stat")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    results = run_backtest(args.min_prior_games, args.min_defense_games, args.seed)
    summarize(results)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "backtest_results.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["player_id", "game_date", "stat", "line", "direction", "predicted_prob", "hit"])
        for r in results:
            w.writerow([r["player_id"], r["game_date"], r["stat"], r["line"], r["direction"], r["predicted_prob"], r["hit"]])
    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
