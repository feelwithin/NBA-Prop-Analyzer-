"""
A simple web UI over the prop model — for sharing with people who
don't want to use a terminal. Styled after a sportsbook-style prop
picker (FanDuel-inspired): pill-style Over/Under buttons, a "slip"
card that collects your picks, and player avatars.

Run locally:
    pip install streamlit
    streamlit run app.py

Deploy for free (so a friend can just click a link):
    See "Share it with a friend" in README.md — Streamlit Community
    Cloud deploys this directly from your GitHub repo.

Branding: colors/theme live in .streamlit/config.toml (Streamlit's
native theming — applies to every built-in widget automatically), and
the logo is assets/logo.png. To rebrand, edit those two things and
the ACCENT constant below; the layout code doesn't hardcode colors
anywhere else.
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
    player_window_stat_avg, player_vs_opponent_stat_avg, player_recent_games,
    _player_team_in_season, _connect, DB_PATH, list_seasons,
)

LOGO_PATH = ROOT / "assets" / "logo.png"
ACCENT = "#1D6FFF"  # keep in sync with primaryColor in .streamlit/config.toml

st.set_page_config(
    page_title="Nabil's Prop Analyzer",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "🏀",
    layout="centered",
)

# A few CSS touches native theming doesn't cover: pill-shaped buttons
# (the FanDuel-style Over/Under toggles and slip actions), a "slip"
# card look, and mobile spacing (16px form inputs prevent iOS Safari's
# auto-zoom-on-focus; columns wrap instead of squeezing on a phone).
st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 1.5rem; max-width: 760px; }}
      div[data-testid="stHorizontalBlock"] {{ flex-wrap: wrap; gap: 0.6rem; }}
      input, select, textarea {{ font-size: 16px !important; }}
      .stButton > button {{
          border-radius: 999px !important;
          font-weight: 700 !important;
          letter-spacing: 0.02em;
      }}
      .section-title {{
          font-size: 0.95rem; font-weight: 700; letter-spacing: 0.04em;
          text-transform: uppercase; opacity: 0.75;
          border-left: 3px solid {ACCENT}; padding-left: 10px;
          margin: 1.3rem 0 0.7rem 0;
      }}
      .slip-title {{
          font-size: 0.8rem; font-weight: 800; letter-spacing: 0.08em;
          text-transform: uppercase; color: {ACCENT};
          margin-bottom: 0.5rem;
      }}
      .app-tagline {{ font-size: 0.92rem; opacity: 0.65; margin-top: -0.2rem; }}
      .avatar-wrap {{ position: relative; width: 40px; height: 40px; flex-shrink: 0; }}
      .avatar-fallback {{
          position: absolute; inset: 0; border-radius: 50%;
          background: {ACCENT}; color: white; font-weight: 800; font-size: 0.8rem;
          display: flex; align-items: center; justify-content: center;
      }}
      .avatar-img {{
          position: absolute; inset: 0; width: 40px; height: 40px;
          border-radius: 50%; object-fit: cover; background: transparent;
      }}
      @media (max-width: 480px) {{
          .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
      }}
    </style>
    """,
    unsafe_allow_html=True,
)


def html(s):
    """Render a custom HTML/CSS string via st.markdown. Collapses all
    whitespace runs (including newlines) to single spaces first —
    Markdown's HTML-block parsing ends at a blank line, and a blank
    line anywhere inside a multi-line f-string (e.g. one produced by
    nesting another helper's multi-line return value) silently breaks
    the block partway through, so the rest renders as literal escaped
    text instead of HTML. Collapsing whitespace up front makes that
    failure mode structurally impossible, regardless of how the
    string was assembled or indented in the source. Safe for both
    HTML fragments and <style> blocks — neither cares about
    whitespace between tokens."""
    st.markdown(" ".join(s.split()), unsafe_allow_html=True)


def section(title):
    html(f"<div class='section-title'>{title}</div>")


