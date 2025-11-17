import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("dt_game_report.lab_quarters_and_runs")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"


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
    name = candidates[0].name  # espn_summary_401810084.json
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
    header = summary.get("header", {})
    competitions = header.get("competitions") or []
    if not competitions:
        raise RuntimeError("No competitions in ESPN summary JSON")
    comp = competitions[0]
    competitors = comp.get("competitors") or []

    teams_by_side: Dict[str, Dict[str, Any]] = {}
    team_id_to_side: Dict[str, str] = {}
    for c in competitors:
        side = c.get("homeAway")
        team = c.get("team", {}) or {}
        tid = str(team.get("id")) if team.get("id") is not None else None
        if not tid or not side:
            continue
        team_id_to_side[tid] = side
        teams_by_side[side] = {
            "id": tid,
            "name": team.get("shortDisplayName") or team.get("displayName"),
        }
    return {
        "teams_by_side": teams_by_side,
        "team_id_to_side": team_id_to_side,
        "competition": comp,
    }


def _build_player_maps(
    summary: Dict[str, Any],
    team_id_to_side: Dict[str, str],
) -> Dict[str, Any]:
    """
    Build a mapping of player (athlete) ids to basic info.

    Returns:
      {
        "players_by_id": {
            "athlete_id": {
                "id": "athlete_id",
                "name": "Shai Gilgeous-Alexander",
                "short_name": "S. Gilgeous-Alexander",
                "jersey": "2",
                "team_id": "1610612760",
                "side": "home" | "away" | None,
            },
            ...
        }
      }
    """
    players_by_id: Dict[str, Dict[str, Any]] = {}

    boxscore = summary.get("boxscore") or {}
    teams_players = boxscore.get("players") or []

    for team_block in teams_players:
        team = team_block.get("team") or {}
        tid = str(team.get("id")) if team.get("id") is not None else None
        side = team_id_to_side.get(tid)

        for stat_block in team_block.get("statistics") or []:
            for ath_entry in stat_block.get("athletes") or []:
                athlete = ath_entry.get("athlete") or {}
                aid = athlete.get("id")
                if aid is None:
                    continue
                aid_str = str(aid)
                if aid_str in players_by_id:
                    # Don't overwrite; first occurrence is enough for identity
                    continue

                players_by_id[aid_str] = {
                    "id": aid_str,
                    "name": athlete.get("displayName"),
                    "short_name": athlete.get("shortName"),
                    "jersey": athlete.get("jersey"),
                    "team_id": tid,
                    "side": side,
                }

    return {"players_by_id": players_by_id}


