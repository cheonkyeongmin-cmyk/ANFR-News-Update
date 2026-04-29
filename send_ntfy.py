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
        # requests encodes headers as latin-1 which breaks Korean.
        # Solution: send as JSON payload instead which supports UTF-8 natively.
        r = requests.post(
            url,
            json={
                "topic": NTFY_TOPIC,
                "title": subject,
                "message": body,
                "priority": {"high": 4, "default": 3}.get(priority, 3),
                "tags": [tags],
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
            subject="ANFR 모니터 오류",
            body="리포트 파일을 찾을 수 없습니다. 크롤러 로그를 확인하세요.",
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
    subject = data.get("email_subject", "ANFR 뉴스 리포트")

    if new_count == 0:
        body = f"총 {total}건 확인 | 신규·업데이트 없음 ✅"
        priority = "default"
        tags = "white_check_mark"
    else:
        # List up to 3 new articles — Korean summary if available, else title
        preview = "\n".join(
            f"• {a.get('summary_ko') or a['title'][:60]}"
            for a in new_articles[:3]
        )
        body = f"총 {total}건 | 신규·업데이트 {new_count}건\n\n{preview}"
        priority = "high"
        tags = "newspaper"

    send_notification(subject=subject, body=body, priority=priority, tags=tags)


if __name__ == "__main__":
    main()
