import argparse
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional

from dt_game_report.fetch_espn_data import FIXTURES_DIR, fetch_and_cache
from dt_game_report.generate_report import (
    REPORTS_DIR,
    SITE_DIR,
    _build_index,
    _sync_reports_to_site,
    build_data,
    render_report,
)

LOG = logging.getLogger("dt_game_report.auto_report")


def _parse_recipients(value: Optional[str]) -> Iterable[str]:
    if not value:
        return []
    parts = [item.strip() for item in value.split(",")]
    return [item for item in parts if item]


def _write_report_html(game_id: str) -> Path:
    data = build_data(game_id)
    html = render_report(data)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"game_{game_id}.html"
    out_path.write_text(html, encoding="utf-8")
    LOG.info("Wrote HTML report: %s", out_path)

    report_files = sorted(REPORTS_DIR.glob("game_*.html"))
    _sync_reports_to_site(report_files)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    _build_index(report_files)

    return out_path


def _send_email(
    *,
    gmail_user: str,
    gmail_app_password: str,
    recipients: Iterable[str],
    subject: str,
    body: str,
    attachments: Iterable[Path],
) -> None:
    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    for attachment in attachments:
        data = attachment.read_bytes()
        if attachment.suffix == ".html":
            maintype, subtype = "text", "html"
        elif attachment.suffix == ".json":
            maintype, subtype = "application", "json"
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_app_password)
        smtp.send_message(msg)
    LOG.info("Email sent to %s", ", ".join(recipients))


def run(
    *,
    game_id: Optional[str] = None,
    recipients: Optional[str] = None,
    subject: Optional[str] = None,
) -> None:
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient_value = recipients or os.environ.get("EMAIL_TO")
    recipient_list = list(_parse_recipients(recipient_value))

    if not gmail_user or not gmail_app_password:
        raise SystemExit(
            "Missing Gmail credentials. Set GMAIL_USER and GMAIL_APP_PASSWORD env vars."
        )
    if not recipient_list:
        raise SystemExit(
            "No recipients provided. Use --to or set EMAIL_TO (comma-separated)."
        )

    used_game_id = fetch_and_cache(game_id)
    html_path = _write_report_html(used_game_id)
    json_path = FIXTURES_DIR / f"espn_summary_{used_game_id}.json"

    if not json_path.exists():
        raise SystemExit(f"Could not find summary JSON at {json_path}")

    subject_line = subject or f"DT Game Report {used_game_id}"
    body = (
        "Your DT Game Report is attached.\n\n"
        f"Game ID: {used_game_id}\n"
        f"Report: {html_path.name}\n"
        f"Summary JSON: {json_path.name}\n"
    )
    _send_email(
        gmail_user=gmail_user,
        gmail_app_password=gmail_app_password,
        recipients=recipient_list,
        subject=subject_line,
        body=body,
        attachments=[html_path, json_path],
    )


def main(argv: Optional[list] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description=(
            "Fetch latest completed game, generate report, and email it via Gmail."
        )
    )
    parser.add_argument(
        "--game-id",
        dest="game_id",
        help="ESPN game id (e.g. 401810077). If omitted, uses latest completed OKC game.",
    )
    parser.add_argument(
        "--to",
        dest="recipients",
        help="Comma-separated recipient list (defaults to EMAIL_TO env var).",
    )
    parser.add_argument(
        "--subject",
        dest="subject",
        help="Optional email subject line.",
    )
    args = parser.parse_args(argv)
    run(game_id=args.game_id, recipients=args.recipients, subject=args.subject)


if __name__ == "__main__":
    main()
