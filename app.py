"""
A simple web UI over the prop model — for sharing with people who
don't want to use a terminal.

Run locally:
    pip install streamlit
    streamlit run app.py

Deploy for free (so a friend can just click a link):
    See "Share it with a friend" in README.md — Streamlit Community
    Cloud deploys this directly from your GitHub repo.

Branding: colors/theme live in .streamlit/config.toml (Streamlit's
native theming — applies to every built-in widget automatically), and
the logo is assets/logo.png. To rebrand, edit those two things; the
layout code below doesn't hardcode colors.
"""
import base64
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from prop_model import (  # noqa: E402
    estimate_prop_probability, combine_probabilities, estimate_same_game_parlay,
    _connect, DB_PATH, list_seasons,
)

LOGO_PATH = ROOT / "assets" / "logo.png"

st.set_page_config(
    page_title="Nabil's Prop Analyzer",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "🏀",
    layout="centered",
)

# A few small CSS touches that native theming doesn't cover: mobile
# spacing, form inputs at >=16px (prevents iOS Safari auto-zooming
# into a field on tap), and letting columns wrap instead of squeezing
# on a narrow phone screen instead of stacking.
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; max-width: 760px; }
      div[data-testid="stHorizontalBlock"] { flex-wrap: wrap; gap: 0.6rem; }
      input, select, textarea { font-size: 16px !important; }
      .section-title {
          font-size: 0.95rem; font-weight: 700; letter-spacing: 0.04em;
          text-transform: uppercase; opacity: 0.75;
          border-left: 3px solid #FF7A1A; padding-left: 10px;
          margin: 1.3rem 0 0.7rem 0;
      }
      .app-tagline { font-size: 0.92rem; opacity: 0.65; margin-top: -0.2rem; }
      @media (max-width: 480px) {
          .block-container { padding-left: 1rem; padding-right: 1rem; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def section(title):
    st.markdown(f"<div class='section-title'>{title}</div>", unsafe_allow_html=True)


@st.cache_resource
def ensure_database():
    """Build data/nba_props.db from the committed seed CSVs on first
    load, if it isn't there already (e.g. a fresh deploy)."""
    if not DB_PATH.exists():
        with st.spinner("Setting up the database (first run only)..."):
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "load_db.py")],
                check=True, cwd=ROOT,
            )
    return True


@st.cache_data
def load_options():
    conn = _connect()
    players = [r["full_name"] for r in conn.execute("SELECT full_name FROM players ORDER BY full_name")]
    teams = [
        (r["abbreviation"], r["team_name"])
        for r in conn.execute("SELECT abbreviation, team_name FROM teams ORDER BY team_name")
    ]
    seasons = list_seasons(conn)

    # season -> team abbreviation -> sorted list of player names who
    # actually logged games for that team that season. Used to narrow
    # long player dropdowns down to just the relevant roster (e.g. once
    # two teams are picked for a same-game parlay, there's no reason to
    # show all ~600 players — only the ~30 who could possibly be in
    # that game).
    by_season_team = {}
    rows = conn.execute(
        """
        SELECT DISTINCT pgl.season, t.abbreviation, p.full_name
        FROM player_game_logs pgl
        JOIN players p ON p.player_id = pgl.player_id
        JOIN teams t ON t.team_id = pgl.team_id
        """
    ).fetchall()
    for row in rows:
        by_season_team.setdefault(row["season"], {}).setdefault(row["abbreviation"], []).append(row["full_name"])
    for season_map in by_season_team.values():
        for abbr in season_map:
            season_map[abbr].sort()

    conn.close()
    return players, teams, seasons, by_season_team


def recent_games_control(label, key, default=20):
    """Preset chips instead of a draggable slider — easier to tap
    precisely on a phone than dragging a thin slider handle. Falls
    back to a slider if the installed Streamlit is too old to have
    segmented_control (added in Streamlit 1.36)."""
    options = [5, 10, 15, 20, 25, 30, 40]
    if hasattr(st, "segmented_control"):
        val = st.segmented_control(label, options, default=default, key=key)
        return val if val is not None else default
    return st.slider(label, min_value=5, max_value=40, value=default, key=key)