def initials(name):
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def avatar_html(player_id, name, size=40):
    """A small circular avatar: tries the real NBA CDN headshot for this
    player_id (works for real players fetched via fetch_data.py — that ID
    is the NBA's own person ID), falling back to an initials badge if the
    image 404s (always true for the synthetic demo dataset's fake IDs, or
    a player without a published headshot). The onerror handler hides the
    broken <img> so the fallback div underneath shows through — no JS
    framework needed, just a plain HTML attribute."""
    url = f"https://cdn.nba.com/headshots/nba/latest/1040x760/{player_id}.png"
    raw = f"""
    <div class="avatar-wrap" style="width:{size}px;height:{size}px;">
      <div class="avatar-fallback" style="width:{size}px;height:{size}px;font-size:{size*0.4:.0f}px;">{initials(name)}</div>
      <img class="avatar-img" style="width:{size}px;height:{size}px;" src="{url}" onerror="this.style.display='none'" />
    </div>
    """
    # Collapsed defensively here too — this string is often embedded
    # inside another multi-line f-string by callers, and a blank line
    # anywhere in the combined markup breaks unsafe_allow_html rendering
    # (see the `html()` helper above for the full explanation).
    return " ".join(raw.split())


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
    player_rows = conn.execute("SELECT player_id, full_name FROM players ORDER BY full_name").fetchall()
    players = [r["full_name"] for r in player_rows]
    player_id_by_name = {r["full_name"]: r["player_id"] for r in player_rows}
    team_rows = conn.execute("SELECT team_id, abbreviation, team_name FROM teams ORDER BY team_name").fetchall()
    teams = [(r["abbreviation"], r["team_name"]) for r in team_rows]
    team_id_by_abbr = {r["abbreviation"]: r["team_id"] for r in team_rows}
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
    return players, player_id_by_name, teams, team_id_by_abbr, seasons, by_season_team


@st.cache_data
def _window_stat_avg(player_id, stat, season, recent_n):
    """Cached wrapper around prop_model.player_window_stat_avg — opens
    its own short-lived connection (rather than taking one as an
    argument) so Streamlit can cache on the plain, hashable arguments.
    Cheap enough to call on every rerun (every widget click reruns the
    whole script), but caching avoids re-querying identical
    (player, stat, season, window) combos as the user clicks around."""
    conn = _connect()
    try:
        return player_window_stat_avg(conn, player_id, stat, season, recent_n)
    finally:
        conn.close()


@st.cache_data
def _vs_opponent_stat_avg(player_id, stat, opponent_team_id, season):
    conn = _connect()
    try:
        return player_vs_opponent_stat_avg(conn, player_id, stat, opponent_team_id, season)
    finally:
        conn.close()


@st.cache_data
def _resolve_player_team(player_id, season):
    conn = _connect()
    try:
        return _player_team_in_season(conn, player_id, season)
    finally:
        conn.close()


@st.cache_data
def _recent_games(player_id, stat, season, recent_n):
    conn = _connect()
    try:
        return player_recent_games(conn, player_id, stat, season, recent_n)
    finally:
        conn.close()


