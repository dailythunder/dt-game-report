from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# Set this to the ESPN event id you want to pull.
# For example, 401810077 for the game you've been testing.
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
    This includes boxscore, plays (PbP), leaders, etc.
    """
    base_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
    params = {"event": event_id}
    print(f"[Fetch ESPN] Requesting summary for event {event_id} ...")
    resp = requests.get(base_url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    print("[Fetch ESPN] Summary fetched successfully.")
    return data


# ----------------- helpers for meta / teams / linescores -----------------


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
            "id": t.get("id") or "",
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


# ----------------- helpers for team totals (full game) -----------------


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
        "LAL": {...stats...},
        "OKC": {...stats...},
      }
    where keys are team abbreviations.
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

            # Names are based on the JSON you uploaded from ESPN
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


# ----------------- helpers for players (full game) -----------------


def _build_athlete_meta(summary: Dict[str, Any], teams_info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Build a mapping from athlete id -> {
      "team_id": "13",
      "side": "home"/"away",
      "name": "Rui Hachimura",
      "position": "F"
    }
    using boxscore.players.
    """
    box = summary.get("boxscore") or {}
    players_teams = box.get("players") or []

    # Map team id to side
    team_id_to_side: Dict[str, str] = {}
    for side in ("home", "away"):
        tid = teams_info[side]["id"]
        if tid:
            team_id_to_side[tid] = side

    athlete_meta: Dict[str, Dict[str, Any]] = {}

    for team_block in players_teams:
        team = team_block.get("team") or {}
        team_id = str(team.get("id") or "")
        side = team_id_to_side.get(team_id)
        if not side:
            continue

        stats_groups = team_block.get("statistics") or []
        if not stats_groups:
            continue
        group = stats_groups[0]
        athletes = group.get("athletes") or []
        keys = group.get("keys") or []

        for row in athletes:
            ath = row.get("athlete") or {}
            aid = str(ath.get("id") or "")
            if not aid:
                continue
            position_obj = ath.get("position") or {}
            pos = position_obj.get("abbreviation") or position_obj.get("displayName") or ""
            name = ath.get("displayName") or ath.get("shortName") or ""
            athlete_meta[aid] = {
                "team_id": team_id,
                "side": side,
                "name": name,
                "position": pos,
                "keys": keys,
            }

    return athlete_meta


