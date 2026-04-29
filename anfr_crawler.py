"""
ANFR News Crawler
- Crawls https://www.anfr.fr/liste-actualites
- Detects new/updated articles vs last run
- Summarizes and translates using Google Gemini (free tier)
- Outputs: JSON, TXT, HTML report
"""

import os
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

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
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ANFRMonitor/1.0)"}


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
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("State file updated.")


# ── Crawling ─────────────────────────────────────────────
def crawl_article_list() -> list[dict]:
    """Crawl the news listing page (handles pagination)."""
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

                href = link_tag.get("href", "")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = BASE_URL + href

                title_tag = item.select_one("h2, h3, h4, .title, .titre") or link_tag
                title = title_tag.get_text(strip=True)

                date_tag = item.select_one("time, .date, .published")
                date_str = date_tag.get_text(strip=True) if date_tag else ""

                if title and href:
                    articles.append({"title": title, "url": href, "date": date_str})
            except Exception as e:
                log.warning(f"Error parsing item: {e}")
                continue

        next_link = soup.select_one("a[rel='next'], .pagination .next a, a.next")
        if next_link and next_link.get("href"):
            next_href = next_link["href"]
            url = next_href if next_href.startswith("http") else BASE_URL + next_href
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

    for selector in ["article .content", "article", ".article-body",
                     ".field-body", "main p", ".content-area"]:
        content = soup.select(selector)
        if content:
            text = " ".join(el.get_text(separator=" ", strip=True) for el in content)
            if len(text) > 100:
                return text[:5000]

    paras = soup.select("p")
    return " ".join(p.get_text(strip=True) for p in paras if len(p.get_text(strip=True)) > 40)[:5000]


# ── LLM ──────────────────────────────────────────────────
def call_gemini(prompt: str) -> str:
    """Call Google Gemini API (free tier) and return text response."""
    if not GEMINI_API_KEY:
        return "[GEMINI_API_KEY not set]"

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        log.warning(f"Gemini error: {e}")
        return f"[LLM error: {e}]"


def summarize_and_translate(title: str, full_text: str) -> dict:
    """Return FR summary, EN translation, KO translation."""
    prompt = f"""
You are a news summarizer. Given the following French article, produce exactly three sections.
Article title: {title}
Article text: {full_text[:3000]}

Respond in this exact format (no extra text):
FR_SUMMARY: [3-5 sentence summary in French]
EN_SUMMARY: [3-5 sentence English summary]
KO_SUMMARY: [3-5 sentence Korean summary]
"""
    result = call_gemini(prompt)

    summaries = {"summary_fr": "", "summary_en": "", "summary_ko": ""}
    for line in result.splitlines():
        if line.startswith("FR_SUMMARY:"):
            summaries["summary_fr"] = line.replace("FR_SUMMARY:", "").strip()
        elif line.startswith("EN_SUMMARY:"):
            summaries["summary_en"] = line.replace("EN_SUMMARY:", "").strip()
        elif line.startswith("KO_SUMMARY:"):
            summaries["summary_ko"] = line.replace("KO_SUMMARY:", "").strip()

    return summaries


# ── Report Generation ─────────────────────────────────────
def generate_txt(articles: list[dict], date_str: str, new_count: int) -> str:
    lines = [
        "=" * 60,
        f"  ANFR 규제 뉴스 리포트 | {date_str}",
        f"  총 {len(articles)}건 | 신규·업데이트 {new_count}건",
        "=" * 60,
        "",
    ]
    for a in articles:
        tag = "[신규]" if a.get("is_new") else "[기존]"
        lines += [
            f"{tag} {a['title']}",
            f"날짜: {a.get('date', 'N/A')} | {a['url']}",
            f"▶ 원문 요약 (FR): {a.get('summary_fr', 'N/A')}",
            f"▶ English Summary: {a.get('summary_en', 'N/A')}",
            f"▶ 한국어 요약: {a.get('summary_ko', 'N/A')}",
            "-" * 60,
            "",
        ]
    return "\n".join(lines)


def generate_html(articles: list[dict], date_str: str, new_count: int) -> str:
    cards = ""
    for a in articles:
        badge = '<span style="background:#FFD700;color:#333;padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px;">🆕 NEW</span>' if a.get("is_new") else ""
        cards += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:20px;margin-bottom:16px;">
            <div style="margin-bottom:8px;">
                <a href="{a['url']}" style="font-size:16px;font-weight:bold;color:#1a1a2e;text-decoration:none;">{a['title']}</a>
                {badge}
            </div>
            <div style="color:#888;font-size:12px;margin-bottom:12px;">{a.get('date','')}</div>
            <div style="font-size:15px;color:#222;margin-bottom:8px;line-height:1.6;">
                🇰🇷 {a.get('summary_ko','N/A')}
            </div>
            <div style="font-size:13px;color:#666;margin-bottom:10px;line-height:1.5;">
                🇬🇧 {a.get('summary_en','N/A')}
            </div>
            <a href="{a['url']}" style="font-size:12px;color:#0066cc;">원문 보기 →</a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:24px;max-width:800px;margin:auto;">
    <div style="background:#1a1a2e;color:#fff;padding:24px;border-radius:8px;margin-bottom:20px;">
        <h1 style="margin:0;font-size:20px;">📡 ANFR 규제 뉴스 리포트</h1>
        <p style="margin:8px 0 0;opacity:0.8;">{date_str}</p>
    </div>
    <div style="background:#fff;border-radius:8px;padding:16px;margin-bottom:20px;display:flex;gap:16px;">
        <div style="text-align:center;flex:1;">
            <div style="font-size:28px;font-weight:bold;color:#1a1a2e;">{len(articles)}</div>
            <div style="color:#666;font-size:13px;">총 기사</div>
        </div>
        <div style="text-align:center;flex:1;">
            <div style="font-size:28px;font-weight:bold;color:#e74c3c;">{new_count}</div>
            <div style="color:#666;font-size:13px;">신규·업데이트</div>
        </div>
    </div>
    {cards}
    <div style="text-align:center;color:#aaa;font-size:11px;margin-top:24px;">
        본 리포트는 ANFR 사이트를 자동 크롤링하여 생성되었습니다.
    </div>
</body></html>"""


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
        a["is_new"] = a["url"] not in state
        log.info(f"{'[NEW]' if a['is_new'] else '[OLD]'} {a['title'][:60]}")

    new_count = sum(1 for a in articles if a["is_new"])
    log.info(f"New articles: {new_count} / {len(articles)}")

    for i, a in enumerate(articles):
        log.info(f"Processing article {i+1}/{len(articles)}: {a['title'][:50]}")
        a["full_text_fr"] = crawl_article_body(a["url"])
        time.sleep(1.2)

        if dry_run:
            a["summary_fr"] = "[dry-run]"
            a["summary_en"] = "[dry-run]"
            a["summary_ko"] = "[dry-run]"
        else:
            summaries = summarize_and_translate(a["title"], a["full_text_fr"])
            a.update(summaries)

    email_subject = f"ANFR 규제 뉴스 리포트 - {date_label} (신규 {new_count}건)"
    txt_content = generate_txt(articles, date_label, new_count)
    html_content = generate_html(articles, date_label, new_count)

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
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info(f"Reports saved: {txt_path}, {html_path}, {json_path}")

    new_state = {a["url"]: a.get("date", "") for a in articles}
    save_state(new_state)

    log.info("=== ANFR Crawler Done ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM calls")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
