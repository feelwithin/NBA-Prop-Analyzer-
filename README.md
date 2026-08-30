# NBA Prop Analyzer

A SQL + Python engine that estimates the probability an NBA player
prop hits — e.g. "Shai Gilgeous-Alexander OVER 30 points" — using a
recency-weighted model of the player's own performance, adjusted for
how well the opponent defends players at that position.

> **This is a statistical/analytical tool, not betting advice.** It
> estimates likelihood from historical patterns; it does not know
> injuries, rotations, or lineup news announced after data was
> pulled, and past performance does not guarantee future results.
> Sports outcomes are inherently uncertain — treat the output as one
> input among many, not a guarantee.

## What it does

Given a player, a stat (points, rebounds, assists, steals, blocks,
or points+rebounds+assists), a line, and an opponent, it returns:

- **A probability** the player goes over (or under) that line
- **The matchup-adjusted expected value** and how it was derived
- **A breakdown**: recency-weighted average, season average, matchup
  factor, home/away factor, raw historical hit rate, and a
  confidence rating based on sample size
- **Combined probability** for multiple props on the same
  player/game (a simple "parlay" check), with an explicit caveat
  that this assumes independence between legs

```
$ python scripts/cli.py --player "Shai Gilgeous-Alexander" --opponent DEN --prop PTS:30 --prop AST:4

Shai Gilgeous-Alexander — PTS OVER 30.0 vs DEN
------------------------------------------------------------
  Probability of hitting:      64.2%   [LIKELY OVER]
  Confidence:                  high
  Recency-weighted average:    29.8
  Season average:              28.4
  Matchup factor:              1.071  (1.0 = league avg)
  Home/away factor:            1.0
  Matchup-adjusted mean:       31.9  (± 6.1 std)
  Raw historical hit rate:     55.0%  (last 20 games)
  note: DEN defends G below league average (factor 1.071) — favorable matchup
...
```

*(illustrative output shape — this exact example requires real data; see Quickstart below)*

## Methodology

1. **Recency-weighted base rate.** Pulls the player's last N games
   (default 20) for the chosen stat and applies exponential decay
   weighting (half-life of 8 games by default) — recent form counts
   more than a game from a month ago, but the whole sample still
   contributes.
2. **Matchup adjustment.** Computes what the opponent allows to
   players at that position (Guard/Forward/Center) relative to the
   league average, from real box-score history
   (`v_team_position_defense`). A factor above 1.0 means the
   opponent is soft against that position; below 1.0 means tough.
   The player's weighted average is scaled by this factor.
3. **Home/away adjustment.** A small, dampened adjustment based on
   the player's own home/away split — only applied when there's
   enough sample size (5+ games) to trust it, and blended 70% toward
   neutral to avoid overreacting to noise.
4. **Probability.** Treats the adjusted mean/std as a normal
   distribution and computes P(stat ≥ line) with a continuity
   correction (since these are discrete counting stats), using
   `math.erf` — no external stats library required.
5. **Cross-check.** Also reports the simple raw hit rate ("hit this
   line in X of the last N games") as a sanity check against the
   modeled probability.

### Why team-level position defense, not "who's guarding him"

The original ask was closer to "who's likely guarding him, and how
good are they at it" — true individual-defender matchup data exists
in the NBA's tracking stats, but it's harder to pull reliably (it
requires per-matchup-pair API calls and has gaps), so this v1 uses a
defensible proxy: how the *opponent team* defends players at that
*position* overall. That's still real signal (a historically weak
perimeter defense will show up in `v_team_position_defense`'s Guard
numbers), just not identity-of-defender-specific. See **Roadmap**
below for the individual-matchup extension.

## Validation: is it actually calibrated?

A probability model is only useful if its numbers mean what they
claim — a pick it rates "70%" should hit roughly 70% of the time
across many picks, not just sound confident. `scripts/backtest.py`
checks this directly and honestly:

It replays every game in the loaded season in chronological order.
For each game, it predicts using ONLY data strictly before that
date — the player's trailing form and every team's position-defense
stats are both computed from history-so-far, never from the future
(a common backtesting mistake is leaking future information into a
"prediction" of the past). Since real historical betting lines
aren't available, each test uses the player's own trailing average
as a synthetic "fair line" with over/under picked at random
(seeded, reproducible) — this checks whether the model's confidence
is well-calibrated in general, not whether a specific sportsbook's
line is beatable.

```bash
python3 scripts/backtest.py
```

