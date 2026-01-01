import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

LOG = logging.getLogger("dt_game_report.generate_report")

# Ensure we can import dt_game_report when running this file directly
THIS_DIR = Path(__file__).resolve().parent
PARENT_DIR = THIS_DIR.parent  # .../src
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from dt_game_report.quarters_and_runs_analysis import analyze_quarters_and_runs

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
TEMPLATES_DIR = REPO_ROOT / "templates"
SITE_DIR = REPO_ROOT / "site"
REPORTS_DIR = REPO_ROOT / "reports"


FIXTURES_DIR = REPO_ROOT / "fixtures"
TEMPLATES_DIR = REPO_ROOT / "templates"
SITE_DIR = REPO_ROOT / "site"


def _get_latest_summary_game_id() -> Optional[str]:
    """Find the most recent espn_summary_<id>.json in fixtures."""
    if not FIXTURES_DIR.exists():
        return None
    candidates = sorted(
        FIXTURES_DIR.glob("espn_summary_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    name = candidates[0].name  # espn_summary_401810077.json
    try:
        return name.split("espn_summary_")[1].split(".")[0]
    except Exception:
        return None


def _load_summary(game_id: str) -> Dict[str, Any]:
    path = FIXTURES_DIR / f"espn_summary_{game_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Could not find ESPN summary JSON at: {path}")
    LOG.info("Loading ESPN summary: %s", path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_team_maps(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Return helpers for mapping team ids to home/away + basic info."""
    header = summary["header"]
    comp = header["competitions"][0]
    competitors = comp["competitors"]
    teams_by_side: Dict[str, Dict[str, Any]] = {}
    team_id_to_side: Dict[str, str] = {}
    for c in competitors:
        side = c["homeAway"]
        team = c["team"]
        tid = team["id"]
        team_id_to_side[tid] = side

        logos = team.get("logos")
        if isinstance(logos, list) and logos:
            logo_url = logos[0].get("href")
        else:
            logo_url = team.get("logo")

        teams_by_side[side] = {
            "id": tid,
            "tricode": team.get("abbreviation"),
            "full_name": team.get("displayName"),
            "short_name": team.get("shortDisplayName"),
            "logo": logo_url,
            "score": int(c.get("score", 0)),
            "linescores": [
                int(ls.get("displayValue", 0)) for ls in c.get("linescores", [])
            ],
        }
    return {
        "teams_by_side": teams_by_side,
        "team_id_to_side": team_id_to_side,
        "competition": comp,
    }


def _extract_team_totals(
    summary: Dict[str, Any], team_id_to_side: Dict[str, str]
) -> Dict[str, Any]:
    """Build game_totals.traditional + misc from boxscore.teams[]."""
    box_teams = summary["boxscore"]["teams"]
    totals_trad = {"home": {}, "away": {}}
    misc = {"home": {}, "away": {}}
    stats_by_tid: Dict[str, Dict[str, Any]] = {}
    for t in box_teams:
        tid = t["team"]["id"]
        stat_map: Dict[str, Any] = {}
        for stat in t.get("statistics", []):
            name = stat.get("name")
            display = stat.get("displayValue")
            stat_map[name] = display
        stats_by_tid[tid] = stat_map

    largest_lead = {"home": 0, "away": 0}

    for tid, smap in stats_by_tid.items():
        side = team_id_to_side.get(tid)
        if not side:
            continue

        def split_pair(key: str) -> (int, int):
            val = smap.get(key) or "0-0"
            parts = str(val).split("-")
            try:
                made = int(parts[0])
            except Exception:
                made = 0
            try:
                att = int(parts[1]) if len(parts) > 1 else 0
            except Exception:
                att = 0
            return made, att

        fg_m, fg_a = split_pair("fieldGoalsMade-fieldGoalsAttempted")
        tp_m, tp_a = split_pair(
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted"
        )
        ft_m, ft_a = split_pair("freeThrowsMade-freeThrowsAttempted")

        def as_int(name: str) -> int:
            val = smap.get(name)
            try:
                return int(val)
            except Exception:
                return 0

        totals_trad[side] = {
            "fg": fg_m,
            "fga": fg_a,
            "fg3": tp_m,
            "fg3a": tp_a,
            "ft": ft_m,
            "fta": ft_a,
            "orb": as_int("offensiveRebounds"),
            "drb": as_int("defensiveRebounds"),
            "trb": as_int("rebounds"),
            "ast": as_int("assists"),
            "stl": as_int("steals"),
            "blk": as_int("blocks"),
            "tov": as_int("turnovers"),
            "pf": as_int("fouls"),
            "pts": as_int("points"),
        }

        misc[side] = {
            "pitp": as_int("pointsInPaint"),
            "fast_break": as_int("fastBreakPoints"),
            "turnover_points": as_int("turnoverPoints"),
            "largest_lead": as_int("largestLead"),
        }
        largest_lead[side] = misc[side]["largest_lead"]

    # points off turnovers: for each team, it's opponent's turnover_points
    for side in ("home", "away"):
        other = "away" if side == "home" else "home"
        misc.setdefault(side, {})
        misc[side]["points_off_to"] = misc.get(other, {}).get("turnover_points", 0)

    return {
        "traditional": totals_trad,
        "misc": misc,
        "largest_lead": largest_lead,
    }


def _extract_players(
    summary: Dict[str, Any], team_id_to_side: Dict[str, str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Build players.home/away list from boxscore.players[]."""
    players_by_side = {"home": [], "away": []}
    for team_block in summary["boxscore"]["players"]:
        team = team_block["team"]
        tid = team["id"]
        side = team_id_to_side.get(tid)
        if not side:
            continue
        if not team_block.get("statistics"):
            continue
        stat_block = team_block["statistics"][0]
        keys = stat_block.get("keys", [])
        athletes = stat_block.get("athletes", [])
        for a in athletes:
            ath = a.get("athlete", {})
            stats_vals = a.get("stats", [])
            stats_map = {
                k: stats_vals[i] for i, k in enumerate(keys) if i < len(stats_vals)
            }

            def split_pair(val: str) -> (int, int):
                parts = str(val).split("-")
                try:
                    made = int(parts[0])
                except Exception:
                    made = 0
                try:
                    att = int(parts[1]) if len(parts) > 1 else 0
                except Exception:
                    att = 0
                return made, att

            fg_m, fg_a = split_pair(
                stats_map.get("fieldGoalsMade-fieldGoalsAttempted", "0-0")
            )
            tp_m, tp_a = split_pair(
                stats_map.get(
                    "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"
                )
            )
            ft_m, ft_a = split_pair(
                stats_map.get("freeThrowsMade-freeThrowsAttempted", "0-0")
            )

            def as_int_key(k: str) -> int:
                v = stats_map.get(k)
                try:
                    return int(v)
                except Exception:
                    return 0

            player = {
                "name": ath.get("displayName"),
                "short_name": ath.get("shortName"),
                "jersey": ath.get("jersey"),
                "position": (ath.get("position") or {}).get("abbreviation"),
                "starter": a.get("starter", False),
                "did_not_play": a.get("didNotPlay", False),
                "reason": a.get("reason"),
                "min": stats_map.get("minutes"),
                "traditional": {
                    "fg": fg_m,
                    "fga": fg_a,
                    "fg3": tp_m,
                    "fg3a": tp_a,
                    "ft": ft_m,
                    "fta": ft_a,
                    "trb": as_int_key("rebounds"),
                    "orb": as_int_key("offensiveRebounds"),
                    "drb": as_int_key("defensiveRebounds"),
                    "ast": as_int_key("assists"),
                    "stl": as_int_key("steals"),
                    "blk": as_int_key("blocks"),
                    "tov": as_int_key("turnovers"),
                    "pf": as_int_key("fouls"),
                    "pts": as_int_key("points"),
                },
            }
            players_by_side[side].append(player)
    return players_by_side


def _compute_leaders(
    players_by_side: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Dict[str, Any]]:
    leaders: Dict[str, Dict[str, Any]] = {"home": {}, "away": {}}
    stat_keys = {
        "points": "pts",
        "rebounds": "trb",
        "assists": "ast",
        "steals": "stl",
        "blocks": "blk",
    }
    for side in ("home", "away"):
        plist = players_by_side.get(side, [])
        for label, stat_key in stat_keys.items():
            best_val = None
            names: List[str] = []
            for p in plist:
                v = p.get("traditional", {}).get(stat_key, 0)
                if best_val is None or v > best_val:
                    best_val = v
                    names = [p.get("name")]
                elif v == best_val and v != 0:
                    names.append(p.get("name"))
            leaders[side][label] = {
                "value": best_val or 0,
                "players": names,
            }
    return leaders


def _build_quarters(
    comp: Dict[str, Any], teams_by_side: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Build minimal quarters list: number + home/away score + team_totals.pts."""
    competitors = comp["competitors"]
    side_lines: Dict[str, List[int]] = {}
    for c in competitors:
        side = c["homeAway"]
        side_lines[side] = [
            int(ls.get("displayValue", 0)) for ls in c.get("linescores", [])
        ]

    num_quarters = max(
        len(side_lines.get("home", [])), len(side_lines.get("away", []))
    )
    quarters: List[Dict[str, Any]] = []
    for i in range(num_quarters):
        h_pts = side_lines.get("home", [0] * num_quarters)[i]
        a_pts = side_lines.get("away", [0] * num_quarters)[i]
        quarters.append(
            {
                "number": i + 1,
                "home_score": h_pts,
                "away_score": a_pts,
                "team_totals": {
                    "traditional": {
                        "home": {
                            "pts": h_pts,
                        },
                        "away": {
                            "pts": a_pts,
                        },
                    }
                },
                "players": {
                    "home": [],
                    "away": [],
                },
            }
        )
    return quarters


def _build_download_urls(game_id: str) -> Dict[str, str]:
    """
    Build direct ESPN JSON URL(s) for the Downloads section.

    We keep the existing key names so the HTML template doesn't need to change.
    """
    base_url = (
        "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
    )
    full_url = f"{base_url}?event={game_id}"

    return {
        "espn_summary_json": full_url,
        # Template key name mentions CSV, but this is actually JSON
        # that includes the full 'plays' array.
        "play_by_play_csv": full_url,
    }



def build_data(game_id: str) -> Dict[str, Any]:
    summary = _load_summary(game_id)
    maps = _build_team_maps(summary)
    teams_by_side = maps["teams_by_side"]
    team_id_to_side = maps["team_id_to_side"]
    comp = maps["competition"]

    season = summary["header"].get("season", {}).get("year")
    date_iso = comp.get("date", "")
    game_date = date_iso.split("T")[0] if "T" in date_iso else date_iso

    totals = _extract_team_totals(summary, team_id_to_side)
    players_by_side = _extract_players(summary, team_id_to_side)
    leaders = _compute_leaders(players_by_side)
    quarters = _build_quarters(comp, teams_by_side)
    download_urls = _build_download_urls(game_id)

    # Quarter-by-quarter + runs (from ESPN summary play-by-play)
    quarters_and_runs = analyze_quarters_and_runs(summary, game_id=game_id)

    data: Dict[str, Any] = {
        "meta": {
            "game_id": game_id,
            "date": game_date,
            "season": season,
            "final_score_home": teams_by_side["home"]["score"],
            "final_score_away": teams_by_side["away"]["score"],
        },
        "teams": {
            "home": teams_by_side["home"],
            "away": teams_by_side["away"],
        },
        "game_totals": {
            "traditional": totals["traditional"],
            "misc": totals["misc"],
        },
        "players": players_by_side,
        "quarters": quarters,
        "leaders": leaders,
        "largest_lead": totals["largest_lead"],
        "files": download_urls,
        "quarters_and_runs": quarters_and_runs,
    }
    return data


def render_report(data: Dict[str, Any]) -> str:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.jinja")
    LOG.info(
        "Rendering template report.html.jinja with game_id=%s",
        data.get("meta", {}).get("game_id"),
    )
    return template.render(data=data)


def _format_date_label(date_iso: str) -> str:
    """Turn '2024-11-10T01:30Z' into something like 'Nov 10, 2024'."""
    if not date_iso:
        return ""
    if "T" in date_iso:
        date_iso = date_iso.split("T")[0]
    try:
        dt = datetime.fromisoformat(date_iso)
        return dt.strftime("%b %-d, %Y")  # e.g. 'Nov 10, 2024'
    except Exception:
        # Windows doesn't like %-d, fallback
        try:
            dt = datetime.fromisoformat(date_iso)
            return dt.strftime("%b %d, %Y")
        except Exception:
            return date_iso


def _describe_game_from_summary(game_id: str) -> Optional[str]:
    """
    Build a label like:
    'Nov 10, 2024 — Pelicans 110, Thunder 118'
    using the cached ESPN summary JSON.
    """
    try:
        summary = _load_summary(game_id)
    except FileNotFoundError:
        return None
    except Exception:
        return None

    header = summary.get("header", {})
    competitions = header.get("competitions") or []
    if not competitions:
        return f"Game {game_id}"
    comp = competitions[0]

    date_iso = comp.get("date", "")
    date_label = _format_date_label(date_iso)

    competitors = comp.get("competitors") or []
    if len(competitors) < 2:
        return f"{date_label} — Game {game_id}"

    # identify home / away nicely
    home = None
    away = None
    for c in competitors:
        if c.get("homeAway") == "home":
            home = c
        elif c.get("homeAway") == "away":
            away = c

    # fallback if flags are weird
    if home is None and competitors:
        home = competitors[0]
    if away is None and len(competitors) > 1:
        away = competitors[1]

    def team_label(c: Dict[str, Any]) -> str:
        team = c.get("team", {}) or {}
        short_name = team.get("shortDisplayName") or team.get("displayName") or ""
        score = c.get("score")
        try:
            score_int = int(score)
            return f"{short_name} {score_int}"
        except Exception:
            return short_name or str(score) or "Unknown"

    home_str = team_label(home or {})
    away_str = team_label(away or {})

    if date_label:
        return f"{date_label} — {away_str}, {home_str}"
    return f"{away_str} at {home_str} (Game {game_id})"


def _build_index(report_files: List[Path]) -> None:
    """
    Build a simple index.html under SITE_DIR listing all game_*.html reports.

    Uses ESPN summary JSON to add date + opponent description.
    """
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    pages = []
    for html_file in report_files:
        game_id = html_file.stem.replace("game_", "")
        label = _describe_game_from_summary(game_id) or f"Game {game_id}"
        pages.append((html_file, game_id, label))

    if not pages:
        return

    # Sort by file mtime descending (newest first)
    pages.sort(key=lambda tup: tup[0].stat().st_mtime, reverse=True)

    lines = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '  <meta charset="utf-8">',
        "  <title>DT Game Report Index</title>",
        "  <style>",
        "    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 1.5rem; }",
        "    h1 { font-size: 1.75rem; margin-bottom: 1rem; }",
        "    ul { list-style: none; padding-left: 0; }",
        "    li { margin-bottom: 0.35rem; }",
        "    a { text-decoration: none; color: #007ac1; }",
        "    a:hover { text-decoration: underline; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <h1>DT Game Report – All Games</h1>",
        "  <ul>",
    ]

    for html_file, game_id, label in pages:
        rel = html_file.name
        lines.append(f'    <li><a href="{rel}">{label}</a></li>')

    lines += [
        "  </ul>",
        "</body>",
        "</html>",
    ]

    index_path = SITE_DIR / "index.html"
    with index_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    LOG.info("Wrote index.html listing %d game reports", len(pages))


def _sync_reports_to_site(report_files: List[Path]) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    for report_file in report_files:
        shutil.copy2(report_file, SITE_DIR / report_file.name)


def main(argv: Optional[list] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Generate DT Game Report HTML from ESPN fixtures"
    )
    parser.add_argument(
        "--game-id",
        dest="game_id",
        help=(
            "ESPN game id (e.g. 401810077). If omitted, uses GAME_ID env or "
            "latest espn_summary_*.json."
        ),
    )
    args = parser.parse_args(argv)

    game_id = args.game_id or os.environ.get("GAME_ID") or _get_latest_summary_game_id()
    if not game_id:
        raise SystemExit(
            "No game id provided and no espn_summary_*.json found in fixtures."
        )
    LOG.info("Using game id: %s", game_id)

    data = build_data(game_id)
    html = render_report(data)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"game_{game_id}.html"
    with out_path.open("w", encoding="utf-8") as f:
        f.write(html)
    LOG.info("Wrote HTML report: %s", out_path)

    report_files = sorted(REPORTS_DIR.glob("game_*.html"))
    _sync_reports_to_site(report_files)
    _build_index(report_files)


if __name__ == "__main__":
    main()