def render_result(r):
    verdict = "LIKELY HIT" if r.probability >= 0.5 else "LIKELY MISS"
    verdict_color = "#2ECC71" if r.probability >= 0.5 else "#FF5C5C"
    confidence_level = r.confidence.split(" ")[0]  # e.g. "low (small sample)" -> "low"

    st.markdown(
        f"""
        <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:0.4rem;">
          <div style="font-weight:700;font-size:1.05rem;">
            {r.player_name} <span style="opacity:0.55;font-weight:400;">({r.season})</span>
          </div>
          <div style="color:{verdict_color};font-weight:700;font-size:0.85rem;letter-spacing:0.03em;">
            {verdict}
          </div>
        </div>
        <div style="opacity:0.7;font-size:0.9rem;margin-bottom:0.4rem;">
          {STAT_LABELS[r.stat]} {r.direction.upper()} {r.line} vs {r.opponent_abbr}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.progress(min(max(r.probability, 0.0), 1.0), text=f"{r.probability:.1%} probability")

    m1, m2 = st.columns(2)
    m1.metric("Confidence", confidence_level.capitalize())
    m2.metric("Historical hit rate", f"{r.historical_hit_rate:.0%}", f"last {r.sample_size} games")
    if confidence_level == "low":
        st.caption(f"⚠️ Confidence: {r.confidence} — take this one with extra caution.")

    with st.expander("Show the breakdown"):
        st.write(f"- Recency-weighted average: **{r.recency_weighted_mean}**")
        st.write(f"- Season average: **{r.season_mean}**")
        st.write(f"- Matchup factor: **{r.matchup_factor}** (1.0 = league average)")
        st.write(f"- Home/away factor: **{r.home_away_factor}**")
        st.write(f"- Matchup-adjusted mean: **{r.adjusted_mean}** (± {r.adjusted_std} std)")
        for note in r.notes:
            st.caption(note)
    st.divider()


ensure_database()
players, teams, seasons, players_by_season_team = load_options()
team_labels = [f"{name} ({abbr})" for abbr, name in teams]
team_abbr_by_label = {f"{name} ({abbr})": abbr for abbr, name in teams}

# ---- Header ----
if LOGO_PATH.exists():
    logo_b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:0.15rem;">
          <img src="data:image/png;base64,{logo_b64}" style="width:52px;height:52px;border-radius:50%;flex-shrink:0;" />
          <div>
            <div style="font-size:1.65rem;font-weight:800;line-height:1.15;">Nabil's Prop Analyzer</div>
            <div class="app-tagline">NBA player prop probabilities, matchup-adjusted</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.title("🏀 Nabil's Prop Analyzer")

st.caption(
    "Estimates the probability an NBA player prop hits, using a recency-weighted "
    "model of their own performance adjusted for the opponent's defense. "
    "**This is an analytics tool, not betting advice** — treat it as one input "
    "among many, not a guarantee."
)

if "legs" not in st.session_state:
    st.session_state.legs = [{"stat": "PTS", "line": 20.0, "direction": "over"}]
if "sgp_legs" not in st.session_state:
    st.session_state.sgp_legs = [{"player": None, "stat": "PTS", "line": 20.0, "direction": "over"}]

if len(seasons) > 1:
    season = st.selectbox(
        "Season", seasons, index=0,
        help="Most recent season is selected by default (includes this year's rookies, "
             "but has fewer games played so far). Earlier seasons have a full sample.",
    )
else:
    season = seasons[0] if seasons else None

STAT_LABELS = {"PTS": "Points", "REB": "Rebounds", "AST": "Assists", "STL": "Steals", "BLK": "Blocks", "PRA": "Points+Rebounds+Assists"}

tab_single, tab_sgp = st.tabs(["Single player", "Same-game parlay"])

with tab_single:
    section("Who and against whom?")

    team_filter_choices = ["All players"] + team_labels
    team_filter_label = st.selectbox(
        "Narrow the player list by team (optional)", team_filter_choices,
        key="single_team_filter",
        help="Pick a team to shorten the Player list below — handy on a phone.",
    )
    if team_filter_label != "All players" and season:
        filter_abbr = team_abbr_by_label[team_filter_label]
        player_options = players_by_season_team.get(season, {}).get(filter_abbr, players)
    else:
        player_options = players

    col1, col2 = st.columns(2)
    with col1:
        player = st.selectbox(
            "Player", player_options, index=None, placeholder="Type to search...", key="single_player",
        )
    with col2:
        team_label = st.selectbox("Opponent", team_labels, index=None, placeholder="Pick a team...", key="single_opponent")

    col3, col4 = st.columns(2)
    with col3:
        home_away = st.radio("Where's the game?", ["Not specified", "Home", "Away"], horizontal=True, key="single_home_away")
    with col4:
        recent_n = recent_games_control("Recent games to weight", key="single_recent_n")

    section("Prop(s)")
    st.caption("Add one or more props for the same player/game — multiple legs are checked as a combo.")

    for i, leg in enumerate(st.session_state.legs):
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
            with c1:
                leg["stat"] = st.selectbox(
                    "Stat", list(STAT_LABELS), format_func=lambda s: STAT_LABELS[s],
                    key=f"stat_{i}", index=list(STAT_LABELS).index(leg["stat"]),
                )
            with c2:
                leg["line"] = st.number_input("Line", key=f"line_{i}", value=leg["line"], step=0.5)
            with c3:
                leg["direction"] = st.selectbox(
                    "Direction", ["over", "under"], key=f"dir_{i}",
                    index=["over", "under"].index(leg["direction"]),
                )
            with c4:
                st.write("")  # vertical alignment spacer
                if len(st.session_state.legs) > 1:
                    if st.button("✕", key=f"remove_{i}", use_container_width=True):
                        st.session_state.legs.pop(i)
                        st.rerun()

    if st.button("+ Add another prop", key="add_prop"):
        st.session_state.legs.append({"stat": "PTS", "line": 20.0, "direction": "over"})
        st.rerun()

    st.write("")
    if st.button("Check pick(s)", type="primary", use_container_width=True, key="check_single"):
        if not player or not team_label:
            st.warning("Pick a player and an opponent first.")
        else:
            opponent_abbr = team_abbr_by_label[team_label]
            is_home = {"Not specified": None, "Home": True, "Away": False}[home_away]

            conn = _connect()
            results = []
            try:
                for leg in st.session_state.legs:
                    r = estimate_prop_probability(
                        player, leg["stat"], leg["line"], opponent_abbr,
                        direction=leg["direction"], is_home=is_home,
                        recent_n=recent_n, conn=conn, season=season,
                    )
                    results.append(r)
            except ValueError as e:
                st.error(str(e))
                results = []
            finally:
                conn.close()

            for r in results:
                render_result(r)

            if len(results) > 1:
                combined = combine_probabilities(results)
                st.markdown(f"## Combined: {combined:.1%}")
                st.caption(
                    "Assumes independence between legs — real stats are often correlated, "
                    "so treat this as a rough estimate, not a precise joint probability."
                )

with tab_sgp:
    section("Which game?")
    st.caption(
        "Pick the two teams playing each other, then add players from EITHER "
        "team below — each player's opponent is figured out automatically."
    )
    colA, colB = st.columns(2)
    with colA:
        team_a_label = st.selectbox("Team A", team_labels, index=None, placeholder="Pick a team...", key="sgp_team_a")
    with colB:
        team_b_label = st.selectbox("Team B", team_labels, index=None, placeholder="Pick the other team...", key="sgp_team_b")

    col5, col6 = st.columns(2)
    with col5:
        home_choices = ["Not specified"] + [t for t in (team_a_label, team_b_label) if t]
        # Team A/B can change between reruns, which changes this dropdown's
        # valid options — if the previously selected value isn't among the
        # new options, Streamlit raises rather than falling back silently,
        # so reset it here before the widget is created.
        if st.session_state.get("sgp_home_team") not in home_choices:
            st.session_state["sgp_home_team"] = "Not specified"
        home_label = st.selectbox("Which team is home? (optional)", home_choices, key="sgp_home_team")
    with col6:
        sgp_recent_n = recent_games_control("Recent games to weight", key="sgp_recent_n")

    # Once both teams are picked, every leg's player list is narrowed to
    # just those two rosters — no reason to search a ~600-player list
    # when only ~30 players could possibly be in this game.
    sgp_player_options = players
    if team_a_label and team_b_label and season:
        abbr_a, abbr_b = team_abbr_by_label[team_a_label], team_abbr_by_label[team_b_label]
        by_team = players_by_season_team.get(season, {})
        narrowed = sorted(set(by_team.get(abbr_a, [])) | set(by_team.get(abbr_b, [])))
        if narrowed:
            sgp_player_options = narrowed

    section("Legs — add a player from either team")

    for i, leg in enumerate(st.session_state.sgp_legs):
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
            with c1:
                leg["player"] = st.selectbox(
                    "Player", sgp_player_options,
                    index=(sgp_player_options.index(leg["player"]) if leg["player"] in sgp_player_options else None),
                    placeholder="Type to search..." if sgp_player_options is not players else "Pick both teams first...",
                    key=f"sgp_player_{i}",
                )
            with c2:
                leg["stat"] = st.selectbox(
                    "Stat", list(STAT_LABELS), format_func=lambda s: STAT_LABELS[s],
                    key=f"sgp_stat_{i}", index=list(STAT_LABELS).index(leg["stat"]),
                )
            with c3:
                leg["line"] = st.number_input("Line", key=f"sgp_line_{i}", value=leg["line"], step=0.5)
            with c4:
                leg["direction"] = st.selectbox(
                    "Direction", ["over", "under"], key=f"sgp_dir_{i}",
                    index=["over", "under"].index(leg["direction"]),
                )
            if len(st.session_state.sgp_legs) > 1:
                if st.button("Remove this leg", key=f"sgp_remove_{i}", use_container_width=True):
                    st.session_state.sgp_legs.pop(i)
                    st.rerun()

    if st.button("+ Add another player/prop", key="sgp_add_leg"):
        st.session_state.sgp_legs.append({"player": None, "stat": "PTS", "line": 20.0, "direction": "over"})
        st.rerun()

    st.write("")
    if st.button("Check same-game parlay", type="primary", use_container_width=True, key="check_sgp"):
        if not team_a_label or not team_b_label:
            st.warning("Pick both teams first.")
        elif team_a_label == team_b_label:
            st.warning("Team A and Team B need to be different teams.")
        elif any(not leg["player"] for leg in st.session_state.sgp_legs):
            st.warning("Every leg needs a player.")
        else:
            team_a_abbr = team_abbr_by_label[team_a_label]
            team_b_abbr = team_abbr_by_label[team_b_label]
            home_team_abbr = team_abbr_by_label[home_label] if home_label != "Not specified" else None

            conn = _connect()
            results, combined = [], None
            try:
                results, combined = estimate_same_game_parlay(
                    st.session_state.sgp_legs, team_a_abbr, team_b_abbr,
                    home_team_abbr=home_team_abbr, recent_n=sgp_recent_n, conn=conn, season=season,
                )
            except ValueError as e:
                st.error(str(e))
                results = []
            finally:
                conn.close()

            for r in results:
                render_result(r)

            if results and combined is not None:
                st.markdown(f"## Combined: {combined:.1%}")
                st.caption(
                    "Assumes independence between legs — same-game props across different "
                    "players are often even MORE correlated than multiple props on one player, "
                    "so treat this as a rough estimate, not a precise joint probability."
                )