It reports a Brier score (lower is better, versus an "always guess
50%" baseline) and a calibration table — predicted-probability
buckets next to the actual hit rate in each bucket, which should
track closely if the model is honest about its own confidence.
Full per-pick results are also written to
`data/output/backtest_results.csv`.

## Data

- **Teams** are real (30 current NBA franchises, correct
  conference/division).
- **The shipped demo dataset** (`data/seed/*.csv`) is a seeded,
  statistically realistic *simulation* — synthetic players, games,
  and box scores, deliberately generated with hidden per-team,
  per-position defensive strength so the matchup-adjustment logic
  has something real to detect (verified in `tests/`). This means
  the whole project runs and is testable with zero setup or API
  keys, but names like "Shai Gilgeous-Alexander" won't exist in it.
- **For real players and real matchup effects**, run
  `scripts/fetch_data.py` on a machine with internet access — it
  pulls real rosters and full-season box scores via the free
  [`nba_api`](https://github.com/swar/nba_api) package into the
  exact same schema, so nothing downstream changes.

## Quickstart

**Option A — instant demo (synthetic data):**
```bash
python3 scripts/generate_seed_data.py
python3 scripts/load_db.py
python3 scripts/cli.py --player "<any generated name — see data/seed/players.csv>" \
    --opponent BOS --stat PTS --line 20
python3 -m unittest tests/test_prop_model.py -v
```

**Option B — real data:**
```bash
pip install nba_api pandas
python3 scripts/fetch_data.py --season 2025-26
python3 scripts/load_db.py
python3 scripts/cli.py --player "Shai Gilgeous-Alexander" --opponent DEN --stat PTS --line 30
```

CLI options:
```
--player NAME        full name or unique substring
--opponent ABBR       e.g. DEN, BOS, LAL
--direction over|under
--home / --away       optional context for the home/away adjustment
--recent-n N          how many recent games to weight (default 20)
--stat STAT --line N  single prop
--prop STAT:LINE      repeatable, for multi-leg parlay checks
```

Or query the SQL views directly:
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('data/nba_props.db')
for row in conn.execute('SELECT * FROM v_team_defense_rating ORDER BY defense_percentile LIMIT 10'):
    print(row)
"
```

## Share it with a friend (no coding required)

`app.py` is a simple web UI over the same model — dropdowns instead
of command-line flags. Two ways to use it:

**Just for you, running locally:**
```bash
pip install streamlit
streamlit run app.py
```
Opens in your browser at `localhost:8501`.

**A link you can send someone (free, no server to manage):**
1. Push this repo to GitHub (see below if you haven't already).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in
   with GitHub, click **New app**, and point it at this repo —
   main file path `app.py`.
3. Click **Deploy**. Streamlit Community Cloud builds and hosts it
   for free, and gives you a public URL like
   `https://your-app-name.streamlit.app` — send that to your friend
   and they can use it from a phone or laptop, no installs needed.

This only works if `data/seed/*.csv` is committed to the repo (it is
by default — see **Data** below) — the app builds its database from
those files the first time it starts. If you fetched real data with
`scripts/fetch_data.py`, that's what your friend will see; if you're
still on the synthetic demo data, mention that so they know the
names aren't real NBA players.

## Schema

```
teams (team_id PK, team_name, abbreviation, city, conference, division)
players (player_id PK, full_name, team_id FK, position, role)
games (game_id PK, game_date, season, home_team_id FK, away_team_id FK, home_score, away_score)
player_game_logs (log_id PK, game_id FK, game_date, player_id FK, team_id FK,
                   opponent_team_id FK, is_home, minutes, points, rebounds, assists,
                   steals, blocks, turnovers, fg_made, fg_attempted, three_made,
                   three_attempted, ft_made, ft_attempted)
```

Feature views (`sql/schema.sql`, documented in `sql/feature_queries.sql`):
`v_player_rolling_stats`, `v_player_home_away_splits`,
`v_team_defense_rating`, `v_team_position_defense`,
`v_player_vs_opponent_history`.

## Project structure

```
nba-prop-analyzer/
├── sql/
│   ├── schema.sql               # tables + feature views
│   └── feature_queries.sql      # readable reference + example queries
├── scripts/
│   ├── generate_seed_data.py    # synthetic demo dataset (reproducible)
│   ├── load_db.py               # CSVs -> SQLite
│   ├── prop_model.py            # the probability engine
│   ├── cli.py                   # command-line interface
│   ├── fetch_data.py            # real data via nba_api (run locally)
│   └── backtest.py              # point-in-time calibration backtest
├── tests/
│   └── test_prop_model.py       # sanity checks on the engine
├── data/seed/                   # generated/fetched CSVs
├── app.py                       # web UI (Streamlit) — see "Share it with a friend"
├── requirements.txt
└── README.md
```

## Known limitation: a small remaining bias in the recency-weighted mean

Backtesting against real 2024-25 data (see **Validation** above) went
through two real rounds of fixes: flooring the std (modest effect —
the effective sample size wasn't actually the bottleneck) and, more
substantially, dampening the matchup-defense adjustment the same way
the home/away adjustment already was (this closed most of the
overconfidence gap: Brier score improved from 0.2491 to 0.2473, and
the model no longer produces wildly overconfident extreme
predictions).

A smaller pattern remains: predictions below 50% run a few points low
(actual outcomes hit more than predicted) while predictions above 50%
run a few points high (actual outcomes hit less than predicted).
Likely cause: the recency-weighted mean gives extra weight to a
player's most recent games, and hot stretches tend to cool back
toward a player's real season level ("regression to the mean") — the
model corrects for this on the matchup/home-away multipliers but not
on the base recency-weighted mean itself. The principled fix is to
shrink the recency-weighted mean toward the season-long mean in
proportion to sample size (an empirical-Bayes-style estimator, the
same idea already used for the dampened adjustments), rather than
hand-tuning another constant against a single backtest run — doing
the latter risks overfitting the model to one dataset/season instead
of actually improving it.

## Roadmap / possible extensions

- **Individual defender matchups**: pull `leagueseasonmatchups` /
  `matchupsrollup` from `nba_api` to know specifically how a player
  performs against a given primary defender, not just team-level
  position defense.
- **Injury/rotation awareness**: cross-reference current injury
  reports so the model doesn't rely on stale minutes assumptions.
- **Better distribution fitting**: swap the normal-approximation for
  a distribution better suited to low-count stats (e.g. blocks,
  steals), such as a Poisson or Negative Binomial fit.

## Skills demonstrated

SQL (multi-table joins, CTEs, window functions, views as a feature
layer), Python statistical modeling (weighted distributions,
normal-CDF probability estimation), data engineering (a real
external-API ETL pipeline alongside a reproducible synthetic
fixture for testing), and automated testing.

## License

MIT — see [LICENSE](LICENSE).
