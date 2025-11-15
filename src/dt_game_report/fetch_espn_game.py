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
ESPN_EVENT_ID = "401585655"


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
    Advanced stats and misc stats will be left for a later pass / computed,
    and are stubbed for now.
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
                        "traditional": {
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
                            "pts": 0,
                        },
                        "advanced": {
                            "ts_pct": None,
                            "efg_pct": None,
                            "usg_pct": None,
                            "off_rating": None,
                            "def_rating": None,
                        },
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
                    names_map[pid]["traditional"]["fg"] = as_int(display_val)
                elif stat_name == "fieldgoalsattempted":
                    names_map[pid]["traditional"]["fga"] = as_int(display_val)
                elif stat_name == "threepointfieldgoalsmade":
                    names_map[pid]["traditional"]["fg3"] = as_int(display_val)
                elif stat_name == "threepointfieldgoalsattempted":
                    names_map[pid]["traditional"]["fg3a"] = as_int(display_val)
                elif stat_name == "freethrowsmade":
                    names_map[pid]["traditional"]["ft"] = as_int(display_val)
                elif stat_name == "freethrowsattempted":
                    names_map[pid]["traditional"]["fta"] = as_int(display_val)
                elif stat_name in ("rebounds", "totalrebounds"):
                    names_map[pid]["traditional"]["trb"] = as_int(display_val)
                elif stat_name == "assists":
                    names_map[pid]["traditional"]["ast"] = as_int(display_val)
                elif stat_name == "steals":
                    names_map[pid]["traditional"]["stl"] = as_int(display_val)
                elif stat_name == "blocks":
                    names_map[pid]["traditional"]["blk"] = as_int(display_val)
                elif stat_name == "turnovers":
                    names_map[pid]["traditional"]["tov"] = as_int(display_val)
                elif stat_name == "points":
                    names_map[pid]["traditional"]["pts"] = as_int(display_val)

        if abbrev:
            data["players_raw"].append(
                {"team_abbrev": abbrev, "players": list(names_map.values())}
            )

    return data


def _map_team_totals_to_home_away(team_data: Dict[str, Any], home_abbrev: str, away_abbrev: str) -> Dict[str, Any]:
    """
    Map raw team totals dict from ESPN to home/away buckets using team abbreviations.
    """
    out = {"home": {}, "away": {}}
    teams_raw = team_data.get("teams_raw") or []
    for t in teams_raw:
        abbrev = t.get("team_abbrev")
        if not abbrev:
            continue
        bucket = None
        if abbrev.upper() == home_abbrev.upper():
            bucket = "home"
        elif abbrev.upper() == away_abbrev.upper():
            bucket = "away"
        if not bucket:
            continue

        def split_makes_attempts(val: Optional[str]) -> Tuple[int, int]:
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

        fg_m, fg_a = split_makes_attempts(t.get("fieldgoals") or t.get("fg"))
        fg3_m, fg3_a = split_makes_attempts(t.get("threepointfieldgoals") or t.get("threepointers"))
        ft_m, ft_a = split_makes_attempts(t.get("freethrows") or t.get("freethrow"))

        def as_int(v: Optional[str]) -> int:
            try:
                return int(v)
            except Exception:
                return 0

        out[bucket] = {
            "fg": fg_m,
            "fga": fg_a,
            "fg3": fg3_m,
            "fg3a": fg3_a,
            "ft": ft_m,
            "fta": ft_a,
            "orb": 0,
            "drb": 0,
            "trb": as_int(t.get("rebounds") or t.get("totalrebounds")),
            "ast": as_int(t.get("assists")),
            "stl": as_int(t.get("steals")),
            "blk": as_int(t.get("blocks")),
            "tov": as_int(t.get("turnovers")),
            "pf": as_int(t.get("fouls") or t.get("personalfouls")),
            "pts": as_int(t.get("points")),
        }

    return out


def _build_players_from_raw(team_data: Dict[str, Any], home_abbrev: str, away_abbrev: str) -> Dict[str, Any]:
    players_raw = team_data.get("players_raw") or []
    players = {"home": [], "away": []}
    for entry in players_raw:
        abbrev = entry.get("team_abbrev", "").upper()
        lst = entry.get("players") or []
        if abbrev == home_abbrev.upper():
            players["home"].extend(lst)
        elif abbrev == away_abbrev.upper():
            players["away"].extend(lst)
    return players


def _compute_leaders(players: Dict[str, Any]) -> Dict[str, Any]:
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
                val = int(p.get("traditional", {}).get(stat_key, 0))
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


def build_dt_schema_from_espn(summary: Dict[str, Any]) -> Dict[str, Any]:
    comp = _extract_competition(summary)
    home_comp, away_comp = _extract_team_side(comp)

    meta = _parse_meta(summary, comp)
    teams = _parse_teams(home_comp, away_comp)
    quarters_basic = _parse_linescores(comp)

    box_data = _parse_boxscore(summary)
    home_abbrev = teams["home"]["tricode"]
    away_abbrev = teams["away"]["tricode"]
    team_totals_traditional = _map_team_totals_to_home_away(
        box_data, home_abbrev, away_abbrev
    )
    players = _build_players_from_raw(box_data, home_abbrev, away_abbrev)
    leaders = _compute_leaders(players)

    largest_lead = {"home": None, "away": None}

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
                            "off_rating": None,
                            "def_rating": None,
                            "net_rating": None,
                            "efg_pct": None,
                            "ts_pct": None,
                        },
                        "away": {
                            "off_rating": None,
                            "def_rating": None,
                            "net_rating": None,
                            "efg_pct": None,
                            "ts_pct": None,
                        },
                    },
                },
                "players": {
                    "home": [],
                    "away": [],
                },
            }
        )

    game_totals = {
        "traditional": team_totals_traditional,
        "advanced": {
            "home": {
                "off_rating": None,
                "def_rating": None,
                "net_rating": None,
                "efg_pct": None,
                "ts_pct": None,
                "pace": None,
            },
            "away": {
                "off_rating": None,
                "def_rating": None,
                "net_rating": None,
                "efg_pct": None,
                "ts_pct": None,
                "pace": None,
            },
        },
        "misc": {
            "home": {
                "pitp": None,
                "second_chance": None,
                "fast_break": None,
                "points_off_to": None,
            },
            "away": {
                "pitp": None,
                "second_chance": None,
                "fast_break": None,
                "points_off_to": None,
            },
        },
    }

    files = {
        "play_by_play_csv": "play_by_play.csv"
    }

    dt_data: Dict[str, Any] = {
        "meta": meta,
        "teams": teams,
        "largest_lead": largest_lead,
        "game_totals": game_totals,
        "players": players,
        "quarters": quarters,
        "leaders": leaders,
        "files": files,
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
    dt_data = build_dt_schema_from_espn(summary)
    out_path = save_dt_game_json(dt_data, fixtures_dir, ESPN_EVENT_ID)

    print("[Fetch ESPN] Done.")
    print(f"[Fetch ESPN] You can now run:")
    print(f"  python src/dt_game_report/generate_report.py --game-json fixtures/{out_path.name}")


if __name__ == "__main__":
    main()
