"""
ANFR News Crawler
- Crawls https://www.anfr.fr/liste-actualites
- Detects new/updated articles vs last run
- Summarizes and translates using Google Gemini
- Outputs: JSON, TXT, HTML report
"""

import os
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
import html

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────
BASE_URL = "https://www.anfr.fr"
LIST_URL = "https://www.anfr.fr/liste-actualites"
STATE_FILE = Path("last_crawl_state.json")
REPORTS_DIR = Path("reports")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ANFRMonitor/1.0)"
}


# ── Helpers ──────────────────────────────────────────────
def get_page(url: str, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(1, retries + 1):
        try:
            log.info(f"Fetching: {url} (attempt {attempt})")
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Error fetching {url}: {e}")
            if attempt < retries:
                time.sleep(3)

    log.error(f"Failed to fetch: {url}")
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Failed to read state file: {e}")
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("State file updated.")


def normalize_url(href: str) -> str:
    if not href:
        return ""

    if href.startswith("http"):
        return href

    if href.startswith("/"):
        return BASE_URL + href

    return BASE_URL + "/" + href


# ── Crawling ─────────────────────────────────────────────
def crawl_article_list() -> list[dict]:
    """Crawl the news listing page."""
    articles = []
    url = LIST_URL

    while url:
        soup = get_page(url)
        if not soup:
            break

        items = soup.select("article, .news-item, .actualite-item, li.item")

        if not items:
            items = soup.select("a[href*='actualite'], a[href*='news']")

        log.info(f"Found {len(items)} items on page")

        for item in items:
            try:
                link_tag = item.select_one("a[href]") if item.name != "a" else item
                if not link_tag:
                    continue

                href = normalize_url(link_tag.get("href", ""))
                if not href:
                    continue

                if "/actualite/" not in href and "actualite" not in href:
                    continue

                title_tag = item.select_one("h1, h2, h3, h4, .title, .titre") or link_tag
                title = title_tag.get_text(" ", strip=True)

                date_tag = item.select_one("time, .date, .published")
                date_str = date_tag.get_text(" ", strip=True) if date_tag else ""

                if title and href:
                    articles.append(
                        {
                            "title": title,
                            "url": href,
                            "date": date_str,
                        }
                    )

            except Exception as e:
                log.warning(f"Error parsing item: {e}")
                continue

        next_link = soup.select_one("a[rel='next'], .pagination .next a, a.next")
        if next_link and next_link.get("href"):
            url = normalize_url(next_link["href"])
            time.sleep(1.5)
        else:
            url = None

    seen = set()
    unique = []

    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)

    log.info(f"Total unique articles found: {len(unique)}")
    return unique


def crawl_article_body(url: str) -> str:
    """Fetch full text of a single article."""
    soup = get_page(url)
    if not soup:
        return ""

    selectors = [
        "article .content",
        "article",
        ".article-body",
        ".field-body",
        "main p",
        ".content-area",
    ]

    for selector in selectors:
        content = soup.select(selector)
        if content:
            text = " ".join(
                el.get_text(" ", strip=True)
                for el in content
            )
            if len(text) > 100:
                return text[:5000]

    paras = soup.select("p")
    return " ".join(
        p.get_text(" ", strip=True)
        for p in paras
        if len(p.get_text(" ", strip=True)) > 40
    )[:5000]


# ── LLM ──────────────────────────────────────────────────
def call_gemini(prompt: str) -> str:
    """Call Google Gemini API and return text response."""
    if not GEMINI_API_KEY:
        return "[GEMINI_API_KEY not set]"

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        return response.text.strip() if response.text else ""

    except Exception as e:
        log.warning(f"Gemini error: {e}")
        return f"[LLM error: {e}]"


def extract_section(text: str, marker: str) -> str:
    """
    Robustly extract a section from Gemini response.
    Handles multi-line responses, bold markers, and variations in spacing/casing.
    """
    import re

    pattern = re.compile(
        rf"\*{{0,2}}{re.escape(marker)}\*{{0,2}}\s*:?\s*(.*?)(?=\*{{0,2}}(?:FR_SUMMARY|EN_SUMMARY|KO_SUMMARY)\*{{0,2}}\s*:|$)",
        re.IGNORECASE | re.DOTALL,
    )

    match = pattern.search(text)
    if match:
        return match.group(1).strip().replace("\n", " ")

    return ""