def _parse_full_game_players(summary: Dict[str, Any],
                             teams_info: Dict[str, Any],
                             base_players_sample: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Build full-game player box for home/away, matching the keys from
    base_players_sample (the keys used in your example_game.json players).
    """
    box = summary.get("boxscore") or {}
    players_teams = box.get("players") or []

    # team id -> side
    team_id_to_side: Dict[str, str] = {}
    for side in ("home", "away"):
        tid = teams_info[side]["id"]
        if tid:
            team_id_to_side[tid] = side

    out_players: Dict[str, List[Dict[str, Any]]] = {"home": [], "away": []}

    for team_block in players_teams:
        team = team_block.get("team") or {}
        team_id = str(team.get("id") or "")
        side = team_id_to_side.get(team_id)
        if not side:
            continue

        stats_groups = team_block.get("statistics") or []
        if not stats_groups:
            continue
        group = stats_groups[0]
        keys = group.get("keys") or []
        athletes = group.get("athletes") or []

        for row in athletes:
            ath = row.get("athlete") or {}
            aid = str(ath.get("id") or "")
            name = ath.get("displayName") or ath.get("shortName") or ""
            pos_obj = ath.get("position") or {}
            pos = pos_obj.get("abbreviation") or pos_obj.get("displayName") or ""
            starter = row.get("starter", False)
            stats_list = row.get("stats") or []

            # Map ESPN keys to internal stats
            stat_map: Dict[str, Any] = {}
            for k, v in zip(keys, stats_list):
                stat_map[k] = v

            # Convenience interpreters
            fg_m, fg_a = _split_makes_attempts(stat_map.get("fieldGoalsMade-fieldGoalsAttempted"))
            tp_m, tp_a = _split_makes_attempts(stat_map.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
            ft_m, ft_a = _split_makes_attempts(stat_map.get("freeThrowsMade-freeThrowsAttempted"))

            def to_int(val: Any) -> int:
                try:
                    return int(val)
                except Exception:
                    return 0

            pts = to_int(stat_map.get("points"))
            reb = to_int(stat_map.get("rebounds"))
            ast = to_int(stat_map.get("assists"))
            stl = to_int(stat_map.get("steals"))
            blk = to_int(stat_map.get("blocks"))
            tov = to_int(stat_map.get("turnovers"))
            pf = to_int(stat_map.get("fouls"))
            minutes = stat_map.get("minutes") or ""

            # Build flat dict matching base sample keys
            flat: Dict[str, Any] = {}
            for key in base_players_sample:
                lk = key.lower()
                if key == "name":
                    flat[key] = name
                elif lk in ("pos", "position"):
                    flat[key] = pos
                elif lk in ("is_starter", "starter"):
                    flat[key] = starter
                elif lk in ("min", "minutes"):
                    flat[key] = minutes
                elif lk == "fg":
                    flat[key] = fg_m
                elif lk == "fga":
                    flat[key] = fg_a
                elif lk in ("fg3", "tp"):
                    flat[key] = tp_m
                elif lk in ("fg3a", "tpa", "three_pa"):
                    flat[key] = tp_a
                elif lk == "ft":
                    flat[key] = ft_m
                elif lk == "fta":
                    flat[key] = ft_a
                elif lk in ("trb", "reb", "rebs"):
                    flat[key] = reb
                elif lk == "ast":
                    flat[key] = ast
                elif lk == "stl":
                    flat[key] = stl
                elif lk == "blk":
                    flat[key] = blk
                elif lk in ("tov", "to"):
                    flat[key] = tov
                elif lk in ("pf", "fouls"):
                    flat[key] = pf
                elif lk == "pts":
                    flat[key] = pts
                else:
                    # default for unknown keys
                    flat[key] = flat.get(key, 0)

            out_players[side].append(flat)

    return out_players


# ----------------- helpers for PbP -> per-quarter stats -----------------


def _zero_stat_block() -> Dict[str, Any]:
    return {
        "fg": 0,
        "fga": 0,
        "fg3": 0,
        "fg3a": 0,
        "ft": 0,
        "fta": 0,
        "trb": 0,
        "oreb": 0,
        "dreb": 0,
        "ast": 0,
        "stl": 0,
        "blk": 0,
        "tov": 0,
        "pf": 0,
        "pts": 0,
    }


def _build_quarter_stats_from_plays(summary: Dict[str, Any],
                                    teams_info: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """
    Walk the ESPN 'plays' array and build per-quarter stats for
    teams and players. We keep a simple internal stat block and
    map it into the final schema later.
    """
    plays = summary.get("plays") or []

    # Map team id -> side
    team_id_to_side: Dict[str, str] = {}
    for side in ("home", "away"):
        tid = teams_info[side]["id"]
        if tid:
            team_id_to_side[tid] = side

    # We'll also build athlete id -> side/name/pos (for quarter players)
    box = summary.get("boxscore") or {}
    players_teams = box.get("players") or []
    athlete_meta: Dict[str, Dict[str, Any]] = {}
    for team_block in players_teams:
        team = team_block.get("team") or {}
        team_id = str(team.get("id") or "")
        side = team_id_to_side.get(team_id)
        if not side:
            continue
        stats_groups = team_block.get("statistics") or []
        if not stats_groups:
            continue
        group = stats_groups[0]
        athletes = group.get("athletes") or []
        for row in athletes:
            ath = row.get("athlete") or {}
            aid = str(ath.get("id") or "")
            if not aid:
                continue
            pos_obj = ath.get("position") or {}
            pos = pos_obj.get("abbreviation") or pos_obj.get("displayName") or ""
            name = ath.get("displayName") or ath.get("shortName") or ""
            athlete_meta[aid] = {
                "side": side,
                "name": name,
                "position": pos,
            }

    # quarter -> side -> stats block
    quarter_team: Dict[int, Dict[str, Dict[str, Any]]] = {}
    # quarter -> side -> athlete_id -> stats block
    quarter_players: Dict[int, Dict[str, Dict[str, Dict[str, Any]]]] = {}

    def get_team_block(q: int, side: str) -> Dict[str, Any]:
        quarter_team.setdefault(q, {})
        if side not in quarter_team[q]:
            quarter_team[q][side] = _zero_stat_block()
        return quarter_team[q][side]

    def get_player_block(q: int, side: str, athlete_id: str) -> Dict[str, Any]:
        quarter_players.setdefault(q, {})
        quarter_players[q].setdefault(side, {})
        if athlete_id not in quarter_players[q][side]:
            quarter_players[q][side][athlete_id] = _zero_stat_block()
        return quarter_players[q][side][athlete_id]

    for play in plays:
        period = play.get("period") or {}
        qnum = int(period.get("number") or 0)
        if qnum <= 0:
            continue

        team = play.get("team") or {}
        team_id = str(team.get("id") or "")
        side = team_id_to_side.get(team_id)

        participants = play.get("participants") or []
        text = (play.get("text") or "").lower()
        short_desc = (play.get("shortDescription") or "").lower()
        scoring = bool(play.get("scoringPlay"))
        shooting_play = bool(play.get("shootingPlay"))
        points_attempted = int(play.get("pointsAttempted") or 0)
        score_value = int(play.get("scoreValue") or 0)

        # Helpers to get athlete ids from participants
        def get_participant_id(idx: int) -> Optional[str]:
            if 0 <= idx < len(participants):
                ath = participants[idx].get("athlete") or {}
                aid = ath.get("id")
                if aid is not None:
                    return str(aid)
            return None

        # Free throws
        if "free throw" in text:
            shooter_id = get_participant_id(0)
            if not side or not shooter_id:
                continue
            team_block = get_team_block(qnum, side)
            player_block = get_player_block(qnum, side, shooter_id)
            team_block["fta"] += 1
            player_block["fta"] += 1
            if scoring:
                team_block["ft"] += 1
                player_block["ft"] += 1
                team_block["pts"] += score_value
                player_block["pts"] += score_value
            continue

        # Field goals (non-FT)
        if shooting_play and points_attempted in (2, 3):
            shooter_id = get_participant_id(0)
            if side and shooter_id:
                team_block = get_team_block(qnum, side)
                player_block = get_player_block(qnum, side, shooter_id)
                team_block["fga"] += 1
                player_block["fga"] += 1
                if points_attempted == 3:
                    team_block["fg3a"] += 1
                    player_block["fg3a"] += 1
                if scoring:
                    team_block["fg"] += 1
                    player_block["fg"] += 1
                    team_block["pts"] += score_value
                    player_block["pts"] += score_value
                    if points_attempted == 3:
                        team_block["fg3"] += 1
                        player_block["fg3"] += 1

                # assists: look for "(Name assists)" pattern via participants[1]
                if "assists" in text:
                    assister_id = get_participant_id(1)
                    if assister_id:
                        meta = athlete_meta.get(assister_id)
                        if meta:
                            a_side = meta["side"]
                            a_team_block = get_team_block(qnum, a_side)
                            a_player_block = get_player_block(qnum, a_side, assister_id)
                            a_team_block["ast"] += 1
                            a_player_block["ast"] += 1

        # Rebounds
        if "rebound" in text:
            reb_id = get_participant_id(0)
            if not reb_id:
                continue
            meta = athlete_meta.get(reb_id)
            if not meta:
                continue
            side_r = meta["side"]
            team_block = get_team_block(qnum, side_r)
            player_block = get_player_block(qnum, side_r, reb_id)
            team_block["trb"] += 1
            player_block["trb"] += 1
            if "offensive" in text:
                team_block["oreb"] += 1
                player_block["oreb"] += 1
            elif "defensive" in text:
                team_block["dreb"] += 1
                player_block["dreb"] += 1

        # Turnovers / steals
        if "turnover" in text:
            to_id = get_participant_id(0)
            if to_id:
                meta_to = athlete_meta.get(to_id)
                if meta_to:
                    s_to = meta_to["side"]
                    t_to = get_team_block(qnum, s_to)
                    p_to = get_player_block(qnum, s_to, to_id)
                    t_to["tov"] += 1
                    p_to["tov"] += 1
            # steals in same text
            if "steals" in text:
                stl_id = get_participant_id(1)
                if stl_id:
                    meta_st = athlete_meta.get(stl_id)
                    if meta_st:
                        s_st = meta_st["side"]
                        t_st = get_team_block(qnum, s_st)
                        p_st = get_player_block(qnum, s_st, stl_id)
                        t_st["stl"] += 1
                        p_st["stl"] += 1

        # Blocks
        if "blocks" in text:
            # pattern like "Chet Holmgren blocks Deandre Ayton's shot"
            blk_id = get_participant_id(1)
            if blk_id:
                meta_b = athlete_meta.get(blk_id)
                if meta_b:
                    s_b = meta_b["side"]
                    t_b = get_team_block(qnum, s_b)
                    p_b = get_player_block(qnum, s_b, blk_id)
                    t_b["blk"] += 1
                    p_b["blk"] += 1

        # Fouls
        if "foul" in text:
            foul_id = get_participant_id(0)
            if foul_id:
                meta_f = athlete_meta.get(foul_id)
                if meta_f:
                    s_f = meta_f["side"]
                    t_f = get_team_block(qnum, s_f)
                    p_f = get_player_block(qnum, s_f, foul_id)
                    t_f["pf"] += 1
                    p_f["pf"] += 1

    return {
        "team": quarter_team,
        "players": quarter_players,
        "athletes": athlete_meta,
    }


# ----------------- leaders (from full-game players) -----------------


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


# ----------------- main DT schema builder -----------------


def build_dt_schema_from_espn(summary: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert ESPN summary JSON into the DT Game Report JSON schema,
    aligning with the structure of example_game.json.
    This version does three big things:
      1) Real full-game team totals (traditional + misc + largest lead)
      2) Real full-game player box (home/away)
      3) Real per-quarter team + player traditional stats from PbP
    """
    comp = _extract_competition(summary)
    home_comp, away_comp = _extract_team_side(comp)

    meta = _parse_meta(summary, comp)
    teams_info = _parse_teams(home_comp, away_comp)
    quarters_basic = _parse_linescores(comp)

    team_totals_by_abbrev = _parse_team_totals(summary)
    home_abbrev = teams_info["home"]["tricode"]
    away_abbrev = teams_info["away"]["tricode"]

    # Start from the base example structure
    data = base

    # Meta
    data.setdefault("meta", {})
    data["meta"].update(meta)

    # Teams
    data.setdefault("teams", {"home": {}, "away": {}})
    data["teams"]["home"].update(teams_info["home"])
    data["teams"]["away"].update(teams_info["away"])

    # Ensure game_totals.traditional exists
    data.setdefault("game_totals", {})
    data["game_totals"].setdefault("traditional", {})
    data["game_totals"]["traditional"].setdefault("home", {})
    data["game_totals"]["traditional"].setdefault("away", {})

    # Also ensure misc + largest_lead exist
    data["game_totals"].setdefault("misc", {"home": {}, "away": {}})
    data.setdefault("largest_lead", {"home": 0, "away": 0})

    # Fill team totals for full game
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
            lk = key.lower()
            if lk in ("fg_pct", "fgp"):
                new_side[key] = pct(fg, fga)
            elif lk in ("fg3_pct", "tp_pct", "three_pct"):
                new_side[key] = pct(fg3, fg3a)
            elif lk in ("ft_pct", "ftp"):
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

    fill_side("home", home_abbrev)
    fill_side("away", away_abbrev)

    # ----------------- full-game players -----------------
    # Figure out what keys your players use from the example file
    base_players_home = data.get("players", {}).get("home", [])
    if base_players_home:
        base_player_keys = list(base_players_home[0].keys())
    else:
        base_player_keys = [
            "name", "position", "starter", "min",
            "fg", "fga", "fg3", "fg3a", "ft", "fta",
            "trb", "ast", "stl", "blk", "tov", "pf", "pts",
        ]

    full_players = _parse_full_game_players(summary, teams_info, base_player_keys)
    data.setdefault("players", {})
    data["players"]["home"] = full_players["home"]
    data["players"]["away"] = full_players["away"]

    # Leaders from full-game players
    data["leaders"] = _compute_leaders(full_players)

    # ----------------- per-quarter from PbP -----------------
    quarter_raw = _build_quarter_stats_from_plays(summary, teams_info)

    # Quarter player key template
    base_quarters = data.get("quarters", [])
    quarter_player_keys: List[str]
    if base_quarters and base_quarters[0].get("players", {}).get("home"):
        quarter_player_keys = list(base_quarters[0]["players"]["home"][0].keys())
    else:
        quarter_player_keys = base_player_keys

    def map_stats_to_keys(stats_block: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k in keys:
            lk = k.lower()
            if k == "name":
                out[k] = stats_block.get("name", "")
            elif lk in ("pos", "position"):
                out[k] = stats_block.get("position", "")
            elif lk in ("is_starter", "starter"):
                # We don't know exact starter by quarter; leave False.
                out[k] = stats_block.get("starter", False)
            elif lk in ("min", "minutes"):
                # No per-quarter minutes; leave blank.
                out[k] = stats_block.get("min", "")
            elif lk == "fg":
                out[k] = stats_block.get("fg", 0)
            elif lk == "fga":
                out[k] = stats_block.get("fga", 0)
            elif lk in ("fg3", "tp"):
                out[k] = stats_block.get("fg3", 0)
            elif lk in ("fg3a", "tpa", "three_pa"):
                out[k] = stats_block.get("fg3a", 0)
            elif lk == "ft":
                out[k] = stats_block.get("ft", 0)
            elif lk == "fta":
                out[k] = stats_block.get("fta", 0)
            elif lk in ("trb", "reb", "rebs"):
                out[k] = stats_block.get("trb", 0)
            elif lk == "ast":
                out[k] = stats_block.get("ast", 0)
            elif lk == "stl":
                out[k] = stats_block.get("stl", 0)
            elif lk == "blk":
                out[k] = stats_block.get("blk", 0)
            elif lk in ("tov", "to"):
                out[k] = stats_block.get("tov", 0)
            elif lk in ("pf", "fouls"):
                out[k] = stats_block.get("pf", 0)
            elif lk == "pts":
                out[k] = stats_block.get("pts", 0)
            else:
                out[k] = out.get(k, 0)
        return out

    # Build new quarters list entirely from linescores + PbP
    new_quarters: List[Dict[str, Any]] = []

    for qinfo in quarters_basic:
        qnum = qinfo["number"]
        q_home_score = qinfo["home_score"]
        q_away_score = qinfo["away_score"]

        # Raw stats from PbP
        team_stats_q = quarter_raw["team"].get(qnum, {})
        player_stats_q = quarter_raw["players"].get(qnum, {})

        # Team totals mapped to whatever keys exist in base quarter team_totals.traditional
        # If base has quarter team_totals, use its keys; otherwise reuse game_totals keys.
        if base_quarters and base_quarters[0].get("team_totals", {}).get("traditional", {}).get("home"):
            base_q_team_keys = list(base_quarters[0]["team_totals"]["traditional"]["home"].keys())
        else:
            base_q_team_keys = list(data["game_totals"]["traditional"]["home"].keys())

        def map_team_side(side: str) -> Dict[str, Any]:
            raw = team_stats_q.get(side, _zero_stat_block())
            mapped: Dict[str, Any] = {}
            for key in base_q_team_keys:
                lk = key.lower()
                if lk == "fg":
                    mapped[key] = raw["fg"]
                elif lk == "fga":
                    mapped[key] = raw["fga"]
                elif lk in ("fg3", "tp"):
                    mapped[key] = raw["fg3"]
                elif lk in ("fg3a", "tpa", "three_pa"):
                    mapped[key] = raw["fg3a"]
                elif lk == "ft":
                    mapped[key] = raw["ft"]
                elif lk == "fta":
                    mapped[key] = raw["fta"]
                elif lk in ("trb", "reb", "rebs"):
                    mapped[key] = raw["trb"]
                elif lk == "ast":
                    mapped[key] = raw["ast"]
                elif lk == "stl":
                    mapped[key] = raw["stl"]
                elif lk == "blk":
                    mapped[key] = raw["blk"]
                elif lk in ("tov", "to"):
                    mapped[key] = raw["tov"]
                elif lk in ("pf", "fouls"):
                    mapped[key] = raw["pf"]
                elif lk == "pts":
                    mapped[key] = raw["pts"]
                elif "pct" in lk:
                    # compute simple percentage from counts
                    if "fg3" in lk:
                        made = raw["fg3"]
                        att = raw["fg3a"]
                    elif "ft" in lk:
                        made = raw["ft"]
                        att = raw["fta"]
                    else:
                        made = raw["fg"]
                        att = raw["fga"]
                    mapped[key] = round(made / att * 100, 1) if att else 0.0
                else:
                    mapped[key] = mapped.get(key, 0)
            return mapped

        team_totals_trad = {
            "home": map_team_side("home"),
            "away": map_team_side("away"),
        }

        # Quarter players: map each athlete to a flat player dict
        q_players_home: List[Dict[str, Any]] = []
        q_players_away: List[Dict[str, Any]] = []

        for side in ("home", "away"):
            side_players_raw = player_stats_q.get(side, {})
            for aid, stats_block in side_players_raw.items():
                meta = quarter_raw["athletes"].get(aid, {})
                stats_block = dict(stats_block)
                stats_block["name"] = meta.get("name", "")
                stats_block["position"] = meta.get("position", "")
                flat = map_stats_to_keys(stats_block, quarter_player_keys)
                if side == "home":
                    q_players_home.append(flat)
                else:
                    q_players_away.append(flat)

        new_quarters.append(
            {
                "number": qnum,
                "home_score": q_home_score,
                "away_score": q_away_score,
                "team_totals": {
                    "traditional": {
                        "home": team_totals_trad["home"],
                        "away": team_totals_trad["away"],
                    },
                    # Keep advanced structure from base, but we don't compute it yet
                    "advanced": base_quarters[0]["team_totals"]["advanced"] if base_quarters else {
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
                    "home": q_players_home,
                    "away": q_players_away,
                },
            }
        )

    data["quarters"] = new_quarters

    # Ensure files block exists for PBP CSV pointer (even if we haven't wired CSV yet)
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
