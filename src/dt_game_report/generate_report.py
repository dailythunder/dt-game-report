from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, select_autoescape


def get_repo_root() -> Path:
    """
    Resolve the repo root based on this file's location.

    Expected layout:
      repo_root/
        src/dt_game_report/generate_report.py
        templates/report.html.jinja
        fixtures/example_game.json
    """
    return Path(__file__).resolve().parents[2]


def load_game_data(fixtures_dir: Path) -> Dict[str, Any]:
    """
    Load the example game JSON from fixtures/.

    For now we always use example_game.json.
    Later this can be swapped for a cached real-game file.
    """
    json_path = fixtures_dir / "example_game.json"
    if not json_path.exists():
        raise FileNotFoundError(f"Could not find fixture JSON at: {json_path}")

    print(f"[DT Game Report] Loading fixture JSON: {json_path}")
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Very small sanity checks, just to catch bad edits early.
    required_top_keys = [
        "meta",
        "teams",
        "largest_lead",
        "game_totals",
        "players",
        "quarters",
        "leaders",
        "files",
    ]
    missing = [k for k in required_top_keys if k not in data]
    if missing:
        raise KeyError(f"Fixture JSON is missing keys: {missing}")

    return data


def build_jinja_env(templates_dir: Path) -> Environment:
    """
    Configure a Jinja2 environment pointed at the templates directory.
    """
    print(f"[DT Game Report] Using templates directory: {templates_dir}")
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


def render_report(env: Environment, data: Dict[str, Any]) -> str:
    """
    Render the HTML report from the Jinja template and data dict.
    """
    template_name = "report.html.jinja"
    print(f"[DT Game Report] Rendering template: {template_name}")
    template = env.get_template(template_name)
    html = template.render(data=data)
    return html


def write_output(html: str, repo_root: Path, fixtures_dir: Path, data: Dict[str, Any]) -> None:
    """
    Write the rendered HTML to output/report.html and copy any supporting files
    (like the play-by-play CSV) into the output folder.
    """
    output_dir = repo_root / "output"
    output_dir.mkdir(exist_ok=True)

    html_path = output_dir / "report.html"
    print(f"[DT Game Report] Writing HTML report to: {html_path}")
    html_path.write_text(html, encoding="utf-8")

    # Copy the PBP CSV into the output folder if it exists
    pbp_name = data.get("files", {}).get("play_by_play_csv")
    if pbp_name:
        src_pbp = fixtures_dir / pbp_name
        if src_pbp.exists():
            dst_pbp = output_dir / pbp_name
            print(f"[DT Game Report] Copying play-by-play CSV to: {dst_pbp}")
            dst_pbp.write_bytes(src_pbp.read_bytes())
        else:
            print(f"[DT Game Report] WARNING: PBP CSV referenced but not found: {src_pbp}")
    else:
        print("[DT Game Report] No play-by-play CSV referenced in data.files")

    print("[DT Game Report] Done. Open output/report.html in your browser to view the sample.")


def main() -> None:
    """
    Entry point used by GitHub Actions and local runs.

    For now this is a pure fixture-driven pipeline:
      - load fixtures/example_game.json
      - render templates/report.html.jinja
      - write output/report.html (+ copy PBP CSV)
    """
    repo_root = get_repo_root()
    fixtures_dir = repo_root / "fixtures"
    templates_dir = repo_root / "templates"

    print(f"[DT Game Report] Repo root: {repo_root}")
    print(f"[DT Game Report] Fixtures dir: {fixtures_dir}")
    print(f"[DT Game Report] Templates dir: {templates_dir}")

    data = load_game_data(fixtures_dir)
    env = build_jinja_env(templates_dir)
    html = render_report(env, data)
    write_output(html, repo_root, fixtures_dir, data)


if __name__ == "__main__":
    main()