def summarize_and_translate(title: str, full_text: str) -> dict:
    """Return FR summary, EN translation, KO translation."""
    if not full_text.strip():
        return {
            "summary_fr": "[본문 추출 실패]",
            "summary_en": "[Article body extraction failed]",
            "summary_ko": "[기사 본문 추출 실패]",
        }

    prompt = f"""You are a news summarizer. Summarize the following French article in 2-3 sentences each.

Article title: {title}
Article text: {full_text[:3000]}

You MUST respond using EXACTLY this format with these exact labels on separate lines:
FR_SUMMARY: <2-3 sentence summary in French>
EN_SUMMARY: <2-3 sentence summary in English>
KO_SUMMARY: <2-3 sentence summary in Korean>

Do not add any other text, headers, or formatting. Start your response directly with FR_SUMMARY:"""

    result = call_gemini(prompt)
    log.debug(f"Gemini raw response: {result[:300]}")

    summaries = {
        "summary_fr": extract_section(result, "FR_SUMMARY"),
        "summary_en": extract_section(result, "EN_SUMMARY"),
        "summary_ko": extract_section(result, "KO_SUMMARY"),
    }

    for key, val in summaries.items():
        if not val:
            log.warning(f"Parsing failed for {key}. Raw response snippet: {result[:200]}")
            summaries[key] = result[:500] if result else "[Gemini response empty]"

    return summaries


# ── Report Generation ─────────────────────────────────────
def generate_txt(articles: list[dict], date_str: str, changed_count: int) -> str:
    lines = [
        "=" * 60,
        f"  ANFR 규제 뉴스 리포트 | {date_str}",
        f"  총 {len(articles)}건 | 신규·업데이트 {changed_count}건",
        "=" * 60,
        "",
    ]

    for a in articles:
        if a.get("is_new"):
            tag = "[신규]"
        elif a.get("is_updated"):
            tag = "[업데이트]"
        else:
            tag = "[기존]"

        lines += [
            f"{tag} {a.get('title', '')}",
            f"날짜: {a.get('date', 'N/A')} | {a.get('url', '')}",
            f"▶ 원문 요약 (FR): {a.get('summary_fr', 'N/A')}",
            f"▶ English Summary: {a.get('summary_en', 'N/A')}",
            f"▶ 한국어 요약: {a.get('summary_ko', 'N/A')}",
            "-" * 60,
            "",
        ]

    return "\n".join(lines)


def generate_html(articles: list[dict], date_str: str, changed_count: int) -> str:
    cards = ""

    for a in articles:
        title = html.escape(a.get("title", ""))
        url = html.escape(a.get("url", ""))
        date = html.escape(a.get("date", ""))
        summary_ko = html.escape(a.get("summary_ko", "N/A"))
        summary_en = html.escape(a.get("summary_en", "N/A"))

        if a.get("is_new"):
            badge = '<span style="background:#FFD700;color:#333;padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px;">🆕 NEW</span>'
        elif a.get("is_updated"):
            badge = '<span style="background:#87CEEB;color:#333;padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px;">🔄 UPDATED</span>'
        else:
            badge = ""

        cards += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:20px;margin-bottom:16px;">
            <div style="margin-bottom:8px;">
                <a href="{url}" style="font-size:16px;font-weight:bold;color:#1a1a2e;text-decoration:none;">{title}</a>
                {badge}
            </div>
            <div style="color:#888;font-size:12px;margin-bottom:12px;">{date}</div>
            <div style="font-size:15px;color:#222;margin-bottom:8px;line-height:1.6;">
                🇰🇷 {summary_ko}
            </div>
            <div style="font-size:13px;color:#666;margin-bottom:10px;line-height:1.5;">
                🇬🇧 {summary_en}
            </div>
            <a href="{url}" style="font-size:12px;color:#0066cc;">원문 보기 →</a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
