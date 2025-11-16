import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import requests

LOG = logging.getLogger("dt_game_report.fetch_espn")


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


TEAM_ABBR = "okc"  # Thunder
TEAM_ESPN_ID = "25"


def http_get_json(url: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    LOG.info("GET %s", url)
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def find_latest_okc_game_id() -> str:
    """Return the most recent *completed* Thunder game id from ESPN."""
    # ESPN team schedule endpoint; includes past and future games
    url = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/teams/{team}/schedule".format(
        team=TEAM_ABBR
    )
    data = http_get_json(url)
    events: List[Dict[str, Any]] = data.get("events", [])
    if not events:
        raise RuntimeError("No events returned from ESPN schedule endpoint")

    completed: List[Tuple[str, str]] = []
    for ev in events:
        # game id
        game_id = ev.get("id")
        if not game_id and "uid" in ev:
            # uid like 's:40~l:46~e:401810077'
            uid = str(ev["uid"])
            if "e:" in uid:
                game_id = uid.split("e:")[-1]
        if not game_id:
            continue

        status = ev.get("status", {})
        stype = status.get("type", {})
        if not stype.get("completed"):
            continue

        date_str = ev.get("date")
        if not date_str:
            continue
        completed.append((game_id, date_str))

    if not completed:
        raise RuntimeError("No completed Thunder games found in schedule data")

    # sort by date string (ISO-8601) descending
    completed.sort(key=lambda tup: tup[1], reverse=True)
    latest_id, latest_date = completed[0]
    LOG.info("Latest completed OKC game from ESPN schedule: %s (date=%s)", latest_id, latest_date)
    return latest_id


def fetch_espn_summary(game_id: str) -> Dict[str, Any]:
    """Fetch ESPN 'summary' JSON for a game id.

    This includes box score, leaders, win probability, *and* a flat list of plays.
    """
    url = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/summary"
    params = {"event": game_id}
    return http_get_json(url, params=params)


def save_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    LOG.info("Wrote JSON: %s", path)


def plays_to_csv_rows(plays: List[Dict[str, Any]]) -> List[List[Any]]:
    """Convert ESPN 'plays' list into simple CSV rows.

    We keep this intentionally flat and defensive so small API changes don't break it.
    """
    header = [
        "play_id",
        "sequence",
        "period",
        "clock",
        "type_id",
        "type_text",
        "description",
        "short_description",
        "team_id",
        "home_score",
        "away_score",
        "score_value",
        "scoring_play",
        "shooting_play",
        "points_attempted",
        "wallclock",
    ]

    rows: List[List[Any]] = [header]
    for p in plays:
        type_obj = p.get("type", {}) or {}
        period = p.get("period", {}) or {}
        clock = p.get("clock", {}) or {}
        team = p.get("team", {}) or {}

        rows.append(
            [
                p.get("id"),
                p.get("sequenceNumber"),
                period.get("number"),
                clock.get("displayValue"),
                type_obj.get("id"),
                type_obj.get("text"),
                p.get("text"),
                p.get("shortDescription"),
                team.get("id"),
                p.get("homeScore"),
                p.get("awayScore"),
                p.get("scoreValue"),
                p.get("scoringPlay"),
                p.get("shootingPlay"),
                p.get("pointsAttempted"),
                p.get("wallclock"),
            ]
        )

    return rows


def write_csv(rows: List[List[Any]], path: Path) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    LOG.info("Wrote CSV: %s", path)


def fetch_and_cache(game_id: Optional[str] = None) -> str:
    """Fetch summary + plays for a game and cache to fixtures.

    Returns the game_id actually used.
    """
    if not game_id:
        LOG.info("No game id provided; looking up latest completed Thunder game on ESPN")
        game_id = find_latest_okc_game_id()
    else:
        LOG.info("Using explicit game id: %s", game_id)

    summary = fetch_espn_summary(game_id)

    # Save the raw summary JSON (includes box score, leaders, plays, etc.)
    summary_path = FIXTURES_DIR / f"espn_summary_{game_id}.json"
    save_json(summary, summary_path)

    # Extract plays and write them to a simple CSV (for AI / analysis use)
    plays = summary.get("plays", [])
    if isinstance(plays, list) and plays:
        csv_rows = plays_to_csv_rows(plays)
        csv_path = FIXTURES_DIR / f"espn_pbp_{game_id}.csv"
        write_csv(csv_rows, csv_path)
    else:
        LOG.warning("No play-by-play data found in ESPN summary JSON for game %s", game_id)

    return game_id


def main(argv: Optional[list] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fetch ESPN summary + PBP for a Thunder game")
    parser.add_argument(
        "--game-id",
        dest="game_id",
        help="ESPN game id (e.g. 401810077). If omitted, uses the last completed OKC game.",
    )
    args = parser.parse_args(argv)

    used_id = fetch_and_cache(args.game_id)
    LOG.info("Done. Cached data for game id %s in %s", used_id, FIXTURES_DIR)


if __name__ == "__main__":
    main()
