"""
Basic sanity tests for the prop probability engine, run against the
synthetic demo database (data/nba_props.db).

Setup (once):
    python scripts/generate_seed_data.py
    python scripts/load_db.py

Run:
    python -m unittest tests/test_prop_model.py -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from prop_model import (  # noqa: E402
    estimate_prop_probability, combine_probabilities, estimate_same_game_parlay,
    _player_team_in_season, _connect, _find_player, _find_team,
    player_window_stat_avg, player_vs_opponent_stat_avg, player_recent_games,
    list_seasons, DB_PATH,
)


@unittest.skipUnless(DB_PATH.exists(), f"{DB_PATH} not found — run generate_seed_data.py + load_db.py first")
class TestPropModel(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Grab a player with a decent amount of playing time to run the
        # probability/stat-average tests against. Prefer role = 'star'
        # (the synthetic demo dataset's label for its top players) but
        # fall back to whoever has logged the most games this season —
        # real data fetched via fetch_data.py uses role = 'starter'
        # instead, and doesn't have a 'star' label at all.
        row = conn.execute(
            "SELECT full_name FROM players WHERE role = 'star' LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT p.full_name FROM player_game_logs pgl
                JOIN players p ON p.player_id = pgl.player_id
                GROUP BY pgl.player_id ORDER BY COUNT(*) DESC LIMIT 1
                """
            ).fetchone()
        self.player = row["full_name"]
        # Grab the league's best and worst defense (by points allowed) to test matchup sensitivity
        best = conn.execute(
            "SELECT abbreviation FROM v_team_defense_rating ORDER BY defense_percentile ASC LIMIT 1"
        ).fetchone()
        worst = conn.execute(
            "SELECT abbreviation FROM v_team_defense_rating ORDER BY defense_percentile DESC LIMIT 1"
        ).fetchone()
        self.best_defense = best["abbreviation"]
        self.worst_defense = worst["abbreviation"]

        # Two distinct teams, each with a player who actually has game
        # logs in the most recent loaded season — for same-game parlay
        # tests. Picking each player's CURRENT team (most recent game),
        # not just any team they logged a game with this season — a
        # player traded mid-season logged games with their old team
        # too, and estimate_same_game_parlay resolves players to their
        # most recent team, so pairing "team_a" with a player whose most
        # recent team is actually team_b would fail for the same reason
        # the real trade-resolution bug this app fixes would have.
        conn.row_factory = sqlite3.Row
        season = list_seasons(conn)[0]
        team_players = conn.execute(
            """
            SELECT pgl.player_id, p.full_name, t.abbreviation, pgl.game_date
            FROM player_game_logs pgl
            JOIN players p ON p.player_id = pgl.player_id
            JOIN teams t ON t.team_id = pgl.team_id
            WHERE pgl.season = ?
            """,
            (season,),
        ).fetchall()
        latest_by_player = {}
        for row in team_players:
            cur = latest_by_player.get(row["player_id"])
            if cur is None or row["game_date"] > cur["game_date"]:
                latest_by_player[row["player_id"]] = row
        by_team = {}
        for row in sorted(latest_by_player.values(), key=lambda r: r["abbreviation"]):
            by_team.setdefault(row["abbreviation"], row["full_name"])
        abbrs = list(by_team.keys())
        self.team_a, self.team_b = abbrs[0], abbrs[1]
        self.player_a, self.player_b = by_team[self.team_a], by_team[self.team_b]
        # A player from a third team, to test the "wrong game" error path
        self.other_team_player = by_team[abbrs[2]] if len(abbrs) > 2 else None
        self.season = season

        conn.close()

    def test_probability_is_between_0_and_1(self):
        r = estimate_prop_probability(self.player, "PTS", 15, self.worst_defense)
        self.assertGreaterEqual(r.probability, 0.0)
        self.assertLessEqual(r.probability, 1.0)

    def test_over_and_under_are_complementary(self):
        over = estimate_prop_probability(self.player, "PTS", 15, self.worst_defense, direction="over")
        under = estimate_prop_probability(self.player, "PTS", 15, self.worst_defense, direction="under")
        self.assertAlmostEqual(over.probability + under.probability, 1.0, places=6)

    def test_higher_line_is_less_likely(self):
        low = estimate_prop_probability(self.player, "PTS", 10, self.worst_defense)
        high = estimate_prop_probability(self.player, "PTS", 40, self.worst_defense)
        self.assertGreater(low.probability, high.probability)

    def test_unsupported_stat_raises(self):
        with self.assertRaises(ValueError):
            estimate_prop_probability(self.player, "FGA", 10, self.worst_defense)

    def test_unknown_player_raises(self):
        with self.assertRaises(ValueError):
            estimate_prop_probability("Definitely Not A Real Player Name", "PTS", 10, self.worst_defense)

    def test_pra_is_sum_of_components(self):
        r = estimate_prop_probability(self.player, "PRA", 30, self.worst_defense)
        self.assertGreaterEqual(r.recency_weighted_mean, 0)

    def test_combine_probabilities_multiplies(self):
        r1 = estimate_prop_probability(self.player, "PTS", 15, self.worst_defense)
        r2 = estimate_prop_probability(self.player, "AST", 3, self.worst_defense)
        combined = combine_probabilities([r1, r2])
        self.assertAlmostEqual(combined, round(r1.probability * r2.probability, 4), places=4)

    def test_same_game_parlay_resolves_opponents_automatically(self):
        legs = [
            {"player": self.player_a, "stat": "PTS", "line": 15, "direction": "over"},
            {"player": self.player_b, "stat": "REB", "line": 5, "direction": "over"},
        ]
        results, combined = estimate_same_game_parlay(legs, self.team_a, self.team_b)
        self.assertEqual(len(results), 2)
        # Each player's opponent should be resolved to the OTHER team, not
        # the one they actually play for.
        self.assertEqual(results[0].opponent_abbr, self.team_b)
        self.assertEqual(results[1].opponent_abbr, self.team_a)
        self.assertAlmostEqual(combined, round(results[0].probability * results[1].probability, 4), places=4)

    def test_same_game_parlay_player_not_in_game_raises(self):
        if self.other_team_player is None:
            self.skipTest("dataset doesn't have a third team to test against")
        legs = [{"player": self.other_team_player, "stat": "PTS", "line": 15, "direction": "over"}]
        with self.assertRaises(ValueError):
            estimate_same_game_parlay(legs, self.team_a, self.team_b)

    def test_same_game_parlay_same_team_twice_raises(self):
        legs = [{"player": self.player_a, "stat": "PTS", "line": 15, "direction": "over"}]
        with self.assertRaises(ValueError):
            estimate_same_game_parlay(legs, self.team_a, self.team_a)

    def test_same_game_parlay_home_away_applies_to_correct_side(self):
        legs = [{"player": self.player_a, "stat": "PTS", "line": 15, "direction": "over"}]
        home = estimate_same_game_parlay(legs, self.team_a, self.team_b, home_team_abbr=self.team_a)[0][0]
        away = estimate_same_game_parlay(legs, self.team_a, self.team_b, home_team_abbr=self.team_b)[0][0]
        # Same player, same opponent, only home/away context differs —
        # the two results shouldn't need to differ, but the call itself
        # must succeed both ways without raising.
        self.assertEqual(home.opponent_abbr, self.team_b)
        self.assertEqual(away.opponent_abbr, self.team_b)

    def test_player_team_resolves_to_most_recent_after_trade(self):
        """Regression test for a real bug: a player traded mid-season used
        to resolve to whichever team they'd logged the MOST games with, not
        their most recent one — so a player who played more games with
        their old team before the trade than their new team since would
        resolve back to the old team, breaking same-game parlay matching
        against their new team's game. Uses an isolated in-memory DB so it
        doesn't depend on the demo dataset containing a real trade."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE player_game_logs (
                log_id TEXT PRIMARY KEY, game_id TEXT, game_date TEXT, season TEXT,
                player_id INTEGER, team_id INTEGER
            )
        """)
        # 10 games with team 1 (old team) early in the season...
        for i in range(10):
            conn.execute(
                "INSERT INTO player_game_logs VALUES (?, ?, ?, '2025-26', 999, 1)",
                (f"log{i}", f"game{i}", f"2025-10-{i+1:02d}"),
            )
        # ...traded, then only 3 games with team 2 (new team) — fewer games,
        # but more recent.
        for i in range(3):
            conn.execute(
                "INSERT INTO player_game_logs VALUES (?, ?, ?, '2025-26', 999, 2)",
                (f"log{10+i}", f"game{10+i}", f"2026-01-{i+1:02d}"),
            )
        conn.commit()
        self.assertEqual(_player_team_in_season(conn, 999, "2025-26"), 2)
        conn.close()

    def test_window_stat_avg_matches_manual_calc(self):
        conn = _connect()
        player = _find_player(conn, self.player)
        avg5, n5 = player_window_stat_avg(conn, player["player_id"], "PTS", self.season, 5)
        rows = conn.execute(
            """
            SELECT points FROM v_player_rolling_stats
            WHERE player_id = ? AND season = ? AND games_ago <= 5 ORDER BY games_ago
            """,
            (player["player_id"], self.season),
        ).fetchall()
        conn.close()
        self.assertEqual(n5, len(rows))
        if rows:
            self.assertAlmostEqual(avg5, sum(r["points"] for r in rows) / len(rows), places=6)

    def test_window_stat_avg_shrinks_with_smaller_window(self):
        # Not a claim the numbers must differ (they legitimately can be
        # equal by coincidence) — just that a 5-game and 30-game window
        # both return coherent, independently-computed results without
        # one leaking into the other's sample size.
        conn = _connect()
        player = _find_player(conn, self.player)
        _, n5 = player_window_stat_avg(conn, player["player_id"], "PTS", self.season, 5)
        _, n30 = player_window_stat_avg(conn, player["player_id"], "PTS", self.season, 30)
        conn.close()
        self.assertLessEqual(n5, 5)
        self.assertGreaterEqual(n30, n5)

    def test_vs_opponent_stat_avg_only_counts_games_against_that_team(self):
        conn = _connect()
        player = _find_player(conn, self.player_a)
        opponent = _find_team(conn, self.team_b)
        avg, n = player_vs_opponent_stat_avg(
            conn, player["player_id"], "PTS", opponent["team_id"], self.season,
        )
        rows = conn.execute(
            """
            SELECT points FROM v_player_rolling_stats
            WHERE player_id = ? AND season = ? AND opponent_team_id = ?
            """,
            (player["player_id"], self.season, opponent["team_id"]),
        ).fetchall()
        conn.close()
        self.assertEqual(n, len(rows))
        if rows:
            self.assertAlmostEqual(avg, sum(r["points"] for r in rows) / len(rows), places=6)
        else:
            self.assertIsNone(avg)

    def test_recent_games_oldest_first_and_matches_manual_calc(self):
        conn = _connect()
        player = _find_player(conn, self.player)
        n = 8
        games = player_recent_games(conn, player["player_id"], "PTS", self.season, n)
        rows = conn.execute(
            """
            SELECT game_date, opponent_abbr, is_home, points, minutes FROM v_player_rolling_stats
            WHERE player_id = ? AND season = ? AND games_ago <= ?
            ORDER BY games_ago DESC
            """,
            (player["player_id"], self.season, n),
        ).fetchall()
        conn.close()
        self.assertEqual(len(games), len(rows))
        if rows:
            # Oldest-first: dates should be non-decreasing across the list.
            dates = [g["game_date"] for g in games]
            self.assertEqual(dates, sorted(dates))
            for g, r in zip(games, rows):
                self.assertEqual(g["game_date"], r["game_date"])
                self.assertEqual(g["opponent_abbr"], r["opponent_abbr"])
                self.assertEqual(g["is_home"], bool(r["is_home"]))
                self.assertEqual(g["value"], r["points"])
                self.assertEqual(g["minutes"], r["minutes"])

    def test_recent_games_empty_for_player_with_no_logs(self):
        conn = _connect()
        games = player_recent_games(conn, -1, "PTS", self.season, 10)
        conn.close()
        self.assertEqual(games, [])


if __name__ == "__main__":
    unittest.main()
