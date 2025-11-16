
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# Default ESPN event ID for testing.
# You can change this to any valid NBA gameId, e.g. "401810077".
ESPN_EVENT_ID = "401810077"


@dataclass
class TeamSide:
    abbrev: str
    full_name: str
    home_away: str  # "home" or "away"


def get_repo_root() -> Path:
    """Resolve the repo root based on this file's location."""
    return Path(__file__).resolve().parents[2]


def fetch_espn_summary(event_id: str) -> Dict[str, Any]:
    """Fetch ESPN NBA game summary JSON for a given event ID."""
    base_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
    params = {"event": event_id}
    print(f"[Fetch ESPN] Requesting summary for event {event_id} ...")
    resp = requests.get(base_url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    print("[Fetch ESPN] Summary fetched successfully.")
    return data


def _extract_competition(summary: Dict[str, Any]) -> Dict[str, Any]:
    header = summary.get("header", {})
    comps = header.get("competitions") or []
    if not comps:
        raise ValueError("No competitions found in ESPN summary JSON.")
    return comps[0]


def _extract_team_sides(competition: Dict[str, Any]) -> Dict[str, TeamSide]:
    """Return mapping 'home'/'away' -> TeamSide."""
    competitors = competition.get("competitors") or []
    if len(competitors) != 2:
        raise ValueError("Expected exactly 2 competitors in ESPN summary.")

    sides: Dict[str, TeamSide] = {}
    for comp_team in competitors:
        team = comp_team.get("team") or {}
        abbrev = (team.get("abbreviation") or "").upper()
        full_name = team.get("displayName") or team.get("name") or ""
        ha = comp_team.get("homeAway")
        if ha not in ("home", "away"):
            continue
        sides[ha] = TeamSide(abbrev=abbrev, full_name=full_name, home_away=ha)

    if "home" not in sides or "away" not in sides:
        raise ValueError("Could not find both home and away teams.")
    return sides


def _parse_meta(summary: Dict[str, Any], comp: Dict[str, Any]) -> Dict[str, Any]:
    game_id = comp.get("id") or summary.get("header", {}).get("id")
    date_str = comp.get("date")
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            date_out = dt.date().isoformat()
            season = str(dt.year)
        except Exception:
            date_out = date_str
            season = ""
    else:
        date_out = ""
        season = ""

    venue = comp.get("venue") or {}
    arena = venue.get("fullName") or ""
    address = venue.get("address") or {}
    city_parts = [address.get("city") or "", address.get("state") or ""]
    city = ", ".join([p for p in city_parts if p])

    score_competitors = comp.get("competitors") or []
    home_score = 0
    away_score = 0
    for c in score_competitors:
        side = c.get("homeAway")
        score_val = c.get("score")
        try:
            score_int = int(score_val) if score_val is not None else 0
        except Exception:
            score_int = 0
        if side == "home":
            home_score = score_int
        elif side == "away":
            away_score = score_int

    return {
        "game_id": game_id or "",
        "date": date_out,
        "season": season,
        "arena": arena,
        "city": city,
        "final_score_home": home_score,
        "final_score_away": away_score,
    }


def _parse_linescores(competition: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract per-quarter scoring from competition.competitors[].linescores."""
    competitors = competition.get("competitors") or []
    if len(competitors) != 2:
        return []

    def _by_side(target: str) -> Dict[str, Any]:
        for c in competitors:
            if c.get("homeAway") == target:
                return c
        return {}

    home = _by_side("home")
    away = _by_side("away")

    home_lines = home.get("linescores") or []
    away_lines = away.get("linescores") or []

    quarters: List[Dict[str, Any]] = []
    num_periods = max(len(home_lines), len(away_lines))

    for idx in range(num_periods):
        q_num = idx + 1
        home_q = home_lines[idx] if idx < len(home_lines) else {}
        away_q = away_lines[idx] if idx < len(away_lines) else {}

        def _score(ls: Dict[str, Any]) -> int:
            v = ls.get("value")
            try:
                return int(v)
            except Exception:
                return 0

        quarters.append(
            {
                "number": q_num,
                "home_score": _score(home_q),
                "away_score": _score(away_q),
            }
        )

    return quarters


def _split_makes_attempts(val: Optional[str]) -> Tuple[int, int]:
    if not isinstance(val, str):
        return 0, 0
    parts = val.split("-")
    if len(parts) != 2:
        return 0, 0
    try:
        made = int(parts[0])
        att = int(parts[1])
        return made, att
    except Exception:
        return 0, 0


def _parse_team_totals(summary: Dict[str, Any], sides: Dict[str, TeamSide]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (traditional_totals, misc_totals) keyed by 'home'/'away'."""
    box = summary.get("boxscore") or {}
    teams_stats = box.get("teams") or []

    traditional = {"home": {}, "away": {}}
    misc = {
        "home": {"pitp": 0.0, "second_chance": 0.0, "fast_break": 0.0, "points_off_to": 0.0},
        "away": {"pitp": 0.0, "second_chance": 0.0, "fast_break": 0.0, "points_off_to": 0.0},
    }
    largest_lead = {"home": 0, "away": 0}

    def side_key_from_abbrev(abbrev: str) -> Optional[str]:
        for key, ts in sides.items():
            if ts.abbrev.upper() == abbrev.upper():
                return key
        return None

    for t in teams_stats:
        team = t.get("team") or {}
        abbrev = (team.get("abbreviation") or "").upper()
        side = side_key_from_abbrev(abbrev)
        if side is None:
            continue

        stats_list = t.get("statistics") or []

        row_trad: Dict[str, Any] = {}
        row_misc = misc[side]

        fg_m = fg_a = fg3_m = fg3_a = ft_m = ft_a = 0
        trb = ast = stl = blk = tov = pf = pts = 0
        pitp = second_chance = fast_break = points_off_to = 0
        ll = 0

        for s in stats_list:
            name = s.get("name")
            val = s.get("displayValue")

            def as_int(x: Optional[str]) -> int:
                try:
                    return int(x)
                except Exception:
                    return 0

            if name == "fieldGoalsMade-fieldGoalsAttempted":
                fg_m, fg_a = _split_makes_attempts(val)
            elif name == "threePointFieldGoalsMade-threePointFieldGoalsAttempted":
                fg3_m, fg3_a = _split_makes_attempts(val)
            elif name == "freeThrowsMade-freeThrowsAttempted":
                ft_m, ft_a = _split_makes_attempts(val)
            elif name in ("totalRebounds", "rebounds"):
                trb = as_int(val)
            elif name == "assists":
                ast = as_int(val)
            elif name == "steals":
                stl = as_int(val)
            elif name == "blocks":
                blk = as_int(val)
            elif name in ("turnovers", "totalTurnovers"):
                tov = as_int(val)
            elif name == "points":
                pts = as_int(val)
            elif name == "fouls":
                pf = as_int(val)
            elif name == "pointsInPaint":
                pitp = as_int(val)
            elif name == "secondChancePoints":
                second_chance = as_int(val)
            elif name == "fastBreakPoints":
                fast_break = as_int(val)
            elif name == "turnoverPoints":
                # ESPN stores "points conceded off turnovers". For our purposes
                # treat this as opponent points off turnovers; here we keep it
                # as this team's points off turnovers for simplicity.
                points_off_to = as_int(val)
            elif name == "largestLead":
                ll = as_int(val)

        # Percentages
        fg_pct = round(fg_m / fg_a * 100, 1) if fg_a else 0.0
        fg3_pct = round(fg3_m / fg3_a * 100, 1) if fg3_a else 0.0
        ft_pct = round(ft_m / ft_a * 100, 1) if ft_a else 0.0

        row_trad.update(
            {
                "fg": fg_m,
                "fga": fg_a,
                "fg_pct": fg_pct,
                "fg3": fg3_m,
                "fg3a": fg3_a,
                "fg3_pct": fg3_pct,
                "ft": ft_m,
                "fta": ft_a,
                "ft_pct": ft_pct,
                "trb": trb,
                "ast": ast,
                "stl": stl,
                "blk": blk,
                "tov": tov,
                "pf": pf,
                "pts": pts,
            }
        )

        traditional[side] = row_trad
        misc[side] = {
            "pitp": float(pitp),
            "second_chance": float(second_chance),
            "fast_break": float(fast_break),
            "points_off_to": float(points_off_to),
        }
        largest_lead[side] = ll

    return traditional, misc, largest_lead


def _parse_players(summary: Dict[str, Any], sides: Dict[str, TeamSide]) -> Dict[str, List[Dict[str, Any]]]:
    """Parse player box score lines from ESPN format into flat dicts."""
    box = summary.get("boxscore") or {}
    players_stats = box.get("players") or []

    players: Dict[str, List[Dict[str, Any]]] = {"home": [], "away": []}

    def side_key_from_abbrev(abbrev: str) -> Optional[str]:
        for key, ts in sides.items():
            if ts.abbrev.upper() == abbrev.upper():
                return key
        return None

    for team_block in players_stats:
        team = team_block.get("team") or {}
        abbrev = (team.get("abbreviation") or "").upper()
        side = side_key_from_abbrev(abbrev)
        if side is None:
            continue

        stat_groups = team_block.get("statistics") or []
        if not stat_groups:
            continue
        group = stat_groups[0]

        keys = group.get("keys") or []
        athletes = group.get("athletes") or []

        for a in athletes:
            athlete = a.get("athlete") or {}
            stats = a.get("stats") or []
            if not stats or len(stats) != len(keys):
                continue

            row: Dict[str, Any] = {}
            name = athlete.get("displayName") or ""
            pos = ""
            pos_obj = athlete.get("position") or {}
            if isinstance(pos_obj, dict):
                pos = pos_obj.get("abbreviation") or ""

            row["name"] = name
            row["position"] = pos
            row["starter"] = bool(a.get("starter", False))

            fg_m = fg_a = fg3_m = fg3_a = ft_m = ft_a = 0
            trb = ast = stl = blk = tov = pf = pts = 0
            minutes = ""

            for key, val in zip(keys, stats):
                def as_int(x: Optional[str]) -> int:
                    try:
                        return int(x)
                    except Exception:
                        return 0

                if key == "fieldGoalsMade-fieldGoalsAttempted":
                    fg_m, fg_a = _split_makes_attempts(val)
                elif key == "threePointFieldGoalsMade-threePointFieldGoalsAttempted":
                    fg3_m, fg3_a = _split_makes_attempts(val)
                elif key == "freeThrowsMade-freeThrowsAttempted":
                    ft_m, ft_a = _split_makes_attempts(val)
                elif key in ("rebounds",):
                    trb = as_int(val)
                elif key == "assists":
                    ast = as_int(val)
                elif key == "steals":
                    stl = as_int(val)
                elif key == "blocks":
                    blk = as_int(val)
                elif key == "turnovers":
                    tov = as_int(val)
                elif key == "fouls":
                    pf = as_int(val)
                elif key == "points":
                    pts = as_int(val)
                elif key == "minutes":
                    minutes = val or ""

            fg_pct = round(fg_m / fg_a * 100, 1) if fg_a else 0.0
            fg3_pct = round(fg3_m / fg3_a * 100, 1) if fg3_a else 0.0
            ft_pct = round(ft_m / ft_a * 100, 1) if ft_a else 0.0

            row.update(
                {
                    "min": minutes,
                    "fg": fg_m,
                    "fga": fg_a,
                    "fg_pct": fg_pct,
                    "fg3": fg3_m,
                    "fg3a": fg3_a,
                    "fg3_pct": fg3_pct,
                    "ft": ft_m,
                    "fta": ft_a,
                    "ft_pct": ft_pct,
                    "trb": trb,
                    "ast": ast,
                    "stl": stl,
                    "blk": blk,
                    "tov": tov,
                    "pf": pf,
                    "pts": pts,
                }
            )

            players[side].append(row)

    return players


def _compute_leaders(players: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    def leaders_for_side(side_players: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not side_players:
            return {
                "points": {"value": 0, "players": []},
                "rebounds": {"value": 0, "players": []},
                "assists": {"value": 0, "players": []},
                "blocks": {"value": 0, "players": []},
                "steals": {"value": 0, "players": []},
            }

        def max_stat(stat_key: str) -> Tuple[int, List[str]]:
            max_val = -1
            names: List[str] = []
            for p in side_players:
                val = int(p.get(stat_key, 0))
                if val > max_val:
                    max_val = val
                    names = [p.get("name", "")]
                elif val == max_val and val > 0:
                    names.append(p.get("name", ""))
            if max_val < 0:
                max_val = 0
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
        "home": leaders_for_side(players.get("home", [])),
        "away": leaders_for_side(players.get("away", [])),
    }


def build_dt_schema_from_espn(summary: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    comp = _extract_competition(summary)
    sides = _extract_team_sides(comp)

    meta = _parse_meta(summary, comp)
    quarters_basic = _parse_linescores(comp)
    team_trad, misc_totals, largest_lead = _parse_team_totals(summary, sides)
    players = _parse_players(summary, sides)
    leaders = _compute_leaders(players)

    # Teams block
    teams = {
        "home": {
            "tricode": sides["home"].abbrev,
            "full_name": sides["home"].full_name,
        },
        "away": {
            "tricode": sides["away"].abbrev,
            "full_name": sides["away"].full_name,
        },
    }

    # Game totals block
    game_totals = {
        "traditional": team_trad,
        "advanced": {
            "home": {
                "off_rating": 0.0,
                "def_rating": 0.0,
                "net_rating": 0.0,
                "efg_pct": 0.0,
                "ts_pct": 0.0,
                "pace": 0.0,
            },
            "away": {
                "off_rating": 0.0,
                "def_rating": 0.0,
                "net_rating": 0.0,
                "efg_pct": 0.0,
                "ts_pct": 0.0,
                "pace": 0.0,
            },
        },
        "misc": misc_totals,
    }

    # Quarters block: scores only for now
    quarters: List[Dict[str, Any]] = []
    for q in quarters_basic:
        quarters.append(
            {
                "number": q["number"],
                "home_score": q["home_score"],
                "away_score": q["away_score"],
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
                            "pts": q["home_score"],
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
                            "pts": q["away_score"],
                        },
                    },
                    "advanced": {
                        "home": {
                            "off_rating": 0.0,
                            "def_rating": 0.0,
                            "net_rating": 0.0,
                            "efg_pct": 0.0,
                            "ts_pct": 0.0,
                        },
                        "away": {
                            "off_rating": 0.0,
                            "def_rating": 0.0,
                            "net_rating": 0.0,
                            "efg_pct": 0.0,
                            "ts_pct": 0.0,
                        },
                    },
                },
                "players": {
                    "home": [],
                    "away": [],
                },
            }
        )

    files = {"play_by_play_csv": "play_by_play.csv"}

    dt_data: Dict[str, Any] = {
        "meta": meta,
        "teams": teams,
        "largest_lead": largest_lead,
        "game_totals": game_totals,
        "players": players,
        "quarters": quarters,
        "leaders": leaders,
        "files": files,
        "source": {
            "provider": "ESPN",
            "event_id": event_id,
        },
    }

    return dt_data


def save_dt_game_json(data: Dict[str, Any], fixtures_dir: Path, event_id: str) -> Path:
    fixtures_dir.mkdir(exist_ok=True)
    out_path = fixtures_dir / f"espn_{event_id}.json"
    print(f"[Fetch ESPN] Writing DT game JSON to: {out_path}")
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return out_path


def main() -> None:
    repo_root = get_repo_root()
    fixtures_dir = repo_root / "fixtures"

    print(f"[Fetch ESPN] Repo root: {repo_root}")
    print(f"[Fetch ESPN] Fixtures dir: {fixtures_dir}")
    print(f"[Fetch ESPN] Using ESPN event id: {ESPN_EVENT_ID}")

    summary = fetch_espn_summary(ESPN_EVENT_ID)
    dt_data = build_dt_schema_from_espn(summary, ESPN_EVENT_ID)
    out_path = save_dt_game_json(dt_data, fixtures_dir, ESPN_EVENT_ID)

    print("[Fetch ESPN] Done.")
    print(f"[Fetch ESPN] You can now run:")
    print(f"  python src/dt_game_report/generate_report.py --game-json fixtures/{out_path.name}")


if __name__ == "__main__":
    main()
