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

See **[WRITEUP.md](WRITEUP.md)** for a shorter, portfolio-facing
summary of the methodology and validation results (including the
calibration chart) — this README is the full technical reference.

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
- **Same-game parlays across multiple players** — pick the two teams
  playing each other, then add props for any players on either
  roster; each player's opponent is figured out automatically. See
  **Same-game parlays** below.

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

For a visual version of that table (a reliability chart — predicted
probability vs. actual hit rate, bucket size shown as point size),
run:

```bash
python3 scripts/make_calibration_chart.py
```

after `backtest.py` — it reads `data/output/backtest_results.csv` and
writes `assets/calibration_chart.png`. See **Calibration history**
below for what this project's own backtest runs found and fixed.

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
- **Multiple seasons at once** are fully supported — the database
  schema, every feature view, `prop_model.py`, `cli.py`,
  `app.py`, and `backtest.py` are all season-scoped, so stats from
  different seasons never blend together. This is what lets you
  keep a full, stable season (e.g. `2024-25`) as a reliable
  baseline while also adding the current season as it fills in
  (rookies included) — see **Adding a new season** below.

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
--season SEASON       e.g. 2025-26 — defaults to the most recent season loaded
--list-seasons        print available seasons and exit
--stat STAT --line N  single prop
--prop STAT:LINE      repeatable, for multi-leg parlay checks
```

## Same-game parlays

`--prop`/`combine_probabilities` above only combine multiple props for
**one player**. A same-game parlay across **different players** in the
same game is a separate mode, in both the CLI and the app, because it
needs a different piece of context: which two teams are actually
playing each other, so each player's opponent can be figured out
automatically instead of you specifying it per leg (and so a stray
player from an unrelated game gets caught as an error, not silently
graded against the wrong opponent).

CLI:
```bash
python3 scripts/cli.py --team-a DEN --team-b BOS --home-team BOS \
    --leg "Shai Gilgeous-Alexander:PTS:30" \
    --leg "Jaylen Brown:PTS:20:under" \
    --leg "Nikola Jokic:PRA:45"
