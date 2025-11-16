
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


# ----------------- helpers for basic patches -----------------


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
            try:
                summed += int(q_side.get("pts", 0))
            except Exception:
                pass
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
            try:
                q["home_score"] = int(home_tt.get("pts", 0))
            except Exception:
                q["home_score"] = 0
        if not q.get("away_score"):
            try:
                q["away_score"] = int(away_tt.get("pts", 0))
            except Exception:
                q["away_score"] = 0

    data["quarters"] = quarters


def _rebuild_full_game_players_from_quarters(data: Dict[str, Any]) -> None:
    """
    Build full-game player stat lines by aggregating quarter-by-quarter
    player stats. This fills flat stat keys (fg, fga, fg3, ... pts) on
    data["players"].home/away entries so the full-game box is not empty.
    """
    quarters: List[Dict[str, Any]] = data.get("quarters") or []
    players_block = data.get("players") or {"home": [], "away": []}

    stat_keys = ["fg", "fga", "fg3", "fg3a", "ft", "fta",
                 "trb", "ast", "stl", "blk", "tov", "pf", "pts"]

    # Aggregate stats by side + player name
    agg: Dict[str, Dict[str, Dict[str, int]]] = {"home": {}, "away": {}}

    for q in quarters:
        q_players = q.get("players") or {}
        for side in ("home", "away"):
            side_q_players = q_players.get(side) or []
            for p in side_q_players:
                name = p.get("name")
                if not name:
                    continue
                side_bucket = agg[side].setdefault(name, {k: 0 for k in stat_keys})
                for k in stat_keys:
                    try:
                        side_bucket[k] += int(p.get(k, 0))
                    except Exception:
                        # If something is weirdly non-numeric, ignore it
                        pass

    # Now merge aggregated stats into the main players list
    for side in ("home", "away"):
        side_players = players_block.get(side) or []
        new_side_players: List[Dict[str, Any]] = []
        for p in side_players:
            name = p.get("name")
            if not name:
                new_side_players.append(p)
                continue

            stats = agg[side].get(name)
            if stats:
                for k, v in stats.items():
                    p[k] = v
                # Also give a "traditional" dict for templates that expect nested stats
                p["traditional"] = dict(stats)
            else:
                # No stats found (e.g., DNP) — keep as-is, but ensure traditional exists
                p.setdefault("traditional", {k: 0 for k in stat_keys})
            new_side_players.append(p)

        players_block[side] = new_side_players

    data["players"] = players_block


