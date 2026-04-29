"""
send_ntfy.py
Reads the latest ANFR JSON report and sends a push notification via ntfy.sh
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
    new_articles = [a for a in articles if a.get("is_new")]
    new_count = len(new_articles)
    total = len(articles)
    crawled_at = data.get("crawled_at", "")[:10]

    if new_count == 0:
        subject = f"ANFR News Report - {crawled_at}"
        body = f"Total {total} articles checked | No new updates"
        priority = "default"
        tags = "white_check_mark"
    else:
        subject = f"ANFR News Report - {crawled_at} ({new_count} new)"
        preview = "\n".join(
            f"• {(a.get('summary_en') or a['title'])[:120]}"
            for a in new_articles[:3]
        )
        body = f"Total {total} articles | {new_count} new/updated\n\n{preview}"
        priority = "high"
        tags = "newspaper"

    send_notification(subject=subject, body=body, priority=priority, tags=tags)


if __name__ == "__main__":
    main()
