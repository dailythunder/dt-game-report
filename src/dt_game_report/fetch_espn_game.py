from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Use your current test game
ESPN_EVENT_ID = "401810077"


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


def _parse_team_totals(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Parse team totals (traditional + misc) from ESPN boxscore. Returns dict:
      {
        "HOME_ABBR": {...stats...},
        "AWAY_ABBR": {...stats...},
      }
    """
    box = summary.get("boxscore") or {}
    teams_stats = box.get("teams") or []

    results: Dict[str, Dict[str, Any]] = {}

    for team_entry in teams_stats:
        team_info = team_entry.get("team") or {}
        abbrev = (team_info.get("abbreviation") or "").upper()
        stats = team_entry.get("statistics") or []
        out: Dict[str, Any] = {}

        for s in stats:
            name = (s.get("name") or "").lower()
            val = s.get("displayValue")

            # These names are based on the JSON you uploaded
            if name in ("fieldgoalsmade-fieldgoalsattempted", "fieldgoals"):
                fg_m, fg_a = _split_makes_attempts(val)
                out["fg"] = fg_m
                out["fga"] = fg_a
            elif name in ("threepointfieldgoalsmade-threepointfieldgoalsattempted", "threepointfieldgoals"):
                fg3_m, fg3_a = _split_makes_attempts(val)
                out["fg3"] = fg3_m
                out["fg3a"] = fg3_a
            elif name in ("freethrowsmade-freethrowsattempted", "freethrows"):
                ft_m, ft_a = _split_makes_attempts(val)
                out["ft"] = ft_m
                out["fta"] = ft_a
            elif name in ("totalrebounds", "rebounds"):
                try:
                    out["trb"] = int(val)
                except Exception:
                    out["trb"] = 0
            elif name == "assists":
                try:
                    out["ast"] = int(val)
                except Exception:
                    out["ast"] = 0
            elif name == "steals":
                try:
                    out["stl"] = int(val)
                except Exception:
                    out["stl"] = 0
            elif name == "blocks":
                try:
                    out["blk"] = int(val)
                except Exception:
                    out["blk"] = 0
            elif name == "turnovers":
                try:
                    out["tov"] = int(val)
                except Exception:
                    out["tov"] = 0
            elif name in ("fouls", "personalfouls"):
                try:
                    out["pf"] = int(val)
                except Exception:
                    out["pf"] = 0
            elif name == "points":
                try:
                    out["pts"] = int(val)
                except Exception:
                    out["pts"] = 0
            elif name == "pointsinthepaint":
                try:
                    out["pitp"] = int(val)
                except Exception:
                    out["pitp"] = 0
            elif name == "secondchancepoints":
                try:
                    out["second_chance"] = int(val)
                except Exception:
                    out["second_chance"] = 0
            elif name == "fastbreakpoints":
                try:
                    out["fast_break"] = int(val)
                except Exception:
                    out["fast_break"] = 0
            elif name == "pointsoffturnovers":
                try:
                    out["points_off_to"] = int(val)
                except Exception:
                    out["points_off_to"] = 0
            elif name == "largestlead":
                try:
                    out["largest_lead"] = int(val)
                except Exception:
                    out["largest_lead"] = 0

        results[abbrev] = out

    return results


def build_dt_schema_from_espn(summary: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert ESPN summary JSON into the DT Game Report JSON schema,
    aligning with the structure of example_game.json.
    """
    comp = _extract_competition(summary)
    home_comp, away_comp = _extract_team_side(comp)

    meta = _parse_meta(summary, comp)
    teams = _parse_teams(home_comp, away_comp)
    quarters_basic = _parse_linescores(comp)

    team_totals_by_abbrev = _parse_team_totals(summary)
    home_abbrev = teams["home"]["tricode"]
    away_abbrev = teams["away"]["tricode"]

    # Start from the base example structure
    data = base

    # Meta
    data.setdefault("meta", {})
    data["meta"].update(meta)

    # Teams
    data.setdefault("teams", {"home": {}, "away": {}})
    data["teams"]["home"].update(teams["home"])
    data["teams"]["away"].update(teams["away"])

    # Ensure game_totals.traditional exists
    data.setdefault("game_totals", {})
    data["game_totals"].setdefault("traditional", {})
    data["game_totals"]["traditional"].setdefault("home", {})
    data["game_totals"]["traditional"].setdefault("away", {})

    # Also ensure misc + largest_lead exist
    data["game_totals"].setdefault("misc", {"home": {}, "away": {}})
    data.setdefault("largest_lead", {"home": 0, "away": 0})

    def fill_side(side_key: str, abbrev: str) -> None:
        side_stats = team_totals_by_abbrev.get(abbrev.upper(), {})
        base_side = data["game_totals"]["traditional"].get(side_key, {})

        fg = side_stats.get("fg", 0)
        fga = side_stats.get("fga", 0)
        fg3 = side_stats.get("fg3", 0)
        fg3a = side_stats.get("fg3a", 0)
        ft = side_stats.get("ft", 0)
        fta = side_stats.get("fta", 0)

        def pct(made: int, att: int) -> float:
            return round(made / att * 100, 1) if att else 0.0

        new_side: Dict[str, Any] = dict(base_side)
        # Core counting stats
        new_side["fg"] = fg
        new_side["fga"] = fga
        new_side["fg3"] = fg3
        new_side["fg3a"] = fg3a
        new_side["ft"] = ft
        new_side["fta"] = fta
        new_side["trb"] = side_stats.get("trb", 0)
        new_side["ast"] = side_stats.get("ast", 0)
        new_side["stl"] = side_stats.get("stl", 0)
        new_side["blk"] = side_stats.get("blk", 0)
        new_side["tov"] = side_stats.get("tov", 0)
        new_side["pf"] = side_stats.get("pf", 0)
        new_side["pts"] = side_stats.get("pts", 0)

        # Percentages, using whatever names exist in base
        for key in base_side.keys():
            if key.lower() in ("fg_pct", "fgp"):
                new_side[key] = pct(fg, fga)
            elif key.lower() in ("fg3_pct", "tp_pct", "three_pct"):
                new_side[key] = pct(fg3, fg3a)
            elif key.lower() in ("ft_pct", "ftp"):
                new_side[key] = pct(ft, fta)

        data["game_totals"]["traditional"][side_key] = new_side

        # Misc stats
        misc = data["game_totals"].setdefault("misc", {"home": {}, "away": {}})
        misc_side = misc.setdefault(side_key, {})
        misc_side["pitp"] = side_stats.get("pitp", 0)
        misc_side["second_chance"] = side_stats.get("second_chance", 0)
        misc_side["fast_break"] = side_stats.get("fast_break", 0)
        misc_side["points_off_to"] = side_stats.get("points_off_to", 0)

        # Largest lead
        if side_stats.get("largest_lead") is not None:
            try:
                data["largest_lead"][side_key] = int(side_stats["largest_lead"])
            except Exception:
                pass

    # Fill home/away team totals
    fill_side("home", home_abbrev)
    fill_side("away", away_abbrev)

    # Quarters: update scores only, keep structure from base
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
                    "team_totals": base_quarters[0].get("team_totals", {}) if base_quarters else {},
                    "players": base_quarters[0].get("players", {}) if base_quarters else {},
                }
            )
    data["quarters"] = base_quarters

    # Ensure files block exists for PBP CSV pointer
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
