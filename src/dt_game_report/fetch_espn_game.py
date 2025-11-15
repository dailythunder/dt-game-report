
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# Replace this with the ESPN event ID you want to test with.
# You can find it in the URL of an ESPN box score, e.g.
# https://www.espn.com/nba/game/_/gameId/401585655  -> event id "401585655"
ESPN_EVENT_ID = "401810077"


@dataclass
class TeamIds:
    home_id: str
    away_id: str


def get_repo_root() -> Path:
    """
    Resolve the repo root based on this file's location.

    Expected layout:
      repo_root/
        src/dt_game_report/fetch_espn_game.py
        fixtures/
    """
    return Path(__file__).resolve().parents[2]


def fetch_espn_summary(event_id: str) -> Dict[str, Any]:
    """
    Fetch ESPN NBA game summary JSON for a given event ID.

    This uses ESPN's site API summary endpoint for NBA games.
    """
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


def _extract_team_side(competition: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    competitors = competition.get("competitors") or []
    if len(competitors) != 2:
        raise ValueError("Expected exactly 2 competitors in ESPN summary.")
    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")
    return home, away


def _parse_meta(summary: Dict[str, Any], comp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the meta dict for the DT schema from ESPN summary.
    """
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


def _parse_teams(home: Dict[str, Any], away: Dict[str, Any]) -> Dict[str, Any]:
    def team_info(comp_entry: Dict[str, Any]) -> Dict[str, str]:
        t = comp_entry.get("team") or {}
        return {
            "tricode": t.get("abbreviation") or "",
            "full_name": t.get("displayName") or t.get("name") or "",
            "logo_url": (t.get("logos") or [{}])[0].get("href") or "",
        }

    return {
        "home": team_info(home),
        "away": team_info(away),
    }


def _parse_linescores(competition: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract per-quarter scoring.

    Returns a list of quarters:
      [{ "number": 1, "home_score": 30, "away_score": 25 }, ...]
    """
    competitors = competition.get("competitors") or []
    if len(competitors) != 2:
        return []

    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")

    home_lines = home.get("linescores") or []
    away_lines = away.get("linescores") or []

    quarters: List[Dict[str, Any]] = []
    num_periods = max(len(home_lines), len(away_lines))

    for idx in range(num_periods):
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
                "number": idx + 1,
                "home_score": _score(home_q),
                "away_score": _score(away_q),
            }
        )

    return quarters


