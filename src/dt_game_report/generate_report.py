import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = ROOT / "fixtures"
TEMPLATES_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"
SITE_DIR = ROOT / "site"


def load_game_data() -> dict:
    example_path = FIXTURES_DIR / "example_game.json"
    with example_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def render_report(data: dict) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.jinja")
    html = template.render(data=data)

    out_path = SITE_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Rendered HTML report to {out_path}")


def copy_play_by_play() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    src_csv = FIXTURES_DIR / "play_by_play.csv"
    dst_csv = OUTPUT_DIR / "play_by_play.csv"
    if src_csv.exists():
        shutil.copy2(src_csv, dst_csv)
        print(f"Copied play-by-play CSV to {dst_csv}")
    else:
        print("No fixtures/play_by_play.csv file found; skipping PBP copy.")


def main() -> None:
    print("DT Game Report - fixture run")
    data = load_game_data()
    render_report(data)
    copy_play_by_play()
    print("Done.")


if __name__ == "__main__":
    main()
