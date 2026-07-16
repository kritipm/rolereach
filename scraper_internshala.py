import urllib.parse

import requests
from bs4 import BeautifulSoup

import config
import database
import experience_filter
import fix_company_names
from scraper_google_jobs import find_company_website

BASE_URL = "https://internshala.com"


def fetch_listing_html(url):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def parse_job_cards(html):
    soup = BeautifulSoup(html, "html.parser")
    cards = []

    for card in soup.find_all(class_="individual_internship"):
        title_el = card.select_one("a.job-title-href")
        if not title_el:
            continue

        company_el = card.select_one("p.company-name")
        location_el = card.select_one("p.row-1-item.locations")

        experience_text = ""
        for row in card.select("div.row-1-item"):
            if row.find("i", class_="ic-16-briefcase"):
                span = row.find("span")
                experience_text = span.get_text(strip=True) if span else ""
                break

        cards.append(
            {
                "title": title_el.get_text(strip=True),
                "relative_url": title_el.get("href"),
                "company": company_el.get_text(strip=True) if company_el else "unknown",
                "location_text": location_el.get_text(" ", strip=True) if location_el else "",
                "experience_text": experience_text,
            }
        )

    return cards


def is_excluded_title(title):
    lowered = title.lower()
    return any(keyword in lowered for keyword in config.TITLE_EXCLUDE_KEYWORDS)


def is_location_allowed(location_text):
    """Internshala is an India-focused platform — a bare city name (Hyderabad, Delhi,
    etc.) is implicitly India-based. Only reject if an explicit foreign marker is present."""
    lowered = location_text.lower()
    return not any(marker in lowered for marker in config.CUTSHORT_FOREIGN_LOCATION_MARKERS)


def extract_job_id(relative_url):
    digits = "".join(ch for ch in relative_url if ch.isdigit())
    return int(digits[-15:]) if digits else None


def build_job(card, job_id):
    job_url = urllib.parse.urljoin(BASE_URL, card["relative_url"])

    return {
        "comment_id": job_id,
        "thread_id": 0,
        "author": card["company"],
        "posted_at": "",
        "matched_keyword": card["title"],
        "text": f"{card['title']} | {card['experience_text']} | {card['location_text']}",
        "url": job_url,
        "company_url": find_company_website(card["company"]),
        "verified": False,
        "source": "internshala",
        "external_id": str(job_id),
        "experience_range": card["experience_text"].strip() or "Not specified",
    }


def scrape_internshala_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        for url in config.INTERNSHALA_URLS:
            try:
                html = fetch_listing_html(url)
            except requests.RequestException:
                continue

            for card in parse_job_cards(html):
                if not card["relative_url"]:
                    continue
                if "internship/detail" in card["relative_url"]:
                    continue
                if is_excluded_title(card["title"]):
                    continue
                if not is_location_allowed(card["location_text"]):
                    continue
                if not experience_filter.has_product_in_title(card["title"]):
                    continue

                combined_text = f"{card['title']} {card['experience_text']}"
                min_years = experience_filter.parse_min_experience(combined_text)
                if not experience_filter.is_experience_allowed(min_years, combined_text):
                    continue

                job_id = extract_job_id(card["relative_url"])
                if job_id is None:
                    continue

                record = build_job(card, job_id)
                database.save_job(conn, record)
                matches.append(record)

        conn.commit()

    return matches


if __name__ == "__main__":
    database.init_db()
    results = scrape_internshala_pm_jobs()
    print(f"Internshala PM postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']} | {job['experience_range']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