</head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:24px;max-width:800px;margin:auto;">
    <div style="background:#1a1a2e;color:#fff;padding:24px;border-radius:8px;margin-bottom:20px;">
        <h1 style="margin:0;font-size:20px;">📡 ANFR 규제 뉴스 리포트</h1>
        <p style="margin:8px 0 0;opacity:0.8;">{html.escape(date_str)}</p>
    </div>
    <div style="background:#fff;border-radius:8px;padding:16px;margin-bottom:20px;display:flex;gap:16px;">
        <div style="text-align:center;flex:1;">
            <div style="font-size:28px;font-weight:bold;color:#1a1a2e;">{len(articles)}</div>
            <div style="color:#666;font-size:13px;">총 기사</div>
        </div>
        <div style="text-align:center;flex:1;">
            <div style="font-size:28px;font-weight:bold;color:#e74c3c;">{changed_count}</div>
            <div style="color:#666;font-size:13px;">신규·업데이트</div>
        </div>
    </div>
    {cards}
    <div style="text-align:center;color:#aaa;font-size:11px;margin-top:24px;">
        본 리포트는 ANFR 사이트를 자동 크롤링하여 생성되었습니다.
    </div>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────
def main(dry_run: bool = False):
    REPORTS_DIR.mkdir(exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")
    date_label = datetime.now().strftime("%Y년 %m월 %d일")

    log.info("=== ANFR Crawler Start ===")

    articles = crawl_article_list()
    if not articles:
        log.warning("No articles found. Exiting.")
        return

    state = load_state()

    for a in articles:
        url = a["url"]
        current_date = a.get("date", "")
        current_title = a.get("title", "")

        old = state.get(url)

        if old is None:
            a["is_new"] = True
            a["is_updated"] = False
        elif isinstance(old, dict):
            a["is_new"] = False
            a["is_updated"] = (
                old.get("date", "") != current_date
                or old.get("title", "") != current_title
            )
        else:
            # Backward compatibility with old state format: {url: date}
            a["is_new"] = False
            a["is_updated"] = old != current_date

        status = "NEW" if a["is_new"] else "UPDATED" if a["is_updated"] else "OLD"
        log.info(f"[{status}] {a['title'][:60]}")

    changed_count = sum(
        1 for a in articles
        if a.get("is_new") or a.get("is_updated")
    )

    log.info(f"New/updated articles: {changed_count} / {len(articles)}")

    for i, a in enumerate(articles):
        log.info(f"Processing article {i + 1}/{len(articles)}: {a['title'][:50]}")

        a["full_text_fr"] = crawl_article_body(a["url"])
        time.sleep(1.2)

        if dry_run:
            a["summary_fr"] = "[dry-run]"
            a["summary_en"] = "[dry-run]"
            a["summary_ko"] = "[dry-run]"
        else:
            summaries = summarize_and_translate(a["title"], a["full_text_fr"])
            a.update(summaries)

    email_subject = f"ANFR 규제 뉴스 리포트 - {date_label} (신규·업데이트 {changed_count}건)"

    txt_content = generate_txt(articles, date_label, changed_count)
    html_content = generate_html(articles, date_label, changed_count)

    txt_path = REPORTS_DIR / f"ANFR_Report_{today}.txt"
    html_path = REPORTS_DIR / f"ANFR_Report_{today}_email.html"
    json_path = REPORTS_DIR / f"ANFR_Report_{today}.json"

    txt_path.write_text(txt_content, encoding="utf-8")
    html_path.write_text(html_content, encoding="utf-8")

    json_data = {
        "crawled_at": datetime.now().isoformat(),
        "email_subject": email_subject,
        "email_body_html": html_content,
        "articles": [
            {k: v for k, v in a.items() if k != "full_text_fr"}
            for a in articles
        ],
    }

    json_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log.info(f"Reports saved: {txt_path}, {html_path}, {json_path}")

    new_state = {
        a["url"]: {
            "title": a.get("title", ""),
            "date": a.get("date", ""),
            "last_seen": datetime.now().isoformat(),
        }
        for a in articles
    }

    save_state(new_state)

    log.info("=== ANFR Crawler Done ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM calls")
    args = parser.parse_args()

    main(dry_run=args.dry_run)