```
- `--team-a` / `--team-b`: the two teams in the game (any order)
- `--home-team`: optional, must be one of the two — enables the
  home/away adjustment for whichever players are on that team
- `--leg`: repeatable, `PLAYER:STAT:LINE` or `PLAYER:STAT:LINE:DIRECTION`
  (direction defaults to `over`) — mix players from either team freely
- Don't combine this mode with `--player`/`--opponent`/`--stat`/`--prop`;
  use `--leg` for every player instead

Web app: the **Same-game parlay** tab (next to **Single player**) works
the same way — pick Team A and Team B, optionally which one is home,
then add a row per player/prop, mixing players from either roster.

Same caveat as any parlay: the combined probability assumes
independence between legs. This matters even more here than for a
single player's props — a big night from one player and a rough one
for a teammate (or a big defensive night from an opponent) are often
correlated in real games, so treat the combined number as a rough
estimate, not a precise joint probability.

## Adding a new season (keeping your existing data)

Once you've fetched a season with `scripts/fetch_data.py`, you don't
have to throw it away to add a newer one. Real NBA game IDs are
globally unique across seasons, and players are matched by their
persistent `player_id`, so seasons merge cleanly — a rookie is just
a new player row, a returning player keeps their full history, and
nothing from the season you already have gets lost.

```bash
# You already have, say, data/seed/*.csv for 2024-25. To add 2025-26
# alongside it (not instead of it), pass --append:
python3 scripts/fetch_data.py --season 2025-26 --append
python3 scripts/load_db.py
```

Now both seasons are in the database, and:
- **CLI**: `--season 2025-26` (or `--season 2024-25`) picks which
  one to use; omitting it defaults to the most recent. Run
  `python3 scripts/cli.py --list-seasons` to see what's loaded.
- **Web app**: a **Season** dropdown appears automatically (only
  shown when more than one season is loaded) — pick the full,
  stable season for a deeper sample, or the current season to
  include this year's rookies and trades, with the tradeoff of
  fewer games played so far.
- **Backtest**: `python3 scripts/backtest.py --season 2025-26`
  restricts calibration testing to one season; omitting `--season`
  tests all loaded seasons at once (each still scored independently
  — a player's or team's stats from one season are never used to
  predict a game in another).

Without `--append`, `fetch_data.py` overwrites `data/seed/*.csv`
with just the one season you fetched — use that if you want to
start over rather than add on.

### Keeping data fresh

Running `fetch_data.py --append` by hand keeps the live app current
(rosters catching recent trades, game logs catching last night's
games) — do this whenever you want an update.

A GitHub Actions workflow that runs this on a daily schedule was
tried and deliberately abandoned, which is worth documenting rather
than quietly deleting: `nba_api` calls `stats.nba.com`, and that API
blocks requests from cloud/datacenter IP ranges — every single
request timed out when run from a GitHub-hosted runner (Microsoft
Azure IPs), regardless of retries. This isn't a bug in this project's
code; it's the same reason plenty of sports-data tools document
"doesn't work from AWS/Azure/GCP." Making it work would mean either
running the schedule from a machine with a real residential IP (a
self-hosted GitHub Actions runner on a machine that's reliably online
at the scheduled time) or routing through a paid proxy service —
both more infrastructure than this project's scope calls for, so
manual refresh is the actual, working answer.

After fetching, `scripts/ci_sanity_check.py` is still worth running
by hand as a quick check before trusting the result — it rebuilds the
DB and refuses to proceed quietly if an unusually large fraction of
rows had to be skipped, which is what a corrupted merge looks like
(see `check_header_compatible()` in `fetch_data.py` for the
first-line defense against that same failure mode):

```bash
python3 scripts/fetch_data.py --season 2025-26 --append
python3 scripts/ci_sanity_check.py
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

### Look and feel

The app has a sportsbook-style look (FanDuel-inspired), branded dark
navy/blue with a logo:
- **Pill-style OVER/UNDER buttons** instead of a dropdown — tap one
  to select that direction, matching the highlighted-pill pattern
  from betting-slip UIs.
- **A "slip" card** groups your prop legs together with a running
  pick count, instead of a bare form.
- **Player avatars**: a real headshot for players fetched via
  `fetch_data.py` (pulled from the NBA's own CDN using their real
  player ID — nothing to configure), falling back to an initials
  badge for the synthetic demo dataset or any player without a
  published photo.
- Player dropdowns narrow to just the relevant team's roster once
  you've picked a team (instead of scrolling ~600 names), and
  "recent games to weight" is a row of tappable presets instead of
  a slider — both aimed at actually being usable on a phone.
- **A live stats preview** under each prop: as soon as a player and
  stat are picked, it draws a StatMuse-style bar chart of their games
  for that stat, colored green/red against your current line and
  Over/Under pick, oldest to newest, labeled with the opponent and
  minutes played. Once an opponent is known, a chip toggle switches
  the chart between "Recent games" (last N games, any opponent — N is
  the selected "recent games" window) and "Vs [team]" (every meeting
  against that specific opponent so far this season). A game where
  they played well below their usual minutes for the shown set
  (blowout, rest, foul trouble, etc.) is drawn faded with its minutes
  called out in amber, plus a footnote — so a short bar reads as "sat
  the 4th", not "in a slump". Underneath, a plain-text line adds their
  average for the window plus — once an opponent is known — their
  average in games actually played against that specific opponent
  this season (usually just a handful of meetings, since teams only
  play each other a few times a year). Updates instantly as you
  change the games-window preset, switch stats, or toggle chart mode.
  This is simple, unweighted
  context — not the recency-weighted, matchup-adjusted number the
  model itself uses for the actual probability.

To rebrand it as your own:
- **Colors/fonts**: edit `.streamlit/config.toml` (native Streamlit
  theming — applies to every widget) and the `ACCENT` constant near
  the top of `app.py` (used by the custom CSS for the section
  headers, slip title, and avatar fallback color) — keep the two in
  sync.
- **Logo**: replace `assets/logo.png` with your own square image
  (used as both the browser-tab favicon and the header mark) — any
  size works, it's resized automatically.
- **Name**: it's hardcoded as "Nabil's Prop Analyzer" in a couple of
  places in `app.py` (`st.set_page_config(page_title=...)` and the
  header markdown) — search for that string to change it.

## Schema

```
teams (team_id PK, team_name, abbreviation, city, conference, division)
players (player_id PK, full_name, team_id FK, position, role)
games (game_id PK [TEXT], game_date, season, home_team_id FK, away_team_id FK, home_score, away_score)
player_game_logs (log_id PK [TEXT], game_id FK, game_date, season, player_id FK, team_id FK,
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
├── assets/
│   └── logo.png                 # app logo (favicon + header)
├── .streamlit/
│   └── config.toml              # app theme (colors/fonts) — edit this to rebrand
├── app.py                       # web UI (Streamlit) — see "Share it with a friend"
├── requirements.txt
└── README.md
```

## Calibration history: three rounds of fixes

Backtesting against real 2024-25 data (see **Validation** above) went
through three real rounds of fixes, each one caught by the same
process — run `scripts/backtest.py`, read the calibration table, fix
what it shows, re-run:

1. **Flooring the std** (modest effect — the effective sample size
   wasn't actually the bottleneck).
2. **Dampening the matchup-defense adjustment** the same way the
   home/away adjustment already was — this closed most of the
   overconfidence gap (Brier score improved from 0.2491 to 0.2473,
   and the model stopped producing wildly overconfident extreme
   predictions).
3. **Shrinking the recency-weighted mean toward the full-season
   mean**, in proportion to sample size (`_shrink_to_season_mean` in
   `prop_model.py`). After step 2, a smaller pattern remained:
   predictions below 50% ran a few points low and predictions above
   50% ran a few points high relative to actual outcomes — consistent
   with "regression to the mean" (a hot/cold recent stretch partially
   reverting). The recency-weighted mean was getting full weight
   regardless of how much recent evidence backed it, while the
   matchup/home-away multipliers were already dampened for exactly
   this kind of overconfidence. Applying the same idea to the base
   mean — an empirical-Bayes-style blend, `n / (n + k)` weight on the
   recent estimate vs. `k / (n + k)` on the season baseline, with
   `k=10` chosen as a round number of the same order as the half-life
   rather than fit to one dataset — is the principled fix, as opposed
   to hand-tuning yet another constant against a single backtest run
   and risking overfitting the model to one dataset/season instead of
   actually improving it.

On the bundled synthetic demo dataset this run reported a Brier score
of 0.2281 (baseline 0.2500) across ~41,700 backtested picks, with the
well-populated buckets (roughly 2,000+ picks each) landing within a
couple of points of perfect calibration — see the chart below,
generated by `scripts/make_calibration_chart.py`.

![Calibration chart](assets/calibration_chart.png)

**The honest result on real data — including a methodology bug found
along the way, and a fix that turned out not to be the answer.**
While validating this fix, `backtest.py`'s synthetic "fair line" was
found to be `round()` of the model's raw (unshrunk) trailing average
while the probability was computed from the shrunk one — a mismatch
that has nothing to do with real calibration but looks exactly like
it. On real fetched data (both loaded seasons merged, ~164,000
backtested picks) that bug alone produced a Brier score barely above
the 0.5-baseline (0.2469, 54.2% directional accuracy). Fixing it — so
the line and the prediction come from the same estimate — moved that
number to **0.2467, 54.1% accuracy: essentially unchanged.**

That result matters more than it looks like it should: it means the
line-anchoring bug, while real and worth fixing, was NOT the cause of
the gap, and neither, apparently, is the regression-to-the-mean
shrinkage this whole investigation started from — shrinkage barely
moved the real-data numbers at all, the same as it barely moved the
synthetic ones. Two things stand out in the real calibration table
that the synthetic run doesn't show: (1) the bulk of test cases land
in the 40-60% predicted range by construction, since the synthetic
line is set from the model's own current estimate — this backtest
design is better at revealing directional bias in the well-populated
middle buckets than at stress-testing extreme-confidence claims, which
end up backed by very few real-data cases; and (2) even in those
well-populated middle buckets, a modest bias remains in the same
direction documented before (predictions pulled a few points away from
50% relative to what actually happens) — small enough that guessing
its cause without more evidence would be exactly the kind of
single-dataset overfitting this section has already warned against
once. This is left open rather than patched with another guess — see
**Roadmap** below.

Both `backtest.py`'s line generation and `prop_model.py` itself are
unaffected in the live app by any of this — a live prediction's line
always comes from the prop you type in, never from the model's own
estimate — so none of this changes what the deployed app tells you
today. It changes how much to trust the app's *stated* confidence
level at the margins, which is exactly why this section exists instead
of just shipping a Brier score and moving on.

## Roadmap / possible extensions

- **Track down the remaining real-data calibration bias**: a modest
  gap (predictions running a few points more extreme than actual
  outcomes) survived both the dampening fixes and the shrinkage fix —
  see **Calibration history** above. Worth investigating with real
  evidence rather than another guess: split the calibration table by
  season (is it worse for the newer, thinner-sample season?), by stat
  (points vs. the lower-count stats using the same normal
  approximation?), or check whether the std estimate itself needs
  the same kind of shrinkage the mean just got.
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
