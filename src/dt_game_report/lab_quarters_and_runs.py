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
        tid = str(team.get("id"))
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
          'score_value': int,  # points on this play (can be 0),
          'text': str,
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
            }
        )

        last_home = home_score
        last_away = away_score

    return seq


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
    """
    Compute 7+ point unanswered runs by either team.

    Returns list of runs with:
      {
        'side': 'home' or 'away',
        'team_id': 'XXX',
        'points': 10,
        'start_index': 12,
        'end_index': 20,
        'start_period': 2,
        'start_clock': '5:12',
        'end_period': 2,
        'end_clock': '3:01',
      }
    """
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
            # unknown side? just flush any pending run
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
            # other team scored: end previous run, maybe start new
            flush_run(pl)
            current_side = side
            current_team_id = team_id
            current_points = pts
            start_play = pl

    # tail
    if plays_seq:
        flush_run(plays_seq[-1])

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

    plays_seq = _extract_basic_play_sequence(summary)

    # quarter-by-quarter team points from plays
    q_points = compute_quarter_team_points(plays_seq, team_id_to_side)

    # 7+ unanswered runs
    runs = compute_unanswered_runs(plays_seq, team_id_to_side, min_points=7)

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
        "unanswered_runs": runs,
    }

    out_path = out_dir / f"analysis_{game_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    LOG.info("Wrote analysis JSON: %s", out_path)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Lab script: compute quarter team points + 7+ unanswered runs"
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