def recent_games_chart_html(stat, player_id, season, recent_n, line=None, direction="over"):
    """A StatMuse-style bar chart of the player's last `recent_n` games
    for `stat` — one bar per game, oldest to newest, colored green/red
    against the current line and pick direction when a line is set:
    green means that game would have HIT the pick (value >= line for
    an over, value < line for an under), red means it would have
    missed — so the coloring flips depending on which side of the pick
    you're looking at, not just whether the bar is above or below the
    line. Games where the player played noticeably fewer minutes than
    their norm in this window are drawn faded with a "·Xm" note under
    the bar, so a short bar reads as "blowout, sat the 4th" rather than
    "having a bad stretch" — the same context StatMuse shows next to
    its game logs. Returns None if the player has no games logged yet
    this season."""
    games = _recent_games(player_id, stat, season, recent_n)
    if not games:
        return None

    label = STAT_LABELS[stat]
    values = [g["value"] for g in games]
    minutes = [g["minutes"] for g in games]
    avg_minutes = sum(minutes) / len(minutes)
    vmax = max(values + ([line] if line is not None else [])) or 1

    bar_w, gap = 30, 12
    plot_h = 84
    top_pad, bottom_pad = 22, 34
    chart_w = len(games) * (bar_w + gap) + gap
    chart_h = top_pad + plot_h + bottom_pad

    bars_svg = []
    line_svg = ""
    if line is not None:
        line_y = top_pad + plot_h - (min(line, vmax) / vmax) * plot_h
        line_svg = f"""
            <line x1="0" y1="{line_y:.1f}" x2="{chart_w}" y2="{line_y:.1f}"
                  stroke="#9AA5B1" stroke-width="1.5" stroke-dasharray="4,4" />
            <text x="4" y="{line_y - 5:.1f}" font-size="10" fill="#9AA5B1" font-weight="700">
                Line {line:g}
            </text>
        """

    for i, g in enumerate(games):
        x = gap + i * (bar_w + gap)
        h = (g["value"] / vmax) * plot_h if vmax else 0
        y = top_pad + plot_h - h
        low_minutes = g["minutes"] < avg_minutes * 0.7
        if line is not None:
            hit = g["value"] >= line if direction == "over" else g["value"] < line
            color = "#2ECC71" if hit else "#FF5C5C"
        else:
            color = ACCENT
        opacity = 0.45 if low_minutes else 1.0
        opp_label = ("vs " if g["is_home"] else "@ ") + (g["opponent_abbr"] or "?")
        min_note = f'<tspan fill="#E8A33D">·{g["minutes"]}m</tspan>' if low_minutes else f'{g["minutes"]}m'
        bars_svg.append(f"""
            <g>
              <rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{max(h, 2):.1f}" rx="6"
                    fill="{color}" fill-opacity="{opacity}" />
              <text x="{x + bar_w/2}" y="{y - 6:.1f}" font-size="11" font-weight="800"
                    fill="currentColor" text-anchor="middle">{g["value"]:g}</text>
              <text x="{x + bar_w/2}" y="{top_pad + plot_h + 14}" font-size="9" fill="#9AA5B1"
                    text-anchor="middle">{opp_label}</text>
              <text x="{x + bar_w/2}" y="{top_pad + plot_h + 26}" font-size="9" fill="#9AA5B1"
                    text-anchor="middle">{min_note}</text>
            </g>
        """)

    any_low_minutes = any(g["minutes"] < avg_minutes * 0.7 for g in games)
    footnote = (
        f'<div style="font-size:0.72rem;opacity:0.55;margin-top:2px;">'
        f'<span style="color:#E8A33D;">●</span> faded bar = well below their usual minutes that game '
        f'(blowout, rest, foul trouble, etc.) — {label.lower()} total may not reflect a normal workload</div>'
        if any_low_minutes else ""
    )

    return f"""
        <div style="margin:-0.2rem 0 0.6rem 0;">
          <div style="overflow-x:auto;">
            <svg width="{chart_w}" height="{chart_h}" viewBox="0 0 {chart_w} {chart_h}"
                 style="display:block;min-width:{chart_w}px;">
              {line_svg}
              {''.join(bars_svg)}
            </svg>
          </div>
          {footnote}
        </div>
    """


def stat_snapshot_line(stat, player_id, season, recent_n, opponent_team_id=None, opponent_abbr=None):
    """One line of text for the live stats preview: the player's plain
    average for `stat` over their last `recent_n` games this season,
    plus — when an opponent is known — their average in games actually
    played against that specific opponent this season. Returns None if
    the player has no games logged yet this season (nothing useful to
    show)."""
    label = STAT_LABELS[stat].lower()
    window_avg, window_n = _window_stat_avg(player_id, stat, season, recent_n)
    if window_avg is None:
        return None
    unit = "game" if window_n == 1 else "games"
    parts = [f"Last {window_n} {unit}: <b>{window_avg:.1f}</b> {label}/gm"]

    if opponent_team_id is not None:
        vs_avg, vs_n = _vs_opponent_stat_avg(player_id, stat, opponent_team_id, season)
        if vs_avg is not None:
            vs_unit = "game" if vs_n == 1 else "games"
            parts.append(f"vs {opponent_abbr} this season: <b>{vs_avg:.1f}</b> {label}/gm ({vs_n} {vs_unit})")
        else:
            parts.append(f"vs {opponent_abbr} this season: no meetings yet")

    return " &nbsp;•&nbsp; ".join(parts)


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