def _extract_basic_play_sequence(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten plays into a simpler list we can reason about.

    Each item:
        {
          'index': int,
          'period': int,
          'clock': '6:32',
          'home_score': int,
          'away_score': int,
          'team_id': str | None,
          'scoring_play': bool,
          'score_value': int,
          'text': str,
          'athlete_ids': [str, ...],
          'play_type_id': str | None,
          'play_type_text': str | None,
          'shooting_play': bool,
          'points_attempted': int,
          'short_desc': str,
        }
    """
    plays_raw = summary.get("plays") or []
    seq: List[Dict[str, Any]] = []

    last_home = 0
    last_away = 0

    for idx, p in enumerate(plays_raw):
        period = (p.get("period") or {}).get("number")
        clock = (p.get("clock") or {}).get("displayValue") or p.get("clock") or ""
        text = p.get("text") or ""
        team = p.get("team") or {}
        team_id = str(team.get("id")) if team.get("id") is not None else None

        home_score = p.get("homeScore")
        away_score = p.get("awayScore")

        try:
            home_score = int(home_score)
        except Exception:
            home_score = last_home
        try:
            away_score = int(away_score)
        except Exception:
            away_score = last_away

        scoring_play = bool(p.get("scoringPlay"))
        score_val = 0
        raw_sv = p.get("scoreValue")
        try:
            score_val = int(raw_sv)
        except Exception:
            # fallback: derive from score delta
            delta_home = home_score - last_home
            delta_away = away_score - last_away
            score_val = max(delta_home, delta_away, 0)

        athlete_ids: List[str] = []
        participants = p.get("participants") or p.get("athletesInvolved") or []
        for part in participants:
            athlete = part.get("athlete") if isinstance(part, dict) else None
            if isinstance(athlete, dict):
                aid = athlete.get("id")
                if aid is not None:
                    athlete_ids.append(str(aid))
                    continue
            if isinstance(part, dict):
                aid = part.get("id")
                if aid is not None:
                    athlete_ids.append(str(aid))

        play_type = p.get("type") or {}
        play_type_id = play_type.get("id")
        play_type_text = play_type.get("text")
        shooting_play = bool(p.get("shootingPlay"))
        points_attempted = p.get("pointsAttempted")
        try:
            points_attempted = int(points_attempted)
        except Exception:
            points_attempted = 0
        short_desc = p.get("shortDescription") or ""

        seq.append(
            {
                "index": idx,
                "period": int(period) if period is not None else None,
                "clock": str(clock),
                "home_score": home_score,
                "away_score": away_score,
                "team_id": team_id,
                "scoring_play": scoring_play,
                "score_value": score_val if scoring_play else 0,
                "text": text,
                "athlete_ids": athlete_ids,
                "play_type_id": play_type_id,
                "play_type_text": play_type_text,
                "shooting_play": shooting_play,
                "points_attempted": points_attempted,
                "short_desc": short_desc,
            }
        )

        last_home = home_score
        last_away = away_score

    return seq


def _empty_stats() -> Dict[str, int]:
    return {
        "pts": 0,
        "fgm": 0,
        "fga": 0,
        "tpm": 0,
        "tpa": 0,
        "ftm": 0,
        "fta": 0,
        "reb": 0,
        "oreb": 0,
        "dreb": 0,
        "ast": 0,
        "stl": 0,
        "blk": 0,
        "tov": 0,
    }


def compute_quarter_team_and_player_totals(
    plays_seq: List[Dict[str, Any]],
    team_id_to_side: Dict[str, str],
    players_by_id: Dict[str, Dict[str, Any]],
) -> (
    Dict[int, Dict[str, Dict[str, int]]],
    Dict[int, Dict[str, Dict[str, Any]]],
):
    """
    Compute by-quarter team + player box-style totals.

    Returns:
      quarter_team_totals: {quarter: {side: stats_dict}}
      quarter_player_totals: {
        quarter: {
          player_id: {
            'player_id', 'name', 'team_id', 'side',
            ...stat fields...
          }, ...
        }, ...
      }
    """
    quarter_team_totals: Dict[int, Dict[str, Dict[str, int]]] = {}
    quarter_player_totals: Dict[int, Dict[str, Dict[str, Any]]] = {}

    def get_team_stats(period: int, side: str) -> Dict[str, int]:
        if period not in quarter_team_totals:
            quarter_team_totals[period] = {}
        if side not in quarter_team_totals[period]:
            quarter_team_totals[period][side] = _empty_stats()
        return quarter_team_totals[period][side]

    def get_player_stats(period: int, player_id: str) -> Dict[str, Any]:
        if period not in quarter_player_totals:
            quarter_player_totals[period] = {}
        if player_id not in quarter_player_totals[period]:
            meta = players_by_id.get(player_id, {})
            team_id = meta.get("team_id")
            side = meta.get("side") or (
                team_id_to_side.get(team_id) if team_id else None
            )
            quarter_player_totals[period][player_id] = {
                "player_id": player_id,
                "name": meta.get("name"),
                "team_id": team_id,
                "side": side,
                **_empty_stats(),
            }
        return quarter_player_totals[period][player_id]

    for pl in plays_seq:
        period = pl.get("period")
        if period is None:
            continue

        text_lower = (pl.get("text") or "").lower()
        type_text_lower = (pl.get("play_type_text") or "").lower()
        short_lower = (pl.get("short_desc") or "").lower()
        shooting_play = pl.get("shooting_play")
        points_attempted = pl.get("points_attempted") or 0
        scoring_play = pl.get("scoring_play")
        score_val = pl.get("score_value") or 0
        team_id = pl.get("team_id")
        side_for_team = team_id_to_side.get(team_id) if team_id else None
        athlete_ids = pl.get("athlete_ids") or []

        # FIELD GOALS & FREE THROWS
        if shooting_play and points_attempted > 0:
            is_free_throw = "free throw" in type_text_lower or "free throw" in text_lower

            if is_free_throw:
                # Free throw attempt
                if athlete_ids:
                    shooter_id = athlete_ids[0]
                    shooter_meta = players_by_id.get(shooter_id, {})
                    shooter_team_id = shooter_meta.get("team_id")
                    shooter_side = shooter_meta.get("side") or (
                        team_id_to_side.get(shooter_team_id)
                        if shooter_team_id
                        else None
                    )

                    if shooter_side:
                        ts = get_team_stats(period, shooter_side)
                        ts["fta"] += 1
                        if scoring_play and score_val > 0:
                            ts["ftm"] += 1
                            ts["pts"] += score_val

                    ps = get_player_stats(period, shooter_id)
                    ps["fta"] += 1
                    if scoring_play and score_val > 0:
                        ps["ftm"] += 1
                        ps["pts"] += score_val

            else:
                # Field goal attempt
                is_three = ("three point" in text_lower) or ("3pt" in short_lower)
                shooter_id = athlete_ids[0] if athlete_ids else None
                made = bool(scoring_play and score_val > 0)

                # Player FG / 3PT / PTS
                if shooter_id:
                    ps = get_player_stats(period, shooter_id)
                    ps["fga"] += 1
                    if is_three:
                        ps["tpa"] += 1
                    if made:
                        ps["fgm"] += 1
                        ps["pts"] += score_val
                        if is_three:
                            ps["tpm"] += 1

                # Team FG / 3PT / PTS
                side = None
                if shooter_id and shooter_id in players_by_id:
                    meta = players_by_id[shooter_id]
                    tid = meta.get("team_id")
                    side = meta.get("side") or (
                        team_id_to_side.get(tid) if tid else None
                    )
                else:
                    side = side_for_team

                if side:
                    ts = get_team_stats(period, side)
                    ts["fga"] += 1
                    if is_three:
                        ts["tpa"] += 1
                    if made:
                        ts["fgm"] += 1
                        ts["pts"] += score_val
                        if is_three:
                            ts["tpm"] += 1

                # Assists: only on made, non-FT
                if made and "assists" in text_lower and len(athlete_ids) > 1:
                    assister_id = athlete_ids[1]
                    ps_ast = get_player_stats(period, assister_id)
                    ps_ast["ast"] += 1
                    meta = players_by_id.get(assister_id, {})
                    side_ast = meta.get("side") or (
                        team_id_to_side.get(meta.get("team_id"))
                        if meta.get("team_id")
                        else None
                    )
                    if side_ast:
                        ts_ast = get_team_stats(period, side_ast)
                        ts_ast["ast"] += 1

        # REBOUNDS
        if "rebound" in type_text_lower:
            is_off = "offensive" in type_text_lower
            if athlete_ids:
                reb_id = athlete_ids[0]
                ps = get_player_stats(period, reb_id)
                ps["reb"] += 1
                if is_off:
                    ps["oreb"] += 1
                else:
                    ps["dreb"] += 1

                meta = players_by_id.get(reb_id, {})
                side = meta.get("side") or (
                    team_id_to_side.get(meta.get("team_id"))
                    if meta.get("team_id")
                    else None
                )
                if side:
                    ts = get_team_stats(period, side)
                    ts["reb"] += 1
                    if is_off:
                        ts["oreb"] += 1
                    else:
                        ts["dreb"] += 1
            else:
                # team rebound
                if side_for_team:
                    ts = get_team_stats(period, side_for_team)
                    ts["reb"] += 1
                    if is_off:
                        ts["oreb"] += 1
                    else:
                        ts["dreb"] += 1

        # TURNOVERS
        if "turnover" in type_text_lower or "turnover" in text_lower or "offensive foul" in text_lower:
            if athlete_ids:
                tov_id = athlete_ids[0]
                ps = get_player_stats(period, tov_id)
                ps["tov"] += 1
                meta = players_by_id.get(tov_id, {})
                side = meta.get("side") or (
                    team_id_to_side.get(meta.get("team_id"))
                    if meta.get("team_id")
                    else None
                )
                if side:
                    ts = get_team_stats(period, side)
                    ts["tov"] += 1
            elif side_for_team:
                ts = get_team_stats(period, side_for_team)
                ts["tov"] += 1

        # STEALS (usually in turnover text)
        if "steals" in text_lower and len(athlete_ids) > 1:
            stealer_id = athlete_ids[1]
            ps = get_player_stats(period, stealer_id)
            ps["stl"] += 1
            meta = players_by_id.get(stealer_id, {})
            side = meta.get("side") or (
                team_id_to_side.get(meta.get("team_id"))
                if meta.get("team_id")
                else None
            )
            if side:
                ts = get_team_stats(period, side)
                ts["stl"] += 1

        # BLOCKS
        if "blocks" in text_lower and len(athlete_ids) > 1:
            blocker_id = athlete_ids[1]
            ps = get_player_stats(period, blocker_id)
            ps["blk"] += 1
            meta = players_by_id.get(blocker_id, {})
            side = meta.get("side") or (
                team_id_to_side.get(meta.get("team_id"))
                if meta.get("team_id")
                else None
            )
            if side:
                ts = get_team_stats(period, side)
                ts["blk"] += 1

    return quarter_team_totals, quarter_player_totals


def compute_quarter_team_points(
    plays_seq: List[Dict[str, Any]], team_id_to_side: Dict[str, str]
) -> Dict[int, Dict[str, int]]:
    """Return points by quarter and side: {quarter: {'home': pts, 'away': pts}}."""
    result: Dict[int, Dict[str, int]] = {}
    for pl in plays_seq:
        if not pl.get("scoring_play"):
            continue
        period = pl.get("period")
        if period is None:
            continue
        team_id = pl.get("team_id")
        side = team_id_to_side.get(team_id) if team_id else None
        if side not in ("home", "away"):
            continue
        pts = int(pl.get("score_value") or 0)
        if period not in result:
            result[period] = {"home": 0, "away": 0}
        result[period][side] += pts
    return result


def compute_unanswered_runs(
    plays_seq: List[Dict[str, Any]], team_id_to_side: Dict[str, str], min_points: int = 7
) -> List[Dict[str, Any]]:
    """Compute 7+ point unanswered runs by either team."""
    runs: List[Dict[str, Any]] = []

    current_side: Optional[str] = None
    current_team_id: Optional[str] = None
    current_points = 0
    start_play: Optional[Dict[str, Any]] = None

    def flush_run(last_play: Optional[Dict[str, Any]]) -> None:
        nonlocal current_side, current_team_id, current_points, start_play
        if current_side and current_points >= min_points and start_play and last_play:
            runs.append(
                {
                    "side": current_side,
                    "team_id": current_team_id,
                    "points": current_points,
                    "start_index": start_play["index"],
                    "end_index": last_play["index"],
                    "start_period": start_play.get("period"),
                    "start_clock": start_play.get("clock"),
                    "end_period": last_play.get("period"),
                    "end_clock": last_play.get("clock"),
                }
            )
        current_side = None
        current_team_id = None
        current_points = 0
        start_play = None

    for pl in plays_seq:
        if not pl.get("scoring_play"):
            continue
        team_id = pl.get("team_id")
        side = team_id_to_side.get(team_id) if team_id else None
        if side not in ("home", "away"):
            flush_run(pl)
            continue
        pts = int(pl.get("score_value") or 0)

        if current_side is None:
            current_side = side
            current_team_id = team_id
            current_points = pts
            start_play = pl
        elif side == current_side:
            current_points += pts
        else:
            flush_run(pl)
            current_side = side
            current_team_id = team_id
            current_points = pts
            start_play = pl

    if plays_seq:
        flush_run(plays_seq[-1])

    return runs


def compute_net_runs(
    plays_seq: List[Dict[str, Any]], team_id_to_side: Dict[str, str], min_margin: int = 8
) -> List[Dict[str, Any]]:
    """Compute 8+ point net runs by either team."""
    runs: List[Dict[str, Any]] = []

    current_side: Optional[str] = None
    current_team_id: Optional[str] = None
    start_play: Optional[Dict[str, Any]] = None
    start_margin_side: Optional[int] = None
    current_net: int = 0
    last_scoring_play: Optional[Dict[str, Any]] = None

    def margin_for_side(pl: Dict[str, Any], side: str) -> int:
        margin = int(pl.get("home_score", 0)) - int(pl.get("away_score", 0))
        return margin if side == "home" else -margin

    def flush_run(final_play: Optional[Dict[str, Any]]) -> None:
        nonlocal current_side, current_team_id, start_play, start_margin_side, current_net
        if current_side and start_play and final_play and current_net >= min_margin:
            runs.append(
                {
                    "side": current_side,
                    "team_id": current_team_id,
                    "net_points": current_net,
                    "start_index": start_play["index"],
                    "end_index": final_play["index"],
                    "start_period": start_play.get("period"),
                    "start_clock": start_play.get("clock"),
                    "end_period": final_play.get("period"),
                    "end_clock": final_play.get("clock"),
                }
            )
        current_side = None
        current_team_id = None
        start_play = None
        start_margin_side = None
        current_net = 0

    for pl in plays_seq:
        if not pl.get("scoring_play"):
            continue

        last_scoring_play = pl

        team_id = pl.get("team_id")
        side_scorer = team_id_to_side.get(team_id) if team_id else None
        if side_scorer not in ("home", "away"):
            flush_run(pl)
            continue

        if current_side is None:
            current_side = side_scorer
            current_team_id = team_id
            start_play = pl
            start_margin_side = margin_for_side(pl, current_side)
            current_net = 0
            continue

        cur_margin_side = margin_for_side(pl, current_side)
        net_change = cur_margin_side - (start_margin_side or 0)

        if side_scorer == current_side:
            current_net = net_change
        else:
            if net_change < 0:
                flush_run(pl)
                current_side = side_scorer
                current_team_id = team_id
                start_play = pl
                start_margin_side = margin_for_side(pl, current_side)
                current_net = 0
            else:
                current_net = net_change

    if last_scoring_play is not None:
        flush_run(last_scoring_play)

    return runs


def compute_highlight_runs(
    plays_seq: List[Dict[str, Any]],
    team_id_to_side: Dict[str, str],
    min_for: int = 8,
    max_against: int = 5,
) -> List[Dict[str, Any]]:
    """Compute highlight runs using start-at-each-scoring-play logic."""
    runs: List[Dict[str, Any]] = []

    scoring_events = [pl for pl in plays_seq if pl.get("scoring_play")]
    n = len(scoring_events)

    for i in range(n):
        start_ev = scoring_events[i]
        start_team_id = start_ev.get("team_id")
        start_side = team_id_to_side.get(start_team_id)
        if start_side not in ("home", "away"):
            continue

        team_a_score = 0
        team_b_score = 0
        max_net = 0
        best_end_ev: Optional[Dict[str, Any]] = None
        best_for = 0
        best_against = 0

        for j in range(i, n):
            ev = scoring_events[j]
            ev_side = team_id_to_side.get(ev.get("team_id"))
            pts = int(ev.get("score_value") or 0)

            if ev_side == start_side:
                team_a_score += pts
            elif ev_side in ("home", "away"):
                team_b_score += pts

            current_net = team_a_score - team_b_score

            if (
                current_net >= min_for
                and team_b_score <= max_against
                and current_net > max_net
            ):
                max_net = current_net
                best_end_ev = ev
                best_for = team_a_score
                best_against = team_b_score

            if team_b_score > max_against:
                break

        if best_end_ev is not None:
            runs.append(
                {
                    "side": start_side,
                    "team_id": start_team_id,
                    "points_for": best_for,
                    "points_against": best_against,
                    "net_points": best_for - best_against,
                    "start_index": start_ev["index"],
                    "end_index": best_end_ev["index"],
                    "start_period": start_ev.get("period"),
                    "start_clock": start_ev.get("clock"),
                    "end_period": best_end_ev.get("period"),
                    "end_clock": best_end_ev.get("clock"),
                }
            )

    return runs


def run_analysis(game_id: Optional[str] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    if not game_id:
        game_id = _get_latest_summary_game_id()
    if not game_id:
        raise SystemExit(
            "No game id provided and no espn_summary_*.json found in fixtures."
        )

    LOG.info("Using game id for analysis: %s", game_id)
    summary = _load_summary(game_id)
    maps = _build_team_maps(summary)
    teams_by_side = maps["teams_by_side"]
    team_id_to_side = maps["team_id_to_side"]
    comp = maps["competition"]

    player_maps = _build_player_maps(summary, team_id_to_side)
    players_by_id = player_maps["players_by_id"]

    plays_seq = _extract_basic_play_sequence(summary)

    # basic points by quarter from plays (unchanged)
    q_points = compute_quarter_team_points(plays_seq, team_id_to_side)

    # full team + player quarter box-style totals (now includes tov)
    quarter_team_totals, quarter_player_totals = compute_quarter_team_and_player_totals(
        plays_seq, team_id_to_side, players_by_id
    )

    # runs
    unanswered_runs = compute_unanswered_runs(
        plays_seq, team_id_to_side, min_points=7
    )
    net_runs = compute_net_runs(plays_seq, team_id_to_side, min_margin=8)
    highlight_runs = compute_highlight_runs(
        plays_seq, team_id_to_side, min_for=8, max_against=5
    )

    # basic meta
    season = summary.get("header", {}).get("season", {}).get("year")
    date_iso = comp.get("date", "")
    game_date = date_iso.split("T")[0] if "T" in date_iso else date_iso

    out_dir = FIXTURES_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    out = {
        "game_id": game_id,
        "date": game_date,
        "season": season,
        "teams": teams_by_side,
        "quarter_team_points": q_points,
        "quarter_team_totals": quarter_team_totals,
        "quarter_player_totals": quarter_player_totals,
        "unanswered_runs": unanswered_runs,
        "net_runs": net_runs,
        "highlight_runs": highlight_runs,
    }

    out_path = out_dir / f"analysis_{game_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    LOG.info("Wrote analysis JSON: %s", out_path)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Lab script: quarter team points + full quarter team/player box "
            "+ 7+ unanswered runs + 8+ net runs + highlight runs "
            "(>=8 pts, opponent <=5)"
        )
    )
    parser.add_argument(
        "--game-id",
        dest="game_id",
        help=(
            "ESPN game id (e.g. 401810084). If omitted, uses latest "
            "espn_summary_*.json in fixtures."
        ),
    )
    args = parser.parse_args(argv)
    run_analysis(args.game_id)


if __name__ == "__main__":
    main()
