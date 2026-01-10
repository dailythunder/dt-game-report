# DT Game Report

Static game report generator for Oklahoma City Thunder games.

This test package is wired to use a single fixture (`fixtures/example_game.json`)
so you can preview the HTML layout and GitHub Pages deployment **without**
calling any live NBA / ESPN APIs yet.

Later, you can replace the fixture loader with real data-fetching code.

## Layout

- `fixtures/example_game.json` — sample box score + quarter splits + misc stats
- `fixtures/play_by_play.csv` — placeholder play‑by‑play CSV (just copied to output)
- `templates/report.html.jinja` — Daily Thunder HTML report template
- `src/dt_game_report/generate_report.py` — main script
- `output/` — generated play‑by‑play CSV
- `site/index.html` — generated HTML report (served by GitHub Pages)

## Usage (once you have Python)

```bash
python -m dt_game_report.generate_report
```

This will:

- read `fixtures/example_game.json`
- render `templates/report.html.jinja` to `site/index.html`
- copy `fixtures/play_by_play.csv` to `output/play_by_play.csv`

## Automated email delivery (Gmail)

You can fetch the latest completed game, generate the HTML report, and email both
the report + ESPN summary JSON using the Gmail SMTP server. This requires a Gmail
App Password (recommended; do not use your main account password).

```bash
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="your_app_password"
export EMAIL_TO="you@gmail.com"

python -m dt_game_report.auto_report
```

Optional flags:

```bash
python -m dt_game_report.auto_report --to "you@gmail.com,friend@example.com" --subject "OKC Recap"
```
