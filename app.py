"""
A simple web UI over the prop model — for sharing with people who
don't want to use a terminal.

Run locally:
    pip install streamlit
    streamlit run app.py

Deploy for free (so a friend can just click a link):
    See "Share it with a friend" in README.md — Streamlit Community
    Cloud deploys this directly from your GitHub repo.
"""
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

st.set_page_config(page_title="NBA Prop Analyzer", page_icon="🏀", layout="centered")


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
    conn.close()
    return players, teams, seasons


ensure_database()
players, teams, seasons = load_options()
team_labels = [f"{name} ({abbr})" for abbr, name in teams]
team_abbr_by_label = {f"{name} ({abbr})": abbr for abbr, name in teams}

st.title("🏀 NBA Prop Analyzer")
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


def render_result(r):
    verdict = "LIKELY HIT ✅" if r.probability >= 0.5 else "LIKELY MISS ❌"
    # confidence is e.g. "low (small sample)" — split off the level so
    # the metric tile doesn't truncate; the detail goes in the note below
    confidence_level = r.confidence.split(" ")[0]
    st.markdown(f"### {r.player_name} ({r.season}) — {STAT_LABELS[r.stat]} {r.direction.upper()} {r.line} vs {r.opponent_abbr}")
    m1, m2, m3 = st.columns(3)
    m1.metric("Probability", f"{r.probability:.1%}", verdict)
    m2.metric("Confidence", confidence_level.capitalize())
    m3.metric("Historical hit rate", f"{r.historical_hit_rate:.0%}", f"last {r.sample_size} games")
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


tab_single, tab_sgp = st.tabs(["Single player", "Same-game parlay"])

with tab_single:
    st.subheader("Who and against whom?")
    col1, col2 = st.columns(2)
    with col1:
        player = st.selectbox("Player", players, index=None, placeholder="Start typing a name...", key="single_player")
    with col2:
        team_label = st.selectbox("Opponent", team_labels, index=None, placeholder="Pick a team...", key="single_opponent")

    col3, col4 = st.columns(2)
    with col3:
        home_away = st.radio("Where's the game?", ["Not specified", "Home", "Away"], horizontal=True, key="single_home_away")
    with col4:
        recent_n = st.slider("Recent games to weight", min_value=5, max_value=40, value=20, key="single_recent_n")

    st.subheader("Prop(s)")
    st.caption("Add one or more props for the same player/game — multiple legs are checked as a combo.")

    for i, leg in enumerate(st.session_state.legs):
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
            if len(st.session_state.legs) > 1:
                if st.button("✕", key=f"remove_{i}"):
                    st.session_state.legs.pop(i)
                    st.rerun()

    col_add, _ = st.columns([1, 3])
    with col_add:
        if st.button("+ Add another prop", key="add_prop"):
            st.session_state.legs.append({"stat": "PTS", "line": 20.0, "direction": "over"})
            st.rerun()

    st.divider()

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
    st.subheader("Which game?")
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
        sgp_recent_n = st.slider("Recent games to weight", min_value=5, max_value=40, value=20, key="sgp_recent_n")

    st.subheader("Legs — add a player from either team")

    for i, leg in enumerate(st.session_state.sgp_legs):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
        with c1:
            leg["player"] = st.selectbox(
                "Player", players, index=(players.index(leg["player"]) if leg["player"] in players else None),
                placeholder="Start typing a name...", key=f"sgp_player_{i}",
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
        with c5:
            if len(st.session_state.sgp_legs) > 1:
                if st.button("✕", key=f"sgp_remove_{i}"):
                    st.session_state.sgp_legs.pop(i)
                    st.rerun()

    col_add2, _ = st.columns([1, 3])
    with col_add2:
        if st.button("+ Add another player/prop", key="sgp_add_leg"):
            st.session_state.sgp_legs.append({"player": None, "stat": "PTS", "line": 20.0, "direction": "over"})
            st.rerun()

    st.divider()

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
