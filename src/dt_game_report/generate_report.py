import argparse
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape


LOG = logging.getLogger("dt_game_report.generate_report")


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
TEMPLATES_DIR = REPO_ROOT / "templates"
SITE_DIR = REPO_ROOT / "site"


# ----------------------- helpers -----------------------


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
        teams_by_side[side] = {
            "id": tid,
            "tricode": team.get("abbreviation"),
            "full_name": team.get("displayName"),
            "short_name": team.get("shortDisplayName"),
            "logo": (team.get("logos") or team.get("logo") or [{}])[0].get("href") if isinstance(team.get("logos"), list) else team.get("logo"),
            "score": int(c.get("score", 0)),
            "linescores": [int(ls.get("displayValue", 0)) for ls in c.get("linescores", [])],
        }
    return {
        "teams_by_side": teams_by_side,
        "team_id_to_side": team_id_to_side,
        "competition": comp,
    }


def _extract_team_totals(summary: Dict[str, Any], team_id_to_side: Dict[str, str]) -> Dict[str, Any]:
    """Build game_totals.traditional + misc from boxscore.teams[]."""
    box_teams = summary["boxscore"]["teams"]
    totals_trad = {"home": {}, "away": {}}
    misc = {"home": {}, "away": {}}
    # first gather stats keyed by team id
    stats_by_tid: Dict[str, Dict[str, Any]] = {}
    for t in box_teams:
        tid = t["team"]["id"]
        stat_map: Dict[str, Any] = {}
        for stat in t.get("statistics", []):
            name = stat.get("name")
            display = stat.get("displayValue")
            stat_map[name] = display
        stats_by_tid[tid] = stat_map

    # largestLead we can also grab here for convenience
    largest_lead = {"home": 0, "away": 0}

    # map into home/away
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
        tp_m, tp_a = split_pair("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
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

        # misc stats
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


def _extract_players(summary: Dict[str, Any], team_id_to_side: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    """Build players.home/away list from boxscore.players[]."""
    players_by_side = {"home": [], "away": []}
    for team_block in summary["boxscore"]["players"]:
        team = team_block["team"]
        tid = team["id"]
        side = team_id_to_side.get(tid)
        if not side:
            continue
        # use first statistics block (traditional box)
        if not team_block.get("statistics"):
            continue
        stat_block = team_block["statistics"][0]
        keys = stat_block.get("keys", [])
        athletes = stat_block.get("athletes", [])
        for a in athletes:
            ath = a.get("athlete", {})
            stats_vals = a.get("stats", [])
            stats_map = {k: stats_vals[i] for i, k in enumerate(keys) if i < len(stats_vals)}

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

            fg_m, fg_a = split_pair(stats_map.get("fieldGoalsMade-fieldGoalsAttempted", "0-0"))
            tp_m, tp_a = split_pair(stats_map.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"))
            ft_m, ft_a = split_pair(stats_map.get("freeThrowsMade-freeThrowsAttempted", "0-0"))

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


def _compute_leaders(players_by_side: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
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


def _build_quarters(comp: Dict[str, Any], teams_by_side: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build minimal quarters list: number + home/away score + team_totals.pts.

    We leave players.home/away empty for now, so the template can still render the
    quarter score tables without pretending we have per-quarter box scores.
    """
    # competitors in header are already linked to teams_by_side by homeAway
    competitors = comp["competitors"]
    # map side->linescores
    side_lines: Dict[str, List[int]] = {}
    for c in competitors:
        side = c["homeAway"]
        side_lines[side] = [int(ls.get("displayValue", 0)) for ls in c.get("linescores", [])]

    num_quarters = max(len(side_lines.get("home", [])), len(side_lines.get("away", [])))
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


def _copy_downloads_and_build_files(game_id: str) -> Dict[str, str]:
    """Copy CSV/JSON into site/downloads and return files mapping for template."""
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    downloads_dir = SITE_DIR / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    files: Dict[str, str] = {}

    # play-by-play CSV
    pbp_src = FIXTURES_DIR / f"espn_pbp_{game_id}.csv"
    if pbp_src.exists():
        pbp_dest = downloads_dir / pbp_src.name
        shutil.copy2(pbp_src, pbp_dest)
        # href relative to site root
        files["play_by_play_csv"] = f"downloads/{pbp_src.name}"

    # raw ESPN summary JSON
    summary_src = FIXTURES_DIR / f"espn_summary_{game_id}.json"
    if summary_src.exists():
        summary_dest = downloads_dir / summary_src.name
        shutil.copy2(summary_src, summary_dest)
        files["espn_summary_json"] = f"downloads/{summary_src.name}"

    return files


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
    downloads_files = _copy_downloads_and_build_files(game_id)

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
        "files": downloads_files,
    }
    return data


def render_report(data: Dict[str, Any]) -> str:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.jinja")
    LOG.info("Rendering template report.html.jinja with game_id=%s", data.get("meta", {}).get("game_id"))
    return template.render(data=data)


def main(argv: Optional[list] = None) -> None:
    import os

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Generate DT Game Report HTML from ESPN fixtures")
    parser.add_argument(
        "--game-id",
        dest="game_id",
        help="ESPN game id (e.g. 401810077). If omitted, uses GAME_ID env or latest espn_summary_*.json.",
    )
    args = parser.parse_args(argv)

    game_id = args.game_id or os.environ.get("GAME_ID") or _get_latest_summary_game_id()
    if not game_id:
        raise SystemExit("No game id provided and no espn_summary_*.json found in fixtures.")
    LOG.info("Using game id: %s", game_id)

    data = build_data(game_id)
    html = render_report(data)

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SITE_DIR / f"game_{game_id}.html"
    with out_path.open("w", encoding="utf-8") as f:
        f.write(html)
    LOG.info("Wrote HTML report: %s", out_path)


if __name__ == "__main__":
    main()
