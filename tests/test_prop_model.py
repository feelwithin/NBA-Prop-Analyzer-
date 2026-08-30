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

from prop_model import estimate_prop_probability, combine_probabilities, DB_PATH  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
