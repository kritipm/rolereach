import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

import config
import database
import experience_filter
import fix_company_names


def fetch_page_html(page):
    target = config.IIMJOBS_CATEGORY_URL
    if page > 1:
        target = f"{target}?page={page}"

    return fetch_url_html(target)


def fetch_url_html(target):
    api_url = (
        f"{config.SCRAPERAPI_BASE_URL}?api_key={config.SCRAPERAPI_KEY}"
        f"&url={urllib.parse.quote(target, safe='')}&render=true"
    )
    resp = requests.get(api_url, timeout=90)
    resp.raise_for_status()
    return resp.text


def parse_job_cards(html):
    soup = BeautifulSoup(html, "html.parser")
    cards = []

    for card in soup.find_all("div", class_="joblist-card-v2"):
        link = card.find("a", href=True)
        title_el = card.find(attrs={"data-testid": "job_title"})
        experience_el = card.find(attrs={"data-testid": "job_experience"})
        location_el = card.find(attrs={"data-testid": "job_location"})

        if not (link and title_el and experience_el and location_el):
            continue

        title_span = title_el.find("span", class_="joblist__title-text")
        title = (title_span or title_el).get_text(strip=True)

        cards.append(
            {
                "title": title,
                "experience_text": experience_el.get_text(strip=True),
                "location_text": location_el.get_text(strip=True),
                "relative_url": link["href"],
            }
        )

    return cards


def split_company_and_role(title):
    parts = title.split(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None, title.strip()


def is_excluded_title(title):
    lowered = title.lower()
    return any(keyword in lowered for keyword in config.TITLE_EXCLUDE_KEYWORDS)


def is_location_allowed(location_text):
    lowered = location_text.lower()
    return any(keyword in lowered for keyword in config.IIMJOBS_LOCATION_ALLOW_KEYWORDS)


def extract_job_id(relative_url):
    match = re.search(r"-(\d+)(?:\?|$)", relative_url)
    return int(match.group(1)) if match else None


def build_job(card, page):
    company, role = split_company_and_role(card["title"])
    job_id = extract_job_id(card["relative_url"])
    job_url = urllib.parse.urljoin("https://www.iimjobs.com", card["relative_url"].split("?")[0])

    return {
        "comment_id": job_id,
        "thread_id": page,
        "author": company or "unknown",
        "posted_at": "",
        "matched_keyword": card["title"],
        "text": f"{card['title']} | {card['experience_text']} | {card['location_text']}",
        "url": job_url,
        "company_url": None,
        "verified": False,
        "source": "iimjobs",
        "external_id": str(job_id) if job_id else None,
        "experience_range": card["experience_text"].strip() or "Not specified",
    }


def scrape_iimjobs_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        for page in range(1, config.IIMJOBS_PAGES_TO_SCAN + 1):
            try:
                html = fetch_page_html(page)
            except requests.RequestException:
                continue

            cards = parse_job_cards(html)
            if not cards:
                break

            for card in cards:
                if is_excluded_title(card["title"]):
                    continue
                if not is_location_allowed(card["location_text"]):
                    continue
                if not experience_filter.has_product_in_title(card["title"]):
                    continue

                min_years = experience_filter.parse_min_experience(card["experience_text"])
                combined_text = f"{card['title']} {card['experience_text']}"
                if not experience_filter.is_experience_allowed(min_years, combined_text):
                    continue

                record = build_job(card, page)
                if record["comment_id"] is None:
                    continue

                database.save_job(conn, record)
                matches.append(record)

        # Best-effort extra pass targeting fresher/0-1yr roles specifically. Whether
        # iimjobs actually honors this query param server-side is unverified; if it
        # 404s, times out, or just returns the same unfiltered set, duplicates are
        # naturally deduped by INSERT OR IGNORE on comment_id and nothing breaks.
        try:
            html = fetch_url_html(config.IIMJOBS_EXPERIENCE_FILTER_URL)
            cards = parse_job_cards(html)
        except requests.RequestException:
            cards = []

        for card in cards:
            if is_excluded_title(card["title"]):
                continue
            if not is_location_allowed(card["location_text"]):
                continue
            if not experience_filter.has_product_in_title(card["title"]):
                continue

            min_years = experience_filter.parse_min_experience(card["experience_text"])
            combined_text = f"{card['title']} {card['experience_text']}"
            if not experience_filter.is_experience_allowed(min_years, combined_text):
                continue

            record = build_job(card, 0)
            if record["comment_id"] is None:
                continue

            database.save_job(conn, record)
            matches.append(record)

        conn.commit()

    return matches


if __name__ == "__main__":
    database.init_db()
    results = scrape_iimjobs_pm_jobs()
    print(f"iimjobs PM postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']}")
        print(f"  {job['text'].split('|', 1)[1].strip()}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
