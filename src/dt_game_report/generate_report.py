
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape


def get_repo_root() -> Path:
    """
    Resolve the repo root based on this file's location.

    Expected layout:
      repo_root/
        src/dt_game_report/generate_report.py
        fixtures/
        templates/
        output/
    """
    return Path(__file__).resolve().parents[2]


def load_game_data(json_path: Path) -> Dict[str, Any]:
    if not json_path.exists():
        raise FileNotFoundError(f"Could not find game JSON at: {json_path}")
    print(f"[DT Game Report] Loading game JSON: {json_path}")
    with json_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _patch_team_points_from_quarters(data: Dict[str, Any]) -> None:
    """
    If game_totals.traditional.[home/away].pts is zero or missing,
    fill it from the sum of quarter team pts.
    """
    quarters: List[Dict[str, Any]] = data.get("quarters") or []
    game_totals = data.get("game_totals") or {}
    trad = game_totals.get("traditional") or {}

    for side in ("home", "away"):
        side_totals = trad.get(side) or {}
        pts = side_totals.get("pts", 0)
        if pts:
            continue  # already set

        summed = 0
        for q in quarters:
            q_team = (q.get("team_totals") or {}).get("traditional") or {}
            q_side = q_team.get(side) or {}
            summed += int(q_side.get("pts", 0))
        side_totals["pts"] = summed
        trad[side] = side_totals

    game_totals["traditional"] = trad
    data["game_totals"] = game_totals


def _patch_quarter_scores_from_team_totals(data: Dict[str, Any]) -> None:
    """
    If quarter.home_score / away_score are zero, use the quarter team pts.
    """
    quarters: List[Dict[str, Any]] = data.get("quarters") or []

    for q in quarters:
        team_totals = (q.get("team_totals") or {}).get("traditional") or {}
        home_tt = team_totals.get("home") or {}
        away_tt = team_totals.get("away") or {}

        if not q.get("home_score"):
            q["home_score"] = int(home_tt.get("pts", 0))
        if not q.get("away_score"):
            q["away_score"] = int(away_tt.get("pts", 0))

    data["quarters"] = quarters


def _recompute_leaders_from_quarters(data: Dict[str, Any]) -> None:
    """
    Recompute leaders (points, rebounds, assists, blocks, steals)
    by aggregating quarter-level player stats.

    This avoids relying on whatever placeholder/full-game player
    block happens to be in the JSON.
    """
    quarters: List[Dict[str, Any]] = data.get("quarters") or []
    # side -> name -> stats
    agg: Dict[str, Dict[str, Dict[str, int]]] = {"home": {}, "away": {}}
    stat_keys = {
        "points": "pts",
        "rebounds": "trb",
        "assists": "ast",
        "blocks": "blk",
        "steals": "stl",
    }

    for q in quarters:
        players_block = q.get("players") or {}
        for side in ("home", "away"):
            side_players = players_block.get(side) or []
            side_agg = agg[side]
            for p in side_players:
                name = p.get("name")
                if not name:
                    continue
                if name not in side_agg:
                    side_agg[name] = {k: 0 for k in stat_keys.values()}
                for stat_name, key in stat_keys.items():
                    val = int(p.get(key, 0))
                    side_agg[name][key] += val

    def leaders_for_side(side: str) -> Dict[str, Any]:
        side_agg = agg.get(side) or {}
        if not side_agg:
            return {
                "points": {"value": 0, "players": []},
                "rebounds": {"value": 0, "players": []},
                "assists": {"value": 0, "players": []},
                "blocks": {"value": 0, "players": []},
                "steals": {"value": 0, "players": []},
            }

        result: Dict[str, Any] = {}
        for label, key in stat_keys.items():
            max_val = 0
            names: List[str] = []
            for name, stats in side_agg.items():
                val = stats.get(key, 0)
                if val > max_val:
                    max_val = val
                    names = [name]
                elif val == max_val and val > 0:
                    names.append(name)
            result[label] = {"value": max_val, "players": names}
        return result

    new_leaders = {
        "home": leaders_for_side("home"),
        "away": leaders_for_side("away"),
    }
    data["leaders"] = new_leaders


def patch_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply small, safe fixes on top of the JSON produced by fetch_espn_game:
      - Fill missing game-level team points from quarter totals
      - Fill missing quarter scoreboard (home_score / away_score)
      - Recompute leaders from quarter player stats
    """
    print("[DT Game Report] Patching data (team pts, quarter scores, leaders)...")
    _patch_team_points_from_quarters(data)
    _patch_quarter_scores_from_team_totals(data)
    _recompute_leaders_from_quarters(data)
    return data


def get_jinja_env(templates_dir: Path) -> Environment:
    print(f"[DT Game Report] Using templates directory: {templates_dir}")
    loader = FileSystemLoader(str(templates_dir))
    env = Environment(
        loader=loader,
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env


def render_report(env: Environment, data: Dict[str, Any]) -> str:
    template_name = "report.html.jinja"
    print(f"[DT Game Report] Rendering template: {template_name}")
    template = env.get_template(template_name)
    html = template.render(data=data)
    return html


def write_output(html: str, output_dir: Path, game_id: str) -> None:
    output_dir.mkdir(exist_ok=True)
    out_file = output_dir / f"dt_game_report_{game_id}.html"
    index_file = output_dir / "index.html"

    print(f"[DT Game Report] Writing HTML to: {out_file}")
    with out_file.open("w", encoding="utf-8") as f:
        f.write(html)

    # Also drop/overwrite index.html to always show the latest game
    print(f"[DT Game Report] Writing HTML to: {index_file}")
    with index_file.open("w", encoding="utf-8") as f:
        f.write(html)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate DT Game Report HTML from JSON.")
    parser.add_argument(
        "--game-json",
        type=str,
        required=True,
        help="Path to the DT game JSON file (e.g. fixtures/espn_401810077.json).",
    )
    args = parser.parse_args()

    repo_root = get_repo_root()
    print(f"[DT Game Report] Repo root: {repo_root}")

    json_path = (repo_root / args.game_json).resolve()
    fixtures_dir = repo_root / "fixtures"
    templates_dir = repo_root / "templates"
    output_dir = repo_root / "output"

    print(f"[DT Game Report] Game JSON: {json_path}")
    print(f"[DT Game Report] Fixtures dir (for CSV, etc.): {fixtures_dir}")
    print(f"[DT Game Report] Templates dir: {templates_dir}")

    data = load_game_data(json_path)
    data = patch_data(data)

    env = get_jinja_env(templates_dir)
    html = render_report(env, data)

    game_id = str(data.get("meta", {}).get("game_id", "game"))
    write_output(html, output_dir, game_id)

    print("[DT Game Report] Done.")


if __name__ == "__main__":
    main()
