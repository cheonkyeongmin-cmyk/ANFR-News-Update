"""
send_ntfy.py
Reads the latest ANFR JSON report and sends a push notification via ntfy.sh
All notification text is sent in English to avoid Korean encoding issues.
"""

import os
import json
import logging
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_BASE = "https://ntfy.sh"


def find_latest_report() -> Path | None:
    reports = sorted(Path("reports").glob("ANFR_Report_????????.json"))
    return reports[-1] if reports else None


def send_notification(subject: str, body: str, priority: str, tags: str):
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC is not set. Skipping notification.")
        return

    url = f"{NTFY_BASE}/{NTFY_TOPIC}"

    try:
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={
                "Title": subject,
                "Priority": priority,
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"ntfy notification sent to topic: {NTFY_TOPIC}")

    except Exception as e:
        log.error(f"Failed to send ntfy notification: {e}")


def make_preview_lines(articles: list[dict]) -> str:
    preview_lines = []

    for idx, a in enumerate(articles, start=1):
        title = a.get("title", "").replace("\n", " ").strip()

        summary = (
            a.get("summary_en")
            or a.get("summary_fr")
            or title
        )
        summary = summary.replace("\n", " ").strip()

        if len(title) > 100:
            title = title[:100] + "..."

        if len(summary) > 180:
            summary = summary[:180] + "..."

        if title and summary and title != summary:
            preview_lines.append(f"{idx}. {title}\n   {summary}")
        else:
            preview_lines.append(f"{idx}. {summary}")

    return "\n\n".join(preview_lines)


def main():
    report_path = find_latest_report()

    if not report_path:
        log.error("No report JSON found in reports/ directory.")
        send_notification(
            subject="ANFR Monitor Error",
            body="Report file not found. Please check crawler logs.",
            priority="high",
            tags="warning",
        )
        return

    log.info(f"Reading report: {report_path}")
    data = json.loads(report_path.read_text(encoding="utf-8"))

    articles = data.get("articles", [])
    total = len(articles)
    crawled_at = data.get("crawled_at", "")[:10]

    changed_articles = [
        a for a in articles
        if a.get("is_new") or a.get("is_updated")
    ]
    changed_count = len(changed_articles)

    if changed_count == 0:
        subject = f"ANFR News Report - {crawled_at}"
        body = f"Total {total} articles checked | No new updates"
        priority = "default"
        tags = "white_check_mark"
    else:
        subject = f"ANFR News Report - {crawled_at} ({changed_count} new/updated)"

        preview = make_preview_lines(changed_articles)

        body = (
            f"Total {total} articles | "
            f"{changed_count} new/updated\n\n"
            f"{preview}"
        )

        priority = "high"
        tags = "newspaper"

    send_notification(
        subject=subject,
        body=body,
        priority=priority,
        tags=tags,
    )


if __name__ == "__main__":
    main()
