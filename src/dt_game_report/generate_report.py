import argparse
import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

LOG = logging.getLogger("dt_game_report.generate_report")


@dataclass
class TeamInfo:
    tricode: str
    full_name: str
    logo_url: str
    id: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def fixtures_dir() -> Path:
    root = repo_root()
    d = root / "fixtures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def templates_dir() -> Path:
    return repo_root() / "templates"


def site_dir() -> Path:
    d = repo_root() / "site"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_latest_summary_file() -> Tuple[str, Path]:
    """Return (game_id, path) for the newest espn_summary_*.json in fixtures/"""
    fdir = fixtures_dir()
    candidates: List[Path] = sorted(
        fdir.glob("espn_summary_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No espn_summary_*.json files found in {fdir}")
    latest = candidates[0]
    stem = latest.stem  # e.g. 'espn_summary_401810077'
    game_id = stem.replace("espn_summary_", "")
    return game_id, latest


def load_summary(game_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Load ESPN summary JSON for a game id.

    If game_id is None, picks the newest espn_summary_*.json in fixtures/.
    """
    fdir = fixtures_dir()
    if game_id:
        path = fdir / f"espn_summary_{game_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Expected summary JSON not found: {path}")
    else:
        game_id, path = find_latest_summary_file()

    LOG.info("Using ESPN summary JSON for game %s: %s", game_id, path)
    with path.open("r", encoding="utf-8") as f:
        summary = json.load(f)
    return game_id, summary


def _index_team_boxscore(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return dict with keys 'home' and 'away' from boxscore.teams[]."""
    teams = summary.get("boxscore", {}).get("teams", []) or []
    out: Dict[str, Dict[str, Any]] = {}
    for t in teams:
        ha = t.get("homeAway")
        if ha not in ("home", "away"):
            continue
        out[ha] = t
    if "home" not in out or "away" not in out:
        raise RuntimeError("Could not find both home and away teams in boxscore.teams")
    return out


def _index_player_boxscore(summary: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Return dict with keys 'home' and 'away' players (full game stats)."""
    result: Dict[str, List[Dict[str, Any]]] = {"home": [], "away": []}
    players_sections = summary.get("boxscore", {}).get("players", []) or []
    for team_block in players_sections:
        team = team_block.get("team", {}) or {}
        abbrev = team.get("abbreviation")
        stats_groups = team_block.get("statistics", []) or []
        if not stats_groups:
            continue
        group = stats_groups[0]
        keys = group.get("keys", []) or []
        athletes = group.get("athletes", []) or []

        # Determine side (home/away) by matching team id against boxscore.teams
        # We fall back to looking at header.competitions.
        side = None
        team_id = str(team.get("id"))
        try:
            teams_by_side = _index_team_boxscore(summary)
            for ha, t in teams_by_side.items():
                if str(t.get("team", {}).get("id")) == team_id:
                    side = ha
                    break
        except Exception:
            side = None

        if side not in ("home", "away"):
            # Fallback: guess based on abbreviation vs header.competitions
            side = "home" if abbrev and abbrev.upper() == "OKC" else "away"

        for a in athletes:
            ath = a.get("athlete", {}) or {}
            stats_list = a.get("stats", []) or []
            if len(stats_list) != len(keys):
                # defensively pad or trim
                if len(stats_list) < len(keys):
                    stats_list = stats_list + ["0"] * (len(keys) - len(stats_list))
                else:
                    stats_list = stats_list[: len(keys)]

            stat_map = dict(zip(keys, stats_list))

            def get_pair(field_key: str) -> Tuple[int, int]:
                raw = stat_map.get(field_key, "0-0")
                if not raw:
                    return 0, 0
                if "-" not in raw:
                    # Sometimes ESPN uses just a single number; treat as made, attempts unknown.
                    try:
                        made = int(raw)
                    except ValueError:
                        made = 0
                    return made, 0
                made_s, att_s = raw.split("-", 1)
                try:
                    made = int(made_s)
                except ValueError:
                    made = 0
                try:
                    att = int(att_s)
                except ValueError:
                    att = 0
                return made, att

            def get_int(field_key: str) -> int:
                val = stat_map.get(field_key, "0")
                try:
                    return int(val)
                except ValueError:
                    # minutes and plusMinus sometimes non-int; ignore here
                    return 0

            fg_m, fg_a = get_pair("fieldGoalsMade-fieldGoalsAttempted")
            fg3_m, fg3_a = get_pair("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
            ft_m, ft_a = get_pair("freeThrowsMade-freeThrowsAttempted")

            player = {
                "player_id": int(ath.get("id", 0)) if str(ath.get("id", "0")).isdigit() else 0,
                "name": ath.get("displayName", ""),
                "position": ath.get("position", {}).get("abbreviation", "") if isinstance(ath.get("position"), dict) else "",
                "starter": bool(a.get("starter")),
                "min": stat_map.get("minutes", ""),
                "traditional": {
                    "fg": fg_m,
                    "fga": fg_a,
                    "fg3": fg3_m,
                    "fg3a": fg3_a,
                    "ft": ft_m,
                    "fta": ft_a,
                    "trb": get_int("rebounds"),
                    "ast": get_int("assists"),
                    "stl": get_int("steals"),
                    "blk": get_int("blocks"),
                    "tov": get_int("turnovers"),
                    "pf": get_int("fouls"),
                    "pts": get_int("points"),
                },
                # advanced placeholder; user decided to skip advanced stats for now
                "advanced": None,
            }
            result[side].append(player)

    return result


def _team_totals_from_boxscore(teams_by_side: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Build game_totals.traditional from boxscore.teams statistics list."""
    def totals_for_side(side: str) -> Dict[str, int]:
        t = teams_by_side[side]
        stats_list = t.get("statistics", []) or []
        stats_by_name = {s.get("name"): s for s in stats_list}

        def get_int(name: str) -> int:
            s = stats_by_name.get(name)
            if not s:
                return 0
            val = s.get("displayValue", "0")
            try:
                return int(val)
            except ValueError:
                # pair like '31-77'
                if "-" in val:
                    first = val.split("-", 1)[0]
                    try:
                        return int(first)
                    except ValueError:
                        return 0
                return 0

        def get_pair(name: str) -> Tuple[int, int]:
            s = stats_by_name.get(name)
            if not s:
                return 0, 0
            val = s.get("displayValue", "0-0")
            if "-" not in val:
                try:
                    made = int(val)
                except ValueError:
                    made = 0
                return made, 0
            made_s, att_s = val.split("-", 1)
            try:
                made = int(made_s)
            except ValueError:
                made = 0
            try:
                att = int(att_s)
            except ValueError:
                att = 0
            return made, att

        fg_m, fg_a = get_pair("fieldGoalsMade-fieldGoalsAttempted")
        fg3_m, fg3_a = get_pair("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
        ft_m, ft_a = get_pair("freeThrowsMade-freeThrowsAttempted")

        return {
            "fg": fg_m,
            "fga": fg_a,
            "fg3": fg3_m,
            "fg3a": fg3_a,
            "ft": ft_m,
            "fta": ft_a,
            "orb": get_int("offensiveRebounds"),
            "drb": get_int("defensiveRebounds"),
            "trb": get_int("rebounds"),
            "ast": get_int("assists"),
            "stl": get_int("steals"),
            "blk": get_int("blocks"),
            "tov": get_int("turnovers"),
            "pf": get_int("fouls"),
            "pts": get_int("points"),
        }

    return {
        "home": totals_for_side("home"),
        "away": totals_for_side("away"),
    }


def _misc_from_boxscore(teams_by_side: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Build game_totals.misc from boxscore.teams statistics.

    We expose:
    - pitp (points in the paint)
    - points_off_to (points off turnovers, using opponent's turnoverPoints)
    - fast_break (fast break points)
    """
    def idx_stats(t: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        stats_list = t.get("statistics", []) or []
        return {s.get("name"): s for s in stats_list}

    home_stats = idx_stats(teams_by_side["home"])
    away_stats = idx_stats(teams_by_side["away"])

    def get_int(stats: Dict[str, Dict[str, Any]], key: str) -> int:
        s = stats.get(key)
        if not s:
            return 0
        val = s.get("displayValue", "0")
        try:
            return int(val)
        except ValueError:
            return 0

    home = {
        "pitp": get_int(home_stats, "pointsInPaint"),
        "points_off_to": get_int(away_stats, "turnoverPoints"),
        "fast_break": get_int(home_stats, "fastBreakPoints"),
    }
    away = {
        "pitp": get_int(away_stats, "pointsInPaint"),
        "points_off_to": get_int(home_stats, "turnoverPoints"),
        "fast_break": get_int(away_stats, "fastBreakPoints"),
    }
    return {"home": home, "away": away}


def _leaders_from_players(players_by_side: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Compute leaders in PTS, REB, AST, BLK, STL from player stats."""
    def leaders_for_side(side: str) -> Dict[str, Any]:
        players = players_by_side.get(side, [])
        if not players:
            return {
                "points": {"value": 0, "players": []},
                "rebounds": {"value": 0, "players": []},
                "assists": {"value": 0, "players": []},
                "blocks": {"value": 0, "players": []},
                "steals": {"value": 0, "players": []},
            }

        def max_stat(stat_key: str) -> Tuple[int, List[str]]:
            max_val = 0
            names: List[str] = []
            for p in players:
                stats = p.get("traditional") or {}
                val = int(stats.get(stat_key, 0) or 0)
                if val > max_val:
                    max_val = val
                    names = [p.get("name", "")]
                elif val == max_val and val > 0:
                    names.append(p.get("name", ""))
            return max_val, names

        pts_val, pts_names = max_stat("pts")
        reb_val, reb_names = max_stat("trb")
        ast_val, ast_names = max_stat("ast")
        blk_val, blk_names = max_stat("blk")
        stl_val, stl_names = max_stat("stl")

        return {
            "points": {"value": pts_val, "players": pts_names},
            "rebounds": {"value": reb_val, "players": reb_names},
            "assists": {"value": ast_val, "players": ast_names},
            "blocks": {"value": blk_val, "players": blk_names},
            "steals": {"value": stl_val, "players": stl_names},
        }

    return {
        "home": leaders_for_side("home"),
        "away": leaders_for_side("away"),
    }


def _build_quarters(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build minimal quarters array with scores by period.

    We do NOT yet compute per-player or per-stat quarter splits; those stay empty
    so the template can still render the structure without crashing.
    """
    header = summary.get("header", {}) or {}
    competitions = header.get("competitions", []) or []
    if not competitions:
        return []
    comp = competitions[0]
    competitors = comp.get("competitors", []) or []
    home_comp = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away_comp = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home_comp or not away_comp:
        return []

    home_lines = home_comp.get("linescores", []) or []
    away_lines = away_comp.get("linescores", []) or []
    num_periods = max(len(home_lines), len(away_lines))

    quarters: List[Dict[str, Any]] = []
    for idx in range(num_periods):
        h_score = int(home_lines[idx]["displayValue"]) if idx < len(home_lines) else 0
        a_score = int(away_lines[idx]["displayValue"]) if idx < len(away_lines) else 0
        quarters.append(
            {
                "number": idx + 1,
                "home_score": h_score,
                "away_score": a_score,
                "team_totals": {
                    "traditional": {
                        "home": {
                            "fg": 0,
                            "fga": 0,
                            "fg3": 0,
                            "fg3a": 0,
                            "ft": 0,
                            "fta": 0,
                            "trb": 0,
                            "ast": 0,
                            "stl": 0,
                            "blk": 0,
                            "tov": 0,
                            "pts": h_score,
                        },
                        "away": {
                            "fg": 0,
                            "fga": 0,
                            "fg3": 0,
                            "fg3a": 0,
                            "ft": 0,
                            "fta": 0,
                            "trb": 0,
                            "ast": 0,
                            "stl": 0,
                            "blk": 0,
                            "tov": 0,
                            "pts": a_score,
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


def _largest_lead_from_boxscore(teams_by_side: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    def get_ll(side: str) -> int:
        stats_list = teams_by_side[side].get("statistics", []) or []
        for s in stats_list:
            if s.get("name") == "largestLead":
                val = s.get("displayValue", "0")
                try:
                    return int(val)
                except ValueError:
                    return 0
        return 0

    return {
        "home": get_ll("home"),
        "away": get_ll("away"),
    }


def build_data(game_id: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    # Teams & meta
    header = summary.get("header", {}) or {}
    competitions = header.get("competitions", []) or []
    comp = competitions[0] if competitions else {}
    date_str = comp.get("date", "")
    season = header.get("season", {}).get("year") or ""

    competitors = comp.get("competitors", []) or []
    home_comp = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away_comp = next((c for c in competitors if c.get("homeAway") == "away"), None)

    teams_box = _index_team_boxscore(summary)

    def team_info(side: str, comp_team: Dict[str, Any]) -> TeamInfo:
        bs_team = teams_box[side].get("team", {}) or {}
        tricode = bs_team.get("abbreviation") or comp_team.get("team", {}).get("abbreviation", "")
        full_name = bs_team.get("displayName") or comp_team.get("team", {}).get("displayName", "")
        logo_url = bs_team.get("logo") or ""
        tid = str(bs_team.get("id", ""))
        return TeamInfo(
            tricode=tricode,
            full_name=full_name,
            logo_url=logo_url,
            id=tid,
        )

    home_team = team_info("home", home_comp or {})
    away_team = team_info("away", away_comp or {})

    meta = {
        "game_id": game_id,
        "date": date_str.split("T")[0] if date_str else "",
        "season": str(season),
        "arena": "",
        "city": "",
        "final_score_home": int(home_comp.get("score", 0)) if home_comp else 0,
        "final_score_away": int(away_comp.get("score", 0)) if away_comp else 0,
    }

    players_by_side = _index_player_boxscore(summary)
    totals_traditional = _team_totals_from_boxscore(teams_box)
    misc_totals = _misc_from_boxscore(teams_box)
    leaders = _leaders_from_players(players_by_side)
    quarters = _build_quarters(summary)
    largest_lead = _largest_lead_from_boxscore(teams_box)

    # Files section (for Downloads card)
    fdir = fixtures_dir()
    files: Dict[str, str] = {}
    pbp_path = fdir / f"espn_pbp_{game_id}.csv"
    if pbp_path.exists():
        files["play_by_play_csv"] = f"../fixtures/{pbp_path.name}"
    summary_path = fdir / f"espn_summary_{game_id}.json"
    if summary_path.exists():
        files["espn_summary_json"] = f"../fixtures/{summary_path.name}"

    data: Dict[str, Any] = {
        "meta": meta,
        "teams": {
            "home": asdict(home_team),
            "away": asdict(away_team),
        },
        "largest_lead": largest_lead,
        "game_totals": {
            "traditional": totals_traditional,
            "advanced": {"home": {}, "away": {}},  # placeholder
            "misc": misc_totals,
        },
        "players": players_by_side,
        "quarters": quarters,
        "leaders": leaders,
        "files": files,
    }
    return data


def render_report(env: Environment, data: Dict[str, Any]) -> str:
    template = env.get_template("report.html.jinja")
    return template.render(data=data)


def main(argv: Optional[list] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Generate DT Game Report HTML from ESPN summary JSON")
    parser.add_argument(
        "--game-id",
        dest="game_id",
        help="ESPN game id (e.g. 401810077). If omitted, uses env GAME_ID or latest espn_summary_*.json.",
    )
    args = parser.parse_args(argv)

    # precedence: CLI arg > env GAME_ID > latest summary file
    game_id = args.game_id or os.environ.get("GAME_ID") or None
    game_id, summary = load_summary(game_id)

    data = build_data(game_id, summary)

    templates_path = templates_dir()
    LOG.info("Using templates directory: %s", templates_path)
    env = Environment(
        loader=FileSystemLoader(str(templates_path)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    html = render_report(env, data)

    out_dir = site_dir()
    out_path = out_dir / f"game_{game_id}.html"
    with out_path.open("w", encoding="utf-8") as f:
        f.write(html)
    LOG.info("Wrote HTML report: %s", out_path)


if __name__ == "__main__":
    main()