def direction_pills(leg, key_prefix):
    """FanDuel-style OVER/UNDER toggle: two pill buttons, the selected
    one filled solid (Streamlit's 'primary' button type, themed to
    ACCENT), the other outlined ('secondary'). Clicking sets the leg's
    direction and reruns immediately so the highlighted pill updates
    in the same tap — same pattern already used for add/remove buttons
    elsewhere on this page."""
    c_over, c_under = st.columns(2)
    with c_over:
        if st.button(
            "OVER", key=f"{key_prefix}_over", use_container_width=True,
            type=("primary" if leg["direction"] == "over" else "secondary"),
        ):
            leg["direction"] = "over"
            st.rerun()
    with c_under:
        if st.button(
            "UNDER", key=f"{key_prefix}_under", use_container_width=True,
            type=("primary" if leg["direction"] == "under" else "secondary"),
        ):
            leg["direction"] = "under"
            st.rerun()


def render_result(r):
    verdict = "LIKELY HIT" if r.probability >= 0.5 else "LIKELY MISS"
    verdict_color = "#2ECC71" if r.probability >= 0.5 else "#FF5C5C"
    confidence_level = r.confidence.split(" ")[0]  # e.g. "low (small sample)" -> "low"

    html(
        f"""
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:0.3rem;">
          {avatar_html(r.player_id, r.player_name, size=44)}
          <div style="flex:1;min-width:0;">
            <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:0.4rem;">
              <div style="font-weight:700;font-size:1.05rem;">
                {r.player_name} <span style="opacity:0.55;font-weight:400;">({r.season})</span>
              </div>
              <div style="color:{verdict_color};font-weight:700;font-size:0.85rem;letter-spacing:0.03em;">
                {verdict}
              </div>
            </div>
            <div style="opacity:0.7;font-size:0.9rem;">
              {STAT_LABELS[r.stat]} {r.direction.upper()} {r.line} vs {r.opponent_abbr}
            </div>
          </div>
        </div>
        """
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
players, player_id_by_name, teams, team_id_by_abbr, seasons, players_by_season_team = load_options()
team_labels = [f"{name} ({abbr})" for abbr, name in teams]
team_abbr_by_label = {f"{name} ({abbr})": abbr for abbr, name in teams}

# ---- Header ----
if LOGO_PATH.exists():
    logo_b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()
    html(
        f"""
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:0.15rem;">
          <img src="data:image/png;base64,{logo_b64}" style="width:52px;height:52px;border-radius:50%;flex-shrink:0;" />
          <div>
            <div style="font-size:1.65rem;font-weight:800;line-height:1.15;">Nabil's Prop Analyzer</div>
            <div class="app-tagline">NBA player prop probabilities, matchup-adjusted</div>
          </div>
        </div>
        """
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

    if player:
        html(
            f"""<div style="display:flex;align-items:center;gap:10px;margin:-0.3rem 0 0.6rem 0;">
                  {avatar_html(player_id_by_name[player], player, size=32)}
                  <span style="opacity:0.75;font-size:0.9rem;">{player}</span>
                </div>"""
        )

    col3, col4 = st.columns(2)
    with col3:
        home_away = st.radio("Where's the game?", ["Not specified", "Home", "Away"], horizontal=True, key="single_home_away")
    with col4:
        recent_n = recent_games_control("Recent games to weight", key="single_recent_n")

    section("Your slip")
    st.caption("Add one or more props for the same player/game — multiple legs are checked as a combo.")

    with st.container(border=True):
        html(f"<div class='slip-title'>🎟️ {len(st.session_state.legs)} pick(s) on this slip</div>")
        for i, leg in enumerate(st.session_state.legs):
            if i > 0:
                st.divider()
            c1, c2 = st.columns([3, 2])
            with c1:
                leg["stat"] = st.selectbox(
                    "Stat", list(STAT_LABELS), format_func=lambda s: STAT_LABELS[s],
                    key=f"stat_{i}", index=list(STAT_LABELS).index(leg["stat"]),
                )
            with c2:
                leg["line"] = st.number_input("Line", key=f"line_{i}", value=leg["line"], step=0.5)

            if player and season:
                opponent_team_id = team_id_by_abbr.get(team_abbr_by_label.get(team_label)) if team_label else None
                chart = recent_games_chart_html(
                    leg["stat"], player_id_by_name[player], season, recent_n,
                    line=leg["line"], direction=leg["direction"],
                )
                if chart:
                    html(chart)
                snapshot = stat_snapshot_line(
                    leg["stat"], player_id_by_name[player], season, recent_n,
                    opponent_team_id=opponent_team_id, opponent_abbr=team_abbr_by_label.get(team_label),
                )
                if snapshot:
                    html(f"<div style='font-size:0.82rem;opacity:0.7;margin:-0.3rem 0 0.6rem 0;'>{snapshot}</div>")

            direction_pills(leg, key_prefix=f"single_{i}")
            if len(st.session_state.legs) > 1:
                if st.button("Remove this leg", key=f"remove_{i}", use_container_width=True):
                    st.session_state.legs.pop(i)
                    st.rerun()

        st.write("")
        if st.button("+ Add another prop", key="add_prop", use_container_width=True):
            st.session_state.legs.append({"stat": "PTS", "line": 20.0, "direction": "over"})
            st.rerun()

        st.write("")
        submitted_single = st.button("Check pick(s)", type="primary", use_container_width=True, key="check_single")

    if submitted_single:
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

    section("Your slip")
    st.caption("Add players from either roster — mix and match for the parlay.")

    with st.container(border=True):
        html(f"<div class='slip-title'>🎟️ {len(st.session_state.sgp_legs)} pick(s) on this slip</div>")
        for i, leg in enumerate(st.session_state.sgp_legs):
            if i > 0:
                st.divider()
            leg["player"] = st.selectbox(
                "Player", sgp_player_options,
                index=(sgp_player_options.index(leg["player"]) if leg["player"] in sgp_player_options else None),
                placeholder="Type to search..." if sgp_player_options is not players else "Pick both teams first...",
                key=f"sgp_player_{i}",
            )
            if leg["player"]:
                html(
                    f"""<div style="display:flex;align-items:center;gap:10px;margin:-0.3rem 0 0.5rem 0;">
                          {avatar_html(player_id_by_name[leg['player']], leg['player'], size=32)}
                          <span style="opacity:0.75;font-size:0.9rem;">{leg['player']}</span>
                        </div>"""
                )
            c2, c3 = st.columns(2)
            with c2:
                leg["stat"] = st.selectbox(
                    "Stat", list(STAT_LABELS), format_func=lambda s: STAT_LABELS[s],
                    key=f"sgp_stat_{i}", index=list(STAT_LABELS).index(leg["stat"]),
                )
            with c3:
                leg["line"] = st.number_input("Line", key=f"sgp_line_{i}", value=leg["line"], step=0.5)

            if leg["player"] and team_a_label and team_b_label and season:
                team_a_id = team_id_by_abbr[team_abbr_by_label[team_a_label]]
                team_b_id = team_id_by_abbr[team_abbr_by_label[team_b_label]]
                own_team_id = _resolve_player_team(player_id_by_name[leg["player"]], season)
                opponent_team_id = team_b_id if own_team_id == team_a_id else team_a_id
                opponent_abbr = next(abbr for abbr, tid in team_id_by_abbr.items() if tid == opponent_team_id)
                chart = recent_games_chart_html(
                    leg["stat"], player_id_by_name[leg["player"]], season, sgp_recent_n,
                    line=leg["line"], direction=leg["direction"],
                )
                if chart:
                    html(chart)
                snapshot = stat_snapshot_line(
                    leg["stat"], player_id_by_name[leg["player"]], season, sgp_recent_n,
                    opponent_team_id=opponent_team_id, opponent_abbr=opponent_abbr,
                )
                if snapshot:
                    html(f"<div style='font-size:0.82rem;opacity:0.7;margin:-0.3rem 0 0.6rem 0;'>{snapshot}</div>")

            direction_pills(leg, key_prefix=f"sgp_{i}")
            if len(st.session_state.sgp_legs) > 1:
                if st.button("Remove this leg", key=f"sgp_remove_{i}", use_container_width=True):
                    st.session_state.sgp_legs.pop(i)
                    st.rerun()

        st.write("")
        if st.button("+ Add another player/prop", key="sgp_add_leg", use_container_width=True):
            st.session_state.sgp_legs.append({"player": None, "stat": "PTS", "line": 20.0, "direction": "over"})
            st.rerun()

        st.write("")
        submitted_sgp = st.button("Check same-game parlay", type="primary", use_container_width=True, key="check_sgp")

    if submitted_sgp:
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
