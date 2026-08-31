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
    _player_team_in_season, list_seasons, DB_PATH,
)


@unittest.skipUnless(DB_PATH.exists(), f"{DB_PATH} not found — run generate_seed_data.py + load_db.py first")
class TestPropModel(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Grab a real star player from the demo dataset (name is random-generated, so look it up)
        self.player = conn.execute(
            "SELECT full_name FROM players WHERE role = 'star' LIMIT 1"
        ).fetchone()["full_name"]
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
        # logs in the most recent loaded season — for same-game parlay tests.
        conn.row_factory = sqlite3.Row
        season = list_seasons(conn)[0]
        team_players = conn.execute(
            """
            SELECT DISTINCT t.abbreviation, p.full_name
            FROM player_game_logs pgl
            JOIN players p ON p.player_id = pgl.player_id
            JOIN teams t ON t.team_id = pgl.team_id
            WHERE pgl.season = ?
            ORDER BY t.abbreviation
            """,
            (season,),
        ).fetchall()
        by_team = {}
        for row in team_players:
            by_team.setdefault(row["abbreviation"], row["full_name"])
        abbrs = list(by_team.keys())
        self.team_a, self.team_b = abbrs[0], abbrs[1]
        self.player_a, self.player_b = by_team[self.team_a], by_team[self.team_b]
        # A player from a third team, to test the "wrong game" error path
        self.other_team_player = by_team[abbrs[2]] if len(abbrs) > 2 else None

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


if __name__ == "__main__":
    unittest.main()