def _recompute_leaders_from_quarters(data: Dict[str, Any]) -> None:
    """
    Recompute leaders (points, rebounds, assists, blocks, steals)
    by aggregating quarter-level player stats.

    This avoids relying on placeholder/full-game player blocks.
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
                for _, key in stat_keys.items():
                    try:
                        val = int(p.get(key, 0))
                    except Exception:
                        val = 0
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


# ----------------- helpers for advanced stats -----------------


def _safe_div(n: float, d: float) -> float:
    return n / d if d else 0.0


def _compute_team_advanced(data: Dict[str, Any]) -> None:
    """
    Compute full-game advanced team stats (off_rating, def_rating,
    net_rating, efg_pct, ts_pct, pace) from game_totals.traditional
    and the quarter team totals (for oreb/dreb).
    """
    game_totals = data.get("game_totals") or {}
    trad = game_totals.get("traditional") or {}
    adv = game_totals.get("advanced") or {}

    quarters: List[Dict[str, Any]] = data.get("quarters") or []

    # First, aggregate offensive rebounds and defensive rebounds from quarters
    oreb_totals = {"home": 0, "away": 0}
    dreb_totals = {"home": 0, "away": 0}

    for q in quarters:
        q_team = (q.get("team_totals") or {}).get("traditional") or {}
        for side in ("home", "away"):
            side_q = q_team.get(side) or {}
            try:
                oreb_totals[side] += int(side_q.get("oreb", 0))
            except Exception:
                pass
            try:
                dreb_totals[side] += int(side_q.get("dreb", 0))
            except Exception:
                pass

    # Basic possessions formula:
    # poss = FGA + 0.44 * FTA - OREB + TOV
    team_poss = {}
    for side, opp in (("home", "away"), ("away", "home")):
        side_trad = trad.get(side) or {}
        fg = int(side_trad.get("fg", 0))
        fga = int(side_trad.get("fga", 0))
        fg3 = int(side_trad.get("fg3", 0))
        ft = int(side_trad.get("ft", 0))
        fta = int(side_trad.get("fta", 0))
        tov = int(side_trad.get("tov", 0))
        pts = int(side_trad.get("pts", 0))
        oreb = oreb_totals.get(side, 0)

        poss = fga + 0.44 * fta - oreb + tov
        team_poss[side] = poss

    # Pace uses both teams' possessions
    poss_home = team_poss.get("home", 0.0)
    poss_away = team_poss.get("away", 0.0)
    if poss_home or poss_away:
        pace = (poss_home + poss_away) / 2.0
    else:
        pace = 0.0

    for side, opp in (("home", "away"), ("away", "home")):
        side_trad = trad.get(side) or {}
        opp_trad = trad.get(opp) or {}

        fg = int(side_trad.get("fg", 0))
        fga = int(side_trad.get("fga", 0))
        fg3 = int(side_trad.get("fg3", 0))
        ft = int(side_trad.get("ft", 0))
        fta = int(side_trad.get("fta", 0))
        pts = int(side_trad.get("pts", 0))

        opp_pts = int(opp_trad.get("pts", 0))

        poss = team_poss.get(side, 0.0)

        efg = _safe_div(fg + 0.5 * fg3, fga)
        ts = _safe_div(pts, 2.0 * (fga + 0.44 * fta))
        off_rating = _safe_div(pts * 100.0, poss)
        def_rating = _safe_div(opp_pts * 100.0, poss)
        net_rating = off_rating - def_rating

        side_adv = adv.get(side, {})
        side_adv.update(
            {
                "off_rating": round(off_rating, 1),
                "def_rating": round(def_rating, 1),
                "net_rating": round(net_rating, 1),
                "efg_pct": round(efg, 3),
                "ts_pct": round(ts, 3),
                "pace": round(pace, 1),
            }
        )
        adv[side] = side_adv

    game_totals["advanced"] = adv
    data["game_totals"] = game_totals


def _compute_quarter_team_advanced(data: Dict[str, Any]) -> None:
    """
    Compute advanced team stats for each quarter from quarter team totals.
    """
    quarters: List[Dict[str, Any]] = data.get("quarters") or []

    for q in quarters:
        team_trad = (q.get("team_totals") or {}).get("traditional") or {}
        team_adv = (q.get("team_totals") or {}).get("advanced") or {}

        new_team_adv = {}

        for side, opp in (("home", "away"), ("away", "home")):
            side_trad = team_trad.get(side) or {}
            opp_trad = team_trad.get(opp) or {}

            fg = int(side_trad.get("fg", 0))
            fga = int(side_trad.get("fga", 0))
            fg3 = int(side_trad.get("fg3", 0))
            ft = int(side_trad.get("ft", 0))
            fta = int(side_trad.get("fta", 0))
            pts = int(side_trad.get("pts", 0))
            oreb = int(side_trad.get("oreb", 0))

            opp_pts = int(opp_trad.get("pts", 0))

            poss = fga + 0.44 * fta - oreb + int(side_trad.get("tov", 0))

            efg = _safe_div(fg + 0.5 * fg3, fga)
            ts = _safe_div(pts, 2.0 * (fga + 0.44 * fta))
            off_rating = _safe_div(pts * 100.0, poss)
            def_rating = _safe_div(opp_pts * 100.0, poss)
            net_rating = off_rating - def_rating

            side_adv = (team_adv.get(side) or {}).copy()
            side_adv.update(
                {
                    "off_rating": round(off_rating, 1),
                    "def_rating": round(def_rating, 1),
                    "net_rating": round(net_rating, 1),
                    "efg_pct": round(efg, 3),
                    "ts_pct": round(ts, 3),
                }
            )
            if side not in new_team_adv:
                new_team_adv[side] = side_adv
            else:
                new_team_adv[side].update(side_adv)

        # Preserve structure: team_totals.advanced.home/away
        existing_tt = q.get("team_totals") or {}
        existing_tt["advanced"] = {
            "home": new_team_adv.get("home", team_adv.get("home", {})),
            "away": new_team_adv.get("away", team_adv.get("away", {})),
        }
        q["team_totals"] = existing_tt

    data["quarters"] = quarters


def _compute_player_advanced(data: Dict[str, Any]) -> None:
    """
    Compute simple advanced stats for full-game players: eFG% and TS%,
    using the aggregated full-game stats we already created.
    """
    players_block = data.get("players") or {"home": [], "away": []}

    for side in ("home", "away"):
        side_players = players_block.get(side) or []
        for p in side_players:
            fg = int(p.get("fg", 0))
            fga = int(p.get("fga", 0))
            fg3 = int(p.get("fg3", 0))
            ft = int(p.get("ft", 0))
            fta = int(p.get("fta", 0))
            pts = int(p.get("pts", 0))

            efg = _safe_div(fg + 0.5 * fg3, fga)
            ts = _safe_div(pts, 2.0 * (fga + 0.44 * fta))

            p["efg_pct"] = round(efg, 3)
            p["ts_pct"] = round(ts, 3)

            # Also mirror into nested advanced dict if present/needed
            adv = p.get("advanced") or {}
            adv.update({"efg_pct": p["efg_pct"], "ts_pct": p["ts_pct"]})
            p["advanced"] = adv

        players_block[side] = side_players

    data["players"] = players_block


# ----------------- patch driver -----------------


def patch_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply fixes and advanced stats on top of the JSON produced by fetch_espn_game:
      - Fill missing game-level team points from quarter totals
      - Fill missing quarter scoreboard (home_score / away_score)
      - Build full-game player stats by aggregating quarter stats
      - Recompute leaders from quarter player stats
      - Compute full-game team advanced metrics
      - Compute quarter-level team advanced metrics
      - Compute player-level eFG% and TS%
    """
    print("[DT Game Report] Patching data (totals, full-game box, leaders, advanced)...")
    _patch_team_points_from_quarters(data)
    _patch_quarter_scores_from_team_totals(data)
    _rebuild_full_game_players_from_quarters(data)
    _recompute_leaders_from_quarters(data)
    _compute_team_advanced(data)
    _compute_quarter_team_advanced(data)
    _compute_player_advanced(data)
    return data


# ----------------- rendering -----------------


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
