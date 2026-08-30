"""
Command-line interface for grading player props.

Examples:
    # Single prop
    python scripts/cli.py --player "Shai Gilgeous-Alexander" --stat PTS --line 30 --opponent DEN

    # Multiple props for the same player/game (treated as a parlay)
    python scripts/cli.py --player "Shai Gilgeous-Alexander" --opponent DEN \\
        --prop PTS:30 --prop AST:4

    # Under bets, home/away context
    python scripts/cli.py --player "Anthony Edwards" --stat REB --line 6 \\
        --opponent BOS --direction under --home
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prop_model import estimate_prop_probability, combine_probabilities, _connect  # noqa: E402


def parse_prop(spec):
    """Parse a 'STAT:LINE' spec, e.g. 'PTS:30' or 'AST:4.5'."""
    try:
        stat, line = spec.split(":")
        return stat.upper(), float(line)
    except ValueError:
        raise SystemExit(f"Invalid --prop '{spec}'. Expected format STAT:LINE, e.g. PTS:30")


def print_result(r):
    # r.probability is already P(the stated over/under condition is true),
    # so this just reflects whether that's more likely than not — it does
    # NOT need to re-check r.direction.
    verdict = "LIKELY HIT" if r.probability >= 0.5 else "LIKELY MISS"
    print(f"\n{r.player_name} — {r.stat} {r.direction.upper()} {r.line} vs {r.opponent_abbr}")
    print("-" * 60)
    print(f"  Probability of hitting:     {r.probability:6.1%}   [{verdict}]")
    print(f"  Confidence:                 {r.confidence}")
    print(f"  Recency-weighted average:   {r.recency_weighted_mean}")
    print(f"  Season average:             {r.season_mean}")
    print(f"  Matchup factor:             {r.matchup_factor}  (1.0 = league avg)")
    print(f"  Home/away factor:           {r.home_away_factor}")
    print(f"  Matchup-adjusted mean:      {r.adjusted_mean}  (± {r.adjusted_std} std)")
    print(f"  Raw historical hit rate:    {r.historical_hit_rate:.1%}  (last {r.sample_size} games)")
    for note in r.notes:
        print(f"  note: {note}")


def main():
    parser = argparse.ArgumentParser(description="Grade NBA player props against matchup-adjusted historical data.")
    parser.add_argument("--player", required=True, help="Player full name (or unique substring)")
    parser.add_argument("--opponent", required=True, help="Opponent team abbreviation, e.g. DEN")
    parser.add_argument("--direction", default="over", choices=["over", "under"])
    parser.add_argument("--home", action="store_true", help="Player's team is playing at home")
    parser.add_argument("--away", action="store_true", help="Player's team is playing away")
    parser.add_argument("--recent-n", type=int, default=20, help="How many recent games to weight (default 20)")
    parser.add_argument("--stat", help="Single-prop mode: stat name (PTS, REB, AST, STL, BLK, PRA)")
    parser.add_argument("--line", type=float, help="Single-prop mode: the line, e.g. 30")
    parser.add_argument("--prop", action="append", default=[], help="Multi-prop mode: STAT:LINE, repeatable")

    args = parser.parse_args()
    if args.home and args.away:
        raise SystemExit("--home and --away are mutually exclusive")
    is_home = True if args.home else (False if args.away else None)

    props = list(args.prop)
    if args.stat and args.line is not None:
        props.append(f"{args.stat}:{args.line}")
    if not props:
        raise SystemExit("Provide either --stat/--line or one or more --prop STAT:LINE")

    conn = _connect()
    results = []
    try:
        for spec in props:
            stat, line = parse_prop(spec)
            r = estimate_prop_probability(
                args.player, stat, line, args.opponent,
                direction=args.direction, is_home=is_home,
                recent_n=args.recent_n, conn=conn,
            )
            results.append(r)
            print_result(r)

        if len(results) > 1:
            combined = combine_probabilities(results)
            print("\n" + "=" * 60)
            print(f"Combined probability (all legs hit, ASSUMING INDEPENDENCE): {combined:.1%}")
            print("Note: real stats are often correlated — treat this as a rough")
            print("upper-bound-ish estimate, not a precise joint probability.")
    except ValueError as e:
        raise SystemExit(f"Error: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
