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

    # Same-game parlay across MULTIPLE PLAYERS (either team) in one game —
    # each player's opponent is figured out automatically from --team-a/--team-b
    python scripts/cli.py --team-a DEN --team-b BOS --home-team BOS \\
        --leg "Shai Gilgeous-Alexander:PTS:30" \\
        --leg "Jaylen Brown:PTS:20:under" \\
        --leg "Nikola Jokic:PRA:45"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prop_model import (  # noqa: E402
    estimate_prop_probability, combine_probabilities, estimate_same_game_parlay,
    _connect, list_seasons,
)


def parse_prop(spec):
    """Parse a 'STAT:LINE' spec, e.g. 'PTS:30' or 'AST:4.5'."""
    try:
        stat, line = spec.split(":")
        return stat.upper(), float(line)
    except ValueError:
        raise SystemExit(f"Invalid --prop '{spec}'. Expected format STAT:LINE, e.g. PTS:30")


def parse_leg(spec):
    """Parse a '--leg' spec for a same-game parlay: 'PLAYER:STAT:LINE' or
    'PLAYER:STAT:LINE:DIRECTION' (direction defaults to over), e.g.
    'Shai Gilgeous-Alexander:PTS:30' or 'Jaylen Brown:PTS:20:under'."""
    parts = spec.split(":")
    if len(parts) not in (3, 4):
        raise SystemExit(
            f"Invalid --leg '{spec}'. Expected PLAYER:STAT:LINE or PLAYER:STAT:LINE:DIRECTION, "
            f"e.g. \"Shai Gilgeous-Alexander:PTS:30\""
        )
    player, stat, line = parts[0], parts[1], parts[2]
    direction = parts[3] if len(parts) == 4 else "over"
    try:
        line = float(line)
    except ValueError:
        raise SystemExit(f"Invalid --leg '{spec}': line '{line}' is not a number.")
    if direction.lower() not in ("over", "under"):
        raise SystemExit(f"Invalid --leg '{spec}': direction must be 'over' or 'under', got '{direction}'.")
    return {"player": player, "stat": stat.upper(), "line": line, "direction": direction.lower()}


def print_result(r):
    # r.probability is already P(the stated over/under condition is true),
    # so this just reflects whether that's more likely than not — it does
    # NOT need to re-check r.direction.
    verdict = "LIKELY HIT" if r.probability >= 0.5 else "LIKELY MISS"
    print(f"\n{r.player_name} ({r.season}) — {r.stat} {r.direction.upper()} {r.line} vs {r.opponent_abbr}")
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
    parser.add_argument("--player", help="Player full name (or unique substring)")
    parser.add_argument("--opponent", help="Opponent team abbreviation, e.g. DEN")
    parser.add_argument("--direction", default="over", choices=["over", "under"])
    parser.add_argument("--home", action="store_true", help="Player's team is playing at home")
    parser.add_argument("--away", action="store_true", help="Player's team is playing away")
    parser.add_argument("--recent-n", type=int, default=20, help="How many recent games to weight (default 20)")
    parser.add_argument("--season", help="e.g. 2025-26. Defaults to the most recent season loaded. Pass --list-seasons to see what's available.")
    parser.add_argument("--list-seasons", action="store_true", help="Print available seasons and exit")
    parser.add_argument(
        "--stat",
        help="Single-prop mode: stat name (PTS, REB, AST, STL, BLK, TOV, FG3M, "
             "PRA, PR, PA, RA, STOCKS)",
    )
    parser.add_argument("--line", type=float, help="Single-prop mode: the line, e.g. 30")
    parser.add_argument("--prop", action="append", default=[], help="Multi-prop mode: STAT:LINE, repeatable")

    parser.add_argument("--team-a", help="Same-game parlay mode: one of the two teams in the game, e.g. DEN")
    parser.add_argument("--team-b", help="Same-game parlay mode: the other team in the game, e.g. BOS")
    parser.add_argument("--home-team", help="Same-game parlay mode: which of --team-a/--team-b is home (optional)")
    parser.add_argument("--leg", action="append", default=[],
                         help="Same-game parlay mode: PLAYER:STAT:LINE or PLAYER:STAT:LINE:DIRECTION, repeatable")

    args = parser.parse_args()

    if args.list_seasons:
        conn = _connect()
        seasons = list_seasons(conn)
        conn.close()
        print("Available seasons:", ", ".join(seasons) if seasons else "(none loaded)")
        return

    same_game_mode = bool(args.team_a or args.team_b or args.leg)

    if same_game_mode:
        if not args.team_a or not args.team_b:
            raise SystemExit("Same-game parlay mode needs both --team-a and --team-b")
        if not args.leg:
            raise SystemExit("Same-game parlay mode needs at least one --leg PLAYER:STAT:LINE")
        if args.player or args.opponent or args.stat or args.line is not None or args.prop:
            raise SystemExit("Don't mix --player/--opponent/--stat/--line/--prop with --team-a/--team-b/--leg — "
                              "use --leg for every player in same-game parlay mode.")

        legs = [parse_leg(spec) for spec in args.leg]
        conn = _connect()
        try:
            results, combined = estimate_same_game_parlay(
                legs, args.team_a, args.team_b, home_team_abbr=args.home_team,
                recent_n=args.recent_n, conn=conn, season=args.season,
            )
            for r in results:
                print_result(r)
            print("\n" + "=" * 60)
            print(f"Same-game parlay combined probability (ASSUMING INDEPENDENCE): {combined:.1%}")
            print("Note: same-game props across different players are often even more")
            print("correlated than multiple props on one player — treat this as a rough")
            print("estimate, not a precise joint probability.")
        except ValueError as e:
            raise SystemExit(f"Error: {e}")
        finally:
            conn.close()
        return

    if not args.player or not args.opponent:
        raise SystemExit("--player and --opponent are required (unless using --list-seasons or same-game parlay mode)")

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
                recent_n=args.recent_n, conn=conn, season=args.season,
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