def _parse_boxscore(summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract basic team & player stats from the boxscore section.

    Returns a dict with raw team and player stats keyed by team abbreviation.
    """
    box = summary.get("boxscore") or {}
    teams_stats = box.get("teams") or []
    players_stats = box.get("players") or []

    data: Dict[str, Any] = {
        "teams_raw": [],
        "players_raw": [],
    }

    # Team totals (traditional)
    for team_entry in teams_stats:
        t = team_entry.get("team") or {}
        abbrev = (t.get("abbreviation") or "").upper()
        stats = team_entry.get("statistics") or []
        totals: Dict[str, Any] = {"team_abbrev": abbrev}

        for s in stats:
            name = (s.get("name") or "").lower()
            val = s.get("displayValue")
            totals[name] = val

        data["teams_raw"].append(totals)

    # Player stats
    for team_players in players_stats:
        team = team_players.get("team") or {}
        abbrev = (team.get("abbreviation") or "").upper()
        names_map: Dict[str, Dict[str, Any]] = {}

        for group in team_players.get("statistics") or []:
            stat_list = group.get("stats") or []
            for s in stat_list:
                athlete = s.get("athlete") or {}
                pid = athlete.get("id")
                if not pid:
                    continue
                if pid not in names_map:
                    names_map[pid] = {
                        "player_id": pid,
                        "name": athlete.get("displayName") or "",
                        "position": athlete.get("position") or "",
                        "starter": athlete.get("starter", False),
                        "min": "",
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
                        "pf": 0,
                        "pts": 0,
                    }

                stat_name = (s.get("name") or "").lower()
                display_val = s.get("displayValue")

                def as_int(v: Optional[str]) -> int:
                    try:
                        return int(v)
                    except Exception:
                        return 0

                if stat_name == "minutes":
                    names_map[pid]["min"] = display_val or ""
                elif stat_name == "fieldgoalsmade":
                    names_map[pid]["fg"] = as_int(display_val)
                elif stat_name == "fieldgoalsattempted":
                    names_map[pid]["fga"] = as_int(display_val)
                elif stat_name == "threepointfieldgoalsmade":
                    names_map[pid]["fg3"] = as_int(display_val)
                elif stat_name == "threepointfieldgoalsattempted":
                    names_map[pid]["fg3a"] = as_int(display_val)
                elif stat_name == "freethrowsmade":
                    names_map[pid]["ft"] = as_int(display_val)
                elif stat_name == "freethrowsattempted":
                    names_map[pid]["fta"] = as_int(display_val)
                elif stat_name in ("rebounds", "totalrebounds"):
                    names_map[pid]["trb"] = as_int(display_val)
                elif stat_name == "assists":
                    names_map[pid]["ast"] = as_int(display_val)
                elif stat_name == "steals":
                    names_map[pid]["stl"] = as_int(display_val)
                elif stat_name == "blocks":
                    names_map[pid]["blk"] = as_int(display_val)
                elif stat_name == "turnovers":
                    names_map[pid]["tov"] = as_int(display_val)
                elif stat_name == "personalFouls".lower():
                    names_map[pid]["pf"] = as_int(display_val)
                elif stat_name == "points":
                    names_map[pid]["pts"] = as_int(display_val)

        if abbrev:
            data["players_raw"].append(
                {"team_abbrev": abbrev, "players": list(names_map.values())}
            )

    return data


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


def _map_team_totals(team_data: Dict[str, Any], home_abbrev: str, away_abbrev: str) -> Dict[str, Any]:
    """
    Build simple team totals structure keyed by 'home' and 'away' using
    ESPN team stats.
    """
    out = {"home": {}, "away": {}}
    teams_raw = team_data.get("teams_raw") or []

    for t in teams_raw:
        abbrev = (t.get("team_abbrev") or "").upper()
        if not abbrev:
            continue

        side = None
        if abbrev == home_abbrev.upper():
            side = "home"
        elif abbrev == away_abbrev.upper():
            side = "away"
        if side is None:
            continue

        fg_m, fg_a = _split_makes_attempts(t.get("fieldgoals"))
        fg3_m, fg3_a = _split_makes_attempts(t.get("threepointfieldgoals"))
        ft_m, ft_a = _split_makes_attempts(t.get("freethrows"))

        def as_int(v: Optional[str]) -> int:
            try:
                return int(v)
            except Exception:
                return 0

        out[side] = {
            "fg": fg_m,
            "fga": fg_a,
            "fg3": fg3_m,
            "fg3a": fg3_a,
            "ft": ft_m,
            "fta": ft_a,
            "trb": as_int(t.get("rebounds") or t.get("totalrebounds")),
            "ast": as_int(t.get("assists")),
            "stl": as_int(t.get("steals")),
            "blk": as_int(t.get("blocks")),
            "tov": as_int(t.get("turnovers")),
            "pf": as_int(t.get("fouls") or t.get("personalfouls")),
            "pts": as_int(t.get("points")),
        }

    return out


def _build_players_flat(team_data: Dict[str, Any], home_abbrev: str, away_abbrev: str,
                        base_players_home: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Convert ESPN players_raw into a flat structure that matches the keys of
    example_game.json player entries as closely as possible.
    """
    players_raw = team_data.get("players_raw") or []

    # Determine which keys the template expects from the sample player
    if base_players_home:
        sample_keys = list(base_players_home[0].keys())
    else:
        # Fallback if sample is missing
        sample_keys = [
            "name", "position", "starter", "min",
            "fg", "fga", "fg3", "fg3a", "ft", "fta",
            "trb", "ast", "stl", "blk", "tov", "pf", "pts",
        ]

    players: Dict[str, List[Dict[str, Any]]] = {"home": [], "away": []}

    for entry in players_raw:
        abbrev = (entry.get("team_abbrev") or "").upper()
        side = None
        if abbrev == home_abbrev.upper():
            side = "home"
        elif abbrev == away_abbrev.upper():
            side = "away"
        if side is None:
            continue

        for p in entry.get("players") or []:
            flat: Dict[str, Any] = {}
            for key in sample_keys:
                if key == "name":
                    flat[key] = p.get("name", "")
                elif key in ("position", "pos"):
                    flat[key] = p.get("position", "")
                elif key in ("starter", "is_starter"):
                    flat[key] = p.get("starter", False)
                elif key in ("min", "minutes"):
                    flat[key] = p.get("min", "")
                elif key == "fg":
                    flat[key] = p.get("fg", 0)
                elif key == "fga":
                    flat[key] = p.get("fga", 0)
                elif key in ("fg3", "three_pm", "tp"):
                    flat[key] = p.get("fg3", 0)
                elif key in ("fg3a", "three_pa"):
                    flat[key] = p.get("fg3a", 0)
                elif key == "ft":
                    flat[key] = p.get("ft", 0)
                elif key == "fta":
                    flat[key] = p.get("fta", 0)
                elif key in ("trb", "reb", "rebs"):
                    flat[key] = p.get("trb", 0)
                elif key == "ast":
                    flat[key] = p.get("ast", 0)
                elif key == "stl":
                    flat[key] = p.get("stl", 0)
                elif key == "blk":
                    flat[key] = p.get("blk", 0)
                elif key == "tov":
                    flat[key] = p.get("tov", 0)
                elif key in ("pf", "fouls"):
                    flat[key] = p.get("pf", 0)
                elif key == "pts":
                    flat[key] = p.get("pts", 0)
                else:
                    # Default for any unknown keys the template might use
                    flat[key] = flat.get(key, 0)

            players[side].append(flat)

    return players


def _compute_leaders(players: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Compute leaders from flattened player stats.
    """
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


def build_dt_schema_from_espn(summary: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert ESPN summary JSON into the DT Game Report JSON schema,
    aligning with the structure of example_game.json as much as possible.
    """
    comp = _extract_competition(summary)
    home_comp, away_comp = _extract_team_side(comp)

    meta = _parse_meta(summary, comp)
    teams = _parse_teams(home_comp, away_comp)
    quarters_basic = _parse_linescores(comp)

    box_data = _parse_boxscore(summary)
    home_abbrev = teams["home"]["tricode"]
    away_abbrev = teams["away"]["tricode"]

    team_totals_simple = _map_team_totals(box_data, home_abbrev, away_abbrev)

    base_players_home = base.get("players", {}).get("home", []) or []
    players_flat = _build_players_flat(box_data, home_abbrev, away_abbrev, base_players_home)
    leaders = _compute_leaders(players_flat)

    # Start from the base example structure
    data = base

    # Meta
    if "meta" not in data:
        data["meta"] = {}
    data["meta"].update(meta)

    # Teams
    if "teams" not in data:
        data["teams"] = {"home": {}, "away": {}}
    data["teams"]["home"].update(teams["home"])
    data["teams"]["away"].update(teams["away"])

    # Game totals - traditional
    if "game_totals" not in data:
        data["game_totals"] = {}
    if "traditional" not in data["game_totals"]:
        data["game_totals"]["traditional"] = {"home": {}, "away": {}}

    # Copy over keys that already exist in base so we don't break the template
    for side in ("home", "away"):
        base_side = data["game_totals"]["traditional"].get(side, {})
        new_side: Dict[str, Any] = {}
        for key in base_side.keys():
            if key == "fg":
                new_side[key] = team_totals_simple[side]["fg"]
            elif key == "fga":
                new_side[key] = team_totals_simple[side]["fga"]
            elif key in ("fg_pct", "fgp"):
                fga = team_totals_simple[side]["fga"]
                fg = team_totals_simple[side]["fg"]
                new_side[key] = round(fg / fga * 100, 1) if fga else 0.0
            elif key == "fg3":
                new_side[key] = team_totals_simple[side]["fg3"]
            elif key == "fg3a":
                new_side[key] = team_totals_simple[side]["fg3a"]
            elif key in ("fg3_pct", "tp_pct"):
                fg3a = team_totals_simple[side]["fg3a"]
                fg3 = team_totals_simple[side]["fg3"]
                new_side[key] = round(fg3 / fg3a * 100, 1) if fg3a else 0.0
            elif key == "ft":
                new_side[key] = team_totals_simple[side]["ft"]
            elif key == "fta":
                new_side[key] = team_totals_simple[side]["fta"]
            elif key in ("ft_pct", "ftp"):
                fta = team_totals_simple[side]["fta"]
                ft = team_totals_simple[side]["ft"]
                new_side[key] = round(ft / fta * 100, 1) if fta else 0.0
            elif key in ("trb", "reb", "rebs"):
                new_side[key] = team_totals_simple[side]["trb"]
            elif key == "ast":
                new_side[key] = team_totals_simple[side]["ast"]
            elif key == "stl":
                new_side[key] = team_totals_simple[side]["stl"]
            elif key == "blk":
                new_side[key] = team_totals_simple[side]["blk"]
            elif key == "tov":
                new_side[key] = team_totals_simple[side]["tov"]
            elif key in ("pf", "fouls"):
                new_side[key] = team_totals_simple[side]["pf"]
            elif key == "pts":
                new_side[key] = team_totals_simple[side]["pts"]
            else:
                # For any other keys (like advanced-ish fields) keep the base value
                new_side[key] = base_side.get(key, 0)
        data["game_totals"]["traditional"][side] = new_side

    # Largest lead placeholder (we don't have this from summary yet)
    data["largest_lead"] = {"home": 0, "away": 0}

    # Players (flattened)
    data.setdefault("players", {})
    data["players"]["home"] = players_flat["home"]
    data["players"]["away"] = players_flat["away"]

    # Leaders
    data["leaders"] = leaders

    # Quarters: update scores only, keep rest of structure from base
    base_quarters = data.get("quarters", [])
    for i, q in enumerate(quarters_basic):
        if i < len(base_quarters):
            base_quarters[i]["number"] = q["number"]
            base_quarters[i]["home_score"] = q["home_score"]
            base_quarters[i]["away_score"] = q["away_score"]
        else:
            base_quarters.append(
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
    data["quarters"] = base_quarters

    # Files block – keep whatever example has, but ensure key exists
    data.setdefault("files", {})
    data["files"].setdefault("play_by_play_csv", "play_by_play.csv")

    return data


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

    example_path = fixtures_dir / "example_game.json"
    if not example_path.exists():
        raise FileNotFoundError(f"Could not find base fixture at {example_path}")
    print(f"[Fetch ESPN] Loading base fixture from: {example_path}")
    with example_path.open("r", encoding="utf-8") as f:
        base = json.load(f)

    summary = fetch_espn_summary(ESPN_EVENT_ID)
    dt_data = build_dt_schema_from_espn(summary, base)
    out_path = save_dt_game_json(dt_data, fixtures_dir, ESPN_EVENT_ID)

    print("[Fetch ESPN] Done.")
    print(f"[Fetch ESPN] You can now run:")
    print(f"  python src/dt_game_report/generate_report.py --game-json fixtures/{out_path.name}")


if __name__ == "__main__":
    main()
