from __future__ import annotations

from typing import Any, Dict, List


def augment_report_context_with_quarters_and_runs(
    ctx: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    ctx = dict(ctx)
    ctx.setdefault("quarters_and_runs", {})
    qa = ctx["quarters_and_runs"]

    qa["quarter_team_points"] = analysis.get("quarter_team_points") or {}
    qa["quarter_team_totals"] = analysis.get("quarter_team_totals") or {}
    qa["quarter_player_totals"] = analysis.get("quarter_player_totals") or {}
    qa["unanswered_runs"] = analysis.get("unanswered_runs") or []
    qa["net_runs"] = analysis.get("net_runs") or []
    qa["highlight_runs"] = analysis.get("highlight_runs") or []
    qa["teams"] = analysis.get("teams") or {}
    qa["game_id"] = analysis.get("game_id")
    qa["date"] = analysis.get("date")
    qa["season"] = analysis.get("season")
    return ctx


def render_quarters_and_runs_html_section(
    analysis: Dict[str, Any],
) -> str:
    teams = analysis.get("teams") or {}
    home = teams.get("home", {})
    away = teams.get("away", {})

    quarter_team_totals = analysis.get("quarter_team_totals") or {}
    quarter_player_totals = analysis.get("quarter_player_totals") or {}
    unanswered_runs = analysis.get("unanswered_runs") or []
    net_runs = analysis.get("net_runs") or []
    highlight_runs = analysis.get("highlight_runs") or []

    home_name = home.get("name", "Home")
    away_name = away.get("name", "Away")

    html: List[str] = []
    html.append('<section class="qr-section">')
    html.append("  <h2>Quarter-by-Quarter Breakdown</h2>")

    # TEAM TOTALS TABLE
    html.append("  <h3>Team totals by quarter</h3>")
    html.append('  <table class="qr-table qr-team-quarters">')
    html.append("    <thead>")
    html.append(
        "      <tr>"
        "<th>Quarter</th>"
        f"<th>{away_name} PTS (FGM/FGA, 3PM/3PA, FTM/FTA)</th>"
        f"<th>{home_name} PTS (FGM/FGA, 3PM/3PA, FTM/FTA)</th>"
        "</tr>"
    )
    html.append("    </thead>")
    html.append("    <tbody>")

    # quarter keys might be int or string; normalize via int sort but access both
    for q in sorted({int(k) for k in quarter_team_totals.keys()}):
        q_tot = quarter_team_totals.get(str(q)) or quarter_team_totals.get(q) or {}
        away_tot = q_tot.get("away", {})
        home_tot = q_tot.get("home", {})

        def fmt_team_row(t: Dict[str, int]) -> str:
            pts = t.get("pts", 0)
            fgm = t.get("fgm", 0)
            fga = t.get("fga", 0)
            tpm = t.get("tpm", 0)
            tpa = t.get("tpa", 0)
            ftm = t.get("ftm", 0)
            fta = t.get("fta", 0)
            return f"{pts} ({fgm}/{fga}, {tpm}/{tpa}, {ftm}/{fta})"

        html.append("      <tr>")
        html.append(f"        <td>Q{q}</td>")
        html.append(f"        <td>{fmt_team_row(away_tot)}</td>")
        html.append(f"        <td>{fmt_team_row(home_tot)}</td>")
        html.append("      </tr>")

    html.append("    </tbody>")
    html.append("  </table>")

    # PLAYER HIGHLIGHTS BY QUARTER (TOP SCORERS)
    html.append("  <h3>Top scorers by quarter</h3>")
    html.append('  <table class="qr-table qr-player-quarters">')
    html.append("    <thead>")
    html.append(
        "      <tr>"
        "<th>Quarter</th>"
        f"<th>{away_name} – top scorers</th>"
        f"<th>{home_name} – top scorers</th>"
        "</tr>"
    )
    html.append("    </thead>")
    html.append("    <tbody>")

    for q in sorted({int(k) for k in quarter_player_totals.keys()}):
        pmap = quarter_player_totals.get(str(q)) or quarter_player_totals.get(q) or {}
        away_players = []
        home_players = []
        for pid, pdata in pmap.items():
            side = pdata.get("side")
            name = pdata.get("name") or f"Player {pid}"
            pts = pdata.get("pts", 0)
            if pts <= 0:
                continue
            desc = f"{name} – {pts} pts"
            if side == "away":
                away_players.append((pts, desc))
            elif side == "home":
                home_players.append((pts, desc))

        away_players.sort(key=lambda x: (-x[0], x[1]))
        home_players.sort(key=lambda x: (-x[0], x[1]))

        away_str = ", ".join(d for _, d in away_players[:3]) if away_players else ""
        home_str = ", ".join(d for _, d in home_players[:3]) if home_players else ""

        html.append("      <tr>")
        html.append(f"        <td>Q{q}</td>")
        html.append(f"        <td>{away_str}</td>")
        html.append(f"        <td>{home_str}</td>")
        html.append("      </tr>")

    html.append("    </tbody>")
    html.append("  </table>")

    # RUNS
    html.append("  <h3>Scoring runs</h3>")
    html.append('  <div class="qr-runs">')

    def fmt_run_side(side: str) -> str:
        return away_name if side == "away" else home_name

    def render_run_list(title: str, runs: list[dict[str, Any]]) -> None:
        html.append('    <div class="qr-runs-block">')
        html.append(f"      <h4>{title}</h4>")
        if not runs:
            html.append("      <p>No runs meeting the threshold.</p>")
            html.append("    </div>")
            return
        html.append('      <ul class="qr-runs-list">')
        for r in runs:
            side = r.get("side")
            team_label = fmt_run_side(side) if side in ("home", "away") else "Team"
            if "points" in r:
                desc_pts = f"{r.get('points', 0)}-0 or better"
            elif "points_for" in r:
                desc_pts = (
                    f"{r.get('points_for', 0)}-{r.get('points_against', 0)} "
                    f"(net {r.get('net_points', 0)})"
                )
            else:
                desc_pts = f"net +{r.get('net_points', 0)}"

            start_q = r.get("start_period")
            end_q = r.get("end_period")
            start_clk = r.get("start_clock")
            end_clk = r.get("end_clock")

            span = f"Q{start_q} {start_clk} → Q{end_q} {end_clk}"
            html.append(
                f"        <li><strong>{team_label}</strong>: {desc_pts} – {span}</li>"
            )
        html.append("      </ul>")
        html.append("    </div>")

    render_run_list("7+ unanswered runs", unanswered_runs)
    render_run_list("8+ net runs (scoreboard margin)", net_runs)
    render_run_list("Highlight runs (>=8 pts, opp ≤5)", highlight_runs)

    html.append("  </div>")
    html.append("</section>")
    return "\n".join(html)
